"""Inbound aggregator — coalesce Telegram messages into one Claude turn (§2.8).

A single user intent often arrives as multiple Telegram updates: a media-group
of photos with one caption, a photo followed by descriptive text, a
caption-then-followup pair. Forwarding each update independently fragments
context across multiple Claude turns and (for media-groups) attaches the
caption to whichever photo arrived first, leaving the rest contextless.

This module buffers offers per route and flushes on a debounce window or on a
max-attachment cap. The flushed string follows the §2.8.2 shape: the user's
typed text once, then a single ``(attachments: …)`` block with all paths in
arrival order. The caption is never repeated per attachment.

GH #50: every flush goes through the GATED ``session_manager.deliver_to_window``
and returns a structured ``delivery.DeliveryResult`` (outcome + reason + copy),
not a bare bool — so a refusal (a live blocking prompt, a non-Claude pane, a
lone-digit payload, a withheld Enter) reaches the topic with its ACTUAL reason.
The debounced flush is fire-and-forget and the photo/document handlers already
acked "sent", so ``_report_delivery_refusal`` is the only thing standing between
the user and a silently-dropped message. The user-turn stamp moved INSIDE the
delivery transaction (``delivery.UserTurnStamp``) so a refusal is never stamped.

REFUSAL OWNERSHIP is explicit and single (``report_refusal``). A FIRE-AND-FORGET
flush (the debounce timer, the media-group boundary, the attachment cap) reports
INSIDE the aggregator — nobody is awaiting its result. A SYNCHRONOUS caller that
inspects the returned ``DeliveryResult`` and posts its own message — the three
forced-flush callers (``bot.forward_command_handler``, ``callback_dispatcher/
effort``, ``callback_dispatcher/late_answer``) and the pending-bind replay —
passes ``report_refusal=False`` and owns the single response. Otherwise one
refusal produced TWO ❌ messages. No path drops a refusal silently.

Public surface:
  - ``aggregator_offer_text(route, text)``
  - ``aggregator_offer_voice(route, transcribed_text)``
  - ``aggregator_offer_photo(route, path, caption, media_group_id)``
  - ``aggregator_offer_document(route, path, caption, media_group_id)``
  - ``aggregator_replay_payload(route, text, attachments)`` — sync replay
  - ``aggregator_flush_route(route, *, report_refusal=True)`` — public
    force-flush → ``DeliveryResult``
  - ``aggregator_clear_route(route)`` — teardown hook (cancels pending flush)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import delivery
from ..config import config
from ..delivery import DeliveryResult, UserTurnStamp
from ..session import session_manager
from .message_sender import safe_send

logger = logging.getLogger(__name__)


Route = tuple[int, int, str]


@dataclass(frozen=True)
class Provenance:
    """The composable provenance FACTS of one offered item (plan §2.3).

    ``_PendingBundle`` flattens typed prose, a voice transcription, a caption and
    a reply-context-rendered quote into indistinguishable ``text_parts``, so
    "is this bundle pure user prose?" is NOT recoverable from the rendered string
    — it must be OBSERVED at the offer site and carried. [r3 P1-2]

    Facts, never a ``kind``: a single bundle can be several things at once (a
    voice note plus a later typed line), and the eligibility rule is a boolean
    expression over the facts, not a taxonomy.
    """

    typed_text: bool = False
    voice: bool = False
    caption: bool = False
    reply_context: bool = False
    attachment: bool = False

    def merge(self, other: Provenance) -> Provenance:
        """OR-compose across every merge into a bundle (plan §2.3)."""
        return Provenance(
            typed_text=self.typed_text or other.typed_text,
            voice=self.voice or other.voice,
            caption=self.caption or other.caption,
            reply_context=self.reply_context or other.reply_context,
            attachment=self.attachment or other.attachment,
        )

    @property
    def free_text_eligible(self) -> bool:
        """Typed prose OR voice, AND none of caption / attachment.

        Voice IS eligible — it is the user speaking, and answering a card by
        voice is the flow PR-2 exists for.

        **REPLY-CONTEXT IS ELIGIBLE (owner decision, 2026-07-12 — supersedes plan
        §2.3, which excluded it).** The owner's dominant gesture at a card is a
        VOICE NOTE sent as a REPLY to it, so the as-planned rule refused their most
        natural way of answering — precisely the friction this lane exists to
        remove. Claude receives the FULL rendered payload, quote and all, exactly
        as the bot renders it for an ordinary send: the quote is CONTEXT for the
        answer, not a competing intent, and the affordance row is rig-proven to
        take 5 k+ chars of multi-line text and commit it whole. The FACT is still
        observed and carried (``_apply_reply_context`` returns whether it rendered
        a quote) — only its effect on eligibility changed, so the decision is one
        line and reversible.

        Captions and attachments stay INELIGIBLE: an ``(attachments: …)`` block is
        a message ABOUT files, not an answer to the question.

        Slash commands never reach here: ``forward_command_handler`` force-flushes
        the bundle and then sends the command through ``send_to_window``
        directly, so a command payload can never ride this lane.
        """
        return (self.typed_text or self.voice) and not (self.caption or self.attachment)


@dataclass
class _PendingBundle:
    text_parts: list[str] = field(default_factory=list)
    attachment_paths: list[Path] = field(default_factory=list)
    flush_handle: asyncio.TimerHandle | None = None
    # OR-composed across every offer merged into this bundle. A bundle created
    # AFTER a media-group boundary / cap flush starts EMPTY and takes its facts
    # from the NEW item only — it must never inherit the popped bundle's (plan
    # §2.3 [r4 P2-1]).
    provenance: Provenance = field(default_factory=Provenance)
    # Track the current media-group so a transition to a different group's
    # first attachment can force-flush the bundle in progress. Telegram
    # delivers media-group items within milliseconds, but two distinct
    # groups arriving inside the same debounce window must NOT merge — that
    # would attach group-2's caption to group-1's images.
    current_media_group_id: str | None = None
    # Caption dedup: Telegram repeats the same caption on every item of a
    # media-group when the user sets it on the album, so without dedup we
    # emit the caption N times.
    seen_captions: set[str] = field(default_factory=set)
    # The offering handler's bot (P1 quarantine surfacing): the debounced
    # flush outlives the handler, so a quarantine-refused delivery needs a
    # captured bot to post the in-topic "NOT delivered" notice. None (older
    # callers / replay) degrades to log-only.
    bot: Any | None = None


@dataclass(frozen=True)
class AggregatorReplayAttachment:
    """Attachment metadata for deterministic pending first-turn replay."""

    path: Path
    caption: str | None = None
    media_group_id: str | None = None
    # Whether a reply-context quote was rendered into ``caption`` (plan §2.3
    # [r4 P2-1]: pending-bind replay must PRESERVE the provenance facts, so the
    # pending store carries them rather than guessing at replay time).
    has_reply_context: bool = False


# Per-route pending bundle. Mutation guarded by ``_route_locks[route]`` so the
# flush callback and the offer paths can't race the same bundle's attachments
# / text-parts list.
_route_pending: dict[Route, _PendingBundle] = {}
_route_locks: dict[Route, asyncio.Lock] = {}

# Strong refs for fire-and-forget tasks. Without this, the GC can collect a
# task before it completes (cpython#91887) — most likely under load, exactly
# when boundary force-flushes fire.
_background_tasks: set[asyncio.Task[object]] = set()


def _spawn_background(coro: Coroutine[object, object, object]) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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
        _spawn_background(_flush(route))

    bundle.flush_handle = loop.call_later(delay, _fire)


def _format_bundle(bundle: _PendingBundle) -> str:
    """Render the §2.8.2 output shape for a bundle."""
    text_block = "\n\n".join(part for part in bundle.text_parts if part)
    if bundle.attachment_paths:
        path_lines = "\n".join(f"  - {path}" for path in bundle.attachment_paths)
        attached_block = f"(attachments:\n{path_lines})"
    else:
        attached_block = ""

    if text_block and attached_block:
        return f"{text_block}\n\n{attached_block}"
    if text_block:
        return text_block
    return attached_block


def _pop_bundle_locked(route: Route) -> _PendingBundle | None:
    """Pop and disarm the route's pending bundle.

    Caller is responsible for holding ``_get_lock(route)``. Split out from
    ``_flush`` so the public ``aggregator_flush_route`` can pop and send
    without re-entering the lock — ``asyncio.Lock`` is non-reentrant, and
    while the previous "lock → release → call _flush which re-locks" shape
    didn't formally deadlock, it left a window where another offer path
    could race in and rebuild the bundle between the cancel and the send.
    """
    bundle = _route_pending.pop(route, None)
    if bundle is not None:
        _cancel_handle(bundle)
    return bundle


async def _report_delivery_refusal(
    route: Route, bundle: _PendingBundle, result: DeliveryResult
) -> None:
    """Best-effort in-topic disclosure of a REFUSED delivery (GH #50 §1.4).

    Generalized from the quarantine-only notice: the debounced flush is
    fire-and-forget (the Telegram handler returned long ago, and the photo /
    document handlers have already acked "sent"), so ANY refusal — a live
    blocking prompt, a non-Claude pane, a lone-digit payload, a withheld Enter —
    must reach the topic with its ACTUAL, actionable reason. A refused payload
    is DROPPED with the notice, never auto-replayed.

    Reuses the normal in-topic send path (``safe_send``) with the bundle's
    captured bot; the fail-closed lookups (no captured bot / unresolvable
    chat) degrade to the caller's log line — never a crash, never a DM
    fallback.
    """
    if bundle.bot is None:
        return
    chat_id = session_manager.get_group_chat_id(route[0], route[1])
    if chat_id is None:
        return
    try:
        await safe_send(
            bundle.bot,
            chat_id,
            f"❌ {result.message}",
            message_thread_id=route[1] or None,
        )
    except Exception:
        logger.exception("delivery refusal notice failed for route %s", route)


async def _try_free_text_answer(
    route: Route, text: str, stamp: UserTurnStamp
) -> DeliveryResult | None:
    """Try to answer a live card with ``text``. ``None`` ⇒ the lane declines.

    Gated FIRST on the cheap, in-memory, route-keyed
    ``interactive_ui.has_interactive_surface`` so the ordinary send path (no card
    up) pays NOTHING — no extra capture, no lock churn. A False there simply
    means "no card is published for this route", so the payload takes the normal
    gated path; if a prompt IS live but uncarded (a restart before the poller
    re-renders), PR-1 refuses it correctly and the poller posts the card within
    ~1s.

    ``free_text.try_answer`` then does the real work under the window send lock:
    a fresh in-lock version-license re-read, the strict surface parse, the nav,
    the SGR-2 landing + typed-state proofs, and the Enter.
    """
    from . import free_text, interactive_ui

    if not free_text.enabled():
        return None
    if not interactive_ui.has_interactive_surface(route[0], route[1]):
        return None
    return await free_text.try_answer(
        route[2],
        text,
        user_turn=stamp,
        display=session_manager.get_display_name(route[2]),
    )


async def _send_bundle(
    route: Route, bundle: _PendingBundle, *, report_refusal: bool = True
) -> DeliveryResult:
    """Render and send a popped bundle. Caller must NOT hold the route lock.

    Returns the STRUCTURED delivery result (outcome + reason + copy) so the
    split replay and the pending-bind replay can surface the real reason instead
    of a bare ``False`` (plan §2.3 / r4 P2-2).

    ``report_refusal`` names the OWNER of the user-facing refusal message, so a
    refusal reaches the user EXACTLY ONCE. ``True`` (the default) is the
    FIRE-AND-FORGET flush — the debounce timer, the media-group boundary flush,
    the attachment-cap flush: nobody is awaiting the result, the photo/document
    handlers already acked "sent", so the aggregator itself must disclose. A
    SYNCHRONOUS caller that INSPECTS the returned result and posts its own
    message (``bot.forward_command_handler``, ``callback_dispatcher/effort``,
    ``callback_dispatcher/late_answer``, and the pending-bind replay's callers)
    passes ``False`` and owns the single response — otherwise the user gets TWO
    ❌ messages for one event.

    THE INVARIANT (peer-review P2): **every refusal — whether the transaction
    RETURNED a refused ``DeliveryResult`` or RAISED — reaches the user exactly
    once, on every flush path.** The raise arm used to build its result and
    ``return`` it immediately, jumping over the reporting block below; on a
    FIRE-AND-FORGET flush nobody awaits that result, so the popped payload
    vanished with only a log line and the user was never told — the precise
    opposite of the double-report ``report_refusal`` was introduced to fix, and
    it now matters more, because a raise PAST a write attempt also arms the
    stranded-draft brake (``session._WriteAttempt``): the user must be told why
    their NEXT message will be refused too. So the raise arm assigns ``result``
    and FALLS THROUGH to the one reporting seam. ``CancelledError`` is a
    ``BaseException`` — it is not caught, and must never be reported as an
    ordinary refusal.
    """
    text_to_send = _format_bundle(bundle)
    if not text_to_send:
        return delivery.delivered("empty bundle")

    window_id = route[2]
    # GH #50 §1.5: the user-turn stamp moved INSIDE the delivery transaction —
    # it now fires after every gate passes and immediately before the Enter, so
    # a REFUSED send is never stamped (the pre-send stamp here used to stamp
    # every refusal). Timing is preserved: the boundary still precedes any prose
    # this turn streams, so a fast prose→AUQ turn can't finalize its prose
    # before the stamp lands. PR-2's Enter carries the SAME stamp (plan §2.4
    # [r5 P1-1]) — a free-text answer IS a user turn.
    stamp = UserTurnStamp(user_id=route[0], thread_id=route[1], window_id=window_id)
    try:
        # GH #50 PR-2 — the FREE-TEXT lane (plan §2.4 "Integration seam"). This
        # is the ONLY place that knows the bundle's PROVENANCE, which is why the
        # lane is invoked here and not in ``send_to_window`` (provenance is
        # flattened by then) or ``text_handler`` (the debounce makes any
        # offer-time check TOCTOU).
        #
        # ``try_free_text_answer`` returns ``None`` when the lane does not apply
        # — no live licensed card on the pane — and the payload falls through to
        # the normal GATED delivery, which refuses on any other live prompt
        # (PR-1) or delivers into the input box. So the lane can only ever ADD a
        # successful answer; it can never make a deliverable message undeliverable.
        result = None
        if bundle.provenance.free_text_eligible:
            result = await _try_free_text_answer(route, text_to_send, stamp)
        if result is None:
            result = await session_manager.deliver_to_window(
                window_id, text_to_send, user_turn=stamp
            )
    except Exception as exc:
        logger.error(
            "aggregator flush raised for route %s: %s",
            route,
            exc,
        )
        # NOT a bare return — fall through to the single reporting seam below.
        # The NEUTRAL written-state copy is the right disclosure here: the raise
        # may have landed before OR after the payload was typed, and if it landed
        # after, the brake is now up and the user needs to know to clear the box.
        result = delivery.refuse(delivery.REASON_SEND_FAILED, written=False)

    if not result.ok:
        logger.warning(
            "aggregator flush delivery refused for route %s: reason=%s outcome=%s "
            "reported_here=%s",
            route,
            result.reason,
            result.outcome.value,
            report_refusal,
        )
        if report_refusal:
            await _report_delivery_refusal(route, bundle, result)
        return result

    # Closes the gap between "prompt accepted" and "first transcript event":
    # the typing loop only refreshes RUNNING / RUNNING_TOOL routes, so
    # without this mark the indicator was dark during preliminary work.
    from .. import route_runtime

    await route_runtime.mark_inbound_sent(route)
    return result


async def _flush(route: Route, *, report_refusal: bool = True) -> DeliveryResult:
    """Send the buffered bundle to the bound tmux window and clear it."""
    async with _get_lock(route):
        bundle = _pop_bundle_locked(route)

    if bundle is None:
        return delivery.delivered("nothing pending")

    return await _send_bundle(route, bundle, report_refusal=report_refusal)


async def _offer_text_part(
    route: Route,
    text: str,
    *,
    bot: Any | None,
    provenance: Provenance,
) -> None:
    if not text:
        return
    lock = _get_lock(route)
    async with lock:
        bundle = _get_or_create_bundle(route)
        bundle.text_parts.append(text)
        bundle.bot = bot or bundle.bot
        bundle.provenance = bundle.provenance.merge(provenance)
        _schedule_flush(route, bundle)


async def aggregator_offer_text(
    route: Route,
    text: str,
    *,
    bot: Any | None = None,
    has_reply_context: bool = False,
) -> None:
    """Append a text part to the route's bundle and (re)schedule flush.

    The aggregator is intentionally independent of
    ``config.reply_context_enabled``. The kill switch only governs the
    quote→prompt rendering and the outbound ``reply_parameters`` anchor —
    bundling Telegram updates into one Claude turn is correct in both modes.
    ``bot`` (the offering handler's) is captured on the bundle for the P1
    quarantine-refusal notice; None preserves the log-only degradation.

    ``has_reply_context`` is OBSERVED by the caller (``_apply_reply_context``
    returns whether it actually rendered a quote) — never guessed from the text,
    which is unrecoverably flattened by then (plan §2.3 [r4 P2-1]).
    """
    await _offer_text_part(
        route,
        text,
        bot=bot,
        provenance=Provenance(typed_text=True, reply_context=has_reply_context),
    )


async def aggregator_offer_voice(
    route: Route,
    transcribed_text: str,
    *,
    bot: Any | None = None,
    has_reply_context: bool = False,
) -> None:
    """Voice transcripts ride the same bundle path, with the VOICE fact set.

    Distinct from ``aggregator_offer_text`` only in provenance — and that
    distinction is the point: a voice note IS the user speaking, so it is
    free-text-eligible (plan §2.3).
    """
    await _offer_text_part(
        route,
        transcribed_text,
        bot=bot,
        provenance=Provenance(voice=True, reply_context=has_reply_context),
    )


async def _offer_attachment(
    route: Route,
    path: Path,
    caption: str | None,
    media_group_id: str | None,
    *,
    bot: Any | None = None,
    has_reply_context: bool = False,
) -> None:
    """Append an attachment (and any caption) to the route's bundle.

    When ``len(attachment_paths)`` reaches the configured cap the bundle is
    force-flushed immediately rather than waiting on the debounce — keeps an
    unbounded media dump from sitting in memory.
    """
    item = Provenance(
        attachment=True,
        caption=bool(caption),
        reply_context=bool(caption) and has_reply_context,
    )
    lock = _get_lock(route)
    flush_now = False
    async with lock:
        bundle = _get_or_create_bundle(route)
        bundle.bot = bot or bundle.bot

        # Boundary force-flush: a new media-group arriving inside the
        # debounce window must not merge with the previous group's items.
        # Pop and dispatch the in-progress bundle without awaiting (the
        # lock is non-reentrant and ``_send_bundle`` does network IO),
        # then start a fresh bundle under the same lock acquisition.
        if (
            media_group_id is not None
            and bundle.current_media_group_id is not None
            and media_group_id != bundle.current_media_group_id
            and (bundle.attachment_paths or bundle.text_parts)
        ):
            old_bundle = _pop_bundle_locked(route)
            if old_bundle is not None:
                _spawn_background(_send_bundle(route, old_bundle))
            bundle = _get_or_create_bundle(route)
            # r2 P2: the FRESH post-boundary bundle must re-capture the bot
            # (the set above landed on the popped bundle) — otherwise a
            # quarantine-refused flush of this second bundle silently loses
            # its in-topic notice. Carry the old bundle's bot as fallback.
            bundle.bot = bot or (old_bundle.bot if old_bundle else None)
            # ...but it must NOT inherit the popped bundle's PROVENANCE (plan
            # §2.3 [r4 P2-1]): the fresh bundle is a different user intent, and
            # carrying group-1's facts over would mis-classify group-2. It starts
            # from the default and takes ``item``'s facts below, like any first
            # offer into a new bundle.

        if caption and caption not in bundle.seen_captions:
            bundle.text_parts.append(caption)
            bundle.seen_captions.add(caption)
        bundle.provenance = bundle.provenance.merge(item)
        bundle.attachment_paths.append(path)
        # Only update on a grouped attachment: a non-grouped item joining the
        # bundle must not erase the "last group" memory, or the next group's
        # boundary check would silently merge it with the previous album.
        if media_group_id is not None:
            bundle.current_media_group_id = media_group_id

        if len(bundle.attachment_paths) >= config.aggregator_max_attachments:
            flush_now = True
            _cancel_handle(bundle)
        else:
            _schedule_flush(route, bundle)

    if flush_now:
        await _flush(route)


async def aggregator_offer_photo(
    route: Route,
    path: Path,
    caption: str | None,
    media_group_id: str | None,
    *,
    bot: Any | None = None,
    has_reply_context: bool = False,
) -> None:
    await _offer_attachment(
        route,
        path,
        caption,
        media_group_id,
        bot=bot,
        has_reply_context=has_reply_context,
    )


async def aggregator_offer_document(
    route: Route,
    path: Path,
    caption: str | None,
    media_group_id: str | None,
    *,
    bot: Any | None = None,
    has_reply_context: bool = False,
) -> None:
    await _offer_attachment(
        route,
        path,
        caption,
        media_group_id,
        bot=bot,
        has_reply_context=has_reply_context,
    )


async def aggregator_replay_payload(
    route: Route,
    *,
    text: str | None,
    attachments: Sequence[AggregatorReplayAttachment],
    text_provenance: Provenance | None = None,
) -> DeliveryResult:
    """Synchronously send a pending first-turn payload and aggregate status.

    This is the safe replay path for unbound-topic payloads held while the user
    chooses a directory/window/session. It intentionally bypasses the offer API:
    offers can force-flush on media-group boundaries or attachment-count caps,
    and those intermediate sends are backgrounded/ignored in the normal live
    aggregation path. Pending replay must instead await every send it causes so
    the UI never reports "First message sent" after an earlier split failed.

    The bundle construction mirrors ``_offer_attachment`` as closely as
    practical: pending text is included once at the front, captions are deduped
    per bundle, media-group boundaries split bundles, and the max-attachment cap
    still prevents unbounded bundle growth. Every split is sent via
    ``_send_bundle`` sequentially.

    GH #50 §1.4: the return is the STRUCTURED result — the FIRST refusal wins
    (so the pending-bind replay, which IS the fresh-session folder-trust case,
    can surface the real reason instead of a bare ``False``); an all-delivered
    replay returns the last success.

    r2 F2(i): the replay STOPS at the first refusal. It used to keep sending the
    remaining split bundles, which is wrong twice over — a refusal means the pane
    would not take the payload (so the later splits are refused too, spamming the
    topic with notices), and a ``draft_written`` refusal leaves the FIRST split
    sitting unsent in the input box, so the NEXT split would be typed onto it and
    its Enter would commit BOTH. (The per-window stranded-draft brake is the
    backstop; this is the caller-side rule that keeps the ordering honest.)
    """
    outcome: DeliveryResult = delivery.delivered("nothing to replay")
    bundle = _PendingBundle()
    max_attachments = max(1, config.aggregator_max_attachments)

    if text:
        bundle.text_parts.append(text)
        # Plan §2.3 [r4 P2-1]: the pending store carries the facts OBSERVED at the
        # original offer (typed vs voice, and whether a reply-context quote was
        # actually rendered), so the pending-bind replay preserves them instead of
        # guessing from the already-flattened string. A pre-GH#50-PR-2 pending
        # payload (no stored facts) degrades to typed-text-only, which is what it
        # was before this lane existed.
        bundle.provenance = text_provenance or Provenance(typed_text=True)

    async def send_current_bundle() -> bool:
        """Send the in-progress bundle. False ⇒ REFUSED, the caller must stop."""
        nonlocal bundle, outcome
        if not (bundle.text_parts or bundle.attachment_paths):
            return True
        # The replay is SYNCHRONOUS and its callers (``_flush_pending_route_payload``
        # → the two directory/session-picker bind seams) surface the returned
        # reason in their own "first message not delivered" edit — so the
        # aggregator must not also post one. Byte-neutral today (the replay's
        # fresh ``_PendingBundle`` carries no ``bot``, which already suppressed
        # the notice), but now the OWNERSHIP is explicit rather than accidental.
        result = await _send_bundle(route, bundle, report_refusal=False)
        bundle = _PendingBundle()
        if not result.ok:
            outcome = result  # First refusal wins and ends the replay.
            return False
        outcome = result
        return True

    for attachment in attachments:
        media_group_id = attachment.media_group_id
        if (
            media_group_id is not None
            and bundle.current_media_group_id is not None
            and media_group_id != bundle.current_media_group_id
            and (bundle.attachment_paths or bundle.text_parts)
        ):
            if not await send_current_bundle():
                return outcome

        if attachment.caption and attachment.caption not in bundle.seen_captions:
            bundle.text_parts.append(attachment.caption)
            bundle.seen_captions.add(attachment.caption)
        # OR-compose the attachment's facts, exactly as ``_offer_attachment``
        # does — so a replayed split carrying an attachment is never
        # free-text-eligible, and a SPLIT bundle created by the loop above
        # starts from the fresh ``_PendingBundle``'s empty facts (never
        # inheriting the sent split's).
        bundle.provenance = bundle.provenance.merge(
            Provenance(
                attachment=True,
                caption=bool(attachment.caption),
                reply_context=bool(attachment.caption) and attachment.has_reply_context,
            )
        )
        bundle.attachment_paths.append(attachment.path)
        if media_group_id is not None:
            bundle.current_media_group_id = media_group_id

        if len(bundle.attachment_paths) >= max_attachments:
            if not await send_current_bundle():
                return outcome

    await send_current_bundle()
    return outcome


async def aggregator_flush_route(
    route: Route, *, report_refusal: bool = True
) -> DeliveryResult:
    """Force-flush a route's bundle. Used by slash-command forwarders.

    Delegates straight to ``_flush`` so the pop and the cancel happen under
    a single lock acquisition — no reentrancy hazard, no race window where a
    concurrent offer can resurrect the bundle between cancel and send. Returns
    the STRUCTURED delivery result (``ok`` False when the forced send was
    attempted but refused/failed).

    ``report_refusal=False`` transfers ownership of the user-facing refusal
    message to the caller (see ``_send_bundle``). The three SYNCHRONOUS
    forced-flush callers — ``bot.forward_command_handler``,
    ``callback_dispatcher/effort``, ``callback_dispatcher/late_answer`` — all
    inspect the returned result, ABORT their own send (the r2 F2(i) caller-abort
    chain), and post their own ❌ with the real reason; without this they got a
    SECOND ❌ from the aggregator for the same event.
    """
    return await _flush(route, report_refusal=report_refusal)


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
    return bundle is not None and (
        bool(bundle.text_parts) or bool(bundle.attachment_paths)
    )
