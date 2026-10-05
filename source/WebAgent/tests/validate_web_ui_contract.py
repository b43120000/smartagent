#!/usr/bin/env python3
"""Deterministic request-ownership tests for the normalized Web UI boundary."""
from __future__ import annotations

import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from agent_core.web_ui.providers.chatgpt import ChatGPTUIAdapter
from agent_core.web_runtime import WebLLMScraper, WebScraperStageError
from agent_core.web_ui.profiles import (
    CHATGPT_COMPOSER_SELECTOR,
    CHATGPT_SEND_SELECTOR,
    CHATGPT_STOP_SELECTORS,
    SEARCH_UNIT_2026_PROFILE,
)
from agent_core.web_ui.providers.chatgpt.profiles import LEGACY_ROLE_PROFILE


class FakeElement:
    def __init__(self, text="", *, attrs=None, order=0, children=None, visible=True, enabled=True):
        self._text = text
        self.attrs = dict(attrs or {})
        self.order = int(order)
        self.children = dict(children or {})
        self.visible = bool(visible)
        self.enabled = bool(enabled)

    def inner_text(self):
        return self._text

    def text_content(self):
        return self._text

    def get_attribute(self, name):
        return self.attrs.get(name)

    def query_selector_all(self, selector):
        return list(self.children.get(selector, []))

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def evaluate(self, script, arg=None):
        if "compareDocumentPosition" in script:
            return self.order > int(getattr(arg, "order", -1))
        if "matches(selector)" in script:
            return bool(self.attrs.get("matches_final", False))
        return None


class FakeLocator:
    def __init__(self, values):
        self.values = values

    def count(self):
        return len(self.values)

    def nth(self, index):
        return self.values[index]

    @property
    def last(self):
        return self.values[-1]


class FakePage:
    def __init__(self):
        self.composer = FakeElement(order=100)
        self.send = FakeElement(order=101)
        self.users = [self.user("old request", "turn:1:user", 1)]
        self.assistants = [self.assistant("old answer", "turn:1:assistant", 2)]
        self.visible_stop = False

    @staticmethod
    def user(text, key, order, *, root_text=None):
        content = FakeElement(text, order=order)
        return FakeElement(
            text if root_text is None else root_text,
            attrs={"data-content-search-unit-key": key},
            order=order,
            children={SEARCH_UNIT_2026_PROFILE.user_content_selector: [content]},
        )

    @staticmethod
    def assistant(text, key, order):
        block = FakeElement(text, order=order)
        return FakeElement(
            text,
            attrs={"data-content-search-unit-key": key},
            order=order,
            children={SEARCH_UNIT_2026_PROFILE.final_content_selector: [block]},
        )

    def locator(self, selector):
        return FakeLocator(self.query_selector_all(selector))

    def query_selector_all(self, selector):
        if selector == CHATGPT_COMPOSER_SELECTOR:
            return [self.composer]
        if selector == CHATGPT_SEND_SELECTOR:
            return [self.send]
        if selector == SEARCH_UNIT_2026_PROFILE.user_turn_selector:
            return self.users
        if selector == SEARCH_UNIT_2026_PROFILE.assistant_turn_selector:
            return self.assistants
        if selector in CHATGPT_STOP_SELECTORS and self.visible_stop:
            return [FakeElement("stop", order=102)]
        return []

    def evaluate(self, script, arg=None):
        if "compareDocumentPosition" in script and isinstance(arg, list):
            return arg[1].order > arg[0].order
        return None


class LegacyOnlyPage(FakePage):
    # Expose only the legacy role-based turn DOM while sharing composer/send controls.

    def __init__(self):
        self.composer = FakeElement(order=100)
        self.send = FakeElement(order=101)
        user_content = FakeElement("legacy request", order=1)
        assistant_content = FakeElement("legacy answer", order=2)
        self.legacy_users = [
            FakeElement(
                "legacy request",
                attrs={"data-message-author-role": "user"},
                order=1,
                children={LEGACY_ROLE_PROFILE.user_content_selector: [user_content]},
            )
        ]
        self.legacy_assistants = [
            FakeElement(
                "legacy answer",
                attrs={"data-message-author-role": "assistant"},
                order=2,
                children={LEGACY_ROLE_PROFILE.final_content_selector: [assistant_content]},
            )
        ]

    def query_selector_all(self, selector):
        if selector == CHATGPT_COMPOSER_SELECTOR:
            return [self.composer]
        if selector == CHATGPT_SEND_SELECTOR:
            return [self.send]
        if selector == LEGACY_ROLE_PROFILE.user_turn_selector:
            return self.legacy_users
        if selector == LEGACY_ROLE_PROFILE.assistant_turn_selector:
            return self.legacy_assistants
        return []


def run() -> dict[str, bool]:
    page = FakePage()
    ui = ChatGPTUIAdapter(page)
    report = ui.probe_compatibility()

    legacy_page = LegacyOnlyPage()
    default_legacy_ui = ChatGPTUIAdapter(legacy_page)
    default_legacy_report = default_legacy_ui.probe_compatibility()
    default_legacy_turns = default_legacy_ui.observation_turns("user")
    explicit_legacy_ui = ChatGPTUIAdapter(legacy_page, profile_name="chatgpt_legacy_role")
    explicit_legacy_report = explicit_legacy_ui.probe_compatibility()
    explicit_legacy_turns = explicit_legacy_ui.observation_turns("user")

    scope = ui.capture_request("new request", conversation_id="conversation-1")

    reused_user_page = FakePage()
    reused_user_ui = ChatGPTUIAdapter(reused_user_page)
    reused_user_scope = reused_user_ui.capture_request(
        "request rendered into the reused node", conversation_id="conversation-1"
    )
    reused_user_page.users[0]._text = "request rendered into the reused node"
    reused_user_page.users[0].children[
        SEARCH_UNIT_2026_PROFILE.user_content_selector
    ][0]._text = "request rendered into the reused node"
    reused_user = reused_user_ui.confirm_user_turn(reused_user_scope)

    unbound_page = FakePage()
    unbound_ui = ChatGPTUIAdapter(unbound_page)
    unbound_scope = unbound_ui.capture_request(
        "expected request not rendered", conversation_id="conversation-1"
    )
    unbound_page.users[0]._text = "unrelated rerendered user text"
    unbound_page.users[0].children[
        SEARCH_UNIT_2026_PROFILE.user_content_selector
    ][0]._text = "unrelated rerendered user text"
    unbound_runtime = WebLLMScraper.__new__(WebLLMScraper)
    unbound_runtime._run_control_hook = lambda: None
    unbound_runtime._web_ui_adapter = lambda: unbound_ui
    unbound_runtime._turn_elements = lambda role: list(unbound_page.users) if role == "user" else []
    unbound_runtime._element_fingerprint = lambda _element: "changed-user-fingerprint"
    unbound_runtime._composer_debug_state = lambda: {}
    unbound_runtime._log_composer_state = lambda _stage: {}
    unbound_runtime._log_stage = lambda *_args, **_kwargs: None
    unbound_rejected = False
    try:
        unbound_runtime._wait_for_user_sent(
            {"_web_ui_scope": unbound_scope, "user_count": 1, "last_user_fp": "old-user-fingerprint"},
            timeout_sec=0.01,
        )
    except WebScraperStageError as exc:
        unbound_rejected = exc.stage == "user_sent" and unbound_scope.user_turn is None

    # A provider may accept submit and show an active Stop control before its
    # virtualized search-unit DOM publishes the new user/assistant pair.  The
    # ACK gate must defer rather than fail at the normal timeout, then bind the
    # unique pair once it appears.
    deferred_page = FakePage()
    deferred_ui = ChatGPTUIAdapter(deferred_page)
    deferred_scope = deferred_ui.capture_request(
        "deferred request scope", conversation_id="conversation-1"
    )
    deferred_page.visible_stop = True
    deferred_calls = {"count": 0}
    deferred_original_reconcile = deferred_ui.reconcile_user_turn

    def deferred_reconcile(scope):
        deferred_calls["count"] += 1
        if deferred_calls["count"] == 2:
            deferred_page.users = [deferred_page.user(
                "deferred request scope", "fallback-turn-88:0:user", 3,
            )]
            deferred_page.assistants = [deferred_page.assistant(
                "working", "fallback-turn-88:1:assistant", 4,
            )]
        return deferred_original_reconcile(scope)

    deferred_ui.reconcile_user_turn = deferred_reconcile
    deferred_runtime = WebLLMScraper.__new__(WebLLMScraper)
    deferred_runtime._run_control_hook = lambda: None
    deferred_runtime._web_ui_adapter = lambda: deferred_ui
    deferred_runtime._turn_elements = (
        lambda role: list(deferred_page.users)
        if role == "user" else list(deferred_page.assistants)
    )
    deferred_runtime._element_fingerprint = lambda _element: "deferred-fingerprint"
    deferred_runtime._composer_debug_state = lambda: {"generation_active": True}
    deferred_runtime._is_generation_active = lambda: True
    deferred_runtime._log_composer_state = lambda _stage: {}
    deferred_runtime_states = []
    deferred_runtime_logs = []
    deferred_runtime._set_request_state = (
        lambda state, detail="": deferred_runtime_states.append((state, detail))
    )
    deferred_runtime._log_stage = (
        lambda stage, detail="": deferred_runtime_logs.append((stage, detail))
    )
    deferred_runtime._active_generation_emergency_sec = 2.0
    deferred_runtime._ui_idle_grace_sec = 0.01
    deferred_snapshot = {
        "_web_ui_scope": deferred_scope,
        "user_count": deferred_scope.baseline_user_count,
        "last_user_fp": "baseline-fingerprint",
    }
    deferred_runtime._wait_for_user_sent(deferred_snapshot, timeout_sec=0.0)
    deferred_scope_bound = bool(
        deferred_scope.user_turn
        and deferred_scope.user_turn.raw_text == "deferred request scope"
    )
    deferred_state_recorded = (
        deferred_snapshot.get("_submit_delivery_confirmed") is True
        and any(
            state == "SUBMIT_CONFIRMED_PENDING_SCOPE"
            for state, _detail in deferred_runtime_states
        )
        and any(stage == "user_scope_deferred" for stage, _detail in deferred_runtime_logs)
    )

    # Search-unit rendering may recycle a fixed number of visible turns.  The
    # new request then has neither a greater user count nor text identical to
    # the software prompt.  A unique fresh assistant unit must recover the
    # paired user anchor without guessing the last visible user.
    recycled_page = FakePage()
    recycled_ui = ChatGPTUIAdapter(recycled_page)
    recycled_scope = recycled_ui.capture_request(
        "software prompt before renderer transformation",
        conversation_id="conversation-1",
    )
    recycled_page.users = [recycled_page.user(
        "renderer transformed prompt",
        "fallback-turn-77:0:user",
        3,
    )]
    recycled_page.assistants = [recycled_page.assistant(
        "[AGENT_PROTOCOL_READY] ready",
        "fallback-turn-77:1:assistant",
        4,
    )]
    recycled_user, recycled_state = recycled_ui.reconcile_user_turn(recycled_scope)

    # Long protocol prompts may be folded or transformed by ChatGPT while the
    # search-unit renderer also recycles a fixed number of visible turns.  The
    # software-owned request marker remains visible and must bind the sole
    # changed turn without waiting for an assistant identity.
    marker_page = FakePage()
    marker_ui = ChatGPTUIAdapter(marker_page)
    marker_request_id = "RR-TELEGRAM-0AB38E305041CE4C"
    marker_scope = marker_ui.capture_request(
        "protocol header\nRUN_ID=" + marker_request_id + "\nfull software prompt",
        conversation_id="conversation-1",
    )
    marker_page.users = [marker_page.user(
        "folded protocol…\nRUN_ID=" + marker_request_id + "\n顯示較多",
        "fallback-turn-91:0:user",
        3,
    )]
    marker_user, marker_state = marker_ui.reconcile_user_turn(marker_scope)
    marker_page.visible_stop = True
    analysed = marker_page.assistant("已分析", "fallback-turn-91:1:assistant", 4)
    marker_page.assistants = [analysed]
    analysed_activity = marker_ui.activity(analysed)
    analysed_final_text = marker_ui.extract_final_text(analysed)

    recycled_scope.user_turn = None
    recycled_runtime = WebLLMScraper.__new__(WebLLMScraper)
    recycled_runtime._run_control_hook = lambda: None
    recycled_runtime._web_ui_adapter = lambda: recycled_ui
    recycled_runtime._turn_elements = (
        lambda role: list(recycled_page.users) if role == "user" else list(recycled_page.assistants)
    )
    recycled_runtime._element_fingerprint = lambda _element: "recycled-fingerprint"
    recycled_runtime._composer_debug_state = lambda: {}
    recycled_runtime._log_composer_state = lambda _stage: {}
    recycled_runtime_logs = []
    recycled_runtime._log_stage = (
        lambda stage, detail="": recycled_runtime_logs.append((stage, detail))
    )
    recycled_runtime._wait_for_user_sent(
        {
            "_web_ui_scope": recycled_scope,
            "user_count": recycled_scope.baseline_user_count,
            "last_user_fp": "baseline-fingerprint",
        },
        timeout_sec=0.1,
    )
    recycled_runtime_state_logged = any(
        stage == "user_scope_reconcile"
        and '"before_confirmed": false' in detail
        and '"after_confirmed": true' in detail
        and '"strategy": "fresh_assistant_conversation_unit"' in detail
        for stage, detail in recycled_runtime_logs
    )

    ambiguous_page = FakePage()
    ambiguous_ui = ChatGPTUIAdapter(ambiguous_page)
    ambiguous_page.users.append(
        ambiguous_page.user("old second", "turn:2:user", 3)
    )
    ambiguous_page.assistants.append(
        ambiguous_page.assistant("old second answer", "turn:2:assistant", 4)
    )
    ambiguous_scope = ambiguous_ui.capture_request(
        "software prompt before ambiguous renderer change",
        conversation_id="conversation-1",
    )
    ambiguous_page.users = [
        ambiguous_page.user("first", "fallback-turn-80:0:user", 3),
        ambiguous_page.user("second", "fallback-turn-81:0:user", 5),
    ]
    ambiguous_page.assistants = [
        ambiguous_page.assistant("first answer", "fallback-turn-80:1:assistant", 4),
        ambiguous_page.assistant("second answer", "fallback-turn-81:1:assistant", 6),
    ]
    ambiguous_user, ambiguous_state = ambiguous_ui.reconcile_user_turn(ambiguous_scope)

    page.users.append(page.user(
        "newrequest",
        "turn:2:user",
        3,
        root_text="newrequest\n顯示更多",
    ))
    user = ui.confirm_user_turn(scope)

    # Re-rendering or mutating the previous answer cannot cross the request
    # boundary because that assistant remains before the current user turn.
    page.assistants[0]._text = "/mnt/data/stale-image.png"
    page.assistants[0].children[SEARCH_UNIT_2026_PROFILE.final_content_selector][0]._text = "/mnt/data/stale-image.png"
    stale_rejected = ui.latest_owned_assistant(scope) is None

    placeholder = page.assistant("思考中", "turn:2:assistant", 4)
    page.assistants.append(placeholder)
    owned = ui.latest_owned_assistant(scope)
    placeholder_rejected = owned is not None and ui.extract_final_text(owned) == ""

    placeholder._text = "[AGENT_SESSION_READY] session_id=SESSION-2"
    placeholder.children[SEARCH_UNIT_2026_PROFILE.final_content_selector][0]._text = placeholder._text
    final_text = ui.extract_final_text(ui.latest_owned_assistant(scope))

    # A renderer-altered long prompt can fail semantic text comparison.  The
    # single user ordinal added after capture remains a safe request anchor.
    ordinal_scope = ui.capture_request(
        "long expected prompt with software-owned protocol framing",
        conversation_id="conversation-1",
    )
    page.users.append(page.user(
        "renderer changed this long prompt substantially",
        "turn:3:user",
        5,
    ))
    ordinal_user = ui.confirm_user_turn(ordinal_scope)
    ordinal_assistant = page.assistant("owned by ordinal fallback", "turn:3:assistant", 6)
    page.assistants.append(ordinal_assistant)
    ordinal_owned = ui.latest_owned_assistant(ordinal_scope)

    # ChatGPT may replace the user node after it was confirmed.  Make the old
    # handle unusable for order comparison and verify ownership automatically
    # rebinds to the live node with the same semantic identity/ordinal.
    assert ordinal_user is not None
    ordinal_user.element.order = 1000
    page.users[-1] = page.user(
        "renderer changed this long prompt substantially",
        "turn:3:user",
        5,
    )
    rebound_owned_without_explicit_rebind = ui.latest_owned_assistant(ordinal_scope)

    # Search/fallback renderers can rotate the newest request from the last
    # query ordinal to the first while retaining its rendered content.  The
    # saved rendered request, not its former ordinal, must remain authoritative.
    rotated_current_user = page.user(
        "renderer changed this long prompt substantially",
        "turn:rotated:user",
        5,
    )
    unrelated_last_user = page.user("older short request", "turn:old:user", 3)
    page.users = [rotated_current_user, page.users[0], unrelated_last_user]
    ordinal_scope.user_turn.element.order = 1000
    rebound_after_ordinal_rotation = ui.latest_owned_assistant(ordinal_scope)

    # A provider may place the paired assistant before its user in document
    # order after search-unit rotation.  Same-snapshot conversation unit
    # identity remains sufficient and must take precedence over _is_after().
    old_grouped_assistant = page.assistant(
        "stale grouped response",
        "fallback-turn-9:1:assistant",
        90,
    )
    page.assistants.append(old_grouped_assistant)
    grouped_scope = ui.capture_request("grouped request", conversation_id="conversation-1")
    grouped_user = page.user(
        "grouped request",
        "fallback-turn-9:0:user",
        200,
    )
    grouped_assistant = page.assistant(
        "grouped response",
        "fallback-turn-9:2:assistant",
        100,
    )
    page.users.append(grouped_user)
    grouped_confirmed = ui.confirm_user_turn(grouped_scope)
    stale_group_rejected = ui.latest_owned_assistant(grouped_scope) is None
    page.assistants.append(grouped_assistant)
    grouped_owned = ui.latest_owned_assistant(grouped_scope)

    # A reload recreates ElementHandles; semantic turn identity must rebind
    # without treating the pre-request assistant as the current response.
    page.users = [
        page.user("old request", "turn:1:user", 1),
        page.user("newrequest", "turn:2:user", 3, root_text="newrequest\n顯示更多"),
    ]
    page.assistants = [
        page.assistant("old answer after reload", "turn:1:assistant", 2),
        page.assistant("[AGENT_SESSION_READY] session_id=SESSION-2", "turn:2:assistant", 4),
    ]
    rebound = ui.rebind_user_turn(scope)
    rebound_assistant = ui.latest_owned_assistant(scope)

    # Runtime must not bypass RequestScope when the preceding assistant node is
    # re-rendered before the genuinely new assistant turn is mounted.
    runtime = WebLLMScraper.__new__(WebLLMScraper)
    runtime._generation_stall_sec = 1.0
    runtime._active_generation_emergency_sec = 1.0
    runtime._active_generation_warn_sec = 1.0
    runtime._run_control_hook = lambda: False
    runtime._check_cancel_requested = lambda _stage: None
    runtime._maybe_recover_disconnected_generation = lambda: False
    runtime._disconnect_signature_visible = lambda: False
    runtime._page_media_state = lambda: {}
    runtime._fresh_ready_page_image_state = lambda *_args, **_kwargs: None
    runtime._is_generation_active = lambda: False
    runtime._response_elements = lambda: []
    stale_element = page.assistants[0]
    runtime._turn_elements = lambda role: [stale_element] if role == "assistant" else []
    runtime._element_fingerprint = lambda _element: "rerendered-stale-fingerprint"
    runtime._log_stage = lambda *_args, **_kwargs: None

    class SequencedAdapter:
        def __init__(self, fresh):
            self.calls = 0
            self.fresh = fresh

        def latest_owned_assistant(self, _scope):
            self.calls += 1
            return self.fresh if self.calls >= 3 else None

    sequenced = SequencedAdapter(rebound_assistant)
    runtime._web_ui_adapter = lambda: sequenced
    runtime_owned = runtime._wait_for_new_assistant_turn(
        {
            "_web_ui_scope": scope,
            "assistant_count": 1,
            "response_count": 1,
            "last_assistant_fp": "original-stale-fingerprint",
            "last_response_fp": "original-response-fingerprint",
        },
        timeout_sec=1.0,
        allow_fresh_ready_image=False,
    )

    results = {
        "search_profile_selected": report.selected_profile == "chatgpt_search_unit_2026",
        "compatibility_supported": report.supported,
        "default_profile_pinned_on_legacy_dom": (
            default_legacy_report.selected_profile == "chatgpt_search_unit_2026"
        ),
        "no_auto_legacy_turn_fallback": default_legacy_turns == (),
        "explicit_legacy_profile_only": bool(
            explicit_legacy_report.selected_profile == "chatgpt_legacy_role"
            and explicit_legacy_turns
            and explicit_legacy_turns[0].raw_text == "legacy request"
        ),
        "exact_user_turn_bound": bool(user and user.ordinal == 2),
        "reused_user_node_bound_by_new_prompt_content": bool(
            reused_user
            and reused_user.ordinal == 1
            and reused_user_scope.user_turn is reused_user
        ),
        "scoped_fingerprint_only_user_mutation_rejected": unbound_rejected,
        "active_generation_defers_user_scope_timeout": deferred_scope_bound,
        "deferred_user_scope_delivery_state_recorded": deferred_state_recorded,
        "recycled_user_bound_from_unique_fresh_assistant_unit": bool(
            recycled_user
            and recycled_user.raw_text == "renderer transformed prompt"
            and recycled_state.get("before_confirmed") is False
            and recycled_state.get("after_confirmed") is True
            and recycled_state.get("strategy") == "fresh_assistant_conversation_unit"
            and recycled_state.get("fresh_assistant_count") == 1
            and recycled_state.get("paired_user_count") == 1
        ),
        "recycled_user_binding_transition_logged": recycled_runtime_state_logged,
        "folded_recycled_user_bound_by_request_marker": bool(
            marker_user
            and marker_user.raw_text.startswith("folded protocol")
            and marker_state.get("strategy") == "existing_or_direct_confirmation"
            and marker_state.get("request_marker_count") == 1
            and marker_state.get("bound_marker_match") is True
        ),
        "analysed_marker_observed_without_completing_turn": bool(
            analysed_activity.analysis_complete_visible
            and analysed_activity.generating
            and analysed_final_text == ""
        ),
        "ambiguous_fresh_assistant_units_fail_closed": bool(
            ambiguous_user is None
            and ambiguous_state.get("after_confirmed") is False
            and ambiguous_state.get("reason") == "multiple_fresh_assistant_units"
        ),
        "folded_user_controls_excluded": bool(user and user.raw_text == "newrequest"),
        "whitespace_fallback_did_not_mutate_prompt": scope.prompt_text == "new request",
        "stale_assistant_rejected": stale_rejected,
        "thinking_placeholder_rejected": placeholder_rejected,
        "owned_final_content_extracted": final_text.startswith("[AGENT_SESSION_READY]"),
        "unique_new_user_ordinal_bound_when_text_differs": bool(
            ordinal_user and ordinal_user.ordinal == 3
        ),
        "ordinal_bound_user_owns_following_assistant": bool(
            ordinal_owned and ordinal_owned.raw_text == "owned by ordinal fallback"
        ),
        "detached_user_anchor_rebound_before_ownership": bool(
            rebound_owned_without_explicit_rebind
            and rebound_owned_without_explicit_rebind.raw_text == "owned by ordinal fallback"
        ),
        "rendered_user_identity_survives_ordinal_rotation": bool(
            ordinal_scope.user_turn.raw_text == "renderer changed this long prompt substantially"
            and rebound_after_ordinal_rotation
            and rebound_after_ordinal_rotation.raw_text == "owned by ordinal fallback"
        ),
        "same_snapshot_conversation_unit_overrides_dom_order": bool(
            grouped_confirmed
            and grouped_owned
            and grouped_owned.raw_text == "grouped response"
            and not ui._is_after(grouped_confirmed, grouped_owned)
        ),
        "baseline_assistant_in_reused_unit_rejected": stale_group_rejected,
        "reload_user_rebound": bool(rebound and rebound.ordinal == 2),
        "reload_owned_assistant_preserved": bool(rebound_assistant and rebound_assistant.ordinal == 2),
        "runtime_rejects_rerendered_stale_assistant": runtime_owned is rebound_assistant,
    }
    results["all_passed"] = all(results.values())
    return results


if __name__ == "__main__":
    result = run()
    print(result)
    raise SystemExit(0 if result["all_passed"] else 1)
