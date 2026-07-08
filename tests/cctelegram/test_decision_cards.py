"""Stage B1 — generic ``Decision`` prompt detection (flag-gated, display-only).

Drives the real ``terminal_parser`` against committed CC 2.1.200 fixtures for
the genuinely-uncovered confirmation-prompt slice (the folder-trust prompt and
the "Switch model?" confirmation, both verified to match NO named pattern).
Covers, per the reviewed plan (Feature B, B.1–B.8):

  - ``parse_generic_decision`` → correct single-select form on each positive;
  - ``None`` on the negative corpus (a quoted decision with ready-for-input
    chrome below → ``_only_chrome_below`` rejects; the permission-prose fixture);
  - detection with ``CC_TELEGRAM_DECISION_CARDS`` ON (Decision pane → Decision)
    and the flag-OFF no-op (default) — provably no new detection;
  - first-match-wins regression pins: the ``settings_select_model`` list still
    resolves to Settings, and representative AUQ / EPM / Permission /
    RestoreCheckpoint panes still resolve to their NAMED pattern, NEVER Decision;
  - the strict-validator veto (Hermes P2-4): a real Permission fixture makes
    ``parse_generic_decision`` return None, AND with the permission flag OFF
    (gate filtered from the detector) a Bash gate is NOT re-surfaced as Decision;
  - flag seeding: ``reset_for_tests`` resets BOTH parser flags; the env re-read;
    the config → parser seed (the ``main._run_bot`` import-order-race dodge);
  - route-state non-regression at the ``status_polling`` seam (Hermes P2-3): a
    NEGATIVE pane does NOT flip a RUNNING route to WAITING_ON_USER, with a
    positive control proving the seam IS wired (a real Decision pane DOES).

The leaf autouse ``_reset_terminal_parser_flag`` fixture (tests/cctelegram/
conftest.py) re-reads BOTH flags from the env before/after each test, so a test
that flips one never leaks.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import terminal_parser as tp
from cctelegram.terminal_parser import (
    extract_interactive_content,
    parse_generic_decision,
    set_decision_cards_enabled,
    set_permission_prompts_enabled,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


_TRUST = "decision_trust_folder_v2.1.200.txt"
_SWITCH = "decision_switch_model_v2.1.200.txt"
_SETTINGS_MODEL = "settings_select_model_v2.1.200.txt"
_NEG_QUOTED = "decision_negative_quoted_scrollback_v2.1.200.txt"
_NEG_PERMISSION_PROSE = "permission_negative_prose_v2.1.190.txt"

# B2.0 rig captures (CC v2.1.204 — real `tmux capture-pane`; keystroke transcript
# in decision_trust_folder_v2.1.204_keystrokes.md).
_TRUST_204 = "decision_trust_folder_v2.1.204.txt"
_TRUST_204_DOWN = "decision_trust_folder_postdown_v2.1.204.txt"
_TRUST_204_UP = "decision_trust_folder_postup_v2.1.204.txt"


@pytest.fixture
def decision_on():
    """Enable the Decision detector for the test body (reset by the leaf autouse)."""
    set_decision_cards_enabled(True)
    yield
    set_decision_cards_enabled(False)


@pytest.fixture
def both_gates_on():
    """Enable BOTH the Permission/Workflow and the Decision detectors."""
    set_permission_prompts_enabled(True)
    set_decision_cards_enabled(True)
    yield
    set_permission_prompts_enabled(False)
    set_decision_cards_enabled(False)


# ── parse_generic_decision: positive shapes ───────────────────────────────


def test_parse_trust_folder() -> None:
    """The real folder-trust prompt parses as a single-select Decision form: the
    title is the TOP line of the contiguous prompt block, both options are
    carried, and the live ``❯`` cursor sits on option 1. The excerpt spans the
    whole block so the safety-check heading is visible in the card (Fix 3)."""
    form = parse_generic_decision(_load(_TRUST))
    assert form is not None
    assert form.select_mode == "single"
    assert form.is_review_screen is False
    assert form.current_question_title == "Accessing workspace:"
    # Fix 3: the full prompt block (incl. the safety-check heading) is in the
    # excerpt shown as the card body.
    assert "Quick safety check:" in form.pane_excerpt
    assert "Yes, I trust this folder" in form.pane_excerpt
    assert [o.label for o in form.options] == ["Yes, I trust this folder", "No, exit"]
    assert [o.number for o in form.options] == [1, 2]
    assert form.options[0].cursor is True
    assert form.options[1].cursor is False


def test_parse_switch_model() -> None:
    """The "Switch model?" confirmation parses as a single-select Decision form
    with the live cursor on option 1. Fix 3: the title is the actual heading
    ("Switch model?"), not the body paragraph nearest the options, and the whole
    prompt block is in the excerpt."""
    form = parse_generic_decision(_load(_SWITCH))
    assert form is not None
    assert form.select_mode == "single"
    assert form.is_review_screen is False
    assert form.current_question_title == "Switch model?"
    assert "Switch model?" in form.pane_excerpt
    assert "This conversation is cached" in form.pane_excerpt
    assert [o.label for o in form.options] == [
        "Yes, switch to Opus 4.8 (1M context) (default)",
        "No, go back",
    ]
    assert [o.number for o in form.options] == [1, 2]
    assert form.options[0].cursor is True


def test_parse_trust_folder_live_2_1_204_cursor_tracks_arrows(decision_on) -> None:
    """B2.0 premise gate: the fresh CC v2.1.204 folder-trust captures (real
    ``tmux capture-pane``) parse as a single-select Decision whose ``❯`` cursor
    tracks the arrow keys — initial + after ``Up`` sit on option 1, after
    ``Down`` on option 2. This pins the live-fixture shape the B2.2 dispatch
    table will key on (family signature + arrow-move-only semantics — E2)."""
    initial = parse_generic_decision(_load(_TRUST_204))
    down = parse_generic_decision(_load(_TRUST_204_DOWN))
    up = parse_generic_decision(_load(_TRUST_204_UP))
    for form in (initial, down, up):
        assert form is not None
        assert form.select_mode == "single"
        assert form.current_question_title == "Accessing workspace:"
        assert [o.label for o in form.options] == [
            "Yes, I trust this folder",
            "No, exit",
        ]
        assert [o.number for o in form.options] == [1, 2]
    # Cursor tracks the arrow keys (E2 = arrows move the ❯ without committing).
    assert (initial.options[0].cursor, initial.options[1].cursor) == (True, False)
    assert (down.options[0].cursor, down.options[1].cursor) == (False, True)
    assert (up.options[0].cursor, up.options[1].cursor) == (True, False)
    # Detected as a Decision through the full extractor (flag ON).
    assert extract_interactive_content(_load(_TRUST_204)).name == "Decision"  # type: ignore[union-attr]


def test_parse_is_flag_independent() -> None:
    """``parse_generic_decision`` is a pure function — it returns a form even
    with the flag OFF (only ``_active_ui_patterns`` / ``extract_interactive_content``
    consult the flag). This keeps the veto callable from other paths."""
    assert tp.decision_cards_enabled() is False
    assert parse_generic_decision(_load(_TRUST)) is not None


# ── parse_generic_decision: negative corpus ───────────────────────────────


def test_parse_negative_quoted_scrollback_returns_none() -> None:
    """A quoted decision block + footer with ready-for-input chrome BELOW it (an
    ``❯`` input box, ``? for shortcuts``, ``◉ … /effort``) is NOT the live bottom
    prompt — ``_only_chrome_below`` rejects it, so the parser returns None."""
    assert parse_generic_decision(_load(_NEG_QUOTED)) is None


def test_parse_permission_prose_returns_none() -> None:
    """Assistant prose with a numbered explanation but no confirmation footer →
    no bottom anchor → None."""
    assert parse_generic_decision(_load(_NEG_PERMISSION_PROSE)) is None


def test_parse_empty_and_no_footer_returns_none() -> None:
    assert parse_generic_decision("") is None
    assert parse_generic_decision("just some text\nwith no footer\n") is None


def test_parse_single_option_returns_none() -> None:
    """A confirmation footer with only ONE numbered option (and no ≥2 block) is
    not a decision prompt — fail closed."""
    pane = " Proceed?\n ❯ 1. Yes\n Enter to confirm · Esc to cancel\n"
    assert parse_generic_decision(pane) is None


def test_parse_no_cursor_returns_none() -> None:
    """≥2 options but NO resolved live ``❯`` cursor → fail closed (a stale /
    off-screen block, not the live picker)."""
    pane = " Proceed?\n 1. Yes\n 2. No\n Enter to confirm · Esc to cancel\n"
    assert parse_generic_decision(pane) is None


# ── Detection via extract_interactive_content (flag ON) ───────────────────


@pytest.mark.parametrize("fixture", [_TRUST, _SWITCH])
def test_decision_pane_detected_flag_on(decision_on, fixture: str) -> None:
    result = extract_interactive_content(_load(fixture))
    assert result is not None, fixture
    assert result.name == "Decision", fixture


@pytest.mark.parametrize("fixture", [_TRUST, _SWITCH])
def test_decision_pane_no_detection_flag_off(fixture: str) -> None:
    """Flag OFF (default) — the Decision pattern is filtered from the detector,
    so a Decision pane produces NO detection (a provable no-op deploy)."""
    assert tp.decision_cards_enabled() is False
    assert extract_interactive_content(_load(fixture)) is None, fixture


def test_negative_pane_no_detection_flag_on(decision_on) -> None:
    """Even with the flag ON, the negative-quoted-scrollback pane must NOT light
    a Decision card (the strict validator vetoes it via ``_only_chrome_below``)."""
    assert extract_interactive_content(_load(_NEG_QUOTED)) is None
    assert extract_interactive_content(_load(_NEG_PERMISSION_PROSE)) is None


# ── First-match-wins regression pins ──────────────────────────────────────


def test_settings_model_list_stays_settings_never_decision(both_gates_on) -> None:
    """The /model Select-model LIST is Settings-covered — with the Decision flag
    ON it must STILL resolve to Settings (first-match-wins; Settings is ordered
    before Decision), never Decision."""
    result = extract_interactive_content(_load(_SETTINGS_MODEL))
    assert result is not None
    assert result.name == "Settings"


def test_settings_confirm_footer_stays_settings_never_decision(
    both_gates_on, sample_pane_settings: str
) -> None:
    """B1 P3-2: the v2.1.200 fixture above has an ``Enter to set as default …``
    footer that ``_RE_DECISION_FOOTER`` can never match, so that pin proves
    non-overlap only. The OLDER /model variant's footer (``Enter to confirm ·
    Esc to exit``) DOES carry Decision's required ``Enter to confirm`` — this
    pin genuinely exercises the load-bearing first-match-wins ordering
    (Settings is ordered before Decision)."""
    result = extract_interactive_content(sample_pane_settings)
    assert result is not None
    assert result.name == "Settings"

    # Teeth: the same variant minus the ``Use /fast …`` prose row (which breaks
    # ``_gate_options_above``'s contiguity walk) STRICTLY parses under
    # Decision's own validator — so ONLY the ordering keeps it Settings.
    trimmed = (
        " Select model\n"
        " Switch between Claude models. Applies to this session and future"
        " Claude Code sessions.\n"
        "\n"
        "   1. Default (recommended)  Opus 4.6 · Most capable for complex work\n"
        " ❯ 2. Sonnet                 Sonnet 4.6 · Best for everyday tasks\n"
        "   3. Haiku                  Haiku 4.5 · Fastest for quick answers\n"
        "\n"
        " Enter to confirm · Esc to exit\n"
    )
    assert parse_generic_decision(trimmed) is not None
    result = extract_interactive_content(trimmed)
    assert result is not None
    assert result.name == "Settings"


def test_auq_pane_stays_askuserquestion(both_gates_on) -> None:
    """A plain single-select AUQ (footer ``Enter to select``) stays
    AskUserQuestion — Decision's footer family deliberately excludes
    ``Enter to select``, and AUQ is ordered first anyway."""
    pane = (
        "  Which lane?\n"
        "❯ 1. A) Ship now\n"
        "  2. B) Bake first\n"
        "  Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )
    result = extract_interactive_content(pane)
    assert result is not None
    assert result.name == "AskUserQuestion"


def test_epm_pane_stays_exitplanmode(both_gates_on) -> None:
    result = extract_interactive_content(_load("epm_v2170_ctrl_plus_g.txt"))
    assert result is not None
    assert result.name == "ExitPlanMode"


def test_permission_pane_stays_permission(both_gates_on) -> None:
    """A real Permission gate (footer ``Esc to cancel``, which IS in Decision's
    family) still resolves to Permission — ordered before Decision."""
    result = extract_interactive_content(_load("permission_bash_v2.1.190.txt"))
    assert result is not None
    assert result.name == "Permission"


def test_restore_checkpoint_stays_restore_never_decision(both_gates_on) -> None:
    """A RestoreCheckpoint pane (footer ``Enter to continue``, which IS in
    Decision's family) still resolves to RestoreCheckpoint (ordered before
    Decision), never Decision."""
    pane = (
        " Restore the code to a previous checkpoint?\n"
        " ❯ 1. Yes, restore\n"
        "   2. No, keep current\n"
        " Enter to continue · Esc to cancel\n"
    )
    result = extract_interactive_content(pane)
    assert result is not None
    assert result.name == "RestoreCheckpoint"


# ── Permission/Workflow gates never become Decision (footer narrowing + veto) ──
#
# Two independent mechanisms keep a real gate from surfacing as a Decision:
#   (Fix 1, Codex P2) Decision's footer REQUIRES ``Enter to (confirm|continue)``.
#     Real Permission / Workflow gates use ``Esc to cancel · Tab to amend`` — no
#     ``Enter to confirm`` line — so they fail Decision's footer scan outright.
#   (Fix, Hermes P2-4) The strict ``parse_permission_prompt`` /
#     ``parse_workflow_approval`` VETO, KEPT as defense-in-depth: even a
#     (synthetic) pane that passes Decision's footer is rejected if a strict
#     gate parser matches it.


@pytest.mark.parametrize(
    "fixture",
    [
        "permission_webfetch_v2.1.190.txt",
        "permission_bash_v2.1.190.txt",
        "permission_write_long_v2.1.190.txt",
        "workflow_dynamic_launch_v2.1.190.txt",
    ],
)
def test_real_gate_never_parses_as_decision(fixture: str) -> None:
    """``parse_generic_decision`` returns None on a real Permission / Workflow
    gate (its ``Esc to cancel`` footer carries no ``Enter to (confirm|continue)``
    → fails the footer scan; the strict veto is the backstop)."""
    assert parse_generic_decision(_load(fixture)) is None


def test_open_verb_bypass_closed(decision_on) -> None:
    """Codex P2 bypass: ``Do you want to open …?`` — ``open`` is OUTSIDE the
    permission verb whitelist, so ``parse_permission_prompt`` returns None and
    the veto MISSES it. Fix 1 closes it STRUCTURALLY: the pane's
    ``Esc to cancel · Tab to amend`` footer has no ``Enter to (confirm|continue)``
    component, so Decision's footer scan never matches — no card, no promotion,
    even with DECISION_CARDS ON and PERMISSION_PROMPTS OFF."""
    pane = (
        "Do you want to open https://example.com in the browser?\n"
        "❯ 1. Yes\n"
        "  2. No\n"
        "Esc to cancel · Tab to amend\n"
    )
    from cctelegram.terminal_parser import parse_permission_prompt

    assert parse_permission_prompt(pane) is None  # the verb-drift the veto misses
    assert parse_generic_decision(pane) is None  # footer narrowing rejects it
    assert tp.permission_prompts_enabled() is False
    assert extract_interactive_content(pane) is None


def test_strict_veto_still_fires_defense_in_depth(decision_on) -> None:
    """The KEPT veto is exercised directly: a (synthetic) pane whose footer is
    ``Esc to cancel · Enter to confirm`` passes BOTH the permission footer scan
    (``^\\s*Esc to cancel``) AND Decision's ``Enter to confirm`` requirement — so
    it reaches Decision step (4), where ``parse_permission_prompt`` matches
    (``Do you want to proceed?``) and vetoes it to None. Proves the veto is live
    defense-in-depth, not dead code."""
    from cctelegram.terminal_parser import parse_permission_prompt

    pane = (
        "Do you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel · Enter to confirm\n"
    )
    assert parse_permission_prompt(pane) is not None  # a real gate on this pane
    assert parse_generic_decision(pane) is None  # vetoed at step (4)
    assert extract_interactive_content(pane) is None


def test_permission_gate_not_reexposed_with_permission_flag_off(decision_on) -> None:
    """Cross-flag re-exposure fix: with ``CC_TELEGRAM_PERMISSION_PROMPTS`` OFF
    (Permission/Workflow filtered from the detector) and the Decision flag ON, a
    real permission / workflow gate falls THROUGH toward Decision — but it is
    NEVER surfaced (footer narrowing rejects it; the veto is the backstop).
    Defeating the permission flag via Decision is exactly what this prevents."""
    assert tp.permission_prompts_enabled() is False
    for fixture in (
        "permission_bash_v2.1.190.txt",
        "permission_webfetch_v2.1.190.txt",
        "permission_write_long_v2.1.190.txt",
        "workflow_dynamic_launch_v2.1.190.txt",
    ):
        assert extract_interactive_content(_load(fixture)) is None, fixture


# ── B2.1 parser hardening: footer-shape guard (§4 / P3-1) ─────────────────
#
# A Decision footer candidate must be footer-SHAPED, not merely CONTAIN the
# affirmative-commit phrase. A mid-redraw AUQ frame can transiently render the
# footer text INSIDE a numbered option's label ("3. Enter to confirm · Esc to
# cancel"); the bare ``_RE_DECISION_FOOTER.search`` accepted that option row as
# the footer and (with the B2 dispatch buttons) would mint a wrong-target card.


def test_footer_scan_rejects_numbered_option_with_footer_label() -> None:
    """§4 / P3-1: a numbered OPTION whose label embeds the confirmation-footer
    phrase must NOT be treated as the footer — so a mid-redraw AUQ-like frame
    does not classify as a Decision. Before the fix the option row
    ``3. Enter to confirm · Esc to cancel`` was accepted as the footer and folded
    into the option block; now the footer scan requires a genuinely
    footer-SHAPED line (not a numbered option / not ``❯``-cursored / all
    ``·``-separated key-hint segments), so there is no footer → None."""
    pane = (
        " Which option?\n"
        " ❯ 1. Do the thing\n"
        "   2. Skip it\n"
        "   3. Enter to confirm · Esc to cancel\n"
    )
    assert parse_generic_decision(pane) is None


def test_footer_scan_rejects_cursored_footer_label() -> None:
    """A ``❯``-cursored line carrying the footer phrase (a live-cursor option row
    mid-redraw, no leading number) is likewise not a footer — fail closed."""
    pane = " Proceed?\n   1. Yes\n   2. No\n ❯ Enter to confirm · Esc to cancel\n"
    assert parse_generic_decision(pane) is None


def test_footer_scan_rejects_non_hint_trailing_segment() -> None:
    """A line that carries ``Enter to confirm`` but ALSO trailing NON-hint prose
    after a ``·`` (e.g. assistant narration quoting the footer) is not
    footer-SHAPED — every ``·``-segment must be a key hint. Over-strict is the
    safe direction (costs only detection)."""
    pane = (
        " Proceed?\n"
        " ❯ 1. Yes\n"
        "   2. No\n"
        " Enter to confirm · here is some prose that is not a hint\n"
    )
    assert parse_generic_decision(pane) is None


def test_footer_shaped_real_footer_still_parses() -> None:
    """The genuine ``Enter to confirm · Esc to cancel`` footer stays
    footer-SHAPED and the real prompt still parses (no positive regression)."""
    pane = (
        " Proceed with the change?\n"
        " ❯ 1. Yes, do it\n"
        "   2. No, cancel\n"
        " Enter to confirm · Esc to cancel\n"
    )
    form = parse_generic_decision(pane)
    assert form is not None
    assert [o.number for o in form.options] == [1, 2]


# ── B2.1 parser hardening: title-instability on bound overflow (§5a/P3-3) ──


def test_decision_title_none_on_prompt_block_bound_overflow() -> None:
    """§5a / P3-3: when the upward prompt-block scan runs past the 10-line bound
    WITHOUT hitting a clean terminator (≥2 blank lines / a separator / a chrome
    line), the title becomes None — never a mid-paragraph fragment. The card
    still renders title-less (options + footer intact)."""
    body = "\n".join(f" paragraph line {i}" for i in range(1, 16))  # 15 lines, no gaps
    pane = body + "\n ❯ 1. Yes\n   2. No\n Enter to confirm · Esc to cancel\n"
    form = parse_generic_decision(pane)
    assert form is not None
    assert form.current_question_title is None
    # Still a usable form — the options + footer survive the title loss.
    assert [o.label for o in form.options] == ["Yes", "No"]
    assert [o.number for o in form.options] == [1, 2]
    assert form.options[0].cursor is True


def test_decision_title_kept_when_terminator_within_bound() -> None:
    """A short prompt block bounded by a clean terminator (a separator line)
    keeps its top line as the title — the bound-overflow None path fires ONLY on
    an unterminated run past the bound."""
    pane = (
        "────────────────────────────────────────\n"
        " Switch the branch?\n"
        " some context about the branch switch\n"
        " ❯ 1. Yes\n"
        "   2. No\n"
        " Enter to confirm · Esc to cancel\n"
    )
    form = parse_generic_decision(pane)
    assert form is not None
    assert form.current_question_title == "Switch the branch?"


def test_extract_title_none_decision_detected_flag_on(decision_on) -> None:
    """End-to-end: a title-less (bound-overflow) Decision pane is still DETECTED
    as a Decision through ``extract_interactive_content`` (the render path
    tolerates a None title — no mint gate is added in B2.1)."""
    body = "\n".join(f" paragraph line {i}" for i in range(1, 16))
    pane = body + "\n ❯ 1. Yes\n   2. No\n Enter to confirm · Esc to cancel\n"
    result = extract_interactive_content(pane)
    assert result is not None
    assert result.name == "Decision"


# ── Flag seeding contract (Hermes P2-1) ───────────────────────────────────


def test_reset_for_tests_resets_both_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reset_for_tests`` re-reads BOTH parser flags from the env (the reset
    seam) so neither leaks between tests."""
    set_permission_prompts_enabled(True)
    set_decision_cards_enabled(True)
    monkeypatch.delenv("CC_TELEGRAM_PERMISSION_PROMPTS", raising=False)
    monkeypatch.delenv("CC_TELEGRAM_DECISION_CARDS", raising=False)
    tp.reset_for_tests()
    assert tp.permission_prompts_enabled() is False
    assert tp.decision_cards_enabled() is False


def test_decision_flag_env_re_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Decision flag tracks ``CC_TELEGRAM_DECISION_CARDS`` on
    ``reset_for_tests`` (env truthiness), independent of the permission flag."""
    monkeypatch.setenv("CC_TELEGRAM_DECISION_CARDS", "on")
    monkeypatch.delenv("CC_TELEGRAM_PERMISSION_PROMPTS", raising=False)
    tp.reset_for_tests()
    assert tp.decision_cards_enabled() is True
    assert tp.permission_prompts_enabled() is False
    assert extract_interactive_content(_load(_TRUST)).name == "Decision"  # type: ignore[union-attr]
    monkeypatch.setenv("CC_TELEGRAM_DECISION_CARDS", "false")
    tp.reset_for_tests()
    assert tp.decision_cards_enabled() is False
    assert extract_interactive_content(_load(_TRUST)) is None


def test_flags_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two parser flags read distinct env vars and toggle independently."""
    monkeypatch.setenv("CC_TELEGRAM_PERMISSION_PROMPTS", "1")
    monkeypatch.setenv("CC_TELEGRAM_DECISION_CARDS", "0")
    tp.reset_for_tests()
    assert tp.permission_prompts_enabled() is True
    assert tp.decision_cards_enabled() is False


class TestConfigAndMainSeeding:
    """The config declaration + the ``main._run_bot`` seed (import-order-race
    dodge, mirroring the Permission flag)."""

    @pytest.fixture
    def _base_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
        monkeypatch.setenv("ALLOWED_USERS", "12345")
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))

    @pytest.mark.usefixtures("_base_env")
    def test_config_reads_decision_cards_flag_on(self, monkeypatch) -> None:
        from cctelegram.config import Config

        monkeypatch.setenv("CC_TELEGRAM_DECISION_CARDS", "on")
        assert Config().decision_cards_enabled is True

    @pytest.mark.usefixtures("_base_env")
    def test_config_decision_cards_defaults_off(self, monkeypatch) -> None:
        from cctelegram.config import Config

        monkeypatch.delenv("CC_TELEGRAM_DECISION_CARDS", raising=False)
        assert Config().decision_cards_enabled is False

    @pytest.mark.usefixtures("_base_env")
    def test_main_style_seed_overrides_import_time_read(self, monkeypatch) -> None:
        """Simulate the import-order race: the parser read OFF at import, then
        ``main._run_bot`` seeds it from config (which loaded ``.env``). The seed
        wins — exactly the reason the seeding exists (mirrors the Permission
        seed at ``main.py``)."""
        from cctelegram.config import Config

        set_decision_cards_enabled(False)  # the stale import-time read
        monkeypatch.setenv("CC_TELEGRAM_DECISION_CARDS", "true")
        cfg = Config()  # config loaded the (now-set) env
        tp.set_decision_cards_enabled(cfg.decision_cards_enabled)  # the main seed
        assert tp.decision_cards_enabled() is True


# ── Route-state non-regression at the status_polling seam (Hermes P2-3) ────


@pytest.mark.usefixtures("fresh_handler_state")
class TestRouteStateNonRegression:
    """A representative NEGATIVE pane driven through the real
    ``update_status_message`` seam must NOT flip a RUNNING route to
    WAITING_ON_USER via ``mark_interactive_pending`` (a parser non-match alone is
    necessary but not sufficient — this exercises the route_runtime promotion
    path end-to-end)."""

    @pytest.fixture
    def mock_bot(self):
        bot = AsyncMock()
        sent = MagicMock()
        sent.message_id = 999
        bot.send_message.return_value = sent
        return bot

    @pytest.mark.asyncio
    async def test_negative_pane_does_not_promote_running_to_waiting(
        self, mock_bot: AsyncMock
    ) -> None:
        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling
        from cctelegram.route_runtime import RunState, TranscriptLifecycleEvent

        set_permission_prompts_enabled(True)
        set_decision_cards_enabled(True)

        window_id = "@7"
        user_id, thread_id = 1, 42
        route = (user_id, thread_id, window_id)

        # Seed RUNNING (empty open_tools) — the promotable state.
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

        mock_window = MagicMock()
        mock_window.window_id = window_id
        negative_pane = _load(_NEG_QUOTED)

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
            mock_tmux.capture_pane = AsyncMock(return_value=negative_pane)

            await status_polling.update_status_message(
                mock_bot, user_id=user_id, window_id=window_id, thread_id=thread_id
            )

        # The negative pane is not an interactive UI → no interactive dispatch
        # and NO promotion: the route is still RUNNING, never WAITING_ON_USER.
        mock_handle_ui.assert_not_called()
        assert route_runtime.snapshot(route).run_state is RunState.RUNNING

    @pytest.mark.asyncio
    async def test_positive_control_decision_pane_promotes(
        self, mock_bot: AsyncMock
    ) -> None:
        """The seam IS wired: a REAL Decision pane (flag ON) that publishes a
        card DOES promote RUNNING → WAITING_ON_USER (so the negative test above
        is not vacuously green)."""
        from cctelegram import route_runtime
        from cctelegram.handlers import status_polling
        from cctelegram.route_runtime import RunState, TranscriptLifecycleEvent

        set_decision_cards_enabled(True)

        window_id = "@8"
        user_id, thread_id = 1, 42
        route = (user_id, thread_id, window_id)

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
            mock_tmux.capture_pane = AsyncMock(return_value=_load(_TRUST))

            await status_polling.update_status_message(
                mock_bot, user_id=user_id, window_id=window_id, thread_id=thread_id
            )

        mock_handle_ui.assert_awaited_once()
        assert route_runtime.snapshot(route).run_state is RunState.WAITING_ON_USER
