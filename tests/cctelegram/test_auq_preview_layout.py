"""GH #54 W1 — AskUserQuestion side-by-side PREVIEW layout parser + capture spine.

Fixture-pinned against real Claude Code 2.1.207 (single/multi-question,
wrap-label, SGR-only cursor) and 2.1.197 (live preview capture) fixtures.
Covers: ``normalize_capture`` region-equality (W1.6/F9), the display-width
helper (W1.1), the preview parse (W1.0-W1.5), panel-content exclusion (W1.4),
the wrap-canonical authority matcher (W1.2), and the no-synthesis rule (F6).
"""

import pathlib

import pytest

from cctelegram import terminal_parser as tp
from cctelegram.terminal_parser import (
    display_width,
    label_matches_authority,
    normalize_capture,
    parse_ask_user_question,
    resolve_ask_form,
)

_FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


# The version-suffixed ANSI/plain preview pairs, enumerated by GLOB (Codex P3
# — a future fixture pair must never silently skip acceptance). The plain twin
# is the same stem's `.txt`, EXCEPT when a scope-ALIGNED `.aligned.txt` exists
# (the 2.1.197 pair: its raw plain was captured at a wider ``-S`` than the
# ANSI, so only the aligned last-150-lines file is region-comparable).


def _enumerate_preview_pairs() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for ansi_path in sorted(_FIXTURES.glob("auq_preview_*.ansi.txt")):
        stem = ansi_path.name[: -len(".ansi.txt")]
        aligned = _FIXTURES / f"{stem}.aligned.txt"
        plain = aligned if aligned.exists() else _FIXTURES / f"{stem}.txt"
        pairs.append((ansi_path.name, plain.name))
    return pairs


_PREVIEW_PAIRS = _enumerate_preview_pairs()


# ── W1.6 / F9 — normalize_capture region equality ──────────────────────────


class TestNormalizeCapture:
    def test_every_ansi_fixture_has_a_plain_twin(self):
        """The glob enumeration (Codex P3) must never pair an ANSI fixture with
        a missing plain twin — that would silently skip acceptance."""
        assert _PREVIEW_PAIRS, "no preview ANSI fixtures found by the glob"
        for ansi_name, plain_name in _PREVIEW_PAIRS:
            assert (_FIXTURES / ansi_name).is_file()
            assert (_FIXTURES / plain_name).is_file(), (
                f"{ansi_name} has no plain twin {plain_name}"
            )

    @pytest.mark.parametrize(("ansi_name", "plain_name"), _PREVIEW_PAIRS)
    def test_region_equality_exact(self, ansi_name: str, plain_name: str):
        """``normalize_capture(ansi).plain`` is EXACTLY the plain capture (Codex
        P2-3 — the tmux trailing-pad normalization lives INSIDE the spine, so
        the acceptance comparison needs no external rstrip)."""
        cap = normalize_capture(_load(ansi_name))
        assert cap is not None
        assert cap.plain == _load(plain_name)

    def test_osc8_hyperlink_is_consumed_whole(self):
        """The OSC-8 hyperlink (F9) is consumed through ST — its invisible
        metadata (the ``id=`` param) never survives, unlike the legacy
        ``_strip_ansi`` which leaves the whole OSC payload behind."""
        raw = _load("auq_preview_singleselect_v2.1.207.ansi.txt")
        assert "id=1iz7s2z" in raw  # the OSC-8 hyperlink metadata is present
        cap = normalize_capture(raw)
        assert cap is not None
        assert "1iz7s2z" not in cap.plain  # the whole OSC payload is consumed
        # The legacy strip leaves the OSC-8 payload behind — the very junk the
        # region-equality normalize closes (F9).
        assert "1iz7s2z" in tp._strip_ansi(raw)

    def test_unknown_control_byte_rejects(self):
        """An UNKNOWN control byte (not part of a recognized escape family)
        REJECTS the pair — the fail-closed rejection signal must exist."""
        # A bare NUL / SOH after otherwise-clean text is not an escape family.
        assert normalize_capture("hello\x00world") is None
        assert normalize_capture("a\x07b") is None  # bare BEL is a control byte
        # A dangling / unterminated OSC also rejects.
        assert normalize_capture("x\x1b]8;;http://e") is None

    # ── Codex P2-2 — the ECMA-48 CSI / ESC grammar, the exact sequences ──

    @pytest.mark.parametrize(
        "seq",
        [
            "\x1b[?25l",  # private-parameter CSI (hide cursor)
            "\x1b[>0c",  # private-parameter CSI (device attributes)
            "\x1b[0 q",  # intermediate-byte CSI (cursor style; SP intermediate)
        ],
    )
    def test_valid_csi_forms_are_consumed(self, seq: str):
        cap = normalize_capture(f"a{seq}b")
        assert cap is not None
        assert cap.plain == "ab"

    def test_charset_designation_consumes_the_designator(self):
        """``ESC ( 0`` takes one following byte — the ``0`` designator must be
        consumed, never leaked into plain."""
        cap = normalize_capture("x\x1b(0y")
        assert cap is not None
        assert cap.plain == "xy"

    def test_esc_followed_by_a_control_byte_rejects(self):
        """``ESC NUL`` is outside the ECMA-48 grammar — reject, never a blind
        two-byte skip."""
        assert normalize_capture("a\x1b\x00b") is None

    def test_trailing_pad_normalized_inside_the_spine(self):
        """Per-line trailing whitespace is normalized INSIDE normalize_capture
        (Codex P2-3); mid-line whitespace and line structure are untouched."""
        cap = normalize_capture("a\tb   \nc\r")
        assert cap is not None
        assert cap.plain == "a\tb\nc"

    def test_returns_ansi_verbatim(self):
        raw = _load("auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt")
        cap = normalize_capture(raw)
        assert cap is not None
        assert cap.ansi == raw


# ── W1.1 — display-cell width (wcswidth semantics) ─────────────────────────


class TestDisplayWidth:
    def test_ascii_is_one_cell_each(self):
        assert display_width("hello") == 5
        assert display_width(" 1. Hyperconsolidated") == len(" 1. Hyperconsolidated")

    def test_east_asian_wide_is_two_cells(self):
        assert display_width("世界") == 4  # two wide CJK chars
        assert display_width("aあb") == 4  # 1 + 2 + 1

    def test_combining_mark_is_zero_width(self):
        # "e" + combining acute accent renders in one cell.
        assert display_width("é") == 1

    def test_zwj_emoji_sequence_is_one_two_cell_grapheme(self):
        # Family emoji: 👨‍👩‍👧 renders as a single two-cell grapheme, not the
        # per-codepoint EAW sum (which would over-count the joined members).
        family = "\U0001f468‍\U0001f469‍\U0001f467"
        assert display_width(family) == 2

    def test_vs16_forces_two_cell_emoji_presentation(self):
        # A text-default symbol + VS16 (U+FE0F) presents as a two-cell emoji.
        assert display_width("❤️") == 2  # ❤️
        assert display_width("❤") == 1  # ❤ (text presentation, narrow)

    def test_vs16_on_an_already_wide_emoji_stays_two_cells(self):
        # Codex P1-1: 👩 is already EAW-wide (2); VS16 must not ADD a cell.
        assert display_width("\U0001f469️") == 2

    def test_skin_tone_modifier_is_zero_width(self):
        # Codex P1-1: 👍🏽 is ONE two-cell grapheme — the modifier (EAW W on its
        # own) extends the base at zero width, never a per-codepoint 2+2 sum.
        assert display_width("\U0001f44d\U0001f3fd") == 2

    def test_box_drawing_is_one_cell(self):
        assert display_width("┌──┐") == 4


# ── W1.0-W1.5 — the preview parse ──────────────────────────────────────────

_SINGLE_SELECT = {
    "auq_preview_singleselect_v2.1.207": (
        ["Stacked summary panel", "Side-by-side columns", "Single-line ticker"],
        1,
    ),
    "auq_preview_singleselect_cursor2_v2.1.207": (
        ["Stacked summary panel", "Side-by-side columns", "Single-line ticker"],
        2,
    ),
    "auq_preview_wraplabels_cursor1_v2.1.207": (
        [
            "Hyperconsolidated observability megadashboard variant",
            "Supercalifragilisticexpial idociousantidisestablishmen "
            "tarianism dashboard",
            "Short label",
        ],
        1,
    ),
    "auq_preview_wraplabels_cursor2_v2.1.207": (
        [
            "Hyperconsolidated observability megadashboard variant",
            "Supercalifragilisticexpial idociousantidisestablishmen "
            "tarianism dashboard",
            "Short label",
        ],
        2,
    ),
    "auq_preview_wraplabels_v2.1.207": (
        [
            "Hyperconsolidated observability megadashboard variant",
            "Supercalifragilisticexpial idociousantidisestablishmen "
            "tarianism dashboard",
            "Short label",
        ],
        1,
    ),
}


class TestPreviewParse:
    @pytest.mark.parametrize("base", sorted(_SINGLE_SELECT))
    def test_single_select_fixtures(self, base: str):
        labels, cursor = _SINGLE_SELECT[base]
        form = parse_ask_user_question(
            _load(f"{base}.txt"), ansi_text=_load(f"{base}.ansi.txt")
        )
        assert form is not None
        assert form._meta.get("layout") == "preview"
        assert [o.number for o in form.options] == [1, 2, 3]
        assert [o.label for o in form.options] == labels
        assert [o.number for o in form.options if o.cursor] == [cursor]
        assert form.is_free_text is False  # W1.5 — preview has no "Type something"
        assert form.tabs == ()
        assert form.options_complete is True  # W1.1b

    def test_2_1_197_live_capture(self):
        form = parse_ask_user_question(
            _load("auq_preview_sidebyside_v2.1.197.aligned.txt"),
            ansi_text=_load("auq_preview_sidebyside_v2.1.197.ansi.txt"),
        )
        assert form is not None
        assert form._meta.get("layout") == "preview"
        assert [o.label for o in form.options] == [
            "Embed web chat in side panel",  # a word-boundary wrap joined
            "Native side-panel chat",
            "Web app is the cockpit",
        ]
        assert [o.number for o in form.options if o.cursor] == [1]

    @pytest.mark.parametrize(
        ("base", "cursor", "answered_alpha"),
        [
            ("auq_preview_multiquestion_q1_v2.1.207", 1, False),
            ("auq_preview_multiquestion_q2_v2.1.207", 1, True),
        ],
    )
    def test_multi_question_populated_tabs(
        self, base: str, cursor: int, answered_alpha: bool
    ):
        form = parse_ask_user_question(
            _load(f"{base}.txt"), ansi_text=_load(f"{base}.ansi.txt")
        )
        assert form is not None
        assert form._meta.get("layout") == "preview"
        # Tabs survive on the preview multi-question layout (W1.0 governance).
        assert [t.label for t in form.tabs] == ["Alpha", "Beta", "Submit"]
        assert form.tabs[0].answered is answered_alpha
        assert form.tabs[-1].is_submit is True
        assert len(form.options) == 3
        assert [o.number for o in form.options if o.cursor] == [cursor]

    def test_wrap_canonical_stored_on_options(self):
        form = parse_ask_user_question(
            _load("auq_preview_wraplabels_cursor1_v2.1.207.txt"),
            ansi_text=_load("auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt"),
        )
        assert form is not None
        # Option 2 is a HARD mid-word wrap — the no-space join reconstructs it.
        assert (
            form.options[1].wrap_canonical
            == "Supercalifragilisticexpialidociousantidisestablishmen"
            "tarianism dashboard"
        )

    def test_multiselect_is_not_preview_and_ansi_is_neutral(self):
        """The 2.1.207 multi-select fixture parses BYTE-IDENTICALLY with/without
        ANSI and is NOT the preview layout (F8: standard tabbed layout, no
        panel)."""
        plain = _load("auq_preview_multiselect_v2.1.207.txt")
        ansi = _load("auq_preview_multiselect_v2.1.207.ansi.txt")
        without = parse_ask_user_question(plain)
        with_ansi = parse_ask_user_question(plain, ansi_text=ansi)
        assert without is not None
        assert without == with_ansi
        assert without._meta.get("layout") != "preview"
        assert without.select_mode == "multi"

    def test_r5_anchor_preview_above_live_ordinary_picker(self):
        """A historical preview picker in scrollback ABOVE a live ORDINARY
        picker must never activate the preview path — the live ordinary picker
        (cursor 2) wins (bottom-most-is-live)."""
        preview = _load("auq_preview_singleselect_v2.1.207.txt")
        ordinary = _load("auq_single_long_scrolled_cursor2_S500.txt")
        form = parse_ask_user_question(preview + "\n" + ordinary)
        assert form is not None
        assert form._meta.get("layout") != "preview"
        assert [o.number for o in form.options if o.cursor] == [2]

    def test_emoji_label_is_never_silently_truncated(self):
        """Codex P1-1, the REPRODUCED case: replacing the seven-cell "Stacked"
        with an equal-DISPLAY-WIDTH three-family-emoji+"d" string (7 cells, 16
        chars) must parse an intact label — the old per-codepoint truncation
        over-counted the emoji and silently cut the label mid-way."""
        family = "\U0001f468‍\U0001f469‍\U0001f467"
        token = family * 3 + "d"
        assert display_width(token) == 7 == display_width("Stacked")
        pane = _load("auq_preview_singleselect_v2.1.207.txt").replace(
            "Stacked", token, 1
        )
        form = parse_ask_user_question(pane)  # tier-1 chevron; plain suffices
        assert form is not None
        assert form._meta.get("layout") == "preview"
        assert form.options_complete is True
        assert form.options[0].label == f"{token} summary panel"
        # No panel content leaked and nothing was cut.
        assert "d sum" != form.options[0].label[-5:]

    def test_non_indented_row_with_panel_art_rejects_the_preview_parse(self):
        """Codex P1-2, the REPRODUCED case: a column-0 text row carrying panel
        art (boundary-aligned so the vote stays consistent) is NOT a
        continuation — it must REJECT the preview parse, never rename option 1
        to "... NOT_INDENTED"."""
        pane = _load("auq_preview_singleselect_v2.1.207.txt")
        lines = pane.split("\n")
        opt1_idx = next(
            i for i, ln in enumerate(lines) if ln.lstrip().startswith("❯ 1.")
        )
        box_col = lines[opt1_idx].index("┌")
        injected = "NOT_INDENTED".ljust(box_col) + "│ x │"
        lines.insert(opt1_idx + 1, injected)
        form = parse_ask_user_question("\n".join(lines))
        # The preview path REJECTS; whatever the ordinary walk yields, the
        # injected text never becomes part of a trusted preview label.
        if form is not None:
            assert form._meta.get("layout") != "preview"
            assert form.options_complete is False
            assert all("NOT_INDENTED" not in (o.label or "") for o in form.options)

    def test_continuation_with_no_option_above_rejects(self):
        """A CONT-classified row with no numbered option above it is an
        unrecognized row — the preview parse rejects (P1-2 fail-closed)."""
        pane = _load("auq_preview_singleselect_v2.1.207.txt")
        lines = pane.split("\n")
        opt1_idx = next(
            i for i, ln in enumerate(lines) if ln.lstrip().startswith("❯ 1.")
        )
        box_col = lines[opt1_idx].index("┌")
        # An indented labelled row with panel art ABOVE the first option.
        stray = "   stray fragment".ljust(box_col) + "│ x │"
        lines.insert(opt1_idx, stray)
        form = parse_ask_user_question("\n".join(lines))
        if form is not None:
            assert form._meta.get("layout") != "preview"


# ── W1.4 — panel-content exclusion ─────────────────────────────────────────


class TestPanelExclusion:
    @pytest.mark.parametrize(
        ("c1_base", "c2_base"),
        [
            (
                "auq_preview_wraplabels_cursor1_v2.1.207",
                "auq_preview_wraplabels_cursor2_v2.1.207",
            ),
            (
                "auq_preview_singleselect_v2.1.207",
                "auq_preview_singleselect_cursor2_v2.1.207",
            ),
        ],
    )
    def test_fingerprint_identical_and_only_cursor_differs(
        self, c1_base: str, c2_base: str
    ):
        c1 = parse_ask_user_question(
            _load(f"{c1_base}.txt"), ansi_text=_load(f"{c1_base}.ansi.txt")
        )
        c2 = parse_ask_user_question(
            _load(f"{c2_base}.txt"), ansi_text=_load(f"{c2_base}.ansi.txt")
        )
        assert c1 is not None and c2 is not None
        # Panel content SWITCHES with the cursor, but the cursor-blind form
        # fingerprint is IDENTICAL — panel text never reached the form.
        assert c1.fingerprint() == c2.fingerprint()
        # The render-determining per-option info differs ONLY in the cursor bit.
        labels1 = [(o.number, o.label) for o in c1.options]
        labels2 = [(o.number, o.label) for o in c2.options]
        assert labels1 == labels2
        cur1 = [o.number for o in c1.options if o.cursor]
        cur2 = [o.number for o in c2.options if o.cursor]
        assert cur1 != cur2
        # Codex P2-1 — the exclusion holds on the FULL form: the excerpt is
        # built from the TRUNCATED label-column rows, so it carries NO panel
        # content and is IDENTICAL across the cursor pair (panel churn +
        # the moving chevron are both normalized out).
        for excerpt in (c1.pane_excerpt, c2.pane_excerpt):
            for panel_text in (
                "RUN: rig-test",  # singleselect cursor-1 panel body
                "CPU  MEM",  # wraplabels panel body
                "card A",  # multiquestion-style panel body (defensive)
                "┌",
                "│",
                "└",
            ):
                assert panel_text not in excerpt
        assert c1.pane_excerpt == c2.pane_excerpt


# ── F6 — no cursor-1 synthesis on a preview form ───────────────────────────


class TestNoSynthesis:
    def test_resolve_never_synthesizes_cursor_on_unproven_preview(self):
        """A wrapping preview pane with NO ANSI (tier-1 glyph absent, tier-2
        unavailable) has no proven cursor — ``resolve_ask_form`` with a side
        file must NOT graft cursor=option-1."""
        import json

        side = json.loads(_load("auq_preview_side_file.json"))
        tool_input = side["tool_input"]
        # wraplabels has NO chevron; without ANSI, no cursor is proven.
        pane = _load("auq_preview_wraplabels_v2.1.207.txt")
        pane_only = parse_ask_user_question(pane)  # no ansi
        assert pane_only is not None
        assert [o.number for o in pane_only.options if o.cursor] == []
        form = resolve_ask_form(tool_input, pane)  # no ansi
        assert form is not None
        assert [o.number for o in form.options if o.cursor] == []


# ── W1.2 — the shared authority-aware label matcher ────────────────────────


class TestLabelMatchesAuthority:
    def test_exact_normalized_leg(self):
        assert label_matches_authority(
            "Native side-panel chat", "", "Native side-panel chat"
        )
        assert not label_matches_authority("Native chat", "", "Web app is the cockpit")

    def test_word_boundary_wrap_matches_via_join(self):
        # 2.1.197: fragments space-joined == authority.
        assert label_matches_authority(
            "Embed web chat in side panel",
            "Embed web chat in sidepanel",
            "Embed web chat in side panel",
        )

    def test_hard_mid_word_wrap_matches_via_wrap_canonical(self):
        authority = (
            "Supercalifragilisticexpialidociousantidisestablishmentarianism dashboard"
        )
        pane_label = (
            "Supercalifragilisticexpial idociousantidisestablishmen tarianism dashboard"
        )
        wrap_canonical = (
            "Supercalifragilisticexpialidociousantidisestablishmentarianism dashboard"
        )
        assert label_matches_authority(pane_label, wrap_canonical, authority)

    def test_empty_wrap_canonical_does_not_widen_ordinary(self):
        # A non-preview option (wrap_canonical == "") reduces to leg A only —
        # a nospace collision must NOT match without a wrap_canonical.
        assert not label_matches_authority("a b", "", "ab")
        assert label_matches_authority("a b", "ab", "ab")  # preview: leg B fires


# ── ANSI threading is additive (default None == today) ─────────────────────


class TestAnsiThreadingIsAdditive:
    def test_ordinary_picker_ansi_none_is_default(self):
        pane = _load("auq_single_picker_v2.1.207.txt")
        assert parse_ask_user_question(pane) == parse_ask_user_question(
            pane, ansi_text=None
        )
