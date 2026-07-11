"""In-memory single-use token store + nav-generation registry + dispatch table
for the Stage-B2 tappable ``Decision`` dispatch lane.

A PARALLEL, Decision-specific lane that reuses the AUQ dispatch DISCIPLINE but
NEVER the AUQ source/form machinery. This leaf owns three storage concerns and
nothing else (all render/callback/dispatch policy lives in Stage B2.3):

  1. ``_tokens`` / ``_rows`` — per-option single-use tokens keyed by route
     ``(user_id, thread_id_or_0, window_id)`` × a monotonic mint generation.
     ``consume`` wins by EXCLUSIVE RESERVATION and the winning consume
     TOMBSTONES the whole route row (§3(3) sibling-burn: a losing/late sibling
     tap — or a Telegram replay of stale ``dcp:`` callback_data — finds only a
     tomb → ``already_consumed``). 300s TTL bounds memory; ``refresh_route_
     deadlines`` (the D3-β analogue) re-stamps a visibly-live card's tokens
     WITHOUT changing the token strings; ``mint_row`` drops prior non-tombstoned
     rows for the route (hygiene).
  2. The per-render NAV-GENERATION registry (§5b(c)) — ``current_nav_
     generation`` / ``rotate_nav_generation`` (per Decision render) /
     ``invalidate_on_dispatch`` (a SYNCHRONOUS dict op called in-lock at
     ``dispatched`` by B2.3). A bot RESTART wipes the registry, so a
     generation-suffixed raw-nav callback fails closed (``current_nav_
     generation`` returns ``None`` until a fresh render re-mints).
  3. The §2b known-good ``(family × CC-version)`` dispatch table + family
     identification — the mint/tap license gate. A MODULE CONSTANT, never
     env-configurable (O-5).

KILL CRITERIA (plan §2 — any ONE forces UNIFICATION with ``pick_token``, this
lane loses its independent existence): (1) a durable / on-disk intent store;
(2) source kinds; (3) multi-question or multi-select support; (4) any
JSONL / side-file resolver import. Accordingly this module imports ONLY stdlib
— NEVER ``pick_token`` / ``auq_source`` / ``route_runtime`` / any JSONL or
side-file resolver — and stays render/callback-path state (NOT a RouteRuntime
field; pull-only, no observer — c313657 forbidden).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..terminal_parser import AskUserQuestionForm

logger = logging.getLogger(__name__)

# TTL bounds MEMORY only — a visibly-live card keeps its tokens alive via the
# poller's ``refresh_route_deadlines`` (D3-β analogue), NOT this constant.
_TOKEN_TTL_SECONDS: Final[float] = 300.0


@dataclass(frozen=True)
class DecisionTokenEntry:
    """Server-side state bound to one Decision option-button tap.

    Frozen: the minted fingerprint anchors the dispatch-time identity re-check,
    so the entry must not mutate. ``row_generation`` is the mint generation the
    route row was created with — it makes the ``(route × generation)`` row key
    unique so a fresh re-mint's row never clobbers a still-tombstoned prior row
    (sibling-burn replay classification stays exact across a re-render).
    """

    window_id: str
    user_id: int
    thread_id: int | None
    fingerprint: str  # decision_prompt_fingerprint(form) at render
    option_number: int
    option_label: str
    expires_at: float  # monotonic deadline
    row_generation: int


@dataclass
class _DecisionRow:
    """The sibling-token row for one rendered Decision card generation.

    Route-scoped (at most one NON-tombstoned row per route — ``mint_row``
    hygiene guarantees it). A WINNING ``consume`` sets ``tombstoned_at`` and
    KEEPS ``tokens`` so a losing/late/replayed sibling tap still resolves to
    the tomb (``already_consumed``) until the row ages past the TTL.
    """

    tokens: list[str]
    row_generation: int
    tombstoned_at: float | None = None


@dataclass(frozen=True)
class DecisionConsume:
    """Typed outcome of ``consume``.

    ``ok`` = this tap WON the route (siblings burned); ``already_consumed`` = a
    sibling/replay found the burned tomb OR a concurrent tap won first;
    ``wrong_user`` = owner mismatch; ``expired`` = token absent / TTL-pruned
    (benign refresh). ``entry`` is present whenever the token was found.
    """

    outcome: Literal["ok", "wrong_user", "expired", "already_consumed"]
    entry: DecisionTokenEntry | None


@dataclass(frozen=True)
class DecisionMintSpec:
    """One option-button to mint a token for (per-option mint input)."""

    option_number: int
    option_label: str


_RouteKey = tuple[int, int, str]  # (user_id, thread_id_or_0, window_id)
_RowKey = tuple[int, int, str, int]  # _RouteKey + generation

_tokens: dict[str, DecisionTokenEntry] = {}
_rows: dict[_RowKey, _DecisionRow] = {}

# Per-window CURRENT nav generation for the raw ↑/↓/⏎/Esc keyboard (§5b(c)).
_nav_generations: dict[str, int] = {}

# MODULE-GLOBAL monotonic counter shared by row mints AND nav rotations — every
# id is globally unique, so a restarted/rotated generation never re-collides
# with a stale card's suffix (the exact §5b(c) attack: an old ``g<n>`` must
# never validate against a NEW card's generation). Only ``reset_for_tests``
# rewinds it.
_INITIAL_GENERATION: Final[int] = 0
_generation_counter: int = _INITIAL_GENERATION

# Injection seam for tests (mirrors pick_token / auq_ledger).
_now: Callable[[], float] = time.monotonic

# §7 dispatch flag (``CC_TELEGRAM_DECISION_DISPATCH``). A MODULE-LOCAL bool so
# this leaf never imports ``config`` (which raises without a bot token — the
# same reason the two ``terminal_parser`` parser flags are local). ``config``
# owns the canonical env declaration; ``main._run_bot`` SEEDS this from it at
# startup (the import-order-race dodge). Checked at BOTH the render mint site and
# the ``dcp:`` callback entry so a flag-OFF deploy is provably inert (the render
# never mints buttons, the callback declines). Reset by ``reset_for_tests``.
_DISPATCH_ENABLED: bool = False

# Serialises token-store mutations so ``consume`` reserves EXCLUSIVELY (the
# critical section never awaits, so concurrent consumes have exactly one winner).
_store_lock = asyncio.Lock()


def _next_generation() -> int:
    global _generation_counter
    _generation_counter += 1
    return _generation_counter


def _rowkey(entry: DecisionTokenEntry) -> _RowKey:
    return (
        entry.user_id,
        entry.thread_id or 0,
        entry.window_id,
        entry.row_generation,
    )


def _prune_expired(now: float | None = None) -> None:
    """Drop expired tokens + dead rows. Row-oriented so tombstoned rows keep
    their tokens (for replay classification) until the row itself ages out."""
    if now is None:
        now = _now()
    for rowkey, row in list(_rows.items()):
        if row.tombstoned_at is not None:
            if now - row.tombstoned_at > _TOKEN_TTL_SECONDS:
                for tok in row.tokens:
                    _tokens.pop(tok, None)
                _rows.pop(rowkey, None)
            continue
        live: list[str] = []
        for tok in row.tokens:
            entry = _tokens.get(tok)
            if entry is not None and entry.expires_at > now:
                live.append(tok)
            else:
                _tokens.pop(tok, None)
        row.tokens = live
        if not live:
            _rows.pop(rowkey, None)


def _mint_token(entry: DecisionTokenEntry) -> str:
    """Register a unique 12-hex token for one option button."""
    for _ in range(8):
        token = secrets.token_hex(6)
        if token not in _tokens:
            _tokens[token] = entry
            return token
    raise RuntimeError("Unable to mint a unique decision token")


def mint_row(
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    fingerprint: str,
    specs: Iterable[DecisionMintSpec],
) -> list[str]:
    """Mint the sibling-token row for one rendered Decision card.

    Always a FRESH mint (Decision cards re-render only on a genuine transition;
    the poller keeps a same-hash card alive via ``refresh_route_deadlines``, not
    a re-mint). Drops any PRIOR NON-tombstoned row for the route + its tokens
    (per-route hygiene — keeps the "one live row per route" invariant that
    sibling-burn relies on), allocates the next module-global generation, and
    returns the tokens in ``specs`` order. Tombstoned prior rows are KEPT (a
    replayed stale sibling still classifies as ``already_consumed``).
    """
    _prune_expired()
    norm = (user_id, thread_id or 0, window_id)
    for rowkey in list(_rows.keys()):
        row = _rows[rowkey]
        if rowkey[:3] == norm and row.tombstoned_at is None:
            for tok in row.tokens:
                _tokens.pop(tok, None)
            _rows.pop(rowkey, None)
    generation = _next_generation()
    deadline = _now() + _TOKEN_TTL_SECONDS
    tokens = [
        _mint_token(
            DecisionTokenEntry(
                window_id=window_id,
                user_id=user_id,
                thread_id=thread_id,
                fingerprint=fingerprint,
                option_number=spec.option_number,
                option_label=spec.option_label,
                expires_at=deadline,
                row_generation=generation,
            )
        )
        for spec in specs
    ]
    _rows[(user_id, thread_id or 0, window_id, generation)] = _DecisionRow(
        tokens=list(tokens), row_generation=generation
    )
    return tokens


def peek(token: str) -> DecisionTokenEntry | None:
    """Look up a token WITHOUT consuming it; ``None`` if gone / tombstoned /
    expired (a burned card reads as resolved)."""
    _prune_expired()
    entry = _tokens.get(token)
    if entry is None:
        return None
    row = _rows.get(_rowkey(entry))
    if row is None or row.tombstoned_at is not None:
        return None
    if entry.expires_at <= _now():
        return None
    return entry


async def consume(token: str, sender_id: int) -> DecisionConsume:
    """Atomically validate + single-use-consume a Decision token by EXCLUSIVE
    RESERVATION; the winning consume TOMBSTONES the whole route row.

    The whole op runs under ``_store_lock`` with NO ``await`` inside, so two
    concurrent consumes have exactly one winner (§3(3)): the first to enter the
    section burns the route; every subsequent tap — the same token, a sibling,
    or a Telegram replay of stale ``dcp:`` callback_data — finds the tomb and
    returns ``already_consumed``. Owner-check precedes the win; an absent /
    TTL-pruned token is the benign ``expired`` refresh case.
    """
    async with _store_lock:
        _prune_expired()
        entry = _tokens.get(token)
        if entry is None:
            return DecisionConsume("expired", None)
        row = _rows.get(_rowkey(entry))
        if row is None:
            return DecisionConsume("expired", None)
        if row.tombstoned_at is not None:
            return DecisionConsume("already_consumed", entry)
        if sender_id != entry.user_id:
            return DecisionConsume("wrong_user", entry)
        if entry.expires_at <= _now():
            return DecisionConsume("expired", entry)
        # WIN — burn the route's live row (winner + all siblings). Keep the
        # tokens so a losing/late sibling still reads the tomb.
        row.tombstoned_at = _now()
        return DecisionConsume("ok", entry)


async def refresh_route_deadlines(
    user_id: int,
    thread_id: int | None,
    window_id: str,
    *,
    min_remaining_s: float,
    now: float | None = None,
) -> int:
    """D3-β analogue: re-stamp a VISIBLY-LIVE card's tokens so their lifetime
    tracks the card's OBSERVED lifetime, WITHOUT changing the token strings.

    Each still-live (``now < expires_at``) token of the route's non-tombstoned
    rows within ``min_remaining_s`` of its deadline is REPLACED with a copy whose
    only change is ``expires_at`` — same token, same fingerprint, same
    generation, so the rendered keyboard stays byte-identical. Never resurrects
    an already-expired token; skips tombstoned rows. Returns the count refreshed.
    """
    norm = (user_id, thread_id or 0, window_id)
    refreshed = 0
    async with _store_lock:
        if now is None:
            now = _now()
        deadline = now + _TOKEN_TTL_SECONDS
        for rowkey, row in _rows.items():
            if rowkey[:3] != norm or row.tombstoned_at is not None:
                continue
            for tok in row.tokens:
                entry = _tokens.get(tok)
                if entry is None:
                    continue
                if now < entry.expires_at <= now + min_remaining_s:
                    _tokens[tok] = replace(entry, expires_at=deadline)
                    refreshed += 1
    return refreshed


def teardown_route(user_id: int, thread_id: int | None, window_id: str) -> None:
    """Drop every row + token for a route AND the window's nav generation.

    Wired (in B2.3) beside every route_runtime clear seam — topic close,
    poller window-gone, stale-window unbind, ``/clear``.
    """
    norm = (user_id, thread_id or 0, window_id)
    for rowkey in list(_rows.keys()):
        if rowkey[:3] == norm:
            row = _rows.pop(rowkey)
            for tok in row.tokens:
                _tokens.pop(tok, None)
    _nav_generations.pop(window_id, None)


# ── Per-render nav-generation registry (§5b(c)) ──────────────────────────────


def current_nav_generation(window_id: str) -> int | None:
    """The window's CURRENT nav generation, or ``None`` when unset.

    ``None`` fails a suffixed raw-nav callback CLOSED — the post-restart /
    post-dispatch state (a fresh render re-mints a new generation). B2.3's
    ``assert_nav_dispatchable`` compares a PRESENT callback generation against
    this value.
    """
    return _nav_generations.get(window_id)


def rotate_nav_generation(window_id: str) -> int:
    """Allocate + store a fresh nav generation for a Decision card render.

    Called on EVERY Decision render (licensed or display-only — both carry the
    raw ↑/↓/⏎/Esc keyboard). Draws from the module-global monotonic counter so a
    rotated generation is globally unique and a stale ``g<old>`` can never
    re-collide with a later card's generation.
    """
    generation = _next_generation()
    _nav_generations[window_id] = generation
    return generation


def invalidate_on_dispatch(window_id: str) -> None:
    """Drop the window's nav generation — a SYNCHRONOUS dict op (B2.3 calls it
    IN-LOCK at ``dispatched``, so a raw-nav tap landing in the
    lock-release→teardown gap already fails ``current_nav_generation``)."""
    _nav_generations.pop(window_id, None)


# ── §2b known-good (family × CC-version) dispatch table ──────────────────────
#
# Family identification: a Decision form matches a family IFF (a) its EXACT
# ordered option-label tuple equals the family's signature AND (b) its
# normalized title matches the family's anchored pattern (both from the strict
# ``parse_generic_decision`` form). Look-alikes are unknown families →
# display-only. The characterization basis is the wave-1 rig re-run on live CC
# 2.1.204 (E1 digit-commits / E2 arrows-move-only / E3 Enter-commits — the
# plan's 2.1.201 is SUPERSEDED); the committed shape fixtures are
# ``decision_trust_folder_v2.1.200.txt`` + ``folder_trust_arrival_plain_v2.1.206.txt``
# + ``folder_trust_arrival_plain_v2.1.207.txt`` (the folder-trust prompt shape
# is version-stable, so the signature holds across 2.1.20x — 2.1.206 and
# 2.1.207 are licensed in ``_DECISION_DISPATCH_TABLE`` below, the B2.4 canary
# precondition; the 2.1.207 rig re-run re-confirmed E2/E3).
_FamilySignature = tuple[tuple[str, ...], "re.Pattern[str]"]

_FAMILY_SIGNATURES: Final[dict[str, _FamilySignature]] = {
    "folder-trust": (
        ("Yes, I trust this folder", "No, exit"),
        re.compile(r"^Accessing workspace:"),
    ),
}

# family → the frozen set of CC-version strings whose keystroke behavior was
# characterized (arrows move / Enter commits) — a MODULE CONSTANT, NEVER
# env-configurable (O-5). Extended only by a commit citing the family's rig
# characterization. Membership is EXACT-STRING (a CC upgrade empties the
# effective allowlist → buttons revert to display-only until re-characterized).
_DECISION_DISPATCH_TABLE: Final[dict[str, frozenset[str]]] = {
    # 2.1.206 licensed from the real rig fixture
    # ``folder_trust_arrival_plain_v2.1.206.txt`` (title "Accessing workspace:",
    # options ["Yes, I trust this folder", "No, exit"], footer
    # "Enter to confirm · Esc to cancel" — identical shape to 2.1.204, so the
    # existing family signature holds; B2.4 canary precondition).
    # 2.1.207 licensed from the real rig fixture
    # ``folder_trust_arrival_plain_v2.1.207.txt`` (byte-identical folder-trust UI
    # to 2.1.206 — same title/security-prose/options/footer). Live rig keystroke
    # re-characterization on 2.1.207: arrows MOVE the ❯ cursor between option 1/2
    # WITHOUT committing (the prompt stays painted), and Enter COMMITS on the
    # highlighted option (the prompt disappears and Claude proceeds) — the E2/E3
    # invariant holds, so the family signature + navigate→verify→Enter dispatch
    # discipline carry; B2.4 canary precondition.
    "folder-trust": frozenset({"2.1.204", "2.1.206", "2.1.207"}),
}


def identify_family(form: AskUserQuestionForm) -> str | None:
    """Return the §2b family id this Decision form matches, or ``None``.

    Match IFF the EXACT ordered option-label tuple equals the family signature
    AND the normalized (stripped) title matches the family's anchored pattern.
    A title-less form (``current_question_title is None``) never matches a
    title-anchored family.
    """
    labels = tuple(o.label for o in form.options)
    title = (form.current_question_title or "").strip()
    for family, (sig_labels, title_re) in _FAMILY_SIGNATURES.items():
        if labels == sig_labels and title and title_re.search(title):
            return family
    return None


def lookup(family: str, version: str) -> bool:
    """True iff ``version`` is a known-good (characterized) CC version for
    ``family`` — EXACT-STRING membership; an unknown family / unknown version →
    False (unlicensed → display-only)."""
    return version in _DECISION_DISPATCH_TABLE.get(family, frozenset())


def set_decision_dispatch_enabled(enabled: bool) -> None:
    """Seed the §7 dispatch flag from config (``main._run_bot`` at startup).

    A module-local write so this leaf stays config-free. The render mint site and
    the ``dcp:`` callback entry consult ``decision_dispatch_enabled()``."""
    global _DISPATCH_ENABLED
    _DISPATCH_ENABLED = enabled


def decision_dispatch_enabled() -> bool:
    """True iff the §7 tappable-Decision-dispatch flag is ON (default OFF)."""
    return _DISPATCH_ENABLED


def reset_for_tests(*, now: Callable[[], float] | None = None) -> None:
    """Clear all in-memory state; optionally inject a monotonic clock.

    Resets tokens, rows, the nav-generation registry, the module-global
    generation counter (so a test that minted into a tombstone cannot leak a
    generation into the next test), AND the §7 dispatch flag back to OFF."""
    global _generation_counter, _now, _DISPATCH_ENABLED
    _tokens.clear()
    _rows.clear()
    _nav_generations.clear()
    _generation_counter = _INITIAL_GENERATION
    _now = now if now is not None else time.monotonic
    _DISPATCH_ENABLED = False
