"""Unit tests for ``terminal_parser.pane_looks_idle`` (the /update idle gate).

Fail-closed ground-truth: True ONLY for a fully-rendered Claude Code frame at an
input box with no run in flight and no live interactive surface; every other
shape (running, picker/gate, no-chrome, empty) is False so /update defers.
"""

from __future__ import annotations

from pathlib import Path

from cctelegram.terminal_parser import pane_looks_idle

FIX = Path(__file__).parent / "fixtures"

_SEP = "─" * 56

# Genuinely idle: post-completion summary line + EMPTY input box, no
# "esc to interrupt" in the bottom bar.
IDLE_PANE = f"""\
✻ Cooked for 2s

{_SEP}
❯
{_SEP}
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

# A half-typed (unsent) draft at the prompt — NOT restart-safe (a restart would
# discard it), so NOT idle (Fix 2).
IDLE_PANE_TYPED = f"""\
✻ Wove for 5s

{_SEP}
❯ a half-typed draft
{_SEP}
  ? for shortcuts
"""

# Mid-redraw / dropped footer: an empty input box but NO ready-status chrome
# below AND no "esc to interrupt" — absence-only, so it must fail CLOSED (Fix 1
# positive-evidence requirement).
MIDREDRAW_NO_STATUS = f"""\
✻ Working…

{_SEP}
❯
{_SEP}
"""

# A body Markdown blockquote (">") + a dropped footer (single separator). In the
# old absence-only logic the "> " line satisfied "input box present" → a false
# idle; structurally it is ABOVE any input-box separator pair, so it must be
# False (Fix 1 — the Codex P1).
BODY_BLOCKQUOTE_MIDREDRAW = f"""\
Here is my analysis:

> we should refactor the parser
> and add more tests

{_SEP}
"""

# A "> " blockquote sitting BETWEEN a separator pair (not the empty ❯ prompt) —
# still not the input box (must be empty), so False.
BLOCKQUOTE_BETWEEN_SEPARATORS = f"""\
> some quoted analysis text
{_SEP}
> another quoted line
{_SEP}
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""


def test_idle_pane_at_empty_input_box_is_idle():
    assert pane_looks_idle(IDLE_PANE) is True


def test_typed_draft_at_prompt_is_not_idle():
    assert pane_looks_idle(IDLE_PANE_TYPED) is False


def test_midredraw_without_ready_status_is_not_idle():
    assert pane_looks_idle(MIDREDRAW_NO_STATUS) is False


def test_body_blockquote_with_dropped_footer_is_not_idle():
    assert pane_looks_idle(BODY_BLOCKQUOTE_MIDREDRAW) is False


def test_blockquote_between_separators_is_not_idle():
    assert pane_looks_idle(BLOCKQUOTE_BETWEEN_SEPARATORS) is False


def test_none_is_not_idle():
    assert pane_looks_idle(None) is False


def test_empty_is_not_idle():
    assert pane_looks_idle("") is False


def test_no_chrome_frame_is_not_idle():
    # No ── chrome anchor → not a fully-rendered pane → conservative False.
    assert pane_looks_idle("just assistant prose\nwith no chrome anchor") is False


def test_running_pane_is_not_idle():
    # Real capture: "esc to interrupt" in the bottom bar → actively running.
    txt = (FIX / "status_busy_160x50_v2.1.198.txt").read_text(encoding="utf-8")
    assert pane_looks_idle(txt) is False


def test_interactive_picker_is_not_idle():
    # Real AUQ picker (Enter to select) → is_interactive_ui → not idle.
    txt = (FIX / "auq_4option_160x50_v2.1.198.txt").read_text(encoding="utf-8")
    assert pane_looks_idle(txt) is False


def test_approval_gate_without_input_box_is_not_idle():
    # A live approval gate REPLACES the input box with its option block: no bare
    # ❯ input box below the footer, so it is rejected even with the Permission
    # flag OFF (the option row "❯ 1." is a picker cursor, not the input box).
    gate = (
        "Do you want to proceed?\n"
        "\n"
        "❯ 1. Yes\n"
        "  2. No, tell Claude what to do differently\n"
        "\n"
        f"{_SEP}\n"
        "Esc to cancel\n"
    )
    assert pane_looks_idle(gate) is False
