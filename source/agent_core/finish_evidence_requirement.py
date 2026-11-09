"""Non-authorizing upstream evidence requirements for one PokeClaw Executor.

This is a lower bound on evidence, not a task plan, Goal Driver or permission.
A model-generated Finish cannot override the request's minimum requirement.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .task_requirement import assess_task_requirement, no_tool_terminal_candidate

SCHEMA = "SMARTAGENT_FINISH_EVIDENCE_REQUIREMENT_V1"


def assess_finish_evidence_requirement(
    request: str, *, attachments: bool = False, authorized_paths: bool = False,
    image_or_artifact_request: bool = False, continuation: bool = False,
) -> dict:
    requirement = assess_task_requirement(
        request, attachments=attachments,
        authorized_paths=authorized_paths,
        image_or_artifact_request=image_or_artifact_request,
        continuation=continuation,
    )
    sources = set(requirement.get("resource_sources") or [])
    # Inline user-provided text needs no external source, but the legacy
    # PR0 candidate deliberately excludes INLINE. This is an Executor-only
    # evidence requirement, not a global change to legacy routing.
    inline_text = (
        requirement.get("route") == "ANSWER_ONLY"
        and sources == {"INLINE"}
        and not requirement.get("required_operations")
        and not requirement.get("external_information_required")
        and not requirement.get("side_effect_required")
    )
    answer_only = bool(no_tool_terminal_candidate(requirement) or inline_text)
    return {
        "schema": SCHEMA,
        "request_sha256": hashlib.sha256(str(request).encode("utf-8")).hexdigest(),
        "assessment_route": str(requirement.get("route") or ""),
        "assessment_reason": str(requirement.get("reason") or "")[:120],
        "resource_sources": list(requirement.get("resource_sources") or []),
        "required_operations": list(requirement.get("required_operations") or []),
        "text_delivery_allowed": True,
        "answer_only_eligible": answer_only,
        "tool_evidence_required": not answer_only,
        "external_state_verification_required": bool(
            requirement.get("required_operations")
            or set(requirement.get("resource_sources") or []) - {"NONE", "INLINE"}
        ),
        "authorizes_operation": False,
    }


def valid_finish_evidence_requirement(value: object, request: str) -> bool:
    return (
        isinstance(value, Mapping) and value.get("schema") == SCHEMA
        and value.get("request_sha256") ==
            hashlib.sha256(str(request).encode("utf-8")).hexdigest()
        and type(value.get("answer_only_eligible")) is bool
        and type(value.get("tool_evidence_required")) is bool
        and value.get("tool_evidence_required") is not value.get("answer_only_eligible")
        and value.get("authorizes_operation") is False
    )


__all__ = [
    "SCHEMA", "assess_finish_evidence_requirement",
    "valid_finish_evidence_requirement",
]
