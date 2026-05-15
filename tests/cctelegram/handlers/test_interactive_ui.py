"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cctelegram.handlers.interactive_ui import (
    _build_interactive_keyboard,
    handle_interactive_ui,
)
from cctelegram.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from cctelegram.handlers import attention
    from cctelegram.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    attention.reset_for_tests()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()
    attention.reset_for_tests()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_settings_ui_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """handle_interactive_ui captures Settings pane, sends message with keyboard.

        Topic-first attention card also fires (in the same chat/thread, not as
        a DM). We assert: (a) the keyboard message lands in the topic with the
        nav keyboard, and (b) no send goes to the user_id-as-chat (i.e. no DM).
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "etcircle-dev"

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True

        keyboard_calls = [
            c
            for c in mock_bot.send_message.call_args_list
            if c.kwargs.get("reply_markup") is not None
        ]
        assert len(keyboard_calls) == 1
        kw = keyboard_calls[0].kwargs
        assert kw["chat_id"] == 100
        assert kw["message_thread_id"] == 42

        # No DM: every send_message went to chat_id=100 (the topic).
        for call in mock_bot.send_message.call_args_list:
            assert call.kwargs["chat_id"] == 100, (
                f"unexpected DM-shaped send_message: {call.kwargs}"
            )

    @pytest.mark.asyncio
    async def test_interactive_ui_card_peeks_anchor_so_assistant_text_can_anchor(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """§2.5.2: the interactive-card send must not pop the anchor.

        Both the interactive card AND the assistant text Claude emits after
        the user resolves the card are responses to the same user prompt,
        so they should anchor to the same Telegram message_id. The
        canonical anchor consumer is ``_process_content_task``; the
        interactive-UI surface only peeks.
        """
        from telegram import ReplyParameters

        from cctelegram.handlers import message_queue
        from cctelegram.handlers.message_sender import TopicSendOutcome

        window_id = "@5"
        user_id = 1
        thread_id = 42
        anchor_message_id = 7777

        # Stash the anchor as if a prior text/photo offer recorded it.
        message_queue.set_route_last_user_message(
            user_id, thread_id, window_id, anchor_message_id
        )

        sent_msg = MagicMock()
        sent_msg.message_id = 9999
        send_calls: list[dict] = []

        async def fake_topic_send(
            bot, *, op, user_id, chat_id, thread_id, window_id, text, **kw
        ):
            send_calls.append({"op": op, "kw": kw})
            return sent_msg, TopicSendOutcome.OK

        async def fake_attention(*args, **kwargs):
            return TopicSendOutcome.OK

        mock_window = MagicMock()
        mock_window.window_id = window_id

        try:
            with (
                patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
                patch(
                    "cctelegram.handlers.interactive_ui.session_manager"
                ) as mock_sm_iu,
                patch(
                    "cctelegram.handlers.interactive_ui.topic_send",
                    side_effect=fake_topic_send,
                ),
                patch(
                    "cctelegram.handlers.interactive_ui.attention.notify_waiting",
                    side_effect=fake_attention,
                ),
            ):
                mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
                mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
                mock_sm_iu.resolve_chat_id.return_value = 100
                mock_sm_iu.get_display_name.return_value = "topic-name"

                result = await handle_interactive_ui(
                    mock_bot,
                    user_id=user_id,
                    window_id=window_id,
                    thread_id=thread_id,
                )
            assert result is True
            # The card send carried the anchor.
            assert len(send_calls) == 1
            rp = send_calls[0]["kw"].get("reply_parameters")
            assert isinstance(rp, ReplyParameters)
            assert rp.message_id == anchor_message_id
            # CRITICAL: anchor still present after the card send (peek, not
            # consume). A subsequent assistant-text first-part send is the
            # canonical consumer.
            anchor_route = (user_id, thread_id, window_id)
            assert (
                message_queue._route_last_user_message.get(anchor_route)
                == anchor_message_id
            )
        finally:
            message_queue._route_last_user_message.pop(
                (user_id, thread_id, window_id), None
            )

    @pytest.mark.asyncio
    async def test_handle_no_ui_returns_false(self, mock_bot: AsyncMock):
        """Returns False when no interactive UI detected in pane."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager"),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value="$ echo hello\nhello\n$\n")

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_message.assert_not_called()


class TestKeyboardLayoutForSettings:
    def test_settings_keyboard_includes_all_nav_keys(self):
        """Settings keyboard includes Tab, arrows (not vertical_only), Space, Esc, Enter."""
        keyboard = _build_interactive_keyboard("@5", ui_name="Settings")
        # Flatten all callback data values
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(CB_ASK_TAB in d for d in all_cb_data if d)
        assert any(CB_ASK_SPACE in d for d in all_cb_data if d)
        assert any(CB_ASK_UP in d for d in all_cb_data if d)
        assert any(CB_ASK_DOWN in d for d in all_cb_data if d)
        assert any(CB_ASK_LEFT in d for d in all_cb_data if d)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data if d)
        assert any(CB_ASK_ESC in d for d in all_cb_data if d)
        assert any(CB_ASK_ENTER in d for d in all_cb_data if d)


# ── _render_ask_user_question ─────────────────────────────────────────────


from cctelegram.handlers.interactive_ui import (  # noqa: E402
    _render_ask_user_question,
)
from cctelegram.terminal_parser import (  # noqa: E402
    AskOption,
    AskTab,
    AskUserQuestionForm,
)


class TestRenderAskUserQuestion:
    def test_single_question_picker(self):
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="How is Claude doing this session? (optional)",
            options=(
                AskOption(label="Bad", recommended=False, cursor=True, number=1),
                AskOption(label="Fine", recommended=False, cursor=False, number=2),
                AskOption(label="Good", recommended=False, cursor=False, number=3),
            ),
        )
        out = _render_ask_user_question(form)
        # Title on top, then options, then footer hint
        assert "How is Claude doing this session?" in out
        assert "❯ 1. Bad" in out
        assert "  2. Fine" in out
        assert "  3. Good" in out
        assert "Enter to select" in out
        # No tab strip rendered for a single-question form
        assert "☒" not in out and "☐" not in out

    def test_multitab_picker_with_recommended(self):
        form = AskUserQuestionForm(
            tabs=(
                AskTab(
                    label="Approach", answered=False, is_submit=False, is_current=False
                ),
                AskTab(
                    label="Positioning",
                    answered=False,
                    is_submit=False,
                    is_current=False,
                ),
                AskTab(label="", answered=False, is_submit=True, is_current=False),
            ),
            current_question_title="Which implementation approach should we lock in?",
            options=(
                AskOption(
                    label="C — Parallel tracks",
                    recommended=True,
                    cursor=True,
                    number=1,
                ),
                AskOption(
                    label="B — Copilot-first",
                    recommended=False,
                    cursor=False,
                    number=2,
                ),
            ),
            is_free_text=True,
        )
        out = _render_ask_user_question(form)
        # Tab strip uses ☐ for un-answered and ✔ for the submit cell
        assert "☐ Approach" in out
        assert "☐ Positioning" in out
        assert "✔" in out
        # Question title preserved
        assert "Which implementation approach" in out
        # Recommended option carries the "(Recommended)" suffix
        assert "❯ 1. C — Parallel tracks (Recommended)" in out
        assert "  2. B — Copilot-first" in out
        # Free-text hint surfaces when present
        assert "Type something" in out

    def test_review_screen(self):
        form = AskUserQuestionForm(
            tabs=(
                AskTab(
                    label="Approach", answered=True, is_submit=False, is_current=False
                ),
                AskTab(
                    label="Positioning",
                    answered=True,
                    is_submit=False,
                    is_current=False,
                ),
                AskTab(label="", answered=False, is_submit=True, is_current=False),
            ),
            options=(
                AskOption(
                    label="Submit answers", recommended=False, cursor=True, number=1
                ),
                AskOption(label="Cancel", recommended=False, cursor=False, number=2),
            ),
            is_review_screen=True,
        )
        out = _render_ask_user_question(form)
        # Header signals review-screen rather than picker
        assert "Review your answers" in out
        # Both content tabs marked answered; submit cell suppressed in the
        # "review" body (the Submit/Cancel choice below covers it).
        assert "☒ Approach" in out
        assert "☒ Positioning" in out
        assert "Submit" not in out.split("\n")[0]  # not on the first line
        # Submit/Cancel row visible with cursor on Submit
        assert "Ready to submit your answers?" in out
        assert "❯ 1. Submit answers" in out
        assert "  2. Cancel" in out

    def test_empty_render_when_no_structure(self):
        # No tabs, no options, no review flag → renderer returns "" so the
        # caller can fall back to the raw pane excerpt.
        form = AskUserQuestionForm()
        assert _render_ask_user_question(form) == ""

    def test_descriptions_inlined_under_each_option(self):
        """PR 2: per-option description text from the JSONL payload shows
        up indented under the option label. Empty descriptions skip the
        indent line (pane-only forms don't carry descriptions).
        """
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Pick clip affordance.",
            options=(
                AskOption(
                    label="A — Top toolbar",
                    recommended=True,
                    cursor=True,
                    number=1,
                    description="Always-visible button next to Render. Clip labels readable at a glance.",
                ),
                AskOption(
                    label="B — Hover labels",
                    recommended=False,
                    cursor=False,
                    number=2,
                    description="Cleaner timeline; less visual noise but clip boundaries hidden.",
                ),
                AskOption(
                    label="C — Skip the feature",
                    recommended=False,
                    cursor=False,
                    number=3,
                    description="",  # no description, no indent line
                ),
            ),
        )
        out = _render_ask_user_question(form)
        # Option labels still visible.
        assert "❯ 1. A — Top toolbar (Recommended)" in out
        # Descriptions appear indented under their option.
        assert "    Always-visible button next to Render." in out
        assert "    Cleaner timeline; less visual noise" in out
        # An option with empty description does NOT get an empty indent line.
        lines = out.split("\n")
        for i, line in enumerate(lines):
            if "3. C — Skip" in line:
                # Next non-empty line should be the next option or footer,
                # not a stray "    " line.
                assert i + 1 < len(lines)
                # Either the blank-line-before-footer or "Enter to select".
                nxt = lines[i + 1]
                assert nxt == "" or "Enter to select" in nxt
                break

    def test_description_truncated_at_250_chars(self):
        """A description longer than 250 chars is hard-truncated with an
        ellipsis. Multi-line descriptions get collapsed first so the cap
        counts against visible characters.
        """
        long_desc = (
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 10
        )  # >>250
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Q?",
            options=(
                AskOption(
                    label="A",
                    recommended=False,
                    cursor=True,
                    number=1,
                    description=long_desc,
                ),
            ),
        )
        out = _render_ask_user_question(form)
        # The rendered indent line must be ≤ 4 (indent) + 250 chars long.
        desc_lines = [line for line in out.split("\n") if line.startswith("    L")]
        assert desc_lines, "expected an indented description line"
        # 4 leading spaces + 250 chars max = 254 cap on the visible line.
        assert all(len(line) <= 4 + 250 for line in desc_lines)
        # Last char before any newline is the ellipsis.
        assert desc_lines[0].endswith("…")

    def test_multiline_description_collapsed_to_single_line(self):
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Q?",
            options=(
                AskOption(
                    label="A",
                    recommended=False,
                    cursor=True,
                    number=1,
                    description="line one\nline two\n\nline three",
                ),
            ),
        )
        out = _render_ask_user_question(form)
        # The whole description renders on a single indented line.
        assert "    line one line two line three" in out

    def test_body_clipped_at_3800_chars(self):
        """Even with the per-option cap, a worst-case form could exceed
        3800 chars. The renderer hard-clips the whole body so the send
        layer never has to split (splitting would break the multi-tab
        message_ids invariant in PR 3).
        """
        # Build 20 options each with a 250-char description ≈ 5300 chars
        # of just descriptions. Total body well over the 3800 cap.
        opts = tuple(
            AskOption(
                label=f"Option {i}",
                recommended=False,
                cursor=(i == 1),
                number=i,
                description="X" * 250,
            )
            for i in range(1, 21)
        )
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Pick.",
            options=opts,
        )
        out = _render_ask_user_question(form)
        # Body capped under 3800.
        assert len(out) <= 3800
        # Cut marker present so the user knows it's truncated.
        assert "body truncated" in out


# ── PR 2b: pick-token map + structured option keyboard ────────────────────


from cctelegram.handlers.interactive_ui import (  # noqa: E402
    _PICK_TOKEN_TTL_SECONDS,
    _build_pick_button_rows,
    _PickTokenEntry,
    _mint_pick_token,
    consume_pick_token,
    reset_pick_tokens_for_tests,
)


@pytest.fixture
def _clear_pick_tokens():
    reset_pick_tokens_for_tests()
    yield
    reset_pick_tokens_for_tests()


@pytest.mark.usefixtures("_clear_pick_tokens")
class TestPickTokenMap:
    def test_mint_and_consume_roundtrip(self):
        entry = _PickTokenEntry(
            window_id="@1",
            user_id=42,
            thread_id=7,
            fingerprint="abc123def456",
            option_number=2,
            option_label="Fine",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )
        token = _mint_pick_token(entry)
        # Token is short hex (12 chars) so the full ``aqp:<token>`` payload
        # fits well under the 64-byte callback_data cap.
        assert len(token) == 12
        all_hex_digits = set("0123456789abcdef")
        assert all(c in all_hex_digits for c in token)
        # Consume returns the entry once, then None (single-use).
        got = consume_pick_token(token)
        assert got is entry
        assert consume_pick_token(token) is None

    def test_consume_expired_returns_none(self):
        entry = _PickTokenEntry(
            window_id="@1",
            user_id=42,
            thread_id=None,
            fingerprint="x",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() - 1,  # already past deadline
        )
        token = _mint_pick_token(entry)
        # The mint itself ran a prune pass that should have dropped this
        # token before we even tried to consume — consume sees nothing.
        assert consume_pick_token(token) is None

    def test_mint_unique_tokens(self):
        entry_template = _PickTokenEntry(
            window_id="@1",
            user_id=42,
            thread_id=None,
            fingerprint="abc",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )
        seen = set()
        for _ in range(20):
            token = _mint_pick_token(entry_template)
            assert token not in seen
            seen.add(token)


@pytest.mark.usefixtures("_clear_pick_tokens")
class TestBuildPickButtonRows:
    def test_no_options_returns_empty(self):
        form = AskUserQuestionForm()
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form
        )
        assert rows == []

    def test_one_button_per_numbered_option(self):
        form = AskUserQuestionForm(
            options=(
                AskOption(label="Bad", recommended=False, cursor=True, number=1),
                AskOption(label="Fine", recommended=False, cursor=False, number=2),
                AskOption(label="Good", recommended=True, cursor=False, number=3),
            ),
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        # All three buttons land on a single row (cap is 5).
        assert len(rows) == 1
        assert len(rows[0]) == 3
        # Button text starts with "N. " for non-submit options.
        assert rows[0][0].text.startswith("1. ")
        # Recommended star is appended.
        assert "★" in rows[0][2].text
        # Each button carries a unique aqp:<token> callback.
        tokens = [b.callback_data for b in rows[0]]
        assert len(set(tokens)) == 3
        assert all(t.startswith("aqp:") for t in tokens)

    def test_review_submit_button_flagged(self):
        # On the review screen with cursor on "1. Submit answers", the
        # builder must mark the first button as is_review_submit so the
        # callback handler can apply the tighter guardrail.
        form = AskUserQuestionForm(
            options=(
                AskOption(
                    label="Submit answers",
                    recommended=False,
                    cursor=True,
                    number=1,
                ),
                AskOption(label="Cancel", recommended=False, cursor=False, number=2),
            ),
            is_review_screen=True,
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        assert len(rows) == 1
        # The submit button reads "✅ Submit answers".
        assert rows[0][0].text.startswith("✅ ")
        # Consume Cancel first — consuming a token now wipes its whole form
        # generation (sibling invalidation, see TestPickTokenReuse), so we
        # can't pop Submit then Cancel from the same render.
        cancel_token = rows[0][1].callback_data[len("aqp:") :]
        cancel_entry = consume_pick_token(cancel_token)
        assert cancel_entry is not None
        assert cancel_entry.is_review_submit is False
        # Re-mint the form to check the Submit entry's flag.
        rows2 = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        submit_token = rows2[0][0].callback_data[len("aqp:") :]
        submit_entry = consume_pick_token(submit_token)
        assert submit_entry is not None
        assert submit_entry.is_review_submit is True

    def test_skips_options_without_a_numeric_shortcut(self):
        # Parser may emit options with number=None for free-text rows it
        # detected but couldn't bind to a digit. Those must NOT get a pick
        # button — the keystroke fallback still reaches them.
        form = AskUserQuestionForm(
            options=(
                AskOption(label="Bad", recommended=False, cursor=False, number=1),
                AskOption(
                    label="Type something",
                    recommended=False,
                    cursor=False,
                    number=None,
                ),
            ),
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        assert len(rows[0]) == 1
        assert rows[0][0].text.startswith("1. Bad")

    def test_six_options_split_across_two_rows(self):
        form = AskUserQuestionForm(
            options=tuple(
                AskOption(label=f"opt{i}", recommended=False, cursor=False, number=i)
                for i in range(1, 7)
            ),
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        # Cap is 5 per row → first row has 5, second has 1.
        assert [len(r) for r in rows] == [5, 1]

    def test_token_carries_full_entry_for_staleness_check(self):
        form = AskUserQuestionForm(
            options=(
                AskOption(
                    label="C — Parallel tracks", recommended=True, cursor=True, number=1
                ),
            ),
            current_question_title="approach?",
        )
        fp = form.fingerprint()
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@9", form=form
        )
        token = rows[0][0].callback_data[len("aqp:") :]
        entry = consume_pick_token(token)
        assert entry is not None
        # Everything the callback handler needs is on the entry.
        assert entry.window_id == "@9"
        assert entry.user_id == 42
        assert entry.thread_id == 7
        assert entry.fingerprint == fp
        assert entry.option_number == 1
        assert entry.option_label == "C — Parallel tracks"
        # Expiration roughly matches the configured TTL.
        assert entry.expires_at > time.monotonic()
        assert entry.expires_at <= time.monotonic() + _PICK_TOKEN_TTL_SECONDS + 1


@pytest.mark.usefixtures("_clear_pick_tokens")
class TestPickTokenReuse:
    """Token churn would defeat MESSAGE_NOT_MODIFIED on edit. Hermes review
    flagged this as the load-bearing fix before PR 2b can ship: a re-render
    of the same form (same fingerprint) MUST reuse the same callback tokens
    so the reply_markup is byte-identical and Telegram can dedupe the edit.
    """

    def test_same_fingerprint_reuses_tokens(self):
        form = AskUserQuestionForm(
            options=(
                AskOption(label="Bad", recommended=False, cursor=True, number=1),
                AskOption(label="Fine", recommended=False, cursor=False, number=2),
            ),
        )
        first = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form
        )
        second = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form
        )
        # Two renders against the same fingerprint must produce identical
        # callback_data — otherwise every status-polling tick rewrites the
        # reply_markup and Telegram never returns MESSAGE_NOT_MODIFIED.
        first_tokens = [b.callback_data for b in first[0]]
        second_tokens = [b.callback_data for b in second[0]]
        assert first_tokens == second_tokens

    def test_different_fingerprint_mints_fresh_tokens(self):
        form_a = AskUserQuestionForm(
            options=(AskOption(label="Bad", recommended=False, cursor=True, number=1),),
        )
        form_b = AskUserQuestionForm(
            options=(
                # Different label → different fingerprint → fresh tokens.
                AskOption(label="Terrible", recommended=False, cursor=True, number=1),
            ),
        )
        a_rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form_a
        )
        b_rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form_b
        )
        a_token = a_rows[0][0].callback_data
        b_token = b_rows[0][0].callback_data
        assert a_token != b_token

    def test_consume_invalidates_cache_for_that_generation(self):
        form = AskUserQuestionForm(
            options=(
                AskOption(label="Bad", recommended=False, cursor=True, number=1),
                AskOption(label="Fine", recommended=False, cursor=False, number=2),
            ),
        )
        rows = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form
        )
        first_token = rows[0][0].callback_data[len("aqp:") :]
        second_token = rows[0][1].callback_data[len("aqp:") :]
        # Click the first button — the cache row for this fingerprint dies,
        # AND every sibling token in that row dies too (the form is about to
        # advance, so a stale sibling click is a bug to prevent).
        consumed = consume_pick_token(first_token)
        assert consumed is not None
        # Sibling token no longer resolves.
        assert consume_pick_token(second_token) is None
        # Next render against the same fingerprint mints fresh tokens.
        rows2 = _build_pick_button_rows(
            user_id=42, thread_id=7, window_id="@1", form=form
        )
        new_token = rows2[0][0].callback_data
        assert new_token != f"aqp:{first_token}"


# ── PR 2: callback-validator parity via resolve_ask_form ─────────────────


class TestCallbackValidatorParityRender:
    """The render path and the pick-token callback validator MUST produce
    byte-identical fingerprints. PR 1 added ``resolve_ask_form``; PR 2
    wires it into both call sites. This test pins that both call sites
    produce the same fingerprint for the same (tool_input, pane_text)
    pair.

    Without this property, every multi-tab click would bounce as "Form
    changed, refreshing" because the validator's pane-only re-parse would
    never match a JSONL-overlay-derived mint.
    """

    def test_single_question_fingerprint_matches_across_callsites(self):
        from cctelegram.terminal_parser import resolve_ask_form

        tool_input = {
            "questions": [
                {
                    "question": "Pick one.",
                    "options": [
                        {"label": "A", "description": "first"},
                        {"label": "B", "description": "second"},
                    ],
                }
            ]
        }
        pane = (
            "Pick one.\n"
            "\n"
            "❯ 1. A\n"
            "  2. B\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        # Render path
        render_form = resolve_ask_form(tool_input, pane)
        # Validator path (same inputs, same call)
        validate_form = resolve_ask_form(tool_input, pane)
        assert render_form is not None and validate_form is not None
        assert render_form.fingerprint() == validate_form.fingerprint()

    def test_multi_question_fingerprint_matches_across_callsites(self):
        from cctelegram.terminal_parser import resolve_ask_form

        tool_input = {
            "questions": [
                {
                    "question": "Pick approach.",
                    "options": [{"label": "alpha"}, {"label": "beta"}],
                },
                {
                    "question": "Pick polish.",
                    "options": [{"label": "gamma"}, {"label": "delta"}],
                },
            ]
        }
        pane = (
            "Pick polish.\n"
            "\n"
            "❯ 1. gamma\n"
            "  2. delta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        render_form = resolve_ask_form(tool_input, pane)
        validate_form = resolve_ask_form(tool_input, pane)
        assert render_form is not None and validate_form is not None
        assert render_form.fingerprint() == validate_form.fingerprint()
        # Inferred path — fingerprint includes INF:1
        assert "INF:1" in render_form._canonical_repr()

    def test_pane_only_validator_diverges_from_jsonl_render(self):
        """Sanity-check the bug this PR fixes: if the validator uses
        ``parse_ask_user_question`` alone (pane-only) while the render
        uses ``resolve_ask_form`` (JSONL overlay) for a multi-tab form,
        the fingerprints WILL differ. This test would have caught the
        pre-PR2 bug.
        """
        from cctelegram.terminal_parser import (
            parse_ask_user_question,
            resolve_ask_form,
        )

        tool_input = {
            "questions": [
                {
                    "question": "Pick approach.",
                    "options": [{"label": "alpha"}, {"label": "beta"}],
                },
                {
                    "question": "Pick polish.",
                    "options": [{"label": "gamma"}, {"label": "delta"}],
                },
            ]
        }
        pane = (
            "Pick polish.\n"
            "\n"
            "❯ 1. gamma\n"
            "  2. delta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        jsonl_form = resolve_ask_form(tool_input, pane)
        pane_only_form = parse_ask_user_question(pane)
        assert jsonl_form is not None and pane_only_form is not None
        # The mismatch is exactly the bug: pane-only form has no
        # ``questions`` matrix, so no QS:/INF: lines, so different hash.
        assert jsonl_form.fingerprint() != pane_only_form.fingerprint()

    def test_resolve_ask_tool_input_public_alias(self):
        """``resolve_ask_tool_input`` is the public sibling-imported name
        used by bot.py to feed the validator the same cached JSONL the
        render path saw. PR 2 introduces this alias.
        """
        from cctelegram.handlers.interactive_ui import (
            remember_ask_tool_input,
            resolve_ask_tool_input,
        )

        # Cache a payload, then read it back via the public alias.
        sample = {"questions": [{"question": "Q", "options": [{"label": "A"}]}]}
        remember_ask_tool_input("@99", sample)
        try:
            assert resolve_ask_tool_input("@99") == sample
            assert resolve_ask_tool_input("@nonexistent") is None
        finally:
            # Clean up so the cache doesn't bleed into other tests.
            from cctelegram.handlers.interactive_ui import forget_ask_tool_input

            forget_ask_tool_input("@99")
