"""ChatGPT DOM implementation of the provider-neutral web_ui contract."""
from .adapter import ChatGPTUIAdapter
from .bridge import BRIDGE_SCRIPT
from .profiles import CHATGPT_UI_PROFILES, ChatGPTUIProfile

__all__ = ["BRIDGE_SCRIPT", "CHATGPT_UI_PROFILES", "ChatGPTUIAdapter", "ChatGPTUIProfile"]

