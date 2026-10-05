"""Compatibility exports for the ChatGPT provider profiles.

New code must obtain a provider adapter through ``web_ui.factory``. These
exports preserve existing imports while the runtime migration is completed.
"""
from .providers.chatgpt.profiles import *  # noqa: F401,F403
