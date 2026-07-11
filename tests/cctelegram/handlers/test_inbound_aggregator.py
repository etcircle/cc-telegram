"""Tests for the §2.8 inbound aggregator."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cctelegram import delivery
from cctelegram.config import config
from cctelegram.handlers import inbound_aggregator
from cctelegram.handlers import message_queue


@pytest.fixture(autouse=True)
def _clear_aggregator_state():
    inbound_aggregator._route_pending.clear()
    inbound_aggregator._route_locks.clear()
    message_queue._route_user_turn_at.clear()
    yield
    inbound_aggregator._route_pending.clear()
    inbound_aggregator._route_locks.clear()
    message_queue._route_user_turn_at.clear()


@pytest.fixture(autouse=True)
def _short_debounce():
    """Use a tiny debounce so tests don't hang waiting on the real 1.5s."""
    original = config.aggregator_debounce_seconds
    config.aggregator_debounce_seconds = 0.05
    yield
    config.aggregator_debounce_seconds = original


@pytest.fixture
def captured_sends():
    sends: list[tuple[str, str]] = []

    async def fake_deliver(window_id: str, text: str, *, user_turn=None):
        sends.append((window_id, text))
        if user_turn is not None:
            # GH #50: the aggregator now hands the delivery transaction a typed
            # pre-commit stamp request instead of stamping pre-send itself. The
            # real transaction fires it immediately before the Enter; the fake
            # fires it on the success path so the turn-boundary tests still see
            # a stamp on a DELIVERED bundle (and never on a refused one).
            message_queue.set_route_user_turn_at(
                user_turn.user_id, user_turn.thread_id, user_turn.window_id
            )
        return delivery.delivered("ok")

    with patch.object(
        inbound_aggregator.session_manager,
        "deliver_to_window",
        side_effect=fake_deliver,
    ):
        yield sends


async def _wait_until_flushed(sends: list, expected: int = 1, timeout: float = 1.0):
    """Poll the sends list until ``expected`` calls land or timeout."""
    elapsed = 0.0
    step = 0.01
    while len(sends) < expected and elapsed < timeout:
        await asyncio.sleep(step)
        elapsed += step


@pytest.mark.asyncio
async def test_single_text_flushes_after_debounce(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_text(route, "hello world")
    await _wait_until_flushed(captured_sends, expected=1)
    assert captured_sends == [("@0", "hello world")]


@pytest.mark.asyncio
async def test_consecutive_text_coalesces(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_text(route, "first")
    await inbound_aggregator.aggregator_offer_text(route, "second")
    await _wait_until_flushed(captured_sends, expected=1)
    assert len(captured_sends) == 1
    flushed_text = captured_sends[0][1]
    assert "first" in flushed_text
    assert "second" in flushed_text


@pytest.mark.asyncio
async def test_media_group_coalesces_to_one_flush(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/img1.jpg"), "look at these", "mg-1"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/img2.jpg"), None, "mg-1"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/img3.jpg"), None, "mg-1"
    )
    await _wait_until_flushed(captured_sends, expected=1)
    assert len(captured_sends) == 1
    flushed = captured_sends[0][1]
    assert flushed.count("look at these") == 1
    assert "/tmp/img1.jpg" in flushed
    assert "/tmp/img2.jpg" in flushed
    assert "/tmp/img3.jpg" in flushed
    # Path arrival order preserved.
    assert (
        flushed.index("img1.jpg")
        < flushed.index("img2.jpg")
        < flushed.index("img3.jpg")
    )
    # Single grouped block: only one "(attachments:" header.
    assert flushed.count("(attachments:") == 1


@pytest.mark.asyncio
async def test_media_group_no_caption_groups_paths(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/a.jpg"), None, "mg-2"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/b.jpg"), None, "mg-2"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/c.jpg"), None, "mg-2"
    )
    await _wait_until_flushed(captured_sends, expected=1)
    assert len(captured_sends) == 1
    flushed = captured_sends[0][1]
    assert flushed.startswith("(attachments:")
    assert "/tmp/a.jpg" in flushed
    assert "/tmp/b.jpg" in flushed
    assert "/tmp/c.jpg" in flushed


@pytest.mark.asyncio
async def test_media_group_then_followup_text_appends_once(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/x1.jpg"), "shared caption", "mg-3"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/x2.jpg"), None, "mg-3"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/x3.jpg"), None, "mg-3"
    )
    await inbound_aggregator.aggregator_offer_text(route, "and one more thing")
    await _wait_until_flushed(captured_sends, expected=1)
    assert len(captured_sends) == 1
    flushed = captured_sends[0][1]
    assert flushed.count("shared caption") == 1
    assert "and one more thing" in flushed
    # Caption appears before follow-up; both before the (attachments: …)
    # block, all three paths grouped.
    assert flushed.index("shared caption") < flushed.index("and one more thing")
    assert flushed.index("and one more thing") < flushed.index("(attachments:")
    assert flushed.count("(attachments:") == 1
    for p in ("/tmp/x1.jpg", "/tmp/x2.jpg", "/tmp/x3.jpg"):
        assert p in flushed


@pytest.mark.asyncio
async def test_photo_then_fast_follow_text_coalesces(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/p.jpg"), None, None
    )
    await inbound_aggregator.aggregator_offer_text(route, "describe this please")
    await _wait_until_flushed(captured_sends, expected=1)
    assert len(captured_sends) == 1
    flushed = captured_sends[0][1]
    assert "describe this please" in flushed
    assert "/tmp/p.jpg" in flushed


@pytest.mark.asyncio
async def test_distinct_media_groups_force_flush_at_boundary(captured_sends):
    """Two media-groups inside the debounce window must NOT merge.

    Caption from group-2 leaking into group-1's bundle was the §2.8 bug:
    the boundary check force-flushes the in-progress bundle when a new
    mg-id arrives.
    """
    route = (1, 100, "@0")
    config.aggregator_debounce_seconds = 5.0
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/g1a.jpg"), "first album", "mg-1"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/g1b.jpg"), None, "mg-1"
    )
    # Boundary: different mg-id → previous bundle force-flushes.
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/g2a.jpg"), "second album", "mg-2"
    )
    await _wait_until_flushed(captured_sends, expected=1)
    # Force the new bundle out so we can inspect it.
    await inbound_aggregator.aggregator_flush_route(route)
    assert len(captured_sends) == 2
    first, second = captured_sends[0][1], captured_sends[1][1]
    assert "first album" in first
    assert "second album" not in first
    assert "/tmp/g1a.jpg" in first and "/tmp/g1b.jpg" in first
    assert "/tmp/g2a.jpg" not in first
    assert "second album" in second
    assert "/tmp/g2a.jpg" in second


@pytest.mark.asyncio
async def test_ungrouped_attachment_does_not_reset_media_group_boundary(captured_sends):
    """An mg=None attachment between two groups must not erase the boundary memory."""
    route = (1, 100, "@0")
    config.aggregator_debounce_seconds = 5.0
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/g1.jpg"), "first album", "mg-1"
    )
    # Non-grouped photo joins the in-progress bundle. Must NOT reset
    # current_media_group_id to None, else the next group's boundary
    # check would skip and merge the two albums.
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/loose.jpg"), None, None
    )
    # Boundary: arrival of mg-2 must force-flush g1 + loose together,
    # then start a fresh bundle for mg-2.
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/g2.jpg"), "second album", "mg-2"
    )
    await _wait_until_flushed(captured_sends, expected=1)
    await inbound_aggregator.aggregator_flush_route(route)
    assert len(captured_sends) == 2
    first, second = captured_sends[0][1], captured_sends[1][1]
    assert "first album" in first
    assert "/tmp/g1.jpg" in first and "/tmp/loose.jpg" in first
    assert "/tmp/g2.jpg" not in first
    assert "second album" not in first
    assert "second album" in second
    assert "/tmp/g2.jpg" in second
    assert "/tmp/g1.jpg" not in second


@pytest.mark.asyncio
async def test_caption_dedup_within_media_group(captured_sends):
    """Telegram repeats the same caption on every media-group item; we dedup."""
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/d1.jpg"), "same caption", "mg-d"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/d2.jpg"), "same caption", "mg-d"
    )
    await inbound_aggregator.aggregator_offer_photo(
        route, Path("/tmp/d3.jpg"), "same caption", "mg-d"
    )
    await _wait_until_flushed(captured_sends, expected=1)
    flushed = captured_sends[0][1]
    assert flushed.count("same caption") == 1


@pytest.mark.asyncio
async def test_max_attachments_triggers_immediate_flush(captured_sends):
    route = (1, 100, "@0")
    original_max = config.aggregator_max_attachments
    config.aggregator_max_attachments = 10
    # Use a large debounce so we can prove the cap, not the timer, fired.
    config.aggregator_debounce_seconds = 5.0
    try:
        for i in range(11):
            await inbound_aggregator.aggregator_offer_photo(
                route, Path(f"/tmp/m{i}.jpg"), None, None
            )
        # No need to wait long: flush should have fired synchronously inside
        # aggregator_offer_photo when the cap was hit.
        await asyncio.sleep(0)
        # The 10th photo trips the cap → first 10 flush. The 11th lands in
        # a fresh bundle and does NOT yet flush (debounce is 5s).
        assert len(captured_sends) == 1
        flushed = captured_sends[0][1]
        # 10 photos in the flushed bundle (m0..m9).
        for i in range(10):
            assert f"/tmp/m{i}.jpg" in flushed
        assert "/tmp/m10.jpg" not in flushed
    finally:
        config.aggregator_max_attachments = original_max


@pytest.mark.asyncio
async def test_force_flush_drains_before_slash_command(captured_sends):
    route = (1, 100, "@0")
    config.aggregator_debounce_seconds = 5.0
    await inbound_aggregator.aggregator_offer_text(route, "pre-command text")
    # No flush yet (long debounce).
    assert captured_sends == []
    await inbound_aggregator.aggregator_flush_route(route)
    assert captured_sends == [("@0", "pre-command text")]


@pytest.mark.asyncio
async def test_teardown_cancels_pending_flush(captured_sends):
    route = (1, 100, "@0")
    config.aggregator_debounce_seconds = 5.0
    await inbound_aggregator.aggregator_offer_text(route, "should not be sent")
    assert inbound_aggregator.has_pending(route)
    inbound_aggregator.aggregator_clear_route(route)
    # Even after waiting past where the debounce would have landed, nothing
    # was sent.
    await asyncio.sleep(0.05)
    assert captured_sends == []
    assert not inbound_aggregator.has_pending(route)


@pytest.mark.asyncio
async def test_voice_offer_treated_as_text(captured_sends):
    route = (1, 100, "@0")
    await inbound_aggregator.aggregator_offer_voice(route, "transcribed audio body")
    await _wait_until_flushed(captured_sends, expected=1)
    assert captured_sends == [("@0", "transcribed audio body")]


@pytest.mark.asyncio
async def test_unbound_topic_pending_then_directory_pick_flushes(captured_sends):
    """Surrogate for the user-flow: pending photos pile up, then a route is
    bound and the bot feeds them to the aggregator + force-flushes.

    The bot.py flow (``_create_and_bind_window`` and the window-picker
    bind path) calls ``aggregator_offer_text`` + ``aggregator_offer_photo``
    + ``aggregator_flush_route`` in that order. Verify the resulting flush
    is the §2.8.2 single-text + grouped-paths shape.
    """
    route = (1, 100, "@0")
    pending_text = "first message in the new topic"
    pending_photos = [
        ("/tmp/u1.jpg", "stash caption", "mg-x"),
        ("/tmp/u2.jpg", "", "mg-x"),
    ]

    await inbound_aggregator.aggregator_offer_text(route, pending_text)
    for path_str, caption, media_group_id in pending_photos:
        await inbound_aggregator.aggregator_offer_photo(
            route, Path(path_str), caption, media_group_id
        )
    await inbound_aggregator.aggregator_flush_route(route)

    assert len(captured_sends) == 1
    flushed = captured_sends[0][1]
    assert "first message in the new topic" in flushed
    assert "stash caption" in flushed
    assert "/tmp/u1.jpg" in flushed
    assert "/tmp/u2.jpg" in flushed
    assert flushed.count("(attachments:") == 1


@pytest.mark.asyncio
async def test_send_to_window_failure_is_logged_not_raised():
    """A send_to_window failure must not crash the flush path."""
    route = (1, 100, "@0")

    async def failing_send(window_id: str, text: str) -> tuple[bool, str]:
        return False, "tmux missing"

    with patch.object(
        inbound_aggregator.session_manager,
        "send_to_window",
        side_effect=failing_send,
    ):
        await inbound_aggregator.aggregator_offer_text(route, "x")
        # Force the flush so we don't depend on the debounce timer.
        await inbound_aggregator.aggregator_flush_route(route)
    # No exception, bundle is gone.
    assert not inbound_aggregator.has_pending(route)


@pytest.mark.asyncio
async def test_send_to_window_exception_is_swallowed():
    """A send_to_window crash must not leak; we log & move on."""
    route = (1, 100, "@0")

    async def crashing_send(window_id: str, text: str) -> tuple[bool, str]:
        raise RuntimeError("boom")

    with patch.object(
        inbound_aggregator.session_manager,
        "send_to_window",
        side_effect=crashing_send,
    ):
        await inbound_aggregator.aggregator_offer_text(route, "x")
        await inbound_aggregator.aggregator_flush_route(route)
    assert not inbound_aggregator.has_pending(route)


def test_session_manager_mock_protocol():
    """Sanity check: ``send_to_window`` is the public API the aggregator uses."""
    assert hasattr(inbound_aggregator.session_manager, "send_to_window")
    # AsyncMock works as a stand-in.
    session_manager_mock = AsyncMock()
    session_manager_mock.send_to_window = AsyncMock(return_value=(True, "ok"))


# ── The user-turn delivery stamp rides the PRE-COMMIT hook (GH #50 §1.5) ─────


@pytest.mark.asyncio
async def test_send_bundle_passes_the_pre_commit_turn_stamp():
    """The aggregator no longer stamps pre-send: it hands the gated delivery
    transaction a narrowly-typed ``UserTurnStamp`` request, which the transaction
    fires after every gate passes and immediately BEFORE the Enter. Timing is
    preserved (the boundary still precedes any prose the turn streams), but a
    REFUSED send is never stamped."""
    route = (1, 100, "@0")
    seen: list[object] = []

    async def checking_deliver(window_id: str, text: str, *, user_turn=None):
        seen.append(user_turn)
        # The stamp has NOT been written by the caller — the transaction owns it.
        assert message_queue.peek_route_user_turn_at(1, 100, "@0") is None
        return delivery.delivered("ok")

    with patch.object(
        inbound_aggregator.session_manager,
        "deliver_to_window",
        side_effect=checking_deliver,
    ):
        await inbound_aggregator.aggregator_offer_text(route, "hello")
        await inbound_aggregator.aggregator_flush_route(route)

    assert len(seen) == 1
    stamp = seen[0]
    assert isinstance(stamp, delivery.UserTurnStamp)
    assert (stamp.user_id, stamp.thread_id, stamp.window_id) == (1, 100, "@0")


@pytest.mark.asyncio
async def test_send_bundle_empty_text_never_delivers():
    """An empty bundle returns early — no delivery, no stamp request."""
    route = (1, 100, "@0")
    bundle = inbound_aggregator._PendingBundle()  # no text/attachments → empty
    result = await inbound_aggregator._send_bundle(route, bundle)
    assert result.ok
    assert message_queue.peek_route_user_turn_at(1, 100, "@0") is None


# ── Refusal in-topic surfacing (P1 quarantine + GH #50 §1.4 generalization) ──
#
# The debounced flush is fire-and-forget (the Telegram handler returned long
# ago), so ANY refused delivery must surface an in-topic error via the
# bundle-captured bot — silence would make the user believe the message reached
# Claude while it was dropped at the send seam. GH #50 §1.4 generalized the
# hardcoded QUARANTINE_SEND_REFUSED_MSG equality-match to carry the ACTUAL
# reason, so a live-prompt / not-Claude / lone-digit refusal surfaces too.


class TestQuarantineRefusalSurfacing:
    @pytest.mark.asyncio
    async def test_quarantine_refusal_surfaces_in_topic_error(self, monkeypatch):
        from cctelegram import session as session_mod

        route = (1, 42, "@1")
        bot = AsyncMock()
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "deliver_to_window",
            AsyncMock(
                return_value=delivery.refuse(
                    delivery.REASON_QUARANTINED,
                    written=False,
                    message=session_mod.QUARANTINE_SEND_REFUSED_MSG,
                )
            ),
        )
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "get_group_chat_id",
            lambda user_id, thread_id: -100500,
        )

        await inbound_aggregator.aggregator_offer_text(route, "hello", bot=bot)
        result = await inbound_aggregator.aggregator_flush_route(route)

        assert result.refused
        bot.send_message.assert_awaited()
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == -100500
        assert kwargs.get("message_thread_id") == 42
        assert "delivered" in kwargs["text"].lower()

    @pytest.mark.asyncio
    async def test_prompt_present_refusal_surfaces_actionable_copy(self, monkeypatch):
        """GH #50 §1.4: the notice is no longer hardcoded to the quarantine
        string — a live-prompt refusal surfaces its OWN actionable copy."""
        route = (1, 42, "@1")
        bot = AsyncMock()
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "deliver_to_window",
            AsyncMock(
                return_value=delivery.refuse(
                    delivery.REASON_PROMPT_PRESENT, written=False
                )
            ),
        )
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "get_group_chat_id",
            lambda user_id, thread_id: -100500,
        )

        await inbound_aggregator.aggregator_offer_text(route, "hello", bot=bot)
        result = await inbound_aggregator.aggregator_flush_route(route)

        assert result.refused
        assert result.reason == delivery.REASON_PROMPT_PRESENT
        text = bot.send_message.await_args.kwargs["text"]
        assert "Answer the card first" in text

    @pytest.mark.asyncio
    async def test_window_gone_failure_also_surfaces(self, monkeypatch):
        # GH #50 §1.4: a refused payload is DROPPED — so EVERY refusal (not
        # just the quarantine) now discloses in-topic rather than log-only.
        route = (1, 42, "@1")
        bot = AsyncMock()
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "deliver_to_window",
            AsyncMock(
                return_value=delivery.refuse(delivery.REASON_WINDOW_GONE, written=False)
            ),
        )
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "get_group_chat_id",
            lambda user_id, thread_id: -100500,
        )

        await inbound_aggregator.aggregator_offer_text(route, "hello", bot=bot)
        result = await inbound_aggregator.aggregator_flush_route(route)

        assert result.refused
        bot.send_message.assert_awaited()

    @pytest.mark.asyncio
    async def test_refusal_without_captured_bot_degrades_to_log_only(self, monkeypatch):
        from cctelegram import session as session_mod

        route = (1, 42, "@1")
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "deliver_to_window",
            AsyncMock(
                return_value=delivery.refuse(
                    delivery.REASON_QUARANTINED,
                    written=False,
                    message=session_mod.QUARANTINE_SEND_REFUSED_MSG,
                )
            ),
        )

        await inbound_aggregator.aggregator_offer_text(route, "hello")
        result = await inbound_aggregator.aggregator_flush_route(route)

        assert result.refused  # no bot captured → refusal logged, never a crash

    @pytest.mark.asyncio
    async def test_media_group_boundary_second_bundle_keeps_notice(self, monkeypatch):
        # r2 P2: the media-group boundary force-flush pops the old bundle and
        # creates a FRESH one — which must re-capture the bot, or the second
        # bundle's quarantine refusal silently degrades to log-only.
        from cctelegram import session as session_mod

        route = (1, 42, "@1")
        bot = AsyncMock()
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "deliver_to_window",
            AsyncMock(
                return_value=delivery.refuse(
                    delivery.REASON_QUARANTINED,
                    written=False,
                    message=session_mod.QUARANTINE_SEND_REFUSED_MSG,
                )
            ),
        )
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "get_group_chat_id",
            lambda user_id, thread_id: -100500,
        )

        await inbound_aggregator.aggregator_offer_photo(
            route, Path("/tmp/a.jpg"), "cap1", "group-1", bot=bot
        )
        # A DIFFERENT media-group inside the debounce window force-flushes the
        # first bundle (backgrounded) and starts a FRESH bundle.
        await inbound_aggregator.aggregator_offer_photo(
            route, Path("/tmp/b.jpg"), "cap2", "group-2", bot=bot
        )
        result = await inbound_aggregator.aggregator_flush_route(route)
        # Drain the backgrounded first-bundle flush.
        for task in list(inbound_aggregator._background_tasks):
            await task

        assert result.refused
        # BOTH refused bundles surfaced a notice — the fresh post-boundary
        # bundle did not lose the captured bot.
        assert bot.send_message.await_count == 2


# ── Refusal OWNERSHIP: one refusal ⇒ exactly one ❌ (peer-review P2) ──────
#
# A buffered message + an immediate forwarded slash command while Claude is
# BLOCKED: the synchronous forced flush refuses, the aggregator posted
# "❌ {reason}" AND the command handler — which inspects the returned
# DeliveryResult and aborts (the r2 F2(i) caller-abort chain) — posted its own.
# The user got TWO ❌ messages for ONE event. Ownership is now explicit
# (``report_refusal``), and no path may drop a refusal silently.


class TestRefusalOwnership:
    @pytest.mark.asyncio
    async def test_a_fire_and_forget_flush_reports_the_refusal_itself(
        self, monkeypatch
    ):
        """The debounce timer / media-group boundary / attachment-cap flushes are
        fire-and-forget — nobody awaits the result, and the photo/document handlers
        already acked "sent" — so the AGGREGATOR must disclose."""
        route = (1, 42, "@1")
        bot = AsyncMock()
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "deliver_to_window",
            AsyncMock(
                return_value=delivery.refuse(
                    delivery.REASON_PROMPT_PRESENT, written=False
                )
            ),
        )
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "get_group_chat_id",
            lambda user_id, thread_id: -100500,
        )

        await inbound_aggregator.aggregator_offer_text(route, "hello", bot=bot)
        result = await inbound_aggregator._flush(route)  # the fire-and-forget seam

        assert result.refused
        assert bot.send_message.await_count == 1  # …reported exactly once, here.

    @pytest.mark.asyncio
    async def test_a_caller_owned_flush_never_posts_a_second_notice(self, monkeypatch):
        """A SYNCHRONOUS caller that inspects the result and posts its own ❌ takes
        ownership — the aggregator stays silent (never two messages for one event),
        and the caller still receives the REAL reason."""
        route = (1, 42, "@1")
        bot = AsyncMock()
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "deliver_to_window",
            AsyncMock(
                return_value=delivery.refuse(
                    delivery.REASON_STRANDED_DRAFT, written=False
                )
            ),
        )
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "get_group_chat_id",
            lambda user_id, thread_id: -100500,
        )

        await inbound_aggregator.aggregator_offer_text(route, "hello", bot=bot)
        result = await inbound_aggregator.aggregator_flush_route(
            route, report_refusal=False
        )

        assert result.refused
        assert result.reason == delivery.REASON_STRANDED_DRAFT  # the caller has it…
        bot.send_message.assert_not_awaited()  # …and the aggregator stayed silent.

    # ── peer-review P2: a RAISED delivery is a refusal too ────────────────
    #
    # THE INVARIANT: every refusal — from a RETURNED ``DeliveryResult`` OR from a
    # RAISED exception — reaches the user EXACTLY ONCE, on every flush path.

    @pytest.mark.asyncio
    async def test_a_fire_and_forget_flush_reports_a_RAISED_delivery(self, monkeypatch):
        """THE BUG (peer-review P2) — the exact OPPOSITE failure of the
        double-report above, introduced by the same ``report_refusal`` fold. The
        exception arm built its ``DeliveryResult`` and RETURNED it immediately,
        jumping over the reporting block: the debounced / media-group-boundary /
        attachment-cap flushes are fire-and-forget, so NOBODY awaits that result —
        the popped payload vanished with only a log line and the user was never
        told. It matters doubly now, because a raise PAST a write attempt also
        arms the stranded-draft brake, so the user must be told why their NEXT
        message will be refused too."""
        route = (1, 42, "@1")
        bot = AsyncMock()
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "deliver_to_window",
            AsyncMock(side_effect=RuntimeError("tmux exploded mid-write")),
        )
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "get_group_chat_id",
            lambda user_id, thread_id: -100500,
        )

        await inbound_aggregator.aggregator_offer_text(route, "hello", bot=bot)
        result = await inbound_aggregator._flush(route)  # the fire-and-forget seam

        assert result.refused
        assert result.reason == delivery.REASON_SEND_FAILED
        assert bot.send_message.await_count == 1  # …reported. Exactly once.
        # The payload really is gone — the notice is the user's ONLY signal.
        assert not inbound_aggregator.has_pending(route)
        # And the copy tells them to check the box (the brake may now be up).
        sent = str(bot.send_message.await_args)
        assert "input box" in sent

    @pytest.mark.asyncio
    async def test_a_caller_owned_flush_stays_silent_when_the_delivery_RAISES(
        self, monkeypatch
    ):
        """The raise path honours OWNERSHIP too: a synchronous forced flush hands
        the structured result to the caller, which posts the single ❌ itself."""
        route = (1, 42, "@1")
        bot = AsyncMock()
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "deliver_to_window",
            AsyncMock(side_effect=RuntimeError("boom")),
        )
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "get_group_chat_id",
            lambda user_id, thread_id: -100500,
        )

        await inbound_aggregator.aggregator_offer_text(route, "hello", bot=bot)
        result = await inbound_aggregator.aggregator_flush_route(
            route, report_refusal=False
        )

        assert result.refused
        assert result.reason == delivery.REASON_SEND_FAILED  # the caller has it…
        bot.send_message.assert_not_awaited()  # …and the aggregator stayed silent.

    @pytest.mark.asyncio
    async def test_a_CANCELLATION_propagates_and_is_never_reported_as_a_refusal(
        self, monkeypatch
    ):
        """``CancelledError`` is a ``BaseException`` — ``except Exception`` does not
        catch it, and it must stay that way: a cancellation is not a refusal, must
        never be swallowed into a ``DeliveryResult``, and must never be posted to
        the topic. (``session.deliver_to_window`` has already armed the brake for
        it if a write was attempted, and re-raised.)"""
        route = (1, 42, "@1")
        bot = AsyncMock()
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "deliver_to_window",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
        monkeypatch.setattr(
            inbound_aggregator.session_manager,
            "get_group_chat_id",
            lambda user_id, thread_id: -100500,
        )

        await inbound_aggregator.aggregator_offer_text(route, "hello", bot=bot)
        with pytest.raises(asyncio.CancelledError):
            await inbound_aggregator._flush(route)

        bot.send_message.assert_not_awaited()


def test_every_synchronous_forced_flush_caller_claims_ownership() -> None:
    """The three forced-flush callers (which abort + post their own ❌) and the
    pending-bind replay (whose callers surface the reason in their bind edit) all
    pass ``report_refusal=False``. A refusal still reaches the user EXACTLY once —
    the aggregator reports only where nobody else will."""
    import inspect

    from cctelegram import bot as bot_mod
    from cctelegram.callback_dispatcher import effort, late_answer

    for owner in (
        bot_mod.forward_command_handler,
        effort.execute_effort_callback,
        late_answer.execute_late_answer_callback,
    ):
        assert "aggregator_flush_route(route, report_refusal=False)" in (
            inspect.getsource(owner)
        )
    assert "report_refusal=False" in inspect.getsource(
        inbound_aggregator.aggregator_replay_payload
    )
