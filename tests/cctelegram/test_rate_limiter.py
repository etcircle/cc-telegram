"""Tests for the TypingAwareRateLimiter sendChatAction group-bucket exemption.

Pins the send-layer contract: sendChatAction skips the per-GROUP limiter (so
multi-topic typing cadence is not paced by the 20/60s message budget) while the
overall 30/s limiter + RetryAfter machinery stay in force, and — the F1 drift
guard — the exemption only rewrites the CLASSIFICATION metadata: the real
(negative) chat_id still reaches the request path, pinned against a future PTB
that would start using ``data`` for the actual request body.

PTB is imported from the TEST environment (project venv) — never an absolute
tool-install path (F2). Blocking assertions use a bounded ``asyncio.wait_for``
+ cancel-and-suppress, never a real 60s wait (F4).
"""

import asyncio
import contextlib

import pytest

from telegram.ext import AIORateLimiter

from cctelegram.rate_limiter import TypingAwareRateLimiter

# A forum/supergroup chat id — negative, so PTB routes it to the group bucket.
_GROUP_CHAT_ID = -100123


async def _ok_callback(*args, **kwargs):
    return True


async def _invoke(limiter, endpoint, chat_id, *, callback=_ok_callback, extra=None):
    """Drive process_request in ExtBot's exact call shape (F1): the real request
    body travels as ``args=(endpoint, data)`` and ``data`` is the SAME object."""
    data = {"chat_id": chat_id}
    if extra:
        data.update(extra)
    return await limiter.process_request(
        callback=callback,
        args=(endpoint, data),
        kwargs={},
        endpoint=endpoint,
        data=data,
        rate_limit_args=None,
    )


async def _assert_blocks(coro):
    """Assert ``coro`` does NOT complete within a short window, then cancel +
    await the still-pending task (F4: no 60s sleep, no leaked task)."""
    task = asyncio.create_task(coro)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.15)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_send_chat_action_skips_group_limiter():
    """RED-first: three sendChatAction calls on the exempting subclass all
    complete near-instantly even with a 1-slot group bucket; the parent class
    (the CONTROL — the un-fixed behavior) blocks on the second."""
    # Subclass: the exemption is in force → all three complete instantly.
    subclass = TypingAwareRateLimiter(group_max_rate=1, group_time_period=60)
    for _ in range(3):
        result = await asyncio.wait_for(
            _invoke(subclass, "sendChatAction", _GROUP_CHAT_ID), timeout=1.0
        )
        assert result is True

    # Parent-class CONTROL (the RED proof): the same 3 sendChatAction calls
    # route through the 1-slot group bucket, so the 2nd blocks.
    parent = AIORateLimiter(group_max_rate=1, group_time_period=60)
    first = await asyncio.wait_for(
        _invoke(parent, "sendChatAction", _GROUP_CHAT_ID), timeout=1.0
    )
    assert first is True
    await _assert_blocks(_invoke(parent, "sendChatAction", _GROUP_CHAT_ID))


async def test_non_typing_endpoints_still_group_limited():
    """The exemption is endpoint-scoped: sendMessage still uses the group
    bucket, so the second call to a group chat blocks."""
    limiter = TypingAwareRateLimiter(group_max_rate=1, group_time_period=60)
    first = await asyncio.wait_for(
        _invoke(limiter, "sendMessage", _GROUP_CHAT_ID), timeout=1.0
    )
    assert first is True
    await _assert_blocks(_invoke(limiter, "sendMessage", _GROUP_CHAT_ID))


async def test_real_request_body_carries_negative_chat_id():
    """F1 drift guard (the most important test): the dummy chat_id is
    classification-only — the REAL negative chat_id reaches the request path
    (``args[1]``) and the original ``data`` dict is not mutated. Pins the
    data-is-classification-only property against a future PTB upgrade that would
    start using ``data`` for the actual request body."""
    limiter = TypingAwareRateLimiter(group_max_rate=1, group_time_period=60)
    recorded: dict = {}

    async def recording_callback(*args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return True

    # ExtBot passes the SAME dict object as both ``data`` and ``args[1]``.
    data = {"chat_id": _GROUP_CHAT_ID, "message_thread_id": 42, "action": "typing"}
    result = await limiter.process_request(
        callback=recording_callback,
        args=("sendChatAction", data),
        kwargs={},
        endpoint="sendChatAction",
        data=data,
        rate_limit_args=None,
    )

    assert result is True
    # The real request body (args[1]) reached the callback with the genuine
    # group chat id — the classification dummy never leaked into the request.
    assert recorded["args"][1] is data
    assert recorded["args"][1]["chat_id"] == _GROUP_CHAT_ID
    # The original data object was NOT mutated by the classification copy.
    assert data["chat_id"] == _GROUP_CHAT_ID


async def test_overall_limiter_still_applies_to_typing():
    """chat=True is kept (the positive dummy is non-None), so the overall
    limiter (here 1/60) still paces sendChatAction — the safety net stays
    layered and the second typing action blocks."""
    limiter = TypingAwareRateLimiter(overall_max_rate=1, overall_time_period=60)
    first = await asyncio.wait_for(
        _invoke(limiter, "sendChatAction", _GROUP_CHAT_ID), timeout=1.0
    )
    assert first is True
    await _assert_blocks(_invoke(limiter, "sendChatAction", _GROUP_CHAT_ID))
