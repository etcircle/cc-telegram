"""Free-text answers on a live interactive card (GH #50 PR-2).

PR-1 made every inbound payload REFUSE at a live blocking surface (a message
typed at an AskUserQuestion picker would have had its Enter commit option 1).
That is correct but a dead end: the AUQ card literally invites the user to
"send a regular message to free-text". PR-2 makes that invitation TRUE for the
two surfaces Claude Code gives a free-text affordance:

    AskUserQuestion (single-select)   row N+1  ``Type something.``
    ExitPlanMode                      row 4    ``Tell Claude what to change``

The executor NAVIGATES to that row, VERIFIES it landed **on the same card it
planned against**, TYPES the payload with the Enter withheld, VERIFIES the typed
state **and the card identity again**, fires the pre-commit user-turn stamp, and
only then presses Enter. On AUQ the prose becomes the ANSWER; on EPM the plan is
REJECTED with the prose as feedback and **plan mode is preserved** (rig-verified
on 2.1.207).

TWO THINGS MUST BE PROVEN, NOT ONE. "The pane is in the right STATE" (a dim
placeholder under the cursor; our text in the row; no input box) is necessary and
NOT sufficient — every one of those legs is equally satisfied by a DIFFERENT card
that another controller rendered while we were navigating or typing, holding our
text in ITS free-text row. So ``SurfaceIdentity`` (WHICH CARD) is captured before
the first key and RE-CHECKED at both observation points that bracket a keystroke.
See that class for the drift trap the identity is designed around — the executor
MUTATES the pane it must re-identify.

AND THE IDENTITY MUST BE OCCURRENCE-UNIQUE, on BOTH surfaces (peer-review
round-2, two P1s). The PANE component alone cannot distinguish two cards that
render the same rows — two AUQs with identical option labels, or two ExitPlanMode
prompts (which ALWAYS render the same three real options). So each surface
carries a MANDATORY out-of-band anchor that names the OCCURRENCE, not the shape:
the AUQ PreToolUse side file's ``tool_use_id`` composite, and — for EPM — a hash
of the plan FILE'S CONTENT (a path is a NAME: a revised plan commonly keeps the
same slug, so the path matched across two different plans). No anchor ⇒ the lane
DECLINES before any keystroke; a changed anchor ⇒ it refuses.

It reuses the shipped dispatch discipline (``_dispatch_pick`` /
``_dispatch_decision_pane_locked``) verbatim: per-window send lock, bounded
cancellation-safe captures, a FRESH in-lock ``pane_command_is_claude`` +
version-license re-read immediately before the first key, monotonic arrow nav,
settle → re-parse → verify, Enter as the ONLY commit key, and a strict
commit-boundary classification.

THE TYPED-STATE PROOF IS SGR-2 (``terminal_parser.parse_free_text_row``): the
placeholder renders DIM, typed text does not. See that module for the empirics
and the TUI-drift note.

VERSION-LICENSED (the ``decision_token`` precedent, MANDATORY per plan §2.4): the
row index, the placeholder labels, the SGR-2 styling and the ``ctrl+g`` footer
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
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .. import delivery, terminal_parser
from ..delivery import DeliveryResult, UserTurnStamp
from ..tmux_manager import pane_command_is_claude, tmux_manager


logger = logging.getLogger(__name__)


# ── Surfaces + the (surface × CC-version) license table ───────────────────

SURFACE_AUQ: Final = "AskUserQuestion"
SURFACE_EPM: Final = "ExitPlanMode"

# The lane is licensed per (surface × EXACT CC version), fixture-pinned. Every CC
# upgrade empties the effective allowlist until the surface is re-characterized
# against fresh rig captures — the honest degradation the ``decision_token``
# table established. Adding a version here without re-capturing
# ``{auq,epm}_freetext_*_v<version>.ansi.txt`` is the one way to make this lane
# dangerous.
_FREE_TEXT_LICENSE_TABLE: Final[dict[str, frozenset[str]]] = {
    SURFACE_AUQ: frozenset({"2.1.207"}),
    SURFACE_EPM: frozenset({"2.1.207"}),
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
    A and render card B *while* the executor navigates or types. Nothing else in
    the proof set catches that — the landing proof, the SGR-2 typed-state proof,
    the payload-tail probe and the row-active footer proof are ALL satisfied by
    card B holding our text. So identity must be captured pre-key and RE-CHECKED
    after the nav and again in the final pre-Enter capture. On ExitPlanMode option
    1 is "Yes, and bypass permissions", so this is the most dangerous hole in the
    lane, and it fails CLOSED.

    ``anchor`` — **MANDATORY, and OCCURRENCE-UNIQUE** (peer-review round-2, BOTH
    P1s). It is the out-of-band, scroll-independent surface-GENERATION id:

        AskUserQuestion → the PreToolUse side file's occurrence identity
                          (``auq_source.peek_surface_identity_for_window`` — the
                          GH #48 composite: a non-empty ``tool_use_id``, else
                          ``(written_at, canonical tool-input fingerprint)``). A
                          new AUQ rewrites the file; a resolved one unlinks it.
        ExitPlanMode    → the plan-CONTENT generation ``_epm_plan_generation``:
                          the live footer's ``~/.claude/plans/<slug>.md`` path
                          PLUS a hash of that file's CONTENT.

    Why it had to become mandatory, per surface:

      * **AUQ** — the anchor used to be OPTIONAL, so a missing / lagging / GC'd
        side file silently degraded the identity to the PANE alone. But
        ``current_question_title`` is normally ABSENT from a pure-pane parse, so
        two DIFFERENT AUQs with identical option labels produce the IDENTICAL
        pane identity. No occurrence anchor ⇒ the lane DECLINES (fall-through to
        PR-1's refusal). There is no second occurrence-unique source: the AUQ
        ``tool_use`` is buffered in JSONL until resolution, so the PreToolUse
        side file is the ONLY pre-resolution witness of "which AUQ is this".
      * **EPM** — the anchor used to be the plan-file PATH. Re-entering
        ExitPlanMode after REVISING the same plan commonly keeps the SAME slug,
        so plan P and plan Q carried an IDENTICAL anchor — and every EPM renders
        the SAME three real options, so the pane component matched too. BOTH
        components matched across two DIFFERENT prompts. The path is a NAME, not
        an OCCURRENCE; the plan's CONTENT is what the prompt is asking about, so
        the content hash is the generation. A revised plan ⇒ different bytes ⇒
        different anchor ⇒ REFUSE.

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
        2. The surface must be the same (AUQ↛EPM, EPM↛AUQ).
        3. The anchors must be EQUAL. Both sides always HAVE one (mandatory at
           capture; a live derivation without one is ``None`` and dies at rule 1),
           so there is no "None matches None" and a captured ``None`` can never
           silently accept a later non-``None``. Gone (the AUQ side file was
           unlinked at its tool_result; the plan file was rewritten) or changed (a
           successor card) are both proof this is no longer our card.
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


# ── The ExitPlanMode plan-CONTENT generation (peer-review round-2 P1) ─────
#
# The plan file is written by Claude BEFORE the prompt renders (the same file
# ``interactive_ui._maybe_post_epm_plan`` reads to post the plan body), so its
# bytes ARE the question the prompt is asking. A REVISION rewrites them; the
# hash therefore changes even when the slug does not.
#
# Considered and REJECTED as the EPM occurrence token:
#
#   * ``os.stat`` (mtime_ns + size) — cheaper, and it does catch a rewrite, but
#     it is a METADATA generation: it flips on a no-op touch (a false refusal
#     that costs a stranded draft) and says nothing about what the prompt is
#     actually asking. The content IS the question; hash the question.
#   * ``status_polling._epm_surface_first_seen_at`` — a per-route FIRST-DETECT
#     stamp, and it LOOKS like an occurrence token, but it is ``setdefault``-ed
#     and only POPPED on an observed EPM *absence* (behind the poller's
#     absent-streak hysteresis). A plan-P→plan-Q transition with no observed gap
#     therefore CARRIES THE SAME STAMP across two different prompts — exactly the
#     case this P1 is about — so it detects nothing here while adding a
#     cross-module lifecycle whose pop/re-stamp could false-refuse a live
#     transaction. Not sound as a safety-critical discriminator.
#
# UNRECOVERABLE (no live footer, path outside ``~/.claude/plans/``, missing /
# unreadable / oversized file) ⇒ ``None`` ⇒ the lane declines (pre-key) or
# refuses (post-key). Fail-closed: on a plan-approval surface there is no
# acceptable guess.

_EPM_PLAN_BASE_PARTS: Final = (".claude", "plans")
_EPM_PLAN_MAX_BYTES: Final = 512 * 1024


def _epm_plan_generation(pane_text: str) -> str | None:
    """``epm:<footer path>:<sha256 of its CONTENT>``, or ``None`` if unrecoverable.

    The read is small (a plan is a few KB) and hits the page cache — the same
    class of bounded, synchronous side-file read
    ``auq_source.peek_surface_identity_for_window`` already performs on the AUQ
    leg of this very function.
    """
    footer_path = terminal_parser.extract_epm_plan_file_path(pane_text)
    if not footer_path:
        return None
    try:
        base = Path.home().joinpath(*_EPM_PLAN_BASE_PARTS).resolve()
        path = Path(footer_path).expanduser().resolve()
        if not path.is_relative_to(base):
            logger.warning(
                "free_text: EPM plan path outside ~/.claude/plans/: %r — no anchor",
                footer_path,
            )
            return None
        with open(path, "rb") as fh:
            data = fh.read(_EPM_PLAN_MAX_BYTES + 1)
    except OSError as exc:
        logger.info(
            "free_text: EPM plan file unreadable (%r): %s — no anchor",
            footer_path,
            exc,
        )
        return None
    except Exception:  # pragma: no cover — an anchor read must never wedge a send
        logger.exception("free_text: EPM plan-generation read failed (%r)", footer_path)
        return None
    if not data or len(data) > _EPM_PLAN_MAX_BYTES:
        return None
    return f"epm:{footer_path}:{hashlib.sha256(data).hexdigest()[:16]}"


def _anchor_for(surface: str, pane_text: str, window_id: str) -> str | None:
    """The OCCURRENCE-unique surface anchor (see :class:`SurfaceIdentity`)."""
    if surface == SURFACE_EPM:
        return _epm_plan_generation(pane_text)
    # Deferred: ``auq_source`` reaches into ``session``; this module is a
    # delivery-path leaf and the repo pins the import direction with a
    # subprocess cycle test.
    from . import auq_source

    try:
        return auq_source.peek_surface_identity_for_window(window_id)
    except Exception:  # pragma: no cover — a side-file read must never wedge a send
        logger.exception(
            "free_text: AUQ surface-anchor read failed for window %s", window_id
        )
        return None


def derive_identity(
    pane_text: str, *, surface: str, target_row: int, window_id: str
) -> SurfaceIdentity | None:
    """One pane observation → the card's identity, or ``None`` if UNIDENTIFIABLE.

    ``None`` ⇔ no occurrence anchor. That is the whole point of returning an
    Optional: a caller can never accidentally construct an anchor-less identity
    and have it compare equal to another anchor-less one.
    """
    anchor = _anchor_for(surface, pane_text, window_id)
    if anchor is None:
        return None
    return SurfaceIdentity(
        surface=surface,
        pane=terminal_parser.free_text_surface_identity(
            pane_text, surface=surface, target_row=target_row
        ),
        anchor=anchor,
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


def _epm_shape(pane_text: str) -> _Shape | None:
    """The ExitPlanMode free-text geometry, or ``None`` to decline."""
    form = terminal_parser.parse_exit_plan_form(pane_text)
    if form is None:
        return None
    # EPM's affordance IS a parsed option (its label is not an ``is_affordance_label``
    # one), so a cursor already parked on it reports normally — the AUQ zero-nav
    # special case above has no EPM twin.
    cursor = next((o.number for o in form.options if o.cursor and o.number), None)
    if cursor is None:
        return None
    last = form.options[-1]
    if last.number is None:
        return None
    return _Shape(
        target_row=last.number,  # the affordance IS a parsed option here (row 4)
        cursor_row=cursor,
        placeholder=terminal_parser.EPM_FREE_TEXT_LABEL,
    )


def plan_from_pane(
    pane_text: str | None, ansi_pane: str, window_id: str
) -> FreeTextPlan | None:
    """Resolve the free-text lane for a live pane, or ``None`` to decline.

    ``None`` means "this lane does not apply" — the caller falls through to the
    normal gated ``deliver_to_window``, which refuses if a prompt is live (PR-1)
    or delivers if the pane is at its input box.

    THE IDENTITY GATE LIVES HERE, ONCE, FOR BOTH SURFACES (peer-review round-2):
    a card we cannot identify by an OCCURRENCE-unique anchor is a card we will
    not type into, because we could never prove — after the nav, or before the
    Enter — that we are still on it. Declining is strictly better than the
    fail-closed post-type refusal it replaces: nothing is typed, so no draft is
    stranded and no brake goes up; PR-1 owns the single refusal.
    """
    if not pane_text:
        return None
    content = terminal_parser.extract_interactive_content(pane_text)
    if content is None:
        return None
    if content.name == SURFACE_AUQ:
        shape = _auq_shape(pane_text, ansi_pane)
    elif content.name == SURFACE_EPM:
        shape = _epm_shape(pane_text)
    else:
        return None
    if shape is None:
        return None
    identity = derive_identity(
        pane_text,
        surface=content.name,
        target_row=shape.target_row,
        window_id=window_id,
    )
    if identity is None:
        # No occurrence anchor — an AUQ with no PreToolUse side file, or an EPM
        # whose plan file is not readable from its live footer.
        return None
    if identity.pane is None:
        # The option block is not fully on the pane at CAPTURE time. It may
        # legitimately scroll away LATER (the anchor carries it then), but a
        # card we never saw whole is a card whose geometry we should not trust.
        return None
    return FreeTextPlan(
        surface=content.name,
        target_row=shape.target_row,
        cursor_row=shape.cursor_row,
        placeholder=shape.placeholder,
        identity=identity,
    )


# ── The bytes-landed probe ───────────────────────────────────────────────

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
    """True iff the TAIL of ``payload`` is visibly on the pane.

    The "our bytes landed" proof. The tail (not the head) because the affordance
    row's FIRST visual line can scroll off under a long draft while its last
    lines — the ones just above the footer — always remain.
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
    """True iff the row's label is (a prefix of) what we typed — AUTHORSHIP.

    The row renders only the FIRST visual line of a wrapped draft, so the label
    is a prefix of the payload's first line, not the whole payload. Compared
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
    subprocess import-cycle test pins the direction. EPM feedback lands in the
    transcript as a genuine user entry, so without this the topic gets a "👤 …"
    duplicate of the message the user just sent.
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

    ansi = await _capture(window_id)
    if not isinstance(ansi, str) or not ansi:
        return _decline("capture_failed")
    pane = _plain(ansi)

    plan = plan_from_pane(pane, ansi, window_id)
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

    # (2) Monotonic arrow nav onto the affordance row. Never a wrap shortcut:
    # over-counting past the last row wraps to row 1, and Enter there commits it
    # (on EPM that is "Yes, and bypass permissions").
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

    # (3) LANDING PROOF (pre-type): we are STILL ON THE SAME CARD, the cursor is
    # on its affordance row, the row still carries its placeholder, and the
    # placeholder is SGR-2 DIM. The dim bit is what makes the row state a proof
    # rather than a guess — it is applied only while the row is selected AND
    # untyped, which is exactly the state we require before typing. A failure here
    # has typed NOTHING, so it falls through to PR-1 (which re-captures and
    # refuses with the accurate reason).
    if time.monotonic() > deadline:
        return _decline("deadline")
    ansi2 = await _capture(window_id)
    if not isinstance(ansi2, str):
        return _decline("capture_failed")
    pane2 = _plain(ansi2)
    # IDENTITY FIRST (peer-review P1): a card that resolved during the nav and was
    # replaced by a same-geometry successor would satisfy every other leg below —
    # the successor's row N+1 is a dim placeholder under our freshly-moved cursor.
    # Checking WHICH CARD before we type is what stops that.
    ident_reason = _identity_reason(pane2, plan, window_id)
    if ident_reason is not None:
        return _decline(ident_reason)
    landed = terminal_parser.parse_free_text_row(ansi2, number=plan.target_row)
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
    where = display or window_id
    if plan.surface == SURFACE_EPM:
        return delivery.delivered(f"Sent as plan feedback to {where}")
    return delivery.delivered(f"Answered the card in {where}")


async def _verify_typed(
    window_id: str,
    plan: FreeTextPlan,
    payload: str,
    *,
    deadline: float,
) -> DeliveryResult | None:
    """The post-type, pre-Enter verifier. ``None`` ⇒ the Enter may be sent.

    THE PROOF SET (every leg AND-ed; a failure withholds the Enter):

      A. ``pane_command_is_claude`` — a bounded re-probe, run FIRST so the pane
         CAPTURE is the LAST observation before the Enter (the r2-F4 ordering:
         a stalled probe after the capture would let a stale frame authorize a
         commit into a freshly-drawn prompt).
      B. ``pane_input_box_present`` is FALSE — the blocking surface still owns
         the pane. If the card resolved mid-type (an AFK auto-resolve), the input
         box is back and Enter would submit a half-typed message; refuse.
         Deliberately called WITHOUT ``expected_draft``: the picker trap
         (``prompt_row_is_option``) is exactly what must fire here.
      C. **IDENTITY — WHICH CARD (peer-review P1).** The extracted surface is
         still ``plan.surface`` AND ``SurfaceIdentity.still_holds``. Without this
         leg, a card that resolved mid-transaction and was replaced by ANOTHER
         card satisfies every remaining leg — B (a card owns the pane), D (our
         text IS on it, because we typed it into the successor's row), and C1 (the
         successor's row N+1 carries our cursor, our text, at normal intensity) —
         and the Enter commits the user's answer to the WRONG QUESTION. On
         ExitPlanMode that is a plan-approval surface. THIS is the leg that says
         no.
      D. THE ROW, or — on AUQ only — THE FOOTER:
           D1 the affordance row is on the pane: it carries the cursor, its label
              is NOT SGR-2 dim (⇒ TYPED, not the placeholder) and the label is a
              prefix of what we typed (⇒ WE typed it);
           D2 (AUQ only) the row scrolled off under a long draft, but the picker
              footer — scoped to the LIVE extracted AUQ region — carries
              ``ctrl+g to edit``, which on 2.1.207 appears IFF the free-text row
              is the ACTIVE row. It proves WHICH ROW, never WHICH CARD; leg C is
              what supplies the latter (via the out-of-band anchor, since the
              pane component is exactly what scrolled away).
      E. the payload TAIL is visibly on the pane (our bytes landed).

    The two overflow shapes differ (rig-measured), and identity closes both:
    the AUQ picker is BOTTOM-anchored, so a long draft scrolls the option block —
    row included — off the TOP (D2 carries the row, the side-file OCCURRENCE
    ANCHOR carries the card); the EPM prompt grows DOWNWARD, so a long draft
    pushes its FOOTER off the bottom (D1 still carries the row — but the footer
    is where the EPM anchor is READ, and the whole surface stops extracting, so
    leg C refuses; an EPM feedback long enough to overflow is DRAFT_WRITTEN,
    fail-closed, because EPM's option 1 is "Yes, and bypass permissions"). A TUI
    has no scrollback (alternate screen), so what scrolls off is genuinely
    unobservable.
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

        ansi = await _capture(window_id)
        if not isinstance(ansi, str):
            return delivery.refuse(delivery.REASON_CAPTURE_TIMEOUT, written=True)
        pane = _plain(ansi)

        reason = _typed_state_reason(ansi, pane, plan, payload, window_id)
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


def _identity_reason(pane: str, plan: FreeTextPlan, window_id: str) -> str | None:
    """``None`` iff the pane is PROVABLY still ``plan``'s card. Fail-closed.

    Two independent gates, and the ORDER matters only for the log reason:

      1. the extracted surface is still ``plan.surface`` — a first-match-wins
         ``extract_interactive_content``, so AUQ→EPM, EPM→AUQ, and
         card→gate/decision/settings/no-surface all refuse here; and
      2. ``SurfaceIdentity.still_holds`` — the SAME-surface, SAME-geometry
         successor (a re-asked AUQ, the next plan) that gate 1 cannot see.

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
        window_id=window_id,
    )
    if live is None:
        # The OCCURRENCE anchor is gone: the AUQ's side file was unlinked (its
        # tool_result fired ⇒ the card RESOLVED), or the EPM's plan file no
        # longer reads. Absence is never a licence to proceed — it is positive
        # evidence the card we planned against is no longer the one on the pane.
        logger.warning(
            "FREE_TEXT surface anchor UNRECOVERABLE on window %s (surface=%s) — "
            "the card this message was answering can no longer be identified; "
            "refusing to commit",
            window_id,
            plan.surface,
        )
        return "surface_anchor_lost"
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
    ansi: str, pane: str, plan: FreeTextPlan, payload: str, window_id: str
) -> str | None:
    """``None`` iff the typed state is PROVEN (see ``_verify_typed``'s legs)."""
    # (B) the blocking surface still owns the pane
    if terminal_parser.pane_input_box_present(pane):
        return "input_box_returned"
    # (C) WHICH CARD — before anything that merely proves WHICH ROW or WHOSE TEXT
    ident_reason = _identity_reason(pane, plan, window_id)
    if ident_reason is not None:
        return ident_reason
    # (E) our bytes landed
    if not payload_tail_landed(pane, payload):
        return "payload_absent"
    # (D) the row, or the AUQ footer
    row = terminal_parser.parse_free_text_row(ansi, number=plan.target_row)
    if row is not None and row.cursor:
        if row.dim:
            return "row_still_placeholder"  # nothing landed in the row
        if not _label_is_our_draft(row.label, payload):
            return "row_not_our_draft"
        return None  # D1 ✓
    if plan.surface == SURFACE_AUQ and terminal_parser.auq_free_text_row_active(pane):
        return None  # D2 ✓ — the row scrolled off; leg C already proved the CARD
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


def card_hint(surface: str, *, version: str | None, has_affordance: bool) -> str:
    """The per-surface card hint (plan §2.2 [r3 P2-4]).

    The card must state the CURRENT truth: pre-PR-2 it promised free-text on
    every AUQ, including the multi-select and unlicensed-version cases where a
    plain message is REFUSED. Now the promise is made only where the lane will
    actually take it.
    """
    if not has_affordance:
        return HINT_NO_FREE_TEXT
    if _ENABLED and licensed(surface, version):
        return HINT_FREE_TEXT
    return HINT_NO_FREE_TEXT
