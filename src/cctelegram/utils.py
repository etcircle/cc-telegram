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


def _resync_past_envelope(s: str, start: int) -> int:
    """Guarded resync past ONE body-shape-failed teammate envelope (GH #57).

    A real agent-teams finish batches the teammate's final REPORT envelope +
    the PARK envelope into ONE parent entry, report first — so the trailing
    park (the key's ONLY close signal) sat behind a markdown-body envelope the
    r3 scanner ``break``ed on. On a body-shape failure — (a) a non-``{`` body,
    (b) a ``{``-leading but undecodable body, (c) a decoded fragment with no
    structural close after it — the caller calls this to skip past the failed
    envelope's OWN close and continue, instead of stopping enumeration.

    ``start`` is the current opener's completing-``>`` index + 1 (for case (c)
    too — the search is over the raw text, independent of the failed decode):

    1. Find the first **LINE-ANCHORED** ``</teammate-message>`` at index
       ``>= start`` — "line-anchored" = preceded by ``\\n`` (covers ``\\r\\n``).
       None → return ``-1`` (caller ``break``s; fail-closed = today's behavior,
       incl. a close that lies BEYOND the byte bound). The line-anchored
       requirement is DELIBERATELY narrower than the valid-envelope path's
       same-line close (``_teammate_tag_completion`` / the whitespace-only gap
       below): it relies on the CC renderer invariant that the close tag is
       emitted on its OWN line — verified 213/213 line-anchored, 0 mid-line,
       across the full local transcript corpus (~400 session files, survey
       2026-07-16), plus the incident entry and the v2.1.197 fixture. A
       RENDERER-DRIFT AUDIT SURFACE: a hypothetical same-line-closed report
       resyncs onto the NEXT envelope's close, where the ownership guard fires
       (a line-anchored opener sits between) → hard break → ``[]``, fail-closed.
    2. **Close-ownership guard (Codex r1 P2-1, r2-broadened):** if ANY
       ``_TEAMMATE_OPEN_RE`` match — the scanner's FULL unanchored opener
       grammar, at ANY column (line-anchored, indented, or mid-line) — occurs
       at an index ``>= start`` and ``<`` the close found in (1), the current
       envelope never closed before another opener appeared (structurally
       broken) → return ``-1`` (caller ``break``s). Accepted fail-dark residual
       (GH #57): a markdown body that MENTIONS the literal opener token
       ``<teammate-message`` (quoted/fenced/indented/mid-prose) hard-breaks the
       resync and a genuine trailing park is lost — the same class as a quoted
       close in a fence, and strictly the safe direction (matches the scanner's
       own opener recognition, per Codex r2). A mid-line CLOSE mention costs
       nothing to skip and is NOT guarded (asymmetry is deliberate — an opener
       signals a crossed envelope boundary, a close does not).
    3. Return ``close_idx + len(_TEAMMATE_CLOSE_TAG)``; the caller sets ``pos``
       there and continues the opener search. Nothing is trusted after a
       resync: the next envelope must still pass the FULL structural gauntlet.
    """
    # (1) first line-anchored close at index >= start.
    close_idx = -1
    search_from = start
    while True:
        c = s.find(_TEAMMATE_CLOSE_TAG, search_from)
        if c < 0:
            break
        if c >= 1 and s[c - 1] == "\n":
            close_idx = c
            break
        search_from = c + 1
    if close_idx < 0:
        return -1  # no line-anchored close (incl. beyond-bound) — fail closed
    # (2) close-ownership guard — the FIRST opener at >= start decides (any
    # earlier one means the current envelope never closed before it).
    om = _TEAMMATE_OPEN_RE.search(s, start)
    if om is not None and om.start() < close_idx:
        return -1  # a crossed envelope boundary — fail closed (r2-broadened)
    # (3) resume past this envelope's own close.
    return close_idx + len(_TEAMMATE_CLOSE_TAG)


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
    entry can carry MULTIPLE envelopes — review P1).

    GH #57 — GUARDED RESYNC on a body-shape failure (supersedes the r3
    stop-on-invalid rule): a real agent-teams finish batches the teammate's
    final markdown REPORT envelope + the JSON PARK envelope into ONE entry,
    report first — so the r3 ``break`` at the report dropped the trailing park
    (the key's ONLY close signal → a 2 h strand). On a body-shape failure —
    (a) a body not IMMEDIATELY starting ``{`` (the markdown report; the
    incident shape), (b) a ``{``-leading but undecodable body (a report opening
    ``{status}: all checks complete`` — Codex r1 P2-3, NOT corruption-only), or
    (c) a payload that decodes but has no structural close after it (the
    decoded fragment is DISCARDED, never appended) — the scanner runs
    ``_resync_past_envelope`` (a line-anchored close + a full-grammar
    opener-before-close ownership guard) and CONTINUES; only when that returns
    ``-1`` (no line-anchored close, or a crossed envelope boundary) does it
    hard-``break``. Every OTHER failure keeps the fail-closed ``break``
    byte-identical: the byte-0 head-anchor miss, an opening tag that never
    completes (quote-aware ``-1``), and any close/bound truncation the resync
    surfaces as ``-1``. After a resync nothing is trusted — the next envelope
    passes the FULL structural gauntlet, so foreign JSON lying BETWEEN
    envelopes is still never borrowed. ACCEPTED consequences (GH #57,
    disclosed): a markdown body that MENTIONS the literal opener token
    ``<teammate-message`` hard-breaks the resync (ownership guard) → a genuine
    trailing park is lost (fail-dark, same class as a quoted close in a fence);
    and the resync relies on the CC renderer emitting the close tag on its own
    line (213/213 line-anchored, survey 2026-07-16 — a RENDERER-DRIFT AUDIT
    SURFACE in ``_resync_past_envelope``). A predicate on a report+park entry
    is now True — CORRECT: it IS a teammate delivery, and its park closes the
    key. Bound math is BYTES: the char pre-slice caps encode cost
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
            # GH #57 case (a): non-JSON body (a markdown report) — the incident
            # shape. Guarded resync past this envelope's own close, then
            # continue; -1 keeps today's fail-closed break.
            nxt = _resync_past_envelope(s, gt + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        try:
            payload, jend = decoder.raw_decode(s, j0)
        except ValueError:
            # GH #57 case (b): a {-leading but undecodable body (a report
            # opening `{status}: …`) — guarded resync (NOT corruption-only).
            nxt = _resync_past_envelope(s, gt + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        k = jend
        while k < len(s) and s[k] in " \t\r\n":
            k += 1
        if not s.startswith(_TEAMMATE_CLOSE_TAG, k):
            # GH #57 case (c): payload decoded but no structural close follows
            # (a report opening with a JSON-looking fragment then prose) — the
            # decoded fragment is DISCARDED (never appended), guarded resync.
            nxt = _resync_past_envelope(s, gt + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
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
    or an envelope not complete within the ``TEAMMATE_ENVELOPE_SCAN_BYTES``
    BYTE bound. GH #57: a report+park entry — a markdown-report envelope
    followed by the JSON park envelope — now classifies machine-initiated
    (the scanner's guarded resync skips the report body and yields the park),
    which is CORRECT: it IS a teammate delivery. A markdown-ONLY entry (no
    valid JSON envelope reachable through the resync) still yields ``[]`` →
    genuine-user (standing doctrine).
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
