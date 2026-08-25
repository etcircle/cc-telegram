"""The kill bound must cover the worst-case LAWFUL lifecycle-lock hold.

GH #65 review r14 P1-D / r15 P2-B. ``kill_window`` waits a bounded time to
ACQUIRE the window-lifecycle lock and then reports an honest failure. That bound
is only meaningful if it exceeds how long a LEGITIMATE holder can hold the lock —
and a single acquisition may run several bounded tmux operations back to back,
so the ceiling is (ops under one hold) x LIFECYCLE_TMUX_TIMEOUT_S.

The accounting is STRUCTURAL, not lexical. The wave-14 scanner counted only
``_bounded_lifecycle`` calls textually nested under an ``async with`` line, so it
could not see the trust acquisition's op (which happens inside
``_revalidate_bind_preconditions`` and therefore counted ZERO) and could not see
an aliased acquisition at all. ``_bounded_lifecycle`` now counts itself against
whatever hold is in force, and these tests DRIVE the real acquisition sites and
read the high-water mark.

The lexical scan is kept as a belt, downgraded to what it can honestly claim.
"""

from __future__ import annotations

import re
from pathlib import Path
import pytest

from cctelegram import tmux_manager as tmux_mod

_SRC_ROOT = Path(tmux_mod.__file__).parent
_ACQUIRE_RE = re.compile(r"^(\s*)async with .*window_lifecycle_lock\(\)")


def test_the_kill_bound_is_derived_from_the_declared_ceiling() -> None:
    worst_case_hold = (
        tmux_mod._MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD * tmux_mod.LIFECYCLE_TMUX_TIMEOUT_S
    )
    assert tmux_mod.KILL_LOCK_TIMEOUT_S > worst_case_hold, (
        f"a kill waits {tmux_mod.KILL_LOCK_TIMEOUT_S}s for the lifecycle lock, "
        f"but a lawful holder may keep it for {worst_case_hold}s — kills would "
        "fail during an ordinary busy creation"
    )


@pytest.mark.asyncio
async def test_bounded_ops_are_counted_wherever_they_happen() -> None:
    """An op reached THROUGH A HELPER counts like any other.

    This is the case the lexical scanner structurally could not see, and it is
    the one that matters: the trust seam's only bounded op lives inside
    ``_revalidate_bind_preconditions``.
    """
    mgr = tmux_mod.tmux_manager
    mgr.reset_lifecycle_lock_for_tests()
    mgr.reset_lifecycle_ops_accounting()

    async def _noop() -> str:
        return "done"

    async def _op_via_helper() -> None:
        # Deliberately NOT lexically inside the ``async with`` below.
        await mgr._bounded_lifecycle(_noop(), what="helper op")

    async with mgr.window_lifecycle_lock():
        await _op_via_helper()
        await mgr._bounded_lifecycle(_noop(), what="direct op")

    assert mgr.lifecycle_ops_high_water == 2, (
        "both ops under one hold must be counted — including the one reached "
        f"through a helper (got {mgr.lifecycle_ops_high_water})"
    )
    mgr.reset_lifecycle_ops_accounting()


@pytest.mark.asyncio
async def test_exceeding_the_declared_ceiling_is_loud() -> None:
    """A hold that grows past the ceiling invalidates the derived kill bound."""
    mgr = tmux_mod.tmux_manager
    mgr.reset_lifecycle_lock_for_tests()
    mgr.reset_lifecycle_ops_accounting()

    async def _noop() -> str:
        return "done"

    with pytest.raises(AssertionError, match="exceeds the declared ceiling"):
        async with mgr.window_lifecycle_lock():
            for _ in range(tmux_mod._MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD + 1):
                await mgr._bounded_lifecycle(_noop(), what="op")
    mgr.reset_lifecycle_ops_accounting()


@pytest.mark.asyncio
async def test_ops_outside_a_hold_are_not_counted() -> None:
    """The ceiling is about a HOLD, so an unheld op must not inflate it."""
    mgr = tmux_mod.tmux_manager
    mgr.reset_lifecycle_lock_for_tests()
    mgr.reset_lifecycle_ops_accounting()

    async def _noop() -> str:
        return "done"

    for _ in range(5):
        await mgr._bounded_lifecycle(_noop(), what="unheld op")

    assert mgr.lifecycle_ops_high_water == 0, (
        "operations outside a lifecycle-lock hold do not contribute to the "
        "worst-case hold"
    )


def test_the_lexical_scan_finds_no_more_than_the_ceiling() -> None:
    """A BELT, and honest about its limits.

    It can only see calls textually nested under an ``async with`` line — it
    cannot see through a helper, and it cannot see an aliased acquisition. It is
    kept because a textually-visible third op is still worth catching early; the
    runtime accounting above is the authority.
    """
    worst = 0
    site = "<none>"
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        lines = path.read_text().splitlines()
        for idx, line in enumerate(lines):
            match = _ACQUIRE_RE.match(line)
            if not match:
                continue
            indent = len(match.group(1))
            count = 0
            for follow in lines[idx + 1 :]:
                if not follow.strip():
                    continue
                if len(follow) - len(follow.lstrip()) <= indent:
                    break
                if "_bounded_lifecycle(" in follow:
                    count += 1
            if count > worst:
                worst, site = count, f"{path.name}:{idx + 1}"

    assert worst <= tmux_mod._MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD, (
        f"{site} textually performs {worst} bounded operations under ONE "
        f"lifecycle-lock acquisition, above the declared ceiling of "
        f"{tmux_mod._MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD}"
    )


def test_the_helper_reached_op_is_invisible_to_the_lexical_scan() -> None:
    """Pins WHY the runtime accounting exists, so the belt is never mistaken
    for the authority."""
    source = Path(tmux_mod.__file__).parent / "handlers" / "trust_flow.py"
    text = source.read_text()
    assert "_revalidate_bind_preconditions(flow, session_mgr)" in text
    assert "_bounded_lifecycle" in text, (
        "the trust seam does perform a bounded op — but inside the helper, "
        "which is exactly what a lexical scan cannot attribute to the hold"
    )
