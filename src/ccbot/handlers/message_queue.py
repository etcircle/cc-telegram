"""Per-user message queue management for ordered message delivery.

Provides a queue-based message processing system that ensures:
  - Messages are sent in receive order (FIFO)
  - Status messages always follow content messages
  - Consecutive content messages can be merged for efficiency
  - Thread-aware sending: each MessageTask carries an optional thread_id
    for Telegram topic support

Rate limiting is handled globally by AIORateLimiter on the Application.

Key components:
  - MessageTask: Dataclass representing a queued message task (with thread_id)
  - get_or_create_queue: Get or create queue and worker for a user
  - Message queue worker: Background task processing user's queue
  - Content task processing with tool_use/tool_result handling
  - Status message tracking and conversion (keyed by (user_id, thread_id))
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import RetryAfter

from ..markdown_v2 import convert_markdown
from ..session import session_manager
from ..terminal_parser import parse_status_line
from ..tmux_manager import tmux_manager
from .message_sender import (
    NO_LINK_PREVIEW,
    PARSE_MODE,
    send_photo,
    send_with_fallback,
    strip_sentinels,
)

logger = logging.getLogger(__name__)


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


# Merge limit for content messages
MERGE_MAX_LENGTH = 3800  # Leave room for markdown conversion overhead


@dataclass
class MessageTask:
    """Message task for queue processing."""

    task_type: Literal["content", "status_update", "status_clear"]
    text: str | None = None
    window_id: str | None = None
    # content type fields
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    thread_id: int | None = None  # Telegram topic thread_id for targeted send
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images


@dataclass
class ActivityDigestState:
    """Single editable per-topic activity digest for noisy tool/thinking events."""

    message_id: int
    window_id: str
    lines: list[str] = field(default_factory=list)
    tool_count: int = 0
    completed_count: int = 0
    last_text: str = ""
    done: bool = False


# Per-user message queues and worker tasks
_message_queues: dict[int, asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[int, asyncio.Task[None]] = {}
_queue_locks: dict[int, asyncio.Lock] = {}  # Protect drain/refill operations

# Map (tool_use_id, user_id, thread_id_or_0) -> telegram message_id
# for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int, int], int] = {}

# Status message tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_status_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}

# Activity digest tracking: one editable message per user/topic that collapses
# tool calls, tool results, and thinking into a Hermes-style activity card.
_activity_msg_info: dict[tuple[int, int], ActivityDigestState] = {}
_tool_activity_indices: dict[tuple[str, int, int], int] = {}
ACTIVITY_DIGEST_CONTENT_TYPES = {"tool_use", "tool_result", "thinking"}
ACTIVITY_DIGEST_MAX_LINES = 10
ACTIVITY_DIGEST_MAX_LINE_LENGTH = 180

# Flood control: user_id -> monotonic time when ban expires
_flood_until: dict[int, float] = {}

# DM fallback dedupe: avoid spamming the user every polling tick if a topic is broken.
_dm_fallback_seen: dict[tuple[int, int | None, str, int], float] = {}
DM_FALLBACK_COOLDOWN_SECONDS = 60

# Direct attention DM dedupe for normal assistant text that asks the user to decide/confirm.
_attention_dm_seen: dict[tuple[int, int | None, str, int], float] = {}
ATTENTION_DM_COOLDOWN_SECONDS = 300

# Topic ids that Telegram rejected during this process lifetime. Keep the binding
# for routing inbound replies to the tmux window, but deliver outbound updates by DM.
_bad_topic_threads: set[tuple[int, int]] = set()

# Max seconds to wait for flood control before dropping tasks
FLOOD_CONTROL_MAX_WAIT = 10


def get_message_queue(user_id: int) -> asyncio.Queue[MessageTask] | None:
    """Get the message queue for a user (if exists)."""
    return _message_queues.get(user_id)


def get_or_create_queue(bot: Bot, user_id: int) -> asyncio.Queue[MessageTask]:
    """Get or create message queue and worker for a user."""
    if user_id not in _message_queues:
        _message_queues[user_id] = asyncio.Queue()
        _queue_locks[user_id] = asyncio.Lock()
        # Start worker task for this user
        _queue_workers[user_id] = asyncio.create_task(
            _message_queue_worker(bot, user_id)
        )
    return _message_queues[user_id]


def _inspect_queue(queue: asyncio.Queue[MessageTask]) -> list[MessageTask]:
    """Non-destructively inspect all items in queue.

    Drains the queue and returns all items. Caller must refill.
    """
    items: list[MessageTask] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _can_merge_tasks(base: MessageTask, candidate: MessageTask) -> bool:
    """Check if two content tasks can be merged."""
    if base.window_id != candidate.window_id:
        return False
    if candidate.task_type != "content":
        return False
    # Activity events are handled by the editable activity digest, not merged
    # into final assistant text.
    if base.content_type in ACTIVITY_DIGEST_CONTENT_TYPES:
        return False
    if candidate.content_type in ACTIVITY_DIGEST_CONTENT_TYPES:
        return False
    return True


async def _merge_content_tasks(
    queue: asyncio.Queue[MessageTask],
    first: MessageTask,
    lock: asyncio.Lock,
) -> tuple[MessageTask, int]:
    """Merge consecutive content tasks from queue.

    Returns: (merged_task, merge_count) where merge_count is the number of
    additional tasks merged (0 if no merging occurred).

    Note on queue counter management:
        When we put items back, we call task_done() to compensate for the
        internal counter increment caused by put_nowait(). This is necessary
        because the items were already counted when originally enqueued.
        Without this compensation, queue.join() would wait indefinitely.
    """
    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []

        for i, task in enumerate(items):
            if not _can_merge_tasks(first, task):
                # Can't merge, keep this and all remaining items
                remaining = items[i:]
                break

            # Check length before merging
            task_length = sum(len(p) for p in task.parts)
            if current_length + task_length > MERGE_MAX_LENGTH:
                # Too long, stop merging
                remaining = items[i:]
                break

            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1

        # Put remaining items back into the queue
        for item in remaining:
            queue.put_nowait(item)
            # Compensate: this item was already counted when first enqueued,
            # put_nowait adds a duplicate count that must be removed
            queue.task_done()

    if merge_count == 0:
        return first, 0

    return (
        MessageTask(
            task_type="content",
            window_id=first.window_id,
            parts=merged_parts,
            tool_use_id=first.tool_use_id,
            content_type=first.content_type,
            thread_id=first.thread_id,
        ),
        merge_count,
    )


async def _message_queue_worker(bot: Bot, user_id: int) -> None:
    """Process message tasks for a user sequentially."""
    queue = _message_queues[user_id]
    lock = _queue_locks[user_id]
    logger.info(f"Message queue worker started for user {user_id}")

    while True:
        try:
            task = await queue.get()
            try:
                # Flood control: drop status, wait for content
                flood_end = _flood_until.get(user_id, 0)
                if flood_end > 0:
                    remaining = flood_end - time.monotonic()
                    if remaining > 0:
                        if task.task_type != "content":
                            # Status is ephemeral — safe to drop
                            continue
                        # Content is actual Claude output — wait then send
                        logger.debug(
                            "Flood controlled: waiting %.0fs for content (user %d)",
                            remaining,
                            user_id,
                        )
                        await asyncio.sleep(remaining)
                    # Ban expired
                    _flood_until.pop(user_id, None)
                    logger.info("Flood control lifted for user %d", user_id)

                if task.task_type == "content":
                    if task.content_type in ACTIVITY_DIGEST_CONTENT_TYPES:
                        await _process_activity_task(bot, user_id, task)
                        continue
                    # Try to merge consecutive content tasks
                    merged_task, merge_count = await _merge_content_tasks(
                        queue, task, lock
                    )
                    if merge_count > 0:
                        logger.debug(f"Merged {merge_count} tasks for user {user_id}")
                        # Mark merged tasks as done
                        for _ in range(merge_count):
                            queue.task_done()
                    await _process_content_task(bot, user_id, merged_task)
                elif task.task_type == "status_update":
                    await _process_status_update_task(bot, user_id, task)
                elif task.task_type == "status_clear":
                    await _do_clear_status_message(bot, user_id, task.thread_id or 0)
            except RetryAfter as e:
                retry_secs = (
                    e.retry_after
                    if isinstance(e.retry_after, int)
                    else int(e.retry_after.total_seconds())
                )
                if retry_secs > FLOOD_CONTROL_MAX_WAIT:
                    _flood_until[user_id] = time.monotonic() + retry_secs
                    logger.warning(
                        "Flood control for user %d: retry_after=%ds, "
                        "pausing queue until ban expires",
                        user_id,
                        retry_secs,
                    )
                else:
                    logger.warning(
                        "Flood control for user %d: waiting %ds",
                        user_id,
                        retry_secs,
                    )
                    await asyncio.sleep(retry_secs)
            except Exception as e:
                logger.error(f"Error processing message task for user {user_id}: {e}")
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info(f"Message queue worker cancelled for user {user_id}")
            break
        except Exception as e:
            logger.error(f"Unexpected error in queue worker for user {user_id}: {e}")


def _send_kwargs(thread_id: int | None) -> dict[str, int]:
    """Build message_thread_id kwargs for bot.send_message()."""
    if thread_id is not None:
        return {"message_thread_id": thread_id}
    return {}


def _delivery_target(user_id: int, thread_id: int | None) -> tuple[int, int | None]:
    """Return (chat_id, effective_thread_id), falling back to DM for known-bad topics."""
    if thread_id is not None and (user_id, thread_id) in _bad_topic_threads:
        return user_id, None
    return session_manager.resolve_chat_id(user_id, thread_id), thread_id


def _mark_bad_topic(user_id: int, thread_id: int | None) -> None:
    if thread_id is not None:
        _bad_topic_threads.add((user_id, thread_id))


async def _dm_fallback(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
    kind: str = "content",
) -> None:
    """DM content/status when topic delivery fails so messages don't disappear."""
    _mark_bad_topic(user_id, thread_id)
    display = session_manager.get_display_name(window_id) if window_id else "unknown"
    dedupe_key = (user_id, thread_id, window_id, hash((kind, text[:500])))
    now = time.monotonic()
    last = _dm_fallback_seen.get(dedupe_key)
    if last is not None and now - last < DM_FALLBACK_COOLDOWN_SECONDS:
        logger.debug(
            "Skipping duplicate DM fallback for user=%d thread=%s window=%s kind=%s",
            user_id,
            thread_id,
            window_id,
            kind,
        )
        return
    _dm_fallback_seen[dedupe_key] = now

    prefix = f"⚠️ CCBot could not post this {kind} in topic {thread_id} ({display}); DM fallback:\n\n"
    body = text
    # Telegram hard limit is 4096; keep room for markdown escaping/fallback.
    if len(prefix) + len(body) > 3800:
        body = body[: 3800 - len(prefix) - 20] + "\n… [truncated]"
    try:
        sent = await send_with_fallback(bot, user_id, prefix + body)
        if sent:
            logger.info(
                "DM fallback sent for user=%d thread=%s window=%s kind=%s",
                user_id,
                thread_id,
                window_id,
                kind,
            )
        else:
            logger.error("DM fallback send returned None for user=%d thread=%s kind=%s", user_id, thread_id, kind)
    except Exception as e:
        logger.error("Failed DM fallback for user=%d thread=%s kind=%s: %s", user_id, thread_id, kind, e)



def _display_name(window_id: str) -> str:
    """Best-effort human name for a tmux window/topic."""
    return session_manager.get_display_name(window_id) or window_id or "Claude"


def _status_display_text(window_id: str, text: str) -> str:
    """Format busy status with the window/topic identity visible."""
    display = _display_name(window_id)
    return f"🟡 Busy — {display}\n{text}"


def _compact_activity_line(task: MessageTask) -> str:
    """Render a compact single-line activity entry."""
    if task.content_type == "thinking":
        return "💭 Thinking"

    raw = " ".join(part.strip() for part in task.parts if part and part.strip())
    raw = strip_sentinels(raw).replace("\n", " ")
    raw = " ".join(raw.split())
    if not raw:
        raw = task.content_type

    if "  ⎿  " in raw:
        left, rest = raw.split("  ⎿  ", 1)
        # Keep the useful stats line, drop expandable/raw output noise.
        stat = rest.split(" ", 18)
        stat_text = " ".join(stat[:18]).strip()
        raw = f"{left} — {stat_text}" if stat_text else left

    if len(raw) > ACTIVITY_DIGEST_MAX_LINE_LENGTH:
        raw = raw[: ACTIVITY_DIGEST_MAX_LINE_LENGTH - 1].rstrip() + "…"

    if task.content_type == "tool_result":
        if "error" in raw.lower():
            return f"❌ {raw}"
        if "interrupted" in raw.lower():
            return f"⏹ {raw}"
        return f"✅ {raw}"
    return f"⚙️ {raw}"


def _render_activity_digest(state: ActivityDigestState) -> str:
    """Render the editable activity digest card."""
    display = _display_name(state.window_id)
    status = "✅ Done" if state.done else "🟡 Busy"
    lines = [f"{status} — {display}"]
    if state.tool_count or state.completed_count:
        lines.append(f"Activity: {state.completed_count}/{state.tool_count} tool calls complete")
    else:
        lines.append("Activity: thinking")

    shown = state.lines[-ACTIVITY_DIGEST_MAX_LINES:]
    hidden = max(0, len(state.lines) - len(shown))
    if hidden:
        lines.append(f"• … {hidden} earlier event(s)")
    lines.extend(f"• {line}" for line in shown)
    return "\n".join(lines)


async def _upsert_activity_digest(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    state: ActivityDigestState,
) -> None:
    """Send or edit the per-topic activity digest."""
    tid = thread_id or 0
    chat_id, effective_thread_id = _delivery_target(user_id, thread_id)
    text = _render_activity_digest(state)
    if text == state.last_text:
        return

    if state.message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=state.message_id,
                text=_ensure_formatted(text),
                parse_mode=PARSE_MODE,
                link_preview_options=NO_LINK_PREVIEW,
            )
            state.last_text = text
            _activity_msg_info[(user_id, tid)] = state
            return
        except RetryAfter:
            raise
        except Exception:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=state.message_id,
                    text=strip_sentinels(text),
                    link_preview_options=NO_LINK_PREVIEW,
                )
                state.last_text = text
                _activity_msg_info[(user_id, tid)] = state
                return
            except RetryAfter:
                raise
            except Exception as e:
                logger.debug("Failed to edit activity digest: %s", e)
                state.message_id = 0

    sent = await send_with_fallback(
        bot,
        chat_id,
        text,
        **_send_kwargs(effective_thread_id),  # type: ignore[arg-type]
    )
    if sent:
        state.message_id = sent.message_id
        state.last_text = text
        _activity_msg_info[(user_id, tid)] = state
    elif thread_id is not None:
        await _dm_fallback(bot, user_id, thread_id, state.window_id, text, kind="activity")


async def _process_activity_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Collapse noisy thinking/tool events into one editable activity message."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    state = _activity_msg_info.get((user_id, tid))
    if state is None or state.window_id != wid or state.done:
        state = ActivityDigestState(message_id=0, window_id=wid)

    line = _compact_activity_line(task)
    if task.content_type == "tool_use":
        state.tool_count += 1
        state.lines.append(line)
        if task.tool_use_id:
            _tool_activity_indices[(task.tool_use_id, user_id, tid)] = len(state.lines) - 1
    elif task.content_type == "tool_result":
        state.completed_count += 1
        if task.tool_use_id and (task.tool_use_id, user_id, tid) in _tool_activity_indices:
            idx = _tool_activity_indices.pop((task.tool_use_id, user_id, tid))
            if 0 <= idx < len(state.lines):
                state.lines[idx] = line
            else:
                state.lines.append(line)
        else:
            state.lines.append(line)
    else:
        # Thinking should show that the topic is alive without sending the full
        # reasoning blob to Telegram.
        if not state.lines or state.lines[-1] != line:
            state.lines.append(line)

    state.done = False
    await _upsert_activity_digest(bot, user_id, task.thread_id, state)

    # Images are real output, not noise. Keep delivering them.
    if task.image_data:
        chat_id, effective_thread_id = _delivery_target(user_id, task.thread_id)
        await _send_task_images(bot, chat_id, task, effective_thread_id)


async def _finalize_activity_digest(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> None:
    """Mark activity digest done before the final assistant text lands."""
    tid = thread_id or 0
    state = _activity_msg_info.get((user_id, tid))
    if not state or state.window_id != window_id or state.done:
        return
    state.done = True
    await _upsert_activity_digest(bot, user_id, thread_id, state)


def _looks_like_attention_request(text: str) -> bool:
    """Heuristic: final assistant text that is probably waiting for user input."""
    cleaned = strip_sentinels(text or "").strip()
    if not cleaned:
        return False
    lower = " ".join(cleaned.lower().split())
    cues = (
        "tell me which",
        "tell me your pick",
        "tell me your picks",
        "tell me your choice",
        "which option",
        "which approach",
        "which one",
        "do you want me to",
        "want me to proceed",
        "want me to continue",
        "ok to proceed",
        "okay to proceed",
        "ok unless you",
        "unless you object",
        "please confirm",
        "confirm before",
        "before i write",
        "before i proceed",
        "owner decision",
        "owner decisions",
        "your recommendation",
        "go with recommendations",
        "go with your recommendations",
        "tell me your picks",
    )
    if any(cue in lower for cue in cues):
        return True
    # A direct final question is usually attention-worthy; avoid tiny greetings.
    return cleaned.endswith("?") and len(cleaned) > 80


async def _attention_dm_if_needed(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    text: str,
) -> None:
    """Send a direct DM when normal assistant text is actually waiting for the user."""
    if not _looks_like_attention_request(text):
        return
    display = session_manager.get_display_name(window_id) if window_id else "unknown"
    cleaned = strip_sentinels(text).strip()
    dedupe_key = (user_id, thread_id, window_id, hash(cleaned[:1000]))
    now = time.monotonic()
    last = _attention_dm_seen.get(dedupe_key)
    if last is not None and now - last < ATTENTION_DM_COOLDOWN_SECONDS:
        logger.debug(
            "Skipping duplicate attention DM for user=%d thread=%s window=%s",
            user_id,
            thread_id,
            window_id,
        )
        return
    _attention_dm_seen[dedupe_key] = now

    prefix = f"🔔 Claude needs your input in {display}"
    if thread_id is not None:
        prefix += f" (topic {thread_id})"
    prefix += ":\n\n"
    body = cleaned
    if len(prefix) + len(body) > 3800:
        body = body[: 3800 - len(prefix) - 20] + "\n… [truncated]"
    try:
        sent = await send_with_fallback(bot, user_id, prefix + body)
        if sent:
            logger.info(
                "Attention DM sent for user=%d thread=%s window=%s",
                user_id,
                thread_id,
                window_id,
            )
        else:
            logger.warning(
                "Attention DM send returned None for user=%d thread=%s window=%s",
                user_id,
                thread_id,
                window_id,
            )
    except Exception as e:
        logger.warning(
            "Failed attention DM for user=%d thread=%s window=%s: %s",
            user_id,
            thread_id,
            window_id,
            e,
        )


async def _send_task_images(bot: Bot, chat_id: int, task: MessageTask, effective_thread_id: int | None = None) -> None:
    """Send images attached to a task, if any."""
    if not task.image_data:
        return
    logger.info(
        "Sending %d image(s) in thread %s",
        len(task.image_data),
        task.thread_id,
    )
    await send_photo(
        bot,
        chat_id,
        task.image_data,
        **_send_kwargs(effective_thread_id),  # type: ignore[arg-type]
    )


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Process a content message task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id, effective_thread_id = _delivery_target(user_id, task.thread_id)

    if task.content_type == "text":
        await _finalize_activity_digest(bot, user_id, task.thread_id, wid)

    # 1. Handle tool_result editing (merged parts are edited together)
    if task.content_type == "tool_result" and task.tool_use_id:
        _tkey = (task.tool_use_id, user_id, tid)
        edit_msg_id = _tool_msg_ids.pop(_tkey, None)
        if edit_msg_id is not None:
            # Clear status message first
            await _do_clear_status_message(bot, user_id, tid)
            # Join all parts for editing (merged content goes together)
            full_text = "\n\n".join(task.parts)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_msg_id,
                    text=_ensure_formatted(full_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                await _send_task_images(bot, chat_id, task, effective_thread_id)
                await _check_and_send_status(bot, user_id, wid, task.thread_id)
                return
            except RetryAfter:
                raise
            except Exception:
                try:
                    # Fallback: plain text with sentinels stripped
                    plain_text = strip_sentinels(task.text or full_text)
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=edit_msg_id,
                        text=plain_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    await _send_task_images(bot, chat_id, task, effective_thread_id)
                    await _check_and_send_status(bot, user_id, wid, task.thread_id)
                    return
                except RetryAfter:
                    raise
                except Exception:
                    logger.debug(f"Failed to edit tool msg {edit_msg_id}, sending new")
                    # Fall through to send as new message

    # 2. Send content messages, converting status message to first content part
    first_part = True
    last_msg_id: int | None = None
    for part in task.parts:
        sent = None

        # For first part, try to convert status message to content (edit instead of delete)
        if first_part:
            first_part = False
            converted_msg_id = await _convert_status_to_content(
                bot,
                user_id,
                tid,
                wid,
                part,
            )
            if converted_msg_id is not None:
                last_msg_id = converted_msg_id
                await _attention_dm_if_needed(bot, user_id, task.thread_id, wid, part)
                continue

        sent = await send_with_fallback(
            bot,
            chat_id,
            part,
            **_send_kwargs(effective_thread_id),  # type: ignore[arg-type]
        )

        if sent:
            last_msg_id = sent.message_id
        elif task.thread_id is not None:
            await _dm_fallback(bot, user_id, task.thread_id, wid, part)
        await _attention_dm_if_needed(bot, user_id, task.thread_id, wid, part)

    # 3. Record tool_use message ID for later editing
    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id

    # 4. Send images if present (from tool_result with base64 image blocks)
    await _send_task_images(bot, chat_id, task, effective_thread_id)

    # 5. After content, check and send status
    await _check_and_send_status(bot, user_id, wid, task.thread_id)


async def _convert_status_to_content(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    content_text: str,
) -> int | None:
    """Convert status message to content message by editing it.

    Returns the message_id if converted successfully, None otherwise.
    """
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if not info:
        return None

    msg_id, stored_wid, _ = info
    chat_id, effective_thread_id = _delivery_target(user_id, thread_id_or_0 or None)
    if stored_wid != window_id:
        # Different window, just delete the old status
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        return None

    # Edit status message to show content
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=_ensure_formatted(content_text),
            parse_mode=PARSE_MODE,
            link_preview_options=NO_LINK_PREVIEW,
        )
        return msg_id
    except RetryAfter:
        raise
    except Exception:
        try:
            # Fallback to plain text with sentinels stripped
            plain = strip_sentinels(content_text)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=plain,
                link_preview_options=NO_LINK_PREVIEW,
            )
            return msg_id
        except RetryAfter:
            raise
        except Exception as e:
            logger.debug(f"Failed to convert status to content: {e}")
            # Message might be deleted or too old, caller will send new message
            return None


async def _process_status_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Process a status update task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id, effective_thread_id = _delivery_target(user_id, task.thread_id)
    skey = (user_id, tid)
    status_text = task.text or ""

    if not status_text:
        # No status text means clear status
        await _do_clear_status_message(bot, user_id, tid)
        return

    current_info = _status_msg_info.get(skey)

    if current_info:
        msg_id, stored_wid, last_text = current_info

        if stored_wid != wid:
            # Window changed - delete old and send new
            await _do_clear_status_message(bot, user_id, tid)
            await _do_send_status_message(bot, user_id, tid, wid, status_text)
        elif status_text == last_text:
            # Same content, skip edit
            return
        else:
            # Same window, text changed - edit in place
            # Send typing indicator when Claude is working
            if "esc to interrupt" in status_text.lower():
                try:
                    await bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                except RetryAfter:
                    raise
                except Exception:
                    pass
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_ensure_formatted(_status_display_text(wid, status_text)),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                _status_msg_info[skey] = (msg_id, wid, status_text)
            except RetryAfter:
                raise
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=_status_display_text(wid, status_text),
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    _status_msg_info[skey] = (msg_id, wid, status_text)
                except RetryAfter:
                    raise
                except Exception as e:
                    logger.debug(f"Failed to edit status message: {e}")
                    _status_msg_info.pop(skey, None)
                    await _do_send_status_message(bot, user_id, tid, wid, status_text)
    else:
        # No existing status message, send new
        await _do_send_status_message(bot, user_id, tid, wid, status_text)


async def _do_send_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Send a new status message and track it (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id, effective_thread_id = _delivery_target(user_id, thread_id)
    # Safety net: delete any orphaned status message before sending a new one.
    # This catches edge cases where tracking was cleared without deleting the message.
    old = _status_msg_info.pop(skey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass
    # Send typing indicator when Claude is working
    if "esc to interrupt" in text.lower():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except RetryAfter:
            raise
        except Exception:
            pass
    sent = await send_with_fallback(
        bot,
        chat_id,
        _status_display_text(window_id, text),
        **_send_kwargs(effective_thread_id),  # type: ignore[arg-type]
    )
    if sent:
        _status_msg_info[skey] = (sent.message_id, window_id, text)
    elif thread_id is not None:
        await _dm_fallback(bot, user_id, thread_id, window_id, _status_display_text(window_id, text), kind="status")


async def _do_clear_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
) -> None:
    """Delete the status message for a user (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if info:
        msg_id = info[0]
        chat_id, effective_thread_id = _delivery_target(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete status message {msg_id}: {e}")


async def _check_and_send_status(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Check terminal for status line and send status message if present."""
    # Skip if there are more messages pending in the queue
    queue = _message_queues.get(user_id)
    if queue and not queue.empty():
        return
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return

    tid = thread_id or 0
    status_line = parse_status_line(pane_text)
    if status_line:
        await _do_send_status_message(bot, user_id, tid, window_id, status_line)


async def enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    parts: list[str],
    tool_use_id: str | None = None,
    content_type: str = "text",
    text: str | None = None,
    thread_id: int | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
) -> None:
    """Enqueue a content message task."""
    logger.debug(
        "Enqueue content: user=%d, window_id=%s, content_type=%s",
        user_id,
        window_id,
        content_type,
    )
    queue = get_or_create_queue(bot, user_id)

    task = MessageTask(
        task_type="content",
        text=text,
        window_id=window_id,
        parts=parts,
        tool_use_id=tool_use_id,
        content_type=content_type,
        thread_id=thread_id,
        image_data=image_data,
    )
    queue.put_nowait(task)


async def enqueue_status_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    status_text: str | None,
    thread_id: int | None = None,
) -> None:
    """Enqueue status update. Skipped if text unchanged or during flood control."""
    # Don't enqueue during flood control — they'd just be dropped
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > time.monotonic():
        return

    tid = thread_id or 0

    # Deduplicate: skip if text matches what's already displayed
    if status_text:
        skey = (user_id, tid)
        info = _status_msg_info.get(skey)
        if info and info[1] == window_id and info[2] == status_text:
            return

    queue = get_or_create_queue(bot, user_id)

    if status_text:
        task = MessageTask(
            task_type="status_update",
            text=status_text,
            window_id=window_id,
            thread_id=thread_id,
        )
    else:
        task = MessageTask(task_type="status_clear", thread_id=thread_id)

    queue.put_nowait(task)


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a user (and optionally a specific thread)."""
    skey = (user_id, thread_id or 0)
    _status_msg_info.pop(skey, None)


def clear_tool_msg_ids_for_topic(user_id: int, thread_id: int | None = None) -> None:
    """Clear tool message ID tracking for a specific topic.

    Removes all entries in _tool_msg_ids that match the given user and thread.
    """
    tid = thread_id or 0
    # Find and remove all matching keys
    keys_to_remove = [
        key for key in _tool_msg_ids if key[1] == user_id and key[2] == tid
    ]
    for key in keys_to_remove:
        _tool_msg_ids.pop(key, None)


async def shutdown_workers() -> None:
    """Stop all queue workers (called during bot shutdown)."""
    for _, worker in list(_queue_workers.items()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    _queue_workers.clear()
    _message_queues.clear()
    _queue_locks.clear()
    logger.info("Message queue workers stopped")
