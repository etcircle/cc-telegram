"""Tmux session/window management via libtmux.

Wraps libtmux to provide async-friendly operations on a single tmux session:
  - list_windows / find_window_by_name: discover Claude Code windows.
  - capture_pane: read terminal content (plain or with ANSI colors).
  - send_keys: forward user input or control keys to a window.
  - create_window / kill_window: lifecycle management.
  - resize_window / creation-time resize: machine-surface geometry
    (config.window_width x window_height, default 160x50) so tall AUQ
    pickers render fully for the parser.

All blocking libtmux calls are wrapped in asyncio.to_thread().

Performance:
  - shutil.which("tmux") is cached process-wide. libtmux's tmux_cmd
    constructor (libtmux/common.py) calls it on every command; py-spy showed
    PATH-walking accounted for ~25% of CPU under 1Hz × 8-binding polling.
  - list_windows() has a 1s TTL cache so the 8 concurrent gather() callers
    in status_poll_loop coalesce to a single tmux subprocess per cycle.

Concurrency (Wave 3a):
  - window_send_lock(window_id) is a per-window asyncio.Lock registry that
    serializes multi-keystroke transactions to one pane: the text→settle→Enter
    send in SessionManager.send_to_window, and the nav→verify→Enter→confirm
    critical section of the AUQ pick dispatch. Lifecycle: an entry is dropped
    on kill_window ONLY; a stale entry for an externally-vanished or
    topic-closed window is harmless (an asyncio.Lock with no holders) and
    bounded by tmux window-id reuse — the next claimant of a reused id simply
    inherits an idle lock. The lock is a LEAF: holders must never acquire
    route locks / route_runtime / message_queue internals while holding it,
    and (with the single exception of an already-in-flight callback answer)
    no Telegram I/O may run while it is held.

    NAMED EXCEPTION to the leaf rule (GH #50, plan §1.5 — the ONLY one):
    ``SessionManager.deliver_to_window`` invokes exactly one SYNCHRONOUS
    ``message_queue.set_route_user_turn_at`` (→ ``route_runtime.stamp_user_turn``)
    while holding this lock — the narrowly-typed ``delivery.UserTurnStamp``
    pre-commit hook, fired after every delivery gate passes and immediately
    before the Enter. It exists so a REFUSED send is never stamped (the
    turn-boundary the live-prose freshness gate and the dashboard 🔔 derivation
    depend on must mark an actual delivery). The hook may not await, may not
    schedule work, and may not mutate anything else; a raise fails CLOSED
    (draft_written, no Enter, no stamp). Any WIDENING of this exception is a
    contract change, not a refactor.
  - mark/clear/window_quarantined is the post-/exit quarantine registry
    (Hermes P1): a /update restart that irrevocably sent /exit but could not
    CONFIRM a relaunch (the shell-wait expired, the relaunch keystroke send
    failed, or the post-relaunch confirm poll never observed Claude — r2
    P1-A) quarantines the window, and SessionManager.send_to_window re-checks
    pane_current_command before typing user text into it (a bare shell would
    EXECUTE the message). Proof of life is STRICTLY pane_command_is_claude
    (the A.0 version-string shape) — "not a shell" is not proof (vim/python/
    ssh in the stranded pane must keep refusing; r2 P1-B). In-memory only;
    cleared on that positive proof, a later CONFIRMED restart, kill_window,
    and the topic teardown seams.
  - mark/clear/window_has_stranded_draft is the GH #50 stranded-draft brake
    (r2 F2), the quarantine's sibling: a gated delivery that WROTE its payload
    but withheld the Enter leaves that text sitting in the pane's input box,
    so the next payload would be APPENDED to it and its Enter would commit
    BOTH. session.deliver_to_window refuses while the brake is up. It lives
    HERE, beside the quarantine, because it is a property of the PANE, not of
    a topic binding — and therefore it may only be released by (a) positive
    proof the input row is EMPTY (session's own self-heal) or (b) proof the
    WINDOW IS DEAD: a CONFIRMED kill_window, or the creation of a brand-new
    window under that id. Topic-level teardown (/unbind, topic close, a
    stale-binding unbind) DELIBERATELY does NOT clear it — unbinding a topic
    says nothing about whether the draft is still in the box, and /unbind
    leaves the window ALIVE (see the "brake lifecycle" comment below).

  - window_lifecycle_lock is the GH #65 (review r12) WINDOW-LIFECYCLE LOCK: a
    single process-wide asyncio.Lock that SERIALIZES window REGISTRATION-OF-A-
    KILL against window ADOPTION. Every gate before it was check-then-act — a
    kill could register after an adopter's gate check and land after its
    verification — so the two are now mutually exclusive rather than merely
    ordered. Held by: kill_window (to register the pending mark and dispatch
    the executor call — the mark OUTLIVES the hold, and the done-callback still
    clears it), create_window (across gate-check → tmux new-window →
    verification listing), and both bind seams (the directory browser's
    bind-to-existing and the trust lane's completion bind) across
    revalidate → bind commit, which are synchronous dict writes and therefore
    a cheap hold.

    LOCK ORDER — the lifecycle lock is INNERMOST. A holder must never await
    the trust-flow creation lock, a route lock, or ANY Telegram I/O while
    holding it. The bounded settlement wait (await_kill_settled /
    await_all_kills_settled, 10s) lives OUTSIDE the lock: wait first, then
    acquire, re-check, commit. Acquiring it while holding a per-window send
    lock is likewise forbidden.

Key class: TmuxManager (singleton instantiated as `tmux_manager`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shlex
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

# Cache the resolved tmux binary path before libtmux is used. Every
# libtmux command (libtmux/common.py:tmux_cmd.__init__) calls
# shutil.which("tmux"), which on a 1Hz × 8-binding poller burns enormous CPU
# walking $PATH. Patch shutil.which itself rather than libtmux.common.shutil
# — libtmux/server.py and libtmux/common.py each `import shutil`, so
# attribute-patching one module would miss the other. One module-level patch
# covers all of them.
_TMUX_BIN: str | None = shutil.which("tmux")
_orig_shutil_which = shutil.which


def _cached_shutil_which(
    cmd: str,
    mode: int = os.F_OK | os.X_OK,
    path: str | None = None,
) -> str | None:
    if cmd == "tmux" and _TMUX_BIN is not None:
        return _TMUX_BIN
    return _orig_shutil_which(cmd, mode=mode, path=path)


# NOTE: this is a process-wide patch of shutil.which, not scoped to libtmux.
# Other libraries that call shutil.which (for any binary other than "tmux")
# pass through to _orig_shutil_which unchanged. A stale _TMUX_BIN (e.g. tmux
# reinstalled to a new path mid-process) is only refreshed on bot restart.
shutil.which = _cached_shutil_which  # type: ignore[assignment]

import libtmux  # noqa: E402  (must follow the shutil patch)

from .config import SENSITIVE_ENV_VARS, config  # noqa: E402

if TYPE_CHECKING:
    from .terminal_parser import PaneCapture

logger = logging.getLogger(__name__)

# GH #65 review r13/r15.
# P1-A: how many times a generation-matched refresh retries before giving up.
# Each attempt only loses to a genuine concurrent invalidation, so a small bound
# is ample; the surrounding lifecycle timeout bounds the wall clock.
_LIST_REFRESH_MAX_ATTEMPTS: Final[int] = 5
# P1-B: every tmux await taken UNDER the window-lifecycle lock is bounded by
# this, so one wedged tmux operation cannot block the lifecycle of every other
# window — including a forced trust cleanup or a topic teardown. Generous
# enough for a real ``new-window`` + Claude launch on a loaded machine.
LIFECYCLE_TMUX_TIMEOUT_S: Final[float] = 30.0
# P1-B/P1-D: the most bounded tmux operations any SINGLE lifecycle-lock
# acquisition performs. Today the worst case is 2, at two sites:
#   * ``create_window``            — the create worker, then the verification
#                                    listing;
#   * the directory bind-to-existing — the fresh existence probe, then the
#                                    unbound listing.
# (The trust completion bind and the legacy bind each perform 1.)
# A new bounded await added under an existing hold MUST be reflected here; the
# wave-14 pin test recomputes this from the source and fails if it drifts.
_MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD: Final[int] = 2
# When True, exceeding that ceiling RAISES instead of logging. Armed by the test
# suite so a hold that grows fails loudly rather than silently invalidating the
# derived kill bound.
_STRICT_LIFECYCLE_INVARIANTS: bool = False


def enable_strict_lifecycle_invariants() -> None:
    """Make a bounded-ops ceiling breach RAISE (test posture)."""
    global _STRICT_LIFECYCLE_INVARIANTS
    _STRICT_LIFECYCLE_INVARIANTS = True


# P1-B: how long ``kill_window`` waits to ACQUIRE the lifecycle lock before
# giving up with an honest failure instead of queueing indefinitely.
#
# DERIVED from the worst-case LAWFUL hold, not hand-tuned (review r14 P1-D): a
# single acquisition may legitimately run several bounded operations back to
# back, so the true ceiling is ops × per-op bound, not one per-op bound. With
# the kill bound below that cumulative figure, an ordinary busy creation would
# make kills fail spuriously — the exact outcome this bound exists to prevent.
# The margin covers the synchronous work between the bounded awaits.
KILL_LOCK_TIMEOUT_S: Final[float] = (
    _MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD * LIFECYCLE_TMUX_TIMEOUT_S
) + 15.0


# P1-C: the message ``create_window`` returns when the window WAS created but
# its verification could not complete. The tuple carries the REAL window id (not
# an empty one), so the caller can reserve or clean the window that exists
# rather than losing track of it.
CREATED_BUT_UNVERIFIED_MESSAGE: Final[str] = (
    "The window was created but could not be verified. Please check tmux."
)


class CallerDisposition(Enum):
    """What the CALLER decided about a window creation (review r16 P1-A)."""

    UNDECIDED = "undecided"
    TAKEN = "taken"
    DECLINED = "declined"


class _CreateDisposition:
    """THE TWO-PARTY HANDSHAKE for a created window's fate.

    Review r16 P1-A. The r15 shape had the worker-completion callback decide
    alone: it fired when the worker finished — which can be BEFORE the shielded
    waiter resumes — read ``taken_by_caller`` as still False, and scheduled a
    kill of a window the caller was about to return successfully. Partial
    creation could get TWO cleanup owners the same way.

    The fix is that neither party may decide alone. Reaping needs BOTH facts:
    the worker's outcome AND the caller's disposition. Whichever party learns
    the second fact performs the reap, exactly once:

      * the worker callback records the outcome, and reaps ONLY if the caller
        has ALREADY declined;
      * the caller always records its disposition in a ``finally`` — TAKEN on
        the successful-return path (set BEFORE the id is handed back), DECLINED
        on timeout, cancellation and refusal — and a DECLINED recorded after the
        worker is done triggers the reap from the caller's side.

    Contracts: a successful creation is NEVER reaped; a cancellation reaps
    EXACTLY once; a partial creation has EXACTLY one cleanup owner.
    """

    def __init__(self) -> None:
        self.caller: CallerDisposition = CallerDisposition.UNDECIDED
        self.worker_done: bool = False
        self.worker_window_id: str = ""
        self._reaped: bool = False

    def _should_reap(self) -> bool:
        """True exactly once, and only when BOTH facts are known."""
        if self._reaped:
            return False
        if not self.worker_done or not self.worker_window_id:
            return False
        if self.caller is not CallerDisposition.DECLINED:
            return False
        self._reaped = True
        return True

    def record_worker(self, window_id: str) -> bool:
        """The worker finished. Returns True if THIS party must reap."""
        self.worker_done = True
        self.worker_window_id = window_id
        return self._should_reap()

    def record_caller(self, disposition: CallerDisposition) -> bool:
        """The caller decided. Returns True if THIS party must reap.

        A disposition is recorded ONCE — the first decision wins, so a
        ``finally`` that defaults to DECLINED can never overwrite the TAKEN the
        success path already recorded.
        """
        if self.caller is CallerDisposition.UNDECIDED:
            self.caller = disposition
        return self._should_reap()


class _LifecycleLock:
    """The window-lifecycle lock, with per-HOLD bounded-op accounting.

    Review r15 P2-B. The ceiling is about how long ONE acquisition can lawfully
    last, which is a CUMULATIVE property — two sequential 30 s operations make a
    60 s hold — not a nesting depth. Counting had to reset when a hold BEGINS
    and be summarised when it ENDS, so the lock is wrapped rather than handed
    out raw. It proxies the whole surface the call sites use (``async with``,
    ``acquire`` / ``release`` for the bounded kill acquisition, and ``locked``).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.ops_in_hold: int = 0
        self.ops_high_water: int = 0

    def locked(self) -> bool:
        return self._lock.locked()

    async def acquire(self) -> bool:
        acquired = await self._lock.acquire()
        self.ops_in_hold = 0
        return acquired

    def release(self) -> None:
        self.ops_high_water = max(self.ops_high_water, self.ops_in_hold)
        self.ops_in_hold = 0
        self._lock.release()

    def note_bounded_op(self) -> int:
        """Record one bounded operation against the current hold."""
        self.ops_in_hold += 1
        self.ops_high_water = max(self.ops_high_water, self.ops_in_hold)
        return self.ops_in_hold

    def reset_accounting(self) -> None:
        self.ops_in_hold = 0
        self.ops_high_water = 0

    async def __aenter__(self) -> "_LifecycleLock":
        await self.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.release()


class LifecycleTimeout(Exception):
    """A tmux operation under the window-lifecycle lock exceeded its bound.

    Typed so every caller can take its own honest refusal arm — and, critically,
    so the lock is RELEASED on the way out (review r13 P1-B). An unbounded await
    under this lock would let a single wedged tmux call freeze every other
    window's lifecycle.
    """


async def capture_pane_pair(
    tmux: Any, window_id: str, scrollback_lines: int = 0
) -> "PaneCapture | None":
    """Capture a pane WITH ANSI and normalize into a ``(plain, ansi)`` pair.

    GH #54 capture spine: every AUQ-consuming observation captures ANSI once and
    derives ``plain`` via ``terminal_parser.normalize_capture``, so a single
    frame drives BOTH the plain parse (options / layout) AND the ANSI-fed tier-2
    SGR cursor detection without a second tmux round-trip. A non-AUQ consumer of
    the same observation uses ``.plain`` and behaves byte-identically to a plain
    capture (region equality).

    A MODULE-LEVEL free function taking the ``tmux`` object (never a method): it
    only ever calls ``tmux.capture_pane(...)``, so a caller that patches the
    ``tmux_manager`` singleton with a mock exposing only ``capture_pane`` keeps
    working — the spine adds no new mock surface.

    Returns ``None`` on capture failure. On an UNKNOWN control sequence
    ``normalize_capture`` REJECTS the pair; the spine then permits EXACTLY ONE
    plain re-capture for that observation (the sole exception to
    one-capture-per-observation, WARNING-logged with the offending introducer
    byte). A failed re-capture is an ordinary capture failure. The re-captured
    plain frame gives genuinely-today's behavior (the pair's ``ansi`` then
    equals ``plain`` — no styling to read).
    """
    from .terminal_parser import (
        PaneCapture,
        normalize_capture,
        normalize_reject_introducer,
    )

    ansi = await tmux.capture_pane(
        window_id, with_ansi=True, scrollback_lines=scrollback_lines
    )
    if ansi is None:
        return None
    pair = normalize_capture(ansi)
    if pair is not None:
        return pair
    # The introducer reporter shares the normalizer's OWN grammar (wave-2 review
    # P3) so a valid SGR followed by a bare BEL blames the BEL, never the SGR.
    logger.warning(
        "capture_pane_pair: normalize rejected window=%s introducer=%s; "
        "one plain re-capture",
        window_id,
        normalize_reject_introducer(ansi),
    )
    plain = await tmux.capture_pane(
        window_id, with_ansi=False, scrollback_lines=scrollback_lines
    )
    if plain is None:
        return None
    return PaneCapture(plain=plain, ansi=plain)


def _compose_launch_command(
    base_command: str, md_settings_path: str, resume_session_id: str | None
) -> str:
    """Compose the ``claude`` launch command line sent to the pane.

    Appends ``--settings <path>`` (the bot-managed MessageDisplay live-prose
    capture settings — Bug 2) when a path is given, then ``--resume <id>`` when
    resuming. Both injected values are shell-quoted: the string is executed by
    the shell via tmux ``send_keys``, so an unquoted path with a space or shell
    metacharacter would split or be mangled. ``base_command`` is left verbatim
    (it is the trusted ``CLAUDE_COMMAND`` config, which may itself carry flags).
    """
    cmd = base_command
    if md_settings_path:
        cmd = f"{cmd} --settings {shlex.quote(md_settings_path)}"
    if resume_session_id:
        cmd = f"{cmd} --resume {shlex.quote(resume_session_id)}"
    return cmd


# ── GH #65 Fix 0: the per-creation, in-pane CLI version probe ────────────────
#
# The trust card's keystroke license must name the version of the binary THIS
# pane's shell will resolve one second later — not the version a bot-side
# subprocess would resolve (PATH / shell function / wrapper divergence), and not
# a cached value (an auto-update can land between creations). So the probe runs
# IN the created pane, in launch-deferred mode (a fresh interactive shell owned
# by the creation flow, nothing else typed yet), with SHELL-RESOLUTION PARITY
# and NONCE-DELIMITED output.
_RE_ENV_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL
)
# The POSITIVE Claude proof: a wrapper that reports its OWN version fails this
# and the card degrades to display-only. The README already requires
# CLAUDE_COMMAND to exec the real binary; the probe does not trust that.
# EXACT conformance to the shape the real binary prints (review r1 P3-1): a
# bare ``N.N.N`` and nothing else before the literal ``(Claude Code)``. A
# suffixed / prefixed / short version fails closed to display-only rather than
# licensing keystrokes against an un-characterized build.
_RE_PROBE_VERSION_LINE: Final[re.Pattern[str]] = re.compile(
    r"(\d+\.\d+\.\d+)\s+\(Claude Code\)"
)
TRUST_VERSION_PROBE_TIMEOUT_S: Final[float] = 5.0
_PROBE_POLL_INTERVAL_S: Final[float] = 0.25


def probe_command_prefix(base_command: str) -> str | None:
    """The ``<env-prefix> <binary>`` slice of ``CLAUDE_COMMAND``, or None.

    Keeps EVERY leading ``NAME=value`` env assignment (a ``PATH=…`` prefix
    changes which binary the shell resolves — it must apply to the probe too),
    keeps the binary token, DROPS the remaining args. Each assignment is
    re-emitted as an UNQUOTED name plus a shell-quoted value: quoting the whole
    word (``'PATH=/x'``) would stop the shell recognizing it as an assignment.
    Returns None when no binary token can be extracted (⇒ no probe ⇒ the card is
    display-only).
    """
    try:
        tokens = shlex.split(base_command)
    except ValueError:
        return None
    parts: list[str] = []
    index = 0
    while index < len(tokens):
        match = _RE_ENV_ASSIGNMENT.fullmatch(tokens[index])
        if match is None:
            break
        parts.append(f"{match.group(1)}={shlex.quote(match.group(2))}")
        index += 1
    if index >= len(tokens):
        return None
    parts.append(shlex.quote(tokens[index]))
    return " ".join(parts)


def compose_version_probe(base_command: str, nonce_a: str, nonce_b: str) -> str | None:
    """The nonce-delimited ``--version`` probe line, or None when un-probeable."""
    prefix = probe_command_prefix(base_command)
    if prefix is None:
        return None
    return f"printf '{nonce_a}\\n'; {prefix} --version; printf '{nonce_b}\\n'"


def parse_probe_version(pane: str, nonce_a: str, nonce_b: str) -> str | None:
    """Extract the probed CC version from ONE capture, or None (fail closed).

    The delimiter match is an EXACT WHOLE-LINE FULLMATCH of the nonce after
    strip (Phase-0 addendum item 4): the shell ECHOES the probe command, and a
    long binary path WRAPS that echo so nonce-A and nonce-B each end an echoed
    line — a naive "line contains the nonce" scan finds an EMPTY region between
    them. Echoed lines never fullmatch; the ``printf`` output lines always do.

    Between the LAST nonce-A line and the FIRST nonce-B line after it, exactly
    one line must carry the positive ``N.N.N (Claude Code)`` shape; zero or
    several ⇒ None. Only the bare version (``2.1.241``) is returned — that is
    what ``decision_token.lookup`` compares.
    """
    lines = pane.splitlines()
    start = -1
    for index, line in enumerate(lines):
        if line.strip() == nonce_a:
            start = index
    if start == -1:
        return None
    end = -1
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == nonce_b:
            end = index
            break
    if end == -1:
        return None
    found: list[str] = []
    for line in lines[start + 1 : end]:
        match = _RE_PROBE_VERSION_LINE.fullmatch(line.strip())
        if match is not None:
            found.append(match.group(1))
    if len(found) != 1:
        return None
    return found[0]


class RestartOutcome(Enum):
    """Result of an in-place ``restart_claude_in_window`` attempt (``/update``)."""

    RESTARTED = "restarted"  # Claude quit + relaunched (confirmed), re-associated
    SKIPPED_BUSY_LOCKED = "skipped_busy_locked"  # window send lock held
    SKIPPED_NOT_IDLE = "skipped_not_idle"  # idle re-check under the lock failed
    SKIPPED_NO_EXIT = "skipped_no_exit"  # pane never dropped to a shell (fail-closed)
    # Relaunch keystroke typed onto a confirmed-shell pane, but Claude was
    # never OBSERVED running within the confirm budget (r2 P1-A: keystroke
    # acceptance is not launch proof — broken CLAUDE_COMMAND / auth failure /
    # instant crash). The window stays quarantined; a late boot self-heals at
    # the next send's re-check.
    RELAUNCH_UNCONFIRMED = "relaunch_unconfirmed"
    ERROR = "error"  # a keystroke send returned False (window gone?)


# Shells the pane drops to once Claude Code has quit. ``pane_current_command``
# reports the shell NAME at the prompt — possibly login-prefixed ("-zsh") — and
# the Claude Code VERSION string (e.g. "2.1.201") while the TUI runs, NEVER
# "node" (A.0 live capture, CC 2.1.20x). Alternative interactive shells (nu /
# pwsh / xonsh) are included so their users' panes are recognized too. Any value
# NOT in this set is treated as "Claude still running", so the fail-closed
# relaunch gate never launches into a live TUI.
_KNOWN_SHELLS = frozenset(
    {
        "zsh",
        "bash",
        "sh",
        "fish",
        "dash",
        "ksh",
        "tcsh",
        "csh",
        "ash",
        "nu",
        "pwsh",
        "xonsh",
    }
)

# Bounded post-``/exit`` shell-wait budget (P2-1). ``/exit`` is IRREVOCABLE, so
# the wait is TWO-phase: normal quits land well inside the PRIMARY window; the
# GRACE extension recovers a LATE exit with a normal relaunch (INFO-logged).
# Without the grace, a pane dropping to a shell just past the primary window
# was a bare shell in a still-BOUND topic — the next Telegram message would be
# typed into (and executed by) that shell. Both phases expiring returns
# ``SKIPPED_NO_EXIT``; the updater's summary uses ``SHELL_WAIT_TOTAL_S`` to
# disclose the may-be-dead aftermath honestly.
_SHELL_POLL_TIMEOUT_S = 5.0
_SHELL_POLL_GRACE_S = 10.0
SHELL_WAIT_TOTAL_S = _SHELL_POLL_TIMEOUT_S + _SHELL_POLL_GRACE_S

# Bounded post-relaunch confirmation budget (r2 P1-A): after the relaunch
# keystroke is ACCEPTED, poll until the pane reports the Claude version string.
# Keystroke acceptance is not launch proof — a broken CLAUDE_COMMAND / invalid
# auth / instant crash drops straight back to the shell, and clearing the
# quarantine on the keystroke would strand a bare shell with no refusal net.
# Public so the updater's summary can disclose the budget honestly.
RELAUNCH_CONFIRM_TIMEOUT_S = 10.0


def pane_command_is_shell(cmd: str | None) -> bool:
    """True iff ``cmd`` (a ``pane_current_command`` value) is a known shell.

    Login shells report themselves as ``-zsh``; a full path
    (``/bin/zsh``) reduces to its basename. Anything else — the Claude Code
    version string while the TUI runs, ``node``, ``None`` (query failure) — is
    NOT a shell, so the restart gate keeps waiting / fails closed.
    """
    if not cmd:
        return False
    base = os.path.basename(cmd.strip()).lstrip("-")
    return base in _KNOWN_SHELLS


# The A.0 empirical contract on macOS (CC 2.1.20x): while the Claude Code TUI
# runs, ``pane_current_command`` reports its VERSION STRING (e.g. "2.1.201") as
# the process title — never "claude" or "node" on that platform. A version-led
# token; a suffix ("2.1.201-beta") is tolerated, a LEADING name ("v2.1.201",
# "claude 2.1.201") never is.
# VERSION-DRIFT RESIDUAL (fail-closed by design): if a future CC changes the
# reported shape, quarantined sends keep REFUSING — recoverable via a /update
# rerun, a window recreate, or a bot restart. The next TUI-drift audit must
# re-verify this predicate alongside the shell set above.
#
# LINUX / WSL2 (CC 2.1.241 native binary, tmux 3.4): tmux on Linux derives
# ``pane_current_command`` from ``/proc/<pid>/comm`` — the EXECUTABLE NAME,
# not the process title — so the TUI reports "claude", never a version
# string. The binary name is not absolute proof — any executable named
# ``claude`` (or a process that rewrites its comm) would match — but it is the
# same trusted managed-pane signal the quarantine already relies on: the bot
# itself launched ``claude`` in this window, and nothing else is installed
# under that name here. Accepted alongside the version shape on that basis.
# "node" stays excluded on purpose: any Node program would match it.
_RE_CLAUDE_VERSION_CMD = re.compile(r"\d+\.\d+\.\d+\S*")
_CLAUDE_BINARY_NAMES = frozenset({"claude"})


def pane_command_is_claude(cmd: str | None) -> bool:
    """True iff ``cmd`` is POSITIVE proof the Claude Code TUI owns the pane.

    Two shapes count (r2 P1-B + Linux): the version string macOS reports as
    the process title, or the ``claude`` executable name Linux/WSL reports
    from ``/proc/<pid>/comm``. "Not a shell" is NOT proof of life: a user
    checking a stranded window may be running vim / python / ssh there, and
    typing user text + Enter into those is the exact hazard the quarantine
    exists to stop. Used by the quarantined send-seam re-check AND the
    post-relaunch confirmation poll.
    """
    if not cmd:
        return False
    token = cmd.strip()
    if _RE_CLAUDE_VERSION_CMD.fullmatch(token) is not None:
        return True
    return os.path.basename(token) in _CLAUDE_BINARY_NAMES


@dataclass
class TmuxWindow:
    """Information about a tmux window."""

    window_id: str
    window_name: str
    cwd: str  # Current working directory
    pane_current_command: str = ""  # Process running in active pane


class TmuxManager:
    """Manages tmux windows for Claude Code sessions."""

    # list_windows TTL. Status polling runs at 1Hz, so a 1s cache window
    # collapses the 8 concurrent gather() callers in status_poll_loop into a
    # single tmux subprocess per cycle. External tmux mutations (manual
    # kill-window from another pane, Claude process exiting) are picked up
    # within one TTL window; explicit mutations through this manager
    # invalidate immediately.
    _LIST_CACHE_TTL = 1.0

    def __init__(self, session_name: str | None = None):
        """Initialize tmux manager.

        Args:
            session_name: Name of the tmux session to use (default from config)
        """
        self.session_name = session_name or config.tmux_session_name
        self._server: libtmux.Server | None = None
        # list_windows cache, keyed by window_id for O(1) find_window_by_id.
        self._list_cache: dict[str, TmuxWindow] | None = None
        self._list_cache_at: float = 0.0
        # GH #65 review r13 P1-A — THE INVALIDATION GENERATION. Bumped by every
        # ``_invalidate_list_cache``; a refresh may publish its snapshot ONLY if
        # the generation it started under is still current. Without it, a
        # refresh already in flight when a kill invalidated could publish its
        # PRE-KILL snapshot afterwards, and a "fresh" caller waiting on the list
        # lock would then accept that corpse through the fast path.
        self._invalidation_generation: int = 0
        # The generation the PUBLISHED snapshot was started under.
        self._list_cache_generation: int = 0
        # asyncio.Lock is created lazily inside _ensure_list_cache. The
        # global tmux_manager is constructed at module import (before any
        # event loop exists), and tests may run multiple asyncio.run()
        # invocations against the same instance — binding a lock to a
        # specific loop here would explode in those cases.
        self._list_lock: asyncio.Lock | None = None
        # Per-window send locks (see "Concurrency" in the module docstring).
        # Each entry records the event loop it was created under: asyncio.Lock
        # is loop-bound at first acquire, so under tests that run a fresh loop
        # per test against this module singleton a stale entry must be
        # recreated rather than reused (production has exactly one loop, so
        # the loop check never fires there).
        self._window_send_locks: dict[
            str, tuple[asyncio.Lock, asyncio.AbstractEventLoop]
        ] = {}
        # Post-/exit quarantine registry (Hermes P1): window_id → wall stamp.
        # A restart that irrevocably sent /exit but could not confirm a
        # relaunch leaves the pane in an UNKNOWN state (it may drop to a bare
        # shell AFTER the wait expires, in a still-bound topic);
        # SessionManager.send_to_window re-checks pane_current_command for a
        # quarantined window before typing user text. In-memory only — a bot
        # restart clears it (documented residual).
        self._quarantined_windows: dict[str, float] = {}
        # GH #50 stranded-draft brake (r2 F2): window_id → wall stamp. See the
        # module docstring + the "brake lifecycle" comment on the mutators.
        # In-memory only — a bot restart clears it (documented residual).
        self._stranded_draft_windows: dict[str, float] = {}
        # GH #65 review r10 P1-B — the KILL-PENDING registry: window_id → count
        # of kills currently in flight for that id. Mirrors the quarantine and
        # brake precedent above, and exists for the one hazard cancellation
        # cannot reach: ``kill_window`` runs libtmux inside ``asyncio.to_thread``,
        # and cancelling the async wrapper does NOT stop the worker thread. A
        # surviving worker could therefore kill a window that had been ADOPTED
        # after the caller's ownership check — a TOCTOU the killer's side cannot
        # close. So it is closed on the ADOPTION side instead: an id with a kill
        # in flight is refused for adoption until the kill settles. A counter,
        # not a flag, so concurrent kills for one id cannot clear each other's
        # pending state. In-memory only — a bot restart clears it.
        self._kill_pending_windows: dict[str, int] = {}
        # GH #65 review r12 P1-A — THE WINDOW-LIFECYCLE LOCK. Serializes the
        # REGISTRATION of a kill against every ADOPTION, so no kill can
        # interpose between an adopter's gate check and its commit. Created
        # lazily on first use, for the same reason the list lock is: an
        # asyncio.Lock binds to the running loop at construction, and this
        # singleton outlives individual test loops. See the module docstring
        # for the LOCK ORDER rule (this lock is INNERMOST).
        self._lifecycle_lock: _LifecycleLock | None = None

    @property
    def server(self) -> libtmux.Server:
        """Get or create tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    def get_session(self) -> libtmux.Session | None:
        """Get the tmux session if it exists."""
        try:
            return self.server.sessions.get(session_name=self.session_name)
        except Exception:
            return None

    def get_or_create_session(self) -> libtmux.Session:
        """Get existing session or create a new one."""
        session = self.get_session()
        if session:
            self._scrub_session_env(session)
            return session

        # Create new session with main window named specifically
        session = self.server.new_session(
            session_name=self.session_name,
            start_directory=str(Path.home()),
        )
        # Rename the default window to the main window name
        if session.windows:
            session.windows[0].rename_window(config.tmux_main_window_name)
        self._scrub_session_env(session)
        return session

    @staticmethod
    def _scrub_session_env(session: libtmux.Session) -> None:
        """Remove sensitive env vars from the tmux session environment.

        Prevents new windows (and their child processes like Claude Code)
        from inheriting secrets such as TELEGRAM_BOT_TOKEN.
        """
        for var in SENSITIVE_ENV_VARS:
            try:
                session.unset_environment(var)
            except Exception:
                pass  # var not set in session env — nothing to remove

    # Field separator for `tmux list-panes -F`. ASCII unit separator (\x1f) is
    # highly unlikely to appear in the captured fields (window names, paths,
    # command names), so split-by-separator is normally unambiguous. Some tmux
    # builds (e.g. tmux 3.4) emit this control byte as its literal octal escape
    # "\037" rather than the raw byte, so both forms are accepted; a line that
    # does not contain exactly five delimiters of a single form (and zero of
    # the other) is ambiguous and skipped (see `_list_windows_direct`).
    _PANE_FIELD_SEP = "\x1f"
    # The literal octal-escape form some tmux builds emit for the separator.
    _ESCAPED_FIELD_SEP = "\\037"
    _PANE_FORMAT = _PANE_FIELD_SEP.join(
        [
            "#{session_name}",
            "#{window_id}",
            "#{window_name}",
            "#{pane_active}",
            "#{pane_current_path}",
            "#{pane_current_command}",
        ]
    )

    async def _list_windows_direct(self) -> list[TmuxWindow]:
        """List windows by running a single `tmux list-panes -a -F` subprocess.

        Replaces the libtmux-driven path which fans out one `tmux list-panes`
        subprocess per window. Falls back to the libtmux implementation
        (`_list_windows_libtmux`) on tmux failure so the bot keeps working
        if tmux misbehaves.
        """
        # If tmux wasn't resolvable at import, the libtmux fallback would
        # fail the same way every cycle. Skip the subprocess attempt to
        # avoid per-second warning spam, and route straight to the
        # fallback (which logs at debug and returns []).
        if _TMUX_BIN is None:
            return await asyncio.to_thread(self._list_windows_libtmux)
        try:
            proc = await asyncio.create_subprocess_exec(
                _TMUX_BIN,
                "list-panes",
                "-a",
                "-F",
                self._PANE_FORMAT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except Exception as e:
            logger.warning(
                "tmux list-panes subprocess failed (%s); falling back to libtmux",
                e,
            )
            return await asyncio.to_thread(self._list_windows_libtmux)

        if proc.returncode != 0:
            logger.warning(
                "tmux list-panes returned non-zero (%s): %s; falling back to libtmux",
                proc.returncode,
                stderr.decode("utf-8", errors="replace").strip(),
            )
            return await asyncio.to_thread(self._list_windows_libtmux)

        windows: list[TmuxWindow] = []
        for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                continue
            # Accept either separator form (raw 0x1F, or the literal "\037" that
            # some tmux builds — e.g. tmux 3.4 — emit for that control byte), but
            # only when exactly five delimiters of a SINGLE form are present and
            # ZERO of the other form (each branch is symmetric — a raw line with
            # a literal "\037" inside a field value is mixed-form too). Anything
            # else (wrong field count, a value that itself contains a delimiter,
            # or a mix of both forms) is malformed for our purposes and skipped
            # so the remaining valid lines are still parsed. Normal lines keep
            # the fast single-subprocess path across tmux versions.
            raw_count = line.count(self._PANE_FIELD_SEP)
            esc_count = line.count(self._ESCAPED_FIELD_SEP)
            if raw_count == 5 and esc_count == 0:
                parts = line.split(self._PANE_FIELD_SEP)
            elif raw_count == 0 and esc_count == 5:
                parts = line.split(self._ESCAPED_FIELD_SEP)
            else:
                logger.debug("Skipping malformed pane line: %r", line)
                continue
            (
                session_name,
                window_id,
                window_name,
                pane_active,
                cwd,
                pane_cmd,
            ) = parts
            if session_name != self.session_name:
                continue
            if pane_active != "1":
                continue
            if window_name == config.tmux_main_window_name:
                continue
            if window_id == "":
                continue
            windows.append(
                TmuxWindow(
                    window_id=window_id,
                    window_name=window_name,
                    cwd=cwd,
                    pane_current_command=pane_cmd,
                )
            )
        return windows

    def _list_windows_libtmux(self) -> list[TmuxWindow]:
        """Fallback: enumerate windows via libtmux (one subprocess per window).

        Used only when the direct `tmux list-panes -a` path fails. Kept as a
        safety net so the bot remains functional if tmux output format
        changes or the binary misbehaves. Wrapped in a top-level try/except
        because libtmux can raise mid-iteration during a server reconnect —
        a fallback must never propagate.
        """
        windows: list[TmuxWindow] = []
        try:
            session = self.get_session()
            if not session:
                return windows
            for window in session.windows:
                name = window.window_name or ""
                # Skip the main window (placeholder window)
                if name == config.tmux_main_window_name:
                    continue
                try:
                    pane = window.active_pane
                    if pane:
                        cwd = pane.pane_current_path or ""
                        pane_cmd = pane.pane_current_command or ""
                    else:
                        cwd = ""
                        pane_cmd = ""
                    windows.append(
                        TmuxWindow(
                            window_id=window.window_id or "",
                            window_name=name,
                            cwd=cwd,
                            pane_current_command=pane_cmd,
                        )
                    )
                except Exception as e:
                    logger.debug(f"Error getting window info: {e}")
        except Exception as e:
            logger.warning("libtmux fallback failed: %s; returning empty list", e)
            return []
        return windows

    async def _ensure_list_cache(
        self, *, min_generation: int = 0
    ) -> dict[str, TmuxWindow]:
        """Return the dict-shaped list_windows cache, refreshing if stale.

        Lock-protected slow path keeps 8 concurrent gather() callers from
        each spawning their own tmux subprocess. The fast-path read is
        unsynchronized — safe under a single asyncio loop where dict
        assignment is atomic.
        """
        # Lazy lock init. Two coroutines hitting a freshly-constructed manager
        # cannot both observe ``None`` and both construct: ``asyncio.Lock()``
        # is a synchronous constructor and the check + assignment have no
        # ``await`` between them, so they execute as one cooperative-scheduling
        # step. Do not insert an ``await`` between these two lines.
        if self._list_lock is None:
            self._list_lock = asyncio.Lock()
        # ``min_generation`` is the caller's freshness floor (review r13 P1-A): a
        # snapshot is only acceptable if it was STARTED at or after that
        # generation. A cached value that predates the caller's invalidation is
        # exactly the corpse the fresh probe exists to avoid, so the fast path
        # must respect it too.
        now = time.monotonic()
        if (
            self._list_cache is not None
            and (now - self._list_cache_at) < self._LIST_CACHE_TTL
            and self._list_cache_generation >= min_generation
        ):
            return self._list_cache
        async with self._list_lock:
            # Re-check under the lock: a refresh that ran while we queued may
            # already have published an acceptable snapshot.
            now = time.monotonic()
            if (
                self._list_cache is not None
                and (now - self._list_cache_at) < self._LIST_CACHE_TTL
                and self._list_cache_generation >= min_generation
            ):
                return self._list_cache
            # THE REFRESH RUNS INSIDE THE LOCK, and a snapshot is published
            # ONLY when the generation is unchanged from its START to its END
            # (review r15 P1-A).
            #
            # The r14 shape stamped the read with its STARTING generation and
            # published unconditionally. That argument was WRONG, and the r14
            # test encoded the error: "the read began after the caller's
            # invalidation" is NOT "the read observed post-invalidation state".
            # A kill landing WHILE ``_list_windows_direct`` is awaiting bumps the
            # generation after our start stamp was taken, so the pre-kill sample
            # got published under a stamp the caller's floor accepts — the exact
            # corpse the guard exists to reject. Concretely: settlement returns,
            # a kill registers and releases, an adopter takes the lock while the
            # kill is still pending, the listing samples the LIVE window, the
            # kill lands and clears the pending mark, the listing returns the
            # corpse, and the post-list pending check sees nothing.
            #
            # Matching START and END generations is what actually closes it: an
            # invalidation anywhere inside the read makes them differ, so the
            # sample is DISCARDED and retried. On exhaustion a caller that
            # DEMANDED freshness gets a typed refusal — never the rejected read.
            # An ordinary (floor-free) caller gets its observation back
            # UNPUBLISHED, so a raced sample can never be handed to anybody else.
            windows: list[TmuxWindow] = []
            for _ in range(_LIST_REFRESH_MAX_ATTEMPTS):
                started_at_generation = self._invalidation_generation
                windows = await self._list_windows_direct()
                if self._invalidation_generation != started_at_generation:
                    logger.debug(
                        "tmux listing invalidated mid-read (gen %d -> %d) — "
                        "discarding the sample and retrying",
                        started_at_generation,
                        self._invalidation_generation,
                    )
                    continue
                self._list_cache = {w.window_id: w for w in windows if w.window_id}
                self._list_cache_at = time.monotonic()
                self._list_cache_generation = started_at_generation
                return self._list_cache

            # No typed refusal here any more (review r16): ADOPTION no longer
            # reads this cache at all, so there is no freshness-critical caller
            # left to refuse. Display callers get the observation back
            # UNPUBLISHED — a raced sample is still never cached for anyone.
            logger.warning(
                "tmux listing raced invalidations %d times — returning an "
                "UNPUBLISHED observation (no stale snapshot is cached)",
                _LIST_REFRESH_MAX_ATTEMPTS,
            )
            return {w.window_id: w for w in windows if w.window_id}

    def _invalidate_list_cache(self) -> int:
        """Drop the list_windows cache after an explicit mutation.

        Always called from async-side code AFTER the libtmux operation has
        returned (i.e. after `await asyncio.to_thread(...)` resolves), so a
        concurrent `list_windows` cannot observe a half-applied state.

        Returns the NEW invalidation generation (review r13 P1-A). Dropping the
        cache is not enough on its own: a refresh already in flight can publish
        its pre-mutation snapshot afterwards, and a caller that invalidated in
        order to get a fresh answer would accept it. The generation lets that
        caller demand a snapshot started at or after its own invalidation.
        """
        self._list_cache = None
        self._list_cache_at = 0.0
        self._invalidation_generation += 1
        return self._invalidation_generation

    async def adoption_listing(self) -> list[TmuxWindow]:
        """THE listing every ADOPTION decision uses. DIRECT, and never cached.

        GH #65 review r16 — a DESIGN REPLACEMENT, not another guard. Three
        consecutive rounds found a defect in the previous round's fix to the
        cached-listing seam (r13's invalidate-then-read, r14's start-stamp,
        r15's start/end match), which is the repo's "three doors into the same
        room" signal: the approach was wrong, not the patches.

        The root problem was that adoption correctness DEPENDED ON THE CACHE.
        Every unrelated invalidation — any kill anywhere, from any topic —
        participated in whether an adopter could trust what it read, and the
        three adoption paths each had to reproduce the same freshness ritual
        (invalidate, floor, re-check) without drifting. Reading tmux DIRECTLY
        removes the dependency instead of guarding it: there is no snapshot, so
        there is nothing to be stale, nothing to publish for someone else, and
        no generation to reason about. The only failure it can raise is the
        ``LifecycleTimeout`` every seam already handles.

        Callers MUST hold the window-lifecycle lock (that is what makes the
        answer usable for a decision — see the module docstring's LOCK ORDER
        rule), and MUST wrap this in ``_bounded_lifecycle`` like any other tmux
        await under the hold.

        The TTL cache stays, DEMOTED to what it was always good at: the 1 Hz
        pollers and display. No adoption decision consults it.
        """
        return await self._list_windows_direct()

    async def list_windows_fresh(self) -> list[TmuxWindow]:
        """``list_windows`` that BYPASSES (and refreshes) the 1 s cache.

        DISPLAY-SIDE ONLY. Adoption decisions must use
        :meth:`adoption_listing` — see its docstring for why the cache is not
        allowed to participate in them (review r16).
        """
        min_generation = self._invalidate_list_cache()
        cache = await self._ensure_list_cache(min_generation=min_generation)
        return list(cache.values())

    async def list_windows(self) -> list[TmuxWindow]:
        """List all windows in the session with their working directories.

        Returns:
            List of TmuxWindow with window info and cwd. Served from a 1s
            TTL cache; mutations through this manager invalidate.
        """
        cache = await self._ensure_list_cache()
        return list(cache.values())

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        """Find a window by its name."""
        cache = await self._ensure_list_cache()
        for window in cache.values():
            if window.window_name == window_name:
                return window
        logger.debug("Window not found by name: %s", window_name)
        return None

    async def find_window_by_id(
        self, window_id: str, *, fresh: bool = False
    ) -> TmuxWindow | None:
        """Find a window by its tmux window ID (e.g. '@0', '@12').

        DISPLAY-SIDE ONLY. ``fresh=True`` bypasses (and refreshes) the 1 s
        cache, which is useful for a display that wants an up-to-date view — but
        it is NOT how adoption decisions read tmux.

        ADOPTION USES :meth:`adoption_listing`, a DIRECT uncached read taken
        under the window-lifecycle lock (review r16). Three rounds of defects in
        the cache-guarding approach ended with the cache removed from adoption
        entirely, so no amount of freshness flagging here makes this method
        suitable for deciding whether a window is safe to adopt.
        """
        min_generation = self._invalidate_list_cache() if fresh else 0
        cache = await self._ensure_list_cache(min_generation=min_generation)
        w = cache.get(window_id)
        if w is None:
            logger.debug("Window not found by id: %s", window_id)
        return w

    async def capture_pane(
        self,
        window_id: str,
        with_ansi: bool = False,
        scrollback_lines: int = 0,
    ) -> str | None:
        """Capture the visible text content of a window's active pane.

        Args:
            window_id: The window ID to capture
            with_ansi: If True, capture with ANSI color codes
            scrollback_lines: If > 0, include this many lines of history
                above the visible region. Useful for AskUserQuestion
                rendering where a long question pushes early options off
                the top of the visible pane; capturing 100+ lines of
                history brings them back. Default 0 = visible only,
                matching the legacy behavior callers like status-line
                parsing depend on.

        Returns:
            The captured text, or None on failure.
        """
        tmux_bin = _TMUX_BIN if _TMUX_BIN is not None else "tmux"
        args: list[str] = [tmux_bin, "capture-pane"]
        if with_ansi:
            args.append("-e")
        if scrollback_lines > 0:
            args.extend(["-S", f"-{scrollback_lines}"])
        args.extend(["-p", "-t", window_id])
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode("utf-8", errors="replace")
            logger.error(
                f"Failed to capture pane {window_id}: "
                f"{stderr.decode('utf-8', errors='replace')}"
            )
            return None
        except Exception as e:
            logger.error(f"Unexpected error capturing pane {window_id}: {e}")
            return None

    async def capture_pane_cancellation_safe(
        self,
        window_id: str,
        with_ansi: bool = False,
        scrollback_lines: int = 0,
    ) -> str | None:
        """``capture_pane`` that REAPS its subprocess if the await is cancelled.

        Identical return semantics to ``capture_pane`` on the normal paths.
        The difference: ``capture_pane`` has no subprocess timeout, so when a
        caller wraps the await in ``asyncio.wait_for`` and the deadline fires (or
        the task is otherwise cancelled) mid-``communicate``, the raw method would
        ORPHAN the tmux subprocess. This variant best-effort ``proc.kill()`` +
        ``await proc.wait()`` in a ``finally`` on ``CancelledError`` before
        re-raising, so repeated /cost against a hung tmux never accumulates
        zombies. The DEFAULT ``capture_pane`` stays byte-identical for every
        other caller.
        """
        tmux_bin = _TMUX_BIN if _TMUX_BIN is not None else "tmux"
        args: list[str] = [tmux_bin, "capture-pane"]
        if with_ansi:
            args.append("-e")
        if scrollback_lines > 0:
            args.extend(["-S", f"-{scrollback_lines}"])
        args.extend(["-p", "-t", window_id])
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode("utf-8", errors="replace")
            logger.error(
                f"Failed to capture pane {window_id}: "
                f"{stderr.decode('utf-8', errors='replace')}"
            )
            return None
        except asyncio.CancelledError:
            # The await was cancelled (a wait_for deadline / task cancel). Kill
            # AND reap the orphaned subprocess before propagating the cancel.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:  # pragma: no cover — proc already gone
                    pass
                # The reap must survive a REPEATED cancellation (review r1 P2):
                # a second CancelledError landing in a bare ``await proc.wait()``
                # would escape an ``except Exception`` and leave the killed proc
                # unreaped. ``shield`` keeps the reap task running even if THIS
                # await is cancelled again; BaseException is caught so the
                # ORIGINAL cancellation is what re-raises below.
                try:
                    await asyncio.shield(proc.wait())
                except BaseException:  # noqa: BLE001 — reap best-effort
                    pass
            raise
        except Exception as e:
            logger.error(f"Unexpected error capturing pane {window_id}: {e}")
            return None

    async def pane_current_command_cancellation_safe(
        self, window_id: str
    ) -> str | None:
        """``pane_current_command`` that REAPS its subprocess when cancelled.

        The command-probe sibling of ``capture_pane_cancellation_safe`` (GH #65
        review r3 P2-3). The plain ``pane_current_command`` has no subprocess
        timeout, so a caller that wraps it in ``asyncio.wait_for`` — which the
        trust lane's bounded slices do, once per slice — would ORPHAN a tmux
        subprocess on every deadline. Same return semantics as the plain method
        on all normal paths; ``None`` on a non-zero exit or non-empty stderr.
        """
        tmux_bin = _TMUX_BIN if _TMUX_BIN is not None else "tmux"
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                tmux_bin,
                "display-message",
                "-p",
                "-t",
                window_id,
                "#{pane_current_command}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:  # pragma: no cover — proc already gone
                    pass
                try:
                    await asyncio.shield(proc.wait())
                except BaseException:  # noqa: BLE001 — reap best-effort
                    pass
            raise
        except Exception as e:
            logger.error(
                "pane_current_command subprocess failed for %s: %s", window_id, e
            )
            return None
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or err:
            logger.error(
                "tmux display-message failed for %s (rc=%s): %s",
                window_id,
                proc.returncode,
                err,
            )
            return None
        return stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _cmd_send_literal(pane: libtmux.Pane, window_id: str, chars: str) -> bool:
        """Send literal text via raw ``send-keys -l -- <chars>`` and check stderr.

        libtmux's ``pane.send_keys(..., literal=True)`` omits the ``--``
        end-of-options separator and never checks stderr, so a payload
        starting with ``-`` (a bullet list, ``--continue``) makes tmux exit 1
        with "invalid flag" while the call silently succeeds. The raw command
        with ``--`` passes dash-leading payloads verbatim; non-empty stderr
        from the returned ``tmux_cmd`` is treated as failure.
        """
        result = pane.cmd("send-keys", "-l", "--", chars)
        if result.stderr:
            logger.error(
                f"tmux send-keys -l failed for window {window_id}: {result.stderr}"
            )
            return False
        return True

    @staticmethod
    def _cmd_resize_window(window: libtmux.Window, width: int, height: int) -> bool:
        """Resize a window via raw ``resize-window -x <w> -y <h>``, check stderr.

        Wave B machine-surface geometry. Follows the ``_cmd_send_literal``
        precedent: libtmux swallows tmux stderr, so a failed resize (e.g.
        size out of tmux's bounds) would silently "succeed" — non-empty
        stderr from the returned ``tmux_cmd`` is treated as failure. On a
        detached window the resize implicitly flips ``window-size`` to
        ``manual`` (rig-verified, tmux 3.6a). Returns bool; never raises —
        geometry is an optimization, never a blocker for the caller.
        """
        try:
            result = window.cmd("resize-window", "-x", str(width), "-y", str(height))
        except Exception as e:
            logger.warning(
                "tmux resize-window failed for window %s: %s",
                getattr(window, "window_id", "?"),
                e,
            )
            return False
        if result.stderr:
            logger.warning(
                "tmux resize-window failed for window %s: %s",
                getattr(window, "window_id", "?"),
                result.stderr,
            )
            return False
        return True

    async def resize_window(self, window_id: str, width: int, height: int) -> bool:
        """Resize a tmux window by its ID to ``width`` x ``height``.

        Resolves the REAL ``libtmux.Window`` INSIDE the worker thread —
        never the lightweight ``TmuxWindow`` dataclass that
        ``find_window_by_id`` returns (it has no ``.cmd``). Idempotent:
        resize-to-same-size is a tmux no-op. Returns False when the session
        or window is gone; never raises.
        """

        def _sync_resize() -> bool:
            session = self.get_session()
            if not session:
                logger.warning("resize_window: no tmux session found")
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    logger.warning("resize_window: window %s not found", window_id)
                    return False
                return self._cmd_resize_window(window, width, height)
            except Exception as e:
                logger.warning("Failed to resize window %s: %s", window_id, e)
                return False

        return await asyncio.to_thread(_sync_resize)

    def window_send_lock(self, window_id: str) -> asyncio.Lock:
        """Return the per-window send lock for ``window_id``, creating on demand.

        Must be called from a running event loop. Serializes multi-keystroke
        pane transactions (see "Concurrency" in the module docstring for the
        lifecycle and the leaf rule). A registry entry created under a
        previous, now-replaced event loop (test-only situation) is recreated:
        the stale lock provably has no holders because its loop is gone.
        """
        running = asyncio.get_running_loop()
        entry = self._window_send_locks.get(window_id)
        if entry is not None:
            lock, loop = entry
            if loop is running:
                return lock
        lock = asyncio.Lock()
        self._window_send_locks[window_id] = (lock, running)
        return lock

    def reset_window_send_locks_for_tests(self) -> None:
        """Drop all per-window send locks (test isolation seam)."""
        self._window_send_locks.clear()

    # ── Post-/exit window quarantine (Hermes P1) ───────────────────────────
    #
    # ``/exit`` is irrevocable. When ``restart_claude_in_window`` cannot
    # confirm a relaunch afterwards — the shell-wait expired
    # (``SKIPPED_NO_EXIT``) or the relaunch keystroke send failed on a
    # confirmed-shell pane — the topic stays BOUND to a pane that is (or may
    # later become) a bare shell, and a Telegram message queued on the send
    # lock during the wait would be typed into (and executed by) that shell.
    # The quarantine makes ``send_to_window`` re-check the live pane command
    # first: a non-shell command (Claude's version string) is positive proof
    # of life and clears the bit; a shell or a failed query REFUSES the send.

    def mark_window_quarantined(self, window_id: str) -> None:
        """Mark ``window_id`` post-/exit UNKNOWN — sends must re-check first."""
        self._quarantined_windows[window_id] = time.time()
        logger.warning(
            "window %s QUARANTINED — /exit sent but no relaunch confirmed; "
            "user sends will be refused until Claude is observed alive",
            window_id,
        )

    def window_quarantined(self, window_id: str) -> bool:
        """True iff ``window_id`` is marked post-/exit unknown."""
        return window_id in self._quarantined_windows

    def clear_window_quarantine(self, window_id: str, *, reason: str) -> None:
        """Clear a quarantine (positive proof of life, or window teardown)."""
        if self._quarantined_windows.pop(window_id, None) is not None:
            logger.info("window %s quarantine cleared (%s)", window_id, reason)

    def reset_window_quarantines_for_tests(self) -> None:
        """Drop all window quarantines (test isolation seam)."""
        self._quarantined_windows.clear()

    # ── The GH #50 stranded-draft brake (r2 F2) ────────────────────────────
    #
    # BRAKE LIFECYCLE — the one rule that makes this safe (peer-review P1):
    #
    #   The brake is a property of the PANE'S CONTENTS, never of a topic
    #   binding. A ``draft_written`` / ``commit_unknown`` delivery left the
    #   user's text sitting in this window's input box with its Enter withheld,
    #   and the user was TOLD it was not delivered. A live input box holding a
    #   draft is legitimately WRITABLE, so until that text is gone the next
    #   payload would be appended to it and ITS Enter would commit BOTH.
    #
    #   Therefore the brake is released on exactly two proofs:
    #
    #     (a) POSITIVE PROOF THE BOX IS EMPTY — ``session._stranded_draft_gate``
    #         captures the pane and requires ``pane_input_row_empty`` True (an
    #         INDETERMINATE frame keeps it). Nothing is ever auto-cleared with a
    #         keystroke: Esc has surface-specific semantics (on folder-trust it
    #         KILLS Claude) and mid-run it interrupts.
    #     (b) PROOF THE WINDOW IS DEAD — a CONFIRMED ``kill_window`` (its entry
    #         is then pure garbage), or ``create_window`` minting a brand-new
    #         window under that id (tmux ids RESET to @0 on a tmux-server
    #         restart, which a long-lived bot process can outlive).
    #
    #   TOPIC TEARDOWN DELIBERATELY DOES NOT CLEAR IT. ``/unbind`` explicitly
    #   LEAVES THE WINDOW ALIVE, and ``cleanup.clear_topic_state`` /
    #   ``inbound_telegram``'s stale-window unbinds run with NO synchronization
    #   against ``window_send_lock`` — so clearing there re-opened the exact
    #   commit chain the brake exists to break: delivery A arms the brake inside
    #   the lock; teardown clears it; send B (an already-popped boundary flush,
    #   or a slash command) — which has been BLOCKED on that same window lock the
    #   whole time — then acquires it, sees a structurally valid input box that
    #   still contains A's draft, appends its own payload and presses Enter,
    #   committing both.
    #
    #   A leaked entry for a window that died WITHOUT a kill_window (an external
    #   `tmux kill-window`, the poller's stale-binding path) is inert, not a
    #   wedge: ``_deliver_locked`` refuses ``window_gone`` on ``find_window_by_id``
    #   BEFORE it ever consults the brake, and (a) self-heals any id that is
    #   later reused. Disclosed residual: a bot restart wipes the brake entirely
    #   (identical to the quarantine registry's).

    def mark_window_stranded_draft(self, window_id: str) -> None:
        """Arm the brake: this window's input box holds a bot-written, unsent draft."""
        if window_id not in self._stranded_draft_windows:
            logger.warning(
                "stranded-draft brake ARMED for window %s — further sends refused "
                "until the input box is cleared",
                window_id,
            )
        self._stranded_draft_windows[window_id] = time.time()

    def window_has_stranded_draft(self, window_id: str) -> bool:
        """True iff a bot-written payload may still be sitting unsent in the box."""
        return window_id in self._stranded_draft_windows

    def clear_window_stranded_draft(self, window_id: str, *, reason: str) -> None:
        """Release the brake — ONLY on empty-box proof or confirmed window death."""
        if self._stranded_draft_windows.pop(window_id, None) is not None:
            logger.info(
                "stranded-draft brake cleared for window %s (%s)", window_id, reason
            )

    def reset_stranded_drafts_for_tests(self) -> None:
        """Drop all stranded-draft brakes (test isolation seam)."""
        self._stranded_draft_windows.clear()

    def window_kill_pending(self, window_id: str) -> bool:
        """True while a kill for this window id is still in flight.

        THE ADOPTION GATE (GH #65 review r10 P1-B). ``kill_window`` dispatches
        libtmux into ``asyncio.to_thread``; cancelling the async wrapper cannot
        stop the worker thread, so a kill can still land after its caller has
        given up and released ownership. Every seam that would ADOPT a window id
        — reusing it for a new window, binding a topic to an existing one, the
        trust lane's completion bind — must consult this first and refuse or
        defer, because a window nobody has adopted is the only thing a straggler
        is allowed to kill.

        **Irreducible residual, stated honestly:** a kill already inside libtmux
        WILL kill the window it was aimed at. That was always its target, and no
        gate here can recall it. What this closes is the harm that mattered — an
        ADOPTED window dying under a new owner. Nothing that has been adopted
        can be killed by a straggler, because adoption cannot happen while the
        kill is pending.
        """
        return self._kill_pending_windows.get(window_id, 0) > 0

    async def await_kill_settled(
        self, window_id: str, *, timeout: float = 10.0, interval: float = 0.05
    ) -> bool:
        """DEFER until no kill for this id is in flight. True if it settled.

        The adoption seams' companion to :meth:`window_kill_pending`: a caller
        that would rather wait than refuse polls here first. Bounded, so a
        wedged libtmux worker degrades to a refusal instead of a hang.
        """
        if not self.window_kill_pending(window_id):
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            if not self.window_kill_pending(window_id):
                return True
        logger.warning(
            "a kill for window %s did not settle within %.1fs — refusing adoption",
            window_id,
            timeout,
        )
        return False

    @property
    def lifecycle_ops_high_water(self) -> int:
        """Most bounded ops observed under a SINGLE lifecycle-lock hold.

        Review r15 P2-B. The accounting is STRUCTURAL — counted at runtime by
        ``_bounded_lifecycle`` — because the lexical scan it replaces could only
        see ``_bounded_lifecycle`` calls textually nested under an ``async with``
        line: the trust acquisition's op hides inside
        ``_revalidate_bind_preconditions`` and counted zero, and an aliased
        acquisition was invisible entirely. The pin test drives each acquisition
        site and reads this.
        """
        return self.window_lifecycle_lock().ops_high_water

    def reset_lifecycle_ops_accounting(self) -> None:
        """Zero the high-water mark (test seam)."""
        self.window_lifecycle_lock().reset_accounting()

    def window_lifecycle_lock(self) -> _LifecycleLock:
        """THE lock that serializes kill-registration against adoption.

        See the module docstring's LOCK ORDER rule: this lock is INNERMOST —
        never await the trust-flow creation lock, a route lock, or Telegram I/O
        while holding it, and do the bounded settlement wait BEFORE acquiring.
        """
        # Constructed here rather than in __init__ so it binds to the loop that
        # actually uses it. The check and the assignment have no ``await``
        # between them, so two coroutines cannot both observe None and both
        # construct — the same argument as ``_list_lock``. Do not insert an
        # ``await`` between these two lines.
        if self._lifecycle_lock is None:
            self._lifecycle_lock = _LifecycleLock()
        return self._lifecycle_lock

    def reset_lifecycle_lock_for_tests(self) -> None:
        """Drop the lifecycle lock so a new event loop gets a fresh one."""
        self._lifecycle_lock = None

    async def _bounded_lifecycle(
        self, coro: Any, *, what: str, timeout: float | None = None
    ) -> Any:
        """Await a tmux operation under the lifecycle lock, with a hard bound.

        Review r13 P1-B. The lifecycle lock serializes kill-registration against
        adoption, which means ANY unbounded await inside it is a global stall:
        one wedged tmux call and no window anywhere can be killed, created or
        adopted — including the forced trust cleanup and topic teardown that
        exist to recover from exactly that kind of wedge. Expiry raises
        ``LifecycleTimeout``, which unwinds out of the ``async with`` and so
        RELEASES the lock; each caller turns it into its own honest refusal.
        """
        # Resolved at CALL time, not bound as a default: a module-level default
        # argument is evaluated at import and would ignore any later change to
        # the constant (including a test's).
        budget = LIFECYCLE_TMUX_TIMEOUT_S if timeout is None else timeout
        # STRUCTURAL ACCOUNTING (review r15 P2-B): count the op against the hold
        # that is actually in force, wherever in the call tree it happens. The
        # kill bound is DERIVED from this maximum, so a hold that quietly grows
        # past the declared ceiling must be loud.
        lock = self.window_lifecycle_lock()
        if lock.locked():
            observed = lock.note_bounded_op()
            if observed > _MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD:
                message = (
                    f"{observed} bounded operations under ONE lifecycle-lock "
                    f"hold exceeds the declared ceiling of "
                    f"{_MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD} — KILL_LOCK_TIMEOUT_S "
                    "is derived from that ceiling and no longer covers the "
                    "worst-case lawful hold"
                )
                if _STRICT_LIFECYCLE_INVARIANTS:
                    raise AssertionError(message)
                logger.error(message)
        try:
            return await asyncio.wait_for(coro, timeout=budget)
        except (TimeoutError, asyncio.TimeoutError) as e:
            raise LifecycleTimeout(
                f"{what} exceeded {budget:.0f}s under the window-lifecycle lock"
            ) from e

    def any_kill_pending(self) -> bool:
        """True while ANY kill is in flight, for any window id.

        ``create_window`` cannot ask about a specific id: TMUX assigns the id,
        so the question "is a kill pending for the id I am about to be given?"
        is unanswerable before the window exists (review r11 P1-A). The sound
        gate is therefore the global one — while any kill is in flight, the id
        it targets could be the one tmux hands us next, because ids RESET to
        ``@0`` when the tmux server restarts.
        """
        return any(count > 0 for count in self._kill_pending_windows.values())

    async def await_all_kills_settled(
        self, *, timeout: float = 10.0, interval: float = 0.05
    ) -> bool:
        """DEFER until NO kill is in flight anywhere. True if all settled."""
        if not self.any_kill_pending():
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            if not self.any_kill_pending():
                return True
        logger.warning(
            "kills still in flight after %.1fs — refusing to create a window "
            "whose id one of them might target",
            timeout,
        )
        return False

    def reset_kill_pending_for_tests(self) -> None:
        """Drop all kill-pending marks (test isolation seam)."""
        self._kill_pending_windows.clear()

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        """Send keys to a specific window.

        Args:
            window_id: The window ID to send to
            text: Text to send
            enter: Whether to press enter after the text
            literal: If True, send text literally. If False, interpret special keys
                     like "Up", "Down", "Left", "Right", "Escape", "Enter".

        Returns:
            True if successful, False otherwise
        """
        if literal and enter:
            # Split into text + delay + Enter via libtmux.
            # Claude Code's TUI sometimes interprets a rapid-fire Enter
            # (arriving in the same input batch as the text) as a newline
            # rather than submit.  A 500ms gap lets the TUI process the
            # text before receiving Enter.
            def _send_literal(chars: str) -> bool:
                session = self.get_session()
                if not session:
                    logger.error("No tmux session found")
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        logger.error(f"Window {window_id} not found")
                        return False
                    pane = window.active_pane
                    if not pane:
                        logger.error(f"No active pane in window {window_id}")
                        return False
                    return self._cmd_send_literal(pane, window_id, chars)
                except Exception as e:
                    logger.error(f"Failed to send keys to window {window_id}: {e}")
                    return False

            def _send_enter() -> bool:
                session = self.get_session()
                if not session:
                    return False
                try:
                    window = session.windows.get(window_id=window_id)
                    if not window:
                        return False
                    pane = window.active_pane
                    if not pane:
                        return False
                    pane.send_keys("", enter=True, literal=False)
                    return True
                except Exception as e:
                    logger.error(f"Failed to send Enter to window {window_id}: {e}")
                    return False

            # Claude Code's ! command mode: send "!" first so the TUI
            # switches to bash mode, wait 1s, then send the rest.
            if text.startswith("!"):
                if not await asyncio.to_thread(_send_literal, "!"):
                    return False
                rest = text[1:]
                if rest:
                    await asyncio.sleep(1.0)
                    if not await asyncio.to_thread(_send_literal, rest):
                        return False
            else:
                if not await asyncio.to_thread(_send_literal, text):
                    return False
            await asyncio.sleep(0.5)
            return await asyncio.to_thread(_send_enter)

        # Other cases: special keys (literal=False) or no-enter
        def _sync_send_keys() -> bool:
            session = self.get_session()
            if not session:
                logger.error("No tmux session found")
                return False

            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    logger.error(f"Window {window_id} not found")
                    return False

                pane = window.active_pane
                if not pane:
                    logger.error(f"No active pane in window {window_id}")
                    return False

                if literal:
                    # Raw `send-keys -l --` path: dash-leading payloads pass
                    # verbatim, tmux errors surface as False (finding 1).
                    if not self._cmd_send_literal(pane, window_id, text):
                        return False
                    if enter:
                        pane.send_keys("", enter=True, literal=False)
                    return True
                pane.send_keys(text, enter=enter, literal=literal)
                return True

            except Exception as e:
                logger.error(f"Failed to send keys to window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_send_keys)

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a tmux window by its ID."""

        def _sync_rename() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.rename_window(new_name)
                logger.info("Renamed window %s to '%s'", window_id, new_name)
                return True
            except Exception as e:
                logger.error(f"Failed to rename window {window_id}: {e}")
                return False

        result = await asyncio.to_thread(_sync_rename)
        self._invalidate_list_cache()
        return result

    async def kill_window(self, window_id: str) -> bool:
        """Kill a tmux window by its ID.

        Acquires the window-lifecycle lock to REGISTER the kill. A caller that
        already holds that lock must use the TWO-PHASE pair instead —
        :meth:`begin_kill_locked` under the hold, :meth:`finish_kill` after
        releasing it. ``asyncio.Lock`` is not reentrant, so calling this under
        the hold would deadlock.
        """
        try:
            await asyncio.wait_for(
                self.window_lifecycle_lock().acquire(),
                timeout=KILL_LOCK_TIMEOUT_S,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.error(
                "could not acquire the window-lifecycle lock within %.0fs to "
                "kill window %s — reporting failure",
                KILL_LOCK_TIMEOUT_S,
                window_id,
            )
            return False
        try:
            inner = self.begin_kill_locked(window_id)
        finally:
            self.window_lifecycle_lock().release()
        return await self.finish_kill(window_id, inner)

    def begin_kill_locked(self, window_id: str) -> "asyncio.Future[bool]":
        """Phase 1 of a two-phase kill: register the mark and dispatch the work.

        The caller MUST hold the window-lifecycle lock. Pair with
        :meth:`finish_kill`, which is awaited with the lock RELEASED — the
        REGISTRATION is what must be atomic with the caller's checks, while the
        tmux round-trip must not be, or a wedged libtmux kill would hold the
        lifecycle lock for every other window.
        """

        def _sync_kill() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.kill()
                logger.info("Killed window %s", window_id)
                return True
            except Exception as e:
                logger.error(f"Failed to kill window {window_id}: {e}")
                return False

        # MARK BEFORE DISPATCH, CLEAR WHEN THE EXECUTOR CALL ACTUALLY RETURNS
        # (review r10 P1-B). The mark is set here, on the event loop, strictly
        # before the work is handed to the thread. It is cleared from a
        # DONE-CALLBACK on the inner future rather than a plain ``finally``,
        # because a ``finally`` on this coroutine runs as soon as WE are
        # cancelled — while the worker thread is still inside ``window.kill()``.
        # The done-callback fires when the thread genuinely finishes, success or
        # failure, so the adoption gate stays shut for exactly as long as a kill
        # can still land.
        def _clear_kill_pending(_fut: "asyncio.Future[bool]") -> None:
            remaining = self._kill_pending_windows.get(window_id, 0) - 1
            if remaining > 0:
                self._kill_pending_windows[window_id] = remaining
            else:
                self._kill_pending_windows.pop(window_id, None)
            # INVALIDATE HERE, NOT AFTER THE AWAIT (review r12 P1-B). The
            # invalidation below the shielded await is SKIPPED when our caller
            # is cancelled — but the worker still lands the kill, so the 1 s
            # listing cache kept serving the corpse and a revalidating adopter
            # could bind it. The done-callback fires on every outcome, so a
            # landed kill ALWAYS invalidates.
            self._invalidate_list_cache()

        # REGISTER (review r12 P1-A). The mark must become visible to adopters
        # before any adopter can pass its gate, and the lifecycle lock — held by
        # our caller — is what makes "register" and "adopt" mutually exclusive
        # rather than merely ordered. The mark OUTLIVES the hold: it is cleared
        # by the done-callback, long after the caller releases.
        self._kill_pending_windows[window_id] = (
            self._kill_pending_windows.get(window_id, 0) + 1
        )
        inner = asyncio.ensure_future(asyncio.to_thread(_sync_kill))
        inner.add_done_callback(_clear_kill_pending)
        return inner

    async def finish_kill(self, window_id: str, inner: "asyncio.Future[bool]") -> bool:
        """Await a dispatched kill and apply its post-conditions.

        Awaited with the lifecycle lock RELEASED (review r13 P1-B), so a wedged
        libtmux kill cannot freeze every other window's lifecycle.
        """
        # SHIELDED: our own cancellation must not detach the mark from the work.
        result = await asyncio.shield(inner)
        self._invalidate_list_cache()
        # Drop the per-window send lock ONLY on a confirmed kill (Wave 3a
        # Hermes P3): a failed kill can leave the window ALIVE with an
        # in-flight holder, and popping here would hand a later acquirer a
        # FRESH lock for the same live window — the split-lock class this
        # registry exists to prevent. A window that vanished externally
        # leaves a stale no-holder entry, which is the documented harmless
        # bound (module docstring).
        if result:
            self._window_send_locks.pop(window_id, None)
            # A killed window's quarantine must not leak onto a later window
            # that reuses the id (tmux ids reset on server restart).
            self._quarantined_windows.pop(window_id, None)
            # GH #50 peer-review P1: a CONFIRMED kill is the ONLY window-death
            # proof that releases the stranded-draft brake. A window that
            # SURVIVES its topic's teardown (/unbind leaves it alive!) keeps its
            # brake — the draft is still in the box. A dead window's entry is
            # pure garbage. Gated on ``result`` for the same reason the send lock
            # is: a FAILED kill can leave the window alive with the draft intact.
            self.clear_window_stranded_draft(window_id, reason="window killed")
        return result

    @staticmethod
    def _transfer_ownership(
        result: tuple[bool, str, str, str],
        disposition: "_CreateDisposition",
        reap: "Callable[[str], None]",
    ) -> tuple[bool, str, str, str]:
        """THE single exit for a ``create_window`` result. Returns it unchanged.

        RETURNING AN ID TRANSFERS OWNERSHIP (review r16 P1-A, made total in r17
        P1). This is the rule that gives every outcome EXACTLY ONE cleanup
        owner: if the caller is handed a window id — the creation SUCCEEDED, it
        came back created-but-unverified, or it partially succeeded — the caller
        now knows about that window and is responsible for settling it (see
        ``inbound_telegram``'s reserve-then-clean arm). Only when NO id reaches
        the caller does the reaper own the window.

        It is a FUNCTION, and every id-bearing return goes through it, because
        the rule failed the moment one return path skipped it: the r16 shape had
        a second ``return`` for the verification timeout that handed back the
        real id without recording TAKEN, so the ``finally`` recorded DECLINED and
        scheduled the reaper for a window the caller was simultaneously being
        told to clean up. One exit is what makes "every id-bearing result" true
        rather than aspirational.

        The transition is recorded BEFORE the id is handed back, so the worker
        callback can never observe "undecided" for a window the caller is taking.
        """
        if result[3] and disposition.record_caller(CallerDisposition.TAKEN):
            reap(disposition.worker_window_id)
        return result

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
        defer_launch: bool = False,
    ) -> tuple[bool, str, str, str]:
        """Create a new tmux window and optionally start Claude Code.

        Args:
            work_dir: Working directory for the new window
            window_name: Optional window name (defaults to directory name)
            start_claude: Whether to start claude command
            resume_session_id: If set, append --resume <id> to claude command
            defer_launch: GH #65 Fix 0 — create the window and resize it but do
                NOT send the launch command, leaving the pane a fresh
                interactive shell owned by the caller. The caller runs its
                in-pane ``--version`` probe there and then calls
                ``launch_claude_in_window`` (which composes the SAME command
                line). Ignored when ``start_claude`` is False.

        Returns:
            Tuple of (success, message, window_name, window_id)
        """
        # Validate directory first
        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        # THE ADOPTION GATE, BEFORE tmux new-window (review r11 P1-A). Checking
        # AFTER creation was not linearizable: the kill's done-callback can
        # clear the counter on the same event-loop turn on which it killed the
        # reused id, so the post-hoc branch saw a clean counter and reported
        # SUCCESS for a window that was already dead. And the id cannot be
        # checked individually — TMUX assigns it, so the only sound question
        # before the fact is the global one. tmux ids RESET to @0 on a server
        # restart, which is exactly how a brand-new window inherits an id a
        # kill is still aimed at.
        #
        # The bounded WAIT runs OUTSIDE the lifecycle lock (review r12 P1-A —
        # the lock must never be held across a 10 s wait); the authoritative
        # RE-CHECK happens under it, immediately before tmux new-window.
        if not await self.await_all_kills_settled():
            # Settlement failing is a REFUSAL, never something to proceed past.
            return (
                False,
                "A window is still being closed. Please try again in a moment.",
                "",
                "",
            )

        # Create window name, adding suffix if name already exists
        final_window_name = window_name if window_name else path.name

        # Check for existing window name
        base_name = final_window_name
        counter = 2
        while await self.find_window_by_name(final_window_name):
            final_window_name = f"{base_name}-{counter}"
            counter += 1

        # Resolve the bot-managed MessageDisplay capture settings once, off the
        # tmux worker thread. Passing it via ``claude --settings`` scopes the
        # live-prose hook (Bug 2) to bot-launched sessions and merges with the
        # global SessionStart / PreToolUse hooks. A failed write degrades
        # gracefully — the window still launches, just without live-prose
        # capture (falls back to post-resolution JSONL delivery).
        md_settings = self._resolve_md_settings()

        # Create window in thread
        def _create_and_start() -> tuple[bool, str, str, str]:
            created_wid_for_reap = ""
            session = self.get_or_create_session()
            try:
                # Create new window
                window = session.new_window(
                    window_name=final_window_name,
                    start_directory=str(path),
                )

                wid = window.window_id or ""
                # From here the WINDOW EXISTS (review r15 P1-B). Any later setup
                # failure must still report this id, or a real window is created
                # that nobody can name and therefore nobody can reap.
                created_wid_for_reap = wid

                # Prevent Claude Code from overriding window name
                window.set_window_option("allow-rename", "off")

                # Wave B machine-surface geometry: resize BEFORE the claude
                # launch so Claude Code starts at final geometry and never
                # repaints mid-startup. A False return is logged (inside the
                # helper) and the window still launches — geometry is an
                # optimization, never a launch blocker.
                self._cmd_resize_window(
                    window, config.window_width, config.window_height
                )

                # Start Claude Code if requested. GH #65 Fix 0: launch-deferred
                # mode leaves the pane a fresh shell for the caller's probe.
                if start_claude and not defer_launch:
                    pane = window.active_pane
                    if pane:
                        cmd = _compose_launch_command(
                            config.claude_command, md_settings, resume_session_id
                        )
                        pane.send_keys(cmd, enter=True)

                logger.info(
                    "Created window '%s' (id=%s) at %s",
                    final_window_name,
                    wid,
                    path,
                )
                return (
                    True,
                    f"Created window '{final_window_name}' at {path}",
                    final_window_name,
                    wid,
                )

            except Exception as e:
                # PARTIAL CREATION (review r15 P1-B): if ``new_window`` already
                # succeeded, the window is REAL even though setup failed, so the
                # id travels with the failure.
                logger.error(f"Failed to create window: {e}")
                return (
                    False,
                    f"Failed to create window: {e}",
                    "",
                    created_wid_for_reap,
                )

        # UNDER THE LIFECYCLE LOCK (review r12 P1-A): the gate re-check, the
        # creation, and the verification listing are ONE critical section, so
        # no kill can register between them. Nothing here awaits the creation
        # lock or Telegram — the lock stays INNERMOST.
        # Every tmux await inside the hold is BOUNDED (review r13 P1-B): one
        # wedged tmux operation must not freeze every other window's lifecycle,
        # and a kill — which topic teardown and forced trust cleanup both need —
        # must be able to make progress. Expiry raises ``LifecycleTimeout``,
        # which RELEASES the lock and lands on the honest refusal below.
        def _reap(window_id: str) -> None:
            logger.warning(
                "window %s exists but no caller took ownership of its creation "
                "(timeout, cancellation or partial creation) — reaping it so it "
                "cannot float unowned",
                window_id,
            )
            # Best effort, and it marks kill-pending like any other kill so no
            # adopter can take the window in the meantime.
            asyncio.ensure_future(self.kill_window(window_id))

        # Declared OUT here so the ``finally`` below can always read it — the
        # caller must record a disposition on every exit, including one taken
        # before the lock was ever acquired.
        disposition: _CreateDisposition | None = None
        try:
            async with self.window_lifecycle_lock():
                if self.any_kill_pending():
                    return (
                        False,
                        "A window is still being closed. Please try again in a moment.",
                        "",
                        "",
                    )
                # A LATE WINDOW MUST NEVER FLOAT UNOWNED (review r14 P1-C).
                # A LATE WINDOW MUST NEVER FLOAT UNOWNED, and a window the
                # caller SUCCESSFULLY took must never be reaped (review r16
                # P1-A). ``wait_for`` cancels the WRAPPER while the worker
                # thread keeps going, so the two facts that decide a window's
                # fate — the worker's outcome and the caller's disposition —
                # arrive in either order. The r15 shape let the worker callback
                # decide ALONE: firing before the shielded waiter resumed, it
                # read "not taken" and scheduled a kill of a window that was
                # about to be returned successfully.
                #
                # Neither party decides alone now. Whichever learns the SECOND
                # fact performs the reap, exactly once.
                disposition = _CreateDisposition()
                inner = asyncio.ensure_future(asyncio.to_thread(_create_and_start))

                def _on_worker_done(fut: "asyncio.Future[Any]") -> None:
                    if fut.cancelled() or fut.exception() is not None:
                        # Nothing was created that we can name, so there is
                        # nothing to reap; the caller still records its own
                        # disposition below.
                        disposition.record_worker("")
                        return
                    _ok, _m, _n, late_wid = fut.result()
                    # Present whenever ``new_window`` succeeded, even if later
                    # setup failed — that is the partial-creation arm.
                    if disposition.record_worker(late_wid):
                        _reap(late_wid)

                inner.add_done_callback(_on_worker_done)
                result = await self._bounded_lifecycle(
                    asyncio.shield(inner), what="create_window"
                )
                # Invalidate AFTER to_thread returns so the brand-new window is
                # visible to the next list_windows call from the resume flow.
                self._invalidate_list_cache()
                # GH #50: a window tmux JUST created cannot hold a bot-written
                # draft, so a brake entry under this id is provably stale. It
                # only exists because tmux window ids RESET to @0 when the tmux
                # SERVER restarts, and a launchd-kept bot process outlives that
                # — so an entry armed on the OLD @0 (whose window died without a
                # kill_window, e.g. with the server) could otherwise meet a
                # brand-new @0. This is the *second* death proof of the brake
                # lifecycle; it does not depend on the window being reaped by
                # us. (It would eventually self-heal anyway, on the first send
                # whose capture proves the fresh window's input row is empty —
                # but a session still BOOTING would first refuse a message with
                # the wrong reason.)
                created, _msg, _name, new_wid = result
                if created and new_wid:
                    self.clear_window_stranded_draft(
                        new_wid, reason="window newly created"
                    )
                    # POST-SETTLEMENT POSITIVE VERIFICATION (review r11 P1-A).
                    # The gate above makes a same-tick kill of our id
                    # unreachable, but success is PROVEN here rather than
                    # inferred from a counter: one existence probe against tmux.
                    #
                    # ABSENCE MUST BE PROVEN, NOT INFERRED.
                    # ``find_window_by_id`` returns None both for "tmux does not
                    # have it" and for "the listing failed" — and reading a
                    # failed enumeration as a dead window would make every
                    # creation fail whenever listing hiccups, which is the
                    # fail-closed direction pointing the wrong way. So the
                    # refusal requires a listing that actually WORKED (it
                    # returned other windows) and did not contain ours; an empty
                    # or failed listing is INDETERMINATE and logged, not fatal.
                    # DIRECT, via ``adoption_listing`` (review r16): this is an
                    # adoption-class decision, so it reads tmux with no cache in
                    # the way — no generation floor to carry, and no unrelated
                    # invalidation able to affect the answer.
                    #
                    # A VERIFICATION TIMEOUT MUST NOT ERASE A REAL WINDOW
                    # (review r14 P1-C). By this point creation is CONFIRMED, so
                    # returning an empty id would hand the caller nothing to
                    # reserve or clean while the window exists — orphaning it.
                    # We return the REAL id with a created-but-unverified
                    # status, and the caller's refusal arm cleans it up.
                    try:
                        listed = await self._bounded_lifecycle(
                            self.adoption_listing(),
                            what="create verification listing",
                        )
                    except LifecycleTimeout as e:
                        logger.error(
                            "could not verify created window %s within its "
                            "bound (%s) — returning it as created-but-unverified "
                            "so the caller can settle it",
                            new_wid,
                            e,
                        )
                        # Falls THROUGH to the single ownership-transfer exit
                        # below (review r17 P1): returning early here skipped the
                        # TAKEN transition, so the ``finally`` recorded DECLINED
                        # and the reaper fired for a window the caller was
                        # simultaneously being told to clean up — two owners,
                        # two possible kills.
                        result = (
                            False,
                            CREATED_BUT_UNVERIFIED_MESSAGE,
                            _name,
                            new_wid,
                        )
                        return self._transfer_ownership(result, disposition, _reap)
                    if listed and not any(w.window_id == new_wid for w in listed):
                        logger.error(
                            "created window %s is absent from a tmux listing of "
                            "%d windows — refusing to report success",
                            new_wid,
                            len(listed),
                        )
                        return (
                            False,
                            "The new window was removed before it could be used. "
                            "Please try again.",
                            "",
                            "",
                        )
                    if not listed:
                        logger.warning(
                            "could not enumerate tmux windows to verify %s — "
                            "proceeding on the pre-create gate alone",
                            new_wid,
                        )
                return self._transfer_ownership(result, disposition, _reap)
        except LifecycleTimeout as e:
            logger.error("window creation exceeded its lifecycle bound: %s", e)
            return (
                False,
                "Creating the window took too long. Please check tmux and try again.",
                "",
                "",
            )
        finally:
            # EVERY OTHER EXIT DECLINES — timeout, cancellation, an early
            # refusal, an unexpected raise. Recording is idempotent and
            # first-decision-wins, so this can never overwrite the TAKEN above;
            # and if the worker already finished, THIS is the party that reaps.
            if disposition is not None and disposition.record_caller(
                CallerDisposition.DECLINED
            ):
                _reap(disposition.worker_window_id)

    def _resolve_md_settings(self) -> str:
        """The bot-managed MessageDisplay settings path, or "" on failure.

        Shared by ``create_window`` and the GH #65 deferred
        ``launch_claude_in_window`` so both compose the SAME command line.
        """
        from . import md_capture

        try:
            md_settings_path = md_capture.ensure_capture_settings()
            return str(md_settings_path) if md_settings_path.exists() else ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not prepare MessageDisplay capture settings: %s", e)
            return ""

    async def launch_claude_in_window(
        self, window_id: str, *, resume_session_id: str | None = None
    ) -> bool:
        """Send the deferred launch command into a ``defer_launch`` window.

        GH #65 Fix 0: the LAUNCH ALWAYS PROCEEDS regardless of the probe's
        outcome, immediately after the probe settles — the pane is still the
        flow-owned fresh shell and the probe output scrolls away under Claude's
        alt-screen. Composes the identical ``_compose_launch_command`` line
        ``create_window`` would have sent.
        """
        md_settings = await asyncio.to_thread(self._resolve_md_settings)
        cmd = _compose_launch_command(
            config.claude_command, md_settings, resume_session_id
        )
        return await self.send_keys(window_id, cmd, enter=True, literal=True)

    async def probe_cli_version(
        self,
        window_id: str,
        *,
        timeout: float = TRUST_VERSION_PROBE_TIMEOUT_S,
    ) -> str | None:
        """Run the nonce-delimited ``--version`` probe IN ``window_id``'s shell.

        GH #65 Fix 0. Returns the bare version string (``"2.1.241"``) on the
        positive ``N.N.N (Claude Code)`` proof between two whole-line nonce
        delimiters, else ``None`` — an un-extractable binary, a send failure, a
        timeout, a wrapper reporting its own version, or any capture failure all
        degrade to ``None``, which makes the trust card DISPLAY-ONLY. NEVER
        raises to the caller (except ``CancelledError``, which propagates), and
        NEVER blocks the launch.
        """
        nonce = secrets.token_hex(4).upper()
        nonce_a, nonce_b = f"CCTGVERA{nonce}", f"CCTGVERB{nonce}"
        probe = compose_version_probe(config.claude_command, nonce_a, nonce_b)
        if probe is None:
            logger.info(
                "trust version probe skipped: no binary token in CLAUDE_COMMAND "
                "(window=%s)",
                window_id,
            )
            return None
        try:
            if not await self.send_keys(window_id, probe, enter=True, literal=True):
                logger.info("trust version probe send failed (window=%s)", window_id)
                return None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                pane = await self.capture_pane(window_id)
                if pane:
                    version = parse_probe_version(pane, nonce_a, nonce_b)
                    if version is not None:
                        logger.info(
                            "trust version probe ok window=%s version=%s",
                            window_id,
                            version,
                        )
                        return version
                await asyncio.sleep(_PROBE_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("trust version probe raised (window=%s): %s", window_id, e)
            return None
        logger.info("trust version probe timed out (window=%s)", window_id)
        return None

    async def pane_current_command(self, window_id: str) -> str | None:
        """Real-time read of a window's active-pane foreground command.

        Runs ``tmux display-message -p -t <wid> '#{pane_current_command}'`` as a
        FRESH subprocess (NOT the 1s ``list_windows`` cache) so the ``/update``
        restart gate sees the live value while polling for Claude Code to quit.
        stderr-checked — tmux / libtmux swallow errors silently (the repo
        gotcha, mirroring ``_cmd_send_literal`` / ``_cmd_resize_window``): a
        non-zero exit OR non-empty stderr returns ``None``, which the caller
        treats as "unknown → still running" (fail-closed).
        """
        tmux_bin = _TMUX_BIN if _TMUX_BIN is not None else "tmux"
        try:
            proc = await asyncio.create_subprocess_exec(
                tmux_bin,
                "display-message",
                "-p",
                "-t",
                window_id,
                "#{pane_current_command}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except Exception as e:
            logger.error(
                "pane_current_command subprocess failed for %s: %s", window_id, e
            )
            return None
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or err:
            logger.error(
                "tmux display-message failed for %s (rc=%s): %s",
                window_id,
                proc.returncode,
                err,
            )
            return None
        return stdout.decode("utf-8", errors="replace").strip()

    async def restart_claude_in_window(
        self,
        window_id: str,
        tracked_session_id: str,
        md_settings: str,
        *,
        claude_command: str,
        idle_recheck: Callable[[], Awaitable[bool]],
        reassociate: Callable[[], Awaitable[None]],
        quit_keys: str = "/exit",
        shell_poll_timeout_s: float = _SHELL_POLL_TIMEOUT_S,
        shell_poll_grace_s: float = _SHELL_POLL_GRACE_S,
        shell_poll_interval_s: float = 0.3,
        claude_confirm_timeout_s: float = RELAUNCH_CONFIRM_TIMEOUT_S,
        relaunch_settle_s: float = 1.0,
    ) -> RestartOutcome:
        """Restart Claude Code IN PLACE inside its existing tmux window.

        The ``/update`` per-window mechanic — quit Claude, wait for the pane to
        drop to a shell, relaunch ``claude_command --resume <tracked_session_id>``
        so it adopts the freshly-updated on-disk version. Runs ENTIRELY inside
        the per-window send lock (the same lock every keystroke path takes), with
        the ``esc_command`` reject-if-held pattern so a concurrent send / pick
        dispatch is never interrupted.

        ``claude_command`` is the launch command to relaunch with (threaded from
        the caller — the SAME value passed to ``run_update``, not read from the
        global config here, so the relaunch target is an explicit contract).

        Collaborators are INJECTED so this stays a tmux leaf:
          - ``idle_recheck()`` — re-checks the route is genuinely idle (run-state
            + pane ground-truth) INSIDE the lock, immediately before quitting.
          - ``reassociate()`` — re-associates routing (ws.session_id override +
            monitor offset at the post-relaunch stat-stable EOF — the bounded
            stat-until-stable loop lives in the collaborator), also inside the
            lock, only after a successful relaunch.

        FAIL-CLOSED: if the pane does not become a shell within the TWO-phase
        bounded wait — ``shell_poll_timeout_s`` (primary) plus
        ``shell_poll_grace_s`` (grace) — the relaunch is ABORTED (never launch
        into a live TUI). The grace exists because ``/exit`` is IRREVOCABLE: a
        pane that drops to a shell only after the primary window is recovered
        with a normal relaunch (INFO-logged) instead of stranding a bare shell
        in a still-bound topic. After the relaunch keystroke, Claude is
        CONFIRMED by a second bounded poll (``claude_confirm_timeout_s``) for
        the strict version-string proof (``pane_command_is_claude``) — the
        keystroke being accepted is not launch proof (r2 P1-A); an unconfirmed
        relaunch keeps the quarantine and returns ``RELAUNCH_UNCONFIRMED``
        (a late boot self-heals at the next send's re-check). Every
        ``send_keys`` return is checked (it returns False silently on a
        vanished window). A ``reassociate()`` raise AFTER a confirmed relaunch
        is caught and returned as ``ERROR`` (never propagated — the caller's
        per-window isolation still records it; Claude is proven alive, so it
        is NOT quarantined).
        """
        # Lazy import dodges the tmux_manager ← callback_dispatcher module-level
        # cycle (callback_dispatcher.interactive imports this module). The busy
        # check + acquire pair below has NO await between them, so it is a
        # genuine try-acquire (see ``_lock_busy``'s contract).
        from .callback_dispatcher.interactive import _lock_busy

        lock = self.window_send_lock(window_id)
        if _lock_busy(lock):
            logger.info("restart %s: send lock busy — skipping", window_id)
            return RestartOutcome.SKIPPED_BUSY_LOCKED
        async with lock:
            # (1) Re-check idle INSIDE the lock. A concurrent user send can't
            # race us (it takes this SAME lock), but the monitor poll could have
            # ingested a fresh generation since the caller's cheap pre-gate.
            if not await idle_recheck():
                logger.info("restart %s: not idle at lock time — skipping", window_id)
                return RestartOutcome.SKIPPED_NOT_IDLE

            # (2) Quit keystroke (A.4: "/exit" + Enter cleanly quits Claude).
            if not await self.send_keys(window_id, quit_keys, enter=True, literal=True):
                logger.warning(
                    "restart %s: quit keystroke send failed (window gone?)", window_id
                )
                return RestartOutcome.ERROR

            # (3) Poll the real-time pane command until it becomes a shell.
            # FAIL-CLOSED: never relaunch on top of a still-live TUI. TWO-phase
            # wait: ``/exit`` is already irrevocably sent, so a LATE exit inside
            # the grace extension must be recovered with a normal relaunch —
            # aborting at the primary window would strand a bare shell in a
            # still-bound topic that executes the next Telegram message.
            start = time.monotonic()
            primary_deadline = start + shell_poll_timeout_s
            final_deadline = primary_deadline + shell_poll_grace_s
            last_cmd: str | None = None
            became_shell = False
            while time.monotonic() < final_deadline:
                await asyncio.sleep(shell_poll_interval_s)
                last_cmd = await self.pane_current_command(window_id)
                if pane_command_is_shell(last_cmd):
                    became_shell = True
                    break
            if not became_shell:
                logger.warning(
                    "restart %s: pane never became a shell within %.1fs "
                    "(primary %.1fs + grace %.1fs; last cmd=%r) — ABORTING, not "
                    "relaunching; the session may be dead — a post-deadline "
                    "/exit leaves a bare shell in the still-bound window",
                    window_id,
                    shell_poll_timeout_s + shell_poll_grace_s,
                    shell_poll_timeout_s,
                    shell_poll_grace_s,
                    last_cmd,
                )
                # /exit is already out; the pane may still drop to a bare
                # shell later — refuse user sends until proven alive (P1).
                self.mark_window_quarantined(window_id)
                return RestartOutcome.SKIPPED_NO_EXIT
            if time.monotonic() > primary_deadline:
                logger.info(
                    "restart %s: LATE exit — pane dropped to a shell %.1fs "
                    "after /exit (past the %.1fs primary window); recovering "
                    "with a normal relaunch",
                    window_id,
                    time.monotonic() - start,
                    shell_poll_timeout_s,
                )

            # (4) Relaunch with --resume so the resumed process adopts the new
            # on-disk version (A.0 result #6).
            cmd_line = _compose_launch_command(
                claude_command, md_settings, tracked_session_id
            )
            if not await self.send_keys(window_id, cmd_line, enter=True, literal=True):
                logger.error("restart %s: relaunch keystroke send failed", window_id)
                # The pane was CONFIRMED a bare shell and no Claude was
                # launched into it — quarantine (P1).
                self.mark_window_quarantined(window_id)
                return RestartOutcome.ERROR

            # (4b) CONFIRM Claude actually came up (r2 P1-A): the keystroke
            # being accepted is not launch proof — a broken CLAUDE_COMMAND /
            # auth failure / instant crash drops straight back to the shell,
            # and clearing the quarantine on the keystroke alone would strand
            # a bare shell with no refusal net. Poll for the strict positive
            # version-string proof (pane_command_is_claude — never "any
            # non-shell").
            confirm_deadline = time.monotonic() + claude_confirm_timeout_s
            confirm_cmd: str | None = None
            claude_confirmed = False
            while time.monotonic() < confirm_deadline:
                await asyncio.sleep(shell_poll_interval_s)
                confirm_cmd = await self.pane_current_command(window_id)
                if pane_command_is_claude(confirm_cmd):
                    claude_confirmed = True
                    break
            if not claude_confirmed:
                logger.error(
                    "restart %s: relaunch typed but Claude was not observed "
                    "running within %.1fs (last cmd=%r) — window stays "
                    "QUARANTINED; a late boot self-heals at the next send's "
                    "re-check",
                    window_id,
                    claude_confirm_timeout_s,
                    confirm_cmd,
                )
                self.mark_window_quarantined(window_id)
                return RestartOutcome.RELAUNCH_UNCONFIRMED
            # Claude OBSERVED running — a (possibly pre-existing) quarantine
            # is resolved by this positive proof; the reassociate-failure
            # ERROR below stays unquarantined (Claude is alive).
            self.clear_window_quarantine(
                window_id, reason="claude confirmed post-relaunch"
            )

            # (5) Give the resumed transcript a head start, then re-associate
            # routing (ws override + monitor offset at the post-replay
            # stat-stable EOF — the settle loop lives in ``reassociate``). A
            # re-association raise here is caught (the relaunch already
            # succeeded) and surfaced as ERROR — never propagated to abort the
            # caller's per-window sweep (Hermes P2).
            await asyncio.sleep(relaunch_settle_s)
            try:
                await reassociate()
            except Exception:
                logger.exception(
                    "restart %s: reassociation failed after relaunch", window_id
                )
                return RestartOutcome.ERROR
            logger.info(
                "restart %s: relaunched 'claude --resume %s'",
                window_id,
                tracked_session_id,
            )
            return RestartOutcome.RESTARTED


# Global instance with default session name
tmux_manager = TmuxManager()
