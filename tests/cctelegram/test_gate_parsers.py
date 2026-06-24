"""Interactive approval-gate detection + parsing (PR-1, display-only).

Drives the real ``terminal_parser`` against the committed Wave-0 v2.1.190
fixtures (``tests/cctelegram/fixtures/permission_*.txt`` /
``workflow_*.txt``). Covers:

  - detection: each gate pane → ``extract_interactive_content`` returns the
    right name (flag ON), AND the collision matrix in BOTH directions (an AUQ
    pane stays AskUserQuestion, an EPM pane stays ExitPlanMode, never a gate);
  - the strict parsers ``parse_permission_prompt`` / ``parse_workflow_approval``
    → options + the deterministic ``(esc)`` affordance strip (S-6 full-label
    parity);
  - the detector kill-switch: flag OFF (with NO bot token in the environment)
    → ``extract_interactive_content`` returns None for both gate panes (proves
    the leaf has no config import);
  - synthetic negative-prose fixtures (assistant text QUOTING a gate) → NO
    match (S-8);
  - the §1.1 visible-pane-liveness decision (the new ``_PICKER_ANCHOR_MARKERS``
    permission anchor is UNNECESSARY — the captured shapes redraw in place).

The ``_reset_terminal_parser_flag`` autouse fixture (leaf conftest) re-reads
the flag from the env before/after each test, so a test that flips it never
leaks. Tests that need the gate ON call ``set_permission_prompts_enabled(True)``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cctelegram import terminal_parser as tp
from cctelegram.terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
    parse_permission_prompt,
    parse_workflow_approval,
    set_permission_prompts_enabled,
    visible_pane_liveness,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


_PERMISSION_FIXTURES = (
    "permission_webfetch_v2.1.190.txt",
    "permission_bash_v2.1.190.txt",
    "permission_write_long_v2.1.190.txt",
    "permission_write_long_visible_v2.1.190.txt",
)
_WORKFLOW_FIXTURES = (
    "workflow_dynamic_launch_v2.1.190.txt",
    "workflow_dynamic_launch_visible_v2.1.190.txt",
)


@pytest.fixture
def gate_on():
    """Enable gate detection for the test body (reset by the leaf autouse)."""
    set_permission_prompts_enabled(True)
    yield
    set_permission_prompts_enabled(False)


# Synthetic negative-prose fixtures (S-8) — committed alongside the real
# captures: assistant text that QUOTES a gate's distinctive strings WITHOUT the
# real option-block + footer co-occurrence. These must NOT light a card.
_NEG_PERMISSION_FIXTURE = "permission_negative_prose_v2.1.190.txt"
_NEG_WORKFLOW_FIXTURE = "workflow_negative_prose_v2.1.190.txt"


# ── Detection (flag ON) ───────────────────────────────────────────────────


@pytest.mark.parametrize("fixture", _PERMISSION_FIXTURES)
def test_permission_pane_detected(gate_on, fixture: str) -> None:
    result = extract_interactive_content(_load(fixture))
    assert result is not None, fixture
    assert result.name == "Permission", fixture


@pytest.mark.parametrize("fixture", _WORKFLOW_FIXTURES)
def test_workflow_pane_detected(gate_on, fixture: str) -> None:
    result = extract_interactive_content(_load(fixture))
    assert result is not None, fixture
    assert result.name == "Workflow", fixture


# ── Collision matrix (both directions) ────────────────────────────────────


def test_epm_pane_stays_exitplanmode_not_workflow(gate_on) -> None:
    """The shared Esc/ctrl+g footer family must NOT let an EPM pane match
    Workflow — disambiguated on the TOP anchor (Risks #1)."""
    result = extract_interactive_content(_load("epm_v2170_ctrl_plus_g.txt"))
    assert result is not None
    assert result.name == "ExitPlanMode"


def test_workflow_pane_is_not_epm(gate_on) -> None:
    """And the Workflow pane must NOT match ExitPlanMode."""
    result = extract_interactive_content(_load("workflow_dynamic_launch_v2.1.190.txt"))
    assert result is not None
    assert result.name == "Workflow"


def test_permission_pane_is_not_workflow_or_epm(gate_on) -> None:
    for fixture in _PERMISSION_FIXTURES:
        result = extract_interactive_content(_load(fixture))
        assert result is not None and result.name == "Permission", fixture


def test_auq_pane_stays_askuserquestion(gate_on) -> None:
    """A plain single-select AUQ pane is still AskUserQuestion with the gate
    patterns active (ordered LAST → first-match-wins protects AUQ)."""
    pane = (
        "  Which lane?\n"
        "❯ 1. A) Ship now\n"
        "  2. B) Bake first\n"
        "  Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )
    result = extract_interactive_content(pane)
    assert result is not None
    assert result.name == "AskUserQuestion"


def test_gate_parsers_decline_other_uis(gate_on) -> None:
    """The strict gate parsers return None on an EPM / AUQ pane."""
    epm = _load("epm_v2170_ctrl_plus_g.txt")
    assert parse_workflow_approval(epm) is None
    assert parse_permission_prompt(epm) is None
    wf = _load("workflow_dynamic_launch_v2.1.190.txt")
    assert parse_permission_prompt(wf) is None  # Workflow is not a permission gate
    perm = _load("permission_webfetch_v2.1.190.txt")
    assert parse_workflow_approval(perm) is None


# ── parse_permission_prompt: options + affordance strip (S-6) ─────────────


@pytest.mark.parametrize(
    ("fixture", "title", "labels"),
    [
        (
            "permission_webfetch_v2.1.190.txt",
            "Do you want to allow Claude to fetch this content?",
            [
                "Yes",
                "Yes, and don't ask again for example.com",
                "No, and tell Claude what to do differently",
            ],
        ),
        (
            "permission_bash_v2.1.190.txt",
            "Do you want to proceed?",
            [
                "Yes",
                "Yes, and always allow access to gatecap-work/ from this project",
                "No",
            ],
        ),
        (
            "permission_write_long_v2.1.190.txt",
            "Do you want to create poem.txt?",
            [
                "Yes",
                "Yes, allow all edits during this session (shift+tab)",
                "No",
            ],
        ),
    ],
)
def test_parse_permission_options(fixture: str, title: str, labels: list[str]) -> None:
    form = parse_permission_prompt(_load(fixture))
    assert form is not None, fixture
    assert form.select_mode == "single"
    assert form.is_review_screen is False
    assert form.current_question_title == title
    assert [o.label for o in form.options] == labels
    assert [o.number for o in form.options] == [1, 2, 3]


@pytest.mark.parametrize("fixture", _PERMISSION_FIXTURES)
def test_permission_esc_affordance_stripped(fixture: str) -> None:
    """No option label carries a trailing ``(esc)`` / ``(Esc)`` affordance."""
    form = parse_permission_prompt(_load(fixture))
    assert form is not None, fixture
    for opt in form.options:
        assert "(esc)" not in opt.label.lower(), opt.label


def test_permission_affordance_strip_deterministic() -> None:
    """The strip is identical on every parse (mint==verify parity, S-6)."""
    pane = _load("permission_webfetch_v2.1.190.txt")
    a = parse_permission_prompt(pane)
    b = parse_permission_prompt(pane)
    assert a is not None and b is not None
    assert [o.label for o in a.options] == [o.label for o in b.options]
    assert a.fingerprint() == b.fingerprint()


def test_permission_full_label_distinguishes_yes_variants() -> None:
    """S-6: "Yes" and "Yes, and don't ask again …" carry the FULL distinct text
    so a later loose-label match cannot confuse the safe Yes with the
    persistent-allow option."""
    form = parse_permission_prompt(_load("permission_webfetch_v2.1.190.txt"))
    assert form is not None
    assert form.options[0].label == "Yes"
    assert form.options[1].label.startswith("Yes, and don't ask again")
    assert form.options[0].label != form.options[1].label


# ── parse_workflow_approval: options + body ───────────────────────────────


@pytest.mark.parametrize("fixture", _WORKFLOW_FIXTURES)
def test_parse_workflow_options(fixture: str) -> None:
    form = parse_workflow_approval(_load(fixture))
    assert form is not None, fixture
    assert form.select_mode == "single"
    assert form.is_review_screen is False
    assert form.current_question_title == "Run a dynamic workflow?"
    assert [o.label for o in form.options] == ["Yes, run it", "View raw script", "No"]
    assert [o.number for o in form.options] == [1, 2, 3]


def test_workflow_body_carries_phases_and_warning() -> None:
    """The phases + token-cost warning are available for the card body."""
    form = parse_workflow_approval(_load("workflow_dynamic_launch_v2.1.190.txt"))
    assert form is not None
    body = form._meta.get("workflow_body", "")
    assert "phases" in body
    assert "Summarize" in body  # the phase line
    assert "Dynamic workflows can use a lot of tokens" in body
    assert form._meta.get("has_token_warning") == "1"


# ── Detector kill-switch (flag OFF, NO bot token) ─────────────────────────


@pytest.mark.parametrize("fixture", _PERMISSION_FIXTURES + _WORKFLOW_FIXTURES)
def test_flag_off_no_detection(monkeypatch: pytest.MonkeyPatch, fixture: str) -> None:
    """Flag OFF with NO TELEGRAM_BOT_TOKEN in the environment →
    ``extract_interactive_content`` returns None for both gate panes. Proves
    the leaf reads a LOCAL env flag and never imports ``config`` (which would
    raise without a token). The autouse reset leaves the flag OFF by default."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_USERS", raising=False)
    monkeypatch.delenv("CC_TELEGRAM_PERMISSION_PROMPTS", raising=False)
    tp.reset_for_tests()  # re-read env → OFF
    assert tp.permission_prompts_enabled() is False
    assert extract_interactive_content(_load(fixture)) is None, fixture


def test_flag_off_via_env_truthy_re_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag tracks the env var on ``reset_for_tests`` (the reset seam)."""
    monkeypatch.setenv("CC_TELEGRAM_PERMISSION_PROMPTS", "true")
    tp.reset_for_tests()
    assert tp.permission_prompts_enabled() is True
    assert (
        extract_interactive_content(_load("permission_bash_v2.1.190.txt")).name  # type: ignore[union-attr]
        == "Permission"
    )
    monkeypatch.setenv("CC_TELEGRAM_PERMISSION_PROMPTS", "false")
    tp.reset_for_tests()
    assert tp.permission_prompts_enabled() is False
    assert extract_interactive_content(_load("permission_bash_v2.1.190.txt")) is None


# ── Negative prose (S-8) ──────────────────────────────────────────────────


def test_negative_permission_prose_no_match(gate_on) -> None:
    """Assistant text quoting "Do you want to allow …" without a real option
    block + footer must NOT light a Permission card (S-8)."""
    pane = _load(_NEG_PERMISSION_FIXTURE)
    assert extract_interactive_content(pane) is None
    assert parse_permission_prompt(pane) is None


def test_negative_workflow_prose_no_match(gate_on) -> None:
    """Assistant text quoting the Workflow strings without the option block +
    Esc footer must NOT light a Workflow card (S-8)."""
    pane = _load(_NEG_WORKFLOW_FIXTURE)
    assert extract_interactive_content(pane) is None
    assert parse_workflow_approval(pane) is None


# ── §1.1 visible-pane-liveness decision ───────────────────────────────────


def test_long_permission_visible_liveness_present(gate_on) -> None:
    """§1.1 DECISION: the captured long-preview permission gate redraws IN
    PLACE — the question stays adjacent to the options at the visible bottom,
    so ``visible_pane_liveness(visible)`` already returns "present" via the
    existing ``is_interactive_ui`` leg (the new Permission pattern matches the
    full visible slice). The planned new ``_PICKER_ANCHOR_MARKERS`` permission
    anchor is therefore UNNECESSARY and was NOT added."""
    visible = _load("permission_write_long_visible_v2.1.190.txt")
    assert visible_pane_liveness(visible) == "present"
    assert is_interactive_ui(visible) is True


def test_webfetch_no_footer_visible_liveness_present(gate_on) -> None:
    """The WebFetch gate has NO ``Esc to cancel`` footer (only the inline
    ``(esc)`` option), so ``is_picker_anchor_visible`` alone would miss it —
    but ``is_interactive_ui`` (the Permission pattern over the full visible
    pane) lights it "present" with the gate flag ON."""
    visible = _load("permission_webfetch_v2.1.190.txt")
    assert visible_pane_liveness(visible) == "present"


# ── S-8 fail-closed: quoted-prompt false positives must NOT detect (P1) ────
#
# The loose top+bottom ``UIPattern`` regexes alone matched assistant prose
# that QUOTES a gate (and even a fully-quoted gate followed by more prose).
# ``extract_interactive_content`` MUST run the strict variant parser AND a
# bottom-terminal requirement (a live gate's footer is at/near the pane
# bottom — only known chrome may follow it). These RED-first negatives pin
# the fail-closed behavior; each is a pane that LOOKS like a gate but is not
# the live active prompt.

# (a) "Claude wants to ..." + footer, NO numbered options. The preamble is
# OPTIONAL context only (Hermes P2) -- without the real question line AND an
# option block it must not light a Permission card.
_NEG_CLAUDE_WANTS_NO_OPTIONS = (
    "When Claude wants to fetch a URL it shows a prompt.\n"
    "\n"
    " Claude wants to fetch content from example.com\n"
    " Some prose explaining what that means, with no option block at all.\n"
    " Esc to cancel . Tab to amend\n"
)

# (b) A permission question + numbered-looking prose + footer, but the gate is
# NOT at the pane bottom -- assistant prose follows the footer (a quoted /
# explained prompt, not the live active one).
_NEG_PERMISSION_NOT_AT_BOTTOM = (
    "For reference, the prompt looks like this:\n"
    "\n"
    " Do you want to proceed?\n"
    " 1. Yes\n"
    "   2. Yes, and always allow\n"
    "   3. No\n"
    " Esc to cancel . Tab to amend\n"
    "\n"
    "So you would normally pick option 1. But I have already finished, so\n"
    "there is nothing for you to approve right now.\n"
)

# (c) "Run a dynamic workflow?" / "Dynamic workflows can use ..." quoted in
# prose, footer present, NO live option block.
_NEG_WORKFLOW_NO_OPTIONS = (
    "A dynamic workflow gate normally shows:\n"
    "\n"
    " Run a dynamic workflow?\n"
    " Dynamic workflows can use a lot of tokens quickly by running subagents.\n"
    " Esc to cancel . Tab to amend\n"
)

# (d) A COMPLETE quoted Workflow block (real-looking options + footer) FOLLOWED
# BY trailing assistant prose -- the third Hermes repro (passes the strict
# parse on its own, so only the bottom-terminal requirement rejects it).
_NEG_WORKFLOW_COMPLETE_THEN_PROSE = (
    "Here is what the workflow gate looks like when it appears:\n"
    "\n"
    " Run a dynamic workflow?\n"
    " This dynamic workflow will spin up subagents.\n"
    " Dynamic workflows can use a lot of tokens quickly.\n"
    " 1. Yes, run it\n"
    "   2. View raw script\n"
    "   3. No\n"
    " Esc to cancel . Tab to amend\n"
    "\n"
    "As you can see, you would tap option 1 to proceed. Tell me what to do.\n"
)


@pytest.mark.parametrize(
    ("pane", "gate"),
    [
        (_NEG_CLAUDE_WANTS_NO_OPTIONS, "Permission"),
        (_NEG_PERMISSION_NOT_AT_BOTTOM, "Permission"),
        (_NEG_WORKFLOW_NO_OPTIONS, "Workflow"),
        (_NEG_WORKFLOW_COMPLETE_THEN_PROSE, "Workflow"),
    ],
)
def test_quoted_prompt_shapes_do_not_detect(gate_on, pane: str, gate: str) -> None:
    """S-8 fail-closed: a quoted / explained / non-bottom gate must NOT light a
    card even with the flag ON (the strict-parse + bottom-terminal gate)."""
    assert extract_interactive_content(pane) is None, gate


def test_claude_wants_to_without_question_or_options_no_match(gate_on) -> None:
    """Hermes P2: ``Claude wants to `` is OPTIONAL context, never a sufficient
    standalone top anchor -- without the real ``Do you want to ...?`` question
    line + an option block, no Permission card."""
    pane = (
        "Earlier today:\n"
        "\n"
        " Claude wants to fetch content from evil.example.com\n"
        "   3. No, and tell Claude what to do differently (esc)\n"
    )
    assert extract_interactive_content(pane) is None
    assert parse_permission_prompt(pane) is None


def test_complete_quoted_workflow_then_prose_strict_parse_rejects(gate_on) -> None:
    """The bottom-terminal requirement: a complete-looking Workflow block with
    trailing assistant prose is NOT the live gate -- ``parse_workflow_approval``
    returns None (the footer is not at/near the pane bottom)."""
    assert parse_workflow_approval(_NEG_WORKFLOW_COMPLETE_THEN_PROSE) is None


def test_complete_quoted_permission_then_prose_strict_parse_rejects(gate_on) -> None:
    """Same bottom-terminal requirement for the Permission variant."""
    assert parse_permission_prompt(_NEG_PERMISSION_NOT_AT_BOTTOM) is None


# ── Codex P2: Workflow phase lines must NOT be absorbed as options ─────────


def test_workflow_phase_list_adjacent_to_options_parses_real_options(gate_on) -> None:
    """A Workflow whose numbered PHASE list sits directly above the option
    block (no intervening prose paragraph) must still parse the REAL options
    (``Yes, run it`` / ``View raw script`` / ``No``), not the phase lines.
    The parser anchors on the bottom-most contiguous numbered block above the
    footer + validates the Workflow label shape."""
    pane = (
        "Workflow(Do a thing)\n"
        "\n"
        "-----------------------------------------------------------------\n"
        " Run a dynamic workflow?\n"
        "\n"
        "  This dynamic workflow will spin up multiple subagents across the\n"
        "  following phases:\n"
        "  1. Sweep - 5 parallel researchers\n"
        "  2. Verify - re-check the riskiest claims\n"
        "  3. Dossier - one agent builds the dossier\n"
        "  1. Yes, run it\n"
        "    2. View raw script\n"
        "    3. No\n"
        "  Esc to cancel . Tab to amend\n"
        "  ctrl+g to edit script in $EDITOR\n"
    )
    form = parse_workflow_approval(pane)
    assert form is not None
    assert [o.label for o in form.options] == ["Yes, run it", "View raw script", "No"]
    assert [o.number for o in form.options] == [1, 2, 3]
    result = extract_interactive_content(pane)
    assert result is not None and result.name == "Workflow"


def test_workflow_wrong_option_labels_rejected(gate_on) -> None:
    """The Workflow strict parser validates the option labels against the known
    shape -- a numbered block that is NOT the Yes/View/No options (only the
    phase list, no real option block) returns None rather than a bogus form."""
    pane = (
        "Workflow(Do a thing)\n"
        "\n"
        "-----------------------------------------------------------------\n"
        " Run a dynamic workflow?\n"
        "  This dynamic workflow will spin up subagents across phases:\n"
        "  1. Sweep - 5 parallel researchers\n"
        "  2. Verify - re-check the riskiest claims\n"
        "  3. Dossier - one agent builds the dossier\n"
        " Esc to cancel . Tab to amend\n"
    )
    assert parse_workflow_approval(pane) is None
    assert extract_interactive_content(pane) is None


# ── Codex P2: terminal_parser imports config-free (ISOLATED subprocess) ────


def test_terminal_parser_imports_without_config_isolated() -> None:
    """``terminal_parser`` is a pure stdlib leaf — importing it AND reading the
    gate flag accessor must succeed with NO ``TELEGRAM_BOT_TOKEN`` /
    ``ALLOWED_USERS`` in the environment (``config`` RAISES without them). The
    in-suite ``test_flag_off_no_detection`` can't prove this — the root conftest
    sets a dummy token before collection, so the parent process already has one.
    Run a FRESH subprocess with the token vars stripped: a non-zero exit (or any
    stderr) would mean ``terminal_parser`` pulled in ``config`` at module load
    (the leaf-purity break the kill-switch design forbids, Hermes P2-3)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in ("TELEGRAM_BOT_TOKEN", "ALLOWED_USERS", "CC_TELEGRAM_PERMISSION_PROMPTS")
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import cctelegram.terminal_parser as tp; "
            "print(tp.permission_prompts_enabled())",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        "`import cctelegram.terminal_parser` failed with no bot token — the "
        "leaf likely imports `config` at module load.\n"
        f"--- stderr ---\n{result.stderr}"
    )
    # Flag OFF by default (no env var) — and the import had no config side effect.
    assert result.stdout.strip() == "False", result.stdout


# ── Bottom-terminal: trailing input-box chrome does NOT false-reject ───────


@pytest.mark.parametrize("fixture", _PERMISSION_FIXTURES)
def test_permission_with_trailing_input_box_chrome_still_detects(
    gate_on, fixture: str
) -> None:
    """A LIVE permission gate followed by the Claude Code input-box / status-bar
    chrome (the real live-pane shape) still detects — the bottom-terminal check
    only rejects arbitrary assistant PROSE below the footer, not chrome."""
    live = _load(fixture) + (
        "\n"
        "────────────────────────────────────────\n"
        "❯ \n"
        "────────────────────────────────────────\n"
        "  Opus 4.8 · Context left: 42%\n"
    )
    result = extract_interactive_content(live)
    assert result is not None, fixture
    assert result.name == "Permission", fixture


def test_workflow_with_trailing_ctrlg_and_chrome_still_detects(gate_on) -> None:
    """The Workflow ``ctrl+g to edit script`` line + input-box chrome below the
    footer are known chrome — the gate still detects."""
    live = _load("workflow_dynamic_launch_v2.1.190.txt") + (
        "\n────────────────────────────────────────\n❯ \n  Opus 4.8 · Context: 42%\n"
    )
    result = extract_interactive_content(live)
    assert result is not None and result.name == "Workflow"
