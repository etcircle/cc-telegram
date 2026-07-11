"""Wave C seam tests — the user-turn stamp mirrors into route_runtime.

Spec section A (v3 C3a): the delivery stamp must land the SAME ``time.time()``
value into ``route_runtime.snapshot(route).last_user_turn_at`` as into
``message_queue``'s ``_route_user_turn_at`` store. The mirror lives INSIDE
``message_queue.set_route_user_turn_at`` (one writer, same-ts by construction).

GH #50 §1.5 MOVED the stamp: the four delivery seams
(``inbound_aggregator._send_bundle``, ``bot.forward_command_handler``, the
``/effort`` callback, the ``aql:`` late answer) no longer stamp PRE-SEND — they
hand ``send_to_window`` a narrowly-typed ``delivery.UserTurnStamp`` request, and
the GATED transaction fires it after every gate passes and immediately BEFORE the
Enter. Timing is preserved (the boundary still precedes any prose the turn
streams, which is what the live-prose freshness gate needs), and the load-bearing
new property is that **a REFUSED send is never stamped** — the seams used to stamp
every refusal.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import route_runtime
from cctelegram.delivery import UserTurnStamp
from cctelegram.handlers import inbound_aggregator, message_queue
from cctelegram.session import session_manager
from cctelegram.tmux_manager import tmux_manager as real_tmux
from tests.conftest import IDLE_PANE_V2_1_207, auq_single_picker_pane


@pytest.fixture(autouse=True)
def _reset():
    route_runtime.reset_for_tests()
    message_queue._route_user_turn_at.clear()
    real_tmux.reset_window_send_locks_for_tests()
    yield
    route_runtime.reset_for_tests()
    message_queue._route_user_turn_at.clear()
    real_tmux.reset_window_send_locks_for_tests()


def _patch_pane(monkeypatch: pytest.MonkeyPatch, pane: str) -> list[tuple]:
    """Wire the REAL delivery transaction onto a fake tmux showing ``pane``."""
    sent: list[tuple] = []

    async def find(window_id: str):
        return SimpleNamespace(window_id=window_id)

    async def capture(
        window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ):
        return pane

    async def send_keys(
        window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        sent.append((window_id, text, enter, literal))
        return True

    monkeypatch.setattr(real_tmux, "find_window_by_id", find)
    monkeypatch.setattr(real_tmux, "capture_pane_cancellation_safe", capture)
    monkeypatch.setattr(
        real_tmux, "pane_current_command", AsyncMock(return_value="2.1.207")
    )
    monkeypatch.setattr(real_tmux, "send_keys", send_keys)
    return sent


def test_set_route_user_turn_at_mirrors_same_ts_into_route_runtime():
    """The shared stamp function writes BOTH stores with the SAME ts value."""
    message_queue.set_route_user_turn_at(1, 42, "@7")
    mq_ts = message_queue.peek_route_user_turn_at(1, 42, "@7")
    rr_ts = route_runtime.snapshot((1, 42, "@7")).last_user_turn_at
    assert mq_ts is not None
    assert rr_ts == mq_ts


@pytest.mark.asyncio
async def test_delivery_transaction_stamps_immediately_before_enter(monkeypatch):
    """The pre-commit hook fires INSIDE the send lock, after every gate, and
    strictly BEFORE the commit Enter (plan §1.5)."""
    stamp_at: dict[str, float | None] = {}
    sent: list[tuple] = []

    async def find(window_id: str):
        return SimpleNamespace(window_id=window_id)

    async def capture(
        window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ):
        return IDLE_PANE_V2_1_207

    async def send_keys(
        window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        if enter and not literal:  # the commit Enter
            stamp_at["at_enter"] = message_queue.peek_route_user_turn_at(1, 42, "@7")
        else:  # the literal write — the stamp must NOT exist yet
            stamp_at["at_write"] = message_queue.peek_route_user_turn_at(1, 42, "@7")
        sent.append((text, enter, literal))
        return True

    monkeypatch.setattr(real_tmux, "find_window_by_id", find)
    monkeypatch.setattr(real_tmux, "capture_pane_cancellation_safe", capture)
    monkeypatch.setattr(
        real_tmux, "pane_current_command", AsyncMock(return_value="2.1.207")
    )
    monkeypatch.setattr(real_tmux, "send_keys", send_keys)

    result = await session_manager.deliver_to_window(
        "@7", "hello", user_turn=UserTurnStamp(1, 42, "@7")
    )

    assert result.ok
    assert stamp_at["at_write"] is None, "stamped before the gates/write — too early"
    assert stamp_at["at_enter"] is not None, "not stamped before the commit Enter"
    rr_ts = route_runtime.snapshot((1, 42, "@7")).last_user_turn_at
    assert rr_ts == stamp_at["at_enter"]


@pytest.mark.asyncio
async def test_refused_delivery_is_never_stamped(monkeypatch):
    """§1.7: no refusal receives a turn stamp. A live AUQ picker owns the pane,
    so the gate refuses before any keystroke — and the turn boundary must stay
    unset (a stamped refusal would suppress the NEXT turn's live prose)."""
    sent = _patch_pane(monkeypatch, auq_single_picker_pane())

    result = await session_manager.deliver_to_window(
        "@7", "hello", user_turn=UserTurnStamp(1, 42, "@7")
    )

    assert result.refused
    assert sent == []
    assert message_queue.peek_route_user_turn_at(1, 42, "@7") is None
    assert route_runtime.snapshot((1, 42, "@7")).last_user_turn_at is None


@pytest.mark.asyncio
async def test_stamp_hook_exception_withholds_enter_and_leaves_no_stamp(monkeypatch):
    """§1.5: a hook exception ⇒ ``draft_written``, NO Enter, NO stamp."""
    sent = _patch_pane(monkeypatch, IDLE_PANE_V2_1_207)
    from cctelegram import session as session_mod

    def boom(_stamp):
        raise RuntimeError("stamp exploded")

    monkeypatch.setattr(session_mod, "_stamp_user_turn", boom)

    result = await session_manager.deliver_to_window(
        "@7", "hello", user_turn=UserTurnStamp(1, 42, "@7")
    )

    assert result.outcome.value == "draft_written"
    assert result.reason == "stamp_failed"
    # The text was written; the commit Enter was NOT sent.
    assert [t for _w, t, e, lit in sent if lit and not e] == ["hello"]
    assert not any(e and not lit for _w, _t, e, lit in sent)
    assert message_queue.peek_route_user_turn_at(1, 42, "@7") is None


@pytest.mark.asyncio
async def test_aggregator_send_bundle_stamps_route_runtime(monkeypatch):
    """End-to-end through the aggregator delivery seam (the REAL transaction)."""
    route = (1, 42, "@7")
    _patch_pane(monkeypatch, IDLE_PANE_V2_1_207)

    await inbound_aggregator.aggregator_offer_text(route, "hello")
    assert (await inbound_aggregator.aggregator_flush_route(route)).ok

    mq_ts = message_queue.peek_route_user_turn_at(1, 42, "@7")
    rr_ts = route_runtime.snapshot(route).last_user_turn_at
    assert mq_ts is not None
    assert rr_ts == mq_ts


@pytest.mark.asyncio
async def test_forward_command_handler_passes_the_stamp_request():
    """The slash-command seam hands the transaction a typed stamp request."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.message = MagicMock()
    update.message.text = "/model"
    update.message.message_thread_id = 42
    update.message.chat = MagicMock()
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = 100
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}

    with (
        patch("cctelegram.bot.is_user_allowed", return_value=True),
        patch("cctelegram.bot._get_thread_id", return_value=42),
        patch("cctelegram.bot.session_manager") as mock_sm,
        patch("cctelegram.bot.tmux_manager") as mock_tmux,
        patch("cctelegram.bot.safe_reply", new_callable=AsyncMock),
    ):
        mock_sm.resolve_window_for_thread.return_value = "@5"
        mock_sm.get_display_name.return_value = "project"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        from cctelegram.bot import forward_command_handler

        await forward_command_handler(update, context)

    kwargs = mock_sm.send_to_window.await_args.kwargs
    assert kwargs["user_turn"] == UserTurnStamp(user_id=1, thread_id=42, window_id="@5")


@pytest.mark.asyncio
async def test_effort_callback_passes_the_stamp_request():
    """End-to-end through the real /effort callback executor: it hands the
    delivery transaction the typed stamp request (never a pre-send stamp)."""
    from cctelegram.callback_dispatcher import effort as effort_mod

    user_id, thread_id, window_id = 1, 10, "@5"
    seen: list[object] = []

    class FakeQuery:
        def __init__(self) -> None:
            self.data = f"eff:medium:{window_id}"
            self.message = SimpleNamespace(message_thread_id=thread_id)

        async def answer(self, *args, **kwargs) -> None:
            pass

        async def edit_message_text(self, *args, **kwargs) -> None:
            pass

    query = FakeQuery()
    authorized = SimpleNamespace(
        command=SimpleNamespace(data=query.data),
        ctx=SimpleNamespace(
            query=query,
            user=SimpleNamespace(id=user_id),
            user_id=user_id,
            thread_id=thread_id,
        ),
    )

    async def send_to_window(_wid: str, _text: str, *, user_turn=None):
        seen.append(user_turn)
        return True, "ok"

    adapters = SimpleNamespace(
        session_manager=SimpleNamespace(
            resolve_window_for_thread=lambda _u, _t: window_id,
            send_to_window=send_to_window,
        ),
        tmux_manager=SimpleNamespace(
            find_window_by_id=AsyncMock(
                return_value=SimpleNamespace(window_id=window_id)
            )
        ),
        route_runtime=SimpleNamespace(mark_inbound_sent=AsyncMock()),
    )

    await effort_mod.execute_effort_callback(authorized, adapters)

    assert seen == [UserTurnStamp(user_id, thread_id, window_id)]


def test_late_answer_seam_passes_the_stamp_request():
    """The FOURTH stamp site (``aql:``) migrated too — no seam stamps pre-send."""
    from cctelegram.callback_dispatcher import late_answer as late_answer_exec

    src = late_answer_exec.__file__
    with open(src) as fh:
        body = fh.read()
    assert "set_route_user_turn_at" not in body
    assert "UserTurnStamp(" in body
