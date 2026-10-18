"""Origin-isolated manual adapters for sites without built-in selectors."""
from dataclasses import dataclass
import hashlib

from ...base_adapter import BaseWebUIAdapter
from ...contracts import CompatibilityReport, ComposerSnapshot, ComposerValidation, ui_text_matches
from ...dom_config import load_dom_config, ELEMENT_FIELDS
from ...provider_registry import provider_from_url, is_other_provider


@dataclass(frozen=True)
class OtherUIProfile:
    name: str
    composer_selector: str = ':not(*)'
    send_selector: str = ':not(*)'
    user_turn_selector: str = ':not(*)'
    user_content_selector: str = ':not(*)'
    assistant_turn_selector: str = ':not(*)'
    final_content_selector: str = ':not(*)'
    stop_selectors: tuple[str, ...] = ()
    busy_selectors: tuple[str, ...] = ()


class OtherUIAdapter(BaseWebUIAdapter):
    stable_turn_attributes = ('data-message-id', 'data-response-id', 'data-query-id',
                              'data-content-search-unit-key', 'data-testid', 'id')

    def __init__(self, page, *, profile_name=''):
        self.provider_name = provider_from_url(page.url)
        if not is_other_provider(self.provider_name):
            raise ValueError('manual_other_origin_required')
        super().__init__(page, profile_name=profile_name)

    def load_active_profile(self):
        document = load_dom_config(self.provider_name)
        if document is None:
            raise ValueError('manual_other_config_missing:請先執行 1.writeSendBox.bat')
        return OtherUIProfile(name=self.provider_name, **{
            ELEMENT_FIELDS[key]: (entry['selector'],) if key=='stop' else entry['selector']
            for key, entry in document['elements'].items()})

    def load_profile_by_name(self, name):
        return self.load_active_profile() if name == self.provider_name else None

    def _active_profile(self):
        # Navigation must never reuse another origin's calibrated selectors.
        if provider_from_url(self.page.url) != self.provider_name:
            raise ValueError('manual_other_page_origin_changed')
        return super()._active_profile()

    def probe_provider_profile(self, profile):
        composer = self.page.locator(profile.composer_selector)
        count = composer.count()
        valid = count == 1 and composer.is_visible() and composer.is_editable()
        return CompatibilityReport(profile.name, valid, count, bool(count and composer.first.is_visible()),
            self.page.locator(profile.send_selector).count(),
            self.page.locator(profile.user_turn_selector).count(),
            self.page.locator(profile.assistant_turn_selector).count(),
            () if valid else ('manual_composer_not_unique_or_editable',))

    def read_provider_composer(self, composer):
        if composer is None:
            return ComposerSnapshot('', '')
        text = composer.evaluate("el => 'value' in el ? el.value : el.innerText") or ''
        return ComposerSnapshot(text, text)

    def validate_provider_composer(self, prompt, snapshot):
        actual = snapshot.primary_text
        return ComposerValidation(ui_text_matches(actual, prompt),
            'primary' if ui_text_matches(actual, prompt) else '', len(prompt), len(actual),
            hashlib.sha256(prompt.encode()).hexdigest(), hashlib.sha256(actual.encode()).hexdigest())
