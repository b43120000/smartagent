"""M365 adapter implements the shared contract without another provider adapter."""
import json
from urllib.parse import urlsplit
from ...base_adapter import BaseWebUIAdapter
from .profiles import active_profile, profile_by_name
from .composer import read_composer_snapshot, validate_composer_prompt
from .compatibility import probe_profile
from .bridge import BRIDGE_SCRIPT, BRIDGE_VERSION


class M365UIAdapter(BaseWebUIAdapter):
    provider_name = "m365"
    bridge_script = BRIDGE_SCRIPT
    bridge_version = BRIDGE_VERSION
    stable_turn_attributes = ("data-message-id", "data-turn-id", "data-testid", "id")
    authenticated_session_selectors = ("button[aria-label*='account' i]", "button[aria-label*='profile' i]")
    login_required_selectors = ("input[type='password']", "input[type='email']", "a[href*='login.microsoftonline.com']")
    login_url_tokens = ("login.microsoftonline.com", "login.live.com", "/signin")

    load_profile_by_name = staticmethod(profile_by_name)
    load_active_profile = staticmethod(active_profile)
    read_provider_composer = staticmethod(read_composer_snapshot)
    validate_provider_composer = staticmethod(validate_composer_prompt)

    def probe_provider_profile(self, profile):
        return probe_profile(self.page, profile)

    def page_reachable(self):
        return (urlsplit(str(getattr(self.page, "url", ""))).hostname == "m365.cloud.microsoft"
                and super().page_reachable())

    def install_input_bridge(self, *, reset_legacy=False):
        selectors = json.dumps({"composer": self.profile.composer_selector, "send": self.profile.send_selector})
        self.page.context.add_init_script("window.__m365Selectors = " + selectors + ";")
        self.page.evaluate("selectors => { window.__m365Selectors = selectors; }", json.loads(selectors))
        super().install_input_bridge(reset_legacy=reset_legacy)

    def conversation_link(self, conversation_id):
        links = self.page.locator('a[href*="/chat/conversation/' + conversation_id + '"]')
        for i in range(links.count()):
            if links.nth(i).is_visible():
                return links.nth(i)
        return None

    def attachment_dom_state(self, expected_names):
        return {"supported": False, "reason": "M365_ATTACHMENTS_NOT_CALIBRATED",
                "text": "", "busy": [], "chips": [], "chip_records": [],
                "alerts": [], "dialogs": [], "upload_ring_count": 0}
