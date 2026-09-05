"""GH #56 Part B — the braked-``/esc`` draft-clear mode (``bot.esc_command``).

On a window under the stranded-draft brake, ``/esc`` stops being a single
interrupt-Escape and becomes the draft-CLEAR gesture: it double-Escapes ONLY a
pane that PROVES a non-empty input box, or the GH #90 guarded brake fallback
(rig 2.1.209 — a single Escape never
clears a draft, two rapid ones do), releases the brake only on fresh empty-row
proof. Recognized blocking prompts and failed captures receive ZERO keys; the
fallback needs a fresh Claude command proof and may interrupt a turn. An
already-clear box releases keylessly; Esc on folder-trust KILLS Claude.
An UNBRAKED window keeps today's single-Escape interrupt byte-identical.

The predicates run for REAL against the captured rig fixtures — only tmux I/O is
faked, so the gate that decides whether keys are sent is the shipped one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import terminal_parser as tp

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
    tmux.pane_current_command = AsyncMock(return_value="2.1.258")
    window = MagicMock()
    window.window_id = "@1"
    tmux.find_window_by_id = AsyncMock(return_value=window)
    if isinstance(send_results, list):
        tmux.send_keys = AsyncMock(side_effect=send_results)
    else:
        tmux.send_keys = AsyncMock(return_value=send_results)

    # FAKE HONESTY (GH #60 P1): honor ``with_ansi`` so the braked-/esc test can go
    # RED pre-fix. ``_esc_bounded_capture`` currently captures WITHOUT ANSI, so a
    # ghost frame would arrive ANSI-stripped (a non-empty draft → the double-Escape
    # fires and the brake is never released); after fix #2 it requests
    # ``with_ansi=True`` and the ANSI ghost cleans to an empty row (keyless
    # release). The existing plain-text fixtures carry no ESC bytes, so stripping
    # is a no-op for them.
    queue = list(captures or [])

    def _capture(window_id: str, with_ansi: bool = False, scrollback_lines: int = 0):
        value = queue.pop(0)
        if value is None or with_ansi:
            return value
        return tp._strip_ansi(value)

    tmux.capture_pane_cancellation_safe = AsyncMock(side_effect=_capture)
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


# ── BRAKED + a fully-DIM ghost suggestion (GH #60): ANSI capture cleans it ──


_GHOST_PROSE = "inputbox_ghost_prose_v2.1.215.ansi.txt"  # real dim ghost, ANSI


@pytest.mark.asyncio
async def test_braked_ghost_only_pane_releases_keylessly_with_ansi_capture() -> None:
    """GH #60 braked-``/esc`` half (RED pre-fix): a window whose box holds only a
    fully-dim GHOST suggestion must take the ALREADY-CLEAR branch — no Escape, brake
    released — because ``_esc_bounded_capture`` captures WITH ANSI so
    ``pane_input_row_empty``'s internal ``clean_ghost_input_text`` sees the ghost.
    Pre-fix (plain capture) the ghost reads as a non-empty draft: a pointless
    double-Escape fires (Esc does not clear a ghost) and the brake never releases."""
    # Two copies survive the pre-fix double-Escape re-capture without an IndexError.
    tmux = _make_tmux(braked=True, captures=[_pane(_GHOST_PROSE), _pane(_GHOST_PROSE)])
    safe_reply, tmux = await _run_esc(tmux)

    tmux.send_keys.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_called_once()
    assert "already clear" in _reply_text(safe_reply).lower()

    # Every ``_esc_bounded_capture`` await must request the ANSI form (fix #2).
    assert tmux.capture_pane_cancellation_safe.await_count >= 1
    for call in tmux.capture_pane_cancellation_safe.await_args_list:
        assert call.kwargs.get("with_ansi") is True


# GH #90: empty control is SYNTHETIC, tall frame is the redacted live capture.
_NOTICE_TALL = "inputbox_limit_notice_tall_draft_v2.1.258.ansi.txt"
_NOTICE_EMPTY = "inputbox_limit_notice_empty_v2.1.258.txt"


@pytest.mark.asyncio
async def test_gh90_braked_notice_draft_clears() -> None:
    tmux = _make_tmux(braked=True, captures=[_pane(_NOTICE_TALL), _pane(_NOTICE_EMPTY)])
    reply, tmux = await _run_esc(tmux)
    assert tmux.send_keys.await_count == 2
    tmux.clear_window_stranded_draft.assert_called_once()
    assert "cleared" in _reply_text(reply)
    assert "fallback" not in _reply_text(reply)


def _unknown_tall_frame() -> str:
    """SYNTHETIC stacked toasts break only the tall fallback, leaving ready chrome."""
    return _pane(_NOTICE_TALL).replace(
        "⚠ /limit-reset to reset your session limit now · uses weekly limit · 1/week",
        "first notice\nsecond notice",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cleared", [True, False])
async def test_gh90_brake_fallback_requires_fresh_empty_release(cleared: bool) -> None:
    unknown = _unknown_tall_frame()
    assert tp.pane_input_box_present(unknown) is False
    tmux = _make_tmux(
        braked=True,
        captures=[
            unknown,
            _pane(_NOTICE_EMPTY).replace(
                "⚠ /limit-reset to reset your session limit now · uses weekly limit · 1/week",
                "first notice\nsecond notice",
            )
            if cleared
            else unknown,
        ],
    )
    tmux.pane_current_command = AsyncMock(return_value="2.1.258")
    reply, tmux = await _run_esc(tmux)
    tmux.pane_current_command.assert_awaited_once_with("@1")
    assert tmux.send_keys.await_count == 2
    for call in tmux.send_keys.await_args_list:
        assert call.args == ("@1", "\x1b") and call.kwargs == {"enter": False}
    if cleared:
        tmux.clear_window_stranded_draft.assert_called_once_with(
            "@1", reason="/esc brake-fallback double-escape cleared the box"
        )
        assert "cleared via the brake fallback" in _reply_text(reply)
        assert "wasn't recognized" in _reply_text(reply)
        assert "/screenshot" in _reply_text(reply)
    else:
        tmux.clear_window_stranded_draft.assert_not_called()
        assert "Couldn't confirm" in _reply_text(reply)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "folder_trust_arrival_plain_v2.1.207.txt",
        "folder_trust_arrival_plain_v2.1.258.ansi.txt",
        _PICKER,
        "gate_epm_v2.1.207.txt",
        "gate_permission_v2.1.207.txt",
        "gate_workflow_v2.1.207.txt",
        "decision_footerless_switchmodel_v2.1.207.txt",
    ],
)
@pytest.mark.parametrize("detectors", [True, False])
async def test_gh90_fallback_refuses_blocking_surfaces(
    name: str, detectors: bool
) -> None:
    tmux = _make_tmux(braked=True, captures=[_pane(name)])
    tmux.pane_current_command = AsyncMock(return_value="2.1.258")
    tp.set_decision_cards_enabled(detectors)
    tp.set_permission_prompts_enabled(detectors)
    try:
        reply, tmux = await _run_esc(tmux)
    finally:
        tp.set_decision_cards_enabled(True)
        tp.set_permission_prompts_enabled(True)
    tmux.send_keys.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_not_called()
    assert "Nothing was sent" in _reply_text(reply)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["zsh", "python", None])
async def test_gh90_fallback_requires_claude(command: str | None) -> None:
    tmux = _make_tmux(braked=True, captures=[_unknown_tall_frame()])
    tmux.pane_current_command = AsyncMock(return_value=command)
    reply, tmux = await _run_esc(tmux)
    tmux.send_keys.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_not_called()
    assert "Nothing was sent" in _reply_text(reply)


@pytest.mark.asyncio
async def test_gh90_fallback_rechecks_brake() -> None:
    tmux = _make_tmux(braked=True, captures=[_unknown_tall_frame()])
    tmux.window_has_stranded_draft.side_effect = [True, False]
    tmux.pane_current_command = AsyncMock(return_value="2.1.258")
    await _run_esc(tmux)
    tmux.pane_current_command.assert_not_awaited()
    tmux.send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_gh90_fallback_command_probe_timeout_is_bounded(monkeypatch) -> None:
    from cctelegram import bot

    tmux = _make_tmux(braked=True, captures=[_unknown_tall_frame()])
    cancelled = asyncio.Event()

    async def hang(_window_id: str) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    tmux.pane_current_command = AsyncMock(side_effect=hang)
    monkeypatch.setattr(bot, "POST_SEND_CAPTURE_DEADLINE_S", 0.01)
    reply, tmux = await _run_esc(tmux)
    assert cancelled.is_set()
    tmux.send_keys.assert_not_awaited()
    assert "Nothing was sent" in _reply_text(reply)


@pytest.mark.asyncio
async def test_gh90_fallback_command_cancellation_propagates() -> None:
    tmux = _make_tmux(braked=True, captures=[_unknown_tall_frame()])
    tmux.pane_current_command = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await _run_esc(tmux)
    tmux.send_keys.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [None, "", "  \n  ", "\x1b[39m  \n"])
async def test_gh90_missing_or_blank_frame_never_uses_fallback(
    frame: str | None,
) -> None:
    tmux = _make_tmux(braked=True, captures=[frame])
    reply, tmux = await _run_esc(tmux)
    tmux.send_keys.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_not_called()
    assert "Nothing was sent" in _reply_text(reply)


def _drifted_prompt_frame(kind: str) -> str:
    """SYNTHETIC review frames: unknown hint below options, or body-only redraw."""
    if kind == "numbered-confirm":
        return "\n".join(
            [
                "─" * 80,
                "Proceed with this change?",
                "❯ 1. Yes",
                "  2. No",
                "New unrecognized confirmation hint",
            ]
        )
    trust = _pane("folder_trust_arrival_plain_v2.1.258.txt")
    if kind == "unnumbered-trust":
        return trust.rstrip() + "\nNew unrecognized confirmation hint\n"
    return trust.split(" ❯ No, exit")[0].rstrip() + "\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["numbered-confirm", "unnumbered-trust", "half-drawn-trust"]
)
async def test_gh90_drifted_prompt_frames_send_zero_keys(kind: str) -> None:
    frame = _drifted_prompt_frame(kind)
    plain = tp.clean_ghost_input_text(frame)
    assert tp.pane_input_box_present(frame) is False
    assert tp.pane_ready_chrome_below_last_rule(frame) is False
    # All old recognizer vetoes miss these frames; negative evidence was unsafe.
    assert tp.extract_interactive_content(plain) is None
    assert not tp.has_live_decision_residue(plain)
    assert tp.parse_generic_decision(plain) is None
    assert tp.parse_permission_prompt(plain) is None
    assert tp.parse_workflow_approval(plain) is None
    tmux = _make_tmux(braked=True, captures=[frame])
    reply, tmux = await _run_esc(tmux)
    tmux.send_keys.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_not_called()
    assert "Nothing was sent" in _reply_text(reply)


@pytest.mark.asyncio
async def test_gh90_unparseable_bracket_with_intact_ready_bar_keeps_brake() -> None:
    # SYNTHETIC: missing top rule, intact ready footer. Even an emptied draft
    # cannot release the brake while the bracket itself remains unparseable.
    bar = "⏵⏵ bypass permissions on · shift+tab to cycle"
    frame = "❯ held draft\n" + "─" * 80 + "\n" + bar
    emptied = frame.replace("held draft", "")
    assert tp.pane_input_box_present(frame) is False
    assert tp.pane_input_row_empty(emptied) is None
    tmux = _make_tmux(braked=True, captures=[frame, emptied])
    reply, tmux = await _run_esc(tmux)
    assert tmux.send_keys.await_count == 2
    tmux.clear_window_stranded_draft.assert_not_called()
    assert "Couldn't confirm" in _reply_text(reply)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["unnumbered-trust", "numbered-confirm", "plain-unnumbered-trust"],
    ids=["E-unnumbered-drift", "F-numbered-drift", "G-plain-unnumbered"],
)
async def test_gh90_shape_veto_stays_active_with_ready_chrome(kind: str) -> None:
    # SYNTHETIC reviewer frames E/F/G: live options retain a painted status bar.
    prompt = (
        _pane("folder_trust_arrival_plain_v2.1.258.txt").rstrip()
        if kind == "plain-unnumbered-trust"
        else _drifted_prompt_frame(kind)
    )
    frame = prompt + "\n⏵⏵ bypass permissions on · shift+tab to cycle"
    assert tp.pane_ready_chrome_below_last_rule(frame)
    assert tp.pane_input_box_present(frame) is False
    assert tp.pane_blocking_prompt_shape(frame) is (kind == "numbered-confirm")
    assert tp.pane_unnumbered_blocking_prompt_shape(frame) is (
        kind != "numbered-confirm"
    )
    tmux = _make_tmux(braked=True, captures=[frame, frame])
    reply, tmux = await _run_esc(tmux)
    tmux.send_keys.assert_not_awaited()
    tmux.clear_window_stranded_draft.assert_not_called()
    assert "Nothing was sent" in _reply_text(reply)
