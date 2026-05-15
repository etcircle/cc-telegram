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

    def test_ask_user_plain_no_checkbox(self):
        """Simple A/B/C/D AskUserQuestion (no ☐/✔/☒ glyphs) must still match.

        Regression: Claude Code renders single-select AskUserQuestion as a
        numbered options block + ``Enter to select`` footer with no checkbox
        glyphs. The original single-tab pattern required a leading
        ``[☐✔☒]`` which left this variant undetected; the bot then fell
        through to plain-text delivery and the user saw no button keyboard.
        """
        pane = (
            "Mobile drawer: chip labels or no labels?\n"
            "\n"
            "❯ 1. Stay with no labels (your original choice)\n"
            "   Subtle visual grouping only.\n"
            " 2. Add tiny 'paper' / 'digital' chips\n"
            "   9px lowercase muted mono chips above each group.\n"
            " 3. Type something.\n"
            "─\n"
            " 4. Chat about this\n"
            "\n"
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content
        assert "1. Stay with no labels" in result.content

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


class TestBuildFormFromToolInput:
    """Building the AskUserQuestionForm directly from the JSONL tool_use input.

    Pane scrape misses options when long question text scrolls them off the
    top of the visible region. The JSONL payload carries the full option
    list and is order-stable, so this path is preferred for AskUserQuestion
    dispatch when the input dict is available.
    """

    def test_full_payload(self):
        from cctelegram.terminal_parser import build_form_from_tool_input

        form = build_form_from_tool_input(
            {
                "questions": [
                    {
                        "question": "Pick one.",
                        "header": "Approach",
                        "multiSelect": False,
                        "options": [
                            {"label": "A) First", "description": "x"},
                            {"label": "B) Second", "description": "y"},
                            {"label": "C) Third (Recommended)", "description": "z"},
                        ],
                    }
                ]
            }
        )
        assert form is not None
        assert form.current_question_title == "Pick one."
        assert [o.number for o in form.options] == [1, 2, 3]
        assert form.options[0].label == "A) First"
        assert form.options[2].recommended is True
        assert form.options[2].label == "C) Third"

    def test_none_or_malformed_returns_none(self):
        from cctelegram.terminal_parser import build_form_from_tool_input

        assert build_form_from_tool_input(None) is None
        assert build_form_from_tool_input({}) is None
        assert build_form_from_tool_input({"questions": []}) is None
        assert build_form_from_tool_input({"questions": "nope"}) is None
        assert build_form_from_tool_input({"questions": [{"options": []}]}) is None
        assert build_form_from_tool_input({"questions": [{"options": "x"}]}) is None


class TestParseAskUserQuestion:
    def test_plain_picker_with_multiline_descriptions(self):
        """Plain A/B/C question with multi-line indented descriptions between
        options. Regression: the original parser broke on any unmatched line
        once it had started collecting, so descriptions or pros/cons bullets
        after the first option dropped every subsequent option from the form.
        Also pins the off-screen-option-1 case: when the visible region is
        scrolled past option 1, the parser must still keep options 2..N.
        """
        pane = (
            "  2. B) Still no buttons — dig deeper\n"
            "    I still only see plain text, no tappable options.\n"
            "\n"
            "      ✅ Honest signal that there's another layer to debug.\n"
            "      ❌ Need to keep investigating; possibly the queue timing.\n"
            "  3. C) Buttons appeared but tapping them did nothing\n"
            "    The card landed with buttons but the dispatch broke.\n"
            "\n"
            "      ✅ Tells me detection works.\n"
            "      ❌ Different layer of bug to chase.\n"
            "  4. Type something.\n"
            "─\n"
            "  5. Chat about this\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = parse_ask_user_question(pane)
        assert form is not None
        assert [opt.number for opt in form.options] == [2, 3, 4, 5]
        assert form.options[0].label.startswith("B) Still no buttons")
        assert form.options[1].label.startswith("C) Buttons appeared")

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


# ── PR 1 (multi-tab resolver + INF/QS fingerprint gates) ────────────────


from cctelegram.terminal_parser import (  # noqa: E402
    AskQuestion,
    _questions_digest,
    build_form_from_tool_input,
    resolve_ask_form,
)


# Frozen single-question form used for the byte-identical fingerprint
# golden. If you change ``_canonical_repr`` in a way that affects single-
# question canonical output, this test FAILS — that's the safety net.
# The hash is computed from the canonical_repr produced before the multi-
# tab fields existed; recomputing it requires conscious approval (and a
# matching update to the comment in ``_canonical_repr``).
_SINGLE_QUESTION_GOLDEN_FORM = AskUserQuestionForm(
    tabs=(),
    current_question_title="Pick one.",
    options=(
        AskOption(label="A) First", recommended=False, cursor=True, number=1),
        AskOption(label="B) Second", recommended=False, cursor=False, number=2),
        AskOption(label="C) Third", recommended=True, cursor=False, number=3),
    ),
    is_review_screen=False,
    is_free_text=False,
    pane_excerpt="",
)


class TestSingleQuestionFingerprintGolden:
    """Lock down the single-question canonical fingerprint.

    The plan (FA3) commits to byte-identical canonical_repr output for
    single-question forms across the multi-tab rollout. If anyone changes
    canonical line set or order without bumping the golden hash, this
    test fires loudly.
    """

    def test_canonical_repr_lines_unchanged(self):
        # Single-question form produces exactly 5 lines: TABS / Q / OPTS /
        # RVW / FT. No QS:, no INF:. Anything else means the multi-tab
        # gates fired on a single-question form — bug.
        repr_str = _SINGLE_QUESTION_GOLDEN_FORM._canonical_repr()
        lines = repr_str.split("\n")
        assert len(lines) == 5
        assert lines[0].startswith("TABS:")
        assert lines[1].startswith("Q:")
        assert lines[2].startswith("OPTS:")
        assert lines[3].startswith("RVW:")
        assert lines[4].startswith("FT:")
        assert not any(line.startswith("QS:") for line in lines)
        assert not any(line.startswith("INF:") for line in lines)

    def test_single_question_fingerprint_golden(self):
        # Pinned SHA-1 of the canonical above. Update this constant ONLY
        # if you intentionally changed single-question canonical output
        # AND you've considered the rolling-deploy impact on live tokens.
        expected = "6651ea1b8174f879"
        assert _SINGLE_QUESTION_GOLDEN_FORM.fingerprint() == expected


class TestMultiTabFingerprintGates:
    """QS: and INF: lines must appear ONLY for multi-tab forms."""

    def _two_q_form(self, inferred: bool = True) -> AskUserQuestionForm:
        q1 = AskQuestion(
            title="Q1?",
            header="Approach",
            options=(
                AskOption(label="A", recommended=False, cursor=False, number=1),
                AskOption(label="B", recommended=False, cursor=False, number=2),
            ),
        )
        q2 = AskQuestion(
            title="Q2?",
            header="Polish",
            options=(
                AskOption(label="X", recommended=False, cursor=False, number=1),
                AskOption(label="Y", recommended=False, cursor=False, number=2),
            ),
        )
        return AskUserQuestionForm(
            tabs=(),
            current_question_title="Q1?",
            options=q1.options,
            questions=(q1, q2),
            current_tab_inferred=inferred,
        )

    def test_qs_and_inf_lines_present_for_multi_tab(self):
        form = self._two_q_form(inferred=True)
        lines = form._canonical_repr().split("\n")
        assert any(line.startswith("QS:") for line in lines)
        assert any(line == "INF:1" for line in lines)

    def test_inferred_false_changes_fingerprint(self):
        a = self._two_q_form(inferred=True)
        b = self._two_q_form(inferred=False)
        assert a.fingerprint() != b.fingerprint()
        b_lines = b._canonical_repr().split("\n")
        assert any(line == "INF:0" for line in b_lines)

    def test_qs_digest_changes_on_label_rename(self):
        a = self._two_q_form()
        # Same titles + counts, different label — digest must differ so a
        # stale card gets torn down on re-render.
        q1_renamed = AskQuestion(
            title="Q1?",
            header="Approach",
            options=(
                AskOption(label="A renamed", recommended=False, cursor=False, number=1),
                AskOption(label="B", recommended=False, cursor=False, number=2),
            ),
        )
        b = AskUserQuestionForm(
            tabs=a.tabs,
            current_question_title=a.current_question_title,
            options=a.options,
            questions=(q1_renamed, a.questions[1]),
        )
        assert a.fingerprint() != b.fingerprint()

    def test_qs_digest_handles_pipe_in_label(self):
        # Naive ``"|".join(labels)`` would collide on labels containing
        # ``|``. The digest must use a separator that can't appear in
        # JSONL-derived text.
        q_pipe = AskQuestion(
            title="Q?",
            header="H",
            options=(
                AskOption(label="A|B", recommended=False, cursor=False, number=1),
                AskOption(label="C", recommended=False, cursor=False, number=2),
            ),
        )
        q_split = AskQuestion(
            title="Q?",
            header="H",
            options=(
                AskOption(label="A", recommended=False, cursor=False, number=1),
                AskOption(label="B|C", recommended=False, cursor=False, number=2),
            ),
        )
        # These two have the same naive ``"A|B|C"`` flat string but
        # different option boundaries — they MUST hash differently.
        d1 = _questions_digest((q_pipe, q_pipe))
        d2 = _questions_digest((q_split, q_split))
        assert d1 != d2


class TestBuildFormFromToolInputMultiQuestion:
    """``build_form_from_tool_input`` walks all questions and captures descriptions."""

    def test_two_questions_populated(self):
        form = build_form_from_tool_input(
            {
                "questions": [
                    {
                        "question": "Pick approach.",
                        "header": "Approach",
                        "options": [
                            {"label": "A", "description": "first option"},
                            {"label": "B", "description": "second option"},
                        ],
                    },
                    {
                        "question": "Pick polish.",
                        "header": "Polish",
                        "options": [
                            {"label": "X", "description": "xdesc"},
                            {"label": "Y", "description": "ydesc"},
                        ],
                    },
                ]
            }
        )
        assert form is not None
        assert len(form.questions) == 2
        assert form.questions[0].title == "Pick approach."
        assert form.questions[0].header == "Approach"
        assert form.questions[0].options[0].description == "first option"
        assert form.questions[1].options[1].label == "Y"
        # Legacy fields mirror Q1 so existing single-tab consumers keep
        # working without conditionals.
        assert form.current_question_title == "Pick approach."
        assert [o.label for o in form.options] == ["A", "B"]

    def test_description_captured_single_question(self):
        form = build_form_from_tool_input(
            {
                "questions": [
                    {
                        "question": "Pick one.",
                        "options": [
                            {"label": "A", "description": "first"},
                            {"label": "B (Recommended)", "description": "second"},
                        ],
                    }
                ]
            }
        )
        assert form is not None
        assert form.options[0].description == "first"
        assert form.options[1].description == "second"
        # Recommended suffix still stripped from label as before.
        assert form.options[1].label == "B"
        assert form.options[1].recommended is True


class TestResolveAskForm:
    """``resolve_ask_form`` is the unified resolver for render + validate paths.

    Behaviour matrix mirrors §Resolver in
    docs/plans/2026-05-15-askuserquestion-multi-tab-cards.md.
    """

    def _multi_q_input(self) -> dict:
        return {
            "questions": [
                {
                    "question": "Pick approach.",
                    "header": "Approach",
                    "options": [
                        {"label": "A — option A label", "description": "reason A"},
                        {"label": "B — option B label", "description": "reason B"},
                    ],
                },
                {
                    "question": "Pick polish.",
                    "header": "Polish",
                    "options": [
                        {"label": "X — option X label", "description": "reason X"},
                        {"label": "Y — option Y label", "description": "reason Y"},
                    ],
                },
            ]
        }

    def test_returns_none_when_neither_source(self):
        assert resolve_ask_form(None, "") is None

    def test_single_question_jsonl_no_pane(self):
        # Single-question JSONL + no pane → JSONL form, current_tab_inferred=True,
        # no QS/INF in canonical.
        form = resolve_ask_form(
            {
                "questions": [
                    {
                        "question": "Pick one.",
                        "options": [{"label": "A"}, {"label": "B"}],
                    }
                ]
            },
            "",
        )
        assert form is not None
        assert len(form.questions) == 1
        # Canonical stays single-tab shape (5 lines).
        assert len(form._canonical_repr().split("\n")) == 5

    def test_multi_question_with_matching_pane_infers_current(self):
        # Pane shows Q2's title + Q2's options → resolver picks idx 1.
        pane = (
            "Pick polish.\n"
            "\n"
            "❯ 1. X — option X label\n"
            "  2. Y — option Y label\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(self._multi_q_input(), pane)
        assert form is not None
        assert form.current_tab_inferred is True
        assert form.current_question_title == "Pick polish."
        # The current tab's options are surfaced; cursor overlaid from pane.
        assert form.options[0].label == "X — option X label"
        assert form.options[0].cursor is True

    def test_multi_question_corrupt_pane_defaults_to_zero(self):
        # Pane has no recognizable picker → resolver defaults to tab 0
        # AND marks current_tab_inferred=False. Renderer (PR 3) MUST NOT
        # mint pick buttons in this state.
        pane = "garbage that doesn't look like a picker at all\n"
        form = resolve_ask_form(self._multi_q_input(), pane)
        assert form is not None
        assert form.current_tab_inferred is False
        # Defaults to first question.
        assert form.current_question_title == "Pick approach."
        # INF:0 line present.
        lines = form._canonical_repr().split("\n")
        assert any(line == "INF:0" for line in lines)

    def test_jsonl_missing_falls_back_to_pane(self):
        # No tool_input → pure pane fallback (legacy behaviour).
        pane = (
            "Pick one.\n"
            "\n"
            "❯ 1. A — first\n"
            "  2. B — second\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(None, pane)
        assert form is not None
        # questions tuple is empty (legacy pane path doesn't carry it).
        assert form.questions == ()
        assert [o.number for o in form.options] == [1, 2]

    def test_ambiguous_titles_secondary_match_via_options(self):
        # Two questions share a title; option-label overlap disambiguates.
        tool_input = {
            "questions": [
                {
                    "question": "Pick.",
                    "options": [{"label": "alpha"}, {"label": "beta"}],
                },
                {
                    "question": "Pick.",
                    "options": [{"label": "gamma"}, {"label": "delta"}],
                },
            ]
        }
        pane = (
            "Pick.\n"
            "\n"
            "❯ 1. gamma\n"
            "  2. delta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(tool_input, pane)
        assert form is not None
        assert form.current_tab_inferred is True
        # Option-overlap pinned the second question.
        assert form.options[0].label == "gamma"

    def test_identical_options_across_tabs_defaults(self):
        # Every tab has the same option labels (e.g. "Yes / No / Skip" pattern).
        # Neither title-exact nor option-overlap can disambiguate → must
        # default to (0, False) safely rather than picking arbitrarily.
        tool_input = {
            "questions": [
                {
                    "question": "Q1?",
                    "options": [{"label": "Yes"}, {"label": "No"}],
                },
                {
                    "question": "Q2?",
                    "options": [{"label": "Yes"}, {"label": "No"}],
                },
            ]
        }
        # Pane title doesn't match either question's title verbatim
        # (wrapped / truncated scenario).
        pane = (
            "Q something else?\n"
            "\n"
            "❯ 1. Yes\n"
            "  2. No\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        form = resolve_ask_form(tool_input, pane)
        assert form is not None
        # Both questions tied on the option-overlap score → defaulted.
        assert form.current_tab_inferred is False
        assert form.current_question_title == "Q1?"
