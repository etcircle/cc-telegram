"""Unit pins for GH #67 — interactive-card churn (send→delete→resend loop).

Covers the plan's unit matrix:

  8.  ``_InteractiveMsgMeta`` round-trip with and without the two new
      provenance fields; an OLD state-file shape loads them as ``None``.
  9.  the ``surface_born_at`` re-stamp rule in ``_refresh_interactive_msg_meta``
      (same surface preserves, kind change / AUQ identity change re-stamps,
      ``created_at`` untouched throughout).
  10. the hydrate window remap (@12 → @13) carries both new fields.
  11. the ``ClearCondition`` decision table — gate veto, stale veto, the
      matching-resolution bypass and every fail-OPEN permutation — exercised
      through ``clear_interactive_msg``, i.e. under the real route lock.
  12. ``NewMessage.timestamp`` plumbing at the PARENT emit site: a real parsed
      transcript entry always carries its ``timestamp`` through, so the
      timestamp-less shape the veto fails open on requires a parser regression.
"""

import json
import time
from pathlib import Path

import pytest

from cctelegram.handlers import auq_source
from cctelegram.handlers import interactive_ui as iui
from cctelegram.session_monitor import SessionInfo, SessionMonitor
from cctelegram.utils import app_dir


_USER = 7
_THREAD = 42
_WID = "@3"
_BORN = "2026-08-24T12:00:00+00:00"
_BEFORE_BORN = "2026-08-24T11:59:00+00:00"
_AFTER_BORN = "2026-08-24T12:01:00+00:00"


@pytest.fixture
def iui_state(tmp_path, monkeypatch):
    """Redirect ``interactive_state.json`` to tmp_path and clear module state.

    Mirrors ``test_interactive_ui._isolated_interactive_state_file`` — the
    persistence write-through fires on every meta mutation these tests make.
    """
    state_file = tmp_path / "interactive_state.json"
    monkeypatch.setattr(iui, "_interactive_state_file_path", lambda: state_file)
    iui.reset_for_tests()
    yield state_file
    iui.reset_for_tests()


def _seed_surface(
    *,
    surface_kind: str | None,
    surface_born_at: str | None,
    tool_use_id: str | None = None,
    msg_id: int = 555,
) -> None:
    """Publish a card exactly as ``_set_interactive_msg`` leaves it, then pin
    the provenance fields to the values under test."""
    ikey = (_USER, _THREAD)
    iui._interactive_msgs[ikey] = msg_id
    iui._interactive_mode[ikey] = _WID
    iui._interactive_msg_meta[ikey] = iui._InteractiveMsgMeta(
        msg_id=msg_id,
        window_id=_WID,
        session_id="sess-1",
        tool_use_id=tool_use_id,
        created_at="2026-08-24T09:00:00+00:00",
        surface_kind=surface_kind,
        surface_born_at=surface_born_at,
    )


async def _clear(cond: iui.ClearCondition | None) -> bool:
    """Run the real clear (bot=None ⇒ no Telegram I/O, lock still held)."""
    return await iui.clear_interactive_msg(_USER, None, _THREAD, clear_condition=cond)


# ── 8. meta round-trip ────────────────────────────────────────────────────


class TestInteractiveMsgMetaRoundTrip:
    def test_round_trip_with_new_fields(self):
        rec = iui._InteractiveMsgMeta(
            msg_id=42,
            window_id="@5",
            session_id="sess-1",
            tool_use_id="toolu_a",
            created_at="2026-08-24T09:00:00+00:00",
            surface_kind="AskUserQuestion",
            surface_born_at=_BORN,
        )
        back = iui._InteractiveMsgMeta.from_dict(json.loads(json.dumps(rec.to_dict())))
        assert back == rec

    def test_round_trip_without_new_fields(self):
        rec = iui._InteractiveMsgMeta(
            msg_id=42,
            window_id="@5",
            session_id="sess-1",
            tool_use_id=None,
            created_at="2026-08-24T09:00:00+00:00",
        )
        assert rec.surface_kind is None
        assert rec.surface_born_at is None
        back = iui._InteractiveMsgMeta.from_dict(json.loads(json.dumps(rec.to_dict())))
        assert back == rec

    def test_old_state_file_shape_loads_with_none_defaults(self):
        """The pre-#67 on-disk shape (no surface_* keys) must still load."""
        back = iui._InteractiveMsgMeta.from_dict(
            {
                "msg_id": 42,
                "window_id": "@5",
                "session_id": "sess-1",
                "tool_use_id": "toolu_a",
                "created_at": "2026-08-24T09:00:00+00:00",
            }
        )
        assert back is not None
        assert back.surface_kind is None
        assert back.surface_born_at is None

    def test_non_string_new_fields_normalize_to_none(self):
        back = iui._InteractiveMsgMeta.from_dict(
            {
                "msg_id": 42,
                "window_id": "@5",
                "session_id": "sess-1",
                "tool_use_id": None,
                "created_at": "x",
                "surface_kind": 17,
                "surface_born_at": "",
            }
        )
        assert back is not None
        assert back.surface_kind is None
        assert back.surface_born_at is None


# ── 9. the re-stamp rule ──────────────────────────────────────────────────


class TestSurfaceBornAtRestampRule:
    def test_same_kind_preserves_born_at_and_created_at(self, iui_state):
        ikey = (_USER, _THREAD)
        iui._set_interactive_msg(
            ikey,
            msg_id=1,
            window_id=_WID,
            session_id="s",
            tool_use_id=None,
            surface_kind="AskUserQuestion",
        )
        first = iui._interactive_msg_meta[ikey]
        # A Q2 → Q3 advance inside ONE AUQ: same kind, no known id change.
        iui._refresh_interactive_msg_meta(
            ikey,
            msg_id=1,
            window_id=_WID,
            session_id="s",
            tool_use_id=None,
            surface_kind="AskUserQuestion",
        )
        after = iui._interactive_msg_meta[ikey]
        assert after.surface_born_at == first.surface_born_at
        assert after.created_at == first.created_at

    def test_kind_change_restamps_and_keeps_created_at(self, iui_state):
        ikey = (_USER, _THREAD)
        iui._set_interactive_msg(
            ikey,
            msg_id=1,
            window_id=_WID,
            session_id="s",
            tool_use_id=None,
            surface_kind="Permission",
        )
        first = iui._interactive_msg_meta[ikey]
        iui._refresh_interactive_msg_meta(
            ikey,
            msg_id=1,
            window_id=_WID,
            session_id="s",
            tool_use_id=None,
            surface_kind="AskUserQuestion",
        )
        after = iui._interactive_msg_meta[ikey]
        assert after.surface_kind == "AskUserQuestion"
        assert after.surface_born_at != first.surface_born_at
        assert after.created_at == first.created_at

    def test_auq_identity_change_restamps(self, iui_state):
        ikey = (_USER, _THREAD)
        iui._set_interactive_msg(
            ikey,
            msg_id=1,
            window_id=_WID,
            session_id="s",
            tool_use_id="toolu_a",
            surface_kind="AskUserQuestion",
        )
        first = iui._interactive_msg_meta[ikey]
        iui._refresh_interactive_msg_meta(
            ikey,
            msg_id=1,
            window_id=_WID,
            session_id="s",
            tool_use_id="toolu_b",
            surface_kind="AskUserQuestion",
        )
        after = iui._interactive_msg_meta[ikey]
        assert after.tool_use_id == "toolu_b"
        assert after.surface_born_at != first.surface_born_at

    def test_unknown_refresh_id_preserves_stamp_and_stored_id(self, iui_state):
        """A late-revealed id must not be erased by a refresh that can't see it
        (Claude Code buffers the AUQ tool_use in JSONL until the answer)."""
        ikey = (_USER, _THREAD)
        iui._set_interactive_msg(
            ikey,
            msg_id=1,
            window_id=_WID,
            session_id="s",
            tool_use_id="toolu_a",
            surface_kind="AskUserQuestion",
        )
        first = iui._interactive_msg_meta[ikey]
        iui._refresh_interactive_msg_meta(
            ikey,
            msg_id=1,
            window_id=_WID,
            session_id="s",
            tool_use_id=None,
            surface_kind="AskUserQuestion",
        )
        after = iui._interactive_msg_meta[ikey]
        assert after.tool_use_id == "toolu_a"
        assert after.surface_born_at == first.surface_born_at

    def test_refresh_without_prior_meta_stamps(self, iui_state):
        ikey = (_USER, _THREAD)
        iui._refresh_interactive_msg_meta(
            ikey,
            msg_id=1,
            window_id=_WID,
            session_id="s",
            tool_use_id=None,
            surface_kind="ExitPlanMode",
        )
        assert iui._interactive_msg_meta[ikey].surface_born_at is not None

    def test_fresh_send_always_stamps(self, iui_state):
        ikey = (_USER, _THREAD)
        iui._set_interactive_msg(
            ikey,
            msg_id=1,
            window_id=_WID,
            session_id="s",
            tool_use_id=None,
            surface_kind="Decision",
        )
        rec = iui._interactive_msg_meta[ikey]
        assert rec.surface_kind == "Decision"
        assert rec.surface_born_at == rec.created_at


# ── 10. hydrate remap carries the new fields ──────────────────────────────


class TestHydrateCarriesProvenance:
    def test_window_remap_keeps_surface_kind_and_born_at(self, iui_state, monkeypatch):
        from unittest.mock import MagicMock

        from cctelegram.session import SessionManager

        iui_state.write_text(
            json.dumps(
                {
                    "interactive_msgs": {
                        "1:10": {
                            "msg_id": 42,
                            "window_id": "@12",
                            "session_id": "s",
                            "tool_use_id": None,
                            "created_at": "2026-08-24T09:00:00+00:00",
                            "surface_kind": "Permission",
                            "surface_born_at": _BORN,
                        }
                    },
                    "auq_context_posted": {},
                }
            )
        )
        sm = MagicMock(spec=SessionManager)
        sm.window_states = {"@13": object()}
        sm.resolve_window_for_thread = lambda uid, tid: "@13"
        monkeypatch.setattr(iui, "session_id_for_window", lambda wid: "s")

        iui.hydrate_interactive_state(sm)

        rec = iui._interactive_msg_meta[(1, 10)]
        assert rec.window_id == "@13"
        assert rec.surface_kind == "Permission"
        assert rec.surface_born_at == _BORN


# ── 11. the ClearCondition decision table (under the route lock) ──────────


@pytest.mark.asyncio
class TestClearConditionDecisionTable:
    @pytest.mark.parametrize("kind", ["Permission", "Workflow", "Decision"])
    async def test_gate_surface_is_never_cleared_by_a_parent_block(
        self, iui_state, kind
    ):
        """A pane-detected gate has NO transcript resolution event, so a
        non-interactive parent block proves nothing about it — the observed
        sustained-churn trigger."""
        _seed_surface(surface_kind=kind, surface_born_at=_BORN)
        cleared = await _clear(
            iui.ClearCondition(
                block_timestamp=_AFTER_BORN,
                content_type="text",
                tool_name=None,
                tool_use_id=None,
            )
        )
        assert cleared is False
        assert iui.has_interactive_surface(_USER, _THREAD) is True

    async def test_gate_veto_holds_even_for_a_tool_result(self, iui_state):
        _seed_surface(surface_kind="Decision", surface_born_at=_BORN)
        cleared = await _clear(
            iui.ClearCondition(
                block_timestamp=_AFTER_BORN,
                content_type="tool_result",
                tool_name="AskUserQuestion",
                tool_use_id="toolu_a",
            )
        )
        assert cleared is False

    async def test_stale_block_older_than_birth_does_not_clear(self, iui_state):
        _seed_surface(surface_kind="AskUserQuestion", surface_born_at=_BORN)
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=_BEFORE_BORN,
                    content_type="text",
                    tool_name=None,
                    tool_use_id=None,
                )
            )
            is False
        )
        assert iui.has_interactive_surface(_USER, _THREAD) is True

    async def test_fresh_block_clears(self, iui_state):
        _seed_surface(surface_kind="AskUserQuestion", surface_born_at=_BORN)
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=_AFTER_BORN,
                    content_type="text",
                    tool_name=None,
                    tool_use_id=None,
                )
            )
            is True
        )
        assert iui.has_interactive_surface(_USER, _THREAD) is False

    async def test_matching_auq_resolution_bypasses_the_stale_veto(self, iui_state):
        """The slow-send case: born_at is minted only after the awaited
        Telegram send, so a genuine tool_result can predate it."""
        _seed_surface(
            surface_kind="AskUserQuestion",
            surface_born_at=_BORN,
            tool_use_id="toolu_a",
        )
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=_BEFORE_BORN,
                    content_type="tool_result",
                    tool_name="AskUserQuestion",
                    tool_use_id="toolu_a",
                )
            )
            is True
        )

    async def test_stale_auq_resolution_takes_the_timestamp_veto(self, iui_state):
        """A tool_result PROVEN to belong to an older AUQ is not a resolution
        of the live surface."""
        _seed_surface(
            surface_kind="AskUserQuestion",
            surface_born_at=_BORN,
            tool_use_id="toolu_b",
        )
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=_BEFORE_BORN,
                    content_type="tool_result",
                    tool_name="AskUserQuestion",
                    tool_use_id="toolu_a",
                )
            )
            is False
        )

    async def test_auq_resolution_against_an_epm_surface_is_not_a_match(
        self, iui_state
    ):
        """The KIND-MATCH closes the replacement bypass: AUQ-A's result can
        never bypass the veto against an EPM that replaced A in place."""
        _seed_surface(surface_kind="ExitPlanMode", surface_born_at=_BORN)
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=_BEFORE_BORN,
                    content_type="tool_result",
                    tool_name="AskUserQuestion",
                    tool_use_id="toolu_a",
                )
            )
            is False
        )

    async def test_epm_resolution_matches_on_kind_alone(self, iui_state):
        """EPM stores no per-instance identity, so its parity is the
        kind-match alone — never compared against an AUQ id."""
        _seed_surface(
            surface_kind="ExitPlanMode",
            surface_born_at=_BORN,
            tool_use_id="toolu_other",
        )
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=_BEFORE_BORN,
                    content_type="tool_result",
                    tool_name="ExitPlanMode",
                    tool_use_id="toolu_epm",
                )
            )
            is True
        )

    async def test_legacy_kind_none_is_a_fail_open_kind_match(self, iui_state):
        _seed_surface(surface_kind=None, surface_born_at=_BORN)
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=_BEFORE_BORN,
                    content_type="tool_result",
                    tool_name="ExitPlanMode",
                    tool_use_id=None,
                )
            )
            is True
        )

    @pytest.mark.parametrize(
        "block_ts",
        [None, "", "not-a-timestamp", "2026-08-24T11:59:00"],  # last one is NAIVE
    )
    async def test_unusable_block_timestamp_fails_open(self, iui_state, block_ts):
        _seed_surface(surface_kind="AskUserQuestion", surface_born_at=_BORN)
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=block_ts,
                    content_type="text",
                    tool_name=None,
                    tool_use_id=None,
                )
            )
            is True
        )

    async def test_missing_born_at_fails_open(self, iui_state):
        _seed_surface(surface_kind="AskUserQuestion", surface_born_at=None)
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=_BEFORE_BORN,
                    content_type="text",
                    tool_name=None,
                    tool_use_id=None,
                )
            )
            is True
        )

    async def test_no_sidecar_at_all_fails_open(self, iui_state):
        """The shape the two sidechain regression pins seed: a card with no
        meta record must keep today's unconditional teardown."""
        iui._interactive_msgs[(_USER, _THREAD)] = 999
        iui._interactive_mode[(_USER, _THREAD)] = _WID
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=_BEFORE_BORN,
                    content_type="text",
                    tool_name=None,
                    tool_use_id=None,
                )
            )
            is True
        )

    async def test_condition_none_is_unconditional(self, iui_state):
        """Every other caller (poller tombstone, /clear, teardown, dispatchers)
        keeps byte-identical behavior."""
        _seed_surface(surface_kind="Permission", surface_born_at=_BORN)
        assert await _clear(None) is True
        assert iui.has_interactive_surface(_USER, _THREAD) is False

    async def test_vetoed_clear_mutates_nothing(self, iui_state):
        _seed_surface(surface_kind="Permission", surface_born_at=_BORN)
        iui._auq_context_posted[_WID] = "form:abc"
        await _clear(
            iui.ClearCondition(
                block_timestamp=_AFTER_BORN,
                content_type="text",
                tool_name=None,
                tool_use_id=None,
            )
        )
        assert iui._interactive_msg_meta.get((_USER, _THREAD)) is not None
        assert iui._interactive_mode.get((_USER, _THREAD)) == _WID
        assert iui._auq_context_posted.get(_WID) == "form:abc"

    async def test_proceeding_clear_forgets_inside_the_transaction(self, iui_state):
        """The conditional forget moved INSIDE the locked Phase-1 (r3 P2-3)."""
        _seed_surface(surface_kind="AskUserQuestion", surface_born_at=_BORN)
        iui._auq_context_posted[_WID] = "form:abc"
        iui._last_completed_ask_tool_input[_WID] = {"questions": []}
        assert (
            await _clear(
                iui.ClearCondition(
                    block_timestamp=_AFTER_BORN,
                    content_type="text",
                    tool_name=None,
                    tool_use_id=None,
                )
            )
            is True
        )
        assert iui._auq_context_posted.get(_WID) is None
        assert iui._last_completed_ask_tool_input.get(_WID) is None


# ── remove_side_file_if_id: the re-read-before-unlink guard ───────────────


class TestRemoveSideFileIfId:
    """The identity guard is a RE-READ at call time, not a cached decision.

    The window it narrows is the cross-PROCESS one against the PreToolUse
    hook's atomic rename (disclosed, not closed) — so the helper must read the
    stored id ITSELF immediately before unlinking, never trust a caller's
    earlier peek.
    """

    _SESSION = "77777777-7777-4777-8777-777777777777"

    def _write(self, tool_use_id: str) -> Path:
        pending = app_dir() / "auq_pending"
        pending.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = pending / f"{self._SESSION}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": self._SESSION,
                    "tool_use_id": tool_use_id,
                    "written_at": time.time(),
                    "tool_input": {
                        "questions": [
                            {
                                "question": "Q?",
                                "multiSelect": False,
                                "options": [{"label": "One", "description": "d"}],
                            }
                        ]
                    },
                }
            )
        )
        return path

    def test_unlinks_on_a_matching_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        path = self._write("toolu_A")
        assert auq_source.remove_side_file_if_id(self._SESSION, "toolu_A") is True
        assert not path.exists()

    def test_id_replaced_on_disk_after_the_callers_read_is_not_unlinked(
        self, tmp_path, monkeypatch
    ):
        """The successor-protection case: the caller peeked A, the hook then
        atomically replaced the file with B's record. The re-read sees B and
        REFUSES — a blind unlink would delete the successor's provenance."""
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        path = self._write("toolu_A")
        peeked = auq_source.peek_side_file_tool_use_id(self._SESSION)
        assert peeked == "toolu_A"
        self._write("toolu_B")  # the hook, in another process

        assert auq_source.remove_side_file_if_id(self._SESSION, peeked or "") is False
        assert path.exists()
        assert auq_source.peek_side_file_tool_use_id(self._SESSION) == "toolu_B"

    def test_empty_stored_id_never_matches(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        path = self._write("")
        assert auq_source.remove_side_file_if_id(self._SESSION, "toolu_A") is False
        assert path.exists()

    def test_missing_file_and_empty_args_are_no_ops(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        assert auq_source.remove_side_file_if_id(self._SESSION, "toolu_A") is False
        assert auq_source.remove_side_file_if_id("", "toolu_A") is False
        assert auq_source.remove_side_file_if_id(self._SESSION, "") is False


# ── 12. NewMessage.timestamp plumbing at the PARENT emit site ─────────────


class TestNewMessageTimestampPlumbing:
    @pytest.mark.asyncio
    async def test_parent_emit_carries_entry_timestamp(
        self, tmp_path, make_jsonl_entry, make_text_block
    ):
        """Every real parsed entry carries its timestamp into ``NewMessage`` —
        so the timestamp-less shape the stale-block veto fails OPEN on requires
        a transcript-parser regression, not an ordinary transcript."""
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(
            "assistant",
            [make_text_block("hello world")],
            session_id="sid",
            timestamp="2026-08-24T12:00:00.000Z",
        )
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        monitor.register_session("sid", jsonl_file, offset=0)

        async def _scan(_active_ids=None):
            return [SessionInfo(session_id="sid", file_path=jsonl_file)]

        monitor.scan_projects = _scan  # type: ignore[method-assign]

        msgs = await monitor.check_for_updates({"sid"})

        assert len(msgs) == 1
        assert msgs[0].timestamp == "2026-08-24T12:00:00.000Z"
