"""Stage B2.3 §5b(c)/O-6 — the generation-suffixed nav gate in
``assert_nav_dispatchable`` (round-4 guardrail 2).

The presence of a window nav generation in ``decision_token`` is the "this window
owns a live GATE card" bit. Rules, all fail-closed BEFORE any key:
  * gen PRESENT must equal the window's current generation (an OLD card's ⏎ after
    a NEW card rendered, or a restart-wiped registry, refuses);
  * gen ABSENT but a live gate generation present → refuse (a pre-B2 un-suffixed
    gate card must never raw-dispatch);
  * gen ABSENT + no gate generation → the legacy AUQ / EPM path (byte-neutral).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import terminal_parser as tp
from cctelegram.handlers import decision_token as dt
from cctelegram.handlers import interactive_ui

_TRUST = (
    Path(__file__).parents[1] / "fixtures" / "decision_trust_folder_v2.1.204.txt"
).read_text()
_USER = 1
_THREAD = 7
_WID = "@3"


@pytest.fixture(autouse=True)
def _reset() -> Any:
    interactive_ui.reset_for_tests()
    dt.reset_for_tests()
    tp.set_decision_cards_enabled(True)
    yield
    interactive_ui.reset_for_tests()
    dt.reset_for_tests()
    tp.reset_for_tests()


class _Tmux:
    def __init__(self, pane: str) -> None:
        self._pane = pane

    async def find_window_by_id(self, window_id: str) -> Any:
        return SimpleNamespace(window_id=window_id, window_name="repo")

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        return self._pane


class _Query:
    def __init__(self) -> None:
        self.answers: list[tuple[str | None, bool | None]] = []

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None
    ) -> None:
        self.answers.append((text, show_alert))


def _seed_surface() -> None:
    # A live interactive surface owned by (user, thread) → window.
    interactive_ui._interactive_msgs[(_USER, _THREAD)] = 999
    interactive_ui._interactive_mode[(_USER, _THREAD)] = _WID


async def _call(gen: int | None) -> tuple[Any, _Query]:
    q = _Query()
    w = await interactive_ui.assert_nav_dispatchable(
        q, _USER, _THREAD, _WID, tmux_mgr=_Tmux(_TRUST), gen=gen
    )
    return w, q


@pytest.mark.asyncio
async def test_matching_generation_dispatches() -> None:
    g = dt.rotate_nav_generation(_WID)
    _seed_surface()
    w, q = await _call(g)
    assert w is not None and w.window_id == _WID
    assert q.answers == []


@pytest.mark.asyncio
async def test_mismatched_generation_refuses() -> None:
    g = dt.rotate_nav_generation(_WID)
    _seed_surface()
    w, q = await _call(g + 1)
    assert w is None
    assert q.answers == [("Card refreshed — use the current card", False)]


@pytest.mark.asyncio
async def test_missing_generation_on_gate_surface_refuses() -> None:
    # A gate generation is live but the callback carried NO suffix (a pre-B2
    # un-suffixed gate card surviving a rollout) → refuse, never raw-dispatch.
    dt.rotate_nav_generation(_WID)
    _seed_surface()
    w, q = await _call(None)
    assert w is None
    assert q.answers == [("Card refreshed — use the current card", False)]


@pytest.mark.asyncio
async def test_restart_wiped_registry_suffixed_tap_refuses() -> None:
    # No current generation (a bot restart wiped the registry) but the published
    # card's ⏎ still carries a stale suffix → fail closed.
    _seed_surface()
    assert dt.current_nav_generation(_WID) is None
    w, q = await _call(5)
    assert w is None
    assert q.answers == [("Card refreshed — use the current card", False)]


@pytest.mark.asyncio
async def test_legacy_unsuffixed_auq_nav_byte_neutral() -> None:
    # No gate generation for this window (AUQ / EPM surface) + an un-suffixed
    # legacy callback → the AUQ nav contract is untouched (dispatches).
    _seed_surface()
    assert dt.current_nav_generation(_WID) is None
    w, q = await _call(None)
    assert w is not None and w.window_id == _WID
    assert q.answers == []


@pytest.mark.asyncio
async def test_post_dispatch_invalidation_makes_stale_suffix_refuse() -> None:
    # The §3 in-lock invalidation: after a confirmed dispatch the nav generation
    # is dropped, so the OLD card's suffixed ⏎ can never act on the pane.
    g = dt.rotate_nav_generation(_WID)
    _seed_surface()
    dt.invalidate_on_dispatch(_WID)  # what the dispatch does in-lock at `dispatched`
    w, q = await _call(g)
    assert w is None
    assert q.answers == [("Card refreshed — use the current card", False)]
