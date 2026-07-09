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
# text entry that begins with the sentinel line below and wraps one or more
# ``<teammate-message …>…</teammate-message>`` envelopes. Scan ONLY the first
# ``TEAMMATE_ENVELOPE_SCAN_BYTES`` UTF-8 BYTES (never a character count — a
# multi-byte payload must not stretch the bound; review P2a) — a huge unclosed
# body must never trigger an unbounded scan; an envelope not structurally
# complete within the bound fails CLOSED to genuine-user (never suppressing a
# real human turn).
TEAMMATE_ENVELOPE_SCAN_BYTES = 65536

_TEAMMATE_OPEN_TAG = b"<teammate-message"
_TEAMMATE_CLOSE_TAG = b"</teammate-message>"
# Byte-0 anchored: line 1 EXACTLY the sentinel (trailing ``\r`` tolerated for
# CRLF; a leading BOM/whitespace makes ``\A`` miss → rejected), zero+
# blank/whitespace-only lines, then a line starting ``<teammate-message`` with a
# trailing word boundary (``<teammate-message>`` or ``<teammate-message …>``).
_TEAMMATE_HEAD_RE = re.compile(
    rb"\AAnother Claude session sent a message:\r?\n"
    rb"(?:[^\S\n]*\r?\n)*"
    rb"<teammate-message\b"
)
_TEAMMATE_OPEN_RE = re.compile(rb"<teammate-message\b")


def teammate_envelope_payload_regions(text: str) -> list[bytes]:
    """The SINGLE bounded teammate-envelope scanner (GH #46 review P2 —
    predicate/parser structural parity).

    Returns the inner-payload byte region (between the opening tag's completing
    ``>`` and the closing ``</teammate-message>``) of EVERY structurally-valid
    envelope within the first ``TEAMMATE_ENVELOPE_SCAN_BYTES`` UTF-8 bytes, in
    order; ``[]`` when the byte-0 head anchor misses or no envelope is
    structurally complete within the bound. Structural validity per envelope:
    the opening tag COMPLETES (its ``>``) within the bound, and the closing tag
    occurs strictly AFTER that completion point and completes within the bound —
    so a close token embedded in the opening tag's quoted attribute text can
    never satisfy the close (it precedes the tag's ``>``), and a never-completing
    opening tag yields nothing. Enumeration continues after each close, so ONE
    parent entry carrying MULTIPLE envelopes (real-data verified) yields every
    region (review P1). Shared by ``is_teammate_message`` and
    ``response_builder.parse_teammate_idle_notifications`` so the two can never
    diverge on structure. Bound math is BYTES: the char pre-slice only caps the
    encode cost (chars >= 1 byte each), the byte slice enforces the bound;
    ``errors="replace"`` keeps a lone surrogate from raising (fail-closed to a
    non-matching byte, never a crash).
    """
    if not text:
        return []
    data = text[:TEAMMATE_ENVELOPE_SCAN_BYTES].encode("utf-8", errors="replace")[
        :TEAMMATE_ENVELOPE_SCAN_BYTES
    ]
    head = _TEAMMATE_HEAD_RE.match(data)
    if head is None:
        return []
    regions: list[bytes] = []
    pos = head.end() - len(_TEAMMATE_OPEN_TAG)
    while True:
        open_m = _TEAMMATE_OPEN_RE.search(data, pos)
        if open_m is None:
            break
        gt = data.find(b">", open_m.end())
        if gt < 0:
            break  # the opening tag never completes within the bound
        close_idx = data.find(_TEAMMATE_CLOSE_TAG, gt + 1)
        if close_idx < 0:
            break  # not closed after tag-completion within the bound — fail closed
        regions.append(data[gt + 1 : close_idx])
        pos = close_idx + len(_TEAMMATE_CLOSE_TAG)
    return regions


def is_teammate_message(text: str) -> bool:
    """True when ``text`` is an agent-teams teammate-message envelope (GH #46).

    A teammate's ``idle_notification`` (and every other teammate message) is
    delivered to the PARENT as a machine-initiated ``type:"user"`` text entry;
    ``route_runtime`` and the adapter use this predicate to classify it as
    machine-initiated (preserving background-agent tombstones/stash/pane-bit)
    rather than a genuine user turn. Defined as "the shared scanner finds at
    least one structurally-valid envelope" — the SAME
    ``teammate_envelope_payload_regions`` the payload parser consumes, so the
    predicate can never stamp text the parser judges structurally invalid
    (review P2b). Fail-closed to genuine-user on ANY drift: a longer first
    line, a leading BOM/space, an opening tag that never completes, a close
    token only inside the opening tag's quoted attributes, or an envelope not
    closed within the ``TEAMMATE_ENVELOPE_SCAN_BYTES`` BYTE bound.
    """
    return bool(teammate_envelope_payload_regions(text))


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
