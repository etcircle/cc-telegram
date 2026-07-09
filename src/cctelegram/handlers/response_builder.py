"""Response message building for Telegram delivery.

Builds paginated response messages from Claude Code output:
  - Handles different content types (text, thinking, tool_use, tool_result)
  - Splits long messages into pages within Telegram's 4096 char limit
  - Truncates thinking content to keep messages compact

Markdown conversion is NOT done here — the send layer (message_sender,
message_queue) handles convert_markdown() so each message is converted
exactly once.

Key function:
  - build_response_parts: Build paginated response messages
"""

import json
import re
from dataclasses import dataclass

from ..markdown_v2 import convert_markdown_tables
from ..telegram_sender import split_message
from ..transcript_parser import TranscriptParser

# The ``<task-notification>`` envelope helpers moved to ``utils`` (the true leaf
# — this module imports ``TranscriptParser``, so ``utils.py`` must own them to
# avoid an import cycle when ``transcript_parser`` needs the SAME predicate
# object it synthesizes the CC 2.1.198 queue-shaped close with; Codex r2 P1).
# Re-exported here ALIAS-ONLY (identical objects, never wrappers — the redundant
# ``as`` marks the intentional re-export) so bot.py / transcript_event_adapter /
# session_monitor keep their existing ``handlers.response_builder`` import path,
# and ``route_runtime``'s genuine-user vs task-notification branch keys on the
# SAME predicate the parser synthesizes with. ``_render_task_notification``
# below uses the two regexes directly.
from ..utils import _TASK_NOTIF_RE as _TASK_NOTIF_RE
from ..utils import _TASK_NOTIF_TAG_RE as _TASK_NOTIF_TAG_RE
from ..utils import (
    TEAMMATE_ENVELOPE_SCAN_BYTES as TEAMMATE_ENVELOPE_SCAN_BYTES,
)
from ..utils import (
    extract_task_notification_task_id as extract_task_notification_task_id,
)
from ..utils import is_task_notification as is_task_notification
from ..utils import is_teammate_message as is_teammate_message
from ..utils import parse_iso_timestamp as parse_iso_timestamp
from ..utils import (
    teammate_envelope_payload_regions as teammate_envelope_payload_regions,
)


# The async-launch background discriminator (GH #44 §3.2a). Anchored on the
# STRUCTURED ``agentId: <id>`` line — the surrounding success sentence is
# diagnostic/fixture coverage only, never load-bearing (codex r3 + hermes
# §9-2: TUI prose drifts across Claude Code versions; the id line is the
# stable part). Callers scope it to Agent/Task tool_result text.
# Leading whitespace tolerated: the transcript parser renders tool_result
# content indented under the "⎿" marker.
_ASYNC_LAUNCH_AGENT_ID_RE = re.compile(
    r"^\s*agentId:\s*([0-9a-fA-F]{6,})\b", re.MULTILINE
)


def extract_async_agent_launch_id(text: str) -> str | None:
    """Extract the ``agentId`` from an async-Agent-launch ``tool_result``.

    Returns the raw id (no ``agent-`` prefix — normalize with
    ``route_runtime.normalize_background_agent_key`` before keying) or
    ``None`` when no ``agentId:`` line is present. Synchronous agents never
    produce one (their tool_result is the agent's final report).
    """
    if not text:
        return None
    m = _ASYNC_LAUNCH_AGENT_ID_RE.search(text)
    return m.group(1) if m else None


def async_agent_launch_id_from_meta(meta: object) -> str | None:
    """Extract a plain ``run_in_background`` Agent's ``agentId`` from the
    STRUCTURED entry-level ``toolUseResult`` (Fix #5 — the version-robust
    PRIMARY anchor; ``extract_async_agent_launch_id`` prose regex is the
    fallback, mirroring the Workflow PR-2 structured-primary pattern).

    ``meta`` is the raw JSONL ``toolUseResult`` dict (or ``None`` / a non-dict).
    Returns the raw agentId (no ``agent-`` prefix — normalize before keying) or
    ``None`` when ``meta`` is not a plain-Agent ``async_launched`` result.

    Discrimination (verified on real di-copilot data): a plain async Agent
    carries ``{status: "async_launched", isAsync: True, agentId: <hex>}``; a
    WORKFLOW launch ALSO carries ``status == "async_launched"`` but ``agentId``
    is absent/None (it uses ``taskId``/``runId``); a SYNCHRONOUS agent carries
    ``status == "completed"`` and no ``agentId``. So we require ALL of
    ``status == "async_launched"`` + ``isAsync is True`` + a non-empty
    ``agentId`` — excluding both Workflows and sync agents.
    """
    if not isinstance(meta, dict):
        return None
    if meta.get("status") != "async_launched" or meta.get("isAsync") is not True:
        return None
    aid = meta.get("agentId")
    return aid if isinstance(aid, str) and aid else None


# The Workflow-tool launch discriminator (ISSUE-6 / Fix 2a). DIFFERENT shape
# from the Agent/Task ``agentId:`` launch — verified against 34 real launches:
# the Task ID is MID-LINE ("Workflow launched in background. Task ID: <id>"),
# the id is the last token on its line, and Task ID (e.g. ``w13z7jqx6``) ≠ Run
# ID (e.g. ``wf_54f46aea-ba6``). ``^.*`` allows the line prefix (incl. the
# ⎿/indent the transcript parser renders tool_result content under); ``\b``
# before "Task ID" prevents a "Subtask ID" false match; the id is bounded by
# line-end so a wrapping backtick / one trailing punct sits OUTSIDE the
# capture → the captured id EQUALS the ``<task-notification>`` close key
# (``extract_task_notification_task_id``), the bracket open/close parity
# invariant. A line-start ``^\s*Task ID:`` anchor (the plan's draft) would
# match NOTHING on the real mid-line shape.
_WF_LAUNCH_TASK_ID_RE = re.compile(
    r"(?im)^.*\bTask ID:\s*`?([A-Za-z0-9_-]+)`?\s*[.,;:)\]]?\s*$"
)
_WF_LAUNCH_RUN_ID_RE = re.compile(r"(?im)^\s*Run ID:\s*(wf_[A-Za-z0-9_-]+)\s*$")
_WF_LAUNCH_DIR_RE = re.compile(r"(?im)^\s*Transcript dir:\s*(\S+)\s*$")


@dataclass
class WorkflowLaunchInfo:
    """Parsed fields from a Workflow-tool launch ``tool_result`` (Fix 2a).

    ``task_id`` is the bracket key body (``wf-task:<task_id>``) and equals the
    ``<task-notification>`` close key. ``transcript_dir`` is kept ONLY when it
    is a real Workflow sidechain dir (under ``subagents/workflows/wf_…``) — it
    feeds the Fix 2c per-bracket mtime heartbeat; a non-Workflow path is
    dropped to ``None`` so the bracket falls back to the launch-wall TTL.
    """

    task_id: str
    run_id: str | None
    transcript_dir: str | None


def extract_workflow_launch_info(text: str) -> WorkflowLaunchInfo | None:
    """Parse a Workflow-tool launch ``tool_result`` (ISSUE-6 / Fix 2a).

    Returns the Task ID + (optional) Run ID + (validated) Transcript dir, or
    ``None`` when no Task ID line is present (the caller logs and opens no
    bracket). Scoped by the caller to ``tool_name == "Workflow"`` tool_results.
    """
    if not text:
        return None
    m = _WF_LAUNCH_TASK_ID_RE.search(text)
    if not m:
        return None
    task_id = m.group(1)
    rm = _WF_LAUNCH_RUN_ID_RE.search(text)
    run_id = rm.group(1) if rm else None
    dm = _WF_LAUNCH_DIR_RE.search(text)
    tdir = dm.group(1) if dm else None
    if tdir and "subagents/workflows/wf_" not in tdir:
        # Only a real Workflow transcript dir feeds the Fix 2c mtime heartbeat.
        tdir = None
    return WorkflowLaunchInfo(task_id=task_id, run_id=run_id, transcript_dir=tdir)


def workflow_launch_info_from_meta(meta: object) -> WorkflowLaunchInfo | None:
    """Parse a Workflow launch from the STRUCTURED entry-level ``toolUseResult``
    (PR-2 — the PRIMARY anchor; ``extract_workflow_launch_info`` prose regex is
    the fallback).

    ``meta`` is the raw ``ParsedEntry.tool_result_meta`` (the JSONL
    ``toolUseResult`` dict, or ``None`` / a non-dict). Returns the Task ID +
    (optional) Run ID + (validated) Transcript dir, or ``None`` when ``meta`` is
    not a Workflow ``async_launched`` result.

    Discrimination note: the Agent/Task ``run_in_background`` async launch ALSO
    carries ``status == "async_launched"`` but a DIFFERENT shape (``agentId``,
    NO ``taskId``) — verified 54-vs-40 in the project JSONL history. So we key on
    the Workflow fields (``taskId``), NEVER on ``status`` alone; an Agent shape
    returns ``None`` (its caller is the ``tool_name == "Workflow"`` branch, so it
    never reaches here in practice, but the guard makes it fail-safe).

    ``transcriptDir`` reuses the SAME drop-to-None guard as the prose parser
    (a path not under ``subagents/workflows/wf_`` is dropped, so only a real
    Workflow sidechain dir feeds the Fix 2c mtime heartbeat).
    """
    if not isinstance(meta, dict):
        return None
    if meta.get("status") != "async_launched":
        return None
    task_id = meta.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        return None
    run_id_raw = meta.get("runId")
    run_id = run_id_raw if isinstance(run_id_raw, str) and run_id_raw else None
    tdir_raw = meta.get("transcriptDir")
    tdir = tdir_raw if isinstance(tdir_raw, str) and tdir_raw else None
    if tdir and "subagents/workflows/wf_" not in tdir:
        # Only a real Workflow transcript dir feeds the Fix 2c mtime heartbeat.
        tdir = None
    return WorkflowLaunchInfo(task_id=task_id, run_id=run_id, transcript_dir=tdir)


def extract_workflow_launch_task_id(text: str) -> str | None:
    """The Workflow launch Task ID — parity with the close key.

    ``extract_workflow_launch_task_id(launch) == extract_task_notification_task_id(close)``
    for all four rendered shapes (``id`` / ``` `id` ``` / ``id.`` /
    ``` `id`. ```), so ``wf-task:<launch>`` == ``wf-task:<close>`` (the bracket
    opens AND closes).
    """
    info = extract_workflow_launch_info(text)
    return info.task_id if info else None


def background_bash_task_id_from_meta(meta: object) -> str | None:
    """Extract a ``run_in_background`` Bash task id from the STRUCTURED
    entry-level ``toolUseResult`` (typing-unification T1.1, 2026-07-08).

    A ``Bash`` tool call with ``run_in_background=true`` writes a tool_result
    whose entry-level ``toolUseResult`` carries a non-empty ``backgroundTaskId``
    (verified real JSONL, Claude Code 2.1.197:
    ``{"stdout":"","stderr":"", ..., "backgroundTaskId":"byziqxhyh"}``). That id
    EQUALS the ``<task-notification>`` ``<task-id>`` fired on completion, so the
    launch and close keys are the SAME bare id — no namespace prefix (unlike
    ``wf-task:``).

    ``meta`` is the raw ``ParsedEntry.tool_result_meta`` (the JSONL
    ``toolUseResult`` dict, or ``None`` / a non-dict). Returns the raw task id
    (normalize with ``route_runtime.normalize_background_agent_key`` before
    keying) or ``None`` when ``meta`` is not a background-Bash launch result.

    Keys on ``backgroundTaskId`` PRESENCE ONLY — never ``status`` or prose. The
    three async-launch shapes are DISJOINT: a plain Agent carries ``agentId`` +
    ``status=="async_launched"``; a Workflow carries ``taskId`` +
    ``status=="async_launched"``; a background Bash carries NEITHER ``status``
    NOR ``agentId``/``taskId`` — so this returns ``None`` for the other two, and
    ``async_agent_launch_id_from_meta`` / ``workflow_launch_info_from_meta``
    return ``None`` for a Bash meta. The caller scopes it to
    ``tool_name == "Bash"`` tool_results.
    """
    if not isinstance(meta, dict):
        return None
    task_id = meta.get("backgroundTaskId")
    return task_id if isinstance(task_id, str) and task_id else None


def resumed_agent_id_from_meta(meta: object) -> str | None:
    """Extract a RESUMED background agent's id from a ``SendMessage`` resume
    tool_result's STRUCTURED entry-level ``toolUseResult`` (Fix C, 2026-07-08).

    A ``SendMessage`` to an already-existing agent (the multi-leg orchestration
    "nudge" pattern) writes a tool_result whose entry-level ``toolUseResult``
    carries a non-empty ``resumedAgentId`` (verified real JSONL, Claude Code
    2.1.204: ``{"success": true, "message": "Agent \\"<id>\\" had no active
    task; resumed from transcript in the background …", "resumedAgentId":
    "<id>"}``). That id EQUALS the agent's ``agentId`` and the
    ``<task-notification>`` close key, so the resume/launch/close keys are the
    SAME bare id (no namespace prefix) — the resumed agent's next stop
    re-tombstones via the existing done path with ZERO new close code.

    ``meta`` is the raw ``ParsedEntry.tool_result_meta`` (the JSONL
    ``toolUseResult`` dict, or ``None`` / a non-dict). Returns the raw agent id
    (normalize with ``route_runtime.normalize_background_agent_key`` before
    keying) or ``None``. Keys on ``resumedAgentId`` PRESENCE ONLY — never
    ``status`` / ``success`` / prose. Non-str / empty / whitespace-only reject.

    FOUR-WAY DISJOINT with the other three launch shapes (verified): a plain
    Agent carries ``agentId`` + ``status``; a Workflow carries ``taskId`` +
    ``status``; a background Bash carries ``backgroundTaskId``; a resume carries
    NONE of those (only ``resumedAgentId``) — so this returns ``None`` for the
    other three metas, and ``async_agent_launch_id_from_meta`` /
    ``workflow_launch_info_from_meta`` / ``background_bash_task_id_from_meta``
    return ``None`` for a resume meta. The caller scopes it to
    ``tool_name == "SendMessage"`` tool_results.
    """
    if not isinstance(meta, dict):
        return None
    rid = meta.get("resumedAgentId")
    if not isinstance(rid, str) or not rid.strip():
        return None
    return rid


@dataclass(frozen=True)
class TeammateIdle:
    """A parsed teammate ``idle_notification`` (GH #46 PR-1).

    ``name`` — the ``from`` field (the teammate's name, e.g.
    ``explore-skill-dispatch``). ``park_ts`` — the parked-at wall-clock epoch,
    or ``None`` when the ``timestamp`` field is missing or unparseable.
    ``park_ts_unparseable`` — True whenever ``park_ts`` is ``None`` (fail-closed:
    the teammate park-close then tombstones UNCONDITIONALLY at the
    ``BgDoneSource.TEAMMATE`` ts-gate — false-dark over false-typing).
    """

    name: str
    park_ts: float | None
    park_ts_unparseable: bool


def parse_teammate_idle_notifications(text: str) -> list[TeammateIdle]:
    """Strict parse of EVERY teammate ``idle_notification`` in ``text`` (GH #46).

    One parent user entry can carry MULTIPLE ``<teammate-message>`` envelopes
    (real-data verified — the 2026-07-09T15:56:55.336Z entry carries two, and
    the SECOND names the teammate whose leg has no other close signal; review
    P1), so this enumerates every structurally-valid envelope via the shared
    ``utils.teammate_envelope_payload_regions`` scanner (the SAME structural
    judgment as ``is_teammate_message`` — review P2) and returns one
    ``TeammateIdle`` per envelope whose inner payload parses as an idle
    notification, in envelope order. Per envelope: take the substring from the
    first ``{`` to the last ``}`` inside the payload region, ``json.loads`` it
    (any failure ⇒ skip), require ``type == "idle_notification"`` and a
    non-empty str ``from`` (⇒ ``name``). ``park_ts`` is the parsed
    ``timestamp`` (``None`` when missing/unparseable); ``park_ts_unparseable``
    mirrors ``park_ts is None`` so a None stamp fails closed to an
    unconditional done downstream. Non-idle teammate envelopes (reports etc.)
    contribute nothing — the predicate still classifies the ENTRY
    machine-initiated; only parks come from here.
    """
    out: list[TeammateIdle] = []
    for region in teammate_envelope_payload_regions(text):
        j0 = region.find(b"{")
        j1 = region.rfind(b"}")
        if j0 < 0 or j1 <= j0:
            continue
        try:
            payload = json.loads(region[j0 : j1 + 1])
        except (ValueError, TypeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "idle_notification":
            continue
        name = payload.get("from")
        if not isinstance(name, str) or not name:
            continue
        ts_raw = payload.get("timestamp")
        park_ts = parse_iso_timestamp(ts_raw) if isinstance(ts_raw, str) else None
        # Fail-closed: any None stamp (missing OR unparseable) marks the park
        # unparseable so the TEAMMATE done tombstones unconditionally.
        out.append(
            TeammateIdle(
                name=name, park_ts=park_ts, park_ts_unparseable=park_ts is None
            )
        )
    return out


def _render_task_notification(text: str) -> str | None:
    """Render an external `<task-notification>` envelope as a clean card.

    Returns None if the text isn't a recognizable task-notification, in
    which case the caller falls back to the default rendering path.
    """
    m = _TASK_NOTIF_RE.match(text)
    if not m:
        return None

    task_id: str | None = None
    summary: str | None = None
    events: list[str] = []
    for tm in _TASK_NOTIF_TAG_RE.finditer(m.group(1)):
        tag = tm.group("tag")
        body = tm.group("body").strip()
        if not body:
            continue
        if tag == "task-id" and task_id is None:
            task_id = body
        elif tag == "summary" and summary is None:
            summary = body
        elif tag == "event":
            events.append(body)

    if not (task_id or summary or events):
        return None

    header = f"🔔 *Task* `{task_id}`" if task_id else "🔔 *Task notification*"
    lines = [header]
    if summary:
        lines.append(summary)
    head = "\n".join(lines)
    if events:
        events_block = "\n".join(events)
        return head + "\n\n" + TranscriptParser._format_expandable_quote(events_block)
    return head


def build_response_parts(
    text: str,
    content_type: str = "text",
    role: str = "assistant",
) -> list[str]:
    """Build paginated response messages for Telegram.

    Returns a list of raw markdown strings, each within Telegram's 4096 char limit.
    Multi-part messages get a [1/N] suffix.
    Markdown-to-MarkdownV2 conversion is done by the send layer, not here.
    """
    text = text.strip()

    # External `<task-notification>` envelopes (injected by hooks / external
    # agents as user-role prompts) get a custom card instead of the raw
    # 👤 echo — they're system events, not "the user said X".
    if role == "user":
        rendered = _render_task_notification(text)
        if rendered is not None:
            return [rendered]

    # User messages: add emoji prefix (no newline)
    if role == "user":
        prefix = "👤 "
        separator = ""
        # User messages are typically short, no special processing needed
        if len(text) > 3000:
            text = text[:3000] + "…"
        return [f"{prefix}{text}"]

    # Truncate thinking content to keep it compact
    if content_type == "thinking":
        start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
        end_tag = TranscriptParser.EXPANDABLE_QUOTE_END
        max_thinking = 500
        if start_tag in text and end_tag in text:
            inner = text[text.index(start_tag) + len(start_tag) : text.index(end_tag)]
            if len(inner) > max_thinking:
                inner = inner[:max_thinking] + "\n\n… (thinking truncated)"
            text = start_tag + inner + end_tag
        elif len(text) > max_thinking:
            text = text[:max_thinking] + "\n\n… (thinking truncated)"

    # Format based on content type
    if content_type == "thinking":
        # Thinking: prefix with "∴ Thinking…" and single newline
        prefix = "∴ Thinking…"
        separator = "\n"
    else:
        # Plain text: no prefix
        prefix = ""
        separator = ""

    # If text contains expandable quote sentinels, don't split —
    # the quote must stay atomic. Truncation is handled by
    # _render_expandable_quote in markdown_v2.py.
    if TranscriptParser.EXPANDABLE_QUOTE_START in text:
        if prefix:
            return [f"{prefix}{separator}{text}"]
        return [text]

    # Convert tables to card-style before splitting so tables aren't broken
    # across messages. The send layer's convert_markdown() call is idempotent.
    text = convert_markdown_tables(text)

    # Split first, then assemble each chunk.
    # Use conservative max to leave room for MarkdownV2 expansion at send layer.
    max_text = 3000 - len(prefix) - len(separator)

    text_chunks = split_message(text, max_length=max_text)
    total = len(text_chunks)

    if total == 1:
        if prefix:
            return [f"{prefix}{separator}{text_chunks[0]}"]
        return [text_chunks[0]]

    parts = []
    for i, chunk in enumerate(text_chunks, 1):
        if prefix:
            parts.append(f"{prefix}{separator}{chunk}\n\n[{i}/{total}]")
        else:
            parts.append(f"{chunk}\n\n[{i}/{total}]")
    return parts
