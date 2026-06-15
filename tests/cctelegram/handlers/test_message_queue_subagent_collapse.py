"""Fix 5 PR-B: the deterministic Workflow-close collapse on the route FIFO.

Covers the message_queue half of the close-collapse:

  - ``collapse_subagent_cards_with_prefix`` is summary-gated (``keep`` stays
    live, ``off`` has no slot), prefix-scoped, and idempotent;
  - ``enqueue_subagent_collapse`` lands a ``subagent_collapse`` control task in
    the route's FIFO AFTER the run's content tasks (so the card exists when it
    fires);
  - the control task is flood-control / RetryAfter-safe EXACTLY like content
    (``_RETRYABLE_TASK_TYPES``) — never the silent ``task_done()`` drop that
    would strand an empty-final card under flood.

Synthetic ids/content (no PII).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import RetryAfter

from cctelegram.handlers import message_queue
from cctelegram.handlers import output_prefs


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_queue_state():
    message_queue.reset_for_tests()
    yield
    message_queue.reset_for_tests()


_PARENT = "psess"
_RUN_A = "wf_runAAA"
_RUN_B = "wf_runBBB"
_PREFIX_A = f"sub:{_PARENT}:{_RUN_A}:"
_PREFIX_B = f"sub:{_PARENT}:{_RUN_B}:"


def _seed_card(
    user_id: int,
    thread_id: int,
    subagent_key: str,
    *,
    collapsed: bool = False,
    window_id: str = "@0",
) -> message_queue.SubagentDigestState:
    """Seed a live (un-collapsed) sub-agent card state slot directly."""
    tid = thread_id or 0
    state = message_queue.SubagentDigestState(
        message_id=1234,
        window_id=window_id,
        subagent_key=subagent_key,
        lines=["• 🔧 **Bash**"],
        tool_count=1,
        collapsed=collapsed,
    )
    message_queue._subagent_msg_info[(user_id, tid, subagent_key)] = state
    return state


@pytest.mark.usefixtures("_clear_queue_state")
class TestCollapsePrefixHelper:
    @pytest.mark.asyncio
    async def test_summary_collapses_matching_cards(self, mock_bot: AsyncMock):
        """⭐ B-i path 3: under ``summary`` the helper collapses every live card
        whose key starts with the run prefix."""
        key = f"{_PREFIX_A}agent-aaa111"
        _seed_card(1, 42, key)
        with patch.object(
            output_prefs,
            "resolve",
            return_value=_prefs(subagent_cards=output_prefs.SUBAGENT_CARDS_SUMMARY),
        ):
            await message_queue.collapse_subagent_cards_with_prefix(
                mock_bot, 1, 42, _PREFIX_A
            )
        assert message_queue._subagent_msg_info[(1, 42, key)].collapsed is True

    @pytest.mark.asyncio
    async def test_keep_recipient_not_collapsed(self, mock_bot: AsyncMock):
        """⭐ B-i (Hermes-delta P1-3): ``keep``/verbose stays live — the helper
        early-returns on a non-summary recipient."""
        key = f"{_PREFIX_A}agent-aaa111"
        _seed_card(1, 42, key)
        with patch.object(
            output_prefs,
            "resolve",
            return_value=_prefs(subagent_cards=output_prefs.SUBAGENT_CARDS_KEEP),
        ):
            await message_queue.collapse_subagent_cards_with_prefix(
                mock_bot, 1, 42, _PREFIX_A
            )
        assert message_queue._subagent_msg_info[(1, 42, key)].collapsed is False

    @pytest.mark.asyncio
    async def test_prefix_only_hits_its_run(self, mock_bot: AsyncMock):
        """⭐ B-i: closing run A collapses ONLY run A's cards; run B stays live."""
        key_a = f"{_PREFIX_A}agent-aaa111"
        key_b = f"{_PREFIX_B}agent-bbb222"
        _seed_card(1, 42, key_a)
        _seed_card(1, 42, key_b)
        with patch.object(
            output_prefs,
            "resolve",
            return_value=_prefs(subagent_cards=output_prefs.SUBAGENT_CARDS_SUMMARY),
        ):
            await message_queue.collapse_subagent_cards_with_prefix(
                mock_bot, 1, 42, _PREFIX_A
            )
        assert message_queue._subagent_msg_info[(1, 42, key_a)].collapsed is True
        assert message_queue._subagent_msg_info[(1, 42, key_b)].collapsed is False

    @pytest.mark.asyncio
    async def test_thread_id_none_normalizes_internally(self, mock_bot: AsyncMock):
        """B-i: a None thread_id matches the tid=0 state slot (normalize
        internally, pass the original thread_id through to the primitives)."""
        key = f"{_PREFIX_A}agent-aaa111"
        _seed_card(1, 0, key)
        with patch.object(
            output_prefs,
            "resolve",
            return_value=_prefs(subagent_cards=output_prefs.SUBAGENT_CARDS_SUMMARY),
        ):
            await message_queue.collapse_subagent_cards_with_prefix(
                mock_bot, 1, None, _PREFIX_A
            )
        assert message_queue._subagent_msg_info[(1, 0, key)].collapsed is True

    @pytest.mark.asyncio
    async def test_idempotent_on_already_collapsed(self, mock_bot: AsyncMock):
        """⭐ B-i: a re-run on an already-collapsed card is a no-op (flush
        branch) — never a crash, never an inflation."""
        key = f"{_PREFIX_A}agent-aaa111"
        _seed_card(1, 42, key, collapsed=True)
        with patch.object(
            output_prefs,
            "resolve",
            return_value=_prefs(subagent_cards=output_prefs.SUBAGENT_CARDS_SUMMARY),
        ):
            # Should not raise; stays collapsed.
            await message_queue.collapse_subagent_cards_with_prefix(
                mock_bot, 1, 42, _PREFIX_A
            )
            await message_queue.collapse_subagent_cards_with_prefix(
                mock_bot, 1, 42, _PREFIX_A
            )
        assert message_queue._subagent_msg_info[(1, 42, key)].collapsed is True


@pytest.mark.usefixtures("_clear_queue_state")
class TestEnqueueAndFifo:
    @pytest.mark.asyncio
    async def test_collapse_task_runs_after_content_in_route_fifo(
        self, mock_bot: AsyncMock
    ):
        """⭐ B-i (Hermes-delta P1-1): a content task and then a
        subagent_collapse task on the same route → the worker runs the content
        FIRST, the collapse SECOND (so the card always exists when it fires)."""
        order: list[str] = []
        route = (1, 42, "@0")

        async def fake_content(bot, user_id, task):
            order.append("content")

        async def fake_collapse(bot, user_id, thread_id, key_prefix):
            order.append("collapse")

        with (
            patch.object(
                message_queue, "_process_content_task", side_effect=fake_content
            ),
            patch.object(
                message_queue,
                "collapse_subagent_cards_with_prefix",
                side_effect=fake_collapse,
            ),
        ):
            queue = message_queue._get_or_create_route(mock_bot, route)
            queue.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["block"],
                    content_type="text",
                    thread_id=42,
                )
            )
            await message_queue.enqueue_subagent_collapse(mock_bot, route, _PREFIX_A)
            await queue.join()
            worker = message_queue._route_workers[route]
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        assert order == ["content", "collapse"]


@pytest.mark.usefixtures("_clear_queue_state")
class TestFloodControlSafety:
    @pytest.mark.asyncio
    async def test_collapse_waits_out_active_flood(self, mock_bot: AsyncMock):
        """⭐ B-i (Hermes v4 P1): with _flood_until ACTIVE when the collapse task
        dequeues, the worker WAITS (does not drop) — the collapse EVENTUALLY
        runs. A non-retryable control task would be dropped here."""
        ran: list[str] = []
        route = (1, 42, "@0")

        async def fake_collapse(bot, user_id, thread_id, key_prefix):
            ran.append(key_prefix)

        # Active flood-control window for this user.
        import time as _time

        message_queue._flood_until[1] = _time.monotonic() + 5

        with (
            patch.object(
                message_queue,
                "collapse_subagent_cards_with_prefix",
                side_effect=fake_collapse,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await message_queue.enqueue_subagent_collapse(mock_bot, route, _PREFIX_A)
            await message_queue._route_queues[route].join()
            worker = message_queue._route_workers[route]
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        assert ran == [_PREFIX_A], "collapse must wait out flood, not be dropped"

    @pytest.mark.asyncio
    async def test_collapse_retries_on_retry_after(self, mock_bot: AsyncMock):
        """⭐ B-i (Hermes v4 P1): a RetryAfter raised by the collapse's own
        Telegram edit must RETRY (up to CONTENT_RETRY_MAX_ATTEMPTS), not drop.
        The collapse is idempotent so the retry re-run is safe."""
        attempts: list[str] = []
        route = (1, 42, "@0")

        async def flaky_collapse(bot, user_id, thread_id, key_prefix):
            attempts.append(key_prefix)
            if len(attempts) == 1:
                raise RetryAfter(timedelta(seconds=1))

        with (
            patch.object(
                message_queue,
                "collapse_subagent_cards_with_prefix",
                side_effect=flaky_collapse,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await message_queue.enqueue_subagent_collapse(mock_bot, route, _PREFIX_A)
            await message_queue._route_queues[route].join()
            worker = message_queue._route_workers[route]
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        assert len(attempts) == 2, "collapse must retry on RetryAfter, not drop"


def _prefs(**overrides):
    """Build an OutputPrefs from the verbose preset with overrides."""
    from dataclasses import replace

    return replace(output_prefs.PRESETS["verbose"], **overrides)
