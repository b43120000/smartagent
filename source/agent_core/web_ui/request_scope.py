"""Request-scoped facade over provider-neutral Web UI observations."""
from __future__ import annotations

from .contracts import RequestScope, TurnRef, UIResponse
from .provider_contract import WebUIProviderAdapter


class RequestScopeTracker:
    def __init__(self, adapter: WebUIProviderAdapter):
        self.adapter = adapter

    def begin(self, prompt: str, *, conversation_id: str = "") -> RequestScope:
        return self.adapter.capture_request(prompt, conversation_id=conversation_id)

    def confirm_user(self, scope: RequestScope) -> TurnRef | None:
        return self.adapter.confirm_user_turn(scope)

    def current_assistant(self, scope: RequestScope) -> TurnRef | None:
        return self.adapter.latest_owned_assistant(scope)

    def response(self, scope: RequestScope) -> UIResponse | None:
        assistant = self.current_assistant(scope)
        if assistant is None or scope.user_turn is None:
            return None
        text = self.adapter.extract_final_text(assistant)
        activity = self.adapter.activity(assistant)
        if not text:
            return None
        return UIResponse(
            scope_id=scope.scope_id,
            profile_name=scope.profile_name,
            user_turn=scope.user_turn,
            assistant_turn=assistant,
            final_text=text,
            activity=activity,
        )
