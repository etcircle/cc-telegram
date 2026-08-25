"""Directory browser and window picker UI for session creation.

Provides UIs in Telegram for:
  - Window picker: list unbound tmux windows for quick binding
  - Directory browser: navigate directory hierarchies to create new sessions

Key components:
  - DIRS_PER_PAGE: Number of directories shown per page
  - User state keys for tracking browse/picker session
  - build_window_picker: Build unbound window picker UI
  - build_directory_browser: Build directory browser UI
  - clear_window_picker_state: Clear picker state from user_data
  - clear_browse_state: Clear browsing state from user_data
"""

import os
import secrets
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..session import ClaudeSession

from ..config import config
from .callback_data import (
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

# Directories per page in directory browser
DIRS_PER_PAGE = 6

# User state keys
STATE_KEY = "state"
STATE_BROWSING_DIRECTORY = "browsing_directory"
STATE_SELECTING_WINDOW = "selecting_window"
BROWSE_PATH_KEY = "browse_path"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"  # Cache of subdirs for current path
BROWSE_UNBOUND_COUNT_KEY = (
    "browse_unbound_count"  # Count of unbound tmux windows (for opt-in bind button)
)
UNBOUND_WINDOWS_KEY = "unbound_windows"  # Cache of (name, cwd) tuples
STATE_SELECTING_SESSION = "selecting_session"
SESSIONS_KEY = "cached_sessions"  # Cache of ClaudeSession list

# GH #66: picker state is keyed per (user, thread), not per user. PTB
# ``user_data`` is one dict per user shared across every topic, so a single
# flat set of picker keys let a picker opened in topic B steal topic A's slot
# and orphan A's card. ``_PENDING_PICKERS_KEY`` holds a per-thread child map
# ``{thread_id: entry}``; every picker key above (STATE_KEY, BROWSE_*,
# UNBOUND_WINDOWS_KEY, SESSIONS_KEY) plus the pending payload keys owned by
# ``inbound_telegram`` live inside THIS thread's entry. The thread id is the
# map key, so there is no ``_pending_thread_id`` sub-key — presence of an entry
# IS ownership.
_PENDING_PICKERS_KEY = "_pending_pickers"

# GH #66 (part D): a picker card's Telegram coordinates, stored in the thread's
# entry so a teardown of that entry (topic close) can disable the orphaned card.
CARD_CHAT_ID_KEY = "_card_chat_id"
CARD_MSG_ID_KEY = "_card_msg_id"

# GH #65 (review r2 P1-A): every picker entry carries an EXPLICIT identity token,
# minted once at entry CREATION. "An entry exists for this thread" is not
# identity — a teardown that clears the entry and an inbound that immediately
# creates a REPLACEMENT are indistinguishable to a presence check, so a creation
# callback that started before the clear could install its flow onto the fresh
# entry (an ABA hijack) or resurrect an unreachable one. A long-lived actor
# therefore captures this token up front and re-validates it before it claims;
# dropping the entry destroys the token with it, so any later claim fails closed.
ENTRY_TOKEN_KEY = "_entry_token"


def picker_entry(user_data: dict | None, thread_id: int | None) -> dict | None:
    """Return this thread's picker entry, or None when absent."""
    if user_data is None or thread_id is None:
        return None
    pickers = user_data.get(_PENDING_PICKERS_KEY)
    if not isinstance(pickers, dict):
        return None
    entry = pickers.get(thread_id)
    return entry if isinstance(entry, dict) else None


def ensure_picker_entry(user_data: dict | None, thread_id: int | None) -> dict | None:
    """Return this thread's picker entry, creating it (and the parent map) if absent.

    ``setdefault`` semantics at BOTH levels (Codex Q2): a sibling thread's entry
    is never replaced, so initializing one topic's picker cannot wipe another's.
    Returns None when there is no ``user_data`` or thread to key on.
    """
    if user_data is None or thread_id is None:
        return None
    pickers = user_data.get(_PENDING_PICKERS_KEY)
    if not isinstance(pickers, dict):
        pickers = {}
        user_data[_PENDING_PICKERS_KEY] = pickers
    entry = pickers.get(thread_id)
    if not isinstance(entry, dict):
        # A NEW entry is a NEW identity (review r2 P1-A). Minted here, once, so
        # every creator gets one without having to remember to.
        entry = {ENTRY_TOKEN_KEY: secrets.token_hex(8)}
        pickers[thread_id] = entry
    return entry


def entry_token(user_data: dict | None, thread_id: int | None) -> str | None:
    """This thread's picker-entry identity token, or None when there is none."""
    entry = picker_entry(user_data, thread_id)
    if entry is None:
        return None
    token = entry.get(ENTRY_TOKEN_KEY)
    return token if isinstance(token, str) and token else None


def drop_picker_entry(user_data: dict | None, thread_id: int | None) -> dict | None:
    """Pop and return this thread's picker entry (only this thread's; None if absent)."""
    if user_data is None or thread_id is None:
        return None
    pickers = user_data.get(_PENDING_PICKERS_KEY)
    if not isinstance(pickers, dict):
        return None
    entry = pickers.pop(thread_id, None)
    if not pickers:
        # Keep ``user_data`` tidy once the last thread's picker is gone.
        user_data.pop(_PENDING_PICKERS_KEY, None)
    return entry if isinstance(entry, dict) else None


def picker_entries(user_data: dict | None) -> list[dict]:
    """Return every thread's picker entry for this user (order-preserving)."""
    if user_data is None:
        return []
    pickers = user_data.get(_PENDING_PICKERS_KEY)
    if not isinstance(pickers, dict):
        return []
    return [entry for entry in pickers.values() if isinstance(entry, dict)]


def clear_all_picker_entries(user_data: dict | None) -> None:
    """Drop EVERY thread's picker entry for this user (a global /start reset)."""
    if user_data is not None:
        user_data.pop(_PENDING_PICKERS_KEY, None)


def clear_browse_state(entry: dict | None) -> None:
    """Clear directory browsing chrome keys from a thread's picker entry.

    Clears only the picker chrome (state + browse caches); the pending payload
    keys (text / attachments) are left intact so a browse→picker transition
    keeps them.
    """
    if entry is not None:
        entry.pop(STATE_KEY, None)
        entry.pop(BROWSE_PATH_KEY, None)
        entry.pop(BROWSE_PAGE_KEY, None)
        entry.pop(BROWSE_DIRS_KEY, None)
        entry.pop(BROWSE_UNBOUND_COUNT_KEY, None)


def clear_window_picker_state(entry: dict | None) -> None:
    """Clear window picker chrome keys from a thread's picker entry."""
    if entry is not None:
        entry.pop(STATE_KEY, None)
        entry.pop(UNBOUND_WINDOWS_KEY, None)


def clear_session_picker_state(entry: dict | None) -> None:
    """Clear session picker chrome keys from a thread's picker entry."""
    if entry is not None:
        entry.pop(STATE_KEY, None)
        entry.pop(SESSIONS_KEY, None)


def build_window_picker(
    windows: list[tuple[str, str, str]],
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build window picker UI for unbound tmux windows.

    Args:
        windows: List of (window_id, window_name, cwd) tuples.

    Returns: (text, keyboard, window_ids) where window_ids is the ordered list for caching.
    """
    window_ids = [wid for wid, _, _ in windows]

    lines = [
        "*Bind to Existing Window*\n",
        "These windows are running but not bound to any topic.",
        "Pick one to attach it here, or start a new session.\n",
    ]
    for _wid, name, cwd in windows:
        display_cwd = cwd.replace(str(Path.home()), "~")
        lines.append(f"• `{name}` — {display_cwd}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(windows), 2):
        row = []
        for j in range(min(2, len(windows) - i)):
            name = windows[i + j][1]
            display = name[:12] + "…" if len(name) > 13 else name
            row.append(
                InlineKeyboardButton(
                    f"🖥 {display}", callback_data=f"{CB_WIN_BIND}{i + j}"
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton("➕ New Session", callback_data=CB_WIN_NEW),
            InlineKeyboardButton("Cancel", callback_data=CB_WIN_CANCEL),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons), window_ids


def build_directory_browser(
    current_path: str, page: int = 0, unbound_count: int = 0
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI.

    Args:
        current_path: Directory currently being shown.
        page: 0-indexed page within ``current_path``'s subdir list.
        unbound_count: Number of unbound tmux windows. When > 0, an
            opt-in "Bind existing window" button row is added so the
            user can pivot to the window picker instead of creating a
            new session.

    Returns: (text, keyboard, subdirs) where subdirs is the full list for caching.
    """
    path = Path(current_path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        path = Path.cwd()

    try:
        entries = list(path.iterdir())
    except (PermissionError, OSError):
        entries = []
    # Check each entry individually: on Windows mounts (/mnt/c via WSL DrvFs)
    # unreadable system files (pagefile.sys, swapfile.sys, DumpStack.log.tmp)
    # make is_dir() raise, which must not wipe out the whole listing
    # (ported from etcircle/cctelegram PR #1).
    names = []
    for d in entries:
        if not (config.show_hidden_dirs or not d.name.startswith(".")):
            continue
        try:
            if d.is_dir():
                names.append(d.name)
        except (PermissionError, OSError):
            continue
    subdirs = sorted(names)

    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start : start + DIRS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_dirs), 2):
        row = []
        for j, name in enumerate(page_dirs[i : i + 2]):
            display = name[:12] + "…" if len(name) > 13 else name
            # Use global index (start + i + j) to avoid long dir names in callback_data
            idx = start + i + j
            row.append(
                InlineKeyboardButton(
                    f"📁 {display}", callback_data=f"{CB_DIR_SELECT}{idx}"
                )
            )
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_DIR_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_DIR_PAGE}{page + 1}")
            )
        buttons.append(nav)

    action_row: list[InlineKeyboardButton] = []
    # Allow going up unless at filesystem root
    if path != path.parent:
        action_row.append(InlineKeyboardButton("..", callback_data=CB_DIR_UP))
    action_row.append(InlineKeyboardButton("Select", callback_data=CB_DIR_CONFIRM))
    action_row.append(InlineKeyboardButton("Cancel", callback_data=CB_DIR_CANCEL))
    buttons.append(action_row)

    # Opt-in bind-existing row (only when unbound tmux windows exist).
    # Lets the user pivot to the window picker instead of creating a new
    # session in the current directory. Inverse transition (picker → browser)
    # already exists via CB_WIN_NEW.
    if unbound_count > 0:
        label = (
            f"🖥 Bind existing window ({unbound_count})"
            if unbound_count > 1
            else "🖥 Bind existing window"
        )
        buttons.append(
            [InlineKeyboardButton(label, callback_data=CB_DIR_BIND_EXISTING)]
        )

    display_path = str(path).replace(str(Path.home()), "~")
    if not subdirs:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\n_(No subdirectories)_"
    else:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\nTap a folder to enter, or select current directory"

    return text, InlineKeyboardMarkup(buttons), subdirs


def _relative_time(file_path: str) -> str:
    """Format file mtime as a human-readable relative time string."""
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return ""
    delta = int(time.time() - mtime)
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


def build_session_picker(
    sessions: list[ClaudeSession],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build session picker UI for resuming an existing Claude session.

    Args:
        sessions: List of ClaudeSession objects (sorted by recency).

    Returns: (text, keyboard).
    """
    lines = [
        "*Resume Session?*\n",
        "Existing sessions found in this directory.\n",
    ]
    for i, s in enumerate(sessions):
        summary = s.summary[:40] + "…" if len(s.summary) > 40 else s.summary
        rel = _relative_time(s.file_path)
        time_str = f" ({rel})" if rel else ""
        lines.append(f"{i + 1}. {summary} — {s.message_count} msgs{time_str}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(sessions), 2):
        row = []
        for j in range(min(2, len(sessions) - i)):
            s = sessions[i + j]
            label = s.summary[:14] + "…" if len(s.summary) > 14 else s.summary
            row.append(
                InlineKeyboardButton(
                    f"▶ {label}", callback_data=f"{CB_SESSION_SELECT}{i + j}"
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton("➕ New Session", callback_data=CB_SESSION_NEW),
            InlineKeyboardButton("Cancel", callback_data=CB_SESSION_CANCEL),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons)
