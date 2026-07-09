"""Shared utility functions used across CC Telegram modules.

Provides:
  - app_dir(): resolve config directory from CC_TELEGRAM_DIR env var.
  - atomic_write_json(): crash-safe JSON file writes via temp+rename.
  - read_cwd_from_jsonl(): extract the cwd field from the first JSONL entry.
  - parse_iso_timestamp(): JSONL ISO8601 timestamp → epoch seconds (None on
    failure) — the SINGLE parse shared by transcript_event_adapter and
    session_monitor so both sides of a timestamp comparison use one clock
    semantics (GH #44).
  - normalize_background_agent_key(): the GH #44 key-normalization contract.
  - is_task_notification() / extract_task_notification_task_id(): the SINGLE
    owner of the ``<task-notification>`` envelope regexes (Codex r2 P1 — moved
    here from handlers.response_builder, the true leaf, so transcript_parser can
    synthesize a queue-shaped close with the SAME predicate object the adapter
    stamps with, without a response_builder→TranscriptParser import cycle;
    response_builder re-exports both as alias-only for its existing callers).
"""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

CC_TELEGRAM_DIR_ENV = "CC_TELEGRAM_DIR"

# The single owner of the external ``<task-notification>`` envelope regexes
# (Codex r2 P1). ``handlers.response_builder`` re-exports the two public
# helpers below as alias-only so bot.py / the adapter / session_monitor keep
# their existing import path; ``transcript_parser`` imports them straight from
# here (response_builder imports TranscriptParser, so the reverse import would
# cycle). Byte-0 anchored + full-envelope so the synthesis predicate is exactly
# the adapter's stamp predicate — a malformed / truncated / suffixed envelope
# is rejected identically at both ends (mint/validate parity).
_TASK_NOTIF_RE = re.compile(
    r"\A<task-notification>(.*?)</task-notification>\s*\Z", re.DOTALL
)
_TASK_NOTIF_TAG_RE = re.compile(
    r"<(?P<tag>task-id|summary|event)>(?P<body>.*?)</(?P=tag)>", re.DOTALL
)


def is_task_notification(text: str) -> bool:
    """True when the text is an external ``<task-notification>`` envelope.

    Public predicate (plan v4 / codex r2 P3-5): the per-user echo gate in
    ``bot.handle_new_message`` must EXEMPT these system events from user-echo
    suppression without duplicating the envelope regexes. Also the CC 2.1.198
    queue-shaped-close synthesis gate in ``transcript_parser.parse_entries`` —
    the SAME object the adapter stamps ``is_task_notification`` with.
    """
    return _TASK_NOTIF_RE.match(text or "") is not None


def extract_task_notification_task_id(text: str) -> str | None:
    """Extract ``<task-id>`` from a ``<task-notification>`` envelope.

    Public extractor beside the predicate (GH #44, codex r3 P3-1 — the
    predicate alone returns bool). For a background async agent the task-id
    IS the agent key (== the sidechain ``agent-<id>.jsonl`` stem minus the
    prefix; fixture-verified). ``None`` when the text is not a recognizable
    envelope or carries no task-id.
    """
    m = _TASK_NOTIF_RE.match(text or "")
    if not m:
        return None
    for tm in _TASK_NOTIF_TAG_RE.finditer(m.group(1)):
        if tm.group("tag") == "task-id":
            body = tm.group("body").strip()
            return body or None
    return None


# The agent-teams teammate ``idle_notification`` envelope (GH #46 PR-1). A
# teammate's park report lands on the PARENT transcript as a ``type:"user"``
# text entry that begins with the sentinel line below and wraps a
# ``<teammate-message …>…</teammate-message>`` envelope. Scan ONLY the first
# ``TEAMMATE_ENVELOPE_SCAN_BYTES`` bytes for the closing tag — a huge unclosed
# body must never trigger an unbounded scan; a not-closed-within-bound envelope
# fails CLOSED to genuine-user (never suppressing a real human turn).
TEAMMATE_ENVELOPE_SCAN_BYTES = 65536

_TEAMMATE_FIRST_LINE = "Another Claude session sent a message:"
_TEAMMATE_CLOSE_TAG = "</teammate-message>"
# Byte-0 anchored: line 1 EXACTLY the sentinel (trailing ``\r`` tolerated for
# CRLF; a leading BOM/whitespace makes ``\A`` miss → rejected), zero+
# blank/whitespace-only lines, then a line starting ``<teammate-message`` with a
# trailing word boundary (``<teammate-message>`` or ``<teammate-message …>``).
_TEAMMATE_HEAD_RE = re.compile(
    r"\AAnother Claude session sent a message:\r?\n"
    r"(?:[^\S\n]*\r?\n)*"
    r"<teammate-message\b"
)


def is_teammate_message(text: str) -> bool:
    """True when ``text`` is an agent-teams teammate-message envelope (GH #46).

    A teammate's ``idle_notification`` (and every other teammate message) is
    delivered to the PARENT as a machine-initiated ``type:"user"`` text entry;
    ``route_runtime`` and the adapter use this predicate to classify it as
    machine-initiated (preserving background-agent tombstones/stash/pane-bit)
    rather than a genuine user turn. Byte-0 anchored + bounded scan — fail-closed
    to genuine-user on ANY drift (a longer first line, a leading BOM/space, an
    envelope not closed within ``TEAMMATE_ENVELOPE_SCAN_BYTES``).
    """
    if not text:
        return False
    prefix = text[:TEAMMATE_ENVELOPE_SCAN_BYTES]
    if _TEAMMATE_HEAD_RE.match(prefix) is None:
        return False
    return _TEAMMATE_CLOSE_TAG in prefix


def normalize_background_agent_key(raw: str) -> str:
    """The GH #44 §3.0 single key-normalization contract.

    The async-launch ``agentId`` and the task-notification ``<task-id>`` are
    raw hex ids; the sidechain file stem is ``agent-<id>``. EVERY seam that
    records or queries the route_runtime ``background_agents`` /
    ``background_agents_done`` structures must pass through this helper — a
    join keyed inconsistently would mean launch provenance never attaches to
    activity/done marks (Busy fails to lift, or clears only by TTL). Strips
    ONE leading ``agent-`` prefix; otherwise identity. Lives in utils (the
    shared leaf) because session_monitor deliberately carries no
    route_runtime import; route_runtime re-exports it as public API.
    """
    return raw[6:] if raw.startswith("agent-") else raw


def parse_iso_timestamp(raw: str | None) -> float | None:
    """Parse a JSONL ISO8601 ``timestamp`` to epoch seconds.

    ``None`` on any failure — consumers (the timestamp-qualified notification
    clears, the GH #44 background-agent idle qualification) must FAIL CLOSED
    on an unparseable stamp rather than guess.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def app_dir() -> Path:
    """Resolve config directory from CC_TELEGRAM_DIR or default ~/.cc-telegram."""
    raw = os.environ.get(CC_TELEGRAM_DIR_ENV, "")
    return Path(raw).expanduser() if raw else Path.home() / ".cc-telegram"


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON data to a file atomically.

    Writes to a temporary file in the same directory, then renames it to the
    target path. This prevents data corruption if the process is interrupted
    mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=indent)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_cwd_from_jsonl(file_path: str | Path) -> str:
    """Read the cwd field from the first JSONL entry that has one."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    cwd = data.get("cwd")
                    if cwd:
                        return cwd
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return ""
