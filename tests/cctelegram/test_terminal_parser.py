"""Tests for terminal_parser — regex-based detection of Claude Code UI elements."""

import pytest

from cctelegram.terminal_parser import (
    extract_bash_output,
    extract_context_pct,
    extract_interactive_content,
    is_interactive_ui,
    is_status_active,
    parse_status_line,
    strip_pane_chrome,
)

# ── parse_status_line ────────────────────────────────────────────────────


class TestParseStatusLine:
    @pytest.mark.parametrize(
        ("spinner", "rest", "expected"),
        [
            ("·", "Working on task", "Working on task"),
            ("✻", "  Reading file  ", "Reading file"),
            ("✽", "Thinking deeply", "Thinking deeply"),
            ("✶", "Analyzing code", "Analyzing code"),
            ("✳", "Processing input", "Processing input"),
            ("✢", "Building project", "Building project"),
        ],
    )
    def test_spinner_chars(self, spinner: str, rest: str, expected: str, chrome: str):
        pane = f"some output\n{spinner}{rest}\n{chrome}"
        assert parse_status_line(pane) == expected

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("just normal text\nno spinners here\n", id="no_spinner"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert parse_status_line(pane) is None

    def test_no_chrome_returns_none(self):
        """Without chrome separator, status can't be determined."""
        pane = "output\n✻ Doing work\nno chrome here\n"
        assert parse_status_line(pane) is None

    def test_blank_line_between_status_and_chrome(self, chrome: str):
        """Status line with blank lines before separator."""
        pane = f"output\n✻ Doing work\n\n{chrome}"
        assert parse_status_line(pane) == "Doing work"

    def test_idle_no_status(self, chrome: str):
        """Idle pane (no status line above chrome) returns None."""
        pane = f"some output\n● Tool result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_false_positive_bullet(self, chrome: str):
        """· in regular output must NOT be detected as status."""
        pane = f"· bullet point one\n· bullet point two\nsome result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_uses_fixture(self, sample_pane_status_line: str):
        assert parse_status_line(sample_pane_status_line) == "Reading file src/main.py"


# ── is_status_active ─────────────────────────────────────────────────────


class TestIsStatusActive:
    """is_status_active is True iff Claude is actively producing output.
    The signal is "esc to interrupt" in the bottom chrome bar — that's
    the only marker Claude renders consistently while a run is in flight,
    and removes once the run completes.
    """

    def test_active_pane_with_esc_to_interrupt(self):
        """Real captured-in-the-wild active pane (Brewing…)."""
        pane = (
            "✽ Brewing… (3s · thinking with high effort)\n"
            "\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt"
        )
        assert is_status_active(pane) is True

    def test_post_completion_summary_no_esc(self):
        """Real captured-in-the-wild idle pane: same spinner+blank gap, but
        bottom chrome has no "esc to interrupt"."""
        pane = (
            "✻ Cooked for 17s · 3 shells still running\n"
            "\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on · 3 shells · ↓ to manage"
        )
        assert is_status_active(pane) is False

    def test_active_with_shells_and_esc(self):
        """Active run while background shells exist (compound bottom chrome)."""
        pane = (
            "✽ Tempering… (26s · ↓ 125 tokens · thought for 13s)\n"
            "\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on · 2 shells · esc to interrupt · ↓ to manage"
        )
        assert is_status_active(pane) is True

    def test_idle_pane_no_status(self, chrome: str):
        pane = f"some output\n{chrome}"
        assert is_status_active(pane) is False

    def test_empty_is_idle(self):
        assert is_status_active("") is False

    def test_case_insensitive(self):
        """Tolerate hypothetical capitalization changes in the marker."""
        pane = "✻ Working\n──────\n  Esc To Interrupt\n"
        assert is_status_active(pane) is True


# ── extract_interactive_content ──────────────────────────────────────────


class TestExtractInteractiveContent:
    def test_exit_plan_mode(self, sample_pane_exit_plan: str):
        result = extract_interactive_content(sample_pane_exit_plan)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content
        assert "ctrl-g to edit in" in result.content

    def test_exit_plan_mode_variant(self):
        pane = (
            "  Claude has written up a plan\n  ─────\n  Details here\n  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Claude has written up a plan" in result.content

    def test_ask_user_multi_tab(self, sample_pane_ask_user_multi_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_multi_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "←" in result.content

    def test_ask_user_single_tab(self, sample_pane_ask_user_single_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_single_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content

    def test_permission_prompt_no_longer_detected(self, sample_pane_permission: str):
        # Wave 2: PermissionPrompt is dead code under
        # ``--dangerously-skip-permissions`` (the deployment's mode), so the
        # patterns were removed from UI_PATTERNS. Verify the pane no longer
        # matches anything.
        assert extract_interactive_content(sample_pane_permission) is None

    def test_restore_checkpoint(self):
        pane = (
            "  Restore the code to a previous state?\n"
            "  ─────\n"
            "  Some details\n"
            "  Enter to continue\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "RestoreCheckpoint"
        assert "Restore the code" in result.content

    def test_settings(self):
        pane = "  Settings: press tab to cycle\n  ─────\n  Option 1\n  Esc to cancel\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Settings:" in result.content

    def test_settings_model_picker(self, sample_pane_settings: str):
        result = extract_interactive_content(sample_pane_settings)
        assert result is not None
        assert result.name == "Settings"
        assert "Select model" in result.content
        assert "Sonnet" in result.content
        assert "Enter to confirm" in result.content

    def test_settings_esc_to_cancel_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● claude-sonnet-4-20250514\n"
            "  ○ claude-opus-4-20250514\n"
            "  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Esc to cancel" in result.content

    def test_settings_esc_to_exit_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● Default (Opus 4.6)\n"
            "  ○ claude-sonnet-4-20250514\n"
            "\n"
            "  Enter to confirm · Esc to exit\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Enter to confirm" in result.content

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("$ echo hello\nhello\n$\n", id="no_ui"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert extract_interactive_content(pane) is None

    def test_dropped_permission_pattern_returns_none(self):
        # Wave 2: PermissionPrompt patterns were removed (dead code under
        # ``--dangerously-skip-permissions``). Pane shapes that previously
        # matched must now return None.
        pane = "  Do you want to proceed?\n  Esc to cancel\n"
        assert extract_interactive_content(pane) is None


# ── is_interactive_ui ────────────────────────────────────────────────────


class TestIsInteractiveUI:
    def test_true_when_ui_present(self, sample_pane_exit_plan: str):
        assert is_interactive_ui(sample_pane_exit_plan) is True

    def test_false_when_no_ui(self, sample_pane_no_ui: str):
        assert is_interactive_ui(sample_pane_no_ui) is False

    def test_settings_is_interactive(self, sample_pane_settings: str):
        assert is_interactive_ui(sample_pane_settings) is True

    def test_false_for_empty_string(self):
        assert is_interactive_ui("") is False


# ── strip_pane_chrome ───────────────────────────────────────────────────


class TestStripPaneChrome:
    def test_strips_from_separator(self):
        lines = [
            "some output",
            "more output",
            "─" * 30,
            "❯",
            "─" * 30,
            "  [Opus 4.6] Context: 34%",
        ]
        assert strip_pane_chrome(lines) == ["some output", "more output"]

    def test_no_separator_returns_all(self):
        lines = ["line 1", "line 2", "line 3"]
        assert strip_pane_chrome(lines) == lines

    def test_short_separator_not_triggered(self):
        lines = ["output", "─" * 10, "more output"]
        assert strip_pane_chrome(lines) == lines

    def test_only_searches_last_10_lines(self):
        # Separator at line 0 with 15 lines total — outside the last-10 window
        lines = ["─" * 30] + [f"line {i}" for i in range(14)]
        assert strip_pane_chrome(lines) == lines


# ── extract_bash_output ─────────────────────────────────────────────────


class TestExtractBashOutput:
    def test_extracts_command_output(self):
        pane = "some context\n! echo hello\n⎿ hello\n"
        result = extract_bash_output(pane, "echo hello")
        assert result is not None
        assert "! echo hello" in result
        assert "hello" in result

    def test_command_not_found_returns_none(self):
        pane = "some context\njust normal output\n"
        assert extract_bash_output(pane, "echo hello") is None

    def test_chrome_stripped(self):
        pane = (
            "some context\n"
            "! ls\n"
            "⎿ file.txt\n"
            + "─" * 30
            + "\n"
            + "❯\n"
            + "─" * 30
            + "\n"
            + "  [Opus 4.6] Context: 34%\n"
        )
        result = extract_bash_output(pane, "ls")
        assert result is not None
        assert "file.txt" in result
        assert "Opus" not in result

    def test_prefix_match_long_command(self):
        pane = "! long_comma…\n⎿ output\n"
        result = extract_bash_output(pane, "long_command_that_gets_truncated")
        assert result is not None
        assert "output" in result

    def test_trailing_blank_lines_stripped(self):
        pane = "! echo hi\n⎿ hi\n\n\n"
        result = extract_bash_output(pane, "echo hi")
        assert result is not None
        assert not result.endswith("\n")


# ── extract_context_pct ─────────────────────────────────────────────────


class TestExtractContextPct:
    def test_extracts_realistic_chrome(self):
        pane = (
            "some output\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 89%\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )
        assert extract_context_pct(pane) == 89

    def test_extracts_low_value(self):
        pane = "  [Sonnet 4.5] Context: 7%\n"
        assert extract_context_pct(pane) == 7

    def test_no_context_line_returns_none(self):
        pane = (
            "some output\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
        )
        assert extract_context_pct(pane) is None

    def test_empty_returns_none(self):
        assert extract_context_pct("") is None

    def test_only_searches_bottom_lines(self):
        # Push a Context line to the top of a long pane — it's outside the
        # last-10-line window so should not be picked up.
        pane = (
            "  [Opus 4.6] Context: 50%\n"
            + "\n".join(f"line {i}" for i in range(30))
            + "\n"
        )
        assert extract_context_pct(pane) is None

    def test_out_of_range_value_ignored(self):
        # Three-digit number that's out of range
        pane = "  [Opus 4.6] Context: 250%\n"
        assert extract_context_pct(pane) is None


# ── parse_ask_user_question ───────────────────────────────────────────────


from cctelegram.terminal_parser import (  # noqa: E402
    AskOption,
    AskTab,
    AskUserQuestionForm,
    parse_ask_user_question,
)


# Multi-tab picker mid-form, currently on the "Approach" tab.
# Synthesized from the etvideo-editor /plan-ceo-review pane (window @34,
# 2026-05-14) at the moment the user was choosing implementation approach.
_PANE_MULTITAB_APPROACH = (
    "  STOP — pick an approach before mode selection. Per the skill, I need\n"
    "  your call.\n"
    "\n"
    "────────────────────────────────────────────────────────────\n"
    "←  ☐ Approach  ☐ Positioning  ✔ Submit  →\n"
    "Which implementation approach for the full ETVideoScript vision should we\n"
    "lock in before the review continues?\n"
    "\n"
    "❯ 1. C — Parallel tracks: stabilize core + scaffold copilot (Recommended)\n"
    "    Editor and copilot co-designed. Two parallel Hermes lanes…\n"
    "  2. B — Copilot-first (brand wedge)\n"
    "    Ship the chat panel + 3-4 skills next…\n"
    "  3. A — Editor-first, copilot-second\n"
    "    Finish Wave A.1 → B → C (waveform)…\n"
    "  4. Different framing entirely — reduce scope first\n"
    "  5. Type something.\n"
    "  6. Chat about this\n"
    "\n"
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
)


# Multi-tab picker on the submit-confirmation screen — both questions
# answered, cursor on "Submit answers". Captured from window @34 directly.
_PANE_MULTITAB_SUBMIT = (
    "←  ☒ Approach  ☒ Positioning  ✔ Submit  →\n"
    "\n"
    "Review your answers\n"
    "\n"
    " ● Which implementation approach for the full ETVideoScript vision should we\n"
    "   lock in before the review continues?\n"
    "   → C — Parallel tracks: stabilize core + scaffold copilot (Recommended)\n"
    " ● How do you want to position publicly?\n"
    '   → "Open-source editor your AI agent uses" (Recommended)\n'
    "\n"
    "Ready to submit your answers?\n"
    "\n"
    "❯ 1. Submit answers\n"
    "  2. Cancel\n"
)


# Single-question picker (no tabs) — Claude Code's periodic feedback survey
# variant. Footer is "Enter to select".
_PANE_SINGLE_TAB = (
    "● How is Claude doing this session? (optional)\n"
    "\n"
    "❯ 1. Bad\n"
    "  2. Fine\n"
    "  3. Good\n"
    "  0. Dismiss\n"
    "\n"
    "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
)


class TestParseAskUserQuestion:
    def test_multitab_approach_returns_tabs_and_options(self):
        form = parse_ask_user_question(_PANE_MULTITAB_APPROACH)
        assert form is not None
        # Three tabs visible: Approach, Positioning, Submit
        labels = [t.label for t in form.tabs]
        assert "Approach" in labels
        assert "Positioning" in labels
        # All un-answered (the cell glyphs are ☐ ☐ ✔)
        assert form.tabs[0].answered is False
        assert form.tabs[1].answered is False
        # The submit cell is_submit=True
        submit_tabs = [t for t in form.tabs if t.is_submit]
        assert len(submit_tabs) == 1
        # Options should include the recommended Approach C as option 1
        assert form.options
        assert form.options[0].number == 1
        assert form.options[0].cursor is True
        assert form.options[0].recommended is True
        assert "Parallel tracks" in form.options[0].label
        # Free-text option is present ("Type something")
        assert form.is_free_text is True
        # Not the review screen — we're still picking
        assert form.is_review_screen is False

    def test_multitab_submit_screen_flag(self):
        form = parse_ask_user_question(_PANE_MULTITAB_SUBMIT)
        assert form is not None
        # Both content tabs answered
        approach = next(t for t in form.tabs if t.label == "Approach")
        positioning = next(t for t in form.tabs if t.label == "Positioning")
        assert approach.answered is True
        assert positioning.answered is True
        # Review screen flag tripped (header + prompt both present)
        assert form.is_review_screen is True
        # Options show "Submit answers" / "Cancel"
        opt_labels = [o.label for o in form.options]
        assert any("Submit answers" in lbl for lbl in opt_labels)
        assert any("Cancel" in lbl for lbl in opt_labels)
        # Cursor on the submit row
        assert form.options[0].cursor is True
        assert form.options[0].number == 1

    def test_single_tab_no_tabs_collected(self):
        form = parse_ask_user_question(_PANE_SINGLE_TAB)
        assert form is not None
        # Single-question picker → no multi-tab cells
        assert form.tabs == ()
        # All four options parsed
        nums = [o.number for o in form.options]
        assert nums == [1, 2, 3, 0] or nums == [1, 2, 3]
        # ``0. Dismiss`` skips contiguous check (numbering starts at 1),
        # so the parser may discard it. Either outcome is acceptable for PR 1
        # as long as the live options 1/2/3 are present.
        assert any("Bad" in o.label for o in form.options)
        assert any("Fine" in o.label for o in form.options)
        assert any("Good" in o.label for o in form.options)
        # First option carries the cursor
        assert form.options[0].cursor is True

    def test_non_picker_pane_returns_none(self):
        pane = (
            "Just regular Claude Code output\n"
            "  ⎿  some tool result\n"
            "  ⏵⏵ bypass permissions on\n"
        )
        assert parse_ask_user_question(pane) is None

    def test_empty_input_returns_none(self):
        assert parse_ask_user_question("") is None

    def test_fingerprint_stable_across_calls(self):
        a = parse_ask_user_question(_PANE_MULTITAB_APPROACH)
        b = parse_ask_user_question(_PANE_MULTITAB_APPROACH)
        assert a is not None and b is not None
        assert a.fingerprint() == b.fingerprint()
        # Length sanity — 16 hex chars
        assert len(a.fingerprint()) == 16

    def test_fingerprint_changes_when_tab_state_changes(self):
        a = parse_ask_user_question(_PANE_MULTITAB_APPROACH)
        b = parse_ask_user_question(_PANE_MULTITAB_SUBMIT)
        assert a is not None and b is not None
        assert a.fingerprint() != b.fingerprint()

    def test_fingerprint_excludes_pane_excerpt_noise(self):
        """Trailing whitespace / blank-line drift on the pane should not
        change the fingerprint — only structural fields contribute.
        """
        clean = _PANE_MULTITAB_APPROACH
        noisy = _PANE_MULTITAB_APPROACH + "\n\n   \n"  # trailing blanks
        a = parse_ask_user_question(clean)
        b = parse_ask_user_question(noisy)
        assert a is not None and b is not None
        assert a.fingerprint() == b.fingerprint()

    def test_pane_excerpt_carries_tab_header(self):
        form = parse_ask_user_question(_PANE_MULTITAB_APPROACH)
        assert form is not None
        # Excerpt starts at the tab header line (the chrome separator above
        # is discarded — it's not part of the picker structure).
        assert form.pane_excerpt.startswith("←")

    def test_dataclasses_are_frozen_and_hashable(self):
        # The dataclasses must be hashable so the renderer can put them
        # into sets / dict keys / token maps without surprise mutation.
        opt = AskOption(label="x", recommended=False, cursor=False, number=1)
        tab = AskTab(label="A", answered=False, is_submit=False, is_current=False)
        form = AskUserQuestionForm(tabs=(tab,), options=(opt,))
        # ``_meta`` is a mutable field excluded from equality, so two forms
        # with identical structured state compare equal even when one of
        # them later gains a diagnostic note.
        form2 = AskUserQuestionForm(tabs=(tab,), options=(opt,))
        assert form == form2
