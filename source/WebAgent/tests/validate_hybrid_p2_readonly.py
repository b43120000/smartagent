"""Offline isolated Hybrid Writer + P2 metadata/collector regression."""
import hashlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "source"))
from agent_core import composer_operation as composer
from agent_core import sr_first_readback as sr
from agent_core.web_runtime import WebLLMScraper
from WebAgent.tests import collect_p2_readonly_evidence as collector


def _simulate_writer(prompt, *, hybrid=True):
    typed = [""]
    steps = []
    class Keyboard:
        def insert_text(self, value):
            assert "\r" not in value and "\n" not in value
            steps.append(("text", len(value)))
            typed[0] += value
        def press(self, key):
            steps.append(("key", key))
            if key == "Shift+Enter":
                typed[0] += "\n"
            else:
                assert key == "Control+End"
    class Scraper:
        _page = SimpleNamespace(keyboard=Keyboard())
        _last_composer_validation = None
        def _composer_matches_prompt(self, value):
            steps.append(("read", len(value)))
            return typed[0] == value
        def _normalize_composer_text(self, value):
            return value.replace("\r\n", "\n")
        def _focus_composer_dom(self):
            return None
        def _check_cancel_requested(self, stage):
            assert stage == "composer_chunk"
        def _log_stage(self, *args):
            return
    # The real editor normalizes CRLF, use LF to model typed text.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "writer.json"
        state = {}
        with patch("agent_core.paths.log_root", return_value=Path(tmp)):
            if hybrid:
                composer._write_hybrid_prompt(Scraper(), prompt, state, path)
            else:
                composer._write_staged_prompt(Scraper(), prompt, state, path)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        if hybrid:
            evidence = Path(tmp) / "p2-readonly" / "hybrid_writer.jsonl"
            row = json.loads(evidence.read_text(encoding="utf-8").splitlines()[-1])
            assert row["mode"] == "ONE_SHOT_PREFIX512_BULK_TAIL"
            assert row["prefix_chars"] == min(512, len(prompt))
        assert typed[0] == prompt
        assert persisted["confirmed_prefix"]["chars"] == len(prompt)
        assert persisted["confirmed_prefix"]["sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    return steps


def test_hybrid_writer():
    multiline = "[WEBUI_SUBMIT_ID]\nWUS-" + "A"*32 + "\n[/WEBUI_SUBMIT_ID]\n"
    multiline += ("Line A " + "Q"*32 + "\n")*85 + "END"
    assert len(multiline) > 1024
    hybrid = _simulate_writer(multiline)
    protected = _simulate_writer(multiline, hybrid=False)
    assert len([s for s in hybrid if s[0] == "read"]) < len([s for s in protected if s[0] == "read"])
    assert ("key", "Shift+Enter") in hybrid
    longline = "S"*5120
    prefix = _simulate_writer(longline)
    assert ("text", 512) in prefix
    assert max(n for kind,n in prefix if kind == "text") <= 4096
    assert sum(n for kind,n in prefix if kind == "text") == len(longline)
    assert len([s for s in prefix if s[0] == "read"]) <= 5
    # The 512-char boundary is independently checked in the writer log.
    crlf_split = "A"*511 + "\n" + "B"*650
    _simulate_writer(crlf_split)
    # If boundary intersects CRLF, the CRLF is atomic in the tail.
    # Text is validated by the normalized readback in Live, not raw insertion.

    # A failed tail must fail-closed, not append/replay a second time.
    class FailedKeyboard:
        def __init__(self): self.typed=""
        def insert_text(self, value): self.typed += value[:1]
        def press(self, key): self.typed += "\n" if key == "Shift+Enter" else ""
    keyboard=FailedKeyboard()
    class FailScraper:
        _page=SimpleNamespace(keyboard=keyboard)
        def _composer_matches_prompt(self, text):return keyboard.typed == text
        def _normalize_composer_text(self, text):return text
        def _focus_composer_dom(self):return None
        def _check_cancel_requested(self, stage):return None
        def _log_stage(self,*args):return None
    with tempfile.TemporaryDirectory() as tmp:
        with patch.object(composer.time, "monotonic", side_effect=[0.0] + [0.0,3.0]*50):
            try:
                composer._write_hybrid_prompt(FailScraper(), "A"*1100, {}, Path(tmp)/"journal.json")
            except composer.ComposerOperationError as error:
                assert "composer_chunk_readback_failed" in str(error)
            else:
                raise AssertionError("failed writer was accepted")



def test_hybrid_task_scope():
    # Verify the actual Scraper dispatcher, not just the lower-level writer.
    scraper = WebLLMScraper.__new__(WebLLMScraper)
    scraper.service = "chatgpt"
    scraper._isolated_browser = None
    scraper._page = SimpleNamespace(url="https://chatgpt.com/c/fixture")
    scraper._composer_hybrid_task_enabled = False
    scraper.get_cdp_endpoint = lambda: "http://127.0.0.1:9222"
    scraper._check_cancel_requested = lambda stage: None
    scraper._log_stage = lambda *args: None
    prompt = "P" * 1500
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"SMARTAGENT_COMPOSER_HYBRID_ONCE_V1": "1"}), \
             patch("agent_core.composer_operation.prepare_composer",
                   return_value={"operation_id": "TEST_OP"}) as prepare, \
             patch("agent_core.paths.runtime_root", return_value=Path(tmp)):
            # Session Bootstrap / Attach: normal protected writer.
            assert scraper._write_prompt_to_composer(prompt)
            assert prepare.call_args.kwargs["hybrid_once"] is False
            assert not getattr(scraper, "_composer_hybrid_once_used", False)
            # Controller READY, then real task starts.
            scraper._composer_hybrid_task_enabled = True
            assert scraper._write_prompt_to_composer(prompt)
            assert prepare.call_args.kwargs["hybrid_once"] is True
            assert scraper._composer_hybrid_once_used is True
            # Second Planner round does not consume/rearm Hybrid.
            assert scraper._write_prompt_to_composer(prompt)
            assert prepare.call_args.kwargs["hybrid_once"] is False
            # Final ACK outside task.
            scraper._composer_hybrid_task_enabled = False
            assert scraper._write_prompt_to_composer(prompt)
            assert prepare.call_args.kwargs["hybrid_once"] is False
            assert prepare.call_count == 4
    # A failure still consumes the grant: never automatic hybrid replay.
    scraper._composer_hybrid_once_used = False
    scraper._composer_hybrid_task_enabled = True
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"SMARTAGENT_COMPOSER_HYBRID_ONCE_V1": "1"}), \
             patch("agent_core.composer_operation.prepare_composer",
                   side_effect=RuntimeError("writer failed")), \
             patch("agent_core.paths.runtime_root", return_value=Path(tmp)):
            try:
                scraper._write_prompt_to_composer(prompt)
            except Exception:
                pass
            else:
                raise AssertionError("expected writer failure")
            assert scraper._composer_hybrid_once_used is True


def test_latest_controller_run_scope():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        logdir = root / "localdata" / "logs"
        (logdir / "p2-readonly").mkdir(parents=True)
        now = datetime.now(timezone.utc)
        fmt = lambda dt: dt.isoformat()
        old = fmt(now - timedelta(hours=2))
        start = fmt(now - timedelta(minutes=1))
        fresh = fmt(now - timedelta(seconds=15))
        def rec(t, event, **kw):
            return {"timestamp": t, "event": event, **kw}
        all_events = [
            rec(old, "controller_started"),
            rec(old, "request_failed", request_id="WA-OLD", error="PRIVATE_OLD_FAILURE"),
            rec(start, "controller_started"),
            rec(start, "browser_send_started", stage="協議初始化", response="PRIVATE_BOOTSTRAP"),
            rec(start, "browser_response_completed", stage="協議初始化", response="PRIVATE_OUTPUT"),
            rec(start, "controller_ready", session_mode="BOOTSTRAP"),
            rec(fresh, "p2_request_ingress", source_kind="WEB_UI",
                request_sha256=hashlib.sha256(b"private-request").hexdigest(),
                request_chars=15, hybrid_eligible=True, request="PRIVATE_BODY"),
            rec(fresh, "browser_send_started", stage="smartagent_tool 規劃回合"),
            rec(fresh, "request_started", request_id="WA-FRESH", request="PRIVATE_REQUEST"),
            rec(fresh, "protocol_rejected", request_id="WA-FRESH", round=1,
                diagnostics=[{"reason": "missing_smartagent_tool_envelope",
                              "marker": "[WEBAGENT_V8_PARSE_ERROR]",
                              "detail": "PRIVATE_PROTOCOL"}]),
        ]
        (logdir / "webagent_runtime.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in all_events)+"\n", encoding="utf-8",
        )
        (logdir / "p2-readonly" / "turn_readback.jsonl").write_text(
            "\n".join(json.dumps(x) for x in [
                {"schema":"P2_READONLY_TURN_V1", "utc":old, "request_id":"WA-OLD"},
                {"schema":"P2_READONLY_TURN_V1", "utc":fresh, "request_id":"WA-FRESH"},
            ])+"\n", encoding="utf-8",
        )
        (logdir / "p2-readonly" / "hybrid_writer.jsonl").write_text(
            "\n".join(json.dumps(x) for x in [
                {"schema":"COMPOSER_HYBRID_WRITER_V1","utc":old, "mode":"OLD"},
                {"schema":"COMPOSER_HYBRID_WRITER_V1","utc":fresh,
                 "mode":"ONE_SHOT_PREFIX512_BULK_TAIL","prefix_chars":512},
            ])+"\n", encoding="utf-8",
        )
        with patch.object(collector, "log_root", return_value=logdir):
            evidence = collector.make_p2_evidence(root)
        trace = json.loads(evidence["p2_admission_trace.json"])["events"]
        summary = json.loads(evidence["p2_terminal_summary.json"])
        turns = json.loads(evidence["p2_turn_observation.json"])["records"]
        writers = json.loads(evidence["p2_writer_trace.json"])["records"]
        assert summary["scope"] == "LATEST_CONTROLLER_RUN"
        assert summary["status"] == "UNKNOWN_NOT_FULLY_CORRELATED"
        assert summary["failed_request_ids"] == []
        assert len(turns) == 1 and turns[0]["request_id"] == "WA-FRESH"
        assert len(writers) == 1 and writers[0]["prefix_chars"] == 512
        assert all("WA-OLD" not in json.dumps(row) for row in trace)
        assert all("PRIVATE" not in json.dumps(row) for row in trace)
        assert any(x.get("browser_stage") == "SESSION_BOOTSTRAP" for x in trace)
        assert any(x.get("browser_stage") == "TASK_PLANNER" for x in trace)
        assert any(x.get("event") == "controller_ready" for x in trace)
        ingress = [x for x in trace if x["event"] == "p2_request_ingress"]
        assert len(ingress) == 1 and ingress[0]["source_kind"] == "WEB_UI"
        assert ingress[0]["hybrid_eligible"] is True
        rej = [x for x in trace if x["event"] == "protocol_rejected"][0]
        assert rej["rejection_stage"] == "TRANSPORT_PARSE"
        assert rej["reason_categories"] == ["MISSING_TOOL_ENVELOPE"]

def test_p2_hook_and_sanitizer():
    class Adapter:
        def durable_turn_id(self, turn):
            return turn.structural_id
        def turns(self, role):
            return ([SimpleNamespace(structural_id="user-u")]
                    if role == "user" else [SimpleNamespace(structural_id="assistant-a")])
    adapter=Adapter()
    turn=SimpleNamespace(structural_id="assistant-a", ordinal=4)
    scope=SimpleNamespace(scope_id="scope-private", user_turn=SimpleNamespace(structural_id="user-u"))
    snapshot={"_web_ui_scope":scope,"assistant_count":4,"user_count":4}
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        logdir=root/"localdata"/"logs"
        with patch.dict(os.environ, {"SMARTAGENT_P2_READONLY_DIAGNOSTIC_V1":"1"}),\
             patch("agent_core.paths.log_root",return_value=logdir):
            sr.capture_p2_readonly_turn(
                stage="COMMIT_READY",expected={"run_id":"WA-TEST","turn_id":2},
                snapshot=snapshot, assistant_turn=turn,
                text="PRIVATE_ASSISTANT_RESPONSE",adapter=adapter,
            )
        raw = (logdir/"p2-readonly"/"turn_readback.jsonl").read_text(encoding="utf-8")
        assert "PRIVATE_ASSISTANT_RESPONSE" not in raw
        assert "scope-private" not in raw
        (logdir/"webagent_runtime.jsonl").write_text("\n".join(json.dumps(x) for x in [
            {"event":"request_started","request_id":"WA-TEST","request":"PRIVATE_USER_TEXT"},
            {"event":"ack_accepted","request_id":"WA-TEST","round":2,"ack_id":"WEBACK-1","content":"PRIVATE_SECRET"},
            {"event":"protocol_rejected","request_id":"WA-TEST","round":3,
             "diagnostics":[{"reason":"missing_smartagent_tool_envelope","detail":"PRIVATE_DANGEROUS"}]},
            {"event":"bounded_repair_observed","request_id":"WA-TEST","round":3,
             "reason_codes":["missing_smartagent_tool_envelope"],"status":"REPAIR_ALLOWED"},
            {"event":"tool_started","request_id":"WA-TEST","tool":"read_file","path":"C:\\private"},
            {"event":"runtime_terminal_accepted","request_id":"WA-TEST"},
            {"event":"request_completed","request_id":"WA-TEST","rendered_response":"PRIVATE_TEXT"},
        ])+"\n",encoding="utf-8")
        with patch.object(collector,"log_root",return_value=logdir):
            bundle=collector.make_p2_evidence(root)
        assert set(bundle)=={"p2_turn_observation.json","p2_admission_trace.json",
                             "p2_terminal_summary.json","p2_writer_trace.json"}
        data=b"".join(bundle.values())
        assert b"PRIVATE" not in data and b"C:\\private" not in data
        assert json.loads(bundle["p2_terminal_summary.json"])["status"]=="ACCEPTED_AND_COMPLETED"
        assert len(json.loads(bundle["p2_turn_observation.json"])["records"])==1
        events = json.loads(bundle["p2_admission_trace.json"])["events"]
        rejected = [e for e in events if e["event"] == "protocol_rejected"]
        assert rejected[0]["reason_categories"] == ["MISSING_TOOL_ENVELOPE"]
        assert all("PRIVATE" not in json.dumps(row) for row in events)
        # Real one-click bundler must also work without any P1 captures.
        from WebAgent.tests.collect_sr_readback_bundle import bundle as collect_zip
        with patch.object(collector,"log_root",return_value=logdir):
            archive = collect_zip(root)
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            assert "p2/p2_terminal_summary.json" in z.namelist()
            assert "p2/p2_admission_trace.json" in z.namelist()
            assert not any(n.startswith("captures/") for n in z.namelist())
            assert b"PRIVATE" not in z.read("p2/p2_admission_trace.json")


if __name__ == "__main__":
    test_hybrid_writer()
    test_hybrid_task_scope()
    test_latest_controller_run_scope()
    test_p2_hook_and_sanitizer()
    print("HYBRID_P2_OFFLINE_OK: strict prefix, fast tail, fail-closed, sanitized collector")
