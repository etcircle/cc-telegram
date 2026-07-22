"""GH #50 §1.2-§1.6 — the gated ``SessionManager.deliver_to_window`` transaction.

Unit-level pins for the single choke point every user payload passes through:

  - the SEGMENT-aware, PER-LINE lone-hotkey refusal (never written);
  - proof of life (``pane_command_is_claude``) on EVERY send (M3);
  - the positive input-box gate, with a bounded retry on an INDETERMINATE frame
    and an IMMEDIATE refusal on a positive hazard;
  - the write with the Enter WITHHELD, incl. the ``!`` bash-mode two-step;
  - the pre-Enter re-verify (the ONE race window the transaction genuinely
    closes) — a prompt appearing between the WRITE and the FINAL CAPTURE ⇒ NO
    Enter, ``draft_written``;
  - the reason→copy map's exhaustiveness (the /cost precedent).

The final-capture→Enter window is the ACCEPTED, DOCUMENTED residual (one tmux
round-trip; no terminal protocol can make it atomic) and is deliberately NOT
asserted as "no Enter" — the same residual the shipped ``_dispatch_pick`` /
``_dispatch_decision`` already accept.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cctelegram import delivery
from cctelegram import session as session_mod
from cctelegram import terminal_parser as tp
from cctelegram.session import session_manager
from cctelegram.tmux_manager import tmux_manager as real_tmux
from tests.conftest import IDLE_PANE_V2_1_207, auq_single_picker_pane, pane_fixture


@pytest.fixture(autouse=True)
def _fresh():
    real_tmux.reset_window_send_locks_for_tests()
    real_tmux.reset_window_quarantines_for_tests()
    session_mod.reset_stranded_drafts_for_tests()
    yield
    real_tmux.reset_window_send_locks_for_tests()
    real_tmux.reset_window_quarantines_for_tests()
    session_mod.reset_stranded_drafts_for_tests()


class _Pane:
    """A scriptable fake pane: a queue of captures + a send-keys recorder."""

    def __init__(self, captures: list[str | None], *, cmd: str = "2.1.207") -> None:
        self.captures = list(captures)
        self.cmd = cmd
        self.sent: list[tuple[str, bool, bool]] = []
        self.capture_calls = 0
        self.with_ansi_calls: list[bool] = []
        # A callable fired right AFTER each literal write (to script a race).
        self.on_write = None

    async def find_window_by_id(self, window_id: str):
        return SimpleNamespace(window_id=window_id)

    async def capture_pane_cancellation_safe(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str | None:
        self.capture_calls += 1
        self.with_ansi_calls.append(with_ansi)
        if len(self.captures) > 1:
            value = self.captures.pop(0)
        else:
            value = self.captures[0]
        # FAKE HONESTY (GH #60 P1): a real ``capture_pane`` returns the ANSI form
        # only when the caller asks for it. The delivery gate always requests
        # ``with_ansi=True``, so a ghost frame reaches the classifier as ANSI; a
        # ``with_ansi=False`` caller would get the ANSI-stripped plain form.
        if value is None or with_ansi:
            return value
        return tp._strip_ansi(value)

    async def pane_current_command(self, window_id: str) -> str | None:
        return self.cmd

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.sent.append((text, enter, literal))
        if literal and not enter and self.on_write is not None:
            self.on_write()
        return True

    # ── views ──────────────────────────────────────────────────────────
    @property
    def written(self) -> list[str]:
        return [t for t, e, lit in self.sent if lit and not e]

    @property
    def committed(self) -> bool:
        return any(t == "" and e and not lit for t, e, lit in self.sent)


def _wire(monkeypatch: pytest.MonkeyPatch, pane: _Pane) -> _Pane:
    for name in (
        "find_window_by_id",
        "capture_pane_cancellation_safe",
        "pane_current_command",
        "send_keys",
    ):
        monkeypatch.setattr(real_tmux, name, getattr(pane, name))
    monkeypatch.setattr(session_mod, "TEXT_SETTLE_S", 0.0)
    monkeypatch.setattr(session_mod, "BASH_MODE_SETTLE_S", 0.0)
    monkeypatch.setattr(session_mod, "GATE_RETRY_DELAY_S", 0.0)
    return pane


# ── §1.3: the lone-hotkey SEGMENT/LINE refusal ───────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        "1",  # the bare hotkey
        "!1",  # the `!` two-step SPLIT emits "1" as its own write (rig C7)
        "first line\n2\nthird line",  # a bare-digit LINE in a multi-line write
        "4",  # the free-text affordance row — it ARMS a mode
    ],
)
def test_lone_hotkey_lines_are_detected(payload: str) -> None:
    assert delivery.lone_hotkey_line(payload) is not None


@pytest.mark.parametrize(
    "payload",
    [
        "12",  # multi-digit — empirically narrowed, NON-proof (disclosed)
        "option 1",
        "!echo 1",
        "line one\nline 2 here\n",
        "٣",  # a Unicode digit is NOT an ASCII [0-9] hotkey
    ],
)
def test_non_hotkey_payloads_pass(payload: str) -> None:
    assert delivery.lone_hotkey_line(payload) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["1", "!1", "a\n2\nb"])
async def test_lone_hotkey_payload_is_never_written(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    """§1.7: refused OUTRIGHT — no capture, no keystroke, even on an idle pane
    (the gate→write window is exactly what makes a digit dangerous)."""
    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))

    result = await session_manager.deliver_to_window("@1", payload)

    assert result.refused
    assert result.reason == delivery.REASON_LONE_HOTKEY
    assert result.outcome is delivery.DeliveryOutcome.NOT_WRITTEN
    assert pane.sent == []
    assert pane.capture_calls == 0
    assert "just a number" in result.message


# ── §1.3b: the RAW-CONTROL-BYTE refusal ──────────────────────────────────
#
# ``tmux send-keys -l`` stops tmux interpreting KEY NAMES; it does NOT make C0/ESC
# bytes inert to the TUI on the other side of the pty. RIG-CONFIRMED (``tmux -L
# ccrig``, ``cat -v`` in the pane): a payload built with ``printf 'A\033[B2B'``
# lands as the literal bytes ``A^[[B2B``. Claude's TUI reads that as ``A``, a
# CURSOR-DOWN escape sequence, then ``2`` — and on a single-select-shaped surface
# a digit is a HOTKEY that COMMITS with no Enter. It fires DURING the write, so no
# amount of post-write verification can undo it: the payload must never be typed.


@pytest.mark.parametrize(
    "payload",
    [
        "answer\x1b[B2",  # ESC — the cursor-move + hotkey commit primitive
        "before\tafter",  # TAB — a live TUI key (advances a picker; completion)
        "line\rmore",  # CR — Enter at the pty; it would commit mid-payload
        "nul\x00byte",
        "del\x7fbyte",
        "c1\x9bbyte",  # C1 CSI, which a UTF-8 terminal decodes back to a control
    ],
)
def test_control_bytes_are_detected(payload: str) -> None:
    assert delivery.unsafe_control_char(payload) is not None


@pytest.mark.parametrize(
    "payload",
    [
        "plain text",
        "line one\nline two\n",  # LF is ALLOWED — a paste-shaped multi-line
        "> quoted reply\n\nmy actual answer\n",  # …the owner's dominant shape
        "emoji 🎨 and unicode ü stay fine",
    ],
)
def test_ordinary_payloads_pass(payload: str) -> None:
    assert delivery.unsafe_control_char(payload) is None


@pytest.mark.asyncio
async def test_a_control_byte_payload_is_never_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused OUTRIGHT — no capture, no keystroke, even on an idle pane."""
    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))

    result = await session_manager.deliver_to_window("@1", "my answer\x1b[B2 teal")

    assert result.refused
    assert result.reason == delivery.REASON_CONTROL_CHARS
    assert result.outcome is delivery.DeliveryOutcome.NOT_WRITTEN
    assert pane.sent == []
    assert pane.capture_calls == 0
    assert "control character" in result.message


@pytest.mark.asyncio
async def test_a_multi_line_payload_still_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard non-regression: newlines are LOAD-BEARING (every voice note and
    every reply-context quote is multi-line, and CC consumes a single
    ``send-keys -l`` burst paste-shaped). Nothing here touches that."""
    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))
    payload = "> Re: the card\n>\n> Claude asked: which colour?\n\nTeal, please.\n"

    result = await session_manager.deliver_to_window("@1", payload)

    assert result.ok
    assert pane.sent == [(payload, False, True), ("", True, False)]


# ── §1.2: the happy path + the withheld Enter ────────────────────────────


@pytest.mark.asyncio
async def test_idle_pane_delivers_write_then_enter(monkeypatch) -> None:
    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))

    result = await session_manager.deliver_to_window("@1", "hello claude")

    assert result.ok
    assert pane.sent == [("hello claude", False, True), ("", True, False)]


@pytest.mark.asyncio
async def test_busy_pane_still_delivers(monkeypatch) -> None:
    """A2 (design-killer) NON-REGRESSION: queueing a message while Claude is
    mid-tool-run is a first-class flow and MUST keep working."""
    pane = _wire(monkeypatch, _Pane([pane_fixture("inputbox_busy_tool_v2.1.207.txt")]))

    result = await session_manager.deliver_to_window("@1", "and then do X")

    assert result.ok
    assert pane.written == ["and then do X"]
    assert pane.committed


@pytest.mark.asyncio
async def test_existing_draft_pane_still_delivers(monkeypatch) -> None:
    pane = _wire(
        monkeypatch, _Pane([pane_fixture("inputbox_draft_typed_v2.1.207.txt")])
    )
    assert (await session_manager.deliver_to_window("@1", "more")).ok
    assert pane.committed


@pytest.mark.asyncio
async def test_bang_command_preserves_the_two_step(monkeypatch) -> None:
    """§1.2 step 4 / r3 P1-5: ``send_keys``'s ``!`` two-step fires ONLY when
    ``literal and enter`` are BOTH true, so the withheld-Enter writer must
    reproduce it — `!` first (the TUI switches to bash mode), then the rest."""
    pane = _wire(
        monkeypatch, _Pane([pane_fixture("inputbox_bashmode_draft_v2.1.207.txt")])
    )

    result = await session_manager.deliver_to_window("@1", "!echo hi")

    assert result.ok
    assert pane.sent == [
        ("!", False, True),
        ("echo hi", False, True),
        ("", True, False),
    ]


@pytest.mark.asyncio
async def test_slash_command_delivers_despite_its_own_completion_overlay(
    monkeypatch,
) -> None:
    """A bare ``/command`` payload legitimately arms the ``/`` completion overlay
    once written, and Enter runs the sorted-first entry — the mechanism
    ``forward_command_handler`` has always relied on. The post-write re-verify
    exempts the ``/`` arm for exactly this shape (never the ``@`` arm). The
    bare-ambiguous-prefix misfire (``/co`` → ``/copy``) is GH #53, filed
    separately."""
    pane = _wire(
        monkeypatch,
        _Pane(
            [
                IDLE_PANE_V2_1_207,  # pre-write gate
                pane_fixture("inputbox_slash_exact_clear_v2.1.207.txt"),  # re-verify
            ]
        ),
    )

    result = await session_manager.deliver_to_window("@1", "/clear")

    assert result.ok
    assert pane.committed


@pytest.mark.asyncio
async def test_prose_payload_into_an_at_overlay_refuses_at_reverify(
    monkeypatch,
) -> None:
    """§1.1 leg 5 / §5 finding 2: any message ending in ``@word`` arms the file
    completion — Enter would select a completion and the message would be NEVER
    sent. This is LIVE TODAY; the re-verify catches it and withholds Enter."""
    pane = _wire(
        monkeypatch,
        _Pane(
            [
                IDLE_PANE_V2_1_207,
                pane_fixture("inputbox_at_overlay_v2.1.207.txt"),
            ]
        ),
    )

    result = await session_manager.deliver_to_window("@1", "please ask @se")

    assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
    assert result.reason == delivery.REASON_REVERIFY_FAILED
    assert pane.written == ["please ask @se"]
    assert not pane.committed
    assert result.message == delivery.DRAFT_WRITTEN_MSG


# ── §1.2 step 2 (M3): proof of life on EVERY send ────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["zsh", "-zsh", "/bin/bash", "vim", "node", None])
async def test_non_claude_pane_refuses(monkeypatch, cmd) -> None:
    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207], cmd=cmd))

    result = await session_manager.deliver_to_window("@1", "rm -rf ./scratch")

    assert result.refused
    assert result.reason == delivery.REASON_NOT_CLAUDE
    assert result.outcome is delivery.DeliveryOutcome.NOT_WRITTEN
    assert pane.sent == []


@pytest.mark.asyncio
async def test_esc_on_folder_trust_left_a_shell_refuses(monkeypatch) -> None:
    """M3, rig-reproduced: ``/esc`` on the folder-trust prompt EXITS Claude,
    leaving a bare SHELL in a still-bound window — the payload must never be
    typed there (it would be EXECUTED)."""
    pane = _wire(
        monkeypatch,
        _Pane([pane_fixture("shell_after_esc_v2.1.207.txt")], cmd="zsh"),
    )

    result = await session_manager.deliver_to_window("@1", "echo pwned")

    assert result.refused
    assert pane.sent == []
    assert "/update" in result.message


# ── §1.2 step 3: the gate, its retry, and its immediate hazards ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    [
        "auq_single_picker_v2.1.207.txt",
        "auq_multi_picker_v2.1.207.txt",
        "gate_epm_v2.1.207.txt",
        "gate_workflow_v2.1.207.txt",
        "gate_permission_v2.1.207.txt",
        "folder_trust_arrival_plain_v2.1.207.txt",
        "switch_model_live_v2.1.207.txt",  # M4 — the parser is BLIND to it
        "settings_warning_v2170.txt",
        "overlay_cost_modal_v2.1.207.txt",
        "inputbox_tasks_mode_v2.1.207.txt",
    ],
)
async def test_every_blocking_surface_refuses_without_a_keystroke(
    monkeypatch, fixture: str
) -> None:
    pane = _wire(monkeypatch, _Pane([pane_fixture(fixture)]))

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.refused
    assert result.outcome is delivery.DeliveryOutcome.NOT_WRITTEN
    assert pane.sent == []


@pytest.mark.asyncio
async def test_positive_hazard_refuses_on_exactly_one_capture(monkeypatch) -> None:
    """A live picker is a POSITIVE hazard — refuse immediately, no retry."""
    pane = _wire(monkeypatch, _Pane([auq_single_picker_pane()]))

    assert (await session_manager.deliver_to_window("@1", "hello")).refused
    assert pane.capture_calls == 1


@pytest.mark.asyncio
async def test_indeterminate_frame_retries_then_delivers(monkeypatch) -> None:
    """A mid-redraw capture (no chrome) RETRIES; the box appears ⇒ delivers."""
    pane = _wire(monkeypatch, _Pane(["", "\n\n(mid-redraw)\n", IDLE_PANE_V2_1_207]))

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.ok
    # 3 gate captures (2 indeterminate + the box) + the pre-Enter re-verify.
    assert pane.capture_calls == 4


@pytest.mark.asyncio
async def test_indeterminate_frame_then_prompt_refuses(monkeypatch) -> None:
    pane = _wire(monkeypatch, _Pane(["", auq_single_picker_pane()]))

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.refused
    assert pane.sent == []


@pytest.mark.asyncio
async def test_indeterminate_to_the_end_refuses(monkeypatch) -> None:
    pane = _wire(monkeypatch, _Pane([""]))

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.refused
    assert result.reason in tp.INPUT_BOX_INDETERMINATE_REASONS
    assert pane.sent == []
    assert pane.capture_calls == session_mod.GATE_CAPTURE_RETRIES + 1


@pytest.mark.asyncio
async def test_capture_timeout_refuses_and_releases_the_lock(monkeypatch) -> None:
    """ONLY ``asyncio.TimeoutError`` classifies (the /cost r1 P2 rule); the lock
    is released so the next send is not wedged."""

    async def hang(window_id: str, with_ansi: bool = False, scrollback_lines: int = 0):
        await asyncio.sleep(10)
        return IDLE_PANE_V2_1_207

    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))
    monkeypatch.setattr(real_tmux, "capture_pane_cancellation_safe", hang)
    monkeypatch.setattr(session_mod, "GATE_CAPTURE_DEADLINE_S", 0.01)

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.refused
    assert result.reason == delivery.REASON_CAPTURE_TIMEOUT
    assert pane.sent == []
    assert not real_tmux.window_send_lock("@1").locked()


# ── §1.7: the WRITE → FINAL-CAPTURE race (the window the re-verify closes) ──


@pytest.mark.asyncio
async def test_prompt_appearing_between_write_and_final_capture_withholds_enter(
    monkeypatch,
) -> None:
    """r4 P1-5, stated so it is actually TRUE: a prompt drawn between the TEXT
    WRITE and the FINAL CAPTURE ⇒ NO Enter, ``draft_written``, neutral copy.

    The final-capture→Enter window is the ACCEPTED, DOCUMENTED residual and is
    NOT asserted here."""
    pane = _Pane([IDLE_PANE_V2_1_207])

    def a_prompt_appears() -> None:
        pane.captures = [auq_single_picker_pane()]

    pane.on_write = a_prompt_appears
    _wire(monkeypatch, pane)

    result = await session_manager.deliver_to_window("@1", "lets do 3 things first")

    assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
    assert result.reason == delivery.REASON_REVERIFY_FAILED
    assert pane.written == ["lets do 3 things first"]
    assert not pane.committed
    # The copy does NOT over-diagnose (r3 P2-3) and offers no auto-cleanup —
    # Esc / Ctrl-U have surface-specific semantics (Esc on folder-trust KILLS
    # Claude).
    assert "was NOT submitted" in result.message
    # GH #56: the remedy is now HONEST — a single Escape / Ctrl+U does NOT clear a
    # draft on 2.1.209; two rapid Escapes do (and /esc performs that safely on a
    # braked window). The copy still offers no unconditional auto-cleanup.
    assert "twice" in result.message.lower()
    assert "Ctrl+U" not in result.message


@pytest.mark.asyncio
async def test_claude_exiting_between_write_and_enter_withholds_enter(
    monkeypatch,
) -> None:
    pane = _Pane([IDLE_PANE_V2_1_207])

    def claude_dies() -> None:
        pane.cmd = "zsh"

    pane.on_write = claude_dies
    _wire(monkeypatch, pane)

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
    assert result.reason == delivery.REASON_NOT_CLAUDE
    assert not pane.committed


# ── r2 F1: an ordinary numbered message must actually SEND ───────────────


def _pane_with_draft(text: str) -> str:
    """The real draft pane with its input-row content replaced by ``text``."""
    return pane_fixture("inputbox_draft_typed_v2.1.207.txt").replace(
        "hello this is a plain draft", text
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["1. buy milk", "2. then eggs"])
async def test_a_numbered_message_is_delivered_not_stranded(
    monkeypatch, payload: str
) -> None:
    """r2 F1, reproduced: the picker trap fired on the RE-VERIFY (the box now
    reads ``❯ 1. buy milk`` because WE typed it), the Enter was withheld, and an
    ordinary numbered message was silently left sitting as a draft, forever."""
    pane = _Pane([IDLE_PANE_V2_1_207])

    def our_text_lands() -> None:
        pane.captures = [_pane_with_draft(payload)]

    pane.on_write = our_text_lands
    _wire(monkeypatch, pane)

    result = await session_manager.deliver_to_window("@1", payload)

    assert result.ok
    assert pane.committed
    assert not session_mod.window_has_stranded_draft("@1")


@pytest.mark.asyncio
async def test_a_multiline_payload_whose_first_line_is_numbered_delivers(
    monkeypatch,
) -> None:
    payload = "1. foo\nsecond line\nthird line"
    pane = _Pane([IDLE_PANE_V2_1_207])
    pane.on_write = lambda: setattr(pane, "captures", [_pane_with_draft("1. foo")])
    _wire(monkeypatch, pane)

    assert (await session_manager.deliver_to_window("@1", payload)).ok


@pytest.mark.asyncio
async def test_a_picker_appearing_in_the_gate_to_write_window_still_refuses(
    monkeypatch,
) -> None:
    """The adversarial twin of the two tests above: the pane shows the PICKER's
    own ``❯ 1. Red`` (it stole our keystrokes), NOT our text — the trap MUST fire.
    ``expected_draft`` is evidence of authorship, never a bypass."""
    pane = _Pane([IDLE_PANE_V2_1_207])
    pane.on_write = lambda: setattr(pane, "captures", [auq_single_picker_pane()])
    _wire(monkeypatch, pane)

    result = await session_manager.deliver_to_window("@1", "1. buy milk")

    assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
    assert not pane.committed


def _reverify_race_pane(hazard: str) -> _Pane:
    """A pane that passes the pre-write gate then shows ``hazard`` at re-verify."""
    pane = _Pane([IDLE_PANE_V2_1_207])
    pane.on_write = lambda: setattr(pane, "captures", [hazard])
    return pane


# ── THE PASTE-COLLAPSE (the GH #50 PR-1 regression) ──────────────────────
#
# CC consumes a large multi-line `send-keys -l` as a PASTE: it collapses the input
# row to `❯\xa0[Pasted text #1 +12 lines]` and REPLACES the status bar with
# `paste again to expand` for ~2s — right across the re-verify (TEXT_SETTLE_S =
# 0.5s). The gate read that as "no input box" and refused, stranding the draft and
# braking the topic. It hit essentially every long / multi-line message.

_PASTE_COLLAPSED = "inputbox_paste_collapsed_v2.1.207.txt"
_PASTE_REVERTED = "inputbox_paste_collapsed_reverted_v2.1.207.txt"

# The owner's real failing payload shape: a voice note delivered as a reply, so the
# reply-context quote pushes it past CC's paste threshold (live log: text_len=809).
_LONG_MULTILINE_PAYLOAD = "\n".join(
    ["> Voice note reply to: the deployment plan and the follow-up items."]
    + [
        f"line {i}: a sentence of reply context long enough to reach the paste "
        f"threshold that CC applies to a single literal write."
        for i in range(1, 8)
    ]
)


@pytest.mark.asyncio
@pytest.mark.parametrize("collapsed", [_PASTE_COLLAPSED, _PASTE_REVERTED])
async def test_a_long_multiline_payload_delivers_through_the_paste_collapse(
    monkeypatch, collapsed: str
) -> None:
    """THE REPORTED BUG. The gate sees the idle box; the write is consumed as a
    PASTE and the pane collapses; the re-verify must still commit the Enter."""
    assert len(_LONG_MULTILINE_PAYLOAD) > 800
    pane = _Pane([IDLE_PANE_V2_1_207])
    pane.on_write = lambda: setattr(pane, "captures", [pane_fixture(collapsed)])
    _wire(monkeypatch, pane)

    result = await session_manager.deliver_to_window("@1", _LONG_MULTILINE_PAYLOAD)

    assert result.ok, result.reason
    assert pane.written == [_LONG_MULTILINE_PAYLOAD]
    assert pane.committed  # the Enter that PR-1 was withholding
    assert not session_mod.window_has_stranded_draft("@1")  # and no brake


@pytest.mark.asyncio
async def test_the_paste_collapsed_pane_is_also_deliverable_at_the_PRE_write_gate(
    monkeypatch,
) -> None:
    """A collapsed draft left over from a previous turn must not block the next
    send (it is a pre-existing draft, which the gate has always allowed)."""
    pane = _wire(monkeypatch, _Pane([pane_fixture(_PASTE_COLLAPSED)]))

    assert (await session_manager.deliver_to_window("@1", "hello")).ok
    assert pane.committed


# ── §3: the re-verify's bounded INDETERMINATE retry (defence in depth) ───
#
# A refusal at the re-verify is the most expensive failure in the transaction: the
# payload is already in the box, so it strands the draft AND brakes the topic. It
# had NO retry — it refused on the FIRST non-None reason, so a single mid-redraw
# frame was enough. It now carries the pre-write gate's discipline. The asymmetry
# is preserved: a POSITIVE hazard still refuses on the first capture.


@pytest.mark.asyncio
async def test_the_reverify_retries_an_indeterminate_frame_then_commits(
    monkeypatch,
) -> None:
    pane = _Pane([IDLE_PANE_V2_1_207])
    # A mid-redraw frame at the first re-verify capture; the box on the retry.
    pane.on_write = lambda: setattr(
        pane, "captures", ["", "\n\n(mid-redraw)\n", _pane_with_draft("hello")]
    )
    _wire(monkeypatch, pane)

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.ok
    assert pane.committed
    # 1 pre-write gate capture + 3 re-verify captures (2 indeterminate + the box).
    assert pane.capture_calls == 4


@pytest.mark.asyncio
async def test_the_reverify_still_strands_when_every_frame_is_indeterminate(
    monkeypatch,
) -> None:
    """The retry BOUNDS the uncertainty — it never converts it into an Enter."""
    pane = _reverify_race_pane("")
    _wire(monkeypatch, pane)

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
    assert result.reason == delivery.REASON_REVERIFY_FAILED
    assert not pane.committed
    assert session_mod.window_has_stranded_draft("@1")
    assert pane.capture_calls == 1 + session_mod.GATE_CAPTURE_RETRIES + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hazard_fixture",
    [
        "auq_single_picker_v2.1.207.txt",  # prompt_row_is_option
        "inputbox_tasks_mode_v2.1.207.txt",  # tasks_mode (Enter is STOLEN)
        "inputbox_at_overlay_v2.1.207.txt",  # completion_overlay
    ],
)
async def test_a_positive_hazard_at_the_reverify_STILL_refuses_on_one_capture(
    monkeypatch, hazard_fixture: str
) -> None:
    """THE SAFETY PROPERTY the retry must not weaken: a real prompt drawn in the
    gate→write window refuses IMMEDIATELY — one capture, no Enter, brake armed."""
    pane = _reverify_race_pane(pane_fixture(hazard_fixture))
    _wire(monkeypatch, pane)

    result = await session_manager.deliver_to_window("@1", "some prose payload")

    assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
    assert not pane.committed
    assert session_mod.window_has_stranded_draft("@1")
    # The pre-write gate's 1 capture + EXACTLY 1 at the re-verify. No retries.
    assert pane.capture_calls == 2


@pytest.mark.asyncio
async def test_claude_exiting_at_the_reverify_is_never_retried(monkeypatch) -> None:
    """A non-Claude pane is a POSITIVE hazard too — one probe, no retry."""
    pane = _Pane([IDLE_PANE_V2_1_207])
    pane.on_write = lambda: setattr(pane, "cmd", "zsh")
    _wire(monkeypatch, pane)

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.reason == delivery.REASON_NOT_CLAUDE
    assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
    assert not pane.committed
    assert pane.capture_calls == 1  # the re-verify never even reached its capture


# ── r2 F2: the stranded-draft brake ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_draft_written_refusal_arms_the_brake(monkeypatch) -> None:
    _wire(monkeypatch, _reverify_race_pane(auq_single_picker_pane()))

    result = await session_manager.deliver_to_window("@1", "first message")

    assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
    assert session_mod.window_has_stranded_draft("@1")


@pytest.mark.asyncio
async def test_the_next_send_never_commits_the_stranded_draft(monkeypatch) -> None:
    """THE BUG (r2 F2): the first payload is stranded in the input box and the
    user is told it was NOT delivered — then the NEXT send passes the gate (a
    live box with a pre-existing draft is legitimately deliverable!), is APPENDED
    to it, and its Enter commits BOTH."""
    pane = _wire(monkeypatch, _reverify_race_pane(auq_single_picker_pane()))
    assert (await session_manager.deliver_to_window("@1", "first")).refused

    # The prompt resolved; the box is back — but OUR unsent draft is still in it.
    pane.captures = [_pane_with_draft("first")]
    pane.on_write = None
    result = await session_manager.deliver_to_window("@1", "second")

    assert result.refused
    assert result.reason == delivery.REASON_STRANDED_DRAFT
    assert result.outcome is delivery.DeliveryOutcome.NOT_WRITTEN
    assert pane.written == ["first"]  # "second" was never typed
    assert not pane.committed  # and nothing was committed
    assert "still sitting UNSENT" in result.message


@pytest.mark.asyncio
async def test_the_brake_releases_on_a_proven_empty_input_row(monkeypatch) -> None:
    """Positive proof only: the user cleared the box in the terminal."""
    pane = _wire(monkeypatch, _reverify_race_pane(auq_single_picker_pane()))
    assert (await session_manager.deliver_to_window("@1", "first")).refused
    assert session_mod.window_has_stranded_draft("@1")

    pane.captures = [IDLE_PANE_V2_1_207]  # cleared
    pane.on_write = None
    result = await session_manager.deliver_to_window("@1", "second")

    assert result.ok
    assert pane.written == ["first", "second"]
    assert not session_mod.window_has_stranded_draft("@1")


@pytest.mark.asyncio
async def test_the_brake_holds_on_an_indeterminate_frame(monkeypatch) -> None:
    """A capture failure / a live prompt is NOT proof the draft is gone."""
    pane = _wire(monkeypatch, _reverify_race_pane(auq_single_picker_pane()))
    assert (await session_manager.deliver_to_window("@1", "first")).refused

    pane.captures = [""]  # indeterminate
    pane.on_write = None
    result = await session_manager.deliver_to_window("@1", "second")

    assert result.reason == delivery.REASON_STRANDED_DRAFT
    assert pane.written == ["first"]


@pytest.mark.asyncio
async def test_an_unbraked_window_costs_no_extra_capture(monkeypatch) -> None:
    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))
    assert (await session_manager.deliver_to_window("@1", "hello")).ok
    assert pane.capture_calls == 2  # the pre-write gate + the re-verify. No more.


# ── The cancellation door into the F2 hazard (peer-review P1) ────────────
#
# The brake used to be armed ONLY from the RETURNED DeliveryResult, so a
# CancelledError raised after the payload was typed (topic teardown cancels
# per-topic tasks; shutdown cancels in-flight work; a cancelled to_thread await
# can still have COMPLETED its tmux write) left the transaction with NO result
# and the brake UNARMED — and the next delivery appended to the leftover draft
# and committed BOTH. Arming keys on the WRITE-ATTEMPTED flag (the same
# information DRAFT_WRITTEN already uses), never on "any raise".


class _ParkingPane(_Pane):
    """A ``_Pane`` that PARKS (awaits forever) at a chosen phase, so the test can
    cancel the delivery task exactly there. ``parked`` fires when it is reached."""

    def __init__(
        self,
        captures: list[str | None],
        *,
        park_at: str,  # "pre_gate_capture" | "reverify_probe" | "enter"
    ) -> None:
        super().__init__(captures)
        self.park_at = park_at
        self.parked = asyncio.Event()
        self.cmd_calls = 0
        self.capture_raises: BaseException | None = None

    async def capture_pane_cancellation_safe(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str | None:
        if self.capture_raises is not None:
            raise self.capture_raises
        if self.park_at == "pre_gate_capture":
            self.parked.set()
            await asyncio.sleep(60)
        return await super().capture_pane_cancellation_safe(
            window_id, with_ansi, scrollback_lines
        )

    async def pane_current_command(self, window_id: str) -> str | None:
        self.cmd_calls += 1
        # The re-verify probe is the SECOND command probe of the transaction.
        if self.park_at == "reverify_probe" and self.cmd_calls >= 2:
            self.parked.set()
            await asyncio.sleep(60)
        return self.cmd

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        if self.park_at == "enter" and enter and not literal:
            self.parked.set()
            await asyncio.sleep(60)  # the commit key never lands
        return await super().send_keys(window_id, text, enter, literal)


async def _cancel_at(pane: _ParkingPane, payload: str = "hello") -> None:
    """Drive a delivery until the pane parks, then CANCEL the task."""
    task = asyncio.create_task(session_manager.deliver_to_window("@1", payload))
    await asyncio.wait_for(pane.parked.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # CancelledError must PROPAGATE — never swallowed.


@pytest.mark.asyncio
async def test_cancellation_during_the_settle_arms_the_brake(monkeypatch) -> None:
    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))
    monkeypatch.setattr(session_mod, "TEXT_SETTLE_S", 60.0)  # park in the settle
    wrote = asyncio.Event()
    pane.on_write = wrote.set

    task = asyncio.create_task(session_manager.deliver_to_window("@1", "hello"))
    await asyncio.wait_for(wrote.wait(), timeout=2)
    await asyncio.sleep(0)  # let the coroutine reach the settle
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert pane.written == ["hello"]  # the payload IS in the input box
    assert not pane.committed
    assert session_mod.window_has_stranded_draft("@1")


@pytest.mark.asyncio
async def test_cancellation_during_the_reverify_arms_the_brake(monkeypatch) -> None:
    pane = _wire(
        monkeypatch, _ParkingPane([IDLE_PANE_V2_1_207], park_at="reverify_probe")
    )
    await _cancel_at(pane)

    assert pane.written == ["hello"]
    assert not pane.committed
    assert session_mod.window_has_stranded_draft("@1")


@pytest.mark.asyncio
async def test_cancellation_during_the_enter_arms_the_brake(monkeypatch) -> None:
    """The Enter may NOT have landed, so the draft is potentially stranded. If it
    DID land, the brake's empty-input-row self-heal releases it on the next send —
    so arming here is the fail-closed, self-correcting direction."""
    pane = _wire(monkeypatch, _ParkingPane([IDLE_PANE_V2_1_207], park_at="enter"))
    await _cancel_at(pane)

    assert pane.written == ["hello"]
    assert not pane.committed
    assert session_mod.window_has_stranded_draft("@1")


@pytest.mark.asyncio
async def test_a_cancellation_armed_brake_refuses_the_next_send(monkeypatch) -> None:
    """The whole point: the NEXT payload must not be appended to the leftover
    draft and committed with it."""
    pane = _wire(
        monkeypatch, _ParkingPane([IDLE_PANE_V2_1_207], park_at="reverify_probe")
    )
    await _cancel_at(pane, "first")

    pane.park_at = "none"  # the transaction can complete now…
    pane.captures = [_pane_with_draft("first")]  # …but our draft is still in the box
    result = await session_manager.deliver_to_window("@1", "second")

    assert result.refused
    assert result.reason == delivery.REASON_STRANDED_DRAFT
    assert result.outcome is delivery.DeliveryOutcome.NOT_WRITTEN
    assert pane.written == ["first"]  # "second" was never typed
    assert not pane.committed  # and NOTHING was committed


@pytest.mark.asyncio
async def test_a_raise_before_any_write_does_NOT_arm_the_brake(monkeypatch) -> None:
    """THE HARD NON-REGRESSION. A raise proves nothing about the pane, so arming
    on "any raise" would false-refuse the legitimate case: a HUMAN's pre-existing
    draft in the box + an unrelated tmux error. Only a WRITE ATTEMPT arms it."""
    pane = _wire(monkeypatch, _ParkingPane([IDLE_PANE_V2_1_207], park_at="none"))
    pane.capture_raises = RuntimeError("tmux exploded")

    with pytest.raises(RuntimeError):
        await session_manager.deliver_to_window("@1", "hello")

    assert pane.written == []  # nothing was typed…
    assert not session_mod.window_has_stranded_draft("@1")  # …so nothing is braked


@pytest.mark.asyncio
async def test_cancellation_before_any_write_does_NOT_arm_the_brake(
    monkeypatch,
) -> None:
    pane = _wire(
        monkeypatch, _ParkingPane([IDLE_PANE_V2_1_207], park_at="pre_gate_capture")
    )
    await _cancel_at(pane)

    assert pane.written == []
    assert not session_mod.window_has_stranded_draft("@1")


# ── peer-review P1: the brake is a property of the PANE, not of the binding ──
#
# Round 1 cleared the brake at ``cleanup.clear_topic_state`` + the four
# ``inbound_telegram`` stale-window unbinds — beside the tmux quarantine those
# seams already drop. That re-opened the very commit chain the brake exists to
# break, because NEITHER is a proof of window death and NEITHER holds the window
# send lock. The brake now has exactly two release proofs: an EMPTY input row, or
# a DEAD/brand-new window (tmux's own kill_window / create_window seams).


def test_no_binding_level_teardown_seam_clears_the_brake() -> None:
    """The inverted twin of the round-1 test that PINNED those very calls."""
    import inspect

    from cctelegram.handlers import cleanup, inbound_telegram

    assert "clear_stranded_draft" not in inspect.getsource(cleanup.clear_topic_state)
    assert "clear_stranded_draft" not in inspect.getsource(inbound_telegram)


@pytest.mark.asyncio
async def test_a_send_queued_across_an_unbind_never_commits_the_stranded_draft(
    monkeypatch,
) -> None:
    """THE P1, reproduced. Delivery A strands its draft and arms the brake INSIDE
    the send lock. ``/unbind`` — which DELIBERATELY leaves the tmux window running
    — then ran ``clear_topic_state`` → ``clear_stranded_draft`` with no
    synchronization against ``window_send_lock``. Send B (an already-popped
    boundary flush, or a slash command), BLOCKED on that same lock the whole time,
    then acquired it, found a structurally valid input box that still held A's
    draft, appended its own payload and pressed Enter — committing BOTH, including
    the one the user was told was NOT delivered."""
    from cctelegram.handlers import cleanup, message_queue
    from cctelegram.session import session_manager as sm

    uid, tid, wid = 9001, 77, "@1"
    pane = _wire(monkeypatch, _reverify_race_pane(auq_single_picker_pane()))

    # A: refused as draft_written — the payload is sitting UNSENT in the box.
    assert (await session_manager.deliver_to_window(wid, "first")).refused
    assert session_mod.window_has_stranded_draft(wid)

    # /unbind: the topic's state is torn down, but the WINDOW KEEPS RUNNING.
    # (Seed a live route so the teardown reaches the per-route loop the round-1
    # ``clear_stranded_draft`` call lived in — otherwise this passes vacuously.)
    sm.bind_thread(uid, tid, wid, "unbind-me")
    message_queue._route_queues[(uid, tid, wid)] = asyncio.Queue()
    sm.unbind_thread(uid, tid)
    await cleanup.clear_topic_state(uid, tid, drop_pending=False)

    # The draft is STILL in that box — the binding said nothing about the pane.
    assert session_mod.window_has_stranded_draft(wid)

    pane.captures = [_pane_with_draft("first")]
    pane.on_write = None
    result = await session_manager.deliver_to_window(wid, "second")

    assert result.refused
    assert result.reason == delivery.REASON_STRANDED_DRAFT
    assert pane.written == ["first"]  # "second" was never typed…
    assert not pane.committed  # …and NOTHING was committed


# ── …and the two release proofs the brake DOES accept ────────────────────


class _FakeTmuxWindow:
    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def set_window_option(self, *_a, **_kw) -> None:
        pass

    @property
    def window_id(self) -> str:
        return "@1"

    @property
    def active_pane(self):
        return None


class _FakeTmuxSession:
    def __init__(self, window: _FakeTmuxWindow | None) -> None:
        self._window = window
        self.windows = SimpleNamespace(get=lambda **_kw: self._window)

    def new_window(self, **_kw) -> _FakeTmuxWindow:
        return _FakeTmuxWindow()


@pytest.mark.asyncio
async def test_a_confirmed_window_kill_clears_the_brake(monkeypatch) -> None:
    """A dead window's brake entry is pure garbage — and ``kill_window`` is the one
    place window death is CONFIRMED, synchronously, by us."""
    window = _FakeTmuxWindow()
    monkeypatch.setattr(real_tmux, "get_session", lambda: _FakeTmuxSession(window))
    real_tmux.mark_window_stranded_draft("@1")

    assert await real_tmux.kill_window("@1") is True

    assert window.killed
    assert not session_mod.window_has_stranded_draft("@1")


@pytest.mark.asyncio
async def test_a_FAILED_kill_KEEPS_the_brake(monkeypatch) -> None:
    """Gated on the CONFIRMED kill for the same reason the send lock is: a failed
    kill can leave the window ALIVE with the draft still in its box."""
    monkeypatch.setattr(real_tmux, "get_session", lambda: _FakeTmuxSession(None))
    real_tmux.mark_window_stranded_draft("@1")

    assert await real_tmux.kill_window("@1") is False

    assert session_mod.window_has_stranded_draft("@1")


@pytest.mark.asyncio
async def test_a_brand_new_window_never_inherits_a_stale_brake(
    monkeypatch, tmp_path
) -> None:
    """The second death proof. tmux window ids RESET to @0 when the tmux SERVER
    restarts, and a launchd-kept bot process outlives that — so an entry armed on
    the OLD @0 (whose window died without a kill_window) could meet a brand-new
    @0. A window tmux JUST created provably holds no bot-written draft."""
    monkeypatch.setattr(
        real_tmux, "get_or_create_session", lambda: _FakeTmuxSession(None)
    )
    monkeypatch.setattr(real_tmux, "find_window_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(real_tmux, "_cmd_resize_window", lambda *_a, **_kw: True)
    real_tmux.mark_window_stranded_draft("@1")

    ok, _msg, _name, wid = await real_tmux.create_window(
        str(tmp_path), start_claude=False
    )

    assert ok and wid == "@1"
    assert not session_mod.window_has_stranded_draft("@1")


# ── r2 F3: a FAILED Enter is COMMIT_UNKNOWN, never "withheld" ────────────


@pytest.mark.asyncio
async def test_a_failed_enter_is_commit_unknown_and_keeps_its_stamp(
    monkeypatch,
) -> None:
    """``send_keys`` returning False does NOT prove the key never reached the pty.
    Claiming ``draft_written`` ("Enter deliberately withheld") would be a LIE, and
    the stamp — which fired immediately before that Enter — must STAND: a
    possibly-committed turn has to move the live-prose turn boundary."""
    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))
    stamps: list[tuple[int, int | None, str]] = []
    monkeypatch.setattr(
        session_mod,
        "_stamp_user_turn",
        lambda s: stamps.append((s.user_id, s.thread_id, s.window_id)),
    )

    async def send_keys(window_id, text, enter=True, literal=True):
        pane.sent.append((text, enter, literal))
        return not enter  # the literal writes succeed; the Enter FAILS

    monkeypatch.setattr(real_tmux, "send_keys", send_keys)

    result = await session_manager.deliver_to_window(
        "@1", "hello", user_turn=delivery.UserTurnStamp(7, 42, "@1")
    )

    assert result.outcome is delivery.DeliveryOutcome.COMMIT_UNKNOWN
    assert result.reason == delivery.REASON_ENTER_FAILED
    assert stamps == [(7, 42, "@1")]  # the stamp STANDS — deliberately
    assert "may or may not have been submitted" in result.message
    # It is still not a success, and it arms the brake (if the Enter never
    # landed, the draft IS stranded; if it did, the empty-row self-heal releases).
    assert result.refused
    assert session_mod.window_has_stranded_draft("@1")


def test_no_provably_uncommitted_outcome_can_carry_a_stamp() -> None:
    """The invariant, stated so it is TRUE: every NOT_WRITTEN / DRAFT_WRITTEN exit
    is decided BEFORE the Enter (a stamp that RAISES is one of them — it never
    committed), so only COMMIT_UNKNOWN can follow a successful stamp."""
    import inspect

    src = inspect.getsource(session_mod.SessionManager._deliver_locked)
    after_enter = src.split("# (7) Enter", 1)[1]
    assert "delivery.refuse(" not in after_enter  # no written= classification
    assert "commit_unknown" in after_enter


# ── r2 F4: the re-verify's LAST observation is the pane capture ──────────


@pytest.mark.asyncio
async def test_the_command_probe_runs_before_the_final_capture(monkeypatch) -> None:
    """A stalled, UNBOUNDED ``pane_current_command`` used to sit BETWEEN the final
    capture and the Enter, so a prompt drawn in that stall was committed against a
    STALE input-box frame. The capture must be the last thing observed."""
    pane = _Pane([IDLE_PANE_V2_1_207])
    order: list[str] = []

    async def cmd(window_id):
        order.append("cmd")
        return "2.1.207"

    async def cap(window_id, with_ansi=False, scrollback_lines=0):
        order.append("capture")
        return pane.captures[0]

    _wire(monkeypatch, pane)
    monkeypatch.setattr(real_tmux, "pane_current_command", cmd)
    monkeypatch.setattr(real_tmux, "capture_pane_cancellation_safe", cap)

    assert (await session_manager.deliver_to_window("@1", "hello")).ok
    # pre-write: cmd → capture; re-verify: cmd → capture (the LAST observation).
    assert order == ["cmd", "capture", "cmd", "capture"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stall_at", [1, 2])
async def test_a_stalled_command_probe_is_bounded_and_classifies(
    monkeypatch, stall_at: int
) -> None:
    """ONLY ``asyncio.TimeoutError`` classifies (the /cost r1 P2 rule) — the probe
    can no longer hang the transaction open, at EITHER seam."""
    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))
    calls = {"n": 0}

    async def cmd(window_id):
        calls["n"] += 1
        if calls["n"] >= stall_at:
            await asyncio.sleep(10)
        return "2.1.207"

    monkeypatch.setattr(real_tmux, "pane_current_command", cmd)
    monkeypatch.setattr(session_mod, "CMD_PROBE_DEADLINE_S", 0.01)

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.reason == delivery.REASON_CMD_PROBE_TIMEOUT
    assert not pane.committed
    assert not real_tmux.window_send_lock("@1").locked()
    if stall_at == 1:  # stalled BEFORE the write
        assert result.outcome is delivery.DeliveryOutcome.NOT_WRITTEN
        assert pane.sent == []
    else:  # stalled at the re-verify — the text is in the box
        assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
        assert session_mod.window_has_stranded_draft("@1")


@pytest.mark.asyncio
async def test_a_genuine_cancellation_of_the_command_probe_propagates(
    monkeypatch,
) -> None:
    _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))

    async def cmd(window_id):
        raise asyncio.CancelledError

    monkeypatch.setattr(real_tmux, "pane_current_command", cmd)

    with pytest.raises(asyncio.CancelledError):
        await session_manager.deliver_to_window("@1", "hello")


# ── r2 F5: a failed literal write is NEVER classified "not written" ──────


@pytest.mark.asyncio
async def test_a_first_segment_write_failure_is_classified_written(monkeypatch) -> None:
    """``send_keys`` returning False does not prove ZERO bytes reached the pane —
    tmux may have failed after writing. Fail closed: arm the brake (the
    empty-input-row self-heal releases it if nothing actually landed)."""
    pane = _wire(monkeypatch, _Pane([IDLE_PANE_V2_1_207]))

    async def send_keys(window_id, text, enter=True, literal=True):
        pane.sent.append((text, enter, literal))
        return False

    monkeypatch.setattr(real_tmux, "send_keys", send_keys)

    result = await session_manager.deliver_to_window("@1", "hello")

    assert result.reason == delivery.REASON_SEND_FAILED
    assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
    assert session_mod.window_has_stranded_draft("@1")


# ── §1.4: the reason → copy map ──────────────────────────────────────────


def test_refusal_copy_is_exhaustive_over_the_reason_set() -> None:
    """STRICT key-set equality (the /cost precedent): a new leg name without
    mapped, actionable copy fails HERE, not in production."""
    assert set(delivery.REFUSAL_COPY) == delivery.DELIVERY_REFUSAL_REASONS


def test_every_parser_leg_name_has_copy() -> None:
    assert tp.INPUT_BOX_FAILURE_REASONS <= set(delivery.REFUSAL_COPY)


def test_copy_is_actionable_per_reason() -> None:
    assert "Answer the card first" in delivery.REFUSAL_COPY["prompt_present"]
    assert "/update" in delivery.REFUSAL_COPY["not_claude"]
    assert "just a number" in delivery.REFUSAL_COPY["lone_hotkey_segment"]
    assert "control character" in delivery.REFUSAL_COPY["control_chars"]
    assert "line breaks are fine" in delivery.REFUSAL_COPY["control_chars"]
    assert "Esc" in delivery.REFUSAL_COPY["tasks_mode"]
    assert "@" in delivery.REFUSAL_COPY["completion_overlay"]


def test_draft_clear_copy_tells_the_truth_gh56() -> None:
    """GH #56: a single Escape / Ctrl+U does NOT clear a draft on 2.1.209; two
    rapid Escapes do (and /esc performs that safely on a braked window). The
    stranded_draft + draft_written copy must advertise the double-press and must
    NOT claim a single Escape or Ctrl+U clears the box."""
    for reason in (delivery.REASON_STRANDED_DRAFT, delivery.REASON_REVERIFY_FAILED):
        copy = delivery.REFUSAL_COPY[reason]
        assert "twice" in copy.lower(), reason
        assert "/esc" in copy, reason
        assert "Ctrl+U" not in copy, reason
    # commit_unknown KEEPS its screenshot-first guidance (the Enter may already
    # have landed — a double-Escape could interrupt the resulting turn) and
    # mentions /esc only CONDITIONALLY.
    cu = delivery.REFUSAL_COPY[delivery.REASON_ENTER_FAILED]
    assert "/screenshot" in cu
    assert "if you still see your text" in cu.lower()


def test_quarantine_copy_is_the_exact_shipped_constant() -> None:
    """The quarantine refusal keeps its EXACT string (the shipped contract)."""
    r = delivery.refuse(
        delivery.REASON_QUARANTINED,
        written=False,
        message=session_mod.QUARANTINE_SEND_REFUSED_MSG,
    )
    assert r.message == session_mod.QUARANTINE_SEND_REFUSED_MSG


# ── §1.6: observability ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refusal_logs_reason_never_pane_text_or_message(
    monkeypatch, caplog
) -> None:
    _wire(monkeypatch, _Pane([auq_single_picker_pane()]))
    caplog.set_level("INFO", logger="cctelegram.session")

    await session_manager.deliver_to_window("@1", "my secret payload")

    refusals = [r for r in caplog.records if "DELIVERY REFUSED" in r.getMessage()]
    assert len(refusals) == 1
    msg = refusals[0].getMessage()
    assert "reason=" in msg and "outcome=not_written" in msg
    assert "my secret payload" not in msg
    assert "favorite color" not in msg  # no pane text


# ── §1.6: the non-exec CLAUDE_COMMAND wrapper warning ────────────────────


def test_non_exec_wrapper_warns(tmp_path, monkeypatch, caplog) -> None:
    from cctelegram import bot as bot_module

    wrapper = tmp_path / "claude"
    wrapper.write_text('#!/bin/sh\n/usr/local/bin/claude-real "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(bot_module.config, "claude_command", str(wrapper))
    monkeypatch.setattr(bot_module.shutil, "which", lambda _c: str(wrapper))
    caplog.set_level("WARNING", logger="cctelegram.bot")

    bot_module._warn_if_non_exec_claude_wrapper()

    assert any("no `exec` line" in r.getMessage() for r in caplog.records)


def test_exec_wrapper_is_silent(tmp_path, monkeypatch, caplog) -> None:
    from cctelegram import bot as bot_module

    wrapper = tmp_path / "claude"
    wrapper.write_text('#!/bin/sh\nexec /usr/local/bin/claude-real "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(bot_module.config, "claude_command", str(wrapper))
    monkeypatch.setattr(bot_module.shutil, "which", lambda _c: str(wrapper))
    caplog.set_level("WARNING", logger="cctelegram.bot")

    bot_module._warn_if_non_exec_claude_wrapper()

    assert not [r for r in caplog.records if "exec" in r.getMessage()]


def test_real_binary_is_silent(tmp_path, monkeypatch, caplog) -> None:
    from cctelegram import bot as bot_module

    binary = tmp_path / "claude"
    binary.write_bytes(b"\x7fELF\x02\x01\x01\x00binary")
    monkeypatch.setattr(bot_module.config, "claude_command", str(binary))
    monkeypatch.setattr(bot_module.shutil, "which", lambda _c: str(binary))
    caplog.set_level("WARNING", logger="cctelegram.bot")

    bot_module._warn_if_non_exec_claude_wrapper()

    assert caplog.records == []


# ── ungated seams stay ungated ───────────────────────────────────────────


def test_esc_and_nav_and_dispatchers_do_not_route_through_the_gate() -> None:
    """``/esc``, the bash quick-keys, and the AUQ/Decision dispatchers key into
    LIVE surfaces BY DESIGN — they call ``tmux_manager.send_keys`` directly and
    must NOT acquire the delivery gate (which would refuse the very pane they
    target). Pinned structurally: only ``send_to_window`` / ``deliver_to_window``
    gate."""
    import inspect

    from cctelegram import bot as bot_module
    from cctelegram.callback_dispatcher import interactive as interactive_mod

    esc_src = inspect.getsource(bot_module.esc_command)
    assert "send_keys" in esc_src
    assert "send_to_window" not in esc_src
    assert "deliver_to_window" not in esc_src

    dispatch_src = inspect.getsource(interactive_mod)
    assert "send_to_window" not in dispatch_src
    assert "deliver_to_window" not in dispatch_src


# ── r1 P1: the recognizers may only LABEL a refusal, never PRE-EMPT the proof ──
#
# ``_input_box_reason`` used to run ``is_interactive_ui`` / ``parse_unknown_blocking_prompt``
# BEFORE the positive input-box proof, so a recognizer firing while the input box was
# demonstrably LIVE refused a legitimate message. The realistic shape: the user ANSWERS an
# AUQ / ExitPlanMode prompt in the terminal, its rendering is still on-screen, and Claude has
# restored the input box below it. AUQ/EPM ``UIPattern``s carry no strict validator (unlike
# Permission/Workflow/Decision, whose ``_only_chrome_below`` guard rejects exactly this), so
# ``is_interactive_ui`` still matched — and EVERY message in that topic was refused until the
# picker scrolled off. The proof is now the sole authority; the recognizers only upgrade an
# already-INDETERMINATE reason to the actionable ``prompt_present`` copy.

_ANSWERED_ABOVE_LIVE_BOX = [
    "auq_single_picker_v2.1.207.txt",
    "auq_multi_picker_v2.1.207.txt",
    "gate_epm_v2.1.207.txt",
    "gate_permission_v2.1.207.txt",
    "gate_workflow_v2.1.207.txt",
    "switch_model_live_v2.1.207.txt",
    "folder_trust_arrival_plain_v2.1.207.txt",
]


@pytest.mark.parametrize("prompt_fixture", _ANSWERED_ABOVE_LIVE_BOX)
@pytest.mark.parametrize(
    "box_fixture",
    ["inputbox_idle_v2.1.207.txt", "inputbox_busy_thinking_v2.1.207.txt"],
)
def test_answered_prompt_above_a_live_input_box_still_delivers(
    prompt_fixture: str, box_fixture: str
) -> None:
    """A resolved prompt still RENDERED above a restored input box must not refuse."""
    pane = pane_fixture(prompt_fixture).rstrip("\n") + "\n" + pane_fixture(box_fixture)
    # The positive proof is unambiguous: the input box is live.
    assert tp.pane_input_box_present(pane) is True
    # ...so the gate must deliver, even though is_interactive_ui matches the stale render.
    assert session_mod.SessionManager._input_box_reason(pane) is None


def test_positive_proof_alone_refuses_every_blocking_surface() -> None:
    """The recognizers buy no SAFETY — the proof already covers every blocking pane.

    This is the flag-independence claim, pinned: the recognizers are filtered by the
    ``CC_TELEGRAM_PERMISSION_PROMPTS`` / ``CC_TELEGRAM_DECISION_CARDS`` display
    kill-switches, so if the gate leaned on them a flag-OFF deploy would reopen the hole.
    """
    for name in _ANSWERED_ABOVE_LIVE_BOX + [
        "inputbox_tasks_mode_v2.1.207.txt",
        "inputbox_at_overlay_v2.1.207.txt",
        "inputbox_slash_overlay_v2.1.207.txt",
        "overlay_cost_modal_v2.1.207.txt",
        "shell_after_esc_v2.1.207.txt",
    ]:
        assert tp.pane_input_box_present(pane_fixture(name)) is False, name


def test_a_live_blocking_prompt_still_refuses_as_prompt_present() -> None:
    """The LABEL upgrade survives: a live gate refuses with the actionable copy."""
    for name in [
        "gate_epm_v2.1.207.txt",
        "gate_workflow_v2.1.207.txt",
        "switch_model_live_v2.1.207.txt",
        "folder_trust_arrival_plain_v2.1.207.txt",
    ]:
        reason = session_mod.SessionManager._input_box_reason(pane_fixture(name))
        assert reason == delivery.REASON_PROMPT_PRESENT, (name, reason)
        assert "answer the card first" in delivery.REFUSAL_COPY[reason].lower()
