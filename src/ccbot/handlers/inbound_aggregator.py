"""Inbound aggregator — coalesce Telegram messages into one Claude turn (§2.8).

A single user intent often arrives as multiple Telegram updates: a media-group
of photos with one caption, a photo followed by descriptive text, a
caption-then-followup pair. Forwarding each update independently fragments
context across multiple Claude turns and (for media-groups) attaches the
caption to whichever photo arrived first, leaving the rest contextless.

This module buffers offers per route and flushes on a debounce window or on a
max-photo cap. The flushed string follows the §2.8.2 shape: the user's typed
text once, then a single ``(images attached: …)`` block with all paths in
arrival order. The caption is never repeated per image.

Public surface:
  - ``aggregator_offer_text(route, text)``
  - ``aggregator_offer_voice(route, transcribed_text)``
  - ``aggregator_offer_photo(route, path, caption, media_group_id)``
  - ``aggregator_flush_route(route)`` — public force-flush (slash-command path)
  - ``aggregator_clear_route(route)`` — teardown hook (cancels pending flush)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import config
from ..session import session_manager
from . import busy_indicator

logger = logging.getLogger(__name__)


Route = tuple[int, int, str]


@dataclass
class _PendingBundle:
    # ``media_group_id`` is intentionally NOT tracked here. Telegram delivers
    # all photos in a media-group within milliseconds of each other, so the
    # debounce window already coalesces them into one bundle without needing
    # to key by group id. Adding a coalesce key would also imply a
    # boundary-check ("different mg-id force-flushes the old bundle") that
    # the §2.8 spec doesn't require.
    text_parts: list[str] = field(default_factory=list)
    photo_paths: list[Path] = field(default_factory=list)
    flush_handle: asyncio.TimerHandle | None = None


# Per-route pending bundle. Mutation guarded by ``_route_locks[route]`` so the
# flush callback and the offer paths can't race the same bundle's photos /
# text-parts list.
_route_pending: dict[Route, _PendingBundle] = {}
_route_locks: dict[Route, asyncio.Lock] = {}


def _get_lock(route: Route) -> asyncio.Lock:
    lock = _route_locks.get(route)
    if lock is None:
        lock = asyncio.Lock()
        _route_locks[route] = lock
    return lock


def _get_or_create_bundle(route: Route) -> _PendingBundle:
    bundle = _route_pending.get(route)
    if bundle is None:
        bundle = _PendingBundle()
        _route_pending[route] = bundle
    return bundle


def _cancel_handle(bundle: _PendingBundle) -> None:
    handle = bundle.flush_handle
    bundle.flush_handle = None
    if handle is not None:
        handle.cancel()


def _schedule_flush(route: Route, bundle: _PendingBundle) -> None:
    """(Re)schedule the debounced flush for this route's bundle.

    Only ever invoked from inside ``async with lock`` in the offer paths,
    so the loop is guaranteed running and ``get_running_loop`` is safe.
    """
    _cancel_handle(bundle)
    loop = asyncio.get_running_loop()
    delay = max(0.0, config.aggregator_debounce_seconds)

    def _fire() -> None:
        # Schedule the flush coroutine on the running loop; the TimerHandle
        # callback itself runs sync.
        asyncio.create_task(_flush(route))

    bundle.flush_handle = loop.call_later(delay, _fire)


def _format_bundle(bundle: _PendingBundle) -> str:
    """Render the §2.8.2 output shape for a bundle."""
    text_block = "\n\n".join(part for part in bundle.text_parts if part)
    if bundle.photo_paths:
        path_lines = "\n".join(f"  - {path}" for path in bundle.photo_paths)
        attached_block = f"(images attached:\n{path_lines})"
    else:
        attached_block = ""

    if text_block and attached_block:
        return f"{text_block}\n\n{attached_block}"
    if text_block:
        return text_block
    return attached_block


async def _flush(route: Route) -> None:
    """Send the buffered bundle to the bound tmux window and clear it."""
    lock = _get_lock(route)
    async with lock:
        bundle = _route_pending.pop(route, None)
        if bundle is None:
            return
        _cancel_handle(bundle)
        text_to_send = _format_bundle(bundle)

    if not text_to_send:
        return

    window_id = route[2]
    try:
        success, message = await session_manager.send_to_window(window_id, text_to_send)
        if not success:
            logger.warning(
                "aggregator flush send_to_window failed for route %s: %s",
                route,
                message,
            )
            return
    except Exception as exc:
        logger.error(
            "aggregator flush raised for route %s: %s",
            route,
            exc,
        )
        return

    # Closes the gap between "prompt accepted" and "first transcript event":
    # the V2 typing loop only refreshes RUNNING / RUNNING_TOOL routes, so
    # without this mark the indicator was dark during preliminary work.
    if config.busy_indicator_v2:
        await busy_indicator.mark_inbound_sent(route)


async def aggregator_offer_text(route: Route, text: str) -> None:
    """Append a text part to the route's bundle and (re)schedule flush.

    The aggregator is intentionally independent of
    ``config.reply_context_enabled``. The kill switch only governs the
    quote→prompt rendering and the outbound ``reply_parameters`` anchor —
    bundling Telegram updates into one Claude turn is correct in both modes.
    """
    if not text:
        return
    lock = _get_lock(route)
    async with lock:
        bundle = _get_or_create_bundle(route)
        bundle.text_parts.append(text)
        _schedule_flush(route, bundle)


async def aggregator_offer_voice(route: Route, transcribed_text: str) -> None:
    """Voice transcripts ride the same path as text."""
    await aggregator_offer_text(route, transcribed_text)


async def aggregator_offer_photo(
    route: Route,
    path: Path,
    caption: str | None,
    media_group_id: str | None,
) -> None:
    """Append a photo (and any caption) to the route's bundle.

    When ``len(photo_paths)`` reaches the configured cap the bundle is
    force-flushed immediately rather than waiting on the debounce — keeps an
    unbounded media dump from sitting in memory.
    """
    # ``media_group_id`` is accepted for API parity with the Telegram
    # update payload but intentionally not stored — see _PendingBundle.
    del media_group_id
    lock = _get_lock(route)
    flush_now = False
    async with lock:
        bundle = _get_or_create_bundle(route)
        if caption:
            bundle.text_parts.append(caption)
        bundle.photo_paths.append(path)
        if len(bundle.photo_paths) >= config.aggregator_max_photos:
            flush_now = True
            _cancel_handle(bundle)
        else:
            _schedule_flush(route, bundle)

    if flush_now:
        await _flush(route)


async def aggregator_flush_route(route: Route) -> None:
    """Force-flush a route's bundle. Used by slash-command forwarders."""
    lock = _get_lock(route)
    async with lock:
        bundle = _route_pending.get(route)
        if bundle is None:
            return
        _cancel_handle(bundle)
    await _flush(route)


def aggregator_clear_route(route: Route) -> None:
    """Drop a route's bundle without sending. Called by ``teardown_route``.

    Pending flush handle is cancelled in-place so a debounce that hadn't yet
    fired can't try to send into a torn-down window.
    """
    bundle = _route_pending.pop(route, None)
    if bundle is not None:
        _cancel_handle(bundle)
    _route_locks.pop(route, None)


def has_pending(route: Route) -> bool:
    """Test helper / introspection: is there a bundle waiting to flush?"""
    bundle = _route_pending.get(route)
    return bundle is not None and (bool(bundle.text_parts) or bool(bundle.photo_paths))
