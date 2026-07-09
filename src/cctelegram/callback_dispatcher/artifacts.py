"""Execute ``dlf:`` artifact-download callbacks (the 📎 tap-to-download lane).

A tap on a 📎 card's option button uploads the pinned local file to the topic
as a Telegram document. Guard order mirrors the ``aql:`` late-answer executor:

  registry lookup (None → graceful expired modal) → owner check → stale-window
  (payload/registry parity + lease + live-window existence) → ``begin_send``
  single-FLIGHT gate → ANSWER THE CALLBACK FIRST (an upload can exceed the
  callback-answer deadline) → ``open_validated_artifact`` (containment re-checked
  against the roots PINNED IN THE ROW at mint time + ``O_NOFOLLOW`` open + fstat
  on the fd) → ``send_document`` passing the OPEN FILE OBJECT → success /
  failure / RetryAfter branches → the fd is closed in a ``finally``.

The upload source is ALWAYS the validated fd — the pathname is never re-opened
(the TOCTOU contract). A download is single-FLIGHT (not single-use): a re-tap
re-uploads the current file content.

Key components: execute_download_file_callback().
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from telegram.error import RetryAfter

from cctelegram.handlers import artifacts
from cctelegram.handlers.callback_data import CB_DOWNLOAD_FILE
from cctelegram.handlers.message_sender import safe_send, send_document

from . import STALE_CALLBACK_TEXT, WRONG_USER_PICK_TEXT, safe_answer, window_lease

logger = logging.getLogger(__name__)

EXPIRED_ARTIFACT_TEXT = (
    "This download card has expired (bot restarted or superseded) — "
    "send /file <path> instead."
)
ALREADY_UPLOADING_TEXT = "Already uploading — hang on…"
RATE_LIMITED_TEXT = "Rate-limited — tap again shortly."


async def execute_download_file_callback(authorized: Any, adapters: Any) -> None:
    user = authorized.ctx.user
    query = authorized.ctx.query
    data = authorized.command.data
    lease = window_lease(authorized, adapters)
    tmux_manager = adapters.tmux_manager
    bot = adapters.bot

    if not data.startswith(CB_DOWNLOAD_FILE):
        return

    # 1. Parse dlf:<window_id>:<token> — window ids never contain ':'.
    parts = data[len(CB_DOWNLOAD_FILE) :].split(":")
    if len(parts) != 2 or not all(parts):
        await safe_answer(query, "Invalid data")
        return
    window_id, token = parts

    # 2. Registry lookup. None → post-restart / TTL / invalidated: the card
    # can't reconstruct the pinned path, so answer the graceful expired modal
    # (the card text still lists the paths for /file).
    row = artifacts.lookup(token)
    if row is None:
        await safe_answer(query, EXPIRED_ARTIFACT_TEXT, show_alert=True)
        return

    # 3. Owner check (the aql:/aqp: precedent) — BEFORE the lease check.
    if user.id != row.owner_id:
        await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
        return

    # 4. Stale window: payload/registry parity, the topic-ownership lease, then
    # live-window existence (the effort.py precedent — a gone window is stale).
    if window_id != row.window_id:
        await safe_answer(query, STALE_CALLBACK_TEXT, show_alert=True)
        return
    if await lease.reject_stale_window(window_id):
        return
    if not await tmux_manager.find_window_by_id(window_id):
        await safe_answer(query, STALE_CALLBACK_TEXT, show_alert=True)
        return

    # 5. Single-FLIGHT gate: live → in_flight (a concurrent tap lands here).
    if not artifacts.begin_send(token):
        await safe_answer(query, ALREADY_UPLOADING_TEXT, show_alert=False)
        return

    chat_id = query.message.chat_id
    thread_id = getattr(query.message, "message_thread_id", None)
    max_bytes = adapters.config.artifact_max_bytes
    name = row.display_name

    # 6. ANSWER THE CALLBACK FIRST — an upload can exceed the callback-answer
    # deadline (Telegram rejects a late answer_callback_query).
    await safe_answer(query, f"Uploading {name}…")

    # 7. Open through the validated-fd contract (containment re-checked against
    # the PINNED roots + O_NOFOLLOW open + fstat on the fd). The fd IS the
    # upload source — the pathname is never re-opened.
    opened = artifacts.open_validated_artifact(
        row.resolved_path, row.pinned_roots, max_bytes
    )
    if opened.file is None:
        artifacts.finish_send(token, False)
        await safe_send(
            bot,
            chat_id,
            f"❌ Upload failed: {opened.reason}",
            message_thread_id=thread_id,
        )
        return

    try:
        try:
            ok, reason = await send_document(
                bot,
                chat_id,
                opened.file,
                filename=Path(row.resolved_path).name,
                message_thread_id=thread_id,
            )
        except RetryAfter:
            # send_document re-raises RetryAfter; the download lane handles it
            # gracefully (a re-tap re-uploads — single-FLIGHT). The in-topic
            # notice is BEST-EFFORT: safe_send itself re-raises RetryAfter, and
            # this lane must never depend on PTB's error handler (fold item 3 —
            # codex P3-1).
            artifacts.finish_send(token, False)
            try:
                await safe_send(
                    bot, chat_id, RATE_LIMITED_TEXT, message_thread_id=thread_id
                )
            except RetryAfter as notice_exc:
                logger.warning(
                    "artifact rate-limit notice itself rate-limited "
                    "(chat=%s thread=%s): %s",
                    chat_id,
                    thread_id,
                    notice_exc,
                )
            return
    finally:
        try:
            opened.file.close()
        except Exception:  # pragma: no cover — defensive close
            pass

    if ok:
        artifacts.finish_send(token, True)
    else:
        artifacts.finish_send(token, False)
        await safe_send(
            bot, chat_id, f"❌ Upload failed: {reason}", message_thread_id=thread_id
        )
