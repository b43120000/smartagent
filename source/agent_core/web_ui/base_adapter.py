"""Provider-neutral algorithms for stable Web UI observations."""
from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Iterable

from .contracts import (
    ArtifactObservation,
    ActivitySnapshot,
    AuthenticationState,
    CompatibilityReport,
    RequestScope,
    TurnRef,
    UISnapshot,
    normalize_ui_text,
    text_digest,
    ui_text_matches,
)


_PLACEHOLDER_TEXTS = {
    "思考中", "正在思考", "已分析", "thinking", "thinking…", "thinking...",
    "working", "working…", "working...", "generating", "generating…",
}

_REQUEST_MARKER_PATTERNS = (
    re.compile(r"\bRUN_ID\s*=\s*(RR-[A-Z0-9][A-Z0-9_-]{5,})\b", re.IGNORECASE),
    re.compile(
        r'''["']request_id["']\s*:\s*["'](RR-[A-Z0-9][A-Z0-9_-]{5,})["']''',
        re.IGNORECASE,
    ),
)


def _request_markers(value: object) -> tuple[str, ...]:
    """Extract software-owned request markers that survive UI text folding."""
    text = str(value or "")
    markers: list[str] = []
    for pattern in _REQUEST_MARKER_PATTERNS:
        for match in pattern.finditer(text):
            marker = str(match.group(1) or "").upper()
            if marker and marker not in markers:
                markers.append(marker)
    return tuple(markers)


def _safe_attr(element, name: str) -> str:
    try:
        return str(element.get_attribute(name) or "")
    except Exception:
        return ""


def _safe_text(element) -> str:
    for method in ("inner_text", "text_content"):
        try:
            value = getattr(element, method)()
            if value is not None:
                return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
        except Exception:
            continue
    return ""


class BaseWebUIAdapter:
    """Shared request ownership and state algorithms without provider selectors."""

    provider_name = ""
    bridge_script = ""
    bridge_version = 0
    attachments = None
    stable_turn_attributes = ("data-message-id", "data-testid", "id")
    authenticated_session_selectors: tuple[str, ...] = ()
    login_required_selectors: tuple[str, ...] = ()
    login_url_tokens: tuple[str, ...] = ()

    @classmethod
    def load_profile_by_name(cls, name: str):
        raise NotImplementedError

    @classmethod
    def load_active_profile(cls):
        raise NotImplementedError

    def probe_provider_profile(self, profile) -> CompatibilityReport:
        raise NotImplementedError

    def read_provider_composer(self, composer):
        raise NotImplementedError

    def validate_provider_composer(self, prompt: str, snapshot):
        raise NotImplementedError

    def __init__(self, page, *, profile_name: str = ""):
        self.page = page
        requested = str(profile_name or "").strip()
        if requested:
            profile = self.load_profile_by_name(requested)
            if profile is None:
                raise RuntimeError(f"WEB_UI_PROFILE_UNKNOWN: {requested}")
            self.profile = profile
        else:
            self.profile = self.load_active_profile()
        self._compatibility_report: CompatibilityReport | None = None

    def probe_compatibility(self, *, force: bool = False) -> CompatibilityReport:
        if self._compatibility_report is not None and not force:
            return self._compatibility_report
        report = self.probe_provider_profile(self.profile)
        self._compatibility_report = report
        return report

    def require_compatible(self) -> CompatibilityReport:
        report = self.probe_compatibility()
        if not report.supported:
            raise RuntimeError(
                "WEB_UI_PROFILE_UNSUPPORTED: " + ",".join(report.reasons or ("unknown",))
            )
        return report

    def _active_profile(self) -> Any:
        self.require_compatible()
        assert self.profile is not None
        return self.profile

    def visible_composer(self):
        profile = self._active_profile()
        locator = self.page.locator(profile.composer_selector)
        for index in range(int(locator.count()) - 1, -1, -1):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return locator.last if locator.count() else None

    def send_control(self):
        profile = self._active_profile()
        locator = self.page.locator(profile.send_selector)
        for index in range(int(locator.count()) - 1, -1, -1):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return locator.last if locator.count() else None

    def composer_snapshot(self):
        """Return normalized composer views without leaking DOM rules."""
        return self.read_provider_composer(self.visible_composer())

    def validate_composer(self, prompt: str):
        return self.validate_provider_composer(prompt, self.composer_snapshot())

    def stop_controls(self) -> tuple:
        profile = self._active_profile()
        controls = []
        for selector in profile.stop_selectors:
            try:
                locator = self.page.locator(selector)
                for index in range(int(locator.count())):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        controls.append(candidate)
            except Exception:
                continue
        return tuple(controls)

    def generation_active(self) -> bool:
        return bool(self.stop_controls())

    def turn_elements(self, role: str) -> list:
        profile = self._active_profile()
        selector = profile.user_turn_selector if role == "user" else profile.assistant_turn_selector
        try:
            return list(self.page.query_selector_all(selector))
        except Exception:
            return []

    def _turn_text(self, element, role: str) -> str:
        """Return semantic message content without turn-level UI controls."""
        if role == "user" and self.profile is not None:
            try:
                candidates = element.query_selector_all(self.profile.user_content_selector)
            except Exception:
                candidates = ()
            texts = [_safe_text(candidate) for candidate in candidates]
            texts = [text for text in texts if text]
            if texts:
                # Nested renderer wrappers can expose the same message more than
                # once.  The longest content node preserves the full prompt while
                # excluding sibling controls exposed by provider renderers.
                return max(texts, key=len)
        return _safe_text(element)

    def _turn_ref(self, element, role: str, ordinal: int) -> TurnRef:
        attrs = tuple(
            (name, value)
            for name in self.stable_turn_attributes
            if (value := _safe_attr(element, name))
        )
        raw_text = self._turn_text(element, role)
        structural_source = "\n".join(f"{name}={value}" for name, value in attrs)
        if not structural_source:
            structural_source = f"{role}:ordinal:{ordinal}"
        structural_id = hashlib.sha256(
            f"{role}\n{structural_source}".encode("utf-8", errors="replace")
        ).hexdigest()
        return TurnRef(
            role=role,
            ordinal=ordinal,
            structural_id=structural_id,
            content_digest=text_digest(raw_text),
            raw_text=raw_text,
            stable_attributes=attrs,
            element=element,
        )

    def turns(self, role: str) -> tuple[TurnRef, ...]:
        result = []
        seen = set()
        for element in self.turn_elements(role):
            turn = self._turn_ref(element, role, len(result) + 1)
            if turn.structural_id in seen:
                continue
            seen.add(turn.structural_id)
            result.append(turn)
        return tuple(result)

    def observation_turns(self, role: str) -> tuple[TurnRef, ...]:
        """Passive observation uses the pinned provider profile; no selector fallback."""
        self.require_compatible()
        return self.turns(role)

    def rendered_turn_text(self, turn: TurnRef | object | None) -> str:
        if turn is None:
            return ""
        if isinstance(turn, TurnRef):
            return str(turn.raw_text or "").strip()
        return _safe_text(turn).strip()

    def turn_attachment_signatures(self, turn: TurnRef | object | None) -> tuple[str, ...]:
        if turn is None:
            return ()
        element = turn.element if isinstance(turn, TurnRef) else turn
        try:
            nodes = list(element.query_selector_all("[data-testid*='file'], [data-testid*='attachment'], img, video, a"))
        except Exception:
            nodes = []
        values: list[str] = []
        for node in nodes:
            parts: list[str] = []
            for attr in ("download", "aria-label", "title", "alt", "data-testid"):
                value = normalize_ui_text(_safe_attr(node, attr))
                if value:
                    parts.append(f"{attr}={value}")
            text = normalize_ui_text(_safe_text(node))
            if text:
                parts.append(f"text={text}")
            if parts:
                values.append("|".join(parts))
        return tuple(sorted(set(values)))

    def element_fingerprint(self, element) -> str:
        if element is None:
            return ""
        if isinstance(element, TurnRef):
            element = element.element
        if element is None:
            return ""
        parts = []
        for name in ("data-message-id", "data-testid", "id"):
            value = _safe_attr(element, name)
            if value:
                parts.append(f"{name}={value}")
        text = _safe_text(element)
        if text:
            parts.append(text)
        if not parts:
            return ""
        return hashlib.sha256("\n".join(parts).encode("utf-8", errors="replace")).hexdigest()

    def disconnect_signature_visible(self) -> bool:
        return False

    def page_reachable(self) -> bool:
        try:
            return bool(self.page.locator("body").count())
        except Exception:
            return False

    def _any_visible(self, selectors: Iterable[str]) -> bool:
        for selector in selectors:
            if not str(selector or "").strip():
                continue
            try:
                locator = self.page.locator(selector)
                for index in range(int(locator.count())):
                    if locator.nth(index).is_visible():
                        return True
            except Exception:
                continue
        return False

    def authentication_state(self) -> AuthenticationState:
        """Return provider-normalized, read-only login evidence."""
        profile = getattr(self, "profile", None)
        composer_selector = str(getattr(profile, "composer_selector", "") or "")
        authenticated_selectors = ((composer_selector,) if composer_selector else ()) + tuple(
            self.authenticated_session_selectors
        )
        if self._any_visible(authenticated_selectors):
            return AuthenticationState(
                provider=self.provider_name, status="AUTHENTICATED",
                authenticated=True, reason="authenticated_ui_visible",
            )
        try:
            current_url = str(getattr(self.page, "url", "") or "").lower()
        except Exception:
            current_url = ""
        if (
            any(token.lower() in current_url for token in self.login_url_tokens)
            or self._any_visible(self.login_required_selectors)
        ):
            return AuthenticationState(
                provider=self.provider_name, status="LOGIN_REQUIRED",
                authenticated=False, reason="login_ui_visible",
            )
        return AuthenticationState(
            provider=self.provider_name, status="UNKNOWN",
            authenticated=False,
            reason="page_reachable_without_authenticated_ui" if self.page_reachable()
            else "page_unreachable",
        )

    def artifact_observations(self, root=None) -> tuple[ArtifactObservation, ...]:
        root = self.page if root is None else root
        selector = (
            'a, button, [role="button"], img, '
            '[data-testid*="download"], [data-testid*="file"], [data-testid*="artifact"], '
            '[aria-label*="download" i], [aria-label*="file" i], [title*="download" i]'
        )
        try:
            elements = list(root.query_selector_all(selector))
        except Exception:
            return ()
        out = []
        for element in elements:
            try:
                tag = str(element.evaluate("el => (el.tagName || '').toLowerCase()") or "").strip().lower()
            except Exception:
                tag = ""
            href = _safe_attr(element, "href").strip()
            src = _safe_attr(element, "src").strip()
            image_info = {}
            if tag == "img":
                try:
                    image_info = element.evaluate(
                        """el => ({
                            src: el.currentSrc || el.src || '',
                            complete: !!el.complete,
                            naturalWidth: Number(el.naturalWidth || 0),
                            naturalHeight: Number(el.naturalHeight || 0),
                            renderedWidth: Number(el.getBoundingClientRect().width || 0),
                            renderedHeight: Number(el.getBoundingClientRect().height || 0),
                            visible: !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'),
                            alt: el.getAttribute('alt') || '',
                            aria_label: el.getAttribute('aria-label') || '',
                            testid: el.getAttribute('data-testid') || '',
                            class_name: String(el.className || '')
                        })"""
                    ) or {}
                    src = str(image_info.get("src") or src).strip()
                except Exception:
                    pass
            out.append(ArtifactObservation(
                element=element,
                tag=tag,
                href=href,
                src=src,
                text=_safe_text(element),
                aria_label=_safe_attr(element, "aria-label").strip(),
                title=_safe_attr(element, "title").strip(),
                testid=_safe_attr(element, "data-testid").strip(),
                download_name=_safe_attr(element, "download").strip(),
                data_filename=_safe_attr(element, "data-filename").strip(),
                filename_attr=_safe_attr(element, "filename").strip(),
                visible=bool(image_info.get("visible")) if tag == "img" else False,
                complete=bool(image_info.get("complete")) if tag == "img" else False,
                natural_width=int(image_info.get("naturalWidth") or 0),
                natural_height=int(image_info.get("naturalHeight") or 0),
                rendered_width=float(image_info.get("renderedWidth") or 0),
                rendered_height=float(image_info.get("renderedHeight") or 0),
                generated_media=self._is_relevant_assistant_image(image_info) if tag == "img" else False,
            ))
        return tuple(out)

    def snapshot(self) -> UISnapshot:
        profile = self._active_profile()
        return UISnapshot(
            profile_name=profile.name,
            user_turns=self.turns("user"),
            assistant_turns=self.turns("assistant"),
        )

    def capture_request(self, prompt: str, *, conversation_id: str = "") -> RequestScope:
        snapshot = self.snapshot()
        return RequestScope(
            scope_id="WEBUI-" + uuid.uuid4().hex.upper(),
            conversation_id=str(conversation_id or ""),
            profile_name=snapshot.profile_name,
            prompt_digest=text_digest(prompt),
            prompt_text=normalize_ui_text(prompt),
            baseline_user_ids=tuple(turn.structural_id for turn in snapshot.user_turns),
            baseline_user_content_digests=tuple(turn.content_digest for turn in snapshot.user_turns),
            baseline_assistant_ids=tuple(
                self.durable_turn_id(turn) for turn in snapshot.assistant_turns
            ),
            baseline_user_count=len(snapshot.user_turns),
            baseline_assistant_count=len(snapshot.assistant_turns),
            request_markers=_request_markers(prompt),
        )

    def _confirmed_user_turn_candidate(
        self,
        scope: RequestScope,
        turns: tuple[TurnRef, ...],
    ) -> TurnRef | None:
        """Bind the submitted user turn without trusting text alone.

        Renderers may insert paragraph/link whitespace or other presentation
        text, especially for long protocol prompts.  Exact semantic text remains
        the first proof.  When it differs, one and only one ordinal beyond the
        pre-submit boundary is still safe: it is the sole user turn created by
        this submit.  Mutations of an existing last user node cannot satisfy the
        ordinal condition.
        """
        baseline = set(scope.baseline_user_ids)
        request_markers = set(scope.request_markers)
        if request_markers:
            marker_matches = [
                turn for turn in turns
                if request_markers.intersection(_request_markers(turn.raw_text))
                and (
                    turn.structural_id not in baseline
                    or turn.ordinal > scope.baseline_user_count
                    or (
                        1 <= turn.ordinal <= len(scope.baseline_user_content_digests)
                        and turn.content_digest
                        != scope.baseline_user_content_digests[turn.ordinal - 1]
                    )
                )
            ]
            if len(marker_matches) == 1:
                return marker_matches[0]
        text_matches = [
            turn for turn in turns
            if ui_text_matches(turn.raw_text, scope.prompt_text)
            and (
                turn.structural_id not in baseline
                or turn.ordinal > scope.baseline_user_count
                or (
                    1 <= turn.ordinal <= len(scope.baseline_user_content_digests)
                    and turn.content_digest != scope.baseline_user_content_digests[turn.ordinal - 1]
                )
            )
        ]
        if text_matches:
            return text_matches[-1]
        ordinal_new = [
            turn for turn in turns
            if turn.ordinal > scope.baseline_user_count
        ]
        if len(ordinal_new) == 1:
            return ordinal_new[0]

        return None

    def confirm_user_turn(self, scope: RequestScope) -> TurnRef | None:
        if self.profile is None or self.profile.name != scope.profile_name:
            self.profile = self.load_profile_by_name(scope.profile_name)
            self._compatibility_report = None
        turns = self.turns("user")
        candidate = self._confirmed_user_turn_candidate(scope, turns)
        if candidate is None:
            return None
        scope.user_turn = candidate
        return scope.user_turn

    def reconcile_user_turn(
        self, scope: RequestScope
    ) -> tuple[TurnRef | None, dict[str, Any]]:
        """Recover a request anchor after a provider recycles visible turns.

        Some provider render modes keep a fixed number of search units.  A new
        request can therefore replace a visible user/assistant pair without
        increasing either DOM count.  If rendered prompt text also differs
        from the software prompt, the normal user confirmation cannot bind the
        request even though the paired assistant is already visible.

        Recovery is deliberately fail-closed: only one fresh assistant
        conversation unit and exactly one user in that same provider-owned
        unit may establish the missing user anchor.  The returned diagnostic
        contains counts and hashes only; it never exposes prompt or response
        text.
        """
        before_confirmed = scope.user_turn is not None
        candidate = self.confirm_user_turn(scope)
        if candidate is not None:
            marker_match = bool(
                set(scope.request_markers).intersection(_request_markers(candidate.raw_text))
            )
            state = {
                "before_confirmed": before_confirmed,
                "after_confirmed": True,
                "strategy": "existing_or_direct_confirmation",
                "reason": "confirmed",
                "baseline_user_count": int(scope.baseline_user_count),
                "baseline_assistant_count": int(scope.baseline_assistant_count),
                "current_user_count": len(self.turns("user")),
                "fresh_assistant_count": 0,
                "fresh_unit_count": 0,
                "paired_user_count": 1,
                "request_marker_count": len(scope.request_markers),
                "bound_marker_match": marker_match,
                "user_ordinal": int(candidate.ordinal),
                "user_id_sha": hashlib.sha256(
                    str(candidate.structural_id or "").encode("utf-8", errors="replace")
                ).hexdigest()[:12],
                "conversation_unit_sha": "",
            }
            return candidate, state

        users = self.turns("user")
        assistants = self.turns("assistant")
        baseline = set(scope.baseline_assistant_ids)
        fresh_assistants = tuple(
            turn for turn in assistants
            if self.durable_turn_id(turn) not in baseline
        )
        fresh_units = {
            self.conversation_unit_id(turn)
            for turn in fresh_assistants
            if self.conversation_unit_id(turn)
        }
        paired_users: tuple[TurnRef, ...] = ()
        unit_id = ""
        reason = "no_fresh_assistant_unit"
        if len(fresh_units) > 1:
            reason = "multiple_fresh_assistant_units"
        elif len(fresh_units) == 1:
            unit_id = next(iter(fresh_units))
            paired_users = tuple(
                turn for turn in users
                if self.conversation_unit_id(turn) == unit_id
            )
            if len(paired_users) == 1:
                scope.user_turn = paired_users[0]
                reason = "paired_unique_user"
            elif not paired_users:
                reason = "fresh_unit_has_no_user"
            else:
                reason = "fresh_unit_has_multiple_users"

        bound = scope.user_turn
        state = {
            "before_confirmed": before_confirmed,
            "after_confirmed": bound is not None,
            "strategy": "fresh_assistant_conversation_unit" if bound is not None else "unresolved",
            "reason": reason,
            "baseline_user_count": int(scope.baseline_user_count),
            "baseline_assistant_count": int(scope.baseline_assistant_count),
            "current_user_count": len(users),
            "fresh_assistant_count": len(fresh_assistants),
            "fresh_unit_count": len(fresh_units),
            "paired_user_count": len(paired_users),
            "request_marker_count": len(scope.request_markers),
            "bound_marker_match": False,
            "user_ordinal": int(getattr(bound, "ordinal", 0) or 0),
            "user_id_sha": hashlib.sha256(
                str(getattr(bound, "structural_id", "") or "").encode(
                    "utf-8", errors="replace"
                )
            ).hexdigest()[:12] if bound is not None else "",
            "conversation_unit_sha": hashlib.sha256(
                unit_id.encode("utf-8", errors="replace")
            ).hexdigest()[:12] if unit_id else "",
        }
        return bound, state

    def rebind_user_turn(self, scope: RequestScope) -> TurnRef | None:
        """Rebind the semantic user anchor after a page reload rebuilt the DOM."""
        turns = self.turns("user")
        if scope.user_turn is not None:
            # Provider fallback keys and query ordinals may be reassigned after
            # a render.  The rendered text captured from the confirmed submit
            # is a stronger anchor than either of those volatile positions.
            rendered_matches = [
                turn for turn in turns
                if ui_text_matches(turn.raw_text, scope.user_turn.raw_text)
            ]
            if len(rendered_matches) == 1:
                scope.user_turn = rendered_matches[0]
                return scope.user_turn
            same_identity = [
                turn for turn in turns
                if turn.structural_id == scope.user_turn.structural_id
            ]
            if same_identity:
                scope.user_turn = same_identity[-1]
                return scope.user_turn
            same_ordinal = [
                turn for turn in turns
                if turn.ordinal == scope.user_turn.ordinal
                and turn.ordinal > scope.baseline_user_count
            ]
            if same_ordinal:
                scope.user_turn = same_ordinal[-1]
                return scope.user_turn
        prompt_matches = [
            turn for turn in turns
            if ui_text_matches(turn.raw_text, scope.prompt_text)
        ]
        if len(prompt_matches) != 1:
            return None
        scope.user_turn = prompt_matches[0]
        return scope.user_turn

    def _is_after(self, earlier: TurnRef, later: TurnRef) -> bool:
        if earlier.element is None or later.element is None:
            return False
        try:
            return bool(later.element.evaluate(
                "(later, earlier) => Boolean(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING)",
                earlier.element,
            ))
        except Exception:
            pass
        try:
            return bool(self.page.evaluate(
                "([earlier, later]) => Boolean(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING)",
                [earlier.element, later.element],
            ))
        except Exception:
            pass
        try:
            return int(getattr(later.element, "order")) > int(getattr(earlier.element, "order"))
        except Exception:
            return False

    def conversation_unit_id(self, turn: TurnRef) -> str:
        """Return a provider-local user/assistant pairing key when available."""
        return ""

    def durable_turn_id(self, turn: TurnRef) -> str:
        """Return identity that survives provider-local unit renumbering."""
        return str(turn.structural_id or "")

    def analysis_complete_visible(self, assistant: TurnRef | object | None = None) -> bool:
        """Provider hook for an analysis-finished UI marker; never implies turn completion."""
        return False

    def owned_assistant_turns(self, scope: RequestScope) -> tuple[TurnRef, ...]:
        # A provider renderer can recreate the confirmed user node while the
        # assistant is mounting. Always rebind before comparing document order;
        # an ElementHandle captured immediately after submit may already be
        # detached even though its semantic turn still exists.
        if self.rebind_user_turn(scope) is None:
            return ()
        baseline = set(scope.baseline_assistant_ids)
        assert scope.user_turn is not None
        assistants = self.turns("assistant")
        unit_id = self.conversation_unit_id(scope.user_turn)
        if unit_id:
            # Unit pairing is evaluated from one current snapshot, so it stays
            # valid even when the provider renumbers or reorders units between
            # polls.  It is stronger than ordinal and document position.
            grouped = tuple(
                turn for turn in assistants
                if self.conversation_unit_id(turn) == unit_id
                and self.durable_turn_id(turn) not in baseline
            )
            if grouped:
                return grouped
        return tuple(
            turn for turn in assistants
            if self.durable_turn_id(turn) not in baseline
            and self._is_after(scope.user_turn, turn)
        )

    def latest_owned_assistant(self, scope: RequestScope) -> TurnRef | None:
        turns = self.owned_assistant_turns(scope)
        return turns[-1] if turns else None

    def assistant_wait_fallbacks(
        self, markers: tuple[str, ...], *, limit: int = 3
    ) -> dict:
        return {}

    def latest_conversation_turn(self) -> TurnRef | None:
        users = self.observation_turns("user")
        assistants = self.observation_turns("assistant")
        if not users:
            return assistants[-1] if assistants else None
        if not assistants:
            return users[-1]
        return assistants[-1] if self._is_after(users[-1], assistants[-1]) else users[-1]

    def extract_final_text(self, assistant: TurnRef | object | None) -> str:
        if assistant is None:
            return ""
        element = assistant.element if isinstance(assistant, TurnRef) else assistant
        profile = self._active_profile()
        try:
            blocks: Iterable = element.query_selector_all(profile.final_content_selector)
        except Exception:
            blocks = ()
        texts: list[str] = []
        for block in blocks:
            text = _safe_text(block).strip()
            normalized = normalize_ui_text(text).lower()
            if text and normalized not in _PLACEHOLDER_TEXTS and text not in texts:
                texts.append(text)
        if texts:
            return "\n\n".join(texts).strip()
        # Some renderer variants place the final-content marker on the turn root.
        try:
            root_is_final = bool(element.evaluate(
                "(el, selector) => el.matches(selector)", profile.final_content_selector
            ))
        except Exception:
            root_is_final = False
        text = _safe_text(element).strip() if root_is_final else ""
        return "" if normalize_ui_text(text).lower() in _PLACEHOLDER_TEXTS else text

    def activity(self, assistant: TurnRef | object | None = None) -> ActivitySnapshot:
        profile = self._active_profile()
        root = assistant.element if isinstance(assistant, TurnRef) else assistant
        generating = False
        for selector in profile.stop_selectors:
            try:
                locator = self.page.locator(selector)
                if locator.count() and any(locator.nth(i).is_visible() for i in range(locator.count())):
                    generating = True
                    break
            except Exception:
                continue
        busy_count = 0
        image_count = video_count = canvas_count = 0
        if root is not None:
            for selector in profile.busy_selectors:
                try:
                    busy_count += sum(
                        1 for element in root.query_selector_all(selector)
                        if not hasattr(element, "is_visible") or element.is_visible()
                    )
                except Exception:
                    continue
            for selector, field in (("img", "image"), ("video", "video"), ("canvas", "canvas")):
                try:
                    count = len(list(root.query_selector_all(selector)))
                except Exception:
                    count = 0
                if field == "image":
                    image_count = count
                elif field == "video":
                    video_count = count
                else:
                    canvas_count = count
        return ActivitySnapshot(
            generating=generating,
            busy_count=busy_count,
            image_count=image_count,
            video_count=video_count,
            canvas_count=canvas_count,
            analysis_complete_visible=self.analysis_complete_visible(assistant),
        )

    @staticmethod
    def _is_relevant_assistant_image(info: dict) -> bool:
        """Exclude decorative/empty <img> nodes from the generation gate."""
        if not bool(info.get("visible")):
            return False
        semantic = " ".join(str(info.get(key) or "") for key in (
            "alt", "aria_label", "testid", "class_name",
        )).lower()
        generation_marker = any(marker in semantic for marker in (
            "generated", "generating", "imagegen", "image-gen", "產生", "生成",
        ))
        natural_width = int(info.get("naturalWidth") or 0)
        natural_height = int(info.get("naturalHeight") or 0)
        rendered_width = float(info.get("renderedWidth") or 0)
        rendered_height = float(info.get("renderedHeight") or 0)
        # A real completed assistant image has useful intrinsic dimensions.
        # A pending image is accepted only when the DOM explicitly identifies
        # it as generation UI or reserves a substantial visible image area.
        substantial = (
            natural_width >= 64 and natural_height >= 64
        ) or (
            rendered_width >= 96 and rendered_height >= 96
        )
        return bool(generation_marker or substantial)

    def media_state(self, assistant_turn) -> dict:
        """Inspect media/busy state scoped to the fresh assistant turn.

        This deliberately looks at semantic loading signals (image readiness,
        progress/busy nodes, loading/generating state attributes) rather than a
        fixed sleep.  It avoids treating an assistant's partially-rendered text
        as final while an image/tool result is still being produced.
        """
        state = {
            "image_count": 0,
            "image_ready": 0,
            "image_pending": 0,
            "ready_image_fingerprint": "",
            "ready_image_signatures": [],
            "response_kind": "unknown",
            "video_count": 0,
            "canvas_count": 0,
            "busy_count": 0,
            "media_pending": False,
            "media_fingerprint": "",
        }
        root = assistant_turn.element if isinstance(assistant_turn, TurnRef) else assistant_turn
        if root is None:
            return state

        media_parts = []
        ready_image_parts = []
        try:
            images = list(root.query_selector_all("img"))
        except Exception:
            images = []
        state["image_count"] = len(images)
        relevant_images = []
        for image in images:
            try:
                info = image.evaluate(
                    """el => ({
                        src: el.currentSrc || el.src || '',
                        complete: !!el.complete,
                        naturalWidth: Number(el.naturalWidth || 0),
                        naturalHeight: Number(el.naturalHeight || 0),
                        renderedWidth: Number(el.getBoundingClientRect().width || 0),
                        renderedHeight: Number(el.getBoundingClientRect().height || 0),
                        visible: !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'),
                        alt: el.getAttribute('alt') || '',
                        aria_label: el.getAttribute('aria-label') || '',
                        testid: el.getAttribute('data-testid') || '',
                        class_name: String(el.className || '')
                    })"""
                ) or {}
            except Exception:
                info = {}
            if not self._is_relevant_assistant_image(info):
                continue
            relevant_images.append(image)
            ready = bool(
                info.get("complete")
                and int(info.get("naturalWidth") or 0) > 0
                and int(info.get("naturalHeight") or 0) > 0
            )
            if ready:
                state["image_ready"] += 1
                ready_image_parts.append(
                    "img|{}|{}x{}".format(
                        str(info.get("src") or "")[:300],
                        int(info.get("naturalWidth") or 0),
                        int(info.get("naturalHeight") or 0),
                    )
                )
            else:
                state["image_pending"] += 1
            media_parts.append(
                "img|{}|{}|{}x{}".format(
                    str(info.get("src") or "")[:300],
                    int(bool(info.get("complete"))),
                    int(info.get("naturalWidth") or 0),
                    int(info.get("naturalHeight") or 0),
                )
            )

        state["image_count"] = len(relevant_images)

        for selector, key in (("video", "video_count"), ("canvas", "canvas_count")):
            try:
                elements = list(root.query_selector_all(selector))
            except Exception:
                elements = []
            state[key] = len(elements)
            for element in elements:
                try:
                    media_parts.append(
                        f"{selector}|"
                        + str(element.get_attribute("src") or element.get_attribute("poster") or "")[:300]
                    )
                except Exception:
                    media_parts.append(selector)

        busy_selectors = (
            '[aria-busy="true"]',
            '[role="progressbar"]',
            '[data-state="loading"]',
            '[data-state="generating"]',
            '[data-state="processing"]',
            '[data-state="thinking"]',
            '[data-state="working"]',
            '[data-state="creating"]',
            '[data-state="preparing"]',
            '[data-testid*="loading"]',
            '[data-testid*="generat"]',
            '[data-testid*="think"]',
            '[data-testid*="working"]',
            '[aria-label*="Thinking"]',
            '[aria-label*="Working"]',
            '[aria-label*="Generating"]',
            '[aria-label*="Processing"]',
            '[aria-label*="Creating"]',
            '[aria-label*="Preparing"]',
            '[class*="loading"]',
            '[class*="generating"]',
            '[class*="thinking"]',
            '[class*="working"]',
            '[class*="creating"]',
            '[class*="processing"]',
            '[class*="skeleton"]',
            '[class*="shimmer"]',
        )
        seen_busy = set()
        for selector in busy_selectors:
            try:
                elements = root.query_selector_all(selector)
            except Exception:
                elements = []
            for element in elements:
                try:
                    # Ignore hidden layout/template nodes.
                    if not element.is_visible():
                        continue
                    ident = (
                        element.get_attribute("data-testid")
                        or element.get_attribute("aria-label")
                        or element.get_attribute("data-state")
                        or element.get_attribute("class")
                        or selector
                    )
                except Exception:
                    continue
                ident = str(ident or selector)[:180]
                if ident not in seen_busy:
                    seen_busy.add(ident)
                    media_parts.append("busy|" + ident)

        state["busy_count"] = len(seen_busy)
        state["ready_image_fingerprint"] = hashlib.sha256(
            "\n".join(sorted(ready_image_parts)).encode("utf-8", errors="replace")
        ).hexdigest()
        state["ready_image_signatures"] = sorted(
            hashlib.sha256(part.encode("utf-8", errors="replace")).hexdigest()
            for part in ready_image_parts
        )
        if state["image_ready"] > 0:
            state["response_kind"] = "image"
        state["media_pending"] = bool(
            state["image_pending"] > 0
            or state["busy_count"] > 0
        )
        state["media_fingerprint"] = hashlib.sha256(
            "\n".join(media_parts).encode("utf-8", errors="replace")
        ).hexdigest()
        return state


    def install_activity_observer(self) -> None:
        """Install the provider-specific assistant mutation observer."""
        profile = self._active_profile()
        self.page.evaluate(
            """(assistantSelector) => {
                if (window.__smartAgentActivityObserverInstalled) return;
                window.__smartAgentActivity = window.__smartAgentActivity || {
                    count: 0, lastMutationAt: 0, lastKind: ''
                };
                const isAssistantRelated = (node) => {
                    const el = node?.nodeType === Node.ELEMENT_NODE
                        ? node : node?.parentElement;
                    return Boolean(el && el.closest(assistantSelector));
                };
                const observer = new MutationObserver((mutations) => {
                    let changed = false, kind = '';
                    for (const mutation of mutations) {
                        if (!isAssistantRelated(mutation.target)) continue;
                        if (mutation.type === 'attributes') {
                            if (!['src','aria-busy','aria-label','data-state','data-testid']
                                .includes(mutation.attributeName || '')) continue;
                            kind = 'attribute:' + (mutation.attributeName || '');
                        } else {
                            kind = mutation.type;
                        }
                        changed = true;
                        break;
                    }
                    if (changed) {
                        window.__smartAgentActivity.count += 1;
                        window.__smartAgentActivity.lastMutationAt = Date.now();
                        window.__smartAgentActivity.lastKind = kind;
                    }
                });
                observer.observe(document.documentElement || document.body, {
                    subtree: true, childList: true, characterData: true,
                    attributes: true,
                    attributeFilter: ['src','aria-busy','aria-label','data-state','data-testid']
                });
                window.__smartAgentActivityObserverInstalled = true;
                window.__smartAgentActivityObserver = observer;
            }""",
            profile.assistant_turn_selector,
        )

    def install_input_bridge(self, *, reset_legacy: bool = False) -> None:
        if reset_legacy:
            state = self.page.evaluate("""() => ({
              installed: !!window.__webAgentDirectBridgeInstalled,
              version: Number(window.__webAgentDirectBridgeVersion || 0)
            })""")
            if state.get("installed") and int(state.get("version") or 0) < self.bridge_version:
                self.page.reload(wait_until="domcontentloaded", timeout=60000)
        if not self.bridge_script or self.bridge_version <= 0:
            raise RuntimeError("WEB_UI_INPUT_BRIDGE_UNAVAILABLE")
        self.page.context.add_init_script(f"({self.bridge_script})()")
        if not self.page.evaluate(self.bridge_script):
            raise RuntimeError("WEB_UI_INPUT_BRIDGE_INSTALL_FAILED")

    def input_bridge_installed(self) -> bool:
        try:
            return bool(self.page.evaluate(
                "() => !!window.__webAgentDirectBridgeInstalled && "
                f"Number(window.__webAgentDirectBridgeVersion || 0) === {self.bridge_version}"
            ))
        except Exception:
            return False

    def pop_input_bridge(self) -> dict | None:
        value = self.page.evaluate("""() => {
          const queue = window.__webAgentDirectQueue || [];
          return queue.length ? queue.shift() : null;
        }""")
        return dict(value) if isinstance(value, dict) else None

    def set_automation_submit(self, active: bool) -> None:
        self.page.evaluate(
            "active => { window.__webAgentAutomationSubmit = Boolean(active); }",
            bool(active),
        )

    def attachment_file_inputs(self) -> tuple:
        if self.attachments is None:
            return ()
        inputs = list(self.attachments.file_inputs(self.page))
        ranked = []
        for element in inputs:
            accept = _safe_attr(element, "accept").strip().lower()
            image_only = bool(accept) and "image/" in accept and not any(
                token in accept for token in (
                    ".pdf", ".txt", ".md", ".py", ".doc", ".docx",
                    ".xls", ".xlsx", ".ppt", ".pptx", "application/", "text/", "*/*",
                )
            )
            ranked.append((1 if image_only else 0, element))
        return tuple(element for _score, element in sorted(ranked, key=lambda item: item[0]))

    def attachment_button(self):
        return self.attachments.attach_button(self.page) if self.attachments is not None else None

    def attachment_menu_items(self) -> tuple:
        return self.attachments.attach_menu_items(self.page) if self.attachments is not None else ()

    def attachment_dom_state(self, expected_names: list[str]) -> dict:
        if self.attachments is None:
            return {"supported": False, "reason": "ATTACHMENTS_UNSUPPORTED"}
        return self.attachments.attachment_dom_state(self.page, expected_names)

    def composer_attachment_count(self) -> int:
        return self.attachments.composer_attachment_count(self.page) if self.attachments is not None else 0

    def clear_one_attachment(self) -> bool:
        return self.attachments.clear_one_attachment(self.page) if self.attachments is not None else False

    def dismiss_rate_limit_dialog(self) -> bool:
        return False

    def conversation_display_name(self) -> str:
        try:
            title = (self.page.title() or "").strip()
        except Exception:
            return ""
        return title[:160]

    def conversation_link(self, conversation_id: str):
        return None

    def dismiss_blocking_dialog(self) -> dict:
        return {"dismissed": False}

    def activity_observer_state(self) -> dict:
        self.install_activity_observer()
        try:
            value = self.page.evaluate("""() => {
                const state = window.__smartAgentActivity || {};
                return {
                    mutation_count: Number(state.count || 0),
                    last_mutation_at: Number(state.lastMutationAt || 0),
                    last_mutation_kind: String(state.lastKind || '')
                };
            }""") or {}
        except Exception:
            value = {}
        return {
            "mutation_count": int(value.get("mutation_count") or 0),
            "last_mutation_at": int(value.get("last_mutation_at") or 0),
            "last_mutation_kind": str(value.get("last_mutation_kind") or ""),
        }


__all__ = ["BaseWebUIAdapter"]
