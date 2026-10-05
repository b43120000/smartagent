"""Gemini attachment capability boundary.

Initial Gemini support deliberately exposes attachments as unsupported instead
of reusing another provider's DOM or reporting false success.
"""
from __future__ import annotations


def file_inputs(page) -> tuple:
    return ()


def attach_button(page):
    return None


def attach_menu_items(page) -> tuple:
    return ()


def attachment_dom_state(page, expected_names: list[str]) -> dict:
    return {
        "supported": False,
        "reason": "GEMINI_ATTACHMENTS_UNSUPPORTED",
        "text": "", "busy": [], "chips": [], "chip_records": [],
        "alerts": [], "dialogs": [], "upload_ring_count": 0,
    }


def composer_attachment_count(page) -> int:
    return 0


def clear_one_attachment(page) -> bool:
    return False


__all__ = [
    "attach_button", "attach_menu_items", "attachment_dom_state",
    "clear_one_attachment", "composer_attachment_count", "file_inputs",
]
