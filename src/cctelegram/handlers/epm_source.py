"""Trust boundary for the ExitPlanMode PreToolUse side file (GH #50 PR-2 r3).

Nothing outside this module reads ``epm_pending/<session_id>.json`` or constructs
that path. It exists for exactly ONE consumer — the free-text executor's
OCCURRENCE anchor (``handlers/free_text.SurfaceIdentity``) — and it exists because
nothing else in the system can answer "WHICH ExitPlanMode prompt is this?".

WHY (peer-review round-3 P1 — the anchor that wasn't an anchor). The EPM anchor
used to be a hash of the plan FILE'S CONTENT, read from the live pane footer. A
content hash verifies the ARTIFACT; it cannot establish the OCCURRENCE, and the
gap is a TOCTOU with a bypass-permissions payoff:

    capture card A's pane  →  another controller resolves A  →  Claude rewrites
    the SAME plan path with plan B  →  B renders  →  the anchor read now returns
    B's hash.

The captured identity is then ``(A's pane shape, B's hash)``; both post-nav and
final pre-Enter checks observe ``(B's pane shape, B's hash)`` — and every EPM
prompt renders the SAME three real options, so the pane component matches too.
Both components agree, and Enter commits card A's feedback onto card B, whose
option 1 is "Yes, and bypass permissions". Reading the file three times detects
LATER changes but can never bind the FIRST read atomically to the pane that was
captured.

RIG-VERIFIED on Claude Code 2.1.207 (a `tmux -L ccrig` scratch session driven
through three consecutive ExitPlanMode prompts):

  * ``PreToolUse`` DOES fire for the ``ExitPlanMode`` tool, with
    ``tool_input = {plan, planFilePath}``;
  * ``tool_use_id`` is present and DISTINCT per invocation (``toolu_01FfhZ…``,
    ``toolu_01GwV2…``, ``toolu_014ef4…``) — a true occurrence witness;
  * ``planFilePath`` was IDENTICAL across all three, and the file was REWRITTEN
    in place each time. The slug is a per-session NAME, reused even across three
    substantively different plans. It is definitively not an occurrence witness,
    which is precisely the P1;
  * ``TMUX_PANE`` IS exported to the hook, so the record can carry the
    ``window_key`` and the read can HARD-predicate on it.

So this lane's occurrence id is the hook's ``tool_use_id``, captured BEFORE the
prompt renders — the same shape (and the same discipline) as the AUQ PreToolUse
side file, which is the only reason the AUQ leg was sound to begin with.

STRICTER THAN THE AUQ LANE, deliberately. The AUQ side file carries no
``window_key``, so it documents an off-contract residual: under a double-
``--resume`` of one session into two windows, a sibling's record is
indistinguishable. This lane closes that — the hook resolves the pane's
``tmux_session:window_id`` (verified available) and the read REQUIRES it to equal
the asking window's key, exactly as ``notify_source`` does. EPM is the surface
where a wrong commit bypasses permissions; it gets the strongest predicate we
have, and the strictness costs nothing elsewhere because this file has exactly
one reader.

FAIL-CLOSED EVERYWHERE. No record, an unresolvable window key, a schema mismatch,
a future-skewed clock ⇒ ``None`` ⇒ ``free_text.derive_identity`` returns ``None``
⇒ the lane DECLINES *before the first keystroke* and falls through to PR-1's
refusal. Nothing is typed, so no draft is stranded. An EPM prompt we cannot name
is an EPM prompt we will not type into.

Deliberately NO read-TTL: a plan-approval card left open for hours is still that
card, and its occurrence id is still the truth. Liveness is not this module's
job — identity is.

Key components:
  - ``EpmRecord`` / ``peek_surface_identity_for_window`` — the validated,
    window-predicated read that yields the occurrence anchor.
  - ``forget_for_window`` / ``unlink_for_session`` — the lifecycle unlinks
    (EPM resolution, session replacement, ``/clear``, topic close).
  - ``gc_stale`` — the 24h startup backstop with the injected ``is_live_session``
    conservative-skip (mirrors ``auq_source`` / ``notify_source``).

A leaf: imports only ``session.peek_session_id_for_window`` and ``utils``
(``tmux_manager`` is deferred inside the window-key builder, mirroring
``notify_source``). No in-memory state — every read hits the file, so there is no
reset seam to leak.
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

_EPM_SCHEMA_VERSION = 1
# Reject records timestamped implausibly in the future (clock tamper / skew) —
# the same guard the AUQ + Notification side files apply.
_EPM_FUTURE_SKEW_SECONDS = 30
# Startup GC cutoff — the crash-orphan backstop, never the staleness authority
# (this lane HAS no staleness authority: identity does not expire).
_EPM_GC_AGE_SECONDS = 24 * 3600

# Path-traversal defense in depth (same as auq_source / notify_source): the
# side-file path is only ever constructed from a canonical-UUID session_id.
_SESSION_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

_EPM_PENDING_DIRNAME = "epm_pending"


@dataclass(frozen=True)
class EpmRecord:
    """A validated ExitPlanMode PreToolUse side-file record.

    Carries NO plan BODY by design — the occurrence anchor needs a NAME for the
    prompt, not its contents, and the plan text is already delivered to the user
    by ``interactive_ui._maybe_post_epm_plan``. ``plan_fingerprint`` (a digest of
    the hook's authoritative ``tool_input.plan``) and ``plan_file_path`` are kept
    for observability and as the composite fallback below — never as the identity
    when a ``tool_use_id`` exists.
    """

    session_id: str
    tool_use_id: str
    window_key: str
    written_at: float
    plan_file_path: str
    plan_fingerprint: str


def _epm_side_file_path(session_id: str) -> Path | None:
    """Resolve the side-file path after UUID validation; ``None`` on a
    non-canonical session_id (refuse to build an escapable path)."""
    if not _SESSION_ID_RE.fullmatch(session_id):
        return None
    return app_dir() / _EPM_PENDING_DIRNAME / f"{session_id}.json"


def _read_record(session_id: str) -> EpmRecord | None:
    """Read + schema-validate the side file for ``session_id``.

    ``None`` on missing file, non-UUID session_id, JSON/shape errors, a
    schema_version mismatch, or an empty ``window_key``. No window predicate and
    no skew check here — :func:`peek_surface_identity_for_window` layers those.
    """
    path = _epm_side_file_path(session_id)
    if path is None:
        logger.warning(
            "EPM side file: refusing to resolve non-UUID session_id=%r", session_id
        )
        return None
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug("EPM side file unreadable for %s: %s", session_id, e)
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("EPM side file malformed JSON for %s: %s", session_id, e)
        return None
    if not isinstance(rec, dict):
        return None
    if rec.get("schema_version") != _EPM_SCHEMA_VERSION:
        logger.warning(
            "EPM side file schema_version=%r unknown for %s",
            rec.get("schema_version"),
            session_id,
        )
        return None
    window_key = rec.get("window_key")
    # The window key is MANDATORY in this lane (unlike AUQ's): it is the
    # double-``--resume`` sibling predicate, and a record without one cannot be
    # predicated, so it cannot be trusted.
    if not isinstance(window_key, str) or not window_key:
        return None
    try:
        written_at = float(rec.get("written_at", 0))
    except (TypeError, ValueError):
        return None
    tool_use_id = rec.get("tool_use_id", "")
    if not isinstance(tool_use_id, str):
        return None
    plan_file_path = rec.get("plan_file_path", "")
    plan_fingerprint = rec.get("plan_fingerprint", "")
    return EpmRecord(
        session_id=session_id,
        tool_use_id=tool_use_id,
        window_key=window_key,
        written_at=written_at,
        plan_file_path=str(plan_file_path or ""),
        plan_fingerprint=str(plan_fingerprint or ""),
    )


def _expected_window_key(window_id: str) -> str:
    """The bot-side counterpart of the hook's ``session:window_id`` key.

    Deferred import — ``tmux_manager`` pulls in config; keeping it out of module
    import time preserves this module's leaf-ness (mirrors ``notify_source``).
    """
    from ..tmux_manager import tmux_manager

    return f"{tmux_manager.session_name}:{window_id}"


def peek_surface_identity_for_window(window_id: str) -> str | None:
    """The ExitPlanMode SURFACE-OCCURRENCE identity for a window, or ``None``.

    The out-of-band anchor the GH #50 PR-2 free-text executor captures before its
    first keystroke and RE-CHECKS after the navigation and again in the final
    pre-Enter capture. It names the PROMPT OCCURRENCE, not the plan artifact, so
    a plan revised in place under the same slug — the round-3 P1 — yields a
    DIFFERENT anchor and the transaction refuses.

    Predicates, all fail-closed:
      1. the window must resolve to a session;
      2. the record must exist and validate;
      3. the record's hook-captured ``window_key`` must equal THIS window's key
         (the double-``--resume`` sibling guard — session-keyed matching alone is
         forbidden on a bypass-permissions surface);
      4. the write must not be future-skewed.

    Deliberately NO read-TTL: a card left open for hours is still that card.

    The occurrence key prefers the hook's ``tool_use_id`` (rig-verified present
    and distinct per invocation on 2.1.207). The ``(written_at, plan
    fingerprint)`` composite is the defensive fallback for a future CC that stops
    emitting one — ``written_at`` is stamped per hook FIRE, so it remains
    occurrence-unique. Mirrors the AUQ composite rather than reinventing it.
    """
    session_id = peek_session_id_for_window(window_id)
    if not session_id:
        return None
    rec = _read_record(session_id)
    if rec is None:
        return None
    if rec.window_key != _expected_window_key(window_id):
        logger.debug(
            "EPM anchor read window=%s reason=window_key_mismatch (%s)",
            window_id,
            rec.window_key,
        )
        return None
    if time.time() - rec.written_at < -_EPM_FUTURE_SKEW_SECONDS:
        logger.debug("EPM anchor read window=%s reason=future_skew", window_id)
        return None
    if rec.tool_use_id:
        return f"epm:tu:{rec.tool_use_id}"
    return f"epm:wf:{rec.written_at!r}:{rec.plan_fingerprint}"


def forget_for_window(window_id: str) -> None:
    """Unlink the side file for a window's CURRENT session (EPM resolution / the
    generic interactive-surface teardown).

    The ``/clear`` race — where ``session_monitor._detect_and_cleanup_changes``
    swaps the session_id out from under us before this runs — is handled by the
    monitor calling :func:`unlink_for_session` with the OLD id, exactly as the
    AUQ lane does.
    """
    session_id = peek_session_id_for_window(window_id)
    if not session_id:
        return
    unlink_for_session(session_id)


def unlink_for_session(session_id: str) -> None:
    """Best-effort UNCONDITIONAL unlink — the teardown seams (EPM resolution,
    session replacement, ``/clear``, topic close)."""
    path = _epm_side_file_path(session_id)
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.debug("EPM unlink for session=%s failed: %s", session_id, e)


def gc_stale(*, is_live_session: Callable[[str], bool] | None = None) -> int:
    """Delete EPM side files older than 24h. The bot-startup crash backstop.

    Mirrors ``auq_source.gc_stale`` / ``notify_source.gc_stale``: when
    ``is_live_session`` is supplied it is called with the file STEM (=
    ``<session_id>``) after the age test — True → SKIP (conservative: Claude
    buffers the ExitPlanMode ``tool_use`` in JSONL until the prompt resolves, so
    a plan card left open >24h has a stale-mtime side file that is STILL the only
    witness of which card it is), and an EXCEPTION → conservative SKIP (never
    delete on uncertainty). A re-stat before unlink guards the TOCTOU window
    against a concurrent hook re-fire. Returns the number of files deleted.
    """
    pending_dir = app_dir() / _EPM_PENDING_DIRNAME
    if not pending_dir.is_dir():
        return 0
    cutoff = time.time() - _EPM_GC_AGE_SECONDS
    deleted = 0
    try:
        entries = list(pending_dir.iterdir())
    except OSError as e:
        logger.warning("EPM GC: iterdir on %s failed: %s", pending_dir, e)
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
        # TOCTOU guard: the hook may have replaced the file (atomic temp+rename)
        # since the initial stat — re-check before unlink.
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
            logger.debug("EPM GC: unlink %s failed: %s", entry, e)
    if deleted:
        logger.info("EPM GC: deleted %d stale ExitPlanMode side file(s)", deleted)
    return deleted


__all__ = [
    "EpmRecord",
    "forget_for_window",
    "gc_stale",
    "peek_surface_identity_for_window",
    "unlink_for_session",
]
