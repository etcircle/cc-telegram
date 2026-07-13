"""Free-text answers on a live AskUserQuestion card (GH #50 PR-2).

PR-1 made every inbound payload REFUSE at a live blocking surface (a message
typed at an AskUserQuestion picker would have had its Enter commit option 1).
That is correct but a dead end: the AUQ card literally invites the user to
"send a regular message to free-text". PR-2 makes that invitation TRUE for the
ONE surface it ships for:

    AskUserQuestion (single-select)   row N+1  ``Type something.``

The executor NAVIGATES to that row, VERIFIES it landed **on the same card it
planned against**, TYPES the payload with the Enter withheld, VERIFIES the typed
state **and the card identity again**, fires the pre-commit user-turn stamp, and
only then presses Enter. The prose becomes the card's ANSWER.

SCOPE (owner decision 2026-07-12): **ExitPlanMode is OUT.** An earlier revision
drove EPM's own affordance row (row 4, ``Tell Claude what to change``) too. It
worked, but its safety rested entirely on a NEW ``PreToolUse(ExitPlanMode)``
hook + side file, because nothing else can name a plan prompt (every EPM renders
the same three real options and the plan-file path is a per-session slug Claude
rewrites in place). The owner runs ``--dangerously-skip-permissions`` anyway, so
hardening a plan-approval surface bought little for a whole hook + state file +
trust boundary. It is REMOVED, not disabled — no vestigial surface constant, no
half-wired lane. **An ExitPlanMode card therefore falls through to PR-1's gate,
which REFUSES the message with its normal actionable copy: a plan card cannot be
answered in prose.** That is the intended, safe degradation. (The pre-PR-2 EPM
machinery — the 📋 plan-body post, ``extract_epm_plan_file_path``, the EPM
interactive card — is untouched.)

**THE GUARD IS THE PRE-TYPE LANDING PROOF, AND SAYING SO PLAINLY IS THE POINT.**
Before a single byte is written, the row under the cursor must satisfy ALL THREE
of: ``cursor`` is on it, its label is EXACTLY ``Type something.``, and that label
is SGR-2 **DIM**. ``dim == True`` holds for exactly ONE shape on a picker — the
SELECTED, UNTYPED placeholder — and a real option row is NEVER dim, not even when
it is the selected row. That is what makes an option commit UNREACHABLE from this
lane, and it is rig-measured, not argued (2026-07-12):

  * an OVERSHOOT that parks the cursor on a real option ⇒ DECLINED (not dim);
  * an UNDERSHOOT that parks on a real option at ``target_row`` ⇒ DECLINED;
  * a payload ``Yes, but use postgres`` against a card whose option 1 is literally
    ``Yes`` ⇒ DECLINED (the label is not the placeholder, and it is not dim);
  * ``Down`` CLAMPS on 2.1.207 (it never wraps), and the nav is ``Down``-only by
    construction (the affordance row is the LAST row, so ``delta >= 0``), so the
    wrap-to-option-1 hazard is unreachable from here;
  * typing while parked on a real option row is SWALLOWED entirely — the pane is
    byte-identical afterwards; there is no auto-jump and no insertion.

THE POST-WRITE LEGS ARE CORROBORATION, NOT DEFENCE IN DEPTH — three of them are
MEASURABLY WEAK, and the code must not pretend otherwise:

  * ``terminal_parser.auq_free_text_row_active`` (the ``ctrl+g`` footer hint) is
    NOT an exact row proof: the hint is ALSO present on the ``N+2. Chat about
    this`` row. It proves "the cursor is on SOME text-field row", never which.
  * :func:`payload_tail_landed` is a WHOLE-PANE substring check and can pass
    SPURIOUSLY (rig: it matched prose echoed in the transcript scrollback from a
    previous answer, on a card that had received nothing).
  * the SGR-2 flip read POST-write (``dim is False``) PASSES on a real option row,
    and ``_label_is_our_draft("Yes", "Yes, but use postgres")`` is True. It is a
    consistency check, not a guard.

WHICH CARD — the identity, and its ACCEPTED RESIDUAL. ``SurfaceIdentity`` is
captured before the first key and RE-CHECKED after the nav and again in the final
pre-Enter capture, so a card that turns over MID-transaction (the side file moves
⇒ the anchor changes ⇒ ``still_holds`` refuses) is caught. It carries a MANDATORY
out-of-band anchor that names the OCCURRENCE, not the shape — the
``PreToolUse(AskUserQuestion)`` side file's per-invocation ``tool_use_id``,
written by the process that is about to block, BEFORE it renders (``auq_source``).
No anchor ⇒ the lane DECLINES before any keystroke. The anchor is read STRICTLY
BEFORE every capture, each capture is SANDWICHED between two EQUAL anchor reads
(``_observe``), the session generation is embedded in it (a ``/clear`` rotation is
itself an anchor change), and the record's OPTION LABELS must AGREE with the pane
it is paired with (``auq_source.anchor_pane_agreement``).

**THE RESIDUAL (owner-accepted, deliberately NOT closed):** a SUCCESSOR AUQ card
with the SAME option labels, whose ``PreToolUse`` record was written BEFORE our
first observation but which had not yet DRAWN, pairs card A's pane with card B's
anchor — and every later observation then agrees with that chimera. The prose
answer reaches a DIFFERENT QUESTION. It is a recoverable annoyance: the user sees
the wrong answer land immediately and answers again. **It is NOT an option
commit** — the landing proof above makes that unreachable, whatever card is on the
pane. An earlier revision grew a question-text binding (a pane question-REGION
extractor, a measured wrap column, a row-consumption walk) to close it; that
machinery failed three straight review rounds on its own injectivity and has been
DELETED. Two AUQs sharing the same labels AND the same question were never
separable on the pane anyway.

**RAW CONTROL BYTES ARE REFUSED.** ``tmux send-keys -l`` stops tmux interpreting
KEY NAMES; it does NOT make ESC/C0 bytes inert to the TUI. An embedded ``ESC [ B``
+ digit is a cursor-move plus a HOTKEY commit, fired during the write itself —
before any verification runs. ``delivery.unsafe_control_char`` is consulted here
to DECLINE (``\\n`` stays allowed: paste-shaped multi-line payloads are this
lane's primary flow); PR-1's gate owns the single refusal message.

**ALSO CARRIED FORWARD (the pre-existing GH #50 M2 residual):** a pty-level split
of a single ``send-keys -l`` could in principle land a digit as a lone HOTKEY with
no Enter. Empirically a whole multi-char payload is consumed PASTE-shaped and is
inert on a live picker, and ``delivery.lone_hotkey_line`` refuses any bare-digit
LINE outright — but this is an empirical narrowing, not a proof, and it stays on
record as a residual.

It reuses the shipped dispatch discipline (``_dispatch_pick`` /
``_dispatch_decision_pane_locked``) verbatim: per-window send lock, bounded
cancellation-safe captures, a FRESH in-lock ``pane_command_is_claude`` +
version-license re-read immediately before the first key, monotonic ``Down``-only
nav, settle → re-parse → verify, Enter as the ONLY commit key, and a strict
commit-boundary classification.

VERSION-LICENSED (the ``decision_token`` precedent, MANDATORY per plan §2.4): the
row index, the placeholder label, the SGR-2 styling and the ``ctrl+g`` footer
proof are per-CC-version TUI empirics. An unlicensed version degrades to PR-1's
refusal — honest, never a wrong keystroke.

COMMIT-BOUNDARY CLASSIFICATION (plan §2.4 [r4 P1-4]) is strict:
  - any bail BEFORE Enter with nothing written ⇒ ``NOT_WRITTEN`` (clean);
  - any bail BEFORE Enter with the payload typed ⇒ ``DRAFT_WRITTEN`` (the text
    sits in the affordance row, Enter withheld, the stranded-draft brake goes up);
  - once Enter is sent, anything unproven ⇒ ``COMMIT_UNKNOWN`` (report honestly,
    NEVER auto-retry).

Pull-only; no observer (c313657 stays forbidden).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from .. import delivery, terminal_parser
from ..delivery import DeliveryResult, UserTurnStamp
from ..tmux_manager import pane_command_is_claude, tmux_manager


if TYPE_CHECKING:  # annotation-only — ``auq_source`` reaches into ``session``
    from .auq_source import SurfaceAnchor


logger = logging.getLogger(__name__)


# ── The surface + its (surface × CC-version) license table ────────────────

SURFACE_AUQ: Final = "AskUserQuestion"

# The lane is licensed per (surface × EXACT CC version), fixture-pinned. Every CC
# upgrade empties the effective allowlist until the surface is re-characterized
# against fresh rig captures — the honest degradation the ``decision_token``
# table established. Adding a version here without re-capturing
# ``auq_freetext_*_v<version>.ansi.txt`` is the one way to make this lane
# dangerous.
#
# It stays a TABLE keyed by surface, rather than a bare version set, because the
# surface IS the unit of characterization: the row index, the placeholder label
# and the SGR-2 styling are properties of one card type on one CC release.
_FREE_TEXT_LICENSE_TABLE: Final[dict[str, frozenset[str]]] = {
    SURFACE_AUQ: frozenset({"2.1.207"}),
}


def licensed(surface: str, version: str | None) -> bool:
    """True iff ``version`` is a characterized CC version for ``surface``.

    EXACT-STRING membership (never a prefix / range): the empirics are per
    release. An unknown surface or version ⇒ False ⇒ PR-1 refusal.
    """
    if not version:
        return False
    return version.strip() in _FREE_TEXT_LICENSE_TABLE.get(surface, frozenset())


# ── The kill switch ───────────────────────────────────────────────────────
#
# A module-local bool, SEEDED from config by ``main._run_bot`` — this module
# never imports ``config`` itself (the ``decision_token`` discipline: config
# raises without a bot token, and the import-order race is a real one). It DOES
# import ``tmux_manager`` — it drives a pane, so it must.

_ENABLED: bool = True


def set_enabled(value: bool) -> None:
    """Seed the flag from config (``main._run_bot`` at startup)."""
    global _ENABLED
    _ENABLED = bool(value)


def enabled() -> bool:
    return _ENABLED


def reset_for_tests() -> None:
    global _ENABLED
    _ENABLED = True


# ── Timings (the shipped dispatch constants) ─────────────────────────────

NAV_SETTLE_S: Final = 0.5
TEXT_SETTLE_S: Final = 0.5
COMMIT_SETTLE_S: Final = 0.4
COMMIT_CONFIRM_ATTEMPTS: Final = 3
CAPTURE_DEADLINE_S: Final = 2.5
CMD_PROBE_DEADLINE_S: Final = 2.5
# The whole transaction. Generous vs PR-1's: this one navigates (up to ~N arrow
# keys), settles twice, and confirms the advance.
TRANSACTION_DEADLINE_S: Final = 25.0

_CAPTURE_TIMEOUT: Final = object()
_CMD_TIMEOUT: Final = object()


# ── Refusal reasons owned by this lane ───────────────────────────────────
#
# Declared in ``delivery`` (the ONE refusal vocabulary — its ``REFUSAL_COPY`` is
# pinned key-set-equal to ``DELIVERY_REFUSAL_REASONS`` by a strict test, so a new
# reason without copy is a build failure) and re-exported here for the executor.

# The two pre-write bail names are LOG-ONLY strings (they never surface as a
# DeliveryResult — the lane falls through and PR-1's gate owns the refusal).
REASON_NAV_FAILED: Final = "nav_send_failed"
REASON_LANDING_FAILED: Final = "landing_unproven"
REASON_VERIFY_FAILED: Final = delivery.REASON_FREE_TEXT_VERIFY_FAILED
REASON_COMMIT_UNCONFIRMED: Final = delivery.REASON_FREE_TEXT_COMMIT_UNCONFIRMED

_VERIFY_FAILED_MSG: Final = delivery.FREE_TEXT_VERIFY_FAILED_MSG
_COMMIT_UNCONFIRMED_MSG: Final = delivery.FREE_TEXT_COMMIT_UNCONFIRMED_MSG


# ── THE SURFACE IDENTITY (peer-review P1 — the wrong-card close) ─────────


@dataclass(frozen=True)
class SurfaceIdentity:
    """WHICH CARD this transaction is answering. Two components — one MANDATORY.

    The whole transaction is a race against Claude Code: another controller (the
    poller, an AFK auto-resolve, a button tap, or Claude itself) can resolve card
    A and render card B *while* the executor navigates or types, and card B then
    holds our text in ITS free-text row. Nothing else in the proof set catches
    that — the post-write SGR-2 flip, the payload-tail probe and the row-active
    footer hint are ALL satisfied by card B. So identity is captured pre-key and
    RE-CHECKED after the nav and again in the final pre-Enter capture: a card that
    turns over MID-transaction moves the side file, which moves the anchor, which
    refuses.

    WHAT IT DOES NOT CATCH (the accepted residual — see the module docstring): a
    successor whose ``PreToolUse`` record was already written BEFORE our first
    observation, but which had not yet DRAWN. The prose then reaches a DIFFERENT
    QUESTION. It can never become an OPTION COMMIT — the PRE-TYPE landing proof
    (cursor + SGR-2 dim + the exact placeholder label) owns that, and it is
    satisfied by exactly one shape on any card.

    ``anchor`` — **MANDATORY, and OCCURRENCE-UNIQUE** (peer-review round-2, BOTH
    P1s). It is the out-of-band, scroll-independent surface-GENERATION id, and it
    carries the window's SESSION generation with it (round-4 P1, below):

        AskUserQuestion → the PreToolUse side file's occurrence identity
                          (``auq_source.peek_surface_identity_for_window`` —
                          ``auq:sid:<session>:tu:<tool_use_id>``). A new AUQ
                          rewrites the file; a resolved one unlinks it.

    Why it had to become MANDATORY (peer-review round-2 P1): the anchor used to
    be OPTIONAL, so a missing / lagging / GC'd side file silently degraded the
    identity to the PANE alone. But ``current_question_title`` is normally ABSENT
    from a pure-pane parse, so two DIFFERENT AUQs with identical option labels
    produce the IDENTICAL pane identity — and an identity captured with
    ``anchor=None`` SKIPPED the anchor comparison entirely, so a successor's
    non-``None`` anchor was IGNORED rather than refused. No occurrence anchor ⇒
    the lane DECLINES (fall-through to PR-1's refusal). There is no second
    occurrence-unique source: the AUQ ``tool_use`` is buffered in JSONL until
    resolution, so the ``PreToolUse`` side file is the ONLY pre-resolution witness
    of *which* AUQ this is — which makes that hook a REQUIREMENT of this lane
    (user-visible, README-documented, startup-warned, ``doctor``-reported).

    **THE SESSION GENERATION IS INSIDE THE ANCHOR (peer-review round-4 P1).**
    ``auq_source`` resolves the window's session through
    ``session.read_session_id_for_window_fresh`` — the hook-written
    ``session_map.json`` — and never the CACHED ``WindowState.session_id``, which
    mirrors that map only as often as the monitor's poll loop reloads it. A
    ``/clear`` (or any session replacement) in the SAME tmux window rotates the
    session while the cache still names the old one: every anchor read then
    resolved the PREDECESSOR's side file while the pane being captured belonged
    to the SUCCESSOR's card. The three observations agreed with each OTHER — a
    self-consistent fiction — and, the pane component being degenerate across
    same-shaped occurrences, nothing refused: the Enter committed the user's
    answer onto the WRONG QUESTION. A per-window predicate could not have seen it
    either — both sessions occupy the SAME tmux window. Because the session id is
    IN the anchor, a rotation between any two of the three observation points
    changes the anchor and ``still_holds`` refuses; and a rotation whose successor
    has no side file yields ``None``, which refuses on rule 1. An empty
    hook-captured ``tool_use_id`` also yields ``None`` (round-4 P2): a
    ``(written_at, content-hash)`` composite is a guessable stand-in for an
    occurrence witness, not one.

    ``pane`` — ``terminal_parser.free_text_surface_identity``: the real options
    1..target_row-1, cursor-blind AND target-row-blind, so it is stable across
    the cursor move and the typing the executor itself performs. It is required
    AT CAPTURE (a card we cannot even see whole is a card we will not type into)
    and may go ``None`` LATER (never weaker — never a shorter prefix) when the
    block scrolls off under a long draft: the AUQ overflow shape, which the
    occurrence anchor then carries alone.
    """

    surface: str
    pane: str | None
    anchor: str  # MANDATORY — an identity without one was never an identity.

    def still_holds(self, live: SurfaceIdentity | None) -> bool:
        """True iff ``live`` is PROVABLY the same card. Fail-closed by default.

        1. No live identity at all ⇒ False (the anchor is unrecoverable ⇒ refuse,
           never guess). Note ``derive_identity`` returns ``None`` exactly then,
           so "no anchor" can never be silently read as "anchor matches".
        2. The surface must be the same (a card→gate / card→ExitPlanMode swap
           refuses here, and ``_identity_reason`` re-extracts to enforce it).
        3. The anchors must be EQUAL. Both sides always HAVE one (mandatory at
           capture; a live derivation without one is ``None`` and dies at rule 1),
           so there is no "None matches None" and a captured ``None`` can never
           silently accept a later non-``None``. GONE (the side file was unlinked
           at its tool_result — the card resolved) and CHANGED (a successor AUQ
           rewrote it, or the session rotated) are both proof this is no longer
           our card.
        4. A pane identity we HAD must still be EQUAL, or be genuinely
           unrecoverable — forgiven ONLY because the matching occurrence anchor
           carries the proof by itself. That single exception is what keeps the
           AUQ overflow shape (a ~5 k-char answer scrolls the option block away)
           working.
        """
        if live is None:
            return False
        if self.surface != live.surface:
            return False
        if live.anchor != self.anchor:
            return False
        if self.pane is not None and live.pane is not None and live.pane != self.pane:
            return False
        return True


# ── The occurrence anchor: the AUQ PreToolUse side file ──────────────────
#
# The anchor is written by Claude Code's ``PreToolUse`` hook BEFORE the picker
# renders, and unlinked when it resolves. That ordering is the whole reason an
# anchor can be an OCCURRENCE witness at all: it is minted out-of-band, by the
# process that is about to block, and it is a NAME for the prompt rather than a
# description of what the prompt is showing.
#
# UNRECOVERABLE (hook not installed, side file GC'd/unlinked, clock skew, no
# ``tool_use_id``, or the window's session not yet in the map) ⇒ ``None`` ⇒ the
# lane DECLINES pre-keystroke and PR-1 owns the refusal. Fail-closed: a card we
# cannot name is a card we will not type into.


def read_surface_anchor(window_id: str) -> SurfaceAnchor | None:
    """The OCCURRENCE-unique surface anchor (see :class:`SurfaceIdentity`).

    Returns the side-file record's KEY **and its content**, from one atomic read
    — the content is what :func:`derive_identity` binds to the captured pane.

    Each call re-resolves the window's SESSION from the hook-written map and
    embeds it in the returned key (round-4 P1), so the session generation is
    re-proven at every one of the three observation points the executor makes —
    a rotation between any two of them changes the anchor and refuses.
    """
    # Deferred: ``auq_source`` reaches into ``session``; this module is a
    # delivery-path leaf and the repo pins the import direction with a
    # subprocess cycle test.
    from . import auq_source

    try:
        return auq_source.peek_surface_anchor_for_window(window_id)
    except Exception:  # pragma: no cover — a side-file read must never wedge a send
        logger.exception(
            "free_text: AUQ surface-anchor read failed for window %s", window_id
        )
        return None


def derive_identity(
    pane_text: str,
    *,
    surface: str,
    target_row: int,
    anchor: SurfaceAnchor | None,
) -> SurfaceIdentity | None:
    """One pane observation + an anchor → the card's identity, or ``None``.

    ``None`` ⇔ no occurrence anchor, or an anchor that PROVABLY does not describe
    this pane. A caller can never accidentally construct an anchor-less identity
    and have it compare equal to another anchor-less one.

    **THE ANCHOR IS BOUND TO THE PANE, NOT TO THE READ ORDER.** The anchor is read
    out-of-band and the pane is captured separately, so nothing but the READ ORDER
    tied them together — and that is not enough. The tempting argument is: "a
    live, unresolved prompt means Claude is BLOCKED on it, so it cannot be
    invoking the next AskUserQuestion — and the hook fires BEFORE its prompt
    renders; therefore 'P is live on the pane at t1' implies 'the side file at t1
    is P's'." The step that is NOT justified is "P is live on the pane":
    ``PreToolUse`` writes card B's record BEFORE B renders, so between B's
    invocation and B's paint the side file already names B while the pane may
    still be SHOWING the just-answered card A — the ``(OLD pane, NEW anchor)``
    chimera.

    So the anchor is not taken on trust: its RECORD's CONTENT (the real OPTION
    LABELS) must AGREE with the pane it is paired with
    (``auq_source.anchor_pane_agreement``). A record that does not describe what
    we are looking at yields ``None``, whatever the read order was.

    Labels are the ONLY pane-observable content, so two same-labelled cards remain
    indistinguishable here — the accepted residual (module docstring). The
    read-first ordering is KEPT (it is free, and it makes the OTHER direction the
    harmless one), and the executor additionally SANDWICHES each capture between
    two anchor reads and requires them EQUAL — so a hook write landing inside an
    observation is DETECTED rather than reasoned about. See ``_observe``.
    """
    if anchor is None:
        return None

    from . import auq_source

    agreement = auq_source.anchor_pane_agreement(
        anchor.tool_input,
        pane_text,
        target_row=target_row,
    )
    if agreement == auq_source.ANCHOR_MISMATCH:
        # The record names a card the pane is not showing. Whatever the read
        # order was, this pairing is a chimera.
        return None
    return SurfaceIdentity(
        surface=surface,
        pane=terminal_parser.free_text_surface_identity(
            pane_text, target_row=target_row
        ),
        anchor=anchor.key,
    )


# ── The pre-type plan ────────────────────────────────────────────────────


@dataclass(frozen=True)
class FreeTextPlan:
    """What the executor resolved from the PRE-type pane."""

    surface: str
    target_row: int  # the free-text affordance row number
    cursor_row: int  # where the live ❯ is now
    placeholder: str  # the expected (dim) label of the target row
    identity: SurfaceIdentity  # WHICH CARD — re-checked post-nav and pre-Enter


@dataclass(frozen=True)
class _Shape:
    """The surface's GEOMETRY, before it is paired with an identity."""

    target_row: int
    cursor_row: int
    placeholder: str


def _auq_shape(pane_text: str, ansi_pane: str) -> _Shape | None:
    """The AUQ single-select free-text geometry, or ``None`` to decline."""
    form = terminal_parser.parse_ask_user_question(pane_text)
    if form is None:
        return None
    # Out of scope (plan §2.2), each for a stated reason:
    #   multi-select   — a THREE-Enter transaction whose first Enter mutates the form
    #   review screen  — the Submit/Cancel screen has no free-text row
    #   multi-question — the tab matrix makes "the answer" ambiguous
    if form.select_mode != "single" or form.is_review_screen:
        return None
    if len(form.questions) > 1:
        return None
    if not form.is_free_text:
        return None
    # The OPTION-COMPLETENESS proof (plan §2.1): a scrolled / partial pane BAILS
    # rather than guessing the row index. ``options_complete`` is True only when
    # the numbering is contiguous from 1 AND an affordance row was parsed in the
    # block — i.e. we are looking at the WHOLE list, so N is trustworthy.
    if not form.options_complete or not form.options:
        return None
    target_row = len(form.options) + 1  # affordances are dropped from ``options``
    cursor = next((o.number for o in form.options if o.cursor and o.number), None)
    if cursor is None:
        # THE CURSOR IS ALREADY ON THE AFFORDANCE ROW (peer-review P2).
        # ``_parse_numbered_options`` DROPS the affordance row and — because an
        # affordance ❯ is the bottom-most, hence live, cursor — deliberately
        # CLEARS every real option's cursor, so no real option reports one. That
        # is not "no cursor", it is "the cursor is exactly where we want it": the
        # user landed there with the card's own ↑/↓ nav buttons, which is what
        # the card invites them to do, and then sent prose. Pre-fix that DECLINED
        # into a PR-1 refusal — the most natural path was the one that didn't work.
        # Read the affordance row directly and take the ZERO-NAV plan.
        row = terminal_parser.parse_free_text_row(ansi_pane, number=target_row)
        if row is None or not row.cursor:
            return None
        cursor = target_row
    return _Shape(
        target_row=target_row,
        cursor_row=cursor,
        placeholder=terminal_parser.AUQ_FREE_TEXT_LABEL,
    )


def plan_from_pane(
    pane_text: str | None, ansi_pane: str, anchor: SurfaceAnchor | None
) -> FreeTextPlan | None:
    """Resolve the free-text lane for a live pane, or ``None`` to decline.

    ``None`` means "this lane does not apply" — the caller falls through to the
    normal gated ``deliver_to_window``, which refuses if a prompt is live (PR-1)
    or delivers if the pane is at its input box. **Every non-AUQ surface lands
    here**, ExitPlanMode included (owner decision 2026-07-12 — its free-text lane
    was removed, so a plan card takes PR-1's refusal).

    ``anchor`` was read BEFORE ``pane_text`` was captured (see
    :func:`derive_identity`) — the caller owns that ordering, and it is the
    load-bearing half of the wrong-card close.

    THE IDENTITY GATE LIVES HERE, ONCE (peer-review round-2): a card we cannot
    identify by an OCCURRENCE-unique anchor is a card we will not type into,
    because we could never prove — after the nav, or before the Enter — that we
    are still on it. Declining is strictly better than the fail-closed post-type
    refusal it replaces: nothing is typed, so no draft is stranded and no brake
    goes up; PR-1 owns the single refusal.
    """
    if not pane_text:
        return None
    # THE CARD MUST *OWN* THE PANE (peer-review round-5 P1-B). A live blocking
    # prompt REPLACES the input box; a RESOLVED one restores it while leaving its
    # rendering on screen. Without this leg the lane would happily plan against a
    # card the user has already answered — and that is the precise state in which
    # the side file can already name the SUCCESSOR (``PreToolUse`` writes B's
    # record before B renders), i.e. the ``(OLD pane, NEW anchor)`` chimera. It is
    # the missing premise of the round-3 argument, and it is cheap to prove
    # instead of assume. (Rig-pinned: ``auq_after_answer_t{0,1,5,30}`` all carry a
    # restored input box.) Same predicate the post-write verifier already uses.
    if terminal_parser.pane_input_box_present(pane_text):
        return None
    content = terminal_parser.extract_interactive_content(pane_text)
    if content is None or content.name != SURFACE_AUQ:
        return None
    shape = _auq_shape(pane_text, ansi_pane)
    if shape is None:
        return None
    identity = derive_identity(
        pane_text,
        surface=SURFACE_AUQ,
        target_row=shape.target_row,
        anchor=anchor,
    )
    if identity is None:
        # Either NO occurrence anchor (no PreToolUse side file for this window's
        # CURRENT session — the hook is not installed, the record was GC'd, the
        # card already resolved, or the hook captured no ``tool_use_id``), or an
        # anchor whose OPTION LABELS do not describe this pane. An unidentifiable
        # card is a card we will not type into.
        return None
    if identity.pane is None:
        # The option block is not fully on the pane at CAPTURE time. It may
        # legitimately scroll away LATER (the anchor carries it then), but a
        # card we never saw whole is a card whose geometry we should not trust.
        return None
    return FreeTextPlan(
        surface=SURFACE_AUQ,
        target_row=shape.target_row,
        cursor_row=shape.cursor_row,
        placeholder=shape.placeholder,
        identity=identity,
    )


# ── The bytes-landed CORROBORATION (not a guard) ─────────────────────────
#
# MEASURED WEAKNESS, stated up front (rig, 2026-07-12): this is a WHOLE-PANE
# substring check and it CAN PASS SPURIOUSLY — on the rig it matched prose echoed
# in the transcript scrollback from a PREVIOUS answer, on a card that had received
# nothing at all. So it corroborates "our bytes are somewhere on this pane"; it
# does NOT prove they landed in the affordance row, and it is not what stands
# between a payload and a wrong keystroke. That is the PRE-TYPE landing proof.

# The tail length compared. Long enough to be unique in a pane, short enough to
# stay well inside the last visible rows of a wrapped draft.
_TAIL_PROBE_CHARS: Final = 40


def _squash(text: str) -> str:
    """Remove ALL whitespace.

    Claude Code's soft wrap only ever INSERTS whitespace (a newline plus the
    continuation indent), and it can break mid-token for a token wider than the
    row. Comparing whitespace-STRIPPED text is therefore immune to every wrap
    shape — a strictly stronger probe than collapsing runs to single spaces,
    which a mid-token hard break would defeat.
    """
    return re.sub(r"\s+", "", text)


def payload_tail_landed(pane_text: str | None, payload: str) -> bool:
    """True iff the TAIL of ``payload`` occurs ANYWHERE on the pane.

    A CORROBORATION, not a proof — see the block comment above: it is a whole-pane
    substring test and it passes spuriously on scrollback that happens to echo the
    text. The tail (not the head) because the affordance row's FIRST visual line
    can scroll off under a long draft while its last lines — the ones just above
    the footer — always remain.
    """
    if not pane_text:
        return False
    tail = _squash(payload)[-_TAIL_PROBE_CHARS:]
    if not tail:
        return False
    return tail in _squash(pane_text)


def _first_visual_line(payload: str) -> str:
    for line in payload.split("\n"):
        if line.strip():
            return line.strip()
    return payload.strip()


def _label_is_our_draft(label: str, payload: str) -> bool:
    """True iff the row's label is (a prefix of) what we typed.

    A CONSISTENCY CHECK, not an authorship proof (rig, 2026-07-12 — this CORRECTS
    the original "AUTHORSHIP" claim): a real option row labelled ``Yes`` is a
    prefix of the payload ``Yes, but use postgres``, so this predicate is True on
    a REAL OPTION under a payload that merely starts with its label. Combined with
    the post-write ``dim is False`` — which a selected real option row also
    satisfies — the pair is NOT a guard. The guard is the PRE-TYPE landing proof.

    The row renders only the FIRST visual line of a wrapped draft, so the label is
    a prefix of the payload's first line, not the whole payload. Compared
    whitespace-stripped for the same wrap-immunity reason as the tail probe.
    """
    squashed_label = _squash(label)
    if not squashed_label:
        return False
    return _squash(_first_visual_line(payload)).startswith(squashed_label)


# ── Bounded, cancellation-safe pane I/O (the /cost r1 P2 rule) ────────────


async def _capture(window_id: str) -> str | None | object:
    """One bounded ANSI capture. Only ``TimeoutError`` classifies."""
    try:
        return await asyncio.wait_for(
            tmux_manager.capture_pane_cancellation_safe(window_id, with_ansi=True),
            timeout=CAPTURE_DEADLINE_S,
        )
    except asyncio.TimeoutError:
        return _CAPTURE_TIMEOUT


async def _pane_command(window_id: str) -> str | None | object:
    try:
        return await asyncio.wait_for(
            tmux_manager.pane_current_command(window_id),
            timeout=CMD_PROBE_DEADLINE_S,
        )
    except asyncio.TimeoutError:
        return _CMD_TIMEOUT


def _plain(ansi: str) -> str:
    """The plain-text view of an ANSI capture (styling stripped)."""
    return terminal_parser.clean_ghost_input_text(ansi)


# ── ONE OBSERVATION = anchor · pane · anchor (round-5 P1-B) ──────────────

REASON_ANCHOR_LOST: Final = "surface_anchor_lost"
REASON_ANCHOR_MOVED: Final = "surface_anchor_moved"
REASON_CAPTURE_FAILED: Final = "capture_failed"


@dataclass(frozen=True)
class _Observation:
    """A pane capture SANDWICHED between two equal anchor reads."""

    anchor: SurfaceAnchor
    ansi: str
    pane: str


async def _observe(window_id: str) -> _Observation | str:
    """One atomic-enough observation of the card, or a failure reason.

    THE SANDWICH (peer-review round-5 P1-B). Reading the anchor BEFORE the pane
    (round 3) makes the *residual* direction the harmless one, but it does not
    make the observation coherent: the side file can move inside the gap, and
    then the anchor names one card while the pane shows another. So the anchor is
    read again AFTER the capture and the two must be EQUAL. Because the side file
    only ever moves FORWARD (a new AUQ's hook rewrites it; a resolved one is
    unlinked), ``anchor(t0) == anchor(t2)`` proves the file did not move anywhere
    in ``[t0, t2]`` — and therefore not at ``t1`` either, when the pane was
    captured. The pane and the anchor are then observations of the SAME instant's
    state, and a hook write landing mid-observation is DETECTED instead of
    reasoned about.

    Cheap: the anchor read is a small local file read, not a subprocess.
    """
    before = read_surface_anchor(window_id)
    if before is None:
        return REASON_ANCHOR_LOST
    ansi = await _capture(window_id)
    if not isinstance(ansi, str) or not ansi:
        return REASON_CAPTURE_FAILED
    after = read_surface_anchor(window_id)
    if after is None or after.key != before.key:
        # A hook write (or a resolution) landed INSIDE this observation. The pane
        # we just captured cannot be attributed to either record. Refuse.
        logger.info(
            "FREE_TEXT anchor MOVED across an observation on window %s — "
            "the card turned over while we were looking at it",
            window_id,
        )
        return REASON_ANCHOR_MOVED
    return _Observation(anchor=before, ansi=ansi, pane=_plain(ansi))


# ── The executor ─────────────────────────────────────────────────────────


class _WriteAttempt:
    """Set immediately BEFORE the first literal write (never after).

    A cancelled write can still have landed, so the stranded-draft brake must
    consider the payload potentially stranded from the instant the attempt
    begins — the same rule ``session._WriteAttempt`` encodes for PR-1.
    """

    __slots__ = ("attempted",)

    def __init__(self) -> None:
        self.attempted = False


async def try_answer(
    window_id: str,
    payload: str,
    *,
    user_turn: UserTurnStamp | None,
    display: str = "",
) -> DeliveryResult | None:
    """Answer a live card with ``payload``, or ``None`` if the lane declines.

    THE LANE IS PURELY ADDITIVE — the invariant that makes it safe to enable by
    default: **every bail BEFORE the first keystroke returns ``None``** and the
    caller falls through to the normal gated ``session.deliver_to_window``,
    which then owns the decision (it refuses on a live prompt — PR-1 — or
    delivers into an input box). So the lane can only ever turn a REFUSED
    message into a delivered ANSWER; it can never make a message that PR-1 alone
    would have handled correctly come out worse, and it never invents its own
    refusal for a payload it has not touched. (Plan §2.4 [r4 P1-4]: "every bail
    BEFORE Enter is ``not_advanced`` — nothing committed ⇒ falls through to the
    PR-1 refusal notice".) A BRAKED window (PR-1's stranded-draft registry) is one
    of those bails: the lane declines and PR-1 emits the one refusal, so the lane
    can never be a way AROUND the brake.

    Once the payload has been TYPED the lane owns the outcome and must NOT fall
    through: the text is sitting in the affordance row, so a second delivery
    attempt would append to it. Those returns are ``DRAFT_WRITTEN`` (Enter
    withheld — the stranded-draft brake goes up) or, past the Enter,
    ``DELIVERED`` / ``COMMIT_UNKNOWN``.

    Holds ``window_send_lock`` for the whole transaction, exactly like
    ``deliver_to_window`` — and releases it before returning ``None``, so the
    fall-through can re-acquire it (``asyncio.Lock`` is not reentrant).
    """
    if not _ENABLED:
        return None

    # The SEGMENT-aware lone-hotkey rule, BEFORE any capture. A digit is a live
    # HOTKEY on a single-select-shaped surface (rig C7/C8) and this lane targets
    # exactly those, so a payload of "3" must never be typed here. Falling
    # through is correct AND sufficient: ``deliver_to_window``'s step 0 applies
    # the SAME ``delivery.lone_hotkey_line`` rule and refuses it with the
    # lone-hotkey copy — one rule, one owner, no duplicate message.
    if delivery.lone_hotkey_line(payload) is not None:
        return None

    # The RAW-CONTROL-BYTE rule (round-5 P1-A), BEFORE any capture. ``send-keys
    # -l`` passes C0/ESC bytes to the pty VERBATIM (rig-confirmed), so an embedded
    # ``ESC [ B`` + digit would move the cursor OFF the affordance row we proved
    # and fire a digit HOTKEY — committing an option with no Enter, during the
    # very write whose result we only verify afterwards. Falling through is
    # correct AND sufficient: ``deliver_to_window``'s step 0b applies the SAME
    # ``delivery.unsafe_control_char`` rule and owns the refusal — one rule, one
    # owner, no duplicate message.
    if delivery.unsafe_control_char(payload) is not None:
        return None

    async with tmux_manager.window_send_lock(window_id):
        write = _WriteAttempt()
        try:
            result = await _answer_locked(window_id, payload, user_turn, write, display)
        except BaseException:
            # CancelledError MUST propagate — but a write that was ATTEMPTED may
            # have landed in the affordance row, so the brake goes up first
            # (mirrors ``session.deliver_to_window``'s handler exactly).
            if write.attempted:
                logger.warning(
                    "free_text: raised AFTER a write attempt on window %s — "
                    "arming the stranded-draft brake before re-raising",
                    window_id,
                )
                _mark_stranded(window_id)
            raise
        if result is not None and result.draft_stranded:
            _mark_stranded(window_id)

    if result is not None and not result.ok:
        logger.info(
            "FREE_TEXT REFUSED window=%s reason=%s outcome=%s",
            window_id,
            result.reason,
            result.outcome.value,
        )
    return result


def _mark_stranded(window_id: str) -> None:
    """Arm the per-window stranded-draft brake (the registry lives in tmux_manager)."""
    tmux_manager.mark_window_stranded_draft(window_id)


def _record_bot_sent(window_id: str, payload: str) -> None:
    """Suppress the 👤-echo for a committed free-text answer.

    Deferred import: ``session`` imports this package's siblings, and the repo's
    subprocess import-cycle test pins the direction. Belt-and-braces against a
    "👤 …" duplicate of the message the user just sent, should a committed answer
    ever surface as a genuine user entry in the transcript.
    """
    from ..session import record_bot_sent_text

    record_bot_sent_text(window_id, payload)


def _stamp(user_turn: UserTurnStamp) -> None:
    """The pre-commit user-turn stamp — the ONE mutation the send lock allows.

    PR-2 is the FIFTH Enter-commit path (plan §2.4 [r5 P1-1]) and MUST carry it:
    a free-text answer IS a user turn, so the live-prose turn boundary and the
    dashboard's 🔔 derivation both depend on it. It cannot delegate to
    ``send_to_window`` (the lock is non-reentrant, and PR-1's input-box gate
    would reject the very surface this lane targets), so it fires the same
    narrowly-typed request under the same documented lock exception.
    """
    from .message_queue import set_route_user_turn_at

    set_route_user_turn_at(user_turn.user_id, user_turn.thread_id, user_turn.window_id)


async def _answer_locked(
    window_id: str,
    payload: str,
    user_turn: UserTurnStamp | None,
    write: _WriteAttempt,
    display: str,
) -> DeliveryResult | None:
    """The gated free-text transaction. Caller holds ``window_send_lock``.

    EVERY return before the first keystroke is ``None`` — the additive
    invariant (see ``try_answer``). ``_decline`` names the bail in ONE INFO log
    so the lane stays diagnosable without inventing a user-facing refusal for a
    payload it never touched.
    """
    deadline = time.monotonic() + TRANSACTION_DEADLINE_S

    def _decline(reason: str) -> None:
        logger.info(
            "FREE_TEXT DECLINED window=%s reason=%s — falling through to the "
            "normal delivery gate (nothing was typed)",
            window_id,
            reason,
        )
        return None

    window = await tmux_manager.find_window_by_id(window_id)
    if not window:
        return _decline("window_gone")

    # (0) THE STRANDED-DRAFT BRAKE (peer-review P1). PR-1 raises it whenever a
    # payload may still be sitting UNSENT in this window — including one this
    # very lane left in a card's affordance row (DRAFT_WRITTEN), and including a
    # COMMIT_UNKNOWN whose Enter may in fact have landed and advanced Claude to
    # ANOTHER live card. While it is up, PR-1 refuses every send until the pane is
    # PROVEN clear. The free-text lane must not be a way around that: navigating
    # and typing into whatever is on the pane now is exactly the append-and-commit
    # chain the brake exists to break. DECLINE, so PR-1 owns the single refusal and
    # the single user-facing notice — and never clear the brake from here (its
    # release rules — an empty-input-row capture or confirmed window death — are
    # PR-1's, and they are the only proofs that mean anything).
    if tmux_manager.window_has_stranded_draft(window_id):
        return _decline("stranded_draft")

    # (1) FRESH proof of life + the version license, INSIDE the lock and
    # immediately before the first key (the AUQ round-2 P1-1 rule): a
    # /update-swapped TUI inside the window-list cache TTL must never be
    # arrow-keyed.
    cmd = await _pane_command(window_id)
    if cmd is _CMD_TIMEOUT:
        return _decline("cmd_probe_timeout")
    assert cmd is None or isinstance(cmd, str)
    if not pane_command_is_claude(cmd):
        # Not Claude ⇒ this lane cannot apply. The normal gate produces its own,
        # correct ``not_claude`` refusal + copy — one owner per refusal reason.
        return _decline("not_claude")

    # ONE OBSERVATION: anchor · pane · anchor (see ``_observe``). The anchor is
    # read BEFORE the capture (round 3) AND re-read after it (round 5), so the
    # pane and the record are attributable to the same instant — and the record's
    # CONTENT is then bound to that pane inside ``derive_identity``.
    obs = await _observe(window_id)
    if isinstance(obs, str):
        return _decline(obs)

    plan = plan_from_pane(obs.pane, obs.ansi, obs.anchor)
    if plan is None:
        return _decline("no_free_text_surface")
    if not licensed(plan.surface, cmd):
        # The honest per-(surface × CC-version) degradation: buttons + the PR-1
        # refusal, never a keystroke driven by an un-characterized empiric.
        return _decline(f"unlicensed:{plan.surface}@{cmd}")

    logger.info(
        "FREE_TEXT window=%s surface=%s target_row=%d cursor_row=%d len=%d",
        window_id,
        plan.surface,
        plan.target_row,
        plan.cursor_row,
        len(payload),
    )

    # (2) Monotonic arrow nav onto the affordance row. ``Down``-ONLY by
    # construction — the affordance row is the LAST row, so ``delta >= 0`` — and
    # ``Down`` CLAMPS on 2.1.207 (rig: it never wraps). The ``Up`` branch is
    # therefore unreachable from this lane and is kept only so a future shape with
    # the affordance above the cursor cannot silently send the wrong key.
    delta = plan.target_row - plan.cursor_row
    key = "Down" if delta > 0 else "Up"
    for _ in range(abs(delta)):
        if time.monotonic() > deadline:
            return _decline("deadline")
        if not await tmux_manager.send_keys(
            window.window_id, key, enter=False, literal=False
        ):
            return _decline(REASON_NAV_FAILED)
    await asyncio.sleep(NAV_SETTLE_S)

    # (3) **THE LANDING PROOF — THE GUARD OF THIS ENTIRE LANE.** Before a single
    # byte is typed, the row under the cursor must be: cursored, labelled EXACTLY
    # ``Type something.``, and SGR-2 DIM. ``dim`` holds for exactly ONE shape — the
    # SELECTED, UNTYPED placeholder — and a REAL OPTION ROW IS NEVER DIM, not even
    # when selected. So an overshoot, an undershoot, a stale frame or a card that
    # turned over mid-nav can never put the payload onto an option row: rig-verified
    # declines on an overshoot, on an undershoot parked at ``target_row``, and on a
    # payload ``Yes, but use postgres`` against a card whose option 1 is ``Yes``.
    # A failure here has typed NOTHING and falls through to PR-1, which owns the
    # single refusal.
    if time.monotonic() > deadline:
        return _decline("deadline")
    obs2 = await _observe(window_id)
    if isinstance(obs2, str):
        return _decline(obs2)
    # Identity first, so the log reason names WHICH CARD when a card turned over
    # during the nav (the anchor moved). It is not what makes the landing safe —
    # the dim proof below is — but a turned-over card should decline for the right
    # reason.
    ident_reason = _identity_reason(obs2.pane, plan, window_id, obs2.anchor)
    if ident_reason is not None:
        return _decline(ident_reason)
    landed = terminal_parser.parse_free_text_row(obs2.ansi, number=plan.target_row)
    if landed is None or not landed.cursor or not landed.dim:
        return _decline(REASON_LANDING_FAILED)
    if landed.label.strip() != plan.placeholder:
        logger.info(
            "FREE_TEXT landing label drift window=%s label=%r expected=%r",
            window_id,
            landed.label,
            plan.placeholder,
        )
        return _decline(REASON_LANDING_FAILED)

    # (4) Type the payload with the Enter WITHHELD. ONE literal write — the
    # ``!`` bash-mode two-step is deliberately NOT reproduced here: bash mode is
    # a property of the INPUT BOX, and a live card owns the keyboard, so a
    # leading ``!`` inside the affordance row is just text. Splitting it would
    # emit a lone ``!`` keystroke into a picker for no reason.
    write.attempted = True
    if not await tmux_manager.send_keys(
        window.window_id, payload, enter=False, literal=True
    ):
        # A False from send_keys does NOT prove zero bytes landed (r2 F5) ⇒
        # classified WRITTEN, fail-closed: the brake goes up and its
        # empty-row self-heal releases it if nothing actually landed.
        return delivery.refuse(delivery.REASON_SEND_FAILED, written=True)
    await asyncio.sleep(TEXT_SETTLE_S)

    # (5) IDENTITY + TYPED-STATE VERIFY. From here every failure is DRAFT_WRITTEN.
    verify = await _verify_typed(window_id, plan, payload, deadline=deadline)
    if verify is not None:
        return verify

    # (6) The pre-commit user-turn stamp. A raise fails CLOSED: no Enter, no stamp.
    if user_turn is not None:
        try:
            _stamp(user_turn)
        except Exception:
            logger.exception(
                "free_text: pre-commit user-turn stamp raised for window %s — "
                "withholding Enter",
                window_id,
            )
            return delivery.refuse(delivery.REASON_STAMP_FAILED, written=True)

    # (7) Enter — the commit. A False does NOT prove the key never reached the
    # pty, so it is COMMIT_UNKNOWN, never "withheld" (r2 F3).
    if not await tmux_manager.send_keys(
        window.window_id, "", enter=True, literal=False
    ):
        return delivery.commit_unknown(delivery.REASON_ENTER_FAILED)

    # (8) Advance confirmation, fail-closed. A committed answer TEARS THE SURFACE
    # DOWN, so its continued presence after a bounded settle is the honest
    # "unconfirmed" signal. Never auto-retried (the Enter cannot be un-sent).
    confirmed = await _confirm_advance(window_id, plan, deadline=deadline)
    if not confirmed:
        return delivery.commit_unknown(
            REASON_COMMIT_UNCONFIRMED, message=_COMMIT_UNCONFIRMED_MSG
        )

    _record_bot_sent(window_id, payload)
    logger.info(
        "FREE_TEXT DELIVERED window=%s surface=%s len=%d",
        window_id,
        plan.surface,
        len(payload),
    )
    return delivery.delivered(f"Answered the card in {display or window_id}")


async def _verify_typed(
    window_id: str,
    plan: FreeTextPlan,
    payload: str,
    *,
    deadline: float,
) -> DeliveryResult | None:
    """The post-type, pre-Enter verifier. ``None`` ⇒ the Enter may be sent.

    **THIS IS NOT THE GUARD.** By the time it runs the payload is already typed,
    and the decision that mattered — WHERE the bytes went — was made by the
    PRE-TYPE landing proof (cursor + SGR-2 dim + the exact placeholder label; see
    ``_answer_locked`` step 3 and the module docstring). What follows is a
    consistency check whose job is to WITHHOLD the Enter when the pane stopped
    looking like the transaction we started, and to fail closed when it cannot
    tell. Three of its legs are measurably weak, and they are labelled as such.

    The legs (every one AND-ed; a failure withholds the Enter):

      A. ``pane_command_is_claude`` — a bounded re-probe, run FIRST so the pane
         CAPTURE is the LAST observation before the Enter (the r2-F4 ordering:
         a stalled probe after the capture would let a stale frame authorize a
         commit into a freshly-drawn prompt).
      B. ``pane_input_box_present`` is FALSE — the blocking surface still owns
         the pane. If the card resolved mid-type (an AFK auto-resolve), the input
         box is back and Enter would submit a half-typed message; refuse.
         Deliberately called WITHOUT ``expected_draft``: the picker trap
         (``prompt_row_is_option``) is exactly what must fire here.
      C. IDENTITY — WHICH CARD: the extracted surface is still ``plan.surface``
         AND ``SurfaceIdentity.still_holds``. This catches a card that TURNED OVER
         mid-transaction (a new card's hook rewrites the side file ⇒ the anchor
         moves ⇒ refuse). It does NOT catch the disclosed same-labels-successor
         residual (module docstring), and it is not what keeps the prose off an
         option row.
      D. THE ROW, or — in the overflow shape — THE FOOTER (BOTH are CORROBORATION):
           D1 the affordance row is on the pane, carries the cursor, is NOT dim,
              and its label is a prefix of what we typed. **WEAK:** a selected REAL
              option row is also not-dim, and an option labelled ``Yes`` is a prefix
              of the payload ``Yes, but use postgres`` — this pair passes on a real
              option row and is therefore NOT a guard;
           D2 the row scrolled off under a long draft, but the live picker's footer
              carries ``ctrl+g to edit``. **WEAK:** that hint is also present on the
              ``N+2. Chat about this`` row, so it proves "the cursor is on SOME
              text-field row", never WHICH row and never WHICH card.
      E. the payload TAIL occurs on the pane. **WEAK:** a whole-pane substring test
         that passes spuriously on scrollback echoing a previous answer.

    THE OVERFLOW SHAPE (rig-measured): the AUQ picker is BOTTOM-anchored, so a long
    answer scrolls the option block — the ``❯ N+1.`` cursor row INCLUDED — off the
    TOP, and a TUI has no scrollback (alternate screen), so what scrolls off is
    genuinely unobservable. That is why D2 exists at all, and why the CARD is
    carried by the out-of-band anchor rather than by the pane.
    """
    attempts = 2
    reason: str | None = None
    for attempt in range(attempts):
        if time.monotonic() > deadline:
            return delivery.refuse(delivery.REASON_DEADLINE, written=True)

        cmd = await _pane_command(window_id)
        if cmd is _CMD_TIMEOUT:
            return delivery.refuse(delivery.REASON_CMD_PROBE_TIMEOUT, written=True)
        assert cmd is None or isinstance(cmd, str)
        if not pane_command_is_claude(cmd):
            return delivery.refuse(delivery.REASON_NOT_CLAUDE, written=True)

        # The anchor·pane·anchor sandwich (``_observe``) — and, like the command
        # probe above it, the capture inside it is the LAST pane observation
        # preceding the Enter (the r2-F4 discipline).
        obs = await _observe(window_id)
        if obs == REASON_CAPTURE_FAILED:
            return delivery.refuse(delivery.REASON_CAPTURE_TIMEOUT, written=True)
        if isinstance(obs, str):
            reason = obs  # anchor lost / moved — retried once, then verify_failed
        else:
            reason = _typed_state_reason(
                obs.ansi, obs.pane, plan, payload, window_id, obs.anchor
            )
            if reason is None:
                return None
        if attempt + 1 < attempts:
            # ONE bounded retry for a mid-redraw frame. A false refusal here is
            # the most expensive failure in the transaction: it strands the draft
            # IN a live card and brakes the topic.
            await asyncio.sleep(NAV_SETTLE_S)

    # The failing LEG is the whole diagnostic value of this refusal (a wrong-card
    # ``surface_identity_changed`` and a mid-redraw ``row_not_found`` are very
    # different events). Leg names only — never pane text, never the payload.
    logger.warning(
        "free_text: typed-state verify FAILED on window %s (surface=%s reason=%s) "
        "— the payload is in the affordance row and its Enter is WITHHELD",
        window_id,
        plan.surface,
        reason,
    )
    return delivery.refuse(
        REASON_VERIFY_FAILED, written=True, message=_VERIFY_FAILED_MSG
    )


def _identity_reason(
    pane: str,
    plan: FreeTextPlan,
    window_id: str,
    anchor: SurfaceAnchor | None,
) -> str | None:
    """``None`` iff the pane still looks like ``plan``'s card. Fail-closed.

    ``anchor`` came from an ``_observe`` sandwich — read before AND after this
    pane's capture, with the two reads equal — so it is attributable to the pane,
    and ``derive_identity`` then binds the record's OPTION LABELS to it. It cannot
    separate two same-labelled cards: that is the disclosed residual (module
    docstring), and it is bounded to "the answer reaches a different QUESTION",
    never an option commit.

    Two independent gates, and the ORDER matters only for the log reason:

      1. the extracted surface is still ``AskUserQuestion`` — a first-match-wins
         ``extract_interactive_content``, so AUQ→ExitPlanMode and
         card→gate/decision/settings/no-surface all refuse here; and
      2. ``SurfaceIdentity.still_holds`` — the SAME-surface, SAME-geometry
         successor (a re-asked AUQ) that gate 1 cannot see.

    Called at BOTH observation points that bracket a keystroke: after the nav
    (pre-write ⇒ the caller DECLINES and PR-1 owns the refusal) and in the final
    pre-Enter capture (post-write ⇒ the caller returns DRAFT_WRITTEN and arms the
    stranded-draft brake).
    """
    content = terminal_parser.extract_interactive_content(pane)
    if content is None or content.name != plan.surface:
        return "surface_gone"
    live = derive_identity(
        pane,
        surface=plan.surface,
        target_row=plan.target_row,
        anchor=anchor,
    )
    if live is None:
        # Either the OCCURRENCE anchor is gone (the PreToolUse side file was
        # unlinked — the tool_result fired ⇒ the card RESOLVED), or the record's
        # option labels do not describe this pane. Absence and disagreement are
        # both positive evidence that the card we planned against is no longer the
        # one in front of us.
        logger.warning(
            "FREE_TEXT surface anchor UNRECOVERABLE on window %s (surface=%s) — "
            "the card this message was answering can no longer be identified; "
            "refusing to commit",
            window_id,
            plan.surface,
        )
        return REASON_ANCHOR_LOST
    if not plan.identity.still_holds(live):
        logger.warning(
            "FREE_TEXT surface identity CHANGED on window %s (surface=%s) — "
            "the card this message was answering is no longer the live one; "
            "refusing to commit",
            window_id,
            plan.surface,
        )
        return "surface_identity_changed"
    return None


def _typed_state_reason(
    ansi: str,
    pane: str,
    plan: FreeTextPlan,
    payload: str,
    window_id: str,
    anchor: SurfaceAnchor | None,
) -> str | None:
    """``None`` iff the post-write state is CONSISTENT (see ``_verify_typed``).

    NOT a guard — the payload is already typed by now, and where it went was
    decided by the PRE-TYPE landing proof. This decides only whether the Enter may
    be sent, and it fails closed.

    ``anchor`` came from the ``_observe`` sandwich around ``pane``'s capture, so
    the anchor KEY (unchanged across the sandwich, and equal to the planned one)
    is attributable to this pane.
    """
    # (B) the blocking surface still owns the pane
    if terminal_parser.pane_input_box_present(pane):
        return "input_box_returned"
    # (C) WHICH CARD — a card that TURNED OVER since the plan (its hook rewrote the
    # side file, so the anchor moved) refuses here.
    ident_reason = _identity_reason(pane, plan, window_id, anchor)
    if ident_reason is not None:
        return ident_reason
    # (E) our bytes are somewhere on the pane (weak — see payload_tail_landed)
    if not payload_tail_landed(pane, payload):
        return "payload_absent"
    # (D) the row, or — in the overflow shape — the AUQ footer. Corroboration.
    row = terminal_parser.parse_free_text_row(ansi, number=plan.target_row)
    if row is not None and row.cursor:
        if row.dim:
            return "row_still_placeholder"  # nothing landed in the row
        if not _label_is_our_draft(row.label, payload):
            return "row_not_our_draft"
        return None  # D1 ✓
    if plan.surface == SURFACE_AUQ and terminal_parser.auq_free_text_row_active(pane):
        return None  # D2 ✓ — the row scrolled off (the overflow shape)
    return "row_not_found"


async def _confirm_advance(
    window_id: str, plan: FreeTextPlan, *, deadline: float
) -> bool:
    """True iff the committed surface is PROVEN gone (fail-closed otherwise)."""
    for attempt in range(COMMIT_CONFIRM_ATTEMPTS):
        await asyncio.sleep(COMMIT_SETTLE_S)
        if time.monotonic() > deadline:
            return False
        ansi = await _capture(window_id)
        if not isinstance(ansi, str):
            continue
        pane = _plain(ansi)
        content = terminal_parser.extract_interactive_content(pane)
        if content is None or content.name != plan.surface:
            return True
        if attempt + 1 >= COMMIT_CONFIRM_ATTEMPTS:
            break
    return False


# ── Card copy (plan §2.5 — the false hint is fixed in lockstep) ───────────

HINT_FREE_TEXT: Final = "💬 Send a message to answer in your own words."
HINT_MULTI_SELECT: Final = "Use the option buttons, then Submit."
HINT_NO_FREE_TEXT: Final = "Answer with the buttons or the ↑/↓/⏎ keys."


def advertises_free_text(
    surface: str, *, version: str | None, has_affordance: bool
) -> bool:
    """True iff a plain message would ACTUALLY be taken as this card's answer.

    The single (flag ON) × (licensed CC version) × (live free-text affordance)
    predicate. It is the one gate behind BOTH ``card_hint`` (the card's
    free-text line) AND the three partial/untrusted-pane notices in
    ``interactive_ui`` (GH #54 W5), so no card copy can ever promise a
    text answer that PR-1's gate would refuse. A preview single-select
    (``is_free_text=False`` ⇒ ``has_affordance=False``) therefore never
    advertises text answers on any version.
    """
    return has_affordance and _ENABLED and licensed(surface, version)


def card_hint(surface: str, *, version: str | None, has_affordance: bool) -> str:
    """The per-surface card hint (plan §2.2 [r3 P2-4]).

    The card must state the CURRENT truth: pre-PR-2 it promised free-text on
    every AUQ, including the multi-select and unlicensed-version cases where a
    plain message is REFUSED. Now the promise is made only where the lane will
    actually take it.
    """
    if advertises_free_text(surface, version=version, has_affordance=has_affordance):
        return HINT_FREE_TEXT
    return HINT_NO_FREE_TEXT
