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

_TEAMMATE_CLOSE_TAG = "</teammate-message>"
# Byte-0 anchored: line 1 EXACTLY the sentinel (trailing ``\r`` tolerated for
# CRLF; a leading BOM/whitespace makes ``\A`` miss → rejected), zero+
# blank/whitespace-only lines, then a line starting ``<teammate-message``
# followed by an EXPLICIT delimiter — whitespace or ``>`` (review r2 P2(ii):
# ``\b`` accepted ``<teammate-message!broken>``; the lookahead does not).
_TEAMMATE_HEAD_RE = re.compile(
    r"\AAnother Claude session sent a message:\r?\n"
    r"(?:[^\S\n]*\r?\n)*"
    r"<teammate-message(?=[\s>])"
)
_TEAMMATE_OPEN_RE = re.compile(r"<teammate-message(?=[\s>])")


def _teammate_tag_completion(s: str, start: int) -> int:
    """Quote-aware scan for the opening tag's completing ``>`` (review r2
    P2(i)): a ``>`` inside a single- or double-quoted attribute value never
    completes the tag. A raw ``<`` ANYWHERE before the completing ``>`` —
    INCLUDING inside quote state — rejects the opener (Hermes r3 P2 + r4 P2):
    a legitimate CC-generated opening tag's quoted attribute values
    (``teammate_id``, ``color``) never contain ``<``, so an in-quote ``<`` is
    always evidence the scan has crossed into a FOLLOWING tag. Without the
    in-quote arm, an UNTERMINATED quoted attribute swallows a later tag
    boundary: the quote state stays open across ``<teammate-message …>`` /
    ``</teammate-message>``, a later quote char flips it closed, an unquoted
    ``>`` "completes" the opener on foreign text, and the immediate-start rule
    then decodes FOREIGN JSON into a park for a teammate this envelope never
    named (the r3 bug class re-entered through quotes). The current tag's own
    ``<`` precedes ``start``. Returns the index of the completing ``>`` or
    ``-1`` (never completed / structurally invalid — fail closed)."""
    quote: str | None = None
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "<":
            return -1  # a tag boundary before completion — fail closed (r4)
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == ">":
            return i
    return -1


def teammate_envelope_payloads(text: str) -> list[Any]:
    """The SINGLE bounded teammate-envelope scanner (GH #46 review P2/r2 —
    predicate/parser structural parity by construction).

    Returns the DECODED inner JSON payload of EVERY structurally-valid
    ``<teammate-message>`` envelope within the first
    ``TEAMMATE_ENVELOPE_SCAN_BYTES`` UTF-8 BYTES, in order; ``[]`` when the
    byte-0 head anchor misses or no envelope is structurally valid within the
    bound. Per envelope: (i) the opening tag must COMPLETE via a QUOTE-AWARE
    ``>`` scan (a quoted ``>`` — or a close token embedded in a quoted
    attribute — never completes it); (ii) the tag name must be followed by an
    explicit whitespace/``>`` delimiter; (iii) the payload is decoded with
    ``json.JSONDecoder().raw_decode`` from the ``{`` that must IMMEDIATELY
    follow tag completion (optional whitespace only — Codex r3 P1: a
    free-ranging ``find("{")`` could cross the envelope boundary and borrow
    FOREIGN JSON from later text, stamping genuine-user text machine-initiated
    and minting a park for a teammate the envelope never named; the immediate
    rule means the payload start can never cross the current close tag or a
    following open tag). raw_decode stops at the JSON value's TRUE end, so a
    literal ``</teammate-message>`` INSIDE a JSON string never terminates the
    envelope (a teammate summary quoting the tag parses correctly); (iv) the
    structural close tag must follow the decoded JSON end (+ optional
    whitespace) within the bound. Enumeration continues after each close (one
    entry can carry MULTIPLE envelopes — review P1) and STOPS at the first
    structurally-invalid envelope — including a non-JSON body (no reliable
    resync point without re-introducing quote-blind close matching; earlier
    valid payloads are kept). ACCEPTED consequence (r2/r3, disclosed): an
    envelope whose body is not IMMEDIATELY a decodable JSON object — e.g. a
    markdown teammate report — classifies as genuine-user (unknown shape =
    human, the pre-GH#46 behavior; fail direction toward never suppressing a
    real human turn). Bound math is BYTES: the char pre-slice caps encode cost
    (chars >= 1 byte each), the byte slice enforces the bound, and the scan
    runs on the decode of the TRUNCATED bytes; ``errors="replace"`` keeps a
    lone surrogate / split trailing char from raising.
    """
    if not text:
        return []
    data = text[:TEAMMATE_ENVELOPE_SCAN_BYTES].encode("utf-8", errors="replace")[
        :TEAMMATE_ENVELOPE_SCAN_BYTES
    ]
    s = data.decode("utf-8", errors="replace")
    if _TEAMMATE_HEAD_RE.match(s) is None:
        return []
    payloads: list[Any] = []
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        open_m = _TEAMMATE_OPEN_RE.search(s, pos)
        if open_m is None:
            break
        gt = _teammate_tag_completion(s, open_m.end())
        if gt < 0:
            break  # the opening tag never completes within the bound
        # Codex r3 P1: the payload must start IMMEDIATELY after the tag
        # completion (whitespace-only gap) — never a free-ranging find, which
        # could cross the envelope boundary and borrow foreign JSON.
        j0 = gt + 1
        while j0 < len(s) and s[j0] in " \t\r\n":
            j0 += 1
        if j0 >= len(s) or s[j0] != "{":
            break  # non-JSON body / nothing after the tag — fail closed
        try:
            payload, jend = decoder.raw_decode(s, j0)
        except ValueError:
            break  # undecodable payload — unknown shape ⇒ genuine-user
        k = jend
        while k < len(s) and s[k] in " \t\r\n":
            k += 1
        if not s.startswith(_TEAMMATE_CLOSE_TAG, k):
            break  # no structural close right after the payload — fail closed
        payloads.append(payload)
        pos = k + len(_TEAMMATE_CLOSE_TAG)
    return payloads


def is_teammate_message(text: str) -> bool:
    """True when ``text`` is an agent-teams teammate-message envelope (GH #46).

    A teammate's ``idle_notification`` (and every other JSON-payload teammate
    message) is delivered to the PARENT as a machine-initiated ``type:"user"``
    text entry; ``route_runtime`` and the adapter use this predicate to
    classify it as machine-initiated (preserving background-agent
    tombstones/stash/pane-bit) rather than a genuine user turn. Defined as
    "the shared scanner yields at least one payload" — the SAME
    ``teammate_envelope_payloads`` the park parser consumes, so predicate-True
    IMPLIES a decodable JSON payload + a structural close and the two can
    never diverge on structure (review r2 P2, the explicit contract).
    Fail-closed to genuine-user on ANY drift: a longer first line, a leading
    BOM/space, a malformed tag name delimiter, an opening tag that never
    completes (quote-aware), a close token only inside quoted attribute text,
    a non-JSON body, or an envelope not complete within the
    ``TEAMMATE_ENVELOPE_SCAN_BYTES`` BYTE bound.
    """
    return bool(teammate_envelope_payloads(text))


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
