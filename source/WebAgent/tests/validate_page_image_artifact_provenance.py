#!/usr/bin/env python3
"""Offline regression coverage for request-proven page-level image downloads."""
from __future__ import annotations

import hashlib
import base64
import os
import sys
import tempfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core import artifact_transfer
from agent_core.web_runtime import WebLLMScraper
from agent_core.web_ui.contracts import ArtifactObservation


class FakeAdapter:
    def __init__(self, observations, ready_fingerprint: str):
        self.observations = tuple(observations)
        self.ready_fingerprint = ready_fingerprint

    def observation_turns(self, _role):
        return ()

    def artifact_observations(self, _root=None):
        return self.observations

    def media_state(self, _root=None):
        ready = sum(
            1 for item in self.observations
            if item.generated_media and item.visible and item.complete
            and item.natural_width > 0 and item.natural_height > 0
        )
        return {"image_ready": ready, "ready_image_fingerprint": self.ready_fingerprint}


def image(src: str, *, complete: bool = True) -> ArtifactObservation:
    return ArtifactObservation(
        element=object(), tag="img", src=src, text="Generated image",
        visible=True, complete=complete,
        natural_width=1024 if complete else 0,
        natural_height=1024 if complete else 0,
        rendered_width=512, rendered_height=512,
        generated_media=True,
    )


def fingerprint(*srcs: str) -> str:
    parts = [f"img|{src}|1024x1024" for src in srcs]
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()


def run() -> dict:
    page = object()
    stale = image("https://example.test/stale.png")
    fresh = image("https://example.test/fresh.png")
    adapter = FakeAdapter([stale], fingerprint(stale.src))
    original = artifact_transfer.create_web_ui_for_page
    artifact_transfer.create_web_ui_for_page = lambda _page: adapter
    try:
        baseline = artifact_transfer.snapshot_page_ready_image_signatures(page)
        assert len(baseline) == 1

        adapter.observations = (stale, fresh)
        adapter.ready_fingerprint = fingerprint(stale.src, fresh.src)
        scope = {
            "assistant_count_before": 0,
            "last_assistant_fp_before": "",
            "page_ready_image_signatures_before": baseline,
            "page_ready_image_fingerprint_before": fingerprint(stale.src),
            "page_ready_image_fingerprint_after": adapter.ready_fingerprint,
            "fresh_page_image_proven": True,
        }
        discovery_diagnostic = {}
        candidates = artifact_transfer.discover_artifact_candidates(
            page, expected_name="5.jpg", scope=scope, strict_scope=True,
            diagnostics=discovery_diagnostic,
        )
        assert len(candidates) == 1 and candidates[0].src == fresh.src
        assert discovery_diagnostic["result"] == "PASS"
        assert discovery_diagnostic["page_image_gate"]["reason"] == "fresh_page_candidate_admitted"
        assert discovery_diagnostic["accepted_candidate_count"] == 1
        assert artifact_transfer.candidate_matches_manifest(
            artifact_transfer.ArtifactCandidate(
                element=None, kind="image", score=1,
                src="https://example.test/generated.png", filename="generated.png",
            ),
            artifact_transfer.ArtifactManifest(
                artifact_id="ART-IMAGE", logical_filename="5.jpg",
                expected_extension=".jpg",
            ),
        )[0]

        # Reproduce the production failure: the producer ask proves a fresh
        # image, then a control-repair ask sees the same image in its baseline.
        scraper = WebLLMScraper.__new__(WebLLMScraper)
        scraper._page = page
        scraper._artifact_scope_ledger = {}
        scraper._last_artifact_scope = None
        scraper._log_stage = lambda *_args, **_kwargs: None
        producer_scope = dict(
            scope,
            producer_scope_id="WEBUI-PRODUCER",
            conversation_id="conversation-1",
        )
        record = scraper._register_artifact_scope("RR-TEST", producer_scope)
        assert record and record["artifact_id"].startswith("ART-")
        assert record["candidate_ids"] == [candidates[0].identity_signature()]

        repair_scope = dict(
            producer_scope,
            producer_scope_id="WEBUI-CONTROL-REPAIR",
            page_ready_image_signatures_before=[
                item.identity_signature()
                for item in artifact_transfer._ready_page_image_candidates(page)
            ],
            page_ready_image_fingerprint_before=adapter.ready_fingerprint,
            page_ready_image_fingerprint_after=adapter.ready_fingerprint,
            fresh_page_image_proven=False,
        )
        assert scraper._register_artifact_scope("RR-TEST", repair_scope) is None
        preserved = scraper._artifact_scope_for_request("RR-TEST")
        assert preserved and preserved["artifact_id"] == record["artifact_id"]
        assert artifact_transfer.discover_artifact_candidates(
            page, expected_name="5.jpg", scope=preserved, strict_scope=True,
        )[0].src == fresh.src
        scraper._consume_artifact_scope("RR-TEST", record["artifact_id"])
        assert scraper._artifact_scope_for_request("RR-TEST") is None

        # Telegram image delivery intentionally supplies a directory and no
        # filename.  Verify that content sniffing creates a real image file in
        # that directory rather than trying to replace the directory itself.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        data_src = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        generated = image(data_src)
        adapter.observations = (generated,)
        adapter.ready_fingerprint = fingerprint(data_src)
        directory_scope = {
            "assistant_count_before": 0,
            "last_assistant_fp_before": "",
            "page_ready_image_signatures_before": [],
            "page_ready_image_fingerprint_before": fingerprint(),
            "page_ready_image_fingerprint_after": adapter.ready_fingerprint,
            "fresh_page_image_proven": True,
        }
        allowed = artifact_transfer.discover_artifact_candidates(
            page, scope=directory_scope, strict_scope=True,
        )
        directory_scope["candidate_ids"] = [allowed[0].identity_signature()]
        with tempfile.TemporaryDirectory() as tmp:
            result = artifact_transfer.download_latest_artifact_with_evidence(
                page, object(), tmp + os.sep, timeout_sec=5,
                scope=directory_scope, strict_scope=True,
            )
            downloaded = Path(result["path"])
            assert downloaded.parent == Path(tmp).resolve()
            assert downloaded.name == "generated_image.png"
            assert downloaded.read_bytes() == png

            # Freeze the original PNG while its data URL is mounted, then
            # remove every image from the DOM. Delivery must use the staged
            # bytes and must not convert or disguise them as JPEG.
            staged = artifact_transfer.stage_png_candidate(
                page, object(), allowed[0], request_id="RR-STAGED",
                artifact_id="ART-STAGED", staging_root=Path(tmp) / "staging",
            )
            staged_scope = dict(
                directory_scope,
                request_id="RR-STAGED",
                artifact_id="ART-STAGED",
                candidate_ids=[allowed[0].identity_signature()],
                **staged,
            )
            adapter.observations = ()
            adapter.ready_fingerprint = fingerprint()
            requested_jpeg = Path(tmp) / "5.jpg"
            staged_result = artifact_transfer.download_latest_artifact_with_evidence(
                page, object(), str(requested_jpeg), timeout_sec=5,
                expected_filename="5.jpg", scope=staged_scope,
                strict_scope=True,
            )
            staged_output = Path(staged_result["path"])
            assert staged_output == requested_jpeg.with_suffix(".png")
            assert staged_output.read_bytes() == png
            assert staged_result["method"] == "staged_png_copy"
            assert staged_result["conversion"] == ""

            # Scope registration itself must freeze the PNG before later
            # protocol/control turns can remount or remove the image DOM.
            adapter.observations = (generated,)
            adapter.ready_fingerprint = fingerprint(data_src)
            registration_scope = dict(
                directory_scope,
                producer_scope_id="WEBUI-STAGING-PRODUCER",
                conversation_id="conversation-staging",
            )
            scraper._browser = object()
            original_staging_root = artifact_transfer.DEFAULT_ARTIFACT_STAGING_ROOT
            artifact_transfer.DEFAULT_ARTIFACT_STAGING_ROOT = Path(tmp) / "runtime-staging"
            try:
                registered_png = scraper._register_artifact_scope(
                    "RR-STAGE-REGISTER", registration_scope
                )
                assert registered_png and registered_png["staged_png_status"] == "READY"
                registered_path = Path(registered_png["staged_png_path"])
                assert registered_path.read_bytes() == png
                repeated_registration = scraper._register_artifact_scope(
                    "RR-STAGE-REGISTER", registration_scope
                )
                assert repeated_registration is registered_png
                scraper._consume_artifact_scope(
                    "RR-STAGE-REGISTER", registered_png["artifact_id"]
                )
                assert not registered_path.exists()
                assert registered_png["staged_png_status"] == "CONSUMED"

                # A provider remount may assign a different DOM/data URL to
                # identical bytes after delivery.  Request + PNG digest must
                # suppress a second ledger entry and delete its staging copy.
                remount_src = (
                    "data:image/png;name=remount;base64,"
                    + base64.b64encode(png).decode("ascii")
                )
                remounted = image(remount_src)
                adapter.observations = (remounted,)
                adapter.ready_fingerprint = fingerprint(remount_src)
                remount_scope = dict(
                    registration_scope,
                    producer_scope_id="WEBUI-STAGING-REMOUNT",
                    page_ready_image_signatures_before=[
                        registered_png["staged_png_candidate_id"]
                    ],
                    page_ready_image_fingerprint_before=fingerprint(data_src),
                    page_ready_image_fingerprint_after=adapter.ready_fingerprint,
                    fresh_page_image_proven=True,
                )
                remount_scope.pop("candidate_ids", None)
                ledger_size = len(scraper._artifact_scope_ledger["RR-STAGE-REGISTER"])
                duplicate = scraper._register_artifact_scope(
                    "RR-STAGE-REGISTER", remount_scope
                )
                assert duplicate, scraper._artifact_scope_ledger["RR-STAGE-REGISTER"]
                assert duplicate["duplicate_content"] is True, duplicate
                assert duplicate["consumed"] is True
                assert len(scraper._artifact_scope_ledger["RR-STAGE-REGISTER"]) == ledger_size
                assert scraper._artifact_scope_for_request("RR-STAGE-REGISTER") is None
                assert not list((Path(tmp) / "runtime-staging").rglob("*.png"))
            finally:
                artifact_transfer.DEFAULT_ARTIFACT_STAGING_ROOT = original_staging_root

        no_proof = dict(scope, fresh_page_image_proven=False)
        assert not artifact_transfer.discover_artifact_candidates(
            page, expected_name="5.jpg", scope=no_proof, strict_scope=True,
        )

        # Before candidate binding, whole-page fingerprint drift still fails
        # closed because there is no exact request-owned identity yet.
        adapter.observations = (stale,)
        changed_again = dict(scope)
        adapter.ready_fingerprint = fingerprint(stale.src)
        changed_diagnostic = {}
        assert not artifact_transfer.discover_artifact_candidates(
            page, expected_name="5.jpg", scope=changed_again, strict_scope=True,
            diagnostics=changed_diagnostic,
        )
        assert changed_diagnostic["reason"] == "current_fingerprint_differs_from_proven_fingerprint"

        # After binding, unrelated page images may mount or unmount. The exact
        # registered candidate remains admissible even though the page-wide
        # fingerprint is different from the original proof snapshot.
        unrelated = image("https://example.test/unrelated.png")
        adapter.observations = (fresh, unrelated)
        adapter.ready_fingerprint = fingerprint(fresh.src, unrelated.src)
        bound_changed_scope = dict(
            scope,
            candidate_ids=[candidates[0].identity_signature()],
        )
        bound_changed_diagnostic = {}
        bound_candidates = artifact_transfer.discover_artifact_candidates(
            page, expected_name="5.jpg", scope=bound_changed_scope,
            strict_scope=True, diagnostics=bound_changed_diagnostic,
        )
        assert [item.src for item in bound_candidates] == [fresh.src]
        assert bound_changed_diagnostic["page_image_gate"]["reason"] == "bound_page_candidate_admitted"

        # If the provider remounts the same visual result under a new URL, the
        # request proof may still pass while the registered candidate identity
        # changes. Diagnostics must distinguish that from a missing image.
        remounted = image("https://example.test/remounted-fresh.png")
        adapter.observations = (stale, remounted)
        adapter.ready_fingerprint = fingerprint(stale.src, remounted.src)
        identity_drift_scope = dict(
            scope,
            page_ready_image_fingerprint_after=adapter.ready_fingerprint,
            candidate_ids=[candidates[0].identity_signature()],
        )
        identity_diagnostic = {}
        assert not artifact_transfer.discover_artifact_candidates(
            page, expected_name="5.jpg", scope=identity_drift_scope, strict_scope=True,
            diagnostics=identity_diagnostic,
        )
        assert identity_diagnostic["prefilter_candidate_count"] == 0
        assert identity_diagnostic["accepted_candidate_count"] == 0
        assert identity_diagnostic["reason"] == "bound_page_candidate_not_mounted"

        adapter.observations = (stale, image("https://example.test/pending.png", complete=False))
        adapter.ready_fingerprint = scope["page_ready_image_fingerprint_after"]
        assert not artifact_transfer.discover_artifact_candidates(
            page, expected_name="5.jpg", scope=scope, strict_scope=True,
        )
    finally:
        artifact_transfer.create_web_ui_for_page = original

    return {
        "fresh_page_image_admitted": True,
        "generated_image_provider_filename_does_not_override_destination": True,
        "stale_page_image_rejected": True,
        "missing_request_proof_rejected": True,
        "unbound_changed_page_state_rejected": True,
        "bound_candidate_survives_page_fingerprint_drift": True,
        "fingerprint_gate_diagnostic": True,
        "candidate_identity_drift_diagnostic": True,
        "incomplete_image_rejected": True,
        "producer_scope_survives_control_repair_ask": True,
        "artifact_identity_is_request_bound": True,
        "consumed_artifact_is_not_reused": True,
        "directory_output_gets_detected_image_filename": True,
        "staged_png_survives_dom_removal": True,
        "staged_png_preserves_original_format": True,
        "scope_registration_stages_and_cleans_png": True,
        "request_png_digest_deduplicates_ui_remount": True,
    }


if __name__ == "__main__":
    print(run())
