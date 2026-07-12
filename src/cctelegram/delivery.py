"""Structured delivery result + payload shaping for the tmux send seam (GH #50).

A pure, stdlib-only leaf (it imports ``terminal_parser`` only) that owns the
vocabulary ``SessionManager.send_to_window`` speaks when it refuses to type a
user payload into a live Claude Code pane:

  - ``DeliveryOutcome`` — the WRITTEN-STATE classification the plan demands:
    ``DELIVERED`` / ``NOT_WRITTEN`` (gate failed before any keystroke) /
    ``DRAFT_WRITTEN`` (text written, Enter deliberately WITHHELD — PROVABLY not
    committed) / ``COMMIT_UNKNOWN`` (the Enter WAS attempted and the transport
    reported failure — the commit is genuinely unproven in BOTH directions).
  - ``DeliveryResult`` — outcome + machine ``reason`` + the per-reason,
    ACTIONABLE user copy. It is the value threaded through the aggregator flush,
    the split replay and the pending-bind replay so a refusal's REAL reason
    reaches the topic instead of a bare ``False``.
  - ``UserTurnStamp`` — the narrowly-typed pre-commit hook request: "stamp the
    user turn for THIS route", invoked after every gate passes and immediately
    before the Enter. It is the ONLY ``route_runtime`` mutation permitted under
    ``window_send_lock`` (an explicit, named exception documented in the lock
    contract). It may not await, may not schedule work, and may not mutate
    anything else.
  - ``literal_segments`` / ``lone_hotkey_line`` — the SEGMENT-aware,
    PER-LINE hotkey refusal (§1.3). On CC 2.1.207 a bare digit is a live HOTKEY
    on a single-select-shaped surface (it commits with NO Enter), so a payload
    whose emitted literal segments contain a bare-digit LINE is never written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from . import terminal_parser


class DeliveryOutcome(Enum):
    """Written-state classification of one ``send_to_window`` transaction.

    The three failure outcomes differ in what they PROVE about the pane:

      - ``NOT_WRITTEN`` — a gate refused before any keystroke. Nothing is on the
        pane; nothing was committed.
      - ``DRAFT_WRITTEN`` — the text is in the input box and the Enter was
        deliberately WITHHELD. PROVABLY not committed; the payload is STRANDED
        (which is why it arms the per-window stranded-draft brake in
        ``session.py``).
      - ``COMMIT_UNKNOWN`` — the Enter WAS attempted and the transport reported
        failure. tmux reporting a failed ``send-keys`` does NOT prove the key
        never reached the pty, so the commit is unproven in BOTH directions. The
        user is told exactly that, and the turn IS stamped (see
        ``UserTurnStamp``): a possibly-committed turn must be treated as a turn.
    """

    DELIVERED = "delivered"
    NOT_WRITTEN = "not_written"
    DRAFT_WRITTEN = "draft_written"
    COMMIT_UNKNOWN = "commit_unknown"


@dataclass(frozen=True)
class UserTurnStamp:
    """The single-purpose pre-commit hook request (plan §1.5).

    Carries only the route identity. ``send_to_window`` invokes exactly one
    synchronous ``message_queue.set_route_user_turn_at`` for it, immediately
    before the Enter. A hook exception ⇒ ``DRAFT_WRITTEN``, no Enter, no stamp
    (fail-closed).

    THE INVARIANT, stated so it is actually TRUE (r2 F3): **no PROVABLY-NOT-
    COMMITTED refusal is stamped.** Every ``NOT_WRITTEN`` and ``DRAFT_WRITTEN``
    outcome is decided BEFORE the stamp fires, so neither can carry one. The one
    outcome that CAN is ``COMMIT_UNKNOWN`` — the Enter was attempted and may have
    landed — and it keeps the stamp DELIBERATELY: a possibly-committed turn must
    move the live-prose turn boundary, or a prose block from that turn would be
    posted as if it belonged to the previous one. The stamp is never rolled back
    (``set_route_user_turn_at`` mutates two stores; a rollback is strictly worse
    than the honest disclosure the user already gets).
    """

    user_id: int
    thread_id: int | None
    window_id: str


# ── Refusal reasons ──────────────────────────────────────────────────────
#
# Machine codes. The parser's ``INPUT_BOX_FAILURE_REASONS`` are re-used
# verbatim so the gate's leg names and the copy map can never drift apart
# (a strict key-set-equality test pins it).

REASON_OK: Final = "ok"
REASON_WINDOW_GONE: Final = "window_gone"
REASON_QUARANTINED: Final = "quarantined"
REASON_NOT_CLAUDE: Final = "not_claude"
REASON_PROMPT_PRESENT: Final = "prompt_present"
REASON_CAPTURE_FAILED: Final = "capture_failed"
REASON_CAPTURE_TIMEOUT: Final = "capture_timeout"
REASON_CMD_PROBE_TIMEOUT: Final = "cmd_probe_timeout"
REASON_DEADLINE: Final = "deadline"
REASON_LONE_HOTKEY: Final = "lone_hotkey_segment"
REASON_CONTROL_CHARS: Final = "control_chars"
REASON_STRANDED_DRAFT: Final = "stranded_draft"
REASON_SEND_FAILED: Final = "send_failed"
REASON_REVERIFY_FAILED: Final = "reverify_failed"
REASON_STAMP_FAILED: Final = "stamp_failed"
REASON_ENTER_FAILED: Final = "enter_failed"

# GH #50 PR-2 — the free-text lane (``handlers/free_text``). Declared HERE, not
# there, because ``REFUSAL_COPY`` below is the ONE refusal vocabulary and a
# strict key-set-equality test pins it to ``DELIVERY_REFUSAL_REASONS``: a new
# reason without copy must be a build failure, not a silent empty message.
#
# ONLY the two POST-WRITE outcomes are reasons. Every free-text bail BEFORE the
# first keystroke returns ``None`` and falls through to THIS gate, which then
# owns the refusal (the lane is purely additive — see ``free_text.try_answer``),
# so a "nav failed" / "landing failed" never reaches the user as its own message.
REASON_FREE_TEXT_VERIFY_FAILED: Final = "free_text_verify_failed"
REASON_FREE_TEXT_COMMIT_UNCONFIRMED: Final = "free_text_commit_unconfirmed"

# Every reason the gate can attach to a NOT_WRITTEN / DRAFT_WRITTEN /
# COMMIT_UNKNOWN result.
DELIVERY_REFUSAL_REASONS: Final = (
    frozenset(
        {
            REASON_WINDOW_GONE,
            REASON_QUARANTINED,
            REASON_NOT_CLAUDE,
            REASON_PROMPT_PRESENT,
            REASON_CAPTURE_FAILED,
            REASON_CAPTURE_TIMEOUT,
            REASON_CMD_PROBE_TIMEOUT,
            REASON_DEADLINE,
            REASON_LONE_HOTKEY,
            REASON_CONTROL_CHARS,
            REASON_STRANDED_DRAFT,
            REASON_SEND_FAILED,
            REASON_REVERIFY_FAILED,
            REASON_STAMP_FAILED,
            REASON_ENTER_FAILED,
            REASON_FREE_TEXT_VERIFY_FAILED,
            REASON_FREE_TEXT_COMMIT_UNCONFIRMED,
        }
    )
    | terminal_parser.INPUT_BOX_FAILURE_REASONS
)


# The NEUTRAL post-write copy (plan §1.3, r3 P2-3): a post-write structural
# failure does NOT prove a prompt appeared — it may be a ``/``-command overlay,
# bash-mode rendering, wrap drift, a capture failure, or an ordinary redraw. So
# the copy never over-diagnoses, and NO automatic cleanup is attempted (Esc /
# Ctrl-U have surface-specific semantics — Esc on folder-trust KILLS Claude).
DRAFT_WRITTEN_MSG: Final = (
    "Not delivered — the terminal changed while your message was being typed. "
    "Your text was NOT submitted; if you see it in the input box, clear it "
    "before continuing."
)

# The COMMIT_UNKNOWN copy (r2 F3). ``send_keys`` returning False does NOT prove
# the Enter never reached the pty, so this must NOT claim the message was
# withheld — it says exactly what is known.
COMMIT_UNKNOWN_MSG: Final = (
    "Your message may or may not have been submitted — the terminal didn't "
    "confirm the final Enter. Check the window (/screenshot) before resending, "
    "so you don't send it twice."
)

# The stranded-draft brake (r2 F2). A DRAFT_WRITTEN payload is still sitting in
# the input box with its Enter withheld; the NEXT send would append to it and
# Enter would commit BOTH — including the one the user was told was not
# delivered. Nothing is auto-cleared: Esc has surface-specific semantics (on the
# folder-trust prompt it KILLS Claude), and /esc mid-generation ALSO interrupts
# the run — so the copy states the cost instead of hiding it.
STRANDED_DRAFT_MSG: Final = (
    "Not delivered — an earlier message is still sitting UNSENT in this "
    "window's input box (the bot typed it but withheld Enter). Sending now "
    "would submit both at once. Clear the input box in the terminal (Esc, or "
    "Ctrl+U), then resend. /esc sends that Escape for you — but if Claude is "
    "mid-run it will ALSO interrupt the run."
)

# ── The PR-2 free-text lane's copy ───────────────────────────────────────
#
# A DRAFT_WRITTEN failure in this lane strands the payload inside a LIVE CARD's
# free-text row (not the input box), so the copy names that row and the one safe
# way out. Nothing is auto-cleared: Esc on a card CANCELS the question, which is
# a real side effect the user must choose.
FREE_TEXT_VERIFY_FAILED_MSG: Final = (
    "Not delivered — your message was typed into the card's free-text row but "
    "the terminal changed before it could be submitted, so the bot did NOT press "
    "Enter. Your text is still sitting in that row: clear it in the window (Esc), "
    "then answer the card."
)
FREE_TEXT_COMMIT_UNCONFIRMED_MSG: Final = (
    "Your message may or may not have been submitted as the card's answer — the "
    "terminal didn't confirm it. Check the window (/screenshot) before resending, "
    "so you don't answer twice."
)
_PROMPT_PRESENT_MSG: Final = (
    "Not delivered — Claude is waiting on a prompt in this topic. Answer the "
    "card first (tap an option, or use the ↑/↓/⏎ keys), then resend."
)
_INDETERMINATE_MSG: Final = (
    "Not delivered — couldn't confirm the terminal is at its input box. Check "
    "the window (/screenshot), then resend."
)

# Per-reason, ACTIONABLE copy (the /cost busy-path precedent). Exhaustive over
# DELIVERY_REFUSAL_REASONS — pinned by a strict key-set-equality test.
REFUSAL_COPY: Final[dict[str, str]] = {
    REASON_WINDOW_GONE: "Not delivered — the window is gone (it may have been closed).",
    REASON_QUARANTINED: "",  # session.QUARANTINE_SEND_REFUSED_MSG owns this one
    REASON_NOT_CLAUDE: (
        "Message NOT delivered — Claude isn't running in this window (a bare "
        "shell would EXECUTE your text). Send /update to restart the session, "
        "then resend."
    ),
    REASON_PROMPT_PRESENT: _PROMPT_PRESENT_MSG,
    "prompt_row_is_option": _PROMPT_PRESENT_MSG,
    "tasks_mode": (
        "Not delivered — the terminal is in the background-tasks view, where "
        "Enter opens the task list instead of sending. Press Esc in the window "
        "(or /esc), then resend."
    ),
    "completion_overlay": (
        "Not delivered — an autocomplete overlay is open in the terminal, so "
        "Enter would pick a completion instead of sending. A message ending in "
        "`@word` (or a bare `/command` prefix left in the input box) arms it — "
        "clear the input box and resend without a trailing `@`."
    ),
    REASON_CAPTURE_FAILED: _INDETERMINATE_MSG,
    REASON_CAPTURE_TIMEOUT: _INDETERMINATE_MSG,
    REASON_CMD_PROBE_TIMEOUT: _INDETERMINATE_MSG,
    REASON_DEADLINE: _INDETERMINATE_MSG,
    "capture_empty": _INDETERMINATE_MSG,
    "no_input_box": _INDETERMINATE_MSG,
    "no_prompt_row": _INDETERMINATE_MSG,
    "no_ready_chrome": _INDETERMINATE_MSG,
    REASON_LONE_HOTKEY: (
        "Not delivered — a message that is just a number can be read as a "
        "KEYPRESS by the terminal (it would pick that option on a live prompt). "
        "Send it with a word instead, e.g. `option 1`."
    ),
    REASON_CONTROL_CHARS: (
        "Not delivered — your message contains a control character (an escape "
        "sequence, a tab or a carriage return). The terminal would read those as "
        "KEYPRESSES rather than text — arrow keys, Tab, Enter — so they are never "
        "typed into a pane. Resend without them (normal line breaks are fine)."
    ),
    REASON_STRANDED_DRAFT: STRANDED_DRAFT_MSG,
    # A failed literal write does NOT prove zero bytes reached the pane (r2 F5),
    # so its copy is the NEUTRAL written-state copy, not "failed to send keys".
    REASON_SEND_FAILED: DRAFT_WRITTEN_MSG,
    REASON_REVERIFY_FAILED: DRAFT_WRITTEN_MSG,
    REASON_STAMP_FAILED: DRAFT_WRITTEN_MSG,
    REASON_ENTER_FAILED: COMMIT_UNKNOWN_MSG,
    # PR-2 free-text lane — only the POST-WRITE outcomes; a pre-write bail falls
    # through to this gate and is refused by IT (the additive invariant).
    REASON_FREE_TEXT_VERIFY_FAILED: FREE_TEXT_VERIFY_FAILED_MSG,
    REASON_FREE_TEXT_COMMIT_UNCONFIRMED: FREE_TEXT_COMMIT_UNCONFIRMED_MSG,
}


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome + machine reason + user-facing copy for one delivery attempt."""

    outcome: DeliveryOutcome
    reason: str
    message: str

    @property
    def ok(self) -> bool:
        return self.outcome is DeliveryOutcome.DELIVERED

    @property
    def refused(self) -> bool:
        """True iff the delivery did not CONFIRM (``ok`` is False).

        NOT a claim about the pane: a ``COMMIT_UNKNOWN`` result is "refused" here
        (the bot never confirmed a commit) yet the Enter may in fact have landed.
        Use ``outcome`` when the pane aftermath matters — the stranded-draft
        brake and the caller-abort rules both do.
        """
        return not self.ok

    @property
    def draft_stranded(self) -> bool:
        """True iff the payload may still be sitting in the input box.

        The arming condition for ``session``'s per-window stranded-draft brake.
        ``COMMIT_UNKNOWN`` is included: if that Enter did NOT land, the draft is
        stranded exactly as in ``DRAFT_WRITTEN``, and if it DID the brake's
        empty-input-row self-heal releases it on the next send.
        """
        return self.outcome in (
            DeliveryOutcome.DRAFT_WRITTEN,
            DeliveryOutcome.COMMIT_UNKNOWN,
        )

    @property
    def as_tuple(self) -> tuple[bool, str]:
        """The legacy ``(success, message)`` shape the sync callers still use."""
        return self.ok, self.message


def delivered(message: str) -> DeliveryResult:
    return DeliveryResult(DeliveryOutcome.DELIVERED, REASON_OK, message)


def refuse(
    reason: str,
    *,
    written: bool,
    message: str | None = None,
) -> DeliveryResult:
    """Build a refusal result for ``reason``.

    ``written`` classifies the pane aftermath: ``False`` ⇒ ``NOT_WRITTEN`` (the
    gate failed before any keystroke — clean); ``True`` ⇒ ``DRAFT_WRITTEN``
    (text is sitting in the input box, Enter deliberately withheld). For the
    Enter-was-attempted case use ``commit_unknown`` — it is NOT a refusal in the
    pane's terms.
    """
    outcome = DeliveryOutcome.DRAFT_WRITTEN if written else DeliveryOutcome.NOT_WRITTEN
    copy = message if message is not None else REFUSAL_COPY.get(reason, "")
    if not copy:
        copy = DRAFT_WRITTEN_MSG if written else _INDETERMINATE_MSG
    return DeliveryResult(outcome, reason, copy)


def commit_unknown(reason: str, *, message: str | None = None) -> DeliveryResult:
    """The Enter was ATTEMPTED and its outcome is unproven (r2 F3).

    Distinct from ``refuse(..., written=True)``, which asserts the Enter was
    deliberately WITHHELD. Claiming that here would be a lie: the key may have
    reached the pty before tmux reported the failure.
    """
    copy = message if message is not None else REFUSAL_COPY.get(reason, "")
    return DeliveryResult(
        DeliveryOutcome.COMMIT_UNKNOWN, reason, copy or COMMIT_UNKNOWN_MSG
    )


# ── Payload shaping (the SEGMENT-aware hotkey refusal, plan §1.3) ────────

# ASCII digits ONLY — Unicode digits are not intended (a payload of "٣" is not
# a terminal hotkey).
_RE_LONE_DIGIT_LINE: Final = re.compile(r"^[0-9]$")


def literal_segments(text: str) -> list[str]:
    """The literal writes the mode-aware writer will ACTUALLY emit for ``text``.

    Claude Code's bash mode needs the ``!`` to land FIRST (so the TUI switches
    modes) and the remainder ~1 s later — ``tmux_manager.send_keys`` does that
    two-step, but ONLY when ``literal and enter`` are both true. The GH #50
    writer withholds the Enter, so it reproduces the split itself — and the
    hotkey refusal must therefore inspect the SEGMENTS, not the payload:
    ``!1`` passes a payload-level ``^\\d$`` test yet emits ``"1"`` as its own
    write, exactly the hotkey shape (rig C7: CONFIRMED FIRES).
    """
    if text.startswith("!"):
        rest = text[1:]
        return ["!", rest] if rest else ["!"]
    return [text]


def lone_hotkey_line(text: str) -> str | None:
    """The first bare-digit LINE any emitted segment carries, or ``None``.

    PER-LINE, not per-segment (rig §5 finding 3): a bare-digit LINE inside a
    multi-line single write DOES fire — ``first line\\n2\\nthird line`` written
    as ONE ``send-keys -l`` COMMITTED option 2 on a live picker. So the rule is:
    refuse if ANY LINE of ANY emitted literal segment is an ASCII ``[0-9]``
    fullmatch. This covers ``"1"``, the ``!1`` two-step split, and the
    multi-line case.

    ``"12"`` and a digit embedded WITHIN a longer line are delivered — an
    empirically narrowed, NON-PROOF case (pty chunking could still split a
    write); the residual is disclosed, not closed.
    """
    for segment in literal_segments(text):
        for line in segment.split("\n"):
            if _RE_LONE_DIGIT_LINE.fullmatch(line):
                return line
    return None


# ── The raw-control-byte refusal (peer-review round-5 P1-A) ──────────────
#
# ``tmux send-keys -l`` stops tmux from interpreting KEY NAMES ("Down", "Enter").
# It does NOT neutralize C0/ESC bytes: they reach the pty VERBATIM, and the
# program on the other side is a terminal application that reads them as keys.
# RIG-CONFIRMED (``tmux -L ccrig``, ``cat -v`` in the pane): a payload built with
# ``printf 'A\033[B2B'`` lands as the literal bytes ``A^[[B2B`` — i.e. Claude's
# TUI sees ``A``, a CURSOR-DOWN escape sequence, then ``2``.
#
# That is a COMMIT primitive. ``delivery.lone_hotkey_line`` cannot see it (the
# line is not a lone digit), the pane gate has already passed, and the executor's
# verification runs only AFTER the write — so an embedded ``ESC [ B`` + digit can
# move the cursor off the row we proved and fire a digit HOTKEY (which on a
# single-select-shaped surface COMMITS with no Enter) before anything re-observes
# the pane. The ONLY sound answer is to refuse the payload before any keystroke.
#
# WHAT IS ALLOWED, AND WHY (decided deliberately):
#
#   \n  (LF, 0x0A) — ALLOWED, and load-bearing. A multi-line payload written in
#       ONE ``send-keys -l`` is consumed PASTE-SHAPED by CC and commits WHOLE
#       (rig: 947-char / 9-line and 5 274-char / 30-line payloads both landed
#       intact). Every real voice note and every reply-context quote is
#       multi-line, so refusing LF would break the lane's primary flow. This code
#       does not touch newline handling AT ALL — the byte set below simply
#       excludes 0x0A.
#
#   \t  (TAB, 0x09) — REFUSED. Tab is a live TUI KEY, not whitespace: on a picker
#       it advances the surface, in the input box it drives completion. The rig
#       confirms ``send-keys -l`` passes the raw 0x09 through. Disclosed cost: a
#       pasted tab-indented code snippet is refused (with actionable copy). We do
#       NOT silently strip or convert it — that would change the bytes Claude
#       receives, which is worse than an honest refusal.
#
#   \r  (CR, 0x0D) — REFUSED. CR is Enter at the pty: an embedded one would COMMIT
#       mid-payload. Telegram and the transcription API both emit LF.
#
#   ESC (0x1B), every other C0, DEL (0x7F) and C1 (U+0080–U+009F) — REFUSED. C1 is
#       included because a UTF-8 terminal decodes U+0080–U+009F back into the C1
#       control range.
_RE_UNSAFE_CONTROL: Final = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def unsafe_control_char(text: str) -> str | None:
    """The first control character a terminal would read as a KEY, or ``None``.

    Everything in C0 except LF, plus DEL and C1. See the block comment above for
    the per-character rationale — in particular why ``\\n`` is allowed (paste-
    shaped multi-line payloads are the lane's primary flow) and ``\\t`` is not.

    Applied at BOTH gated seams — ``session.deliver_to_window`` step 0 (which
    owns the single refusal) and ``free_text.try_answer`` (which declines and
    falls through to it) — because the SAME payload reaches ``send_keys`` through
    both, and the hazard is a property of the bytes, not of the lane.
    """
    m = _RE_UNSAFE_CONTROL.search(text)
    return m.group(0) if m else None


def is_bare_slash_payload(text: str) -> bool:
    """True iff ``text`` is a bare ``/command`` (no argument, no whitespace).

    Such a payload legitimately arms the ``/`` completion overlay once written,
    and Enter runs the sorted-first entry — the mechanism ``forward_command_handler``
    has always relied on. The post-write re-verify therefore exempts the ``/``
    arm of the completion-overlay leg for exactly this shape (never the ``@``
    arm, which is pure data loss). See ``terminal_parser._completion_overlay_armed``.
    """
    stripped = text.strip()
    return (
        stripped.startswith("/")
        and len(stripped) > 1
        and not any(c.isspace() for c in stripped)
    )
