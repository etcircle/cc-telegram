"""Scenario: the GH #52 FOOTERLESS Decision card — UNMOCKED render + one card.

The r2 review (P2-4) required the "ONE card" claim to be proven on the REAL
render path, not through a mocked ``handle_interactive_ui``: drive the actual
poller (``status_polling.update_status_message``) with the renderer UNMOCKED
against the promoted rig fixture, then COUNT the actual posts:

  - exactly ONE Decision card message is sent across repeated poll ticks (the
    same-hash dedup holds — no per-tick duplicate cards);
  - NO "🪦 …resolved" tombstone / unknown-blocking excerpt card coexists with
    it (``parse_unknown_blocking_prompt`` and the named Decision path are
    mutually exclusive via ``extract_interactive_content``);
  - the route promotes RUNNING → WAITING_ON_USER, typing off;
  - flag OFF → NO card at all + NO promotion.

Modeled on ``tests/scenarios/test_interactive_approval_gates.py`` (the real
tmux-singleton patching means the poller's internal renderer runs for real).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cctelegram import route_runtime, terminal_parser
from cctelegram.handlers import interactive_ui, status_polling
from cctelegram.route_runtime import RunState
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_SESSION_ID = "52525252-5252-4252-8252-525252525252"
_THREAD_ID = 91
_FIXTURES = Path(__file__).parent.parent / "cctelegram" / "fixtures"
_POS_CLEAN = "decision_footerless_switchmodel_v2.1.207.txt"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


@pytest.fixture
def decision_on():
    terminal_parser.set_decision_cards_enabled(True)
    yield
    terminal_parser.set_decision_cards_enabled(False)


def _bind(scenario: ScenarioHarness, pane: str) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        _THREAD_ID, wid, display_name="repo", cwd="/repo", session_id=_SESSION_ID
    )
    return wid


async def _poll(scenario: ScenarioHarness, wid: str, n: int = 1) -> None:
    for _ in range(n):
        await status_polling.update_status_message(
            scenario.bot,
            user_id=scenario.user_id,
            window_id=wid,
            thread_id=_THREAD_ID,
        )


def _decision_card_sends(scenario: ScenarioHarness) -> list[str]:
    """The texts of NEW messages that are the footerless Decision card."""
    out: list[str] = []
    for s in scenario.bot.sent:
        if s.method != "send_message":
            continue
        text = s.kwargs.get("text") or ""
        if "Switch model?" in text and "Yes, switch to Sonnet 5" in text:
            out.append(text)
    return out


@pytest.mark.asyncio
async def test_footerless_pane_posts_exactly_one_card_no_tombstone(
    scenario: ScenarioHarness, decision_on
) -> None:
    wid = _bind(scenario, _load(_POS_CLEAN))
    route = (scenario.user_id, _THREAD_ID, wid)
    await route_runtime.mark_inbound_sent(route)  # RUNNING
    assert route_runtime.snapshot(route).run_state is RunState.RUNNING

    # Repeated REAL poll ticks — the renderer is UNMOCKED; the same-hash dedup
    # must keep the card count at exactly ONE.
    await _poll(scenario, wid, 4)

    cards = _decision_card_sends(scenario)
    assert len(cards) == 1, f"expected exactly ONE Decision card, got {len(cards)}"

    # NO tombstone / unknown-blocking excerpt card anywhere in the topic — the
    # named Decision path and the unknown-tombstone path are mutually exclusive.
    for s in scenario.bot.sent:
        text = s.kwargs.get("text") or ""
        assert "🪦" not in text, (
            f"tombstone card posted beside the Decision card: {text!r}"
        )

    # Promotion: WAITING_ON_USER + typing off (the UI-name-agnostic poller path).
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.typing_eligible is False

    # The card is a live interactive surface (the one-card authority the
    # tombstone lane consults before posting).
    assert interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)


@pytest.mark.asyncio
async def test_flag_off_footerless_pane_posts_nothing(
    scenario: ScenarioHarness,
) -> None:
    # Root conftest pins the flag OFF; assert it explicitly for clarity.
    terminal_parser.set_decision_cards_enabled(False)
    wid = _bind(scenario, _load(_POS_CLEAN))
    route = (scenario.user_id, _THREAD_ID, wid)
    await route_runtime.mark_inbound_sent(route)

    await _poll(scenario, wid, 3)

    assert _decision_card_sends(scenario) == []
    assert route_runtime.snapshot(route).run_state is RunState.RUNNING
    assert not interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)
