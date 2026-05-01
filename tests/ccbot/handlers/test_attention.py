"""Tests for handlers.attention — heuristic and digest waiting indicator."""

from ccbot.handlers import attention
from ccbot.handlers.message_queue import ActivityDigestState, _render_activity_digest


def test_is_attention_request_empty():
    assert attention.is_attention_request("") is False
    assert attention.is_attention_request("   \n  ") is False


def test_is_attention_request_cue_phrases():
    assert attention.is_attention_request("Do you want me to keep going?") is True
    assert attention.is_attention_request("Please confirm before I proceed.") is True
    assert attention.is_attention_request("Tell me which approach you want.") is True
    assert attention.is_attention_request("ok unless you object") is True


def test_is_attention_request_long_question():
    long_q = (
        "We have two reasonable migrations on the table; do you have a strong "
        "preference for the staged rollout over the dual-write?"
    )
    assert attention.is_attention_request(long_q) is True


def test_is_attention_request_short_question_ignored():
    # Short questions shouldn't trip the heuristic — too prone to false positives.
    assert attention.is_attention_request("Done?") is False


def test_is_attention_request_normal_status_text():
    assert attention.is_attention_request("Wrote 12 files. All tests pass.") is False


def test_render_activity_digest_waiting_indicator():
    state = ActivityDigestState(message_id=0, window_id="@0")
    state.lines = ["⚙️ Read foo.py"]
    state.tool_count = 1
    state.completed_count = 1

    busy = _render_activity_digest(state, waiting=False)
    assert busy.startswith("✅ Done") or busy.startswith("🟡 Busy")

    waiting = _render_activity_digest(state, waiting=True)
    assert waiting.startswith("🔔 Waiting on you")


def test_render_activity_digest_done_when_not_waiting():
    state = ActivityDigestState(message_id=0, window_id="@0", done=True)
    rendered = _render_activity_digest(state, waiting=False)
    assert rendered.startswith("✅ Done")
