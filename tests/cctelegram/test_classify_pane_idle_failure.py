"""Unit tests for ``terminal_parser.classify_pane_idle_failure``.

The classifier REPLAYS ``pane_looks_idle``'s five leg checks purely to NAME the
first failing leg for logging + reason-specific fallback copy. It is never
authoritative — ``pane_looks_idle`` decides. The load-bearing invariant is
AGREEMENT: ``classify_pane_idle_failure`` returns ``None`` iff
``pane_looks_idle`` returns ``True``, across every pane fixture the repo carries.
"""

from __future__ import annotations

from pathlib import Path

from cctelegram.terminal_parser import (
    classify_pane_idle_failure,
    pane_looks_idle,
)

# Reuse the exact fixtures the pane_looks_idle unit test pins.
from tests.cctelegram.test_pane_looks_idle import (  # noqa: E402
    BLOCKQUOTE_BETWEEN_SEPARATORS,
    BODY_BLOCKQUOTE_MIDREDRAW,
    IDLE_PANE,
    IDLE_PANE_AGENTS_BAR_NO_SHELLS,
    IDLE_PANE_BG_SHELLS,
    IDLE_PANE_BG_SHELL_SINGULAR,
    IDLE_PANE_TYPED,
    MIDREDRAW_NO_STATUS,
)

FIX = Path(__file__).parent / "fixtures"

_SEP = "─" * 56

BUSY_PANE = f"""\
✽ Brewing… (3s · thinking with high effort)

{_SEP}
❯
{_SEP}
  ⏵⏵ bypass permissions on · esc to interrupt
"""


class TestLegNaming:
    def test_idle_pane_returns_none(self):
        assert classify_pane_idle_failure(IDLE_PANE) is None

    def test_none_input_names_capture_empty(self):
        assert classify_pane_idle_failure(None) == "capture_empty"

    def test_empty_input_names_capture_empty(self):
        assert classify_pane_idle_failure("") == "capture_empty"

    def test_active_generation_names_active_status(self):
        assert classify_pane_idle_failure(BUSY_PANE) == "active_status"

    def test_running_fixture_names_active_status(self):
        txt = (FIX / "status_busy_160x50_v2.1.198.txt").read_text(encoding="utf-8")
        assert classify_pane_idle_failure(txt) == "active_status"

    def test_live_picker_names_interactive(self):
        txt = (FIX / "auq_4option_160x50_v2.1.198.txt").read_text(encoding="utf-8")
        assert classify_pane_idle_failure(txt) == "interactive"

    def test_typed_draft_names_input_not_empty(self):
        assert classify_pane_idle_failure(IDLE_PANE_TYPED) == "input_not_empty"

    def test_midredraw_dropped_footer_names_no_ready_chrome(self):
        # Empty box but no ready-status marker below → the mid-redraw shape.
        assert classify_pane_idle_failure(MIDREDRAW_NO_STATUS) == "no_ready_chrome"

    def test_no_chrome_frame_names_no_input_box(self):
        assert (
            classify_pane_idle_failure("just assistant prose\nwith no chrome anchor")
            == "no_input_box"
        )

    def test_body_blockquote_dropped_footer_names_no_input_box(self):
        # Single separator (< 2) → no input-box bracket.
        assert classify_pane_idle_failure(BODY_BLOCKQUOTE_MIDREDRAW) == "no_input_box"

    def test_blockquote_between_separators_names_input_not_empty(self):
        # A "> quoted" line between the bottom separator pair is a non-empty,
        # non-cursor input row (leg 3).
        assert (
            classify_pane_idle_failure(BLOCKQUOTE_BETWEEN_SEPARATORS)
            == "input_not_empty"
        )

    def test_background_shells_names_background_shells(self):
        assert classify_pane_idle_failure(IDLE_PANE_BG_SHELLS) == "background_shells"

    def test_single_background_shell_names_background_shells(self):
        assert (
            classify_pane_idle_failure(IDLE_PANE_BG_SHELL_SINGULAR)
            == "background_shells"
        )

    def test_agents_bar_no_shells_is_idle(self):
        assert classify_pane_idle_failure(IDLE_PANE_AGENTS_BAR_NO_SHELLS) is None


class TestAgreementWithAuthority:
    """The classifier's None/non-None must NEVER disagree with pane_looks_idle."""

    def _all_pane_fixtures(self) -> list[str]:
        texts: list[str] = []
        # In-repo pane fixtures: capture-shaped .txt files under fixtures/.
        for path in sorted(FIX.glob("*.txt")):
            try:
                texts.append(path.read_text(encoding="utf-8"))
            except Exception:
                continue
        # Plus the module-level synthetic panes both suites share.
        texts.extend(
            [
                IDLE_PANE,
                IDLE_PANE_TYPED,
                MIDREDRAW_NO_STATUS,
                BODY_BLOCKQUOTE_MIDREDRAW,
                BLOCKQUOTE_BETWEEN_SEPARATORS,
                IDLE_PANE_BG_SHELLS,
                IDLE_PANE_BG_SHELL_SINGULAR,
                IDLE_PANE_AGENTS_BAR_NO_SHELLS,
                BUSY_PANE,
                "",
                "just assistant prose\nwith no chrome anchor",
            ]
        )
        return texts

    def test_none_iff_pane_looks_idle_true(self):
        checked = 0
        for text in self._all_pane_fixtures():
            authority = pane_looks_idle(text)
            reason = classify_pane_idle_failure(text)
            assert (reason is None) is authority, (
                f"disagreement: pane_looks_idle={authority} but reason={reason!r} "
                f"for pane starting {text[:60]!r}"
            )
            checked += 1
        # Guard against a vacuous pass (no fixtures iterated).
        assert checked >= 12

    def test_none_input_agreement(self):
        # None is not a str fixture but the authority accepts it.
        assert pane_looks_idle(None) is False
        assert classify_pane_idle_failure(None) is not None
