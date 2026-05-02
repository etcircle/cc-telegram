"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Cleans up bindings whose tmux window has been killed

Topic-existence detection is reactive only: real topic_send/topic_edit failures
classify into ``_TOPIC_BROKEN_OUTCOMES`` and trigger emergency DMs from the
message queue. We deliberately do NOT poll Telegram for topic liveness; the
previously-used ``unpin_all_forum_topic_messages`` probe was destructive (it
clears pinned messages on success, not a no-op) and runs every 60s for every
bound topic, which would silently wipe legitimate user pins.

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import logging
import time
from typing import Literal

from telegram import Bot
from telegram.constants import ChatAction

from ..config import config
from ..session import session_manager
from ..terminal_parser import (
    extract_context_pct,
    is_interactive_ui,
    is_status_active,
    parse_status_line,
)
from ..tmux_manager import tmux_manager
from . import busy_indicator
from .busy_indicator import RunState
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .cleanup import clear_topic_state
from .message_queue import enqueue_status_update, get_content_queue

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Typing-action refresh interval. Telegram drops the native typing indicator
# after ~5s, so we re-emit faster than that. Decoupled from status polling
# because the per-binding tmux fan-out in ``status_poll_loop`` can push the
# full cycle past the 5s TTL (~6-8s on macOS with ~14 bindings), making the
# indicator flash on instead of staying continuous. This loop reads
# ``busy_indicator.state(route)`` directly with no tmux I/O so cadence stays
# tight regardless of binding count.
TYPING_ACTION_INTERVAL = 3.0

# Wall-clock seconds of confirmed idle (post-completion summary or no status
# line) before the stale "🟡 Busy" message is cleared. Time-based rather than
# poll-count-based because the polling loop iterates all bindings sequentially
# — with N bound topics, any single window is only polled every N seconds, so
# a poll-count threshold makes the perceived clear delay scale with how many
# topics the user has open. 4s of confirmed idle is comfortably longer than
# Claude's slowest UI transition while still feeling responsive.
IDLE_CLEAR_DELAY_SECONDS = 4.0

# Per-route idle-state machine, keyed by ``(user_id, thread_id_or_0)``:
#   - missing   → last poll saw an active status (or this route is brand new)
#   - float ts  → first poll where idle was confirmed; waiting out the delay
#   - "cleared" → idle delay elapsed and the clear has already been enqueued;
#                 further idle ticks are no-ops until ``is_running`` flips
#                 back true and the entry is dropped.
_idle_state: dict[tuple[int, int], float | Literal["cleared"]] = {}


def reset_idle_counter(user_id: int, thread_id: int | None) -> None:
    """Drop the idle state for a route.

    Called by topic teardown so a re-bound topic starts with a clean slate
    instead of inheriting a stale entry from a previous binding.
    """
    _idle_state.pop((user_id, thread_id or 0), None)


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot, user_id, window_id, None, thread_id=thread_id
            )
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return

    # Piggy-back on the existing pane capture for the context-window
    # indicator. Pure parser, no extra I/O.
    if config.busy_indicator_v2:
        busy_indicator.update_context_pct(
            (user_id, thread_id or 0, window_id),
            extract_context_pct(pane_text),
        )

    interactive_window = get_interactive_window(user_id, thread_id)
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if is_interactive_ui(pane_text):
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(user_id, bot, thread_id)
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(user_id, bot, thread_id)

    # Check for permission prompt (interactive UI not triggered via JSONL)
    # ALWAYS check UI, regardless of skip_status
    if should_check_new_ui and is_interactive_ui(pane_text):
        logger.debug(
            "Interactive UI detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(bot, user_id, window_id, thread_id)
        return

    # Compute active-state up front so the typing indicator fires even
    # when ``skip_status`` is True — that's when Claude is busiest and the
    # "Felix's Claude is typing…" line under the topic title is most
    # useful. Without this, the queue gets so full during active work that
    # the rest of update_status_message returns early and the typing
    # action never gets sent.
    status_line = parse_status_line(pane_text)
    key = (user_id, thread_id or 0)

    # When Claude is actively running, the spinner line sits directly above
    # the chrome separator. Post-completion summaries (e.g. "✻ Cooked for
    # 2s") get a blank line inserted above the chrome — same spinner glyph,
    # but Claude is idle. ``is_status_active`` reads that gap; we use it
    # rather than scanning the status text for keywords because Claude's
    # working statuses ("Reading file …") don't always include "esc to
    # interrupt", and past-tense summaries don't always omit the spinner.
    is_running = bool(status_line) and is_status_active(pane_text)

    # V1 path: gate typing-action on the pane-derived ``is_running``. V2
    # delegates the typing-action send to ``typing_action_loop`` (it reads
    # busy_indicator.state directly with no tmux I/O so cadence stays under
    # Telegram's 5s TTL even with many bindings). Firing it from both places
    # would just double-bill the API.
    typing_active = (not config.busy_indicator_v2) and is_running

    if typing_active:
        # Re-emit Telegram's native typing indicator on every active poll
        # so the "Felix's Claude is typing…" line under the topic title
        # stays alive while Claude works. The action expires after ~5s, and
        # we poll roughly every second per binding, so this keeps the
        # indicator continuous without burning excessive API calls.
        # Pass message_thread_id so the indicator shows in the topic, not
        # at the chat level.
        try:
            await bot.send_chat_action(
                chat_id=session_manager.resolve_chat_id(user_id, thread_id),
                action=ChatAction.TYPING,
                message_thread_id=thread_id,
            )
            logger.debug(
                "typing_action user=%d thread=%s window=%s sent",
                user_id,
                thread_id,
                window_id,
            )
        except Exception as e:
            # Best-effort: never block the status update on a transient
            # chat-action failure (rate limit, network, etc.).
            logger.debug(
                "typing_action user=%d thread=%s window=%s failed: %s",
                user_id,
                thread_id,
                window_id,
                e,
            )

    # Normal status line check — skip the rest if queue is non-empty.
    # Typing indicator already fired above so the active-work UX still
    # works during heavy queue activity.
    if skip_status:
        return

    if is_running:
        _idle_state.pop(key, None)
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_line,
            thread_id=thread_id,
        )
        return

    # Either no status line at all, or a static post-completion summary.
    # Wait IDLE_CLEAR_DELAY_SECONDS of confirmed idle, then clear once.
    state = _idle_state.get(key)
    if state == "cleared":
        return  # Already cleared this idle stretch.
    now = time.monotonic()
    if state is None:
        _idle_state[key] = now
        return
    # state is the timestamp of the first idle observation.
    assert isinstance(state, float)
    if (now - state) < IDLE_CLEAR_DELAY_SECONDS:
        return
    _idle_state[key] = "cleared"
    await enqueue_status_update(
        bot,
        user_id,
        window_id,
        None,
        thread_id=thread_id,
    )


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows.

    Topic-existence detection is reactive: ``topic_send``/``topic_edit`` paths
    in ``message_queue`` already classify topic-shaped failures and route them
    to emergency DMs. The previous proactive ``unpin_all_forum_topic_messages``
    probe was destructive (it clears pinned messages on success, not a no-op),
    so we no longer poll Telegram for liveness. Stale bindings whose tmux
    window has been killed are still cleaned up below per-iteration.
    """
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    while True:
        try:
            # Run per-binding work concurrently. Serial iteration scaled
            # poll latency with the binding count — with ~14 topics and
            # ~1.5s per capture_pane, a full cycle took ~21s, longer than
            # Telegram's 5s typing-action TTL. As a result the in-topic
            # "Felix's Claude is typing…" indicator expired between polls
            # and never appeared continuous. Parallel iteration brings the
            # full cycle down to roughly the slowest single binding.
            bindings = list(session_manager.iter_thread_bindings())
            if bindings:
                await asyncio.gather(
                    *(
                        _poll_one_binding(bot, user_id, thread_id, wid)
                        for user_id, thread_id, wid in bindings
                    ),
                    return_exceptions=True,
                )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)


async def _poll_one_binding(bot: Bot, user_id: int, thread_id: int, wid: str) -> None:
    """Single-binding poll body extracted from ``status_poll_loop`` so the
    outer loop can run all bindings concurrently via ``asyncio.gather``.
    """
    try:
        # Clean up stale bindings (window no longer exists)
        w = await tmux_manager.find_window_by_id(wid)
        if not w:
            session_manager.unbind_thread(user_id, thread_id)
            await clear_topic_state(user_id, thread_id, bot)
            logger.info(
                "Cleaned up stale binding: user=%d thread=%d window_id=%s",
                user_id,
                thread_id,
                wid,
            )
            return

        # UI detection happens unconditionally in update_status_message.
        # Status enqueue is skipped inside update_status_message when
        # interactive UI is detected (returns early) or when this route's
        # content queue has pending tasks (unrelated routes do not throttle).
        queue = get_content_queue((user_id, thread_id, wid))
        skip_status = queue is not None and queue.qsize() > 0

        await update_status_message(
            bot,
            user_id,
            wid,
            thread_id=thread_id,
            skip_status=skip_status,
        )
    except Exception as e:
        logger.debug(
            "Status update error for user %d thread %d: %s",
            user_id,
            thread_id,
            e,
        )


async def typing_action_loop(bot: Bot) -> None:
    """Re-emit Telegram's native typing indicator for every actively-running
    route on a fixed cadence, independent of pane polling.

    Reads ``busy_indicator.state(route)`` directly: no tmux subprocess fan-out,
    no per-binding capture_pane. With ~14 bindings the status poller's full
    cycle empirically lands at 6-8s on macOS, longer than Telegram's ~5s
    typing-action TTL, so the indicator was flashing rather than holding
    steady. This loop fires every ``TYPING_ACTION_INTERVAL`` seconds so the
    cadence stays well under the TTL regardless of binding count.

    V1 (``busy_indicator_v2`` off) keeps the legacy pane-derived path in
    ``update_status_message``. The two paths are mutually exclusive — when V2
    is on, ``update_status_message`` skips the typing-action send.
    """
    if not config.busy_indicator_v2:
        logger.info(
            "Typing-action loop: V2 indicator disabled, deferring to status poller"
        )
        return
    logger.info("Typing-action loop started (interval: %ss)", TYPING_ACTION_INTERVAL)
    while True:
        try:
            bindings = list(session_manager.iter_thread_bindings())
            sends: list = []
            for user_id, thread_id, wid in bindings:
                run = busy_indicator.state((user_id, thread_id or 0, wid))
                if run not in (RunState.RUNNING, RunState.RUNNING_TOOL):
                    continue
                sends.append(_send_typing_action(bot, user_id, thread_id, wid))
            if sends:
                await asyncio.gather(*sends, return_exceptions=True)
        except Exception as e:
            logger.error("Typing-action loop error: %s", e)
        await asyncio.sleep(TYPING_ACTION_INTERVAL)


async def _send_typing_action(bot: Bot, user_id: int, thread_id: int, wid: str) -> None:
    """Best-effort typing-action send. Failures (rate limit, network) are
    logged at debug and swallowed — never let one route's failure abort the
    gather over all routes.
    """
    try:
        await bot.send_chat_action(
            chat_id=session_manager.resolve_chat_id(user_id, thread_id or None),
            action=ChatAction.TYPING,
            message_thread_id=thread_id or None,
        )
        logger.debug(
            "typing_action user=%d thread=%s window=%s sent",
            user_id,
            thread_id,
            wid,
        )
    except Exception as e:
        logger.debug(
            "typing_action user=%d thread=%s window=%s failed: %s",
            user_id,
            thread_id,
            wid,
            e,
        )
