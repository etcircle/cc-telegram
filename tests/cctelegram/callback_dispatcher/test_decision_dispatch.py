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


class _EnterIntoNamedUiPicker(_DecisionPicker):
    """Enter lands the pane on a NAMED interactive UI (the Settings warning)
    that ALSO decision-parses — the review-r1 P2-B confirm-parity case: the
    bare ``parse_generic_decision`` would fp-compare it as a "different
    Decision" and wrongly confirm; the FULL extractor names it Settings."""

    def __init__(self, *args: Any, landing_pane: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._landing_pane = landing_pane
        self._landed = False

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.sent.append((window_id, keys, enter, literal))
        if window_id != self.window_id or self._landed:
            return True
        scr = self.screens[self.idx]
        if keys == "Down":
            self.cursor = self.cursor + 1 if self.cursor < scr.n_nav else 1
        elif keys == "Up":
            self.cursor = self.cursor - 1 if self.cursor > 1 else scr.n_nav
        elif keys == "Enter":
            self._landed = True
        return True

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        if self._landed and window_id == self.window_id:
            return self._landing_pane
        return await super().capture_pane(
            window_id, with_ansi=with_ansi, scrollback_lines=scrollback_lines
        )


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


def test_classify_advance_empty_capture_fails_closed() -> None:
    """r2 Hermes P2: an EMPTY/blank post-Enter capture is NOT positive absence
    proof of the committed prompt ("we didn't see the thing") — it must classify
    False (⇒ commit_unconfirmed, unreleased), never a false ``dispatched`` that
    would finalize the card and release the single-use ledger key."""
    assert cbi._classify_decision_advance("", "decision:abc") is False
    assert cbi._classify_decision_advance("   \n\n  ", "decision:abc") is False


# ── GH #52 confirm-side: only a proven-FOOTERED form / no-residue confirms ──

_MODAL_RULE = "▔" * 80
_FOOTERLESS_PANE = (
    f"{_MODAL_RULE}\n   Switch model?\n   body line\n\n"
    "   ❯ 1. Yes, switch\n     2. No, go back\n"
)
_FOOTERED_TWIN = _FOOTERLESS_PANE + " Enter to confirm · Esc to cancel\n"


def test_footered_mint_then_footerless_reparse_is_commit_unconfirmed() -> None:
    """r5 P1(a): the SAME footered prompt captured mid-redraw WITHOUT its footer
    re-parses FOOTERLESS → a variant-distinct form that MUST NOT confirm. Only a
    proven-FOOTERED live form may establish a "different form" resolution."""
    footered = tp.parse_generic_decision(_FOOTERED_TWIN)
    assert footered is not None
    assert tp.decision_variant_of(footered) == tp.DECISION_VARIANT_FOOTERED
    footered_fp = tp.decision_prompt_fingerprint(footered)
    # The post-Enter pane re-parses FOOTERLESS (footer dropped mid-redraw).
    footerless = tp.parse_generic_decision(_FOOTERLESS_PANE)
    assert footerless is not None
    assert tp.decision_variant_of(footerless) == tp.DECISION_VARIANT_FOOTERLESS
    assert cbi._classify_decision_advance(_FOOTERLESS_PANE, footered_fp) is False


def test_footered_mint_then_folder_trust_minus_footer_is_commit_unconfirmed() -> None:
    """r5 P1(b) — a REAL fixture, not a synthetic ▔ form: a footer-dropped frame of a
    ``─``-ruled folder-trust prompt fails the strict footerless parser entirely
    (extractor None), but its still-standing terminal option block IS DECISION
    RESIDUE → commit_unconfirmed, never a false ``dispatched``."""
    ft = (_FX / "folder_trust_arrival_plain_v2.1.207.txt").read_text()
    ft_nofooter = "\n".join(
        line for line in ft.split("\n") if "Enter to confirm" not in line
    )
    assert tp.extract_interactive_content(ft_nofooter) is None
    assert tp.has_decision_residue(ft_nofooter) is True
    assert cbi._classify_decision_advance(ft_nofooter, "decision:whatever") is False


def test_different_proven_footered_form_confirms_dispatched() -> None:
    """A DIFFERENT proven-FOOTERED Decision form on the confirm pane (the committed
    prompt resolved and a new footered one raised within the settle) → dispatched."""
    trust = tp.parse_generic_decision(_TRUST)
    assert trust is not None
    committed_fp = tp.decision_prompt_fingerprint(trust)
    # A DIFFERENT footered form (different options) is now on the pane.
    assert cbi._classify_decision_advance(_FOOTERED_TWIN, committed_fp) is True


def test_no_residue_pane_confirms_dispatched() -> None:
    """A confirm pane with NO Decision residue (input box restored, no footer, no
    terminal option block) → the committed prompt is positively gone → dispatched."""
    restored = (
        _FX / "decision_footerless_neg_inputbox_restored_v2.1.207.txt"
    ).read_text()
    assert tp.extract_interactive_content(restored) is None
    assert tp.has_decision_residue(restored) is False
    assert cbi._classify_decision_advance(restored, "decision:whatever") is True


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
async def test_dispatched_finalizes_before_callback_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r1 P2-C named pin — §5b(b) ordering per the plan: on a CONFIRMED
    dispatch the terminal teardown (pop the persisted surface → the inert
    "✅ … sent" edit) runs FIRST, THEN the callback answer — answering first left
    a crash/network window where the callback was acked but the persisted
    surface was not yet terminal."""
    picker = _DecisionPicker(_WINDOW_ID, [_Screen(_TRUST, 2, 2)])
    key = _seed_accepted(2, "No, exit")
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    order: list[str] = []

    async def _finalize(*args: Any, **kwargs: Any) -> None:
        order.append("finalize")

    monkeypatch.setattr(cbi.interactive_ui, "finalize_decision_dispatch", _finalize)
    monkeypatch.setattr(cbi, "handle_interactive_ui", AsyncMock(return_value=True))

    class _OrderQuery(_Query):
        async def answer(
            self, text: str | None = None, show_alert: bool | None = None
        ) -> None:
            order.append("answer")
            await super().answer(text, show_alert)

    q = _OrderQuery()
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
        option_number=2,
        option_label="No, exit",
        ledger_key=key,
    )
    assert order == ["finalize", "answer"], order


@pytest.mark.asyncio
async def test_commit_into_named_ui_pane_records_commit_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r1 P2-B named pin — confirm-side extractor parity: the post-Enter
    pane parses as a NAMED interactive UI (the Settings warning, which ALSO
    decision-parses with a different fingerprint) while decision footer markers
    are still present ⇒ ``commit_unconfirmed``, NEVER ``dispatched`` (the bare
    ``parse_generic_decision`` recognizer would have fp-compared it as a
    "different Decision" and wrongly confirmed + released)."""
    # Premise guard: the fixture must decision-parse under the WEAK recognizer
    # yet be named Settings by the FULL extractor (first-match-wins).
    weak = tp.parse_generic_decision(_SETTINGS)
    assert weak is not None
    assert tp.decision_prompt_fingerprint(weak) != _fingerprint()
    named = tp.extract_interactive_content(_SETTINGS)
    assert named is not None and named.name == "Settings"

    picker = _EnterIntoNamedUiPicker(
        _WINDOW_ID, [_Screen(_TRUST, 2, 2)], landing_pane=_SETTINGS
    )
    q, finalize = await _run_dispatch(picker, 2, "No, exit", monkeypatch)
    entry = auq_ledger.lookup(_ledger_key(2))
    assert entry is not None and entry.state == "commit_unconfirmed"
    assert "Enter" in [k for _w, k, _e, _l in picker.sent]
    assert q.answers == [("Action sent; refreshing card.", False)]
    finalize.assert_not_awaited()  # unreleased + no terminal teardown


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


# ── GH #52 r2 P2-3: the POSITIVE-authorization matrix — variant ∈ {missing,
# unknown, footerless, footered} across ALL FOUR seams (mint / identify_family /
# dispatch pre-commit / confirm-side). The three non-footered variants carry
# folder-trust's EXACT licensed title/options, so nothing but the variant gate
# stands between them and a mint/license/commit. ──────────────────────────────

_FT_LABELS = ("Yes, I trust this folder", "No, exit")
_FT_TITLE = "Accessing workspace:"

_VARIANT_META: dict[str, dict[str, str]] = {
    "missing": {},
    "unknown": {"decision_variant": "bogus"},
    "footerless": {"decision_variant": tp.DECISION_VARIANT_FOOTERLESS},
    "footered": {"decision_variant": tp.DECISION_VARIANT_FOOTERED},
}
_NON_FOOTERED = ["missing", "unknown", "footerless"]


def _ft_form(variant: str) -> tp.AskUserQuestionForm:
    """A clean single-select folder-trust-signature form carrying ``variant``."""
    options = tuple(
        tp.AskOption(label=label, recommended=False, cursor=(i == 0), number=i + 1)
        for i, label in enumerate(_FT_LABELS)
    )
    excerpt_lines = [_FT_TITLE, "body line"] + [
        f"{'❯ ' if o.cursor else '  '}{o.number}. {o.label}" for o in options
    ]
    return tp.AskUserQuestionForm(
        current_question_title=_FT_TITLE,
        options=options,
        is_review_screen=False,
        is_free_text=False,
        pane_excerpt="\n".join(excerpt_lines),
        select_mode="single",
        options_complete=True,
        _meta=dict(_VARIANT_META[variant]),
    )


# Seam 1 — decision_token.identify_family (the leaf belt-and-braces gate).


@pytest.mark.parametrize("variant", _NON_FOOTERED)
def test_identify_family_refuses_non_footered_variant(variant: str) -> None:
    assert dt.identify_family(_ft_form(variant)) is None


def test_identify_family_accepts_footered_variant() -> None:
    assert dt.identify_family(_ft_form("footered")) == "folder-trust"


# Seam 2 — the render mint (_build_decision_pick_rows).


@pytest.mark.parametrize("variant", _NON_FOOTERED)
def test_mint_refuses_non_footered_variant(variant: str) -> None:
    from unittest.mock import patch

    from cctelegram.handlers import interactive_ui as iui

    with patch.object(iui, "parse_generic_decision", return_value=_ft_form(variant)):
        rows = iui._build_decision_pick_rows(
            _OWNER_ID, _THREAD_ID, _WINDOW_ID, "pane text", _LICENSED
        )
    assert rows is None


def test_mint_accepts_footered_variant() -> None:
    from unittest.mock import patch

    from cctelegram.handlers import interactive_ui as iui

    with patch.object(iui, "parse_generic_decision", return_value=_ft_form("footered")):
        rows = iui._build_decision_pick_rows(
            _OWNER_ID, _THREAD_ID, _WINDOW_ID, "pane text", _LICENSED
        )
    assert rows, "the footered positive control must mint dcp: rows"
    datas = [b.callback_data for row in rows for b in row]
    assert all(d and d.startswith("dcp:") for d in datas)


# Seam 3 — the dispatch pre-commit gate (_dispatch_decision_pane_locked).


async def _run_pane_locked_with_form(
    form: tp.AskUserQuestionForm, minted_fingerprint: str
) -> cbi._DecisionPaneOutcome:
    from unittest.mock import patch

    tmux = SimpleNamespace(
        capture_pane=AsyncMock(return_value="pane text"),
        pane_current_command=AsyncMock(return_value=_LICENSED),
        send_keys=AsyncMock(return_value=True),
    )
    with (
        patch.object(
            cbi,
            "extract_interactive_content",
            return_value=tp.InteractiveUIContent(content="x", name="Decision"),
        ),
        patch.object(cbi, "parse_generic_decision", return_value=form),
    ):
        return await cbi._dispatch_decision_pane_locked(
            user=SimpleNamespace(id=_OWNER_ID),
            tmux_manager=tmux,
            w=SimpleNamespace(window_id=_WINDOW_ID),
            window_id=_WINDOW_ID,
            minted_fingerprint=minted_fingerprint,
            option_number=1,
            option_label=_FT_LABELS[0],
            ledger_key=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", _NON_FOOTERED)
async def test_precommit_bails_on_non_footered_variant(variant: str) -> None:
    form = _ft_form(variant)
    outcome = await _run_pane_locked_with_form(
        form, tp.decision_prompt_fingerprint(form)
    )
    assert outcome.kind == "not_advanced"
    assert outcome.reason == "variant_not_footered"


@pytest.mark.asyncio
async def test_precommit_passes_variant_gate_on_footered() -> None:
    # Positive control for the GATE (not the whole transaction): a footered form
    # proceeds PAST the variant gate — a deliberately-mismatched fingerprint then
    # bails at the NEXT check, proving the variant gate was traversed.
    outcome = await _run_pane_locked_with_form(_ft_form("footered"), "nope")
    assert outcome.kind == "not_advanced"
    assert outcome.reason == "fingerprint_mismatch"


# Seam 4 — the confirm-side (_classify_decision_advance).


@pytest.mark.parametrize("variant", _NON_FOOTERED)
def test_confirm_refuses_non_footered_variant(variant: str) -> None:
    from unittest.mock import patch

    form = _ft_form(variant)
    with (
        patch.object(
            cbi,
            "extract_interactive_content",
            return_value=tp.InteractiveUIContent(content="x", name="Decision"),
        ),
        patch.object(cbi, "parse_generic_decision", return_value=form),
    ):
        # Even with a DIFFERENT fingerprint (which for a proven-footered form
        # would confirm), a non-footered variant must NOT establish resolution.
        assert cbi._classify_decision_advance("pane text", "different-fp") is False


def test_confirm_accepts_different_footered_form() -> None:
    from unittest.mock import patch

    form = _ft_form("footered")
    with (
        patch.object(
            cbi,
            "extract_interactive_content",
            return_value=tp.InteractiveUIContent(content="x", name="Decision"),
        ),
        patch.object(cbi, "parse_generic_decision", return_value=form),
    ):
        assert cbi._classify_decision_advance("pane text", "different-fp") is True
