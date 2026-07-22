"""GH #60 — the delivery gate must read a CC ghost suggestion as an EMPTY box.

Incident (2026-07-22, window @7, CC 2.1.212): after a session wound down, CC
rendered a fully-SGR-2-dim ghost suggestion ``/clear`` in the empty input row.
``classify_input_box_failure`` ANSI-STRIPPED the ghost (it did not ghost-clean),
so leg 5 saw a bare ``/word`` and refused every send as ``completion_overlay`` —
a self-sustaining topic wedge (the ghost only clears when someone types, and the
gate prevented all typing).

Fix: ``classify_input_box_failure`` runs ``clean_ghost_input_text`` instead of a
bare ``_strip_ansi``, so a FULLY-DIM ghost is blanked to a bare ``❯`` before the
five legs run. A real draft (normal intensity) or ANY dim/normal MIX is left
untouched — the existing fail-closed SGR-2 discriminator, unchanged.

These pins go RED pre-fix (the classifier stripped instead of cleaning) and green
after. The transaction test drives the REAL ``SessionManager.deliver_to_window``
with an ``with_ansi``-honoring fake so the wedge is exercised end to end.
"""

from __future__ import annotations

import logging

import pytest

from cctelegram import delivery
from cctelegram import terminal_parser as tp
from cctelegram.session import session_manager

# Reuse the shipped delivery-gate harness (its ``_Pane`` honors ``with_ansi``).
from tests.cctelegram.test_delivery_gate import _Pane, _fresh, _wire  # noqa: F401
from tests.conftest import pane_fixture

# ── Fixtures + the verbatim incident row ─────────────────────────────────────

_GHOST_PROSE = "inputbox_ghost_prose_v2.1.215.ansi.txt"  # REAL unmodified frame
_GHOST_SLASH = "inputbox_ghost_slash_clear_synthetic_v2.1.215.ansi.txt"
_GHOST_AT = "inputbox_ghost_at_word_synthetic_v2.1.215.ansi.txt"
_GHOST_NUMBERED = "inputbox_ghost_numbered_synthetic_v2.1.215.ansi.txt"
_REAL_DRAFT = "inputbox_real_draft_v2.1.217.ansi.txt"

# The GH #60 incident row VERBATIM (window @7, CC 2.1.212, 2026-07-22): a bare
# ``/clear`` rendered entirely SGR-2 dim after the ``❯`` glyph. Pinned as a test
# constant, separate from the synthetic full-frame fixture.
_INCIDENT_ROW = "\x1b[39m❯\xa0\x1b[2m/clear\x1b[0m"

# The set of new fixtures introduced by this wave — EXCLUDED from the corpus
# equivalence sweep (they deliberately FLIP the classification).
_GH60_FIXTURES = frozenset(
    {_GHOST_PROSE, _GHOST_SLASH, _GHOST_AT, _GHOST_NUMBERED, _REAL_DRAFT}
)


def _frame(name: str) -> str:
    return pane_fixture(name)


def _replace_input_row(frame: str, new_row_ansi: str) -> str:
    """Return ``frame`` with its FIRST ``❯`` input row swapped for ``new_row_ansi``.

    Keeps the surrounding rule pair + status chrome intact, so the result is a
    complete, classifiable frame differing only in the input-row content/intensity.
    """
    out: list[str] = []
    done = False
    for line in frame.split("\n"):
        if not done and tp._strip_ansi(line).strip()[:1] == "❯":
            out.append(new_row_ansi)
            done = True
        else:
            out.append(line)
    assert done, "no input row found in frame"
    return "\n".join(out)


# ── Parser repro (RED pre-fix) ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fixture", "pre_fix_reason"),
    [
        (_GHOST_SLASH, "completion_overlay"),  # `/clear` ghost → leg 5 `/`-arm
        (_GHOST_AT, "completion_overlay"),  # `ask @reviewer` ghost → leg 5 `@`-arm
        (_GHOST_NUMBERED, "prompt_row_is_option"),  # `1. …` ghost → leg 2 picker trap
    ],
    ids=["slash", "at_word", "numbered"],
)
def test_dim_ghost_frames_classify_none_after_ghost_clean(
    fixture: str, pre_fix_reason: str
) -> None:
    """A fully-dim ghost shaped as ``/word`` / ``@word`` / ``N. …`` must classify
    ``None`` (the box is empty). Pre-fix it stripped the ghost to visible text and
    returned ``pre_fix_reason`` — the wedge."""
    frame = _frame(fixture)
    # Documents the pre-fix hazard: the naive strip STILL returns the wedge reason.
    assert tp.classify_input_box_failure(tp._strip_ansi(frame)) == pre_fix_reason
    # The fix: ghost-cleaned, the row is empty → the gate passes.
    assert tp.classify_input_box_failure(frame) is None
    assert tp.pane_input_box_present(frame) is True


def test_prose_ghost_classifies_none_on_both_paths() -> None:
    """A PROSE ghost was always benign (no leg-5/leg-2 shape) — documents that
    only slash/@/numbered ghosts ever wedged."""
    frame = _frame(_GHOST_PROSE)
    assert tp.classify_input_box_failure(tp._strip_ansi(frame)) is None
    assert tp.classify_input_box_failure(frame) is None


def test_incident_row_verbatim_gate_passes() -> None:
    """The exact @7 incident bytes, wrapped in a minimal real frame: the naive
    strip returns ``completion_overlay`` (the wedge), the fix returns ``None``."""
    rule = "─" * 50
    frame = f"{rule}\n{_INCIDENT_ROW}\n{rule}\n  ? for shortcuts\n"
    assert tp.classify_input_box_failure(tp._strip_ansi(frame)) == "completion_overlay"
    assert tp.classify_input_box_failure(frame) is None
    assert tp.pane_input_box_present(frame) is True


def test_pane_input_row_empty_true_on_ghost_frames() -> None:
    """The stranded-draft brake's release probe already ghost-cleans internally —
    a ghost frame reports EMPTY (True), so the brake can self-heal."""
    for name in (_GHOST_PROSE, _GHOST_SLASH, _GHOST_AT, _GHOST_NUMBERED):
        assert tp.pane_input_row_empty(_frame(name)) is True, name


# ── Fail-closed pins (a normal-intensity draft is NEVER blanked) ─────────────


def test_real_normal_intensity_draft_is_never_blanked() -> None:
    """A REAL current-version (2.1.217) normal-intensity typed draft stays a
    draft — the SGR-2 discriminator only blanks a FULLY-dim row."""
    frame = _frame(_REAL_DRAFT)
    assert tp.pane_input_row_empty(frame) is False  # a genuine draft, not empty
    # The ghost cleaner leaves it byte-identical to a plain strip.
    assert tp.clean_ghost_input_text(frame) == tp._strip_ansi(frame)


def test_real_co_draft_ansi_still_refuses_completion_overlay() -> None:
    """GH #53 narrowing intact — a real ``/co`` draft at NORMAL intensity still
    arms the completion overlay and is refused at the pre-write gate, now also
    exercised under an ANSI normal-intensity rendering."""
    frame = _replace_input_row(_frame(_GHOST_SLASH), "\x1b[39m❯\xa0/co\x1b[0m")
    assert tp.classify_input_box_failure(frame) == "completion_overlay"
    # The fix does not touch a normal-intensity row: same as the strip path.
    assert tp.classify_input_box_failure(tp._strip_ansi(frame)) == "completion_overlay"


def test_dim_normal_mix_row_is_left_untouched() -> None:
    """A dim/normal MIX (``/cl`` dim, ``ear`` normal) is NOT a ghost — it must be
    left as ``/clear`` and refused, byte-identical to the strip path."""
    frame = _replace_input_row(
        _frame(_GHOST_SLASH), "\x1b[39m❯\xa0\x1b[2m/cl\x1b[0mear\x1b[0m"
    )
    assert tp.clean_ghost_input_text(frame) == tp._strip_ansi(frame)
    assert tp.classify_input_box_failure(frame) == "completion_overlay"


def test_slash_exact_reverify_exemption_stays_green() -> None:
    """The re-verify's exact-payload ``/`` exemption is unchanged — a bare
    ``/clear`` payload in the box is authorized (leg 5 exempt)."""
    frame = pane_fixture("inputbox_slash_exact_clear_v2.1.207.txt")
    assert (
        tp.classify_input_box_failure(
            frame, allow_slash_completion=True, expected_draft="/clear"
        )
        is None
    )


# ── Corpus non-regression (Codex P2 shape: classification equivalence) ───────


def test_existing_corpus_classification_is_equivalent_under_ghost_clean() -> None:
    """For EVERY pre-existing fixture, the new ghost-clean path classifies exactly
    as the old ``_strip_ansi`` path — the correct equivalence claim (Codex
    replayed this across all 127 fixtures with zero mismatches). The five GH #60
    fixtures are excluded: they deliberately FLIP."""
    from tests.conftest import _FIXTURES_DIR

    for path in sorted(_FIXTURES_DIR.glob("*.txt")):
        if path.name in _GH60_FIXTURES:
            continue
        text = path.read_text()
        assert tp.classify_input_box_failure(text) == tp.classify_input_box_failure(
            tp._strip_ansi(text)
        ), path.name


# The explicitly NON-ghost set (no fully-dim input row): ``clean_ghost_input_text``
# must be byte-identical to a bare ``_strip_ansi`` here (no blanking occurs).
_NON_GHOST_BYTE_EQ = (
    _REAL_DRAFT,
    "idle_real_draft_input_row_v2.1.206.txt",
    "inputbox_idle_v2.1.207.txt",
    "inputbox_busy_tool_v2.1.207.txt",
    "auq_single_picker_v2.1.207.txt",
)


@pytest.mark.parametrize("name", _NON_GHOST_BYTE_EQ)
def test_clean_ghost_is_byte_identical_to_strip_on_nonghost_fixtures(name: str) -> None:
    text = pane_fixture(name)
    assert tp.clean_ghost_input_text(text) == tp._strip_ansi(text)


# ── The transaction: real deliver_to_window over an ANSI ghost frame ─────────


@pytest.mark.asyncio
async def test_slash_ghost_pre_write_frame_delivers(monkeypatch) -> None:
    """End-to-end wedge repro: a pre-write gate that captures the ANSI slash-ghost
    frame must PASS (ghost → empty box), write the payload, and — with a
    normal-intensity re-verify frame — press Enter. Pre-fix the pre-write gate
    refused ``completion_overlay`` and nothing was ever written."""
    reverify = _replace_input_row(
        _frame(_GHOST_SLASH), "\x1b[39m❯\xa0go ahead and refactor\x1b[0m"
    )
    pane = _wire(monkeypatch, _Pane([_frame(_GHOST_SLASH), reverify]))

    result = await session_manager.deliver_to_window("@1", "go ahead and refactor")

    assert result.ok
    assert pane.written == ["go ahead and refactor"]
    assert pane.committed
    # Both captures requested the ANSI form (the classifier needs the SGR bytes).
    assert pane.with_ansi_calls and all(pane.with_ansi_calls)


@pytest.mark.asyncio
async def test_at_word_reverify_hazard_still_refuses(monkeypatch, caplog) -> None:
    """Companion: the @-arm's REAL hazard is preserved under ANSI feeds. The
    pre-write ghost frame passes, the payload is written, but a re-verify frame
    whose row carries a trailing ``@word`` at NORMAL intensity refuses — the
    public pair is ``DRAFT_WRITTEN`` + ``REASON_REVERIFY_FAILED`` (the
    ``completion_overlay`` leg surfaces only in the re-verify WARNING log)."""
    reverify = _replace_input_row(
        _frame(_GHOST_SLASH), "\x1b[39m❯\xa0please ask @se\x1b[0m"
    )
    pane = _wire(monkeypatch, _Pane([_frame(_GHOST_SLASH), reverify]))

    with caplog.at_level(logging.WARNING, logger="cctelegram.session"):
        result = await session_manager.deliver_to_window("@1", "please ask @se")

    assert result.outcome is delivery.DeliveryOutcome.DRAFT_WRITTEN
    assert result.reason == delivery.REASON_REVERIFY_FAILED
    assert pane.written == ["please ask @se"]  # written, but Enter withheld
    assert not pane.committed
    assert "leg=completion_overlay" in caplog.text
