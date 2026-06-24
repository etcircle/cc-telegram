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
