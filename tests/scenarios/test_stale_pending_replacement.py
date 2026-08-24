"""Scenario: concurrent unbound-topic pickers coexist per thread (GH #66).

Pre-GH #66 the picker payload was a single per-USER slot, so a second unbound
topic REPLACED the first's pending payload and the first's card orphaned. Under
per-(user, thread) keying each topic owns its own picker entry, so two topics
mid-picker at the same time both keep their own text/state, and a cancel in one
topic never touches the other's payload.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers.callback_data import CB_DIR_CANCEL
from cctelegram.handlers.directory_browser import (
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    picker_entry,
)
from tests.conftest import ScenarioHarness, make_update_callback, make_update_text

pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_two_topics_pickers_coexist_without_displacement(
    scenario: ScenarioHarness,
) -> None:
    # Topic A: user is browsing for a directory after sending "first".
    update_a = make_update_text("first", thread_id=42)
    await bot_module.text_handler(update_a, scenario.context)
    entry_a = picker_entry(scenario.user_data, 42)
    assert entry_a is not None
    assert entry_a["_pending_thread_text"] == "first"

    # Topic B arrives with a new unbound text — it opens its OWN picker and
    # must NOT displace topic A (GH #66).
    update_b = make_update_text("second", thread_id=43, message_id=200)
    await bot_module.text_handler(update_b, scenario.context)

    entry_b = picker_entry(scenario.user_data, 43)
    assert entry_b is not None
    assert entry_b["_pending_thread_text"] == "second"
    assert entry_b[STATE_KEY] == STATE_BROWSING_DIRECTORY

    # Topic A is fully intact — no displacement.
    entry_a = picker_entry(scenario.user_data, 42)
    assert entry_a is not None
    assert entry_a["_pending_thread_text"] == "first"
    assert entry_a[STATE_KEY] == STATE_BROWSING_DIRECTORY


@pytest.mark.asyncio
async def test_cancel_in_one_topic_preserves_the_other_topics_payload(
    scenario: ScenarioHarness,
) -> None:
    """A cancel in topic A must NOT clear topic B's pending payload."""
    await bot_module.text_handler(
        make_update_text("first", thread_id=42), scenario.context
    )
    await bot_module.text_handler(
        make_update_text("second", thread_id=43, message_id=200),
        scenario.context,
    )

    # Cancel callback fires from topic A (thread 42).
    cancel_update = make_update_callback(CB_DIR_CANCEL, thread_id=42)
    await bot_module.callback_handler(cancel_update, scenario.context)

    # Topic A's entry is gone; topic B's payload survives untouched.
    assert picker_entry(scenario.user_data, 42) is None
    entry_b = picker_entry(scenario.user_data, 43)
    assert entry_b is not None
    assert entry_b["_pending_thread_text"] == "second"
    # The cancel was acknowledged.
    cancel_update.callback_query.answer.assert_awaited()
