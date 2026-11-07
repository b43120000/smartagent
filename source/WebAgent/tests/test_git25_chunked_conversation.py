"""Regression: 10k physical submit / all logical conversation phases."""
from __future__ import annotations

import pytest

from agent_core.conversation_chunk_transport import (
    ChunkTransferError,
    MAX_CHUNK_MESSAGE_CHARS,
    make_chunk_messages,
    validate_chunk_ack,
)


@pytest.mark.parametrize("length", [0, 1, 9000, 9001, 10000, 10001, 36000, 80000])
def test_all_chunks_bounded_and_lossless(length):
    prompt = "中A🙂\n" * (length // 4) + "x" * (length % 4)
    messages = make_chunk_messages(prompt)
    if len(prompt) <= MAX_CHUNK_MESSAGE_CHARS:
        assert messages == [(prompt, None)]
    else:
        assert len(messages) > 1
        assert all(len(text) <= MAX_CHUNK_MESSAGE_CHARS for text, _ in messages)
        assert all(ack is not None for _, ack in messages[:-1])
        assert messages[-1][1] is None
        recovered = []
        for message, ack in messages:
            recovered.append(message.split("[DATA_BEGIN]\n", 1)[1].split("\n[DATA_END]", 1)[0])
            if ack:
                validate_chunk_ack(ack + "\n", ack)
        assert "".join(recovered) == prompt


def test_ack_must_be_exact_or_stop():
    _, ack = make_chunk_messages("x" * 36000)[0]
    with pytest.raises(ChunkTransferError, match="CHUNK_ACK_NOT_CONFIRMED"):
        validate_chunk_ack("unrelated model answer", ack)
    with pytest.raises(ChunkTransferError, match="CHUNK_ACK_NOT_CONFIRMED"):
        validate_chunk_ack("", ack)


def test_bootstrap_commit_only_on_final_chunk():
    prompt = "[AGENT_PROTOCOL_BOOTSTRAP]\n" + "protocol\n" * 5000 + "\n[/AGENT_PROTOCOL_BOOTSTRAP]"
    messages = make_chunk_messages(prompt)
    assert len(messages) > 2
    assert all("mode=ACCUMULATE_ONLY" in msg for msg, _ in messages[:-1])
    assert "mode=COMMIT" in messages[-1][0]
    assert messages[-1][1] is None


def test_already_small_normal_turn_is_identical():
    original = '{"tool":"turn_commit","action_count":1}'
    assert make_chunk_messages(original) == [(original, None)]
