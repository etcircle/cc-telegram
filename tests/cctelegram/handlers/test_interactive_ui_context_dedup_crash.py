"""Wave 1 crash-recovery tests for the two-phase context-post gate.

Plan v4 §5.3 (``temp/2026-05-25-auq-followups-master-plan-v4.md``).

The pre-Wave-1 single-phase ``claim_auq_context_post`` persisted the
dedup marker the moment a claim succeeded — BEFORE any chunk reached
Telegram. If the bot crashed between claim and the first chunk landing,
the persisted marker survived restart and silently suppressed every
future render of the same AUQ on that window.

Wave 1 split that into three calls:

  * ``claim_auq_context_post_in_memory(window_id, dedup_key) -> str | None``
    — phase 1, in-memory pending entry only; NOT persisted.
  * ``commit_auq_context_post(window_id, claim_token, message_ids, **fields)``
    — phase 2, persists dedup marker + chunked record. Only fires
    after at least one chunk lands on Telegram.
  * ``rollback_auq_context_post(window_id, claim_token)`` — phase 3,
    drops the pending entry without persisting.

A restart between phase 1 and phase 2 drops the pending entry (in
memory only). The next render claims again and re-posts, because the
restarted bot has no proof the prior chunk reached Telegram. This is
the intended trade-off: re-render on crash, never silently suppress.

Tests use an injectable clock (``_pending_claim_clock``) for TTL
behavior — no real ``sleep`` calls.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def _isolated_state(tmp_path, monkeypatch):
    """Redirect interactive_state.json to a tmp_path and clear all
    relevant module state — including the new Wave 1 pending dict.
    """
    from cctelegram.handlers import interactive_ui as iui

    fake_file = tmp_path / "interactive_state.json"
    monkeypatch.setattr(iui, "_interactive_state_file_path", lambda: fake_file)

    iui._interactive_msgs.clear()
    iui._interactive_msg_meta.clear()
    iui._auq_context_posted.clear()
    iui._auq_context_post_pending.clear()
    iui._auq_context_msgs.clear()
    iui._last_completed_ask_tool_input.clear()
    iui._last_auq_tool_use_id.clear()
    yield fake_file
    iui._interactive_msgs.clear()
    iui._interactive_msg_meta.clear()
    iui._auq_context_posted.clear()
    iui._auq_context_post_pending.clear()
    iui._auq_context_msgs.clear()
    iui._last_completed_ask_tool_input.clear()
    iui._last_auq_tool_use_id.clear()


def _simulate_restart(_isolated_state):
    """Simulate a process restart by clearing all in-memory module
    state and re-loading ``interactive_state.json`` via
    ``hydrate_interactive_state``.

    The pending dict ``_auq_context_post_pending`` is in-memory only;
    a real restart drops it. The persisted dicts
    (``_auq_context_posted``, ``_auq_context_msgs``,
    ``_interactive_msg_meta``) round-trip through disk via hydrate.
    """
    from cctelegram.handlers import interactive_ui as iui

    iui._interactive_msgs.clear()
    iui._interactive_msg_meta.clear()
    iui._auq_context_posted.clear()
    iui._auq_context_post_pending.clear()
    iui._auq_context_msgs.clear()
    iui._last_completed_ask_tool_input.clear()
    iui._last_auq_tool_use_id.clear()


# Real ``commit`` requires the full set of fields — wrap in a helper
# so each test stays focused on the lifecycle, not the field plumbing.
def _commit_with_dummy_fields(
    iui, window_id: str, token: str, message_ids: tuple[int, ...]
) -> bool:
    return iui.commit_auq_context_post(
        window_id,
        token,
        message_ids,
        text="📋 AskUserQuestion — full details\n\nQ?",
        source={"questions": [{"question": "Q?"}]},
        user_id=1,
        chat_id=100,
        thread_id=None,
        session_id="sess-x",
    )


class TestClaimBeforeFirstChunk:
    """Plan §5.3 scenario 1 — claim → crash before first chunk →
    restart drops the pending entry → next render re-posts.

    This is the bug Wave 1 fixes. Pre-Wave-1, the claim was persisted
    eagerly and survived restart, silently suppressing the next render.
    """

    def test_pending_does_not_survive_restart(self, _isolated_state):
        from cctelegram.handlers import interactive_ui as iui

        token = iui.claim_auq_context_post_in_memory("@5", "toolu_xyz")
        assert token is not None
        # In-memory pending → on-disk: nothing.
        assert "@5" in iui._auq_context_post_pending
        if _isolated_state.exists():
            import json as _json

            data = _json.loads(_isolated_state.read_text())
            assert "@5" not in data.get("auq_context_posted", {})

        # Simulate crash: clear in-memory dicts (no on-disk pending to
        # re-hydrate, by design).
        _simulate_restart(_isolated_state)

        # Hydrate (file may not exist) — no pending claim survives.
        from unittest.mock import MagicMock

        from cctelegram.session import SessionManager

        sm = MagicMock(spec=SessionManager)
        sm.window_states = {}
        sm.resolve_window_for_thread = lambda _u, _t: None
        iui.hydrate_interactive_state(sm)

        assert iui._auq_context_post_pending == {}
        assert iui._auq_context_posted.get("@5") is None

        # Next render's claim succeeds — no permanent suppression.
        next_token = iui.claim_auq_context_post_in_memory("@5", "toolu_xyz")
        assert next_token is not None
        # Fresh token, not the post-crash ghost.
        assert next_token != token


class TestCommitPartialSurvivesRestart:
    """Plan §5.3 scenario 2 — commit(partial msg_ids) → restart →
    record survives → next render does NOT re-post (would duplicate).

    The Wave 1 invariant: once chunks reach Telegram, persistence
    fires; restart preserves the dedup marker so the picker doesn't
    duplicate the partial context message.
    """

    def test_committed_partial_marker_round_trips_through_disk(self, _isolated_state):
        import json as _json

        from cctelegram.handlers import interactive_ui as iui

        token = iui.claim_auq_context_post_in_memory("@5", "toolu_xyz")
        assert token is not None
        committed = _commit_with_dummy_fields(iui, "@5", token, (12345,))
        assert committed is True

        # Persisted marker AND chunked record on disk.
        data = _json.loads(_isolated_state.read_text())
        assert data["auq_context_posted"]["@5"] == "toolu_xyz"
        assert data["auq_context_msgs"]["@5"]["message_ids"] == [12345]

        # Restart-simulate.
        _simulate_restart(_isolated_state)

        # Hydrate — marker re-loaded.
        from unittest.mock import MagicMock

        from cctelegram.session import SessionManager

        sm = MagicMock(spec=SessionManager)
        sm.window_states = {"@5": object()}  # known to session manager
        sm.resolve_window_for_thread = lambda _u, _t: None
        iui.hydrate_interactive_state(sm)

        assert iui._auq_context_posted.get("@5") == "toolu_xyz"
        # Next render's claim returns None — the persisted marker
        # blocks re-post. Without this, the bot would re-send the
        # context message and DUPLICATE the chunks already on Telegram.
        assert iui.claim_auq_context_post_in_memory("@5", "toolu_xyz") is None


class TestPendingClaimTTLPurge:
    """Plan §5.3 scenario 3 — claim → 60s TTL elapse → next claim
    succeeds (in-memory claim expired).

    Same-process abandoned claims expire so a hung coroutine doesn't
    permanently block subsequent claims for the same window. Tests use
    an injectable clock (``_pending_claim_clock``) — no real sleeps.
    """

    def test_stale_pending_purged_on_next_claim(self, _isolated_state, monkeypatch):
        from cctelegram.handlers import interactive_ui as iui

        clock_state = {"t": 1000.0}
        monkeypatch.setattr(iui, "_pending_claim_clock", lambda: clock_state["t"])

        token1 = iui.claim_auq_context_post_in_memory("@5", "toolu_first")
        assert token1 is not None

        # Same instant: second claim blocked by the in-flight pending.
        assert iui.claim_auq_context_post_in_memory("@5", "toolu_second") is None

        # +30s: still within TTL → still blocked.
        clock_state["t"] = 1030.0
        assert iui.claim_auq_context_post_in_memory("@5", "toolu_second") is None

        # +61s: TTL elapsed → next claim purges the stale pending and
        # succeeds with a fresh token.
        clock_state["t"] = 1061.0
        token2 = iui.claim_auq_context_post_in_memory("@5", "toolu_second")
        assert token2 is not None
        assert token2 != token1

    def test_committed_marker_blocks_even_after_ttl(self, _isolated_state, monkeypatch):
        """After commit, the persisted ``_auq_context_posted`` marker
        blocks subsequent claims independent of TTL. The TTL only
        applies to pending entries.
        """
        from cctelegram.handlers import interactive_ui as iui

        clock_state = {"t": 1000.0}
        monkeypatch.setattr(iui, "_pending_claim_clock", lambda: clock_state["t"])

        token = iui.claim_auq_context_post_in_memory("@5", "toolu_xyz")
        assert token is not None
        _commit_with_dummy_fields(iui, "@5", token, (12345,))

        # Pending drained; persisted marker in place.
        assert "@5" not in iui._auq_context_post_pending
        assert iui._auq_context_posted.get("@5") == "toolu_xyz"

        # Even +3600s later, the marker still blocks claim_in_memory
        # — that's the persistence boundary, not the TTL boundary.
        clock_state["t"] = 4600.0
        assert iui.claim_auq_context_post_in_memory("@5", "toolu_other") is None


class TestPartialSendInvariantThroughSendFunction:
    """Plan §5.3 scenario 4 (codex P2 #2): chunk 1 lands → chunk 2
    fails → no rollback → restart → record has just chunk 1's msg_id
    → next render does NOT duplicate.

    Drives the full ``_send_auq_context_message`` flow with a fake
    ``topic_send`` so we exercise the integration between the loop's
    PARTIAL_SENT branch and ``commit_auq_context_post``.
    """

    @pytest.mark.asyncio
    async def test_partial_send_persists_then_restart_skips_repost(
        self, _isolated_state, monkeypatch
    ):
        from unittest.mock import Mock

        from cctelegram.handlers import interactive_ui as iui
        from cctelegram.handlers.message_sender import TopicSendOutcome

        # Tool input large enough to chunk into 2+ parts.
        long_desc = ("paragraph " * 120).strip()  # ~1080 chars
        tool_input = {
            "questions": [
                {
                    "question": "Q?",
                    "options": [
                        {"label": f"Option {i}", "description": long_desc}
                        for i in range(1, 11)
                    ],
                }
            ]
        }

        send_count = {"n": 0}

        async def _ok_then_fail(*_args, **_kwargs):
            send_count["n"] += 1
            if send_count["n"] == 1:
                # Chunk 1 lands.
                return Mock(message_id=11111), TopicSendOutcome.OK
            # Chunk 2 fails structurally — sent=None.
            return None, TopicSendOutcome.TOPIC_NOT_FOUND

        monkeypatch.setattr(iui, "topic_send", _ok_then_fail)
        # Pin session_id_for_window across send + hydrate so the persisted
        # chunked record's session_id matches what hydrate sees and the
        # session-mismatch prune branch doesn't drop the record.
        monkeypatch.setattr(iui, "session_id_for_window", lambda _wid: "sess-x")

        token = iui.claim_auq_context_post_in_memory("@5", "toolu_partial")
        assert token is not None
        result = await iui._send_auq_context_message(
            None,  # type: ignore[arg-type]
            user_id=1,
            thread_id=None,
            chat_id=100,
            window_id="@5",
            source=tool_input,
            claim_token=token,
        )
        assert result is iui._ContextSendResult.PARTIAL_SENT

        # Chunk 1 landed → commit fired with the truncated msg_ids.
        assert iui._auq_context_posted.get("@5") == "toolu_partial"
        assert iui._auq_context_msgs["@5"].message_ids == (11111,)

        # Restart-simulate.
        _simulate_restart(_isolated_state)

        # Hydrate from disk: the partial record + marker round-trip.
        from unittest.mock import MagicMock

        from cctelegram.session import SessionManager

        sm = MagicMock(spec=SessionManager)
        sm.window_states = {"@5": object()}
        sm.resolve_window_for_thread = lambda _u, _t: None
        iui.hydrate_interactive_state(sm)

        # Post-restart state preserves the partial commit.
        assert iui._auq_context_posted.get("@5") == "toolu_partial"
        assert iui._auq_context_msgs["@5"].message_ids == (11111,)

        # Next render's claim returns None — the dedup marker blocks
        # re-post and prevents duplication of chunk 1 on Telegram.
        assert iui.claim_auq_context_post_in_memory("@5", "toolu_partial") is None
