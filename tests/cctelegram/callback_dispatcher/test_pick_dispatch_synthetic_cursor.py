"""RED-first contract: the pick dispatch BAILS on a SYNTHETIC (phantom) cursor.

``terminal_parser._overlay_cursor_and_selection`` defaults the cursor to option
1 when the live pane carries no visible ``❯`` (the real cursor scrolled off a
tall card whose top options are above the captured region). On the side-file
render path the resolved ``current_form`` therefore ALWAYS shows a cursor —
PHANTOM at option 1 when the real cursor is off-screen. Tapping option 1 then
computes ``delta = 0`` (no nav), the verify step re-parses the same phantom and
passes, and ``Enter`` commits whatever the REAL (off-screen) cursor is on — a
WRONG dispatch.

The guard makes ``_dispatch_pick`` re-parse a PANE-ONLY form and, when it has NO
real ``cursor=True`` option (so ``current_form``'s cursor is the synthetic
default), BAIL ``not_advanced`` BEFORE sending any keystroke. ``Enter`` is never
sent on a synthetic cursor.

RED on current code: the dispatch sends ``Enter`` (the wrong commit).
GREEN: no keystroke is sent and the ledger records ``not_advanced``.

A visible-cursor regression test proves the normal path is byte-identical
(still navigates + commits).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cctelegram.callback_dispatcher import (
    DispatcherAdapters,
    authorize_initial,
    execute,
    parse,
)
from cctelegram.handlers import auq_ledger, auq_source, pick_token
from cctelegram.handlers.callback_data import CB_ASK_PICK
from cctelegram.session import WindowState, session_manager
from cctelegram.terminal_parser import resolve_ask_form

_FX = Path(__file__).parents[1] / "fixtures"
_OWNER_ID = 1
_THREAD_ID = 10
_WINDOW_ID = "@1"
_SID = "4766fb07-7057-4981-9832-93e524ab943e"
_SIDEFILE = _FX / "auq_single_select_with_affordances_sidefile.json"
_PANE_WITH_CURSOR = (_FX / "auq_single_select_with_affordances_pane.txt").read_text()
# The same picker captured with the ❯ cursor scrolled OFF the visible region
# (the tall-card off-screen-cursor case): no ``❯`` anywhere in the options.
_PANE_NO_CURSOR = _PANE_WITH_CURSOR.replace("❯ ", "  ")


def _write_side_file(cc_dir: Path) -> dict:
    sidefile = json.loads(_SIDEFILE.read_text())
    pending = cc_dir / "auq_pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    (pending / f"{_SID}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SID,
                "tool_use_id": sidefile["tool_use_id"],
                "written_at": time.time(),
                "tool_input": sidefile["tool_input"],
            }
        )
    )
    return sidefile["tool_input"]


class _StaticPicker:
    """A fake tmux whose pane is a FIXED capture (the off-screen-cursor pane).

    Records every ``send_keys`` so the test can assert NO keystroke was sent.
    ``capture_pane`` always returns the same pane (the cursor never appears),
    modelling a tall card whose real cursor stays off-screen.
    """

    def __init__(self, window_id: str, pane: str) -> None:
        self.window_id = window_id
        self.pane = pane
        self.sent: list[tuple[str, str, bool, bool]] = []

    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del with_ansi, scrollback_lines
        return self.pane if window_id == self.window_id else ""

    async def find_window_by_id(self, window_id: str) -> Any:
        if window_id != self.window_id:
            return None
        return SimpleNamespace(window_id=self.window_id, window_name="repo")

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.sent.append((window_id, keys, enter, literal))
        return True


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = SimpleNamespace(message_thread_id=_THREAD_ID)
        self.answers: list[tuple[str | None, bool | None]] = []

    async def answer(
        self, text: str | None = None, show_alert: bool | None = None
    ) -> None:
        self.answers.append((text, show_alert))


class FakeSessionManager:
    def resolve_window_for_thread(
        self, _user_id: int, _thread_id: int | None
    ) -> str | None:
        return _WINDOW_ID


def _ctx(query: FakeQuery, user_id: int = _OWNER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        update=SimpleNamespace(
            message=None,
            callback_query=query,
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=None,
        ),
        context=SimpleNamespace(user_data={}, bot=SimpleNamespace()),
        user=SimpleNamespace(id=user_id),
        query=query,
        user_id=user_id,
        thread_id=_THREAD_ID,
    )


def _adapters(picker: _StaticPicker) -> DispatcherAdapters:
    return DispatcherAdapters(
        session_manager=FakeSessionManager(),
        tmux_manager=picker,
        bot=SimpleNamespace(),
        route_runtime=SimpleNamespace(
            snapshot=lambda _route: None, mark_inbound_sent=AsyncMock()
        ),
        config=SimpleNamespace(browse_root="."),
        terminal_parser=SimpleNamespace(
            resolve_ask_form=lambda cached, pane: resolve_ask_form(cached, pane)
        ),
    )


def _mint_callback(option_number: int, option_label: str, pane: str) -> str:
    """Mint a side-file-sourced pick token for ``option_number`` against ``pane``.

    The source MUST resolve to ``side_file`` (the phantom-cursor render path), so
    ``current_form`` carries the overlay's synthetic cursor.
    """
    source = auq_source.resolve_auq_source(_WINDOW_ID, None, pane)
    assert source.kind == "side_file", f"expected side_file, got {source.kind}"
    form = resolve_ask_form(source.payload, pane)
    assert form is not None
    fingerprint = form.fingerprint()
    token = pick_token.mint(
        pick_token.PickTokenEntry(
            window_id=_WINDOW_ID,
            user_id=_OWNER_ID,
            thread_id=_THREAD_ID,
            fingerprint=fingerprint,
            option_number=option_number,
            option_label=option_label,
            is_review_submit=False,
            expires_at=time.monotonic() + 300,
            source_kind=source.kind,
            source_fingerprint=source.source_fingerprint,
            row_generation=1,
        )
    )
    route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
    return f"{CB_ASK_PICK}{route_hash}:{fingerprint[:8]}:{option_number}:{token}"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from cctelegram.callback_dispatcher import interactive as cb_interactive

    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests(path=tmp_path / "ledger.jsonl", start_time=time.time())
    auq_source.reset_for_tests()
    session_manager.window_states[_WINDOW_ID] = WindowState(
        cwd="/tmp/cwd", session_id=_SID
    )
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    monkeypatch.setattr(
        cb_interactive, "handle_interactive_ui", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        "cctelegram.handlers.interactive_ui.resolve_ask_tool_input", lambda _wid: None
    )
    yield
    session_manager.window_states.pop(_WINDOW_ID, None)
    pick_token.reset_for_tests()
    auq_ledger.reset_for_tests()
    auq_source.reset_for_tests()


async def _run(callback_data: str, picker: _StaticPicker) -> FakeQuery:
    query = FakeQuery(callback_data)
    authorized = authorize_initial(parse(query.data.encode()), _ctx(query))
    await execute(authorized, _adapters(picker))
    return query


def _ledger_state(option_number: int, pane: str) -> str | None:
    route_hash = auq_ledger.make_route_hash(_OWNER_ID, _THREAD_ID, _WINDOW_ID)
    form = resolve_ask_form(
        auq_source.resolve_auq_source(_WINDOW_ID, None, pane).payload, pane
    )
    assert form is not None
    key = auq_ledger.make_ledger_key(route_hash, form.fingerprint()[:8], option_number)
    entry = auq_ledger.lookup(key)
    return entry.state if entry is not None else None


# ── the guard: a synthetic cursor never commits ─────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_bails_on_synthetic_cursor(tmp_path: Path) -> None:
    _write_side_file(tmp_path)
    picker = _StaticPicker(_WINDOW_ID, _PANE_NO_CURSOR)

    # Tapping option 1 with the real cursor OFF-SCREEN (phantom at 1): the guard
    # must bail BEFORE any keystroke — Enter is never sent against the unknown
    # real cursor position.
    await _run(
        _mint_callback(
            1,
            "Full fix: descriptions + ordering + first-render robustness",
            _PANE_NO_CURSOR,
        ),
        picker,
    )
    assert picker.sent == [], "no keystroke may be sent on a synthetic cursor"
    assert _ledger_state(1, _PANE_NO_CURSOR) == "not_advanced"


@pytest.mark.asyncio
async def test_visible_cursor_dispatch_unchanged(tmp_path: Path) -> None:
    """Regression: a VISIBLE real-cursor dispatch is byte-identical (still
    navigates + commits Enter). The pane HAS the ❯ cursor on option 1; tapping
    option 1 sends only Enter (cursor already on target)."""
    _write_side_file(tmp_path)
    picker = _StaticPicker(_WINDOW_ID, _PANE_WITH_CURSOR)

    await _run(
        _mint_callback(
            1,
            "Full fix: descriptions + ordering + first-render robustness",
            _PANE_WITH_CURSOR,
        ),
        picker,
    )
    keys = [(k, e, lit) for _w, k, e, lit in picker.sent]
    # Cursor already on option 1 → only Enter (the version-stable commit).
    assert keys == [("Enter", False, False)]
