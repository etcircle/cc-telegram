"""Stage B2.3 — the tappable Decision dispatch executor (``dcp:`` lane).

Covers the §3 pane-locked transaction (extractor parity → body-inclusive
fingerprint identity → geometry/family gates → FRESH version license →
nav→verify→Enter with motion proof → confirm) + the ledger discipline
(``accepted → dispatched``/``released`` on the confirmed-gone proof;
``not_advanced`` pre-commit bail falls through; ``commit_unconfirmed`` stays
unreleased, refresh-only), plus the round-4 named requirements: the fresh-version
decline, the extractor-parity decline on ``settings_warning_v2170.txt``, the bail
re-mint, and the lock-busy ``accepted → not_advanced`` downgrade.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cctelegram import terminal_parser as tp
from cctelegram.callback_dispatcher import interactive as cbi
from cctelegram.handlers import auq_ledger, decision_token as dt
from tests.conftest import Fake168Picker, _Screen

_FX = Path(__file__).parents[1] / "fixtures"
_TRUST = (_FX / "decision_trust_folder_v2.1.204.txt").read_text()
_SETTINGS = (_FX / "settings_warning_v2170.txt").read_text()
_WINDOW_ID = "@3"
_THREAD_ID = 7
_OWNER_ID = 1
_LICENSED = "2.1.204"


def _trust_form() -> Any:
    form = tp.parse_generic_decision(_TRUST)
    assert form is not None
    return form


def _fingerprint() -> str:
    return tp.decision_prompt_fingerprint(_trust_form())


def _ledger_key(opt: int) -> str:
    route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
    return auq_ledger.make_ledger_key(route_hash, _fingerprint()[:8], opt)


def _seed_accepted(opt: int, label: str) -> str:
    key = _ledger_key(opt)
    auq_ledger.record(
        key,
        state="accepted",
        user_id=_OWNER_ID,
        window_id=_WINDOW_ID,
        full_fingerprint=_fingerprint(),
        option_number=opt,
        option_label=label,
    )
    return key


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path) -> Any:
    auq_ledger.reset_for_tests(path=tmp_path / "ledger.jsonl", start_time=time.time())
    dt.reset_for_tests()
    dt.set_decision_dispatch_enabled(True)
    tp.set_decision_cards_enabled(True)
    yield
    auq_ledger.reset_for_tests()
    dt.reset_for_tests()
    tp.reset_for_tests()


class _DecisionPicker(Fake168Picker):
    """Keystroke-aware fake of the folder-trust Decision prompt.

    Reuses ``Fake168Picker`` (Down/Up move ❯; Enter selects a real option and
    RESOLVES the pane → non-Decision) and adds the per-window send lock + the
    FRESH ``pane_current_command`` read the §2b tap gate consults."""

    def __init__(self, *args: Any, command: str = _LICENSED, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lock = asyncio.Lock()
        self.command = command

    def window_send_lock(self, window_id: str) -> asyncio.Lock:
        return self.lock

    async def pane_current_command(self, window_id: str) -> str | None:
        return self.command


class _StuckCursorPicker(_DecisionPicker):
    """A quoted / dead pane: nav keys never move the cursor (wiggle fails)."""

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.sent.append((window_id, keys, enter, literal))
        return True  # cursor frozen — a quoted block can't move its ❯


class _StuckEnterPicker(_DecisionPicker):
    """Enter is a no-op: the prompt never resolves → ``commit_unconfirmed``."""

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.sent.append((window_id, keys, enter, literal))
        if window_id != self.window_id or self.resolved:
            return True
        scr = self.screens[self.idx]
        if keys == "Down":
            self.cursor = self.cursor + 1 if self.cursor < scr.n_nav else 1
        elif keys == "Up":
            self.cursor = self.cursor - 1 if self.cursor > 1 else scr.n_nav
        # Enter deliberately does NOT advance.
        return True


class _Query:
    def __init__(self) -> None:
        self.answers: list[tuple[str | None, bool | None]] = []
        self.message = SimpleNamespace(message_thread_id=_THREAD_ID)

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None
    ) -> None:
        self.answers.append((text, show_alert))


async def _run_dispatch(
    picker: _DecisionPicker,
    opt: int,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_Query, AsyncMock]:
    key = _seed_accepted(opt, label)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    finalize = AsyncMock()
    monkeypatch.setattr(cbi.interactive_ui, "finalize_decision_dispatch", finalize)
    monkeypatch.setattr(cbi, "handle_interactive_ui", AsyncMock(return_value=True))
    q = _Query()
    await cbi._dispatch_decision(
        query=q,
        context=SimpleNamespace(bot=SimpleNamespace()),
        user=SimpleNamespace(id=_OWNER_ID),
        tmux_manager=picker,
        adapters=SimpleNamespace(session_manager=SimpleNamespace()),
        w=SimpleNamespace(window_id=_WINDOW_ID),
        window_id=_WINDOW_ID,
        thread_id=_THREAD_ID,
        minted_fingerprint=_fingerprint(),
        option_number=opt,
        option_label=label,
        ledger_key=key,
    )
    return q, finalize


@pytest.mark.asyncio
async def test_happy_delta_nonzero_navigates_and_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picker = _DecisionPicker(_WINDOW_ID, [_Screen(_TRUST, 2, 2)])
    q, finalize = await _run_dispatch(picker, 2, "No, exit", monkeypatch)
    entry = auq_ledger.lookup(_ledger_key(2))
    # dispatched → released (§8): lookup treats a released latest row as None.
    assert entry is None
    # A Down nav + an Enter commit were sent.
    keys = [k for _w, k, _e, _l in picker.sent]
    assert "Down" in keys and "Enter" in keys
    assert q.answers == [("✅ No, exit", False)]
    finalize.assert_awaited_once()
    # §3 in-lock nav-generation invalidation.
    assert dt.current_nav_generation(_WINDOW_ID) is None


@pytest.mark.asyncio
async def test_happy_wiggle_delta_zero_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picker = _DecisionPicker(_WINDOW_ID, [_Screen(_TRUST, 2, 2)])
    q, finalize = await _run_dispatch(
        picker, 1, "Yes, I trust this folder", monkeypatch
    )
    # No pre-commit persisted row (dispatched → released → None); the wiggle
    # sent Down then Up (motion proof) then Enter.
    assert auq_ledger.lookup(_ledger_key(1)) is None
    keys = [k for _w, k, _e, _l in picker.sent]
    assert keys.count("Down") >= 1 and keys.count("Up") >= 1 and "Enter" in keys
    finalize.assert_awaited_once()


@pytest.mark.asyncio
async def test_static_quoted_pane_wiggle_fails_pre_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picker = _StuckCursorPicker(_WINDOW_ID, [_Screen(_TRUST, 2, 2)])
    q, finalize = await _run_dispatch(
        picker, 1, "Yes, I trust this folder", monkeypatch
    )
    entry = auq_ledger.lookup(_ledger_key(1))
    assert entry is not None and entry.state == "not_advanced"
    assert "Enter" not in [k for _w, k, _e, _l in picker.sent]
    assert q.answers == [("Action not registered; refreshing card.", False)]
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_no_resolve_records_commit_unconfirmed_unreleased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The round-3 zero-absence variant: Enter is sent but the committed prompt
    # (same fingerprint) is still on the pane at confirm → ``commit_unconfirmed``,
    # UNRELEASED (release fires ONLY on the confirmed-gone proof), no teardown.
    picker = _StuckEnterPicker(_WINDOW_ID, [_Screen(_TRUST, 2, 2)])
    q, finalize = await _run_dispatch(picker, 2, "No, exit", monkeypatch)
    entry = auq_ledger.lookup(_ledger_key(2))
    assert entry is not None and entry.state == "commit_unconfirmed"
    assert "Enter" in [k for _w, k, _e, _l in picker.sent]
    assert q.answers == [("Action sent; refreshing card.", False)]
    finalize.assert_not_awaited()  # never terminal-teardown on an unconfirmed


def test_parse_nav_payload_suffixed_and_legacy() -> None:
    """Round-4 guardrail 1: ``(window_id, gen)`` parses BEFORE the window is used.

    A gate card's ``@N:g<gen>`` splits cleanly; a legacy AUQ ``@N`` yields gen
    None; a malformed suffix falls back to the legacy path (never a dead-tap on
    a window mis-read as ``@12:g7``)."""
    assert cbi._parse_nav_payload("@12:g7") == ("@12", 7)
    assert cbi._parse_nav_payload("@0:g0") == ("@0", 0)
    assert cbi._parse_nav_payload("@12") == ("@12", None)
    assert cbi._parse_nav_payload("@12:gx") == ("@12:gx", None)
    assert cbi._parse_nav_payload("@12:g") == ("@12:g", None)


@pytest.mark.asyncio
async def test_fresh_version_gate_declines_before_any_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cached-licensed but the FRESH live command is an unlicensed CC version.
    picker = _DecisionPicker(_WINDOW_ID, [_Screen(_TRUST, 2, 2)], command="2.1.205")
    q, finalize = await _run_dispatch(
        picker, 1, "Yes, I trust this folder", monkeypatch
    )
    entry = auq_ledger.lookup(_ledger_key(1))
    assert entry is not None and entry.state == "not_advanced"
    assert entry.failed_reason == "version_unlicensed"
    assert picker.sent == []  # NO keystroke to an unlicensed pane
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_version_gate_declines_on_shell_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picker = _DecisionPicker(_WINDOW_ID, [_Screen(_TRUST, 2, 2)], command="-zsh")
    _q, finalize = await _run_dispatch(
        picker, 1, "Yes, I trust this folder", monkeypatch
    )
    entry = auq_ledger.lookup(_ledger_key(1))
    assert entry is not None and entry.state == "not_advanced"
    assert picker.sent == []
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_extractor_parity_declines_on_settings_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Settings pane that DECISION-parses but whose extractor name is "Settings"
    # (first-match-wins) → decline before any keystroke.
    picker = _DecisionPicker(_WINDOW_ID, [_Screen(_SETTINGS, 2, 2)])
    _q, finalize = await _run_dispatch(
        picker, 1, "Yes, I trust this folder", monkeypatch
    )
    entry = auq_ledger.lookup(_ledger_key(1))
    assert entry is not None and entry.state == "not_advanced"
    assert entry.failed_reason == "extractor_parity"
    assert picker.sent == []
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_fingerprint_drift_bails_pre_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picker = _DecisionPicker(_WINDOW_ID, [_Screen(_TRUST, 2, 2)])
    # Seed accepted for a DIFFERENT (mismatched) minted fingerprint.
    key = _ledger_key(1)
    auq_ledger.record(
        key,
        state="accepted",
        user_id=_OWNER_ID,
        window_id=_WINDOW_ID,
        full_fingerprint="deadbeefdeadbeef",
        option_number=1,
        option_label="Yes, I trust this folder",
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    monkeypatch.setattr(cbi.interactive_ui, "finalize_decision_dispatch", AsyncMock())
    monkeypatch.setattr(cbi, "handle_interactive_ui", AsyncMock(return_value=True))
    q = _Query()
    await cbi._dispatch_decision(
        query=q,
        context=SimpleNamespace(bot=SimpleNamespace()),
        user=SimpleNamespace(id=_OWNER_ID),
        tmux_manager=picker,
        adapters=SimpleNamespace(session_manager=SimpleNamespace()),
        w=SimpleNamespace(window_id=_WINDOW_ID),
        window_id=_WINDOW_ID,
        thread_id=_THREAD_ID,
        minted_fingerprint="deadbeefdeadbeef",
        option_number=1,
        option_label="Yes, I trust this folder",
        ledger_key=key,
    )
    entry = auq_ledger.lookup(key)
    assert entry is not None and entry.state == "not_advanced"
    assert entry.failed_reason == "fingerprint_mismatch"
    assert picker.sent == []


@pytest.mark.asyncio
async def test_decision_lock_busy_downgrades_accepted_to_not_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-4 named: a busy send lock at dispatch time downgrades the
    already-written ``accepted`` to ``not_advanced`` (Enter provably never sent)
    and the callback falls through to a fresh re-render — never stranding a
    crash-ambiguous ``accepted``."""
    picker = _DecisionPicker(_WINDOW_ID, [_Screen(_TRUST, 2, 2)])
    key = _seed_accepted(1, "Yes, I trust this folder")
    rerender = AsyncMock(return_value=True)
    monkeypatch.setattr(cbi, "handle_interactive_ui", rerender)
    monkeypatch.setattr(cbi.interactive_ui, "finalize_decision_dispatch", AsyncMock())
    q = _Query()
    # Hold the window lock so `_lock_busy` is True at dispatch time.
    await picker.lock.acquire()
    try:
        await cbi._dispatch_decision(
            query=q,
            context=SimpleNamespace(bot=SimpleNamespace()),
            user=SimpleNamespace(id=_OWNER_ID),
            tmux_manager=picker,
            adapters=SimpleNamespace(session_manager=SimpleNamespace()),
            w=SimpleNamespace(window_id=_WINDOW_ID),
            window_id=_WINDOW_ID,
            thread_id=_THREAD_ID,
            minted_fingerprint=_fingerprint(),
            option_number=1,
            option_label="Yes, I trust this folder",
            ledger_key=key,
        )
    finally:
        picker.lock.release()
    entry = auq_ledger.lookup(key)
    assert entry is not None and entry.state == "not_advanced"
    assert entry.failed_reason == "lock_busy"
    assert picker.sent == []  # Enter provably never sent
    rerender.assert_awaited()  # fell through to a fresh re-render
