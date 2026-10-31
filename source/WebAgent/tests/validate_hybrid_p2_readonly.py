"""Offline isolated Hybrid Writer + P2 metadata/collector regression."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "source"))
from agent_core import composer_operation as composer
from agent_core import sr_first_readback as sr
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
    assert ("text", 1024) in prefix
    assert len([s for s in prefix if s[0] == "read"]) <= 5
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


if __name__ == "__main__":
    test_hybrid_writer()
    test_p2_hook_and_sanitizer()
    print("HYBRID_P2_OFFLINE_OK: strict prefix, fast tail, fail-closed, sanitized collector")
