from __future__ import annotations

"""Request-scoped delivery planning for WebGPT-generated images."""

import os
import re
from pathlib import Path


_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_IMAGE_NOUN = r"(?:圖片|照片|圖像|影像|(?:這|該|此|那)張圖|image|photo|picture)"
_IMAGE_VERB = r"(?:生成|產生|畫|繪製|製作|create|generate|draw|render|make)"
_IMAGE_EDIT_VERB = (
    r"(?:修改|編輯|調整|修圖|重畫|重製|移除|去除|拿掉|刪除|替換|換掉|改成|"
    r"edit|modify|retouch|remove|erase|replace|change)"
)
_IMAGE_CLASSIFIER = (
    _IMAGE_VERB
    + r".{0,8}(?:一|1)?\s*張"
    + r"(?!\s*(?:表|表格|試算表|清單|報表))"
)


def is_image_generation_request(request: str) -> bool:
    text = str(request or "")
    return bool(
        re.search(_IMAGE_VERB + r".{0,40}" + _IMAGE_NOUN, text, re.IGNORECASE | re.DOTALL)
        or re.search(_IMAGE_NOUN + r".{0,40}" + _IMAGE_VERB, text, re.IGNORECASE | re.DOTALL)
        # Colloquial Chinese image requests often omit 圖片/照片 entirely:
        # 「生成一張柴犬衝浪」.  The 張 classifier is strong evidence of a
        # visual request; explicitly exclude common tabular/document nouns.
        or re.search(_IMAGE_CLASSIFIER, text, re.IGNORECASE | re.DOTALL)
        # Editing requests often do not contain a generation verb.  This
        # classifier is used only to choose automatic delivery; request-scoped
        # image observation is enabled independently in web_runtime.
        or re.search(_IMAGE_EDIT_VERB + r".{0,40}" + _IMAGE_NOUN, text, re.IGNORECASE | re.DOTALL)
        or re.search(_IMAGE_NOUN + r".{0,40}" + _IMAGE_EDIT_VERB, text, re.IGNORECASE | re.DOTALL)
    )


def _requested_filename(request: str) -> str:
    match = re.search(
        r"(?:檔名(?:叫|為|是)?|就叫|命名(?:為|成)?|filename\s*(?:is|=|:)?)[\s：:]*[「『\"']?"
        r"([^，,；;\r\n\"'」』]+?\.(?:png|jpe?g|webp))",
        str(request or ""),
        re.IGNORECASE,
    )
    return Path(match.group(1).strip()).name if match else ""


def _requested_windows_path(request: str) -> str:
    text = str(request or "")
    full_file = re.search(
        r"([A-Za-z]:[\\/][^，,；;\r\n]+?\.(?:png|jpe?g|webp))",
        text,
        re.IGNORECASE,
    )
    if full_file:
        return full_file.group(1).strip().strip("\"'")
    path = re.search(r"([A-Za-z]:[\\/][^，,；;\r\n]+)", text)
    if not path:
        return ""
    value = path.group(1).strip().strip("\"'")
    # Stop before an unpunctuated filename clause when possible.
    value = re.split(
        r"\s+(?:檔名|就叫|命名|filename)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return value.rstrip(" .")


def plan_image_delivery(
    request: str,
    *,
    workspace: str | Path,
    interface_name: str,
    source_tag: str,
    request_id: str,
) -> dict:
    """Return a bounded two-phase delivery plan, or an empty dict."""
    if not is_image_generation_request(request):
        return {}

    root = Path(workspace).expanduser().resolve()
    remote_telegram = (
        str(interface_name or "").strip().lower() == "remote"
        and str(source_tag or "").strip().upper().startswith("REMOTEAGENT_TELEGRAM")
    )
    requested_path = _requested_windows_path(request)
    filename = _requested_filename(request)
    if not requested_path:
        if not remote_telegram:
            return {}
        # Use an explicit file path.  A trailing slash is not durable across
        # authorization/path-normalization layers and can turn a directory
        # request into an extensionless file.
        target = (
            root / ".agents" / "generated_image_outbound"
            / str(request_id) / "generated_image.png"
        )
        return {
            "kind": "image",
            "delivery": "telegram",
            "output_path": str(target),
            "expected_filename": target.name,
        }

    target = Path(requested_path)
    if target.suffix.lower() not in _IMAGE_SUFFIXES:
        if filename:
            target = target / filename
        else:
            return {
                "kind": "image",
                "delivery": "local",
                "output_path": str(target) + os.sep,
                "expected_filename": "",
            }
    return {
        "kind": "image",
        # ``telegram`` means queue the successfully downloaded local file as a
        # second delivery.  The explicit user path remains the primary target.
        "delivery": "telegram" if remote_telegram else "local",
        "output_path": str(target),
        "expected_filename": target.name,
    }


__all__ = ["is_image_generation_request", "plan_image_delivery"]
