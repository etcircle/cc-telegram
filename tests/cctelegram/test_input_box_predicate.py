"""GH #50 §1.1 — ``terminal_parser.pane_input_box_present`` against REAL panes.

The delivery gate is POSITIVE structural evidence that Claude Code is at its
ready input box. Every case below is a captured CC 2.1.207 rig pane
(``tests/cctelegram/fixtures/``), so the predicate is fixture-pinned exactly like
``clean_ghost_input_text`` / ``pane_command_is_claude`` — the next TUI-drift audit
re-runs it.

The load-bearing asymmetry: a BUSY pane must still PASS (queueing while Claude
works is a first-class flow), while every blocking surface must FAIL — including
``Switch model?``, which the parser cannot recognize at all (M4). That is why the
gate is a positive proof and not "no known prompt matched".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cctelegram import terminal_parser as tp

FIXTURES = Path(__file__).parent / "fixtures"


def _pane(name: str) -> str:
    return (FIXTURES / name).read_text()


# ── The pane MAY receive text (the gate PASSES) ──────────────────────────

DELIVERABLE = [
    # A0 — the plain idle input box.
    "inputbox_idle_v2.1.207.txt",
    # A2 (design-killer) — QUEUEING WHILE BUSY must keep working. The rule-pair
    # + prompt row + ready chrome persist through every busy shape.
    "inputbox_busy_thinking_v2.1.207.txt",
    "inputbox_busy_tool_v2.1.207.txt",
    # D10 — pre-existing / wrapped / multi-line drafts still deliver
    # (continuation rows carry NO glyph — the reason this is NOT pane_looks_idle).
    "inputbox_draft_typed_v2.1.207.txt",
    "inputbox_wrapped_draft_v2.1.207.txt",
    "inputbox_multiline_draft_v2.1.207.txt",
    # A live background shell (the `· 1 shell` status bar) is not a blocker.
    "inputbox_bgshell_v2.1.207.txt",
    # B5 — the agent task-list FOOTER coexists with the box; Enter still submits.
    "inputbox_tasklist_footer_v2.1.207.txt",
    # §5 finding 4 — BASH mode: the prompt glyph is `!`, not `❯`. A `❯`-only leg
    # would refuse EVERY `!command`.
    "inputbox_bashmode_empty_v2.1.207.txt",
    "inputbox_bashmode_draft_v2.1.207.txt",
    # B6 — a slash command WITH an argument raises no completion overlay.
    "inputbox_slash_with_arg_v2.1.207.txt",
    # Manual-mode chrome (`⏸ manual mode on · ? for shortcuts`).
    "inputbox_manual_mode_v2.1.207.txt",
    # The 2.1.206 ghost-suggestion + real-draft rows (no emptiness leg here).
    "idle_frame_plain_v2.1.206.txt",
    "idle_ghost_input_row_v2.1.206.txt",
    "idle_real_draft_input_row_v2.1.206.txt",
    # THE PASTE-COLLAPSE (the GH #50 PR-1 regression). A large multi-line payload
    # is consumed as a PASTE: CC collapses the row to `❯\xa0[Pasted text #1 +12
    # lines]` and REPLACES the status bar with `paste again to expand`. The box is
    # right there, holding the text, and Enter submits it — but leg 3 saw none of
    # the old chrome markers and the delivery gate's re-verify refused EVERY long
    # / multi-line message (the owner's 809-char voice note).
    "inputbox_paste_collapsed_v2.1.207.txt",
    # ~2s later the status bar REVERTS while the collapsed draft remains.
    "inputbox_paste_collapsed_reverted_v2.1.207.txt",
]


@pytest.mark.parametrize("name", DELIVERABLE)
def test_ready_input_box_panes_pass(name: str) -> None:
    assert tp.pane_input_box_present(_pane(name)) is True
    assert tp.classify_input_box_failure(_pane(name)) is None


# ── The pane must REFUSE (the gate FAILS) ────────────────────────────────

REFUSED = [
    # A4 (design-killer) — EVERY blocking family REPLACES the box.
    ("auq_single_picker_v2.1.207.txt", "prompt_row_is_option"),
    ("auq_multi_picker_v2.1.207.txt", None),
    ("gate_epm_v2.1.207.txt", None),
    ("gate_workflow_v2.1.207.txt", None),
    ("gate_permission_v2.1.207.txt", None),
    ("folder_trust_arrival_plain_v2.1.207.txt", None),
    # M4 — the parser is BLIND to `Switch model?` (footer-less ⇒
    # parse_generic_decision returns None), yet the positive gate still refuses.
    ("switch_model_live_v2.1.207.txt", None),
    ("unknown_blocking_confirm_switch_model_v2.1.197.txt", None),
    # Settings / RestoreCheckpoint-class modals.
    ("settings_warning_v2170.txt", None),
    ("settings_select_model_v2.1.200.txt", None),
    # B5 — the /cost + /usage overlays REPLACE the box.
    ("overlay_cost_modal_v2.1.207.txt", None),
    ("cost_overlay_live_v2.1.206.txt", None),
    ("usage_overlay_live_v2.1.206.txt", None),
    # §5 finding 1 — the Enter-STEALING background-tasks mode: legs 1-3 all pass,
    # but typed text is swallowed and Enter opens the Shell-details modal.
    ("inputbox_tasks_mode_v2.1.207.txt", "tasks_mode"),
    # §5 finding 2 — the input-capturing completion overlays.
    ("inputbox_at_overlay_v2.1.207.txt", "completion_overlay"),
    ("inputbox_slash_overlay_v2.1.207.txt", "completion_overlay"),
    ("inputbox_slash_exact_clear_v2.1.207.txt", "completion_overlay"),
    # M3 — a bare shell after `/esc` on folder-trust EXITED Claude.
    ("shell_after_esc_v2.1.207.txt", None),
]


@pytest.mark.parametrize("name,reason", REFUSED)
def test_blocking_panes_refuse(name: str, reason: str | None) -> None:
    pane = _pane(name)
    assert tp.pane_input_box_present(pane) is False
    got = tp.classify_input_box_failure(pane)
    assert got in tp.INPUT_BOX_FAILURE_REASONS
    if reason is not None:
        assert got == reason


def test_empty_capture_is_indeterminate() -> None:
    assert tp.pane_input_box_present("") is False
    assert tp.classify_input_box_failure("") == "capture_empty"
    assert tp.classify_input_box_failure(None) == "capture_empty"


def test_synthetic_unknown_bottom_prompt_refuses() -> None:
    """A prompt shape the parser has NEVER seen still refuses — the whole point
    of inverting the gate (M4 generalized)."""
    pane = (
        "  Some assistant prose above.\n"
        "\n"
        "  Reticulate the splines?\n"
        "  This has never shipped in any Claude Code version.\n"
        "\n"
        "  ❯ 1. Absolutely\n"
        "    2. Never\n"
        "\n"
        "  Press any key to continue · Esc to bail\n"
    )
    assert tp.pane_input_box_present(pane) is False


def test_flag_independence_folder_trust_refuses_with_detectors_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate never consults ``_active_ui_patterns``, so the display
    kill-switches cannot reopen the hole (plan §1.1)."""
    tp.set_permission_prompts_enabled(False)
    tp.set_decision_cards_enabled(False)
    try:
        pane = _pane("folder_trust_arrival_plain_v2.1.207.txt")
        # The DETECTOR is genuinely blind with the flags off …
        assert tp.extract_interactive_content(pane) is None
        # … and the gate refuses anyway.
        assert tp.pane_input_box_present(pane) is False
    finally:
        tp.reset_for_tests()


def test_slash_completion_exemption_is_scoped_to_the_slash_arm() -> None:
    """``allow_slash_completion`` exempts ONLY the ``/`` arm — never the ``@``
    arm, which is pure data loss (Enter picks a file completion and the message
    is never sent)."""
    slash = _pane("inputbox_slash_exact_clear_v2.1.207.txt")
    at = _pane("inputbox_at_overlay_v2.1.207.txt")
    assert (
        tp.pane_input_box_present(
            slash, allow_slash_completion=True, expected_draft="/clear"
        )
        is True
    )
    assert (
        tp.pane_input_box_present(
            at, allow_slash_completion=True, expected_draft="please ask @se"
        )
        is False
    )


# ── r2 F6: the ``/`` exemption needs PROOF the row is OUR payload ────────
#
# Keyed on the payload SHAPE alone, the exemption also covered a PRE-EXISTING
# ``/co`` draft a human left in the input box — Enter would then run ``/copy`` on
# text the bot never authored. (The ambiguous-prefix misfire itself is GH #53 and
# out of scope; this only refuses to WIDEN it.)


def test_slash_exemption_requires_the_row_to_be_our_exact_payload() -> None:
    pre_existing = _pane("inputbox_slash_overlay_v2.1.207.txt")  # the row reads `/co`
    # Our payload is a bare slash command, but the box does NOT hold it.
    assert (
        tp.pane_input_box_present(
            pre_existing, allow_slash_completion=True, expected_draft="/cost"
        )
        is False
    )
    # And the shape alone (no draft evidence at all) never exempts.
    assert tp.pane_input_box_present(pre_existing, allow_slash_completion=True) is False


def test_slash_exemption_is_never_granted_to_a_PREFIX_of_our_payload() -> None:
    """A half-written ``/co`` while our payload is ``/cost`` is exactly the GH #53
    hazard — the exemption demands the EXACT first line, never a prefix."""
    half = _pane("inputbox_slash_overlay_v2.1.207.txt")  # `/co`
    assert (
        tp.pane_input_box_present(
            half, allow_slash_completion=True, expected_draft="/cost"
        )
        is False
    )


# ── r2 F1: the picker trap is FIRST-ROW-ONLY and PAYLOAD-AWARE ───────────
#
# The gate WRITES the payload and re-verifies AFTER, so an ordinary `1. buy milk`
# renders the box as `❯ 1. buy milk`. The unqualified trap fired there, the Enter
# was withheld, and the message was never sent — it just sat as a draft.

_DRAFT_FIXTURE = "inputbox_draft_typed_v2.1.207.txt"
_DRAFT_LITERAL = "hello this is a plain draft"


def _pane_with_draft(text: str) -> str:
    """The real draft pane with its input-row content replaced by ``text``."""
    return _pane(_DRAFT_FIXTURE).replace(_DRAFT_LITERAL, text)


@pytest.mark.parametrize(
    "payload",
    [
        "1. buy milk",
        "2. then eggs",
        "10. and the tenth thing",
        "1. foo\nsecond line\nthird line",  # a multi-line payload, numbered FIRST line
    ],
)
def test_our_own_numbered_payload_is_not_mistaken_for_a_picker(payload: str) -> None:
    pane = _pane_with_draft(payload.split("\n", 1)[0])
    # Without the payload evidence the trap fires (the pre-write gate's
    # fail-closed shape — a human's numbered draft is refused; disclosed).
    assert tp.classify_input_box_failure(pane) == "prompt_row_is_option"
    # WITH it, the row is proven to be our own text and the message delivers.
    assert tp.pane_input_box_present(pane, expected_draft=payload) is True


def test_a_wrapped_numbered_payload_still_matches_its_first_visual_row() -> None:
    """A long first line soft-wraps, so the visual row is only a PREFIX of it."""
    long_line = "1. " + "buy milk and eggs and bread " * 8
    pane = _pane_with_draft(long_line[:120])  # the terminal's first visual row
    assert tp.pane_input_box_present(pane, expected_draft=long_line) is True


def test_a_live_picker_in_the_gate_to_write_window_STILL_refuses() -> None:
    """The adversarial case the trap exists for: a picker appeared between the
    gate and the write, so the pane shows the PICKER's own `❯ 1. Red` — NOT our
    text. ``expected_draft`` must not launder it."""
    picker = _pane("auq_single_picker_v2.1.207.txt")
    assert tp.pane_input_box_present(picker, expected_draft="1. buy milk") is False
    assert tp.classify_input_box_failure(picker, expected_draft="1. buy milk") == (
        "prompt_row_is_option"
    )
    # And even a payload that shares the picker's option-1 PREFIX cannot pass:
    # the picker replaces the ready status chrome with its own footer (leg 3).
    assert tp.pane_input_box_present(picker, expected_draft="1. Red is nice") is False


def test_option_row_trap_is_redundant_on_the_real_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEASURED, not asserted: with the trap disabled ENTIRELY, every blocking
    pane in the corpus is still refused by another leg (the AUQ single picker by
    leg 3 ``no_ready_chrome``) and every deliverable pane still passes.

    That is what licenses narrowing it — it is defence in depth (for a
    hypothetical picker variant rendering ready-chrome below its footer), never
    the load-bearing leg.
    """
    import re

    monkeypatch.setattr(tp, "_RE_OPTION_ROW_CONTENT", re.compile(r"(?!x)x"))
    for name, _reason in REFUSED:
        assert tp.pane_input_box_present(_pane(name)) is False, name
    for name in DELIVERABLE:
        assert tp.pane_input_box_present(_pane(name)) is True, name


# ── r2 F2: the input-row emptiness probe (the stranded-draft self-heal) ──


@pytest.mark.parametrize(
    "name,expected",
    [
        ("inputbox_idle_v2.1.207.txt", True),
        ("inputbox_bashmode_empty_v2.1.207.txt", True),
        # A CC ≥2.1.206 DIM ghost suggestion reads as EMPTY (it is not typed text).
        ("idle_ghost_input_row_v2.1.206.txt", True),
        ("inputbox_draft_typed_v2.1.207.txt", False),
        ("inputbox_wrapped_draft_v2.1.207.txt", False),
        ("idle_real_draft_input_row_v2.1.206.txt", False),
        # A live blocking prompt REPLACES the box ⇒ INDETERMINATE, never "empty".
        ("auq_single_picker_v2.1.207.txt", None),
        ("gate_epm_v2.1.207.txt", None),
        ("shell_after_esc_v2.1.207.txt", None),
    ],
)
def test_pane_input_row_empty(name: str, expected: bool | None) -> None:
    assert tp.pane_input_row_empty(_pane(name)) is expected


def test_pane_input_row_empty_is_indeterminate_on_a_dead_capture() -> None:
    assert tp.pane_input_row_empty(None) is None
    assert tp.pane_input_row_empty("") is None
    assert tp.pane_input_row_empty("\n\n(mid-redraw)\n") is None


# ── LABELED TOP RULE (CC 2.1.207): the pre-existing /update + /cost breakage ──
#
# A few seconds after a plan is approved, CC pins the plan slug into the input
# box's TOP rule — ``────… add-ok-to-note ──`` — and it PERSISTS for the rest of
# the session (only ``/clear`` drops it). ``_RE_RULE_SEPARATOR`` matched pure
# dashes only, so ``_input_box_rows`` could not find the bracket:
#
#   - ``pane_input_box_present`` ⇒ the GH #50 delivery gate would refuse EVERY
#     message in that topic (``no_input_box``); and
#   - ``pane_looks_idle`` ⇒ a PRE-EXISTING bug, shipped long before GH #50:
#     ``/update`` and ``/cost`` were BROKEN (silently deferring / refusing) in any
#     topic where a plan had been approved.
#
# Both are pinned here on the real rig captures.

_LABELED_RULE_PANES = [
    "epm_after_approve_t5_v2.1.207.txt",
    "epm_after_approve_t30_idle_v2.1.207.txt",
    "epm_plan_label_persists_next_turn_v2.1.207.txt",
]

_POST_RESOLUTION_PANES = _LABELED_RULE_PANES + [
    "epm_after_approve_t0_v2.1.207.txt",
    "epm_after_approve_t1_v2.1.207.txt",
    "epm_plan_label_after_clear_v2.1.207.txt",  # /clear DROPS the label
    "auq_after_answer_t0_v2.1.207.txt",
    "auq_after_answer_t1_v2.1.207.txt",
    "auq_after_answer_t5_v2.1.207.txt",
    "auq_after_answer_t30_v2.1.207.txt",
    "trust_after_accept_t0_v2.1.207.txt",
    "trust_after_accept_t5_v2.1.207.txt",
    "control_gitrepo_branch_no_label_v2.1.207.txt",  # no label — the control
]


@pytest.mark.parametrize("name", _LABELED_RULE_PANES)
def test_a_labeled_top_rule_is_still_an_input_box(name: str) -> None:
    pane = _pane(name)
    # The fixture genuinely CARRIES the labeled rule (a separator that is not
    # pure dashes) — otherwise this pin would be vacuous.
    rules = [
        ln.strip()
        for ln in tp._strip_ansi(pane).split("\n")
        if tp._is_rule_separator(ln)
    ]
    assert any(r.strip("─").strip() for r in rules), f"{name} carries no labeled rule"
    assert tp.pane_input_box_present(pane) is True


@pytest.mark.parametrize("name", _POST_RESOLUTION_PANES)
def test_every_answered_prompt_pane_still_delivers(name: str) -> None:
    """After ANY prompt resolves the topic must accept messages again."""
    assert tp.pane_input_box_present(_pane(name)) is True


@pytest.mark.parametrize(
    "name",
    [
        "epm_after_approve_t30_idle_v2.1.207.txt",  # labeled rule + settled
        "epm_plan_label_persists_next_turn_v2.1.207.txt",
        "epm_plan_label_after_clear_v2.1.207.txt",
        "auq_after_answer_t5_v2.1.207.txt",
        "auq_after_answer_t30_v2.1.207.txt",
        "trust_after_accept_t0_v2.1.207.txt",
        "trust_after_accept_t5_v2.1.207.txt",
        "control_gitrepo_branch_no_label_v2.1.207.txt",
    ],
)
def test_settled_panes_look_idle_so_update_and_cost_recover(name: str) -> None:
    """The PRE-EXISTING bug (NOT introduced by GH #50): the labeled top rule also
    broke ``pane_looks_idle``, so ``/update`` deferred and ``/cost`` refused in
    any topic where a plan had been approved."""
    assert tp.pane_looks_idle(tp.clean_ghost_input_text(_pane(name))) is True


@pytest.mark.parametrize(
    "name",
    [
        "auq_before_answer_v2.1.207.txt",
        "epm_before_approve_v2.1.207.txt",
        "trust_before_answer_v2.1.207.txt",
    ],
)
def test_the_live_prompt_controls_still_refuse(name: str) -> None:
    """The positive controls for the panes above: while the prompt is LIVE the
    gate refuses and the pane is not idle (the labeled-rule tolerance must not
    have widened the box into a live prompt)."""
    pane = _pane(name)
    assert tp.pane_input_box_present(pane) is False
    assert tp.pane_looks_idle(tp.clean_ghost_input_text(pane)) is False


# ── THE PASTE-COLLAPSE (the GH #50 PR-1 regression) ──────────────────────
#
# Rig-reproduced on CC 2.1.207: a payload written in ONE `tmux send-keys -l` past
# ~800 chars / ~13 lines is consumed as a PASTE. CC collapses the input row to
# `❯\xa0[Pasted text #1 +12 lines]` AND **REPLACES THE STATUS BAR** with the
# single line `  paste again to expand` for ~2s — squarely across the delivery
# gate's post-write re-verify (`TEXT_SETTLE_S` = 0.5s).
#
# None of leg 3's old markers survives that, so `no_ready_chrome` fired and every
# long / multi-line message (a voice note with a reply-context quote — the
# owner's 809-char report) was refused, left as a stranded draft, and braked the
# topic. It is a fully READY input box: Enter submits.

_PASTE_COLLAPSED = "inputbox_paste_collapsed_v2.1.207.txt"
_PASTE_REVERTED = "inputbox_paste_collapsed_reverted_v2.1.207.txt"


@pytest.mark.parametrize("name", [_PASTE_COLLAPSED, _PASTE_REVERTED])
def test_the_paste_collapsed_box_is_a_READY_input_box(name: str) -> None:
    pane = _pane(name)
    # The fixture genuinely carries the collapsed placeholder — otherwise the pin
    # is vacuous.
    assert "[Pasted text #1" in pane, name
    assert tp.pane_input_box_present(pane) is True
    assert tp.classify_input_box_failure(pane) is None


def test_the_collapsed_fixture_carries_the_paste_hint_status_bar() -> None:
    """The distinguishing chrome: `paste again to expand` REPLACES the mode line,
    and NONE of the pre-existing ready markers is on the pane below the box."""
    pane = _pane(_PASTE_COLLAPSED)
    assert "paste again to expand" in pane
    # The status bar is genuinely GONE (this is what broke leg 3).
    assert "shift+tab to cycle" not in pane
    assert "? for shortcuts" not in pane
    # And the reverted twin proves CC restores it (the owner's live shape).
    assert "shift+tab to cycle" in _pane(_PASTE_REVERTED)


def test_a_collapsed_paste_draft_is_NOT_an_empty_input_row() -> None:
    """The stranded-draft brake must not self-release on a collapsed draft — the
    payload IS in the box, it is just rendered as a placeholder."""
    assert tp.pane_input_row_empty(_pane(_PASTE_COLLAPSED)) is False
    assert tp.pane_input_row_empty(_pane(_PASTE_REVERTED)) is False


def test_a_paste_collapsed_pane_is_NOT_idle() -> None:
    """`paste again to expand` is deliberately NOT in `_READY_STATUS_MARKERS`: a
    collapsed paste holds an UNCOMMITTED draft, so `/update` must still defer
    (a restart would discard it) and `/cost` must still refuse."""
    for name in (_PASTE_COLLAPSED, _PASTE_REVERTED):
        assert tp.pane_looks_idle(tp.clean_ghost_input_text(_pane(name))) is False, name


@pytest.mark.parametrize("name,_reason", REFUSED)
def test_paste_hint_below_a_blocking_pane_still_refuses(
    name: str, _reason: str | None
) -> None:
    """The SAFETY ARGUMENT, MEASURED not asserted (the shared-constant question).

    Widening leg 3's alphabet cannot let a blocking prompt through, because a
    blocking prompt REPLACES the input box: it fails leg 1 (`no_input_box`) or
    leg 2 (`prompt_row_is_option`) no matter what leg 3 says. Here the paste hint
    is adversarially APPENDED below every blocking pane in the corpus — each one
    still refuses.
    """
    poisoned = _pane(name).rstrip("\n") + "\n  paste again to expand\n"
    assert tp.pane_input_box_present(poisoned) is False, name
    assert tp.classify_input_box_failure(poisoned) in tp.INPUT_BOX_FAILURE_REASONS


@pytest.mark.parametrize(
    "name", ["gate_permission_v2.1.207.txt", "gate_workflow_v2.1.207.txt"]
)
def test_the_paste_hint_rejects_a_QUOTED_gate(name: str) -> None:
    """The gate-rejection lane needed NO change and is already correct.

    `_only_chrome_below` consumes no marker set at all — it is a structural
    ALLOW-LIST (blank / bare separator / the gate's own `ctrl+<x>` hints). The
    paste hint is none of those, so a "gate" rendered ABOVE a live
    paste-collapsed box is correctly rejected as quoted scrollback: the hint
    PROVES the input box is live, so the gate is not the active bottom prompt.
    """
    # The suite pins both detector kill-switches OFF; turn them on so the gate
    # patterns are actually in `_active_ui_patterns` and the pin is not vacuous.
    tp.set_permission_prompts_enabled(True)
    tp.set_decision_cards_enabled(True)
    try:
        pane = _pane(name)
        assert tp.extract_interactive_content(pane) is not None  # live ⇒ surfaced
        quoted = pane.rstrip("\n") + "\n  paste again to expand\n"
        assert tp.extract_interactive_content(quoted) is None  # quoted ⇒ dropped
    finally:
        tp.reset_for_tests()


# ── The NON-BREAKING SPACE in the input row (load-bearing, now pinned) ───
#
# CC renders `❯\xa0` (U+00A0), never `❯ `. Today's code copes only INCIDENTALLY
# (`str.strip()` drops NBSP), and that incidental behavior decides whether the row
# reads EMPTY — the stranded-draft brake's ONLY release condition.

_NBSP = "\xa0"


def _box(row: str) -> str:
    """A minimal ready input box whose input row is exactly ``row``."""
    rule = "─" * 40
    return f"  some prose\n{rule}\n{row}\n{rule}\n  ? for shortcuts\n"


def test_the_real_captured_rows_carry_a_NON_BREAKING_space() -> None:
    """Not a synthetic claim — both rig fixtures really do."""
    assert f"❯{_NBSP}[Pasted text #1" in _pane(_PASTE_COLLAPSED)
    assert f"❯{_NBSP}[Pasted text #1" in _pane(_PASTE_REVERTED)


@pytest.mark.parametrize(
    "row,empty",
    [
        (f"❯{_NBSP}", True),  # the REAL empty input row
        ("❯ ", True),  # the ASCII twin
        ("❯", True),  # bare glyph
        (f"❯{_NBSP}[Pasted text #1 +12 lines]", False),  # the REAL collapsed row
        (f"❯{_NBSP}hello there", False),
        (f"!{_NBSP}echo hi", False),  # bash mode (rig C9)
    ],
)
def test_nbsp_input_rows_are_normalized(row: str, empty: bool) -> None:
    pane = _box(row)
    assert tp.pane_input_row_empty(pane) is empty, row
    assert tp.pane_input_box_present(pane) is True, row


def test_an_nbsp_numbered_row_still_trips_the_picker_trap() -> None:
    """The normalization must not hide a picker cursor behind an NBSP."""
    assert (
        tp.classify_input_box_failure(_box(f"❯{_NBSP}1. Red")) == "prompt_row_is_option"
    )


def test_nbsp_normalization_does_not_leak_outside_the_input_box_lane() -> None:
    """Scoped to `_input_box_rows`. The chrome region below the box, the rule
    scan, and every other parser see the pane VERBATIM — a global NBSP fold would
    change unrelated matching (option labels, gate footers, prose)."""
    assert tp._normalize_input_row(f"a{_NBSP}b") == "a b"
    # The gate/idle parsers are untouched by it — a live gate still surfaces
    # (the gate lane is a structural allow-list, not a space-fold).
    tp.set_permission_prompts_enabled(True)
    try:
        assert (
            tp.extract_interactive_content(_pane("gate_permission_v2.1.207.txt"))
            is not None
        )
    finally:
        tp.reset_for_tests()
    # And a body line carrying an NBSP is NOT folded outside the input-box rows.
    assert f"a{_NBSP}b" in _box(f"❯{_NBSP}x").replace("some prose", f"a{_NBSP}b")


def test_agreement_predicate_and_classifier_never_disagree() -> None:
    """``classify_input_box_failure`` returns None IFF the predicate is True —
    over EVERY pane fixture in the repo (the classify_pane_idle_failure precedent)."""
    for path in sorted(FIXTURES.glob("*.txt")):
        text = path.read_text()
        present = tp.pane_input_box_present(text)
        reason = tp.classify_input_box_failure(text)
        assert present is (reason is None), path.name
        if reason is not None:
            assert reason in tp.INPUT_BOX_FAILURE_REASONS, (path.name, reason)


def test_indeterminate_reasons_are_a_subset_of_the_leg_names() -> None:
    assert tp.INPUT_BOX_INDETERMINATE_REASONS <= tp.INPUT_BOX_FAILURE_REASONS


# ── GH #56: the tall multi-line draft fallback (exactly-1-separator scan) ──
#
# A reply-quoted message renders a ~18-row draft INSIDE the input box, pushing the
# box's TOP rule above the 20-line `_CHROME_SCAN_LINES` window. Only the bottom
# rule is in view, so the original `_input_box_rows` returned None and the delivery
# gate's POST-WRITE re-verify concluded `no_input_box` — Enter withheld, the
# stranded-draft brake armed, the NEXT message refused too (a topic wedge on the
# owner's dominant gesture). The fallback scans UPWARD for the top rule under a
# three-part structural proof; the coupled fix adds `⏸ manual mode on` to leg 3's
# alphabet (the rig fixture's only status row).

_TALL_DRAFT = "inputbox_tall_draft_v2.1.209.txt"
_TALL_DRAFT_ANSI = "inputbox_tall_draft_v2.1.209.ansi.txt"
_TALL_DRAFT_CLEARED = "inputbox_tall_draft_cleared_v2.1.209.txt"


@pytest.mark.parametrize("name", [_TALL_DRAFT, _TALL_DRAFT_ANSI])
def test_tall_reply_quoted_draft_is_a_READY_input_box(name: str) -> None:
    """RED-first: today `classify_input_box_failure` returns `no_input_box` on this
    real rig capture (the top rule at line 30 is outside the 20-line window); the
    fallback flips it to a fully-ready box. Plain AND ANSI captures agree."""
    pane = _pane(name)
    # The fixture genuinely has the tall shape — top rule far above, one in-window
    # separator — otherwise this pin is vacuous.
    assert "[Telegram reply context]" in tp._strip_ansi(pane)
    assert tp.pane_input_box_present(pane) is True
    assert tp.classify_input_box_failure(pane) is None
    # The brake's release proof reads the SAME rows: the box is FOUND and its input
    # row is non-empty (False, never None — None would mean "box not found").
    assert tp.pane_input_row_empty(pane) is False


def test_tall_draft_cleared_capture_releases_the_brake() -> None:
    """The brake-release twin: after the draft is cleared the input row is provably
    empty (True), so `clear_window_stranded_draft`'s only proof holds."""
    pane = _pane(_TALL_DRAFT_CLEARED)
    assert tp.pane_input_row_empty(pane) is True
    assert tp.pane_input_box_present(pane) is True


def test_the_manual_mode_marker_is_in_the_leg3_alphabet_not_the_idle_one() -> None:
    """The coupled alphabet fix, pinned BOTH ways: `manual mode on` is a leg-3
    ready marker (so the tall-draft box passes) but is NOT in the idle-status
    alphabet (a manual-mode pane holding a draft is not "idle" for /update /
    /cost — the paste-collapse precedent)."""
    assert "manual mode on" in tp._INPUT_READY_CHROME_MARKERS
    assert "manual mode on" not in tp._READY_STATUS_MARKERS


def test_a_manual_mode_status_bar_passes_leg3_with_a_draft_present() -> None:
    """The direct leg-3 pin: a normal 2-separator box whose ONLY status marker is
    `⏸ manual mode on`, holding a draft, is a ready input box (before the addition,
    leg 3 returned `no_ready_chrome` — the same false-refusal class as the
    paste-collapse regression)."""
    rule = "─" * 40
    pane = f"  prose above\n{rule}\n❯ my drafted reply\n{rule}\n  ⏸ manual mode on\n"
    assert tp.classify_input_box_failure(pane) is None
    assert tp.pane_input_box_present(pane) is True


# ── The three-part structural proof: two reproduced spoofs must STILL refuse ──


def _tall(draft_rows: list[str], status_row: str, below_extra: str = "") -> str:
    """A synthetic pane whose TOP rule is pushed out of the 20-line window by a
    tall draft (so exactly ONE separator is in the window). Mirrors the rig
    fixture's geometry."""
    rule = "─" * 40
    pad = "\n".join(f"  filler line {i}" for i in range(6))
    body = "\n".join(draft_rows)
    tail = f"\n{below_extra}" if below_extra else ""
    return f"{pad}\n{rule}\n{body}\n{rule}\n  {status_row}{tail}\n"


def test_spoof_lone_separator_is_a_live_prompts_TOP_rule_STILL_refuses() -> None:
    """Codex r2 (b): the lone in-window separator is a LIVE PROMPT's top rule with
    the picker body `❯ 1. Yes` below it — no numbered row may sit below the
    presumed bottom rule."""
    draft = ["❯ a stale draft above"] + [f"  draft cont {i}" for i in range(18)]
    pane = _tall(
        draft,
        "❯ 1. Yes",
        below_extra="    2. No\n  Enter to select · Esc to cancel",
    )
    assert tp.pane_input_box_present(pane) is False
    assert tp.classify_input_box_failure(pane) == "no_input_box"


def test_the_option_row_below_the_lone_separator_guard_is_load_bearing() -> None:
    """Part (b) in isolation: even when the first-below row IS a status bar
    (spoofing part (a)), a picker option row further below refuses."""
    draft = ["❯ a stale draft above"] + [f"  draft cont {i}" for i in range(18)]
    pane = _tall(draft, "esc to interrupt", below_extra="  ❯ 1. Yes\n    2. No")
    assert tp.pane_input_box_present(pane) is False
    assert tp.classify_input_box_failure(pane) == "no_input_box"


def test_spoof_effort_header_substring_marker_STILL_refuses() -> None:
    """Codex r2 (a): a header below the lone separator CONTAINS `/effort` (a leg-3
    substring-alphabet hit), but the STRICT full-row grammar rejects it — the whole
    row must BE a status bar, not merely embed a marker."""
    draft = ["❯ a stale draft above"] + [f"  draft cont {i}" for i in range(18)]
    pane = _tall(draft, "Which /effort level do you want? Choose one:")
    # Sanity: the leg-3 substring alphabet WOULD hit `/effort` (the spoof's premise).
    assert any(m in "Which /effort level" for m in tp._INPUT_READY_CHROME_MARKERS)
    # The strict grammar is what refuses it.
    assert tp._is_status_row("Which /effort level do you want? Choose one:") is False
    assert tp.pane_input_box_present(pane) is False
    assert tp.classify_input_box_failure(pane) == "no_input_box"


def test_a_draft_containing_a_rule_like_line_still_refuses_fail_closed() -> None:
    """Disclosed residual: a reply-quote of terminal output that CONTAINS a `─…`
    line makes the upward scan pair with the draft-internal rule → no glyph row
    directly below it → fail-closed refusal, exactly as today."""
    draft = [
        "❯ pasted some terminal output:",
        "  " + "─" * 40,  # a rule-like line INSIDE the draft
        "  and here is more of the pasted output continuing below the rule",
    ] + [f"  draft cont {i}" for i in range(16)]
    pane = _tall(draft, "⏸ manual mode on")
    assert tp.pane_input_box_present(pane) is False
    assert tp.classify_input_box_failure(pane) == "no_input_box"


# ── The STRONG corpus pin: every EXISTING fixture's classification is unchanged ──
#
# The fallback only fires when there is EXACTLY ONE separator in the 20-line
# window, so it cannot disturb the ≥2 path. This bakes the pre-change
# classification of every existing corpus fixture (the 2.1.209 fixtures are new /
# changing, so they are excluded) and asserts byte-exact equality — a stronger pin
# than refused-vs-passed.
_BASELINE_CLASSIFICATIONS = {
    "auq-baseline-pane.txt": "no_input_box",
    "auq_4option_160x50_v2.1.198.txt": "no_input_box",
    "auq_after_answer_t0_v2.1.207.txt": None,
    "auq_after_answer_t1_v2.1.207.txt": None,
    "auq_after_answer_t30_v2.1.207.txt": None,
    "auq_after_answer_t5_v2.1.207.txt": None,
    "auq_before_answer_v2.1.207.txt": "no_input_box",
    "auq_freetext_overflow_v2.1.207.txt": "no_input_box",
    "auq_freetext_row_selected_pretype_v2.1.207.ansi.txt": "no_input_box",
    "auq_freetext_row_typed_large_v2.1.207.ansi.txt": "no_input_box",
    "auq_freetext_row_typed_v2.1.207.ansi.txt": "prompt_row_is_option",
    "auq_freetext_typed_identical_label_v2.1.207.ansi.txt": "prompt_row_is_option",
    "auq_longlabel_160x50_v2.1.198.txt": "no_input_box",
    "auq_multi_picker_v2.1.207.txt": "no_input_box",
    # CC 2.1.237 multi-question gutter layout (the AUQ details-card hotfix's
    # real capture). A live picker replaces the input box ⇒ no_input_box, i.e.
    # the delivery gate keeps refusing a payload at this surface.
    "auq_multiq_gutter_pane_v2.1.237.txt": "no_input_box",
    "auq_multiq_q1_pane.txt": "prompt_row_is_option",
    "auq_multiq_q2_after_pick_pane.txt": "prompt_row_is_option",
    "auq_multiq_submit_pane.txt": "no_input_box",
    "auq_multiselect_2_toggled_tmux_capture.txt": "prompt_row_is_option",
    "auq_multiselect_compressed_long_cursor_only_tmux_capture.txt": "no_input_box",
    "auq_multiselect_fresh_tmux_capture.txt": "no_input_box",
    "auq_multiselect_long_scrolled_toggled_S500.txt": "no_input_box",
    "auq_multiselect_ready_to_submit_tmux_capture.txt": "no_input_box",
    "auq_multiselect_review_cursor_cancel.txt": "no_input_box",
    "auq_multiselect_review_cursor_submit.txt": "no_input_box",
    # GH #54 preview fixtures (r1 fold P2 — added to keep the dict corpus-complete
    # after the merge onto main; all live pickers ⇒ no_input_box, verified).
    "auq_preview_multiquestion_q1_v2.1.207.ansi.txt": "no_input_box",
    "auq_preview_multiquestion_q1_v2.1.207.txt": "no_input_box",
    "auq_preview_multiquestion_q2_v2.1.207.ansi.txt": "no_input_box",
    "auq_preview_multiquestion_q2_v2.1.207.txt": "no_input_box",
    "auq_preview_multiselect_v2.1.207.ansi.txt": "no_input_box",
    "auq_preview_multiselect_v2.1.207.txt": "no_input_box",
    "auq_preview_sidebyside_v2.1.197.aligned.txt": "no_input_box",
    "auq_preview_sidebyside_v2.1.197.ansi.txt": "no_input_box",
    "auq_preview_sidebyside_v2.1.197.txt": "no_input_box",
    "auq_preview_singleselect_cursor2_v2.1.207.ansi.txt": "no_input_box",
    "auq_preview_singleselect_cursor2_v2.1.207.txt": "no_input_box",
    "auq_preview_singleselect_v2.1.207.ansi.txt": "no_input_box",
    "auq_preview_singleselect_v2.1.207.txt": "no_input_box",
    "auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt": "no_input_box",
    "auq_preview_wraplabels_cursor1_v2.1.207.txt": "no_input_box",
    "auq_preview_wraplabels_cursor2_v2.1.207.ansi.txt": "no_input_box",
    "auq_preview_wraplabels_cursor2_v2.1.207.txt": "no_input_box",
    "auq_preview_wraplabels_v2.1.207.ansi.txt": "no_input_box",
    "auq_preview_wraplabels_v2.1.207.txt": "no_input_box",
    "auq_single_long_scrolled_cursor1_S500.txt": "no_input_box",
    "auq_single_long_scrolled_cursor2_S500.txt": "no_input_box",
    "auq_single_long_scrolled_cursor3_S500.txt": "no_input_box",
    "auq_single_long_scrolled_cursor4_S500.txt": "no_input_box",
    "auq_single_long_scrolled_cursor5_S500.txt": "no_input_box",
    "auq_single_picker_v2.1.207.txt": "prompt_row_is_option",
    "auq_single_select_with_affordances_pane.txt": "no_input_box",
    "auq_stale_tabheader_over_live_picker_S500.txt": "no_input_box",
    "control_gitrepo_branch_no_label_v2.1.207.txt": None,
    "cost_overlay_d_v2.1.206.txt": "no_input_box",
    "cost_overlay_live_v2.1.206.txt": "no_input_box",
    "cost_overlay_w_v2.1.206.txt": "no_input_box",
    # GH #52 footerless-Decision fixtures — a live footerless prompt REPLACES the
    # input box (no_input_box), and the post-commit restored box is deliverable
    # (None). Flag-independent: the delivery gate never consults the Decision flag.
    "decision_footerless_neg_inputbox_restored_v2.1.207.txt": None,
    "decision_footerless_neg_model_picker_v2.1.207.txt": "no_input_box",
    "decision_footerless_switchmodel_scrollback_v2.1.207.txt": "no_input_box",
    "decision_footerless_switchmodel_v2.1.207.txt": "no_input_box",
    "decision_negative_quoted_scrollback_v2.1.200.txt": None,
    "decision_switch_model_v2.1.200.txt": "no_input_box",
    "decision_trust_folder_postdown_v2.1.204.txt": "no_input_box",
    "decision_trust_folder_postup_v2.1.204.txt": "no_input_box",
    "decision_trust_folder_v2.1.200.txt": "no_input_box",
    "decision_trust_folder_v2.1.204.txt": "no_input_box",
    "detailed_transcript_full_v2.1.206.txt": "no_input_box",
    "epm_after_approve_t0_v2.1.207.txt": None,
    "epm_after_approve_t1_v2.1.207.txt": None,
    "epm_after_approve_t30_idle_v2.1.207.txt": None,
    "epm_after_approve_t5_v2.1.207.txt": None,
    "epm_before_approve_v2.1.207.txt": "no_input_box",
    "epm_plan_label_after_clear_v2.1.207.txt": None,
    "epm_plan_label_persists_next_turn_v2.1.207.txt": None,
    "epm_v2170_ctrl_plus_g.txt": "no_input_box",
    "folder_trust_arrival_plain_v2.1.206.txt": "no_input_box",
    "folder_trust_arrival_plain_v2.1.207.txt": "no_input_box",
    # GH #65 / Wave 3 rig (2026-08-25). Every one of these is a blocking trust
    # prompt, a post-commit/post-cancel corpse still SHOWING that prompt's text,
    # a transitional blank frame, or a bare shell running the version probe —
    # none is a ready input box, so all classify ``no_input_box`` and the
    # delivery gate refuses. (The post-commit corpses are exactly why the trust
    # lane's slice classifier checks the pane COMMAND before any pane TEXT.)
    "folder_trust_arrival_plain_v2.1.239.txt": "no_input_box",
    "folder_trust_arrival_plain_v2.1.241.txt": "no_input_box",
    "folder_trust_e2c_navto2_plain_v2.1.241.txt": "no_input_box",
    "folder_trust_postdigit2_t2_plain_v2.1.241.txt": "no_input_box",
    "folder_trust_postdown2_plain_v2.1.241.txt": "no_input_box",
    "folder_trust_postdown_plain_v2.1.241.txt": "no_input_box",
    "folder_trust_postenter_t1_plain_v2.1.241.txt": "no_input_box",
    "folder_trust_postesc_t4_plain_v2.1.241.txt": "no_input_box",
    "folder_trust_postup_plain_v2.1.241.txt": "no_input_box",
    # GH #72: the 2.1.246 re-characterization of the SAME surface — identical
    # shape, so identical classifications.
    "folder_trust_arrival_plain_v2.1.246.txt": "no_input_box",
    "folder_trust_e2c_navto2_plain_v2.1.246.txt": "no_input_box",
    "folder_trust_postdigit2_t2_plain_v2.1.246.txt": "no_input_box",
    "folder_trust_postdown2_plain_v2.1.246.txt": "no_input_box",
    "folder_trust_postdown_plain_v2.1.246.txt": "no_input_box",
    "folder_trust_postesc_t4_plain_v2.1.246.txt": "no_input_box",
    "folder_trust_postup_plain_v2.1.246.txt": "no_input_box",
    "version_probe_plain_v2.1.239.txt": "no_input_box",
    "version_probe_plain_v2.1.241.txt": "no_input_box",
    "version_probe_plain_v2.1.246.txt": "no_input_box",
    "gate_epm_v2.1.207.txt": "no_input_box",
    "gate_permission_v2.1.207.txt": "no_input_box",
    "gate_workflow_v2.1.207.txt": "no_input_box",
    "gh43_bg_shell_frame.txt": None,
    "idle_frame_plain_v2.1.206.txt": None,
    "idle_ghost_input_row_v2.1.206.txt": None,
    "idle_real_draft_input_row_v2.1.206.txt": None,
    "inputbox_at_overlay_v2.1.207.txt": "completion_overlay",
    "inputbox_bashmode_draft_v2.1.207.txt": None,
    "inputbox_bashmode_empty_v2.1.207.txt": None,
    "inputbox_bgshell_v2.1.207.txt": None,
    "inputbox_busy_thinking_v2.1.207.txt": None,
    "inputbox_busy_tool_v2.1.207.txt": None,
    # GH #84 — a 2.1.247 capture of a 1709-byte payload typed as 512-byte CHUNKS.
    # Byte-capped chunks render as an ordinary LITERAL tall draft (never the
    # `[Pasted text]` collapse), so it is a plain deliverable box: nothing about
    # the predicate changed for GH #84, and this fixture is the pin for that.
    "inputbox_chunked_draft_v2.1.247.txt": None,
    "inputbox_draft_typed_v2.1.207.txt": None,
    "inputbox_idle_v2.1.207.txt": None,
    # GH #62: the 2.1.238 idle rig capture. Deliverable BEFORE the change too (a
    # normal 2-separator box, so `_is_status_row` is never consulted) — it is baked
    # here as the negative control for the tall-draft twin below.
    "inputbox_idle_v2.1.238.txt": None,
    "inputbox_manual_mode_v2.1.207.txt": None,
    "inputbox_multiline_draft_v2.1.207.txt": None,
    "inputbox_paste_collapsed_reverted_v2.1.207.txt": None,
    "inputbox_paste_collapsed_v2.1.207.txt": None,
    # r2 fold P3: the 2.1.209 paste-collapse twin is NOT flipped by GH #56 (it
    # classified deliverable pre-change — a normal 2-separator box whose status
    # bar is the paste hint), so it belongs in the baked map.
    "inputbox_paste_collapsed_v2.1.209.txt": None,
    "inputbox_slash_exact_clear_v2.1.207.txt": "completion_overlay",
    "inputbox_slash_overlay_v2.1.207.txt": "completion_overlay",
    "inputbox_slash_with_arg_v2.1.207.txt": None,
    # GH #62 — THE REGRESSION PIN, baked with its POST-fix classification. Before
    # the alphabet extension this real 2.1.238 capture classified `no_input_box`
    # (the `⏵⏵ auto mode on (shift+tab to cycle)` bar was outside the grammar), so
    # the tall-draft fallback fail-closed and the topic wedged. Its own explicit
    # flip test is `test_gh62_tall_draft_2_1_238_is_a_READY_input_box`.
    "inputbox_tall_draft_v2.1.238.txt": None,
    # GH #73 — the CC 2.1.246 rig captures, baked at their POST-fix values (the
    # GH #62 precedent). `inputbox_tall_draft_v2.1.246.txt` is the REGRESSION twin:
    # it classified `no_input_box` before the right-block split, because the `/rc`
    # Remote Control pill is right-ALIGNED on the status row and the segment
    # splitter handed the hint fullmatch a tail it could not consume. The flip is
    # additionally pinned by its own explicit test below.
    "inputbox_idle_v2.1.246.txt": None,
    "inputbox_paste_collapsed_v2.1.246.txt": None,
    "inputbox_rc_active_v2.1.246.txt": None,
    "inputbox_rc_connecting_v2.1.246.txt": None,
    # GH #81 — the first `-e` (ANSI-preserving) `/rc` capture in the corpus: CC
    # 2.1.251 wraps the pill in an OSC 8 hyperlink. This bottom-rows fixture keeps
    # its two-rule box, so it classifies `None` both before and after the OSC strip;
    # the leak's real victim is the exactly-one-separator fallback, flipped by
    # `test_gh81_osc_status_row.py`.
    "inputbox_rc_osc8_agents_v2.1.251.txt": None,
    "inputbox_tall_draft_v2.1.246.txt": None,
    "inputbox_tasklist_footer_v2.1.207.txt": None,
    "inputbox_tasks_mode_v2.1.207.txt": "tasks_mode",
    "inputbox_wrapped_draft_v2.1.207.txt": None,
    "overlay_cost_modal_v2.1.207.txt": "no_input_box",
    "permission_bash_v2.1.190.txt": "no_input_box",
    "permission_negative_prose_v2.1.190.txt": "no_input_box",
    "permission_webfetch_advance_v2.1.190.txt": None,
    "permission_webfetch_bgshells_v2.1.190.txt": "no_input_box",
    "permission_webfetch_v2.1.190.txt": "no_input_box",
    "permission_write_long_v2.1.190.txt": "no_input_box",
    "permission_write_long_visible_v2.1.190.txt": "no_input_box",
    "scrollback_full_with_live_auq_v2.1.206.txt": "prompt_row_is_option",
    "settings_select_model_v2.1.200.txt": "no_input_box",
    "settings_warning_v2170.txt": "no_input_box",
    "shell_after_esc_v2.1.207.txt": "no_input_box",
    "status_busy_160x50_v2.1.198.txt": None,
    "switch_model_live_v2.1.207.txt": "no_input_box",
    "trust_after_accept_t0_v2.1.207.txt": None,
    "trust_after_accept_t5_v2.1.207.txt": None,
    "trust_before_answer_v2.1.207.txt": "no_input_box",
    "unknown_blocking_confirm_switch_model_v2.1.197.txt": "no_input_box",
    "usage_overlay_live_v2.1.206.txt": "no_input_box",
    "workflow_dynamic_launch_v2.1.190.txt": "no_input_box",
    "workflow_dynamic_launch_visible_v2.1.190.txt": "no_input_box",
    "workflow_negative_prose_v2.1.190.txt": "no_input_box",
}


def test_existing_corpus_classifications_are_unchanged() -> None:
    for name, expected in _BASELINE_CLASSIFICATIONS.items():
        assert (FIXTURES / name).exists(), name  # baseline must not go stale
        assert tp.classify_input_box_failure(_pane(name)) == expected, name


def test_the_baked_baseline_covers_the_whole_fixture_directory() -> None:
    """SET EQUALITY between the fixture-directory listing and the baked dict
    (r1 fold P2): any future fixture landing in the directory without a baked
    classification fails HERE instead of being silently uncovered. The three
    tall-draft 2.1.209 GH #56 fixtures are the only exclusions — they are the
    fixtures this change deliberately FLIPS / newly-introduces with their own
    explicit pins above (`inputbox_paste_collapsed_v2.1.209.txt` is NOT flipped
    — it classified deliverable pre-change too — so it is BAKED, r2 fold P3).

    The two GH #62 2.1.238 captures are BAKED rather than excluded: their
    post-fix classification is the stable one this suite pins going forward, and
    the tall-draft twin additionally carries its own explicit flip test."""
    gh56_fixtures = {
        _TALL_DRAFT,
        _TALL_DRAFT_ANSI,
        _TALL_DRAFT_CLEARED,
    }
    # GH #60: the fully-dim ghost fixtures + a fresh normal-intensity draft —
    # newly introduced and deliberately FLIPPED (or pinned) in
    # test_gh60_ghost_delivery_gate.py, so they are excluded here like the GH #56
    # set rather than baked into the "unchanged corpus" baseline above.
    gh60_fixtures = {
        "inputbox_ghost_prose_v2.1.215.ansi.txt",
        "inputbox_ghost_slash_clear_synthetic_v2.1.215.ansi.txt",
        "inputbox_ghost_at_word_synthetic_v2.1.215.ansi.txt",
        "inputbox_ghost_numbered_synthetic_v2.1.215.ansi.txt",
        "inputbox_real_draft_v2.1.217.ansi.txt",
    }
    on_disk = {p.name for p in FIXTURES.glob("*.txt")}
    assert on_disk == set(_BASELINE_CLASSIFICATIONS) | gh56_fixtures | gh60_fixtures


# ── GH #56 r1 fold (Codex P1): the strict segment-FULLMATCH status-row grammar ──
#
# The original `_is_status_row` was SUBTRACTIVE (strip marker substrings, reject
# only ALPHABETIC residue) — `❯ /effort?` passed it (marker stripped, residue
# `❯ ?` non-alphabetic) and, with an older reachable rule + a stale `❯` row
# above, produced a FULL gate bypass on a live-blocking-shaped pane; the empty
# stale-`❯` variant even allowed a KEYLESS brake release. All three reproduced
# RED against the subtractive grammar; the anchored fullmatch grammar refuses
# each (every `·`-segment must BE a canonical chrome form — no leading/trailing
# residue is ever accepted).


def _lone_sep_pane(status_row: str, *, stale_rows: list[str] | None = None) -> str:
    """A pane with exactly ONE separator in the 20-line window: an older rule +
    stale rows above, >18 draft/blank rows, the lone separator, then the spoof
    "status" row below it."""
    rule = "─" * 40
    pad = "\n".join(f"  filler line {i}" for i in range(6))
    body = "\n".join(
        stale_rows
        if stale_rows is not None
        else ["❯ a stale draft above"] + [f"  draft cont {i}" for i in range(18)]
    )
    return f"{pad}\n{rule}\n{body}\n{rule}\n  {status_row}\n"


def test_spoof_effort_marker_row_below_lone_separator_STILL_refuses() -> None:
    """Codex r1-fold repro (i): `❯ /effort?` passed the SUBTRACTIVE grammar and
    `classify_input_box_failure` returned None on a live-blocking-shaped pane —
    a full gate bypass. The fullmatch grammar rejects the segment outright."""
    assert tp._is_status_row("❯ /effort?") is False
    pane = _lone_sep_pane("❯ /effort?")
    assert tp.classify_input_box_failure(pane) is not None
    assert tp.pane_input_box_present(pane) is False


def test_spoof_shell_token_row_below_lone_separator_STILL_refuses() -> None:
    """Codex r1-fold repro (ii): the shell arm was equally weak — `❯ 1 shell?`
    passed the subtractive grammar (glyph + trailing `?` residue tolerated). The
    fullmatch form `\\d+ shells?(…)` accepts no residue on either side."""
    assert tp._is_status_row("❯ 1 shell?") is False
    assert tp._is_status_row("1 shell?") is False
    pane = _lone_sep_pane("❯ 1 shell?")
    assert tp.classify_input_box_failure(pane) is not None
    assert tp.pane_input_box_present(pane) is False


def test_spoof_empty_stale_prompt_row_never_releases_the_brake() -> None:
    """Codex r1-fold repro (iii): with a stale EMPTY `❯` row under the older
    rule, the subtractive grammar's fake bracket made `pane_input_row_empty`
    return True — a KEYLESS brake release on a spoofed surface. Post-fix the
    fallback never fires, so the probe stays indeterminate (None), never True."""
    rule = "─" * 40
    pane = "  filler\n" + rule + "\n❯\n" + ("\n" * 20) + rule + "\n  ❯ /effort?\n"
    assert tp.pane_input_row_empty(pane) is not True
    assert tp.classify_input_box_failure(pane) == "no_input_box"


def test_the_grammar_accepts_the_real_status_rows() -> None:
    """The grammar must accept every REAL captured status-row shape the fallback
    relies on (incl. the tall-draft fixture's own row)."""
    for row in (
        "⏸ manual mode on",  # the tall-draft fixture's only status row
        "⏸ manual mode on · ? for shortcuts · ← for agents",  # the cleared twin
        "⏵⏵ bypass permissions on (shift+tab to cycle)",
        "⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents",
        "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
        "⏵⏵ bypass permissions on · 1 shell",
        "⏵⏵ bypass permissions on · 2 shells · ← for agents · ↓ to manage",
        "? for shortcuts · ← for agents",
        "esc to interrupt · ← for agents",
        "paste again to expand",
        "! for shell mode",
    ):
        assert tp._is_status_row(row) is True, row


# ── GH #56 r5 fold: the CANONICAL GRAMMAR — sound against recombination,
#    COMPLETE against the real panes ──────────────────────────────────────────
#
# r4's literal ENUMERATION was sound but TOO NARROW: sampling the owner's three
# LIVE bot panes (2.1.208/2.1.209) surfaced `ctrl+t to hide tasks` — a hint the
# fixture corpus does not contain — so the fallback fail-closed EXACTLY on the
# busy/tasks panes where the owner's reply-quoted messages actually wedge.
# Enumeration had mistaken "what our fixtures hold" for "what CC renders".

_LIVE_BOT_ROWS = [
    # Sampled from the running bot's panes, 2026-07-14 (real 2.1.208/2.1.209
    # sessions; status row = first non-blank row below the bottom rule).
    "⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt "
    "· ctrl+t to hide tasks · ← for agents",
    "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
    "⏵⏵ bypass permissions on · 1 shell · ← for agents · ↓ to manage",
    # GH #62 — the SAME completeness lesson, one CC minor later. Sampled from the
    # owner's live panes 2026-08-21 on real CC 2.1.238 (8 DISTINCT bars across
    # four sessions, four samples each). FIVE of the eight were REFUSED by the
    # 2.1.209-pinned grammar: `auto mode on` was not a mode text at all, `PR #309`
    # and `1 shell, 1 monitor` were not slots, and a bare `1 monitor` had no form
    # (the other three bypass-permissions rows were already accepted). Two of the
    # refused bars wedged real topics that day.
    "⏵⏵ auto mode on (shift+tab to cycle)",
    "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    "⏵⏵ bypass permissions on (shift+tab to cycle) · PR #309 · ← for agents "
    "· ↓ to manage",
    "⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt "
    "· ← for agents · ↓ to manage",
    "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents · ↓ to manage",
    "⏵⏵ bypass permissions on · 1 monitor · ← for agents · ↓ to manage",
    "⏵⏵ bypass permissions on · 1 shell, 1 monitor · ← for agents · ↓ to manage",
]

# The DISTINCT real status rows the non-circular corpus sweep derives (the
# >=2-separator deliverable fixtures — see the sweep test below).
_REAL_CORPUS_STATUS_ROWS = [
    "! for shell mode",
    "? for shortcuts · ← for agents",
    "esc to interrupt · ← for agents",
    "paste again to expand",
    "⏵⏵ bypass permissions on (shift+tab to cycle)",
    "⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents",
    "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
    "⏵⏵ bypass permissions on · 1 shell",
    "⏵⏵ bypass permissions on · 1 shell · ← for agents · ↓ to manage",
    "⏸ manual mode on",
    "⏸ manual mode on · ? for shortcuts · ← for agents",
]


def _status_row_of(name: str) -> str:
    """The first non-blank row below a fixture's BOTTOM rule — its status bar."""
    lines = tp._strip_ansi(_pane(name)).split("\n")
    seps = [i for i, line in enumerate(lines) if tp._is_rule_separator(line)]
    assert seps, name
    return next(line.strip() for line in lines[seps[-1] + 1 :] if line.strip())


# GH #73 — the CC 2.1.246 rows carry the right-ALIGNED `/rc` pill, whose padding
# run is capture-exact (tens of spaces), so they are read FROM the fixtures rather
# than re-typed as literals.
_REAL_CORPUS_STATUS_ROWS_246 = [
    _status_row_of(name)
    for name in (
        "inputbox_idle_v2.1.246.txt",
        "inputbox_paste_collapsed_v2.1.246.txt",
        "inputbox_rc_active_v2.1.246.txt",
        "inputbox_rc_connecting_v2.1.246.txt",
        "inputbox_tall_draft_v2.1.246.txt",
    )
]


@pytest.mark.parametrize("row", _LIVE_BOT_ROWS)
def test_the_live_bot_rows_are_accepted(row: str) -> None:
    """THE COMPLETENESS PIN (provenance: sampled from the running bot's panes —
    2026-07-14 on CC 2.1.208/2.1.209, 2026-08-21 on CC 2.1.238). The 2.1.209 row 1
    carries `ctrl+t to hide tasks`, which NO fixture contains; the 2.1.238 rows
    carry `auto mode on`, `PR #309` and `1 shell, 1 monitor`. Each generation of
    this list contains rows the grammar of the generation before it REFUSED
    (5 of the 8 sampled 2.1.238 bars), fail-closing the tall-draft fallback on
    the owner's busiest windows — which is exactly why this pin is LIVE-sampled
    and not fixture-derived (GH #56 r5, GH #62)."""
    assert tp._is_status_row(row) is True, row
    # And it works end-to-end: a tall draft under this status bar delivers.
    pane = _lone_sep_pane(row)
    assert tp.classify_input_box_failure(pane) is None, row
    assert tp.pane_input_box_present(pane) is True, row


@pytest.mark.parametrize(
    "row",
    [
        # A hint repeated — at-most-once membership.
        "⏵⏵ bypass permissions on · ← for agents · ← for agents",
        "esc to interrupt · esc to interrupt",
        # A MODE after a HINT — the ordered structure (mode is the head or absent).
        "← for agents · ⏸ manual mode on",
        # TWO modes.
        "⏸ manual mode on · ⏵⏵ bypass permissions on",
        "⏸ manual mode on · ⏸ manual mode on",
        # A mode combined with an EXCLUSIVE standalone form — structurally
        # impossible: the exclusive forms are whole ROWS, never segments.
        "⏸ manual mode on · paste again to expand",
        "⏸ manual mode on · ! for shell mode",
        "paste again to expand · ← for agents",
        # A bare shell / effort is not a status bar.
        "1 shell",
        "/effort",
        # Unknown text in any segment.
        "⏸ manual mode on · surprise text",
    ],
)
def test_grammar_edges_refuse_through_the_full_predicates(row: str) -> None:
    """The grammar's own edges — repeats, ordering, mode/exclusive combination,
    bare tokens, unknown text — all refuse, driven END-TO-END (the gate refuses the
    pane and the stale-empty-`❯` geometry yields no keyless brake release)."""
    assert tp._is_status_row(row) is False, row
    pane = _lone_sep_pane(row)
    assert tp.classify_input_box_failure(pane) is not None, row
    assert tp.pane_input_box_present(pane) is False, row
    rule = "─" * 40
    empty_pane = "  filler\n" + rule + "\n❯\n" + ("\n" * 20) + rule + f"\n  {row}\n"
    assert tp.pane_input_row_empty(empty_pane) is not True, row


def test_the_hint_tail_is_deliberately_ORDER_FREE() -> None:
    """DISCLOSED: the corpus + the three live rows pin `esc → ctrl+t → ← → ↓` and
    `? → ←`, but NO observed row contains BOTH `? for shortcuts` and `esc to
    interrupt`, so their relative order cannot be established without GUESSING.
    Order-freedom adds no unsoundness — a valid bar's hints are all valid hints,
    and REPEATS + UNKNOWN text are still rejected (pinned above)."""
    assert tp._is_status_row("⏸ manual mode on · ← for agents · ? for shortcuts")
    assert tp._is_status_row("⏸ manual mode on · ? for shortcuts · ← for agents")
    # …but a repeat still refuses, which is what keeps order-freedom sound.
    assert (
        tp._is_status_row("⏸ manual mode on · ? for shortcuts · ? for shortcuts")
        is False
    )


# ── GH #56 r2 fold (Codex P1): the ENUMERATED whitelist — no empty segments,
#    no glyph×marker×parenthetical cross-product ─────────────────────────────
#
# The r1 fullmatch grammar was still gameable at two edges: EMPTY `·` segments
# were SKIPPED, so `· /effort ·` reduced to the single valid segment `/effort`
# (Codex drove the full tall-fallback geometry: classify None +
# pane_input_box_present True + a keyless brake release on the stale-empty-`❯`
# variant); and the generative prefix/parenthetical combination accepted
# impossible forms like `/effort (manual mode on)` and `⏵◐⏸/effort`. All three
# reproduced RED against the r1 grammar; the enumerated whitelist refuses each.


def test_spoof_empty_dot_segments_STILL_refuse_full_geometry() -> None:
    """Codex r2 repro (a): `· /effort ·` — empty segments must REJECT the row,
    not be skipped. Driven through the FULL fallback geometry: the gate refuses
    AND the stale-empty-`❯` variant never yields a keyless brake release."""
    assert tp._is_status_row("· /effort ·") is False
    assert tp._is_status_row("·") is False
    assert tp._is_status_row("· ·") is False
    # Full geometry — the gate bypass shape (stale draft above, lone in-window
    # separator, the spoof row below it).
    pane = _lone_sep_pane("· /effort ·")
    assert tp.classify_input_box_failure(pane) is not None
    assert tp.pane_input_box_present(pane) is False
    # The keyless-brake-release shape: a stale EMPTY `❯` under the older rule.
    rule = "─" * 40
    empty_pane = (
        "  filler\n" + rule + "\n❯\n" + ("\n" * 20) + rule + "\n  · /effort ·\n"
    )
    assert tp.pane_input_row_empty(empty_pane) is not True
    assert tp.classify_input_box_failure(empty_pane) == "no_input_box"


def test_spoof_marker_wrapped_in_another_markers_parenthetical_refuses() -> None:
    """Codex r2 repro (b1): the cross-product accepted `/effort (manual mode
    on)` — a marker must never validate wrapped in another marker's
    parenthetical. Only the exact observed decorated forms are whitelisted."""
    assert tp._is_status_row("/effort (manual mode on)") is False


def test_spoof_glyph_soup_prefix_refuses() -> None:
    """Codex r2 repro (b2): the cross-product accepted `⏵◐⏸/effort` — a marker
    must never validate behind an arbitrary glyph run. Each whitelist form
    carries exactly the decoration it renders with on real panes."""
    assert tp._is_status_row("⏵◐⏸/effort") is False


# One REPRESENTATIVE string per regex-CLASS grammar member (GH #62). The regex
# members can't be enumerated, so the lockstep is pinned on a representative of
# each: a new marker landing in leg 3 that no representative covers fails direction
# (a), and every representative leg 3 does NOT carry shows up in the explicit
# `not_in_leg3` set below.
_GRAMMAR_REGEX_CLASS_REPRESENTATIVES = {
    # `_RE_STATUS_TASKS` — one per composer family (`F4t`).
    "1 shell, 1 monitor",
    "2 monitors",
    "2 shells still running",
    "1 team",
    "3 local agents",
    "◆ ultraplan ready",
    "◇ ultraplan needs your input",
    "◇ ultraplan",
    "◇ 2 remote dynamic workflows",
    "◇ 1 cloud session",
    "1 background dynamic workflow",
    "4 Artifact comment monitors",
    "1 MCP task",
    "2 MCP jobs",
    "7 background tasks",
    "dreaming",
    "auto-mode scan",
    # the tasks component's own coupled suffix
    "↓ to view",
    # `_RE_STATUS_PRLINK` / `_RE_STATUS_MEMORIES`
    "PR #309",
    "MR !12",
    "12 memories recalled",
    # `_RE_STATUS_AGENTS` / `_RE_STATUS_DRAFTS` (hint-tail members)
    "← for agents",
    "← 3 agents",
    "← 99+ done",
    "1 feedback draft",
    # `_RE_STATUS_RIGHT_BLOCK` — CC 2.1.246's right-ALIGNED footer child (GH #73).
    # It is NOT a `·` segment: it is split off the row before the grammar runs, so
    # the representative is listed here purely to keep the lockstep an honest
    # superset. It can never satisfy acceptance alone (see the `not_in_leg3`
    # comment below).
    "/rc active",
}


def _grammar_vocabulary() -> set[str]:
    """Every literal token the canonical grammar can consume, plus one
    representative per regex-class member."""
    return (
        set(tp._STATUS_ROW_EXCLUSIVE)
        | set(tp._STATUS_ROW_HINTS)
        | {tp._STATUS_EFFORT_TAIL}
        | {
            "bypass permissions on",
            "accept edits on",
            "plan mode on",
            "manual mode on",
            # GH #62 (CC 2.1.238 `SUu` table)
            "auto mode on",
            "don't ask on",
        }
        | {"shift+tab to cycle"}
        | _GRAMMAR_REGEX_CLASS_REPRESENTATIVES
    )


def test_the_grammar_and_leg3_alphabet_stay_in_lockstep() -> None:
    """SINGLE SOURCE, both directions pinned so neither can drift silently.

    (a) Every leg-3 marker is part of the grammar's vocabulary — a marker added to
        `_INPUT_READY_CHROME_MARKERS` without deciding its grammar membership fails
        HERE. (Under r4's enumeration three markers were uncovered; the canonical
        grammar now covers them all.)

    (b) The grammar tokens that leg 3's alphabet does NOT carry are an EXPLICIT,
        pinned set. That divergence is FAIL-CLOSED, not a hazard: a row made only
        of such tokens lets the fallback LOCATE the box, and leg 3 then refuses the
        pane as `no_ready_chrome` — a refusal, never a wrong commit. It is
        asserted BEHAVIOURALLY in
        `test_a_grammar_only_singleton_row_still_refuses_fail_closed`, so the
        divergence is measured rather than assumed away. (In practice these tokens
        appear alongside a mode bar, which leg 3 does carry — e.g. the live
        `… · ctrl+t to hide tasks · ← for agents` row.)

        NOTE the two lanes are deliberately DIFFERENT SHAPES: this direction
        compares against the substring marker TUPLE only, while leg 3 also accepts
        `_RE_INPUT_READY_TASK_TOKEN` (`· <n> shells?/monitors?`). So several tasks
        representatives listed here are in fact leg-3-reachable inside a real bar;
        listing them keeps the set an honest superset of the grammar-only tokens.
    """
    vocab = _grammar_vocabulary()
    uncovered_markers = {
        m for m in tp._INPUT_READY_CHROME_MARKERS if not any(m in v for v in vocab)
    }
    assert uncovered_markers == set()

    not_in_leg3 = {
        v for v in vocab if not any(m in v for m in tp._INPUT_READY_CHROME_MARKERS)
    }
    assert not_in_leg3 == {
        # GH #56
        "ctrl+t to hide tasks",
        "ctrl+t to show tasks",
        "Enter to view tasks",
        # GH #62 — the new hint-tail literals …
        "esc to return to team lead",
        "/tasks to see subagents",
        "/diff to hide diff",
        "Enter to view memories",
        "ctrl+c to copy",
        "gh auth login for PR status",
        "install gh for PR status",
        # … the new whole-row exclusive form …
        "Pasting…",
        # … the typed slots (PR link, memories) …
        "PR #309",
        "MR !12",
        "12 memories recalled",
        # … the tasks families and the coupled view hint …
        "1 shell, 1 monitor",
        "2 monitors",
        "2 shells still running",
        "1 team",
        "3 local agents",
        "◆ ultraplan ready",
        "◇ ultraplan needs your input",
        "◇ ultraplan",
        "◇ 2 remote dynamic workflows",
        "◇ 1 cloud session",
        "1 background dynamic workflow",
        "4 Artifact comment monitors",
        "1 MCP task",
        "2 MCP jobs",
        "7 background tasks",
        "dreaming",
        "auto-mode scan",
        "↓ to view",
        # … and the counted hint-tail members (`← for agents` IS in leg 3).
        "← 3 agents",
        "← 99+ done",
        "1 feedback draft",
        # GH #73 — the right-ALIGNED `/rc` pill. Leg 3 does NOT carry it, and that
        # divergence is WEAKER than the ones above: the pill is a typed slot that
        # never satisfies acceptance on its own, so a row made only of it is
        # refused by the GRAMMAR too (pinned in
        # `test_gh73_a_right_block_only_row_is_not_a_status_bar`) and can never
        # reach the leg-3 refusal at all.
        "/rc active",
    }


def test_every_REAL_status_row_the_grammar_accepts_also_satisfies_leg3() -> None:
    """The property that actually holds, asserted BEHAVIOURALLY on the REAL rows.

    GH #62 RETRACTS the stronger claim this test used to carry ("every bar the
    grammar accepts satisfies leg 3"): it is FALSE, and was already false before —
    a row built only of grammar-only vocabulary (`ctrl+t to hide tasks`, and after
    GH #62 also `/diff to hide diff`, `ctrl+c to copy`, a bare `1 feedback
    draft`, …) is accepted by the grammar and rejected by leg 3.

    The shipped contract is the FAIL-CLOSED DIVERGENCE, unchanged: such a row lets
    the fallback LOCATE the box and leg 3 then refuses the pane as
    `no_ready_chrome` — a refusal, never a wrong commit (pinned in
    `test_a_grammar_only_singleton_row_still_refuses_fail_closed`).

    What this test pins is the load-bearing part: every row we have actually
    OBSERVED on a real pane carries a mode marker or a leg-3-covered hint, so the
    fallback and leg 3 agree on reality."""

    def _below_marker_ok(row: str) -> bool:
        return any(m in row for m in tp._INPUT_READY_CHROME_MARKERS) or bool(
            tp._RE_INPUT_READY_TASK_TOKEN.search("· " + row)
        )

    for row in _LIVE_BOT_ROWS + _REAL_CORPUS_STATUS_ROWS + _REAL_CORPUS_STATUS_ROWS_246:
        assert tp._is_status_row(row) is True, row
        assert _below_marker_ok(row), row


# ── GH #56 r3 fold (Codex P1, THIRD spoof family): the whole-row ORDERED
#    TEMPLATE — ends the segment-recombination class ─────────────────────────
#
# r1 (subtractive), r2 (per-segment fullmatch) and the r2 whitelist all validated
# segments INDEPENDENTLY, so ANY recombination of individually-valid segments
# passed: `/effort · /effort` (repeat), a doubled paste hint, TWO incompatible
# mode markers, and `١ shell` (`\d` is Unicode-wide). Each drove the FULL gate
# bypass + the keyless brake release. The terminal fix is the ordered slot
# machine (fixed order, at-most-once, ASCII digits, whole-row consumption) —
# per-segment validation was the wrong SHAPE, so this is an approach change, not
# a fourth edge patch.

_R3_SPOOF_ROWS = [
    "/effort · /effort",  # repeated segment
    "paste again to expand · paste again to expand",  # repeated hint
    "⏸ manual mode on · ⏵⏵ bypass permissions on",  # two incompatible modes
    "١ shell · /effort",  # Arabic-Indic digit + unpaired /effort
]


@pytest.mark.parametrize("row", _R3_SPOOF_ROWS)
def test_r3_recombination_spoofs_refuse_through_the_full_predicates(
    row: str,
) -> None:
    """All four r3 reproductions, driven end-to-end: the grammar refuses the
    row, the gate refuses the pane, and the stale-empty-`❯` geometry never
    yields a keyless brake release."""
    assert tp._is_status_row(row) is False
    pane = _lone_sep_pane(row)
    assert tp.classify_input_box_failure(pane) is not None
    assert tp.pane_input_box_present(pane) is False
    rule = "─" * 40
    empty_pane = "  filler\n" + rule + "\n❯\n" + ("\n" * 20) + rule + f"\n  {row}\n"
    assert tp.pane_input_row_empty(empty_pane) is not True
    assert tp.classify_input_box_failure(empty_pane) == "no_input_box"


def test_the_task_token_is_ascii_only() -> None:
    """`\\d` is UNICODE-wide — an Arabic-Indic `١ shell` must never read as the
    task-count token, in the template OR in leg 3's substring arm. GH #62 widened
    that arm to monitors, so both nouns are pinned."""
    assert tp._is_status_row("١ shell") is False
    assert tp._is_status_row("⏵⏵ bypass permissions on · 1 shell") is True
    assert tp._RE_INPUT_READY_TASK_TOKEN.search("· ١ shell") is None
    assert tp._RE_INPUT_READY_TASK_TOKEN.search("· 1 shell") is not None
    assert tp._RE_INPUT_READY_TASK_TOKEN.search("· ١ monitor") is None
    assert tp._RE_INPUT_READY_TASK_TOKEN.search("· 1 monitor") is not None
    assert tp._RE_INPUT_READY_TASK_TOKEN.search("· 2 monitors") is not None


# ── GH #56 r4 fold (Codex P1, FIFTH round of the same class): WHOLE-ROW
#    ENUMERATION — the terminal end of the recombination class ───────────────
#
# r3's slot machine STILL let mutually exclusive slots coexist
# (`⏸ manual mode on · paste again to expand` passed, though the paste hint
# REPLACES the whole status bar): ordering + at-most-once does not imply
# COMPATIBILITY. And it normalized Unicode spaces, so an NBSP variant of a real
# row passed. ANY per-part predicate over segments/slots is unsoundable here —
# so the grammar is now an ENUMERATION of COMPLETE rows. Mutually exclusive
# shapes cannot combine BY CONSTRUCTION: no template contains both.


def test_spoof_mutually_exclusive_shapes_cannot_combine() -> None:
    """Codex r4 repro (a): the paste hint REPLACES the status bar, so it can
    never appear alongside a mode bar. Under whole-row enumeration there simply
    IS no template containing both — driven through the FULL predicates."""
    row = "⏸ manual mode on · paste again to expand"
    assert tp._is_status_row(row) is False
    pane = _lone_sep_pane(row)
    assert tp.classify_input_box_failure(pane) is not None
    assert tp.pane_input_box_present(pane) is False
    rule = "─" * 40
    empty_pane = "  filler\n" + rule + "\n❯\n" + ("\n" * 20) + rule + f"\n  {row}\n"
    assert tp.pane_input_row_empty(empty_pane) is not True
    assert tp.classify_input_box_failure(empty_pane) == "no_input_box"


@pytest.mark.parametrize(
    "row",
    [
        "⏸\xa0manual mode on",  # NBSP inside a real mode row
        "paste\xa0again to expand",  # NBSP inside the paste hint
        "? for shortcuts\xa0· ← for agents",  # NBSP around the separator
    ],
)
def test_spoof_nbsp_variants_of_real_rows_refuse(row: str) -> None:
    """Codex r4 repro (b): the chrome region is explicitly OUTSIDE
    `_normalize_input_row`'s contract (which is scoped to the rows INSIDE the
    input-box bracket), so the templates match ASCII space ONLY and an NBSP
    variant of a real row REFUSES. Driven through the FULL predicates."""
    assert tp._is_status_row(row) is False
    pane = _lone_sep_pane(row)
    assert tp.classify_input_box_failure(pane) is not None
    assert tp.pane_input_box_present(pane) is False
    rule = "─" * 40
    empty_pane = "  filler\n" + rule + "\n❯\n" + ("\n" * 20) + rule + f"\n  {row}\n"
    assert tp.pane_input_row_empty(empty_pane) is not True


def test_no_real_corpus_chrome_row_carries_a_unicode_space() -> None:
    """The ASCII-only rule is corpus-SAFE, not just strict: the NBSP CC emits
    lives in the INPUT row (`❯\\xa0`), never in a status bar."""
    unicode_spaces = ("\xa0", " ", " ", "﻿")
    for name, expected in _BASELINE_CLASSIFICATIONS.items():
        if expected is not None:
            continue  # only deliverable panes have a status bar below the box
        lines = tp._strip_ansi(_pane(name)).split("\n")
        located = tp._input_box_rows(lines)
        if located is None:
            continue
        _top, bottom, _rows = located
        for i in range(bottom + 1, len(lines)):
            assert not any(ch in lines[i] for ch in unicode_spaces), (name, i)


def test_the_exclusive_forms_are_rows_not_segments() -> None:
    """The STRUCTURAL property, asserted on the grammar itself (r5): the paste hint
    and the bash-mode indicator REPLACE the whole status bar, so they are modelled
    as WHOLE-ROW alternatives — never as segments a bar can also carry. Mutual
    exclusion is therefore unrepresentable, not a rule to enforce."""
    # They are exclusive-row forms … (GH #62 adds CC 2.1.238's `Pasting…`, which
    # is an early `return` in the footer component exactly like the other two).
    assert tp._STATUS_ROW_EXCLUSIVE == {
        "paste again to expand",
        "! for shell mode",
        "Pasting…",
    }
    # … and they are NOT reachable as a segment of a composed BAR.
    assert not (tp._STATUS_ROW_EXCLUSIVE & tp._STATUS_ROW_HINTS)
    for exclusive in tp._STATUS_ROW_EXCLUSIVE:
        assert tp._RE_STATUS_MODE.fullmatch(exclusive) is None, exclusive
        assert tp._is_status_row(exclusive) is True, exclusive
        # A bar can never absorb one.
        assert tp._is_status_row(f"⏸ manual mode on · {exclusive}") is False
        assert tp._is_status_row(f"{exclusive} · ← for agents") is False


def test_every_real_corpus_status_row_matches_a_template() -> None:
    """THE CORPUS IS THE AUTHORITY, and the sweep is NON-CIRCULAR (r4 P2).

    The r3 sweep filtered fixtures with `classify_input_box_failure`, which on
    the ONE-separator path calls `_is_status_row` — so a too-narrow template
    just SKIPPED the fixture and the loose count still passed. Here the sweep is
    restricted to fixtures with >=2 separators in the bottom scan window: that
    is exactly the path `_input_box_rows` resolves WITHOUT ever consulting
    `_is_status_row`, so neither the fixture selection nor the extracted row
    depends on the predicate under test. Every real status bar so derived must
    FULLMATCH a template — a too-narrow enumeration fails LOUDLY here instead of
    silently fail-closing panes.
    """
    checked: list[str] = []
    for path in sorted(FIXTURES.glob("*.txt")):
        text = path.read_text()
        lines = tp._strip_ansi(text).split("\n")
        start = max(0, len(lines) - tp._CHROME_SCAN_LINES)
        seps = [i for i in range(start, len(lines)) if tp._is_rule_separator(lines[i])]
        if len(seps) < 2:
            continue  # the fallback path — would be circular
        # On this path classify_* never consults _is_status_row, so using it to
        # select the READY (status-bar-bearing) panes is not circular.
        if tp.classify_input_box_failure(text) is not None:
            continue
        first_below = next(
            (
                lines[i].strip()
                for i in range(seps[-1] + 1, len(lines))
                if lines[i].strip()
            ),
            None,
        )
        if first_below is None:
            continue
        checked.append(first_below)
        assert tp._is_status_row(first_below) is True, (path.name, first_below)
    # The sweep genuinely covered the corpus, across DISTINCT real shapes.
    assert len(checked) >= 30
    assert len(set(checked)) >= 8


def test_the_two_separator_path_never_consults_the_status_row_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The NON-CIRCULARITY of the sweep above, PROVEN rather than asserted: with
    `_is_status_row` replaced by a bomb, every >=2-separator fixture still
    classifies — so that path (and the sweep built on it) is independent of the
    predicate under test."""

    def _boom(_line: str) -> bool:
        raise AssertionError("_is_status_row must NOT be consulted on the >=2-sep path")

    monkeypatch.setattr(tp, "_is_status_row", _boom)
    checked = 0
    for path in sorted(FIXTURES.glob("*.txt")):
        text = path.read_text()
        lines = tp._strip_ansi(text).split("\n")
        start = max(0, len(lines) - tp._CHROME_SCAN_LINES)
        seps = [i for i in range(start, len(lines)) if tp._is_rule_separator(lines[i])]
        if len(seps) < 2:
            continue
        tp.classify_input_box_failure(text)  # must not raise
        checked += 1
    assert checked >= 40


# ── GH #56 r6 fold, COMPLETED by GH #62: mode GLYPHS are BOUND to their TEXT ──
#
# The r5 MODE segment cross-producted glyph × text, so it accepted pairings CC
# never renders (`⏸ bypass permissions on`, `⏵⏵ manual mode on`). Clear-eyed about
# the impact: this buys ~nothing in SAFETY (anyone who can print the mispaired
# glyph can equally print the correctly-paired one, which MUST be accepted), so the
# delta is ≈0 — it is a tightening for CORRECTNESS and reviewability, not a hazard
# fix. Nothing else about the grammar changed.
#
# r6 could only bind the two OBSERVED pairs and left `accept edits on` /
# `plan mode on` on EITHER glyph (guessing would have risked a false-refusal
# wedge). GH #62 reads the whole table off the CC 2.1.238 binary (`SUu`: mode ⇒
# indicator text + symbol constant), so every pair is now bound from a source, not
# from a fixture we happen to hold — and the two new modes (`auto`, `dontAsk`)
# arrive already bound.

_BOUND_MODE_PAIRS = [
    ("⏸", "manual mode on"),
    ("⏸", "plan mode on"),
    ("⏵⏵", "accept edits on"),
    ("⏵⏵", "bypass permissions on"),
    ("⏵⏵", "don't ask on"),
    ("⏵⏵", "auto mode on"),
]


@pytest.mark.parametrize(
    "row",
    [
        "⏸ bypass permissions on",  # bypass is ⏵⏵-only
        "⏸ bypass permissions on (shift+tab to cycle)",
        "⏵⏵ manual mode on",  # manual is ⏸-only
        "⏵⏵ manual mode on (shift+tab to cycle)",
        # …and a mispaired mode cannot be laundered by a valid hint tail.
        "⏸ bypass permissions on · ← for agents",
        # GH #62 — the two pairs r6 deliberately left loose are now BOUND, so
        # their cross-products refuse too.
        "⏸ accept edits on",
        "⏸ accept edits on (shift+tab to cycle)",
        "⏵⏵ plan mode on",
        "⏵⏵ plan mode on (shift+tab to cycle)",
        # …as do the two new modes on the wrong glyph.
        "⏸ auto mode on",
        "⏸ don't ask on",
    ],
)
def test_mispaired_mode_glyphs_refuse_through_the_full_predicates(row: str) -> None:
    """CC binds each mode text to one glyph — read off the 2.1.238 `SUu` table
    (`lMr` = U+23F8 `⏸` for `default`/`plan`, `Fdt` = U+23F5 U+23F5 `⏵⏵` for
    `acceptEdits`/`bypassPermissions`/`dontAsk`/`auto`). The cross-product
    pairings are refused."""
    assert tp._is_status_row(row) is False, row
    pane = _lone_sep_pane(row)
    assert tp.classify_input_box_failure(pane) is not None, row
    assert tp.pane_input_box_present(pane) is False, row
    rule = "─" * 40
    empty_pane = "  filler\n" + rule + "\n❯\n" + ("\n" * 20) + rule + f"\n  {row}\n"
    assert tp.pane_input_row_empty(empty_pane) is not True, row


@pytest.mark.parametrize(("glyph", "text"), _BOUND_MODE_PAIRS)
def test_correctly_paired_modes_are_still_accepted(glyph: str, text: str) -> None:
    """The binding must not cost completeness on ANY table pairing — the whole
    point of reading the table instead of guessing. Each pair is accepted bare,
    with the cycle parenthetical, and carrying a hint tail."""
    assert tp._is_status_row(f"{glyph} {text}") is True
    assert tp._is_status_row(f"{glyph} {text} (shift+tab to cycle)") is True
    assert tp._is_status_row(f"{glyph} {text} · ? for shortcuts · ← for agents") is True
    # Still AT MOST ONE mode segment per row.
    assert tp._is_status_row(f"{glyph} {text} · ⏸ manual mode on") is False


# ── GH #62: the CC 2.1.238 status-bar drift ──────────────────────────────────
#
# Two live incidents 2026-08-21 (windows @2/@3): a reply-quoted Telegram message
# rendered a TALL draft, the delivery gate's post-write re-verify took the GH #56
# exactly-one-separator fallback, and leg (a) refused because the pane's bar was
# `⏵⏵ auto mode on (shift+tab to cycle)` — a mode the 2.1.209-pinned grammar did
# not know. Enter withheld → stranded-draft brake → topic WEDGE.
#
# The alphabet is extended from the RENDERER, not from the fixtures: the 2.1.238
# mode table (`SUu`), the tasks composer (`F4t`), the tasks component (`BHs`) and
# the shared pluralizer `wt(n, sg, pl = sg + "s")` in the plaintext JS bundle at
# `~/.local/share/claude/versions/2.1.238`, cross-checked against 8 live bars and
# two isolated-rig captures. A count is bound singular/plural ONLY where that
# snippet PROVES the `n === 1` conditional (everything below), never guessed.

_TALL_DRAFT_238 = "inputbox_tall_draft_v2.1.238.txt"
_IDLE_238 = "inputbox_idle_v2.1.238.txt"


def test_gh62_tall_draft_2_1_238_is_a_READY_input_box() -> None:
    """THE REGRESSION PIN. This real 2.1.238 rig capture classified `no_input_box`
    before the alphabet extension — the exact shape that wedged two live topics.
    Post-fix it is a fully-ready box, and the brake's release proof reads the SAME
    rows: box FOUND, input row non-empty (False, never None)."""
    pane = _pane(_TALL_DRAFT_238)
    # The fixture genuinely has the tall shape — a >18-row draft inside the box,
    # so exactly ONE separator is in the 20-line window. Otherwise this is vacuous.
    lines = tp._strip_ansi(pane).split("\n")
    start = max(0, len(lines) - tp._CHROME_SCAN_LINES)
    seps = [i for i in range(start, len(lines)) if tp._is_rule_separator(lines[i])]
    assert len(seps) == 1
    assert "quoted line twelve" in tp._strip_ansi(pane)

    assert tp.pane_input_box_present(pane) is True
    assert tp.classify_input_box_failure(pane) is None
    assert tp.pane_input_row_empty(pane) is False


def test_gh62_the_idle_2_1_238_capture_is_the_negative_control() -> None:
    """The same session with an EMPTY box: a normal 2-separator pane (so the
    fallback never fires), deliverable, and its input row provably empty — the
    brake-release twin."""
    pane = _pane(_IDLE_238)
    assert tp.classify_input_box_failure(pane) is None
    assert tp.pane_input_box_present(pane) is True
    assert tp.pane_input_row_empty(pane) is True


def test_gh62_the_2_1_238_captures_need_no_rule_separator_change() -> None:
    """`_RE_RULE_SEPARATOR` is deliberately NOT extended (GH #62 §Design 7),
    pinned on the real captures: every input-box rule in both 2.1.238 fixtures
    already satisfies `_is_rule_separator`, so the box is located on the
    UNCHANGED regex.

    PROVENANCE NOTE: the 2.1.238 capture set carries PURE-dash rules only — no
    labelled (plan-slug / effort-titled) top rule was captured on this version.
    The labelled form therefore stays pinned by the 2.1.207 fixtures in
    `test_a_labeled_top_rule_is_still_an_input_box`, and this test asserts only
    what the 2.1.238 captures actually contain."""
    for name in (_IDLE_238, _TALL_DRAFT_238):
        lines = tp._strip_ansi(_pane(name)).split("\n")
        located = tp._input_box_rows(lines)
        assert located is not None, name
        top, bottom, _rows = located
        assert tp._is_rule_separator(lines[top]), name
        assert tp._is_rule_separator(lines[bottom]), name
        # Pure dashes on this version — the labelled arm is not exercised here.
        assert set(lines[top].strip()) == {"─"}, name
        assert set(lines[bottom].strip()) == {"─"}, name


# A mode-carrying prefix, so the typed-slot vectors below are tested as they
# actually render — the acceptance condition is UNCHANGED (`has_mode or seen`), so
# a row of ONLY typed slots is still not a status bar (pinned separately).
_M238 = "⏵⏵ auto mode on"


@pytest.mark.parametrize(
    "row",
    [
        # local_bash — `o === 1 ? "1 shell" : `${o} shells`` + the comma join with
        # the monitors half; monitors alone when there are no shells.
        f"{_M238} · 1 shell, 1 monitor",
        f"{_M238} · 2 shells, 3 monitors",
        f"{_M238} · 2 monitors",
        f"{_M238} · 1 monitor",
        # the LEGACY (<=2.1.217) suffix form, kept as a version-compat alternative
        f"{_M238} · 2 shells still running",
        f"{_M238} · 1 shell still running",
        # the footer PR/MR link (`hKl`: prefix + `#<n>` / `!<n>`)
        f"{_M238} · PR #309",
        f"{_M238} · MR !12",
        # the two modes CC 2.1.238 added
        "⏵⏵ auto mode on",
        "⏵⏵ don't ask on",
        "⏵⏵ auto mode on (shift+tab to cycle)",
        # the agents counter (`← for agents` / `← <n> agent(s)` / `← <n> done`)
        "← 3 agents",
        "← 1 agent",
        "← 99+ done",
        "← 99+ agents",
        # memories (`wt(n, "memory", "memories")`)
        f"{_M238} · 12 memories recalled",
        f"{_M238} · 1 memory recalled",
        # the remaining tasks families
        f"{_M238} · ◆ ultraplan ready",
        f"{_M238} · ◇ ultraplan needs your input",
        f"{_M238} · ◇ ultraplan",
        f"{_M238} · 1 team",
        f"{_M238} · 3 local agents",
        f"{_M238} · ◇ 2 remote dynamic workflows",
        f"{_M238} · ◇ 1 cloud session",
        f"{_M238} · 1 background dynamic workflow",
        f"{_M238} · 4 Artifact comment monitors",
        f"{_M238} · 1 MCP task",
        f"{_M238} · 2 MCP jobs",
        f"{_M238} · 7 background tasks",
        f"{_M238} · dreaming",
        f"{_M238} · auto-mode scan",
        # the whole-ROW paste transient
        "Pasting…",
        # the new hint-tail members, incl. the feedback-draft regex class
        "← for agents · 1 feedback draft",
        "← for agents · 3 feedback drafts",
        f"{_M238} · esc to return to team lead",
        f"{_M238} · /tasks to see subagents",
        f"{_M238} · /diff to hide diff",
        f"{_M238} · Enter to view memories",
        f"{_M238} · ctrl+c to copy",
        f"{_M238} · gh auth login for PR status",
        f"{_M238} · install gh for PR status",
    ],
)
def test_gh62_the_new_2_1_238_vocabulary_is_accepted(row: str) -> None:
    """Every form the extension adds, accepted — and accepted END-TO-END at the
    leg that actually wedged: the tall-draft fallback LOCATES the input box under
    this status bar instead of returning `None`.

    The pane's final classification is then either fully deliverable, or leg 3's
    `no_ready_chrome` for a row built only of grammar-only vocabulary — the
    FAIL-CLOSED divergence pinned in
    `test_a_grammar_only_singleton_row_still_refuses_fail_closed`. Nothing else
    may come back."""
    assert tp._is_status_row(row) is True, row
    pane = _lone_sep_pane(row)
    assert tp._input_box_rows(tp._strip_ansi(pane).split("\n")) is not None, row
    assert tp.classify_input_box_failure(pane) in (None, "no_ready_chrome"), row


@pytest.mark.parametrize(
    "row",
    [
        # RECOMBINATION across families — the composer is a SWITCH over the one
        # shared task type, so only the shell/monitor pair is ever comma-joined.
        f"{_M238} · 1 shell, 1 team",
        f"{_M238} · 1 shell, 2 shells",
        f"{_M238} · dreaming, 1 MCP task",
        f"{_M238} · ◆ ultraplan ready, auto-mode scan",
        f"{_M238} · 1 shell,, 1 monitor",
        f"{_M238} · 1 shell, and stuff",
        # the LEGACY suffix form never joins monitors (the two never co-render)
        f"{_M238} · 1 shell still running, 1 monitor",
        # the ultraplan phase GLYPHS are bound (◆ = plan_ready, ◇ = the rest)
        f"{_M238} · ◇ ultraplan ready",
        f"{_M238} · ◆ ultraplan needs your input",
        f"{_M238} · ◆ ultraplan",
        # COUNT SHAPES — bound wherever the `n === 1` conditional is proven
        f"{_M238} · 1 monitors",
        f"{_M238} · 2 monitor",
        f"{_M238} · 1 shells",
        f"{_M238} · 2 shell",
        f"{_M238} · 1 memories recalled",
        f"{_M238} · 2 memory recalled",
        f"{_M238} · 1 teams",
        f"{_M238} · 2 MCP job",
        "← for agents · 1 feedback drafts",
        "← for agents · 2 feedback draft",
        # PROSE / residue around a valid token
        f"{_M238} · PR # 309",
        f"{_M238} · PR #309 is ready to merge",
        f"{_M238} · MR #12",
        f"{_M238} · ← 3 agents are working",
        f"{_M238} · 12 memories",
        # REPEATS — the regex-class tail members are at-most-once too
        "← for agents · ← 3 agents",
        "← 3 agents · ← 99+ done",
        "← for agents · 1 feedback draft · 2 feedback drafts",
        # TYPED SLOTS ALONE are not a status bar (acceptance is UNCHANGED)
        "PR #309",
        "MR !12",
        "2 monitors",
        "1 shell, 1 monitor",
        "12 memories recalled",
        "1 background task",
        "◆ ultraplan ready",
        # UNICODE digits — `[0-9]`, never `\\d`, in every new numeric template
        f"{_M238} · ١ shell, ١ monitor",
        f"{_M238} · ٢ monitors",
        f"{_M238} · ١٢ memories recalled",
        f"{_M238} · PR #٣٠٩",
        "← ٣ agents",
        f"{_M238} · ١ feedback draft",
    ],
)
def test_gh62_recombinations_and_bad_count_shapes_refuse(row: str) -> None:
    """The r3 refusal family extended to the new vocabulary, driven END-TO-END:
    the grammar refuses the row, the gate refuses the pane, and the
    stale-empty-`❯` geometry never yields a keyless brake release."""
    assert tp._is_status_row(row) is False, row
    pane = _lone_sep_pane(row)
    assert tp.classify_input_box_failure(pane) is not None, row
    assert tp.pane_input_box_present(pane) is False, row
    rule = "─" * 40
    empty_pane = "  filler\n" + rule + "\n❯\n" + ("\n" * 20) + rule + f"\n  {row}\n"
    assert tp.pane_input_row_empty(empty_pane) is not True, row


# ── `↓ to view` is COUPLED to the tasks slot, not a free hint ────────────────
#
# The tasks component (`BHs`) emits `· ↓ to view` itself, immediately after the
# tasks text, and ONLY when `xCl` holds (the list is exactly one ultraplan remote
# agent). Modelling it as a free hint would let it float anywhere in the tail; it
# is consumed as ONE cursor step instead.


@pytest.mark.parametrize(
    "tasks",
    ["◆ ultraplan ready", "◇ ultraplan needs your input", "◇ ultraplan"],
)
def test_gh62_view_hint_is_accepted_immediately_after_an_ultraplan_slot(
    tasks: str,
) -> None:
    assert tp._is_status_row(f"{_M238} · {tasks} · ↓ to view") is True
    # …and it stays optional (the coupling never becomes a requirement).
    assert tp._is_status_row(f"{_M238} · {tasks}") is True
    # …and the tail still works after it.
    assert tp._is_status_row(f"{_M238} · {tasks} · ↓ to view · ← for agents") is True


@pytest.mark.parametrize(
    "row",
    [
        # standalone — never a free hint
        "↓ to view",
        f"{_M238} · ↓ to view",
        # after a NON-ultraplan tasks slot
        f"{_M238} · 1 shell · ↓ to view",
        f"{_M238} · 7 background tasks · ↓ to view",
        # not IMMEDIATELY after the ultraplan slot
        f"{_M238} · ◇ ultraplan · ← for agents · ↓ to view",
        # repeated
        f"{_M238} · ◇ ultraplan · ↓ to view · ↓ to view",
    ],
)
def test_gh62_view_hint_refuses_anywhere_else(row: str) -> None:
    assert tp._is_status_row(row) is False, row
    pane = _lone_sep_pane(row)
    assert tp.pane_input_box_present(pane) is False, row


# ── The grammar ↔ leg-3 divergence is FAIL-CLOSED (r2-P2-1 fold) ─────────────
#
# GH #56's v2 claim "every accepted bar satisfies leg 3" was FALSE and is
# RETRACTED. Rows built only of grammar-only vocabulary ARE accepted by the
# grammar and are NOT covered by leg 3's alphabet. The shipped contract is that
# this divergence is a REFUSAL, never a wrong commit — asserted here rather than
# assumed away.


@pytest.mark.parametrize(
    "row",
    [
        "ctrl+t to hide tasks",  # pre-existing (GH #56)
        "/diff to hide diff",
        "ctrl+c to copy",
        "Enter to view memories",
        "/tasks to see subagents",
        "esc to return to team lead",
        "gh auth login for PR status",
        "1 feedback draft",  # the bare regex-class member
        "← 3 agents",  # the counted agents form (`← for agents` IS in leg 3)
        "Pasting…",  # the new whole-row exclusive form
    ],
)
def test_a_grammar_only_singleton_row_still_refuses_fail_closed(row: str) -> None:
    """The grammar ACCEPTS the row (so the fallback locates the box) and leg 3 then
    refuses the pane as `no_ready_chrome` — an INDETERMINATE reason, so the
    delivery gate retries the capture and then refuses. Fail-closed: a refusal,
    never a wrong commit.

    NOT asserted here, deliberately: `pane_input_row_empty` is a DIFFERENT
    predicate (the stranded-draft brake's release proof) and never consults leg 3
    — on such a pane it correctly reports the box as located and empty, which is
    the right answer, not a spoof. The r1-r4 spoof tests assert the no-release
    property because there the grammar REFUSES the row outright."""
    assert tp._is_status_row(row) is True, row
    pane = _lone_sep_pane(row)
    assert tp.classify_input_box_failure(pane) == "no_ready_chrome", row
    assert "no_ready_chrome" in tp.INPUT_BOX_INDETERMINATE_REASONS
    assert tp.pane_input_box_present(pane) is False, row


# ── GH #73: the CC 2.1.246 RIGHT-ALIGNED `/rc` status-bar element ─────────────
#
# CC 2.1.246 shipped Remote Control, whose `/rc` pill is the first footer element
# that is NOT part of the `·`-joined hint line: the footer container is a ROW flex
# box whose second child (`MY`) carries `marginLeft: "auto"`, so the pill lands
# right-ALIGNED on the SAME physical row, separated by a run of padding spaces. A
# tmux capture therefore reads
#
#   `  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents   …   /rc`
#
# and the segment splitter handed `← for agents   …   /rc` to the hint fullmatch,
# which refused. Same consequence as GH #62: the GH #56 tall-draft fallback took
# leg (a) → `no_input_box` → Enter withheld → stranded-draft brake → topic WEDGE.
#
# The alphabet is read off the RENDERER, never guessed — the label function in the
# plaintext bundle at `~/.local/share/claude/versions/2.1.246` (@204455048) is
# exhaustive: `/rc failed` (error) | `/rc reconnecting` | `/rc active` |
# `/rc connecting…`, plus the `/rc active` ⇒ bare `/rc` abbreviation (@212974312)
# once the `rc-active-badge` notification has been seen 5 times. Both rig windows
# settled into the ABBREVIATED form, so it is the common shape, not an edge case.

_TALL_DRAFT_246 = "inputbox_tall_draft_v2.1.246.txt"
_IDLE_246 = "inputbox_idle_v2.1.246.txt"
_PASTE_COLLAPSED_246 = "inputbox_paste_collapsed_v2.1.246.txt"
_RC_CONNECTING_246 = "inputbox_rc_connecting_v2.1.246.txt"
_RC_ACTIVE_246 = "inputbox_rc_active_v2.1.246.txt"


def test_gh73_tall_draft_2_1_246_is_a_READY_input_box() -> None:
    """THE REGRESSION PIN. This real 2.1.246 rig capture classified `no_input_box`
    before the right-block split — the wedge shape. Post-fix it is a fully-ready
    box, and the brake's release proof reads the SAME rows: box FOUND, input row
    non-empty (False, never None)."""
    pane = _pane(_TALL_DRAFT_246)
    # Non-vacuity: the fixture genuinely has the tall shape (exactly ONE separator
    # in the 20-line window, so the fallback is the path under test) and its status
    # row genuinely carries the right-aligned pill.
    lines = tp._strip_ansi(pane).split("\n")
    start = max(0, len(lines) - tp._CHROME_SCAN_LINES)
    seps = [i for i in range(start, len(lines)) if tp._is_rule_separator(lines[i])]
    assert len(seps) == 1
    assert "> line fifteen of the quoted block" in tp._strip_ansi(pane)
    assert _status_row_of(_TALL_DRAFT_246).endswith(" /rc")

    assert tp.pane_input_box_present(pane) is True
    assert tp.classify_input_box_failure(pane) is None
    assert tp.pane_input_row_empty(pane) is False


def test_gh73_the_pre_fix_failing_row_now_passes() -> None:
    """The measured before/after, stated as the row rather than the pane: the bar
    ALONE was accepted and the same bar PLUS the right-aligned pill was refused."""
    bar = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
    assert tp._is_status_row(bar) is True
    for pill in ("/rc connecting…", "/rc active", "/rc"):
        assert tp._is_status_row(f"{bar}{' ' * 40}{pill}") is True, pill


def test_gh73_the_2_1_246_captures_are_the_negative_controls() -> None:
    """The same sessions with an EMPTY box: normal 2-separator panes (so the
    fallback never fires), deliverable, input row provably empty — and still IDLE,
    so `/update` and `/cost` keep working on a `/rc` pane."""
    for name in (_IDLE_246, _RC_CONNECTING_246, _RC_ACTIVE_246):
        pane = _pane(name)
        assert tp.classify_input_box_failure(pane) is None, name
        assert tp.pane_input_box_present(pane) is True, name
        assert tp.pane_input_row_empty(pane) is True, name
        assert tp.pane_looks_idle(tp.clean_ghost_input_text(pane)) is True, name


def test_gh73_the_captures_cover_all_three_OBSERVED_pill_states() -> None:
    """Non-vacuity for the alphabet: the fixtures hold `connecting…`, `active` and
    the bare abbreviation. `/rc reconnecting` and `/rc failed` are binary-derived
    only — they are not reachable on demand, so no synthetic fixture was built for
    them (they are still in the regex, from the renderer's own label function)."""
    assert _status_row_of(_RC_CONNECTING_246).endswith(" /rc connecting…")
    assert _status_row_of(_RC_ACTIVE_246).endswith(" /rc active")
    assert _status_row_of(_IDLE_246).endswith(" /rc")


def test_gh73_the_exclusive_row_co_renders_with_the_right_block() -> None:
    """WHY the split runs BEFORE the exclusive check, PANE-CONFIRMED rather than
    inferred: `Pasting…` / `paste again to expand` / `Press <key> again to exit`
    are early `return`s INSIDE the LEFT child (FooterHintLine), so the right block
    still renders beside them. This real capture holds exactly that row."""
    row = _status_row_of(_PASTE_COLLAPSED_246)
    assert row.startswith("paste again to expand")
    assert row.endswith(" /rc")
    assert tp._is_status_row(row) is True
    pane = _pane(_PASTE_COLLAPSED_246)
    assert "[Pasted text #1" in pane
    assert tp.pane_input_box_present(pane) is True
    # An UNCOMMITTED draft is still not idle, and the brake must not self-release.
    assert tp.pane_input_row_empty(pane) is False
    assert tp.pane_looks_idle(tp.clean_ghost_input_text(pane)) is False


_GH73_ACCEPTED_ROWS = [
    # the two verbose states + the abbreviation, on the real bar shape
    "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents      /rc",
    "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents      /rc active",
    "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents      /rc connecting…",
    # the two binary-derived states with no capture
    "⏸ manual mode on · ? for shortcuts      /rc reconnecting",
    "⏸ manual mode on · ? for shortcuts      /rc failed",
    # the pill's own keyboard-SELECTED suffix (`bridgeSelected` — derived from the
    # chord renderer, whose `<Key> to <action>` join and `Enter` label are proven
    # by two literals the grammar already accepts)
    "⏸ manual mode on · ? for shortcuts      /rc active · Enter to view",
    # an EXCLUSIVE row beside the right block (the captured shape)
    "paste again to expand      /rc",
    "Pasting…      /rc active",
    "! for shell mode      /rc",
    # a single-space gap (`columnGap: 1` — reachable when the bar nearly fills the
    # row) is a boundary too: the pill token is what proves the split point
    "⏸ manual mode on · ? for shortcuts /rc active",
]


@pytest.mark.parametrize("row", _GH73_ACCEPTED_ROWS)
def test_gh73_rows_carrying_the_right_block_are_accepted(row: str) -> None:
    assert tp._is_status_row(row) is True


_GH73_REFUSED_ROWS = [
    # UNKNOWN pill state — the alphabet is closed, not a `/rc <anything>` wildcard
    "⏸ manual mode on · ? for shortcuts      /rc bogus",
    "⏸ manual mode on · ? for shortcuts      /rc ACTIVE",
    "⏸ manual mode on · ? for shortcuts      /RC active",
    # unconsumed text AFTER the pill — the block is anchored at end-of-row
    "⏸ manual mode on · ? for shortcuts      /rc active extra",
    "⏸ manual mode on · ? for shortcuts      /rc active · Enter to view tasks",
    # a DIFFERENT right-block member (the IDE selection, the `Debug` flag, the
    # cloud link, the PR link): real renders, but no capture holds one, so they
    # stay fail-closed exactly like residual (3)'s `⧉` indicator
    "⏸ manual mode on      ⧉ In foo.py",
    "⏸ manual mode on      Debug",
    "⏸ manual mode on      Debug · /rc active",
    # the LEFT half must still be a well-formed bar — the split is not an escape
    # hatch for arbitrary prose
    "hello there      /rc active",
    "❯ tell me about /rc active",
    "1. Yes, trust this folder      /rc active",
    # BYTE DISCIPLINE: an NBSP variant of the pill refuses (this lane never
    # normalizes Unicode spaces — GH #56)
    "⏸ manual mode on · ? for shortcuts      /rc\xa0active",
    "⏸ manual mode on · ? for shortcuts\xa0\xa0\xa0\xa0\xa0\xa0/rc active",
]


@pytest.mark.parametrize("row", _GH73_REFUSED_ROWS)
def test_gh73_unlicensed_right_aligned_text_still_refuses(row: str) -> None:
    """Fail-closed on everything the renderer read does not license. Asserted
    through the FULL predicates too, so the refusal is the shipped behavior and
    not just a grammar detail."""
    assert tp._is_status_row(row) is False
    pane = _lone_sep_pane(row)
    assert tp.pane_input_box_present(pane) is False, row
    assert tp.classify_input_box_failure(pane) == "no_input_box", row
    # …and the stranded-draft brake never gets a keyless release from such a row.
    assert tp.pane_input_row_empty(pane) is not True, row


@pytest.mark.parametrize("row", ["/rc", "/rc active", "/rc connecting…"])
def test_gh73_a_right_block_only_row_is_not_a_status_bar(row: str) -> None:
    """THE NON-WIDENING DECISION, pinned. The renderer DOES permit an empty left
    half (FooterHintLine's fall-through can render nothing), so this row shape is
    real — but acceptance stays exactly `has_mode or >=1 HINT`, the invariant GH
    #62 shipped, so the pill widens nothing on its own, precisely like a bare
    `PR #309` / `2 monitors` / `12 memories recalled`. No capture in any corpus
    holds such a row; it degrades to today's fail-closed refusal."""
    assert tp._is_status_row(row) is False
    for typed_slot in ("PR #309", "2 monitors", "12 memories recalled"):
        assert tp._is_status_row(typed_slot) is False


def test_gh73_the_split_is_a_NO_OP_without_a_right_block() -> None:
    """Byte-identity for every older pane: `_split_status_right_block` returns the
    row UNCHANGED unless it ends in a licensed block, so no pre-2.1.246 row can
    take a different path through the predicate."""
    for row in _LIVE_BOT_ROWS + _REAL_CORPUS_STATUS_ROWS:
        assert tp._split_status_right_block(row) == row, row
    # And a row that DOES carry one loses exactly the block plus its padding.
    assert (
        tp._split_status_right_block("⏸ manual mode on      /rc active")
        == "⏸ manual mode on"
    )


def test_gh73_needs_no_leg3_or_idle_alphabet_change() -> None:
    """MEASURED, per the GH #62 precedent that grammar and leg 3 are SEPARATE
    alphabets extended only where the frames prove a need: every 2.1.246 capture
    already carries a leg-3 marker (`shift+tab to cycle` / the paste hint) to the
    LEFT of the pill, so `_INPUT_READY_CHROME_MARKERS` and `_READY_STATUS_MARKERS`
    are deliberately UNCHANGED — `/rc` is grammar vocabulary only."""
    assert "/rc" not in tp._INPUT_READY_CHROME_MARKERS
    assert "/rc" not in tp._READY_STATUS_MARKERS
    for name in (_IDLE_246, _RC_CONNECTING_246, _RC_ACTIVE_246, _TALL_DRAFT_246):
        below = _status_row_of(name)
        assert any(m in below for m in tp._INPUT_READY_CHROME_MARKERS), name


def test_gh73_needs_no_rule_separator_change() -> None:
    """`_RE_RULE_SEPARATOR` is unchanged, pinned on the real captures: every
    input-box rule in the 2.1.246 fixtures already satisfies `_is_rule_separator`
    (the capture set carries PURE-dash rules only)."""
    for name in (_IDLE_246, _TALL_DRAFT_246, _PASTE_COLLAPSED_246):
        lines = tp._strip_ansi(_pane(name)).split("\n")
        seps = [line for line in lines if tp._is_rule_separator(line)]
        assert len(seps) >= 2, name
        assert all(set(line.strip()) == {"─"} for line in seps), name
