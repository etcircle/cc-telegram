"""Transport-plumbing leaf: an AIORateLimiter that exempts sendChatAction from
the per-GROUP bucket.

Purpose: typing indicators (``sendChatAction``) are not messages, but
python-telegram-bot's rate limiter classifies every request purely on
``data["chat_id"]`` and routes a negative (forum) chat_id through the 20/60s
per-group bucket — the same budget real content messages use. That both broke
multi-topic typing cadence (N busy topics ⇒ N×3s > Telegram's ~5s typing TTL
⇒ the indicator blinks) and starved content sends behind pending typing
actions (live-verified 2026-07-08, pid 42762). ``TypingAwareRateLimiter``
presents a POSITIVE dummy chat_id to the classifier for ``sendChatAction`` so
the request skips the group bucket while KEEPING the overall 30/s limiter and
the RetryAfter machinery; every other endpoint delegates untouched.

PTB version-coupling pin: verified against the installed PTB 22.x
``telegram/ext/_aioratelimiter.py`` — ``process_request`` classification reads
only ``data["chat_id"]`` and IGNORES ``endpoint``; the real request body
travels as ``args=(endpoint, data)`` and reaches the transport via
``callback(*args, **kwargs)``, so ``data`` passed to the limiter is
classification metadata ONLY. ExtBot passes the SAME dict object as both
``data`` and ``args[1]``, so the exemption builds a fresh dict and never
mutates the shared object (the real negative chat_id must reach the request).
``test_rate_limiter.py`` pins this property against the installed PTB so a
future upgrade that starts using ``data`` for the actual request fails loudly.

Core components:
  * TypingAwareRateLimiter — the AIORateLimiter subclass; overrides only
    ``process_request`` to swap in a positive dummy chat_id for
    ``sendChatAction`` before delegating to super().
"""

from collections.abc import Callable, Coroutine
from typing import Any

from telegram._utils.types import JSONDict
from telegram.ext import AIORateLimiter

# The API method whose requests we exempt from the per-group message bucket.
_SEND_CHAT_ACTION = "sendChatAction"

# A POSITIVE dummy chat_id: PTB's classifier marks chat=True for any non-None
# chat_id (so the overall 30/s limiter + RetryAfter machinery stay), and
# group=False for a positive int (so the 20/60s per-group bucket is skipped).
_EXEMPT_CLASSIFIER_CHAT_ID = 1


class TypingAwareRateLimiter(AIORateLimiter):
    """AIORateLimiter that exempts sendChatAction from the per-GROUP bucket.

    Only ``process_request`` is overridden; for ``sendChatAction`` the
    classification ``data`` is replaced (not mutated) with a positive dummy
    chat_id so ``super().process_request`` skips the group limiter while the
    overall limiter + RetryAfter loop stay in force. The real request body,
    which travels through ``args``, is never touched.
    """

    async def process_request(
        self,
        callback: Callable[..., Coroutine[Any, Any, bool | JSONDict | list[JSONDict]]],
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: int | None,
    ) -> bool | JSONDict | list[JSONDict]:
        if endpoint == _SEND_CHAT_ACTION:
            # Fresh dict — ExtBot passes the SAME object as data and args[1],
            # so mutating it would strip the real chat_id from the request.
            data = {**data, "chat_id": _EXEMPT_CLASSIFIER_CHAT_ID}
        return await super().process_request(
            callback, args, kwargs, endpoint, data, rate_limit_args
        )
