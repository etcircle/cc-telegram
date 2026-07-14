"""GH #52 — FOOTERLESS Decision detection (the CC 2.1.207 ``Switch model?`` shape).

The ``Switch model?`` confirmation renders with NO footer — it ends at its last
option under a full-width ``▔`` modal rule — so the footered-only
``parse_generic_decision`` was blind to it (no card, no ``WAITING_ON_USER``
promotion, typing stayed on while Claude was blocked). This exercises the additive
FOOTERLESS leg + the strict-footerless preflight in ``extract_interactive_content``.

Rig provenance (CC 2.1.207, ``temp/rig-20260711-textprompt-gaps/``): the POSITIVE
fixtures are promoted from ``gap3-03`` (canonical clean) + ``gap3-07`` (scrollback
noise above). ``gap3-02`` / ``gap3-04`` / ``gap3-05`` are byte-identical duplicates
of ``gap3-03`` and are NOT promoted. The NEGATIVES are ``gap3-01`` (intact ``/model``
picker) + ``gap3-06`` (input box restored post-commit).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cctelegram import terminal_parser as tp
from cctelegram.terminal_parser import (
    DECISION_VARIANT_FOOTERED,
    DECISION_VARIANT_FOOTERLESS,
    AskOption,
    AskUserQuestionForm,
    decision_prompt_fingerprint,
    decision_variant_of,
    extract_interactive_content,
    footerless_decision_reject_reason,
    has_decision_residue,
    parse_generic_decision,
    parse_unknown_blocking_prompt,
    set_decision_cards_enabled,
    set_permission_prompts_enabled,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_MODAL_RULE = "▔" * 80  # a full-width ▔ modal rule


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


_POS_CLEAN = "decision_footerless_switchmodel_v2.1.207.txt"
_POS_SCROLLBACK = "decision_footerless_switchmodel_scrollback_v2.1.207.txt"
_NEG_MODEL_PICKER = "decision_footerless_neg_model_picker_v2.1.207.txt"
_NEG_INPUTBOX = "decision_footerless_neg_inputbox_restored_v2.1.207.txt"
# A pre-existing footerless capture already in the corpus — a third real positive.
_POS_EXISTING = "switch_model_live_v2.1.207.txt"


@pytest.fixture
def decision_on():
    set_decision_cards_enabled(True)
    yield
    set_decision_cards_enabled(False)


@pytest.fixture
def both_gates_on():
    set_permission_prompts_enabled(True)
    set_decision_cards_enabled(True)
    yield
    set_permission_prompts_enabled(False)
    set_decision_cards_enabled(False)


def _synthetic_footerless(
    *, anchor: str = "", body: str = "some body line", title: str = "Switch model?"
) -> str:
    """An otherwise-POSITIVE footerless frame; inject ``anchor`` into the pre-option
    title/body region for the redraw matrix."""
    lines = [_MODAL_RULE, f"   {title}"]
    if anchor:
        lines.append(f"   {anchor}")
    lines.append(f"   {body}")
    lines.append("")
    lines.append("   ❯ 1. Yes, do it")
    lines.append("     2. No, cancel")
    return "\n".join(lines) + "\n"


# ── Parser positives ──────────────────────────────────────────────────────


@pytest.mark.parametrize("fixture", [_POS_CLEAN, _POS_SCROLLBACK, _POS_EXISTING])
def test_footerless_positive_parses(fixture: str) -> None:
    form = parse_generic_decision(_load(fixture))
    assert form is not None
    assert form.current_question_title == "Switch model?"
    assert len(form.options) == 2
    assert [o.number for o in form.options] == [1, 2]
    assert [o.cursor for o in form.options] == [True, False]
    assert form.options_complete is True
    assert form.select_mode == "single"
    assert form.is_review_screen is False
    assert decision_variant_of(form) == DECISION_VARIANT_FOOTERLESS
    # Excerpt spans title → last option (the ▔ rule is excluded).
    assert form.pane_excerpt.splitlines()[0].strip() == "Switch model?"
    assert form.pane_excerpt.strip().endswith("2. No, go back")
    assert "▔" not in form.pane_excerpt


@pytest.mark.parametrize("fixture", [_POS_CLEAN, _POS_SCROLLBACK, _POS_EXISTING])
def test_footerless_positive_extract_detects(decision_on, fixture: str) -> None:
    content = extract_interactive_content(_load(fixture))
    assert content is not None
    assert content.name == "Decision"


def test_footerless_flag_off_no_preflight_detection() -> None:
    # Flag OFF (default via reset): the preflight does not run and Decision is
    # filtered from the walk — no detection at all.
    set_decision_cards_enabled(False)
    assert extract_interactive_content(_load(_POS_CLEAN)) is None


# ── Parser negatives ──────────────────────────────────────────────────────


def test_intact_model_picker_is_not_footerless() -> None:
    # The /model picker is ▔-ruled + titled but has content BELOW its options
    # (effort line + footer), so the terminal-block / blank-below requirement
    # fails — and it is a NAMED Settings surface.
    assert parse_generic_decision(_load(_NEG_MODEL_PICKER)) is None
    assert footerless_decision_reject_reason(_load(_NEG_MODEL_PICKER)) is not None


def test_intact_model_picker_stays_settings(both_gates_on) -> None:
    content = extract_interactive_content(_load(_NEG_MODEL_PICKER))
    assert content is not None
    assert content.name == "Settings"


def test_inputbox_restored_is_not_a_decision(decision_on) -> None:
    # Post-commit: the input box + status bar are restored below → no terminal
    # numbered block, no card.
    assert parse_generic_decision(_load(_NEG_INPUTBOX)) is None
    assert extract_interactive_content(_load(_NEG_INPUTBOX)) is None


# ── Adversarials ──────────────────────────────────────────────────────────


def test_no_modal_rule_refuses() -> None:
    frame = "   Switch model?\n   body\n\n   ❯ 1. Yes\n     2. No\n"
    # No ▔ rule anywhere → the title walk reaches BOF (title at line 0, no room for
    # a rule above) so it refuses ``no_title``; a rule elsewhere would refuse
    # ``not_rule_adjacent``. Either way the footerless parse REFUSES.
    assert footerless_decision_reject_reason(frame) in ("no_title", "not_rule_adjacent")
    assert parse_generic_decision(frame) is None


def test_rule_separated_from_title_by_blank_refuses() -> None:
    frame = f"{_MODAL_RULE}\n\n   Switch model?\n\n   ❯ 1. Yes\n     2. No\n"
    assert footerless_decision_reject_reason(frame) == "not_rule_adjacent"


def test_prose_directly_under_the_rule_becomes_the_resolved_title_DISCLOSED() -> None:
    """KNOWN-ACCEPTED disclosed residual (r2 review, ARGUED — not a silent pass).

    A prose line inserted between the ▔ rule and the heading is byte-structurally
    INDISTINGUISHABLE from the genuine title+subtitle modal: the REAL gap3-03
    renders TWO contiguous meaningful lines directly under its rule ("Switch
    model?" + the subtitle prose), so any content-blind predicate that refuses
    "prose directly under the rule" refuses the genuine modal class too — proven
    below by deleting gap3-03's title line, which makes its SUBTITLE (prose) the
    rule-adjacent resolved title and still parses. The prescribed check ("the
    line immediately below the rule must BE the resolved title") is already the
    shipped gate (``_is_modal_rule(lines[title_idx - 1])`` on the walk-up top).
    Consequence bound: a wrong TITLE STRING on a display-only card (options +
    excerpt correct; the footerless variant never dispatches — the GH #52
    footered-only fence). The BLANK-separation half of the plan's negative DOES
    refuse (``test_rule_separated_from_title_by_blank_refuses``)."""
    frame = (
        f"{_MODAL_RULE}\n   prose line\n   Switch model?\n\n   ❯ 1. Yes\n     2. No\n"
    )
    form = parse_generic_decision(frame)
    assert form is not None
    assert form.current_question_title == "prose line"

    # The structural-indistinguishability proof: the REAL fixture minus its title
    # line puts its subtitle PROSE directly under the rule — the same shape, and
    # it MUST keep parsing (refusing it would refuse the whole multi-line modal
    # class the positive fixtures pin).
    real = _load(_POS_CLEAN)
    mutated = "\n".join(
        line for line in real.split("\n") if line.strip() != "Switch model?"
    )
    mform = parse_generic_decision(mutated)
    assert mform is not None
    assert mform.current_question_title == (
        "Your next response will be slower and use more tokens"
    )


def test_title_less_block_refuses() -> None:
    # No prompt content above the options at all → title None → refuse.
    frame = f"{_MODAL_RULE}\n\n   ❯ 1. Yes\n     2. No\n"
    assert parse_generic_decision(frame) is None
    assert footerless_decision_reject_reason(frame) in ("no_title", "not_rule_adjacent")


def test_options_starting_at_two_refuses() -> None:
    frame = f"{_MODAL_RULE}\n   Title\n\n   ❯ 2. Yes\n     3. No\n"
    assert footerless_decision_reject_reason(frame) == "no_terminal_block"


def test_cursor_missing_refuses() -> None:
    frame = f"{_MODAL_RULE}\n   Title\n\n     1. Yes\n     2. No\n"
    assert footerless_decision_reject_reason(frame) == "no_cursor"


def test_prompt_quoted_above_live_input_box_refuses() -> None:
    # The footerless block is NOT the terminal content (an input box follows) →
    # blank-below / terminal-block fails.
    frame = (
        f"{_MODAL_RULE}\n   Switch model?\n\n   ❯ 1. Yes\n     2. No\n\n"
        "─────\n❯ \n─────\n"
        "  ? for shortcuts\n"
    )
    assert parse_generic_decision(frame) is None
    assert footerless_decision_reject_reason(frame) == "no_terminal_block"


def test_malformed_footer_between_title_and_options_refuses() -> None:
    # A strict footer sitting BETWEEN the title and the terminal options is a
    # malformed footered frame, not a footerless prompt → attached-footer veto.
    frame = (
        f"{_MODAL_RULE}\n   Switch model?\n"
        "   Enter to confirm · Esc to cancel\n"
        "   ❯ 1. Yes\n     2. No\n"
    )
    assert footerless_decision_reject_reason(frame) == "veto_attached_footer"
    assert parse_generic_decision(frame) is None


def test_settings_footer_between_title_and_options_refuses() -> None:
    # The exact Settings footer row (single-key hint segment) must be recognized by
    # the attached-footer veto's hint-row recognizer.
    frame = (
        f"{_MODAL_RULE}\n   Switch model?\n"
        "   Enter to set as default · s to use this session only "
        "· Esc to cancel\n"
        "   ❯ 1. Yes\n     2. No\n"
    )
    assert footerless_decision_reject_reason(frame) == "veto_attached_footer"


def test_auq_select_footer_between_title_and_options_refuses() -> None:
    frame = (
        f"{_MODAL_RULE}\n   Switch model?\n"
        "   Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        "   ❯ 1. Yes\n     2. No\n"
    )
    assert footerless_decision_reject_reason(frame) == "veto_attached_footer"


def test_stale_answered_folder_trust_above_live_footerless_detects(decision_on) -> None:
    # A STALE answered folder-trust (footered Decision) high in scrollback, above a
    # LIVE footerless Switch model? — the footerless leg must still detect (P1-3).
    stale = (
        "─── answered folder-trust ───\n"
        "   Accessing workspace:\n   ❯ 1. Yes, I trust this folder\n"
        "     2. No, exit\n Enter to confirm · Esc to cancel\n"
    )
    composite = stale + "\n\n" + _load(_POS_CLEAN)
    content = extract_interactive_content(composite)
    assert content is not None
    assert content.name == "Decision"
    assert decision_variant_of(parse_generic_decision(composite)) == (
        DECISION_VARIANT_FOOTERLESS
    )


# ── The r3 P1 preflight composites: stale VALIDATOR-LESS named surface above a
# live footerless modal (r2 review P2-4 — the r3 pins mandated ALL THREE loose
# validator-less families, not only the stale FOOTERED Decision above) ─────────

# Each stale surface loose-matches its NAMED pattern (no strict validator), so
# WITHOUT the preflight first-match-wins would return it and
# `parse_generic_decision` would never run. Vacuity guard: each must extract as
# its OWN name when it is the pane's terminal content.
_STALE_AUQ = "  1. Alpha\n  2. Beta\n Enter to select · Esc to cancel\n"
_STALE_EPM = " Would you like to proceed?\n\n ❯ 1. Yes\n   2. No\n Esc to cancel\n"
_STALE_SETTINGS = (
    " Select model\n\n ❯ 1. Default\n   2. Opus\n Enter to confirm · Esc to cancel\n"
)


@pytest.mark.parametrize(
    "stale,own_name",
    [
        (_STALE_AUQ, "AskUserQuestion"),
        (_STALE_EPM, "ExitPlanMode"),
        (_STALE_SETTINGS, "Settings"),
    ],
    ids=["stale-auq", "stale-epm", "stale-settings"],
)
def test_stale_named_surface_above_live_footerless_decision_wins(
    decision_on, stale: str, own_name: str
) -> None:
    # Vacuity guard: the stale surface ALONE is a genuine loose match of its
    # named pattern (else the composite proves nothing about the preflight).
    alone = extract_interactive_content(stale)
    assert alone is not None and alone.name == own_name

    composite = stale + "\n\n" + _load(_POS_CLEAN)
    content = extract_interactive_content(composite)
    assert content is not None
    assert content.name == "Decision"
    assert decision_variant_of(parse_generic_decision(composite)) == (
        DECISION_VARIANT_FOOTERLESS
    )


def test_live_auq_with_stale_modal_chrome_above_stays_auq(decision_on) -> None:
    # The INVERSE composite: a LIVE AUQ owns the pane bottom; stale ▔ modal
    # chrome sits above it in scrollback. The footerless preflight must refuse
    # (the AUQ footer is the last non-blank line — no terminal numbered block)
    # and the named walk returns AskUserQuestion.
    stale_chrome = (
        f"{_MODAL_RULE}\n   old modal title\n   old body\n\n"
        "   ❯ 1. Old one\n     2. Old two\n"
    )
    live_auq = "  1. Alpha\n  2. Beta\n Enter to select · Esc to cancel\n"
    composite = stale_chrome + "\n\n" + live_auq
    content = extract_interactive_content(composite)
    assert content is not None
    assert content.name == "AskUserQuestion"


def test_stale_modal_rule_high_with_unrelated_bottom_list_refuses() -> None:
    # Stale ▔ chrome high in scrollback + an unrelated numbered list at pane bottom,
    # separated by a ≥2-blank CLEAN TERMINATOR gap (the rule is NOT attached to a
    # title above THAT block) → refuse. The prompt-block walk stops at the gap, so
    # the resolved title's neighbour is a blank, not the rule.
    frame = (
        f"{_MODAL_RULE}\n   some old modal\n\n\n"
        "lots of unrelated output\nmore output\n\n\n"
        "shopping list:\n   ❯ 1. milk\n     2. eggs\n"
    )
    assert footerless_decision_reject_reason(frame) == "not_rule_adjacent"


def test_literal_model_prose_pane_bottom_is_accepted_residual() -> None:
    # ACCEPTED RESIDUAL (pinned as KNOWN-accepted): a ▔ row + titled cursored
    # numbered list rendered as literal model-prose pane bottom footerless-parses.
    # Fail-dark by design — a real running pane would show the input box below.
    frame = _synthetic_footerless(anchor="", body="here are your choices")
    assert footerless_decision_reject_reason(frame) is None


# ── Redraw matrix — every VETOED classification anchor (r4 P2-2 / r5 P2) ────

# The validator-less patterns' DISTINCTIVE anchors: a dropped-footer redraw of each
# named surface, ▔-adjacent, must refuse SPECIFICALLY through its classification
# entry (assert the refusing veto entry, not just None).
# One sample line per VETOED classification entry (r5 P2 fold, exact-reason):
# each frame must refuse SPECIFICALLY through its own classification entry —
# never merely "some veto*" — so the matrix exercises the veto WIRING, not just
# rule-adjacency failure. The classified anchors run BEFORE the verb-agnostic
# fallback (r2 fold), so permission:0's whitelisted-verb sample reports ITS entry.
_VETOED_ANCHOR_SAMPLES: dict[str, str] = {
    "epm:0": "Would you like to proceed?",
    "epm:1": "Claude has written up a plan for you",
    "auq-multi:0": "←  ☐ A  ☒ B →",
    "auq-single:0": "☐ pick one",
    "restore-checkpoint:0": "Restore the code to this checkpoint?",
    "settings:0": "Settings: press tab to cycle",
    "settings:1": "Select model",
    "settings:2": "Settings Warning found",
    "permission:0": "Do you want to allow this?",
    "permission:1": "Claude wants to run a command",
    "workflow:0": "Run a dynamic workflow?",
    "workflow:1": "This dynamic workflow will do things",
    "workflow:2": "Dynamic workflows can use tools",
}


def test_redraw_matrix_covers_every_vetoed_classification_entry() -> None:
    """The sample map is EXHAUSTIVE over the VETOED classification entries
    (set-equality — a future vetoed anchor without a redraw-matrix sample fails
    here instead of silently escaping the matrix)."""
    vetoed = {
        f"{id_}:{idx}"
        for (id_, idx, _p, _f), cls in tp._DECISION_ANCHOR_CLASSIFICATION.items()
        if cls == tp._VETOED
    }
    assert set(_VETOED_ANCHOR_SAMPLES) == vetoed


@pytest.mark.parametrize("key,anchor", sorted(_VETOED_ANCHOR_SAMPLES.items()))
def test_footer_dropped_redraw_refuses_via_its_veto_entry(
    key: str, anchor: str
) -> None:
    frame = _synthetic_footerless(anchor=anchor)
    assert footerless_decision_reject_reason(frame) == f"veto_anchor:{key}"


def test_novel_verb_permission_shape_refuses_via_verb_agnostic_fallback() -> None:
    """A verb OUTSIDE ``parse_permission_prompt``'s whitelist ("open") matches no
    classified anchor, so the verb-AGNOSTIC ``Do you want to…?`` veto is the
    fallback that refuses it (the r2 reorder keeps classified anchors FIRST)."""
    frame = _synthetic_footerless(anchor="Do you want to open the file?")
    assert footerless_decision_reject_reason(frame) == "veto_verb_agnostic"


def test_plain_numbered_auq_anchor_is_the_disclosed_dropped_footer_residual() -> None:
    # The plain-numbered AUQ anchor is classified ``excluded(generic-option-shape)``
    # (it is Decision's own option shape). Its footer-dropped ▔-adjacent frame DOES
    # footerless-parse — the OPPOSITE pin (a KNOWN-accepted disclosed residual: an
    # answered plain AUQ whose footer was dropped is indistinguishable on the pane
    # from a footerless decision; fail-dark, display-only, self-heals next frame).
    frame = f"{_MODAL_RULE}\n   Pick a color\n\n   ❯ 1. Red\n     2. Blue\n"
    assert footerless_decision_reject_reason(frame) is None
    assert decision_variant_of(parse_generic_decision(frame)) == (
        DECISION_VARIANT_FOOTERLESS
    )


# ── The veto-classification tie test (each anchor classified exactly once) ──


def test_every_top_anchor_classified_exactly_once() -> None:
    seen: dict[tuple[str, int, str, int], str] = {}
    for pattern in tp.UI_PATTERNS:
        for idx, rx in enumerate(pattern.top):
            key = (pattern.id, idx, rx.pattern, rx.flags)
            cls = tp._DECISION_ANCHOR_CLASSIFICATION.get(key)
            assert cls is not None, f"top anchor {key} is UNCLASSIFIED (regex drift?)"
            assert cls == tp._VETOED or cls.startswith("excluded("), cls
            seen[key] = cls
    # No stale entries in the map (every classified key still exists as a live
    # anchor) — a removed/edited anchor must not leave dead classification rows.
    assert set(tp._DECISION_ANCHOR_CLASSIFICATION) == set(seen)


def test_veto_anchor_set_matches_vetoed_classification() -> None:
    veto_keys = {k for k, _ in tp._footerless_veto_anchors()}
    expected = {
        f"{id_}:{idx}"
        for (id_, idx, _p, _f), cls in tp._DECISION_ANCHOR_CLASSIFICATION.items()
        if cls == tp._VETOED
    }
    assert veto_keys == expected
    # Plain-AUQ + Decision's own numbered shapes are EXCLUDED, not vetoed.
    assert "auq-plain:0" not in veto_keys
    assert "decision:0" not in veto_keys


def test_unique_pattern_ids() -> None:
    ids = [p.id for p in tp.UI_PATTERNS]
    assert len(ids) == len(set(ids)), f"duplicate UIPattern ids: {ids}"
    assert all(ids), "every UIPattern must carry a non-empty id"


# ── Full-corpus non-regression sweep (baked baseline; GH #56 precedent) ─────

# Fixtures whose ``extract_interactive_content`` NAME flips because of GH #52: the
# two new footerless positives + a pre-existing footerless capture that was
# previously undetected. Everything else must be byte-identical.
_GH52_EXPECTED_FLIPS: dict[str, str] = {
    _POS_CLEAN: "Decision",
    _POS_SCROLLBACK: "Decision",
    _POS_EXISTING: "Decision",
}


def test_full_corpus_names_unchanged_except_footerless_positives(both_gates_on) -> None:
    baseline = json.loads(
        (_FIXTURES / "decision_footerless_corpus_baseline.json").read_text()
    )
    for name, expected in sorted(baseline.items()):
        content = extract_interactive_content(_load(name))
        got = None if content is None else content.name
        assert got == expected, f"{name}: expected {expected!r}, got {got!r}"
    # The baked baseline already encodes the flips — assert they are present so the
    # baseline can never silently drop a footerless positive.
    for name, expected in _GH52_EXPECTED_FLIPS.items():
        assert baseline[name] == expected


def test_the_baked_baseline_covers_the_whole_fixture_directory(both_gates_on) -> None:
    """SET EQUALITY between the fixture-directory listing and the baked JSON
    (r2 review P3, the GH #56 input-box-baseline pattern): a future fixture
    landing in the directory without a baked classification fails HERE instead
    of silently escaping the corpus sweep."""
    baseline = json.loads(
        (_FIXTURES / "decision_footerless_corpus_baseline.json").read_text()
    )
    on_disk = {p.name for p in _FIXTURES.glob("*.txt")}
    assert on_disk == set(baseline)


# ── Fingerprint stability + variant separation ─────────────────────────────


def _move_cursor_to_second(pane: str) -> str:
    return pane.replace("❯ 1. Yes", "  1. Yes").replace("  2. No", "❯ 2. No")


def test_footerless_fingerprint_stable_across_cursor_move() -> None:
    p1 = _load(_POS_CLEAN)
    f1 = parse_generic_decision(p1)
    f2 = parse_generic_decision(_move_cursor_to_second(p1))
    assert f1 is not None and f2 is not None
    assert [o.cursor for o in f1.options] != [o.cursor for o in f2.options]
    assert decision_prompt_fingerprint(f1) == decision_prompt_fingerprint(f2)


def test_footerless_fingerprint_stable_across_scrollback_churn() -> None:
    base = _load(_POS_CLEAN)
    churned = "extra scrollback line\nanother one\n" + base
    f1 = parse_generic_decision(base)
    f2 = parse_generic_decision(churned)
    assert f1 is not None and f2 is not None
    assert decision_prompt_fingerprint(f1) == decision_prompt_fingerprint(f2)


def test_footered_vs_footerless_identical_text_differ() -> None:
    opts = (
        AskOption(label="A", recommended=False, cursor=True, number=1),
        AskOption(label="B", recommended=False, cursor=False, number=2),
    )
    footerless = AskUserQuestionForm(
        current_question_title="X",
        options=opts,
        pane_excerpt="X\n❯ 1. A\n  2. B",
        select_mode="single",
        _meta={"decision_variant": DECISION_VARIANT_FOOTERLESS},
    )
    footered = AskUserQuestionForm(
        current_question_title="X",
        options=opts,
        pane_excerpt="X\n❯ 1. A\n  2. B",
        select_mode="single",
        _meta={"decision_variant": DECISION_VARIANT_FOOTERED},
    )
    assert decision_prompt_fingerprint(footerless) != decision_prompt_fingerprint(
        footered
    )


# ── parse_unknown_blocking_prompt interplay (GH #47-R1) ────────────────────


def test_footerless_pane_no_longer_taken_by_unknown_blocking(decision_on) -> None:
    # The gap3-03 pane now takes the NAMED Decision path, so
    # parse_unknown_blocking_prompt's guard (requirement 1: extract returns None)
    # EXCLUDES it — no tombstone-avoider excerpt.
    pane = _load(_POS_CLEAN)
    assert extract_interactive_content(pane) is not None
    assert parse_unknown_blocking_prompt(pane) is None


def test_footerless_pane_flag_off_falls_to_unknown_blocking() -> None:
    # Flag OFF: extract returns None, so the unknown-blocking fallback DOES fire (a
    # tombstone-avoider excerpt) — the pre-GH#52 behavior.
    set_decision_cards_enabled(False)
    pane = _load(_POS_CLEAN)
    assert extract_interactive_content(pane) is None
    excerpt = parse_unknown_blocking_prompt(pane)
    assert excerpt is not None
    assert "Switch model?" in excerpt


# ── Confirm-side residue predicate ─────────────────────────────────────────


def test_residue_true_on_footerless_pane() -> None:
    assert has_decision_residue(_load(_POS_CLEAN)) is True


def test_residue_true_on_footered_folder_trust_minus_footer() -> None:
    ft = _load("folder_trust_arrival_plain_v2.1.207.txt")
    ft_nofooter = "\n".join(
        line for line in ft.split("\n") if "Enter to confirm" not in line
    )
    # The footerless parser refuses (─ rule, not ▔), but the still-standing option
    # block IS residue.
    assert parse_generic_decision(ft_nofooter) is None
    assert has_decision_residue(ft_nofooter) is True


def test_residue_false_on_restored_input_box() -> None:
    assert has_decision_residue(_load(_NEG_INPUTBOX)) is False


def test_residue_false_on_empty_pane() -> None:
    assert has_decision_residue("") is False
    assert has_decision_residue("   \n  \n") is False


# ── Loose anchor widening + mixed-anchor _try_extract (P2-2) ───────────────


def test_decision_bottom_anchor_accepts_numbered_row() -> None:
    # The widened loose bottom anchor matches an any-numbered-option row.
    assert re.compile(tp._RE_DECISION_BOTTOM_OPTION).search("  2. No, go back")
    assert not tp._RE_DECISION_BOTTOM_OPTION.search("  just prose")


def test_mixed_stale_footer_and_live_option_block(decision_on) -> None:
    # A stale footered Decision footer high in scrollback + a live footerless option
    # block at the bottom: the footerless leg wins via the preflight.
    stale = (
        "   Old confirm\n   ❯ 1. Alpha\n     2. Beta\n"
        " Enter to confirm · Esc to cancel\n"
    )
    composite = stale + "\n\n" + _load(_POS_CLEAN)
    content = extract_interactive_content(composite)
    assert content is not None and content.name == "Decision"
    form = parse_generic_decision(composite)
    assert form is not None and form.current_question_title == "Switch model?"


# ── status_polling scenario: card + WAITING promotion + flag-OFF nothing ────


@pytest.mark.usefixtures("fresh_handler_state")
class TestFooterlessRouteStatePromotion:
    """A LIVE footerless Decision pane driven through the real
    ``update_status_message`` seam promotes RUNNING → WAITING_ON_USER (typing
    off); flag OFF promotes nothing. The renderer is MOCKED here (the B1
    promotion-test precedent); the UNMOCKED render/count + one-card/tombstone
    interplay is pinned by the scenario
    ``tests/scenarios/test_decision_footerless_card.py`` (r2 review P2-4)."""

    @pytest.fixture
    def mock_bot(self):
        from unittest.mock import AsyncMock, MagicMock

        bot = AsyncMock()
        sent = MagicMock()
        sent.message_id = 4242
        bot.send_message.return_value = sent
        return bot

    @staticmethod
    async def _seed_running(route) -> None:
        from cctelegram import route_runtime
        from cctelegram.route_runtime import RunState, TranscriptLifecycleEvent

        await route_runtime.ingest_transcript_event(
            route,
            TranscriptLifecycleEvent(
                role="assistant",
                block_type="text",
                tool_use_id=None,
                tool_name=None,
                stop_reason=None,
            ),
        )
        assert route_runtime.snapshot(route).run_state is RunState.RUNNING

    @pytest.mark.asyncio
    async def test_footerless_pane_promotes_running_to_waiting(self, mock_bot) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling
        from cctelegram.route_runtime import RunState

        set_decision_cards_enabled(True)
        window_id = "@31"
        user_id, thread_id = 1, 42
        route = (user_id, thread_id, window_id)
        await self._seed_running(route)

        mock_window = MagicMock()
        mock_window.window_id = window_id
        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling,
                "handle_interactive_ui",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_handle_ui,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
            patch.object(
                status_polling,
                "_drain_content_queue_before_first_picker_publish",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_load(_POS_CLEAN))
            await status_polling.update_status_message(
                mock_bot, user_id=user_id, window_id=window_id, thread_id=thread_id
            )

        mock_handle_ui.assert_awaited_once()
        assert route_runtime.snapshot(route).run_state is RunState.WAITING_ON_USER

    @pytest.mark.asyncio
    async def test_flag_off_footerless_pane_does_not_promote(self, mock_bot) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling
        from cctelegram.route_runtime import RunState

        set_decision_cards_enabled(False)
        window_id = "@32"
        user_id, thread_id = 1, 42
        route = (user_id, thread_id, window_id)
        await self._seed_running(route)

        mock_window = MagicMock()
        mock_window.window_id = window_id
        with (
            patch.object(status_polling, "tmux_manager") as mock_tmux,
            patch.object(
                status_polling, "handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
            patch.object(
                status_polling, "enqueue_status_update", new_callable=AsyncMock
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=_load(_POS_CLEAN))
            await status_polling.update_status_message(
                mock_bot, user_id=user_id, window_id=window_id, thread_id=thread_id
            )

        mock_handle_ui.assert_not_called()
        assert route_runtime.snapshot(route).run_state is RunState.RUNNING
