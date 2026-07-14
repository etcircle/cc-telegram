"""Execute interactive AskUserQuestion navigation and pick callbacks.

Core responsibilities:
  - Own CB_ASK_* navigation, refresh, and tokenized pick callbacks.
  - Preserve wrong-user and stale-form safety for interactive picks.
  - Re-render interactive cards after dispatch through injected adapters.

Key components:
  - execute_interactive_callback()
"""

from __future__ import annotations

from typing import Any, Literal, cast

import asyncio
import logging
from dataclasses import dataclass
from types import SimpleNamespace

from cctelegram.handlers import (
    auq_ledger,
    auq_source,
    decision_token,
    interactive_ui,
    pick_intent,
    pick_token,
)
from cctelegram.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_PICK,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_TOGGLE,
    CB_ASK_UP,
    CB_DECISION_PICK,
)
from cctelegram.handlers.inbound_telegram import _get_thread_id
from cctelegram.handlers.interactive_ui import (
    NAV_ESC_CLEAR,
    assert_nav_dispatchable,
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from cctelegram.terminal_parser import (
    DECISION_VARIANT_FOOTERED,
    AskUserQuestionForm,
    _loose_label_match,
    _pane_looks_like_picker,
    decision_prompt_fingerprint,
    decision_variant_of,
    extract_interactive_content,
    has_decision_residue,
    parse_generic_decision,
    resolve_ask_form,
)
from cctelegram.tmux_manager import capture_pane_pair, pane_command_is_claude
from cctelegram.tmux_manager import tmux_manager as _tmux_singleton

from . import (
    WRONG_USER_PICK_TEXT,
    _answer_invalid_pending_picker_callback,
    _validate_pending_picker_callback,
    owner_matches,
    safe_answer,
    window_lease,
)

logger = logging.getLogger(__name__)
resolve_ask_tool_input = interactive_ui.resolve_ask_tool_input

# v2.1.168 navigate-to-target + Enter dispatch settle windows. After the nav
# keystrokes we wait for the cursor redraw before re-capturing to verify; after
# Enter we wait for the advance redraw before re-capturing to confirm. Tuned so
# the first confirm capture almost always already sees the advance (the redraw
# lag is ~10-100ms ≪ 0.5s), so the common path records ``dispatched`` and a
# genuinely-stuck future variant degrades to ``commit_unconfirmed`` (refresh).
NAV_SETTLE = 0.5
COMMIT_SETTLE = 0.5


def _is_multi_q(form: AskUserQuestionForm) -> bool:
    """True iff the form carries ≥2 real (non-submit) question tabs."""
    return len([t for t in form.tabs if not t.is_submit]) >= 2


def _classify_advance(
    committed: AskUserQuestionForm,
    entry: Any,
    aform: AskUserQuestionForm | None,
    resolved: bool,
) -> bool:
    """Decide whether the post-Enter pane proves the EXACT expected advance.

    ``committed`` is the form the user tapped (the live re-parse at validate);
    ``entry`` carries ``.option_number`` + ``.is_review_submit``; ``aform`` is the
    confirm re-parse (None when the pane no longer parses as a picker); ``resolved``
    is True only when the picker positively disappeared (markers absent). Returns
    True ONLY on a proven expected transition — every other shape fails CLOSED so a
    sticky / over-advanced / wrong-tab form is never recorded ``dispatched``.
    """
    # Review-screen CANCEL (option 2): success == left the review screen.
    if (
        committed.is_review_screen
        and entry.option_number == 2
        and not entry.is_review_submit
    ):
        return resolved or (aform is not None and not aform.is_review_screen)
    # Submit / single-question pick: success == the tool RESOLVED (picker gone).
    # NOT "aform is None" — a parse-failure is not a resolution.
    if entry.is_review_submit or not _is_multi_q(committed):
        return resolved
    # Multi-question pick: a POSITIVE forward transition only; a disappearance is
    # NOT success (a non-final pick must advance to the next question).
    if aform is None:
        return False
    before = [t.answered for t in committed.tabs if not t.is_submit]
    after = [t.answered for t in aform.tabs if not t.is_submit]
    if len(before) != len(after):
        return False
    ci = sum(before)  # committed question = the first unanswered tab index
    if ci >= len(before) or before[ci]:
        return False
    expected = before[:]
    expected[ci] = True  # EXACTLY the committed tab flips False → True
    if after != expected:  # over-advance / wrong-tab / no-flip CAUGHT
        return False
    if ci == len(before) - 1:
        return aform.is_review_screen  # final pick → review screen
    # Non-final: prove the shown question IS questions[ci+1] by its OPTION-SET
    # (stronger than the possibly-duplicate title), unique among the remaining
    # unanswered questions → else fail closed.
    if aform.is_review_screen:
        return False
    qs = committed.questions
    if not (len(qs) > ci + 1):
        return False
    next_opts = tuple(o.label for o in qs[ci + 1].options)
    if not next_opts or tuple(o.label for o in aform.options) != next_opts:
        return False
    remaining = [tuple(o.label for o in qs[j].options) for j in range(ci + 1, len(qs))]
    return remaining.count(next_opts) == 1


async def _refresh_pick_card(
    query: Any,
    context: Any,
    update: Any,
    user: Any,
    tmux_manager: Any,
    adapters: Any,
    *,
    text: str,
    show_alert: bool = False,
    fallback_window_id: str | None = None,
) -> None:
    """Answer the callback with ``text`` and re-render the live picker card.

    Used by every short-circuit branch in the pick handler (legacy/new
    expired token, malformed callback_data, ledger projection that wants
    the user to retry). Resolves the route's current window via
    ``get_interactive_window``; falls back to ``fallback_window_id`` when
    the ledger row pointed at a window that's no longer bound.

    ``show_alert`` is passed through to the callback answer: the dead-token
    (``peek_none`` / ``expired``) callers set it ``True`` so their honest
    "tap again" prompt is a MODAL the user can't miss, while the ledger-state
    callers keep the default ``False`` so their specific warnings (e.g.
    ``failed_before_digit`` "Action failed previously; refreshing.") stay as
    non-modal toasts.
    """
    await safe_answer(query, text, show_alert=show_alert)
    thread_id = _get_thread_id(update)
    window_id = get_interactive_window(user.id, thread_id) or fallback_window_id or ""
    if window_id:
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )


async def _rerender_picker(
    context: Any,
    user: Any,
    tmux_manager: Any,
    adapters: Any,
    window_id: str,
    thread_id: int | None,
) -> None:
    """Re-render the live picker card after a (non-)committing dispatch outcome.

    Orphan-card safety is the visible-pane liveness bail inside
    ``handle_interactive_ui``.
    """
    await handle_interactive_ui(
        context.bot,
        user.id,
        window_id,
        thread_id,
        tmux_mgr=tmux_manager,
        session_mgr=adapters.session_manager,
    )


@dataclass(frozen=True)
class _PickPaneOutcome:
    """Structured outcome of the locked pane-critical dispatch section.

    ``kind`` is the terminal ledger state already recorded inside the lock;
    ``reason`` carries the bail sub-reason (None on a confirmed dispatch). The
    unlocked response section renders the callback answer + re-render from it.
    """

    kind: Literal["dispatched", "not_advanced", "commit_unconfirmed"]
    reason: str | None = None


def _window_send_lock(tmux_mgr: Any, window_id: str) -> asyncio.Lock:
    """Resolve the per-window send lock for ``window_id``.

    Production passes the real ``TmuxManager`` (which owns the registry —
    see the lifecycle + leaf rule in ``tmux_manager``'s module docstring);
    test fakes that don't implement ``window_send_lock`` fall back to the
    module singleton's registry so the dispatch still serializes per window.
    """
    getter = getattr(tmux_mgr, "window_send_lock", None)
    if getter is not None:
        return cast(asyncio.Lock, getter(window_id))
    return _tmux_singleton.window_send_lock(window_id)


def _lock_busy(lock: asyncio.Lock) -> bool:
    """True when the window send lock is held OR has live (non-cancelled) waiters.

    ``lock.locked()`` alone is NOT a busy test (Hermes Wave-3b P2-1):
    ``release()`` sets the lock free and wakes the first waiter, but until the
    event loop schedules that waiter it sits in the lock's waiter queue with
    ``locked() == False``. A bare ``locked()`` check in that gap, followed by
    ``async with lock``, QUEUES behind the pending waiter and fires the
    control key late — violating reject-rather-than-queue.

    This reads the CPython-private ``asyncio.Lock._waiters`` deque because
    there is no public waiter introspection, and the alternative —
    ``wait_for(lock.acquire(), 0)`` — is scheduling-dependent even for a free
    lock. The ``getattr`` guard means a future CPython rename degrades to the
    bare ``locked()`` behavior; the contract test
    (``test_lock_busy_cpython_contract_free_with_waiter``) constructs the
    free-with-waiter state against the REAL ``asyncio.Lock`` so such a rename
    breaks loudly in CI, not silently. Cancelled waiters don't count — that
    mirrors ``acquire()``'s own all-cancelled synchronous fast path.

    A not-busy verdict followed immediately by ``async with lock`` (with NO
    await between them) remains atomic on the event loop: a free lock with no
    live waiters acquires synchronously, so the helper+acquire pair is
    yield-free — a genuine try-acquire.
    """
    if lock.locked():
        return True
    waiters = getattr(lock, "_waiters", None)  # CPython asyncio.Lock internal
    return bool(waiters) and any(not w.cancelled() for w in waiters)


# Wave 3b reject-if-held: the busy answer for a single-key control arriving
# while a multi-keystroke pane transaction (e.g. ``_dispatch_pick``) holds the
# window's send lock. Shared with the /esc command and the screenshot
# quick-key path (bot.py / bash.py).
WINDOW_BUSY_TEXT = "⏳ Action in progress — try again in a second"


async def _control_key_or_busy(
    query: Any,
    tmux_mgr: Any,
    window_id: str,
    key: str,
    *,
    literal: bool = False,
) -> bool | None:
    """Try-acquire the per-window send lock and send ONE control key under it.

    Wave 3b reject-if-held for the interactive control paths (Up/Down/Left/
    Right/Escape/Enter/Space/Tab and the ``aqt:`` toggle digit). Returns None
    when the lock is held — the busy answer has already been sent and the
    caller must return without dispatching. Otherwise returns the
    ``send_keys`` bool.

    This is option (b) — a genuine try-acquire, not a bare ``locked()``
    peek-then-send: the ``_lock_busy`` check (held OR live waiters — the
    release→waiter-wakeup gap counts as busy) is immediately followed by the
    acquire with NO await between them, so on the single-threaded event loop
    the pair is atomic (``asyncio.Lock.acquire`` on a free, waiter-less lock
    does not yield). The key is then sent UNDER the lock, so it can never
    interleave inside ``_dispatch_pick_pane_locked``'s verify→Enter critical
    section — the Hermes R2 P1-1 false-``dispatched`` class a plain
    peek-then-send would leave open (peek free → dispatch acquires → control
    key lands mid-transaction). The lock stays a leaf: ``safe_answer`` runs
    only on the reject path, never while the lock is held.
    """
    lock = _window_send_lock(tmux_mgr, window_id)
    if _lock_busy(lock):
        await safe_answer(query, WINDOW_BUSY_TEXT)
        return None
    async with lock:
        return await tmux_mgr.send_keys(window_id, key, enter=False, literal=literal)


async def _dispatch_pick(
    *,
    query: Any,
    context: Any,
    user: Any,
    tmux_manager: Any,
    adapters: Any,
    w: Any,
    window_id: str,
    thread_id: int | None,
    fingerprint: str,
    option_number: int,
    option_label: str,
    is_review_submit: bool,
    current_form: AskUserQuestionForm,
    ledger_key: str | None,
) -> None:
    """Navigate the live cursor to the tapped option, verify, Enter, confirm advance.

    v2.1.168 model: a BARE DIGIT no longer reliably SELECTS (in the notes
    side-panel variant it only navigates), so the bot drives the live cursor to
    the target with ``Up``/``Down``, VERIFIES the cursor landed on the target
    option (same form, right number, matching label, and for Submit the
    review-Submit anchor), presses ``Enter`` (the version-stable commit), then
    re-parses the pane and records the terminal ``dispatched`` ONLY after a
    CONFIRMED expected advance (``_classify_advance``).

    Shared by the live ``ok`` path and D2 restart-recovery. The caller writes the
    ``accepted`` claim BEFORE calling this (the live path inline; recovery inside
    ``pick_token.recover_and_consume``). ``ledger_key`` is None only on a
    collision-suppression fall-through — the ``if ledger_key is not None`` guards
    keep those writes off another route's row. ``send_keys`` returns False (does
    not raise) on failure; every return is checked (Wave-3 P1).

    Ledger semantics: a PRE-COMMIT bail (cursor unknown, nav send False, verify
    fail) records ``not_advanced`` (``Enter`` provably never sent → the callback
    handler FALLS THROUGH on a re-tap, safe). Once ``Enter`` is sent the outcome
    is either a confirmed ``dispatched`` (idempotency lock) or ``commit_unconfirmed``
    (refresh-only, never auto-redispatch — no re-tap can re-send the commit key).

    Concurrency (Wave 3a, finding 6 / Hermes P1-3): the whole pane-critical
    section — cursor find, nav sends, settles, verify capture, Enter, confirm
    capture, ``_classify_advance``, and the terminal ledger write (file I/O,
    deliberately INSIDE so a concurrent tap can't race the outcome record) —
    runs in ``_dispatch_pick_pane_locked`` under the per-window send lock. The
    Telegram response (``safe_answer`` + ``_rerender_picker``) runs strictly
    AFTER release: the lock is a leaf and no Telegram I/O may run while held.
    """
    async with _window_send_lock(tmux_manager, w.window_id):
        outcome = await _dispatch_pick_pane_locked(
            user=user,
            tmux_manager=tmux_manager,
            w=w,
            window_id=window_id,
            fingerprint=fingerprint,
            option_number=option_number,
            option_label=option_label,
            is_review_submit=is_review_submit,
            current_form=current_form,
            ledger_key=ledger_key,
        )
    # ── Unlocked response section: Telegram I/O only after lock release. ──
    if outcome.kind == "dispatched":
        await safe_answer(query, f"{option_number}. {option_label[:32]}")
    elif outcome.kind == "not_advanced":
        await safe_answer(query, "Action not registered; refreshing card.")
    else:  # commit_unconfirmed
        await safe_answer(query, "Action sent; refreshing card.")
    await _rerender_picker(context, user, tmux_manager, adapters, window_id, thread_id)


async def _dispatch_pick_pane_locked(
    *,
    user: Any,
    tmux_manager: Any,
    w: Any,
    window_id: str,
    fingerprint: str,
    option_number: int,
    option_label: str,
    is_review_submit: bool,
    current_form: AskUserQuestionForm,
    ledger_key: str | None,
) -> _PickPaneOutcome:
    """Pane-critical section of the pick dispatch — caller holds the window lock.

    Performs the nav→settle→verify→Enter→settle→confirm keystroke transaction
    plus the TERMINAL ledger write (``dispatched`` / ``not_advanced`` /
    ``commit_unconfirmed``), and NO Telegram I/O. Returns the structured
    outcome the unlocked response section in ``_dispatch_pick`` renders from.
    """

    def _bail_not_advanced(reason: str) -> _PickPaneOutcome:
        if ledger_key is not None:
            auq_ledger.record(ledger_key, state="not_advanced", failed_reason=reason)
        return _PickPaneOutcome("not_advanced", reason)

    def _bail_commit_unconfirmed(reason: str) -> _PickPaneOutcome:
        if ledger_key is not None:
            auq_ledger.record(
                ledger_key, state="commit_unconfirmed", failed_reason=reason
            )
        return _PickPaneOutcome("commit_unconfirmed", reason)

    # Synthetic-cursor SAFETY guard (BEFORE any keystroke; defense in depth — the
    # load-bearing fail-closed gate is the pane-only real-cursor check at verify).
    # On the side-file render path ``current_form`` carries the live pane's cursor
    # overlaid onto the full side-file options — but
    # ``terminal_parser._overlay_cursor_and_selection`` DEFAULTS the cursor to
    # option 1 when the live pane shows no ``❯`` (the real cursor scrolled off a
    # tall card whose top options are above the captured region). That PHANTOM
    # cursor at option 1 makes a tap on option 1 compute ``delta = 0`` (no nav),
    # verify-pass the same phantom, and commit ``Enter`` against whatever the REAL
    # (off-screen) cursor is on — a WRONG dispatch.
    # Re-parse a PANE-ONLY form: when ``current_form`` claims a cursor but the
    # fresh pane proof is ABSENT (empty/unparseable capture ⇒ ``gpane_form is
    # None``) OR carries NO real ``cursor=True`` option, ``current_form``'s cursor
    # is unprovable / the synthetic default → BAIL ``not_advanced`` before any
    # keystroke (the callback falls through; the user re-taps once the cursor is
    # visible, or uses manual nav). FAIL-CLOSED: a missing pane proof is treated
    # as no-real-cursor. Enter is NEVER sent on a synthetic cursor. A real visible
    # pane cursor ⇒ no bail (visible-cursor dispatch is byte-identical).
    # (Making an off-screen option-1 tap actually dispatch is a separate
    # follow-up; here it SAFELY no-ops.)
    if any(o.cursor for o in current_form.options):
        # GH #54 capture spine (seam 4): ANSI pair so a chevron-less preview
        # picker's SGR cursor is parsed here — else the pre-guard would BAIL every
        # preview dispatch as ``cursor_synthetic``.
        gpair = await capture_pane_pair(tmux_manager, w.window_id, scrollback_lines=500)
        gpane = gpair.plain if gpair is not None else None
        gpane_ansi = gpair.ansi if gpair is not None else None
        gpane_form = (
            resolve_ask_form(None, gpane, ansi_text=gpane_ansi) if gpane else None
        )
        if gpane_form is None or not any(o.cursor for o in gpane_form.options):
            logger.info(
                "AUQ_PICK nav cursor_synthetic user=%d window=%s opt=%d "
                "(no real pane cursor%s — bailing before keystroke)",
                user.id,
                window_id,
                option_number,
                "; capture empty/unparseable"
                if gpane_form is None
                else "; phantom default",
            )
            return _bail_not_advanced("cursor_synthetic")

    target = option_number
    cur = next((o for o in current_form.options if o.cursor), None)
    if cur is None or cur.number is None:
        logger.info(
            "AUQ_PICK nav cursor_unknown user=%d window=%s opt=%d",
            user.id,
            window_id,
            target,
        )
        return _bail_not_advanced("cursor_unknown")

    # MONOTONIC navigation: step the cursor one row at a time toward the target —
    # never a wrap-shortcut. ``Down`` increases the number, ``Up`` decreases it.
    delta = target - cur.number
    nav_key = "Down" if delta > 0 else "Up"
    for _ in range(abs(delta)):
        if not await tmux_manager.send_keys(
            w.window_id, nav_key, enter=False, literal=False
        ):
            logger.warning(
                "AUQ_PICK nav send_keys(%s) returned False user=%d window=%s",
                nav_key,
                user.id,
                window_id,
            )
            return _bail_not_advanced("nav_send_failed")

    await asyncio.sleep(NAV_SETTLE)

    vpair = await capture_pane_pair(tmux_manager, w.window_id, scrollback_lines=500)
    vpane = vpair.plain if vpair is not None else None
    vpane_ansi = vpair.ansi if vpair is not None else None
    vform: AskUserQuestionForm | None = None
    if vpane:
        vsource = auq_source.resolve_auq_source(window_id, None, vpane)
        vform = resolve_ask_form(vsource.payload, vpane, ansi_text=vpane_ansi)
    vc = next((o for o in vform.options if o.cursor), None) if vform else None
    # PANE-ONLY real-cursor requirement (the load-bearing fail-closed gate). The
    # overlaid ``vform`` cursor above may be the SYNTHETIC default that
    # ``_overlay_cursor_and_selection`` plants on option 1 when the live pane
    # shows no ``❯`` (a tall card whose real cursor scrolled off-screen, OR a
    # TOCTOU repaint between the pre-guard and here). Re-parse a PANE-ONLY form
    # (``tool_input=None`` ⇒ pure pane, no side-file overlay, no phantom default)
    # and require a REAL pane ``❯`` provably on the target — so ``Enter`` is
    # NEVER sent against an off-screen cursor. A legitimate navigate-into-view
    # scrolls the real cursor onto the captured region, so the pane-only form
    # WILL carry a real cursor at the target on the happy path.
    vpane_only = resolve_ask_form(None, vpane, ansi_text=vpane_ansi) if vpane else None
    vpane_real_cursor = (
        next((o for o in vpane_only.options if o.cursor), None) if vpane_only else None
    )
    logger.info(
        "AUQ_PICK nav_verify user=%d window=%s target=%d cursor_num=%s "
        "cursor_label=%s real_cursor_num=%s",
        user.id,
        window_id,
        target,
        vc.number if vc else None,
        (vc.label[:24] if vc else None),
        vpane_real_cursor.number if vpane_real_cursor else None,
    )
    if not (
        vform is not None
        and vform.fingerprint() == fingerprint  # still the SAME form (cursor-blind)
        and vc is not None
        and vc.number == target
        and _loose_label_match(vc.label, option_label, vc.wrap_canonical)
        and (
            not is_review_submit
            or (vform.review_submit_dispatchable(option_label) and vc.number == 1)
        )
        # A REAL pane cursor must be provably on the target — a synthetic/phantom
        # cursor (no pane ``❯``, or the ``❯`` elsewhere) fails closed here even if
        # the pre-guard let this tap through (TOCTOU). Enter never commits against
        # an off-screen cursor.
        and vpane_real_cursor is not None
        and vpane_real_cursor.number == target
    ):
        return _bail_not_advanced("verify_failed")

    # Cursor confirmed on target — commit with the version-stable Enter. A False
    # return means it never reached tmux (contract: send_keys False == not sent),
    # so this is still a PRE-COMMIT bail.
    if not await tmux_manager.send_keys(
        w.window_id, "Enter", enter=False, literal=False
    ):
        logger.warning(
            "AUQ_PICK commit send_keys(Enter) returned False user=%d window=%s",
            user.id,
            window_id,
        )
        return _bail_not_advanced("commit_send_failed")

    await asyncio.sleep(COMMIT_SETTLE)

    # ── Enter WAS sent: from here a failure is at worst ``commit_unconfirmed`` ──
    pane2_pair = await capture_pane_pair(
        tmux_manager, w.window_id, scrollback_lines=500
    )
    pane2 = pane2_pair.plain if pane2_pair is not None else None
    pane2_ansi = pane2_pair.ansi if pane2_pair is not None else None
    if pane2 is None:
        return _bail_commit_unconfirmed("confirm_capture_failed")
    asource = auq_source.resolve_auq_source(window_id, None, pane2)
    aform = (
        resolve_ask_form(asource.payload, pane2, ansi_text=pane2_ansi)
        if pane2
        else None
    )
    if aform is None:
        if _pane_looks_like_picker(pane2):
            # Markers present but unparseable → AMBIGUOUS; never record dispatched.
            return _bail_commit_unconfirmed("confirm_parse_failed")
        resolved = True  # picker positively GONE → the tool resolved.
    else:
        resolved = False

    confirm_entry = SimpleNamespace(
        option_number=option_number, is_review_submit=is_review_submit
    )
    if not _classify_advance(current_form, confirm_entry, aform, resolved):
        return _bail_commit_unconfirmed("commit_unconfirmed")

    # CONFIRMED expected advance — record the terminal ``dispatched`` (the
    # idempotency lock). Recorded OUTSIDE any further send so a later failure
    # (record write, answer, re-render) can NEVER downgrade a confirmed dispatch.
    if ledger_key is not None:
        auq_ledger.record(ledger_key, state="dispatched")
    logger.info(
        "AUQ_PICK dispatch_ok user=%d window=%s opt=%d label=%s",
        user.id,
        window_id,
        option_number,
        option_label[:24],
    )
    return _PickPaneOutcome("dispatched")


async def _attempt_pick_recovery(
    token: str,
    sender_id: int,
    route_hash: str,
    fp8: str,
    opt_num: int,
    *,
    query: Any,
    context: Any,
    user: Any,
    tmux_manager: Any,
    adapters: Any,
    reject_stale_window: Any,
) -> bool:
    """D2 restart-recovery at a token-less dead branch (peek_none / expired).

    Returns True iff this took over the click (dispatched the recovered option OR
    answered a decline that has its own message); False to fall through to the
    caller's default honest refresh modal. Reached only AFTER the top ledger gate,
    so a recoverable tap provably has no blocking ledger row for its own option.
    """
    intent = pick_intent.lookup_intent(token)
    if intent is None:
        return False
    # Callback-payload parity: the immutable callback_data must agree with the
    # stored intent's derived key — else a corrupt/tampered store row could map a
    # real button token to a different option/route. Mismatch → no recovery.
    if (
        route_hash
        != auq_ledger.make_route_hash(
            intent.user_id, intent.thread_id, intent.window_id
        )
        or fp8 != intent.full_fingerprint[:8]
        or opt_num != intent.option_number
    ):
        logger.info(
            "AUQ_PICK recover parity_mismatch user=%d token=%s", sender_id, token[:6]
        )
        return False
    # Owner-auth (the historic peek_none branch had none) BEFORE the lease check,
    # mirroring the live path's 785→789 ordering.
    if intent.user_id != sender_id:
        logger.info("AUQ_PICK recover wrong_user user=%d", sender_id)
        await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
        return True
    if await reject_stale_window(intent.window_id):
        logger.info(
            "AUQ_PICK recover stale_window user=%d window=%s",
            sender_id,
            intent.window_id,
        )
        return True

    async def _capture(wid: str, scrollback: int, with_ansi: bool = True) -> str | None:
        # GH #54 capture spine: hand pick_token the ANSI frame; it normalizes
        # into a (plain, ansi) pair so the re-parsed cursor is a REAL parsed
        # cursor (tier-2 SGR on a chevron-less preview picker). ``with_ansi=False``
        # is pick_token's one-plain-recapture request on a normalize rejection.
        return await tmux_manager.capture_pane(
            wid, with_ansi=with_ansi, scrollback_lines=scrollback
        )

    result = await pick_token.recover_and_consume(
        token,
        intent,
        sender_id,
        capture_pane=_capture,
        find_window_by_id=tmux_manager.find_window_by_id,
    )
    logger.info(
        "AUQ_PICK recover outcome=%s user=%d window=%s opt=%d",
        result.outcome,
        sender_id,
        intent.window_id,
        intent.option_number,
    )
    if result.outcome == "wrong_user":
        await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
        return True
    if result.outcome == "already":
        await safe_answer(
            query,
            f"Action already received: {intent.option_label[:32]}",
            show_alert=False,
        )
        return True
    if result.outcome == "in_progress":
        await safe_answer(query, "Action in progress", show_alert=False)
        return True
    if result.outcome in ("superseded", "stale_form", "source_drift", "window_gone"):
        # The on-screen keyboard is current/changed — fall through to the honest
        # refresh modal so the user taps the live card.
        return False
    # outcome == "ok": the accepted claim is already written inside
    # recover_and_consume; dispatch the digit.
    assert (
        result.window_id is not None
        and result.option_number is not None
        and result.option_label is not None
        and result.current_form is not None
    )
    w = await tmux_manager.find_window_by_id(result.window_id)
    if not w:
        # The window vanished between recover_and_consume's phase-B find and now.
        # The ``accepted`` claim is already written (inside the reservation), so
        # record ``failed_before_digit`` (a re-tappable projection) rather than
        # leaving the ledger stuck at ``accepted`` → "Action in progress" forever.
        if result.ledger_key is not None:
            auq_ledger.record(
                result.ledger_key,
                state="not_advanced",
                failed_reason="window gone before recovery dispatch",
            )
        await safe_answer(query, "Window not found", show_alert=True)
        return True
    # The review-Submit cursor guard runs INSIDE recover_and_consume (before its
    # accepted claim), so an ``ok`` result has already passed it — navigate +
    # commit. The fingerprint is the recovered form's own (recover_and_consume
    # already proved it equals ``intent.full_fingerprint``), so the nav-verify
    # step compares against the same form.
    await _dispatch_pick(
        query=query,
        context=context,
        user=user,
        tmux_manager=tmux_manager,
        adapters=adapters,
        w=w,
        window_id=result.window_id,
        thread_id=result.thread_id,
        fingerprint=result.current_form.fingerprint(),
        option_number=result.option_number,
        option_label=result.option_label,
        is_review_submit=result.is_review_submit,
        current_form=result.current_form,
        ledger_key=result.ledger_key,
    )
    return True


# ── Stage B2.3: the tappable Decision dispatch lane ──────────────────────────
#
# A PARALLEL, Decision-specific dispatch that reuses the AUQ dispatch DISCIPLINE
# (per-window send lock + ``_lock_busy`` reject, monotonic arrow nav,
# settle→re-parse→verify, Enter as the ONLY commit key, fail-closed advance
# classification, ``auq_ledger`` idempotency) but NEVER the AUQ source/form
# machinery — a Decision pane returns None from ``resolve_auq_source`` /
# ``resolve_ask_form`` (the P1-C dead-tap the lane exists to avoid). Because the
# first keystroke of any nav is an ARROW, dispatch is additionally gated on a
# known-good (family × CC-version) license read FRESH at tap time (§2b).


def _decision_cursor(form: AskUserQuestionForm) -> Any:
    """The single live ``❯`` option in a parsed Decision form, or None."""
    return next((o for o in form.options if o.cursor), None)


def _decision_stable_key(entry: decision_token.DecisionTokenEntry) -> str:
    """The shared-ledger key a live Decision token reconstructs (collision test).

    Mirrors the render mint: ``make_ledger_key(make_route_hash(...),
    decision_fingerprint[:8], opt)``. The ``fp8`` derives from the body-inclusive
    ``decision_prompt_fingerprint`` (domain-prefixed ``decision:``) so it can
    never collide with the AUQ lane's bare-``_canonical_repr`` fp8 (§8)."""
    return auq_ledger.make_ledger_key(
        auq_ledger.make_route_hash(entry.user_id, entry.thread_id, entry.window_id),
        entry.fingerprint[:8],
        entry.option_number,
    )


def _classify_decision_advance(pane2: str, minted_fingerprint: str) -> bool:
    """True iff the committed Decision provably resolved on the post-Enter pane —
    the r4 P1 + r5 P1 false-advance close (with a DEFINED confirm-side residue
    predicate).

    ``dispatched`` may be recorded ONLY when the confirm pane shows (i) a DIFFERENT
    proven-FOOTERED Decision form, OR (ii) NO DECISION RESIDUE. Anything else —
    a footerless / variant-less Decision form, or residue present under a named UI
    or a None extract — fails CLOSED to ``commit_unconfirmed`` (refresh-only).

    Runs the FULL ``extract_interactive_content`` (never the bare
    ``parse_generic_decision`` — a WEAKER recognizer: a Settings/AUQ pane that
    merely decision-parses would fp-compare as a "different Decision" and wrongly
    confirm):

      * extractor → ``Decision`` AND the live form is proven FOOTERED: compare
        ``decision_prompt_fingerprint``. Same fp = the round-3 zero-absence variant
        (the committed prompt re-raised before its absence was observed) →
        unconfirmed; different fp = it resolved and a new FOOTERED one raised within
        the settle → resolved. A footerless / variant-less re-parse of the SAME
        logical prompt (the r5 P1(a) reproduced false-advance) is NOT a different
        form → fail closed.
      * extractor → ANOTHER named UI, or None: ``dispatched`` ONLY when there is NO
        DECISION RESIDUE (``has_decision_residue`` — a strict Decision footer line
        OR a terminal contiguous numbered-option block; a still-standing option
        block IS residue even when no parser recognizes the frame, closing the r5
        P1(b) ``─``-ruled footer-dropped folder-trust false-advance). An EMPTY/blank
        capture carries no residue but is NOT positive absence proof (r2 Hermes P2)
        → fail closed to unconfirmed.
    """
    content = extract_interactive_content(pane2)
    if content is not None and content.name == "Decision":
        aform = parse_generic_decision(pane2)
        if aform is None:
            # Extractor/validator disagreement — ambiguous, fail closed.
            return False
        # Only a proven-FOOTERED live form may establish a "different form"
        # resolution (r4 P1): a footerless / variant-less re-parse is treated as
        # the same logical prompt still present → unconfirmed.
        if decision_variant_of(aform) != DECISION_VARIANT_FOOTERED:
            return False
        return decision_prompt_fingerprint(aform) != minted_fingerprint
    if not pane2 or not pane2.strip():
        return False  # empty capture ≠ positive absence proof (r2 Hermes P2)
    # Named UI / unparseable frame: dispatched ONLY when NO Decision residue remains.
    return not has_decision_residue(pane2)


@dataclass(frozen=True)
class _DecisionPaneOutcome:
    """Structured outcome of the locked Decision-dispatch pane-critical section.

    ``kind`` is the terminal ledger state already recorded inside the lock;
    ``reason`` carries the bail sub-reason (None on a confirmed dispatch).
    """

    kind: Literal["dispatched", "not_advanced", "commit_unconfirmed"]
    reason: str | None = None


async def _dispatch_decision_pane_locked(
    *,
    user: Any,
    tmux_manager: Any,
    w: Any,
    window_id: str,
    minted_fingerprint: str,
    option_number: int,
    option_label: str,
    ledger_key: str | None,
) -> _DecisionPaneOutcome:
    """§3 dispatch transaction — the caller holds the window send lock.

    Extractor parity → body-inclusive fingerprint identity → geometry/family
    gates → FRESH (family × CC-version) license → nav→settle→verify (motion
    proof) → Enter → confirm → terminal ledger write. NO Telegram I/O. Every gate
    runs BEFORE any keystroke, so a keystroke is NEVER sent to an unlicensed /
    non-matching shape.
    """

    def _bail_not_advanced(reason: str) -> _DecisionPaneOutcome:
        if ledger_key is not None:
            auq_ledger.record(ledger_key, state="not_advanced", failed_reason=reason)
        return _DecisionPaneOutcome("not_advanced", reason)

    def _bail_commit_unconfirmed(reason: str) -> _DecisionPaneOutcome:
        if ledger_key is not None:
            auq_ledger.record(
                ledger_key, state="commit_unconfirmed", failed_reason=reason
            )
        return _DecisionPaneOutcome("commit_unconfirmed", reason)

    # (b) Extractor parity — the FULL detector must return a Decision (a
    # Settings / AUQ pane that merely decision-parses is caught here, since
    # first-match-wins gives it its OWN name). Same 500-line scrollback as render.
    pane = await tmux_manager.capture_pane(w.window_id, scrollback_lines=500)
    if not pane:
        return _bail_not_advanced("capture_failed")
    content = extract_interactive_content(pane)
    if content is None or content.name != "Decision":
        logger.info(
            "DECISION dispatch decline extractor_parity user=%d window=%s name=%s",
            user.id,
            window_id,
            None if content is None else content.name,
        )
        return _bail_not_advanced("extractor_parity")
    live_form = parse_generic_decision(pane)
    if live_form is None:
        return _bail_not_advanced("parse_failed")

    # GH #52 — POSITIVE authorization at tap: a card minted FOOTERED whose live pane
    # re-parses footerless / variant-less mid-dispatch must NOT commit (the digit
    # HOTKEY / Enter semantics of the two shapes are not proven equivalent). Bail
    # PRE-COMMIT (Enter never sent → fall through / re-render).
    if decision_variant_of(live_form) != DECISION_VARIANT_FOOTERED:
        return _bail_not_advanced("variant_not_footered")

    # (c) Identity — the body-inclusive fingerprint (two folder-trust prompts for
    # DIFFERENT dirs differ) + geometry/family gates.
    if decision_prompt_fingerprint(live_form) != minted_fingerprint:
        return _bail_not_advanced("fingerprint_mismatch")
    opts = live_form.options
    if sum(1 for o in opts if o.cursor) != 1:
        return _bail_not_advanced("cursor_geometry")
    if any(o.selected is not None for o in opts):
        # This lane never dispatches a multi-select / checkbox shape.
        return _bail_not_advanced("multi_select_marker")
    if not live_form.options_contiguous_from_one():
        return _bail_not_advanced("options_noncontiguous")
    if live_form.select_mode != "single":
        return _bail_not_advanced("not_single_select")
    family = decision_token.identify_family(live_form)
    if family is None:
        return _bail_not_advanced("family_unknown")

    # (a) FRESH (family × CC-version) license — a direct stderr-checked
    # ``display-message`` read (NOT the 1s list cache), INSIDE the lock,
    # immediately before the first keystroke, so a /update-swapped TUI inside the
    # cache TTL can never be arrow-keyed. Non-version-shaped / unlicensed →
    # decline before ANY key + an INFO log (post-/update dead taps stay
    # observable).
    live_cmd = await tmux_manager.pane_current_command(window_id)
    if not pane_command_is_claude(live_cmd) or not decision_token.lookup(
        family, live_cmd or ""
    ):
        logger.info(
            "DECISION dispatch declined: live pane command %r not licensed for "
            "family %s (user=%d window=%s)",
            live_cmd,
            family,
            user.id,
            window_id,
        )
        return _bail_not_advanced("version_unlicensed")

    # (d) Nav + liveness (motion proof normative). ``delta`` is computed from the
    # PRE-nav live cursor (never a minted form); the wiggle proves the ❯ can move
    # (a quoted block cannot).
    target = option_number
    cur = _decision_cursor(live_form)
    if cur is None or cur.number is None:
        return _bail_not_advanced("cursor_unknown")
    pre_nav_pos = cur.number

    async def _reparse_decision() -> AskUserQuestionForm | None:
        p = await tmux_manager.capture_pane(w.window_id, scrollback_lines=500)
        return parse_generic_decision(p) if p else None

    delta = target - pre_nav_pos
    if delta != 0:
        nav_key = "Down" if delta > 0 else "Up"
        for _ in range(abs(delta)):
            if not await tmux_manager.send_keys(
                w.window_id, nav_key, enter=False, literal=False
            ):
                return _bail_not_advanced("nav_send_failed")
        await asyncio.sleep(NAV_SETTLE)
        vform = await _reparse_decision()
        vc = _decision_cursor(vform) if vform else None
        if (
            vform is None
            or decision_prompt_fingerprint(vform) != minted_fingerprint
            or vc is None
            or vc.number != target
            or vc.number == pre_nav_pos  # MOTION observed (delta != 0)
        ):
            return _bail_not_advanced("verify_failed")
        landed_label = vc.label
    else:
        # Wiggle: delta == 0 → nudge one row away (direction by bounds) and back,
        # requiring the cursor to MOVE then RETURN — a strong live-cursor proof
        # (a quoted block can't move its ❯). Not a structural close (a verify→Enter
        # TOCTOU window remains, same class as AUQ's).
        away, back = ("Down", "Up") if target <= 1 else ("Up", "Down")
        if not await tmux_manager.send_keys(
            w.window_id, away, enter=False, literal=False
        ):
            return _bail_not_advanced("nav_send_failed")
        await asyncio.sleep(NAV_SETTLE)
        wform = await _reparse_decision()
        wc = _decision_cursor(wform) if wform else None
        if (
            wform is None
            or decision_prompt_fingerprint(wform) != minted_fingerprint
            or wc is None
            or wc.number == target  # cursor did NOT move → not a live picker
        ):
            return _bail_not_advanced("wiggle_no_motion")
        if not await tmux_manager.send_keys(
            w.window_id, back, enter=False, literal=False
        ):
            return _bail_not_advanced("nav_send_failed")
        await asyncio.sleep(NAV_SETTLE)
        vform = await _reparse_decision()
        vc = _decision_cursor(vform) if vform else None
        if (
            vform is None
            or decision_prompt_fingerprint(vform) != minted_fingerprint
            or vc is None
            or vc.number != target
        ):
            return _bail_not_advanced("verify_failed")
        landed_label = vc.label

    # (e) Verify (all shapes): the loose landing-label match (post-nav only).
    if not _loose_label_match(landed_label, option_label):
        return _bail_not_advanced("verify_failed")

    # (f) Commit — the version-stable Enter (a False return means it never reached
    # tmux → still a PRE-COMMIT bail).
    if not await tmux_manager.send_keys(
        w.window_id, "Enter", enter=False, literal=False
    ):
        return _bail_not_advanced("commit_send_failed")
    await asyncio.sleep(COMMIT_SETTLE)
    pane2 = await tmux_manager.capture_pane(w.window_id, scrollback_lines=500)
    if not pane2:
        return _bail_commit_unconfirmed("confirm_capture_failed")
    if not _classify_decision_advance(pane2, minted_fingerprint):
        return _bail_commit_unconfirmed("commit_unconfirmed")

    # CONFIRMED — record ``dispatched`` (idempotency lock), invalidate the nav
    # GENERATION in-lock (§3: a raw-nav tap landing in the lock-release→teardown
    # gap already fails ``current_nav_generation``), and release the ledger key
    # (§8: ``released`` fires ONLY on the confirmed-gone proof).
    if ledger_key is not None:
        auq_ledger.record(ledger_key, state="dispatched")
    decision_token.invalidate_on_dispatch(window_id)
    if ledger_key is not None:
        auq_ledger.release_key(ledger_key)
    logger.info(
        "DECISION dispatch_ok user=%d window=%s opt=%d label=%s",
        user.id,
        window_id,
        option_number,
        option_label[:24],
    )
    return _DecisionPaneOutcome("dispatched")


async def _dispatch_decision(
    *,
    query: Any,
    context: Any,
    user: Any,
    tmux_manager: Any,
    adapters: Any,
    w: Any,
    window_id: str,
    thread_id: int | None,
    minted_fingerprint: str,
    option_number: int,
    option_label: str,
    ledger_key: str | None,
) -> None:
    """Lock-acquire (reject-if-held) → pane-locked transaction → unlocked response.

    Reject-if-held (NOT queue): a message already queued on the send lock would
    otherwise flush a keystroke mid-transaction. On a busy lock the already-written
    ``accepted`` row is DOWNGRADED to ``not_advanced`` (Enter provably never sent)
    and the callback falls through to a fresh-mint re-render — never stranding a
    crash-ambiguous ``accepted``.
    """
    lock = _window_send_lock(tmux_manager, w.window_id)
    if _lock_busy(lock):
        if ledger_key is not None:
            auq_ledger.record(
                ledger_key, state="not_advanced", failed_reason="lock_busy"
            )
        await safe_answer(query, WINDOW_BUSY_TEXT)
        await _rerender_picker(
            context, user, tmux_manager, adapters, window_id, thread_id
        )
        return
    async with lock:
        outcome = await _dispatch_decision_pane_locked(
            user=user,
            tmux_manager=tmux_manager,
            w=w,
            window_id=window_id,
            minted_fingerprint=minted_fingerprint,
            option_number=option_number,
            option_label=option_label,
            ledger_key=ledger_key,
        )
    # ── Unlocked response: Telegram I/O only after lock release. ──
    if outcome.kind == "dispatched":
        # §5b(b)/§3 ORDERING (review r1 P2-C — the plan text is normative):
        # FIRST the dispatch-terminal teardown — pop the persisted interactive
        # surface (a stale raw-nav tap then fails ``has_interactive_surface``,
        # restart-safe) and edit the card into the inert "✅ … sent" final state —
        # THEN answer the callback. Answering first left a crash/network window
        # where the callback was acked but the persisted surface was not yet
        # terminal (a restart inside it would rehydrate a live-looking card for
        # a committed dispatch). The nav generation was already invalidated
        # IN-LOCK (§3), covering the lock-release→teardown gap either way.
        await interactive_ui.finalize_decision_dispatch(
            context.bot,
            user.id,
            thread_id,
            window_id,
            option_label,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query, f"✅ {option_label[:32]}")
        return
    if outcome.kind == "not_advanced":
        await safe_answer(query, "Action not registered; refreshing card.")
    else:  # commit_unconfirmed
        await safe_answer(query, "Action sent; refreshing card.")
    await _rerender_picker(context, user, tmux_manager, adapters, window_id, thread_id)


def _parse_nav_payload(raw: str) -> tuple[str, int | None]:
    """Split an ``aq:*`` nav callback payload into ``(window_id, generation)``.

    Gate cards (Decision / Permission / Workflow — §5b(c) + owner decision O-6)
    carry a generation-suffixed shape ``@N:g<gen>``; AUQ / EPM keep the legacy
    un-suffixed ``@N`` (byte-neutral, the non-regressive constraint). Window ids
    are ``@N`` (colon-free), so the trailing ``:g<digits>`` is unambiguous. Parsed
    BEFORE ``reject_stale_window_callback`` (round-4 guardrail 1: slicing the
    whole payload as a window id would treat ``@12:g7`` as a window and dead-tap).
    A malformed / absent suffix → ``None``; ``assert_nav_dispatchable`` still fails
    a live GATE surface closed on a missing generation.
    """
    idx = raw.rfind(":g")
    if idx != -1:
        tail = raw[idx + 2 :]
        if tail.isdigit():
            return raw[:idx], int(tail)
    return raw, None


async def execute_interactive_callback(authorized: Any, adapters: Any) -> None:
    update = authorized.ctx.update
    context = authorized.ctx.context
    user = authorized.ctx.user
    query = authorized.ctx.query
    data = authorized.command.data
    cb_thread_id = authorized.ctx.thread_id
    lease = window_lease(authorized, adapters)
    tmux_manager = adapters.tmux_manager

    async def reject_stale_window_callback(window_id: str) -> bool:
        return await lease.reject_stale_window(window_id)

    async def reject_invalid_pending_picker(
        expected_states: tuple[str, ...],
        answer_text: str,
    ) -> tuple[bool, int | None]:
        ok, pending_tid, _reason = _validate_pending_picker_callback(
            context.user_data,
            cb_thread_id,
            expected_states,
        )
        if ok:
            return False, pending_tid
        await _answer_invalid_pending_picker_callback(query, answer_text)
        return True, pending_tid

    # Interactive UI: Up arrow
    if data.startswith(CB_ASK_UP):
        window_id, nav_gen = _parse_nav_payload(data[len(CB_ASK_UP) :])
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager, gen=nav_gen
        )
        if w is None:
            return
        nav_ok = await _control_key_or_busy(query, tmux_manager, w.window_id, "Up")
        if nav_ok is None:
            return
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Up",
            nav_ok,
        )
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query)

    # Interactive UI: Down arrow
    elif data.startswith(CB_ASK_DOWN):
        window_id, nav_gen = _parse_nav_payload(data[len(CB_ASK_DOWN) :])
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager, gen=nav_gen
        )
        if w is None:
            return
        nav_ok = await _control_key_or_busy(query, tmux_manager, w.window_id, "Down")
        if nav_ok is None:
            return
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Down",
            nav_ok,
        )
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query)

    # Interactive UI: Left arrow
    elif data.startswith(CB_ASK_LEFT):
        window_id, nav_gen = _parse_nav_payload(data[len(CB_ASK_LEFT) :])
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager, gen=nav_gen
        )
        if w is None:
            return
        nav_ok = await _control_key_or_busy(query, tmux_manager, w.window_id, "Left")
        if nav_ok is None:
            return
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Left",
            nav_ok,
        )
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query)

    # Interactive UI: Right arrow
    elif data.startswith(CB_ASK_RIGHT):
        window_id, nav_gen = _parse_nav_payload(data[len(CB_ASK_RIGHT) :])
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager, gen=nav_gen
        )
        if w is None:
            return
        nav_ok = await _control_key_or_busy(query, tmux_manager, w.window_id, "Right")
        if nav_ok is None:
            return
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Right",
            nav_ok,
        )
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query)

    # Interactive UI: Escape
    elif data.startswith(CB_ASK_ESC):
        window_id, nav_gen = _parse_nav_payload(data[len(CB_ASK_ESC) :])
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        # F2: ESC carve-out. On a stale picker, still reap the Telegram card.
        w = await assert_nav_dispatchable(
            query,
            user.id,
            thread_id,
            window_id,
            tmux_mgr=tmux_manager,
            is_esc=True,
            gen=nav_gen,
        )
        if w == NAV_ESC_CLEAR:
            await clear_interactive_msg(
                user.id, context.bot, thread_id, session_mgr=adapters.session_manager
            )
            await safe_answer(query, "⎋ Esc")
            return
        if w is None:
            return
        nav_ok = await _control_key_or_busy(query, tmux_manager, w.window_id, "Escape")
        if nav_ok is None:
            return
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Escape",
            nav_ok,
        )
        await clear_interactive_msg(
            user.id, context.bot, thread_id, session_mgr=adapters.session_manager
        )
        await safe_answer(query, "⎋ Esc")

    # Interactive UI: Enter
    elif data.startswith(CB_ASK_ENTER):
        window_id, nav_gen = _parse_nav_payload(data[len(CB_ASK_ENTER) :])
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager, gen=nav_gen
        )
        if w is None:
            return
        nav_ok = await _control_key_or_busy(query, tmux_manager, w.window_id, "Enter")
        if nav_ok is None:
            return
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Enter",
            nav_ok,
        )
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query, "⏎ Enter")

    # Interactive UI: Space
    elif data.startswith(CB_ASK_SPACE):
        window_id, nav_gen = _parse_nav_payload(data[len(CB_ASK_SPACE) :])
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager, gen=nav_gen
        )
        if w is None:
            return
        nav_ok = await _control_key_or_busy(query, tmux_manager, w.window_id, "Space")
        if nav_ok is None:
            return
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Space",
            nav_ok,
        )
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query, "␣ Space")

    # Interactive UI: Tab
    elif data.startswith(CB_ASK_TAB):
        window_id, nav_gen = _parse_nav_payload(data[len(CB_ASK_TAB) :])
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager, gen=nav_gen
        )
        if w is None:
            return
        nav_ok = await _control_key_or_busy(query, tmux_manager, w.window_id, "Tab")
        if nav_ok is None:
            return
        logger.info(
            "AUQ_TAP nav_dispatch user=%d window=%s key=%s send_keys_ok=%s",
            user.id,
            window_id,
            "Tab",
            nav_ok,
        )
        await asyncio.sleep(0.5)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query, "⇥ Tab")

    # Interactive UI: refresh display (F1: included in the nav-guard family)
    elif data.startswith(CB_ASK_REFRESH):
        window_id, nav_gen = _parse_nav_payload(data[len(CB_ASK_REFRESH) :])
        thread_id = _get_thread_id(update)
        if await reject_stale_window_callback(window_id):
            return
        w = await assert_nav_dispatchable(
            query, user.id, thread_id, window_id, tmux_mgr=tmux_manager, gen=nav_gen
        )
        if w is None:
            return
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query, "🔄")

    # Interactive UI: multi-select toggle (digit-only, no ledger, token not consumed)
    elif data.startswith(CB_ASK_TOGGLE):
        payload = data[len(CB_ASK_TOGGLE) :]
        parts = payload.split(":")
        if len(parts) != 4:
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return
        _route_hash, _fp8, opt_str, token = parts
        try:
            opt_num = int(opt_str)
        except ValueError:
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return

        entry = pick_token.peek(token)
        if entry is None:
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return
        thread_id = entry.thread_id
        window_id = entry.window_id
        if not owner_matches(entry, user.id):
            await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
            return
        if opt_num != entry.option_number:
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
                fallback_window_id=window_id,
            )
            return
        if await reject_stale_window_callback(window_id):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await safe_answer(query, "Window not found", show_alert=True)
            return

        pane = await tmux_manager.capture_pane(w.window_id, scrollback_lines=500)
        # Source-stickiness: re-resolve using the SAME source this toggle button
        # was minted against, if it is still live + unchanged. A transient pane
        # degradation can make resolve_auq_source flip side_file→pane at tap;
        # that flip changes the resolved form's fingerprint and silently rejects
        # the toggle. Pinning the minted source keeps the toggle dispatching as
        # long as the underlying question hasn't actually changed (a replaced
        # side file has a different canonical fingerprint → no pin → fall back).
        sticky_input = auq_source.peek_sticky_source(
            window_id, entry.source_kind, entry.source_fingerprint
        )
        if sticky_input is not None:
            resolved_input = sticky_input
            # peek_sticky_source only returns non-None for the side_file /
            # jsonl_cache kinds (it returns None for "pane"), so the minted
            # kind here is always a valid ResolvedAuqSource.kind literal.
            resolved_src = auq_source.ResolvedAuqSource(
                kind=cast(Literal["side_file", "jsonl_cache"], entry.source_kind),
                payload=sticky_input,
                source_fingerprint=entry.source_fingerprint,
            )
        else:
            resolved_src = auq_source.resolve_auq_source(window_id, None, pane or "")
            resolved_input = resolved_src.payload
        current_form = (
            adapters.terminal_parser.resolve_ask_form(resolved_input, pane)
            if pane
            else None
        )
        if (
            current_form is None
            or current_form.fingerprint() != entry.fingerprint
            or current_form.select_mode != "multi"
            or not current_form.options_complete
        ):
            logger.info(
                "AUQ_TAP toggle_reject user=%d window=%s opt=%d minted_fp=%s live_fp=%s "
                "reason_form_none=%s reason_fp=%s reason_mode=%s reason_incomplete=%s "
                "minted_src=%s live_src=%s minted_src_fp=%s live_src_fp=%s "
                "live_sel_mode=%s live_opts_complete=%s live_cursor=%s live_selected=%s",
                user.id,
                window_id,
                entry.option_number,
                entry.fingerprint[:8],
                current_form.fingerprint()[:8] if current_form else "none",
                current_form is None,
                bool(
                    current_form is not None
                    and current_form.fingerprint() != entry.fingerprint
                ),
                bool(current_form is not None and current_form.select_mode != "multi"),
                bool(current_form is not None and not current_form.options_complete),
                entry.source_kind,
                resolved_src.kind,
                entry.source_fingerprint[:8],
                resolved_src.source_fingerprint[:8],
                current_form.select_mode if current_form else "none",
                current_form.options_complete if current_form else "none",
                [o.number for o in current_form.options if o.cursor]
                if current_form
                else None,
                {o.number: o.selected for o in current_form.options}
                if current_form
                else None,
            )
            await safe_answer(query, "Form changed, refreshing.", show_alert=False)
            await handle_interactive_ui(
                context.bot,
                user.id,
                window_id,
                thread_id,
                tmux_mgr=tmux_manager,
                session_mgr=adapters.session_manager,
            )
            return

        toggle_ok = await _control_key_or_busy(
            query, tmux_manager, w.window_id, str(entry.option_number), literal=True
        )
        if toggle_ok is None:
            return
        if not toggle_ok:
            logger.warning(
                "Toggle-token dispatch: tmux send_keys(digit=%d) returned False for window=%s user=%d",
                entry.option_number,
                window_id,
                user.id,
            )
            await safe_answer(query, "toggle failed; refreshing", show_alert=False)
            await handle_interactive_ui(
                context.bot,
                user.id,
                window_id,
                thread_id,
                tmux_mgr=tmux_manager,
                session_mgr=adapters.session_manager,
            )
            return

        logger.info(
            "AUQ_TAP toggle_dispatch_ok user=%d window=%s opt=%d send_keys_ok=%s "
            "minted_fp=%s live_fp=%s minted_src=%s live_src=%s "
            "live_sel_mode=%s live_opts_complete=%s live_cursor=%s live_selected=%s",
            user.id,
            window_id,
            entry.option_number,
            toggle_ok,
            entry.fingerprint[:8],
            current_form.fingerprint()[:8],
            entry.source_kind,
            resolved_src.kind,
            current_form.select_mode,
            current_form.options_complete,
            [o.number for o in current_form.options if o.cursor],
            {o.number: o.selected for o in current_form.options},
        )
        await asyncio.sleep(0.3)
        await handle_interactive_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            tmux_mgr=tmux_manager,
            session_mgr=adapters.session_manager,
        )
        await safe_answer(query, f"Toggled {entry.option_number}")

    # Interactive UI: structured option pick (PR 2b + Wave 3 ledger)
    elif data.startswith(CB_ASK_PICK):
        payload = data[len(CB_ASK_PICK) :]
        parts = payload.split(":")
        # Parse shape:
        #   len == 4 → keyed ``aqp:<route_hash>:<fp8>:<opt>:<token>``;
        #              the leading triplet feeds the restart-safe ledger.
        #   anything else → malformed → refresh card.
        # ``ledger_key`` stays ``str | None`` because the collision-suppression
        # paths below (wrong-user/live-token collision and same-user route/window
        # drift) reset it to ``None`` to avoid clobbering another route's row.
        ledger_key: str | None = None
        token: str
        if len(parts) == 4:
            route_hash, fp8, opt_str, token = parts
            try:
                opt_num = int(opt_str)
            except ValueError:
                logger.info("AUQ_PICK malformed user=%d", user.id)
                await _refresh_pick_card(
                    query,
                    context,
                    update,
                    user,
                    tmux_manager,
                    adapters,
                    text="Card expired, refreshing.",
                )
                return
            ledger_key = auq_ledger.make_ledger_key(route_hash, fp8, opt_num)
            logger.info(
                "AUQ_PICK entry user=%d window=%s opt=%d fp8=%s token=%s",
                user.id,
                "?",
                opt_num,
                fp8,
                token[:6],
            )
        else:
            logger.info("AUQ_PICK malformed user=%d", user.id)
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return

        # Ledger lookup FIRST (restart recovery). Wave 3 §7.2 contract:
        # ledger consulted BEFORE token validate so a post-restart duplicate
        # tap can be detected even when the in-memory pick-token store has
        # been wiped.
        existing = auq_ledger.lookup(ledger_key)

        # v4 §7.2 owner-mismatch handling. Could be (a) wrong-user replay
        # (owner already dispatched; another user in the topic clicks the
        # same callback_data); or (b) legitimate live-token collision (two
        # routes hashed to the same triplet AND the clicker owns a live
        # pick token for the same stable key). Distinguish by peeking the
        # current user's live token: if it reconstructs the same key, this
        # is collision → clear ledger gate for this click, fall through to
        # the in-process token path. Otherwise wrong-user → reject.
        if existing is not None and existing.user_id != user.id:
            live = pick_token.peek(token)
            is_collision = (
                live is not None
                and live.user_id == user.id
                and ledger_key is not None
                and pick_token.stable_key(live) == ledger_key
            )
            if not is_collision:
                logger.info("AUQ_PICK wrong_user user=%d window=%s", user.id, "?")
                await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
                return
            # Plan v4 §7.2: "ledger entry from the other route stays put
            # (its owner can still see 'Action already received' on
            # retry)." Drop both the local gate AND the ledger_key so the
            # follow-up dispatch writes go to nothing — otherwise the
            # accepted/digit_sent/dispatched writes below would overwrite
            # the owner's row at the same key.
            existing = None
            ledger_key = None

        # Same-user defensive collision check: the route_hash matches but
        # the stored window_id differs from this route's current binding.
        # The hashes can collide across (user, thread, window) triplets;
        # if the bound window has drifted, treat the ledger row as a
        # collision, fall through to the token path, and likewise drop
        # ledger_key so this dispatch doesn't clobber a row that legitimately
        # belongs to a different window's lifecycle.
        if existing is not None:
            bound_window = get_interactive_window(user.id, _get_thread_id(update))
            if bound_window and existing.window_id != bound_window:
                existing = None
                ledger_key = None

        # Apply the §7.1 per-state behavior matrix.
        if existing is not None:
            proj_state = existing.state
            if (
                existing.state in ("accepted", "digit_sent")
                and existing.accepted_at < auq_ledger.process_start_time()
            ):
                proj_state = "unknown"
            logger.info(
                "AUQ_PICK ledger_hit user=%d window=%s opt=%d proj_state=%s raw_state=%s",
                user.id,
                existing.window_id,
                existing.option_number,
                proj_state,
                existing.state,
            )
            if proj_state == "dispatched":
                await safe_answer(
                    query,
                    f"Action already received: {existing.option_label[:32]}",
                    show_alert=False,
                )
                return
            if proj_state in ("accepted", "digit_sent"):
                await safe_answer(query, "Action in progress", show_alert=False)
                return
            if proj_state == "unknown":
                await _refresh_pick_card(
                    query,
                    context,
                    update,
                    user,
                    tmux_manager,
                    adapters,
                    text="Action interrupted; please re-tap.",
                    fallback_window_id=existing.window_id,
                )
                return
            if proj_state == "failed_before_digit":
                await _refresh_pick_card(
                    query,
                    context,
                    update,
                    user,
                    tmux_manager,
                    adapters,
                    text="Action failed previously; refreshing.",
                    fallback_window_id=existing.window_id,
                )
                return
            if proj_state == "failed_after_digit":
                await _refresh_pick_card(
                    query,
                    context,
                    update,
                    user,
                    tmux_manager,
                    adapters,
                    text=("Action sent but interrupted; refreshing — verify in tmux."),
                    fallback_window_id=existing.window_id,
                )
                return
            if proj_state == "commit_unconfirmed":
                # ``Enter`` WAS sent for this key but the advance was never
                # confirmed. A re-tap must NEVER re-send the commit key, so this
                # REFRESHES ONLY and returns — the re-render shows the live pane
                # (caught-up next question, or genuinely stuck → the orthogonal
                # un-ledgered ``⏎ Enter`` nav button is the user's escape).
                await _refresh_pick_card(
                    query,
                    context,
                    update,
                    user,
                    tmux_manager,
                    adapters,
                    text="Action sent; refreshing card.",
                    fallback_window_id=existing.window_id,
                )
                return
            if proj_state == "not_advanced":
                # A PRE-COMMIT bail (``Enter`` provably never sent → nothing
                # committed). FALL THROUGH (no return) so a fresh-token re-tap
                # re-validates against the live form and retries the navigate +
                # commit — safe because the commit key was never sent for this key.
                logger.info(
                    "AUQ_PICK ledger_not_advanced fallthrough user=%d window=%s opt=%d",
                    user.id,
                    existing.window_id,
                    existing.option_number,
                )

        # R4: side-effect-free peek to read entry.window_id (and entry.user_id
        # for the wrong-user gate). The pane capture, source/form re-resolve,
        # and single-use consume all move INSIDE
        # pick_token.validate_and_consume (atomic by exclusive reservation).
        # The stale-window lease check stays here — it needs safe_answer — and
        # fires BEFORE validate_and_consume so a stale-window tap never reserves
        # or burns the owner's token.
        peeked = pick_token.peek(token)
        if peeked is None:
            # Token never existed, was already used, aged past the TTL, or — the
            # D2 case — was wiped by a bot RESTART while the published card kept
            # its old keyboard (dead token strings baked into callback_data). Try
            # restart-recovery first; if it doesn't take over, refresh the card so
            # the user taps a fresh button. (The ledger gate above already
            # answered any real SEQUENTIAL duplicate.)
            logger.info("AUQ_PICK peek_none user=%d token=%s", user.id, token[:6])
            if await _attempt_pick_recovery(
                token,
                user.id,
                route_hash,
                fp8,
                opt_num,
                query=query,
                context=context,
                user=user,
                tmux_manager=tmux_manager,
                adapters=adapters,
                reject_stale_window=reject_stale_window_callback,
            ):
                return
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="↻ Refreshed — tap your choice again.",
                show_alert=True,
            )
            return
        thread_id = peeked.thread_id
        window_id = peeked.window_id
        # Wrong-user gate, BEFORE the lease check — preserves the authorization
        # invariant (a shared-topic intruder gets WRONG_USER_PICK_TEXT, never
        # the option label or a stale-window message) and matches the prior
        # owner-before-lease ordering. Side-effect-free: no reserve, no consume,
        # so it cannot burn the owner's token. validate_and_consume's own phase
        # (a) owner check is the authoritative, race-safe re-check.
        if not owner_matches(peeked, user.id):
            logger.info("AUQ_PICK wrong_user user=%d window=%s", user.id, window_id)
            await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
            return
        if await reject_stale_window_callback(window_id):
            logger.info("AUQ_PICK stale_window user=%d window=%s", user.id, window_id)
            return

        # Atomic validate + single-use consume. Re-resolves the AUQ source via
        # the SAME auq_source.resolve_auq_source the minter used (measurable
        # source parity), re-parses the live pane (fingerprint staleness), and
        # wins-or-loses the consume by exclusive reservation — without holding
        # the store lock across capture_pane / find_window_by_id. Capture with
        # the SAME 500-line scrollback as the render path so the validate pane
        # slice matches the mint pane slice (a smaller capture would shift
        # current_tab_inferred / options and bounce long pickers).
        # GH #54 capture spine (seam 3, the PRIMARY live tap path — wave-2 review
        # P1): the frame is captured WITH ANSI so pick_token's re-parse can prove
        # the SGR tier-2 cursor on a chevron-less preview picker — a plain frame
        # here parsed NO cursor while the rendered form had one, so the token was
        # CONSUMED and the dispatch then bailed ``cursor_unknown`` (a dead tap on
        # every preview pick). ``with_ansi=False`` is pick_token's one-plain-
        # recapture request on a normalize rejection (P2-A).
        async def _capture(
            wid: str, scrollback: int, with_ansi: bool = True
        ) -> str | None:
            return await tmux_manager.capture_pane(
                wid, with_ansi=with_ansi, scrollback_lines=scrollback
            )

        result = await pick_token.validate_and_consume(
            token,
            user.id,
            capture_pane=_capture,
            find_window_by_id=tmux_manager.find_window_by_id,
        )
        entry = result.entry
        current_form = result.current_form
        logger.info(
            "AUQ_PICK validate user=%d window=%s opt=%d outcome=%s is_review_submit=%s",
            user.id,
            window_id,
            peeked.option_number,
            result.outcome,
            peeked.is_review_submit,
        )
        if result.outcome == "wrong_user":
            await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
            return
        if result.outcome == "already_consumed":
            # In-flight CONCURRENT duplicate (a second tap arrived while the
            # first held the reservation, or a losing sibling whose row was
            # tombstoned). The sequential duplicate was already answered by the
            # ledger gate above; this is the concurrent-race UX.
            await safe_answer(query, "Action already received.", show_alert=False)
            return
        if result.outcome == "expired":
            # A token that survived peek but lost the consume race / TTL-pruned
            # mid-flight. Same restart-recovery net as peek_none (gated identically
            # — a tombstoned/live row declines via the cache-row proof).
            if await _attempt_pick_recovery(
                token,
                user.id,
                route_hash,
                fp8,
                opt_num,
                query=query,
                context=context,
                user=user,
                tmux_manager=tmux_manager,
                adapters=adapters,
                reject_stale_window=reject_stale_window_callback,
            ):
                return
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="↻ Refreshed — tap your choice again.",
                show_alert=True,
                fallback_window_id=window_id,
            )
            return
        if result.outcome == "window_gone":
            await safe_answer(query, "Window not found", show_alert=True)
            return
        if result.outcome in ("stale_form", "source_drift"):
            logger.info(
                "Pick-token %s reject: user=%d window=%s opt=%d minted_fp=%s",
                result.outcome,
                user.id,
                window_id,
                peeked.option_number,
                peeked.fingerprint,
            )
            await safe_answer(query, "Form changed, refreshing.", show_alert=False)
            await handle_interactive_ui(
                context.bot,
                user.id,
                window_id,
                thread_id,
                tmux_mgr=tmux_manager,
                session_mgr=adapters.session_manager,
            )
            return
        # outcome == "ok": entry + current_form are present (validate_and_consume
        # hands the live re-parse back on a winning consume).
        assert entry is not None and current_form is not None
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await safe_answer(query, "Window not found", show_alert=True)
            return

        # Submit-button guardrail: a click flagged ``is_review_submit`` only
        # fires when the live parse still says we're on the review screen with
        # the literal "Submit answers" row as option 1 AND a matching minted
        # label — CURSOR-BLIND. The digit `1` activates Submit regardless of the
        # terminal cursor (verified on Claude Code v2.1.161), so we no longer
        # require the cursor on Submit; the review-screen + option#1 + literal
        # "Submit answers" + minted-label anchors mean a non-review screen, a
        # relabeled Submit, or a reordered review layout all SAFELY DECLINE
        # rather than dispatching the wrong action.
        if entry.is_review_submit and not current_form.review_submit_dispatchable(
            entry.option_label
        ):
            logger.info(
                "AUQ_PICK submit_guard_reject user=%d window=%s",
                user.id,
                window_id,
            )
            await safe_answer(
                query, "Review screen moved, refreshing.", show_alert=False
            )
            await handle_interactive_ui(
                context.bot,
                user.id,
                window_id,
                thread_id,
                tmux_mgr=tmux_manager,
                session_mgr=adapters.session_manager,
            )
            return

        # Write-ahead ledger BEFORE dispatch. ``ledger_key`` is None ONLY on a
        # collision-suppression fall-through (set above at the wrong-user/
        # live-token and same-user window-drift checks): the ledger row belongs
        # to a DIFFERENT route, so we must NOT write to that key here or we'd
        # clobber the rightful owner's lifecycle. These guards therefore
        # protect collision suppression — do not remove them. (The legacy
        # one-part ``aqp:<token>`` callback shape that also used to leave
        # ledger_key None was removed in Wave 4; only the collision path
        # remains.)
        if ledger_key is not None:
            auq_ledger.record(
                ledger_key,
                state="accepted",
                user_id=user.id,
                window_id=window_id,
                full_fingerprint=entry.fingerprint,
                option_number=entry.option_number,
                option_label=entry.option_label,
            )

        # Navigate the live cursor to the tapped option, verify, Enter, and
        # confirm the advance before recording ``dispatched`` (v2.1.168 model) —
        # via the shared helper (also used by D2 restart-recovery). The
        # ``accepted`` claim was already written above. ``current_form`` is the
        # live re-parse handed back by ``validate_and_consume``.
        await _dispatch_pick(
            query=query,
            context=context,
            user=user,
            tmux_manager=tmux_manager,
            adapters=adapters,
            w=w,
            window_id=window_id,
            thread_id=thread_id,
            fingerprint=entry.fingerprint,
            option_number=entry.option_number,
            option_label=entry.option_label,
            is_review_submit=entry.is_review_submit,
            current_form=current_form,
            ledger_key=ledger_key,
        )

    # Interactive UI: Stage B2 tappable Decision option pick (dcp: lane)
    elif data.startswith(CB_DECISION_PICK):
        # (0) dispatch-flag check — a stale flag-ON-epoch button after a config
        # flip declines cleanly (the nav keys still work).
        if not decision_token.decision_dispatch_enabled():
            await safe_answer(query, "Dispatch disabled — use the nav keys.")
            return
        payload = data[len(CB_DECISION_PICK) :]
        parts = payload.split(":")
        if len(parts) != 4:
            logger.info("DECISION_PICK malformed user=%d", user.id)
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return
        d_route_hash, d_fp8, d_opt_str, d_token = parts
        try:
            d_opt_num = int(d_opt_str)
        except ValueError:
            logger.info("DECISION_PICK malformed user=%d", user.id)
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="Card expired, refreshing.",
            )
            return
        d_ledger_key: str | None = auq_ledger.make_ledger_key(
            d_route_hash, d_fp8, d_opt_num
        )

        # (1) Ledger lookup FIRST (§8 restart-safe duplicate idempotency), then
        # the AUQ collision matrix (copied): owner-mismatch → live-token-peek
        # collision test → else WRONG_USER BEFORE any state text leaks.
        d_existing = auq_ledger.lookup(d_ledger_key)
        if d_existing is not None and d_existing.user_id != user.id:
            live = decision_token.peek(d_token)
            is_collision = (
                live is not None
                and live.user_id == user.id
                and _decision_stable_key(live) == d_ledger_key
            )
            if not is_collision:
                logger.info("DECISION_PICK wrong_user user=%d", user.id)
                await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
                return
            d_existing = None
            d_ledger_key = None
        # Same-user window-drift: the route_hash collided but the bound window
        # differs — drop the row + key so this dispatch never clobbers it.
        if d_existing is not None:
            d_bound = get_interactive_window(user.id, _get_thread_id(update))
            if d_bound and d_existing.window_id != d_bound:
                d_existing = None
                d_ledger_key = None

        # Per-state matrix (mirrors aqp:, minus the legacy digit states).
        if d_existing is not None:
            d_proj = d_existing.state
            if (
                d_existing.state == "accepted"
                and d_existing.accepted_at < auq_ledger.process_start_time()
            ):
                d_proj = "unknown"
            logger.info(
                "DECISION_PICK ledger_hit user=%d window=%s opt=%d proj=%s raw=%s",
                user.id,
                d_existing.window_id,
                d_existing.option_number,
                d_proj,
                d_existing.state,
            )
            if d_proj == "dispatched":
                await safe_answer(
                    query,
                    f"Action already received: {d_existing.option_label[:32]}",
                )
                return
            if d_proj == "accepted":
                await safe_answer(query, "Action in progress")
                return
            if d_proj in ("unknown", "commit_unconfirmed"):
                # ``unknown`` = a pre-restart in-flight claim; ``commit_unconfirmed``
                # = Enter WAS sent but unconfirmed → never auto-redispatch. Both
                # REFRESH ONLY.
                await _refresh_pick_card(
                    query,
                    context,
                    update,
                    user,
                    tmux_manager,
                    adapters,
                    text=(
                        "Action interrupted; please re-tap."
                        if d_proj == "unknown"
                        else "Action sent; refreshing card."
                    ),
                    fallback_window_id=d_existing.window_id,
                )
                return
            # ``not_advanced`` → FALL THROUGH (a fresh-token re-tap re-validates;
            # Enter provably never sent).

        # (2) token peek → owner → stale-window lease.
        d_peeked = decision_token.peek(d_token)
        if d_peeked is None:
            # No durable D2 recovery for Decision — it re-mints from the live pane
            # trivially (§8). Refresh so the user taps a fresh button.
            logger.info(
                "DECISION_PICK peek_none user=%d token=%s", user.id, d_token[:6]
            )
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="↻ Refreshed — tap your choice again.",
                show_alert=True,
            )
            return
        d_thread_id = d_peeked.thread_id
        d_window_id = d_peeked.window_id
        if not owner_matches(d_peeked, user.id):
            logger.info(
                "DECISION_PICK wrong_user user=%d window=%s", user.id, d_window_id
            )
            await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
            return
        if await reject_stale_window_callback(d_window_id):
            logger.info(
                "DECISION_PICK stale_window user=%d window=%s", user.id, d_window_id
            )
            return

        # (3) Consume by EXCLUSIVE RESERVATION (§3(3) sibling-burn — the winning
        # consume tombstones the whole route row, so a losing/late sibling or a
        # Telegram replay finds only a tomb).
        d_consume = await decision_token.consume(d_token, user.id)
        logger.info(
            "DECISION_PICK consume user=%d window=%s opt=%d outcome=%s",
            user.id,
            d_window_id,
            d_peeked.option_number,
            d_consume.outcome,
        )
        if d_consume.outcome == "wrong_user":
            await safe_answer(query, WRONG_USER_PICK_TEXT, show_alert=True)
            return
        if d_consume.outcome == "already_consumed":
            await safe_answer(query, "Action already received.")
            return
        if d_consume.outcome == "expired":
            await _refresh_pick_card(
                query,
                context,
                update,
                user,
                tmux_manager,
                adapters,
                text="↻ Refreshed — tap your choice again.",
                show_alert=True,
                fallback_window_id=d_window_id,
            )
            return
        d_entry = d_consume.entry
        assert d_entry is not None
        d_w = await tmux_manager.find_window_by_id(d_window_id)
        if not d_w:
            await safe_answer(query, "Window not found", show_alert=True)
            return

        # (4) Write-ahead ledger ``accepted`` BEFORE dispatch (None only on a
        # collision-suppression fall-through above — the guards keep the writes
        # off another route's row).
        if d_ledger_key is not None:
            auq_ledger.record(
                d_ledger_key,
                state="accepted",
                user_id=user.id,
                window_id=d_window_id,
                full_fingerprint=d_entry.fingerprint,
                option_number=d_entry.option_number,
                option_label=d_entry.option_label,
            )

        # (5) Navigate → verify → Enter → confirm (§3), the reject-if-held lock.
        await _dispatch_decision(
            query=query,
            context=context,
            user=user,
            tmux_manager=tmux_manager,
            adapters=adapters,
            w=d_w,
            window_id=d_window_id,
            thread_id=d_thread_id,
            minted_fingerprint=d_entry.fingerprint,
            option_number=d_entry.option_number,
            option_label=d_entry.option_label,
            ledger_key=d_ledger_key,
        )
