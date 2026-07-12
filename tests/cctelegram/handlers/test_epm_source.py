"""GH #50 PR-2 r3 — the ExitPlanMode PreToolUse side-file trust boundary.

``handlers/epm_source.py`` exists to answer ONE question the plan artifact
cannot: *which ExitPlanMode prompt is this?* The round-3 P1 was that the old
anchor (a hash of the plan FILE's content, read from the live footer) answers a
DIFFERENT question — it identifies the artifact, not the occurrence — and the two
diverge exactly when it matters: the successor prompt REWRITES THE SAME PATH, so
a read taken after the swap returns the successor's hash while the executor still
holds the predecessor's pane. Every EPM renders the same three real options, so
the pane component matches too, and the Enter commits card A's feedback onto card
B — whose option 1 is "Yes, and bypass permissions".

RIG-VERIFIED on CC 2.1.207 (three consecutive ExitPlanMode prompts in a scratch
``tmux -L ccrig`` session):

  * ``PreToolUse`` DOES fire for ``ExitPlanMode``, ``tool_input = {plan,
    planFilePath}``, with a per-invocation ``tool_use_id``
    (``toolu_01FfhZ…`` / ``toolu_01GwV2…`` / ``toolu_014ef4…`` — all distinct);
  * ``planFilePath`` was byte-identical across all THREE prompts and the file was
    rewritten in place each time. The slug is a per-SESSION name, reused even
    across substantively different plans;
  * ``TMUX_PANE`` is exported to the hook, so the record carries a ``window_key``
    and the read can HARD-predicate on it (strictly stronger than the AUQ lane,
    whose session-keyed record cannot tell two ``--resume`` siblings apart).

These tests pin the trust boundary: the window-key predicate, schema validation,
future-skew rejection, the tool_use_id occurrence anchor (and its defensive
composite fallback), the teardown unlinks, and the 24h GC with the injected
``is_live_session`` conservative-skip.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cctelegram.handlers import epm_source
from cctelegram.session import WindowState, session_manager
from cctelegram.tmux_manager import tmux_manager

_SID = "550e8400-e29b-41d4-a716-4466554400aa"
_SID_B = "550e8400-e29b-41d4-a716-4466554400bb"
_WID = "@7"
_OTHER_WID = "@8"


@pytest.fixture
def _cc_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    yield tmp_path


@pytest.fixture
def _bound_window():
    session_manager.window_states[_WID] = WindowState(cwd="/tmp/x", session_id=_SID)
    yield
    session_manager.window_states.pop(_WID, None)


def _window_key(window_id: str = _WID) -> str:
    return f"{tmux_manager.session_name}:{window_id}"


def _write_record(
    cc_dir: Path,
    session_id: str = _SID,
    *,
    tool_use_id: str = "toolu_01FfhZG6H6fRzTyJZL5P5qoF",
    window_key: str | None = None,
    written_at: float | None = None,
    plan_file_path: str = "/Users/x/.claude/plans/read-app-py-logical-pumpkin.md",
    plan_fingerprint: str = "0a1620b6f022f898",
    schema: int = 1,
) -> Path:
    d = cc_dir / "epm_pending"
    d.mkdir(mode=0o700, exist_ok=True)
    rec = {
        "schema_version": schema,
        "session_id": session_id,
        "tool_use_id": tool_use_id,
        "window_key": _window_key() if window_key is None else window_key,
        "written_at": time.time() if written_at is None else written_at,
        "plan_file_path": plan_file_path,
        "plan_fingerprint": plan_fingerprint,
    }
    p = d / f"{session_id}.json"
    p.write_text(json.dumps(rec))
    return p


class TestOccurrenceAnchor:
    def test_the_anchor_is_the_per_invocation_tool_use_id(self, _cc_dir, _bound_window):
        _write_record(_cc_dir, tool_use_id="toolu_ABC")
        assert epm_source.peek_surface_identity_for_window(_WID) == "epm:tu:toolu_ABC"

    def test_a_successor_prompt_on_the_SAME_plan_path_yields_a_DIFFERENT_anchor(
        self, _cc_dir, _bound_window
    ):
        """THE ROUND-3 P1, at the anchor.

        The rig proved the plan PATH (and therefore anything derived from the
        file at that path) is reused verbatim across prompts. Only the hook's
        occurrence id separates them.
        """
        same_path = "/Users/x/.claude/plans/read-app-py-logical-pumpkin.md"
        _write_record(
            _cc_dir,
            tool_use_id="toolu_01FfhZG6H6fRzTyJZL5P5qoF",
            plan_file_path=same_path,
            plan_fingerprint="0a1620b6f022f898",  # plan A
        )
        first = epm_source.peek_surface_identity_for_window(_WID)

        # Claude re-plans: the hook re-fires, SAME path, new content, NEW id.
        _write_record(
            _cc_dir,
            tool_use_id="toolu_01GwV29afXsN7j3biBdEx1iB",
            plan_file_path=same_path,
            plan_fingerprint="7d76cee3fd4cd16c",  # plan B
        )
        second = epm_source.peek_surface_identity_for_window(_WID)

        assert first != second

    def test_composite_fallback_when_the_hook_captured_no_tool_use_id(
        self, _cc_dir, _bound_window
    ):
        _write_record(_cc_dir, tool_use_id="", written_at=1783843329.45639)
        anchor = epm_source.peek_surface_identity_for_window(_WID)
        assert anchor is not None
        assert anchor.startswith("epm:wf:")
        assert "0a1620b6f022f898" in anchor

    def test_no_side_file_is_None(self, _cc_dir, _bound_window):
        assert epm_source.peek_surface_identity_for_window(_WID) is None

    def test_no_session_for_the_window_is_None(self, _cc_dir):
        _write_record(_cc_dir)
        assert epm_source.peek_surface_identity_for_window("@999") is None


class TestHardWindowKeyPredicate:
    def test_a_sibling_windows_record_never_lights_this_window(
        self, _cc_dir, _bound_window
    ):
        """The double-``--resume`` guard: two windows, ONE session id.

        The side file is session-keyed, so a sibling's record sits at the very
        path this window reads. Only the hook-captured ``window_key`` tells them
        apart — and on a bypass-permissions surface a session-keyed match alone
        is forbidden.
        """
        _write_record(_cc_dir, window_key=_window_key(_OTHER_WID))
        assert epm_source.peek_surface_identity_for_window(_WID) is None

    def test_an_empty_window_key_is_refused(self, _cc_dir, _bound_window):
        _write_record(_cc_dir, window_key="")
        assert epm_source.peek_surface_identity_for_window(_WID) is None

    def test_a_missing_window_key_is_refused(self, _cc_dir, _bound_window):
        d = _cc_dir / "epm_pending"
        d.mkdir(mode=0o700, exist_ok=True)
        (d / f"{_SID}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": _SID,
                    "tool_use_id": "toolu_X",
                    "written_at": time.time(),
                }
            )
        )
        assert epm_source.peek_surface_identity_for_window(_WID) is None


class TestValidation:
    def test_unknown_schema_version_is_refused(self, _cc_dir, _bound_window):
        _write_record(_cc_dir, schema=99)
        assert epm_source.peek_surface_identity_for_window(_WID) is None

    def test_malformed_json_is_refused(self, _cc_dir, _bound_window):
        d = _cc_dir / "epm_pending"
        d.mkdir(mode=0o700, exist_ok=True)
        (d / f"{_SID}.json").write_text("{not json")
        assert epm_source.peek_surface_identity_for_window(_WID) is None

    def test_future_skewed_write_is_refused(self, _cc_dir, _bound_window):
        _write_record(_cc_dir, written_at=time.time() + 600)
        assert epm_source.peek_surface_identity_for_window(_WID) is None

    def test_there_is_NO_read_ttl_a_long_open_card_is_still_that_card(
        self, _cc_dir, _bound_window
    ):
        """Identity does not expire. A plan card left open for hours is still
        the same plan card, and its occurrence id is still the truth."""
        _write_record(_cc_dir, tool_use_id="toolu_OLD", written_at=time.time() - 86_400)
        assert epm_source.peek_surface_identity_for_window(_WID) == "epm:tu:toolu_OLD"

    def test_a_non_uuid_session_id_never_builds_a_path(self, _cc_dir):
        session_manager.window_states[_WID] = WindowState(
            cwd="/tmp/x", session_id="../../etc/passwd"
        )
        try:
            assert epm_source.peek_surface_identity_for_window(_WID) is None
        finally:
            session_manager.window_states.pop(_WID, None)


class TestLifecycle:
    def test_forget_for_window_unlinks_the_current_sessions_file(
        self, _cc_dir, _bound_window
    ):
        p = _write_record(_cc_dir)
        assert p.exists()
        epm_source.forget_for_window(_WID)
        assert not p.exists()

    def test_unlink_for_session_is_the_old_session_id_seam(self, _cc_dir):
        p = _write_record(_cc_dir, session_id=_SID_B)
        epm_source.unlink_for_session(_SID_B)
        assert not p.exists()

    def test_unlink_is_best_effort_on_a_missing_file(self, _cc_dir):
        epm_source.unlink_for_session(_SID_B)  # must not raise


class TestGcStale:
    def _age(self, path: Path, seconds: float) -> None:
        old = time.time() - seconds
        import os

        os.utime(path, (old, old))

    def test_reaps_a_stale_orphan(self, _cc_dir):
        p = _write_record(_cc_dir)
        self._age(p, 25 * 3600)
        assert epm_source.gc_stale() == 1
        assert not p.exists()

    def test_keeps_a_fresh_file(self, _cc_dir):
        p = _write_record(_cc_dir)
        assert epm_source.gc_stale() == 0
        assert p.exists()

    def test_a_LIVE_sessions_stale_file_is_KEPT(self, _cc_dir):
        """Claude buffers the ExitPlanMode tool_use in JSONL until the prompt
        resolves, so a plan card left open >24h has a stale-mtime side file that
        is STILL the only witness of which card it is."""
        p = _write_record(_cc_dir)
        self._age(p, 25 * 3600)
        assert epm_source.gc_stale(is_live_session=lambda sid: sid == _SID) == 0
        assert p.exists()

    def test_a_raising_predicate_conservatively_SKIPS(self, _cc_dir):
        p = _write_record(_cc_dir)
        self._age(p, 25 * 3600)

        def boom(_sid: str) -> bool:
            raise RuntimeError("predicate exploded")

        assert epm_source.gc_stale(is_live_session=boom) == 0
        assert p.exists()

    def test_no_dir_is_a_no_op(self, _cc_dir):
        assert epm_source.gc_stale() == 0
