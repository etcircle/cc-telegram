"""GH #81 — OSC 8 hyperlinks must not leak onto the ready-status row.

CC ≥2.1.24x wraps the right-aligned ``/rc`` status pill in an OSC 8 hyperlink::

    ESC ] 8 ; id=cbtayp ; https://claude.ai/code/session_…?from=cli ESC \\ /rc ESC ] 8 ; ; ESC \\

``_RE_ANSI_ANY`` is CSI-shaped (``ESC [ or ] <digits/;> <intermediates> <final>``)
and has no OSC branch: it consumed ``ESC ] 8 ; i`` and LEFT
``d=cbtayp;https://…/rc8;;`` sitting on the row as visible text. ``_is_status_row``
then refused the row, and the GH #56 exactly-one-separator fallback — the leg every
tall reply-quote draft depends on — bailed with ``no_input_box``: Enter withheld,
stranded-draft brake armed, "the terminal changed while your message was being
typed".

The GH #73 ``/rc`` fixtures (``inputbox_rc_*_v2.1.246.txt``) were captured WITHOUT
``-e``, so the hyperlink wrapper was never in the corpus. This module pins the real
``-e`` capture.
"""

from __future__ import annotations

from pathlib import Path

from cctelegram import terminal_parser as tp
from cctelegram.handlers.reply_context import ReplyContext, render_for_claude

FIXTURES = Path(__file__).parent / "fixtures"

# The REAL ``tmux capture-pane -e`` bottom rows of a live CC 2.1.251 pane (160x50,
# ``/rc`` active, background-agent panel below the status bar), bytes verbatim:
# blank, top rule, ``❯`` row, bottom rule, status row WITH the OSC 8 ``/rc`` link,
# blank, two agent-panel rows.
FIXTURE_NAME = "inputbox_rc_osc8_agents_v2.1.251.txt"
RAW_TAIL = (FIXTURES / FIXTURE_NAME).read_text(encoding="utf-8")

_TAIL_ROWS = RAW_TAIL.split("\n")
# Row 4 is the status bar; rows 3.. are the box's BOTTOM rule and everything below
# it (bottom rule, status row, blank, agents panel) — the chrome tail a synthesized
# tall-draft pane reuses verbatim.
RAW_STATUS_ROW = _TAIL_ROWS[4]
RAW_BELOW_BOX = "\n".join(_TAIL_ROWS[3:])
RAW_TOP_RULE = _TAIL_ROWS[1]


def _assert_fixture_shape() -> None:
    """The fixture really carries the hyperlink (otherwise every pin is vacuous)."""
    assert "\x1b]8;id=cbtayp;" in RAW_STATUS_ROW
    assert "\x1b\\/rc" in RAW_STATUS_ROW
    assert RAW_STATUS_ROW.endswith("\x1b]8;;\x1b\\")


# ── 1. the status row strips clean and IS a status bar ──────────────────────


def test_status_row_strips_to_the_visible_pill_and_is_a_status_row() -> None:
    """RED before the fix: ``_strip_ansi`` leaked ``d=cbtayp;https://…/rc8;;``."""
    _assert_fixture_shape()
    plain = tp._strip_ansi(RAW_STATUS_ROW)
    assert "https://" not in plain
    assert "\x1b" not in plain
    assert "8;;" not in plain
    assert "d=cbtayp" not in plain
    # Exactly the two visible flex children: the left hint block and the ``/rc``
    # pill, separated by the right-alignment padding.
    left, sep, right = plain.rstrip().rpartition(" ")
    assert sep == " "
    assert right == "/rc"
    assert left.strip() == (
        "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents · ↓ to manage"
    )
    assert tp._is_status_row(RAW_STATUS_ROW) is True


# ── 2. the whole-fixture pre-clean carries no escape residue ────────────────


def test_clean_ghost_input_text_leaves_no_osc_residue_on_the_fixture() -> None:
    """RED before the fix: the ghost tokenizer shares ``_RE_ANSI_ANY``, so the URL
    survived into the cleaned plain text the delivery gate reads."""
    cleaned = tp.clean_ghost_input_text(RAW_TAIL)
    assert "\x1b" not in cleaned
    assert "https://" not in cleaned
    assert "8;;" not in cleaned
    assert "/rc" in cleaned  # the VISIBLE pill text is preserved


# ── 3. the tall reply-quote draft delivers on this pane ─────────────────────

_PROSE = [f"  scrollback prose line {i}" for i in range(23)]

# GH #83 trimmed the reply-quote wrapper from 15 scaffold rows to 5, so a
# one-line quote no longer reaches past the 20-row chrome window at all (that
# is the point of the change). The tall-draft leg this module exercises still
# has to be reachable, so the scenario now quotes a REAL multi-line block —
# the shape that still overflows the window and still needs the GH #56
# upward scan.
_MULTILINE_QUOTE = "\n".join(
    [
        "Wave 2 plan (store + tenancy), as agreed:",
        "",
        "1. Store",
        "   - move the per-tenant blobs behind a single repository facade",
        "   - keep the on-disk layout, only the accessor changes",
        "   - back-fill the index lazily on first read, never at boot",
        "2. Tenancy",
        "   - thread the tenant id through the request scope, not a global",
        "   - every query gets an explicit tenant predicate (no implicit joins)",
        "   - the admin console keeps its cross-tenant read, gated on the role",
        "3. Migration",
        "   - dual-write for one release, then flip the read path",
        "   - the rollback is a config flag, not a schema revert",
        "   - keep the old reader compiled in until the flip is a week old",
        "",
        "Next on your word: wave 2 (store, tenancy).",
    ]
)


def _reply_quote_draft() -> str:
    """The exact ``render_for_claude`` payload a reply-quoted Telegram send types."""
    ctx = ReplyContext(
        original_message_id=3405,
        quoted_text=_MULTILINE_QUOTE,
        original_text=_MULTILINE_QUOTE,
    )
    return render_for_claude("do wave 2", ctx)


def _pane_with_draft(below_box: str) -> tuple[str, str]:
    """A 160x50-shaped pane: prose, top rule, the 21-row draft, then ``below_box``.

    The draft's first row carries the ``❯`` glyph; every continuation row is
    indented and glyph-less, exactly as CC renders a wrapped multi-line draft.
    """
    draft = _reply_quote_draft()
    rows = draft.split("\n")
    assert len(rows) == 21, len(rows)
    # Codex r1 P2-2: this synthesis counts LOGICAL rows, so it is only
    # display-faithful while every logical row fits one 160-column pane row.
    # Each box row is a 2-char prefix ("❯ " / "  ") plus the logical row, so
    # the budget is 158. A longer row would soft-wrap on a real pane and this
    # 50-row pane would be a shape tmux can never produce.
    assert max(len(r) for r in rows) <= 158, max(rows, key=len)
    box = [f"❯ {rows[0]}"] + [f"  {r}" for r in rows[1:]]
    assert all(len(r) <= 160 for r in box)
    pane = "\n".join([*_PROSE, RAW_TOP_RULE, *box, below_box])
    return pane, draft


def test_tall_reply_quote_draft_on_the_osc_pane_is_a_READY_input_box() -> None:
    """RED before the fix: ``no_input_box`` — the GH #56 fallback's step (a) asks
    ``_is_status_row`` about the row below the lone rule, and the leaked URL made
    it False. Enter was withheld and the stranded-draft brake armed."""
    _assert_fixture_shape()
    pane, draft = _pane_with_draft(RAW_BELOW_BOX)
    # The pane really has the tall geometry this bug needs: 50 rows, and exactly
    # ONE rule separator inside the 20-row chrome window (the top rule is above it).
    rows = pane.rstrip("\n").split("\n")
    assert len(rows) == 50
    window = [tp._strip_ansi(r) for r in rows[-tp._CHROME_SCAN_LINES :]]
    assert sum(1 for r in window if tp._is_rule_separator(r)) == 1
    assert tp.classify_input_box_failure(pane, expected_draft=draft) is None
    assert tp.pane_input_box_present(pane, expected_draft=draft) is True


def test_the_osc_bytes_are_the_only_difference_from_a_hyperlink_free_pane() -> None:
    """Parity: hand-removing the OSC wrapper (leaving the visible ``/rc``) gives a
    pane that classifies identically — proof the escape, not the pill, was the
    blocker."""
    stripped_below = RAW_BELOW_BOX.replace(
        "\x1b]8;id=cbtayp;"
        "https://claude.ai/code/session_01Ce23gUHjoCNACkhAGPYxeG?from=cli\x1b\\",
        "",
    ).replace("\x1b]8;;\x1b\\", "")
    assert "\x1b]" not in stripped_below
    pane, draft = _pane_with_draft(stripped_below)
    assert tp.classify_input_box_failure(pane, expected_draft=draft) is None


# ── 4. dim tracking survives the OSC skip (the GH #60 ghost blanking) ───────


def test_a_dim_ghost_row_carrying_an_osc_link_still_blanks_to_a_bare_prompt() -> None:
    """The ghost text is fully dim and the OSC 8 wrapper sits AFTER the ``ESC[0m``.

    Before the fix the leaked ``d=x;https://…`` chars were VISIBLE and NON-dim, so
    the row read as a dim/normal MIX and failed closed — a real ghost row was left
    standing (the GH #60 wedge, back again through the hyperlink)."""
    row = (
        "\x1b[39m❯ \x1b[2m/clear\x1b[0m"
        "\x1b]8;id=x;https://claude.ai/code/session_deadbeef?from=cli\x1b\\"
        "\x1b]8;;\x1b\\"
    )
    cleaned = tp.clean_ghost_input_text(row)
    assert cleaned.strip() == "❯"
    assert "https://" not in cleaned
    # And the SGR machine agrees: the only visible chars are the prompt + the
    # dim ghost, nothing from the OSC payload.
    assert "".join(c for c, _ in tp._visible_chars_with_dim(row)) == "❯ /clear"


# ── 5. both terminators, and the unterminated form is unchanged ─────────────


def test_bel_terminated_osc_is_stripped() -> None:
    assert tp._strip_ansi("a\x1b]0;some window title\x07b") == "ab"
    assert tp._strip_ansi("a\x1b]8;;\x07b") == "ab"
    visible = "".join(c for c, _ in tp._visible_chars_with_dim("a\x1b]0;title\x07b"))
    assert visible == "ab"


def test_an_unterminated_osc_degrades_exactly_as_before_fail_closed() -> None:
    """No BEL and no ``ESC \\`` before end-of-input: ``_RE_OSC`` cannot match, so
    the existing catch-all still eats ``ESC ] 8 ; i`` and leaks the rest. Pinned as
    UNCHANGED behaviour — the fix must not silently start swallowing text that has
    no terminator to bound it."""
    row = "\x1b[39m  tail \x1b]8;id=x;https://example.invalid/rc"
    assert tp._strip_ansi(row) == "  tail d=x;https://example.invalid/rc"


# ── 6. the predicate and the classifier agree on the new fixture ────────────


def test_agreement_predicate_and_classifier_on_the_new_fixture() -> None:
    reason = tp.classify_input_box_failure(RAW_TAIL)
    assert tp.pane_input_box_present(RAW_TAIL) is (reason is None)
    if reason is not None:
        assert reason in tp.INPUT_BOX_FAILURE_REASONS


# ── 7. an unterminated OSC must never consume the FOLLOWING rows ────────────


def test_an_unterminated_osc_cannot_eat_the_next_rows_visible_text() -> None:
    """Codex plan-review P2. ``_strip_ansi`` runs on WHOLE-PANE text, so a payload
    class that allowed ``\\n`` would let an unterminated OSC on row A swallow row B
    up to a later ``ESC \\`` — silently deleting visible rows from the pane the
    delivery gate reads. The class excludes ``\\r`` and ``\\n``, so the damage stays
    on row A."""
    pane = (
        "\x1b[39mrow A \x1b]8;id=x;https://example.invalid/u\n"
        "row B keeps every visible char \x1b\\ visible tail\n"
        "row C untouched\n"
    )
    out = tp._strip_ansi(pane)
    lines = out.split("\n")
    # Row A degrades exactly as today (the catch-all eats ``ESC ] 8 ; i``).
    assert lines[0] == "row A d=x;https://example.invalid/u"
    # Row B is FULLY preserved — nothing before its ``ESC \\`` was consumed.
    assert lines[1] == "row B keeps every visible char  visible tail"
    assert lines[2] == "row C untouched"
