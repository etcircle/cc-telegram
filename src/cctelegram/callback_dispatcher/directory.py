"""Execute directory, session, and existing-window callback flows.

Core responsibilities:
  - Own CB_DIR_*, CB_SESSION_*, and CB_WIN_* callback execution.
  - Keep pending-topic picker revalidation next to picker mutations.
  - Transition between directory browser, session picker, and window picker UI.

Key components:
  - execute_directory_callback()
"""

from __future__ import annotations

from typing import Any

import logging
from pathlib import Path
from cctelegram.tmux_manager import LifecycleTimeout
from cctelegram.handlers.callback_data import (
    CB_DIR_BIND_EXISTING,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)
from cctelegram.handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    BROWSE_UNBOUND_COUNT_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    build_session_picker,
    build_window_picker,
    clear_browse_state,
    clear_session_picker_state,
    clear_window_picker_state,
    picker_entry,
)
from cctelegram.handlers.inbound_telegram import (
    _clear_pending_route_payload,
    _create_and_bind_window,
    _flush_pending_route_payload,
    _get_thread_id,
    _list_unbound_windows,
)
from cctelegram.handlers.message_sender import safe_edit

from . import safe_answer

from . import (
    _validate_pending_picker_callback,
    revalidate_before_mutation,
    window_lease,
)

logger = logging.getLogger(__name__)

# GH #66 part D: shown when a picker callback lands on a card whose in-memory
# state is gone (bot restart) or has moved on (stale scrollback button). Editing
# the dead card self-heals it — the next message opens a fresh picker.
PICKER_EXPIRED_TEXT = "This picker expired — send a message here to open a new one."


async def execute_directory_callback(authorized: Any, adapters: Any) -> None:
    update = authorized.ctx.update
    context = authorized.ctx.context
    user = authorized.ctx.user
    query = authorized.ctx.query
    data = authorized.command.data
    cb_thread_id = authorized.ctx.thread_id
    lease = window_lease(authorized, adapters)
    session_manager = adapters.session_manager
    tmux_manager = adapters.tmux_manager

    async def reject_stale_window_callback(window_id: str) -> bool:
        return await lease.reject_stale_window(window_id)

    def _entry() -> dict | None:
        """This callback's own thread picker entry (GH #66)."""
        return picker_entry(context.user_data, cb_thread_id)

    async def reject_invalid_pending_picker(
        expected_states: tuple[str, ...],
    ) -> tuple[bool, int | None]:
        ok, pending_tid, _reason = _validate_pending_picker_callback(
            context.user_data,
            cb_thread_id,
            expected_states,
        )
        if ok:
            return False, pending_tid
        # GH #66 part D: restart-orphan (entry gone) or stale scrollback button
        # (wrong state) — disable the dead card instead of a bare popup so the
        # next message opens a fresh picker.
        await safe_edit(query, PICKER_EXPIRED_TEXT, reply_markup=None)
        await safe_answer(query)
        return True, pending_tid

    # Directory browser handlers
    if data.startswith(CB_DIR_SELECT):
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,)
        )
        if stale:
            return
        entry = _entry()
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await safe_answer(query, "Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = entry.get(BROWSE_DIRS_KEY, []) if entry else []
        if idx < 0 or idx >= len(cached_dirs):
            await safe_answer(
                query, "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        default_path = str(Path.cwd())
        current_path = (
            entry.get(BROWSE_PATH_KEY, default_path) if entry else default_path
        )
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await safe_answer(query, "Directory not found", show_alert=True)
            return

        new_path_str = str(new_path)
        logger.info(
            "CB_DIR_SELECT: idx=%d name=%s current=%s -> new=%s (user=%d, thread=%s)",
            idx,
            subdir_name,
            current_path,
            new_path_str,
            user.id,
            pending_tid,
        )
        if entry is not None:
            entry[BROWSE_PATH_KEY] = new_path_str
            entry[BROWSE_PAGE_KEY] = 0

        unbound_count = entry.get(BROWSE_UNBOUND_COUNT_KEY, 0) if entry else 0
        msg_text, keyboard, subdirs = build_directory_browser(
            new_path_str, unbound_count=unbound_count
        )
        if entry is not None:
            entry[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await safe_answer(query)

    elif data == CB_DIR_UP:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,)
        )
        if stale:
            return
        entry = _entry()
        default_path = str(Path.cwd())
        current_path = (
            entry.get(BROWSE_PATH_KEY, default_path) if entry else default_path
        )
        current = Path(current_path).resolve()
        parent = current.parent
        # No restriction - allow navigating anywhere

        parent_path = str(parent)
        if entry is not None:
            entry[BROWSE_PATH_KEY] = parent_path
            entry[BROWSE_PAGE_KEY] = 0

        unbound_count = entry.get(BROWSE_UNBOUND_COUNT_KEY, 0) if entry else 0
        msg_text, keyboard, subdirs = build_directory_browser(
            parent_path, unbound_count=unbound_count
        )
        if entry is not None:
            entry[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await safe_answer(query)

    elif data.startswith(CB_DIR_PAGE):
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,)
        )
        if stale:
            return
        entry = _entry()
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await safe_answer(query, "Invalid data")
            return
        default_path = str(Path.cwd())
        current_path = (
            entry.get(BROWSE_PATH_KEY, default_path) if entry else default_path
        )
        if entry is not None:
            entry[BROWSE_PAGE_KEY] = pg

        unbound_count = entry.get(BROWSE_UNBOUND_COUNT_KEY, 0) if entry else 0
        msg_text, keyboard, subdirs = build_directory_browser(
            current_path, pg, unbound_count=unbound_count
        )
        if entry is not None:
            entry[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await safe_answer(query)

    elif data == CB_DIR_CONFIRM:
        stale, pending_thread_id = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,)
        )
        if stale:
            return
        entry = _entry()
        default_path = str(Path.cwd())
        selected_path = (
            entry.get(BROWSE_PATH_KEY, default_path) if entry else default_path
        )

        clear_browse_state(entry)

        # Check for existing sessions in this directory
        sessions = await session_manager.list_sessions_for_directory(selected_path)
        if not await revalidate_before_mutation(
            query,
            context,
            pending_thread_id,
            PICKER_EXPIRED_TEXT,
        ):
            return
        if sessions:
            # Show session picker — store state for later. Re-resolve the entry:
            # ``revalidate_before_mutation`` proved this thread still owns it.
            entry = _entry()
            if entry is not None:
                entry[STATE_KEY] = STATE_SELECTING_SESSION
                entry[SESSIONS_KEY] = sessions
                entry["_selected_path"] = selected_path
            text, keyboard = build_session_picker(sessions)
            await safe_edit(query, text, reply_markup=keyboard)
            await safe_answer(query)
            return

        # No existing sessions — create new window directly
        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_thread_id,
            tmux_mgr=adapters.tmux_manager,
            session_mgr=adapters.session_manager,
        )

    elif data == CB_DIR_CANCEL:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,)
        )
        if stale:
            return
        # Dropping the entry clears browse chrome + payload + files together.
        _clear_pending_route_payload(context.user_data, cb_thread_id, delete_files=True)
        await safe_edit(query, "Cancelled")
        await safe_answer(query, "Cancelled")

    # Session picker: resume existing session
    elif data.startswith(CB_SESSION_SELECT):
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_SESSION,)
        )
        if stale:
            return
        entry = _entry()
        try:
            idx = int(data[len(CB_SESSION_SELECT) :])
        except ValueError:
            await safe_answer(query, "Invalid data")
            return

        cached_sessions = entry.get(SESSIONS_KEY, []) if entry else []
        if idx < 0 or idx >= len(cached_sessions):
            await safe_answer(query, "Session not found")
            return

        session = cached_sessions[idx]
        selected_path = (
            entry.get("_selected_path", str(Path.cwd())) if entry else str(Path.cwd())
        )
        clear_session_picker_state(entry)
        if entry is not None:
            entry.pop("_selected_path", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            tmux_mgr=adapters.tmux_manager,
            session_mgr=adapters.session_manager,
            resume_session_id=session.session_id,
        )

    elif data == CB_SESSION_NEW:
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_SESSION,)
        )
        if stale:
            return
        entry = _entry()
        selected_path = (
            entry.get("_selected_path", str(Path.cwd())) if entry else str(Path.cwd())
        )
        clear_session_picker_state(entry)
        if entry is not None:
            entry.pop("_selected_path", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            tmux_mgr=adapters.tmux_manager,
            session_mgr=adapters.session_manager,
        )

    elif data == CB_SESSION_CANCEL:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_SESSION,)
        )
        if stale:
            return
        _clear_pending_route_payload(context.user_data, cb_thread_id, delete_files=True)
        await safe_edit(query, "Cancelled")
        await safe_answer(query, "Cancelled")

    # Window picker: bind existing window
    elif data.startswith(CB_WIN_BIND):
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_WINDOW,)
        )
        if stale:
            return
        entry = _entry()
        try:
            idx = int(data[len(CB_WIN_BIND) :])
        except ValueError:
            await safe_answer(query, "Invalid data")
            return

        cached_windows: list[str] = entry.get(UNBOUND_WINDOWS_KEY, []) if entry else []
        if idx < 0 or idx >= len(cached_windows):
            await safe_answer(
                query, "Window list changed, please retry", show_alert=True
            )
            return
        selected_wid = cached_windows[idx]

        # Verify window still exists
        w = await tmux_manager.find_window_by_id(selected_wid)
        if not w:
            display = session_manager.get_display_name(selected_wid)
            await safe_answer(
                query, f"Window '{display}' no longer exists", show_alert=True
            )
            return

        thread_id = _get_thread_id(update)
        if thread_id is None:
            await safe_answer(query, "Not in a topic", show_alert=True)
            return

        current_unbound_ids = {
            wid
            for wid, _, _ in await _list_unbound_windows(
                adapters.tmux_manager, adapters.session_manager
            )
        }
        if selected_wid not in current_unbound_ids:
            await safe_answer(
                query, "Window is no longer unbound, please retry", show_alert=True
            )
            return

        ok, _pending_tid, _reason = _validate_pending_picker_callback(
            context.user_data,
            cb_thread_id,
            (STATE_SELECTING_WINDOW,),
        )
        if not ok:
            # GH #66 part D: the entry vanished during the async re-checks above
            # (bot restart / cancel) — self-heal the dead card.
            await safe_edit(query, PICKER_EXPIRED_TEXT, reply_markup=None)
            await safe_answer(query)
            return

        # THE ADOPTION GATE (GH #65 review r10 P1-B). Binding a topic to an
        # EXISTING window is an adoption: a kill still in flight for that id
        # (its async wrapper cancelled, its libtmux worker thread very much
        # alive) would otherwise land on the window we just handed the user.
        # Refuse while the kill can still fire — a window nobody has adopted is
        # the only thing a straggler is allowed to kill.
        if not await tmux_manager.await_kill_settled(selected_wid):
            await safe_edit(
                query,
                "⚠️ That window is being closed right now. Please pick again in "
                "a moment.",
                reply_markup=None,
            )
            await safe_answer(query)
            return

        # EVERY PRECONDITION IS RE-VALIDATED AFTER THE WAIT (review r11 P1-B).
        # The checks above ran BEFORE a wait that can last seconds, and all of
        # them can go stale inside it: the window can die, another topic can
        # claim it, and the picker entry can be replaced. Binding the object we
        # resolved before the wait is binding a stale observation.
        #
        # REVALIDATE → COMMIT UNDER THE LIFECYCLE LOCK (review r12 P1-A), so no
        # kill can REGISTER between the last check and the bind. The lock is
        # INNERMOST: no Telegram I/O runs inside it, so each arm records a
        # refusal and the reply is sent AFTER the hold is released. The bind
        # itself is a synchronous dict write.
        refusal: str | None = None
        expired = False
        display = session_manager.get_display_name(selected_wid)
        try:
            async with tmux_manager.window_lifecycle_lock():
                # FRESH (review r12 P1-B): the 1 s listing cache can be a full
                # second behind a landed kill, which is exactly the corpse this
                # probe exists to catch. BOUNDED (review r13 P1-B): no tmux await
                # under this lock may be unbounded, or one wedged call freezes every
                # other window's lifecycle.
                w = await tmux_manager._bounded_lifecycle(
                    tmux_manager.find_window_by_id(selected_wid, fresh=True),
                    what="bind-to-existing existence probe",
                )
                if not w:
                    refusal = (
                        f"Window '{session_manager.get_display_name(selected_wid)}' "
                        "no longer exists"
                    )
                elif tmux_manager.window_kill_pending(selected_wid):
                    refusal = "That window is being closed right now, please retry"
                else:
                    current_unbound_ids = {
                        wid
                        for wid, _, _ in await tmux_manager._bounded_lifecycle(
                            _list_unbound_windows(
                                adapters.tmux_manager, adapters.session_manager
                            ),
                            what="bind-to-existing unbound listing",
                        )
                    }
                    if selected_wid not in current_unbound_ids:
                        refusal = "Window is no longer unbound, please retry"
                    else:
                        ok, _pending_tid, _reason = _validate_pending_picker_callback(
                            context.user_data,
                            cb_thread_id,
                            (STATE_SELECTING_WINDOW,),
                        )
                        if not ok:
                            expired = True
                        else:
                            display = w.window_name
                            clear_window_picker_state(_entry())
                            session_manager.bind_thread(
                                user.id, thread_id, selected_wid, window_name=display
                            )

        except LifecycleTimeout as e:
            logger.error("bind-to-existing exceeded its lifecycle bound: %s", e)
            await safe_answer(
                query,
                "That took too long — please check tmux and try again",
                show_alert=True,
            )
            return

        if expired:
            await safe_edit(query, PICKER_EXPIRED_TEXT, reply_markup=None)
            await safe_answer(query)
            return
        if refusal is not None:
            await safe_answer(query, refusal, show_alert=True)
            return

        # Replay pending text and/or attachments through the synchronous
        # aggregator helper so §2.8.2 formatting is preserved without
        # offer-path background/intermediate flushes hiding failures.
        route = (user.id, thread_id, selected_wid)
        pending_delivered = await _flush_pending_route_payload(route, context.user_data)
        if pending_delivered is not None and not pending_delivered.ok:
            # GH #50 §1.4: surface the REAL refusal reason — the fresh-session
            # folder-trust prompt is exactly this case.
            await safe_edit(
                query,
                f"✅ Bound to window `{display}`\n\n"
                "The first message was not delivered.\n\n"
                f"⚠️ {pending_delivered.message}\n\n"
                "The pending payload was cleared; please resend it here.",
            )
            await safe_answer(
                query, "Bound; first message not delivered", show_alert=True
            )
            return

        first_turn_note = (
            "\n\nFirst message sent."
            if pending_delivered is not None and pending_delivered.ok
            else ""
        )
        await safe_edit(
            query,
            f"✅ Bound to window `{display}`{first_turn_note}",
        )
        await safe_answer(query, "Bound")

    # Window picker: new session → transition to directory browser
    elif data == CB_WIN_NEW:
        stale, pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_WINDOW,)
        )
        if stale:
            return
        entry = _entry()
        # Preserve pending thread info, clear only picker chrome.
        clear_window_picker_state(entry)
        unbound_count = len(
            await _list_unbound_windows(adapters.tmux_manager, adapters.session_manager)
        )
        start_path = str(adapters.config.browse_root)
        msg_text, keyboard, subdirs = build_directory_browser(
            start_path, unbound_count=unbound_count
        )
        logger.info(
            "CB_WIN_NEW: opening directory browser at %s (subdirs=%d, user=%d, thread=%s)",
            start_path,
            len(subdirs),
            user.id,
            pending_tid,
        )
        if entry is not None:
            entry[STATE_KEY] = STATE_BROWSING_DIRECTORY
            entry[BROWSE_PATH_KEY] = start_path
            entry[BROWSE_PAGE_KEY] = 0
            entry[BROWSE_DIRS_KEY] = subdirs
            entry[BROWSE_UNBOUND_COUNT_KEY] = unbound_count
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await safe_answer(query)

    # Directory browser: opt-in pivot to window picker
    elif data == CB_DIR_BIND_EXISTING:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_BROWSING_DIRECTORY,)
        )
        if stale:
            return
        entry = _entry()
        unbound = await _list_unbound_windows(
            adapters.tmux_manager, adapters.session_manager
        )
        if not unbound:
            await safe_answer(query, "No unbound windows available", show_alert=True)
            return
        msg_text, keyboard, win_ids = build_window_picker(unbound)
        # Swap state from browse → picker. Keep pending thread/text/attachments
        # so the bind handler can flush them once a window is chosen.
        clear_browse_state(entry)
        if entry is not None:
            entry[STATE_KEY] = STATE_SELECTING_WINDOW
            entry[UNBOUND_WINDOWS_KEY] = win_ids
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await safe_answer(query)

    # Window picker: cancel
    elif data == CB_WIN_CANCEL:
        stale, _pending_tid = await reject_invalid_pending_picker(
            (STATE_SELECTING_WINDOW,)
        )
        if stale:
            return
        _clear_pending_route_payload(context.user_data, cb_thread_id, delete_files=True)
        await safe_edit(query, "Cancelled")
        await safe_answer(query, "Cancelled")
