"""The kill bound must cover the worst-case LAWFUL lifecycle-lock hold.

GH #65 review r14 P1-D. ``kill_window`` waits a bounded time to ACQUIRE the
window-lifecycle lock and then reports an honest failure. That bound is only
meaningful if it exceeds how long a LEGITIMATE holder can hold the lock — and a
single acquisition may run several bounded tmux operations back to back, so the
ceiling is (ops under one hold) x LIFECYCLE_TMUX_TIMEOUT_S, not one per-op
bound. Set below that, kills would fail during ordinary busy creation, which is
the opposite of what the bound is for.

The pairwise assertion this replaces (bound > one per-op timeout) was satisfied
by a value that did NOT cover two sequential operations.

Rather than restate the constant, this recomputes the worst case FROM THE
SOURCE, so a future bounded await added under an existing hold fails here
instead of silently shrinking the kill's effective safety margin.
"""

from __future__ import annotations

import re
from pathlib import Path

from cctelegram import tmux_manager as tmux_mod

_SRC_ROOT = Path(tmux_mod.__file__).parent
_ACQUIRE_RE = re.compile(r"^(\s*)async with .*window_lifecycle_lock\(\)")


def _max_bounded_ops_under_one_hold() -> tuple[int, str]:
    """Largest number of ``_bounded_lifecycle`` awaits inside one hold."""
    worst = 0
    worst_site = "<none>"
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
                # The hold ends at the first line indented at or above the
                # ``async with`` itself.
                if len(follow) - len(follow.lstrip()) <= indent:
                    break
                if "_bounded_lifecycle(" in follow:
                    count += 1
            if count > worst:
                worst = count
                worst_site = f"{path.name}:{idx + 1}"
    return worst, worst_site


def test_every_bounded_await_under_a_hold_is_accounted_for() -> None:
    observed, site = _max_bounded_ops_under_one_hold()
    assert observed > 0, "the source scan found no bounded awaits — check the regex"
    assert observed <= tmux_mod._MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD, (
        f"{site} performs {observed} bounded operations under ONE lifecycle-lock "
        f"acquisition, but _MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD is "
        f"{tmux_mod._MAX_BOUNDED_OPS_PER_LIFECYCLE_HOLD}. Update the constant — "
        "the kill bound is derived from it."
    )


def test_the_kill_bound_covers_the_worst_case_cumulative_hold() -> None:
    observed, site = _max_bounded_ops_under_one_hold()
    worst_case_hold = observed * tmux_mod.LIFECYCLE_TMUX_TIMEOUT_S
    assert tmux_mod.KILL_LOCK_TIMEOUT_S > worst_case_hold, (
        f"a kill waits {tmux_mod.KILL_LOCK_TIMEOUT_S}s for the lifecycle lock, "
        f"but {site} can lawfully hold it for {worst_case_hold}s "
        f"({observed} x {tmux_mod.LIFECYCLE_TMUX_TIMEOUT_S}s) — kills would fail "
        "during an ordinary busy creation"
    )
