"""Bootstrap selectors are candidates, not evidence of live compatibility."""
from dataclasses import dataclass
from ...dom_config import apply_dom_config


@dataclass(frozen=True)
class M365UIProfile:
    name: str
    composer_selector: str
    send_selector: str
    user_turn_selector: str
    user_content_selector: str
    assistant_turn_selector: str
    final_content_selector: str
    stop_selectors: tuple[str, ...]
    busy_selectors: tuple[str, ...]


M365_BOOTSTRAP_PROFILE = M365UIProfile(
    name="m365_bootstrap_v1",
    composer_selector="textarea, [contenteditable='true'][role='textbox'], [contenteditable='true'][data-lexical-editor='true']",
    send_selector="button[data-testid='send-button'], button[aria-label='Send'], button[aria-label='Send message'], button[aria-label='傳送'], button[aria-label='发送']",
    user_turn_selector="[data-message-author-role='user'], [data-testid='user-message'], [data-role='user']",
    user_content_selector="[data-testid='message-content'], .message-content",
    assistant_turn_selector="[data-message-author-role='assistant'], [data-testid='assistant-message'], [data-role='assistant']",
    final_content_selector="[data-testid='message-content'], .message-content, .markdown",
    stop_selectors=("button[aria-label='Stop generating']", "button[aria-label='Stop response']", "button[aria-label='停止產生']", "button[data-testid='stop-button']"),
    busy_selectors=("[aria-busy='true']", "[role='progressbar']", "[data-state='generating']", "[data-state='thinking']"),
)


def profile_by_name(name):
    if name == M365_BOOTSTRAP_PROFILE.name:
        return apply_dom_config("m365", M365_BOOTSTRAP_PROFILE)
    from ...profile_store import profile_values
    values = profile_values("m365", name=name)
    return apply_dom_config("m365", M365UIProfile(**values)) if values else None


def active_profile():
    from ...profile_store import profile_values
    values = profile_values("m365")
    return apply_dom_config("m365", M365UIProfile(**values) if values else M365_BOOTSTRAP_PROFILE)
