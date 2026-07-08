"""Stage B2.2 — ``terminal_parser.decision_prompt_fingerprint`` (§3b).

The body-inclusive Decision identity canonical, assembled from STRUCTURED parse
fields (never regex-stripped raw text). Drives the REAL ``parse_generic_decision``
against the committed folder-trust shape fixture + constructed panes for the
round-2 collision classes:

  (a) two cross-directory trust prompts (same family, different workspace path in
      the body) → DIFFERENT fingerprints;
  (b) two bodies differing ONLY by a literal ❯ / ☑ / [x] embedded in a path →
      DIFFERENT fingerprints (the round-2 collision class — NO glyph stripping);
  (c) cursor movement across option rows does NOT rotate the fingerprint;
  (d) the ``decision:`` domain prefix ⇒ Decision fp8 ≠ AUQ fp8 for an IDENTICAL
      title + option set (cross-lane ledger-key collision impossible §8);
  (e) title=None (bound-overflow / no prompt block) fingerprints stably and
      differs from a titled twin.
"""

from __future__ import annotations

from pathlib import Path

from cctelegram.terminal_parser import (
    AskOption,
    AskUserQuestionForm,
    decision_prompt_fingerprint,
    parse_generic_decision,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_TRUST = "decision_trust_folder_v2.1.200.txt"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


# A generic titled Decision pane the strict parser accepts (title + body +
# options + confirmation footer), parameterized on the body path.
def _decision_pane(path_line: str) -> str:
    return (
        "────────────────────────────────────────────────────────────\n"
        " Accessing workspace:\n"
        f"{path_line}\n"
        " Quick safety check: is this a project you trust?\n"
        " ❯ 1. Yes, I trust this folder\n"
        "   2. No, exit\n"
        " Enter to confirm · Esc to cancel\n"
    )


# ── (a) cross-directory bodies differ ────────────────────────────────────────


def test_cross_directory_bodies_produce_different_fingerprints() -> None:
    a = parse_generic_decision(_decision_pane(" /private/tmp/project-a"))
    b = parse_generic_decision(_decision_pane(" /private/tmp/project-b"))
    assert a is not None and b is not None
    # Same family, same title + option labels — ONLY the body path differs.
    assert a.current_question_title == b.current_question_title
    assert [o.label for o in a.options] == [o.label for o in b.options]
    assert decision_prompt_fingerprint(a) != decision_prompt_fingerprint(b)


def test_real_trust_fixture_vs_cross_directory_twin_differ() -> None:
    base = parse_generic_decision(_load(_TRUST))
    twin = parse_generic_decision(
        _load(_TRUST).replace("/private/tmp/cc-scratch-b1/proj", "/home/other/repo")
    )
    assert base is not None and twin is not None
    assert decision_prompt_fingerprint(base) != decision_prompt_fingerprint(twin)


# ── (b) path-embedded cursor/checkbox glyphs are NOT stripped ────────────────


def test_literal_glyph_in_body_path_is_not_stripped() -> None:
    plain = parse_generic_decision(_decision_pane(" /private/tmp/proj"))
    assert plain is not None
    base_fp = decision_prompt_fingerprint(plain)
    for glyph in ("❯", "☑", "[x]"):
        variant = parse_generic_decision(_decision_pane(f" /private/tmp/{glyph}proj"))
        assert variant is not None
        assert decision_prompt_fingerprint(variant) != base_fp, glyph


# ── (c) cursor movement across option rows does NOT rotate identity ──────────


def test_cursor_movement_does_not_rotate_fingerprint() -> None:
    on_first = (
        "────────────────────────────────────────────────────────────\n"
        " Switch model?\n"
        " Your next response will be slower.\n"
        " ❯ 1. Yes, switch\n"
        "   2. No, go back\n"
        " Enter to confirm · Esc to cancel\n"
    )
    on_second = (
        "────────────────────────────────────────────────────────────\n"
        " Switch model?\n"
        " Your next response will be slower.\n"
        "   1. Yes, switch\n"
        " ❯ 2. No, go back\n"
        " Enter to confirm · Esc to cancel\n"
    )
    f1 = parse_generic_decision(on_first)
    f2 = parse_generic_decision(on_second)
    assert f1 is not None and f2 is not None
    # The cursor moved (structurally isolated), so o.cursor differs...
    assert [o.cursor for o in f1.options] != [o.cursor for o in f2.options]
    # ...but the fingerprint (cursor metadata EXCLUDED) is identical.
    assert decision_prompt_fingerprint(f1) == decision_prompt_fingerprint(f2)


# ── (d) domain prefix ⇒ Decision fp8 ≠ AUQ fp8 for identical title+options ───


def test_domain_prefix_disjoins_decision_and_auq_fp8() -> None:
    opts = (
        AskOption(
            label="Yes, I trust this folder", recommended=False, cursor=True, number=1
        ),
        AskOption(label="No, exit", recommended=False, cursor=False, number=2),
    )
    form = AskUserQuestionForm(
        current_question_title="Accessing workspace:",
        options=opts,
        is_review_screen=False,
        is_free_text=False,
        select_mode="single",
    )
    decision_fp = decision_prompt_fingerprint(form)
    auq_fp = form.fingerprint()  # the AUQ lane's bare _canonical_repr digest
    # The 8-char slice is the SHARED auq_action_ledger.jsonl key component — it
    # MUST differ across lanes for the SAME title + option set.
    assert decision_fp != auq_fp
    assert decision_fp[:8] != auq_fp[:8]


# ── (e) title=None fingerprints stably and differs from a titled twin ────────


def test_title_none_fingerprints_stably_and_differs_from_titled() -> None:
    # Options directly under a chrome separator → no prompt block → title None.
    no_title = (
        "────────────────────────────────────────────────────────────\n"
        " ❯ 1. Yes, I trust this folder\n"
        "   2. No, exit\n"
        " Enter to confirm · Esc to cancel\n"
    )
    form = parse_generic_decision(no_title)
    assert form is not None
    assert form.current_question_title is None
    # Stable: identical pane → identical fingerprint (no crash on None title).
    again = parse_generic_decision(no_title)
    assert again is not None
    assert decision_prompt_fingerprint(form) == decision_prompt_fingerprint(again)
    # Differs from a titled twin carrying the SAME option set.
    titled = parse_generic_decision(_decision_pane(" /private/tmp/proj"))
    assert titled is not None
    assert decision_prompt_fingerprint(form) != decision_prompt_fingerprint(titled)
