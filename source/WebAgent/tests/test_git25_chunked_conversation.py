"""Regression: legacy single-submit default with dormant chunk/ACK/COMMIT support."""
from __future__ import annotations

import pytest

from agent_core.conversation_chunk_transport import (
    ChunkTransferError,
    MAX_CHUNK_MESSAGE_CHARS,
    MAX_SUBMIT_CHARS,
    make_chunk_messages,
    validate_chunk_ack,
)
from agent_core.payload_budget import (
    PromptBudgetExceeded,
    WEBGPT_PROMPT_HARD_MAX_BYTES,
)


def test_default_uses_existing_single_total_budget():
    assert MAX_SUBMIT_CHARS == WEBGPT_PROMPT_HARD_MAX_BYTES
    assert MAX_CHUNK_MESSAGE_CHARS == WEBGPT_PROMPT_HARD_MAX_BYTES


@pytest.mark.parametrize("length", [0, 1, 9000, 10000, 10001, 36564, 80000, 200000, 524288])
def test_in_budget_ascii_never_chunks_by_default(length):
    prompt = "x" * length
    assert make_chunk_messages(prompt) == [(prompt, None)]


def test_utf8_cjk_init_single_submit():
    prompt = "[AGENT_PROTOCOL_BOOTSTRAP]\n" + "協議內容\n" * 5000
    assert len(prompt) < MAX_CHUNK_MESSAGE_CHARS
    assert make_chunk_messages(prompt) == [(prompt, None)]


@pytest.mark.parametrize("payload", ["x" * (512 * 1024 + 1), "中" * 174763])
def test_over_total_budget_rejected_before_any_chunk(payload):
    with pytest.raises(PromptBudgetExceeded, match="WEB_PROMPT_BUDGET_EXCEEDED"):
        make_chunk_messages(payload)


@pytest.mark.parametrize("length", [9000, 9001, 10000, 10001, 36000, 80000])
def test_optional_chunking_can_still_be_enabled_without_loss(length):
    prompt = "中A🙂\n" * (length // 4) + "x" * (length % 4)
    limit = 7800  # Explicit opt-in lower threshold, not the runtime default.
    messages = make_chunk_messages(prompt, limit=limit)
    assert len(messages) > 1
    assert all(len(text) <= limit for text, _ in messages)
    assert all(ack is not None for _, ack in messages[:-1])
    assert messages[-1][1] is None
    recovered = []
    for message, ack in messages:
        recovered.append(message.split("[DATA_BEGIN]\n", 1)[1].split("\n[DATA_END]", 1)[0])
        if ack:
            validate_chunk_ack(ack + "\n", ack)
    assert "".join(recovered) == prompt


def test_ack_must_be_exact_or_stop():
    _, ack = make_chunk_messages("x" * 36000, limit=7800)[0]
    with pytest.raises(ChunkTransferError, match="CHUNK_ACK_NOT_CONFIRMED"):
        validate_chunk_ack("unrelated model answer", ack)
    with pytest.raises(ChunkTransferError, match="CHUNK_ACK_NOT_CONFIRMED"):
        validate_chunk_ack("", ack)


def test_bootstrap_commit_only_on_final_chunk_when_explicitly_enabled():
    prompt = "[AGENT_PROTOCOL_BOOTSTRAP]\n" + "protocol\n" * 5000 + "\n[/AGENT_PROTOCOL_BOOTSTRAP]"
    messages = make_chunk_messages(prompt, limit=7800)
    assert len(messages) > 2
    assert all("mode=ACCUMULATE_ONLY" in msg for msg, _ in messages[:-1])
    assert "mode=COMMIT" in messages[-1][0]
    assert messages[-1][1] is None


def test_normal_turn_unchanged():
    original = '{"tool":"turn_commit","action_count":1}'
    assert make_chunk_messages(original) == [(original, None)]
