"""Offline, negative-first checks for the 2A in-process code identity gate.

These synthetic records DO NOT attest any currently running Agent/Worker and
must never be counted as genuine Live or loaded-source SHA evidence.
"""
from __future__ import annotations

import copy
from agent_core.source_attestation import (
    TRI_ONE_REQUIRED_FUNCTIONS, require_tri_one_loaded_source_attestation,
)


def _verified_record() -> dict:
    return {
        "manifest_verified_before_capture": True,
        "modules": {
            name: {
                "loaded": True,
                "source_path": "source/" + name.replace(".", "/") + ".py",
                "disk_sha256_at_capture": "d" * 64,
                "live_matches_recompiled_disk": {f: True for f in funcs},
            }
            for name, funcs in TRI_ONE_REQUIRED_FUNCTIONS.items()
        },
    }


def _rejects(record: dict) -> None:
    try:
        require_tri_one_loaded_source_attestation(record)
    except RuntimeError:
        return
    raise AssertionError("invalid loaded source attestation was accepted")


def main() -> None:
    good = _verified_record()
    result = require_tri_one_loaded_source_attestation(good)
    assert result["status"] == "PASS"
    assert result["source_commit"] == "NOT_ATTESTED"
    assert len(result["coverage"]) == 6
    for module, funcs in TRI_ONE_REQUIRED_FUNCTIONS.items():
        for change in ("module_missing", "unloaded", "foreign_path", "missing_disk_sha"):
            bad = copy.deepcopy(good)
            if change == "module_missing":
                del bad["modules"][module]
            elif change == "unloaded":
                bad["modules"][module]["loaded"] = False
            elif change == "foreign_path":
                bad["modules"][module]["source_path"] = "/tmp/foreign.py"
            else:
                bad["modules"][module]["disk_sha256_at_capture"] = None
            _rejects(bad)
        for func in funcs:
            bad = copy.deepcopy(good)
            bad["modules"][module]["live_matches_recompiled_disk"][func] = False
            _rejects(bad)
    bad = copy.deepcopy(good)
    bad["manifest_verified_before_capture"] = False
    _rejects(bad)
    _rejects({})
    print("TRI_ONE_2A_LOADED_ATTESTATION_OFFLINE_OK")


if __name__ == "__main__":
    main()
