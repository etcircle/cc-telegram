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


# ── wave-2 review folds ───────────────────────────────────────────────────────

_BASELINE_PANE = (_FIX / "auq-baseline-pane.txt").read_text()


class TestNormalizeRejectIntroducer:
    """P3 — the rejection WARNING blames the byte the normalizer's OWN grammar
    rejected, never a preceding VALID sequence's introducer."""

    def test_sgr_then_bare_bel_blames_the_bel(self):
        raw = "\x1b[1mhi\x1b[0m\x07"  # valid SGRs; the bare BEL is the reject
        assert tp.normalize_capture(raw) is None
        reason = tp.normalize_reject_introducer(raw)
        assert "0x07" in reason, reason
        assert not reason.startswith("ESC"), reason

    def test_esc_outside_the_grammar_blames_the_esc_pair(self):
        raw = "ok\x1b\x00rest"
        assert tp.normalize_capture(raw) is None
        assert tp.normalize_reject_introducer(raw).startswith("ESC+")

    def test_unterminated_string_control_named(self):
        raw = "x\x1b]8;;http://e.com"  # OSC with no BEL/ST terminator
        assert tp.normalize_capture(raw) is None
        assert "unterminated" in tp.normalize_reject_introducer(raw)

    def test_accepted_frame_reports_unknown(self):
        assert tp.normalize_capture("plain text") is not None
        assert tp.normalize_reject_introducer("plain text") == "?"


class TestPickTokenPlainRecapture:
    """P2-A — a normalize REJECTION at the pick_token seams takes the spine's
    ONE plain re-capture (plain-only, no SGR tier) instead of turning into a
    FOREVER-``stale_form`` against a card the render path published via its own
    plain fallback."""

    _WID = "@w2a"
    _USER = 42
    _THREAD = 7

    @pytest.fixture(autouse=True)
    def _reset(self, _cc_dir):
        from cctelegram.handlers import pick_token

        pick_token.reset_for_tests()
        yield
        pick_token.reset_for_tests()

    def _mint_baseline_token(self) -> str:
        from cctelegram.handlers import pick_token

        form = tp.resolve_ask_form(None, _BASELINE_PANE)
        assert form is not None
        src = auq_source.resolve_auq_source(self._WID, None, _BASELINE_PANE)
        return pick_token.mint(
            pick_token.PickTokenEntry(
                window_id=self._WID,
                user_id=self._USER,
                thread_id=self._THREAD,
                fingerprint=form.fingerprint(),
                option_number=1,
                option_label="Done navigating",
                is_review_submit=False,
                expires_at=time.time() + 300,
                source_kind=src.kind,
                source_fingerprint=src.source_fingerprint,
                row_generation=1,
            )
        )

    def _rejecting_capture(self, *, plain_ok: bool = True):
        calls: list[bool] = []

        async def _capture(_wid: str, _scrollback: int, with_ansi: bool = True):
            calls.append(with_ansi)
            if with_ansi:
                return _BASELINE_PANE + "\x07"  # bare BEL ⇒ normalize REJECTS
            return _BASELINE_PANE if plain_ok else None

        return _capture, calls

    @staticmethod
    def _window_finder(wid: str):
        async def _find(_wid: str):
            return SimpleNamespaceWindow(wid)

        return _find

    @pytest.mark.asyncio
    async def test_validate_site_takes_the_one_plain_recapture(self):
        from cctelegram.handlers import pick_token

        token = self._mint_baseline_token()
        capture, calls = self._rejecting_capture()
        result = await pick_token.validate_and_consume(
            token,
            self._USER,
            capture_pane=capture,
            find_window_by_id=self._window_finder(self._WID),
        )
        assert result.outcome == "ok", result.outcome
        assert calls == [True, False]  # exactly ONE plain re-capture

    @pytest.mark.asyncio
    async def test_validate_site_failed_recapture_stays_stale_form(self):
        from cctelegram.handlers import pick_token

        token = self._mint_baseline_token()
        capture, calls = self._rejecting_capture(plain_ok=False)
        result = await pick_token.validate_and_consume(
            token,
            self._USER,
            capture_pane=capture,
            find_window_by_id=self._window_finder(self._WID),
        )
        assert result.outcome == "stale_form"
        assert calls == [True, False]

    @pytest.mark.asyncio
    async def test_recover_site_takes_the_one_plain_recapture(self):
        from cctelegram.handlers import pick_intent, pick_token

        form = tp.resolve_ask_form(None, _BASELINE_PANE)
        assert form is not None
        src = auq_source.resolve_auq_source(self._WID, None, _BASELINE_PANE)
        intent = pick_intent.PickIntent(
            token="tok-recover-w2a",
            full_fingerprint=form.fingerprint(),
            source_kind=src.kind,  # "pane" — parity subsumed by the form match
            source_fingerprint=src.source_fingerprint,
            user_id=self._USER,
            thread_id=self._THREAD,
            window_id=self._WID,
            session_id=None,
            option_number=1,
            option_label="Done navigating",
            is_review_submit=False,
            minted_at=time.time(),
            sibling_option_numbers=(1,),
            sibling_tokens=("tok-recover-w2a",),
        )
        capture, calls = self._rejecting_capture()
        result = await pick_token.recover_and_consume(
            "tok-recover-w2a",
            intent,
            self._USER,
            capture_pane=capture,
            find_window_by_id=self._window_finder(self._WID),
        )
        assert result.outcome == "ok", result.outcome
        assert result.current_form is not None
        assert calls == [True, False]


class SimpleNamespaceWindow:
    def __init__(self, wid: str) -> None:
        self.window_id = wid


class TestZeroOptionRescueNarrowing:
    """P2-B — a zero-option form that still carries CONTRADICTING evidence (a
    parsed tab header / current title inconsistent with the TTL-free side file)
    keeps the pre-W2 ``bail_partial``; the pure no-evidence shape still rescues."""

    _WID = "@rescue-b"
    _SID = "22222222-3333-4444-8555-666666666666"

    def _setup(self, _cc_dir):
        session_manager.window_states[self._WID] = WindowState(
            cwd="/tmp/c", session_id=self._SID
        )
        return _write_preview_side_file(_cc_dir, self._SID)

    def _teardown(self):
        session_manager.window_states.pop(self._WID, None)

    def _resolve_with_form(self, monkeypatch, form):
        def _fake_parse(pane_text, *, ansi_text=None):
            return form

        monkeypatch.setattr(tp, "parse_ask_user_question", _fake_parse)
        return auq_source.resolve_auq_source_for_render(self._WID, "some pane text")

    def test_contradicting_tab_header_bails_never_rescues(self, _cc_dir, monkeypatch):
        tool_input = self._setup(_cc_dir)  # record header: "Chat surface"
        try:
            zero = dataclasses.replace(
                tp.build_form_from_tool_input(tool_input),
                options=(),
                current_question_title=None,
                tabs=(
                    tp.AskTab(
                        label="Alpha", answered=False, is_submit=False, is_current=True
                    ),
                    tp.AskTab(
                        label="Beta", answered=False, is_submit=False, is_current=False
                    ),
                    tp.AskTab(
                        label="Submit", answered=False, is_submit=True, is_current=False
                    ),
                ),
            )
            r = self._resolve_with_form(monkeypatch, zero)
            assert r.decision == "bail"
            assert r.reason == "bail_partial_no_pane_form"
            assert r.dispatch_trusted is False
        finally:
            self._teardown()

    def test_contradicting_title_bails_never_rescues(self, _cc_dir, monkeypatch):
        tool_input = self._setup(_cc_dir)
        try:
            zero = dataclasses.replace(
                tp.build_form_from_tool_input(tool_input),
                options=(),
                current_question_title="A completely different question entirely?",
                tabs=(),
            )
            r = self._resolve_with_form(monkeypatch, zero)
            assert r.decision == "bail"
            assert r.reason == "bail_partial_no_pane_form"
        finally:
            self._teardown()

    def test_no_evidence_zero_option_form_still_rescues(self, _cc_dir, monkeypatch):
        tool_input = self._setup(_cc_dir)
        try:
            zero = dataclasses.replace(
                tp.build_form_from_tool_input(tool_input),
                options=(),
                current_question_title=None,
                tabs=(),
            )
            r = self._resolve_with_form(monkeypatch, zero)
            assert r.decision == "rescue"
        finally:
            self._teardown()

    def test_consistent_tab_header_still_rescues(self, _cc_dir, monkeypatch):
        tool_input = self._setup(_cc_dir)
        try:
            zero = dataclasses.replace(
                tp.build_form_from_tool_input(tool_input),
                options=(),
                current_question_title=None,
                tabs=(
                    tp.AskTab(
                        label="Chat surface",
                        answered=False,
                        is_submit=False,
                        is_current=True,
                    ),
                ),
            )
            r = self._resolve_with_form(monkeypatch, zero)
            assert r.decision == "rescue"
        finally:
            self._teardown()

    # ── r3 residual P2: DAMAGED-but-consistent evidence must NOT contradict ──
    # (contradiction requires CLEAN positive mismatch; ellipsis-truncated
    # fragments compare prefix-tolerantly, and a below-floor shred never
    # decides — indeterminate ⇒ rescue, the pre-narrowing fail direction).

    def test_ellipsized_own_title_still_rescues(self, _cc_dir, monkeypatch):
        # The reviewer's shape (a): a mid-redraw ellipsized/garbled variant of
        # the side file's OWN title (record: "Where should you actually chat
        # with the agent? This decides the whole redesign. …"). Pre-fix the
        # full-string bidirectional-startswith failed ⇒ a GENUINE rescue fell
        # to the display-only bail_partial (the exact loss rescue prevents).
        tool_input = self._setup(_cc_dir)
        try:
            zero = dataclasses.replace(
                tp.build_form_from_tool_input(tool_input),
                options=(),
                current_question_title=(
                    "Where should you actually chat with … whole redesign..."
                ),
                tabs=(),
            )
            r = self._resolve_with_form(monkeypatch, zero)
            assert r.decision == "rescue"
        finally:
            self._teardown()

    def test_ellipsis_truncated_tab_matching_header_still_rescues(
        self, _cc_dir, monkeypatch
    ):
        # The reviewer's shape (b): a CC-truncated tab label whose remaining
        # prefix matches the record header "Chat surface" is CONSISTENT.
        tool_input = self._setup(_cc_dir)
        try:
            zero = dataclasses.replace(
                tp.build_form_from_tool_input(tool_input),
                options=(),
                current_question_title=None,
                tabs=(
                    tp.AskTab(
                        label="Chat surfa…",
                        answered=False,
                        is_submit=False,
                        is_current=True,
                    ),
                ),
            )
            r = self._resolve_with_form(monkeypatch, zero)
            assert r.decision == "rescue"
        finally:
            self._teardown()

    def test_below_floor_shred_title_still_rescues(self, _cc_dir, monkeypatch):
        # Pin (d): a 2-char damaged shred ("Fo…") is INDETERMINATE — it never
        # decides a contradiction (the documented
        # _ZERO_OPT_DAMAGED_EVIDENCE_MIN_CHARS floor).
        tool_input = self._setup(_cc_dir)
        try:
            zero = dataclasses.replace(
                tp.build_form_from_tool_input(tool_input),
                options=(),
                current_question_title="Fo…",
                tabs=(),
            )
            r = self._resolve_with_form(monkeypatch, zero)
            assert r.decision == "rescue"
        finally:
            self._teardown()

    def test_below_floor_shred_tab_still_rescues(self, _cc_dir, monkeypatch):
        # The tab-leg twin of pin (d): a damaged tab shred is skipped, not a
        # contradiction — even though it matches no header.
        tool_input = self._setup(_cc_dir)
        try:
            zero = dataclasses.replace(
                tp.build_form_from_tool_input(tool_input),
                options=(),
                current_question_title=None,
                tabs=(
                    tp.AskTab(
                        label="Zx…",
                        answered=False,
                        is_submit=False,
                        is_current=True,
                    ),
                ),
            )
            r = self._resolve_with_form(monkeypatch, zero)
            assert r.decision == "rescue"
        finally:
            self._teardown()

    def test_long_damaged_foreign_title_still_bails(self, _cc_dir, monkeypatch):
        # A DAMAGED fragment ABOVE the floor that matches NO record question is
        # still a clean positive mismatch on its surviving prefix ⇒ contradicts
        # (the narrowing must not turn every ellipsis into a rescue pass).
        tool_input = self._setup(_cc_dir)
        try:
            zero = dataclasses.replace(
                tp.build_form_from_tool_input(tool_input),
                options=(),
                current_question_title=(
                    "Which database engine should the new service … use..."
                ),
                tabs=(),
            )
            r = self._resolve_with_form(monkeypatch, zero)
            assert r.decision == "bail"
            assert r.reason == "bail_partial_no_pane_form"
        finally:
            self._teardown()


class TestWrappedMultiQuestionCandidateSelection:
    """P2-C — the multi-question candidate SELECTION accepts a wrapped preview
    label via the wrap-canonical leg (it exact-compared reconstructed labels, so
    a wrapped multi-Q preview with an unusable title fell to ``no_candidate`` →
    a pane-only form → buttons suppressed + descriptions lost)."""

    _WID = "@wrapmq"
    _SID = "33333333-4444-5555-8666-777777777777"

    # The AUTHORITY labels: option 2's pane reconstruction is LOSSY (mid-word
    # wrap inserts spurious spaces) so only the wrap-canonical leg matches it.
    _AUTHORITY = (
        "Hyperconsolidated observability megadashboard variant",
        "Supercalifragilisticexpialidociousantidisestablishmentarianism dashboard",
        "Short label",
    )

    def _tool_input(self) -> dict:
        return {
            "questions": [
                {
                    # Deliberately NOT the pane's question text — the title-match
                    # candidate pick must MISS so selection falls to the
                    # subsequence loop (the reviewer's shape).
                    "question": "Q1 — which variant should we build?",
                    "header": "Variant",
                    "multiSelect": False,
                    "options": [{"label": lab} for lab in self._AUTHORITY],
                },
                {
                    "question": "Q2 — and which color scheme?",
                    "header": "Colors",
                    "multiSelect": False,
                    "options": [{"label": "Dark"}, {"label": "Light"}],
                },
            ]
        }

    def test_wrapped_labels_find_the_candidate_and_buttons_mint(self, _cc_dir):
        from cctelegram.handlers import pick_token

        pick_token.reset_for_tests()
        session_manager.window_states[self._WID] = WindowState(
            cwd="/tmp/c", session_id=self._SID
        )
        tool_input = self._tool_input()
        pending = _cc_dir / "auq_pending"
        pending.mkdir(mode=0o700, exist_ok=True)
        (pending / f"{self._SID}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": self._SID,
                    "tool_use_id": "tool-use-wrap-mq",
                    "written_at": time.time(),
                    "tool_input": tool_input,
                }
            )
        )
        try:
            cap = _pair("auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt")
            pane_form = tp.parse_ask_user_question(cap.plain, ansi_text=cap.ansi)
            assert pane_form is not None and pane_form.options

            # The candidate is FOUND (pre-fix: exact label compare on option 2's
            # lossy join → no_candidate).
            record = auq_source._read_pretool_side_file(self._SID)
            assert record is not None
            assert auq_source._record_consistent_with_pane(record, pane_form) == (
                True,
                "ok",
            )

            # End-to-end: side_file_ok ⇒ the merged form carries the AUTHORITATIVE
            # questions matrix ⇒ trusted aqp: buttons mint (and the ctx card's
            # descriptions ride the side-file payload).
            r = auq_source.resolve_auq_source_for_render(
                self._WID, cap.plain, ansi_text=cap.ansi
            )
            assert r.decision == "side_file_ok", (r.decision, r.reason)
            assert r.form is not None and len(r.form.questions) == 2
            assert r.payload == tool_input
            rows = interactive_ui._build_pick_button_rows(
                1,
                42,
                self._WID,
                r.form,
                auq_source.ResolvedAuqSource(
                    kind=r.kind,
                    payload=r.payload,
                    source_fingerprint=r.source_fingerprint,
                ),
            )
            assert rows, "trusted pick buttons must mint on the merged form"
        finally:
            session_manager.window_states.pop(self._WID, None)
            pick_token.reset_for_tests()
