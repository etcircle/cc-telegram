"""RED tests for the PR-A full-identity ledger key + state() helper (plan v3 §5 / §8 test 7).

The stateless callback path keys idempotency on the FULL identity
``(user_id, thread_id, window_id, fp16, opt)`` instead of the truncated
``(route_hash8, fp8, opt)`` triplet, so two distinct routes that happen to share
a fingerprint+option can never collide in the ledger (retiring the §7.2
collision-defense branch). The old 3-arg key builder is preserved (renamed
``make_legacy_ledger_key``) for the legacy token path during dual-read.

``state(key)`` is a pure raw-state read — NO ``process_start_time`` projection;
``accepted``/``commit_unconfirmed`` stay durable across restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cctelegram.handlers import auq_ledger

_FP16 = "deadbeefcafe0011"


@pytest.fixture
def setup_ledger(tmp_path: Path):
    auq_ledger.reset_for_tests(path=tmp_path / "ledger.jsonl", now=lambda: 1000.0)
    yield
    auq_ledger.reset_for_tests()


# ── new full-identity make_ledger_key ────────────────────────────────────────


def test_full_identity_key_format() -> None:
    assert auq_ledger.make_ledger_key(42, 10, "@5", _FP16, 2) == f"42:10:@5:{_FP16}:2"


def test_full_identity_key_normalizes_none_thread_to_zero() -> None:
    assert auq_ledger.make_ledger_key(42, None, "@5", _FP16, 2) == f"42:0:@5:{_FP16}:2"


def test_full_identity_key_uses_full_fp16_not_truncated() -> None:
    # The whole 16-hex fingerprint must appear — no fp8 truncation.
    key = auq_ledger.make_ledger_key(42, 10, "@5", _FP16, 2)
    assert _FP16 in key


def test_distinct_user_yields_distinct_key() -> None:
    k1 = auq_ledger.make_ledger_key(42, 10, "@5", _FP16, 2)
    k2 = auq_ledger.make_ledger_key(99, 10, "@5", _FP16, 2)
    assert k1 != k2


def test_distinct_window_yields_distinct_key() -> None:
    k1 = auq_ledger.make_ledger_key(42, 10, "@5", _FP16, 2)
    k2 = auq_ledger.make_ledger_key(42, 10, "@7", _FP16, 2)
    assert k1 != k2


# ── plan §8 test 7: collision-immunity ───────────────────────────────────────


def test_collision_immunity_independent_states(setup_ledger) -> None:
    """Two routes (diff window/user) same fp16/opt → independent ledger states."""
    key_a = auq_ledger.make_ledger_key(42, 10, "@5", _FP16, 2)
    key_b = auq_ledger.make_ledger_key(99, 10, "@7", _FP16, 2)
    assert key_a != key_b

    auq_ledger.record(
        key_a,
        state="accepted",
        user_id=42,
        window_id="@5",
        full_fingerprint=_FP16,
        option_number=2,
        option_label="A",
    )
    # route B is untouched.
    assert auq_ledger.state(key_a) == "accepted"
    assert auq_ledger.state(key_b) is None


# ── state() helper ───────────────────────────────────────────────────────────


def test_state_none_for_missing_key(setup_ledger) -> None:
    assert auq_ledger.state("42:10:@5:nope:2") is None


def test_state_none_for_none_key(setup_ledger) -> None:
    assert auq_ledger.state(None) is None


def test_state_returns_latest_persisted_state(setup_ledger) -> None:
    key = auq_ledger.make_ledger_key(42, 10, "@5", _FP16, 2)
    auq_ledger.record(
        key,
        state="accepted",
        user_id=42,
        window_id="@5",
        full_fingerprint=_FP16,
        option_number=2,
        option_label="A",
    )
    assert auq_ledger.state(key) == "accepted"
    auq_ledger.record(key, state="dispatched")
    assert auq_ledger.state(key) == "dispatched"


def test_state_is_raw_not_projected(tmp_path: Path) -> None:
    # No process_start_time → unknown projection: an accepted row stamped before
    # the current process start still reads raw "accepted" (durable refuse).
    auq_ledger.reset_for_tests(
        path=tmp_path / "raw.jsonl", now=lambda: 5000.0, start_time=9000.0
    )
    key = auq_ledger.make_ledger_key(42, 10, "@5", _FP16, 2)
    auq_ledger.record(
        key,
        state="accepted",
        user_id=42,
        window_id="@5",
        full_fingerprint=_FP16,
        option_number=2,
        option_label="A",
    )
    assert auq_ledger.state(key) == "accepted"


# ── renamed legacy builder (behavior-neutral regression guard) ───────────────


def test_legacy_ledger_key_unchanged_format() -> None:
    assert (
        auq_ledger.make_legacy_ledger_key("0a1b2c3d", "deadbeef", 2)
        == "0a1b2c3d:deadbeef:2"
    )
