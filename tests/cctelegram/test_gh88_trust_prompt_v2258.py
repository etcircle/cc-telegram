"""GH #88 — the CC 2.1.258 folder-trust prompt REDESIGN.

2.1.258 removed the ``N.`` numbering, INVERTED the option order, and moved the
default cursor onto the DESTRUCTIVE row (``❯ No, exit``). Every one of those
alone broke the GH #65 creation lane, which degraded all the way back to the
pre-#65 "didn't register in time" KILL.

Covered here, against the REAL rig captures (never reconstructed strings):

  * §A the UNNUMBERED footered option-block walk + the ``option_style`` stamp,
    the title/excerpt derivation, and the CURSOR-STABLE fingerprint;
  * §A the residue predicates (``has_decision_residue`` gains the unnumbered
    block; ``has_live_decision_residue`` is the narrower LIVE-only twin);
  * §B the ORDER-AGNOSTIC family match;
  * §C the LABEL-based Trust target at mint;
  * §E the ``(family × version × option_style)`` license;
  * §D the ``⚠`` pre-approval block on the 🔐 card + its sanitizer;
  * §F ``SliceKind.UNKNOWN_PROMPT``.

The end-to-end lane (advisory hold, unknown→trust recovery, the ``tst:`` tap on
2.1.258) lives in ``tests/scenarios/test_gh88_trust_v2258.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cctelegram import terminal_parser as tp
from cctelegram.handlers import decision_token, trust_flow

_FIXTURES = Path(__file__).parent / "fixtures"

_ARRIVAL_PLAIN = "folder_trust_arrival_plain_v2.1.258.txt"
_ARRIVAL_SETTINGS = "folder_trust_arrival_settings_v2.1.258.txt"
_POSTDOWN_PLAIN = "folder_trust_postdown_plain_v2.1.258.txt"
_POSTDOWN2_PLAIN = "folder_trust_postdown2_plain_v2.1.258.txt"
_POSTUP_PLAIN = "folder_trust_postup_plain_v2.1.258.txt"
_POSTDOWN_SETTINGS = "folder_trust_postdown_settings_v2.1.258.txt"
_POSTESC = "folder_trust_postesc_t4_plain_v2.1.258.txt"
_POSTENTER_NOEXIT = "folder_trust_postenter_noexit_t4_plain_v2.1.258.txt"
_AFTER_ACCEPT = "trust_after_accept_repl_v2.1.258.txt"

_V258 = "2.1.258"
_V246 = "2.1.246"


def _fx(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _form(name: str) -> tp.AskUserQuestionForm:
    form = tp.parse_generic_decision(_fx(name))
    assert form is not None, f"{name} must parse as a Decision"
    return form


@pytest.fixture(autouse=True)
def _decision_cards_on() -> Any:
    tp.set_decision_cards_enabled(True)
    yield
    tp.reset_for_tests()
    decision_token.reset_for_tests()


# ── §A — the unnumbered footered parse ───────────────────────────────────────


@pytest.mark.parametrize("name", [_ARRIVAL_PLAIN, _ARRIVAL_SETTINGS])
def test_the_unnumbered_arrival_frame_parses_as_a_footered_decision(name: str) -> None:
    """The regression that killed the lane: this used to return ``None``."""
    form = _form(name)
    assert tp.decision_variant_of(form) == tp.DECISION_VARIANT_FOOTERED
    assert tp.option_style_of(form) == tp.OPTION_STYLE_UNNUMBERED
    assert form.select_mode == "single"
    assert form.options_contiguous_from_one()
    assert [(o.number, o.label, o.cursor) for o in form.options] == [
        (1, "No, exit", True),  # the DESTRUCTIVE option is row 1 AND the default
        (2, "Yes, I trust this folder", False),
    ]


@pytest.mark.parametrize("name", [_ARRIVAL_PLAIN, _ARRIVAL_SETTINGS])
def test_title_and_excerpt_come_from_the_unnumbered_block_top(name: str) -> None:
    """``_decision_option_block_top`` is numbered-only, so the unnumbered walk's
    own block top must stand in for it — otherwise the title is ``None`` and the
    title-anchored family signature can never match."""
    form = _form(name)
    assert form.current_question_title == "Accessing workspace:"
    assert form.pane_excerpt.splitlines()[0].strip() == "Accessing workspace:"
    assert form.pane_excerpt.splitlines()[-1].strip().startswith("Enter to confirm")


def test_the_settings_variant_carries_the_warning_block_in_its_excerpt() -> None:
    excerpt = _form(_ARRIVAL_SETTINGS).pane_excerpt
    assert "⚠ This folder pre-approves 18 tool permissions" in excerpt
    assert "⚠" not in _form(_ARRIVAL_PLAIN).pane_excerpt


def test_the_numbered_grammar_is_never_mixed_in() -> None:
    """A 2.1.246 frame keeps the NUMBERED style byte-for-byte."""
    form = _form("folder_trust_arrival_plain_v2.1.246.txt")
    assert tp.option_style_of(form) == tp.OPTION_STYLE_NUMBERED
    assert [(o.number, o.label) for o in form.options] == [
        (1, "Yes, I trust this folder"),
        (2, "No, exit"),
    ]


# ── §A — the fingerprint is CURSOR-STABLE and body-inclusive ────────────────


def test_the_fingerprint_is_stable_across_every_cursor_position() -> None:
    """The post-Down verify re-computes the fingerprint on the MOVED frame. If a
    cursor move rotated the identity, Trust could never commit on 2.1.258."""
    fps = {
        tp.decision_prompt_fingerprint(_form(n))
        for n in (_ARRIVAL_PLAIN, _POSTDOWN_PLAIN, _POSTDOWN2_PLAIN, _POSTUP_PLAIN)
    }
    assert len(fps) == 1, fps


def test_plain_and_settings_variants_fingerprint_differently() -> None:
    """The ``⚠`` block lands in the fingerprint BODY — correct: one tap on the
    settings variant grants 18 pre-approved permissions the plain one does not."""
    assert tp.decision_prompt_fingerprint(
        _form(_ARRIVAL_PLAIN)
    ) != tp.decision_prompt_fingerprint(_form(_ARRIVAL_SETTINGS))
    # …and the settings variant is itself cursor-stable.
    assert tp.decision_prompt_fingerprint(
        _form(_ARRIVAL_SETTINGS)
    ) == tp.decision_prompt_fingerprint(_form(_POSTDOWN_SETTINGS))


# ── §A — fail-closed properties of the unnumbered walk ──────────────────────


_FOOTER = " Enter to confirm · Esc to cancel"


def _synthetic(*rows: str) -> str:
    return "\n".join(
        [
            "─" * 80,
            " Accessing workspace:",
            "",
            " /tmp/x",
            "",
            *rows,
            "",
            _FOOTER,
            "",
        ]
    )


def test_a_prose_row_flush_against_the_options_terminates_the_run() -> None:
    """The COLUMN rule is the stopping condition (no blank line needed).

    ``Security guide`` is 1-space-indented while the option labels sit at column
    3 (a cursor glyph plus its space occupies exactly the two cells a non-cursor
    row fills with spaces), so even with NO blank between them the prose row is
    not absorbed as a third option."""
    pane = _synthetic(" Security guide", " ❯ No, exit", "   Yes, I trust this folder")
    form = tp.parse_generic_decision(pane)
    assert form is not None
    assert [o.label for o in form.options] == ["No, exit", "Yes, I trust this folder"]


def test_a_prose_row_at_the_option_column_is_absorbed_and_breaks_the_family() -> None:
    """The DISCLOSED weakness, pinned rather than hidden: a row that aligns to
    the label column IS taken as an option — and the family signature (exact
    label SET + count) is what refuses it, so nothing is ever dispatched."""
    pane = _synthetic("   Security guide", " ❯ No, exit", "   Yes, I trust this folder")
    form = tp.parse_generic_decision(pane)
    assert form is not None
    assert len(form.options) == 3
    assert decision_token.identify_family(form) is None


@pytest.mark.parametrize(
    "rows",
    [
        # No cursor at all.
        ("   No, exit", "   Yes, I trust this folder"),
        # TWO cursors — ambiguous.
        (" ❯ No, exit", " ❯ Yes, I trust this folder"),
        # A single row is never an option block.
        (" ❯ No, exit",),
        # A rule row inside the run.
        (" ❯ ────────────", "   Yes, I trust this folder"),
    ],
)
def test_the_unnumbered_walk_fails_closed(rows: tuple[str, ...]) -> None:
    assert tp.parse_generic_decision(_synthetic(*rows)) is None


def test_the_two_grammars_are_never_mixed() -> None:
    """A run containing a numbered row refuses outright rather than half-parsing."""
    pane = _synthetic(" ❯ 1. Yes, I trust this folder", "   No, exit")
    form = tp.parse_generic_decision(pane)
    # The NUMBERED walk owns this frame (one numbered row ⇒ <2 options ⇒ refuse);
    # the unnumbered fallback must NOT rescue it.
    assert form is None


# ── §A — residue predicates ─────────────────────────────────────────────────


def _drop_footer(pane: str) -> str:
    return "\n".join(
        line for line in pane.split("\n") if not line.strip().startswith("Enter to ")
    )


def test_a_footer_dropped_unnumbered_block_is_still_residue() -> None:
    """Codex r1 P2-3: without this the confirm side would record ``dispatched``
    on a prompt that never resolved."""
    frame = _drop_footer(_fx(_ARRIVAL_PLAIN))
    assert "Enter to confirm" not in frame
    assert tp.has_decision_residue(frame) is True


def test_the_confirm_side_fails_closed_on_a_footer_dropped_unnumbered_block() -> None:
    from cctelegram.callback_dispatcher import interactive as cbi

    frame = _drop_footer(_fx(_ARRIVAL_PLAIN))
    minted = tp.decision_prompt_fingerprint(_form(_ARRIVAL_PLAIN))
    assert cbi._classify_decision_advance(frame, minted) is False


def test_the_confirm_side_accepts_the_real_post_accept_repl() -> None:
    from cctelegram.callback_dispatcher import interactive as cbi

    minted = tp.decision_prompt_fingerprint(_form(_ARRIVAL_PLAIN))
    assert cbi._classify_decision_advance(_fx(_AFTER_ACCEPT), minted) is True


def test_live_residue_is_narrower_than_the_confirm_side_predicate() -> None:
    """A corpse (Claude exited, prompt text retained above a shell prompt) is
    RESIDUE but is NOT a LIVE prompt — the ``UNKNOWN_PROMPT`` hold must never
    fire on it."""
    for name in (_POSTESC, _POSTENTER_NOEXIT):
        pane = _fx(name)
        assert tp.has_decision_residue(pane) is True, name
        assert tp.has_live_decision_residue(pane) is False, name


def test_live_residue_fires_on_a_footer_only_frame() -> None:
    """The §F trigger: a partially-drawn prompt whose options are unreadable."""
    pane = "\n".join(["", " Some prompt nobody parses", "", _FOOTER, "", ""])
    assert tp.parse_generic_decision(pane) is None
    assert tp.has_live_decision_residue(pane) is True


def test_live_residue_is_false_for_a_stale_footer_above_a_live_input_box() -> None:
    """A scrollback footer with a ready pane below it is RUNNING, never a hold."""
    pane = _fx(_POSTESC).rstrip() + "\n" + _fx("inputbox_idle_v2.1.207.txt")
    assert tp.has_live_decision_residue(pane) is False


# ── §B — the order-agnostic family match ────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        _ARRIVAL_PLAIN,
        _ARRIVAL_SETTINGS,
        _POSTDOWN_PLAIN,
        "folder_trust_arrival_plain_v2.1.246.txt",
        "folder_trust_arrival_plain_v2.1.241.txt",
    ],
)
def test_one_signature_identifies_both_option_orders(name: str) -> None:
    assert decision_token.identify_family(_form(name)) == "folder-trust"


def test_a_duplicate_label_cannot_smuggle_a_third_row_past_the_set_match() -> None:
    """The option COUNT check beside the SET comparison."""
    form = _form(_ARRIVAL_PLAIN)
    extra = tp.AskOption(
        label="No, exit", recommended=False, cursor=False, number=3, description=""
    )
    smuggled = tp.AskUserQuestionForm(
        current_question_title=form.current_question_title,
        options=form.options + (extra,),
        pane_excerpt=form.pane_excerpt,
        select_mode="single",
        _meta=dict(form._meta),
    )
    assert decision_token.identify_family(smuggled) is None


# ── §E — the (family × version × option_style) license ──────────────────────


def test_the_license_is_keyed_on_the_rendering_not_just_the_version() -> None:
    lookup = decision_token.lookup
    assert lookup("folder-trust", _V258, tp.OPTION_STYLE_UNNUMBERED) is True
    # 2.1.258 does NOT license an un-characterized NUMBERED rendering…
    assert lookup("folder-trust", _V258, tp.OPTION_STYLE_NUMBERED) is False
    # …and 2.1.246 does not license the NEW unnumbered one.
    assert lookup("folder-trust", _V246, tp.OPTION_STYLE_UNNUMBERED) is False
    assert lookup("folder-trust", _V246, tp.OPTION_STYLE_NUMBERED) is True
    # A style-less form fails closed (positive authorization).
    assert lookup("folder-trust", _V258, None) is False


# ── §C — the LABEL-based Trust target ───────────────────────────────────────


def _flow(version: str) -> trust_flow.TrustFlow:
    return trust_flow.TrustFlow(
        generation=1,
        user_id=1,
        thread_id=7,
        chat_id=None,
        card_chat_id=None,
        card_msg_id=None,
        created_wid="@fake-gh88",
        window_name="w",
        selected_path="/repo",
        create_message="",
        resume_id=None,
        cli_version=version,
        user_data=None,
    )


@pytest.mark.parametrize(
    ("name", "version", "expected_number"),
    [
        (_ARRIVAL_PLAIN, _V258, 2),  # 2.1.258: Trust is row TWO
        (_ARRIVAL_SETTINGS, _V258, 2),
        ("folder_trust_arrival_plain_v2.1.246.txt", _V246, 1),  # 2.1.246: row ONE
    ],
)
def test_the_mint_targets_the_affirmative_label_not_a_position(
    name: str, version: str, expected_number: int
) -> None:
    decision_token.set_trust_card_dispatch_enabled(True)
    decision_token.set_decision_dispatch_force_disabled(False)
    flow = _flow(version)
    token = trust_flow._mint_trust_token_locked(flow, _fx(name))
    assert token is not None
    entry = decision_token.peek(token)
    assert entry is not None
    assert entry.option_number == expected_number
    assert entry.option_label == decision_token.TRUST_AFFIRMATIVE_LABEL
    assert entry.option_style == tp.option_style_of(_form(name))


def test_the_mint_declines_when_the_affirmative_label_is_ambiguous() -> None:
    """Zero or MORE THAN ONE affirmative row ⇒ no token (display-only card)."""
    decision_token.set_trust_card_dispatch_enabled(True)
    decision_token.set_decision_dispatch_force_disabled(False)
    pane = _synthetic(
        " ❯ Yes, I trust this folder",
        "   Yes, I trust this folder",
    )
    assert trust_flow._mint_trust_token_locked(_flow(_V258), pane) is None


def test_the_mint_declines_an_unlicensed_rendering_before_any_token() -> None:
    """A 2.1.258 pane rendering the OLD numbered shape mints NOTHING."""
    decision_token.set_trust_card_dispatch_enabled(True)
    decision_token.set_decision_dispatch_force_disabled(False)
    numbered_pane = _fx("folder_trust_arrival_plain_v2.1.246.txt")
    assert trust_flow._mint_trust_token_locked(_flow(_V258), numbered_pane) is None
    unnumbered_pane = _fx(_ARRIVAL_PLAIN)
    assert trust_flow._mint_trust_token_locked(_flow(_V246), unnumbered_pane) is None


# ── §D — the ⚠ pre-approval block on the card ───────────────────────────────


def test_the_warning_block_is_extracted_only_for_the_settings_variant() -> None:
    assert trust_flow.trust_warning_block(_fx(_ARRIVAL_PLAIN)) is None
    block = trust_flow.trust_warning_block(_fx(_ARRIVAL_SETTINGS))
    assert block is not None
    assert block.startswith("⚠ This folder pre-approves 18 tool permissions")
    assert "Security guide" not in block
    assert "No, exit" not in block
    assert "Enter to confirm" not in block


def test_the_card_shows_the_warning_only_when_present() -> None:
    flow = _flow(_V258)
    with_warning, keyboard = trust_flow.build_trust_card(
        flow,
        trust_token="tok",
        warning_block=trust_flow.trust_warning_block(_fx(_ARRIVAL_SETTINGS)),
    )
    assert "pre-approves 18 tool permissions" in with_warning
    assert "open the tmux window for the full list" in with_warning
    assert any(
        b.callback_data and b.callback_data.startswith("tst:t:")
        for row in keyboard.inline_keyboard
        for b in row
    )
    plain, _kb = trust_flow.build_trust_card(
        flow, trust_token="tok", warning_block=None
    )
    assert "pre-approves" not in plain


_INJECTION = (
    "⚠ This folder pre-approves 3 tool permissions in .claude/settings.json:\n"
    "  ```\n"
    "  Bash(rm -rf *), [click me](http://evil.example/x), *bold*, \\ escape\n"
    "  `inline` and a \x07 bell and a \x1b[31m sequence\n"
)


def test_the_sanitizer_neutralises_an_injection_payload() -> None:
    out = trust_flow.sanitize_pane_copy(_INJECTION)
    assert "`" not in out
    assert "\x07" not in out and "\x1b" not in out
    # The visible content survives (copy, not censorship).
    assert "pre-approves 3 tool permissions" in out
    assert "click me" in out


def test_an_injection_payload_cannot_break_the_card_or_its_keyboard() -> None:
    from cctelegram.markdown_v2 import convert_markdown

    text, keyboard = trust_flow.build_trust_card(
        _flow(_V258),
        trust_token="tok",
        warning_block=trust_flow.sanitize_pane_copy(_INJECTION),
    )
    # The rendered MarkdownV2 must not raise, and the buttons must survive.
    assert convert_markdown(text)
    datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert any(d and d.startswith("tst:t:") for d in datas)
    assert any(d and d.startswith("tst:c:") for d in datas)


def test_the_sanitizer_caps_length() -> None:
    out = trust_flow.sanitize_pane_copy("x" * 5000)
    assert len(out) <= trust_flow.PANE_COPY_CAP
    assert out.endswith("…")


# ── §F — SliceKind.UNKNOWN_PROMPT ───────────────────────────────────────────


@pytest.mark.parametrize("name", [_ARRIVAL_PLAIN, _ARRIVAL_SETTINGS, _POSTDOWN_PLAIN])
def test_the_live_2_1_258_frame_classifies_trust_frame(name: str) -> None:
    """The whole point of the fix: this used to classify RUNNING, burn the
    registration budget, and KILL the window."""
    assert (
        trust_flow.classify_slice(
            registered=False, pane_command=_V258, pane_text=_fx(name)
        )
        is trust_flow.SliceKind.TRUST_FRAME
    )


@pytest.mark.parametrize("name", [_POSTESC, _POSTENTER_NOEXIT])
def test_the_2_1_258_corpse_frames_classify_shell(name: str) -> None:
    pane = _fx(name)
    assert "Enter to confirm" in pane, "the corpse must still show the prompt text"
    assert (
        trust_flow.classify_slice(registered=False, pane_command="zsh", pane_text=pane)
        is trust_flow.SliceKind.SHELL
    )


def test_an_unrecognized_live_prompt_classifies_unknown_prompt() -> None:
    pane = "\n".join(["", " Some prompt nobody parses", "", _FOOTER, "", ""])
    assert (
        trust_flow.classify_slice(registered=False, pane_command=_V258, pane_text=pane)
        is trust_flow.SliceKind.UNKNOWN_PROMPT
    )


def test_a_stale_footer_above_a_live_input_box_stays_running() -> None:
    pane = _fx(_POSTESC).rstrip() + "\n" + _fx("inputbox_idle_v2.1.207.txt")
    assert (
        trust_flow.classify_slice(registered=False, pane_command=_V258, pane_text=pane)
        is trust_flow.SliceKind.RUNNING
    )


def test_the_unknown_prompt_card_is_display_only() -> None:
    text, keyboard = trust_flow.build_unknown_prompt_card(
        _flow(_V258), pane_tail_text="some pane tail"
    )
    datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert datas == [f"{'tst:'}c:1"], datas  # ONLY the zero-keystroke Cancel
    assert "some pane tail" in text
    assert "can't read" in text


def test_the_unknown_prompt_card_tail_is_sanitized() -> None:
    from cctelegram.markdown_v2 import convert_markdown

    text, _kb = trust_flow.build_unknown_prompt_card(
        _flow(_V258),
        pane_tail_text=trust_flow.sanitize_pane_copy(_INJECTION),
    )
    assert "`inline`" not in text
    assert convert_markdown(text)


# ── §E — the dispatch's pre-keystroke style parity ──────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pane_name", "minted_style"),
    [
        # A card minted from the NUMBERED shape against a live UNNUMBERED pane…
        (_ARRIVAL_PLAIN, tp.OPTION_STYLE_NUMBERED),
        # …and the reverse.
        ("folder_trust_arrival_plain_v2.1.246.txt", tp.OPTION_STYLE_UNNUMBERED),
    ],
)
async def test_a_style_mismatch_declines_before_any_keystroke(
    pane_name: str, minted_style: str
) -> None:
    from unittest.mock import AsyncMock

    from cctelegram.callback_dispatcher import interactive as cbi

    pane = _fx(pane_name)
    tmux = SimpleNamespace(
        capture_pane=AsyncMock(return_value=pane),
        pane_current_command=AsyncMock(return_value=_V258),
        send_keys=AsyncMock(return_value=True),
    )
    form = tp.parse_generic_decision(pane)
    assert form is not None
    outcome = await cbi._dispatch_decision_pane_locked(
        user=SimpleNamespace(id=1),
        tmux_manager=tmux,
        w=SimpleNamespace(window_id="@1"),
        window_id="@1",
        minted_fingerprint=tp.decision_prompt_fingerprint(form),
        option_number=2,
        option_label=decision_token.TRUST_AFFIRMATIVE_LABEL,
        ledger_key=None,
        minted_option_style=minted_style,
    )
    assert outcome.kind == "not_advanced"
    assert outcome.reason == "option_style_mismatch"
    tmux.send_keys.assert_not_called()
