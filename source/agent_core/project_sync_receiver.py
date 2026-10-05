"""Bind sync transport to the actual receiver, including navigation checks."""
from urllib.parse import urlsplit

from .project_sync_protocol import ProjectSyncProtocolError


def browser_receiver_identity(scraper) -> str:
    page = getattr(scraper, "_page", None)
    raw_url = str(getattr(page, "url", "") or "").strip()
    url = urlsplit(raw_url)
    path = url.path.rstrip("/")
    if not url.netloc or not path or path in {"/app", "/chat"}:
        raise ProjectSyncProtocolError("project_sync_receiver_identity_unavailable")

    # conversation_id() is provider-aware and therefore requires the complete
    # URL.  Passing only ``url.path`` loses the hostname/provider and made every
    # valid ChatGPT project conversation look unavailable.
    from .conversation_identity import conversation_id, conversation_provider, same_conversation

    provider = conversation_provider(raw_url)
    if provider != "chatgpt":
        raise ProjectSyncProtocolError(
            f"project_sync_receiver_provider_not_implemented:{provider or 'unknown'}"
        )

    expected_url = str(getattr(scraper, "_expected_execution_url", "") or "").strip()
    if expected_url and not same_conversation(raw_url, expected_url):
        raise ProjectSyncProtocolError("project_sync_receiver_changed")

    cid = conversation_id(raw_url)
    if not cid:
        raise ProjectSyncProtocolError("project_sync_receiver_identity_unavailable")
    path = "/c/" + cid
    return url.scheme.lower() + "://" + url.netloc.lower() + path


def bind_receiver_transport(transport, identity_provider):
    identity = str(identity_provider() or "").strip()
    if not identity:
        raise ProjectSyncProtocolError("project_sync_receiver_identity_unavailable")

    def send(prompt, attachments):
        if str(identity_provider() or "").strip() != identity:
            raise ProjectSyncProtocolError("project_sync_receiver_changed")
        reply = transport(prompt, attachments)
        if str(identity_provider() or "").strip() != identity:
            raise ProjectSyncProtocolError("project_sync_receiver_changed")
        return reply

    return identity, send
