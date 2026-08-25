"""Execute the GH #65 folder-trust creation-flow callbacks (``tst:`` lane).

Core responsibilities:
  - ``tst:t:<token>`` — the [✅ Trust this folder] tap: a synchronous state
    claim under the creation lock, then the SHIPPED ``dcp:`` dispatch
    transaction (window send lock + reject-if-held, extractor parity,
    body-inclusive fingerprint identity, a FRESH in-lock license re-read,
    exact-step nav + motion proof, ``Enter`` as the ONLY commit key,
    ``auq_action_ledger`` idempotency) with the trust lane's OWN license
    predicate — the version PROBED in this very pane at creation.
  - ``tst:c:<generation>`` — the [❌ Cancel] tap: NO keystrokes; the window is
    KILLED through the typed guard, and a cancel that races a registration is
    SPARED (the completion tail then wins).

Ownership is PRE-BINDING (the window is created but deliberately not bound
yet), so the stale-window lease is substituted by the picker-entry claim:
the entry must exist for THIS thread, carry this flow's generation, and be in
an acceptable phase — the directory-browser precedent.

Key components: execute_trust_callback().
"""

from __future__ import annotations

import logging
from typing import Any

from cctelegram.handlers import decision_token, trust_flow
from cctelegram.handlers.callback_data import CB_TRUST_PICK
from cctelegram.handlers.message_sender import safe_edit
from cctelegram.tmux_manager import pane_command_is_claude

from . import WRONG_USER_PICK_TEXT, safe_answer

logger = logging.getLogger(__name__)

TRUST_EXPIRED_TEXT = "This card expired — send a message here to start a new session."
TRUST_BUSY_TEXT = "Already being handled."


async def execute_trust_callback(authorized: Any, adapters: Any) -> None:
    context = authorized.ctx.context
    user = authorized.ctx.user
    query = authorized.ctx.query
    data = authorized.command.data
    thread_id = authorized.ctx.thread_id
    tmux_manager = adapters.tmux_manager

    # OWNER GATE FIRST (review r1 P2-4). The card lives in a shared forum topic,
    # so another allowed user can tap it — and their PTB ``user_data`` is a
    # DIFFERENT dict, so every entry-keyed check below would read "expired" and
    # EDIT THE OWNER'S CARD. A non-owner tap answers the callback and touches
    # nothing: no edit, no token consume, keyboard intact.
    card = getattr(query, "message", None)
    owner = trust_flow.flow_owner_for_card(
        getattr(card, "chat_id", None), getattr(card, "message_id", None)
    )
    if owner is not None and owner != user.id:
        logger.info("TRUST_TAP wrong_user user=%d owner=%d", user.id, owner)
        await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
        return

    payload = data[len(CB_TRUST_PICK) :]
    kind, _, rest = payload.partition(":")

    if kind == "c":
        await _handle_cancel(query, context, user, thread_id, rest, tmux_manager)
        return
    if kind == "t":
        await _handle_trust(query, context, user, thread_id, rest, tmux_manager)
        return
    logger.info("TRUST_TAP malformed user=%d", user.id)
    await safe_answer(query, TRUST_EXPIRED_TEXT, show_alert=True)


async def _handle_cancel(
    query: Any,
    context: Any,
    user: Any,
    thread_id: int | None,
    raw_generation: str,
    tmux_manager: Any,
) -> None:
    """[❌ Cancel] — claim, then EVERY exit releases the phase IT claimed.

    Wave 4 (review r4 P1-A). The old shape claimed ``cancelling`` but released
    through a ``dispatching``-only CAS, so the stale-generation and both SPARED
    exits never released at all and a stale card permanently wedged a newer
    flow. Now the card's generation is validated INSIDE the acquisition (a stale
    card never claims), and everything after the acquisition sits in a
    ``try/finally`` that restores ``awaiting_trust`` on any escape.
    """
    try:
        card_generation = int(raw_generation)
    except ValueError:
        await safe_edit(query, TRUST_EXPIRED_TEXT, reply_markup=None)
        await safe_answer(query)
        return

    claim = await trust_flow.claim_for_cancel(
        user.id,
        thread_id,
        user_data=context.user_data,
        card_generation=card_generation,
    )
    if not claim.ok or claim.flow is None:
        if claim.reason == "wrong_state":
            await safe_answer(query, TRUST_BUSY_TEXT)
        else:
            await safe_edit(query, TRUST_EXPIRED_TEXT, reply_markup=None)
            await safe_answer(query)
        return

    flow = claim.flow
    settled = False
    try:
        outcome = await trust_flow.cancel_flow(flow, context.bot, tmux_manager)
        # ``cancel_flow`` owns the release for every outcome it handles.
        settled = True
        if outcome is trust_flow.CleanupOutcome.SPARED_REGISTERED:
            await safe_answer(query, "Session already started — binding it instead.")
            return
        if outcome is trust_flow.CleanupOutcome.SPARED_BOUND:
            await safe_answer(query, "Already bound.")
            return
        await trust_flow.finish_cancelled_flow(flow)
        await safe_answer(
            query,
            "Cancelled"
            if outcome is trust_flow.CleanupOutcome.KILLED
            else "Cancelled; cleanup failed",
            show_alert=outcome is trust_flow.CleanupOutcome.KILL_FAILED,
        )
    finally:
        if not settled:
            # An exception or a cancellation escaped mid-cancel: hand the flow
            # back rather than leaving it claimed forever. The release CAS names
            # the phase THIS actor holds.
            await trust_flow.release_claim(
                flow,
                expect=trust_flow.PHASE_CANCELLING,
                to=trust_flow.PHASE_AWAITING_TRUST,
            )


async def _handle_trust(
    query: Any,
    context: Any,
    user: Any,
    thread_id: int | None,
    token: str,
    tmux_manager: Any,
) -> None:
    # GATE 1 of the two-gate contract at the CALLBACK entry (the render mint is
    # the other): the trust flag ON and no EXPLICIT operator kill switch.
    if not decision_token.trust_card_dispatch_enabled():
        await safe_answer(query, "Trust dispatch is disabled — answer in tmux.")
        return

    claim = await trust_flow.claim_for_dispatch(
        user.id, thread_id, user_data=context.user_data
    )
    if not claim.ok or claim.flow is None:
        if claim.reason == "wrong_state":
            await safe_answer(query, TRUST_BUSY_TEXT)
        else:
            await safe_edit(query, TRUST_EXPIRED_TEXT, reply_markup=None)
            await safe_answer(query)
        return
    flow = claim.flow
    # EVERYTHING after the acquisition runs inside the guard (review r4 P1-B):
    # the token consume and the window lookup below are awaits too, and a
    # cancellation in either used to leave ``dispatching`` set forever — a phase
    # the ceiling can only ever retry a CAS against, never clear.
    try:
        await _dispatch_after_claim(query, context, user, flow, token, tmux_manager)
    finally:
        if flow.phase == trust_flow.PHASE_DISPATCHING:
            await trust_flow.release_claim(
                flow,
                expect=trust_flow.PHASE_DISPATCHING,
                to=(
                    trust_flow.PHASE_AWAITING_REGISTRATION
                    if flow.enter_sent_at is not None
                    else trust_flow.PHASE_AWAITING_TRUST
                ),
            )


async def _dispatch_after_claim(
    query: Any,
    context: Any,
    user: Any,
    flow: Any,
    token: str,
    tmux_manager: Any,
) -> None:
    """The Trust tap's body, running under the caller's ``dispatching`` claim."""
    peeked = decision_token.peek(token)
    if peeked is not None and peeked.user_id != user.id:
        # Belt and braces beside the entry-level owner gate above.
        await trust_flow.release_dispatch_claim(
            flow, phase=trust_flow.PHASE_AWAITING_TRUST
        )
        await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
        return
    if (
        peeked is None
        or peeked.window_id != flow.created_wid
        or flow.fingerprint is None
        or peeked.fingerprint != flow.fingerprint
    ):
        # EXPIRED / STALE tap. The prompt itself may well still be live, so a
        # bare answer would leave the visible button permanently dead — do the
        # graceful RE-RENDER the dcp:/AUQ expired-tap paths do (fresh capture,
        # fresh mint when the pane is still a licensed trust frame).
        await trust_flow.release_dispatch_claim(
            flow, phase=trust_flow.PHASE_AWAITING_TRUST
        )
        await _refresh_trust_card(flow, context, tmux_manager)
        await safe_answer(query, "↻ Refreshed — tap Trust again.", show_alert=True)
        return

    consumed = await decision_token.consume(token, user.id)
    if consumed.outcome != "ok" or consumed.entry is None:
        await trust_flow.release_dispatch_claim(
            flow, phase=trust_flow.PHASE_AWAITING_TRUST
        )
        if consumed.outcome == "already_consumed":
            await safe_answer(query, "Action already received.")
            return
        if consumed.outcome == "wrong_user":
            await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
            return
        await _refresh_trust_card(flow, context, tmux_manager)
        await safe_answer(query, "↻ Refreshed — tap Trust again.", show_alert=True)
        return
    entry = consumed.entry

    w = await tmux_manager.find_window_by_id(flow.created_wid)
    if not w:
        await trust_flow.release_dispatch_claim(
            flow, phase=trust_flow.PHASE_AWAITING_TRUST
        )
        await safe_answer(query, "Window not found", show_alert=True)
        return

    # P1-C(ii): ``dispatching`` must never become a black hole. Any raise or
    # cancellation between the claim and the normal release would strand the
    # flow in a phase that suspends the kill-capable budgets, so the phase is
    # restored in a ``finally``: ``awaiting_registration`` when the transaction
    # provably SENT Enter, ``awaiting_trust`` otherwise. The WAIT loop's global
    # observation ceiling is the backstop for anything this cannot reach.
    outcome = None
    progress: dict[str, bool] = {"enter_sent": False}

    def _note_commit_sent() -> None:
        progress["enter_sent"] = True
        trust_flow.note_dispatch_enter_sent(flow)

    try:
        outcome = await _dispatch_trust(
            user=user,
            tmux_manager=tmux_manager,
            w=w,
            flow=flow,
            entry=entry,
            on_commit_sent=_note_commit_sent,
        )
        # The hook is the AUTHORITY (review r3 P2-5): a cancellation after the
        # Enter but before the return must still restore ``awaiting_registration``
        # with a rebased budget, never ``awaiting_trust`` with a stale deadline.
        enter_sent = progress["enter_sent"] or (
            outcome is not None and outcome.kind in ("dispatched", "commit_unconfirmed")
        )
    finally:
        enter_sent = progress["enter_sent"] or (
            outcome is not None and outcome.kind in ("dispatched", "commit_unconfirmed")
        )
        if flow.phase == trust_flow.PHASE_DISPATCHING:
            await trust_flow.release_dispatch_claim(
                flow,
                phase=(
                    trust_flow.PHASE_AWAITING_REGISTRATION
                    if enter_sent
                    else trust_flow.PHASE_AWAITING_TRUST
                ),
            )

    if outcome is None:
        # The send lock was held — Enter provably never sent.
        await trust_flow.release_dispatch_claim(
            flow, phase=trust_flow.PHASE_AWAITING_TRUST
        )
        await _refresh_trust_card(flow, context, tmux_manager)
        await safe_answer(query, "Window busy; refreshing card.")
        return

    if outcome.kind == "not_advanced":
        # PRE-COMMIT bail: DEMOTE explicitly back to ``awaiting_trust`` with a
        # fresh render (fresh tokens re-validate against the live pane).
        await trust_flow.release_dispatch_claim(
            flow, phase=trust_flow.PHASE_AWAITING_TRUST
        )
        await _refresh_trust_card(flow, context, tmux_manager)
        await safe_answer(query, "Action not registered; refreshing card.")
        return

    # ``dispatched`` OR ``commit_unconfirmed``: the Enter WAS sent (addendum r1
    # P1 — a post-Enter blank frame classifies ``commit_unconfirmed``, never
    # ``dispatched``). Both end the human trust wait, so both REBASE the
    # registration budget and enter ``awaiting_registration``; the WAIT task's
    # documented demotion recovers if the Enter did not in fact commit.
    await trust_flow.release_dispatch_claim(
        flow, phase=trust_flow.PHASE_AWAITING_REGISTRATION
    )
    await trust_flow.edit_card_public(
        flow,
        context.bot,
        "✅ Trust sent — starting the session…"
        if outcome.kind == "dispatched"
        else "✅ Trust sent (unconfirmed) — checking the window…",
    )
    await safe_answer(
        query,
        "✅ Trust sent" if outcome.kind == "dispatched" else "Action sent; confirming.",
    )


async def _refresh_trust_card(flow: Any, context: Any, tmux_manager: Any) -> None:
    """Re-render the card through the LIVE-PANE-GATED seam.

    The single re-render seam: an expired/stale tap, a busy send lock, and a
    pre-commit bail all use it, so a live prompt never ends up behind a dead
    button. ``refresh_card_if_live`` owns the two guards (review r2 P2-C): a
    positive ``pane_command_is_claude`` before ANY re-mint — a committed or
    cancelled prompt leaves its text painted on a dead pane — and a re-validation
    of the flow's identity/generation after its own capture await.
    """
    await trust_flow.refresh_card_if_live(flow, context.bot, tmux_manager)


async def _dispatch_trust(
    *,
    user: Any,
    tmux_manager: Any,
    w: Any,
    flow: Any,
    entry: Any,
    on_commit_sent: Any = None,
) -> Any:
    """Lock-acquire (reject-if-held) → the shipped locked pane transaction.

    Returns ``None`` when the send lock was busy (Enter provably never sent).
    """
    from cctelegram.handlers import auq_ledger

    from .interactive import (
        _dispatch_decision_pane_locked,
        _lock_busy,
        _window_send_lock,
    )

    lock = _window_send_lock(tmux_manager, w.window_id)
    if _lock_busy(lock):
        if flow.ledger_key is not None:
            auq_ledger.record(
                flow.ledger_key, state="not_advanced", failed_reason="lock_busy"
            )
        return None

    def _trust_license(family: str, live_cmd: str | None) -> bool:
        """The trust lane's FRESH in-lock license predicate."""
        return (
            family == trust_flow.TRUST_FAMILY
            and pane_command_is_claude(live_cmd)
            and bool(flow.cli_version)
            and decision_token.lookup(trust_flow.TRUST_FAMILY, flow.cli_version or "")
        )

    if flow.ledger_key is not None:
        auq_ledger.record(
            flow.ledger_key,
            state="accepted",
            user_id=user.id,
            window_id=flow.created_wid,
            full_fingerprint=entry.fingerprint,
            option_number=entry.option_number,
            option_label=entry.option_label,
        )
    async with lock:
        return await _dispatch_decision_pane_locked(
            user=user,
            tmux_manager=tmux_manager,
            w=w,
            window_id=flow.created_wid,
            minted_fingerprint=entry.fingerprint,
            option_number=entry.option_number,
            option_label=entry.option_label,
            ledger_key=flow.ledger_key,
            license_check=_trust_license,
            on_commit_sent=on_commit_sent,
        )
