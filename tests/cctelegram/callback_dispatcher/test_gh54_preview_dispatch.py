"""GH #54 W4 — the pick dispatch transaction works on a chevron-less preview.

Drives ``_dispatch_pick_pane_locked`` (the nav→verify→Enter→confirm keystroke
transaction) against a STATEFUL fake tmux that serves the REAL 2.1.207 wraplabels
ANSI fixtures (a preview picker with NO ``❯`` glyph — the cursor is proven ONLY
by SGR bold+153). This pins the W2 capture-spine value end-to-end at the dispatch
seam:

  * the nav delta comes from a REAL parsed cursor (tier-2 SGR), observed via the
    ANSI pair inside the lock — never the synthetic option-1 fallback;
  * the post-nav verify accepts the wrap-canonical label (the ``_loose_label_match``
    callsite passes ``vc.wrap_canonical``), so a lossy wrapped label still
    dispatches;
  * a no-real-cursor step fails closed to ``not_advanced``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import cctelegram.terminal_parser as tp
from cctelegram.callback_dispatcher import interactive as cbi

_FIX = Path(__file__).parents[1] / "fixtures"
_WID = "@prev"


def _ansi(name: str) -> str:
    return (_FIX / name).read_text()


def _plain(ansi: str) -> str:
    cap = tp.normalize_capture(ansi)
    assert cap is not None
    return cap.plain


class _StatefulFake:
    """Cursor-aware fake: Down advances cursor1→cursor2, Enter resolves."""

    def __init__(self, frames: list[str]):
        self._frames = frames  # ANSI frames; last is the resolved (no picker)
        self._idx = 0

    async def capture_pane(self, window_id, with_ansi=False, scrollback_lines=0):
        del scrollback_lines
        frame = self._frames[self._idx]
        return frame if with_ansi else _plain(frame)

    async def send_keys(self, window_id, key, *, enter=False, literal=False):
        del window_id, enter, literal
        if key in ("Down", "Up") and self._idx == 0:
            self._idx = 1  # nav to cursor2
        elif key == "Enter":
            self._idx = 2  # commit → resolved
        return True


@pytest.mark.asyncio
async def test_preview_wrap_pick_dispatches_via_sgr_cursor():
    c1 = _ansi("auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt")
    c2 = _ansi("auq_preview_wraplabels_cursor2_v2.1.207.ansi.txt")
    resolved = "user@host repo % \n"

    # The form the button minted from (parsed WITH ANSI → cursor=1).
    current_form = tp.parse_ask_user_question(_plain(c1), ansi_text=c1)
    assert current_form is not None
    assert next(o.number for o in current_form.options if o.cursor) == 1

    # Option 2's AUTHORITY label = its wrap_canonical (no-space): this differs
    # from the pane's lossy space-joined label, so a match can ONLY come from the
    # wrap-canonical leg — proving the dispatch verify passes vc.wrap_canonical.
    opt2 = current_form.options[1]
    authority_label = opt2.wrap_canonical
    assert authority_label  # preview options carry it

    fake = _StatefulFake([c1, c2, resolved])
    outcome = await cbi._dispatch_pick_pane_locked(
        user=SimpleNamespace(id=1),
        tmux_manager=fake,
        w=SimpleNamespace(window_id=_WID),
        window_id=_WID,
        fingerprint=current_form.fingerprint(),
        option_number=2,
        option_label=authority_label,
        is_review_submit=False,
        current_form=current_form,
        ledger_key=None,
    )
    assert outcome.kind == "dispatched", (outcome.kind, outcome.reason)
    assert fake._idx == 2  # nav'd then committed


@pytest.mark.asyncio
async def test_preview_pick_no_real_cursor_fails_closed():
    # The fake never advances the cursor (stuck on cursor1) — a tap on option 2
    # can never verify the cursor onto the target → not_advanced (fail-closed,
    # never a wrong Enter).
    c1 = _ansi("auq_preview_wraplabels_cursor1_v2.1.207.ansi.txt")
    current_form = tp.parse_ask_user_question(_plain(c1), ansi_text=c1)
    assert current_form is not None

    class _StuckFake(_StatefulFake):
        async def send_keys(self, window_id, key, *, enter=False, literal=False):
            del window_id, key, enter, literal
            return True  # accept the key but NEVER move the cursor

    fake = _StuckFake([c1, c1, c1])
    outcome = await cbi._dispatch_pick_pane_locked(
        user=SimpleNamespace(id=1),
        tmux_manager=fake,
        w=SimpleNamespace(window_id=_WID),
        window_id=_WID,
        fingerprint=current_form.fingerprint(),
        option_number=2,
        option_label=current_form.options[1].wrap_canonical,
        is_review_submit=False,
        current_form=current_form,
        ledger_key=None,
    )
    assert outcome.kind == "not_advanced", (outcome.kind, outcome.reason)
