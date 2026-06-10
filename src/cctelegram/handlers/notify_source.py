"""Trust boundary for the Notification-hook side file (Wave B busy-signal).

Nothing outside this module reads ``notify_pending/<session_id>.json`` or
constructs that path. Mirrors the ``auq_source`` side-file discipline:
UUID-validated path construction, schema validation, future-skew rejection,
and a bot-startup GC with an injected liveness predicate — plus the two
Wave-B-specific guarantees:

  - ``notification_pending_for_window`` applies the HARD window-key read
    predicate: a record is returned ONLY when its hook-captured
    ``window_key`` equals ``f"{tmux_session}:{window_id}"`` for the asking
    window (hermes P1-2 — session-keyed lookup alone is forbidden; under a
    double-``--resume`` of one session into two windows, only the window
    whose pane fired the notification lights 🔔).
  - ``unlink_if_generation_matches`` is the generation-guarded unlink: it
    re-reads before unlinking and removes the file ONLY if the generation
    still matches what the caller consumed, so a hook re-fire between read
    and unlink survives (hermes P2-4).

Deliberately NO read-TTL here: staleness is runtime-state-driven —
``status_polling`` enforces ``route_runtime.NOTIFY_TTL_SECONDS`` from the
snapshot on every tick (v4 fix 2 strand-proofing) and treats an on-disk
record older than the TTL as absent.

Key components:
  - ``NotificationRecord`` / ``notification_pending_for_window`` — the
    validated, window-predicated read.
  - ``unlink_if_generation_matches`` / ``unlink_for_session`` — guarded +
    unconditional lifecycle unlinks.
  - ``gc_stale`` — 24h startup GC with injected ``is_live_session``
    conservative-skip (mirrors ``auq_source.gc_stale``).

A leaf: imports only ``session.peek_session_id_for_window`` and ``utils``
(``tmux_manager`` is deferred inside the window-key builder). No in-memory
state — every read hits the file, so there is no reset seam to leak.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..session import peek_session_id_for_window
from ..utils import app_dir

logger = logging.getLogger(__name__)

_NOTIFY_SCHEMA_VERSION = 1
# Reject records timestamped implausibly in the future (clock tamper / skew)
# — same guard the AUQ side file applies.
_NOTIFY_FUTURE_SKEW_SECONDS = 30
# Startup GC cutoff. Far beyond the 30-min runtime TTL by design: GC is the
# crash-orphan backstop, not the staleness authority.
_NOTIFY_GC_AGE_SECONDS = 24 * 3600

# Path-traversal defense in depth (same as auq_source): the side-file path is
# only ever constructed from a canonical-UUID session_id.
_SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


@dataclass(frozen=True)
class NotificationRecord:
    """A validated Notification-hook side-file record.

    Carries NO notification message text by design (codex P3-6) — only the
    fields the runtime bit needs: hook-fire wall clock (``ts``), the
    hook-resolved ``window_key``, the re-fire ``generation`` nonce, and a
    coarse ``kind`` classifier.
    """

    session_id: str
    ts: float
    window_key: str
    generation: str
    kind: str


def _notify_side_file_path(session_id: str) -> Path | None:
    """Resolve the side-file path after UUID validation; ``None`` on a
    non-canonical session_id (refuse to build an escapable path)."""
    if not _SESSION_ID_RE.fullmatch(session_id):
        return None
    return app_dir() / "notify_pending" / f"{session_id}.json"


def _read_record(session_id: str) -> NotificationRecord | None:
    """Read + schema-validate the side file for ``session_id``.

    ``None`` on missing file, non-UUID session_id, JSON/shape errors,
    schema_version mismatch, or an empty ``window_key``/``generation``.
    No TTL and no window predicate here — callers layer those.
    """
    path = _notify_side_file_path(session_id)
    if path is None:
        logger.warning(
            "Notify side file: refusing to resolve non-UUID session_id=%r",
            session_id,
        )
        return None
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug("Notify side file unreadable for %s: %s", session_id, e)
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("Notify side file malformed JSON for %s: %s", session_id, e)
        return None
    if not isinstance(rec, dict):
        return None
    if rec.get("schema_version") != _NOTIFY_SCHEMA_VERSION:
        logger.warning(
            "Notify side file schema_version=%r unknown for %s",
            rec.get("schema_version"),
            session_id,
        )
        return None
    window_key = rec.get("window_key")
    generation = rec.get("generation")
    if not isinstance(window_key, str) or not window_key:
        return None
    if not isinstance(generation, str) or not generation:
        return None
    try:
        ts = float(rec.get("ts", 0))
    except (TypeError, ValueError):
        return None
    return NotificationRecord(
        session_id=session_id,
        ts=ts,
        window_key=window_key,
        generation=generation,
        kind=str(rec.get("kind", "") or ""),
    )


def _expected_window_key(window_id: str) -> str:
    """The bot-side counterpart of the hook's ``session:window_id`` key.

    Deferred import — ``tmux_manager`` pulls in config; keeping it out of
    module import time preserves this module's leaf-ness for the import
    graph the same way ``auq_source`` defers ``terminal_parser``.
    """
    from ..tmux_manager import tmux_manager

    return f"{tmux_manager.session_name}:{window_id}"


def notification_pending_for_window(window_id: str) -> NotificationRecord | None:
    """Return the validated notification record for ``window_id``, or None.

    The HARD read-time predicate: the record's hook-captured ``window_key``
    must equal this window's ``f"{tmux_session}:{window_id}"`` — a
    session-keyed match alone is forbidden (double-``--resume`` sibling
    safety). Also rejects future-skewed timestamps. NO read-TTL (staleness
    is enforced from runtime state by the poller). Read-only.
    """
    session_id = peek_session_id_for_window(window_id)
    if not session_id:
        return None
    rec = _read_record(session_id)
    if rec is None:
        return None
    if rec.window_key != _expected_window_key(window_id):
        logger.debug(
            "Notify read window=%s reason=window_key_mismatch (%s)",
            window_id,
            rec.window_key,
        )
        return None
    if time.time() - rec.ts < -_NOTIFY_FUTURE_SKEW_SECONDS:
        logger.debug("Notify read window=%s reason=future_skew", window_id)
        return None
    return rec


def unlink_if_generation_matches(session_id: str, generation: str) -> bool:
    """Generation-guarded unlink: re-read, unlink ONLY on a generation match.

    Returns True iff the file was unlinked. A hook re-fire between the
    caller's read and this unlink writes a NEW generation — the mismatch
    leaves the fresh record in place for the next poll tick.
    """
    rec = _read_record(session_id)
    if rec is None or rec.generation != generation:
        return False
    path = _notify_side_file_path(session_id)
    if path is None:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.debug("Notify unlink for session=%s failed: %s", session_id, e)
        return False
    return True


def unlink_for_session(session_id: str) -> None:
    """Best-effort UNCONDITIONAL unlink — the teardown seams (session
    replacement / ``/clear`` / topic close), where no generation survives
    to guard against."""
    path = _notify_side_file_path(session_id)
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.debug("Notify unlink for session=%s failed: %s", session_id, e)


def gc_stale(*, is_live_session: Callable[[str], bool] | None = None) -> int:
    """Delete notification side files older than 24h. Bot-startup backstop.

    Mirrors ``auq_source.gc_stale``: when ``is_live_session`` is supplied it
    is called with the file STEM (= the ``<session_id>``) after the age test
    — True → SKIP (conservative: a tracked session's file is left for the
    runtime TTL / teardown seams to reap); an EXCEPTION → conservative SKIP
    (never delete on uncertainty; caught around the predicate call only so
    the pass continues). A re-stat before unlink guards the TOCTOU window
    against a concurrent hook re-fire. Returns the number of files deleted.
    """
    pending_dir = app_dir() / "notify_pending"
    if not pending_dir.is_dir():
        return 0
    cutoff = time.time() - _NOTIFY_GC_AGE_SECONDS
    deleted = 0
    try:
        entries = list(pending_dir.iterdir())
    except OSError as e:
        logger.warning("Notify GC: iterdir on %s failed: %s", pending_dir, e)
        return 0
    for entry in entries:
        if not entry.is_file():
            continue
        if not entry.name.endswith(".json"):
            continue
        stem = entry.stem
        if not _SESSION_ID_RE.fullmatch(stem):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        if is_live_session is not None:
            try:
                if is_live_session(stem):
                    continue
            except Exception:
                continue
        # TOCTOU guard: the hook may have replaced the file (atomic
        # temp+rename) since the initial stat — re-check before unlink.
        try:
            current_mtime = entry.stat().st_mtime
        except OSError:
            continue
        if current_mtime >= cutoff:
            continue
        try:
            entry.unlink()
            deleted += 1
        except OSError as e:
            logger.debug("Notify GC: unlink %s failed: %s", entry, e)
    if deleted:
        logger.info("Notify GC: deleted %d stale notification file(s)", deleted)
    return deleted


__all__ = [
    "NotificationRecord",
    "gc_stale",
    "notification_pending_for_window",
    "unlink_for_session",
    "unlink_if_generation_matches",
]
