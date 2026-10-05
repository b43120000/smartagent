"""Read-only Claude UI capability probing."""
from __future__ import annotations

from ...contracts import CompatibilityReport
from .profiles import CLAUDE_WEB_2026_PROFILE, ClaudeUIProfile


def _count(page, selector: str) -> int:
    try:
        return int(page.locator(selector).count())
    except Exception:
        try:
            return len(list(page.query_selector_all(selector)))
        except Exception:
            return 0


def _visible_count(page, selector: str) -> int:
    try:
        locator = page.locator(selector)
        return sum(1 for index in range(int(locator.count())) if locator.nth(index).is_visible())
    except Exception:
        return 0


def probe_profile(page, profile: ClaudeUIProfile) -> CompatibilityReport:
    composer_count = _count(page, profile.composer_selector)
    composer_visible_count = _visible_count(page, profile.composer_selector)
    send_count = _count(page, profile.send_selector)
    user_count = _count(page, profile.user_turn_selector)
    assistant_count = _count(page, profile.assistant_turn_selector)
    reasons: list[str] = []
    if not composer_count:
        reasons.append("composer_missing")
    elif not composer_visible_count:
        reasons.append("composer_not_visible")
    return CompatibilityReport(
        selected_profile=profile.name,
        supported=not reasons,
        composer_count=composer_count,
        composer_visible=bool(composer_visible_count),
        send_count=send_count,
        user_turn_count=user_count,
        assistant_turn_count=assistant_count,
        reasons=tuple(reasons),
    )


def select_compatible_profile(page) -> tuple[ClaudeUIProfile, CompatibilityReport]:
    profile = CLAUDE_WEB_2026_PROFILE
    return profile, probe_profile(page, profile)


__all__ = ["probe_profile", "select_compatible_profile"]
