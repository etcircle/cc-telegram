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
    _pick_token_cache,
    _pick_tokens,
    clear_interactive_msg,
    consume_pick_token,
    peek_pick_token,
    reset_pick_tokens_for_tests,
    set_interactive_mode,
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


@pytest.mark.usefixtures("_clear_pick_tokens")
class TestPeekPickTokenIsNonDestructive:
    """CB3 — wrong-user clicks must NOT destroy the legitimate owner's token."""

    def _entry(self, user_id: int = 42) -> _PickTokenEntry:
        return _PickTokenEntry(
            window_id="@1",
            user_id=user_id,
            thread_id=7,
            fingerprint="fp1",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )

    def test_peek_returns_entry_without_consuming(self):
        token = _mint_pick_token(self._entry())
        # Peek N times → same entry every time, token still alive.
        for _ in range(3):
            got = peek_pick_token(token)
            assert got is not None
            assert got.user_id == 42
        # The real consume still works after peeks.
        consumed = consume_pick_token(token)
        assert consumed is not None
        # Now actually gone.
        assert peek_pick_token(token) is None
        assert consume_pick_token(token) is None

    def test_peek_does_not_drop_sibling_cache(self):
        # Mint two tokens in the same cache row (same fingerprint).
        e1 = self._entry()
        e2 = _PickTokenEntry(
            window_id=e1.window_id,
            user_id=e1.user_id,
            thread_id=e1.thread_id,
            fingerprint=e1.fingerprint,
            option_number=2,
            option_label="B",
            is_review_submit=False,
            expires_at=e1.expires_at,
        )
        t1 = _mint_pick_token(e1)
        t2 = _mint_pick_token(e2)
        cache_key = (e1.user_id, e1.thread_id or 0, e1.window_id, e1.fingerprint)
        _pick_token_cache[cache_key] = [t1, t2]
        # Peek t1 — neither t2 nor the cache row should be touched.
        assert peek_pick_token(t1) is e1
        assert peek_pick_token(t2) is e2
        assert _pick_token_cache.get(cache_key) == [t1, t2]


@pytest.mark.usefixtures("_clear_interactive_state")
class TestAssertNavDispatchable:
    """P1.1 + P1.3 + CB1 + CB5 + F2 — nav-callback guard helper.

    These tests pin the public contract of ``assert_nav_dispatchable``:
    return values and per-branch behaviour. They mock the tmux + helper
    surface so we can drive each branch deterministically.
    """

    def _query(self) -> AsyncMock:
        q = AsyncMock()
        q.answer = AsyncMock()
        return q

    @pytest.mark.asyncio
    async def test_no_interactive_surface_short_circuits(self):
        from cctelegram.handlers.interactive_ui import assert_nav_dispatchable

        q = self._query()
        result = await assert_nav_dispatchable(q, 42, 7, "@0")
        assert result is None
        q.answer.assert_awaited_once_with("No live interactive UI")

    @pytest.mark.asyncio
    async def test_no_interactive_surface_esc_returns_clear_sentinel(self):
        # F2: ESC carve-out — picker is gone, but ESC should still proceed
        # to the cleanup branch in the caller.
        from cctelegram.handlers.interactive_ui import (
            NAV_ESC_CLEAR,
            assert_nav_dispatchable,
        )

        q = self._query()
        result = await assert_nav_dispatchable(q, 42, 7, "@0", is_esc=True)
        assert result == NAV_ESC_CLEAR
        # ESC carve-out doesn't answer the query (the caller does after
        # running clear_interactive_msg).
        q.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_mismatch_short_circuits(self):
        from cctelegram.handlers import interactive_ui as iui

        # has_interactive_surface checks _interactive_msgs OR
        # _multi_tab_sessions, not _interactive_mode. Set both so we reach
        # the window-mismatch guard rather than tripping the surface guard
        # first.
        iui._interactive_msgs[(42, 7)] = 999
        iui.set_interactive_mode(42, "@otherwindow", 7)
        q = self._query()
        result = await iui.assert_nav_dispatchable(q, 42, 7, "@requested")
        assert result is None
        q.answer.assert_awaited_once_with("Window changed")

    @pytest.mark.asyncio
    async def test_visible_pane_absent_short_circuits(self):
        # User left the picker, terminal is back at shell. visible_pane
        # returns shell output → liveness=absent → short-circuit.
        from cctelegram.handlers import interactive_ui as iui

        iui._interactive_msgs[(42, 7)] = 999
        iui.set_interactive_mode(42, "@0", 7)
        q = self._query()
        fake_window = MagicMock()
        fake_window.window_id = "@0"
        with (
            patch.object(
                iui.tmux_manager,
                "find_window_by_id",
                new_callable=AsyncMock,
                return_value=fake_window,
            ),
            patch.object(
                iui.tmux_manager,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="$ ls\nfile.txt\n$ \n",
            ),
        ):
            result = await iui.assert_nav_dispatchable(q, 42, 7, "@0")
        assert result is None
        q.answer.assert_awaited_once_with("Picker closed, refreshing")

    @pytest.mark.asyncio
    async def test_visible_pane_unknown_proceeds(self):
        # CB1: empty visible capture (alt-screen / redraw race) is UNKNOWN.
        # MUST NOT short-circuit — that would destroy a live picker the
        # very next frame brings back.
        from cctelegram.handlers import interactive_ui as iui

        iui._interactive_msgs[(42, 7)] = 999
        iui.set_interactive_mode(42, "@0", 7)
        q = self._query()
        fake_window = MagicMock()
        fake_window.window_id = "@0"
        with (
            patch.object(
                iui.tmux_manager,
                "find_window_by_id",
                new_callable=AsyncMock,
                return_value=fake_window,
            ),
            patch.object(
                iui.tmux_manager,
                "capture_pane",
                new_callable=AsyncMock,
                return_value="",  # empty visible
            ),
        ):
            result = await iui.assert_nav_dispatchable(q, 42, 7, "@0")
        # Proceed: returns the live window object, no short-circuit answer.
        assert result is fake_window
        q.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_picker_present_returns_window(self):
        from cctelegram.handlers import interactive_ui as iui

        iui._interactive_msgs[(42, 7)] = 999
        iui.set_interactive_mode(42, "@0", 7)
        q = self._query()
        fake_window = MagicMock()
        fake_window.window_id = "@0"
        pane = (
            "Pick.\n"
            "\n"
            "❯ 1. A\n"
            "  2. B\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )
        with (
            patch.object(
                iui.tmux_manager,
                "find_window_by_id",
                new_callable=AsyncMock,
                return_value=fake_window,
            ),
            patch.object(
                iui.tmux_manager,
                "capture_pane",
                new_callable=AsyncMock,
                return_value=pane,
            ),
        ):
            result = await iui.assert_nav_dispatchable(q, 42, 7, "@0")
        assert result is fake_window
        q.answer.assert_not_called()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestP14PollerJsonlRaceDeferral:
    """P1.4 — poller-driven render defers when JSONL cache hasn't caught up.

    The status poller can detect an interactive UI in the pane before
    session_monitor parses the matching tool_use JSONL entry. If the cache
    is empty AND the pane shows partial options (first option number > 1),
    handle_interactive_ui returns False without rendering. After bounded
    wait elapses, it force-renders the pane-only form without pick buttons.
    """

    @pytest.fixture(autouse=True)
    def _reset_defer(self):
        from cctelegram.handlers import interactive_ui as iui

        iui._reset_render_defer_for_tests()
        iui.forget_ask_tool_input("@5")
        yield
        iui._reset_render_defer_for_tests()
        iui.forget_ask_tool_input("@5")

    @pytest.mark.asyncio
    async def test_poller_defers_when_cache_empty_and_pane_partial(self, mock_bot):
        """Cache empty + pane first option number > 1 → return False, no render."""
        from cctelegram.handlers import interactive_ui as iui

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        # Pane shows options 4-5 only (the picker trailer; real options
        # 1-3 are off-screen because the question prose is long).
        visible_pane = (
            "Pick one.\n"
            "...long question...\n"
            "❯ 4. Type something\n"
            "  5. Chat about this\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=visible_pane)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            # JSONL cache is empty for this window.
            result = await iui.handle_interactive_ui(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                from_poller=True,
            )

        assert result is False
        # No card sent; defer state recorded.
        mock_bot.send_message.assert_not_called()
        assert window_id in iui._render_deferred_since

    @pytest.mark.asyncio
    async def test_poller_renders_when_cache_arrives(self, mock_bot):
        """Cache populated → defer state cleared, render proceeds."""
        from cctelegram.handlers import interactive_ui as iui

        window_id = "@5"
        # Pre-set defer state to simulate a prior deferred tick.
        import time as _t

        iui._render_deferred_since[window_id] = _t.monotonic()
        # Now JSONL arrives — remember_ask_tool_input is the canonical
        # callback. Calling it must clear the defer marker.
        iui.remember_ask_tool_input(
            window_id,
            {
                "questions": [
                    {
                        "question": "Pick one.",
                        "options": [
                            {"label": "Alpha"},
                            {"label": "Beta"},
                            {"label": "Gamma"},
                            {"label": "Type something"},
                            {"label": "Chat about this"},
                        ],
                    }
                ]
            },
        )
        assert window_id not in iui._render_deferred_since

    @pytest.mark.asyncio
    async def test_poller_does_not_defer_when_first_option_is_1(self, mock_bot):
        """Pane first option == 1 → not partial → render normally even with empty cache."""
        from cctelegram.handlers import interactive_ui as iui

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        visible_pane = (
            "Pick one.\n"
            "\n"
            "❯ 1. A\n"
            "  2. B\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=visible_pane)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            result = await iui.handle_interactive_ui(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                from_poller=True,
            )
        # First option is 1 → pane is the full picker → render normally.
        assert result is True
        assert window_id not in iui._render_deferred_since

    @pytest.mark.asyncio
    async def test_poller_force_renders_after_timeout(self, mock_bot):
        """Bounded-wait elapsed → force-render with pane-only, NO pick buttons."""
        from cctelegram.handlers import interactive_ui as iui

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        # Same partial pane as the first test.
        visible_pane = (
            "Pick one.\n"
            "❯ 4. Type something\n"
            "  5. Chat about this\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        import time as _t

        # Backdate the defer timestamp well past the timeout.
        iui._render_deferred_since[window_id] = _t.monotonic() - 100.0

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=visible_pane)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            result = await iui.handle_interactive_ui(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                from_poller=True,
            )
        # Force-render: card sent, but no pick buttons. Defer state cleared.
        assert result is True
        assert window_id not in iui._render_deferred_since
        mock_bot.send_message.assert_called()
        # No pick buttons: the reply_markup keyboard has no aqp: callback_data.
        last_kw = mock_bot.send_message.call_args.kwargs
        markup = last_kw.get("reply_markup")
        if markup is not None:
            # Walk the keyboard; aqp: prefix is the pick-button signature.
            all_buttons = [b for row in markup.inline_keyboard for b in row]
            assert not any(
                getattr(b, "callback_data", "").startswith("aqp:") for b in all_buttons
            ), "Force-render must not mint pick buttons (P1.4)"

    @pytest.mark.asyncio
    async def test_non_poller_path_ignores_defer(self, mock_bot):
        """from_poller=False → defer logic never fires (JSONL/monitor path)."""
        from cctelegram.handlers import interactive_ui as iui

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        visible_pane = (
            "Pick one.\n"
            "❯ 4. Type something\n"
            "  5. Chat about this\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=visible_pane)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            # Default from_poller=False → JSONL path; renders even with
            # cache empty and partial pane.
            result = await iui.handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )
        assert result is True
        # No defer marker because we never went through the poller branch.
        assert window_id not in iui._render_deferred_since

    @pytest.mark.asyncio
    async def test_poller_defers_when_cache_stale_and_pane_partial(self, mock_bot):
        """Stale cache (different question) + partial pane → defer like cache_empty.

        Regression scenario from the production log: a previous AUQ's
        tool_input still sits in ``_latest_ask_tool_input`` when Claude
        fires a NEW AUQ. ``resolve_ask_form`` tags the form with
        ``_meta["stale_fallback"]="1"`` and the defer guard must treat
        that the same as cache_empty — otherwise a partial 2-option card
        ships before the new tool_use lands in JSONL.
        """
        from cctelegram.handlers import interactive_ui as iui

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        # Cache holds the PREVIOUS AUQ — totally different question and
        # option labels from what the pane is now showing.
        iui.remember_ask_tool_input(
            window_id,
            {
                "questions": [
                    {
                        "question": "How should the v3 manifest model the timeline?",
                        "options": [
                            {"label": "multi-track"},
                            {"label": "single-track"},
                        ],
                    }
                ]
            },
        )

        # Live pane shows a NEW AUQ. Options 1-2 scrolled off the top
        # because the question prose is long; only options 3-4 are
        # captured. The strong-match check fails (no overlap with cached
        # labels), so resolve_ask_form tags stale_fallback and returns the
        # pane-only form.
        visible_pane = (
            "Fastest path to the CEO review you queued next; effort spent only "
            "where it actually moves the needle.\n"
            "\n"
            "❯ 3. Type something\n"
            "  4. Chat about this\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=visible_pane)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            result = await iui.handle_interactive_ui(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                from_poller=True,
            )

        # Deferred: no card sent, defer state recorded for next tick.
        assert result is False
        mock_bot.send_message.assert_not_called()
        assert window_id in iui._render_deferred_since

    @pytest.mark.asyncio
    async def test_poller_renders_stale_after_timeout_without_pick_buttons(
        self, mock_bot
    ):
        """Stale + partial + bounded-wait elapsed → force-render, NO pick buttons."""
        import time as _t

        from cctelegram.handlers import interactive_ui as iui

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        iui.remember_ask_tool_input(
            window_id,
            {
                "questions": [
                    {
                        "question": "How should the v3 manifest model the timeline?",
                        "options": [
                            {"label": "multi-track"},
                            {"label": "single-track"},
                        ],
                    }
                ]
            },
        )

        visible_pane = (
            "Fastest path to the CEO review.\n"
            "❯ 3. Type something\n"
            "  4. Chat about this\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        # Backdate the defer timestamp past the timeout.
        iui._render_deferred_since[window_id] = _t.monotonic() - 100.0

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=visible_pane)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            result = await iui.handle_interactive_ui(
                mock_bot,
                user_id=1,
                window_id=window_id,
                thread_id=42,
                from_poller=True,
            )

        assert result is True
        assert window_id not in iui._render_deferred_since
        mock_bot.send_message.assert_called()
        last_kw = mock_bot.send_message.call_args.kwargs
        markup = last_kw.get("reply_markup")
        if markup is not None:
            all_buttons = [b for row in markup.inline_keyboard for b in row]
            assert not any(
                getattr(b, "callback_data", "").startswith("aqp:") for b in all_buttons
            ), "Force-render of stale_fallback must not mint pick buttons"

    @pytest.mark.asyncio
    async def test_non_poller_stale_partial_renders_without_pick_buttons(
        self, mock_bot
    ):
        """Callback rerender path: stale cache + partial pane → no defer, no picks.

        The pick-button callback dispatches a keystroke, sleeps 500ms, and
        re-invokes handle_interactive_ui without ``tool_input=`` so it falls
        through to the cached ``_latest_ask_tool_input``. If the next AUQ
        is already live on the pane but JSONL hasn't flushed yet, the
        cached input is stale. We don't want to stall the rerender (the
        user is mid-interaction and expects feedback) but we MUST NOT
        mint pick buttons against an option list whose visible digits
        belong to the OLD question.
        """
        from cctelegram.handlers import interactive_ui as iui

        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        iui.remember_ask_tool_input(
            window_id,
            {
                "questions": [
                    {
                        "question": "How should the v3 manifest model the timeline?",
                        "options": [
                            {"label": "multi-track"},
                            {"label": "single-track"},
                        ],
                    }
                ]
            },
        )

        visible_pane = (
            "Fastest path to the CEO review.\n"
            "❯ 3. Type something\n"
            "  4. Chat about this\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        with (
            patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm_iu,
            patch("cctelegram.handlers.attention.session_manager") as mock_sm_att,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=visible_pane)
            mock_sm_iu.resolve_chat_id.return_value = 100
            mock_sm_att.resolve_chat_id.return_value = 100
            mock_sm_att.get_display_name.return_value = "topic"
            # Default from_poller=False — the callback rerender path.
            result = await iui.handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        # Renders (don't stall callback feedback) but no pick buttons.
        assert result is True
        # Non-poller path never records defer state.
        assert window_id not in iui._render_deferred_since
        mock_bot.send_message.assert_called()
        last_kw = mock_bot.send_message.call_args.kwargs
        markup = last_kw.get("reply_markup")
        if markup is not None:
            all_buttons = [b for row in markup.inline_keyboard for b in row]
            assert not any(
                getattr(b, "callback_data", "").startswith("aqp:") for b in all_buttons
            ), "Callback rerender on stale_fallback must not mint pick buttons"


@pytest.mark.usefixtures("_clear_pick_tokens")
class TestClearInteractiveMsgPrunesTokens:
    """P2.2 — clear_interactive_msg must drop pick-tokens for the cleared route."""

    @pytest.mark.asyncio
    async def test_clear_drops_tokens_for_active_window(self):
        user_id, thread_id, window_id = 42, 7, "@1"
        # Set up interactive mode so clear_interactive_msg sees the route.
        set_interactive_mode(user_id, window_id, thread_id)
        entry = _PickTokenEntry(
            window_id=window_id,
            user_id=user_id,
            thread_id=thread_id,
            fingerprint="fp1",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )
        token = _mint_pick_token(entry)
        cache_key = (user_id, thread_id, window_id, "fp1")
        _pick_token_cache[cache_key] = [token]
        assert token in _pick_tokens
        # bot=None → no Telegram I/O; the prune still runs.
        await clear_interactive_msg(user_id, bot=None, thread_id=thread_id)
        assert token not in _pick_tokens
        assert cache_key not in _pick_token_cache

    @pytest.mark.asyncio
    async def test_clear_leaves_other_routes_alone(self):
        # Two routes for the same user but different threads / windows.
        user_id = 42
        set_interactive_mode(user_id, "@1", 7)
        set_interactive_mode(user_id, "@2", 8)
        e1 = _PickTokenEntry(
            window_id="@1",
            user_id=user_id,
            thread_id=7,
            fingerprint="fp1",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )
        e2 = _PickTokenEntry(
            window_id="@2",
            user_id=user_id,
            thread_id=8,
            fingerprint="fp2",
            option_number=1,
            option_label="A",
            is_review_submit=False,
            expires_at=time.monotonic() + 60,
        )
        t1 = _mint_pick_token(e1)
        t2 = _mint_pick_token(e2)
        _pick_token_cache[(user_id, 7, "@1", "fp1")] = [t1]
        _pick_token_cache[(user_id, 8, "@2", "fp2")] = [t2]
        # Clear thread 7 only.
        await clear_interactive_msg(user_id, bot=None, thread_id=7)
        assert t1 not in _pick_tokens
        assert (user_id, 7, "@1", "fp1") not in _pick_token_cache
        # Thread 8 untouched.
        assert t2 in _pick_tokens
        assert (user_id, 8, "@2", "fp2") in _pick_token_cache

    @pytest.mark.asyncio
    async def test_clear_no_active_window_is_noop(self):
        # No prior set_interactive_mode → _interactive_mode pop returns None.
        # Clear must not raise; the prune loop just doesn't fire.
        await clear_interactive_msg(99, bot=None, thread_id=1)
        # No tokens to assert about; this is just a non-crash check.


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


# ── PR 3: multi-tab state machine, lock, generation guard, FA5+ gate ────


class TestAskToolInputDigest:
    def test_none_returns_none(self):
        from cctelegram.handlers.interactive_ui import _ask_tool_input_digest

        assert _ask_tool_input_digest(None) is None

    def test_content_based_equality(self):
        from cctelegram.handlers.interactive_ui import _ask_tool_input_digest

        # Structurally equal but distinct dict objects must produce equal
        # digests. Without content-based comparison the rerender_guard
        # would false-positive on every cache read.
        a = {"questions": [{"question": "Q", "options": [{"label": "A"}]}]}
        b = {"questions": [{"question": "Q", "options": [{"label": "A"}]}]}
        assert a is not b  # sanity — distinct objects
        assert _ask_tool_input_digest(a) == _ask_tool_input_digest(b)

    def test_content_changes_produce_different_digests(self):
        from cctelegram.handlers.interactive_ui import _ask_tool_input_digest

        a = {"questions": [{"question": "Q1", "options": [{"label": "A"}]}]}
        b = {"questions": [{"question": "Q2", "options": [{"label": "A"}]}]}
        assert _ask_tool_input_digest(a) != _ask_tool_input_digest(b)

    def test_no_guard_sentinel_distinct_from_none(self):
        from cctelegram.handlers.interactive_ui import _NO_GUARD

        # ``None`` is a real value (= "cache was cleared") and must be
        # distinguishable from ``_NO_GUARD`` (= "don't guard at all").
        assert _NO_GUARD is not None


class TestHasInteractiveSurface:
    def test_returns_false_when_neither_map(self):
        from cctelegram.handlers.interactive_ui import (
            _interactive_msgs,
            _multi_tab_sessions,
            has_interactive_surface,
        )

        _interactive_msgs.clear()
        _multi_tab_sessions.clear()
        assert has_interactive_surface(42, 7) is False

    def test_returns_true_for_single_card(self):
        from cctelegram.handlers.interactive_ui import (
            _interactive_msgs,
            _multi_tab_sessions,
            has_interactive_surface,
        )

        _interactive_msgs.clear()
        _multi_tab_sessions.clear()
        _interactive_msgs[(42, 7)] = 100
        try:
            assert has_interactive_surface(42, 7) is True
        finally:
            _interactive_msgs.clear()

    def test_returns_true_for_multi_tab(self):
        from cctelegram.handlers.interactive_ui import (
            _MultiTabSession,
            _interactive_msgs,
            _multi_tab_sessions,
            has_interactive_surface,
        )

        _interactive_msgs.clear()
        _multi_tab_sessions.clear()
        _multi_tab_sessions[(42, 7)] = _MultiTabSession(
            window_id="@1",
            shape_digest="x",
            message_ids=[1, 2, 3],
            current_tab_idx=0,
        )
        try:
            assert has_interactive_surface(42, 7) is True
        finally:
            _multi_tab_sessions.clear()


class TestPickButtonRowsFA5Gate:
    """FA5+ safety: multi-tab forms with current_tab_inferred=False MUST
    NOT mint pick buttons. The dispatched digit could answer the wrong
    tab in the live TUI.
    """

    def _multi_tab_form(self, inferred: bool):
        from cctelegram.terminal_parser import (
            AskOption,
            AskQuestion,
            AskUserQuestionForm,
        )

        q1 = AskQuestion(
            title="Q1?",
            header="A",
            options=(
                AskOption(label="alpha", recommended=False, cursor=False, number=1),
                AskOption(label="beta", recommended=False, cursor=False, number=2),
            ),
        )
        q2 = AskQuestion(
            title="Q2?",
            header="B",
            options=(
                AskOption(label="gamma", recommended=False, cursor=False, number=1),
                AskOption(label="delta", recommended=False, cursor=False, number=2),
            ),
        )
        return AskUserQuestionForm(
            tabs=(),
            current_question_title="Q1?",
            options=q1.options,
            questions=(q1, q2),
            current_tab_inferred=inferred,
        )

    def test_inferred_true_mints_buttons(self):
        from cctelegram.handlers.interactive_ui import _build_pick_button_rows

        rows = _build_pick_button_rows(
            user_id=1, thread_id=2, window_id="@1", form=self._multi_tab_form(True)
        )
        assert rows  # non-empty

    def test_inferred_false_returns_empty(self):
        from cctelegram.handlers.interactive_ui import _build_pick_button_rows

        rows = _build_pick_button_rows(
            user_id=1, thread_id=2, window_id="@1", form=self._multi_tab_form(False)
        )
        assert rows == []

    def test_single_question_form_ignores_inferred_flag(self):
        # Single-question forms always carry current_tab_inferred=True
        # by default; FA5+ only applies to multi-tab. Sanity-check that
        # a single-question form with inferred=False (artificial) still
        # gets buttons — the gate only fires for multi-tab.
        from cctelegram.handlers.interactive_ui import _build_pick_button_rows
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Pick.",
            options=(AskOption(label="A", recommended=False, cursor=False, number=1),),
            questions=(),  # single-question shape
            current_tab_inferred=False,
        )
        rows = _build_pick_button_rows(
            user_id=1, thread_id=2, window_id="@1", form=form
        )
        assert rows  # still gets buttons; FA5+ doesn't apply


class TestPickButtonRows19Cap:
    """Options 10+ render as text only — no pick button. Sending literal
    ``"10"`` would type ``1`` then ``0`` and dispatch wrong.
    """

    def test_options_10_plus_skipped(self):
        from cctelegram.handlers.interactive_ui import _build_pick_button_rows
        from cctelegram.terminal_parser import AskOption, AskUserQuestionForm

        opts = tuple(
            AskOption(
                label=f"opt {i}",
                recommended=False,
                cursor=(i == 1),
                number=i,
            )
            for i in range(1, 13)  # 1..12
        )
        form = AskUserQuestionForm(
            tabs=(),
            current_question_title="Pick.",
            options=opts,
        )
        rows = _build_pick_button_rows(
            user_id=1, thread_id=2, window_id="@1", form=form
        )
        # Flatten rows → button labels.
        labels = [btn.text for row in rows for btn in row]
        # Buttons exist for 1..9.
        for i in range(1, 10):
            assert any(f"{i}." in lab or lab.startswith(str(i)) for lab in labels), (
                f"missing button for option {i}: {labels}"
            )
        # No buttons for 10, 11, 12.
        for i in range(10, 13):
            assert not any(lab.startswith(f"{i}.") for lab in labels), (
                f"unexpected button for option {i}: {labels}"
            )


class TestMultiTabPostN:
    """First-render multi-tab flow: post one card per question."""

    @pytest.fixture
    def _clear_multi_state(self):
        from cctelegram.handlers.interactive_ui import (
            _interactive_mode,
            _interactive_msgs,
            _latest_ask_tool_input,
            _multi_tab_sessions,
            _route_locks,
        )

        _interactive_msgs.clear()
        _interactive_mode.clear()
        _multi_tab_sessions.clear()
        _latest_ask_tool_input.clear()
        _route_locks.clear()
        yield
        _interactive_msgs.clear()
        _interactive_mode.clear()
        _multi_tab_sessions.clear()
        _latest_ask_tool_input.clear()
        _route_locks.clear()

    @pytest.mark.skip(
        reason="Multi-tab dispatch disabled in handle_interactive_ui (2026-05-15) — "
        "user preferred the legacy single-card flow. The state-machine code remains "
        "in place; re-enable the dispatch in handle_interactive_ui to revive this test."
    )
    @pytest.mark.asyncio
    async def test_post_n_cards_for_multi_question(self, _clear_multi_state):
        """Multi-tab form posts N cards; current tab carries pick buttons,
        non-current tabs have no markup."""
        from cctelegram.handlers.interactive_ui import (
            _multi_tab_sessions,
            handle_interactive_ui,
            remember_ask_tool_input,
        )

        # Three-question form. Cache the JSONL payload so resolve_ask_form
        # picks it up.
        remember_ask_tool_input(
            "@multi",
            {
                "questions": [
                    {
                        "question": "Pick A.",
                        "options": [
                            {"label": "alpha", "description": "first"},
                            {"label": "beta", "description": "second"},
                        ],
                    },
                    {
                        "question": "Pick B.",
                        "options": [
                            {"label": "gamma", "description": "third"},
                            {"label": "delta", "description": "fourth"},
                        ],
                    },
                    {
                        "question": "Pick C.",
                        "options": [
                            {"label": "epsilon", "description": "fifth"},
                            {"label": "zeta", "description": "sixth"},
                        ],
                    },
                ]
            },
        )

        # Pane points to the FIRST question — current_tab_idx will be 0.
        pane_text = (
            "Pick A.\n"
            "\n"
            "❯ 1. alpha\n"
            "  2. beta\n"
            "\n"
            "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
        )

        # Mock the bot: each topic_send returns a fresh fake message_id.
        bot = AsyncMock()
        sent_counter = [100]

        async def fake_send_message(*args, **kwargs):
            sent_counter[0] += 1
            msg = MagicMock()
            msg.message_id = sent_counter[0]
            return msg

        bot.send_message.side_effect = fake_send_message

        with patch("cctelegram.handlers.interactive_ui.tmux_manager") as mock_tmux:
            window_mock = MagicMock()
            window_mock.window_id = "@multi"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window_mock)
            mock_tmux.capture_pane = AsyncMock(return_value=pane_text)

            with patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm:
                mock_sm.resolve_chat_id = MagicMock(return_value=12345)
                mock_sm.resolve_session_for_window = AsyncMock(return_value=None)
                with patch(
                    "cctelegram.handlers.interactive_ui.session_id_for_window",
                    return_value="sess-1",
                ):
                    result = await handle_interactive_ui(
                        bot, user_id=7, window_id="@multi", thread_id=42
                    )

        assert result is True
        # 3 cards sent (one per question).
        assert bot.send_message.call_count == 3
        # Session recorded all 3 message_ids.
        session = _multi_tab_sessions.get((7, 42))
        assert session is not None
        assert len(session.message_ids) == 3
        assert session.current_tab_idx == 0
        # Current tab (card 0) has reply_markup; others don't.
        calls = bot.send_message.call_args_list
        # First call: current tab → reply_markup present.
        assert calls[0].kwargs.get("reply_markup") is not None
        # Subsequent cards: no markup.
        assert calls[1].kwargs.get("reply_markup") is None
        assert calls[2].kwargs.get("reply_markup") is None


class TestClearInteractiveMsgWalksBothMaps:
    @pytest.mark.asyncio
    async def test_clear_walks_multi_tab_message_ids(self):
        from cctelegram.handlers.interactive_ui import (
            _MultiTabSession,
            _interactive_msgs,
            _multi_tab_sessions,
            _route_locks,
            clear_interactive_msg,
        )

        # Seed both maps for one route.
        _interactive_msgs.clear()
        _multi_tab_sessions.clear()
        _route_locks.clear()
        _interactive_msgs[(42, 7)] = 50
        _multi_tab_sessions[(42, 7)] = _MultiTabSession(
            window_id="@1",
            shape_digest="x",
            message_ids=[100, 101, 102],
            current_tab_idx=1,
        )

        bot = AsyncMock()
        deleted_ids: list[int] = []

        async def fake_delete_message(chat_id, message_id, **kwargs):
            deleted_ids.append(message_id)

        bot.delete_message.side_effect = fake_delete_message

        with patch("cctelegram.handlers.interactive_ui.session_manager") as mock_sm:
            mock_sm.resolve_chat_id = MagicMock(return_value=12345)
            with patch("cctelegram.handlers.interactive_ui.attention") as mock_att:
                mock_att.dismiss = AsyncMock()
                await clear_interactive_msg(42, bot, 7)

        # Single card AND all 3 multi-tab cards deleted.
        assert sorted(deleted_ids) == [50, 100, 101, 102]
        # Both maps cleared.
        assert (42, 7) not in _interactive_msgs
        assert (42, 7) not in _multi_tab_sessions

        _route_locks.clear()
