"""Unit tests for the AUQ action ledger.

Covers:
  - State-transition correctness across all 5 persisted states.
  - First-write field-required validation.
  - Latest-line-wins reload semantics.
  - Corrupt-line tolerance (skip with warning, keep parsing).
  - Duplicate-state-line idempotency.
  - LRU compaction at startup.
  - Pure ``lookup()`` — does NOT classify owner / collision (that lives
    in the callback handler per the v4 §7.2 contract).
  - Injectable clock + path so we never touch real disk or wall-time.

The ledger is module-level singleton state; every test calls
``reset_for_tests(path=..., now=..., start_time=...)`` in a fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cctelegram.handlers import auq_ledger


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "auq_action_ledger.jsonl"


@pytest.fixture
def clock():
    """Mutable wall-clock — tests advance it explicitly."""

    class _Clock:
        t: float = 1000.0

        def __call__(self) -> float:
            return self.t

        def tick(self, delta: float = 1.0) -> None:
            self.t += delta

    return _Clock()


@pytest.fixture
def setup_ledger(ledger_path: Path, clock):
    """Wire the module to the tmp path + injected clock; reset at end."""
    auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
    yield
    auq_ledger.reset_for_tests()


def _first_write_kwargs(**overrides):
    base = dict(
        state="accepted",
        user_id=42,
        window_id="@7",
        full_fingerprint="ff" * 20,
        option_number=2,
        option_label="alpha",
    )
    base.update(overrides)
    return base


class TestRecordFirstWrite:
    def test_first_record_succeeds_and_returns_entry(self, setup_ledger):
        entry = auq_ledger.record("rh:fp:2", **_first_write_kwargs())
        assert entry.key == "rh:fp:2"
        assert entry.state == "accepted"
        assert entry.user_id == 42
        assert entry.window_id == "@7"
        assert entry.option_number == 2
        assert entry.option_label == "alpha"
        assert entry.accepted_at == 1000.0
        assert entry.digit_sent_at is None
        assert entry.dispatched_at is None
        assert entry.failed_reason is None

    def test_first_record_requires_identity_fields(self, setup_ledger):
        with pytest.raises(ValueError, match="First record"):
            auq_ledger.record("rh:fp:2", state="accepted", user_id=42)

    def test_invalid_state_raises(self, setup_ledger):
        with pytest.raises(ValueError, match="Invalid ledger state"):
            auq_ledger.record(
                "rh:fp:2",
                state="bogus",  # type: ignore[arg-type]
                user_id=1,
                window_id="@1",
                full_fingerprint="ff",
                option_number=1,
                option_label="x",
            )

    def test_first_write_can_directly_set_digit_sent(self, setup_ledger):
        # Edge: caller can skip 'accepted' on the very first write. This is
        # a defensive contract — handler always writes 'accepted' first in
        # practice, but the module must not corrupt state if it doesn't.
        entry = auq_ledger.record("rh:fp:2", **_first_write_kwargs(state="digit_sent"))
        assert entry.state == "digit_sent"
        assert entry.accepted_at == 1000.0
        assert entry.digit_sent_at == 1000.0


class TestStateTransitions:
    def test_accepted_then_digit_sent_then_dispatched(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(0.5)
        e2 = auq_ledger.record("k:1", state="digit_sent")
        clock.tick(0.5)
        e3 = auq_ledger.record("k:1", state="dispatched")

        assert e2.state == "digit_sent"
        assert e2.accepted_at == 1000.0  # preserved from first write
        assert e2.digit_sent_at == 1000.5
        assert e2.dispatched_at is None

        assert e3.state == "dispatched"
        assert e3.accepted_at == 1000.0
        assert e3.digit_sent_at == 1000.5
        assert e3.dispatched_at == 1001.0

    def test_failed_before_digit_terminal(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(0.1)
        entry = auq_ledger.record(
            "k:1", state="failed_before_digit", failed_reason="tmux gone"
        )
        assert entry.state == "failed_before_digit"
        assert entry.digit_sent_at is None
        assert entry.dispatched_at is None
        assert entry.failed_reason == "tmux gone"

    def test_failed_after_digit_preserves_digit_sent_at(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(0.1)
        auq_ledger.record("k:1", state="digit_sent")
        clock.tick(0.4)
        entry = auq_ledger.record(
            "k:1", state="failed_after_digit", failed_reason="enter raised"
        )
        assert entry.state == "failed_after_digit"
        assert entry.digit_sent_at == 1000.1
        assert entry.dispatched_at is None
        assert entry.failed_reason == "enter raised"


class TestLookupAndReload:
    def test_lookup_none_returns_none(self, setup_ledger):
        assert auq_ledger.lookup(None) is None
        assert auq_ledger.lookup("not-here") is None

    def test_lookup_returns_latest_entry_per_key(self, setup_ledger, clock):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(0.5)
        auq_ledger.record("k:1", state="digit_sent")
        clock.tick(0.5)
        auq_ledger.record("k:1", state="dispatched")
        latest = auq_ledger.lookup("k:1")
        assert latest is not None
        assert latest.state == "dispatched"

    def test_reload_picks_up_latest_line_wins(self, setup_ledger, clock, ledger_path):
        auq_ledger.record("k:1", **_first_write_kwargs())
        clock.tick(0.5)
        auq_ledger.record("k:1", state="digit_sent")
        clock.tick(0.5)
        auq_ledger.record("k:1", state="dispatched")

        # Simulate process restart by clearing in-memory + reloading from
        # the same path (different clock — restart preserves disk state).
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        loaded = auq_ledger.lookup("k:1")
        assert loaded is not None
        assert loaded.state == "dispatched"
        assert loaded.accepted_at == 1000.0
        assert loaded.digit_sent_at == 1000.5
        assert loaded.dispatched_at == 1001.0

    def test_duplicate_terminal_state_is_idempotent_on_reload(
        self, setup_ledger, clock, ledger_path
    ):
        # Same key, same terminal state written twice. After reload only
        # the latest line wins — shape is preserved.
        auq_ledger.record("k:1", **_first_write_kwargs(state="dispatched"))
        clock.tick(0.5)
        auq_ledger.record("k:1", state="dispatched")
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        loaded = auq_ledger.lookup("k:1")
        assert loaded is not None
        assert loaded.state == "dispatched"


class TestCorruptLineTolerance:
    def test_skips_garbage_lines_in_middle(
        self, setup_ledger, clock, ledger_path, caplog
    ):
        auq_ledger.record("k:1", **_first_write_kwargs())
        # Manually append a corrupt line + a valid second-key line.
        with open(ledger_path, "a", encoding="utf-8") as f:
            f.write("this is not json at all\n")
            f.write(
                '{"key": "k:2", "state": "dispatched", "user_id": 99}\n'
            )  # incomplete
            f.write(
                json.dumps(
                    {
                        "key": "k:3",
                        "state": "accepted",
                        "user_id": 7,
                        "window_id": "@9",
                        "full_fingerprint": "ee" * 20,
                        "option_number": 1,
                        "option_label": "good",
                        "accepted_at": 1234.0,
                        "digit_sent_at": None,
                        "dispatched_at": None,
                        "failed_reason": None,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        # Original k:1 survives; corrupt k:2 line is skipped; k:3 loads.
        assert auq_ledger.lookup("k:1") is not None
        assert auq_ledger.lookup("k:2") is None
        loaded = auq_ledger.lookup("k:3")
        assert loaded is not None
        assert loaded.option_label == "good"
        assert any("corrupt line" in rec.message for rec in caplog.records)

    def test_skips_unknown_state(self, setup_ledger, clock, ledger_path, caplog):
        with open(ledger_path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "key": "k:1",
                        "state": "made_up_state",
                        "user_id": 1,
                        "window_id": "@1",
                        "full_fingerprint": "aa",
                        "option_number": 1,
                        "option_label": "x",
                        "accepted_at": 1.0,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        assert auq_ledger.lookup("k:1") is None
        assert any("unknown state" in rec.message for rec in caplog.records)


class TestLRUCompaction:
    def test_compaction_at_startup_when_over_cap(self, monkeypatch, ledger_path, clock):
        # Force a small cap so the test is cheap.
        monkeypatch.setattr(auq_ledger, "LRU_CAP", 5)
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        # Write 10 entries spread over time so accepted_at differs.
        for i in range(10):
            clock.tick(1.0)
            auq_ledger.record(f"k:{i}", **_first_write_kwargs(option_number=i + 1))
        # Reload — startup compaction trims to the 5 most-recent-per-key.
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        survivors = [auq_ledger.lookup(f"k:{i}") for i in range(10)]
        present = [s for s in survivors if s is not None]
        # The 5 newest keys (k:5..k:9) survive.
        assert len(present) == 5
        kept_keys = {e.key for e in present}
        assert kept_keys == {"k:5", "k:6", "k:7", "k:8", "k:9"}

    def test_compaction_drops_entries_older_than_retention(
        self, monkeypatch, ledger_path, clock
    ):
        monkeypatch.setattr(auq_ledger, "LRU_CAP", 100)  # not LRU-bound here
        monkeypatch.setattr(auq_ledger, "RETENTION_SECONDS", 100.0)
        # Seed an old + a new entry then force compaction by exceeding LRU
        # cap (LRU_CAP=2 forces it).
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        auq_ledger.record("old", **_first_write_kwargs())  # accepted_at=1000
        clock.tick(1000.0)  # advance well past retention
        auq_ledger.record("new", **_first_write_kwargs(option_number=3))
        monkeypatch.setattr(auq_ledger, "LRU_CAP", 1)
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        assert auq_ledger.lookup("old") is None  # dropped by retention
        assert auq_ledger.lookup("new") is not None


class TestProcessStartTimeProjection:
    """Callers use process_start_time() to decide whether a pre-process-
    start accepted/digit_sent entry should be projected to ``unknown``.
    """

    def test_process_start_time_is_stable_within_a_run(self, setup_ledger):
        a = auq_ledger.process_start_time()
        b = auq_ledger.process_start_time()
        assert a == b

    def test_reset_for_tests_can_set_start_time(self, ledger_path, clock):
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=12345.0)
        assert auq_ledger.process_start_time() == 12345.0

    def test_caller_projects_pre_start_entries_to_unknown(self, ledger_path, clock):
        # Simulate: prior process wrote an `accepted` entry, then crashed;
        # current process started later, so the entry's accepted_at is
        # before process_start_time.
        auq_ledger.reset_for_tests(path=ledger_path, now=clock, start_time=clock())
        auq_ledger.record("k:1", **_first_write_kwargs())
        # Bump start time forward (simulates restart).
        auq_ledger.reset_for_tests(
            path=ledger_path,
            now=clock,
            start_time=clock() + 100.0,
        )
        entry = auq_ledger.lookup("k:1")
        assert entry is not None
        assert entry.state == "accepted"  # raw row unchanged
        # The caller's projection rule — exercised in callback handler
        # tests — applies HERE; this test just confirms the inputs.
        assert entry.accepted_at < auq_ledger.process_start_time()
