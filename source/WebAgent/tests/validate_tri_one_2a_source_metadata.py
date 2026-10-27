"""TaskResult source-metadata contract regression; fixtures only, no runtime task."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_core.protocol_manifest import require_protocol_manifest
from agent_core.remote_events import RemoteEventStore
from agent_core.source_attestation import capture, build_task_result_source_metadata
import agent_core.remote_events as remote_events
import agent_core.tri_one_task_result as task_result_module
import RemoteAgent.telegram_webagent_worker as worker_module


from agent_core.source_attestation import build_task_result_source_metadata
from agent_core.tri_one_task_result import (
    TaskResultContractError, make_task_result, validate_task_result,
)


def build(**overrides):
    values = dict(
        request_id="RR-SOURCE-META", task_id="TASK-SOURCE-META", task_epoch="EPOCH-SOURCE-META",
        intent_digest="DIGEST-SOURCE-META", source_interface="LOCAL_TEST",
        trusted_reply_route={"transport": "LOCAL_TEST", "origin": "source-metadata-fixture"},
        state="COMPLETED", outcome="UNKNOWN", terminal_acceptance=None,
        evidence_refs=[], operational_action_count=0, answer_text="fixture", artifacts=[],
        created_at="2026-10-27T00:00:00.000Z",
    )
    values.update(overrides)
    return make_task_result(**values)


def rejected(reason, fn):
    try:
        fn()
    except TaskResultContractError as error:
        assert str(error) == reason, (reason, str(error))
    else:
        raise AssertionError("expected rejection: " + reason)


def run():
    base = build()
    assert base["source_commit"] == "NOT_ATTESTED"
    assert base["source_metadata"]["loaded_code_status"] == "NOT_ATTESTED"
    assert validate_task_result(base)["source_commit"] == "NOT_ATTESTED"

    # A bare Git SHA or legacy metadata flag is not a source attestation.
    rejected("source_metadata_commit_mismatch", lambda: build(source_commit="a" * 40))
    forged = {
        "schema": "SMARTAGENT_TASK_RESULT_SOURCE_METADATA_V1",
        "loaded_code_status": "PASS", "source_commit_status": "PASS",
        "source_commit": "a" * 40, "source_commit_claim": "a" * 40,
        "source_commit_claim_status": "SIGNED", "source_commit_trust": "UNSIGNED",
        "modules": {
            "agent_core.remote_events": {
                "source_path": "source/agent_core/remote_events.py",
                "disk_sha256": "b" * 64,
                "live_matches_recompiled_disk": {"RemoteEventStore.emit": True},
                "functions": {"RemoteEventStore.emit": True},
            },
        },
    }
    rejected("source_commit_requires_trusted_metadata", lambda: build(source_metadata=forged))
    forged["source_commit_trust"] = "MACHINE_AUTHORIZATION"
    rejected("source_commit_verifier_unavailable", lambda: build(source_metadata=forged))

    # A valid unsigned build-commit claim remains a claim; it cannot fill source_commit.
    claim = {
        "schema": "SMARTAGENT_TASK_RESULT_SOURCE_METADATA_V1",
        "loaded_code_status": "PASS", "source_commit_status": "NOT_ATTESTED",
        "source_commit": "NOT_ATTESTED", "source_commit_claim": "c" * 40,
        "source_commit_claim_status": "UNSIGNED_BUILD_PROVENANCE",
        "publisher_signature_status": "NOT_PROVIDED",
        "attestation_sha256": "d" * 64, "protocol_manifest_sha256": "e" * 64,
        "update_manifest_sha256": "f" * 64, "source_provenance_sha256": "1" * 64,
        "source_tree_sha": "2" * 40, "reason": "source_commit_not_attested_unsigned_provenance",
        "modules": {
            "agent_core.remote_events": {
                "source_path": "source/agent_core/remote_events.py",
                "disk_sha256": "3" * 64,
                "live_matches_recompiled_disk": {"RemoteEventStore.emit": True},
                "functions": {"RemoteEventStore.emit": True},
            },
        },
    }
    result = build(source_metadata=claim)
    assert result["source_commit"] == "NOT_ATTESTED"
    assert result["source_metadata"]["source_commit_claim"] == "c" * 40
    assert result["source_metadata"]["publisher_signature_status"] == "NOT_PROVIDED"
    assert validate_task_result(result)["source_commit"] == "NOT_ATTESTED"

    missing = build_task_result_source_metadata(
        {"schema": "SMARTAGENT_SOURCE_ATTESTATION_V1", "manifest_verified_before_capture": False},
    )
    assert missing["loaded_code_status"] == "NOT_ATTESTED"
    assert missing["source_commit"] == "NOT_ATTESTED"
    assert missing["source_commit_claim"] is None
    root = Path(__file__).resolve().parents[3]
    manifest_status = require_protocol_manifest(root)
    assert manifest_status.get("hash_verification") == "PASS", manifest_status
    assert not manifest_status.get("hash_mismatches"), manifest_status
    attestation = capture(role="task_result_producer", manifest_verified=True)
    source_metadata = build_task_result_source_metadata(attestation, root=root)
    assert source_metadata["loaded_code_status"] == "PASS", source_metadata
    assert source_metadata["source_commit"] == "NOT_ATTESTED", source_metadata
    assert source_metadata["modules"]["agent_core.remote_events"]["live_matches_recompiled_disk"]["RemoteEventStore.emit"] is True

    # Exercise the real RemoteEventStore -> TaskResult producer path with a
    # synthetic in-memory task and a temporary outbox; no receiver or Runtime runs.
    previous_flag = os.environ.get("TRI_ONE_TASK_RESULT_V1")
    os.environ["TRI_ONE_TASK_RESULT_V1"] = "1"
    try:
        with tempfile.TemporaryDirectory(prefix="tri-one-source-meta-") as temp:
            route = {"transport": "LOCAL_TEST", "origin": "source-metadata-fixture"}
            task = SimpleNamespace(
                request_id="RR-SOURCE-META-E2E", task_id="TASK-SOURCE-META-E2E",
                task_epoch="EPOCH-SOURCE-META-E2E", state="COMPLETED",
                completed_at="2026-10-27T00:00:00.000Z", reply_route=route,
                metadata={"intent_digest": "DIGEST-SOURCE-META-E2E", "reply_route": route},
                result_ledger={"final": {"intent_digest": "DIGEST-SOURCE-META-E2E", "task_outcome": "UNKNOWN", "response": "fixture"}},
                action_ledger={},
            )
            event = RemoteEventStore(Path(temp) / "events.json").emit(
                "TASK_COMPLETED", task, status="COMPLETED",
                payload={"task_outcome": "UNKNOWN", "summary": "fixture"},
            )
            assert event.payload["source_metadata"]["loaded_code_status"] == "PASS"
            assert event.payload["source_metadata"]["source_commit"] == "NOT_ATTESTED"
            record = event.payload["task_result_v1"]
            assert record["source_metadata"]["loaded_code_status"] == "PASS"
            assert record["source_commit"] == "NOT_ATTESTED"
            assert record["source_metadata"]["modules"]["agent_core.tri_one_task_result"]["live_matches_recompiled_disk"]["make_task_result_from_remote_event"] is True
    finally:
        if previous_flag is None:
            os.environ.pop("TRI_ONE_TASK_RESULT_V1", None)
        else:
            os.environ["TRI_ONE_TASK_RESULT_V1"] = previous_flag

    return {"tri_one_2a_source_metadata": "PASS", "cases": 8}


if __name__ == "__main__":
    print(run())
