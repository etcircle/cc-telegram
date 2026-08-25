"""Scenario: concurrent unbound-topic pickers coexist per thread (GH #66).

Pre-GH #66 the picker payload was a single per-USER slot, so a second unbound
topic REPLACED the first's pending state and the first's card orphaned. Under
per-(user, thread) keying each topic owns its own picker entry, so two topics
mid-picker at the same time both keep their own state, and a cancel in one topic
never touches the other's.

GH #74 narrowed what can be pending: a text message is only the knock that opens
the picker, so the payload that has to survive the other topic's traffic is an
ATTACHMENT — the one kind the user would otherwise have to upload twice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.callback_data import CB_DIR_CANCEL
from cctelegram.handlers.directory_browser import (
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    picker_entry,
)
from tests.conftest import (
    ScenarioHarness,
    _make_message,
    _make_user,
    make_update_callback,
    make_update_text,
)

pytestmark = pytest.mark.scenario


def _make_document_update(*, thread_id: int, caption: str) -> MagicMock:
    document = MagicMock(name="Document")
    document.file_size = 1024
    document.file_name = f"{caption}.txt"
    document.file_unique_id = f"uid-{thread_id}"

    async def _download(dest: Any) -> Any:
        Path(dest).write_bytes(b"\x00")
        return dest

    tg_file = MagicMock()
    tg_file.download_to_drive = AsyncMock(side_effect=_download)
    document.get_file = AsyncMock(return_value=tg_file)

    msg = _make_message(thread_id=thread_id, caption=caption, document=document)
    msg.chat.send_action = AsyncMock()
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user()
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


@pytest.mark.asyncio
async def test_two_topics_pickers_coexist_without_displacement(
    scenario: ScenarioHarness,
) -> None:
    # Topic A: user is browsing for a directory after sending "first".
    update_a = make_update_text("first", thread_id=42)
    await bot_module.text_handler(update_a, scenario.context)
    entry_a = picker_entry(scenario.user_data, 42)
    assert entry_a is not None
    assert entry_a[STATE_KEY] == STATE_BROWSING_DIRECTORY
    browse_a = entry_a[BROWSE_PATH_KEY]

    # Topic B arrives with a new unbound text — it opens its OWN picker and
    # must NOT displace topic A (GH #66).
    update_b = make_update_text("second", thread_id=43, message_id=200)
    await bot_module.text_handler(update_b, scenario.context)

    entry_b = picker_entry(scenario.user_data, 43)
    assert entry_b is not None
    assert entry_b[STATE_KEY] == STATE_BROWSING_DIRECTORY

    # Topic A is fully intact — no displacement.
    entry_a = picker_entry(scenario.user_data, 42)
    assert entry_a is not None
    assert entry_a[STATE_KEY] == STATE_BROWSING_DIRECTORY
    assert entry_a[BROWSE_PATH_KEY] == browse_a
    # Neither knock was stored for a post-bind replay (GH #74).
    assert "_pending_thread_text" not in entry_a
    assert "_pending_thread_text" not in entry_b


@pytest.mark.asyncio
async def test_cancel_in_one_topic_preserves_the_other_topics_payload(
    scenario: ScenarioHarness,
) -> None:
    """A cancel in topic A must NOT clear topic B's pending attachment."""
    await bot_module.text_handler(
        make_update_text("first", thread_id=42), scenario.context
    )
    await bot_module.document_handler(
        _make_document_update(thread_id=43, caption="second"), scenario.context
    )
    entry_b = picker_entry(scenario.user_data, 43)
    assert entry_b is not None
    pending_b = list(entry_b["_pending_thread_attachments"])
    assert [a.caption for a in pending_b] == ["second"]

    # Cancel callback fires from topic A (thread 42).
    cancel_update = make_update_callback(CB_DIR_CANCEL, thread_id=42)
    await bot_module.callback_handler(cancel_update, scenario.context)

    # Topic A's entry is gone; topic B's payload survives untouched — file and
    # all, since a cancel deletes the files of the topic it cancels.
    assert picker_entry(scenario.user_data, 42) is None
    entry_b = picker_entry(scenario.user_data, 43)
    assert entry_b is not None
    assert list(entry_b["_pending_thread_attachments"]) == pending_b
    assert Path(pending_b[0].path).exists()
    # The cancel was acknowledged.
    cancel_update.callback_query.answer.assert_awaited()
