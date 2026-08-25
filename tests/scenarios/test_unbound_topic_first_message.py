"""Scenario: first message in an unbound topic opens the directory browser.

When a user sends text in a topic with no ``thread_bindings`` entry, the bot
must:
  - reply with the directory browser keyboard,
  - say on that card that the message is a knock and will not be sent (GH #74),
  - claim this thread's picker entry and key it by thread id (GH #66) so
    callbacks know which thread owns the picker,
  - store NOTHING to replay: the text is neither stashed nor delivered.

A separate scenario (``test_gh74_bind_trigger_no_payload``) walks the trigger
message through every bind lane; ``test_stale_pending_replacement`` covers the
case where a *second* unbound topic shows up while the first is still mid-picker
— GH #66: the two coexist as independent per-thread entries (no displacement).
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.directory_browser import (
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    picker_entry,
)
from tests.conftest import ScenarioHarness, make_update_text

pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_unbound_topic_text_opens_browser_without_stashing_the_text(
    scenario: ScenarioHarness,
) -> None:
    update = make_update_text("hello claude", thread_id=42)

    await bot_module.text_handler(update, scenario.context)

    # Browser reply was sent (with an inline keyboard).
    update.message.reply_text.assert_awaited()
    sent_kwargs = update.message.reply_text.await_args.kwargs
    assert "reply_markup" in sent_kwargs
    # GH #74: the card says so BEFORE the user can be surprised by a message
    # that silently went nowhere.
    card = update.message.reply_text.await_args.args[0]
    assert "won't be sent to Claude" in card, card
    # The thread's per-topic picker entry is claimed (GH #66) — and holds no
    # payload: the text was the knock, not the first turn (GH #74).
    entry = picker_entry(scenario.user_data, 42)
    assert entry is not None
    assert "_pending_thread_text" not in entry
    assert entry[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert BROWSE_PATH_KEY in entry
    # No tmux send_keys: nothing is forwarded until the directory is picked.
    assert scenario.tmux.sent_keys == []


@pytest.mark.asyncio
async def test_unbound_topic_no_thread_id_rejects(
    scenario: ScenarioHarness,
) -> None:
    """Text outside a named topic rejects rather than auto-creating a window."""
    update = make_update_text("hello", thread_id=None)

    await bot_module.text_handler(update, scenario.context)

    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "named topic" in reply_text


@pytest.mark.asyncio
async def test_bound_topic_with_dead_window_unbinds_and_warns(
    scenario: ScenarioHarness,
) -> None:
    """Bound topic but window gone → unbind + plain error, no tmux send."""
    scenario.session_manager.thread_bindings.setdefault(scenario.user_id, {})[42] = "@9"
    scenario.session_manager.window_display_names["@9"] = "ghost"

    update = make_update_text("hello", thread_id=42)
    await bot_module.text_handler(update, scenario.context)

    update.message.reply_text.assert_awaited()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "no longer exists" in reply_text
    # Binding was removed.
    bindings = scenario.session_manager.thread_bindings.get(scenario.user_id, {})
    assert 42 not in bindings
