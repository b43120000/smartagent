"""Read-only M365 profile probing; ambiguous composers fail closed."""
from ...contracts import CompatibilityReport


def probe_profile(page, profile):
    counts = []
    for selector in (profile.composer_selector, profile.send_selector,
                     profile.user_turn_selector, profile.assistant_turn_selector):
        try:
            counts.append(page.locator(selector).count())
        except Exception:
            counts.append(0)
    try:
        boxes = page.locator(profile.composer_selector)
        visible = sum(boxes.nth(i).is_visible() for i in range(boxes.count()))
    except Exception:
        visible = 0
    reasons = () if visible == 1 else ("m365_composer_not_unique_visible",)
    return CompatibilityReport(selected_profile=profile.name, supported=not reasons,
        composer_count=counts[0], composer_visible=bool(visible), send_count=counts[1],
        user_turn_count=counts[2], assistant_turn_count=counts[3], reasons=reasons)
