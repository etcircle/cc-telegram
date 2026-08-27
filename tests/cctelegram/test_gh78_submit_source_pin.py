"""GH #78 — the AUQ review-screen Submit tap must validate against the MINTED
source, not a fresh run of a DIFFERENT resolver priority chain.

The bug: on a multi-question review screen the RENDER resolver
(``resolve_auq_source_for_render``) sees a PreToolUse side file that is
inconsistent with the pane (the review screen's "Submit answers"/"Cancel" rows
match no question) and ``bail``s to ``kind="pane"`` — it never falls through to
the JSONL cache. ``pick_token.validate_and_consume`` then re-ran the STRICT
chain (``resolve_auq_source``), which DOES fall through: side_file(rejected) →
explicit(None) → **jsonl_cache**. The jsonl payload overlays the canonical
question text onto the pane form, so the validate fingerprint differs from the
minted one BY CONSTRUCTION and every Submit tap rejected ``stale_form``,
forever (observed: 124 poller re-mints in 6.5 min, then TTL death).

The invariant this file pins: **validate is source-PINNED**. The minted
(kind, source_fingerprint) is resolved to a payload EXACTLY ONCE per tap,
BEFORE any keystroke, via ``auq_source.resolve_minted_payload``; that payload
is then carried through nav-verify and the post-Enter confirm. No site ever
re-runs the strict chain, and no site re-peeks after Enter (a successful
dispatch legitimately makes the source vanish — a re-peek would turn a
successful Submit into a false ``commit_unconfirmed``).

Same bug class as ``tests/cctelegram/handlers/test_gh54_w2_spine.py:362-450``
(mint/validate capture-axis divergence producing a forever-``stale_form``);
here the divergent axis is the SOURCE resolver rather than the capture.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram.callback_dispatcher import interactive
from cctelegram.handlers import auq_source, pick_token, status_polling
from cctelegram.session import WindowState, session_manager
from cctelegram.terminal_parser import resolve_ask_form

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_SUBMIT_PANE = (_FIXTURE_DIR / "auq_multiq_submit_pane.txt").read_text()
_RESOLVED_PANE = "user@host repo % \n"

_USER = 42
_THREAD = 7
_WINDOW = "@78"
_SESSION_ID = "78787878-7878-4878-8878-787878787878"

# The 2-question tool_input the incident's AUQ carried (the same shape
# ``tests/scenarios/test_auq_multi_question.py`` drives). Its overlay adds the
# question titles the review pane does not render, so
# ``resolve_ask_form(_TOOL_INPUT, pane)`` fingerprints DIFFERENTLY from
# ``resolve_ask_form(None, pane)`` — the divergence that broke Submit.
_TOOL_INPUT: dict[str, Any] = {
    "questions": [
        {
            "question": (
                "Which implementation approach should we take for the new "
                "caching layer?"
            ),
            "header": "Approach",
            "multiSelect": False,
            "options": [
                {"label": "Write-through cache with Redis backend"},
                {"label": "Write-back cache with periodic flush"},
                {"label": "No cache, optimize queries instead"},
            ],
        },
        {
            "question": "How should we roll this out to production users?",
            "header": "Rollout",
            "multiSelect": False,
            "options": [
                {"label": "Immediate full rollout to everyone"},
                {"label": "Gradual canary over one week"},
                {"label": "Feature-flagged opt-in only"},
            ],
        },
    ]
}

# A DIFFERENT question — used to prove a REPLACED minted source rejects as
# ``source_drift`` (not ``stale_form``).
_OTHER_TOOL_INPUT: dict[str, Any] = {
    "questions": [
        {
            "question": "Something else entirely?",
            "header": "Other",
            "multiSelect": False,
            "options": [{"label": "Yes"}, {"label": "No"}],
        }
    ]
}


# ── harness ───────────────────────────────────────────────────────────────────


@pytest.fixture
def cc_dir(tmp_path, monkeypatch):
    """Point ``app_dir()`` at tmp_path, bind the window, reset both leaves."""
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    auq_source.reset_for_tests()
    pick_token.reset_for_tests()
    session_manager.window_states[_WINDOW] = WindowState(
        cwd="/tmp/cwd", session_id=_SESSION_ID
    )
    yield tmp_path
    session_manager.window_states.pop(_WINDOW, None)
    auq_source.reset_for_tests()
    pick_token.reset_for_tests()


def _write_side_file(cc_dir: Path, tool_input: dict) -> None:
    """Write a FRESH PreToolUse side file for the bound session."""
    pending = cc_dir / "auq_pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    (pending / f"{_SESSION_ID}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": "toolu_gh78",
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )


def _install_jsonl_cache(payload: dict | None) -> None:
    auq_source.set_jsonl_cache_getter(lambda _wid: payload)


def _window_finder(window_id: str | None = _WINDOW):
    async def _find(_wid: str):
        if window_id is None:
            return None
        return SimpleNamespace(window_id=window_id)

    return _find


def _pane_capture(pane: str):
    async def _capture(_wid: str, _scrollback: int, with_ansi: bool = True) -> str:
        return pane

    return _capture


def _mint(
    *,
    fingerprint: str,
    source_kind: str,
    source_fingerprint: str,
    option_number: int = 1,
    option_label: str = "Submit answers",
    is_review_submit: bool = True,
) -> str:
    return pick_token.mint(
        pick_token.PickTokenEntry(
            window_id=_WINDOW,
            user_id=_USER,
            thread_id=_THREAD,
            fingerprint=fingerprint,
            option_number=option_number,
            option_label=option_label,
            is_review_submit=is_review_submit,
            expires_at=time.monotonic() + 300,
            source_kind=source_kind,
            source_fingerprint=source_fingerprint,
            row_generation=1,
        )
    )


def _mint_incident_row() -> tuple[str, Any]:
    """Mint the incident's card: side file present + INCONSISTENT with the review
    pane + jsonl cache populated → the render resolver ``bail``s to ``pane``.

    Returns ``(token, minted_form)``.
    """
    render = auq_source.resolve_auq_source_for_render(_WINDOW, _SUBMIT_PANE)
    # The incident shape, asserted so this test fails loudly if the render
    # resolver's bail semantics ever change (that would be a deliberate fix of a
    # different kind — see the issue's rejected option 5).
    assert render.decision == "bail", render.decision
    assert render.kind == "pane", render.kind
    assert render.dispatch_trusted is True
    assert render.form is not None
    token = _mint(
        fingerprint=render.form.fingerprint(),
        source_kind=render.kind,
        source_fingerprint=render.source_fingerprint,
    )
    return token, render.form


async def _validate(token: str, *, pane: str = _SUBMIT_PANE):
    return await pick_token.validate_and_consume(
        token,
        _USER,
        capture_pane=_pane_capture(pane),
        find_window_by_id=_window_finder(),
    )


# ── 1. RED core: the incident in one test ─────────────────────────────────────


class TestReviewSubmitValidatesAgainstMintedSource:
    @pytest.mark.asyncio
    async def test_submit_tap_on_review_screen_validates_ok(self, cc_dir):
        """THE incident. Review screen, side file inconsistent (→ minted
        ``pane``), JSONL cache populated. Pre-fix the strict re-resolve at
        validate picked ``jsonl_cache``, whose overlay fingerprint can never
        equal the pane-minted one → forever ``stale_form``."""
        _write_side_file(cc_dir, _TOOL_INPUT)
        _install_jsonl_cache(_TOOL_INPUT)
        token, _form = _mint_incident_row()

        result = await _validate(token)

        assert result.outcome == "ok", result.outcome
        assert result.current_form is not None
        # The pinned (pane) payload was used: the returned form is the PANE-only
        # parse, not the jsonl-overlaid one.
        pane_only = resolve_ask_form(None, _SUBMIT_PANE)
        assert pane_only is not None
        assert result.current_form.fingerprint() == pane_only.fingerprint()

    def test_the_two_resolvers_really_do_diverge_here(self, cc_dir):
        """Pins the ROOT CAUSE so a future resolver change can't silently make
        the test above vacuous: on this pane the render resolver says ``pane``
        while the strict chain says ``jsonl_cache``, and the two payloads
        fingerprint the form differently."""
        _write_side_file(cc_dir, _TOOL_INPUT)
        _install_jsonl_cache(_TOOL_INPUT)

        render = auq_source.resolve_auq_source_for_render(_WINDOW, _SUBMIT_PANE)
        strict = auq_source.resolve_auq_source(_WINDOW, None, _SUBMIT_PANE)
        assert (render.kind, strict.kind) == ("pane", "jsonl_cache")

        pane_form = resolve_ask_form(None, _SUBMIT_PANE)
        overlay_form = resolve_ask_form(_TOOL_INPUT, _SUBMIT_PANE)
        assert pane_form is not None and overlay_form is not None
        assert pane_form.fingerprint() != overlay_form.fingerprint()


# ── 2. The pinned side_file / jsonl_cache kinds ───────────────────────────────


class TestPinnedNonPaneKinds:
    """A minted ``jsonl_cache`` row validates against THAT cache entry only: an
    unchanged cache is ``ok``; a replaced or vanished one is ``source_drift``
    (the honest label — pre-fix a replaced source reported ``stale_form``,
    because the form-fingerprint compare fired before the parity check)."""

    def _mint_jsonl_row(self) -> str:
        _install_jsonl_cache(_TOOL_INPUT)
        src = auq_source.resolve_auq_source(_WINDOW, None, _SUBMIT_PANE)
        assert src.kind == "jsonl_cache"
        form = resolve_ask_form(src.payload, _SUBMIT_PANE)
        assert form is not None
        return _mint(
            fingerprint=form.fingerprint(),
            source_kind=src.kind,
            source_fingerprint=src.source_fingerprint,
        )

    @pytest.mark.asyncio
    async def test_unchanged_cache_is_ok(self, cc_dir):
        token = self._mint_jsonl_row()
        assert (await _validate(token)).outcome == "ok"

    @pytest.mark.asyncio
    async def test_replaced_cache_is_source_drift(self, cc_dir):
        token = self._mint_jsonl_row()
        _install_jsonl_cache(_OTHER_TOOL_INPUT)
        assert (await _validate(token)).outcome == "source_drift"

    @pytest.mark.asyncio
    async def test_vanished_cache_is_source_drift(self, cc_dir):
        token = self._mint_jsonl_row()
        _install_jsonl_cache(None)
        assert (await _validate(token)).outcome == "source_drift"

    @pytest.mark.asyncio
    async def test_pinned_form_change_is_still_stale_form(self, cc_dir):
        """Honest ``stale_form``: the pin RESOLVES (same source) but the live
        pane advanced, so the re-parsed form's fingerprint differs."""
        token = self._mint_jsonl_row()
        assert (await _validate(token, pane=_RESOLVED_PANE)).outcome == "stale_form"


# ── 3. Full dispatch: nav-verify + post-Enter confirm use the SAME pin ────────


class _SequencedPicker:
    """Fake tmux whose pane changes per successive ``capture_pane`` call.

    ``_dispatch_pick_pane_locked`` captures in a fixed order: synthetic-cursor
    guard → nav verify → post-Enter confirm. The last frame repeats once the
    list is exhausted.
    """

    def __init__(self, frames: list[str]) -> None:
        self.window_id = _WINDOW
        self.frames = frames
        self._idx = 0
        self.sent: list[tuple[str, str, bool, bool]] = []

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del with_ansi, scrollback_lines
        if window_id != self.window_id:
            return ""
        frame = self.frames[min(self._idx, len(self.frames) - 1)]
        self._idx += 1
        return frame

    async def find_window_by_id(self, window_id: str) -> Any:
        if window_id != self.window_id:
            return None
        return SimpleNamespace(window_id=self.window_id, window_name="repo")

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.sent.append((window_id, keys, enter, literal))
        return True


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(interactive, "NAV_SETTLE", 0)
    monkeypatch.setattr(interactive, "COMMIT_SETTLE", 0)


class TestFullDispatchUsesThePin:
    @pytest.mark.asyncio
    async def test_review_submit_dispatches_with_a_populated_jsonl_cache(
        self, cc_dir, monkeypatch
    ):
        """Sites 2 (nav verify) and 3 (post-Enter confirm) must use the PINNED
        payload. Pre-fix both re-ran the strict chain, which resolved
        ``jsonl_cache`` → ``verify_failed`` (no keystroke at all)."""
        _write_side_file(cc_dir, _TOOL_INPUT)
        _install_jsonl_cache(_TOOL_INPUT)
        _token, minted_form = _mint_incident_row()

        # guard + verify frames = the live review screen (cursor already on
        # Submit ⇒ zero nav keys); the post-Enter frame = the resolved prompt.
        picker = _SequencedPicker([_SUBMIT_PANE, _SUBMIT_PANE, _RESOLVED_PANE])
        monkeypatch.setattr(interactive.auq_ledger, "record", lambda *a, **k: None)

        outcome = await interactive._dispatch_pick_pane_locked(
            user=SimpleNamespace(id=_USER),
            tmux_manager=picker,
            w=SimpleNamespace(window_id=_WINDOW),
            window_id=_WINDOW,
            fingerprint=minted_form.fingerprint(),
            option_number=1,
            option_label="Submit answers",
            is_review_submit=True,
            current_form=minted_form,
            pinned_payload=None,  # minted kind == "pane" ⇒ pane-only re-parse
            ledger_key=None,
        )

        assert outcome.kind == "dispatched", (outcome.kind, outcome.reason)
        assert picker.sent == [(_WINDOW, "Enter", False, False)]

    @pytest.mark.asyncio
    async def test_non_pane_pin_still_sees_the_resolved_pane_as_gone(
        self, cc_dir, monkeypatch
    ):
        """Post-Enter liveness must be decided PANE-ONLY even when the pin
        carries a real payload (fold: Codex diff-review P2). A retained
        side_file/jsonl payload can synthesize a form over ANY pane — including
        the resolved shell prompt — so a payload-shaped liveness test would
        make ``resolved`` unreachable and downgrade every successful non-pane
        dispatch to ``commit_unconfirmed``. The pin shapes the form only for
        nav-verify and ``_classify_advance``; recovery's side-file payload rides
        this same ``_dispatch_pick_pane_locked`` confirm path."""
        _install_jsonl_cache(_TOOL_INPUT)
        overlay_form = resolve_ask_form(_TOOL_INPUT, _SUBMIT_PANE)
        assert overlay_form is not None
        _mint(
            fingerprint=overlay_form.fingerprint(),
            source_kind="jsonl_cache",
            source_fingerprint=auq_source._canonical_dict_fingerprint(_TOOL_INPUT),
        )

        picker = _SequencedPicker([_SUBMIT_PANE, _SUBMIT_PANE, _RESOLVED_PANE])
        monkeypatch.setattr(interactive.auq_ledger, "record", lambda *a, **k: None)

        outcome = await interactive._dispatch_pick_pane_locked(
            user=SimpleNamespace(id=_USER),
            tmux_manager=picker,
            w=SimpleNamespace(window_id=_WINDOW),
            window_id=_WINDOW,
            fingerprint=overlay_form.fingerprint(),
            option_number=1,
            option_label="Submit answers",
            is_review_submit=True,
            current_form=overlay_form,
            pinned_payload=_TOOL_INPUT,  # non-None pin: the deviation's hard case
            ledger_key=None,
        )

        assert outcome.kind == "dispatched", (outcome.kind, outcome.reason)
        assert picker.sent == [(_WINDOW, "Enter", False, False)]

    @pytest.mark.asyncio
    async def test_dispatch_never_looks_the_source_up_again(self, cc_dir, monkeypatch):
        """PIN ONCE, mechanically. Inside the keystroke transaction NOTHING may
        consult the source again — not the jsonl getter, not the side file. The
        pin is resolved before the first keystroke and carried.

        Why it must be mechanical rather than "it happens to agree": after Enter
        a SUCCESSFUL dispatch legitimately makes the minted source vanish (side
        file consumed / cache cleared on tool resolution), and
        ``_dispatch_pick_pane_locked`` has no post-commit drift outcome — a
        re-peek there could only ever downgrade a success. RED pre-fix: both
        the verify and the confirm step called ``resolve_auq_source``."""
        _write_side_file(cc_dir, _TOOL_INPUT)
        jsonl_calls: list[str] = []
        side_file_calls: list[str] = []
        auq_source.set_jsonl_cache_getter(
            lambda wid: (jsonl_calls.append(wid), _TOOL_INPUT)[1]
        )
        _token, minted_form = _mint_incident_row()

        real_read = auq_source._read_live_pretool_record

        def _counting_read(window_id: str, *a, **k):
            side_file_calls.append(window_id)
            return real_read(window_id, *a, **k)

        picker = _SequencedPicker([_SUBMIT_PANE, _SUBMIT_PANE, _RESOLVED_PANE])
        monkeypatch.setattr(interactive.auq_ledger, "record", lambda *a, **k: None)
        # Start counting only once the pin is already resolved (the mint above
        # legitimately consults both sources).
        monkeypatch.setattr(auq_source, "_read_live_pretool_record", _counting_read)
        jsonl_calls.clear()

        outcome = await interactive._dispatch_pick_pane_locked(
            user=SimpleNamespace(id=_USER),
            tmux_manager=picker,
            w=SimpleNamespace(window_id=_WINDOW),
            window_id=_WINDOW,
            fingerprint=minted_form.fingerprint(),
            option_number=1,
            option_label="Submit answers",
            is_review_submit=True,
            current_form=minted_form,
            pinned_payload=None,
            ledger_key=None,
        )

        assert outcome.kind == "dispatched", (outcome.kind, outcome.reason)
        assert jsonl_calls == [], jsonl_calls
        assert side_file_calls == [], side_file_calls


# ── 4. resolve_minted_payload semantics (the shared helper) ───────────────────


class TestResolveMintedPayload:
    """The helper is ``resolve_auq_source``'s OWN leg for the minted kind,
    evaluated in isolation: same per-leg trust checks, no cross-kind
    fall-through."""

    def test_pane_kind_pins_to_none_and_is_ok(self, cc_dir):
        assert auq_source.resolve_minted_payload(
            _WINDOW, "pane", "deadbeef", _SUBMIT_PANE
        ) == (None, True)

    def test_jsonl_cache_hit(self, cc_dir):
        _install_jsonl_cache(_TOOL_INPUT)
        fp = auq_source._canonical_dict_fingerprint(_TOOL_INPUT)
        assert auq_source.resolve_minted_payload(
            _WINDOW, "jsonl_cache", fp, _SUBMIT_PANE
        ) == (_TOOL_INPUT, True)

    def test_jsonl_cache_miss_does_not_fall_back(self, cc_dir):
        """The deliberate divergence from the ``aqt:`` toggle lane: a replaced
        minted source does NOT fall back to another leg — a pick dispatches
        keys, so it must surface as ``source_drift``. (Pre-fix the chain would
        have happily resolved the side file here.)"""
        _write_side_file(cc_dir, _TOOL_INPUT)
        _install_jsonl_cache(_OTHER_TOOL_INPUT)
        fp = auq_source._canonical_dict_fingerprint(_TOOL_INPUT)
        assert auq_source.resolve_minted_payload(
            _WINDOW, "jsonl_cache", fp, _SUBMIT_PANE
        ) == (None, False)

    def test_side_file_leg_keeps_the_pane_consistency_guard(self, cc_dir):
        """The pin is NOT ``peek_sticky_source``: for ``side_file`` it goes
        through ``resolve_record``, so a record the LIVE PANE contradicts is
        rejected. Pinning pane-agnostically here would let a stale side file
        validate a tap against a genuinely different live question — the
        wrong-action class this repo already guards at
        ``_record_consistent_with_pane``."""
        _write_side_file(cc_dir, _TOOL_INPUT)
        fp = auq_source._canonical_dict_fingerprint(_TOOL_INPUT)

        # A review screen matches no question → the record is inconsistent.
        assert auq_source.resolve_minted_payload(
            _WINDOW, "side_file", fp, _SUBMIT_PANE
        ) == (None, False)
        # …while the pane-agnostic toggle pin still resolves it.
        assert auq_source.peek_sticky_source(_WINDOW, "side_file", fp) == _TOOL_INPUT

    def test_unknown_kind_is_not_pinned(self, cc_dir):
        assert auq_source.resolve_minted_payload(_WINDOW, "bogus", "x", "") == (
            None,
            False,
        )


# ── 5. The drift-remint livelock + deadline starvation ────────────────────────


class TestDriftRemintLatch:
    """Secondary GH #78 failure: ``_remint_on_source_drift`` compares the STRICT
    live source against the minted tags and re-renders through the RENDER
    resolver. On the review screen those two standingly disagree (minted
    ``pane`` vs strict ``jsonl_cache``), so the re-mint could NEVER converge —
    it fired every ~1s tick (124× in 6.5 min observed) AND its True return made
    the poller skip ``refresh_route_deadlines``, so the card's tokens TTL-died
    (~300s) under a live picker."""

    _ROUTE = (_USER, _THREAD, _WINDOW)

    @pytest.fixture(autouse=True)
    def _clean_latch(self):
        status_polling._drift_remint_latch.clear()
        yield
        status_polling._drift_remint_latch.clear()

    def _seed_pane_minted_row(self) -> None:
        """Seed the cache row the card was minted with: the render resolver's
        ``bail`` tags (kind ``pane``) — while the strict chain sees the
        populated jsonl cache."""
        render = auq_source.resolve_auq_source_for_render(_WINDOW, _SUBMIT_PANE)
        assert render.kind == "pane" and render.form is not None
        pick_token._pick_token_cache[
            (_USER, _THREAD, _WINDOW, render.form.fingerprint())
        ] = pick_token._CacheRow(
            tokens=["gh78tok"],
            row_generation=1,
            source_kind=render.kind,
            source_fingerprint=render.source_fingerprint,
            consumed_generation=None,
        )

    @pytest.mark.asyncio
    async def test_non_converging_drift_reminits_once_then_latches(
        self, cc_dir, monkeypatch
    ):
        _write_side_file(cc_dir, _TOOL_INPUT)
        _install_jsonl_cache(_TOOL_INPUT)
        self._seed_pane_minted_row()

        calls: list[int] = []

        async def _fake_ui(*_a, **_k):
            calls.append(1)

        monkeypatch.setattr(status_polling, "handle_interactive_ui", _fake_ui)
        bot = SimpleNamespace()

        first = await status_polling._remint_on_source_drift(
            bot, _USER, _THREAD, _WINDOW, _SUBMIT_PANE
        )
        assert first is True and len(calls) == 1
        # Three more ticks with the SAME standing disagreement — latched.
        for _ in range(3):
            assert (
                await status_polling._remint_on_source_drift(
                    bot, _USER, _THREAD, _WINDOW, _SUBMIT_PANE
                )
                is False
            )
        assert len(calls) == 1, "the non-converging re-mint must fire exactly once"
        assert status_polling._drift_remint_latch[self._ROUTE].tags == (
            "jsonl_cache",
            auq_source._canonical_dict_fingerprint(_TOOL_INPUT),
        )

    @pytest.mark.asyncio
    async def test_failed_rerender_unlatches_so_the_next_tick_retries(
        self, cc_dir, monkeypatch
    ):
        """A re-render that raises (or is cancelled) minted NO replacement card,
        so it must not leave the latch armed — that would suppress repair until
        the source changes or the route is torn down (fold: Codex diff P2)."""
        _write_side_file(cc_dir, _TOOL_INPUT)
        _install_jsonl_cache(_TOOL_INPUT)
        self._seed_pane_minted_row()

        calls: list[int] = []

        async def _failing_ui(*_a, **_k):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("telegram edit blew up")

        monkeypatch.setattr(status_polling, "handle_interactive_ui", _failing_ui)
        bot = SimpleNamespace()

        with pytest.raises(RuntimeError):
            await status_polling._remint_on_source_drift(
                bot, _USER, _THREAD, _WINDOW, _SUBMIT_PANE
            )
        assert self._ROUTE not in status_polling._drift_remint_latch
        # The next tick retries the repair (and latches on its success).
        assert (
            await status_polling._remint_on_source_drift(
                bot, _USER, _THREAD, _WINDOW, _SUBMIT_PANE
            )
            is True
        )
        assert len(calls) == 2
        assert self._ROUTE in status_polling._drift_remint_latch

    @pytest.mark.asyncio
    async def test_failing_arm_never_pops_a_newer_equal_tagged_arm(
        self, cc_dir, monkeypatch
    ):
        """Ownership, not value equality (fold r2: Codex P2). While the failing
        render awaits, the route can be cleared and re-armed with IDENTICAL
        tags by a concurrent tick; the original task's failure path must leave
        that newer arm installed — popping it would grant the next tick an
        extra re-mint the newer arm already spent."""
        _write_side_file(cc_dir, _TOOL_INPUT)
        _install_jsonl_cache(_TOOL_INPUT)
        self._seed_pane_minted_row()

        newer_arm = status_polling._DriftRemintArm(
            ("jsonl_cache", auq_source._canonical_dict_fingerprint(_TOOL_INPUT))
        )

        async def _failing_ui_with_race(*_a, **_k):
            # Simulate the concurrent clear + equal-tagged re-arm mid-await.
            status_polling._drift_remint_latch.pop(self._ROUTE, None)
            status_polling._drift_remint_latch[self._ROUTE] = newer_arm
            raise RuntimeError("telegram edit blew up")

        monkeypatch.setattr(
            status_polling, "handle_interactive_ui", _failing_ui_with_race
        )
        with pytest.raises(RuntimeError):
            await status_polling._remint_on_source_drift(
                SimpleNamespace(), _USER, _THREAD, _WINDOW, _SUBMIT_PANE
            )
        assert status_polling._drift_remint_latch.get(self._ROUTE) is newer_arm

    @pytest.mark.asyncio
    async def test_latch_is_dropped_once_the_source_converges(
        self, cc_dir, monkeypatch
    ):
        """The latch must not disarm the genuine flip the re-mint was built for:
        once live == minted the entry is dropped, so a LATER drift re-mints on
        its first tick."""
        _write_side_file(cc_dir, _TOOL_INPUT)
        _install_jsonl_cache(_TOOL_INPUT)
        self._seed_pane_minted_row()
        monkeypatch.setattr(status_polling, "handle_interactive_ui", _noop_async)
        bot = SimpleNamespace()

        assert await status_polling._remint_on_source_drift(
            bot, _USER, _THREAD, _WINDOW, _SUBMIT_PANE
        )
        assert self._ROUTE in status_polling._drift_remint_latch

        # The jsonl cache clears (tool resolved) → strict now resolves ``pane``,
        # which equals the minted tags → converged.
        _install_jsonl_cache(None)
        assert (
            await status_polling._remint_on_source_drift(
                bot, _USER, _THREAD, _WINDOW, _SUBMIT_PANE
            )
            is False
        )
        assert self._ROUTE not in status_polling._drift_remint_latch

    def test_topic_teardown_clears_the_latch(self):
        status_polling._drift_remint_latch[self._ROUTE] = (
            status_polling._DriftRemintArm(("pane", "abc"))
        )
        other = (_USER, _THREAD + 1, _WINDOW)
        status_polling._drift_remint_latch[other] = status_polling._DriftRemintArm(
            ("pane", "abc")
        )

        status_polling.clear_route_caches_for_topic(_USER, _THREAD)

        assert self._ROUTE not in status_polling._drift_remint_latch
        assert other in status_polling._drift_remint_latch


async def _noop_async(*_a, **_k) -> None:
    return None


def _scrolled_submit_pane() -> str:
    """The site-(b) shape: the tab header has scrolled off, so
    ``extract_interactive_content`` is None while the Submit tail anchors are
    visible — the branch the incident's card lived on."""
    return "".join(_SUBMIT_PANE.splitlines(keepends=True)[2:])


class TestDriftRemintDoesNotStarveDeadlines:
    """``refresh_route_deadlines`` must be reached on the FIRST latched tick
    (the True return) as well as on subsequent ticks. A True return used to
    early-return past it at BOTH poller callsites, so a non-converging drift
    re-rendered forever while the live card's tokens TTL-expired underneath."""

    @pytest.fixture(autouse=True)
    def _route_state(self):
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
        )

        _interactive_mode[(_USER, _THREAD)] = _WINDOW
        _interactive_msgs[(_USER, _THREAD)] = 777
        status_polling._drift_remint_latch.clear()
        yield
        _interactive_mode.pop((_USER, _THREAD), None)
        _interactive_msgs.pop((_USER, _THREAD), None)
        status_polling._last_published_ui_hash.clear()
        status_polling._drift_remint_latch.clear()

    async def _drive(self, monkeypatch, pane: str, drifted: bool) -> list[int]:
        refreshes: list[int] = []

        async def _refresh(*_a, **_k):
            refreshes.append(1)

        async def _remint(*_a, **_k):
            return drifted

        monkeypatch.setattr(status_polling, "handle_interactive_ui", _noop_async)
        monkeypatch.setattr(status_polling, "_remint_on_source_drift", _remint)
        monkeypatch.setattr(
            status_polling.pick_token, "refresh_route_deadlines", _refresh
        )
        monkeypatch.setattr(
            status_polling,
            "tmux_manager",
            SimpleNamespace(
                find_window_by_id=lambda _wid: _async_value(
                    SimpleNamespace(window_id=_WINDOW)
                ),
                capture_pane=lambda *_a, **_k: _async_value(pane),
            ),
        )
        await status_polling.update_status_message(
            SimpleNamespace(), user_id=_USER, window_id=_WINDOW, thread_id=_THREAD
        )
        return refreshes

    @pytest.mark.asyncio
    @pytest.mark.parametrize("drifted", [True, False])
    async def test_same_hash_branch_refreshes_deadlines(self, drifted, monkeypatch):
        from cctelegram.handlers.status_polling import extract_interactive_content

        ui_content = extract_interactive_content(_SUBMIT_PANE)
        assert ui_content is not None
        status_polling._last_published_ui_hash[(_USER, _THREAD, _WINDOW)] = (
            status_polling._ui_render_hash(_WINDOW, _SUBMIT_PANE, ui_content)
        )
        assert await self._drive(monkeypatch, _SUBMIT_PANE, drifted) == [1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("drifted", [True, False])
    async def test_picker_anchor_branch_refreshes_deadlines(self, drifted, monkeypatch):
        assert await self._drive(monkeypatch, _scrolled_submit_pane(), drifted) == [1]


def _async_value(value):
    async def _coro():
        return value

    return _coro()
