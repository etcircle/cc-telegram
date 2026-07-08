"""Stage B2.2 — ``handlers.decision_token`` (the Decision-lane leaf store).

Covers the three storage concerns:

  - Token store: mint / peek / consume; the concurrent reservation race (exactly
    one winner); §3(3) SIBLING-BURN (a winning consume tombstones the whole
    route row → a replayed sibling finds a tomb); the 300s TTL + the
    ``refresh_route_deadlines`` D3-β analogue keeping a live card's tokens with
    the SAME token strings; per-route hygiene on a fresh mint; ``teardown_route``.
  - The §5b(c) nav-generation registry: rotate-on-render advances + is per-window;
    ``invalidate_on_dispatch`` and a registry wipe both fail closed (no current
    generation).
  - The §2b (family × CC-version) dispatch table + family identification against
    the REAL folder-trust fixture, plus the negatives.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cctelegram.handlers import decision_token as dt
from cctelegram.terminal_parser import (
    AskOption,
    AskUserQuestionForm,
    parse_generic_decision,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_TRUST = "decision_trust_folder_v2.1.200.txt"


class _Clock:
    """Mutable monotonic stand-in — tests advance it explicitly."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, delta: float = 1.0) -> None:
        self.t += delta


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture(autouse=True)
def _reset(clock: _Clock):
    dt.reset_for_tests(now=clock)
    yield
    dt.reset_for_tests()


def _specs(*pairs: tuple[int, str]) -> list[dt.DecisionMintSpec]:
    return [dt.DecisionMintSpec(option_number=n, option_label=lbl) for n, lbl in pairs]


def _mint(window_id: str = "@3", fp: str = "fp0") -> list[str]:
    return dt.mint_row(
        user_id=1,
        thread_id=7,
        window_id=window_id,
        fingerprint=fp,
        specs=_specs((1, "Yes"), (2, "No")),
    )


# ── token store: mint / peek / consume ───────────────────────────────────────


def test_mint_and_peek() -> None:
    toks = _mint()
    assert len(toks) == 2
    e = dt.peek(toks[0])
    assert e is not None
    assert e.option_number == 1 and e.option_label == "Yes"
    assert dt.peek("no-such-token") is None


async def test_consume_wins_then_owner_and_expired() -> None:
    toks = _mint()
    res = await dt.consume(toks[0], sender_id=1)
    assert res.outcome == "ok"
    assert res.entry is not None and res.entry.option_number == 1
    # A never-minted token is the benign refresh case.
    assert (await dt.consume("ghost", 1)).outcome == "expired"


async def test_wrong_user_does_not_burn() -> None:
    toks = _mint()
    assert (await dt.consume(toks[0], sender_id=999)).outcome == "wrong_user"
    # The legitimate owner can still win — the wrong-user tap burned nothing.
    assert (await dt.consume(toks[0], sender_id=1)).outcome == "ok"


# ── concurrent reservation race: exactly one winner ──────────────────────────


async def test_concurrent_consume_exactly_one_winner() -> None:
    toks = _mint()
    a, b = await asyncio.gather(
        dt.consume(toks[0], 1),
        dt.consume(toks[1], 1),
    )
    assert sorted((a.outcome, b.outcome)) == ["already_consumed", "ok"]


async def test_same_token_double_tap_second_loses() -> None:
    toks = _mint()
    first = await dt.consume(toks[0], 1)
    second = await dt.consume(toks[0], 1)
    assert first.outcome == "ok"
    assert second.outcome == "already_consumed"


# ── §3(3) sibling-burn: winning consume tombs the whole route ────────────────


async def test_sibling_burn_replay_finds_tomb() -> None:
    toks = _mint()
    assert (await dt.consume(toks[0], 1)).outcome == "ok"
    # A losing / late / replayed SIBLING finds only the tomb.
    assert (await dt.consume(toks[1], 1)).outcome == "already_consumed"
    # Both option cards read as resolved.
    assert dt.peek(toks[0]) is None
    assert dt.peek(toks[1]) is None


# ── TTL expiry + refresh_route_deadlines (same token strings) ────────────────


async def test_ttl_expiry_then_refresh_keeps_same_tokens(clock: _Clock) -> None:
    toks = _mint()
    # Just before expiry, a refresh re-stamps the live tokens (same strings).
    clock.tick(299.0)
    refreshed = await dt.refresh_route_deadlines(
        1, 7, "@3", min_remaining_s=60.0, now=clock()
    )
    assert refreshed == 2
    # The token STRINGS are unchanged — the keyboard stays byte-identical.
    assert dt.peek(toks[0]) is not None
    # Advance past the ORIGINAL deadline: the refresh kept it alive.
    clock.tick(60.0)  # t = 1000 + 299 + 60 = 1359 (original deadline was 1300)
    assert dt.peek(toks[0]) is not None
    assert (await dt.consume(toks[0], 1)).outcome == "ok"


async def test_ttl_expiry_without_refresh_prunes(clock: _Clock) -> None:
    toks = _mint()
    clock.tick(301.0)  # past the 300s deadline
    assert dt.peek(toks[0]) is None
    assert (await dt.consume(toks[0], 1)).outcome == "expired"


async def test_refresh_never_resurrects_expired_token(clock: _Clock) -> None:
    toks = _mint()
    clock.tick(301.0)
    refreshed = await dt.refresh_route_deadlines(
        1, 7, "@3", min_remaining_s=60.0, now=clock()
    )
    assert refreshed == 0
    assert dt.peek(toks[0]) is None


# ── per-route hygiene on fresh mint ──────────────────────────────────────────


def test_fresh_mint_drops_prior_nontombstoned_row() -> None:
    old = _mint(fp="fpA")
    new = _mint(fp="fpB")  # a genuine transition → fresh mint
    # The prior card's tokens are gone (superseded); only the new row is live.
    assert dt.peek(old[0]) is None
    assert dt.peek(old[1]) is None
    assert dt.peek(new[0]) is not None


async def test_fresh_mint_keeps_tombstoned_row_for_replay(clock: _Clock) -> None:
    old = _mint(fp="fpA")
    assert (await dt.consume(old[0], 1)).outcome == "ok"  # tombstone the row
    _mint(fp="fpB")  # fresh re-render — must KEEP the tombstone
    # A stale replay of the OLD card's sibling still classifies as consumed.
    assert (await dt.consume(old[1], 1)).outcome == "already_consumed"


# ── teardown_route ───────────────────────────────────────────────────────────


async def test_teardown_route_drops_tokens_and_nav_generation() -> None:
    toks = _mint()
    dt.rotate_nav_generation("@3")
    assert dt.current_nav_generation("@3") is not None
    dt.teardown_route(1, 7, "@3")
    assert dt.peek(toks[0]) is None
    assert (await dt.consume(toks[0], 1)).outcome == "expired"
    assert dt.current_nav_generation("@3") is None


# ── §5b(c) nav-generation registry ───────────────────────────────────────────


def test_rotate_advances_and_is_per_window() -> None:
    assert dt.current_nav_generation("@3") is None  # unset → fail closed
    g1 = dt.rotate_nav_generation("@3")
    g2 = dt.rotate_nav_generation("@3")
    assert g2 != g1
    assert dt.current_nav_generation("@3") == g2
    # Per-window: another window has its own generation, globally unique.
    gother = dt.rotate_nav_generation("@9")
    assert gother not in (g1, g2)
    assert dt.current_nav_generation("@9") == gother
    assert dt.current_nav_generation("@3") == g2  # untouched


def test_invalidate_on_dispatch_fails_closed() -> None:
    dt.rotate_nav_generation("@3")
    assert dt.current_nav_generation("@3") is not None
    dt.invalidate_on_dispatch("@3")
    assert dt.current_nav_generation("@3") is None


def test_registry_wipe_fails_closed() -> None:
    dt.rotate_nav_generation("@3")
    dt.reset_for_tests()  # simulate a restart wiping the in-memory registry
    assert dt.current_nav_generation("@3") is None


def test_rotated_generation_never_recollides_after_invalidate() -> None:
    # A dispatched card's g<old>, replayed after a NEW card renders, must never
    # validate against the new card's generation (the §5b(c) attack).
    g_old = dt.rotate_nav_generation("@3")
    dt.invalidate_on_dispatch("@3")
    g_new = dt.rotate_nav_generation("@3")
    assert g_new != g_old
    assert dt.current_nav_generation("@3") == g_new


# ── §2b dispatch table + family identification ───────────────────────────────


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text()


def test_family_positive_from_real_fixture_and_license() -> None:
    form = parse_generic_decision(_load(_TRUST))
    assert form is not None
    assert dt.identify_family(form) == "folder-trust"
    # Licensed for the characterized CC version; nothing else.
    assert dt.lookup("folder-trust", "2.1.204") is True
    assert dt.lookup("folder-trust", "2.1.200") is False
    assert dt.lookup("folder-trust", "2.1.201") is False


def test_family_positive_on_the_licensed_v2_1_204_fixture() -> None:
    """The ONLY licensed version's own live fixture must identify (wave-2
    review fold — Codex P3 / Hermes P2): the table says "2.1.204", so a
    family-signature/title drift that leaves ``lookup("folder-trust",
    "2.1.204")`` green while the real .204 shape stops identifying would
    silently render the only licensed version display-only. This test dies
    with the drift instead."""
    form = parse_generic_decision(_load("decision_trust_folder_v2.1.204.txt"))
    assert form is not None
    assert dt.identify_family(form) == "folder-trust"
    assert dt.lookup("folder-trust", "2.1.204") is True


def test_family_negative_option_tuple_mismatch() -> None:
    form = AskUserQuestionForm(
        current_question_title="Accessing workspace:",
        options=(
            AskOption(
                label="Yes, I trust this folder",
                recommended=False,
                cursor=True,
                number=1,
            ),
            AskOption(label="No thanks", recommended=False, cursor=False, number=2),
        ),
        select_mode="single",
    )
    assert dt.identify_family(form) is None


def test_family_negative_title_anchor_mismatch() -> None:
    form = AskUserQuestionForm(
        current_question_title="Some other heading",
        options=(
            AskOption(
                label="Yes, I trust this folder",
                recommended=False,
                cursor=True,
                number=1,
            ),
            AskOption(label="No, exit", recommended=False, cursor=False, number=2),
        ),
        select_mode="single",
    )
    assert dt.identify_family(form) is None


def test_family_negative_title_none() -> None:
    form = AskUserQuestionForm(
        current_question_title=None,
        options=(
            AskOption(
                label="Yes, I trust this folder",
                recommended=False,
                cursor=True,
                number=1,
            ),
            AskOption(label="No, exit", recommended=False, cursor=False, number=2),
        ),
        select_mode="single",
    )
    assert dt.identify_family(form) is None


def test_lookup_unknown_family_and_version() -> None:
    assert dt.lookup("no-such-family", "2.1.204") is False
    assert dt.lookup("folder-trust", "9.9.9") is False
