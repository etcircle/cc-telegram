"""Unit tests for ``terminal_parser.clean_ghost_input_text``.

CC 2.1.206 renders a contextual GHOST suggestion in the idle input row —
``❯ ok fix it and let me know when I can test`` — styled ENTIRELY DIM (SGR-2).
A plain tmux capture reads it as a typed draft, so ``pane_looks_idle`` fails
its empty-input-row leg (``input_not_empty``) and ``/cost`` / ``/update``
false-refuse. The pre-clean BLANKS a fully-dim ghost (keeping the bare prompt)
and fails CLOSED — a real draft or a dim/normal mix is left untouched.
"""

from __future__ import annotations

from pathlib import Path

from cctelegram.terminal_parser import (
    classify_pane_idle_failure,
    clean_ghost_input_text,
    pane_looks_idle,
)

FIX = Path(__file__).parent / "fixtures"

# The REAL 2.1.206 ghost capture (raw ANSI, escapes verbatim). It carries a
# ``· 1 shell`` status token, so the fixture pane itself is not restart-idle.
GHOST_FIXTURE = (FIX / "idle_ghost_input_row_v2.1.206.txt").read_text(encoding="utf-8")

# The SAME capture with the background-shell token removed, so the ghost text is
# the SOLE thing keeping ``pane_looks_idle`` from returning True. The raw capture
# interleaves ANSI colour codes around the token (``· \x1b[38;5;44m1 shell``), so
# the removal targets the exact raw form.
GHOST_FIXTURE_NO_SHELL = GHOST_FIXTURE.replace(
    "\x1b[38;5;246m · \x1b[38;5;44m1 shell", ""
)


# ── (a) the real ghost fixture blanks + a full idle pane passes ──────────────


def test_real_ghost_row_is_blanked_to_bare_prompt():
    cleaned = clean_ghost_input_text(GHOST_FIXTURE)
    lines = cleaned.split("\n")
    prompt_rows = [ln for ln in lines if ln.strip().startswith("❯")]
    assert prompt_rows, "no prompt row found in cleaned output"
    # The ghost text is gone — the input row is just the bare prompt.
    for row in prompt_rows:
        assert row.strip() == "❯"
    # And the ghost's literal text no longer appears anywhere.
    assert "ok fix it and let me know" not in cleaned


def test_ghost_cleaned_full_idle_pane_passes_pane_looks_idle():
    # With the shell token removed, the ghost is the ONLY blocker: cleaning it
    # makes the pane read as genuinely idle.
    cleaned = clean_ghost_input_text(GHOST_FIXTURE_NO_SHELL)
    assert classify_pane_idle_failure(cleaned) is None
    assert pane_looks_idle(cleaned) is True


def test_uncleaned_ghost_pane_reads_not_empty_without_the_fix():
    # Sanity pin on the ROOT CAUSE: strip ANSI WITHOUT the ghost-blanking (feed
    # the fixture straight through pane_looks_idle would still have raw escapes;
    # instead simulate the pre-fix path — a plain-capture-equivalent read of the
    # ghost line reports a non-empty input row).
    from cctelegram.terminal_parser import _strip_ansi

    plain = _strip_ansi(GHOST_FIXTURE_NO_SHELL)
    assert classify_pane_idle_failure(plain) == "input_not_empty"


# ── (b) a real typed draft is left untouched → still refuses ────────────────

# The REAL 2.1.206 at-rest draft capture (raw ANSI, escapes verbatim): a typed
# unsent draft renders at DEFAULT intensity — ``ESC[39m❯ RIGTEST dim probe do
# not send`` — with NO SGR-2 anywhere; the mid-run probe of the same draft
# rendered ``ESC[38;5;246m❯ ESC[39m<text>``, also non-dim. Empirical proof that
# on 2.1.206 dim is EXCLUSIVELY the ghost suggestion (codex P2 evidence).
REAL_DRAFT_FIXTURE = (FIX / "idle_real_draft_input_row_v2.1.206.txt").read_text(
    encoding="utf-8"
)


def test_real_draft_fixture_row_is_never_blanked():
    cleaned = clean_ghost_input_text(REAL_DRAFT_FIXTURE)
    # The typed draft text survives verbatim on the prompt row.
    assert "RIGTEST dim probe do not send" in cleaned
    prompt_rows = [ln for ln in cleaned.split("\n") if ln.strip().startswith("❯")]
    assert prompt_rows and "RIGTEST" in prompt_rows[0]
    # And the idle gate still refuses on the draft.
    assert classify_pane_idle_failure(cleaned) == "input_not_empty"
    assert pane_looks_idle(cleaned) is False


def test_real_draft_fixture_without_running_status_line_still_refuses():
    # Drop the spinner/status first line (`✻ Sautéed … 1 shell still running`)
    # so the draft row is evaluated in a fully at-rest pane shape.
    lines = REAL_DRAFT_FIXTURE.split("\n")
    at_rest = "\n".join(ln for ln in lines if "Sautéed" not in ln)
    assert "Sautéed" not in at_rest  # the variant actually removed the line
    cleaned = clean_ghost_input_text(at_rest)
    assert "RIGTEST dim probe do not send" in cleaned
    assert classify_pane_idle_failure(cleaned) == "input_not_empty"
    assert pane_looks_idle(cleaned) is False


def _draft_pane(text: str) -> str:
    # A normal-intensity (NON-dim) draft after the prompt, otherwise an
    # idle-shaped pane. Built by substituting the ghost span for normal text.
    return GHOST_FIXTURE_NO_SHELL.replace(
        "\x1b[2mok fix it and let me know when I can test\x1b[0m",
        f"{text}",
    )


def test_real_draft_normal_intensity_is_untouched():
    draft = _draft_pane("please run the tests")
    cleaned = clean_ghost_input_text(draft)
    # The draft text survives (it is a genuine unsent draft).
    assert "please run the tests" in cleaned
    assert classify_pane_idle_failure(cleaned) == "input_not_empty"
    assert pane_looks_idle(cleaned) is False


# ── (c) a dim/normal MIX after the prompt fails CLOSED (untouched) ──────────


def test_mixed_dim_and_normal_is_untouched():
    # ``❯ <dim>ghost</dim><normal>real</normal>`` — a genuine draft partly
    # overlapping a suggestion. ANY non-dim char after the prompt ⇒ leave it.
    mixed = GHOST_FIXTURE_NO_SHELL.replace(
        "\x1b[2mok fix it and let me know when I can test\x1b[0m",
        "\x1b[2mghost part \x1b[0mreal typed part",
    )
    cleaned = clean_ghost_input_text(mixed)
    assert "real typed part" in cleaned
    assert classify_pane_idle_failure(cleaned) == "input_not_empty"


# ── (d) dim state tracking across interleaved codes ────────────────────────


def test_color_codes_inside_dim_span_still_count_as_all_dim():
    # The real shape: ``ESC[39m❯ ESC[2m...ESC[0m`` — a color select (39) precedes
    # the dim start, and colors interleaved INSIDE the dim span must not clear it.
    line = "\x1b[39m❯\xa0\x1b[2mall \x1b[38;5;244mdim \x1b[36mtext\x1b[0m"
    cleaned = clean_ghost_input_text(line)
    assert cleaned.strip() == "❯"


def test_sgr_22_ends_dim_so_trailing_text_is_a_real_draft():
    # ``ESC[2mghost ESC[22mreal`` — SGR-22 (normal intensity) clears dim, so
    # ``real`` is non-dim ⇒ MIX ⇒ untouched.
    line = "\x1b[39m❯ \x1b[2mghost \x1b[22mreal\x1b[0m"
    cleaned = clean_ghost_input_text(line)
    assert "real" in cleaned
    assert "ghost" in cleaned  # nothing blanked (fail closed)


def test_reset_all_zero_ends_dim():
    # ``ESC[2mghost ESC[0mreal`` — reset-all clears dim, so ``real`` is a mix.
    line = "\x1b[39m❯ \x1b[2mghost \x1b[0mreal"
    cleaned = clean_ghost_input_text(line)
    assert "real" in cleaned and "ghost" in cleaned


# ── (d2) COMBINED SGR forms must reach the state machine (codex P3) ─────────
# A substring probe for ``ESC[2m`` misses valid combined parameter lists; the
# helper must classify these as dim and blank the ghost.


def test_combined_bold_dim_1_2m_classifies_dim():
    line = "\x1b[39m❯ \x1b[1;2mghost suggestion\x1b[0m"
    assert clean_ghost_input_text(line).strip() == "❯"


def test_combined_reset_then_dim_0_2m_applies_dim():
    # ``0;2`` = reset-all THEN dim → dim is active for the following text.
    line = "\x1b[39m❯ \x1b[0;2mghost suggestion\x1b[0m"
    assert clean_ghost_input_text(line).strip() == "❯"


def test_combined_color_then_dim_38_5_244_2m_classifies_dim():
    # A 256-colour selector followed by dim in ONE param list — the selector's
    # sub-params are consumed, then the trailing ``2`` applies dim.
    line = "\x1b[39m❯ \x1b[38;5;244;2mghost suggestion\x1b[0m"
    assert clean_ghost_input_text(line).strip() == "❯"


def test_combined_reset_then_22_stays_normal_intensity():
    # ``0;22`` nets normal intensity — following text is NON-dim → untouched.
    line = "\x1b[39m❯ \x1b[2m\x1b[0;22mreal draft text"
    cleaned = clean_ghost_input_text(line)
    assert "real draft text" in cleaned


def test_dim_then_combined_clear_2_22m_ends_dim():
    # A single list that sets then clears (``2;22``) nets NON-dim.
    line = "\x1b[39m❯ \x1b[2;22mreal draft text"
    cleaned = clean_ghost_input_text(line)
    assert "real draft text" in cleaned


# ── (e) no input row / no ANSI passes through equivalently ─────────────────


def test_plain_text_no_ansi_no_input_row_passthrough():
    plain = "Here is some analysis:\n\n  1. Bad    2. Fine\n"
    assert clean_ghost_input_text(plain) == plain


def test_plain_ansi_non_prompt_line_is_only_stripped():
    # A dim status line that is NOT the input row (no leading prompt glyph) is
    # ANSI-stripped but its text is preserved.
    line = "  \x1b[2m✻ Cooked for 2s\x1b[0m"
    assert clean_ghost_input_text(line) == "  ✻ Cooked for 2s"


def test_empty_and_none_inputs():
    assert clean_ghost_input_text(None) == ""
    assert clean_ghost_input_text("") == ""


def test_bare_prompt_no_ghost_unchanged():
    line = "\x1b[39m❯\x1b[0m"
    cleaned = clean_ghost_input_text(line)
    assert cleaned.strip() == "❯"


def test_pane_without_ansi_or_ghost_is_byte_equivalent_to_strip():
    # A plain idle pane (no dim anywhere) must round-trip unchanged.
    sep = "─" * 56
    idle = f"✻ Cooked for 2s\n\n{sep}\n❯\n{sep}\n  ⏵⏵ bypass permissions on\n"
    assert clean_ghost_input_text(idle) == idle
    assert pane_looks_idle(clean_ghost_input_text(idle)) is True
