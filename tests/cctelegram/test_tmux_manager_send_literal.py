"""Tests for the literal-send path of TmuxManager.send_keys (finding 1).

libtmux's ``pane.send_keys(..., literal=True)`` runs ``tmux send-keys -l <text>``
with NO ``--`` end-of-options separator and never checks stderr, so any payload
starting with ``-`` (a bullet list, ``--continue``) makes tmux exit 1 with
"invalid flag" while the wrapper returns True — silent message loss.

The fix sends literal text via the raw ``pane.cmd("send-keys", "-l", "--", text)``
and treats non-empty stderr as failure (return False). Non-literal (key-name)
sends stay on the libtmux convenience path unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from cctelegram.tmux_manager import TmuxManager


class FakePane:
    """Records both raw ``cmd`` invocations and libtmux ``send_keys`` calls."""

    def __init__(self, stderr: list[str] | None = None) -> None:
        self.cmd_calls: list[tuple[Any, ...]] = []
        self.send_keys_calls: list[tuple[str, bool, bool]] = []
        self._stderr = stderr or []

    def cmd(self, *args: Any) -> SimpleNamespace:
        self.cmd_calls.append(args)
        return SimpleNamespace(stderr=list(self._stderr), stdout=[])

    def send_keys(self, text: str, enter: bool = True, literal: bool = True) -> None:
        self.send_keys_calls.append((text, enter, literal))


def _manager_with_pane(pane: FakePane) -> TmuxManager:
    manager = TmuxManager(session_name="test-session")
    window = SimpleNamespace(active_pane=pane)
    session = SimpleNamespace(
        windows=SimpleNamespace(get=lambda **kw: window),
    )
    manager.get_session = lambda: session  # type: ignore[method-assign]
    return manager


def _literal_cmd_calls(pane: FakePane) -> list[tuple[Any, ...]]:
    return [c for c in pane.cmd_calls if c[0] == "send-keys"]


@pytest.mark.asyncio
async def test_literal_no_enter_leading_dash_uses_separator() -> None:
    """A leading-``-`` payload is passed AFTER ``--`` and returns True."""
    pane = FakePane()
    manager = _manager_with_pane(pane)

    ok = await manager.send_keys("@1", "- hello", enter=False, literal=True)

    assert ok is True
    assert _literal_cmd_calls(pane) == [("send-keys", "-l", "--", "- hello")]
    # The unsafe libtmux convenience path must not be used for literal text.
    assert pane.send_keys_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["--continue", "--", "-v", "a -- b --x"])
async def test_literal_with_enter_dash_payloads_pass_verbatim(payload: str) -> None:
    """Dash-shaped payloads go through ``--`` verbatim; Enter still follows."""
    pane = FakePane()
    manager = _manager_with_pane(pane)

    with patch("asyncio.sleep", new=AsyncMock()):
        ok = await manager.send_keys("@1", payload, enter=True, literal=True)

    assert ok is True
    assert _literal_cmd_calls(pane) == [("send-keys", "-l", "--", payload)]
    # Enter is sent separately via the (non-literal) libtmux path — preserved.
    assert pane.send_keys_calls == [("", True, False)]


@pytest.mark.asyncio
async def test_literal_stderr_returns_false() -> None:
    """Non-empty stderr from tmux → log + return False (no silent success)."""
    pane = FakePane(stderr=["command send-keys: invalid flag -"])
    manager = _manager_with_pane(pane)

    ok = await manager.send_keys("@1", "- hello", enter=False, literal=True)

    assert ok is False


@pytest.mark.asyncio
async def test_literal_enter_stderr_skips_enter() -> None:
    """A failed literal send must NOT be followed by a bare Enter."""
    pane = FakePane(stderr=["command send-keys: invalid flag -"])
    manager = _manager_with_pane(pane)

    with patch("asyncio.sleep", new=AsyncMock()):
        ok = await manager.send_keys("@1", "- hello", enter=True, literal=True)

    assert ok is False
    # No Enter send after the failed text send.
    assert pane.send_keys_calls == []


@pytest.mark.asyncio
async def test_bang_mode_segments_both_use_separator() -> None:
    """Claude Code ``!`` bash-mode split: both segments use the raw -l -- path."""
    pane = FakePane()
    manager = _manager_with_pane(pane)

    with patch("asyncio.sleep", new=AsyncMock()):
        ok = await manager.send_keys("@1", "!-x", enter=True, literal=True)

    assert ok is True
    assert _literal_cmd_calls(pane) == [
        ("send-keys", "-l", "--", "!"),
        ("send-keys", "-l", "--", "-x"),
    ]
    assert pane.send_keys_calls == [("", True, False)]


@pytest.mark.asyncio
async def test_nonliteral_key_send_stays_on_libtmux_path() -> None:
    """Key-name sends (literal=False) keep the existing libtmux call."""
    pane = FakePane()
    manager = _manager_with_pane(pane)

    ok = await manager.send_keys("@1", "Escape", enter=False, literal=False)

    assert ok is True
    assert pane.cmd_calls == []
    assert pane.send_keys_calls == [("Escape", False, False)]


@pytest.mark.asyncio
async def test_literal_send_exception_returns_false() -> None:
    """The existing exception guard is preserved on the raw-cmd path."""

    class RaisingPane(FakePane):
        def cmd(self, *args: Any) -> SimpleNamespace:
            raise RuntimeError("tmux gone")

    pane = RaisingPane()
    manager = _manager_with_pane(pane)

    ok = await manager.send_keys("@1", "- hello", enter=False, literal=True)

    assert ok is False
