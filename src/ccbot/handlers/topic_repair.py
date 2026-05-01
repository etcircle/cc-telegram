"""Topic repair pipeline (Stage 3 scaffold — disabled by default).

When ``topic_send``/``topic_edit`` returns ``TOPIC_NOT_FOUND`` or
``TOPIC_CLOSED`` the user's topic is broken. The repair pipeline tries to
recover the topic itself before falling back to an emergency DM:

  1. ``TOPIC_CLOSED`` → ``bot.reopen_forum_topic`` (per-thread throttle).
  2. ``TOPIC_NOT_FOUND`` → ``bot.create_forum_topic`` with a "Rescue: …"
     name, rebind the binding, retry the send (per-window throttle).
  3. Both fail → ``EMERGENCY_DM``.

Status: scaffolded, not wired into the topic_* wrappers. Enable with the
env var ``CCBOT_TOPIC_REPAIR=1`` once the implementation is hardened and
tests cover throttling + permission failure modes. The plan in
``docs/plans/2026-05-01-topic-first-attention-notifications.md`` §3.3 has
the full spec.

Until that flag flips, the rest of the system continues to use the
existing emergency-DM path in ``message_queue._emergency_dm``.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass

from telegram import Bot

from .message_sender import TopicSendOutcome

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """Return whether the topic repair pipeline is enabled.

    Default is OFF: the topic recreation path requires manage_topics admin
    in the supergroup, and a wrong call could spam Rescue: topics. Flip
    explicitly via environment after smoke-testing.
    """
    return os.environ.get("CCBOT_TOPIC_REPAIR", "0").lower() in ("1", "true", "yes")


REOPEN_THROTTLE_SECONDS = 5 * 60  # one reopen attempt / topic / 5 min
RESCUE_THROTTLE_SECONDS = 15 * 60  # one rescue-create / window / 15 min


class RepairAction(enum.Enum):
    """What the caller should do after try_repair returns."""

    RETRY_SAME_THREAD = "RETRY_SAME_THREAD"
    RETRY_NEW_THREAD = "RETRY_NEW_THREAD"
    EMERGENCY_DM = "EMERGENCY_DM"
    NOOP = "NOOP"


@dataclass
class RepairResult:
    """Outcome of a single ``try_repair`` call."""

    action: RepairAction
    new_thread_id: int | None = None
    reason: str = ""


# Per-(user, thread) reopen throttle and per-(user, window) rescue throttle.
# Kept process-local; reset on restart, which is fine — repair is best-effort.
_last_reopen_at: dict[tuple[int, int], float] = {}
_last_rescue_at: dict[tuple[int, str], float] = {}

# Async locks per window, so concurrent failures (queue worker + status probe)
# don't both try to recreate the same topic.
_repair_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _lock_for(user_id: int, window_id: str) -> asyncio.Lock:
    key = (user_id, window_id)
    lock = _repair_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _repair_locks[key] = lock
    return lock


async def try_repair(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int,
    window_id: str,
    outcome: TopicSendOutcome,
) -> RepairResult:
    """Attempt to repair a broken topic.

    SCAFFOLD ONLY. Until ``CCBOT_TOPIC_REPAIR`` is enabled this returns
    ``EMERGENCY_DM`` so the caller falls through to the existing DM path
    without any reopen/create side effects.
    """
    if not is_enabled():
        return RepairResult(
            action=RepairAction.EMERGENCY_DM,
            reason=f"topic_repair disabled (outcome={outcome.value})",
        )

    if outcome not in (TopicSendOutcome.TOPIC_NOT_FOUND, TopicSendOutcome.TOPIC_CLOSED):
        return RepairResult(
            action=RepairAction.NOOP, reason=f"non-broken outcome {outcome.value}"
        )

    # Real implementation goes here. See plan §3.3:
    #   - TOPIC_CLOSED → bot.reopen_forum_topic + RETRY_SAME_THREAD
    #   - TOPIC_NOT_FOUND → bot.create_forum_topic + rebind + RETRY_NEW_THREAD
    #   - Throttle via _last_reopen_at / _last_rescue_at, lock via _lock_for.
    # Leaving as TODO until the smoke-test path on a Telegram supergroup with
    # manage_topics admin is run. Returning EMERGENCY_DM keeps current
    # behaviour identical to today.
    logger.warning(
        "topic_repair stub invoked user=%d thread=%d window=%s outcome=%s "
        "— returning EMERGENCY_DM (real reopen/create not yet implemented)",
        user_id,
        thread_id,
        window_id,
        outcome.value,
    )
    return RepairResult(
        action=RepairAction.EMERGENCY_DM,
        reason=f"topic_repair scaffold (outcome={outcome.value})",
    )


def reset_for_tests() -> None:
    """Test-only: clear all throttles and locks."""
    _last_reopen_at.clear()
    _last_rescue_at.clear()
    _repair_locks.clear()


def _record_reopen(user_id: int, thread_id: int) -> None:
    """Internal helper for the future implementation."""
    _last_reopen_at[(user_id, thread_id)] = time.monotonic()


def _record_rescue(user_id: int, window_id: str) -> None:
    """Internal helper for the future implementation."""
    _last_rescue_at[(user_id, window_id)] = time.monotonic()
