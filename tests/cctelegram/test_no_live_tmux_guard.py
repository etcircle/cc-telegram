"""The suite's own live-tmux backstop must cover a FRESH TmuxManager too.

Review r9 P2-B: the guard poisoned only the singleton's already-cached
``_server``, so a test that constructed its own ``TmuxManager()`` reached the
real tmux server through every libtmux route (``send_keys`` / ``kill_window`` /
``rename_window`` / ``create_window``, all via ``asyncio.to_thread``).
"""

from __future__ import annotations

import asyncio

import pytest

from cctelegram import tmux_manager as tmux_mod


def test_a_fresh_manager_cannot_acquire_a_real_libtmux_server() -> None:
    fresh = tmux_mod.TmuxManager()
    with pytest.raises(AssertionError, match="REAL libtmux Server"):
        _ = fresh.server


def test_the_singletons_cached_server_is_poisoned_too() -> None:
    with pytest.raises(AssertionError, match="REAL tmux server"):
        _ = tmux_mod.tmux_manager.server.sessions


@pytest.mark.asyncio
async def test_the_tmux_binary_is_refused_by_argv() -> None:
    with pytest.raises(RuntimeError, match="live tmux blocked in tests"):
        await asyncio.create_subprocess_exec("tmux", "list-windows")
    with pytest.raises(RuntimeError, match="live tmux blocked in tests"):
        await asyncio.create_subprocess_exec("/opt/homebrew/bin/tmux", "kill-window")
    # bytes argv must be covered as well
    with pytest.raises(RuntimeError, match="live tmux blocked in tests"):
        await asyncio.create_subprocess_exec(b"tmux", b"list-windows")
    # Review r10 P3-B: the tmux binary is not always the FIRST shell token.
    for shell_cmd in (
        "tmux list-windows",
        "env tmux list-windows",
        "command tmux kill-window",
        "true; tmux kill-window",
        "echo hi && tmux kill-server",
        "(tmux list-windows)",
    ):
        with pytest.raises(RuntimeError, match="live tmux blocked in tests"):
            await asyncio.create_subprocess_shell(shell_cmd)


@pytest.mark.asyncio
async def test_other_subprocesses_are_untouched() -> None:
    """The poison is scoped by argv — the suite still spawns real interpreters."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/echo", "ok", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    assert out.strip() == b"ok"
