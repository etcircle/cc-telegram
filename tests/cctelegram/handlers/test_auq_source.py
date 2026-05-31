"""Tests for the AUQ-source resolver leaf (R5).

Covers the public ``auq_source`` seam: the typed ``resolve_auq_source``
resolver with its per-kind ``source_fingerprint`` (the mint/validate parity
witness), the injected JSONL-cache getter lifecycle, and the
remember-before-mint parity invariant (§8.1). The trust-boundary unit tests
(path traversal, schema/fingerprint, TTL/skew, the ``checked_any``
vacuous-true case) live in ``test_interactive_ui.py``'s pretool block, now
re-pointed at this seam; this file adds the resolver-return + fingerprint
coverage that R5 introduces.

Fixtures are REAL captures: ``auq_single_select_with_affordances_*`` is a
paired pane + side file for the ``side_file`` kind; ``auq-baseline-pane.txt``
is a real picker capture for the ``pane`` kind.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cctelegram.handlers import auq_source
from cctelegram.session import WindowState, session_manager
from cctelegram.terminal_parser import resolve_ask_form

_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures"
_AFFORDANCE_SIDEFILE = _FIXTURE_DIR / "auq_single_select_with_affordances_sidefile.json"
_AFFORDANCE_PANE = _FIXTURE_DIR / "auq_single_select_with_affordances_pane.txt"
_BASELINE_PANE = _FIXTURE_DIR / "auq-baseline-pane.txt"


@pytest.fixture
def _cc_dir(tmp_path, monkeypatch):
    """Point app_dir() at tmp_path and reset the leaf before/after."""
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    auq_source.reset_for_tests()
    yield tmp_path
    auq_source.reset_for_tests()


def _bind_window(window_id: str, session_id: str) -> None:
    session_manager.window_states[window_id] = WindowState(
        cwd="/tmp/cwd", session_id=session_id
    )


def _unbind_window(window_id: str) -> None:
    session_manager.window_states.pop(window_id, None)


def _write_affordance_side_file(cc_dir: Path, session_id: str) -> dict:
    """Write the real affordances side file under cc_dir, fresh ``written_at``.

    Returns the ``tool_input`` dict the side file carries.
    """
    sidefile = json.loads(_AFFORDANCE_SIDEFILE.read_text())
    tool_input = sidefile["tool_input"]
    pending = cc_dir / "auq_pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    (pending / f"{session_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": sidefile["tool_use_id"],
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )
    return tool_input


# ── side_file kind ───────────────────────────────────────────────────────────


class TestResolveSideFileKind:
    _WID = "@auqsrc-sf"
    _SID = "4766fb07-7057-4981-9832-93e524ab943e"

    def test_resolves_side_file_kind_with_stable_fingerprint(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            tool_input = _write_affordance_side_file(_cc_dir, self._SID)
            pane = _AFFORDANCE_PANE.read_text()

            resolved = auq_source.resolve_auq_source(self._WID, None, pane)
            assert resolved.kind == "side_file"
            assert resolved.payload == tool_input

            # Same inputs → same fingerprint (stable witness).
            again = auq_source.resolve_auq_source(self._WID, None, pane)
            assert again.source_fingerprint == resolved.source_fingerprint
        finally:
            _unbind_window(self._WID)

    def test_mutated_side_file_source_yields_different_fingerprint(self, _cc_dir):
        _bind_window(self._WID, self._SID)
        try:
            _write_affordance_side_file(_cc_dir, self._SID)
            pane = _AFFORDANCE_PANE.read_text()
            base = auq_source.resolve_auq_source(self._WID, None, pane)
            assert base.kind == "side_file"

            # Mutate the side file's tool_input (drop an option), keeping the
            # first three labels so the pane still matches → still side_file,
            # but a DIFFERENT source fingerprint (the drift case).
            sidefile = json.loads(_AFFORDANCE_SIDEFILE.read_text())
            mutated = sidefile["tool_input"]
            mutated["questions"][0]["header"] = "MUTATED HEADER"
            (_cc_dir / "auq_pending" / f"{self._SID}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": self._SID,
                        "tool_use_id": sidefile["tool_use_id"],
                        "written_at": time.time(),
                        "tool_input": mutated,
                    }
                )
            )
            auq_source.reset_for_tests()  # drop the cached record
            drifted = auq_source.resolve_auq_source(self._WID, None, pane)
            assert drifted.kind == "side_file"
            assert drifted.source_fingerprint != base.source_fingerprint
        finally:
            _unbind_window(self._WID)


# ── jsonl_cache kind ─────────────────────────────────────────────────────────


class TestResolveJsonlCacheKind:
    _WID = "@auqsrc-jc"

    _CACHE_INPUT = {
        "questions": [
            {
                "question": "Pick a fruit",
                "options": [{"label": "Apple"}, {"label": "Banana"}],
            }
        ]
    }

    def test_explicit_dict_resolves_jsonl_cache_kind(self, _cc_dir):
        # No side file, explicit dict given → jsonl_cache branch.
        resolved = auq_source.resolve_auq_source(self._WID, self._CACHE_INPUT, "")
        assert resolved.kind == "jsonl_cache"
        assert resolved.payload == self._CACHE_INPUT
        again = auq_source.resolve_auq_source(self._WID, self._CACHE_INPUT, "")
        assert again.source_fingerprint == resolved.source_fingerprint

    def test_injected_cache_resolves_jsonl_cache_kind(self, _cc_dir):
        # No side file, explicit None, injected cache populated → jsonl_cache.
        auq_source.set_jsonl_cache_getter(
            lambda wid: self._CACHE_INPUT if wid == self._WID else None
        )
        resolved = auq_source.resolve_auq_source(self._WID, None, "")
        assert resolved.kind == "jsonl_cache"
        assert resolved.payload == self._CACHE_INPUT

    def test_mutated_cache_source_yields_different_fingerprint(self, _cc_dir):
        base = auq_source.resolve_auq_source(self._WID, self._CACHE_INPUT, "")
        mutated = {
            "questions": [
                {
                    "question": "Pick a fruit",
                    "options": [{"label": "Apple"}, {"label": "Cherry"}],
                }
            ]
        }
        drifted = auq_source.resolve_auq_source(self._WID, mutated, "")
        assert drifted.kind == "jsonl_cache"
        assert drifted.source_fingerprint != base.source_fingerprint


# ── pane kind ─────────────────────────────────────────────────────────────────


class TestResolvePaneKind:
    _WID = "@auqsrc-pane"

    def test_resolves_pane_kind_with_stable_fingerprint(self, _cc_dir):
        # No side file, explicit None, no injected cache (reset default) →
        # the pane branch. payload is None; fingerprint over the form's
        # canonical repr.
        pane = _BASELINE_PANE.read_text()
        # Sanity: the baseline pane really parses to a form.
        assert resolve_ask_form(None, pane) is not None

        resolved = auq_source.resolve_auq_source(self._WID, None, pane)
        assert resolved.kind == "pane"
        assert resolved.payload is None
        assert resolved.source_fingerprint  # non-empty sha

        # Same pane → same fingerprint. (NO drift test: a changed pane changes
        # the FORM fingerprint, and validation returns stale_form first — the
        # pane source fp shares the canonical input with the form fp; §8.1.)
        again = auq_source.resolve_auq_source(self._WID, None, pane)
        assert again.source_fingerprint == resolved.source_fingerprint


# ── getter lifecycle / reset isolation ───────────────────────────────────────


class TestGetterResetIsolation:
    _WID = "@auqsrc-reset"

    _CACHE_INPUT = {
        "questions": [{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}]
    }

    def test_reset_restores_noop_getter(self, _cc_dir):
        pane = _BASELINE_PANE.read_text()
        auq_source.set_jsonl_cache_getter(
            lambda wid: self._CACHE_INPUT if wid == self._WID else None
        )
        # With the fake getter, explicit=None resolves jsonl_cache.
        resolved = auq_source.resolve_auq_source(self._WID, None, pane)
        assert resolved.kind == "jsonl_cache"

        # reset_for_tests rebinds the getter back to the no-op default.
        auq_source.reset_for_tests()
        after = auq_source.resolve_auq_source(self._WID, None, pane)
        assert after.kind == "pane", (
            "reset_for_tests() must restore the no-op getter so a fake cache "
            "cannot leak across tests"
        )


# ── remember-before-mint parity invariant (§8.1) ─────────────────────────────


class TestRememberBeforeMintParity:
    """The load-bearing JSONL-render parity dependency (§8.1).

    The JSONL render path calls ``interactive_ui.remember_ask_tool_input``
    BEFORE mint, which populates ``_last_completed_ask_tool_input``. The
    injected getter reads exactly that dict, so a validator calling
    ``resolve_auq_source(wid, None, pane)`` lands on the SAME source the
    minter saw — same dict, same fingerprint. This pins that the production
    getter (wired in conftest, mirroring bot.post_init) reads the cache.
    """

    _WID = "@auqsrc-parity"
    _INPUT = {
        "questions": [{"question": "Q", "options": [{"label": "A"}, {"label": "B"}]}]
    }

    def test_remember_then_resolve_sees_same_jsonl_source(self, _cc_dir):
        from cctelegram.handlers import interactive_ui

        # Mirror bot.post_init / conftest wiring: the production getter reads
        # interactive_ui's in-process cache.
        auq_source.set_jsonl_cache_getter(
            lambda wid: interactive_ui._last_completed_ask_tool_input.get(wid)
        )
        try:
            interactive_ui.remember_ask_tool_input(self._WID, self._INPUT, "toolu_x")

            # No side file, explicit None, empty pane → jsonl_cache branch reads
            # the remembered dict via the getter.
            resolved = auq_source.resolve_auq_source(self._WID, None, "")
            assert resolved.kind == "jsonl_cache"
            assert resolved.payload == self._INPUT

            # The fingerprint is the exact same one the minter would record for
            # this source (deterministic over the same dict).
            again = auq_source.resolve_auq_source(self._WID, None, "")
            assert again.source_fingerprint == resolved.source_fingerprint
        finally:
            interactive_ui._last_completed_ask_tool_input.pop(self._WID, None)
