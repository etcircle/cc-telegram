"""Scenario: /dashboard claims a topic as the cross-topic dashboard host.

The /dashboard command must be handled by the bot itself — registered BEFORE
the catch-all command forwarder so it is never forwarded to Claude Code via
tmux keystrokes — and posting it in a topic claims that topic as the invoking
user's dashboard host (message posted there + record persisted through
SessionManager).
"""

from __future__ import annotations

import pytest
from telegram.ext import CommandHandler, MessageHandler

from cctelegram import bot as bot_module
from cctelegram.handlers.dashboard import dashboard_command
from tests.conftest import ScenarioHarness, make_update_command

pytestmark = pytest.mark.scenario


def test_dashboard_handler_registered_before_command_forwarder():
    """PTB dispatches in registration order within a group: the /dashboard
    CommandHandler must come before the catch-all MessageHandler that forwards
    unknown /commands to Claude (pre-C/P2-5)."""
    app = bot_module.create_bot()
    handlers = app.handlers[0]
    dash_idx = next(
        (
            i
            for i, h in enumerate(handlers)
            if isinstance(h, CommandHandler) and "dashboard" in h.commands
        ),
        None,
    )
    assert dash_idx is not None, "/dashboard CommandHandler is not registered"
    fwd_idx = next(
        i
        for i, h in enumerate(handlers)
        if isinstance(h, MessageHandler)
        and h.callback is bot_module.forward_command_handler
    )
    assert dash_idx < fwd_idx


@pytest.mark.asyncio
async def test_dashboard_in_bound_topic_not_forwarded_to_tmux(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo")

    update = make_update_command("dashboard", thread_id=42)
    await dashboard_command(update, scenario.context)

    # Nothing reached the tmux pane.
    assert scenario.tmux.sent_keys == []
    # A dashboard message was posted into the topic and persisted.
    sends = [s for s in scenario.bot.sent if s.method == "send_message"]
    assert len(sends) == 1
    assert sends[0].kwargs.get("message_thread_id") == 42
    rec = scenario.session_manager.get_dashboard(scenario.chat_id, scenario.user_id)
    assert rec is not None
    assert rec["thread_id"] == 42
    assert rec["msg_id"] == sends[0].message_id
    assert "repo" in sends[0].kwargs["text"]


@pytest.mark.asyncio
async def test_topic_close_of_unbound_dashboard_host_clears_record(
    scenario: ScenarioHarness,
) -> None:
    """Hermes Wave C review P2-4: a dedicated dashboard host topic has NO
    bound window, so topic_closed_handler's no-binding branch must still
    clear the dashboard record for that (chat, thread) — not leave it to the
    eventual send-failure backstop."""
    from tests.conftest import make_update_topic_closed

    update = make_update_command("dashboard", thread_id=7)
    await dashboard_command(update, scenario.context)
    assert (
        scenario.session_manager.get_dashboard(scenario.chat_id, scenario.user_id)
        is not None
    )

    close = make_update_topic_closed(thread_id=7)
    await bot_module.topic_closed_handler(close, scenario.context)

    assert (
        scenario.session_manager.get_dashboard(scenario.chat_id, scenario.user_id)
        is None
    )
    assert scenario.tmux.kill_calls == []  # still a no-window no-op otherwise


@pytest.mark.asyncio
async def test_dashboard_rejects_general_topic(scenario: ScenarioHarness) -> None:
    update = make_update_command("dashboard", thread_id=None)
    await dashboard_command(update, scenario.context)
    update.message.reply_text.assert_awaited()
    assert (
        scenario.session_manager.get_dashboard(scenario.chat_id, scenario.user_id)
        is None
    )
