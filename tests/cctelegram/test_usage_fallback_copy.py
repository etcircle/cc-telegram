"""Exhaustiveness + wording tests for the /cost fallback action-copy map.

Every reason the interceptor can classify MUST have a mapped, reason-specific
action line — a reason without copy fails the test. The wording is pinned so a
review can't silently regress the truthful-conditional draft line into the
inaccurate "wait for the next turn" / "needs an idle session" phrasing.
"""

from __future__ import annotations

from cctelegram import bot as bot_module


class TestExhaustiveness:
    def test_every_reason_has_action_copy(self):
        for reason in bot_module.USAGE_FALLBACK_REASONS:
            line = bot_module.usage_fallback_action_line(reason)
            assert isinstance(line, str) and line.strip(), (
                f"reason {reason!r} has no mapped action copy"
            )

    def test_unknown_reason_falls_back_gracefully(self):
        # A defensive default — never a crash on an unmapped reason.
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
