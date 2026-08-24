"""AUQ details-after-picker regression — the poller's first-publish ctx-settle gate.

The ~1 Hz status poller detects a fresh AskUserQuestion on its FIRST transient
frame, where ``resolve_auq_source_for_render`` can bail (``ctx_source is None``)
because the pane / PreToolUse side file hasn't settled. Publishing the picker on
that first bail tick strands the 📋 details card to a LATER tick (the user sees
the picker first, details after). The poller now DEFERS the first picker publish
until ctx resolves — bounded by ``AUQ_FIRST_PUBLISH_CTX_SETTLE_TICKS`` and
fail-open after the cap. Because ctx + picker are emitted by the SAME
``handle_interactive_ui`` call, once ctx resolves the first publish emits both
in order (details, then picker).

The deferral state is a PLAIN route-level consecutive-defer counter (``route ->
int``): every defer tick increments it and no per-tick pane signal (ANSI,
parse-state flap, render-hash) can reset it, so it is bounded against ALL churn by
construction and always reaches the cap. Fresh-per-card is guaranteed by
publish/resolve CLEARING the counter — the first-publish gate fires ONCE PER CARD
(a multi-question AUQ navigates Q1→Q2 inside the one published card via the
refresh branch, never a second first-publish).

These are poller-control-flow tests: ``handle_interactive_ui`` is mocked to drive
the ``FirstPublishCtxGate`` directly (defer / publish), so the tests assert the
poller's deferral bookkeeping, not the card render itself.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram import route_runtime
from cctelegram.handlers import status_polling
from cctelegram.handlers.interactive_ui import FirstPublishCtxGate
from cctelegram.handlers.status_polling import update_status_message


@pytest.fixture(autouse=True)
def _reset():
    """Clean poller-local route caches + route_runtime around each test."""
    status_polling._last_pane_capture.clear()
    status_polling._last_published_ui_hash.clear()
    status_polling._auq_first_publish_defer.clear()
    status_polling._absent_streak.clear()
    status_polling._prev_run_state.clear()
    route_runtime.reset_for_tests()
    yield
    status_polling._last_pane_capture.clear()
    status_polling._last_published_ui_hash.clear()
    status_polling._auq_first_publish_defer.clear()
    status_polling._absent_streak.clear()
    status_polling._prev_run_state.clear()
    route_runtime.reset_for_tests()


@pytest.fixture
def mock_bot() -> AsyncMock:
    return AsyncMock()


_WID = "@5"
_ROUTE = (1, 42, "@5")


@contextlib.contextmanager
def _poller_tick(
    handle_side_effect,
    *,
    ui_name: str = "AskUserQuestion",
    render_hash: str = "auq-hash",
    ui_present: bool = True,
):
    """Patch context for one AUQ first-publish poller tick; yields the mocked
    ``handle_interactive_ui``.

    - ``render_hash`` is the ``_ui_render_hash`` stored in ``_last_published_ui_hash``
      on publish; vary it across ticks to simulate spinner/ANSI noise — the plain
      defer counter does not consult it, so it must NOT affect the count.
    - ``ui_present=False`` simulates a NONE FRAME: ``extract_interactive_content``
      returns None (the AUQ is not visible this tick), so the first-publish branch
      is skipped. Under the monotonic invariant a None frame must leave the count
      untouched. The status path is stubbed to a no-op so the tick is inert.
    """
    mock_window = MagicMock()
    mock_window.window_id = _WID
    mock_tmux = MagicMock()
    mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
    # capture_pane_pair only calls tmux.capture_pane; any non-empty text works
    # because extract_interactive_content is patched to yield the stand-in.
    mock_tmux.capture_pane = AsyncMock(return_value="pane text\n")
    ui_value = SimpleNamespace(name=ui_name, content="Q?") if ui_present else None
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(status_polling, "tmux_manager", mock_tmux))
        stack.enter_context(
            patch.object(
                status_polling,
                "extract_interactive_content",
                return_value=ui_value,
            )
        )
        stack.enter_context(
            patch.object(status_polling, "_ui_render_hash", return_value=render_hash)
        )
        # Keep the drain barrier a no-op (no real content queue in the test).
        stack.enter_context(
            patch.object(status_polling, "get_content_queue", return_value=None)
        )
        mock_handle = stack.enter_context(
            patch.object(
                status_polling,
                "handle_interactive_ui",
                new_callable=AsyncMock,
                side_effect=handle_side_effect,
            )
        )
        if not ui_present:
            # A None frame falls through to the status path; stub it so the tick is
            # inert (the test only cares that the defer count is untouched).
            stack.enter_context(
                patch.object(status_polling, "parse_status_line", return_value=None)
            )
            stack.enter_context(
                patch.object(
                    status_polling, "enqueue_status_update", new_callable=AsyncMock
                )
            )
        yield mock_handle


async def _run(mock_bot):
    await update_status_message(mock_bot, user_id=1, window_id=_WID, thread_id=42)


@pytest.mark.asyncio
async def test_first_publish_defers_when_ctx_unresolved(mock_bot):
    """A first-publish tick whose ctx bails DEFERS: no publish, no hash, count=1."""
    seen: list[FirstPublishCtxGate | None] = []

    async def defer(*_a, first_publish_ctx_gate=None, **_k):
        seen.append(first_publish_ctx_gate)
        if first_publish_ctx_gate is not None:
            first_publish_ctx_gate.deferred = True
        return False

    with (
        _poller_tick(defer) as mock_handle,
        patch.object(
            status_polling.route_runtime,
            "mark_interactive_pending",
            new_callable=AsyncMock,
        ) as mark_pending,
    ):
        await _run(mock_bot)

    # A gate WAS offered (first publish, AUQ, under cap) and it deferred.
    assert len(seen) == 1 and seen[0] is not None and seen[0].deferred is True
    mock_handle.assert_awaited_once()
    # Hash NOT stored → next tick re-enters as a first publish.
    assert _ROUTE not in status_polling._last_published_ui_hash
    # Plain consecutive-defer count == 1.
    assert status_polling._auq_first_publish_defer[_ROUTE] == 1
    # No surface published → never promoted to WAITING this tick.
    mark_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_defer_then_publish_in_order_on_next_tick(mock_bot):
    """(b) Tick 1 defers (ctx unresolved); tick 2 publishes once ctx resolves.

    Because ctx + picker are emitted by the SAME handle_interactive_ui call, the
    publishing tick emits both in order — this asserts the poller state (defer →
    retry → publish) that lets that happen, and that the count is cleared.
    """
    seen: list[FirstPublishCtxGate | None] = []
    state = {"n": 0}

    async def resolve_on_second(*_a, first_publish_ctx_gate=None, **_k):
        state["n"] += 1
        seen.append(first_publish_ctx_gate)
        if state["n"] == 1 and first_publish_ctx_gate is not None:
            first_publish_ctx_gate.deferred = True
            return False
        return True  # ctx resolved: publish (gate present, deferred stays False)

    # Tick 1: defers.
    with _poller_tick(resolve_on_second):
        await _run(mock_bot)
    assert status_polling._auq_first_publish_defer[_ROUTE] == 1
    assert _ROUTE not in status_polling._last_published_ui_hash

    # Tick 2: still a first publish (hash absent), gate offered, publishes.
    with (
        _poller_tick(resolve_on_second),
        patch.object(
            status_polling.route_runtime,
            "mark_interactive_pending",
            new_callable=AsyncMock,
        ) as mark_pending,
    ):
        await _run(mock_bot)
    assert seen[1] is not None and seen[1].deferred is False
    assert status_polling._last_published_ui_hash[_ROUTE] == "auq-hash"
    assert _ROUTE not in status_polling._auq_first_publish_defer
    mark_pending.assert_awaited_once_with(_ROUTE)


@pytest.mark.asyncio
async def test_fail_open_publishes_after_cap(mock_bot):
    """After the settle cap with ctx still unresolved, the picker publishes anyway
    (fail open) — a genuinely ctx-less AUQ is never stranded."""
    cap = status_polling.AUQ_FIRST_PUBLISH_CTX_SETTLE_TICKS
    published_ctxless = {"done": False}

    async def defer_while_gated(*_a, first_publish_ctx_gate=None, **_k):
        if first_publish_ctx_gate is not None:
            first_publish_ctx_gate.deferred = True
            return False
        # Poller withdrew the gate (cap reached): ctx-less fail-open publish.
        published_ctxless["done"] = True
        return True

    # `cap` deferral ticks: count climbs 1..cap, hash never stored.
    for expected in range(1, cap + 1):
        with _poller_tick(defer_while_gated):
            await _run(mock_bot)
        assert status_polling._auq_first_publish_defer[_ROUTE] == expected
        assert _ROUTE not in status_polling._last_published_ui_hash

    # Next tick: count == cap → may_defer False → gate withdrawn →
    # handle_interactive_ui publishes ctx-less.
    with (
        _poller_tick(defer_while_gated),
        patch.object(
            status_polling.route_runtime,
            "mark_interactive_pending",
            new_callable=AsyncMock,
        ) as mark_pending,
    ):
        await _run(mock_bot)
    assert published_ctxless["done"] is True
    assert status_polling._last_published_ui_hash[_ROUTE] == "auq-hash"
    assert _ROUTE not in status_polling._auq_first_publish_defer
    mark_pending.assert_awaited_once_with(_ROUTE)


@pytest.mark.asyncio
async def test_first_publish_immediate_when_ctx_resolved(mock_bot):
    """A first tick that already resolves ctx publishes immediately — the common
    case is unchanged (no deferral)."""
    seen: list[FirstPublishCtxGate | None] = []

    async def publish(*_a, first_publish_ctx_gate=None, **_k):
        seen.append(first_publish_ctx_gate)
        return True  # ctx resolved: publish, leave gate.deferred False

    with (
        _poller_tick(publish),
        patch.object(
            status_polling.route_runtime,
            "mark_interactive_pending",
            new_callable=AsyncMock,
        ) as mark_pending,
    ):
        await _run(mock_bot)
    # A gate was offered but the call published without deferring.
    assert len(seen) == 1 and seen[0] is not None and seen[0].deferred is False
    assert status_polling._last_published_ui_hash[_ROUTE] == "auq-hash"
    assert _ROUTE not in status_polling._auq_first_publish_defer
    mark_pending.assert_awaited_once_with(_ROUTE)


@pytest.mark.asyncio
async def test_already_published_route_never_defers(mock_bot):
    """Once a card exists (route already in the published-hash), later ticks pass
    NO gate — refreshes never defer (regression guard)."""
    seen: list[object] = []

    async def record(*_a, first_publish_ctx_gate=None, **_k):
        seen.append(first_publish_ctx_gate)
        return True

    # Simulate a route whose picker was already published.
    status_polling._last_published_ui_hash[_ROUTE] = "prev-hash"

    with _poller_tick(record):
        await _run(mock_bot)
    # is_first_publish_for_route is False → gate is None → no deferral path.
    assert seen == [None]
    assert _ROUTE not in status_polling._auq_first_publish_defer


@pytest.mark.asyncio
async def test_non_auq_first_publish_never_defers(mock_bot):
    """A non-AUQ interactive surface (Settings) never gets a gate — the deferral
    is AUQ-scoped and EPM/Permission/etc. publish immediately."""
    seen: list[object] = []

    async def record(*_a, first_publish_ctx_gate=None, **_k):
        seen.append(first_publish_ctx_gate)
        return True

    with _poller_tick(record, ui_name="Settings"):
        await _run(mock_bot)
    assert seen == [None]
    assert _ROUTE not in status_polling._auq_first_publish_defer
    assert status_polling._last_published_ui_hash[_ROUTE] == "auq-hash"


@pytest.mark.asyncio
async def test_defer_state_cleared_after_publish_stops_nudge(mock_bot):
    """F2: a published route clears the defer state, and the capture nudge cannot
    keep forcing 1 Hz pane scrapes after the card exists."""
    state = {"n": 0}

    async def defer_then_publish(*_a, first_publish_ctx_gate=None, **_k):
        state["n"] += 1
        if state["n"] == 1 and first_publish_ctx_gate is not None:
            first_publish_ctx_gate.deferred = True
            return False
        return True  # publish on the second call

    # Tick A: defer → state present (drives the capture nudge).
    with _poller_tick(defer_then_publish):
        await _run(mock_bot)
    assert _ROUTE in status_polling._auq_first_publish_defer

    # Tick B: publish → state cleared (not re-added on the published path).
    with _poller_tick(defer_then_publish):
        await _run(mock_bot)
    assert _ROUTE not in status_polling._auq_first_publish_defer

    # Tick C: no defer state, not in interactive mode, and the last capture is
    # recent (tick B just captured), so ``should_capture`` is False → the poller
    # SKIPS the pane scrape and never calls handle_interactive_ui. Proves the
    # nudge does not keep capturing after the card exists.
    with (
        _poller_tick(defer_then_publish) as mock_handle,
        patch.object(
            status_polling, "_process_idle_clear_only", new_callable=AsyncMock
        ),
    ):
        await _run(mock_bot)
    mock_handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_count_climbs_monotonically_despite_ansi_and_parse_flap(mock_bot):
    """(a) The plain counter is bounded against ALL churn.

    ``_ui_render_hash`` (ANSI) AND the parsed form (``resolve_ask_form``) both flap
    every ctx-less tick. The route-level counter consults NEITHER, so it climbs
    monotonically to the cap and fails open — the exact case the earlier
    identity-keyed versions (r2/r4/r5) suppressed indefinitely. This test doubles
    as a regression guard: any identity-keyed counter would reset here and never
    publish, failing the assertions below.
    """
    cap = status_polling.AUQ_FIRST_PUBLISH_CTX_SETTLE_TICKS
    published = {"done": False}

    async def defer_while_gated(*_a, first_publish_ctx_gate=None, **_k):
        if first_publish_ctx_gate is not None:
            first_publish_ctx_gate.deferred = True
            return False
        published["done"] = True  # gate withdrawn (cap reached) → fail-open publish
        return True

    counts: list[int] = []
    published_tick = None
    for i in range(1, cap + 3):  # generous headroom; must publish by cap + 1
        # Parse-state flaps form↔None (would alternate fingerprint↔sentinel under
        # any identity key); ANSI/render-hash flaps every tick too.
        flap_form = SimpleNamespace(fingerprint=lambda: "x") if i % 2 else None
        with (
            _poller_tick(defer_while_gated, render_hash=f"ansi-{i}"),
            patch.object(status_polling, "resolve_ask_form", return_value=flap_form),
        ):
            await _run(mock_bot)
        if published["done"]:
            published_tick = i
            break
        counts.append(status_polling._auq_first_publish_defer[_ROUTE])

    # Count climbed 1..cap monotonically despite the flap, then published.
    assert counts == list(range(1, cap + 1))
    assert published["done"] is True
    assert published_tick == cap + 1
    assert _ROUTE not in status_polling._auq_first_publish_defer


@pytest.mark.asyncio
async def test_count_persists_across_none_frames_and_climbs_to_cap(mock_bot):
    """THE monotonic-invariant regression guard (the r5-failing case).

    ``AUQ → None → AUQ → None …`` every tick with ctx bailing. A NONE frame (the
    AUQ is not visible / ``ui_content is None``) must NOT reset the count — it
    persists and the count climbs monotonically across the AUQ frames to the cap,
    then publishes (fail open). The r5 regenerate-each-tick pop zeroed the count on
    every None frame → indefinite suppression; the monotonic READ-don't-pop fix
    makes this bounded.
    """
    cap = status_polling.AUQ_FIRST_PUBLISH_CTX_SETTLE_TICKS
    published = {"done": False}

    async def defer_while_gated(*_a, first_publish_ctx_gate=None, **_k):
        if first_publish_ctx_gate is not None:
            first_publish_ctx_gate.deferred = True
            return False
        published["done"] = True  # gate withdrawn (cap reached) → fail-open publish
        return True

    auq_frames = 0
    published_at_auq_frame = None
    # Interleave AUQ (odd ticks) and None (even ticks) frames.
    for i in range(1, cap * 2 + 4):
        ui_present = i % 2 == 1
        with _poller_tick(defer_while_gated, ui_present=ui_present):
            await _run(mock_bot)
        if published["done"]:
            published_at_auq_frame = auq_frames + 1
            break
        if ui_present:
            auq_frames += 1
            # Count == number of AUQ frames so far — climbs monotonically.
            assert status_polling._auq_first_publish_defer[_ROUTE] == auq_frames
        else:
            # A None frame did NOT reset (nor touch) the count.
            assert status_polling._auq_first_publish_defer.get(_ROUTE, 0) == auq_frames

    # It published (fail open) despite the None frames — NEVER suppressed — and did
    # so on the (cap+1)-th AUQ frame (cap deferrals then the fail-open publish).
    assert published["done"] is True
    assert published_at_auq_frame == cap + 1
    assert _ROUTE not in status_polling._auq_first_publish_defer


@pytest.mark.asyncio
async def test_leaked_count_fails_open_early_never_suppressed(mock_bot):
    """A leaked/stale count (a prior AUQ vanished without firing a clear) is
    SELF-LIMITING: the next card finds a high count and fails open EARLY — a
    bounded, one-card ordering imperfection, the OPPOSITE of suppression. Encodes
    the 'bounded, never suppress' property as a test, not just a comment."""
    cap = status_polling.AUQ_FIRST_PUBLISH_CTX_SETTLE_TICKS
    published = {"done": False}
    seen: list[object] = []

    async def defer_or_publish(*_a, first_publish_ctx_gate=None, **_k):
        seen.append(first_publish_ctx_gate)
        if first_publish_ctx_gate is not None:
            first_publish_ctx_gate.deferred = True
            return False
        published["done"] = True
        return True

    # Seed a leaked count AT the cap (as if a prior AUQ deferred to cap then
    # vanished with no clear). The counter is never lowered by a pane read, so the
    # next first-publish AUQ sees count >= cap.
    status_polling._auq_first_publish_defer[_ROUTE] = cap

    with _poller_tick(defer_or_publish):
        await _run(mock_bot)

    # count >= cap → may_defer False → NO gate → immediate fail-open publish
    # (early, bounded), NOT suppressed; and the publish clears the leaked count.
    assert seen == [None]
    assert published["done"] is True
    assert _ROUTE not in status_polling._auq_first_publish_defer


@pytest.mark.asyncio
async def test_second_auq_after_resolution_gets_fresh_window(mock_bot):
    """(c) A second AUQ on the same route after the first resolved gets a FRESH
    defer window (count starts at 0), because publish/resolve clears the counter —
    no identity key needed."""
    state = {"n": 0}

    async def defer_then_publish(*_a, first_publish_ctx_gate=None, **_k):
        state["n"] += 1
        if state["n"] == 1 and first_publish_ctx_gate is not None:
            first_publish_ctx_gate.deferred = True
            return False
        return True  # publish on the second call

    # First AUQ: defer (count 1) then publish (count cleared, hash stored).
    with _poller_tick(defer_then_publish):
        await _run(mock_bot)
    assert status_polling._auq_first_publish_defer[_ROUTE] == 1
    with _poller_tick(defer_then_publish):
        await _run(mock_bot)
    assert _ROUTE not in status_polling._auq_first_publish_defer

    # The first card resolves — resolution clears the published-hash (modeled here;
    # in production _on_interactive_clear / the clear paths pop it), so the next
    # AUQ is a fresh first publish. Also drop the watchdog capture stamp so the
    # poller scrapes the pane on the next tick (models the WATCHDOG_INTERVAL tick on
    # which the new card is first detected).
    status_polling._last_published_ui_hash.pop(_ROUTE, None)
    status_polling._last_pane_capture.pop(_ROUTE, None)

    # Second AUQ (new card): defers with a FRESH window — count starts at 1, never
    # inheriting anything from the first card.
    async def defer(*_a, first_publish_ctx_gate=None, **_k):
        if first_publish_ctx_gate is not None:
            first_publish_ctx_gate.deferred = True
        return False

    with _poller_tick(defer):
        await _run(mock_bot)
    assert status_polling._auq_first_publish_defer[_ROUTE] == 1
