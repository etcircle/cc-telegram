"""Stage B2.2 — ``auq_ledger.release_key`` (the Decision lane's per-key release).

Additive to ``release_window`` (the AUQ lane's window-field sweep). Pins:

  - a round trip: an ``accepted`` Decision-lane row → ``release_key`` →
    ``lookup`` returns None (a byte-identical re-raise is dispatchable again),
    idempotent + never fabricating an absent key;
  - the pre-existing ``release_window`` cross-release is HARMLESS-and-consistent
    for the Decision lane: an AUQ ``tool_result`` firing ``release_window`` on a
    window ALSO releases a Decision-lane row carrying that window field →
    ``lookup`` None, no state corruption (§8).

The Decision fp8 derives from ``decision_prompt_fingerprint`` (whose hashed
input carries the ``decision:`` prefix), so its ledger key can never collide
with an AUQ key by construction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cctelegram.handlers import auq_ledger
from cctelegram.terminal_parser import (
    parse_generic_decision,
    decision_prompt_fingerprint,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_TRUST = "decision_trust_folder_v2.1.200.txt"


@pytest.fixture
def setup_ledger(tmp_path: Path):
    auq_ledger.reset_for_tests(path=tmp_path / "auq_action_ledger.jsonl")
    yield
    auq_ledger.reset_for_tests()


def _decision_key(window_id: str = "@3") -> tuple[str, str, int]:
    """Reconstruct a Decision-lane ledger key from the REAL folder-trust form."""
    form = parse_generic_decision((_FIXTURES / _TRUST).read_text())
    assert form is not None
    fp8 = decision_prompt_fingerprint(form)[:8]
    route_hash = auq_ledger.make_route_hash(1, 7, window_id)
    return auq_ledger.make_ledger_key(route_hash, fp8, 1), fp8, 1


def _accept(key: str, fp8: str, opt: int, window_id: str = "@3") -> None:
    auq_ledger.record(
        key,
        state="accepted",
        user_id=1,
        window_id=window_id,
        full_fingerprint=fp8 + "0" * 8,
        option_number=opt,
        option_label="Yes, I trust this folder",
    )


def test_release_key_round_trip(setup_ledger) -> None:
    key, fp8, opt = _decision_key()
    _accept(key, fp8, opt)
    assert auq_ledger.lookup(key) is not None  # dispatchable / claimed
    assert auq_ledger.release_key(key) is True
    assert auq_ledger.lookup(key) is None  # released → re-raise dispatchable again


def test_release_key_absent_and_idempotent(setup_ledger) -> None:
    key, fp8, opt = _decision_key()
    # Absent key → never fabricates a row (record's first-write identity rule).
    assert auq_ledger.release_key(key) is False
    _accept(key, fp8, opt)
    assert auq_ledger.release_key(key) is True
    # Second release is a no-op (already released).
    assert auq_ledger.release_key(key) is False


def test_release_window_cross_release_is_harmless_for_decision_lane(
    setup_ledger,
) -> None:
    # A Decision-lane row carrying window @3, plus an unrelated window @9 row.
    key, fp8, opt = _decision_key("@3")
    _accept(key, fp8, opt, window_id="@3")
    other_key, other_fp8, _ = _decision_key("@9")
    _accept(other_key, other_fp8, 1, window_id="@9")

    # An AUQ tool_result on @3 fires release_window — it also releases the
    # Decision row carrying window @3 (matches the window_id FIELD). Harmless:
    # the row just becomes dispatchable again; @9 is untouched.
    released = auq_ledger.release_window("@3")
    assert released == 1
    assert auq_ledger.lookup(key) is None
    assert auq_ledger.lookup(other_key) is not None
