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

from cctelegram.terminal_parser import parse_usage_output, usage_overlay_present

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


# ── Adversarial anchor probes (round-1 converged P3) ───────────────────────
# The old anchor was unordered substring membership: the five tab words in ANY
# order / concatenated / embedded in prose matched, so with a delayed overlay
# arbitrary pane prose could be reported as structured usage. The anchor now
# requires the five ORDERED whole tokens (whitespace-separated, no other word
# chars on the line) + structural overlay evidence (the modal's top rule /
# the Esc footer / the Session sub-header).


@pytest.mark.parametrize(
    "pane",
    [
        # The five words embedded in ordinary prose (in order, other words
        # interleaved) — plus secret-looking output below.
        (
            "I checked the Settings page, the Status view, the Config file, "
            "the Usage tab and the Stats panel\nsecret output\n❯ "
        ),
        # Reversed order (the literal Hermes probe).
        "Stats Usage Config Status Settings\nsecret output\n❯ ",
        # Concatenated (no whitespace between tokens — the Codex probe).
        "SettingsStatusConfigUsageStats\nsecret output\n❯ ",
        # The EXACT tab-bar line but with NO structural overlay evidence
        # around it (no rule above, no footer, no Session sub-header) —
        # e.g. assistant prose QUOTING the tab bar.
        (
            "some earlier prose\n"
            "   Settings  Status   Config   Usage   Stats\n"
            "more prose\n❯ "
        ),
    ],
)
def test_adversarial_probes_return_none(pane: str):
    assert parse_usage_output(pane) is None
    assert usage_overlay_present(pane) is False


@pytest.mark.parametrize("fixture", _OVERLAY_FIXTURES)
def test_usage_overlay_present_true_on_real_captures(fixture: str):
    assert usage_overlay_present(_read(fixture)) is True


def test_usage_overlay_present_false_on_non_overlay_panes():
    assert usage_overlay_present("✻ Cooked for 2s\n❯\n  ? for shortcuts") is False
    assert usage_overlay_present(None) is False
    assert usage_overlay_present("") is False
