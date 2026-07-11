"""Scenario: voice message gets transcribed and forwarded.

The voice path downloads OGG bytes from Telegram, asks the OpenAI substrate
(``transcribe_voice``) to convert them to text, and offers the transcription
to the per-route inbound aggregator. The user also gets an echo bubble
with the raw transcription text. Substrate boundaries (Telegram file
download + OpenAI transcription) are stubbed; the handler stack is real.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import inbound_telegram as inbound_module
from tests.conftest import ScenarioHarness, _make_message, _make_user


pytestmark = pytest.mark.scenario


def _make_voice_update(*, thread_id: int, duration_s: int = 37) -> MagicMock:
    voice = MagicMock(name="Voice")
    voice.duration = duration_s
    voice_file = MagicMock(name="VoiceFile")
    voice_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"\x00\x01"))
    voice.get_file = AsyncMock(return_value=voice_file)

    msg = _make_message(thread_id=thread_id, voice=voice)
    msg.chat.send_action = AsyncMock()
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user()
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


@pytest.mark.asyncio
async def test_voice_message_transcribes_and_offers_to_aggregator(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    # OpenAI substrate stub: return a known transcription. ``transcribe_voice``
    # is resolved through ``inbound_telegram``'s namespace at call time, so the
    # patch must land there (the legacy ``bot_module`` alias still re-exports
    # the same callable, but that re-export wouldn't reach ``voice_handler``).
    transcribe = AsyncMock(return_value="hello voice")
    monkeypatch.setattr(inbound_module, "transcribe_voice", transcribe)
    # Aggregator offer (substrate to inbound aggregator) — record the call.
    offered: list[tuple[tuple[int, int, str], str]] = []

    async def fake_offer(
        route: tuple[int, int, str], text: str, *, bot: object | None = None
    ) -> None:
        offered.append((route, text))

    monkeypatch.setattr(inbound_module, "aggregator_offer_voice", fake_offer)
    # Pretend we have an OpenAI key configured.
    monkeypatch.setattr(bot_module.config, "openai_api_key", "sk-fake")

    update = _make_voice_update(thread_id=42)
    await bot_module.voice_handler(update, scenario.context)

    assert offered == [((scenario.user_id, 42, wid), "hello voice")]
    transcribe.assert_awaited_once_with(b"\x00\x01", duration_s=37)
    # Echo bubble was sent.
    update.message.reply_text.assert_awaited()
    echo_text = update.message.reply_text.await_args.args[0]
    assert "hello voice" in echo_text


@pytest.mark.asyncio
async def test_voice_echo_failure_does_not_lose_delivered_turn(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    monkeypatch.setattr(
        inbound_module, "transcribe_voice", AsyncMock(return_value="delivered voice")
    )
    offered: list[tuple[tuple[int, int, str], str]] = []

    async def fake_offer(
        route: tuple[int, int, str], text: str, *, bot: object | None = None
    ) -> None:
        offered.append((route, text))

    monkeypatch.setattr(inbound_module, "aggregator_offer_voice", fake_offer)
    monkeypatch.setattr(bot_module.config, "openai_api_key", "sk-fake")
    update = _make_voice_update(thread_id=42)
    update.message.reply_text.side_effect = RuntimeError("echo transport broke")

    with caplog.at_level("WARNING", logger=inbound_module.logger.name):
        await bot_module.voice_handler(update, scenario.context)

    assert offered == [((scenario.user_id, 42, wid), "delivered voice")]
    assert any(
        "voice transcription echo failed" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "classification", "reply_fragment"),
    [
        (asyncio.TimeoutError(), "timeout", "transcription timed out"),
        (
            httpx.ConnectError("no route", request=httpx.Request("POST", "https://x")),
            "connect",
            "Transcription failed",
        ),
        (
            httpx.HTTPStatusError(
                "rate",
                request=httpx.Request("POST", "https://x"),
                response=httpx.Response(
                    429, request=httpx.Request("POST", "https://x")
                ),
            ),
            "http_429",
            "Transcription failed",
        ),
        (
            ValueError("Empty transcription returned by API"),
            "empty",
            "Empty transcription",
        ),
    ],
)
async def test_voice_observability_logs_are_classified_and_content_free(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    classification: str,
    reply_fragment: str,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    monkeypatch.setattr(
        inbound_module, "transcribe_voice", AsyncMock(side_effect=failure)
    )
    monkeypatch.setattr(bot_module.config, "openai_api_key", "sk-fake")
    update = _make_voice_update(thread_id=42, duration_s=360)

    with caplog.at_level("INFO", logger=inbound_module.logger.name):
        await bot_module.voice_handler(update, scenario.context)

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "voice transcription received" in m
        and "duration_s=360" in m
        and "bytes=2" in m
        and "thread=42" in m
        for m in messages
    )
    assert any(f"classification={classification}" in m for m in messages)
    assert all("TOP SECRET TRANSCRIPTION" not in m for m in messages)
    assert reply_fragment in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_voice_success_log_has_latency_and_length_without_text(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    secret = "TOP SECRET TRANSCRIPTION"
    monkeypatch.setattr(
        inbound_module, "transcribe_voice", AsyncMock(return_value=secret)
    )
    monkeypatch.setattr(inbound_module, "aggregator_offer_voice", AsyncMock())
    monkeypatch.setattr(bot_module.config, "openai_api_key", "sk-fake")
    update = _make_voice_update(thread_id=42)

    with caplog.at_level("INFO", logger=inbound_module.logger.name):
        await bot_module.voice_handler(update, scenario.context)

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "voice transcription succeeded" in m
        and "latency_ms=" in m
        and f"text_len={len(secret)}" in m
        for m in messages
    )
    assert all(secret not in m for m in messages)


@pytest.mark.asyncio
async def test_voice_with_no_api_key_warns(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot_module.config, "openai_api_key", "")
    update = _make_voice_update(thread_id=42)
    await bot_module.voice_handler(update, scenario.context)

    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "OpenAI API key" in reply_text


@pytest.mark.asyncio
async def test_voice_with_no_binding_replies_with_error(
    scenario: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bot_module.config, "openai_api_key", "sk-fake")
    update = _make_voice_update(thread_id=42)
    await bot_module.voice_handler(update, scenario.context)

    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "No session bound" in reply_text
