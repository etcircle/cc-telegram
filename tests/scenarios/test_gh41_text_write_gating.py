"""Scenario: GH #41 group_chat_ids write gating at the text handler.

The text handler writes ``group_chat_ids`` for supergroup forum routing, but
the write must:
  - be SKIPPED when ``thread_id`` is None (a DM / General message), so no
    ``user:0`` garbage key is minted;
  - still fire for the first message in an unbound NAMED topic, so the
    directory-browser bootstrap has the mapping before any binding exists.
"""

from __future__ import annotations

import pytest

from cctelegram import bot as bot_module
from tests.conftest import (
    _DEFAULT_CHAT_ID,
    _DEFAULT_USER_ID,
    ScenarioHarness,
    make_update_text,
)


pytestmark = pytest.mark.scenario


@pytest.mark.asyncio
async def test_no_thread_id_writes_no_user_zero_key(
    scenario: ScenarioHarness,
) -> None:
    """A thread_id=None message (General / DM) mints NO ``user:0`` mapping."""
    update = make_update_text("stray text", thread_id=None)

    await bot_module.text_handler(update, scenario.context)

    assert f"{_DEFAULT_USER_ID}:0" not in scenario.session_manager.group_chat_ids


@pytest.mark.asyncio
async def test_unbound_named_topic_bootstrap_writes_mapping(
    scenario: ScenarioHarness,
) -> None:
    """The unbound-topic bootstrap still writes the mapping (regression pin)."""
    update = make_update_text("hello claude", thread_id=42)

    await bot_module.text_handler(update, scenario.context)

    assert (
        scenario.session_manager.group_chat_ids[f"{_DEFAULT_USER_ID}:42"]
        == _DEFAULT_CHAT_ID
    )
