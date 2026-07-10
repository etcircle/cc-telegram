"""Tests for parse_usage_output against the Claude Code 2.1.206 /cost (== /usage) overlay.

The /cost and /usage commands open the SAME full-screen modal. The parser
anchors on the stable ``Settings  Status  Config  Usage  Stats`` tab bar and the
``Esc to cancel`` footer, extracts the body between them, and returns readable
lines. These pins use the real captured overlay fixtures (both the /cost variants
and the /usage twin), version-named per the repo's fixture-pin discipline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cctelegram.terminal_parser import parse_usage_output

_FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (_FIXTURES / name).read_text()


# Every real 2.1.206 overlay capture — the settled top, the day/week toggles,
# and the /usage twin (which additionally has a welcome card in scrollback above
# the modal that the tab-bar anchor must skip past).
_OVERLAY_FIXTURES = (
    "cost_overlay_live_v2.1.206.txt",
    "cost_overlay_d_v2.1.206.txt",
    "cost_overlay_w_v2.1.206.txt",
    "usage_overlay_live_v2.1.206.txt",
)


@pytest.mark.parametrize("fixture", _OVERLAY_FIXTURES)
def test_overlay_parses_to_readable_body(fixture: str):
    """Each real overlay capture parses to a non-empty readable body."""
    info = parse_usage_output(_read(fixture))
    assert info is not None, f"{fixture} should parse as a usage overlay"
    assert info.parsed_lines, f"{fixture} produced no body lines"
    body = "\n".join(info.parsed_lines)
    # The limit bars are present in every scroll position of the modal
    # (the _d / _w toggles are captured scrolled, so "Total cost:" may have
    # scrolled off — the limit-bar section is the always-visible core).
    assert "% used" in body
    assert "Current week (Fable)" in body
    # The tab-bar header itself is skipped, and the box-drawing rule dropped.
    assert "Settings  Status" not in body
    assert not any(set(line) <= set("▔▁─") for line in info.parsed_lines)


def test_cost_live_extracts_the_key_numbers():
    """The settled /cost top carries cost + the three limit bars."""
    info = parse_usage_output(_read("cost_overlay_live_v2.1.206.txt"))
    assert info is not None
    body = "\n".join(info.parsed_lines)
    assert "$0.2819" in body
    assert "Current session" in body
    assert "Current week (all models)" in body
    assert "Current week (Fable)" in body
    # A progress-bar line collapses to just its percentage text.
    assert "56% used" in body
    # No leading block-bar glyphs survive on the percentage lines.
    assert "█56% used" not in body


def test_day_and_week_breakdown_labels_survive():
    """The d/w toggle changes the breakdown label; the parser keeps it."""
    day = "\n".join(
        parse_usage_output(_read("cost_overlay_d_v2.1.206.txt")).parsed_lines
    )
    week = "\n".join(
        parse_usage_output(_read("cost_overlay_w_v2.1.206.txt")).parsed_lines
    )
    assert "Last 24h" in day
    assert "Last 7d" in week


def test_usage_twin_skips_scrollback_above_the_modal():
    """The /usage capture has a welcome card above the overlay; it must be dropped."""
    info = parse_usage_output(_read("usage_overlay_live_v2.1.206.txt"))
    assert info is not None
    body = "\n".join(info.parsed_lines)
    # Welcome-card / banner text from the scrollback ABOVE the tab bar is excluded.
    assert "Welcome back Emiliyan" not in body
    assert "Tips for getting started" not in body
    # The modal body IS present.
    assert "Total cost:" in body


def test_non_overlay_pane_returns_none():
    """A normal pane (no tab bar) is not a usage overlay."""
    assert parse_usage_output("just some regular\ntmux pane text\n❯ ") is None


def test_empty_pane_returns_none():
    assert parse_usage_output("") is None
