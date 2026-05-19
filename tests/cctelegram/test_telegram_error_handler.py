"""Unit tests for Telegram Application error categorisation.

Covers stale callback/edit suppression plus Forbidden, Conflict, NetworkError,
and unknown-error logging branches in the bot-level PTB error handler.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError

from cctelegram.bot import _telegram_error_handler


async def _handle(
    error: Exception, caplog: pytest.LogCaptureFixture, level: str
) -> None:
    with caplog.at_level(level, logger="cctelegram.bot"):
        await _telegram_error_handler(None, SimpleNamespace(error=error))


@pytest.mark.asyncio
async def test_handler_suppresses_query_too_old(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _handle(BadRequest("Query is too old"), caplog, "INFO")

    assert "telegram_stale_callback" in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_handler_suppresses_query_id_invalid(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _handle(BadRequest("query id is invalid"), caplog, "INFO")

    assert "telegram_stale_callback" in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_handler_suppresses_message_not_modified(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _handle(BadRequest("Message is not modified"), caplog, "INFO")

    assert "telegram_stale_callback" in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_handler_suppresses_message_to_edit_not_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _handle(BadRequest("message to edit not found"), caplog, "INFO")

    assert "telegram_stale_callback" in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_handler_categorises_forbidden_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _handle(Forbidden("bot was blocked by user"), caplog, "INFO")

    assert "telegram_forbidden" in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_handler_categorises_conflict_at_critical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _handle(
        Conflict("terminated by other getUpdates request"), caplog, "CRITICAL"
    )

    assert "telegram_conflict_multiple_pollers" in caplog.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_handler_categorises_network_error_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _handle(NetworkError("connection reset"), caplog, "WARNING")

    assert "telegram_network_error" in caplog.text


@pytest.mark.asyncio
async def test_handler_logs_unknown_error_at_error_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _handle(BadRequest("Chat not found"), caplog, "ERROR")

    assert "telegram_unhandled_error" in caplog.text
    assert any(record.exc_info for record in caplog.records)
