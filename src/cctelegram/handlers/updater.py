"""``/update`` orchestration — update the Claude Code CLI + restart idle sessions.

Owner-only manual command flow (no scheduler). ``run_update`` (1) updates the
Claude Code CLI binary via ``<claude executable> update``, (2) walks every bound
topic and restarts each genuinely-IDLE session IN PLACE inside its existing tmux
window (preserving the window id, via ``tmux_manager.restart_claude_in_window``)
so it adopts the new on-disk version, (3) defers busy sessions, and (4) reports a
progressive summary.

Fail-closed + non-regressive: only ``IDLE_CLEARED`` + pane-idle routes are
restarted (busy / waiting / background-agent routes defer), restarts run
SEQUENTIALLY under each window's send lock, and a single-flight guard rejects a
concurrent ``/update``. Collaborators (session manager, tmux manager, monitor)
are injected so the flow is unit-testable with fakes.

Key entry point: ``run_update``.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .. import route_runtime, terminal_parser
from ..monitor_state import TrackedSession
from ..tmux_manager import SHELL_WAIT_TOTAL_S, RestartOutcome

logger = logging.getLogger(__name__)

# Module-level single-flight guard (P2-B). The check + set below have NO await
# between them, so they are atomic on the event loop — a genuine try-acquire that
# rejects a second concurrent ``/update`` rather than queuing it.
_update_running = False

# Hard ceiling on the ``claude update`` subprocess (Hermes P2) — a stalled
# update must not hang the whole command / wedge the single-flight guard.
_CLI_UPDATE_TIMEOUT_S = 120.0

# SKIPPED_NO_EXIT aftermath disclosure (P2-1). ``/exit`` was already sent when
# the shell-wait expired, so the window is in an UNKNOWN state: either Claude is
# still alive (harmless), or it exits later and leaves a bare shell in the
# still-BOUND topic — whose next Telegram message would be typed into (and
# executed by) that shell. The summary must say so, not just "didn't exit".
_NO_EXIT_REASON = (
    f"sent /exit but the pane didn't drop to a shell within {SHELL_WAIT_TOTAL_S:.0f}s "
    "— the session may be dead; check the window before sending messages"
)

# Bounded stat-until-stable window for the post-relaunch EOF registration: the
# resume replay may still be appending when the first stat lands, so re-stat
# until the size is unchanged across two consecutive stats (hard-capped). The
# cap expiring just uses the LAST observed size — the offset is always a real
# stat of the file, so ``offset <= filesize`` holds regardless.
_SETTLE_STAT_INTERVAL_S = 0.3
_SETTLE_STAT_MAX_WAIT_S = 5.0


def reset_for_tests() -> None:
    """Clear the single-flight guard (test isolation seam)."""
    global _update_running
    _update_running = False


async def _run_cli_update(claude_command: str) -> str:
    """Run ``<claude executable> update`` (non-fatal) and return a status line.

    The update executable is ``shlex.split(claude_command)[0]`` — NEVER the raw
    ``claude_command`` string appended with ``update`` (P2-2 / Hermes P2-2:
    ``CLAUDE_COMMAND`` may carry flags like ``--dangerously-skip-permissions`` or
    be a wrapper, so ``"<claude_command> update"`` would be malformed). Run via
    ``create_subprocess_exec`` (NO ``shell=True``). A non-zero exit is non-fatal:
    the auto-updater may already have refreshed the symlink, and the subsequent
    in-place restart adopts whatever version is on disk.
    """
    parts = shlex.split(claude_command)
    if not parts:
        return (
            "⚠️ CLAUDE_COMMAND is empty — skipped the binary update; "
            "restarting onto the on-disk version."
        )
    executable = parts[0]
    try:
        proc = await asyncio.create_subprocess_exec(
            executable,
            "update",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as e:
        logger.warning("claude update failed to launch (%s)", e)
        return (
            f"⚠️ Could not run '{executable} update' ({e}); "
            "restarting onto the on-disk version."
        )
    # Bound the wait (Hermes P2): a stalled ``claude update`` must never hang
    # the whole command and wedge the single-flight guard. On timeout, kill +
    # reap the process and continue NON-FATAL onto the on-disk version.
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_CLI_UPDATE_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning(
            "claude update timed out after %ss; killing", _CLI_UPDATE_TIMEOUT_S
        )
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001 — best-effort reap; never fatal
            pass
        return (
            f"⚠️ '{executable} update' timed out after {_CLI_UPDATE_TIMEOUT_S}s; "
            "restarting onto the on-disk version."
        )
    out = (
        stdout.decode("utf-8", errors="replace")
        + "\n"
        + stderr.decode("utf-8", errors="replace")
    ).strip()
    tail = out.splitlines()[-1].strip() if out else ""
    if proc.returncode == 0:
        return "✅ CLI update complete." + (f" {tail[:120]}" if tail else "")
    logger.warning("claude update exited %s: %s", proc.returncode, out[:500])
    return (
        f"⚠️ '{executable} update' exited {proc.returncode}; "
        "restarting onto the on-disk version."
    )


def _format_summary(
    restarted: list[str],
    deferred: list[str],
    skipped: list[tuple[str, str]],
) -> str:
    """Render the final ``♻️ Restarted N · deferred M · skipped K`` summary."""

    def _names(items: list[str]) -> str:
        return ", ".join(items) if items else "—"

    lines = [
        f"♻️ Restarted {len(restarted)} idle: {_names(restarted)}",
        f"⏸ Deferred {len(deferred)} busy: {_names(deferred)}",
    ]
    if skipped:
        lines.append(
            f"⚠️ Skipped {len(skipped)}: "
            + ", ".join(f"{name} ({reason})" for name, reason in skipped)
        )
    return "\n".join(lines)


async def run_update(
    *,
    report: Callable[[str], Awaitable[None]],
    session_mgr: Any,
    tmux: Any,
    monitor: Any,
    claude_command: str,
    md_settings: str,
) -> None:
    """Update the CLI, then restart every idle bound session in place.

    ``report`` is an async callback the caller wires to a progressive Telegram
    message (edit-in-place). ``monitor`` may be ``None`` (routing re-association
    then skips the offset registration; the restart still relaunches).
    """
    global _update_running
    if _update_running:
        await report("⏳ An update is already in progress — try again shortly.")
        return
    _update_running = True
    try:
        await report("🔄 Updating Claude Code CLI…")
        version_line = await _run_cli_update(claude_command)
        await report(f"{version_line}\n♻️ Restarting idle sessions…")

        restarted: list[str] = []
        deferred: list[str] = []
        skipped: list[tuple[str, str]] = []

        # Snapshot the bindings up front — restarts don't mutate bindings, but a
        # concurrent topic close could; iterate a stable list.
        for user_id, thread_id, window_id in list(session_mgr.iter_thread_bindings()):
            route: route_runtime.Route = (user_id, thread_id or 0, window_id)
            name = session_mgr.get_display_name(window_id)

            # Cheap pre-gate: obviously-busy routes never touch tmux. Also
            # auto-defers a background-agent (projected RUNNING) or a pending
            # prompt (projected WAITING) route — free protection.
            snap = route_runtime.snapshot(route)
            if snap.run_state is not route_runtime.RunState.IDLE_CLEARED:
                deferred.append(name)
                continue

            w = await tmux.find_window_by_id(window_id)
            if w is None:
                skipped.append((name, "window gone"))
                continue

            ws = session_mgr.get_window_state(window_id)
            tracked_sid = ws.session_id
            if not tracked_sid:
                skipped.append((name, "no session id"))
                continue

            # Isolate per-window failures (Hermes P2): a raise in ONE restart
            # must NOT abort the loop and strand later windows / drop the
            # summary. Bucket as an error and CONTINUE.
            try:
                outcome = await _restart_one(
                    route=route,
                    window_id=window_id,
                    tracked_sid=tracked_sid,
                    md_settings=md_settings,
                    claude_command=claude_command,
                    session_mgr=session_mgr,
                    tmux=tmux,
                    monitor=monitor,
                )
            except Exception:  # noqa: BLE001 — one window can't sink the sweep
                logger.exception(
                    "update: restart of window %s (%s) raised", window_id, name
                )
                skipped.append((name, "restart error"))
                continue
            if outcome is RestartOutcome.RESTARTED:
                restarted.append(name)
            elif outcome in (
                RestartOutcome.SKIPPED_BUSY_LOCKED,
                RestartOutcome.SKIPPED_NOT_IDLE,
            ):
                deferred.append(name)
            elif outcome is RestartOutcome.SKIPPED_NO_EXIT:
                skipped.append((name, _NO_EXIT_REASON))
            else:  # RestartOutcome.ERROR
                skipped.append((name, "restart error"))

        await report(f"{version_line}\n{_format_summary(restarted, deferred, skipped)}")
    finally:
        _update_running = False


async def route_is_idle(route: route_runtime.Route, window_id: str, tmux: Any) -> bool:
    """Authoritative idle gate: run-state IDLE_CLEARED AND pane idle at the box.

    Run separately BEFORE quitting Claude (inside the send lock). Run-state can
    LAG a pane that just started a new generation, so the visible-pane
    ``pane_looks_idle`` ground-truth is the required second gate.
    """
    if (
        route_runtime.snapshot(route).run_state
        is not route_runtime.RunState.IDLE_CLEARED
    ):
        return False
    pane = await tmux.capture_pane(window_id)
    return terminal_parser.pane_looks_idle(pane)


async def _settled_file_size(
    file_path: Any, *, interval_s: float, max_wait_s: float
) -> int | None:
    """Stat ``file_path`` until its size is STABLE (unchanged across two
    consecutive stats), bounded by ``max_wait_s``.

    A still-growing file at cap expiry yields its LAST observed size — every
    return value is a real stat of the file, so the caller's
    ``offset <= filesize`` invariant holds either way. Returns ``None`` when a
    stat fails (caller skips the registration, matching the prior single-stat
    behavior).
    """
    try:
        last = file_path.stat().st_size
    except OSError:
        return None
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        await asyncio.sleep(interval_s)
        try:
            cur = file_path.stat().st_size
        except OSError:
            return None
        if cur == last:
            return cur
        last = cur
    return last


async def reassociate_routing(
    session_mgr: Any,
    monitor: Any,
    window_id: str,
    tracked_sid: str,
    *,
    settle_interval_s: float = _SETTLE_STAT_INTERVAL_S,
    settle_max_wait_s: float = _SETTLE_STAT_MAX_WAIT_S,
) -> None:
    """Re-associate routing after an in-place relaunch (ws override + offset).

    (a) Resume-override — force ``ws.session_id`` back to ``tracked_sid`` if the
    SessionStart hook diverged. Near-no-op on CC 2.1.20x (the ``--resume`` id is
    STABLE, A.0), kept defensive to mirror the proven directory-browser path.

    (b) Register the monitor at the POST-RELAUNCH stat-stable EOF: a small
    bounded stat-until-stable loop (``_settled_file_size`` — re-stat every
    ``settle_interval_s`` until the size is unchanged across two consecutive
    stats, hard-capped at ``settle_max_wait_s``; a still-growing file at the cap
    uses the LAST observed size). The idle session's transcript is fully
    consumed, so registering at that EOF skips the small resume-startup append
    AND guarantees ``offset <= filesize`` (the offset is always a real stat) —
    a truncating/rewriting replay can never leave ``tracked_offset >
    filesize``, which would reset the offset to 0 and re-deliver the whole
    transcript (``session_monitor._read_new_lines``).
    """
    ws = session_mgr.get_window_state(window_id)
    if ws.session_id != tracked_sid:
        logger.info(
            "update restart: window %s session_id %s -> %s",
            window_id,
            ws.session_id,
            tracked_sid,
        )
        ws.session_id = tracked_sid
        session_mgr._save_state()

    if monitor is None:
        return
    file_path = session_mgr._build_session_file_path(tracked_sid, ws.cwd)
    if file_path is None:
        return
    settled_eof = await _settled_file_size(
        file_path, interval_s=settle_interval_s, max_wait_s=settle_max_wait_s
    )
    if settled_eof is None:
        return
    existing = monitor.state.get_session(tracked_sid)
    if existing is None:
        monitor.register_session(tracked_sid, file_path, offset=settled_eof)
    else:
        # Already tracked (the stable-id case): advance to the settled EOF. Also
        # clamps a stale offset > filesize so the reset-to-0 flood never fires.
        monitor.state.update_session(
            TrackedSession(
                session_id=tracked_sid,
                file_path=str(file_path),
                last_byte_offset=settled_eof,
                parent_session_id=existing.parent_session_id,
            )
        )
        monitor.state.save_if_dirty()


async def _restart_one(
    *,
    route: route_runtime.Route,
    window_id: str,
    tracked_sid: str,
    md_settings: str,
    claude_command: str,
    session_mgr: Any,
    tmux: Any,
    monitor: Any,
) -> RestartOutcome:
    """Restart ONE window, supplying the idle-recheck + re-association closures."""

    async def _idle_recheck() -> bool:
        return await route_is_idle(route, window_id, tmux)

    async def _reassociate() -> None:
        await reassociate_routing(session_mgr, monitor, window_id, tracked_sid)

    return await tmux.restart_claude_in_window(
        window_id,
        tracked_sid,
        md_settings,
        claude_command=claude_command,
        idle_recheck=_idle_recheck,
        reassociate=_reassociate,
    )
