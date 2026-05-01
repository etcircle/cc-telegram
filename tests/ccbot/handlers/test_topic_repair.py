"""Tests for the (currently disabled) topic_repair scaffold.

The repair pipeline is shipped behind a feature flag so production behaviour
matches today: every call returns EMERGENCY_DM. These tests pin that
contract so the wrappers cannot accidentally enable the unfinished
reopen/recreate path.
"""

from __future__ import annotations

import pytest

from ccbot.handlers import topic_repair
from ccbot.handlers.message_sender import TopicSendOutcome


@pytest.fixture(autouse=True)
def _reset_repair_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CCBOT_TOPIC_REPAIR", raising=False)
    topic_repair.reset_for_tests()


def test_disabled_by_default() -> None:
    assert topic_repair.is_enabled() is False


def test_flag_parses_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("CCBOT_TOPIC_REPAIR", value)
        assert topic_repair.is_enabled() is True
    for value in ("0", "false", "no", ""):
        monkeypatch.setenv("CCBOT_TOPIC_REPAIR", value)
        assert topic_repair.is_enabled() is False


@pytest.mark.asyncio
async def test_try_repair_returns_emergency_dm_when_disabled() -> None:
    result = await topic_repair.try_repair(
        bot=object(),  # type: ignore[arg-type]  # not touched while disabled
        user_id=1,
        thread_id=42,
        window_id="@0",
        outcome=TopicSendOutcome.TOPIC_NOT_FOUND,
    )
    assert result.action is topic_repair.RepairAction.EMERGENCY_DM
    assert result.new_thread_id is None
    assert "disabled" in result.reason.lower()


@pytest.mark.asyncio
async def test_try_repair_noop_for_non_broken_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CCBOT_TOPIC_REPAIR", "1")
    result = await topic_repair.try_repair(
        bot=object(),  # type: ignore[arg-type]
        user_id=1,
        thread_id=42,
        window_id="@0",
        outcome=TopicSendOutcome.OTHER,
    )
    assert result.action is topic_repair.RepairAction.NOOP


@pytest.mark.asyncio
async def test_try_repair_stub_when_flag_on_returns_emergency_dm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the flag on, the unimplemented body must not call Telegram.

    Until reopen/create are wired, the stub must keep returning EMERGENCY_DM
    so behaviour matches today. This is the safety net that prevents an
    accidental flag flip from spawning Rescue: topics.
    """
    monkeypatch.setenv("CCBOT_TOPIC_REPAIR", "1")
    result = await topic_repair.try_repair(
        bot=object(),  # type: ignore[arg-type]  # must not be touched
        user_id=1,
        thread_id=42,
        window_id="@0",
        outcome=TopicSendOutcome.TOPIC_CLOSED,
    )
    assert result.action is topic_repair.RepairAction.EMERGENCY_DM
