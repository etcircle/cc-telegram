"""Exhaustiveness + wording tests for the /cost fallback action-copy map.

Every reason the interceptor can classify MUST have a mapped, reason-specific
action line — a reason without copy fails the test. The wording is pinned so a
review can't silently regress the truthful-conditional draft line into the
inaccurate "wait for the next turn" / "needs an idle session" phrasing.
"""

from __future__ import annotations

from cctelegram import bot as bot_module
from cctelegram import terminal_parser


class TestExhaustiveness:
    def test_copy_map_key_set_equals_canonical_reason_set(self):
        # STRICT set equality on the MAP ITSELF (review r1 P2 — the helper's
        # graceful default made a keys-through-the-helper check vacuous:
        # deleting a map entry still passed). A reason without copy, or copy
        # for a reason not in the canonical set, both fail.
        assert set(bot_module._USAGE_FALLBACK_ACTION) == set(
            bot_module.USAGE_FALLBACK_REASONS
        )

    def test_every_classifier_reason_maps_to_copy(self):
        # Tie the canonical set to the CLASSIFIER's reason values: every leg
        # name classify_pane_idle_failure can emit must land on a copy-mapped
        # fallback reason — directly (positive hazards) or via the bot's
        # indeterminate normalization (mid-redraw legs → chrome_indeterminate).
        # A NEW classifier reason without copy fails here.
        checked = 0
        for leg in terminal_parser.PANE_IDLE_FAILURE_REASONS:
            normalized = (
                "chrome_indeterminate" if leg in bot_module._INDETERMINATE_LEGS else leg
            )
            assert normalized in bot_module._USAGE_FALLBACK_ACTION, (
                f"classifier leg {leg!r} normalizes to {normalized!r} "
                "which has no mapped action copy"
            )
            checked += 1
        assert checked >= 7  # guard against a vacuous pass

    def test_classifier_reason_constant_matches_observed_legs(self):
        # The constant must cover every leg the classifier actually returns on
        # the canonical fixtures (mint/validate parity for the tie itself).
        from tests.cctelegram.test_pane_looks_idle import (
            BLOCKQUOTE_BETWEEN_SEPARATORS,
            BODY_BLOCKQUOTE_MIDREDRAW,
            IDLE_PANE_BG_SHELLS,
            IDLE_PANE_TYPED,
            MIDREDRAW_NO_STATUS,
        )

        observed = {
            terminal_parser.classify_pane_idle_failure(p)
            for p in (
                None,
                "just prose\nwith no chrome",
                IDLE_PANE_TYPED,
                MIDREDRAW_NO_STATUS,
                BODY_BLOCKQUOTE_MIDREDRAW,
                BLOCKQUOTE_BETWEEN_SEPARATORS,
                IDLE_PANE_BG_SHELLS,
            )
        }
        observed.discard(None)
        assert observed <= terminal_parser.PANE_IDLE_FAILURE_REASONS

    def test_unknown_reason_falls_back_gracefully(self):
        # A defensive runtime default — never a crash on an unmapped reason
        # (exhaustiveness is enforced by the map-level tests above, not here).
        line = bot_module.usage_fallback_action_line("some_future_reason")
        assert isinstance(line, str) and line.strip()


class TestReasonSpecificWording:
    def test_active_status_says_turn_ends(self):
        line = bot_module.usage_fallback_action_line("active_status").lower()
        assert "working" in line or "turn ends" in line
        assert "unsent draft" not in line

    def test_input_not_empty_is_truthful_conditional_draft(self):
        line = bot_module.usage_fallback_action_line("input_not_empty").lower()
        # The truthful-conditional submit-or-clear text.
        assert "draft" in line
        assert "submit" in line or "clear" in line
        # Never the inaccurate phrasings the r2 catch rejects.
        assert "wait for the next" not in line
        assert "needs an idle session" not in line

    def test_interactive_says_answer_the_prompt(self):
        for reason in ("interactive", "interactive_surface"):
            line = bot_module.usage_fallback_action_line(reason).lower()
            assert "prompt" in line
            assert "answer" in line

    def test_background_shells_says_defer(self):
        line = bot_module.usage_fallback_action_line("background_shells").lower()
        assert "background shell" in line

    def test_transient_reasons_say_try_again(self):
        for reason in ("lock_busy", "capture_failed", "capture_timeout"):
            line = bot_module.usage_fallback_action_line(reason).lower()
            assert "try again" in line

    def test_chrome_indeterminate_says_read_cleanly(self):
        line = bot_module.usage_fallback_action_line("chrome_indeterminate").lower()
        assert "read the terminal" in line or "couldn't read" in line
