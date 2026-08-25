"""Unified cleanup API for topic state.

Provides centralized cleanup functions that coordinate state cleanup across
all modules, preventing memory leaks when topics are deleted.

Functions:
  - clear_topic_state: Clean up all memory state for a specific topic
"""

import logging
from pathlib import Path
from typing import Any

from telegram import Bot
from telegram.error import TelegramError

from .. import md_capture, route_runtime
from ..session import session_id_for_window, session_manager
from ..tmux_manager import tmux_manager

from . import (
    artifacts,
    attention,
    auq_source,
    late_answer,
    notify_source,
    pane_signals,
    pick_intent,
    trust_flow,
    usage_cache,
)
from .dashboard import clear_dashboards_in_thread
from .directory_browser import (
    CARD_CHAT_ID_KEY,
    CARD_MSG_ID_KEY,
    picker_entries,
)
from .inbound_aggregator import aggregator_clear_route
from .interactive_ui import clear_interactive_msg
from .message_queue import (
    clear_status_msg_info,
    clear_tool_msg_ids_for_topic,
    routes_for_topic,
    teardown_route,
)


logger = logging.getLogger(__name__)


def _delete_pending_attachment_files(attachments: list[Any]) -> None:
    """Best-effort deletion for pending-route attachment objects."""
    for attachment in attachments:
        path = getattr(attachment, "path", None)
        if path is None and isinstance(attachment, dict):
            path = attachment.get("path")
        if not isinstance(path, (str, Path)):
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            logger.debug("failed to delete pending attachment %s: %s", path, e)


async def _disable_picker_card(bot: Bot | None, entry: dict[str, Any]) -> None:
    """Edit a thread's recorded picker card to an inert self-heal message.

    GH #66 part D: a torn-down entry (topic close / /start) leaves its Telegram
    card looking live until tapped; disabling it here removes the stale keyboard.
    Best-effort — a failed edit never blocks teardown.
    """
    card_chat_id = entry.get(CARD_CHAT_ID_KEY)
    card_msg_id = entry.get(CARD_MSG_ID_KEY)
    if bot is None or not card_chat_id or not card_msg_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=card_chat_id,
            message_id=card_msg_id,
            text="Picker closed — send a message here to reopen.",
            reply_markup=None,
        )
    except TelegramError as e:
        logger.debug("failed to disable picker card: %s", e)


async def disable_all_picker_cards(
    bot: Bot | None, user_data: dict[str, Any] | None
) -> None:
    """Disable every recorded picker card for this user (a /start global reset).

    GH #66 (Codex Q5): ``clear_all_picker_entries`` drops the entries but their
    cards would otherwise stay live-looking until each is tapped. Call this BEFORE
    the clear so the coordinates are still available.
    """
    for entry in picker_entries(user_data):
        await _disable_picker_card(bot, entry)


async def teardown_all_creation_flows(
    user_id: int,
    bot: Bot | None = None,
    user_data: dict[str, Any] | None = None,
) -> None:
    """GH #65 Fix 6: tear down EVERY topic's creation flow for one user.

    The ``/start`` global reset runs this BEFORE ``clear_all_picker_entries`` so
    each flow's window is settled (guarded cleanup) while its entry — the
    ownership token — is still present. A flow whose COMPLETION WON inside the
    teardown window is never abandoned half-bound (its retained tail is
    awaited); the now-BOUND topic then goes through the normal bound-topic
    teardown, per Fix 6's protocol (review r1 P2-3).
    """
    for thread_id in await trust_flow.teardown_all_for_user(
        user_id, session_mgr=session_manager
    ):
        logger.info(
            "creation flow completed during /start teardown — running the "
            "bound-topic teardown (user=%d, thread=%d)",
            user_id,
            thread_id,
        )
        await clear_topic_state(user_id, thread_id, bot, user_data)


async def clear_topic_state(
    user_id: int,
    thread_id: int,
    bot: Bot | None = None,
    user_data: dict[str, Any] | None = None,
    *,
    drop_pending: bool = True,
) -> None:
    """Clear all memory state associated with a topic.

    Callers must pass ``drop_pending`` per §2.1.2:
      - Topic close + window still alive → ``drop_pending=False`` (pending
        content may still be useful when topic reopens).
      - Topic delete + window killed → ``drop_pending=True``.
      - Stale-binding GC (window killed externally) → ``drop_pending=True``.
    """
    # Cancel any background ``!``-command pane-capture task for this topic
    # (review finding 20). Without this, a ≤30s capture loop keeps posting the
    # OLD window's output into a rebound topic after close/delete/stale-binding
    # GC — and /unbind, which also routes through clear_topic_state. Lazy
    # import: inbound_telegram pulls in the heavy handler stack and this module
    # is imported early by status_polling (mirrors the message_queue→cleanup
    # lazy-import precedent).
    from .inbound_telegram import _cancel_bash_capture

    _cancel_bash_capture(user_id, thread_id)

    # GH #65 Fix 6: tear down a live creation-flow (folder-trust) WAIT task for
    # this topic FIRST — the window must be settled before the entry (the
    # ownership token) is dropped below. The teardown's own lock choreography
    # releases the creation lock before it cancels/awaits, so calling it here
    # (holding nothing) cannot deadlock.
    await trust_flow.teardown_thread(
        user_id,
        thread_id,
        bot=bot,
        user_data=user_data,
        reason="topic teardown",
        session_mgr=session_manager,
    )

    # Tear down any per-route queue first so its in-flight task can record
    # _tool_msg_ids before we sweep them below.
    for route in routes_for_topic(user_id, thread_id):
        # §2.8: drop any pending aggregator bundle for this route so a
        # debounced flush can't fire into a torn-down window after the
        # queue is gone.
        aggregator_clear_route(route)
        # Bug 2: tear down the MessageDisplay live-prose capture for this route's
        # window. Resolved from the route's window_id via window_states (NOT
        # thread_bindings — callers unbind the thread BEFORE clear_topic_state,
        # so the binding is already gone; the route + window_state survive until
        # teardown_route below). The session_monitor deleted-window seam
        # backstops any queue-less window not in routes_for_topic.
        _md_session = session_id_for_window(route[2])
        if _md_session:
            md_capture.teardown_session(_md_session)
            # Wave B: topic close also tears down the route's notification
            # side file (same session resolution as the md_capture teardown).
            notify_source.unlink_for_session(_md_session)
        # D2: tomb this window's durable pick mint-intents on topic close (the
        # store is window-keyed; route[2] is the window_id). Orphan-safety is also
        # provided by recovery-time re-validation + the 24h GC, but tombing here
        # keeps the store hygienic.
        pick_intent.teardown_window(route[2])
        # Drop ONLY the per-window AUQ side-file freshness floor on topic close /
        # window-delete (window-keyed; route[2] is the window_id). Floor-only — NOT
        # forget_for_window, which would ALSO unlink the session-keyed side file and
        # strand a double-`--resume` sibling's live AUQ. Harmless to leak by
        # correctness (max()-only-widens), cleared here for hygiene.
        auq_source.clear_side_file_freshness(route[2])
        # P1: a closed topic's window keeps no post-/exit quarantine — a later
        # window reusing the id must not inherit it (kill_window also pops it;
        # this covers the close-without-kill path).
        tmux_manager.clear_window_quarantine(route[2], reason="topic teardown")
        # GH #50 peer-review P1: the stranded-draft brake is DELIBERATELY NOT
        # cleared here. Unlike everything else in this loop it is a property of
        # the PANE'S CONTENTS, not of the topic binding — and this function does
        # NOT imply the window is dead: ``/unbind`` routes through here and
        # EXPLICITLY leaves the window running. Worse, this runs with no
        # synchronization against ``window_send_lock``, so clearing the brake
        # here let a send that had been BLOCKED on that lock the whole time (an
        # already-popped boundary flush, a slash command) acquire it, find a
        # structurally valid input box that still held the previous payload's
        # unsent draft, append its own text and press Enter — committing BOTH,
        # including the one the user was told was NOT delivered. The brake is
        # released only on positive empty-box proof, or by tmux itself on a
        # CONFIRMED kill_window / create_window (see the brake-lifecycle comment
        # in ``tmux_manager``). Topic close / /kill DO kill the window, so the
        # brake is dropped there — at the kill, under the right proof.
        await teardown_route(route, drop_pending=drop_pending)

    # Clear status message tracking
    clear_status_msg_info(user_id, thread_id)

    # Clear tool message ID tracking
    clear_tool_msg_ids_for_topic(user_id, thread_id)

    # Clear interactive UI state (also deletes message from chat)
    await clear_interactive_msg(user_id, bot, thread_id, session_mgr=session_manager)

    # Drop any live attention card state — fresh topic gets a fresh episode.
    attention.clear(user_id, thread_id)

    # Wave C: a dashboard hosted in this thread dies with the topic (the host
    # topic may have no bound window, so binding-centric cleanup alone would
    # miss it — pre-C fix 3). Thread ids are CHAT-LOCAL, so the clear is
    # scoped to this topic's chat via the persisted group_chat_ids mapping
    # (review P2-3); an unresolvable chat falls back to the all-chats sweep
    # with a warning inside clear_dashboards_in_thread. The user re-runs
    # /dashboard elsewhere.
    clear_dashboards_in_thread(
        thread_id, chat_id=session_manager.get_group_chat_id(user_id, thread_id)
    )

    # Tear down ALL route_runtime state for this topic — run-state, open_tools,
    # context_usage, and pane_interactive_pending. ``teardown_route`` above only
    # reaches routes present in ``message_queue._route_queues`` (queued routes);
    # a route can carry route_runtime state with NO queue worker (mark_inbound_sent
    # / JSONL replay / status_polling.mark_interactive_pending operate directly on
    # the route key), so deriving runtime ownership from _route_queues would strand
    # e.g. a pane-set WAITING_ON_USER after the topic is closed / the window is gone
    # (hermes round-2 P2). route_runtime owns its own topic-teardown seam.
    route_runtime.clear_routes_for_topic(user_id, thread_id or 0)
    # GH #43: drop the topic's pane-derived background-job records beside
    # the route_runtime teardown (same ownership rationale — pull-only
    # leaf state keyed by route must not survive the topic).
    pane_signals.clear_routes_for_topic(user_id, thread_id or 0)
    # /cost overlay cache: drop the topic's cached usage overlays beside the
    # route_runtime teardown (same ownership rationale — pull-only leaf state
    # keyed by route must not survive the topic).
    usage_cache.clear_routes_for_topic(user_id, thread_id or 0)
    # Wave A lifecycle seam (c): drop the topic's aql: late-answer cards
    # beside the route_runtime teardown — topic-keyed, NOT via the queued-
    # routes loop above, so a queue-less route's card dies with the topic
    # too (the same _route_queues gap that gave route_runtime its own seam).
    late_answer.invalidate_topic(user_id, thread_id or 0)
    # Artifact delivery lane: drop the topic's 📎 download cards (+ offer-dedup)
    # here — the COVERING seam (topic close/delete + the status-poller
    # window-gone path all route through clear_topic_state); topic-keyed for the
    # same queue-less-route reason as late_answer above.
    artifacts.invalidate_topic(user_id, thread_id or 0)

    # Pop the status poller's route-local caches for this topic (gate P3-1) —
    # a rebound topic reusing the same route key must not inherit a stale
    # ``_last_published_ui_hash`` (skips the first-picker content-drain
    # barrier), ``_prev_run_state`` (defeats seed-without-edit repaint
    # semantics), ``_last_pane_capture`` (delays the first watchdog scrape),
    # or ``_absent_streak``. Lazy import: status_polling imports this module
    # at the top, so the reverse edge must stay function-local (same
    # precedent as the inbound_telegram import above).
    from .status_polling import clear_route_caches_for_topic

    clear_route_caches_for_topic(user_id, thread_id or 0)

    # Clear this thread's pending picker entry from user_data (GH #66: per-thread
    # keyed, so closing this topic never touches another topic's in-flight entry).
    # GH #65 wave 3 (r3 P1-2): the drop goes through the LOCKED trust_flow seam —
    # never a direct map pop — so the entry clear, its identity-token
    # invalidation and any live flow's terminal mark are ONE critical section.
    if user_data is not None:
        entry = await trust_flow.clear_topic_entry(user_id, thread_id, user_data)
        if entry is not None:
            if drop_pending:
                _delete_pending_attachment_files(
                    list(entry.get("_pending_thread_attachments") or [])
                )
            # GH #66 part D: disable the orphaned picker card so a stale button
            # tapped after the topic closed edits to a self-heal message.
            await _disable_picker_card(bot, entry)
