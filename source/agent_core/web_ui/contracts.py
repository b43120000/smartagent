"""Stable public data contracts for browser UI observation.

Only this package translates volatile ChatGPT DOM into these records.  Callers
must make decisions from the records instead of importing CSS selectors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any


_WS_RE = re.compile(r"\s+")


def normalize_ui_text(value: object) -> str:
    text = str(value or "").replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _WS_RE.sub(" ", text).strip()


def text_digest(value: object) -> str:
    return hashlib.sha256(
        normalize_ui_text(value).encode("utf-8", errors="replace")
    ).hexdigest()


def ui_text_matches(actual: object, expected: object) -> bool:
    """Compare rendered text without mutating either side.

    Exact normalized equality is preferred.  The second layer ignores Unicode
    whitespace only, covering renderer-added boundaries around rich links and
    blob URLs while preserving all non-whitespace content.
    """
    left = normalize_ui_text(actual)
    right = normalize_ui_text(expected)
    if left == right:
        return True
    return re.sub(r"\s+", "", left) == re.sub(r"\s+", "", right)


@dataclass(frozen=True)
class TurnRef:
    role: str
    ordinal: int
    structural_id: str
    content_digest: str
    raw_text: str
    stable_attributes: tuple[tuple[str, str], ...] = ()
    element: Any = field(default=None, compare=False, repr=False)

    @property
    def text(self) -> str:
        return normalize_ui_text(self.raw_text)


@dataclass(frozen=True)
class ArtifactObservation:
    element: Any = field(default=None, compare=False, repr=False)
    tag: str = ""
    href: str = ""
    src: str = ""
    text: str = ""
    aria_label: str = ""
    title: str = ""
    testid: str = ""
    download_name: str = ""
    data_filename: str = ""
    filename_attr: str = ""
    visible: bool = False
    complete: bool = False
    natural_width: int = 0
    natural_height: int = 0
    rendered_width: float = 0.0
    rendered_height: float = 0.0
    generated_media: bool = False


@dataclass(frozen=True)
class UISnapshot:
    profile_name: str
    user_turns: tuple[TurnRef, ...]
    assistant_turns: tuple[TurnRef, ...]


@dataclass
class RequestScope:
    scope_id: str
    conversation_id: str
    profile_name: str
    prompt_digest: str
    prompt_text: str
    baseline_user_ids: tuple[str, ...]
    baseline_user_content_digests: tuple[str, ...]
    baseline_assistant_ids: tuple[str, ...]
    baseline_user_count: int
    baseline_assistant_count: int
    request_markers: tuple[str, ...] = ()
    user_turn: TurnRef | None = None


@dataclass(frozen=True)
class ActivitySnapshot:
    generating: bool
    busy_count: int
    image_count: int
    video_count: int
    canvas_count: int
    analysis_complete_visible: bool = False

    @property
    def active(self) -> bool:
        return bool(
            self.generating
            or self.busy_count
            or self.image_count
            or self.video_count
            or self.canvas_count
        )


@dataclass(frozen=True)
class ComposerSnapshot:
    """Read-only representations of the current browser composer.

    ``primary_text`` is the renderer-facing value (``value``/``innerText``).
    ``semantic_text`` is the inverse DOM serialization used to recover the
    software prompt without renderer-only paragraph or rich-link decoration.
    """

    primary_text: str
    semantic_text: str

    @property
    def candidates(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.primary_text, self.semantic_text)))


@dataclass(frozen=True)
class ComposerValidation:
    matched: bool
    matched_view: str
    expected_length: int
    observed_length: int
    expected_sha256: str
    observed_sha256: str


@dataclass(frozen=True)
class CompatibilityReport:
    selected_profile: str
    supported: bool
    composer_count: int
    composer_visible: bool
    send_count: int
    user_turn_count: int
    assistant_turn_count: int
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "selected_profile": self.selected_profile,
            "supported": self.supported,
            "composer_count": self.composer_count,
            "composer_visible": self.composer_visible,
            "send_count": self.send_count,
            "user_turn_count": self.user_turn_count,
            "assistant_turn_count": self.assistant_turn_count,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class AuthenticationState:
    provider: str
    status: str
    authenticated: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status,
            "authenticated": self.authenticated,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class UIResponse:
    scope_id: str
    profile_name: str
    user_turn: TurnRef
    assistant_turn: TurnRef
    final_text: str
    activity: ActivitySnapshot
