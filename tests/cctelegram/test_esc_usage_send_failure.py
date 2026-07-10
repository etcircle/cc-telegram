"""Tests for /esc and the /usage + /cost overlay interceptors.

The repo contract: ``TmuxManager.send_keys`` returns False on failure, never
raises. /esc replies honestly on a failed send. The /usage + /cost interceptors
(shared ``_run_usage_overlay`` scaffold) additionally:

- PREFLIGHT the pane under the send lock and refuse with ZERO keystrokes unless
  the pane shows positive idle evidence (``pane_looks_idle``) and no live
  interactive surface — typing "/cost" + Enter into a live AUQ picker would
  COMMIT the highlighted option (round-1 converged P1).
- Send Escape ONLY when the post-settle capture shows the overlay chrome; if
  the overlay never opened, the pane is left untouched and the reply is honest
  (the unconditional Esc was the /esc-hazard arm of the same P1).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SEND_FAILED_TEXT = "❌ Failed to send — window may be gone"

_FIXTURES = Path(__file__).parent / "fixtures"

_SEP = "─" * 56

# A genuinely idle Claude Code frame (mirrors test_pane_looks_idle.IDLE_PANE):
# post-completion summary + EMPTY ❯ input box + ready status chrome.
IDLE_PANE = f"""\
✻ Cooked for 2s

{_SEP}
❯
{_SEP}
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

# A mid-generation frame — the live-run chrome fails pane_looks_idle.
BUSY_PANE = f"""\
✻ Cooking… (esc to interrupt)

{_SEP}
❯
{_SEP}
  esc to interrupt
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
    send_results: bool | list[bool],
    pane_text: str | list[str | None] | None = "some pane content",
) -> MagicMock:
    tmux = MagicMock()
    # Wave 3b: /esc and /usage consult the per-window send lock; a bare
    # MagicMock attribute would return a truthy ``locked()`` and trip the
    # reject-if-held branch, so hand out a real (free) asyncio.Lock.
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


async def _run(command_name: str, tmux: MagicMock) -> AsyncMock:
    """Drive usage_command/cost_command against the given tmux mock; return safe_reply."""
    from cctelegram.handlers import usage_cache

    usage_cache.reset_for_tests()
    update = _make_update()
    context = _make_context()
    safe_reply_mock = AsyncMock()
    snap = MagicMock()
    snap.context_usage = None  # no bridge-side metrics unless a test seeds them
    with (
        patch(_PATCH_ALLOWED, return_value=True),
        patch(_PATCH_THREAD, return_value=42),
        patch(_PATCH_SM) as mock_sm,
        patch(_PATCH_TMUX, tmux),
        patch(_PATCH_REPLY, safe_reply_mock),
        patch("cctelegram.bot.route_runtime.snapshot", return_value=snap),
        patch("cctelegram.bot.peek_session_id_for_window", return_value="sess-1"),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        mock_sm.resolve_window_for_thread.return_value = "@1"
        from cctelegram import bot as bot_module

        await getattr(bot_module, command_name)(update, context)
    return safe_reply_mock


class TestEscCommand:
    @pytest.mark.asyncio
    async def test_failed_send_replies_failure_not_sent_escape(self):
        update = _make_update()
        context = _make_context()
        tmux = _make_tmux(send_results=False)
        safe_reply_mock = AsyncMock()

        with (
            patch(_PATCH_ALLOWED, return_value=True),
            patch(_PATCH_THREAD, return_value=42),
            patch(_PATCH_SM) as mock_sm,
            patch(_PATCH_TMUX, tmux),
            patch(_PATCH_REPLY, safe_reply_mock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"

            from cctelegram.bot import esc_command

            await esc_command(update, context)

        safe_reply_mock.assert_awaited_once()
        args, _ = safe_reply_mock.call_args
        assert args[1] == SEND_FAILED_TEXT
        assert "Sent Escape" not in args[1]

    @pytest.mark.asyncio
    async def test_successful_send_replies_sent_escape(self):
        update = _make_update()
        context = _make_context()
        tmux = _make_tmux(send_results=True)
        safe_reply_mock = AsyncMock()

        with (
            patch(_PATCH_ALLOWED, return_value=True),
            patch(_PATCH_THREAD, return_value=42),
            patch(_PATCH_SM) as mock_sm,
            patch(_PATCH_TMUX, tmux),
            patch(_PATCH_REPLY, safe_reply_mock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@1"

            from cctelegram.bot import esc_command

            await esc_command(update, context)

        safe_reply_mock.assert_awaited_once()
        args, _ = safe_reply_mock.call_args
        assert args[1] == "⎋ Sent Escape"


class TestUsageOverlayPreflight:
    """The round-1 converged P1: never type into a non-idle pane, never Esc blind."""

    @pytest.mark.asyncio
    async def test_busy_generation_pane_refuses_with_zero_keystrokes(self):
        """A mid-generation pane refuses the command — nothing is typed."""
        tmux = _make_tmux(send_results=True, pane_text=[BUSY_PANE])
        safe_reply_mock = await _run("cost_command", tmux)

        tmux.send_keys.assert_not_called()
        safe_reply_mock.assert_awaited_once()
        notice = safe_reply_mock.call_args.args[1]
        # v5: a bridge-side snapshot card with the reason-specific action line
        # (active generation → "Claude is working — try again when the turn ends").
        assert "working" in notice.lower() or "turn ends" in notice.lower()
        assert "snapshot" in notice.lower()

    @pytest.mark.asyncio
    async def test_live_picker_pane_refuses_with_zero_keystrokes(self):
        """A live AUQ picker refuses — "/cost" + Enter would commit an option."""
        tmux = _make_tmux(send_results=True, pane_text=[_picker_fixture()])
        safe_reply_mock = await _run("usage_command", tmux)

        tmux.send_keys.assert_not_called()
        safe_reply_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_preflight_capture_failure_refuses_with_zero_keystrokes(self):
        """No preflight capture ⇒ no idle proof (after bounded retries) ⇒ nothing typed."""
        # Indeterminate frames retry up to 2 extra times; feed 3 Nones.
        tmux = _make_tmux(send_results=True, pane_text=[None, None, None])
        safe_reply_mock = await _run("cost_command", tmux)

        tmux.send_keys.assert_not_called()
        safe_reply_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_overlay_opened_happy_path_escapes(self):
        """Idle preflight → send → overlay chrome present → Esc dismisses it."""
        tmux = _make_tmux(send_results=True, pane_text=[IDLE_PANE, _overlay_fixture()])
        safe_reply_mock = await _run("cost_command", tmux)

        calls = tmux.send_keys.await_args_list
        assert calls[0].args[1] == "/cost"
        assert calls[1].args[1] == "Escape"
        assert tmux.send_keys.await_count == 2
        args, _ = safe_reply_mock.call_args
        assert "Total cost:" in args[1]

    @pytest.mark.asyncio
    async def test_overlay_never_opened_does_not_escape(self):
        """The command was sent but no overlay chrome appeared — do NOT Esc
        (an Escape into an active generation would interrupt it); reply honestly."""
        tmux = _make_tmux(
            send_results=True, pane_text=[IDLE_PANE, "✻ Cooking… (esc to interrupt)"]
        )
        safe_reply_mock = await _run("cost_command", tmux)

        # Only the "/cost" send — no Escape.
        assert tmux.send_keys.await_count == 1
        assert tmux.send_keys.await_args_list[0].args[1] == "/cost"
        notice = safe_reply_mock.call_args.args[1]
        assert "didn't open" in notice

    @pytest.mark.asyncio
    async def test_post_send_capture_failure_does_not_escape(self):
        """No capture after the send ⇒ overlay state unknown ⇒ no blind Esc."""
        tmux = _make_tmux(send_results=True, pane_text=[IDLE_PANE, None])
        safe_reply_mock = await _run("usage_command", tmux)

        assert tmux.send_keys.await_count == 1
        safe_reply_mock.assert_awaited_once()


class TestUsageCommand:
    @pytest.mark.asyncio
    async def test_failed_usage_send_skips_capture_and_replies_failure(self):
        """Preflight passes (idle), the /usage send fails → honest failure reply;
        no post-send capture, no Escape."""
        tmux = _make_tmux(send_results=False, pane_text=[IDLE_PANE])
        safe_reply_mock = await _run("usage_command", tmux)

        safe_reply_mock.assert_awaited_once()
        args, _ = safe_reply_mock.call_args
        # v5: the honest safety text is preserved VERBATIM, snapshot appended.
        assert args[1].startswith(SEND_FAILED_TEXT)
        assert "snapshot" in args[1].lower()
        # Exactly ONE capture (the preflight); the post-send capture is skipped.
        assert tmux.capture_pane_cancellation_safe.await_count == 1
        # Only the /usage send happened; no dismiss-Escape after a failed send.
        assert tmux.send_keys.await_count == 1

    @pytest.mark.asyncio
    async def test_failed_dismiss_escape_replies_failure_not_usage_output(self):
        """Overlay opened, the dismiss Escape send fails (window vanished) →
        honest failure, never presented as usage output."""
        tmux = _make_tmux(
            send_results=[True, False], pane_text=[IDLE_PANE, _overlay_fixture()]
        )
        safe_reply_mock = await _run("usage_command", tmux)

        safe_reply_mock.assert_awaited_once()
        args, _ = safe_reply_mock.call_args
        # v5: the honest safety text is preserved VERBATIM, snapshot appended;
        # never presented as usage output.
        assert args[1].startswith(SEND_FAILED_TEXT)
        assert "Total cost:" not in args[1]

    @pytest.mark.asyncio
    async def test_successful_sends_present_usage_output(self):
        tmux = _make_tmux(send_results=True, pane_text=[IDLE_PANE, _overlay_fixture()])
        safe_reply_mock = await _run("usage_command", tmux)

        safe_reply_mock.assert_awaited_once()
        args, _ = safe_reply_mock.call_args
        assert "Total cost:" in args[1]
        assert tmux.send_keys.await_count == 2


class TestCostCommand:
    """`/cost` is intercepted bot-side (alias of /usage) — same overlay scaffold."""

    @pytest.mark.asyncio
    async def test_cost_sends_slash_cost_and_dismisses_open_overlay(self):
        """/cost sends the "/cost" overlay command then an Escape to dismiss it."""
        tmux = _make_tmux(send_results=True, pane_text=[IDLE_PANE, _overlay_fixture()])
        safe_reply_mock = await _run("cost_command", tmux)

        # First key is "/cost" (NOT "/usage"); second is the dismiss Escape.
        calls = tmux.send_keys.await_args_list
        assert calls[0].args[1] == "/cost"
        assert calls[1].args[1] == "Escape"
        assert tmux.send_keys.await_count == 2
        safe_reply_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cost_parse_miss_on_open_overlay_fails_open_with_note_and_raw(self):
        """An overlay frame whose body can't be parsed still gets dismissed
        (chrome IS present) + a fail-open reply carrying the raw capture."""
        # Overlay chrome only — rule + tab bar + footer, EMPTY body → the parser
        # detects the overlay but extracts no lines.
        chrome_only = (
            "▔" * 80
            + "\n   Settings  Status   Config   Usage   Stats\n"
            + "   Esc to cancel\n"
        )
        tmux = _make_tmux(send_results=True, pane_text=[IDLE_PANE, chrome_only])
        safe_reply_mock = await _run("cost_command", tmux)

        # The overlay was still dismissed (Escape sent) even on a parse miss.
        assert tmux.send_keys.await_args_list[1].args[1] == "Escape"
        args, _ = safe_reply_mock.call_args
        assert "Couldn't parse the cost screen" in args[1]
        assert "Settings" in args[1]  # the raw capture rides along

    @pytest.mark.asyncio
    async def test_cost_parses_real_overlay_fixture(self):
        """A real 2.1.206 overlay capture is parsed to readable body lines."""
        tmux = _make_tmux(send_results=True, pane_text=[IDLE_PANE, _overlay_fixture()])
        safe_reply_mock = await _run("cost_command", tmux)

        args, _ = safe_reply_mock.call_args
        assert "Total cost:" in args[1]
        assert "56% used" in args[1]
        # A clean parse does NOT prepend the fail-open note.
        assert "Couldn't parse" not in args[1]
