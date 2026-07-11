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
