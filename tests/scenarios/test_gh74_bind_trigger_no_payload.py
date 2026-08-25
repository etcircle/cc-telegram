"""Scenario: GH #74 — an unbound topic's text is a knock, not a payload.

The message that opens the directory picker is the trigger and nothing else. It
is never stashed and never replayed into the freshly bound / created / resumed
window, so the bind lands on a topic that is merely READY and the user types the
prompt they actually want.

Each lane is walked end to end from the Telegram seam and asserts the same three
things: the picker card warned that the message would not be sent, the bind
produced NO delivery into the pane, and the post-bind card carries none of the
"first message" copy the replay used to generate.

Attachments are deliberately untouched by #74 — a photo/document parked in an
unbound topic still replays, which is what
``tests/scenarios/test_document_upload.py`` and the flush regressions in
``tests/cctelegram/test_pending_route_payload.py`` pin.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram import terminal_parser
from cctelegram.callback_dispatcher import DispatcherAdapters, dispatch_callback
from cctelegram.config import config
from cctelegram.handlers import inbound_telegram as inbound_module
from cctelegram.handlers.callback_data import (
    CB_DIR_BIND_EXISTING,
    CB_DIR_CONFIRM,
    CB_WIN_BIND,
)
from cctelegram.handlers.directory_browser import (
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    picker_entry,
)
from tests.conftest import (
    ScenarioHarness,
    make_update_real_callback,
    make_update_text,
)

pytestmark = pytest.mark.scenario

_THREAD = 42
_KNOCK = "hi"


def _adapters(scenario: ScenarioHarness) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=scenario.session_manager,
        tmux_manager=scenario.tmux,
        bot=scenario.bot,
        route_runtime=SimpleNamespace(),
        config=config,
        terminal_parser=terminal_parser,
    )


async def _tap(scenario: ScenarioHarness, data: str) -> Any:
    update = make_update_real_callback(
        data,
        bot=scenario.bot,
        thread_id=_THREAD,
        user_id=scenario.user_id,
        chat_id=scenario.chat_id,
    )
    await dispatch_callback(
        update,
        scenario.context,
        _adapters(scenario),
        is_user_allowed_func=lambda _uid: True,
    )
    return update


async def _knock(scenario: ScenarioHarness, *, browse_path: str = "/repo") -> str:
    """Send the trigger text and return the picker card the user was shown."""
    update = make_update_text(_KNOCK, thread_id=_THREAD)
    await bot_module.text_handler(update, scenario.context)
    entry = picker_entry(scenario.user_data, _THREAD)
    assert entry is not None and entry[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert "_pending_thread_text" not in entry
    entry[BROWSE_PATH_KEY] = browse_path
    return update.message.reply_text.await_args.args[0]


def _card_edits(scenario: ScenarioHarness) -> list[str]:
    return [
        str(s.kwargs.get("text") or "")
        for s in scenario.bot.sent
        if s.method == "edit_message_text"
    ]


def _assert_nothing_was_delivered(scenario: ScenarioHarness) -> None:
    assert scenario.tmux.written_texts == [], scenario.tmux.written_texts
    assert scenario.tmux.sent_keys == [], scenario.tmux.sent_keys
    cards = _card_edits(scenario)
    assert not any("First message sent" in c for c in cards), cards
    assert not any("not delivered" in c for c in cards), cards
    assert not any("please resend" in c for c in cards), cards
    assert picker_entry(scenario.user_data, _THREAD) is None


@pytest.mark.asyncio
async def test_the_picker_card_says_the_trigger_will_not_be_sent(
    scenario: ScenarioHarness,
) -> None:
    card = await _knock(scenario)

    assert "won't be sent to Claude" in card, card
    assert "send your prompt" in card, card


@pytest.mark.asyncio
async def test_bind_to_existing_window_delivers_nothing(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    await _knock(scenario)

    await _tap(scenario, CB_DIR_BIND_EXISTING)
    await _tap(scenario, f"{CB_WIN_BIND}0")

    assert scenario.session_manager.thread_bindings[scenario.user_id][_THREAD] == wid
    _assert_nothing_was_delivered(scenario)


@pytest.mark.asyncio
async def test_create_new_window_delivers_nothing(
    scenario: ScenarioHarness,
) -> None:
    # The SessionStart hook registers the window the create is about to mint.
    scenario._write_session_map_entry("@0", "sid-created", "/repo")
    await _knock(scenario)

    await _tap(scenario, CB_DIR_CONFIRM)

    assert scenario.session_manager.thread_bindings[scenario.user_id][_THREAD] == "@0"
    assert any("Send messages here" in c for c in _card_edits(scenario))
    _assert_nothing_was_delivered(scenario)


@pytest.mark.asyncio
async def test_resume_delivers_nothing(scenario: ScenarioHarness) -> None:
    """The lane the #74 incident came from: a `--resume` of a long transcript.

    The replay used to fire ~600 ms after the hook registered the session, while
    the TUI was still painting, and the delivery gate (correctly) refused into a
    pane with no composer. With no payload there is nothing to refuse.
    """
    scenario._write_session_map_entry("@0", "sess-resume", "/repo")
    await _knock(scenario)
    update = make_update_real_callback(
        "x", bot=scenario.bot, thread_id=_THREAD, user_id=scenario.user_id
    )

    await inbound_module._create_and_bind_window(
        update.callback_query,
        scenario.context,
        update.effective_user,
        "/repo",
        _THREAD,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
        resume_session_id="sess-resume",
    )

    assert scenario.session_manager.thread_bindings[scenario.user_id][_THREAD] == "@0"
    assert any("Resumed" in c for c in _card_edits(scenario)), _card_edits(scenario)
    _assert_nothing_was_delivered(scenario)
