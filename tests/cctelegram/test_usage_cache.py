"""Unit tests for ``handlers/usage_cache`` — the /cost overlay result cache.

A tiny in-memory leaf: the overlay SUCCESS path writes the rendered snippet
keyed by ROUTE + the window's CURRENT session identity; the busy-path fallback
reads it back (30-min TTL, session-identity match). Torn down at the same
route-scoped seams as ``pane_signals``. In-memory only — restart wipes it.
"""

from __future__ import annotations

import pytest

from cctelegram.handlers import usage_cache

ROUTE_A = (1, 42, "@1")
ROUTE_B = (1, 43, "@2")


@pytest.fixture(autouse=True)
def _reset_cache():
    usage_cache.reset_for_tests()
    yield
    usage_cache.reset_for_tests()


class TestRecordAndPeek:
    def test_record_then_peek_same_route_and_session_hits(self):
        usage_cache.record(ROUTE_A, "sess-1", "Total cost: $1.23", now=1000.0)
        entry = usage_cache.peek(ROUTE_A, "sess-1", now=1000.0)
        assert entry is not None
        assert entry.text == "Total cost: $1.23"
        assert entry.written_at == 1000.0

    def test_peek_absent_route_is_none(self):
        assert usage_cache.peek(ROUTE_A, "sess-1", now=1000.0) is None

    def test_peek_other_route_is_none(self):
        usage_cache.record(ROUTE_A, "sess-1", "cost A", now=1000.0)
        assert usage_cache.peek(ROUTE_B, "sess-1", now=1000.0) is None

    def test_peek_different_session_identity_is_miss(self):
        # Window ids recycle; a later read whose CURRENT session differs is a
        # miss (never window-only keying).
        usage_cache.record(ROUTE_A, "sess-1", "cost A", now=1000.0)
        assert usage_cache.peek(ROUTE_A, "sess-2", now=1000.0) is None

    def test_none_session_never_matches(self):
        usage_cache.record(ROUTE_A, "sess-1", "cost A", now=1000.0)
        assert usage_cache.peek(ROUTE_A, None, now=1000.0) is None

    def test_record_with_none_session_is_not_stored(self):
        # No session identity ⇒ nothing to key isolation on ⇒ do not store.
        usage_cache.record(ROUTE_A, None, "cost A", now=1000.0)
        assert usage_cache.peek(ROUTE_A, "sess-1", now=1000.0) is None

    def test_record_overwrites_previous_for_same_key(self):
        usage_cache.record(ROUTE_A, "sess-1", "old", now=1000.0)
        usage_cache.record(ROUTE_A, "sess-1", "new", now=1100.0)
        entry = usage_cache.peek(ROUTE_A, "sess-1", now=1100.0)
        assert entry is not None
        assert entry.text == "new"
        assert entry.written_at == 1100.0


class TestTTL:
    def test_29_min_old_entry_is_present(self):
        usage_cache.record(ROUTE_A, "sess-1", "cost", now=0.0)
        entry = usage_cache.peek(ROUTE_A, "sess-1", now=29 * 60.0)
        assert entry is not None

    def test_31_min_old_entry_is_absent(self):
        usage_cache.record(ROUTE_A, "sess-1", "cost", now=0.0)
        assert usage_cache.peek(ROUTE_A, "sess-1", now=31 * 60.0) is None

    def test_exactly_30_min_is_present(self):
        usage_cache.record(ROUTE_A, "sess-1", "cost", now=0.0)
        assert usage_cache.peek(ROUTE_A, "sess-1", now=30 * 60.0) is not None


class TestTeardown:
    def test_clear_route_removes_only_that_route(self):
        usage_cache.record(ROUTE_A, "sess-1", "A", now=0.0)
        usage_cache.record(ROUTE_B, "sess-1", "B", now=0.0)
        usage_cache.clear_route(ROUTE_A)
        assert usage_cache.peek(ROUTE_A, "sess-1", now=0.0) is None
        assert usage_cache.peek(ROUTE_B, "sess-1", now=0.0) is not None

    def test_clear_route_on_absent_route_is_noop(self):
        usage_cache.clear_route(ROUTE_A)  # must not raise

    def test_clear_routes_for_topic_removes_all_routes_in_thread(self):
        r1 = (1, 42, "@1")
        r2 = (1, 42, "@2")  # same (user, thread), different window
        other = (1, 99, "@3")
        usage_cache.record(r1, "s", "1", now=0.0)
        usage_cache.record(r2, "s", "2", now=0.0)
        usage_cache.record(other, "s", "3", now=0.0)
        usage_cache.clear_routes_for_topic(1, 42)
        assert usage_cache.peek(r1, "s", now=0.0) is None
        assert usage_cache.peek(r2, "s", now=0.0) is None
        assert usage_cache.peek(other, "s", now=0.0) is not None

    def test_reset_for_tests_clears_everything(self):
        usage_cache.record(ROUTE_A, "sess-1", "A", now=0.0)
        usage_cache.reset_for_tests()
        assert usage_cache.peek(ROUTE_A, "sess-1", now=0.0) is None


class TestTeardownSeamWiring:
    """The cache is torn down at the same route-scoped seams as pane_signals.

    Each test drives the REAL seam function (review r1 P2 — never
    usage_cache directly): topic teardown via cleanup.clear_topic_state and
    the monitor rotation sweep via _detect_and_cleanup_changes here; the bot
    /clear branch + the inbound stale-window unbind are scenario tests
    (tests/scenarios/test_usage_cache_teardown.py).
    """

    @pytest.mark.asyncio
    async def test_clear_topic_state_tears_down_cache(self):
        # cleanup.clear_topic_state is the covering topic-teardown seam.
        from cctelegram.handlers import cleanup

        route = (7, 314, "@9")
        usage_cache.record(route, "sess-x", "cost", now=None)
        assert usage_cache.peek(route, "sess-x") is not None
        await cleanup.clear_topic_state(7, 314)
        assert usage_cache.peek(route, "sess-x") is None

    @pytest.mark.asyncio
    async def test_monitor_rotation_sweep_tears_down_cache(self, tmp_path):
        # The session-rotation sweep (_detect_and_cleanup_changes) clears the
        # rotated window's route-scoped caches — usage_cache beside
        # pane_signals/decision_token (the test_session_monitor flip pattern).
        from cctelegram.session import session_manager
        from cctelegram.session_monitor import SessionMonitor

        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        route = (7, 314, "@11")
        usage_cache.record(route, "sess-old", "cost", now=None)
        assert usage_cache.peek(route, "sess-old") is not None

        monitor._last_session_map = {"@11": "session-old"}

        async def fake_load_current_map():
            return {"@11": "session-new"}

        monitor._load_current_session_map = fake_load_current_map  # type: ignore[method-assign]
        saved_bindings = dict(session_manager.thread_bindings)
        session_manager.thread_bindings[7] = {314: "@11"}
        try:
            await monitor._detect_and_cleanup_changes()
        finally:
            session_manager.thread_bindings.clear()
            session_manager.thread_bindings.update(saved_bindings)

        assert usage_cache.peek(route, "sess-old") is None
