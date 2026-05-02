"""Regression tests for message_queue worker behavior.

Covers two recently-fixed bugs:

  - ``_check_and_send_status`` must NOT resurrect a "🟡 Busy" status card
    from a post-completion pane summary. The polling path already gates on
    ``is_status_active`` but the post-content path was missing the same
    check, so a static "✻ Worked for 2s" line could re-create a Busy card
    immediately after Claude finished.

  - ``_message_queue_worker`` must retry content tasks that raise
    ``RetryAfter`` and only drop ephemeral status updates. The previous
    implementation called ``task_done()`` in ``finally`` after handling
    ``RetryAfter`` without re-dispatching, silently dropping real Claude
    output on rate-limit hits.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import RetryAfter

from ccbot.handlers import message_queue


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


def _reset_state() -> None:
    message_queue._route_queues.clear()
    message_queue._route_workers.clear()
    message_queue._route_locks.clear()
    message_queue._route_pending_ephemeral.clear()
    message_queue._route_ephemeral_kick.clear()
    message_queue._route_inflight.clear()
    message_queue._route_tearing_down.clear()
    message_queue._status_msg_info.clear()
    message_queue._tool_msg_ids.clear()
    message_queue._agent_tool_ids.clear()
    message_queue._activity_msg_info.clear()
    message_queue._tool_activity_indices.clear()
    message_queue._flood_until.clear()


@pytest.fixture
def _clear_queue_state():
    """Drop per-route queue/worker state between tests."""
    _reset_state()
    yield
    _reset_state()


@pytest.mark.usefixtures("_clear_queue_state")
class TestCheckAndSendStatus:
    """``_check_and_send_status`` must not resurrect Busy from a stale spinner."""

    @pytest.mark.asyncio
    async def test_post_completion_summary_clears_status(self, mock_bot: AsyncMock):
        """A static "✻ Worked for 2s" line above a blank-line gap is idle.

        Regression: this is the exact pane state that produced the stale
        "🟡 Busy — di-copilot-3 / Worked for 2s" card. ``parse_status_line``
        still returns the spinner text, but ``is_status_active`` reports
        False because the spinner sits above a blank line, not directly
        above the chrome separator. Without the gate, the post-content
        status path resurrected the Busy card the polling path had just
        cleared.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        post_completion_pane = (
            "⏺ Done.\n"
            "\n"
            "✻ Worked for 2s\n"
            "\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
        )

        with (
            patch.object(message_queue, "tmux_manager") as mock_tmux,
            patch.object(
                message_queue, "_do_send_status_message", new_callable=AsyncMock
            ) as mock_send,
            patch.object(
                message_queue, "_do_clear_status_message", new_callable=AsyncMock
            ) as mock_clear,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=post_completion_pane)

            await message_queue._check_and_send_status(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_send.assert_not_called()
            mock_clear.assert_awaited_once_with(mock_bot, 1, 42)

    @pytest.mark.asyncio
    async def test_active_status_sends_busy(self, mock_bot: AsyncMock):
        """A real active spinner (sits directly above chrome) still sends."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        busy_pane = (
            "✻ Cooking for 2s\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt\n"
        )

        with (
            patch.object(message_queue, "tmux_manager") as mock_tmux,
            patch.object(
                message_queue, "_do_send_status_message", new_callable=AsyncMock
            ) as mock_send,
            patch.object(
                message_queue, "_do_clear_status_message", new_callable=AsyncMock
            ) as mock_clear,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=busy_pane)

            await message_queue._check_and_send_status(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_clear.assert_not_called()
            mock_send.assert_awaited_once()
            args = mock_send.await_args.args
            # _do_send_status_message(bot, user_id, thread_id_or_0, window_id, text)
            assert args[1] == 1
            assert args[2] == 42
            assert args[3] == window_id
            assert args[4] == "Cooking for 2s"

    @pytest.mark.asyncio
    async def test_no_status_line_clears(self, mock_bot: AsyncMock):
        """Pane with no spinner at all → clear (consistent with idle path)."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        idle_pane = (
            "$ echo done\n"
            "done\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
        )

        with (
            patch.object(message_queue, "tmux_manager") as mock_tmux,
            patch.object(
                message_queue, "_do_send_status_message", new_callable=AsyncMock
            ) as mock_send,
            patch.object(
                message_queue, "_do_clear_status_message", new_callable=AsyncMock
            ) as mock_clear,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=idle_pane)

            await message_queue._check_and_send_status(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_send.assert_not_called()
            mock_clear.assert_awaited_once_with(mock_bot, 1, 42)


@pytest.mark.usefixtures("_clear_queue_state")
class TestRetryAfterContentRetry:
    """``_message_queue_worker`` must retry content tasks on ``RetryAfter``."""

    @pytest.mark.asyncio
    async def test_content_task_retried_after_retry_after(self, mock_bot: AsyncMock):
        """A content task that raises RetryAfter once must be retried, not lost.

        Regression: prior implementation set ``_flood_until`` (or slept) and
        then ran ``queue.task_done()`` in ``finally``, dropping the actual
        Claude message. Content tasks are real output — losing them is silent
        data loss.
        """
        attempts = []

        async def flaky_process(bot, user_id, task):
            attempts.append(task)
            if len(attempts) == 1:
                raise RetryAfter(timedelta(seconds=1))
            # Second attempt succeeds.

        route = (1, 42, "@0")
        with (
            patch.object(
                message_queue,
                "_process_content_task",
                side_effect=flaky_process,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            queue = message_queue._get_or_create_route(mock_bot, route)
            queue.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["hello world"],
                    content_type="text",
                    thread_id=42,
                )
            )
            await queue.join()
            # Stop the worker so the test fixture can clean up cleanly.
            worker = message_queue._route_workers[route]
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        assert len(attempts) == 2, (
            "content task was not retried — RetryAfter dropped the message"
        )
        # We slept the retry-after window once between attempts.
        assert mock_sleep.await_count >= 1

    @pytest.mark.asyncio
    async def test_status_task_dropped_on_retry_after(self, mock_bot: AsyncMock):
        """Status updates are ephemeral — RetryAfter drops them, no retry."""
        attempts = []

        async def flaky_process(bot, user_id, task):
            attempts.append(task)
            raise RetryAfter(timedelta(seconds=1))

        route = (1, 42, "@0")
        with patch.object(
            message_queue,
            "_process_status_update_task",
            side_effect=flaky_process,
        ):
            await message_queue.enqueue_status_update(
                mock_bot,
                user_id=1,
                window_id="@0",
                status_text="Cooking for 2s",
                thread_id=42,
            )
            # Yield repeatedly so the worker picks up and drains the ephemeral.
            for _ in range(50):
                if attempts:
                    break
                await asyncio.sleep(0)
            worker = message_queue._route_workers[route]
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        assert len(attempts) == 1, (
            "status update was retried — should be dropped as ephemeral"
        )

    @pytest.mark.asyncio
    async def test_content_dropped_after_max_retries(self, mock_bot: AsyncMock, caplog):
        """Persistent RetryAfter eventually gives up after max attempts.

        We do not want an infinite loop on a stuck rate limit; bound the
        retries and log loudly so the operator knows real output was lost.
        """
        import logging

        attempts = []

        async def always_fails(bot, user_id, task):
            attempts.append(task)
            raise RetryAfter(timedelta(seconds=1))

        route = (1, 42, "@0")
        with (
            patch.object(
                message_queue,
                "_process_content_task",
                side_effect=always_fails,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
            caplog.at_level(logging.ERROR, logger="ccbot.handlers.message_queue"),
        ):
            queue = message_queue._get_or_create_route(mock_bot, route)
            queue.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["hello world"],
                    content_type="text",
                    thread_id=42,
                )
            )
            await queue.join()
            worker = message_queue._route_workers[route]
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        assert len(attempts) == message_queue.CONTENT_RETRY_MAX_ATTEMPTS
        assert any("Content task dropped" in rec.message for rec in caplog.records), (
            "expected an ERROR log when content is finally dropped"
        )


async def _yield_until(predicate, *, ticks: int = 200) -> bool:
    """Yield to the loop until ``predicate()`` is truthy or ticks exhausted."""
    for _ in range(ticks):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


@pytest.mark.usefixtures("_clear_queue_state")
class TestRouteIsolation:
    """Per-route queues prevent cross-topic head-of-line blocking."""

    @pytest.mark.asyncio
    async def test_status_for_route_b_lands_while_route_a_is_blocked(
        self, mock_bot: AsyncMock
    ):
        route_a = (1, 100, "@0")
        route_b = (1, 200, "@1")

        a_started = asyncio.Event()
        a_release = asyncio.Event()
        a_done = asyncio.Event()
        b_status_sent = asyncio.Event()

        async def slow_content(bot, user_id, task):
            a_started.set()
            await a_release.wait()
            a_done.set()

        async def fast_status(bot, user_id, task):
            b_status_sent.set()

        with (
            patch.object(
                message_queue,
                "_process_content_task",
                side_effect=slow_content,
            ),
            patch.object(
                message_queue,
                "_process_status_update_task",
                side_effect=fast_status,
            ),
        ):
            queue_a = message_queue._get_or_create_route(mock_bot, route_a)
            queue_a.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["pile A"],
                    content_type="text",
                    thread_id=100,
                )
            )

            await a_started.wait()

            await message_queue.enqueue_status_update(
                mock_bot,
                user_id=1,
                window_id="@1",
                status_text="route B working",
                thread_id=200,
            )

            assert await _yield_until(b_status_sent.is_set)
            assert b_status_sent.is_set()
            assert not a_done.is_set(), (
                "route B status should NOT wait for route A's content"
            )

            a_release.set()
            await a_done.wait()
            for r in (route_a, route_b):
                worker = message_queue._route_workers.get(r)
                if worker is not None:
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass


@pytest.mark.usefixtures("_clear_queue_state")
class TestEphemeralCoalesce:
    """Ephemeral status drains after every content task; coalesces to latest."""

    @pytest.mark.asyncio
    async def test_latest_status_wins_and_drains_after_each_content(
        self, mock_bot: AsyncMock
    ):
        route = (1, 100, "@0")

        content_calls: list[message_queue.MessageTask] = []
        status_calls: list[message_queue.MessageTask] = []
        between_status: list[bool] = []

        async def slow_content(bot, user_id, task):
            content_calls.append(task)
            # Simulate latency so we can race a status enqueue mid-flight.
            await asyncio.sleep(0)

        async def status_proc(bot, user_id, task):
            status_calls.append(task)
            between_status.append(len(content_calls) > 0)

        with (
            patch.object(
                message_queue,
                "_process_content_task",
                side_effect=slow_content,
            ),
            patch.object(
                message_queue,
                "_process_status_update_task",
                side_effect=status_proc,
            ),
        ):
            queue = message_queue._get_or_create_route(mock_bot, route)

            async def feeder():
                # Drip-feed content + status interleaved; if all content
                # arrives before the worker starts, the merge collapses
                # them and we lose the multi-tick observation we need.
                for i in range(5):
                    queue.put_nowait(
                        message_queue.MessageTask(
                            task_type="content",
                            window_id="@0",
                            parts=[f"part {i}"],
                            content_type="text",
                            thread_id=100,
                        )
                    )
                    await message_queue.enqueue_status_update(
                        mock_bot,
                        user_id=1,
                        window_id="@0",
                        status_text=f"v{i}",
                        thread_id=100,
                    )
                    # Yield so the worker can drain this tick before more
                    # content arrives (otherwise merge collapses everything).
                    for _ in range(3):
                        await asyncio.sleep(0)

            await feeder()
            await queue.join()
            # Final status should land after the last content tick.
            await _yield_until(lambda: status_calls and status_calls[-1].text == "v4")

            worker = message_queue._route_workers[route]
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        # Drain runs after every content tick — coalesced ephemerals only
        # show the latest "vN" between ticks, never older values.
        assert content_calls, "expected content tasks to run"
        assert status_calls, "status drain should run after content tick"
        for sent in status_calls:
            assert sent.text in {f"v{i}" for i in range(5)}
        # Older slots must not resurrect: once "v4" has been sent, no
        # subsequent send goes back to v0..v3 (latest-wins coalesce).
        last_v4 = max(i for i, t in enumerate(status_calls) if t.text == "v4")
        for t in status_calls[last_v4 + 1 :]:
            assert t.text == "v4", "stale ephemeral resurrected after v4"


@pytest.mark.usefixtures("_clear_queue_state")
class TestEphemeralKick:
    """An idle worker wakes on kick rather than sitting on an empty queue."""

    @pytest.mark.asyncio
    async def test_status_into_idle_route_lands_quickly(self, mock_bot: AsyncMock):
        route = (1, 100, "@0")
        sent = asyncio.Event()

        async def status_proc(bot, user_id, task):
            sent.set()

        with patch.object(
            message_queue,
            "_process_status_update_task",
            side_effect=status_proc,
        ):
            # Spawn worker by registering the route, queue stays empty.
            message_queue._get_or_create_route(mock_bot, route)
            # Let the worker park on its idle wait.
            await asyncio.sleep(0)

            await message_queue.enqueue_status_update(
                mock_bot,
                user_id=1,
                window_id="@0",
                status_text="just a ping",
                thread_id=100,
            )

            try:
                await asyncio.wait_for(sent.wait(), timeout=0.5)
            finally:
                worker = message_queue._route_workers[route]
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass

        assert sent.is_set(), "ephemeral kick failed to wake idle worker"


@pytest.mark.usefixtures("_clear_queue_state")
class TestTeardownDrainThenCancel:
    """teardown_route waits for in-flight dispatch before cancelling."""

    @pytest.mark.asyncio
    async def test_inflight_tool_use_finishes_and_records_msg_id(
        self, mock_bot: AsyncMock
    ):
        route = (1, 100, "@0")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_content(bot, user_id, task):
            started.set()
            await release.wait()
            # Simulate the tool_use side-effect of recording _tool_msg_ids.
            if task.tool_use_id and task.content_type == "tool_use":
                tid = task.thread_id or 0
                message_queue._tool_msg_ids[(task.tool_use_id, user_id, tid)] = 7777

        with patch.object(
            message_queue,
            "_process_activity_task",
            side_effect=slow_content,
        ):
            queue = message_queue._get_or_create_route(mock_bot, route)
            queue.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["running tool"],
                    content_type="tool_use",
                    tool_use_id="tu-1",
                    thread_id=100,
                )
            )
            # Pile a queued task that should be dropped on drop_pending=True.
            queue.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["queued (should drop)"],
                    content_type="text",
                    thread_id=100,
                )
            )

            await started.wait()
            teardown = asyncio.create_task(
                message_queue.teardown_route(route, drop_pending=True)
            )
            # Give teardown a chance to start; it must NOT proceed past the
            # inflight wait until we release the in-flight task.
            await asyncio.sleep(0)
            assert not teardown.done(), "teardown_route hard-cancelled mid-await"

            release.set()
            await asyncio.wait_for(teardown, timeout=1.0)

        # The in-flight tool_use ran to completion and recorded its message id.
        assert message_queue._tool_msg_ids.get(("tu-1", 1, 100)) == 7777
        assert route not in message_queue._route_workers
        assert route not in message_queue._route_queues


@pytest.mark.usefixtures("_clear_queue_state")
class TestTeardownHardCancelRejected:
    """teardown_route does NOT cancel a task that is awaiting topic_send."""

    @pytest.mark.asyncio
    async def test_dispatch_runs_to_completion(self, mock_bot: AsyncMock):
        route = (1, 100, "@0")
        started = asyncio.Event()
        release = asyncio.Event()
        completed = asyncio.Event()

        async def slow_content(bot, user_id, task):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A failure here means teardown hard-cancelled mid-send.
                raise
            completed.set()

        with patch.object(
            message_queue,
            "_process_content_task",
            side_effect=slow_content,
        ):
            queue = message_queue._get_or_create_route(mock_bot, route)
            queue.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["mid send"],
                    content_type="text",
                    thread_id=100,
                )
            )
            await started.wait()
            teardown = asyncio.create_task(
                message_queue.teardown_route(route, drop_pending=True)
            )
            await asyncio.sleep(0)
            assert not teardown.done()
            release.set()
            await asyncio.wait_for(teardown, timeout=1.0)

        assert completed.is_set(), "in-flight dispatch was hard-cancelled by teardown"


@pytest.mark.usefixtures("_clear_queue_state")
class TestRebindToolResultAnchoring:
    """tool_msg_ids survive a route rebind (key is (id, user, thread))."""

    @pytest.mark.asyncio
    async def test_tool_result_after_rebind_finds_prior_message_id(
        self, mock_bot: AsyncMock
    ):
        route_old = (1, 100, "@3")
        route_new = (1, 100, "@7")

        async def record_tool_use(bot, user_id, task):
            if task.content_type == "tool_use" and task.tool_use_id:
                tid = task.thread_id or 0
                message_queue._tool_msg_ids[(task.tool_use_id, user_id, tid)] = 4242

        with patch.object(
            message_queue,
            "_process_activity_task",
            side_effect=record_tool_use,
        ):
            queue_old = message_queue._get_or_create_route(mock_bot, route_old)
            queue_old.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@3",
                    parts=["calling tool"],
                    content_type="tool_use",
                    tool_use_id="tu-9",
                    thread_id=100,
                )
            )
            await queue_old.join()

            # Rebind: old route drains naturally then is torn down.
            await message_queue.teardown_route(route_old, drop_pending=False)

            # _tool_msg_ids is keyed by (tool_use_id, user, thread) — the
            # prior message id must survive even though the window changed.
            assert message_queue._tool_msg_ids.get(("tu-9", 1, 100)) == 4242

            # Issue a new tool_result on the rebound route — anchoring works
            # because the lookup is still keyed by (id, user, thread).
            queue_new = message_queue._get_or_create_route(mock_bot, route_new)
            anchored: list[int] = []

            async def edit_tool_result(bot, user_id, task):
                tid = task.thread_id or 0
                msg_id = message_queue._tool_msg_ids.get(
                    (task.tool_use_id, user_id, tid)
                )
                if msg_id is not None:
                    anchored.append(msg_id)

            with patch.object(
                message_queue,
                "_process_activity_task",
                side_effect=edit_tool_result,
            ):
                queue_new.put_nowait(
                    message_queue.MessageTask(
                        task_type="content",
                        window_id="@7",
                        parts=["tool result"],
                        content_type="tool_result",
                        tool_use_id="tu-9",
                        thread_id=100,
                    )
                )
                await queue_new.join()
                worker = message_queue._route_workers[route_new]
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass

        assert anchored == [4242], (
            "tool_result on rebound route did not find prior tool_use message"
        )


@pytest.mark.usefixtures("_clear_queue_state")
class TestTeardownRaceGate:
    """Bug 1 regression: enqueue during teardown must NOT resurrect the route.

    Without ``_route_tearing_down`` gating, a fresh ``enqueue_content_message``
    that lands between ``inflight.wait()`` returning and ``worker.cancel()``
    re-creates the queue/worker via ``_get_or_create_route``; the worker
    then races with cancellation mid-``await topic_send`` and leaks
    ``_tool_msg_ids`` slots.
    """

    @pytest.mark.asyncio
    async def test_enqueue_during_teardown_is_dropped(self, mock_bot: AsyncMock):
        route = (1, 100, "@0")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_content(bot, user_id, task):
            started.set()
            await release.wait()

        with patch.object(
            message_queue,
            "_process_content_task",
            side_effect=slow_content,
        ):
            queue = message_queue._get_or_create_route(mock_bot, route)
            queue.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["in flight"],
                    content_type="text",
                    thread_id=100,
                )
            )
            await started.wait()

            # Start teardown — it parks on inflight.wait() because the
            # in-flight task is still running.
            teardown = asyncio.create_task(
                message_queue.teardown_route(route, drop_pending=True)
            )
            # Yield once so teardown starts and adds the route to
            # _route_tearing_down.
            await asyncio.sleep(0)

            assert route in message_queue._route_tearing_down

            # Race: an enqueue lands while teardown is parked. Without the
            # gate this would re-spawn the worker and queue a task the
            # cancel will then race against. With the gate it's dropped.
            await message_queue.enqueue_content_message(
                mock_bot,
                user_id=1,
                window_id="@0",
                parts=["raced in"],
                content_type="text",
                thread_id=100,
            )

            # Also try a status update during teardown — it must drop too.
            await message_queue.enqueue_status_update(
                mock_bot,
                user_id=1,
                window_id="@0",
                status_text="should be dropped",
                thread_id=100,
            )

            # Let the in-flight task finish so teardown can complete.
            release.set()
            await asyncio.wait_for(teardown, timeout=1.0)

        # Route fully removed; the racing enqueues did NOT resurrect it.
        assert route not in message_queue._route_queues
        assert route not in message_queue._route_workers
        assert route not in message_queue._route_tearing_down
        assert route not in message_queue._route_pending_ephemeral


@pytest.mark.usefixtures("_clear_queue_state")
class TestKickClearRace:
    """Bug 2 regression: kick.clear() must happen under the lock.

    If the worker drains the slot, releases the lock, then clears the
    kick OUTSIDE the lock, a status enqueue that lands between drain and
    clear will set the slot AND set the kick, then the worker clears the
    kick anyway and parks indefinitely on an empty kick. Fix: clear the
    kick under the same lock that snapshots the slot.
    """

    @pytest.mark.asyncio
    async def test_status_lands_after_drain(self, mock_bot: AsyncMock):
        # Stress this by interleaving content + status many times. With
        # the kick clear OUTSIDE the lock, occasional iterations would
        # park the second status until the next content arrives. With
        # the fix, every status eventually lands.
        route = (1, 100, "@0")
        content_calls = 0
        status_calls: list[str] = []

        async def fast_content(bot, user_id, task):
            nonlocal content_calls
            content_calls += 1
            await asyncio.sleep(0)

        async def status_proc(bot, user_id, task):
            status_calls.append(task.text or "")

        with (
            patch.object(
                message_queue,
                "_process_content_task",
                side_effect=fast_content,
            ),
            patch.object(
                message_queue,
                "_process_status_update_task",
                side_effect=status_proc,
            ),
        ):
            queue = message_queue._get_or_create_route(mock_bot, route)
            for i in range(20):
                queue.put_nowait(
                    message_queue.MessageTask(
                        task_type="content",
                        window_id="@0",
                        parts=[f"c{i}"],
                        content_type="text",
                        thread_id=100,
                    )
                )
                await message_queue.enqueue_status_update(
                    mock_bot,
                    user_id=1,
                    window_id="@0",
                    status_text=f"s{i}",
                    thread_id=100,
                )
                await asyncio.sleep(0)

            await queue.join()
            # Final ping — without the under-lock clear+set fix, this can
            # park behind a stale clear() from the worker.
            await message_queue.enqueue_status_update(
                mock_bot,
                user_id=1,
                window_id="@0",
                status_text="final-ping",
                thread_id=100,
            )

            # The final ping must eventually arrive.
            ok = await _yield_until(lambda: "final-ping" in status_calls, ticks=400)
            worker = message_queue._route_workers[route]
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        assert ok, "final status was never drained — kick race regressed"

    @pytest.mark.asyncio
    async def test_kick_never_cleared_with_pending_slot(self, mock_bot: AsyncMock):
        """Runtime invariant: when the slot is None after lock-protected
        snapshot, the kick is allowed to be cleared. We can't easily
        assert "during" the brief window between unlock and the worker
        loop's re-entry, but we can assert post-condition: after a status
        is enqueued, either the kick is set OR the slot is empty (the
        worker has consumed it). The two states "slot present AND kick
        cleared" should never persist.
        """
        route = (1, 100, "@0")
        sent: list[str] = []

        async def status_proc(bot, user_id, task):
            sent.append(task.text or "")

        with patch.object(
            message_queue,
            "_process_status_update_task",
            side_effect=status_proc,
        ):
            message_queue._get_or_create_route(mock_bot, route)
            await asyncio.sleep(0)
            await message_queue.enqueue_status_update(
                mock_bot,
                user_id=1,
                window_id="@0",
                status_text="ping",
                thread_id=100,
            )
            # Yield enough that the worker drains.
            await _yield_until(lambda: bool(sent), ticks=200)

            # Post-condition: slot is None AND kick is cleared (drained
            # state). The forbidden state is "slot present AND kick
            # cleared," which would park the worker.
            slot = message_queue._route_pending_ephemeral.get(route)
            kick = message_queue._route_ephemeral_kick.get(route)
            assert slot is None or (kick is not None and kick.is_set()), (
                "invariant violated: pending slot present but kick cleared"
            )

            worker = message_queue._route_workers[route]
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass


@pytest.mark.usefixtures("_clear_queue_state")
class TestToolResultEditPath:
    """Test gap E: tool_result actually drives an edit, not a fresh send."""

    @pytest.mark.asyncio
    async def test_tool_result_edits_recorded_message_id(self, mock_bot: AsyncMock):
        """Drive a real tool_use → record id → tool_result → edit path.

        Mocks ``topic_send``/``topic_edit`` at the boundary so we observe
        whether ``_process_content_task`` chooses ``edit_message_text``
        (with the recorded message_id) over ``send_message``.
        """
        sent_msg = MagicMock()
        sent_msg.message_id = 31337

        send_calls: list[dict] = []
        edit_calls: list[dict] = []

        from ccbot.handlers.message_sender import TopicSendOutcome

        async def fake_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            send_calls.append(
                {
                    "op": op,
                    "msg_text": text,
                    "thread_id": thread_id,
                }
            )
            return sent_msg, TopicSendOutcome.OK

        async def fake_edit(
            bot, *, op, user_id, chat_id, thread_id, window_id, message_id, text, **kw
        ):
            edit_calls.append(
                {
                    "op": op,
                    "message_id": message_id,
                    "msg_text": text,
                }
            )
            return TopicSendOutcome.OK

        async def noop_check_status(*a, **k):
            return None

        async def noop_finalize(*a, **k):
            return None

        async def noop_attention(*a, **k):
            return None

        with (
            patch.object(message_queue, "topic_send", side_effect=fake_send),
            patch.object(message_queue, "topic_edit", side_effect=fake_edit),
            patch.object(
                message_queue,
                "_check_and_send_status",
                side_effect=noop_check_status,
            ),
            patch.object(
                message_queue,
                "_finalize_activity_digest",
                side_effect=noop_finalize,
            ),
            patch.object(
                message_queue,
                "_maybe_attention_or_dismiss",
                side_effect=noop_attention,
            ),
            patch.object(
                message_queue,
                "_convert_status_to_content",
                AsyncMock(return_value=None),
            ),
            patch.object(
                message_queue.session_manager,
                "resolve_chat_id",
                return_value=1,
            ),
        ):
            # First: tool_use — should send and record message_id.
            await message_queue._process_content_task(
                mock_bot,
                1,
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["⚙️ Bash"],
                    content_type="tool_use",
                    tool_use_id="tu-edit-1",
                    thread_id=100,
                ),
            )
            assert len(send_calls) == 1
            assert send_calls[0]["op"] == "content"
            assert message_queue._tool_msg_ids[("tu-edit-1", 1, 100)] == 31337

            # Now: tool_result for the same id — should edit (NOT send).
            await message_queue._process_content_task(
                mock_bot,
                1,
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["✅ done"],
                    content_type="tool_result",
                    tool_use_id="tu-edit-1",
                    thread_id=100,
                ),
            )

        assert len(edit_calls) == 1, "tool_result did not edit in place"
        assert edit_calls[0]["op"] == "tool_result"
        assert edit_calls[0]["message_id"] == 31337
        # send was NOT called a second time for the tool_result.
        assert len(send_calls) == 1, (
            "tool_result fell through to send instead of editing"
        )
        # The id is consumed by the edit.
        assert ("tu-edit-1", 1, 100) not in message_queue._tool_msg_ids


@pytest.mark.usefixtures("_clear_queue_state")
class TestWireOrderRegression:
    """Test gap F: locks in the per-route drain-after-content-tick contract.

    Enqueue [content_1, status_S1, content_2, status_S2] and assert the
    actual wire-side order is content_1, status, content_2, status. Status
    coalesces (latest-wins) per drain so we don't assert which of S1/S2
    lands; we DO assert the relative ordering is content-then-status.
    """

    @pytest.mark.asyncio
    async def test_per_route_drain_after_each_content_tick(self, mock_bot: AsyncMock):
        route = (1, 100, "@0")

        wire: list[str] = []

        async def content_proc(bot, user_id, task):
            wire.append(f"content:{task.parts[0]}")
            # Simulate latency so the next content enqueue can land in
            # the queue while status drains.
            await asyncio.sleep(0)

        async def status_proc(bot, user_id, task):
            wire.append(f"status:{task.text}")

        with (
            patch.object(
                message_queue,
                "_process_content_task",
                side_effect=content_proc,
            ),
            patch.object(
                message_queue,
                "_process_status_update_task",
                side_effect=status_proc,
            ),
        ):
            queue = message_queue._get_or_create_route(mock_bot, route)

            # Enqueue content_1 + status_S1, then yield so the worker
            # processes one tick before content_2 lands (otherwise the
            # merge collapses content_1 and content_2 together).
            queue.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["c1"],
                    content_type="text",
                    thread_id=100,
                )
            )
            await message_queue.enqueue_status_update(
                mock_bot,
                user_id=1,
                window_id="@0",
                status_text="S1",
                thread_id=100,
            )
            for _ in range(10):
                await asyncio.sleep(0)

            queue.put_nowait(
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["c2"],
                    content_type="text",
                    thread_id=100,
                )
            )
            await message_queue.enqueue_status_update(
                mock_bot,
                user_id=1,
                window_id="@0",
                status_text="S2",
                thread_id=100,
            )

            await queue.join()
            # Yield until the second drain has had a chance to run.
            await _yield_until(
                lambda: (
                    wire.count("content:c2") > 0
                    and any(
                        s.startswith("status:")
                        for s in wire[wire.index("content:c2") :]
                    )
                )
            )

            worker = message_queue._route_workers[route]
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

        # Find positions and assert the per-route invariant holds.
        c1_idx = wire.index("content:c1")
        c2_idx = wire.index("content:c2")
        assert c1_idx < c2_idx

        # Between c1 and c2 there must be at least one status drain.
        between = wire[c1_idx + 1 : c2_idx]
        assert any(s.startswith("status:") for s in between), (
            f"no status drained between content_1 and content_2; wire={wire}"
        )
        # After c2 there must be at least one status drain.
        after = wire[c2_idx + 1 :]
        assert any(s.startswith("status:") for s in after), (
            f"no status drained after content_2; wire={wire}"
        )


@pytest.mark.usefixtures("_clear_queue_state")
class TestActivityDigestHeader:
    """Render the digest header from RunState + context_pct under V2 flag."""

    def _state(self, *, done: bool = False) -> message_queue.ActivityDigestState:
        s = message_queue.ActivityDigestState(message_id=0, window_id="@7")
        s.tool_count = 1
        s.completed_count = 1
        s.done = done
        return s

    def _enable_v2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ccbot.config import config as cfg

        monkeypatch.setattr(cfg, "busy_indicator_v2", True)
        monkeypatch.setattr(cfg, "context_pct_threshold", 80)

    def test_legacy_path_unchanged_when_flag_off(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.config import config as cfg
        from ccbot.handlers import busy_indicator

        monkeypatch.setattr(cfg, "busy_indicator_v2", False)
        busy_indicator.reset_for_tests()

        s = self._state(done=True)
        rendered = message_queue._render_activity_digest(s, route=(1, 42, "@7"))
        # Legacy path: state.done → "✅ Done"; no ctx suffix even at high pct.
        assert rendered.startswith("✅ Done — ")
        assert "ctx" not in rendered

    def test_v2_running_state_renders_busy(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.handlers import busy_indicator

        self._enable_v2(monkeypatch)
        busy_indicator.reset_for_tests()
        route = (1, 42, "@7")
        busy_indicator._run_state[route] = busy_indicator.RunState.RUNNING_TOOL

        rendered = message_queue._render_activity_digest(self._state(), route=route)
        assert rendered.startswith("🟡 Busy — ")

    def test_v2_idle_cleared_renders_done(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.handlers import busy_indicator

        self._enable_v2(monkeypatch)
        busy_indicator.reset_for_tests()
        route = (1, 42, "@7")
        busy_indicator._run_state[route] = busy_indicator.RunState.IDLE_CLEARED

        rendered = message_queue._render_activity_digest(self._state(), route=route)
        assert rendered.startswith("✅ Done — ")

    def test_v2_idle_recent_renders_done(self, monkeypatch: pytest.MonkeyPatch):
        """IDLE_RECENT must render as Done, not Busy.

        Regression: the digest is finalized exactly once when the assistant's
        final text lands; nothing re-renders it on the IDLE_RECENT →
        IDLE_CLEARED decay 4s later. Mapping IDLE_RECENT → "🟡 Busy" produced
        a stuck "Busy" header that never flipped to Done in production. The
        decay grace window matters for typing-action / Busy-card lifecycles
        (status_polling reads state() each tick), not for this header.
        """
        from ccbot.handlers import busy_indicator

        self._enable_v2(monkeypatch)
        busy_indicator.reset_for_tests()
        route = (1, 42, "@7")
        busy_indicator._run_state[route] = busy_indicator.RunState.IDLE_RECENT

        rendered = message_queue._render_activity_digest(self._state(), route=route)
        assert rendered.startswith("✅ Done — ")

    def test_v2_waiting_on_user(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.handlers import busy_indicator

        self._enable_v2(monkeypatch)
        busy_indicator.reset_for_tests()
        route = (1, 42, "@7")
        busy_indicator._run_state[route] = busy_indicator.RunState.WAITING_ON_USER

        rendered = message_queue._render_activity_digest(self._state(), route=route)
        assert rendered.startswith("🔔 Waiting on you — ")

    def test_v2_broken_topic(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.handlers import busy_indicator

        self._enable_v2(monkeypatch)
        busy_indicator.reset_for_tests()
        route = (1, 42, "@7")
        busy_indicator._run_state[route] = busy_indicator.RunState.BROKEN_TOPIC

        rendered = message_queue._render_activity_digest(self._state(), route=route)
        assert rendered.startswith("⚠️ Topic unreachable — ")

    def test_v2_ctx_below_threshold_no_suffix(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.handlers import busy_indicator

        self._enable_v2(monkeypatch)
        busy_indicator.reset_for_tests()
        route = (1, 42, "@7")
        busy_indicator.update_context_pct(route, 50)

        rendered = message_queue._render_activity_digest(self._state(), route=route)
        assert "ctx" not in rendered

    def test_v2_ctx_at_threshold_neutral_suffix(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.handlers import busy_indicator

        self._enable_v2(monkeypatch)
        busy_indicator.reset_for_tests()
        route = (1, 42, "@7")
        busy_indicator.update_context_pct(route, 89)

        rendered = message_queue._render_activity_digest(self._state(), route=route)
        first_line = rendered.split("\n", 1)[0]
        assert first_line.endswith("· ctx 89%")
        assert "⚠️" not in first_line

    def test_v2_ctx_critical_warning(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.handlers import busy_indicator

        self._enable_v2(monkeypatch)
        busy_indicator.reset_for_tests()
        route = (1, 42, "@7")
        busy_indicator.update_context_pct(route, 97)

        rendered = message_queue._render_activity_digest(self._state(), route=route)
        first_line = rendered.split("\n", 1)[0]
        assert first_line.endswith("· ⚠️ ctx 97%")

    # --- V2-OFF digest parity (Test D) ----------------------------------
    # Exhaustive matrix: with the flag OFF, the header reproduces the
    # pre-Stage-3 behavior byte-for-byte across done × waiting × route.

    def test_v2_off_busy_when_not_done_not_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from ccbot.config import config as cfg
        from ccbot.handlers import busy_indicator

        monkeypatch.setattr(cfg, "busy_indicator_v2", False)
        busy_indicator.reset_for_tests()

        rendered = message_queue._render_activity_digest(
            self._state(done=False),
            waiting=False,
            route=(1, 42, "@7"),
        )
        assert rendered.startswith("🟡 Busy — ")
        assert "ctx" not in rendered

    def test_v2_off_done_when_done_not_waiting(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.config import config as cfg
        from ccbot.handlers import busy_indicator

        monkeypatch.setattr(cfg, "busy_indicator_v2", False)
        busy_indicator.reset_for_tests()

        rendered = message_queue._render_activity_digest(
            self._state(done=True),
            waiting=False,
            route=(1, 42, "@7"),
        )
        assert rendered.startswith("✅ Done — ")
        assert "ctx" not in rendered

    def test_v2_off_waiting_overrides_done(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.config import config as cfg
        from ccbot.handlers import busy_indicator

        monkeypatch.setattr(cfg, "busy_indicator_v2", False)
        busy_indicator.reset_for_tests()

        # waiting=True takes precedence even with done=True.
        rendered = message_queue._render_activity_digest(
            self._state(done=True),
            waiting=True,
            route=(1, 42, "@7"),
        )
        assert rendered.startswith("🔔 Waiting on you — ")
        assert "ctx" not in rendered

    def test_v2_off_ignores_run_state_and_ctx(self, monkeypatch: pytest.MonkeyPatch):
        from ccbot.config import config as cfg
        from ccbot.handlers import busy_indicator

        monkeypatch.setattr(cfg, "busy_indicator_v2", False)
        busy_indicator.reset_for_tests()
        route = (1, 42, "@7")
        # Even with high context_pct + a non-default RunState, the legacy
        # path renders the legacy header and never adds the ctx suffix.
        busy_indicator.update_context_pct(route, 99)
        busy_indicator._run_state[route] = busy_indicator.RunState.WAITING_ON_USER

        rendered = message_queue._render_activity_digest(
            self._state(done=False),
            waiting=False,
            route=route,
        )
        assert rendered.startswith("🟡 Busy — ")
        assert "ctx" not in rendered


@pytest.mark.usefixtures("_clear_queue_state")
class TestAgentToolProminence:
    """§2.7: Agent / Task subagent invocations are promoted out of the digest."""

    def _agent_use(
        self,
        *,
        tool_use_id: str = "agent-1",
        tool_name: str = "Agent",
        description: str = "Investigate flaky test",
        subagent_type: str = "code-investigator",
        prompt: str = "Look at the failing test and report root cause.",
    ) -> message_queue.MessageTask:
        return message_queue.MessageTask(
            task_type="content",
            window_id="@9",
            parts=[f"**{tool_name}**({description})"],
            content_type="tool_use",
            tool_use_id=tool_use_id,
            thread_id=200,
            tool_name=tool_name,
            tool_input={
                "description": description,
                "subagent_type": subagent_type,
                "prompt": prompt,
            },
        )

    def _agent_result(
        self,
        *,
        tool_use_id: str = "agent-1",
        text: str = "Subagent finished. Root cause: flaky network mock.",
    ) -> message_queue.MessageTask:
        # NOTE: ``tool_input`` is intentionally omitted — production
        # tool_result blocks don't carry the original tool_use input. The
        # description / subagent_type must surface from
        # ``_agent_tool_ids`` (recorded at tool_use dispatch time), not
        # from the tool_result task itself.
        return message_queue.MessageTask(
            task_type="content",
            window_id="@9",
            parts=[text],
            content_type="tool_result",
            tool_use_id=tool_use_id,
            thread_id=200,
            text=text,
        )

    @pytest.fixture
    def _agent_test_patches(self):
        from ccbot.handlers.message_sender import TopicSendOutcome

        sent_msg = MagicMock()
        sent_msg.message_id = 7777

        send_calls: list[dict] = []
        edit_calls: list[dict] = []
        upsert_calls: list[message_queue.ActivityDigestState] = []

        async def fake_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            send_calls.append(
                {
                    "op": op,
                    "text": text,
                    "thread_id": thread_id,
                    "kw": kw,
                }
            )
            return sent_msg, TopicSendOutcome.OK

        async def fake_edit(
            bot, *, op, user_id, chat_id, thread_id, window_id, message_id, text, **kw
        ):
            edit_calls.append(
                {
                    "op": op,
                    "message_id": message_id,
                    "text": text,
                }
            )
            return TopicSendOutcome.OK

        async def fake_upsert(bot, user_id, thread_id, state):
            # Snapshot counters so the test can assert post-bump values
            # even if the same state object mutates again later.
            snap = message_queue.ActivityDigestState(
                message_id=state.message_id,
                window_id=state.window_id,
                tool_count=state.tool_count,
                completed_count=state.completed_count,
                done=state.done,
            )
            snap.lines = list(state.lines)
            upsert_calls.append(snap)

        async def noop(*a, **k):
            return None

        with (
            patch.object(message_queue, "topic_send", side_effect=fake_send),
            patch.object(message_queue, "topic_edit", side_effect=fake_edit),
            patch.object(
                message_queue,
                "_upsert_activity_digest",
                side_effect=fake_upsert,
            ),
            patch.object(
                message_queue,
                "_check_and_send_status",
                side_effect=noop,
            ),
            patch.object(
                message_queue,
                "_finalize_activity_digest",
                side_effect=noop,
            ),
            patch.object(
                message_queue,
                "_maybe_attention_or_dismiss",
                side_effect=noop,
            ),
            patch.object(
                message_queue,
                "_convert_status_to_content",
                AsyncMock(return_value=None),
            ),
            patch.object(
                message_queue.session_manager,
                "resolve_chat_id",
                return_value=1,
            ),
        ):
            yield {
                "send_calls": send_calls,
                "edit_calls": edit_calls,
                "upsert_calls": upsert_calls,
                "sent_msg": sent_msg,
            }

    @pytest.mark.asyncio
    async def test_agent_tool_use_promoted_to_top_level(
        self, mock_bot: AsyncMock, _agent_test_patches
    ):
        await message_queue._process_agent_task(mock_bot, 1, self._agent_use())
        send_calls = _agent_test_patches["send_calls"]
        assert len(send_calls) == 1
        body = send_calls[0]["text"]
        assert body.startswith("🤖 Subagent dispatched — code-investigator")
        assert "Description: Investigate flaky test" in body
        assert "▶ Look at the failing test" in body
        # Top-level tool_use sends are silent — the user shouldn't get a
        # ping every time a subagent dispatches alongside other tools.
        assert send_calls[0]["kw"].get("disable_notification") is True

    @pytest.mark.asyncio
    async def test_agent_tool_result_edits_top_level_message(
        self, mock_bot: AsyncMock, _agent_test_patches
    ):
        await message_queue._process_agent_task(mock_bot, 1, self._agent_use())
        await message_queue._process_agent_task(mock_bot, 1, self._agent_result())
        edit_calls = _agent_test_patches["edit_calls"]
        assert len(edit_calls) == 1
        assert edit_calls[0]["op"] == "tool_result"
        assert edit_calls[0]["message_id"] == 7777
        edited_body = edit_calls[0]["text"]
        assert edited_body.startswith("🤖✅ Subagent done — code-investigator")
        assert "flaky network mock" in edited_body

    @pytest.mark.asyncio
    async def test_agent_tool_counter_still_tracks(
        self, mock_bot: AsyncMock, _agent_test_patches
    ):
        await message_queue._process_agent_task(mock_bot, 1, self._agent_use())
        await message_queue._process_agent_task(mock_bot, 1, self._agent_result())
        upserts = _agent_test_patches["upsert_calls"]
        assert upserts, "activity digest was never upserted"
        last = upserts[-1]
        assert last.tool_count == 1
        assert last.completed_count == 1
        # Body of the digest must NOT carry an Agent line — the top-level
        # message owns that surface; the digest only counts.
        assert all("Agent" not in ln and "Subagent" not in ln for ln in last.lines)

    @pytest.mark.asyncio
    async def test_legacy_task_name_treated_as_agent(
        self, mock_bot: AsyncMock, _agent_test_patches
    ):
        task = self._agent_use(tool_use_id="task-1", tool_name="Task")
        await message_queue._process_agent_task(mock_bot, 1, task)
        send_calls = _agent_test_patches["send_calls"]
        assert send_calls, "legacy Task did not get the top-level promotion"
        assert send_calls[0]["text"].startswith("🤖 Subagent dispatched — ")

    @pytest.mark.asyncio
    async def test_non_agent_tool_use_still_collapses(
        self, mock_bot: AsyncMock, _agent_test_patches
    ):
        # A Read tool_use should NOT take the agent path — it goes through
        # the activity digest as a single "⚙️ Read foo.py" line. We assert
        # by routing the dispatcher and confirming no top-level send fires.
        read_task = message_queue.MessageTask(
            task_type="content",
            window_id="@9",
            parts=["**Read**(foo.py)"],
            content_type="tool_use",
            tool_use_id="read-1",
            thread_id=200,
            tool_name="Read",
            tool_input={"file_path": "foo.py"},
        )
        # Drive the predicate directly (cheap, doesn't need a real worker).
        assert message_queue._is_agent_tool_use(read_task) is False
        # And confirm Agent prediction holds for the Agent task too.
        assert message_queue._is_agent_tool_use(self._agent_use()) is True

    @pytest.mark.asyncio
    async def test_agent_disable_notification_setting(
        self, mock_bot: AsyncMock, _agent_test_patches
    ):
        await message_queue._process_agent_task(mock_bot, 1, self._agent_use())
        send_calls = _agent_test_patches["send_calls"]
        assert send_calls[0]["kw"].get("disable_notification") is True

    @pytest.mark.asyncio
    async def test_non_agent_tool_use_routes_to_activity(
        self, mock_bot: AsyncMock, _agent_test_patches
    ):
        """Dispatcher splits Agent vs. activity-digest tools by predicate.

        A Read tool_use must land in ``_process_activity_task`` and NEVER
        in the Agent / content paths — the digest owns short-tool surfaces.
        """
        read_task = message_queue.MessageTask(
            task_type="content",
            window_id="@9",
            parts=["**Read**(foo.py)"],
            content_type="tool_use",
            tool_use_id="read-1",
            thread_id=200,
            tool_name="Read",
            tool_input={"file_path": "foo.py"},
        )
        queue: asyncio.Queue[message_queue.MessageTask] = asyncio.Queue()
        lock = asyncio.Lock()
        with (
            patch.object(
                message_queue, "_process_activity_task", new_callable=AsyncMock
            ) as mock_activity,
            patch.object(
                message_queue, "_process_agent_task", new_callable=AsyncMock
            ) as mock_agent,
            patch.object(
                message_queue, "_process_content_task", new_callable=AsyncMock
            ) as mock_content,
        ):
            await message_queue._dispatch_task(mock_bot, 1, queue, lock, read_task)
        mock_activity.assert_awaited_once()
        mock_agent.assert_not_awaited()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_tool_result_render_uses_recorded_input(
        self, mock_bot: AsyncMock, _agent_test_patches
    ):
        """End-to-end: tool_result rendering pulls description / subagent_type
        from ``_agent_tool_ids`` (stashed at tool_use dispatch), not from a
        tool_input field on the tool_result task itself.

        Locks the Bug 1 fix: the production parser path emits tool_result
        ``MessageTask``s without ``tool_input``, so this test deliberately
        uses ``_agent_result()`` (which omits it).
        """
        await message_queue._process_agent_task(mock_bot, 1, self._agent_use())
        # Confirm the dispatch stashed the input dict.
        recorded = message_queue._agent_tool_ids.get(("agent-1", 1, 200))
        assert recorded is not None
        assert recorded.get("subagent_type") == "code-investigator"

        await message_queue._process_agent_task(mock_bot, 1, self._agent_result())
        edit_calls = _agent_test_patches["edit_calls"]
        assert len(edit_calls) == 1
        body = edit_calls[0]["text"]
        # Both header and description must come from the recorded input,
        # NOT from the tool_result task (which has no tool_input).
        assert body.startswith("🤖✅ Subagent done — code-investigator")
        assert "Description: Investigate flaky test" in body
        assert "flaky network mock" in body
        # The agent_tool_ids entry should be popped after a successful
        # tool_result render so the lifetime stays bounded.
        assert ("agent-1", 1, 200) not in message_queue._agent_tool_ids


@pytest.mark.usefixtures("_clear_queue_state")
class TestReplyParametersAnchor:
    """§2.5.2 outbound anchor: first part of assistant text replies-to user msg.

    The first ``topic_send`` for a ``content_type='text'`` task picks up
    ``_route_last_user_message[route]`` and passes
    ``reply_parameters=ReplyParameters(message_id=...)``. Subsequent multipart
    parts, status sends, activity-digest sends, and tool sends MUST NOT
    anchor.
    """

    @pytest.fixture
    def _capture_send_kwargs(self):
        from ccbot.handlers.message_sender import TopicSendOutcome

        sent_msg = MagicMock()
        sent_msg.message_id = 5555
        send_calls: list[dict] = []

        async def fake_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            send_calls.append(
                {
                    "op": op,
                    "text": text,
                    "kw": kw,
                }
            )
            return sent_msg, TopicSendOutcome.OK

        async def fake_edit(
            bot, *, op, user_id, chat_id, thread_id, window_id, message_id, text, **kw
        ):
            return TopicSendOutcome.OK

        async def noop(*a, **k):
            return None

        with (
            patch.object(message_queue, "topic_send", side_effect=fake_send),
            patch.object(message_queue, "topic_edit", side_effect=fake_edit),
            patch.object(
                message_queue,
                "_check_and_send_status",
                side_effect=noop,
            ),
            patch.object(
                message_queue,
                "_finalize_activity_digest",
                side_effect=noop,
            ),
            patch.object(
                message_queue,
                "_maybe_attention_or_dismiss",
                side_effect=noop,
            ),
            patch.object(
                message_queue,
                "_convert_status_to_content",
                AsyncMock(return_value=None),
            ),
            patch.object(
                message_queue.session_manager,
                "resolve_chat_id",
                return_value=1,
            ),
            patch.object(
                message_queue,
                "_upsert_activity_digest",
                AsyncMock(return_value=None),
            ),
        ):
            yield send_calls

    @pytest.mark.asyncio
    async def test_first_assistant_text_part_uses_reply_parameters(
        self, mock_bot: AsyncMock, _capture_send_kwargs
    ):
        from telegram import ReplyParameters

        message_queue.set_route_last_user_message(1, 100, "@0", 12345)
        await message_queue._process_content_task(
            mock_bot,
            1,
            message_queue.MessageTask(
                task_type="content",
                window_id="@0",
                parts=["Here is my answer."],
                content_type="text",
                thread_id=100,
            ),
        )
        assert len(_capture_send_kwargs) == 1
        rp = _capture_send_kwargs[0]["kw"].get("reply_parameters")
        assert isinstance(rp, ReplyParameters)
        assert rp.message_id == 12345
        # Anchor consumed.
        assert message_queue._route_last_user_message.get((1, 100, "@0")) is None

    @pytest.mark.asyncio
    async def test_subsequent_multipart_parts_no_reply_parameters(
        self, mock_bot: AsyncMock, _capture_send_kwargs
    ):
        message_queue.set_route_last_user_message(1, 100, "@0", 12345)
        await message_queue._process_content_task(
            mock_bot,
            1,
            message_queue.MessageTask(
                task_type="content",
                window_id="@0",
                parts=["part one", "part two", "part three"],
                content_type="text",
                thread_id=100,
            ),
        )
        assert len(_capture_send_kwargs) == 3
        from telegram import ReplyParameters

        first = _capture_send_kwargs[0]["kw"].get("reply_parameters")
        assert isinstance(first, ReplyParameters)
        assert first.message_id == 12345
        for later in _capture_send_kwargs[1:]:
            assert "reply_parameters" not in later["kw"]
        # Anchor consumed exactly once for the whole multipart run — the
        # second part's "no reply_parameters" check above proves the anchor
        # didn't leak per-part; this proves it didn't leak across runs.
        assert message_queue._route_last_user_message.get((1, 100, "@0")) is None

    @pytest.mark.asyncio
    async def test_status_update_no_reply_parameters(
        self, mock_bot: AsyncMock, _capture_send_kwargs
    ):
        message_queue.set_route_last_user_message(1, 100, "@0", 12345)
        # Status sends go through _do_send_status_message → topic_send.
        # Drive that by enqueueing then directly invoking the helper.
        await message_queue._do_send_status_message(
            mock_bot, 1, 100, "@0", "Cooking for 2s"
        )
        assert len(_capture_send_kwargs) == 1
        assert "reply_parameters" not in _capture_send_kwargs[0]["kw"]
        # Anchor stays untouched (status sends never consume it).
        assert message_queue._route_last_user_message.get((1, 100, "@0")) == 12345

    @pytest.mark.asyncio
    async def test_activity_digest_no_reply_parameters(self, mock_bot: AsyncMock):
        """Activity digest is UI state, not conversation: never anchor.

        Deliberately does NOT use the ``_capture_send_kwargs`` fixture,
        which patches ``_upsert_activity_digest`` away — we want to drive
        the real upsert here so its ``topic_send`` call is observable.
        """
        from ccbot.handlers.message_sender import TopicSendOutcome

        sent_msg = MagicMock()
        sent_msg.message_id = 5555
        send_calls: list[dict] = []

        async def fake_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            send_calls.append({"op": op, "kw": kw})
            return sent_msg, TopicSendOutcome.OK

        message_queue.set_route_last_user_message(1, 100, "@0", 12345)
        with (
            patch.object(message_queue, "topic_send", side_effect=fake_send),
            patch.object(
                message_queue.session_manager, "resolve_chat_id", return_value=1
            ),
        ):
            state = message_queue.ActivityDigestState(message_id=0, window_id="@0")
            state.tool_count = 1
            state.lines = ["⚙️ Read(some_file.py)"]
            await message_queue._upsert_activity_digest(mock_bot, 1, 100, state)
        assert len(send_calls) == 1
        assert send_calls[0]["op"] == "activity"
        assert "reply_parameters" not in send_calls[0]["kw"]

    @pytest.mark.asyncio
    async def test_tool_use_no_reply_parameters(
        self, mock_bot: AsyncMock, _capture_send_kwargs
    ):
        message_queue.set_route_last_user_message(1, 100, "@0", 12345)
        await message_queue._process_content_task(
            mock_bot,
            1,
            message_queue.MessageTask(
                task_type="content",
                window_id="@0",
                parts=["⚙️ Bash(ls)"],
                content_type="tool_use",
                tool_use_id="t-1",
                thread_id=100,
            ),
        )
        assert len(_capture_send_kwargs) == 1
        assert "reply_parameters" not in _capture_send_kwargs[0]["kw"]
        # Anchor still pending — only assistant *text* consumes it.
        assert message_queue._route_last_user_message.get((1, 100, "@0")) == 12345

    @pytest.mark.asyncio
    async def test_reply_context_disabled_skips_anchor(
        self, mock_bot: AsyncMock, _capture_send_kwargs
    ):
        from ccbot.config import config as ccbot_config

        message_queue.set_route_last_user_message(1, 100, "@0", 12345)
        original = ccbot_config.reply_context_enabled
        ccbot_config.reply_context_enabled = False
        try:
            await message_queue._process_content_task(
                mock_bot,
                1,
                message_queue.MessageTask(
                    task_type="content",
                    window_id="@0",
                    parts=["assistant reply"],
                    content_type="text",
                    thread_id=100,
                ),
            )
        finally:
            ccbot_config.reply_context_enabled = original
        assert len(_capture_send_kwargs) == 1
        assert "reply_parameters" not in _capture_send_kwargs[0]["kw"]
        # Anchor was NOT popped — disabled mode is purely additive on
        # outbound, the inbound stash still records.
        assert message_queue._route_last_user_message.get((1, 100, "@0")) == 12345

    @pytest.mark.asyncio
    async def test_teardown_route_drops_anchor(self, mock_bot: AsyncMock):
        route = (1, 100, "@0")
        message_queue.set_route_last_user_message(1, 100, "@0", 12345)
        # Create a route worker so teardown has something to dismantle.
        message_queue._get_or_create_route(mock_bot, route)
        await message_queue.teardown_route(route, drop_pending=True)
        assert message_queue._route_last_user_message.get(route) is None
