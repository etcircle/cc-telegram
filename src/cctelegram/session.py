"""Claude Code session management — the core state hub.

Manages the key mappings:
  Window→Session (window_states): which Claude session_id a window holds (keyed by window_id).
  User→Thread→Window (thread_bindings): topic-to-window bindings (1 topic = 1 window_id).

Responsibilities:
  - Persist/load state to ~/.cc-telegram/state.json.
  - Sync window↔session bindings from session_map.json (written by hook).
  - Resolve window IDs to ClaudeSession objects (JSONL file reading).
  - Track per-user read offsets for unread-message detection.
  - Manage thread↔window bindings for Telegram topic routing.
  - Send keystrokes to tmux windows and retrieve message history.
  - Maintain window_id→display name mapping for UI display.
  - Re-resolve stale window IDs on startup (tmux server restart recovery).

Key class: SessionManager (singleton instantiated as `session_manager`).
Key methods for thread binding access:
  - resolve_window_for_thread: Get window_id for a user's thread
  - iter_thread_bindings: Generator for iterating all (user_id, thread_id, window_id)
  - find_users_for_session: Find all users bound to a session_id
"""

import asyncio
import fcntl
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import aiofiles

from . import delivery, terminal_parser
from .config import config
from .delivery import DeliveryOutcome, DeliveryResult, UserTurnStamp
from .tmux_manager import pane_command_is_claude, tmux_manager
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json

logger = logging.getLogger(__name__)


# ── The GH #50 delivery gate: bounds + sentinels ─────────────────────────
# Every probe is BOUNDED (``capture_pane`` / ``pane_current_command`` shell out
# to tmux with no timeout of their own): a stalled probe must never let a STALE
# frame authorize the Enter (r2 F4). Only ``asyncio.TimeoutError`` classifies.
GATE_CAPTURE_DEADLINE_S = 2.5
# The same bound for the ``pane_current_command`` proof-of-life probe. It runs
# TWICE per delivery: at the pre-write gate, and — FIRST, before the capture —
# at the re-verify, so the pane capture is the LAST observation before the Enter
# (r2 F4: a stalled probe let a stale input-box frame authorize the commit).
CMD_PROBE_DEADLINE_S = 2.5
# An INDETERMINATE frame (a mid-redraw capture) is retried; a POSITIVE hazard
# refuses on the FIRST capture. The /cost preflight precedent.
GATE_CAPTURE_RETRIES = 2
GATE_RETRY_DELAY_S = 0.3
# Overall transaction budget, checked at the phase boundaries (never as a
# ``wait_for`` around the WRITE — cancelling mid-write would leave a half-typed
# payload). Exhaustion before the write ⇒ not_written; after ⇒ draft_written.
DELIVERY_DEADLINE_S = 20.0
# Mirrors ``tmux_manager.send_keys``'s own settles so the withheld-Enter writer
# reproduces the shipped timing exactly.
BASH_MODE_SETTLE_S = 1.0
TEXT_SETTLE_S = 0.5

# Sentinel distinguishing "the bounded capture timed out" from "tmux returned
# no pane" (``None``) — the two classify differently.
delivery_CAPTURE_TIMEOUT: object = object()
# The same, for the bounded ``pane_current_command`` probe.
delivery_CMD_TIMEOUT: object = object()


# ── The stranded-draft brake (r2 F2) ─────────────────────────────────────
#
# A ``DRAFT_WRITTEN`` / ``COMMIT_UNKNOWN`` transaction leaves the payload sitting
# in the input box with its Enter withheld — and the user is TOLD it was not
# delivered. But a live input box holding a pre-existing draft is legitimately
# DELIVERABLE (rig D10, a hard non-regression), so without a brake the very next
# message passed the gate, was APPENDED to the stranded text, and its Enter
# committed BOTH — silently submitting the message the bot had already disclaimed,
# concatenated with the new one.
#
# So a window that carries a bot-stranded draft is marked, and further payloads
# are REFUSED until the draft is provably gone. Released ONLY on positive proof:
#   (a) ``terminal_parser.pane_input_row_empty`` True on a fresh capture (the user
#       cleared it; an INDETERMINATE frame KEEPS the brake) — the self-heal in
#       ``_stranded_draft_gate`` below; or
#   (b) the WINDOW IS PROVABLY DEAD (a confirmed ``kill_window``) or brand-new
#       (``create_window``).
# NEVER auto-cleared with a keystroke: Esc has surface-specific semantics (on
# folder-trust it KILLS Claude) and mid-run it interrupts.
#
# The REGISTRY LIVES IN ``tmux_manager`` (peer-review P1), beside the post-/exit
# quarantine it mirrors, because the brake is a property of the PANE'S CONTENTS —
# not of a topic binding. Topic teardown must NOT clear it: ``/unbind``
# deliberately leaves the window ALIVE, and the teardown seams hold no
# ``window_send_lock``, so clearing there let a send already BLOCKED on that lock
# proceed straight onto the leftover draft and commit both. See the "brake
# lifecycle" comment in ``tmux_manager`` for the full rule + the residuals. These
# four names stay here as the delivery-path vocabulary (and the test seam).
#
# DISCLOSED RESIDUAL: a bot restart wipes it, so a draft stranded before the
# restart is no longer braked and the next message can concatenate onto it
# (identical to the quarantine registry's restart residual).


def mark_window_stranded_draft(window_id: str) -> None:
    """Arm the brake: this window's input box holds a bot-written, unsent draft."""
    tmux_manager.mark_window_stranded_draft(window_id)


def window_has_stranded_draft(window_id: str) -> bool:
    return tmux_manager.window_has_stranded_draft(window_id)


def clear_stranded_draft(window_id: str, *, reason: str) -> None:
    """Release the brake — empty-input-row proof ONLY (window death is tmux's seam)."""
    tmux_manager.clear_window_stranded_draft(window_id, reason=reason)


def reset_stranded_drafts_for_tests() -> None:
    tmux_manager.reset_stranded_drafts_for_tests()


class _WriteAttempt:
    """Did the gated transaction ATTEMPT a literal write into the input box?

    The brake used to be armed ONLY from the RETURNED ``DeliveryResult``
    (``result.draft_stranded``), so a ``CancelledError`` — or any unexpected
    exception — raised AFTER the payload was typed but BEFORE the Enter left the
    transaction without a result and the brake UNARMED: the next delivery passed
    the gate (a box holding a draft IS a writable box), APPENDED its own text and
    committed BOTH with its Enter. That is exactly the F2 hazard the brake exists
    to close, re-entered through the cancellation door — and it is reachable in
    production (``cleanup.clear_topic_state`` cancels per-topic tasks; shutdown
    cancels in-flight work; and a cancelled ``asyncio.to_thread``/subprocess await
    can still have COMPLETED its tmux write).

    The flag is set immediately BEFORE the first ``send_keys`` write — never
    after — because a write whose await is cancelled may still have landed. It is
    the SAME information the ``DRAFT_WRITTEN`` classification already uses (r2 F5:
    every post-write-attempt failure classifies WRITTEN), so arming on it adds no
    new imprecision. A raise BEFORE any write attempt leaves it False and does NOT
    arm the brake — the hard non-regression: a raise proves nothing about the
    pane, and arming there would false-refuse a HUMAN's pre-existing draft after
    an unrelated tmux error.
    """

    __slots__ = ("attempted",)

    def __init__(self) -> None:
        self.attempted = False


def _stamp_user_turn(stamp: UserTurnStamp) -> None:
    """The pre-commit hook body (plan §1.5) — the ONE mutation the send lock allows.

    Deferred import: ``handlers.message_queue`` imports ``session`` at module
    level, so a top-level import here would cycle (the repo's subprocess
    import-cycle test pins it). The call is SYNCHRONOUS and cheap — it must not
    await, must not schedule work, and must not mutate anything else.
    """
    from .handlers.message_queue import set_route_user_turn_at

    set_route_user_turn_at(stamp.user_id, stamp.thread_id, stamp.window_id)


# Post-/exit quarantine refusal (Hermes P1). The EXACT string is the contract
# between ``send_to_window`` and the aggregator's in-topic disclosure
# (compared by equality, never substring) — see ``tmux_manager``'s quarantine
# registry. Covers both the confirmed-shell and the unknown (query-failed)
# pane state, hence the hedged wording.
QUARANTINE_SEND_REFUSED_MSG = (
    "Message NOT delivered — the session appears to have exited during "
    "/update and the pane may be a bare shell. Check the window / restart "
    "the session, then resend."
)


# ── Bot-sent text tracking (for user-message dedup) ──────────────────────
#
# When the bot forwards a Telegram message into a tmux pane via
# ``send_to_window``, Claude logs that text as a user-role JSONL entry. If
# we then naively forward all user messages back to Telegram, every
# Telegram-typed prompt is echoed as a "👤 …" bubble.
#
# We avoid that by recording each bot-originated send keyed by session_id
# and consuming the entry when the matching JSONL line shows up. Direct
# typing into the tmux pane (which never goes through ``send_to_window``)
# is still surfaced — that's the whole point of CC_TELEGRAM_SHOW_USER_MESSAGES.

# How long a recorded send is allowed to remain unmatched before it expires.
# Long enough to absorb monitor poll latency and JSONL flush delay; short
# enough that an unrelated repeat of the same text minutes later still echoes.
BOT_SEND_DEDUP_TTL_SECONDS = 60.0

# Cap per-session deque length so a runaway logger doesn't grow without bound.
BOT_SEND_DEDUP_MAX_PER_SESSION = 32

# session_id -> deque[(monotonic_ts, normalized_text)]
_bot_sent_by_session: dict[str, deque[tuple[float, str]]] = {}


def _normalize_for_dedup(text: str) -> str:
    """Canonical form for comparing bot sends to JSONL user-message text."""
    return text.strip()


def _prune_expired_sends(buf: deque[tuple[float, str]], now: float) -> None:
    while buf and (now - buf[0][0]) > BOT_SEND_DEDUP_TTL_SECONDS:
        buf.popleft()


def _track_bot_sent_text(session_id: str, text: str) -> None:
    """Record a successful bot send for later user-message dedup."""
    normalized = _normalize_for_dedup(text)
    if not normalized:
        return
    buf = _bot_sent_by_session.setdefault(
        session_id, deque(maxlen=BOT_SEND_DEDUP_MAX_PER_SESSION)
    )
    now = time.monotonic()
    _prune_expired_sends(buf, now)
    buf.append((now, normalized))


def consume_bot_sent_text(session_id: str, text: str) -> bool:
    """Return True iff this user-message text matches a recent bot send.

    Consumes (removes) the matched entry so a *second* identical user
    message doesn't get suppressed by a single recorded send. Used by
    ``session_monitor`` to drop echoes the user already saw in Telegram.
    """
    buf = _bot_sent_by_session.get(session_id)
    if not buf:
        return False
    now = time.monotonic()
    _prune_expired_sends(buf, now)
    target = _normalize_for_dedup(text)
    if not target:
        return False
    for i, (_, recorded) in enumerate(buf):
        if recorded == target:
            del buf[i]
            return True
    return False


def reset_bot_send_tracking() -> None:
    """Test-only: drop all recorded bot sends."""
    _bot_sent_by_session.clear()


@dataclass
class WindowState:
    """Persistent state for a tmux window.

    Attributes:
        session_id: Associated Claude session ID (empty if not yet detected)
        cwd: Working directory for direct file path construction
        window_name: Display name of the window
    """

    session_id: str = ""
    cwd: str = ""
    window_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "cwd": self.cwd,
        }
        if self.window_name:
            d["window_name"] = self.window_name
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowState":
        return cls(
            session_id=data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
        )


@dataclass
class ClaudeSession:
    """Information about a Claude Code session."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


@dataclass
class SessionManager:
    """Manages session state for Claude Code.

    All internal keys use window_id (e.g. '@0', '@12') for uniqueness.
    Display names (window_name) are stored separately for UI presentation.

    window_states: window_id -> WindowState (session_id, cwd, window_name)
    user_window_offsets: user_id -> {window_id -> byte_offset}
    thread_bindings: user_id -> {thread_id -> window_id}
    window_display_names: window_id -> window_name (for display)
    group_chat_ids: "user_id:thread_id" -> group chat_id (for supergroup routing)
    """

    window_states: dict[str, WindowState] = field(default_factory=dict)
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    thread_bindings: dict[int, dict[int, str]] = field(default_factory=dict)
    # "<chat_id>:<owner_user_id>" -> {"thread_id": int, "msg_id": int,
    # "pinned": bool} — the Wave C cross-topic dashboard record (one dashboard
    # message per (chat, owner)). Owned HERE so it round-trips through the ONE
    # _load_state/_save_state path; the unknown-key-dropping state.json
    # rewrite is exactly why an ad-hoc second writer is forbidden.
    dashboards: dict[str, dict[str, Any]] = field(default_factory=dict)
    # user_id -> {"verbosity": str, "<knob>": value, ...} — per-user output
    # verbosity settings (plan v4 PR-1; resolved by handlers/output_prefs).
    # Owned HERE for the same reason as dashboards: the unknown-key-dropping
    # state.json rewrite means only SessionManager fields round-trip. Values
    # are validated permissively on load (shape only) — output_prefs
    # re-validates knob values on every read, so stale junk is inert.
    # Downgrade loss is ACCEPTED: an older binary saving state.json drops
    # this key; settings are re-settable preferences with no correctness
    # coupling (plan v4 §6 / codex r1 P2-5).
    user_settings: dict[int, dict[str, Any]] = field(default_factory=dict)
    # window_id -> display name (window_name)
    window_display_names: dict[str, str] = field(default_factory=dict)
    # "user_id:thread_id" -> group chat_id (for supergroup forum topic routing)
    # IMPORTANT: This mapping is essential for supergroup/forum topic support.
    # Telegram Bot API requires group chat_id (negative number like -100xxx)
    # as the chat_id parameter when sending messages to forum topics.
    # Using user_id as chat_id will fail with "Message thread not found".
    # See: https://core.telegram.org/bots/api#sendmessage
    # History: originally added in 5afc111, erroneously removed in 26cb81f,
    # restored in PR #23.
    group_chat_ids: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load_state()

    def _save_state(self) -> None:
        state: dict[str, Any] = {
            "window_states": {k: v.to_dict() for k, v in self.window_states.items()},
            "user_window_offsets": {
                str(uid): offsets for uid, offsets in self.user_window_offsets.items()
            },
            "thread_bindings": {
                str(uid): {str(tid): wid for tid, wid in bindings.items()}
                for uid, bindings in self.thread_bindings.items()
            },
            "window_display_names": self.window_display_names,
            "group_chat_ids": self.group_chat_ids,
            "dashboards": self.dashboards,
            "user_settings": {
                str(uid): settings for uid, settings in self.user_settings.items()
            },
        }
        atomic_write_json(config.state_file, state)
        logger.debug("State saved to %s", config.state_file)

    def _is_window_id(self, key: str) -> bool:
        """Check if a key looks like a tmux window ID (e.g. '@0', '@12')."""
        return key.startswith("@") and len(key) > 1 and key[1:].isdigit()

    def _load_state(self) -> None:
        """Load state synchronously during initialization."""
        if config.state_file.exists():
            try:
                state = json.loads(config.state_file.read_text())
                self.window_states = {
                    k: WindowState.from_dict(v)
                    for k, v in state.get("window_states", {}).items()
                }
                self.user_window_offsets = {
                    int(uid): offsets
                    for uid, offsets in state.get("user_window_offsets", {}).items()
                }
                self.thread_bindings = {
                    int(uid): {int(tid): wid for tid, wid in bindings.items()}
                    for uid, bindings in state.get("thread_bindings", {}).items()
                }
                self.window_display_names = state.get("window_display_names", {})
                self.group_chat_ids = {
                    k: int(v) for k, v in state.get("group_chat_ids", {}).items()
                }
                self.dashboards = self._parse_dashboards(state.get("dashboards", {}))
                self.user_settings = self._parse_user_settings(
                    state.get("user_settings", {})
                )

            except (json.JSONDecodeError, ValueError, OSError) as e:
                # OSError included (finding 19): the singleton constructs at
                # module import, so an unreadable state.json (permissions /
                # I/O error) must degrade to empty state instead of putting
                # launchd into a crash loop.
                logger.warning("Failed to load state: %s", e)
                self.window_states = {}
                self.user_window_offsets = {}
                self.thread_bindings = {}
                self.window_display_names = {}
                self.group_chat_ids = {}
                self.dashboards = {}
                self.user_settings = {}
                pass

    @staticmethod
    def _parse_user_settings(raw: Any) -> dict[int, dict[str, Any]]:
        """Validate the persisted ``user_settings`` map shape; drop malformed.

        Shape-only validation (int-able key, dict value) — knob VALUES are
        re-validated by ``output_prefs`` on every read, so an unknown or
        stale knob entry is inert rather than load-fatal.
        """
        parsed: dict[int, dict[str, Any]] = {}
        if not isinstance(raw, dict):
            return parsed
        for key, rec in raw.items():
            try:
                uid = int(key)
                if not isinstance(rec, dict):
                    raise ValueError("record is not a dict")
                parsed[uid] = dict(rec)
            except (TypeError, ValueError) as e:
                logger.warning("Dropping malformed user_settings entry %r: %s", key, e)
        return parsed

    @staticmethod
    def _parse_dashboards(raw: Any) -> dict[str, dict[str, Any]]:
        """Validate the persisted ``dashboards`` map; drop malformed entries.

        A malformed entry (bad key shape / non-int ids) is dropped rather than
        failing the whole load — the user just re-runs ``/dashboard``.
        """
        parsed: dict[str, dict[str, Any]] = {}
        if not isinstance(raw, dict):
            return parsed
        for key, rec in raw.items():
            try:
                chat_str, owner_str = str(key).split(":", 1)
                _ = (int(chat_str), int(owner_str))  # key-shape validation
                if not isinstance(rec, dict):
                    raise ValueError("record is not a dict")
                parsed[str(key)] = {
                    "thread_id": int(rec["thread_id"]),
                    "msg_id": int(rec["msg_id"]),
                    "pinned": bool(rec.get("pinned", False)),
                }
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Dropping malformed dashboard entry %r: %s", key, e)
        return parsed

    async def resolve_stale_ids(self) -> None:
        """Re-resolve persisted window IDs against live tmux windows.

        Called on startup. Handles stale IDs: a window_id no longer exists
        but its display name matches a live window, so the entry is re-pointed
        at the new id (or dropped if no live window matches).

        Builds {window_name: window_id} from live windows, then remaps or drops
        entries. Pre-2026-02-11 ``window_name``-keyed entries are no longer
        migrated; any such legacy keys found on load are dropped with a one-shot
        per-map warning (the live hook only ever emits ``@N`` keys).
        """
        windows = await tmux_manager.list_windows()
        live_by_name: dict[str, str] = {}  # window_name -> window_id
        live_ids: set[str] = set()
        for w in windows:
            live_by_name[w.window_name] = w.window_id
            live_ids.add(w.window_id)

        changed = False
        # Stale display_names keys whose pop must wait until thread_bindings
        # and user_window_offsets have finished their display-name lookups —
        # otherwise their lookups by stale window_id miss and the bindings /
        # offsets are silently dropped.
        stale_display_name_keys: set[str] = set()

        # --- Re-resolve window_states (window_id keys only) ---
        legacy_window_state_keys = [
            key for key in self.window_states if not self._is_window_id(key)
        ]
        if legacy_window_state_keys:
            logger.warning(
                "dropping legacy window_name-keyed %s entries: %s",
                "window_states",
                sorted(legacy_window_state_keys),
            )
            changed = True
        new_window_states: dict[str, WindowState] = {}
        for key, ws in self.window_states.items():
            if not self._is_window_id(key):
                # Pre-2026-02-11 window_name-keyed legacy entry — dropped.
                continue
            if key in live_ids:
                new_window_states[key] = ws
            else:
                # Stale ID — try re-resolve by display name
                display = self.window_display_names.get(key, ws.window_name or key)
                new_id = live_by_name.get(display)
                if new_id:
                    logger.info(
                        "Re-resolved stale window_id %s -> %s (name=%s)",
                        key,
                        new_id,
                        display,
                    )
                    new_window_states[new_id] = ws
                    ws.window_name = display
                    self.window_display_names[new_id] = display
                    stale_display_name_keys.add(key)
                    changed = True
                else:
                    logger.info(
                        "Dropping stale window_state: %s (name=%s)", key, display
                    )
                    changed = True
        self.window_states = new_window_states

        # --- Re-resolve thread_bindings (window_id values only) ---
        legacy_thread_binding_vals = sorted(
            {
                val
                for bindings in self.thread_bindings.values()
                for val in bindings.values()
                if not self._is_window_id(val)
            }
        )
        if legacy_thread_binding_vals:
            logger.warning(
                "dropping legacy window_name-keyed %s entries: %s",
                "thread_bindings",
                legacy_thread_binding_vals,
            )
            changed = True
        for uid, bindings in self.thread_bindings.items():
            new_bindings: dict[int, str] = {}
            for tid, val in bindings.items():
                if not self._is_window_id(val):
                    # Pre-2026-02-11 window_name-keyed legacy binding — dropped.
                    continue
                if val in live_ids:
                    new_bindings[tid] = val
                else:
                    display = self.window_display_names.get(val, val)
                    new_id = live_by_name.get(display)
                    if new_id:
                        logger.info(
                            "Re-resolved thread binding %s -> %s (name=%s)",
                            val,
                            new_id,
                            display,
                        )
                        new_bindings[tid] = new_id
                        self.window_display_names[new_id] = display
                        changed = True
                    else:
                        logger.info(
                            "Dropping stale thread binding: user=%d, thread=%d, wid=%s",
                            uid,
                            tid,
                            val,
                        )
                        changed = True
            self.thread_bindings[uid] = new_bindings

        # Remove empty user entries
        empty_users = [uid for uid, b in self.thread_bindings.items() if not b]
        for uid in empty_users:
            del self.thread_bindings[uid]

        # --- Re-resolve user_window_offsets (window_id keys only) ---
        legacy_offset_keys = sorted(
            {
                key
                for offsets in self.user_window_offsets.values()
                for key in offsets
                if not self._is_window_id(key)
            }
        )
        if legacy_offset_keys:
            logger.warning(
                "dropping legacy window_name-keyed %s entries: %s",
                "user_window_offsets",
                legacy_offset_keys,
            )
            changed = True
        for uid, offsets in self.user_window_offsets.items():
            new_offsets: dict[str, int] = {}
            for key, offset in offsets.items():
                if not self._is_window_id(key):
                    # Pre-2026-02-11 window_name-keyed legacy offset — dropped.
                    continue
                if key in live_ids:
                    new_offsets[key] = offset
                else:
                    display = self.window_display_names.get(key, key)
                    new_id = live_by_name.get(display)
                    if new_id:
                        new_offsets[new_id] = offset
                        changed = True
                    else:
                        changed = True
            self.user_window_offsets[uid] = new_offsets

        # Drop the stale window_id keys from display_names now that every
        # consumer has had a chance to look them up.
        for key in stale_display_name_keys:
            self.window_display_names.pop(key, None)

        if changed:
            self._save_state()
            logger.info("Startup re-resolution complete")

        # Clean up session_map.json: stale window IDs (no live tmux window).
        await self._cleanup_stale_session_map_entries(live_ids)

    async def _cleanup_stale_session_map_entries(self, live_ids: set[str]) -> None:
        """Remove entries for tmux windows that no longer exist.

        When windows are closed externally (outside CC Telegram), session_map.json
        retains orphan references. This cleanup removes entries whose window_id
        is not in the current set of live tmux windows.
        """
        if not config.session_map_file.exists():
            return

        prefix = f"{config.tmux_session_name}:"
        # Hold session_map.lock across read-modify-write so a concurrent hook
        # update can't be clobbered by writing back our stale snapshot.
        lock_path = config.session_map_file.with_suffix(".lock")
        try:
            with open(lock_path, "w") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                try:
                    try:
                        session_map = json.loads(config.session_map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        return

                    stale_keys = [
                        key
                        for key in session_map
                        if key.startswith(prefix)
                        and self._is_window_id(key[len(prefix) :])
                        and key[len(prefix) :] not in live_ids
                    ]
                    if not stale_keys:
                        return

                    for key in stale_keys:
                        del session_map[key]
                        logger.info("Removed stale session_map entry: %s", key)

                    atomic_write_json(config.session_map_file, session_map)
                    logger.info(
                        "Cleaned up %d stale session_map entries (windows no longer in tmux)",
                        len(stale_keys),
                    )
                finally:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
        except OSError as e:
            logger.warning("Failed to clean up stale session_map entries: %s", e)

    # --- Display name management ---

    def get_display_name(self, window_id: str) -> str:
        """Get display name for a window_id, fallback to window_id itself."""
        return self.window_display_names.get(window_id, window_id)

    def update_display_name(self, window_id: str, new_name: str) -> None:
        """Update the display name for a window and persist state."""
        self.window_display_names[window_id] = new_name
        # Also update WindowState.window_name if it exists
        if window_id in self.window_states:
            self.window_states[window_id].window_name = new_name
        self._save_state()
        logger.info("Updated display name: window_id %s -> '%s'", window_id, new_name)

    # --- Group chat ID management (supergroup forum topic routing) ---

    def set_group_chat_id(
        self, user_id: int, thread_id: int | None, chat_id: int
    ) -> None:
        """Store the group chat_id for a user+thread combination.

        In supergroups with forum topics, messages must be sent to the group's
        chat_id (negative number like -100xxx) rather than the user's personal ID.
        Telegram's Bot API rejects message_thread_id when chat_id is a private
        user ID — the thread only exists within the group context.

        DO NOT REMOVE this method or the group_chat_ids mapping.
        Without it, all outbound messages in forum topics fail with
        "Message thread not found". See commit history: 5afc111 → 26cb81f → PR #23.

        GH #41 sticky-when-BOUND overwrite guard: thread ids are chat-local, so
        a colliding (user, thread) message from a DIFFERENT forum would otherwise
        overwrite the mapping of a genuinely-bound topic in the first forum and
        misroute its outbound sends. When an entry already exists with a DIFFERENT
        chat_id AND the user has a live thread BINDING for that thread, the
        overwrite is REFUSED. This is a BOUND guard, not a liveness guard —
        ``get_window_for_thread`` reads ``thread_bindings`` only and does not prove
        the tmux window exists, so a STALE binding freezes the old mapping until
        the stale-window unbind clears the binding, after which the overwrite
        self-heals. No existing entry / no binding → write as today (the
        directory-browser bootstrap into a brand-new topic still works).
        """
        tid = thread_id or 0
        key = f"{user_id}:{tid}"
        existing = self.group_chat_ids.get(key)
        if existing is not None and existing != chat_id:
            if self.get_window_for_thread(user_id, tid) is not None:
                logger.warning(
                    "Refusing to overwrite bound group chat_id: user=%d thread=%s "
                    "old_chat=%d new_chat=%d (a colliding cross-forum thread id "
                    "cannot steal a bound topic's mapping)",
                    user_id,
                    thread_id,
                    existing,
                    chat_id,
                )
                return
        if existing != chat_id:
            self.group_chat_ids[key] = chat_id
            self._save_state()
            logger.debug(
                "Stored group chat_id: user=%d, thread=%s, chat_id=%d",
                user_id,
                thread_id,
                chat_id,
            )

    def resolve_chat_id(self, user_id: int, thread_id: int | None = None) -> int:
        """Resolve the correct chat_id for sending messages.

        Returns the stored group chat_id when a thread_id is present and a
        mapping exists, otherwise falls back to user_id (for private chats).

        Every outbound Telegram API call (send_message, edit_message_text,
        delete_message, send_chat_action, edit_forum_topic, etc.) MUST use
        this method instead of raw user_id. Using user_id directly breaks
        supergroup forum topic routing.
        """
        if thread_id is not None:
            key = f"{user_id}:{thread_id}"
            group_id = self.group_chat_ids.get(key)
            if group_id is not None:
                return group_id
        return user_id

    def get_group_chat_id(self, user_id: int, thread_id: int | None) -> int | None:
        """Return the stored group chat_id for (user, thread), or None.

        Unlike ``resolve_chat_id`` this NEVER falls back to ``user_id`` — it
        is the fail-closed lookup for chat-scoped surfaces (the Wave C
        dashboard render/teardown): an unresolvable mapping must mean
        "exclude", never "assume some chat".
        """
        return self.group_chat_ids.get(f"{user_id}:{thread_id or 0}")

    # --- Dashboard record management (Wave C cross-topic dashboard) ---
    #
    # One dashboard message per (chat_id, owner_user_id). All methods are
    # SYNCHRONOUS (mutate + _save_state, no await inside — the bind/unbind
    # style) so a caller holding the dashboard module's per-(chat, owner)
    # asyncio operation lock can persist without a suspension point. The
    # Telegram-I/O-spanning claim/move/self-heal flow lives in
    # ``handlers.dashboard``; this is only the durable record.

    @staticmethod
    def _dashboard_key(chat_id: int, owner_id: int) -> str:
        return f"{chat_id}:{owner_id}"

    def get_dashboard(self, chat_id: int, owner_id: int) -> dict[str, Any] | None:
        """Return a copy of the (chat, owner) dashboard record, or None."""
        rec = self.dashboards.get(self._dashboard_key(chat_id, owner_id))
        return dict(rec) if rec is not None else None

    def set_dashboard(
        self, chat_id: int, owner_id: int, thread_id: int, msg_id: int
    ) -> None:
        """Claim/move the (chat, owner) dashboard to ``thread_id``/``msg_id``.

        A fresh claim is never pinned — pinning is opt-in via
        ``/dashboard pin`` (``set_dashboard_pinned``).
        """
        self.dashboards[self._dashboard_key(chat_id, owner_id)] = {
            "thread_id": int(thread_id),
            "msg_id": int(msg_id),
            "pinned": False,
        }
        self._save_state()
        logger.info(
            "Dashboard set: chat=%d owner=%d thread=%d msg=%d",
            chat_id,
            owner_id,
            thread_id,
            msg_id,
        )

    def clear_dashboard(self, chat_id: int, owner_id: int) -> None:
        """Drop the (chat, owner) dashboard record (host topic dead / moved)."""
        if self.dashboards.pop(self._dashboard_key(chat_id, owner_id), None):
            self._save_state()
            logger.info("Dashboard cleared: chat=%d owner=%d", chat_id, owner_id)

    def update_dashboard_msg_id(self, chat_id: int, owner_id: int, msg_id: int) -> None:
        """Repoint the record at a re-sent message (edit-404 self-heal)."""
        rec = self.dashboards.get(self._dashboard_key(chat_id, owner_id))
        if rec is None:
            return
        rec["msg_id"] = int(msg_id)
        self._save_state()

    def set_dashboard_pinned(self, chat_id: int, owner_id: int, pinned: bool) -> None:
        """Record pin state — called only AFTER a successful pin API call."""
        rec = self.dashboards.get(self._dashboard_key(chat_id, owner_id))
        if rec is None:
            return
        rec["pinned"] = bool(pinned)
        self._save_state()

    # --- Per-user output settings (plan v4 PR-1) ---
    #
    # Synchronous mutate + _save_state, mirroring the dashboard record style.
    # Values are opaque here; handlers/output_prefs owns knob semantics and
    # validates on read.

    def get_user_settings(self, user_id: int) -> dict[str, Any]:
        """Return a copy of the user's stored output settings ({} if none)."""
        rec = self.user_settings.get(user_id)
        return dict(rec) if rec is not None else {}

    def set_user_setting(self, user_id: int, key: str, value: Any) -> None:
        """Set one stored knob/preset value and persist."""
        rec = self.user_settings.setdefault(user_id, {})
        rec[key] = value
        self._save_state()
        logger.info("User setting saved: user=%d %s=%r", user_id, key, value)

    def replace_user_settings(self, user_id: int, settings: dict[str, Any]) -> None:
        """Replace the user's stored settings wholesale (preset tap = clean
        slate: choosing a preset drops stale per-knob overrides)."""
        self.user_settings[user_id] = dict(settings)
        self._save_state()
        logger.info("User settings replaced: user=%d %r", user_id, settings)

    def iter_dashboards(self) -> Iterator[tuple[int, int, dict[str, Any]]]:
        """Iterate all dashboards as ``(chat_id, owner_id, record_copy)``."""
        for key, rec in list(self.dashboards.items()):
            try:
                chat_str, owner_str = key.split(":", 1)
                yield int(chat_str), int(owner_str), dict(rec)
            except ValueError:  # pragma: no cover - load already validates
                continue

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        """Poll session_map.json until an entry for window_id appears.

        Returns True if the entry was found within timeout, False otherwise.
        """
        logger.debug(
            "Waiting for session_map entry: window_id=%s, timeout=%.1f",
            window_id,
            timeout,
        )
        key = f"{config.tmux_session_name}:{window_id}"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                if config.session_map_file.exists():
                    async with aiofiles.open(config.session_map_file, "r") as f:
                        content = await f.read()
                    session_map = json.loads(content)
                    info = session_map.get(key, {})
                    if info.get("session_id"):
                        # Found — load into window_states immediately
                        logger.debug(
                            "session_map entry found for window_id %s", window_id
                        )
                        await self.load_session_map()
                        return True
            except (json.JSONDecodeError, OSError):
                pass
            await asyncio.sleep(interval)
        logger.warning(
            "Timed out waiting for session_map entry: window_id=%s", window_id
        )
        return False

    async def load_session_map(self) -> None:
        """Read session_map.json and update window_states with new session associations.

        Keys in session_map are formatted as "tmux_session:window_id" (e.g. "cc-telegram:@12").
        Only entries matching our tmux_session_name are processed.
        Also cleans up window_states entries not in current session_map.
        Updates window_display_names from the "window_name" field in values.
        """
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        prefix = f"{config.tmux_session_name}:"
        valid_wids: set[str] = set()
        changed = False

        for key, info in session_map.items():
            # Only process entries for our tmux session
            if not key.startswith(prefix):
                continue
            window_id = key[len(prefix) :]
            if not self._is_window_id(window_id):
                continue
            valid_wids.add(window_id)
            new_sid = info.get("session_id", "")
            new_cwd = info.get("cwd", "")
            new_wname = info.get("window_name", "")
            if not new_sid:
                continue
            state = self.get_window_state(window_id)
            if state.session_id != new_sid or state.cwd != new_cwd:
                logger.info(
                    "Session map: window_id %s updated sid=%s, cwd=%s",
                    window_id,
                    new_sid,
                    new_cwd,
                )
                state.session_id = new_sid
                state.cwd = new_cwd
                changed = True
            # Update display name
            if new_wname:
                state.window_name = new_wname
                if self.window_display_names.get(window_id) != new_wname:
                    self.window_display_names[window_id] = new_wname
                    changed = True

        # Clean up window_states entries not in current session_map.
        stale_wids = [w for w in self.window_states if w and w not in valid_wids]
        for wid in stale_wids:
            logger.info("Removing stale window_state: %s", wid)
            del self.window_states[wid]
            changed = True

        if changed:
            self._save_state()

    # --- Window state management ---

    def get_window_state(self, window_id: str) -> WindowState:
        """Get or create window state."""
        if window_id not in self.window_states:
            self.window_states[window_id] = WindowState()
        return self.window_states[window_id]

    def clear_window_session(self, window_id: str) -> None:
        """Clear session association for a window (e.g., after /clear command)."""
        state = self.get_window_state(window_id)
        state.session_id = ""
        self._save_state()
        logger.info("Cleared session for window_id %s", window_id)

    @staticmethod
    def _encode_cwd(cwd: str) -> str:
        """Encode a cwd path to match Claude Code's project directory naming.

        Replaces all non-alphanumeric characters (except dash) with dashes.
        E.g. /home/user_name/Code/project -> -home-user-name-Code-project
        """
        return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)

    def _build_session_file_path(self, session_id: str, cwd: str) -> Path | None:
        """Build the direct file path for a session from session_id and cwd."""
        if not session_id or not cwd:
            return None
        encoded_cwd = self._encode_cwd(cwd)
        return config.claude_projects_path / encoded_cwd / f"{session_id}.jsonl"

    async def _get_session_direct(
        self, session_id: str, cwd: str
    ) -> ClaudeSession | None:
        """Get a ClaudeSession directly from session_id and cwd (no scanning)."""
        file_path = self._build_session_file_path(session_id, cwd)

        # Fallback: glob search if direct path doesn't exist
        if not file_path or not file_path.exists():
            pattern = f"*/{session_id}.jsonl"
            matches = list(config.claude_projects_path.glob(pattern))
            if matches:
                file_path = matches[0]
                logger.debug("Found session via glob: %s", file_path)
            else:
                return None

        # Single pass: read file once, extract summary + count messages
        summary = ""
        last_user_msg = ""
        message_count = 0
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    message_count += 1
                    try:
                        data = json.loads(line)
                        # Check for summary
                        if data.get("type") == "summary":
                            s = data.get("summary", "")
                            if s:
                                summary = s
                        # Track last user message as fallback
                        elif TranscriptParser.is_user_message(data):
                            parsed = TranscriptParser.parse_message(data)
                            if parsed and parsed.text.strip():
                                last_user_msg = parsed.text.strip()
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None

        if not summary:
            summary = last_user_msg[:50] if last_user_msg else "Untitled"

        return ClaudeSession(
            session_id=session_id,
            summary=summary,
            message_count=message_count,
            file_path=str(file_path),
        )

    # --- Directory session listing ---

    async def list_sessions_for_directory(self, cwd: str) -> list[ClaudeSession]:
        """List existing Claude sessions for a directory.

        Encodes the cwd path to find the project directory under
        ~/.claude/projects/{encoded_cwd}/, globs *.jsonl files, and
        extracts summary info from each.

        Returns a list sorted by mtime (most recent first), capped at 10.
        """
        encoded_cwd = self._encode_cwd(cwd)
        project_dir = config.claude_projects_path / encoded_cwd
        if not project_dir.is_dir():
            return []

        # Collect JSONL files sorted by mtime (newest first)
        jsonl_files = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        # Skip sessions-index and cap at 10
        sessions: list[ClaudeSession] = []
        for f in jsonl_files:
            if f.stem == "sessions-index":
                continue
            if len(sessions) >= 10:
                break
            session_id = f.stem
            session = await self._get_session_direct(session_id, cwd)
            if session and session.message_count > 0:
                sessions.append(session)
        return sessions

    # --- Window → Session resolution ---

    async def resolve_session_for_window(self, window_id: str) -> ClaudeSession | None:
        """Resolve a tmux window to the best matching Claude session.

        Uses persisted session_id + cwd to construct file path directly.
        Returns None if no session is associated with this window.
        """
        state = self.get_window_state(window_id)

        if not state.session_id or not state.cwd:
            return None

        session = await self._get_session_direct(state.session_id, state.cwd)
        if session:
            return session

        # File no longer exists, clear state
        logger.warning(
            "Session file no longer exists for window_id %s (sid=%s, cwd=%s)",
            window_id,
            state.session_id,
            state.cwd,
        )
        state.session_id = ""
        state.cwd = ""
        self._save_state()
        return None

    # --- User window offset management ---

    def update_user_window_offset(
        self, user_id: int, window_id: str, offset: int
    ) -> None:
        """Update the user's last read offset for a window."""
        if user_id not in self.user_window_offsets:
            self.user_window_offsets[user_id] = {}
        self.user_window_offsets[user_id][window_id] = offset
        self._save_state()

    # --- Thread binding management ---

    def bind_thread(
        self, user_id: int, thread_id: int, window_id: str, window_name: str = ""
    ) -> None:
        """Bind a Telegram topic thread to a tmux window.

        Args:
            user_id: Telegram user ID
            thread_id: Telegram topic thread ID
            window_id: Tmux window ID (e.g. '@0')
            window_name: Display name for the window (optional)
        """
        if user_id not in self.thread_bindings:
            self.thread_bindings[user_id] = {}
        self.thread_bindings[user_id][thread_id] = window_id
        if window_name:
            self.window_display_names[window_id] = window_name
        self._save_state()
        display = window_name or self.get_display_name(window_id)
        logger.info(
            "Bound thread %d -> window_id %s (%s) for user %d",
            thread_id,
            window_id,
            display,
            user_id,
        )

    def unbind_thread(self, user_id: int, thread_id: int) -> str | None:
        """Remove a thread binding. Returns the previously bound window_id, or None."""
        bindings = self.thread_bindings.get(user_id)
        if not bindings or thread_id not in bindings:
            return None
        window_id = bindings.pop(thread_id)
        if not bindings:
            del self.thread_bindings[user_id]
        self._save_state()
        logger.info(
            "Unbound thread %d (was %s) for user %d",
            thread_id,
            window_id,
            user_id,
        )
        return window_id

    def get_window_for_thread(self, user_id: int, thread_id: int) -> str | None:
        """Look up the window_id bound to a thread."""
        bindings = self.thread_bindings.get(user_id)
        if not bindings:
            return None
        return bindings.get(thread_id)

    def resolve_window_for_thread(
        self,
        user_id: int,
        thread_id: int | None,
    ) -> str | None:
        """Resolve the tmux window_id for a user's thread.

        Returns None if thread_id is None or the thread is not bound.
        """
        if thread_id is None:
            return None
        return self.get_window_for_thread(user_id, thread_id)

    def iter_thread_bindings(self) -> Iterator[tuple[int, int, str]]:
        """Iterate all thread bindings as (user_id, thread_id, window_id).

        Provides encapsulated access to thread_bindings without exposing
        the internal data structure directly.
        """
        for user_id, bindings in self.thread_bindings.items():
            for thread_id, window_id in bindings.items():
                yield user_id, thread_id, window_id

    async def find_users_for_session(
        self,
        session_id: str,
    ) -> list[tuple[int, str, int]]:
        """Find all users whose thread-bound window maps to the given session_id.

        Returns list of (user_id, window_id, thread_id) tuples.

        Performance note: this used to call ``resolve_session_for_window``
        per binding, which opens and reads each window's full JSONL file
        just to get the session_id. With many bound windows and multi-MB
        JSONLs, that turned every callback in the session-monitor →
        handle_new_message hot path into seconds of file I/O — and the
        per-user message queue backlog grew minutes deep, deferring real
        Telegram delivery. ``window_states[wid].session_id`` is already
        kept in sync with session_map.json by ``load_session_map``, so we
        can answer this from the in-memory dict in microseconds.
        """
        result: list[tuple[int, str, int]] = []
        for user_id, thread_id, window_id in self.iter_thread_bindings():
            ws = self.window_states.get(window_id)
            if ws and ws.session_id and ws.session_id == session_id:
                result.append((user_id, window_id, thread_id))
        return result

    # --- Tmux helpers ---

    async def send_to_window(
        self,
        window_id: str,
        text: str,
        *,
        user_turn: UserTurnStamp | None = None,
    ) -> tuple[bool, str]:
        """Send text to a tmux window by ID (legacy ``(ok, message)`` shape).

        Thin wrapper over :meth:`deliver_to_window` for the synchronous callers
        that only surface the message. New callers that need the REASON (the
        aggregator flush + the pending-bind replay) use ``deliver_to_window``.
        """
        return (
            await self.deliver_to_window(window_id, text, user_turn=user_turn)
        ).as_tuple

    async def deliver_to_window(
        self,
        window_id: str,
        text: str,
        *,
        user_turn: UserTurnStamp | None = None,
    ) -> DeliveryResult:
        """Type ``text`` into a window and commit it with Enter — GATED (GH #50).

        THE SINGLE CHOKE POINT for every user payload that reaches a pane. The
        whole transaction runs under the per-window send lock, so concurrent
        sends to the SAME window (the aggregator's unawaited boundary-flush vs a
        cap-flush) serialize FIFO instead of interleaving keystrokes.

        Sequence (plan §1.2 — every step fail-closed):

          0. SEGMENT-aware hotkey refusal (``delivery.lone_hotkey_line``) —
             BEFORE any capture, so a lone-digit payload is NEVER written.
          1. Bounded, cancellation-safe pane capture (``capture_pane_cancellation_safe``
             under ``asyncio.wait_for``); the whole transaction carries an overall
             deadline.
          2. ``pane_command_is_claude`` — the strict version-string fullmatch, on
             a BOUNDED probe (r2 F4). Closes M3: ``/esc`` on a folder-trust prompt
             EXITS Claude, and a bare-shell pane would EXECUTE the payload as a
             shell command.
          2b. The stranded-draft brake (r2 F2) — refuse while a previous payload
             may still be sitting unsent in the input box; released only on a
             capture proving the input row is EMPTY.
          3. ``pane_input_box_present`` — the POSITIVE structural proof that the
             pane is at Claude's ready input box (bounded retry on an
             INDETERMINATE frame; a positive hazard refuses on the first capture).
          4. Write the text with the Enter WITHHELD (mode-aware: the ``!``
             bash-mode two-step is reproduced explicitly — ``send_keys`` performs
             it ONLY when ``literal and enter`` are BOTH true).
          5. Re-verify: the BOUNDED ``pane_command_is_claude`` probe FIRST, then
             the pane capture LAST (r2 F4 — the capture must be the final
             observation before the Enter, or a stalled probe lets a stale frame
             authorize a commit into a freshly-drawn prompt). The re-verify is
             payload-aware (``expected_draft``), so an ordinary ``1. buy milk``
             is not mistaken for a picker cursor.
          6. The pre-commit user-turn stamp (the ONE documented ``route_runtime``
             mutation allowed under this lock — see ``tmux_manager``'s contract).
          7. Enter. A failed Enter is ``COMMIT_UNKNOWN``, never "withheld".

        Residuals (bounded + disclosed, NOT closed):
          - gate → write: a prompt appearing in that window can still take a
            keystroke. Mitigated empirically (a multi-char payload written in ONE
            ``send-keys -l`` is consumed paste-shaped and is inert) and by step 0.
          - final capture → Enter: one tmux round-trip. No terminal protocol can
            make this atomic — the identical residual the shipped ``_dispatch_pick``
            / ``_dispatch_decision`` already accept.
          - a bot restart wipes the stranded-draft brake (in-memory, like the
            tmux quarantine registry).
        """
        display = self.get_display_name(window_id)
        logger.debug(
            "deliver_to_window: window_id=%s (%s), text_len=%d",
            window_id,
            display,
            len(text),
        )

        # (0) The lone-hotkey SEGMENT refusal — payload-only, no capture needed,
        # and it must never be written even onto an idle pane (the gate→write
        # window is exactly what makes it dangerous).
        hotkey = delivery.lone_hotkey_line(text)
        if hotkey is not None:
            logger.info(
                "DELIVERY REFUSED window=%s reason=%s outcome=not_written",
                window_id,
                delivery.REASON_LONE_HOTKEY,
            )
            return delivery.refuse(delivery.REASON_LONE_HOTKEY, written=False)

        # r2 F2 + the cancellation fold: the brake is armed through ONE seam, and
        # INSIDE the send lock — a queued send already waiting on
        # ``window_send_lock`` must never acquire it before the brake is up.
        # BOTH paths arm it: the normal ``draft_stranded`` return, and a raise
        # (cancellation or otherwise) that happened after a write was ATTEMPTED.
        async with tmux_manager.window_send_lock(window_id):
            write = _WriteAttempt()
            try:
                result = await self._deliver_locked(
                    window_id, text, display, user_turn, write
                )
            except BaseException:
                # CancelledError MUST propagate (never swallowed into a
                # DeliveryResult) — but the draft it may have left behind is
                # exactly as stranded as a DRAFT_WRITTEN one, so the brake goes
                # up first. Cancellation during the settle, the re-verify, the
                # stamp, or the ENTER await all land here: the Enter may not have
                # landed, and if it did the brake's empty-input-row self-heal
                # releases it on the next send (fail-closed, self-correcting).
                if write.attempted:
                    logger.warning(
                        "deliver_to_window: raised AFTER a write attempt on "
                        "window %s — arming the stranded-draft brake before "
                        "re-raising",
                        window_id,
                    )
                    mark_window_stranded_draft(window_id)
                raise
            if result.draft_stranded:
                mark_window_stranded_draft(window_id)

        if result.ok or result.outcome is DeliveryOutcome.COMMIT_UNKNOWN:
            # Record the bot-originated send so the session_monitor can
            # suppress the matching user-message echo from JSONL. Without
            # this, every Telegram-typed message gets re-delivered as a
            # "👤 …" bubble — pure duplication for our workflow.
            # COMMIT_UNKNOWN records too: if that Enter DID land, Claude logs the
            # text as a user entry and the echo must still be suppressed; if it
            # did not, the record simply expires unmatched (TTL 60s).
            state = self.window_states.get(window_id)
            if state and state.session_id:
                _track_bot_sent_text(state.session_id, text)
        if not result.ok:
            # §1.6 observability: ONE INFO per refusal carrying the machine
            # reason + the written-state classification. NEVER pane text, NEVER
            # message content.
            logger.info(
                "DELIVERY REFUSED window=%s reason=%s outcome=%s",
                window_id,
                result.reason,
                result.outcome.value,
            )
        return result

    async def _deliver_locked(
        self,
        window_id: str,
        text: str,
        display: str,
        user_turn: UserTurnStamp | None,
        write: _WriteAttempt,
    ) -> DeliveryResult:
        """The gated delivery transaction. Caller holds ``window_send_lock``.

        ``write`` is the caller's write-attempt probe: this method SETS it
        immediately before the first literal ``send_keys``, so a cancellation /
        unexpected raise anywhere from that instant on (the settle, the
        re-verify, the stamp, the Enter) still arms the stranded-draft brake in
        ``deliver_to_window``'s handler — see ``_WriteAttempt``.
        """
        deadline = time.monotonic() + DELIVERY_DEADLINE_S

        window = await tmux_manager.find_window_by_id(window_id)
        if not window:
            return delivery.refuse(delivery.REASON_WINDOW_GONE, written=False)

        # (2) Proof of life. Runs on EVERY send now (not just quarantined
        # windows): "not a shell" is NOT proof — a user who followed "check the
        # window" may be running vim/python/ssh there, and typing user text +
        # Enter would land in THAT program. A quarantined window keeps its own
        # explicit refusal copy (the exact-string contract the aggregator
        # disclosure matches on).
        quarantined = tmux_manager.window_quarantined(window_id)
        cmd = await self._pane_command_for_gate(window_id)
        if cmd is delivery_CMD_TIMEOUT:
            return delivery.refuse(delivery.REASON_CMD_PROBE_TIMEOUT, written=False)
        assert cmd is None or isinstance(cmd, str)
        if not pane_command_is_claude(cmd):
            if quarantined:
                logger.warning(
                    "deliver_to_window: window %s quarantined (pane cmd=%r is "
                    "not Claude) — REFUSING delivery of %d chars",
                    window_id,
                    cmd,
                    len(text),
                )
                return delivery.refuse(
                    delivery.REASON_QUARANTINED,
                    written=False,
                    message=QUARANTINE_SEND_REFUSED_MSG,
                )
            logger.warning(
                "deliver_to_window: window %s pane cmd=%r is not Claude — "
                "REFUSING delivery of %d chars (a bare shell would EXECUTE it)",
                window_id,
                cmd,
                len(text),
            )
            return delivery.refuse(delivery.REASON_NOT_CLAUDE, written=False)
        if quarantined:
            tmux_manager.clear_window_quarantine(
                window_id, reason="claude alive at send re-check"
            )

        # (2b) The stranded-draft brake (r2 F2). An earlier transaction left a
        # payload in the input box with its Enter withheld and TOLD the user it
        # was not delivered; appending to it now would commit BOTH on our Enter.
        # Released only on positive proof the box is empty again.
        brake = await self._stranded_draft_gate(window_id, deadline=deadline)
        if brake is not None:
            return brake

        # (1)+(3) The positive input-box gate, with a bounded retry on an
        # INDETERMINATE frame (a mid-redraw capture); a POSITIVE hazard (a live
        # prompt, a picker option row, the tasks mode, a completion overlay)
        # refuses on the FIRST capture.
        gate = await self._gate_input_box(window_id, deadline=deadline)
        if gate is not None:
            return gate

        # (4) Write with the Enter WITHHELD. The ``!`` bash-mode two-step is
        # reproduced explicitly — ``send_keys``'s own two-step fires ONLY when
        # ``literal and enter`` are BOTH true.
        for i, segment in enumerate(delivery.literal_segments(text)):
            if i:
                await asyncio.sleep(BASH_MODE_SETTLE_S)
            # Set BEFORE the write, never after: a write whose await is CANCELLED
            # can still have landed in the pane, so the brake must consider the
            # payload potentially stranded from the instant the attempt begins.
            write.attempted = True
            ok = await tmux_manager.send_keys(
                window.window_id, segment, enter=False, literal=True
            )
            if not ok:
                # r2 F5: a False from ``send_keys`` does NOT prove zero bytes
                # reached the pane — tmux may have failed AFTER writing, and a
                # later segment's failure certainly leaves the earlier ones
                # there. EVERY post-write-attempt failure is therefore classified
                # WRITTEN (fail-closed): it arms the stranded-draft brake, whose
                # empty-input-row self-heal releases it if nothing landed.
                return delivery.refuse(delivery.REASON_SEND_FAILED, written=True)
        await asyncio.sleep(TEXT_SETTLE_S)

        # (5) Re-verify immediately before the commit. From here on ANY failure
        # is DRAFT_WRITTEN: the text sits in the input box and the Enter is
        # withheld. No automatic cleanup is attempted (Esc / Ctrl-U have
        # surface-specific semantics — Esc on folder-trust KILLS Claude).
        #
        # ORDER IS LOAD-BEARING (r2 F4): the command probe runs FIRST and the
        # pane CAPTURE is the LAST observation before the stamp + Enter. The
        # previous order captured the pane, then awaited an UNBOUNDED
        # ``pane_current_command`` — a probe that stalled while a blocking prompt
        # was drawn let a STALE input-box frame authorize the Enter (which
        # commits option 1). Both probes are bounded, and the deadline is
        # re-checked after every await.
        allow_slash = delivery.is_bare_slash_payload(text)
        if time.monotonic() > deadline:
            return delivery.refuse(delivery.REASON_DEADLINE, written=True)
        cmd2 = await self._pane_command_for_gate(window_id)
        if cmd2 is delivery_CMD_TIMEOUT:
            return delivery.refuse(delivery.REASON_CMD_PROBE_TIMEOUT, written=True)
        assert cmd2 is None or isinstance(cmd2, str)
        if not pane_command_is_claude(cmd2):
            return delivery.refuse(delivery.REASON_NOT_CLAUDE, written=True)
        if time.monotonic() > deadline:
            return delivery.refuse(delivery.REASON_DEADLINE, written=True)
        pane = await self._capture_for_gate(window_id)
        if not (pane is None or isinstance(pane, str)):
            return delivery.refuse(delivery.REASON_CAPTURE_TIMEOUT, written=True)
        reason = self._input_box_reason(
            pane, allow_slash_completion=allow_slash, expected_draft=text
        )
        if reason is not None:
            return delivery.refuse(
                delivery.REASON_REVERIFY_FAILED,
                written=True,
                message=delivery.DRAFT_WRITTEN_MSG,
            )

        # (6) The pre-commit user-turn stamp — the ONE synchronous route_runtime
        # mutation the lock contract permits (documented exception). A raise
        # fails CLOSED: draft_written, no Enter, no stamp.
        if user_turn is not None:
            try:
                _stamp_user_turn(user_turn)
            except Exception:
                logger.exception(
                    "deliver_to_window: pre-commit user-turn stamp raised for "
                    "window %s — withholding Enter",
                    window_id,
                )
                return delivery.refuse(delivery.REASON_STAMP_FAILED, written=True)

        # (7) Enter — the commit. A False here does NOT prove the key never
        # reached the pty (r2 F3), so the result is COMMIT_UNKNOWN, never
        # "draft_written" (which asserts a deliberate withhold). The user-turn
        # stamp above STANDS — a possibly-committed turn must move the turn
        # boundary — and the honest copy tells the user to check the window.
        if not await tmux_manager.send_keys(
            window.window_id, "", enter=True, literal=False
        ):
            return delivery.commit_unknown(delivery.REASON_ENTER_FAILED)
        return delivery.delivered(f"Sent to {display}")

    async def _stranded_draft_gate(
        self, window_id: str, *, deadline: float
    ) -> DeliveryResult | None:
        """The stranded-draft brake. ``None`` ⇒ no draft (or it is provably gone).

        Zero cost for the overwhelming majority of sends (one set lookup). For a
        BRAKED window it takes ONE extra capture and releases the brake only on
        positive proof (``pane_input_row_empty`` True); an INDETERMINATE frame —
        a capture failure, a mid-redraw, or a live blocking prompt (no input box
        at all) — keeps it, which is the fail-closed direction.
        """
        if not window_has_stranded_draft(window_id):
            return None
        if time.monotonic() > deadline:
            return delivery.refuse(delivery.REASON_DEADLINE, written=False)
        pane = await self._capture_for_gate(window_id)
        empty = (
            terminal_parser.pane_input_row_empty(pane)
            if (pane is None or isinstance(pane, str))
            else None
        )
        if empty is True:
            clear_stranded_draft(window_id, reason="input row observed EMPTY")
            return None
        return delivery.refuse(delivery.REASON_STRANDED_DRAFT, written=False)

    async def _pane_command_for_gate(self, window_id: str) -> str | None | object:
        """One BOUNDED ``pane_current_command`` probe for the delivery gate (r2 F4).

        Returns the command string, ``None`` when tmux reported none, or the
        ``delivery_CMD_TIMEOUT`` sentinel when the bounded wait expired. A genuine
        caller/shutdown cancellation PROPAGATES — only ``asyncio.TimeoutError``
        classifies (the /cost r1 P2 rule, shared with ``_capture_for_gate``).
        """
        try:
            return await asyncio.wait_for(
                tmux_manager.pane_current_command(window_id),
                timeout=CMD_PROBE_DEADLINE_S,
            )
        except asyncio.TimeoutError:
            return delivery_CMD_TIMEOUT

    async def _capture_for_gate(self, window_id: str) -> str | None | object:
        """One bounded, cancellation-safe pane capture for the delivery gate.

        Returns the pane text, ``None`` on a tmux capture failure, or the
        ``delivery_CAPTURE_TIMEOUT`` sentinel when the bounded wait expired. A
        genuine caller/shutdown cancellation PROPAGATES (never swallowed into a
        refusal) — only ``asyncio.TimeoutError`` classifies (the /cost r1 P2
        rule).
        """
        try:
            return await asyncio.wait_for(
                tmux_manager.capture_pane_cancellation_safe(window_id, with_ansi=True),
                timeout=GATE_CAPTURE_DEADLINE_S,
            )
        except asyncio.TimeoutError:
            return delivery_CAPTURE_TIMEOUT

    @staticmethod
    def _input_box_reason(
        pane: str | None,
        *,
        allow_slash_completion: bool = False,
        expected_draft: str | None = None,
    ) -> str | None:
        """The gate verdict for one captured frame — ``None`` iff it may receive text.

        The POSITIVE input-box proof is the SOLE AUTHORITY, and it is consulted
        FIRST. The pane recognizers (``is_interactive_ui`` /
        ``parse_unknown_blocking_prompt`` / ``pane_blocking_prompt_shape``) are
        strictly a LABELLING aid: they run ONLY after the proof has already
        FAILED, and only to upgrade an INDETERMINATE reason to the actionable
        ``prompt_present`` copy ("answer the card first") instead of burning the
        retry budget on the generic "couldn't confirm the input box".

        They must NEVER pre-empt the proof (r1 P1, probe-reproduced). A
        recognizer that fires while the input box is demonstrably LIVE is a FALSE
        REFUSAL of a legitimate message, and this gate sits in front of EVERY
        inbound message. The concrete case: an ANSWERED AUQ picker / ExitPlanMode
        prompt whose rendering is still on-screen ABOVE the restored input box
        still matches ``is_interactive_ui`` (the AUQ/EPM ``UIPattern``s carry no
        strict validator, so unlike Permission/Workflow/Decision they have no
        ``_only_chrome_below`` guard) — pre-empting there refused every message
        in the topic until the picker scrolled off. The recognizers also buy NO
        safety here: across all 25 real 2.1.207 pane fixtures the positive proof
        ALONE refuses every blocking surface (all six gate families, the bare
        shell, the /cost overlay, both completion overlays, the tasks mode) and
        passes every deliverable shape — which is exactly the flag-independence
        claim (the recognizers are filtered by the display kill-switches; the
        input-box proof never is). ``pane_blocking_prompt_shape`` already
        documented this discipline; the other two now follow it.

        ``expected_draft`` is passed ONLY at the post-write re-verify (never by
        the pre-write gate, which has no payload in the box yet). It is evidence
        of AUTHORSHIP — see ``terminal_parser.classify_input_box_failure``.
        """
        if not pane:
            return "capture_empty"
        reason = terminal_parser.classify_input_box_failure(
            pane,
            allow_slash_completion=allow_slash_completion,
            expected_draft=expected_draft,
        )
        if reason is None:
            return None
        if reason in terminal_parser.INPUT_BOX_INDETERMINATE_REASONS and (
            terminal_parser.is_interactive_ui(pane)
            or terminal_parser.parse_unknown_blocking_prompt(pane)
            or terminal_parser.pane_blocking_prompt_shape(pane)
        ):
            # LABELLING ONLY (never a decision): the proof has already failed and
            # the frame is INDETERMINATE, so upgrade the generic "couldn't confirm
            # the input box" to the actionable "answer the card first" — and stop
            # burning retries on a frame that will not become writable.
            return delivery.REASON_PROMPT_PRESENT
        return reason

    async def _gate_input_box(
        self, window_id: str, *, deadline: float
    ) -> DeliveryResult | None:
        """Run the pre-write gate. ``None`` ⇒ the pane may receive the payload."""
        attempts = GATE_CAPTURE_RETRIES + 1
        reason = delivery.REASON_CAPTURE_FAILED
        for attempt in range(attempts):
            if time.monotonic() > deadline:
                return delivery.refuse(delivery.REASON_DEADLINE, written=False)
            pane = await self._capture_for_gate(window_id)
            if pane is delivery_CAPTURE_TIMEOUT:
                return delivery.refuse(delivery.REASON_CAPTURE_TIMEOUT, written=False)
            assert pane is None or isinstance(pane, str)
            reason = self._input_box_reason(pane) or ""
            if not reason:
                return None
            if reason not in terminal_parser.INPUT_BOX_INDETERMINATE_REASONS:
                # A POSITIVE hazard — refuse on the FIRST capture, never retry.
                return delivery.refuse(reason, written=False)
            if attempt + 1 < attempts:
                await asyncio.sleep(GATE_RETRY_DELAY_S)
        return delivery.refuse(reason or delivery.REASON_CAPTURE_FAILED, written=False)

    # --- Message history ---

    async def get_recent_messages(
        self,
        window_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> tuple[list[dict], int]:
        """Get user/assistant messages for a window's session.

        Resolves window → session, then reads the JSONL.
        Supports byte range filtering via start_byte/end_byte.
        Returns (messages, total_count).
        """
        session = await self.resolve_session_for_window(window_id)
        if not session or not session.file_path:
            return [], 0

        file_path = Path(session.file_path)
        if not file_path.exists():
            return [], 0

        # Read JSONL entries (optionally filtered by byte range)
        entries: list[dict] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                if start_byte > 0:
                    await f.seek(start_byte)

                while True:
                    # Check byte limit before reading
                    if end_byte is not None:
                        current_pos = await f.tell()
                        if current_pos >= end_byte:
                            break

                    line = await f.readline()
                    if not line:
                        break

                    data = TranscriptParser.parse_line(line)
                    if data:
                        entries.append(data)
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
            return [], 0

        parsed_entries, _ = TranscriptParser.parse_entries(entries)
        # Skip lifecycle-only entries: they exist solely to drive run-state
        # transitions and have no visible content, so they must not fan out
        # into /history. The CC 2.1.198 queue-shaped ``<task-notification>``
        # close synthesizes a lifecycle-only user-text entry with REAL text
        # (the envelope) — without this filter it would leak into /history;
        # the pre-existing empty-text lifecycle markers were invisible only by
        # luck (empty string).
        all_messages = [
            {
                "role": e.role,
                "text": e.text,
                "content_type": e.content_type,
                "timestamp": e.timestamp,
            }
            for e in parsed_entries
            if not e.lifecycle_only
        ]

        return all_messages, len(all_messages)


session_manager = SessionManager()


def session_id_for_window(window_id: str | None) -> str | None:
    """Best-effort sync session_id lookup for ``message_refs`` provenance.

    Single source of truth for the ``WindowState.session_id or None`` dance
    used by attention/interactive-UI/message-queue sends. Returns ``None``
    when ``window_id`` is empty or the window has no recorded session yet
    (the ref row is still inserted with a NULL session_id and the resolver
    falls back to visible Telegram text).
    """
    if not window_id:
        return None
    state = session_manager.get_window_state(window_id)
    return state.session_id or None


def peek_session_id_for_window(window_id: str | None) -> str | None:
    """Read-only sibling of ``session_id_for_window``.

    ``session_id_for_window`` calls ``session_manager.get_window_state``
    which auto-creates a ``WindowState`` on miss. Callers that should
    NOT mutate SessionManager state on unknown windows — notably the
    AUQ PreToolUse reader — use this peek variant instead. Returns
    ``None`` when ``window_id`` is empty or the window isn't currently
    in ``window_states``.
    """
    if not window_id:
        return None
    state = session_manager.window_states.get(window_id)
    if state is None:
        return None
    return state.session_id or None
