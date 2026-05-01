"""Topic-first attention card.

One bold, audible message per ``(user_id, thread_id)`` route that says
"Claude is waiting for you". Drives an idle↔waiting state machine so the
notification fires exactly once per attention episode and silent edits keep
the card current without spamming the user.

State machine:

    IDLE ── notify_waiting ──▶ WAITING   (fresh ``topic_send`` — pushes a notification)
    WAITING ── notify_waiting (same fingerprint) ──▶ WAITING (no-op, silent)
    WAITING ── notify_waiting (new fingerprint) ──▶ WAITING (edit-only, silent)
    WAITING ── dismiss ──▶ IDLE          (edit card to acknowledged trailer)
    *       ── clear  ──▶ IDLE           (hard reset — used by topic teardown)

DM is *not* the primary surface. ``notify_waiting`` only emits a topic
message; emergency DM fallback is the responsibility of higher-level repair
code (Stage 3, ``topic_repair``).

Public surface:
  - ``notify_waiting(bot, user_id, thread_id, window_id, prompt_text, *, kind)``
  - ``dismiss(bot, user_id, thread_id)``
  - ``clear(user_id, thread_id)`` — synchronous teardown for ``cleanup``
  - ``is_waiting(user_id, thread_id)`` — for digest integration
  - ``is_attention_request(text)`` — heuristic shared with the queue worker
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Literal

from telegram import Bot

from ..session import session_manager
from .message_sender import (
    TopicSendOutcome,
    strip_sentinels,
    topic_edit,
    topic_send,
)

logger = logging.getLogger(__name__)


# Minimum seconds between two fresh attention-card sends for the same
# (user, thread). Within this window the same fingerprint is silently
# ignored and a different fingerprint becomes an edit, never a fresh send.
# Mitigates "thinking↔waiting flapping" pointed out in the plan §5.
ATTENTION_REPEAT_DWELL_SECONDS = 30

# Cap the prompt preview embedded in the card body so we don't ship a 4K wall.
PROMPT_PREVIEW_LIMIT = 600

# Ack trailer text on dismiss. Kept short: the user already saw the prompt.
DISMISS_TRAILER = "✅ Acknowledged — Claude is no longer waiting."


@dataclass
class AttentionState:
    """One per ``(user_id, thread_id)``: tracks the live attention card."""

    message_id: int
    window_id: str
    last_fingerprint: str
    state: Literal["idle", "waiting"]
    last_send_at: float
    kind: str


# Keyed by ``(user_id, thread_id_or_0)`` so DM-only routes (thread_id is None)
# still get a stable card slot.
_attention_state: dict[tuple[int, int], AttentionState] = {}


# ── Heuristic ──────────────────────────────────────────────────────────────


_ATTENTION_CUES: tuple[str, ...] = (
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
)


def is_attention_request(text: str) -> bool:
    """Heuristic: final assistant text that is probably waiting for user input.

    Lifted unchanged from the legacy ``_looks_like_attention_request`` in
    ``message_queue`` so both call sites share the same definition.
    """
    cleaned = strip_sentinels(text or "").strip()
    if not cleaned:
        return False
    lower = " ".join(cleaned.lower().split())
    if any(cue in lower for cue in _ATTENTION_CUES):
        return True
    # A direct final question is usually attention-worthy; avoid tiny greetings.
    return cleaned.endswith("?") and len(cleaned) > 80


# ── Internals ──────────────────────────────────────────────────────────────


def _key(user_id: int, thread_id: int | None) -> tuple[int, int]:
    return (user_id, thread_id or 0)


def _fingerprint(window_id: str, kind: str, prompt_text: str) -> str:
    body = f"{window_id}\0{kind}\0{prompt_text[:1000]}"
    return hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()


def _display_name(window_id: str) -> str:
    return session_manager.get_display_name(window_id) or window_id or "Claude"


def _topic_link(chat_id: int, thread_id: int | None) -> str | None:
    if thread_id is None:
        return None
    chat = str(chat_id)
    if not chat.startswith("-100"):
        return None
    return f"https://t.me/c/{chat[4:]}/{thread_id}"


def _render_card(
    *,
    window_id: str,
    chat_id: int,
    thread_id: int | None,
    kind: str,
    prompt_text: str,
) -> str:
    """Render the attention card body."""
    display = _display_name(window_id)
    preview = strip_sentinels(prompt_text or "").strip()
    if len(preview) > PROMPT_PREVIEW_LIMIT:
        preview = preview[: PROMPT_PREVIEW_LIMIT - 1].rstrip() + "…"
    if kind == "interactive_ui":
        header = f"🔔 Claude is waiting for input — {display}"
    else:
        header = f"🔔 Claude needs a decision — {display}"
    lines: list[str] = [header, "Tap to open the topic and respond."]
    link = _topic_link(chat_id, thread_id)
    if link:
        lines.append(link)
    if preview:
        lines.append("")
        lines.append(preview)
    return "\n".join(lines)


# ── Public API ─────────────────────────────────────────────────────────────


def is_waiting(user_id: int, thread_id: int | None) -> bool:
    """Return True if there is a live (waiting) attention card for this route."""
    state = _attention_state.get(_key(user_id, thread_id))
    return bool(state and state.state == "waiting")


async def notify_waiting(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    prompt_text: str,
    kind: Literal["interactive_ui", "assistant_text"] = "assistant_text",
) -> TopicSendOutcome:
    """Idle→waiting sends a fresh card; waiting→waiting edits in place.

    Returns the ``TopicSendOutcome`` of the underlying topic operation so the
    caller can route to repair (Stage 3) on ``TOPIC_NOT_FOUND``/
    ``TOPIC_CLOSED``. Same-fingerprint repeats inside the dwell window are
    silently treated as ``OK`` (no Telegram call made).
    """
    key = _key(user_id, thread_id)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    text = _render_card(
        window_id=window_id,
        chat_id=chat_id,
        thread_id=thread_id,
        kind=kind,
        prompt_text=prompt_text,
    )
    fingerprint = _fingerprint(window_id, kind, prompt_text)
    now = time.monotonic()
    existing = _attention_state.get(key)

    # WAITING + same fingerprint within dwell: silent no-op.
    if (
        existing is not None
        and existing.state == "waiting"
        and existing.window_id == window_id
        and existing.last_fingerprint == fingerprint
        and (now - existing.last_send_at) < ATTENTION_REPEAT_DWELL_SECONDS
    ):
        logger.debug(
            "attention noop user=%d thread=%s window=%s kind=%s fingerprint=%s",
            user_id,
            thread_id,
            window_id,
            kind,
            fingerprint,
        )
        return TopicSendOutcome.OK

    # WAITING (any fingerprint): edit the existing card silently.
    if (
        existing is not None
        and existing.state == "waiting"
        and existing.window_id == window_id
        and existing.message_id
    ):
        outcome = await topic_edit(
            bot,
            op="attention",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            message_id=existing.message_id,
            text=text,
        )
        if outcome is TopicSendOutcome.OK:
            existing.last_fingerprint = fingerprint
            existing.kind = kind
            return TopicSendOutcome.OK
        # Edit failed (message gone, topic shifted, etc.). Fall through to
        # send a fresh card below — that is closer to the user's intent than
        # silently dropping the update.
        logger.debug(
            "attention edit failed user=%d thread=%s window=%s outcome=%s — sending fresh card",
            user_id,
            thread_id,
            window_id,
            outcome.value,
        )
        _attention_state.pop(key, None)

    # IDLE → WAITING: send a fresh, audible card.
    sent, outcome = await topic_send(
        bot,
        op="attention",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=window_id,
        text=text,
        disable_notification=False,
    )
    if sent is None:
        # Don't mark waiting if the send failed — caller will route to repair
        # (Stage 3) and may retry, in which case we want to act like idle.
        return outcome

    _attention_state[key] = AttentionState(
        message_id=sent.message_id,
        window_id=window_id,
        last_fingerprint=fingerprint,
        state="waiting",
        last_send_at=now,
        kind=kind,
    )
    return TopicSendOutcome.OK


async def dismiss(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
) -> None:
    """Mark the attention card as acknowledged.

    Edits the card to the ack trailer and flips state back to idle. Safe to
    call when no card exists (no-op). Never DMs.
    """
    key = _key(user_id, thread_id)
    state = _attention_state.get(key)
    if state is None or state.state != "waiting":
        # Idle already — nothing to do. Keep state slot so subsequent
        # notify_waiting still hits the IDLE→WAITING branch cleanly.
        if state is not None:
            state.state = "idle"
        return

    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    if state.message_id:
        outcome = await topic_edit(
            bot,
            op="attention",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=state.window_id,
            message_id=state.message_id,
            text=DISMISS_TRAILER,
        )
        if outcome is not TopicSendOutcome.OK:
            logger.debug(
                "attention dismiss edit non-OK user=%d thread=%s window=%s outcome=%s",
                user_id,
                thread_id,
                state.window_id,
                outcome.value,
            )
    state.state = "idle"


def clear(user_id: int, thread_id: int | None) -> None:
    """Hard-clear attention state for a route (no Telegram I/O).

    Used by topic teardown (``cleanup.clear_topic_state``) and on session
    rotation so a fresh attention episode never inherits a stale fingerprint.
    """
    _attention_state.pop(_key(user_id, thread_id), None)


def reset_for_tests() -> None:
    """Test-only: drop all attention state."""
    _attention_state.clear()
