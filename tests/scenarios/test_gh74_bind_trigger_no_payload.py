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

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
    ensure_picker_entry,
    picker_entry,
)
from tests.conftest import (
    ScenarioHarness,
    _make_message,
    _make_user,
    make_update_real_callback,
    make_update_text,
)

pytestmark = pytest.mark.scenario

_THREAD = 42
_KNOCK = "hi"


def _document_update(caption: str = "notes") -> MagicMock:
    """A document Update — the payload kind GH #74 deliberately still holds."""
    document = MagicMock(name="Document")
    document.file_size = 1024
    document.file_name = "notes.txt"
    document.file_unique_id = "uid-74"

    async def _download(dest: Any) -> Any:
        Path(dest).write_bytes(b"\x00")
        return dest

    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock(side_effect=_download)
    document.get_file = AsyncMock(return_value=tg_file)

    msg = _make_message(thread_id=_THREAD, caption=caption, document=document)
    msg.chat.send_action = AsyncMock()
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user()
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


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
    assert not any("message sent" in c.lower() for c in cards), cards
    assert not any("attachment sent" in c.lower() for c in cards), cards
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
async def test_text_typed_mid_picker_says_it_will_not_be_sent(
    scenario: ScenarioHarness,
) -> None:
    """A document opened the picker; the user then types the actual prompt.

    That text reaches the picker-owned nudge, which pre-#74 only pointed at the
    card — so the prompt was discarded in silence.
    """
    await bot_module.document_handler(_document_update(), scenario.context)
    entry = picker_entry(scenario.user_data, _THREAD)
    assert entry is not None
    pending = list(entry["_pending_thread_attachments"])
    assert pending

    update = make_update_text("please refactor this", thread_id=_THREAD)
    await bot_module.text_handler(update, scenario.context)

    reply = update.message.reply_text.await_args.args[0]
    assert "directory browser above" in reply, reply
    assert "won't be sent to Claude" in reply, reply
    assert "once the topic is bound" in reply, reply
    # The nudge changed nothing: the picker still belongs to this thread and
    # still holds the attachment it was opened with.
    entry = picker_entry(scenario.user_data, _THREAD)
    assert entry is not None
    assert entry[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert "_pending_thread_text" not in entry
    assert list(entry["_pending_thread_attachments"]) == pending
    assert scenario.tmux.sent_keys == []


@pytest.mark.asyncio
async def test_a_pre_upgrade_text_stash_is_scrubbed_on_the_next_inbound(
    scenario: ScenarioHarness, tmp_path: Path
) -> None:
    """Migration-on-access for entries written by a pre-#74 process.

    Nothing reads the legacy keys any more, but a picker the user never finishes
    keeps its entry alive for the life of the process — so the text would sit in
    ``user_data`` indefinitely. The next inbound into the topic scrubs it,
    without disturbing the attachment or the picker ownership around it.
    """
    payload = tmp_path / "carried-over.bin"
    payload.write_bytes(b"data")
    legacy = inbound_module.PendingAttachment(str(payload), "caption", None)
    entry = ensure_picker_entry(scenario.user_data, _THREAD)
    entry.update(
        {
            STATE_KEY: STATE_BROWSING_DIRECTORY,
            BROWSE_PATH_KEY: "/repo",
            "_pending_thread_text": "text from the old process",
            "_pending_thread_text_facts": {"typed_text": True, "reply_context": False},
            "_pending_thread_attachments": [legacy],
        }
    )

    await bot_module.text_handler(
        make_update_text("a new message", thread_id=_THREAD), scenario.context
    )

    entry = picker_entry(scenario.user_data, _THREAD)
    assert entry is not None, "the scrub must not cost this thread its picker"
    assert "_pending_thread_text" not in entry
    assert "_pending_thread_text_facts" not in entry
    # Everything the scrub must NOT touch.
    assert entry[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert entry[BROWSE_PATH_KEY] == "/repo"
    assert list(entry["_pending_thread_attachments"]) == [legacy]
    assert payload.exists()


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
