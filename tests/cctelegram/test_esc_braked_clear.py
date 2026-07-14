"""GH #56 Part B — the braked-``/esc`` draft-clear mode (``bot.esc_command``).

On a window under the stranded-draft brake, ``/esc`` stops being a single
interrupt-Escape and becomes the draft-CLEAR gesture: it double-Escapes ONLY a
pane that PROVES a non-empty input box (rig 2.1.209 — a single Escape never
clears a draft, two rapid ones do), releases the brake only on fresh empty-row
proof, and sends ZERO keystrokes on any other shape (a blocking prompt, an
indeterminate frame, or an already-clear box — Esc on folder-trust KILLS Claude).
An UNBRAKED window keeps today's single-Escape interrupt byte-identical.

The predicates run for REAL against the captured rig fixtures — only tmux I/O is
faked, so the gate that decides whether keys are sent is the shipped one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_FIXTURES = Path(__file__).parent / "fixtures"
SEND_FAILED_TEXT = "❌ Failed to send — window may be gone"

_TALL_DRAFT = "inputbox_tall_draft_v2.1.209.txt"  # box present, row NON-empty
_TALL_DRAFT_CLEARED = "inputbox_tall_draft_cleared_v2.1.209.txt"  # row empty
_PICKER = "auq_single_picker_v2.1.207.txt"  # a live blocking prompt


def _pane(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _make_update() -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.message = MagicMock()
    update.message.message_thread_id = 42
    return update


def _make_tmux(
    *,
    braked: bool,
    captures: list[str | None] | None = None,
    send_results: bool | list[bool] = True,
) -> MagicMock:
    tmux = MagicMock()
    tmux.window_send_lock = MagicMock(return_value=asyncio.Lock())
    tmux.window_has_stranded_draft = MagicMock(return_value=braked)
    tmux.clear_window_stranded_draft = MagicMock()
    window = MagicMock()
    window.window_id = "@1"
    tmux.find_window_by_id = AsyncMock(return_value=window)
    if isinstance(send_results, list):
        tmux.send_keys = AsyncMock(side_effect=send_results)
    else:
        tmux.send_keys = AsyncMock(return_value=send_results)
    tmux.capture_pane_cancellation_safe = AsyncMock(side_effect=list(captures or []))
    return tmux


async def _run_esc(tmux: MagicMock) -> tuple[AsyncMock, MagicMock]:
    update = _make_update()
    context = MagicMock()
    safe_reply = AsyncMock()
    with (
        patch("cctelegram.bot.is_user_allowed", return_value=True),
        patch("cctelegram.bot._get_thread_id", return_value=42),
        patch("cctelegram.bot.session_manager") as mock_sm,
        patch("cctelegram.bot.tmux_manager", tmux),
        patch("cctelegram.bot.safe_reply", safe_reply),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        mock_sm.resolve_window_for_thread.return_value = "@1"
        from cctelegram.bot import esc_command

        await esc_command(update, context)
    return safe_reply, tmux


def _reply_text(safe_reply: AsyncMock) -> str:
    safe_reply.assert_awaited_once()
    args, _ = safe_reply.call_args
    return args[1]


# ── UNBRAKED: byte-identical single-Escape interrupt ─────────────────────


@pytest.mark.asyncio
async def test_unbraked_esc_sends_exactly_one_escape() -> None:
    tmux = _make_tmux(braked=False)
    safe_reply, tmux = await _run_esc(tmux)
    # ONE Escape, no captures, no brake release.
    tmux.send_keys.assert_awaited_once_with("@1", "\x1b", enter=False)
    tmux.capture_pane_cancellation_safe.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_not_called()
    assert "Sent Escape" in _reply_text(safe_reply)


# ── BRAKED + a proven non-empty draft: double-Escape clears it ───────────


@pytest.mark.asyncio
async def test_braked_double_escape_clears_the_draft_and_releases_the_brake() -> None:
    tmux = _make_tmux(
        braked=True,
        captures=[_pane(_TALL_DRAFT), _pane(_TALL_DRAFT_CLEARED)],
    )
    safe_reply, tmux = await _run_esc(tmux)
    # TWO Escapes (both control-char, no Enter), then the brake is released on the
    # fresh empty-row proof.
    assert tmux.send_keys.await_count == 2
    for call in tmux.send_keys.await_args_list:
        assert call.args == ("@1", "\x1b")
        assert call.kwargs == {"enter": False}
    tmux.clear_window_stranded_draft.assert_called_once()
    assert "cleared" in _reply_text(safe_reply).lower()


@pytest.mark.asyncio
async def test_braked_double_escape_that_does_not_clear_keeps_the_brake() -> None:
    # The re-capture STILL shows the draft (row non-empty) — the box did not clear.
    tmux = _make_tmux(
        braked=True,
        captures=[_pane(_TALL_DRAFT), _pane(_TALL_DRAFT)],
    )
    safe_reply, tmux = await _run_esc(tmux)
    assert tmux.send_keys.await_count == 2
    tmux.clear_window_stranded_draft.assert_not_called()
    assert "couldn't confirm" in _reply_text(safe_reply).lower()


@pytest.mark.asyncio
async def test_braked_double_escape_send_failure_reports_and_keeps_the_brake() -> None:
    tmux = _make_tmux(
        braked=True,
        captures=[_pane(_TALL_DRAFT)],  # only the first capture is reached
        send_results=[True, False],  # the second Escape fails
    )
    safe_reply, tmux = await _run_esc(tmux)
    assert tmux.send_keys.await_count == 2
    tmux.clear_window_stranded_draft.assert_not_called()
    assert _reply_text(safe_reply) == SEND_FAILED_TEXT


# ── BRAKED + already-clear box: NO keys, release on the existing proof ────


@pytest.mark.asyncio
async def test_braked_already_clear_box_releases_with_no_keystrokes() -> None:
    tmux = _make_tmux(braked=True, captures=[_pane(_TALL_DRAFT_CLEARED)])
    safe_reply, tmux = await _run_esc(tmux)
    tmux.send_keys.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_called_once()
    assert "already clear" in _reply_text(safe_reply).lower()


# ── BRAKED + an unsafe/indeterminate shape: NO keys, KEEP the brake ──────


@pytest.mark.asyncio
async def test_braked_live_picker_frame_sends_nothing_and_keeps_the_brake() -> None:
    tmux = _make_tmux(braked=True, captures=[_pane(_PICKER)])
    safe_reply, tmux = await _run_esc(tmux)
    tmux.send_keys.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_not_called()
    assert "nothing was sent" in _reply_text(safe_reply).lower()


@pytest.mark.asyncio
async def test_braked_indeterminate_capture_sends_nothing_and_keeps_the_brake() -> None:
    # A capture failure / timeout ⇒ None ⇒ box-proof fails ⇒ fail-closed, no keys.
    tmux = _make_tmux(braked=True, captures=[None])
    safe_reply, tmux = await _run_esc(tmux)
    tmux.send_keys.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_not_called()
    assert "nothing was sent" in _reply_text(safe_reply).lower()
