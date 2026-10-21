"""Temporary offline regression for the authorized SmartAgentv2_1 source."""
import sys
from pathlib import Path

source = Path(__file__).resolve().parent
sys.path.insert(0, str(source))

from agent_core import conversation_identity as identity
from WebAgent import browser_bridge

assert Path(identity.__file__).resolve() == source / "agent_core" / "conversation_identity.py", identity.__file__
assert Path(browser_bridge.__file__).resolve() == source / "WebAgent" / "browser_bridge.py", browser_bridge.__file__
print("AUTHORIZED_SOURCE_IMPORT_OK")

first = "https://example.org/chats/one"
second = "https://example.org/chats/two"
other_origin = "https://example.net/chats/one"
assert identity.conversation_provider(first).startswith("other_")
assert browser_bridge.provider_for_url(first) == identity.conversation_provider(first)
assert len(identity.conversation_id(first)) == 64
assert identity.conversation_id(first) == identity.conversation_id(first + "/")
assert identity.conversation_id(first) != identity.conversation_id(second)
assert identity.conversation_id(first) != identity.conversation_id(other_origin)
assert identity.same_conversation(first, first)
assert not identity.same_conversation(first, second)
assert not identity.same_conversation(first, other_origin)
assert browser_bridge.normalize_conversation_url(first) == first
print("OTHER_URL_IDENTITY_AND_ISOLATION_OK")

for invalid in ("", "file:///tmp/chat", "ftp://example.org/chat", "javascript:alert(1)"):
    assert not identity.conversation_provider(invalid), invalid
    assert not identity.conversation_id(invalid), invalid
print("INVALID_URL_REJECTION_OK")

known = (
    ("https://chatgpt.com/c/ABC123", "chatgpt", "abc123"),
    ("https://claude.ai/chat/ABC123", "claude", "abc123"),
    ("https://gemini.google.com/app/ABC123", "gemini", "abc123"),
)
for url, provider, expected_id in known:
    assert identity.conversation_provider(url) == provider, (url, identity.conversation_provider(url))
    assert identity.conversation_id(url) == expected_id, (url, identity.conversation_id(url))
    assert identity.same_conversation(url, url)
print("KNOWN_PROVIDER_IDENTITY_OK")
print("WEBURL_LOCAL_REGRESSION_OK")