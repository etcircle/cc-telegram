"""Bot-side reader + lifecycle for the MessageDisplay live-prose capture (Bug 2).

The ``MessageDisplay`` hook appender (``_md_display_appender.py``) writes each
streaming ``delta`` of an assistant message to a per-session NDJSON under
``<CC_TELEGRAM_DIR>/msg_display/<session>.ndjson`` while the message is still on
screen — BEFORE Claude Code co-flushes the whole turn (prose + the trailing
AskUserQuestion / ExitPlanMode ``tool_use``) to the session JSONL at resolution.
That buffering is Bug 2: the bot derives content from the JSONL via byte-offset
reads, so during a live prompt the explanatory prose isn't on the bridge and the
Telegram user chooses blind.

This module is the bot side of that mechanism. It:
  * resolves the appender path + writes the bot-managed ``--settings`` file that
    scopes the hook to bot-launched sessions (``ensure_capture_settings``);
  * reads the per-session NDJSON ON DEMAND at picker-render time and
    reconstructs the completed prose of each finalized message as a
    ``ProseRecord`` (``read_prose_records``) — accumulating the per-flush
    ``delta`` values because each hook invocation is a fresh process that cannot
    accumulate in memory, and ``MessageDisplay.message_id`` has no JSONL
    counterpart (so the bot, not the hook, owns grouping);
  * exposes ``normalize_prose`` — the SINGLE normalization contract shared by
    the live capture's ``norm_hash`` here and the post-resolution JSONL dedup
    (PR-D). Using one function on both sides is the mint/validate parity that
    keeps the live-shown text and the JSONL copy comparing equal;
  * tears down a session's capture file on resolution / teardown
    (``teardown_session``) and sweeps stale files at startup (``gc_stale``).

Pull-only by construction: there is no background tailer or observer channel
(the c313657 fan-out pattern is forbidden). The render path reads when it needs
the data; the bounded retry that waits for a not-yet-final message lives in the
caller (status_polling, PR-C).

The surface that POSTS a ``ProseRecord`` before the picker card, the freshness
gate, the shown-live marker, and the JSONL dedup all land in PR-C+D; this module
ships the capture + read + normalization + lifecycle primitives they build on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .utils import app_dir, atomic_write_json

logger = logging.getLogger(__name__)

# Per-session NDJSON capture lives under this subdirectory of the config dir.
MD_DISPLAY_DIRNAME = "msg_display"
# The bot-managed settings file passed to ``claude --settings`` so the
# MessageDisplay hook fires only for bot-launched sessions (merges with the
# global SessionStart / PreToolUse hooks; verified for new launches).
_SETTINGS_FILENAME = "md_hook_settings.json"
# Hook timeout (seconds). The appender is sub-millisecond; this is a generous
# ceiling matching the AUQ PreToolUse precedent. Hook failures are non-blocking.
_MD_HOOK_TIMEOUT_S = 5


# ── Paths + capture-settings management ──────────────────────────────────────


def appender_path() -> Path:
    """Absolute path to the stdlib MessageDisplay appender script (shipped in
    the package, run directly so the package is never imported)."""
    return Path(__file__).resolve().parent / "_md_display_appender.py"


def capture_settings_path() -> Path:
    """Path to the bot-managed ``--settings`` file registering the hook."""
    return app_dir() / _SETTINGS_FILENAME


def msg_display_dir() -> Path:
    return app_dir() / MD_DISPLAY_DIRNAME


def session_ndjson_path(session_id: str) -> Path:
    return msg_display_dir() / f"{session_id}.ndjson"


def _hook_command() -> str:
    """The shell command Claude runs for the hook: the bot's own interpreter
    (absolute, guaranteed to exist with a stdlib) running the appender. Both
    paths are shell-quoted; the hook executes in the tmux pane, not the bot."""
    python = sys.executable or "python3"
    return f"{shlex.quote(python)} {shlex.quote(str(appender_path()))}"


def _desired_settings() -> dict:
    return {
        "hooks": {
            "MessageDisplay": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _hook_command(),
                            "timeout": _MD_HOOK_TIMEOUT_S,
                        }
                    ]
                }
            ]
        }
    }


def ensure_capture_dir() -> Path:
    """Create the capture dir at mode 0700 (prose can carry sensitive context)
    and return it. Idempotent; tightens a pre-existing loose-mode dir."""
    d = msg_display_dir()
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError as e:
        logger.warning("md_capture: could not ensure %s at 0700: %s", d, e)
    return d


def ensure_capture_settings() -> Path:
    """Write (idempotently) the bot-managed MessageDisplay ``--settings`` file
    and return its path. Rewrites only when the desired content differs (the
    interpreter / appender path can move across installs)."""
    ensure_capture_dir()
    path = capture_settings_path()
    desired = _desired_settings()
    try:
        if path.exists() and json.loads(path.read_text()) == desired:
            return path
    except (OSError, json.JSONDecodeError):
        pass
    try:
        atomic_write_json(path, desired)
    except OSError as e:
        logger.warning("md_capture: could not write %s: %s", path, e)
    return path


def capture_settings_has_message_display() -> bool:
    """Whether the bot-managed settings file currently registers a
    MessageDisplay hook (used by the startup self-check warning)."""
    try:
        data = json.loads(capture_settings_path().read_text())
    except (OSError, json.JSONDecodeError):
        return False
    entries = data.get("hooks", {}).get("MessageDisplay")
    return isinstance(entries, list) and len(entries) > 0


# ── Normalization (the shared dedup contract) ────────────────────────────────


def normalize_prose(text: str) -> str:
    """Canonicalize prose for cross-source equality.

    The ONLY transforms (per the locked dedup contract): CR/CRLF → LF, strip
    trailing whitespace per line, strip leading/trailing blank lines. No
    interior whitespace collapse — that would conflate genuinely different
    prose. This same function normalizes BOTH the live captured text (for the
    ``norm_hash`` stored here) and the post-resolution JSONL aggregate (PR-D's
    dedup), so the two compare equal regardless of streaming vs flush quirks.

    PR-C/D PARITY CONTRACT (codex + panel PR-B P2 — lock before coding the
    comparator): for a SINGLE assistant text block this is provably equal on
    both sides, which is Bug 2's observed shape (every message in the live
    capture was single-block). But the JSONL side strips EACH text block
    independently (``transcript_parser.py`` ``entry.text.strip()``) and the
    plan joins real-text blocks with a single ``\n``, whereas this normalizes
    one whole display string and PRESERVES interior blank lines / indentation.
    So a multi-text-block message whose live display carries a blank line
    BETWEEN blocks would hash-diverge from the per-block-stripped-then-joined
    JSONL aggregate → a dedup miss → the prose double-posts (benign, not a
    crash). PR-C MUST reduce both sides to ONE pre-hash shape — e.g. aggregate
    the JSONL side from RAW (pre-strip) block text joined with ``\n`` and
    normalize the whole, matching the live string — and add the adversarial
    multi-block fixture. This is unobserved today and has no consumer in PR-B.
    """
    lf = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in lf.split("\n")]
    return "\n".join(lines).strip()


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── On-demand read + accumulation ────────────────────────────────────────────


@dataclass(frozen=True)
class ProseRecord:
    """A completed (``final=True``) assistant prose message, reconstructed from
    the per-flush MessageDisplay deltas of one ``message_id``."""

    session_id: str
    transcript_path: str
    md_message_id: str
    text: str
    raw_hash: str
    norm_hash: str
    first_seen_at: float
    final_at: float


@dataclass
class _Accumulator:
    transcript_path: str = ""
    deltas: dict[int, str] | None = None  # index -> delta (last write wins)
    finalized: bool = False
    first_seen_at: float = 0.0
    final_at: float = 0.0

    def add(self, *, index: int, delta: str, final: bool, captured_at: float) -> None:
        if self.deltas is None:
            self.deltas = {}
            self.first_seen_at = captured_at
        self.deltas[index] = delta
        self.first_seen_at = min(self.first_seen_at, captured_at)
        if final:
            self.finalized = True
            self.final_at = max(self.final_at, captured_at)

    def text(self) -> str:
        if not self.deltas:
            return ""
        # Deltas are "newly completed lines" already carrying their own
        # newlines; concatenate in index order with no separator.
        return "".join(self.deltas[i] for i in sorted(self.deltas))


def read_prose_records(
    session_id: str, *, base_dir: Path | None = None
) -> list[ProseRecord]:
    """Read the session's MessageDisplay NDJSON and return one ``ProseRecord``
    per FINALIZED message, ordered by ``final_at`` ascending (freshest last).

    Tolerant by construction: a missing file yields ``[]``; corrupt or partial
    (un-terminated final) lines are skipped; a not-yet-final message is omitted
    (the caller's bounded retry re-reads until its final delta lands). Returns
    only finalized messages — the "recent-final" set the render path selects
    from.

    COST (panel PR-B P3): this re-reads + re-parses the WHOLE per-session file
    on every call. The file holds every delta since the last ``teardown_session``
    and MessageDisplay fires for every assistant message, so a long heavy-
    streaming stretch between prompts could grow it. PR-C's bounded retry calls
    this repeatedly on the picker-render hot path — if that proves costly, read
    incrementally from a persisted byte offset (per-resolution teardown keeps it
    small in the common case).
    """
    path = (
        (base_dir / MD_DISPLAY_DIRNAME / f"{session_id}.ndjson")
        if base_dir is not None
        else session_ndjson_path(session_id)
    )
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []

    accs: dict[str, _Accumulator] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # Corrupt or partially-written (e.g. last line during a concurrent
            # append) — skip; a real final lands on a subsequent read.
            continue
        if not isinstance(rec, dict):
            continue
        payload = rec.get("payload")
        captured_at = rec.get("captured_at")
        if not isinstance(payload, dict) or not isinstance(captured_at, (int, float)):
            continue
        mid = payload.get("message_id")
        delta = payload.get("delta")
        index = payload.get("index")
        if not isinstance(mid, str) or not mid:
            continue
        if not isinstance(delta, str):
            delta = ""
        if not isinstance(index, int):
            # Without an ordering index we cannot place the delta; treat each
            # such flush as its own slot so nothing is silently dropped.
            index = len(accs.get(mid, _Accumulator()).deltas or {})
        final = bool(payload.get("final"))
        tp = payload.get("transcript_path")
        acc = accs.get(mid)
        if acc is None:
            acc = _Accumulator()
            accs[mid] = acc
        if isinstance(tp, str) and tp:
            acc.transcript_path = tp
        acc.add(index=index, delta=delta, final=final, captured_at=float(captured_at))

    records: list[ProseRecord] = []
    for mid, acc in accs.items():
        if not acc.finalized:
            continue
        text = acc.text()
        records.append(
            ProseRecord(
                session_id=session_id,
                transcript_path=acc.transcript_path,
                md_message_id=mid,
                text=text,
                raw_hash=_sha256(text),
                norm_hash=_sha256(normalize_prose(text)),
                first_seen_at=acc.first_seen_at,
                final_at=acc.final_at,
            )
        )
    records.sort(key=lambda r: r.final_at)
    return records


# ── Lifecycle / teardown ─────────────────────────────────────────────────────


def teardown_session(session_id: str, *, base_dir: Path | None = None) -> None:
    """Remove a session's capture file (on AUQ/EPM resolution, session
    replacement, ``/clear``, topic close). Best-effort; missing is fine."""
    path = (
        (base_dir / MD_DISPLAY_DIRNAME / f"{session_id}.ndjson")
        if base_dir is not None
        else session_ndjson_path(session_id)
    )
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("md_capture: could not unlink %s: %s", path, e)


def gc_stale(max_age_seconds: float = 3600.0, *, base_dir: Path | None = None) -> int:
    """Sweep capture files older than ``max_age_seconds`` (startup GC, mirroring
    the AUQ side-file 1h GC). Returns the count removed. A live but unanswered
    prompt's file is younger than the TTL; a crashed/abandoned one ages out."""
    d = (base_dir / MD_DISPLAY_DIRNAME) if base_dir is not None else msg_display_dir()
    removed = 0
    now = time.time()
    try:
        entries = list(d.iterdir())
    except (FileNotFoundError, OSError):
        return 0
    for f in entries:
        if not f.name.endswith(".ndjson"):
            continue
        try:
            if now - f.stat().st_mtime > max_age_seconds:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
