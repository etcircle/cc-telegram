"""Scenario: cosmetic inbound typing failures never block payload delivery."""

from __future__ import annotations

import pytest
from telegram.error import TimedOut

from cctelegram import bot as bot_module
from cctelegram.handlers import inbound_telegram as inbound_module
from cctelegram.handlers.inbound_aggregator import aggregator_flush_route
from tests.conftest import ScenarioHarness, make_update_text


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_text_typing_timeout_still_delivers_to_tmux(
    scenario: ScenarioHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route = (scenario.user_id, 42, wid)
    update = make_update_text("text survives typing timeout", thread_id=42)
    update.message.chat.send_action.side_effect = TimedOut("typing timed out")

    with caplog.at_level("WARNING", logger=inbound_module.logger.name):
        await bot_module.text_handler(update, scenario.context)
        delivered = await aggregator_flush_route(route)

    assert delivered is True
    assert any(
        sent_wid == wid and "text survives typing timeout" in keys
        for sent_wid, keys, _, _ in scenario.tmux.sent_keys
    )
    assert any(
        "inbound typing action failed (non-fatal) thread=42" in record.getMessage()
        for record in caplog.records
    )
