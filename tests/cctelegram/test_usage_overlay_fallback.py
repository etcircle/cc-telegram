"""Tests for the /cost + /usage busy-path snapshot fallback + observability.

The shared ``bot._run_usage_overlay`` scaffold now:

- LOGS every exit classified (receipt + one INFO per exit path).
- On EVERY non-overlay exit (refusals AND post-preflight failures) posts a
  bridge-side "cost snapshot" card instead of a dead-end refusal: context %
  from ``route_runtime.snapshot(route).context_usage`` + the cached last
  successful overlay (absolute + age) + a REASON-SPECIFIC action line.
- Retries the preflight capture on INDETERMINATE frames only (bounded), and
  refuses POSITIVE hazards immediately with exactly ONE capture.
- Caches the overlay result on the SUCCESS path (keyed route + session id).

These drive ``cost_command`` / ``usage_command`` against fake tmux + patched
``route_runtime`` / ``usage_cache`` / session identity, asserting behavior at
the Telegram reply seam.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram.handlers import usage_cache
from cctelegram.route_runtime import ContextUsage

_FIXTURES = Path(__file__).parent / "fixtures"
_SEP = "─" * 56

SEND_FAILED_TEXT = "❌ Failed to send — window may be gone"

IDLE_PANE = f"""\
✻ Cooked for 2s

{_SEP}
❯
{_SEP}
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

BUSY_PANE = f"""\
✻ Cooking… (esc to interrupt)

{_SEP}
❯
{_SEP}
  esc to interrupt
"""

DRAFT_PANE = f"""\
✻ Wove for 5s

{_SEP}
❯ a half-typed draft
{_SEP}
  ? for shortcuts
"""

BG_SHELLS_PANE = f"""\
✻ Cooked for 2s

{_SEP}
❯
{_SEP}
  ⏵⏵ bypass permissions on · 2 shells · ← for agents
"""

# Mid-redraw: empty box, no ready-status marker below → indeterminate.
MIDREDRAW_PANE = f"""\
✻ Working…

{_SEP}
❯
{_SEP}
"""


def _overlay_fixture() -> str:
    return (_FIXTURES / "cost_overlay_live_v2.1.206.txt").read_text()


def _picker_fixture() -> str:
    return (_FIXTURES / "auq_4option_160x50_v2.1.198.txt").read_text()


def _make_update(user_id: int = 1, thread_id: int | None = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = 100
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


def _make_tmux(
    send_results: bool | list[bool] = True,
    pane_text: str | list[str | None] | None = None,
) -> MagicMock:
    tmux = MagicMock()
    tmux.window_send_lock = MagicMock(return_value=asyncio.Lock())
    window = MagicMock()
    window.window_id = "@1"
    tmux.find_window_by_id = AsyncMock(return_value=window)
    if isinstance(send_results, list):
        tmux.send_keys = AsyncMock(side_effect=send_results)
    else:
        tmux.send_keys = AsyncMock(return_value=send_results)
    if isinstance(pane_text, list):
        tmux.capture_pane = AsyncMock(side_effect=list(pane_text))
        tmux.capture_pane_cancellation_safe = AsyncMock(side_effect=list(pane_text))
    else:
        tmux.capture_pane = AsyncMock(return_value=pane_text)
        tmux.capture_pane_cancellation_safe = AsyncMock(return_value=pane_text)
    return tmux


_PATCH_ALLOWED = "cctelegram.bot.is_user_allowed"
_PATCH_THREAD = "cctelegram.bot._get_thread_id"
_PATCH_SM = "cctelegram.bot.session_manager"
_PATCH_TMUX = "cctelegram.bot.tmux_manager"
_PATCH_REPLY = "cctelegram.bot.safe_reply"


async def _run(
    command_name: str,
    tmux: MagicMock,
    *,
    context_usage: ContextUsage | None = None,
    session_id: str | None = "sess-1",
    locked: bool = False,
) -> AsyncMock:
    """Drive usage_command/cost_command against the mock; return safe_reply mock."""
    update = _make_update()
    context = _make_context()
    safe_reply_mock = AsyncMock()

    if locked:
        held = asyncio.Lock()
        await held.acquire()
        tmux.window_send_lock = MagicMock(return_value=held)

    snap = MagicMock()
    snap.context_usage = context_usage

    with (
        patch(_PATCH_ALLOWED, return_value=True),
        patch(_PATCH_THREAD, return_value=42),
        patch(_PATCH_SM) as mock_sm,
        patch(_PATCH_TMUX, tmux),
        patch(_PATCH_REPLY, safe_reply_mock),
        patch("cctelegram.bot.route_runtime.snapshot", return_value=snap),
        patch(
            "cctelegram.bot.peek_session_id_for_window",
            return_value=session_id,
        ),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        mock_sm.resolve_window_for_thread.return_value = "@1"
        from cctelegram import bot as bot_module

        await getattr(bot_module, command_name)(update, context)
    return safe_reply_mock


@pytest.fixture(autouse=True)
def _reset_cache():
    usage_cache.reset_for_tests()
    yield
    usage_cache.reset_for_tests()


# ── Test 3: idle overlay path unchanged (happy path still parses) ──────────


class TestIdleOverlayPathUnchanged:
    @pytest.mark.asyncio
    async def test_idle_overlay_parses_and_caches(self):
        tmux = _make_tmux(pane_text=[IDLE_PANE, _overlay_fixture()])
        reply = await _run("cost_command", tmux)
        reply.assert_awaited_once()
        assert "Total cost:" in reply.call_args.args[1]
        # SUCCESS path populates the cache.
        entry = usage_cache.peek((1, 42, "@1"), "sess-1")
        assert entry is not None
        assert "Total cost:" in entry.text

    @pytest.mark.asyncio
    async def test_idle_overlay_escapes(self):
        tmux = _make_tmux(pane_text=[IDLE_PANE, _overlay_fixture()])
        await _run("usage_command", tmux)
        calls = tmux.send_keys.await_args_list
        assert calls[0].args[1] == "/usage"
        assert calls[1].args[1] == "Escape"


# ── Test 1/2: busy pane → snapshot card, zero keystrokes; grouping pin ─────


class TestBusyPaneSnapshot:
    @pytest.mark.asyncio
    async def test_cost_busy_pane_posts_snapshot_zero_keystrokes(self):
        cu = ContextUsage(tokens=112_000, max_tokens=200_000)
        tmux = _make_tmux(pane_text=[BUSY_PANE])
        reply = await _run("cost_command", tmux, context_usage=cu)

        tmux.send_keys.assert_not_called()
        reply.assert_awaited_once()
        body = reply.call_args.args[1]
        # Context % is surfaced (112000/200000 = 56%).
        assert "56%" in body
        # Reason-specific action line (active generation).
        assert "working" in body.lower() or "turn ends" in body.lower()

    @pytest.mark.asyncio
    async def test_usage_busy_pane_identical_behavior(self):
        cu = ContextUsage(tokens=112_000, max_tokens=200_000)
        tmux = _make_tmux(pane_text=[BUSY_PANE])
        reply = await _run("usage_command", tmux, context_usage=cu)

        tmux.send_keys.assert_not_called()
        reply.assert_awaited_once()
        assert "56%" in reply.call_args.args[1]

    @pytest.mark.asyncio
    async def test_busy_snapshot_renders_cached_overlay_with_age(self):
        cu = ContextUsage(tokens=100_000, max_tokens=200_000)
        # Seed a prior successful overlay 26 min ago.
        import time

        usage_cache.record(
            (1, 42, "@1"), "sess-1", "Total cost: $2.00", now=time.time() - 26 * 60
        )
        tmux = _make_tmux(pane_text=[BUSY_PANE])
        reply = await _run("cost_command", tmux, context_usage=cu)
        body = reply.call_args.args[1]
        assert "Total cost: $2.00" in body
        assert "as of" in body.lower()
        assert "ago" in body.lower()


# ── Test 4: lock-busy → snapshot, not the bare retry text ──────────────────


class TestLockBusy:
    @pytest.mark.asyncio
    async def test_lock_busy_posts_snapshot(self, caplog):
        cu = ContextUsage(tokens=60_000, max_tokens=200_000)
        tmux = _make_tmux(pane_text=[IDLE_PANE])
        with caplog.at_level(logging.INFO, logger="cctelegram.bot"):
            reply = await _run("cost_command", tmux, context_usage=cu, locked=True)

        tmux.send_keys.assert_not_called()
        # No preflight capture at all — the lock was busy.
        tmux.capture_pane_cancellation_safe.assert_not_called()
        body = reply.call_args.args[1]
        assert "30%" in body  # 60000/200000
        assert "lock_busy" in caplog.text


# ── Test 5: immediate refusals (positive hazards) — exactly ONE capture ────


class TestImmediateRefusals:
    @pytest.mark.asyncio
    async def test_live_picker_one_capture(self):
        tmux = _make_tmux(pane_text=[_picker_fixture()])
        await _run("cost_command", tmux)
        assert tmux.capture_pane_cancellation_safe.await_count == 1
        tmux.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_draft_in_box_one_capture(self):
        tmux = _make_tmux(pane_text=[DRAFT_PANE])
        reply = await _run("cost_command", tmux)
        assert tmux.capture_pane_cancellation_safe.await_count == 1
        tmux.send_keys.assert_not_called()
        # The truthful-conditional draft line.
        assert "draft" in reply.call_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_active_generation_one_capture(self):
        tmux = _make_tmux(pane_text=[BUSY_PANE])
        await _run("cost_command", tmux)
        assert tmux.capture_pane_cancellation_safe.await_count == 1
        tmux.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_background_shells_one_capture(self):
        tmux = _make_tmux(pane_text=[BG_SHELLS_PANE])
        reply = await _run("cost_command", tmux)
        assert tmux.capture_pane_cancellation_safe.await_count == 1
        tmux.send_keys.assert_not_called()
        assert "background shell" in reply.call_args.args[1].lower()


# ── Test 6: retry on indeterminate frame ───────────────────────────────────


class TestIndeterminateRetry:
    @pytest.mark.asyncio
    async def test_midredraw_then_idle_proceeds(self):
        # Frame 1 mid-redraw (indeterminate) → frame 2 idle → overlay proceeds.
        tmux = _make_tmux(pane_text=[MIDREDRAW_PANE, IDLE_PANE, _overlay_fixture()])
        reply = await _run("cost_command", tmux)
        # Two preflight captures + one post-send capture.
        assert tmux.capture_pane_cancellation_safe.await_count == 3
        assert "Total cost:" in reply.call_args.args[1]

    @pytest.mark.asyncio
    async def test_all_indeterminate_refuses_after_bounded_retries(self):
        tmux = _make_tmux(pane_text=[MIDREDRAW_PANE, MIDREDRAW_PANE, MIDREDRAW_PANE])
        reply = await _run("cost_command", tmux)
        # 1 initial + 2 retries = 3 captures, then refuse.
        assert tmux.capture_pane_cancellation_safe.await_count == 3
        tmux.send_keys.assert_not_called()
        # chrome-indeterminate action line.
        assert "read the terminal" in reply.call_args.args[1].lower()


# ── Test 8: no-data shape ──────────────────────────────────────────────────


class TestNoDataShape:
    @pytest.mark.asyncio
    async def test_no_context_no_cache_still_renders_card(self):
        tmux = _make_tmux(pane_text=[BUSY_PANE])
        reply = await _run("cost_command", tmux, context_usage=None)
        body = reply.call_args.args[1]
        # Never today's bare refusal — a classified reason + no-data note.
        assert "no bridge-side metrics" in body.lower()
        # Still carries the reason-specific action line.
        assert "working" in body.lower() or "turn ends" in body.lower()


# ── Test 8b: post-preflight failure exits keep safety text + append snapshot ─


class TestPostPreflightFailures:
    @pytest.mark.asyncio
    async def test_send_failure_keeps_text_and_appends_snapshot(self):
        cu = ContextUsage(tokens=80_000, max_tokens=200_000)
        tmux = _make_tmux(send_results=False, pane_text=[IDLE_PANE])
        reply = await _run("usage_command", tmux, context_usage=cu)
        body = reply.call_args.args[1]
        assert SEND_FAILED_TEXT in body
        assert "40%" in body  # snapshot appended

    @pytest.mark.asyncio
    async def test_post_send_capture_none_keeps_text_and_appends_snapshot(self):
        cu = ContextUsage(tokens=80_000, max_tokens=200_000)
        tmux = _make_tmux(send_results=True, pane_text=[IDLE_PANE, None])
        reply = await _run("cost_command", tmux, context_usage=cu)
        body = reply.call_args.args[1]
        assert "usage screen may be open" in body or "may be open" in body
        assert "40%" in body

    @pytest.mark.asyncio
    async def test_overlay_never_opened_keeps_text_and_appends_snapshot(self):
        cu = ContextUsage(tokens=80_000, max_tokens=200_000)
        tmux = _make_tmux(
            send_results=True,
            pane_text=[IDLE_PANE, "✻ Cooking… (esc to interrupt)"],
        )
        reply = await _run("cost_command", tmux, context_usage=cu)
        body = reply.call_args.args[1]
        assert "didn't open" in body
        assert "40%" in body
        # No Escape into an unknown state.
        assert tmux.send_keys.await_count == 1

    @pytest.mark.asyncio
    async def test_dismiss_failure_keeps_text_and_appends_snapshot(self):
        cu = ContextUsage(tokens=80_000, max_tokens=200_000)
        tmux = _make_tmux(
            send_results=[True, False], pane_text=[IDLE_PANE, _overlay_fixture()]
        )
        reply = await _run("usage_command", tmux, context_usage=cu)
        body = reply.call_args.args[1]
        assert SEND_FAILED_TEXT in body
        assert "Total cost:" not in body  # never presented as usage output
        assert "40%" in body


# ── Test 8d: preflight deadline → capture_timeout fallback + lock released ──


class TestPreflightDeadline:
    @pytest.mark.asyncio
    async def test_hung_capture_falls_back_and_releases_lock(self, caplog):
        cu = ContextUsage(tokens=40_000, max_tokens=200_000)

        async def _hang(*_a, **_kw):
            await asyncio.Event().wait()

        tmux = _make_tmux()
        tmux.capture_pane_cancellation_safe = AsyncMock(side_effect=_hang)
        the_lock = asyncio.Lock()
        tmux.window_send_lock = MagicMock(return_value=the_lock)

        with caplog.at_level(logging.INFO, logger="cctelegram.bot"):
            # We must NOT patch asyncio.sleep-based real wait_for; use a tiny
            # deadline override via the module constant patch.
            with patch("cctelegram.bot.PREFLIGHT_DEADLINE_S", 0.05):
                reply = await _run("cost_command", tmux, context_usage=cu)

        reply.assert_awaited_once()
        assert "capture_timeout" in caplog.text
        # The lock is released after the transaction.
        assert not the_lock.locked()
        # No keystrokes were sent into a hung pane.
        tmux.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_send_capture_hang_no_escape_and_snapshot(self, caplog):
        cu = ContextUsage(tokens=40_000, max_tokens=200_000)

        # Preflight returns idle; post-send capture hangs.
        async def _capture(*_a, **_kw):
            if not _capture.first_done:
                _capture.first_done = True
                return IDLE_PANE
            await asyncio.Event().wait()

        _capture.first_done = False  # type: ignore[attr-defined]

        tmux = _make_tmux(send_results=True)
        tmux.capture_pane_cancellation_safe = AsyncMock(side_effect=_capture)
        the_lock = asyncio.Lock()
        tmux.window_send_lock = MagicMock(return_value=the_lock)

        with caplog.at_level(logging.INFO, logger="cctelegram.bot"):
            with patch("cctelegram.bot.POST_SEND_CAPTURE_DEADLINE_S", 0.05):
                reply = await _run("cost_command", tmux, context_usage=cu)

        # /cost was sent; the post-send capture hung; NO blind Escape.
        sent = [c.args[1] for c in tmux.send_keys.await_args_list]
        assert "/cost" in sent
        assert "Escape" not in sent
        body = reply.call_args.args[1]
        assert "20%" in body  # 40000/200000
        assert not the_lock.locked()


# ── Test 8e: cache TTL 30 min ──────────────────────────────────────────────


class TestCacheTTLInBusyCard:
    @pytest.mark.asyncio
    async def test_31_min_old_cache_absent_from_card(self):
        import time

        cu = ContextUsage(tokens=40_000, max_tokens=200_000)
        usage_cache.record(
            (1, 42, "@1"), "sess-1", "Total cost: $9.99", now=time.time() - 31 * 60
        )
        tmux = _make_tmux(pane_text=[BUSY_PANE])
        reply = await _run("cost_command", tmux, context_usage=cu)
        body = reply.call_args.args[1]
        assert "Total cost: $9.99" not in body
        # But context % still shows.
        assert "20%" in body


# ── Test 10: every exit emits exactly one classified INFO line ─────────────


class TestExitLogging:
    @pytest.mark.asyncio
    async def test_busy_exit_logs_pane_busy_reason(self, caplog):
        tmux = _make_tmux(pane_text=[BUSY_PANE])
        with caplog.at_level(logging.INFO, logger="cctelegram.bot"):
            await _run("cost_command", tmux)
        # One receipt + one classified exit.
        assert "active_status" in caplog.text

    @pytest.mark.asyncio
    async def test_success_exit_logs_lifecycle(self, caplog):
        tmux = _make_tmux(pane_text=[IDLE_PANE, _overlay_fixture()])
        with caplog.at_level(logging.INFO, logger="cctelegram.bot"):
            await _run("cost_command", tmux)
        assert "esc_sent" in caplog.text or "overlay_present" in caplog.text
