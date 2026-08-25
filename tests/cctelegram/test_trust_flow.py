"""Unit coverage for the GH #65 folder-trust creation-flow lane.

Covers the pure, fixture-CONSUMING pieces of the lane:
  - Fix 0's nonce-delimited in-pane ``--version`` probe (whole-line fullmatch
    against the REAL wrapped-echo rig captures, env-prefix preservation, the
    positive ``(Claude Code)`` proof);
  - Fix 1's slice classifier (registration first, pane COMMAND before pane
    TEXT, blank ⇒ indeterminate) against the REAL 2.1.239 / 2.1.241 frames;
  - Fix 4's typed cleanup arbitration + its declared linearization point;
  - Fix 2's two-gate kill-switch posture and the dispatch-table entries;
  - the budget knobs' config validation.

The end-to-end lane (card render, tap → dispatch, teardown, inbound re-read)
lives in ``tests/scenarios/test_trust_card_flow.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cctelegram import terminal_parser, tmux_manager as tmux_mod
from cctelegram.handlers import decision_token, trust_flow
from cctelegram.utils import app_dir

_FIXTURES = Path(__file__).parent / "fixtures"


def _fx(name: str) -> str:
    return (_FIXTURES / name).read_text()


# The version-probe rig captures: the echoed command line WRAPS, so nonce-A and
# nonce-B each end an echoed line as well as their own ``printf`` output line.
_PROBE_NONCES = {
    "version_probe_plain_v2.1.241.txt": ("CCTGVERA9f31", "CCTGVERB9f31", "2.1.241"),
}


@pytest.fixture(autouse=True)
def _decision_cards_on() -> Any:
    """The Decision detector gates ``parse_generic_decision``'s extractor twin."""
    terminal_parser.set_decision_cards_enabled(True)
    yield
    terminal_parser.reset_for_tests()
    decision_token.reset_for_tests()


# ── Fix 0: the nonce-delimited version probe ─────────────────────────────────


def test_probe_parser_extracts_version_between_whole_line_nonces() -> None:
    """The REAL rig capture parses — and only via a whole-line FULLMATCH."""
    pane = _fx("version_probe_plain_v2.1.241.txt")
    nonce_a, nonce_b, expected = _PROBE_NONCES["version_probe_plain_v2.1.241.txt"]
    assert tmux_mod.parse_probe_version(pane, nonce_a, nonce_b) == expected


def test_probe_parser_ignores_the_wrapped_echoed_command_line() -> None:
    """Addendum item 4: the echoed command carries BOTH nonces and wraps.

    A "line CONTAINS nonce-A, next line CONTAINS nonce-B, take what's between"
    scan finds an EMPTY region on this exact capture. Prove the failure mode is
    real (the echo lines do contain the nonces, and are NOT whole-line matches)
    and that the shipped parser still resolves the version.
    """
    pane = _fx("version_probe_plain_v2.1.241.txt")
    nonce_a, nonce_b, expected = _PROBE_NONCES["version_probe_plain_v2.1.241.txt"]
    lines = pane.splitlines()
    containing_a = [line for line in lines if nonce_a in line]
    echoed = [line for line in containing_a if line.strip() != nonce_a]
    assert echoed, "the rig capture must contain the wrapped echoed command"
    assert any(nonce_b in line for line in lines if line.strip() != nonce_b)
    # The naive scan: first line CONTAINING A, first line after it CONTAINING B.
    naive_start = next(i for i, line in enumerate(lines) if nonce_a in line)
    naive_end = next(
        i for i in range(naive_start + 1, len(lines)) if nonce_b in lines[i]
    )
    naive_region = [line for line in lines[naive_start + 1 : naive_end] if line.strip()]
    assert naive_region == [], "the naive scan must be the one that finds nothing"
    # The shipped whole-line fullmatch parser still resolves it.
    assert tmux_mod.parse_probe_version(pane, nonce_a, nonce_b) == expected


def test_probe_parser_requires_the_claude_code_proof() -> None:
    """A wrapper reporting its OWN version fails the positive proof."""
    pane = "A1\n1.2.3 (my-wrapper)\nB1\n"
    assert tmux_mod.parse_probe_version(pane, "A1", "B1") is None
    assert tmux_mod.parse_probe_version(
        "A1\n9.9.9 (Claude Code)\nB1\n", "A1", "B1"
    ) == ("9.9.9")


@pytest.mark.parametrize(
    "pane",
    [
        "A1\n2.1.241 (Claude Code)\n",  # no closing nonce
        "2.1.241 (Claude Code)\nB1\n",  # no opening nonce
        "A1\nB1\n",  # nothing between them
        "A1\n2.1.241 (Claude Code)\n2.1.240 (Claude Code)\nB1\n",  # ambiguous
    ],
)
def test_probe_parser_fails_closed(pane: str) -> None:
    assert tmux_mod.parse_probe_version(pane, "A1", "B1") is None


def test_probe_command_prefix_preserves_env_assignments_unquoted_names() -> None:
    """A ``PATH=…`` prefix changes resolution, so it must apply to the probe —
    and its NAME must stay unquoted or the shell stops treating it as an
    assignment."""
    prefix = tmux_mod.probe_command_prefix(
        "DISABLE_AUTOUPDATER=1 PATH=/opt/bin:/usr/bin /opt/cc/claude --settings x"
    )
    assert prefix == "DISABLE_AUTOUPDATER=1 PATH=/opt/bin:/usr/bin /opt/cc/claude"
    assert not prefix.startswith("'")


def test_probe_command_prefix_quotes_values_and_binary_but_drops_args() -> None:
    # An unquoted space ends the assignment VALUE, so the next token is the
    # binary and everything after it is dropped.
    assert tmux_mod.probe_command_prefix("FOO=a b --resume x") == "FOO=a b"
    # A quoted value stays one token and is re-quoted; the binary path with a
    # space is quoted too, and the trailing args are dropped.
    assert (
        tmux_mod.probe_command_prefix("FOO='a b' '/opt/my cc/claude' --resume x")
        == "FOO='a b' '/opt/my cc/claude'"
    )


@pytest.mark.parametrize("command", ["", "   ", "FOO=1", "'unterminated"])
def test_probe_command_prefix_unextractable_is_none(command: str) -> None:
    assert tmux_mod.probe_command_prefix(command) is None


def test_compose_version_probe_is_nonce_delimited() -> None:
    probe = tmux_mod.compose_version_probe("claude --dangerously", "AAA", "BBB")
    assert probe == "printf 'AAA\\n'; claude --version; printf 'BBB\\n'"
    assert tmux_mod.compose_version_probe("FOO=1", "AAA", "BBB") is None


# ── Fix 1: the slice classifier ──────────────────────────────────────────────


def test_registration_wins_even_when_the_pane_already_returned_to_shell() -> None:
    """Addendum item 2 ordering: the REGISTRATION-MAP check stays FIRST."""
    assert (
        trust_flow.classify_slice(
            registered=True,
            pane_command="zsh",
            pane_text=_fx("folder_trust_postesc_t4_plain_v2.1.241.txt"),
        )
        is trust_flow.SliceKind.REGISTERED
    )


@pytest.mark.parametrize(
    "fixture",
    [
        "folder_trust_postdigit2_t2_plain_v2.1.241.txt",
        "folder_trust_postesc_t4_plain_v2.1.241.txt",
    ],
)
def test_stale_prompt_text_on_a_shell_pane_classifies_shell(fixture: str) -> None:
    """A dead pane RETAINS the trust prompt text — the pane COMMAND decides.

    These are the REAL post-digit-commit and post-Escape captures: ``claude``
    has exited to ``zsh`` but the prompt block is still painted above the shell
    prompt. A text-only liveness check would render a LIVE card onto a corpse;
    the shipped classifier must return SHELL (⇒ guarded cleanup).
    """
    pane = _fx(fixture)
    assert "Enter to confirm" in pane, "the corpse must still show the prompt text"
    assert (
        trust_flow.classify_slice(registered=False, pane_command="zsh", pane_text=pane)
        is trust_flow.SliceKind.SHELL
    )


def test_non_claude_non_shell_command_with_trust_text_is_indeterminate() -> None:
    """The npm ``claude.exe`` shape: no card, no kill — the global-ceiling spare.

    Widening ``pane_command_is_claude`` for ``claude.exe`` is a SEPARATE issue;
    this lane must degrade to the documented fail-open spare.
    """
    assert not tmux_mod.pane_command_is_claude("claude.exe")
    assert not tmux_mod.pane_command_is_shell("claude.exe")
    assert (
        trust_flow.classify_slice(
            registered=False,
            pane_command="claude.exe",
            pane_text=_fx("folder_trust_arrival_plain_v2.1.241.txt"),
        )
        is trust_flow.SliceKind.INDETERMINATE
    )


def test_blank_post_enter_frame_is_indeterminate_never_a_failure() -> None:
    """Addendum item 3: the T+1s post-Enter capture is 51 blank lines."""
    pane = _fx("folder_trust_postenter_t1_plain_v2.1.241.txt")
    assert pane.strip() == "", "the 2.1.241 rig frame must be genuinely blank"
    assert (
        trust_flow.classify_slice(
            registered=False, pane_command="2.1.241", pane_text=pane
        )
        is trust_flow.SliceKind.INDETERMINATE
    )


@pytest.mark.parametrize(
    "fixture",
    [
        "folder_trust_arrival_plain_v2.1.239.txt",
        "folder_trust_arrival_plain_v2.1.241.txt",
        "folder_trust_postdown_plain_v2.1.241.txt",
        "folder_trust_e2c_navto2_plain_v2.1.241.txt",
    ],
)
def test_live_trust_frame_under_a_claude_pane_classifies_trust(fixture: str) -> None:
    assert (
        trust_flow.classify_slice(
            registered=False, pane_command="claude", pane_text=_fx(fixture)
        )
        is trust_flow.SliceKind.TRUST_FRAME
    )


def test_the_rig_frames_prove_arrows_WRAP_not_clamp() -> None:
    """Addendum item 1, from the REAL captures — the AUQ clamp does NOT hold.

    ``Down`` from option 1 lands on option 2; a SECOND ``Down`` lands back on
    option 1. That is why nav is exact-step from the parsed cursor and the
    pre-Enter verify is the ONLY licence to commit: an overshoot silently sits
    on the WRONG option instead of clamping at the end of the list.
    """
    from cctelegram.terminal_parser import parse_generic_decision

    def _cursor(fixture: str) -> int | None:
        form = parse_generic_decision(_fx(fixture))
        assert form is not None
        return next(o.number for o in form.options if o.cursor)

    assert _cursor("folder_trust_arrival_plain_v2.1.241.txt") == 1
    assert _cursor("folder_trust_postdown_plain_v2.1.241.txt") == 2
    assert _cursor("folder_trust_postdown2_plain_v2.1.241.txt") == 1  # WRAPPED
    assert _cursor("folder_trust_postup_plain_v2.1.241.txt") == 2  # WRAPPED
    assert _cursor("folder_trust_e2c_navto2_plain_v2.1.241.txt") == 2


def test_running_repl_with_no_prompt_classifies_running() -> None:
    assert (
        trust_flow.classify_slice(
            registered=False,
            pane_command="2.1.241",
            pane_text=_fx("inputbox_idle_v2.1.207.txt"),
        )
        is trust_flow.SliceKind.RUNNING
    )


def test_unreadable_pane_command_is_indeterminate() -> None:
    assert (
        trust_flow.classify_slice(
            registered=False, pane_command=None, pane_text="anything"
        )
        is trust_flow.SliceKind.INDETERMINATE
    )


# ── Fix 4: typed cleanup + the linearization point ───────────────────────────


class _KillRecorder:
    def __init__(self, *, result: bool | BaseException = True) -> None:
        self.calls: list[str] = []
        self._result = result

    async def kill_window(self, window_id: str) -> bool:
        self.calls.append(window_id)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _write_session_map(window_id: str, session_id: str) -> None:
    from cctelegram.config import config

    path = app_dir() / "session_map.json"
    current: dict[str, Any] = {}
    if path.exists():
        current = json.loads(path.read_text())
    current[f"{config.tmux_session_name}:{window_id}"] = {
        "session_id": session_id,
        "cwd": "/repo",
        "window_name": "repo",
    }
    path.write_text(json.dumps(current))


@pytest.fixture
def _clean_session_map() -> Any:
    """Isolate BOTH ownership proofs the cleanup guard reads.

    ``cleanup_created_window`` spares on BOUND (``thread_bindings``) or
    REGISTERED (the cached ``window_states`` peek OR the fresh map read), so a
    binding or window_state leaked by a neighbouring unit test would spare the
    window and make these assertions pass/fail for the wrong reason.
    """
    from cctelegram.session import session_manager

    path = app_dir() / "session_map.json"
    path.unlink(missing_ok=True)
    bindings = dict(session_manager.thread_bindings)
    states = dict(session_manager.window_states)
    session_manager.thread_bindings.clear()
    session_manager.window_states.clear()
    yield
    path.unlink(missing_ok=True)
    session_manager.thread_bindings.clear()
    session_manager.thread_bindings.update(bindings)
    session_manager.window_states.clear()
    session_manager.window_states.update(states)


@pytest.mark.asyncio
async def test_cleanup_kills_an_unowned_window(_clean_session_map: Any) -> None:
    tmux = _KillRecorder()
    outcome = await trust_flow.cleanup_created_window("@7", "repo", tmux, reason="t")
    assert outcome is trust_flow.CleanupOutcome.KILLED
    assert tmux.calls == ["@7"]


@pytest.mark.asyncio
async def test_registration_before_the_fresh_read_spares(
    _clean_session_map: Any,
) -> None:
    """The declared LINEARIZATION POINT: a registration observed at/before the
    FRESH session-map read WINS, and no kill is issued."""
    _write_session_map("@7", "sid-1")
    tmux = _KillRecorder()
    outcome = await trust_flow.cleanup_created_window("@7", "repo", tmux, reason="t")
    assert outcome is trust_flow.CleanupOutcome.SPARED_REGISTERED
    assert tmux.calls == [], "a registered window must never be killed"


@pytest.mark.asyncio
async def test_registration_after_the_fresh_read_loses(
    _clean_session_map: Any,
) -> None:
    """A registration landing AFTER the read and before the tmux kill LOSES.

    Documented consequence (the "registration always wins" claim is RETRACTED):
    the window dies and the orphaned map entry is reaped by the existing
    startup/poll sweeps.
    """

    class _RegisterDuringKill(_KillRecorder):
        async def kill_window(self, window_id: str) -> bool:
            _write_session_map(window_id, "sid-late")
            return await super().kill_window(window_id)

    tmux = _RegisterDuringKill()
    outcome = await trust_flow.cleanup_created_window("@7", "repo", tmux, reason="t")
    assert outcome is trust_flow.CleanupOutcome.KILLED
    assert tmux.calls == ["@7"]


@pytest.mark.asyncio
async def test_bound_window_is_spared(_clean_session_map: Any) -> None:
    from cctelegram.session import session_manager

    session_manager.thread_bindings.setdefault(1, {})[42] = "@7"
    try:
        tmux = _KillRecorder()
        outcome = await trust_flow.cleanup_created_window(
            "@7", "repo", tmux, reason="t"
        )
    finally:
        session_manager.thread_bindings.clear()
    assert outcome is trust_flow.CleanupOutcome.SPARED_BOUND
    assert tmux.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result", [False, RuntimeError("tmux exploded")], ids=["false", "raise"]
)
async def test_kill_failure_is_typed_and_carries_honest_copy(
    _clean_session_map: Any, result: Any
) -> None:
    tmux = _KillRecorder(result=result)
    outcome = await trust_flow.cleanup_created_window("@7", "repo", tmux, reason="t")
    assert outcome is trust_flow.CleanupOutcome.KILL_FAILED
    note = trust_flow.cleanup_note(outcome, "repo", "@7")
    assert "couldn't close" in note and "check tmux" in note


@pytest.mark.asyncio
async def test_missing_window_id_is_kill_failed(_clean_session_map: Any) -> None:
    tmux = _KillRecorder()
    outcome = await trust_flow.cleanup_created_window("", "repo", tmux, reason="t")
    assert outcome is trust_flow.CleanupOutcome.KILL_FAILED
    assert tmux.calls == []


# ── Fix 2: the two-gate kill-switch posture + the dispatch table ─────────────


def test_both_rig_characterized_versions_are_licensed() -> None:
    assert decision_token.lookup("folder-trust", "2.1.239")
    assert decision_token.lookup("folder-trust", "2.1.241")
    assert not decision_token.lookup("folder-trust", "2.1.242")


@pytest.mark.parametrize(
    ("trust_on", "force_disabled", "expected"),
    [
        (True, False, True),
        (False, False, False),
        (True, True, False),
        (False, True, False),
    ],
)
def test_trust_dispatch_two_gate_contract(
    trust_on: bool, force_disabled: bool, expected: bool
) -> None:
    decision_token.set_trust_card_dispatch_enabled(trust_on)
    decision_token.set_decision_dispatch_force_disabled(force_disabled)
    assert decision_token.trust_card_dispatch_enabled() is expected


# ── Budgets ──────────────────────────────────────────────────────────────────


def test_registration_budget_is_hook_timeout_plus_the_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cctelegram.config import config

    monkeypatch.setattr(config, "hook_timeout_override", None)
    monkeypatch.setattr(config, "hook_timeout_extension_s", 15.0)
    assert trust_flow.registration_budget_s() == pytest.approx(20.0)
    assert trust_flow.registration_budget_s(resume=True) == pytest.approx(30.0)
    monkeypatch.setattr(config, "hook_timeout_override", 42.0)
    assert trust_flow.registration_budget_s() == pytest.approx(57.0)


def test_lane_is_disabled_by_a_zero_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    from cctelegram.config import config

    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 0.0)
    assert trust_flow.lane_enabled() is False
    monkeypatch.setattr(config, "trust_prompt_ceiling_s", 900.0)
    assert trust_flow.lane_enabled() is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 15.0),
        ("", 15.0),
        ("30", 30.0),
        ("0", 0.0),
        ("-1", 15.0),
        ("nan", 15.0),
        ("inf", 15.0),
        ("not-a-number", 15.0),
    ],
)
def test_budget_env_validation(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
) -> None:
    from cctelegram.config import _parse_positive_float_env

    if raw is None:
        monkeypatch.delenv("CC_TELEGRAM_HOOK_TIMEOUT_EXTENSION_S", raising=False)
    else:
        monkeypatch.setenv("CC_TELEGRAM_HOOK_TIMEOUT_EXTENSION_S", raw)
    assert _parse_positive_float_env(
        "CC_TELEGRAM_HOOK_TIMEOUT_EXTENSION_S", 15.0, allow_zero=True
    ) == pytest.approx(expected)
