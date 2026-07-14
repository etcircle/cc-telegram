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
    "inputbox_draft_typed_v2.1.207.txt": None,
    "inputbox_idle_v2.1.207.txt": None,
    "inputbox_manual_mode_v2.1.207.txt": None,
    "inputbox_multiline_draft_v2.1.207.txt": None,
    "inputbox_paste_collapsed_reverted_v2.1.207.txt": None,
    "inputbox_paste_collapsed_v2.1.207.txt": None,
    "inputbox_slash_exact_clear_v2.1.207.txt": "completion_overlay",
    "inputbox_slash_overlay_v2.1.207.txt": "completion_overlay",
    "inputbox_slash_with_arg_v2.1.207.txt": None,
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
