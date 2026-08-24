"""GH #66: restart-orphan picker cards self-heal; /start is a global reset.

Picker state is per (user, thread) and purely in-memory. After a bot restart a
picker card's entry is gone, so tapping it can no longer resolve — the callback
now edits the dead card to a self-heal message (keyboard stripped) instead of a
bare popup. ``/start`` drops EVERY topic's picker entry.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.callback_data import CB_DIR_SELECT
from cctelegram.handlers.directory_browser import (
    CARD_CHAT_ID_KEY,
    CARD_MSG_ID_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_WINDOW,
    ensure_picker_entry,
    picker_entry,
)
from tests.conftest import ScenarioHarness, make_update_callback, make_update_text

pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_restart_orphan_picker_card_selfheals_on_tap(
    scenario: ScenarioHarness,
) -> None:
    # No entry for thread 42 — the in-memory loss a restart produces.
    update = make_update_callback(f"{CB_DIR_SELECT}0", thread_id=42)
    await bot_module.callback_handler(update, scenario.context)

    query = update.callback_query
    # The dead card was edited (self-heal) with its keyboard stripped.
    query.edit_message_text.assert_awaited_once()
    kwargs = query.edit_message_text.await_args.kwargs
    assert kwargs.get("reply_markup") is None
    text = query.edit_message_text.await_args.args[0]
    assert "expired" in text
    query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_start_command_clears_all_picker_entries(
    scenario: ScenarioHarness,
) -> None:
    ensure_picker_entry(scenario.user_data, 42).update(
        {STATE_KEY: STATE_BROWSING_DIRECTORY}
    )
    ensure_picker_entry(scenario.user_data, 43).update(
        {STATE_KEY: STATE_SELECTING_WINDOW}
    )

    update = make_update_text("/start", thread_id=None)
    await bot_module.start_command(update, scenario.context)

    # Global reset: every topic's picker entry is gone.
    assert picker_entry(scenario.user_data, 42) is None
    assert picker_entry(scenario.user_data, 43) is None


@pytest.mark.asyncio
async def test_start_command_disables_recorded_picker_cards(
    scenario: ScenarioHarness,
) -> None:
    """Codex Q5: /start disables EACH entry's recorded card (not just clears state)."""
    ensure_picker_entry(scenario.user_data, 42).update(
        {
            STATE_KEY: STATE_BROWSING_DIRECTORY,
            CARD_CHAT_ID_KEY: -100,
            CARD_MSG_ID_KEY: 11,
        }
    )
    ensure_picker_entry(scenario.user_data, 43).update(
        {STATE_KEY: STATE_SELECTING_WINDOW, CARD_CHAT_ID_KEY: -100, CARD_MSG_ID_KEY: 22}
    )

    update = make_update_text("/start", thread_id=None)
    await bot_module.start_command(update, scenario.context)

    edits = [s for s in scenario.bot.sent if s.method == "edit_message_text"]
    disabled_ids = {s.kwargs["message_id"] for s in edits}
    assert disabled_ids == {11, 22}
    for s in edits:
        assert s.kwargs["reply_markup"] is None
        assert "Picker closed" in s.kwargs["text"]
    # …and the entries are gone.
    assert picker_entry(scenario.user_data, 42) is None
    assert picker_entry(scenario.user_data, 43) is None
