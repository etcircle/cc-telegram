"""Interactive UI handling for Claude Code prompts.

Handles interactive terminal UIs displayed by Claude Code:
  - AskUserQuestion: Multi-choice question prompts
  - ExitPlanMode: Plan mode exit confirmation
  - Permission Prompt: Tool permission requests
  - RestoreCheckpoint: Checkpoint restoration selection

Provides:
  - Keyboard navigation (up/down/left/right/enter/esc)
  - Terminal capture and display
  - Interactive mode tracking per user and thread

State dicts are keyed by (user_id, thread_id_or_0) for Telegram topic support.
"""

import hashlib
import logging
import time

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from ..session import session_manager
from ..terminal_parser import extract_interactive_content, is_interactive_ui
from ..tmux_manager import tmux_manager
from . import attention
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from .message_sender import (
    NO_LINK_PREVIEW,
    TopicSendOutcome,
    topic_delete,
    topic_edit,
    topic_send,
)

logger = logging.getLogger(__name__)

# Tool names that trigger interactive UI via JSONL (terminal capture + inline keyboard)
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

# Track interactive UI message IDs: (user_id, thread_id_or_0) -> message_id
_interactive_msgs: dict[tuple[int, int], int] = {}

# Track interactive mode: (user_id, thread_id_or_0) -> window_id
_interactive_mode: dict[tuple[int, int], str] = {}

# Track direct-message notifications for interactive prompts. Telegram does not
# reliably push notifications for edited topic/status messages, so prompts also
# get a DM, but cooldown prevents spam while Claude redraws/updates the same UI.
_interactive_dm_fingerprints: dict[tuple[int, int], str] = {}
_interactive_dm_last_sent: dict[tuple[int, int], float] = {}
INTERACTIVE_DM_COOLDOWN_SECONDS = 60


def get_interactive_window(user_id: int, thread_id: int | None = None) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_id or 0))


def set_interactive_mode(
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Set interactive mode for a user."""
    logger.debug(
        "Set interactive mode: user=%d, window_id=%s, thread=%s",
        user_id,
        window_id,
        thread_id,
    )
    _interactive_mode[(user_id, thread_id or 0)] = window_id


def clear_interactive_mode(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive mode for a user (without deleting message)."""
    logger.debug("Clear interactive mode: user=%d, thread=%s", user_id, thread_id)
    _interactive_mode.pop((user_id, thread_id or 0), None)


def get_interactive_msg_id(user_id: int, thread_id: int | None = None) -> int | None:
    """Get the interactive message ID for a user."""
    return _interactive_msgs.get((user_id, thread_id or 0))


def _topic_link(chat_id: int, thread_id: int | None) -> str | None:
    """Build a best-effort Telegram private supergroup topic link."""
    if thread_id is None:
        return None
    chat = str(chat_id)
    if not chat.startswith("-100"):
        return None
    return f"https://t.me/c/{chat[4:]}/{thread_id}"


async def _notify_waiting_dm(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    prompt_text: str,
) -> None:
    """Emergency-only DM fallback when the topic-first attention card fails.

    The normal path is ``attention.notify_waiting`` (in-topic card). This DM
    is reached only when the topic itself cannot be written to (deleted,
    closed, forbidden) so the user still gets a signal that Claude is blocked.
    """
    ikey = (user_id, thread_id or 0)
    fingerprint = hashlib.sha1(
        f"{window_id}\0{thread_id or 0}\0{prompt_text}".encode("utf-8", "replace")
    ).hexdigest()
    now = time.monotonic()
    last = _interactive_dm_last_sent.get(ikey)
    if _interactive_dm_fingerprints.get(ikey) == fingerprint:
        return
    if last is not None and now - last < INTERACTIVE_DM_COOLDOWN_SECONDS:
        logger.debug(
            "Skipping interactive waiting DM due cooldown user=%d thread=%s window=%s",
            user_id,
            thread_id,
            window_id,
        )
        _interactive_dm_fingerprints[ikey] = fingerprint
        return
    _interactive_dm_fingerprints[ikey] = fingerprint
    _interactive_dm_last_sent[ikey] = now

    display = session_manager.get_display_name(window_id) or window_id
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    link = _topic_link(chat_id, thread_id)
    message = f"🔔 Claude is waiting for input in {display}"
    if link:
        message += f"\n{link}"
    try:
        await bot.send_message(
            chat_id=user_id,
            text=message,
            link_preview_options=NO_LINK_PREVIEW,
        )
        logger.info(
            "Interactive waiting DM sent to user=%d thread=%s window=%s",
            user_id,
            thread_id,
            window_id,
        )
    except Exception as e:
        # Non-fatal: the in-topic interactive UI still exists. This commonly
        # fails if the user has not opened a DM with the bot.
        logger.debug("Failed to send interactive waiting DM to %d: %s", user_id, e)


def _build_interactive_keyboard(
    window_id: str,
    ui_name: str = "",
) -> InlineKeyboardMarkup:
    """Build keyboard for interactive UI navigation.

    ``ui_name`` controls the layout: ``RestoreCheckpoint`` omits ←/→ keys
    since only vertical selection is needed.
    """
    vertical_only = ui_name == "RestoreCheckpoint"

    rows: list[list[InlineKeyboardButton]] = []
    # Row 1: directional keys
    rows.append(
        [
            InlineKeyboardButton(
                "␣ Space", callback_data=f"{CB_ASK_SPACE}{window_id}"[:64]
            ),
            InlineKeyboardButton("↑", callback_data=f"{CB_ASK_UP}{window_id}"[:64]),
            InlineKeyboardButton(
                "⇥ Tab", callback_data=f"{CB_ASK_TAB}{window_id}"[:64]
            ),
        ]
    )
    if vertical_only:
        rows.append(
            [
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "←", callback_data=f"{CB_ASK_LEFT}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "→", callback_data=f"{CB_ASK_RIGHT}{window_id}"[:64]
                ),
            ]
        )
    # Row 2: action keys
    rows.append(
        [
            InlineKeyboardButton(
                "⎋ Esc", callback_data=f"{CB_ASK_ESC}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "🔄", callback_data=f"{CB_ASK_REFRESH}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "⏎ Enter", callback_data=f"{CB_ASK_ENTER}{window_id}"[:64]
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> bool:
    """Capture terminal and send interactive UI content to user.

    Handles AskUserQuestion, ExitPlanMode, Permission Prompt, and
    RestoreCheckpoint UIs. Returns True if UI was detected and sent,
    False otherwise.
    """
    ikey = (user_id, thread_id or 0)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return False

    # Capture plain text (no ANSI colors)
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        logger.debug("No pane text captured for window_id %s", window_id)
        return False

    # Quick check if it looks like an interactive UI
    if not is_interactive_ui(pane_text):
        logger.debug(
            "No interactive UI detected in window_id %s (last 3 lines: %s)",
            window_id,
            pane_text.strip().split("\n")[-3:],
        )
        return False

    # Extract content between separators
    content = extract_interactive_content(pane_text)
    if not content:
        return False

    # Build message with navigation keyboard
    keyboard = _build_interactive_keyboard(window_id, ui_name=content.name)

    # Send as plain text (no markdown conversion)
    text = content.content

    # Check if we have an existing interactive message to edit
    existing_msg_id = _interactive_msgs.get(ikey)
    if existing_msg_id:
        try:
            edit_outcome = await topic_edit(
                bot,
                op="interactive",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                window_id=window_id,
                message_id=existing_msg_id,
                text=text,
                plain=True,
                reply_markup=keyboard,
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                _interactive_mode[ikey] = window_id
                await attention.notify_waiting(
                    bot,
                    user_id=user_id,
                    thread_id=thread_id,
                    window_id=window_id,
                    prompt_text=text,
                    kind="interactive_ui",
                )
                return True
            edit_outcome = TopicSendOutcome.OTHER
            logger.debug(
                "Edit failed for interactive msg %s: %s, sending new",
                existing_msg_id,
                e,
            )
        if edit_outcome is TopicSendOutcome.OK:
            _interactive_mode[ikey] = window_id
            await attention.notify_waiting(
                bot,
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                prompt_text=text,
                kind="interactive_ui",
            )
            return True
        # Edit failed — fall through to fresh send while keeping the old id
        # so we can delete it after a new one lands.

    # Send new message (plain text — terminal content is not markdown)
    logger.info(
        "Sending interactive UI to user %d for window_id %s", user_id, window_id
    )
    sent, send_outcome = await topic_send(
        bot,
        op="interactive",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=window_id,
        text=text,
        plain=True,
        reply_markup=keyboard,
    )
    if sent is None:
        # Topic send failed — still mark interactive mode (prevents per-poll
        # retry spam) and try the topic-first attention card. If that also
        # cannot reach the topic, emergency-fall back to a direct DM.
        _interactive_mode[ikey] = window_id
        outcome = await attention.notify_waiting(
            bot,
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            prompt_text=text,
            kind="interactive_ui",
        )
        if outcome is not TopicSendOutcome.OK and send_outcome in (
            TopicSendOutcome.TOPIC_NOT_FOUND,
            TopicSendOutcome.TOPIC_CLOSED,
            TopicSendOutcome.FORBIDDEN,
        ):
            await _notify_waiting_dm(bot, user_id, window_id, thread_id, text)
        return False
    _interactive_msgs[ikey] = sent.message_id
    _interactive_mode[ikey] = window_id
    await attention.notify_waiting(
        bot,
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        prompt_text=text,
        kind="interactive_ui",
    )
    # New message sent successfully — now safe to delete the old one
    if existing_msg_id:
        await topic_delete(
            bot,
            op="interactive",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            message_id=existing_msg_id,
        )
    return True


async def clear_interactive_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
) -> None:
    """Clear tracked interactive message, delete from chat, and exit interactive mode."""
    ikey = (user_id, thread_id or 0)
    msg_id = _interactive_msgs.pop(ikey, None)
    _interactive_mode.pop(ikey, None)
    _interactive_dm_fingerprints.pop(ikey, None)
    logger.debug(
        "Clear interactive msg: user=%d, thread=%s, msg_id=%s",
        user_id,
        thread_id,
        msg_id,
    )
    if bot and msg_id:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        await topic_delete(
            bot,
            op="interactive",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=None,
            message_id=msg_id,
        )
    if bot:
        await attention.dismiss(bot, user_id=user_id, thread_id=thread_id)
