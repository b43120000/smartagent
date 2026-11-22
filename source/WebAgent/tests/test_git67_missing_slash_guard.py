"""Git67 sidecar: reproduce a model-only deleted separator, no network/tools.

This is NOT a historical sender-root-cause reproduction. It demonstrates
whether an observed incorrect model decision could silently survive parsing
while the original goal contains the exact correct path.
"""
import json
import pytest

from WebAgent.minimal_decision import (
    MinimalDecisionError, parse_minimal_decision_with_diagnostics,
)

GOOD = r"E:\AIspace\.case_d_backups"
BAD = r"E:\AIspace.case_d_backups"
GOAL = "NLIT_CASE_ID=IT10\nNLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E\n" + GOOD


def response(path, field="path"):
    return json.dumps({"type": "tool_call", "name": "observe_fact",
                       "arguments": {field: path}}, ensure_ascii=False)


def test_missing_exact_separator_in_model_action_fails_closed():
    # The parser formerly ACCEPTED the 25-byte mutated path even though
    # the unmodified 26-byte path was present in the original user goal.
    with pytest.raises(MinimalDecisionError) as err:
        parse_minimal_decision_with_diagnostics(response(BAD), original_goal=GOAL)
    assert err.value.code == "model_explicit_path_separator_missing"


def test_valid_original_path_is_not_rewritten():
    decision, meta = parse_minimal_decision_with_diagnostics(
        response(GOOD), original_goal=GOAL
    )
    assert decision.arguments["path"] == GOOD
    assert meta["status"] == "ACCEPTED"


def test_explicitly_requested_other_path_is_allowed():
    decision, meta = parse_minimal_decision_with_diagnostics(
        response(BAD), original_goal="查詢 " + BAD
    )
    assert decision.arguments["path"] == BAD
    assert meta["status"] == "ACCEPTED"


def test_when_both_explicit_paths_in_goal_do_not_infer_user_intent():
    decision, meta = parse_minimal_decision_with_diagnostics(
        response(BAD), original_goal=GOAL + "\n對照路徑：" + BAD
    )
    assert decision.arguments["path"] == BAD
    assert meta["status"] == "ACCEPTED"


def test_other_unrelated_windows_path_is_not_arbitrarily_changed():
    unrelated = r"E:\AIspace\some.other_folder"
    decision, meta = parse_minimal_decision_with_diagnostics(
        response(unrelated), original_goal=GOAL
    )
    assert decision.arguments["path"] == unrelated
    assert meta["status"] == "ACCEPTED"
