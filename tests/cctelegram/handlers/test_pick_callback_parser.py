"""RED tests for the PR-A strict pick-callback parser (plan v3 §2 / §8 test 9).

The dispatcher must disambiguate the legacy token-keyed shape from the new
stateless self-describing shape purely from the wire grammar, with no in-memory
lookup. After stripping the ``aqp:``/``aqt:`` prefix and splitting on ``:`` the
payload is exactly 4 fields ``(rh8, f2, f3, f4)``:

  Legacy:    rh8(8hex) : fp8(8hex)  : opt(digits)  : token(12hex)
  Stateless: rh8(8hex) : fp16(16hex): u+owner5(5hex): opt(digits)

Disambiguation is airtight: field-2 length 8 (legacy) vs 16 (stateless); a
stateless field-3 always starts with ``u`` so an all-digit ``owner5`` still
parses stateless. A 6-hex legacy token (the v2 wrong assumption) is malformed.
"""

from __future__ import annotations

import pytest

from cctelegram.handlers.callback_data import (
    LegacyPickCallback,
    StatelessPickCallback,
    parse_pick_callback,
)

_RH8 = "0a1b2c3d"
_FP8 = "deadbeef"
_FP16 = "deadbeefcafe0011"
_TOKEN12 = "0123456789ab"


def test_legacy_shape_parses() -> None:
    parsed = parse_pick_callback(f"{_RH8}:{_FP8}:2:{_TOKEN12}")
    assert parsed == LegacyPickCallback(
        route_hash=_RH8, fp8=_FP8, option_number=2, token=_TOKEN12
    )


def test_stateless_shape_parses() -> None:
    parsed = parse_pick_callback(f"{_RH8}:{_FP16}:u1f2e3:2")
    assert parsed == StatelessPickCallback(
        route_hash=_RH8, fp16=_FP16, owner5="1f2e3", option_number=2
    )


def test_stateless_all_digit_owner5_still_parses_stateless() -> None:
    # The literal 'u' is the discriminator — an all-digit owner5 must NOT be
    # misread as a legacy opt field.
    parsed = parse_pick_callback(f"{_RH8}:{_FP16}:u12345:7")
    assert isinstance(parsed, StatelessPickCallback)
    assert parsed.owner5 == "12345"
    assert parsed.option_number == 7


def test_two_digit_option_parses() -> None:
    legacy = parse_pick_callback(f"{_RH8}:{_FP8}:10:{_TOKEN12}")
    assert isinstance(legacy, LegacyPickCallback)
    assert legacy.option_number == 10
    stateless = parse_pick_callback(f"{_RH8}:{_FP16}:uabcde:10")
    assert isinstance(stateless, StatelessPickCallback)
    assert stateless.option_number == 10


def test_six_hex_legacy_token_is_malformed() -> None:
    # token must be exactly 12 hex (secrets.token_hex(6)); 6 hex → malformed.
    assert parse_pick_callback(f"{_RH8}:{_FP8}:2:0123ab") is None


def test_stateless_missing_u_prefix_is_malformed() -> None:
    # field-2 is 16hex (so not legacy) but field-3 lacks the 'u' → neither shape.
    assert parse_pick_callback(f"{_RH8}:{_FP16}:1f2e3:2") is None


def test_bad_route_hash_length_is_malformed() -> None:
    assert parse_pick_callback(f"0a1b:{_FP8}:2:{_TOKEN12}") is None


def test_wrong_field_count_is_malformed() -> None:
    assert parse_pick_callback(f"{_RH8}:{_FP8}:2") is None  # 3 parts
    assert parse_pick_callback(f"{_RH8}:{_FP16}:u1f2e3:2:extra") is None  # 5 parts
    assert parse_pick_callback("") is None


def test_three_digit_option_is_malformed() -> None:
    # opt grammar is \d{1,2}.
    assert parse_pick_callback(f"{_RH8}:{_FP8}:100:{_TOKEN12}") is None
    assert parse_pick_callback(f"{_RH8}:{_FP16}:u1f2e3:100") is None


def test_uppercase_hex_is_malformed() -> None:
    # fingerprints are lowercase hexdigest; the grammar is [0-9a-f].
    assert parse_pick_callback(f"{_RH8}:DEADBEEF:2:{_TOKEN12}") is None
    assert parse_pick_callback(f"{_RH8.upper()}:{_FP16}:u1f2e3:2") is None


def test_non_hex_fields_are_malformed() -> None:
    assert parse_pick_callback(f"{_RH8}:zzzzzzzz:2:{_TOKEN12}") is None
    assert parse_pick_callback(f"{_RH8}:{_FP16}:uzzzzz:2") is None


def test_trailing_newline_in_any_field_is_malformed() -> None:
    # Python `$` matches before a trailing \n; a STRICT parser must reject a
    # newline embedded in any field (fullmatch, not match + ^...$). Codex+Hermes P2.
    assert parse_pick_callback(f"{_RH8}:{_FP8}:2:{_TOKEN12}\n") is None  # token field
    assert parse_pick_callback(f"{_RH8}\n:{_FP8}:2:{_TOKEN12}") is None  # rh8 field
    assert parse_pick_callback(f"{_RH8}:{_FP8}:2\n:{_TOKEN12}") is None  # opt field
    assert parse_pick_callback(f"{_RH8}:{_FP16}:u1f2e3:2\n") is None  # stateless opt
    assert parse_pick_callback(f"{_RH8}:{_FP16}:u1f2e3\n:2") is None  # stateless owner


def test_unicode_digit_option_is_malformed() -> None:
    # `\d` matches Unicode decimal digits (and int() accepts them); the wire
    # grammar is ASCII [0-9] only. Arabic-Indic TWO (U+0662) must be rejected.
    assert parse_pick_callback(f"{_RH8}:{_FP8}:٢:{_TOKEN12}") is None  # legacy opt
    assert parse_pick_callback(f"{_RH8}:{_FP16}:u1f2e3:٢") is None  # stateless opt


@pytest.mark.parametrize(
    "payload",
    [
        f"{_RH8}:{_FP8}:2:{_TOKEN12}",  # legacy
        f"{_RH8}:{_FP16}:u12345:2",  # stateless all-digit owner
        f"{_RH8}:{_FP16}:uabcde:2",  # stateless hex owner
    ],
)
def test_valid_shapes_round_trip_disjoint(payload: str) -> None:
    parsed = parse_pick_callback(payload)
    assert parsed is not None
    # exactly one of the two dataclasses, never both/neither.
    assert isinstance(parsed, (LegacyPickCallback, StatelessPickCallback))
