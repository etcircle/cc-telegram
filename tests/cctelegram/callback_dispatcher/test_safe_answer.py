"""Unit tests for stale-safe callback-query answering.

Covers happy-path callback answers, show_alert propagation, stale-query
suppression, and re-raising unexpected Telegram BadRequest failures.
"""

from __future__ import annotations

from typing import Any

import pytest
from telegram.error import BadRequest

from cctelegram.callback_dispatcher import safe_answer


class FakeQuery:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        if self.exc is not None:
            raise self.exc


@pytest.mark.asyncio
async def test_safe_answer_text_none_happy_path() -> None:
    query = FakeQuery()

    result = await safe_answer(query)

    assert result is True
    assert query.calls == [((), {})]


@pytest.mark.asyncio
async def test_safe_answer_passes_text_and_alert() -> None:
    query = FakeQuery()

    result = await safe_answer(query, "Working", show_alert=False)

    assert result is True
    assert query.calls == [(("Working",), {"show_alert": False})]


@pytest.mark.asyncio
async def test_safe_answer_passes_show_alert_true() -> None:
    query = FakeQuery()

    result = await safe_answer(query, "Look here", show_alert=True)

    assert result is True
    assert query.calls == [(("Look here",), {"show_alert": True})]


@pytest.mark.asyncio
async def test_safe_answer_swallows_query_is_too_old(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = FakeQuery(BadRequest("Query is too old and response timeout expired"))

    with caplog.at_level("INFO", logger="cctelegram.callback_dispatcher"):
        result = await safe_answer(query)

    assert result is False
    assert "safe_answer skipped stale callback" in caplog.text


@pytest.mark.asyncio
async def test_safe_answer_swallows_query_id_invalid(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = FakeQuery(BadRequest("query id is invalid"))

    with caplog.at_level("INFO", logger="cctelegram.callback_dispatcher"):
        result = await safe_answer(query)

    assert result is False
    assert "safe_answer skipped stale callback" in caplog.text


@pytest.mark.asyncio
async def test_safe_answer_reraises_other_bad_request() -> None:
    query = FakeQuery(BadRequest("Chat not found"))

    with pytest.raises(BadRequest, match="Chat not found"):
        await safe_answer(query)
