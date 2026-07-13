"""GH #54 Wave 2 — the ANSI capture spine + rescue parity + preview dispatch.

Unit coverage for the W2 seams that wire Wave 1's preview-layout parser into the
AUQ render / poller-hash / dispatch call graph:

  * item 6 — SGR-only poller acceptance: the render-identity hash (fed the ANSI
    pair through the ACTUAL ``peek_render_identity`` seam) CHANGES on a
    chevron-less cursor move, while the cursor-blind form fingerprint stays
    EQUAL and preview-panel churn is excluded;
  * item 3 — the auq_source authority-aware label matcher accepts a lossy
    preview label under ``label_matches_authority`` (both wrap kinds) while an
    ordinary option stays byte-identical;
  * item 4 — ``_build_pick_button_rows`` declines a pane-only multi-question
    preview (tabs present, questions matrix absent) yet keeps a single-question
    preview's buttons;
  * item 2 — the rescue-gate parity: a 0-option pane form + a live side file
    resolves ``rescue`` (never ``bail_partial``), while an options-bearing
    partial pane stays ``bail_partial``;
  * the spine's region-equality acceptance for the preview fixtures.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import pytest

import cctelegram.terminal_parser as tp
from cctelegram.handlers import auq_source, interactive_ui
from cctelegram.session import WindowState, session_manager

_FIX = Path(__file__).parents[1] / "fixtures"
_PREVIEW_SIDE_FILE = _FIX / "auq_preview_side_file.json"


def _pair(name: str) -> tp.PaneCapture:
    cap = tp.normalize_capture((_FIX / name).read_text())
    assert cap is not None, name
    return cap


# ── item 6 — SGR-only poller acceptance (through the real hash seam) ──────────


class TestSgrOnlyPollerAcceptance:
    """The render-identity hash is fed the ANSI pair via ``peek_render_identity``.

    The wraplabels cursor pair is chevron-less on 2.1.207 — the cursor is proven
    ONLY by SGR bold+153, so an ANSI-blind hash cannot see a cursor move. These
    pin that (a) the cursor-blind form fingerprint is EQUAL across the pair,
    (b) preview-panel churn is excluded from the identity, and (c) the render
    identity CHANGES via the SGR-derived cursor alone.
    """

    def test_wrap_cursor_blind_fingerprint_equal_across_pair(self):
        c1 = _pair("auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt")
        c2 = _pair("auq_preview_wraplabels_cursor2_v2.1.207.ansi.txt")
        f1 = tp.parse_ask_user_question(c1.plain, ansi_text=c1.ansi)
        f2 = tp.parse_ask_user_question(c2.plain, ansi_text=c2.ansi)
        assert f1 is not None and f2 is not None
        # Distinct cursors on the two frames...
        assert next(o.number for o in f1.options if o.cursor) == 1
        assert next(o.number for o in f2.options if o.cursor) == 2
        # ...but the cursor-blind canonical is identical (panel churn excluded).
        assert f1.fingerprint() == f2.fingerprint()

    def test_wrap_render_identity_changes_via_sgr_cursor(self):
        c1 = _pair("auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt")
        c2 = _pair("auq_preview_wraplabels_cursor2_v2.1.207.ansi.txt")
        h1 = auq_source.peek_render_identity("@w1", c1.plain, ansi_text=c1.ansi)
        h2 = auq_source.peek_render_identity("@w1", c2.plain, ansi_text=c2.ansi)
        assert h1 != h2

    def test_wrap_render_identity_ansi_is_load_bearing(self):
        # Without the ANSI pair the cursor is unprovable on a chevron-less pane,
        # so the two frames collapse to the SAME hash — proving ANSI is what
        # repaints the card on a cursor move (spine seam 2).
        c1 = _pair("auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt")
        c2 = _pair("auq_preview_wraplabels_cursor2_v2.1.207.ansi.txt")
        assert auq_source.peek_render_identity(
            "@w1", c1.plain
        ) == auq_source.peek_render_identity("@w1", c2.plain)

    def test_structural_chevron_render_identity_changes(self):
        # The singleselect pair carries a real ``❯`` (tier-1), so the identity
        # changes on the cursor move with OR without ANSI.
        c1 = _pair("auq_preview_singleselect_v2.1.207.ansi.txt")
        c2 = _pair("auq_preview_singleselect_cursor2_v2.1.207.ansi.txt")
        f1 = tp.parse_ask_user_question(c1.plain, ansi_text=c1.ansi)
        f2 = tp.parse_ask_user_question(c2.plain, ansi_text=c2.ansi)
        assert f1 is not None and f2 is not None
        assert f1.fingerprint() == f2.fingerprint()  # cursor-blind
        assert auq_source.peek_render_identity(
            "@w2", c1.plain, ansi_text=c1.ansi
        ) != auq_source.peek_render_identity("@w2", c2.plain, ansi_text=c2.ansi)


# ── item 3 — the auq_source authority-aware label matcher ─────────────────────


class TestPreviewLabelMatcher:
    def _preview_option(self, label: str, canonical: str):
        return tp.AskOption(
            label=label,
            recommended=False,
            cursor=False,
            number=1,
            wrap_canonical=canonical,
        )

    def test_word_boundary_wrap_matches_authority(self):
        # Word-boundary wrap CONSUMES the space; the joined label carries it.
        opt = self._preview_option(
            "Embed web chat in side panel", "Embedwebchatinsidepanel"
        )
        assert auq_source._pane_option_label_matches(
            opt, "Embed web chat in side panel"
        )

    def test_mid_word_wrap_matches_authority(self):
        # A hard mid-word break does NOT consume the space — the space-joined
        # form gains a spurious space, but the wrap_canonical (no-space) matches.
        opt = self._preview_option("Supercalifragi listic", "Supercalifragilistic")
        assert auq_source._pane_option_label_matches(opt, "Supercalifragilistic")

    def test_ordinary_option_stays_exact(self):
        # wrap_canonical empty ⇒ leg B never fires; a case/space mismatch that
        # the loose ``_normalize_pick_label`` would accept is REJECTED here, so
        # single-column matching is byte-identical (wrong-question protection).
        opt = tp.AskOption(label="yes  sir", recommended=False, cursor=False, number=1)
        assert not auq_source._pane_option_label_matches(opt, "Yes Sir")
        # An exact (recommended-normalized) ordinary match still passes.
        opt2 = tp.AskOption(label="Yes Sir", recommended=False, cursor=False, number=1)
        assert auq_source._pane_option_label_matches(opt2, "Yes Sir (Recommended)")

    def test_record_consistent_with_preview_pane(self):
        # The real 2.1.197 preview pane parsed WITH ANSI, checked against the
        # real preview side-file record → consistent under the wrap rule.
        sf = json.loads(_PREVIEW_SIDE_FILE.read_text())
        record = auq_source.PreToolAskRecord(
            tool_input=sf["tool_input"],
            session_id=sf["session_id"],
            tool_use_id=sf["tool_use_id"],
            written_at=time.time(),
            input_fingerprint="0" * 12,
        )
        cap = _pair("auq_preview_sidebyside_v2.1.197.ansi.txt")
        pane_form = tp.parse_ask_user_question(cap.plain, ansi_text=cap.ansi)
        assert pane_form is not None and pane_form.options
        ok, reason = auq_source._record_consistent_with_pane(record, pane_form)
        assert ok, reason


# ── item 4 — the pane-only multi-question preview decline ─────────────────────


class TestButtonMintingGates:
    def _resolved(self, form):
        return auq_source.ResolvedAuqSource(
            kind="pane",
            payload=None,
            source_fingerprint=auq_source._pane_fingerprint("x"),
        )

    def test_pane_only_multiquestion_preview_declines_buttons(self):
        cap = _pair("auq_preview_multiquestion_q1_v2.1.207.ansi.txt")
        form = tp.parse_ask_user_question(cap.plain, ansi_text=cap.ansi)
        assert form is not None
        assert form._meta.get("layout") == "preview"
        assert not form.questions  # pane-only: no authoritative matrix
        assert [t.label for t in form.tabs if not t.is_submit] == ["Alpha", "Beta"]
        rows = interactive_ui._build_pick_button_rows(
            1, 42, "@mq", form, self._resolved(form)
        )
        assert rows == []  # no trusted aqp: rows — cannot confirm a Q1→Q2 advance

    def test_single_question_preview_keeps_buttons(self):
        from cctelegram.handlers import pick_token

        pick_token.reset_for_tests()
        cap = _pair("auq_preview_singleselect_v2.1.207.ansi.txt")
        form = tp.parse_ask_user_question(cap.plain, ansi_text=cap.ansi)
        assert form is not None and form._meta.get("layout") == "preview"
        assert not form.tabs  # single-question: no tab header
        rows = interactive_ui._build_pick_button_rows(
            2, 7, "@sq", form, self._resolved(form)
        )
        assert rows, "single-question preview must mint pick buttons"
        pick_token.reset_for_tests()


# ── item 2 — the W2 rescue-gate parity ────────────────────────────────────────


@pytest.fixture
def _cc_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    auq_source.reset_for_tests()
    yield tmp_path
    auq_source.reset_for_tests()


def _write_preview_side_file(cc_dir: Path, session_id: str) -> dict:
    sf = json.loads(_PREVIEW_SIDE_FILE.read_text())
    pending = cc_dir / "auq_pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    (pending / f"{session_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": sf["tool_use_id"],
                "written_at": time.time(),
                "tool_input": sf["tool_input"],
            }
        )
    )
    return sf["tool_input"]


class TestRescueGateParity:
    _WID = "@rescue"
    _SID = "11111111-2222-3333-4444-555555555555"

    def test_zero_option_form_plus_live_side_file_rescues(self, _cc_dir, monkeypatch):
        """W1 slip / a pane that yields a 0-OPTION non-None form + a live side
        file ⇒ decision ``rescue`` (never ``bail_partial``). W1 is disabled by
        MONKEYPATCHING ``parse_ask_user_question`` to return a 0-option form (the
        pre-W1 F3 shape), reproducing the resolver-predicate mismatch W2 closes.
        """
        session_manager.window_states[self._WID] = WindowState(
            cwd="/tmp/c", session_id=self._SID
        )
        tool_input = _write_preview_side_file(_cc_dir, self._SID)
        zero_form = dataclasses.replace(
            tp.build_form_from_tool_input(tool_input), options=()
        )

        def _fake_parse(pane_text, *, ansi_text=None):
            return zero_form

        monkeypatch.setattr(tp, "parse_ask_user_question", _fake_parse)
        try:
            r = auq_source.resolve_auq_source_for_render(self._WID, "some pane text")
            assert r.decision == "rescue"
            assert r.dispatch_trusted is False
            assert r.kind == "side_file"
        finally:
            session_manager.window_states.pop(self._WID, None)

    def test_options_bearing_partial_pane_stays_bail_partial(
        self, _cc_dir, monkeypatch
    ):
        """An options-bearing but INCOMPLETE/inconsistent pane form (not a
        0-option one) is NOT swept into rescue — it stays the ``bail_partial``
        pane render (byte-identical to today).
        """
        session_manager.window_states[self._WID] = WindowState(
            cwd="/tmp/c", session_id=self._SID
        )
        tool_input = _write_preview_side_file(_cc_dir, self._SID)
        full = tp.build_form_from_tool_input(tool_input)
        # An incomplete pane form: options present, but NOT a complete picker and
        # inconsistent with the side file (single foreign option, no matrix).
        partial = dataclasses.replace(
            full,
            options=(
                tp.AskOption(
                    label="Totally different option",
                    recommended=False,
                    cursor=True,
                    number=1,
                ),
            ),
            options_complete=False,
            questions=(),
        )

        def _fake_parse(pane_text, *, ansi_text=None):
            return partial

        monkeypatch.setattr(tp, "parse_ask_user_question", _fake_parse)
        try:
            r = auq_source.resolve_auq_source_for_render(self._WID, "some pane text")
            assert r.decision == "bail"
            assert r.reason.startswith("bail_partial_")
            assert r.dispatch_trusted is False
        finally:
            session_manager.window_states.pop(self._WID, None)


# ── the spine's region-equality acceptance for the preview fixtures ───────────


class TestNormalizeCaptureRegionEquality:
    @pytest.mark.parametrize(
        ("plain_name", "ansi_name"),
        [
            (
                "auq_preview_sidebyside_v2.1.197.aligned.txt",
                "auq_preview_sidebyside_v2.1.197.ansi.txt",
            ),
            (
                "auq_preview_singleselect_v2.1.207.txt",
                "auq_preview_singleselect_v2.1.207.ansi.txt",
            ),
            (
                "auq_preview_wraplabels_cursor1_v2.1.207.txt",
                "auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt",
            ),
            (
                "auq_preview_multiquestion_q1_v2.1.207.txt",
                "auq_preview_multiquestion_q1_v2.1.207.ansi.txt",
            ),
        ],
    )
    def test_normalized_ansi_plain_equals_plain_after_rstrip(
        self, plain_name, ansi_name
    ):
        plain = (_FIX / plain_name).read_text()
        cap = tp.normalize_capture((_FIX / ansi_name).read_text())
        assert cap is not None
        want = "\n".join(ln.rstrip() for ln in plain.split("\n"))
        got = "\n".join(ln.rstrip() for ln in cap.plain.split("\n"))
        assert got == want
