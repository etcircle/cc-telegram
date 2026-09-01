"""Shared fixtures for the whole CC Telegram test suite + Wave A scenario harness.

The import-time environment bootstrap for ``cctelegram.config`` lives in the
repository-root ``conftest.py`` so it runs before any test collection.

This file hosts the **scenario harness** used by ``tests/scenarios/*`` —
black-box tests that drive the bot from the public Telegram seam through the
real handler stack to ``tmux_manager`` / ``session_manager``, with no
monkeypatch of handler internals in *test bodies*.

Reset-seam note: handler modules expose a co-located ``reset_for_tests()``
seam next to the state it resets — ``message_queue.reset_for_tests()`` and
``interactive_ui.reset_for_tests()`` join the existing
``route_runtime`` / ``auq_ledger`` / ``attention`` seams. ``_reset_all_handler_state``
calls those seams directly. ``inbound_aggregator`` and ``status_polling``
still have small fixture-side clears below (their module state is a couple of
caches); keeping any residual reset code in this file — not in test bodies —
preserves the kill-criterion signal: scenarios fail the bar only when the
*tests themselves* must reach into handler internals.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# B2.4 default flip (2026-07-11): production defaults for the two DETECTOR
# flags are now ON; the suite floor pins them OFF so the existing scenario /
# unit baseline stays today-shaped (the output_prefs verbose-pin precedent).
# Tests that assert the NEW unset-env default delenv these and call
# terminal_parser.reset_for_tests() locally.
os.environ["CC_TELEGRAM_PERMISSION_PROMPTS"] = "false"
os.environ["CC_TELEGRAM_DECISION_CARDS"] = "false"
# GH #65: the same floor posture for the folder-trust CREATION lane. ``0``
# disables it, so every pre-existing scenario keeps the pre-#65 inline
# wait-then-kill creation path. The lane's own tests restore a real ceiling by
# monkeypatching ``config.trust_prompt_ceiling_s``.
os.environ["CC_TELEGRAM_TRUST_PROMPT_CEILING_S"] = "0"

from cctelegram import bot as bot_module
from cctelegram.handlers import inbound_telegram as inbound_module
from cctelegram import route_runtime, terminal_parser, transcript_event_adapter
from cctelegram import session as session_module
from cctelegram.session import session_manager as _real_sm
from cctelegram import tmux_manager as tmux_mod
from cctelegram.tmux_manager import TmuxWindow, tmux_manager as _real_tmux
from cctelegram.utils import app_dir
from tests.cctelegram._adoption_protocol import AdoptionProtocolMixin
from cctelegram.handlers import (
    artifacts,
    attention,
    auq_ledger,
    auq_source,
    dashboard,
    inbound_aggregator,
    interactive_ui,
    late_answer,
    message_queue,
    pick_intent,
    pick_token,
    status_polling,
    trust_flow,
    usage_cache,
)

# GH #65 review r8 P3: arm the trust lane's invariant assertions for the WHOLE
# suite, at import. Arming them inside ``reset_for_tests`` alone made strictness
# test-ORDER dependent — the first test in a process, and any single test run in
# isolation (which is how a failure actually gets reproduced), ran with the
# invariants merely logging, so the lane's safety net was inert in exactly the
# run where it matters most.
trust_flow.enable_strict_invariants()
# GH #65 r15 P2-B: a lifecycle hold that grows past the declared ceiling
# invalidates the DERIVED kill bound, so make it raise for the whole suite.
tmux_mod.enable_strict_lifecycle_invariants()


# ──────────────────────────────────────────────────────────────────────────
# Fake tmux substrate
# ──────────────────────────────────────────────────────────────────────────


# GH #50: every payload delivery is now GATED on POSITIVE structural proof that
# the pane is at Claude Code's ready input box (``pane_input_box_present``) AND
# on ``pane_command_is_claude`` (the strict version-string fullmatch). The fake
# substrate must therefore model a REALISTIC pane by default, or every scenario
# send would (correctly) refuse. These are the real CC 2.1.207 rig captures.
_FIXTURES_DIR = Path(__file__).parent / "cctelegram" / "fixtures"


def pane_fixture(name: str) -> str:
    """Read one real captured tmux pane from ``tests/cctelegram/fixtures/``."""
    return (_FIXTURES_DIR / name).read_text()


IDLE_PANE_V2_1_207 = pane_fixture("inputbox_idle_v2.1.207.txt")
# The version-string shape ``pane_current_command`` reports while the TUI runs.
CLAUDE_PANE_COMMAND = "2.1.207"


def auq_single_picker_pane() -> str:
    """A LIVE AskUserQuestion single-select picker (CC 2.1.207 rig)."""
    return pane_fixture("auq_single_picker_v2.1.207.txt")


@dataclass
class _PaneWindow:
    """In-memory representation of one fake tmux window."""

    window_id: str
    window_name: str
    cwd: str = "/tmp/test"
    pane_text: str = ""
    pane_text_ansi: str = ""
    pane_current_command: str = CLAUDE_PANE_COMMAND


@dataclass
class FakeTmux(AdoptionProtocolMixin):
    """Stand-in for ``tmux_manager`` used by scenario tests.

    Fixture binds these methods onto the real ``tmux_manager`` singleton so
    every consumer (``bot.py``, ``session_monitor``, ``handlers/*``) sees the
    fake regardless of import order.
    """

    windows: dict[str, _PaneWindow] = field(default_factory=dict)
    sent_keys: list[tuple[str, str, bool, bool]] = field(default_factory=list)
    kill_calls: list[str] = field(default_factory=list)
    rename_calls: list[tuple[str, str]] = field(default_factory=list)
    create_calls: list[dict[str, Any]] = field(default_factory=list)
    # GH #65: the launch-deferred creation substrate. ``probe_version_response``
    # is what the in-pane ``--version`` probe resolves to (None ⇒ the trust card
    # degrades to display-only); ``launch_calls`` records the deferred launch.
    probe_version_response: str | None = None
    probe_calls: list[str] = field(default_factory=list)
    launch_calls: list[str] = field(default_factory=list)
    create_response: tuple[bool, str] | None = None  # override for failure injection
    send_keys_response: bool | None = None  # override for failure injection
    # GH #50: fired right AFTER a LITERAL (Enter-withheld) write, so a scenario
    # can script the WRITE → RE-VERIFY race — the one window the delivery
    # transaction genuinely closes, and the only way to reach ``draft_written``
    # from the public Telegram seam.
    on_write: Any | None = None
    # GH #65 review r1 (P1-2): fired at the TOP of ``list_windows``, so a
    # scenario can script the "a binding / creation flow appears while the
    # handler is still building the directory browser" race — the one gap the
    # locked decision has to close.
    on_list_windows: Any | None = None
    _next_id: int = 0

    # ── seeding helpers ────────────────────────────────────────────────
    def add_window(
        self,
        *,
        window_id: str | None = None,
        window_name: str,
        cwd: str = "/tmp/test",
        pane_text: str | None = None,
        pane_text_ansi: str = "",
    ) -> str:
        # GH #50: default to the REAL idle input-box pane so the delivery gate
        # passes. A test that wants a blocking surface passes it explicitly.
        if pane_text is None:
            pane_text = IDLE_PANE_V2_1_207
        if window_id is None:
            window_id = f"@{self._next_id}"
            self._next_id += 1
        elif window_id.startswith("@"):
            try:
                self._next_id = max(self._next_id, int(window_id[1:]) + 1)
            except ValueError:
                pass
        self.windows[window_id] = _PaneWindow(
            window_id=window_id,
            window_name=window_name,
            cwd=cwd,
            pane_text=pane_text,
            pane_text_ansi=pane_text_ansi or pane_text,
        )
        return window_id

    def set_pane(self, window_id: str, text: str, *, ansi: str | None = None) -> None:
        w = self.windows.get(window_id)
        if w:
            w.pane_text = text
            w.pane_text_ansi = ansi if ansi is not None else text

    def set_pane_command(self, window_id: str, cmd: str) -> None:
        """Override the window's ``pane_current_command`` (GH #50 proof of life)."""
        w = self.windows.get(window_id)
        if w:
            w.pane_current_command = cmd

    # ── GH #50 delivery-transaction views ──────────────────────────────
    @property
    def written_texts(self) -> list[str]:
        """Literal payload segments actually TYPED into a pane (Enter withheld)."""
        return [
            keys
            for _wid, keys, enter, literal in self.sent_keys
            if literal and not enter and keys
        ]

    @property
    def committed(self) -> bool:
        """True iff a bare Enter (the delivery commit key) was ever sent."""
        return any(
            keys == "" and enter and not literal
            for _wid, keys, enter, literal in self.sent_keys
        )

    def delivered(self, text: str) -> bool:
        """True iff ``text`` was typed AND committed with the Enter."""
        return text in self.written_texts and self.committed

    def _to_tmux_window(self, w: _PaneWindow) -> TmuxWindow:
        return TmuxWindow(
            window_id=w.window_id,
            window_name=w.window_name,
            cwd=w.cwd,
            pane_current_command=w.pane_current_command,
        )

    # ── tmux_manager interface (async) ─────────────────────────────────
    async def list_windows(self) -> list[TmuxWindow]:
        if self.on_list_windows is not None:
            hook, self.on_list_windows = self.on_list_windows, None
            await hook()
        return [self._to_tmux_window(w) for w in self.windows.values()]

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        w = self.windows.get(window_id)
        return self._to_tmux_window(w) if w else None

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        for w in self.windows.values():
            if w.window_name == window_name:
                return self._to_tmux_window(w)
        return None

    async def adoption_listing(self) -> Any:
        """The DIRECT adoption read (GH #65 r16) — never the cache."""
        return [self._to_tmux_window(w) for w in self.windows.values()]

    async def kill_window(self, window_id: str) -> bool:
        self.kill_calls.append(window_id)
        return self.windows.pop(window_id, None) is not None

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        self.rename_calls.append((window_id, new_name))
        w = self.windows.get(window_id)
        if w:
            w.window_name = new_name
            return True
        return False

    async def send_keys(
        self,
        window_id: str,
        keys: str,
        enter: bool = True,
        literal: bool = True,
    ) -> bool:
        self.sent_keys.append((window_id, keys, enter, literal))
        if literal and not enter and self.on_write is not None:
            self.on_write()
        if self.send_keys_response is not None:
            return self.send_keys_response
        return window_id in self.windows

    async def capture_pane(
        self,
        window_id: str,
        with_ansi: bool = False,
        scrollback_lines: int = 0,
    ) -> str:
        del scrollback_lines  # fake pane is whatever was set; no extra history
        w = self.windows.get(window_id)
        if not w:
            return ""
        return w.pane_text_ansi if with_ansi else w.pane_text

    async def pane_current_command(self, window_id: str) -> str | None:
        """GH #50: the delivery gate's proof-of-life query (every send)."""
        w = self.windows.get(window_id)
        return w.pane_current_command if w else None

    async def capture_pane_cancellation_safe(
        self,
        window_id: str,
        with_ansi: bool = False,
        scrollback_lines: int = 0,
    ) -> str:
        # The fake never hangs, so the cancellation-safe path is behaviorally
        # identical to capture_pane here (the reap-on-cancel logic is unit-tested
        # against the real subprocess mock in test_capture_pane_cancellation_safe).
        return await self.capture_pane(
            window_id, with_ansi=with_ansi, scrollback_lines=scrollback_lines
        )

    async def create_window(
        self,
        cwd: str,
        window_name: str | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, str, str]:
        self.create_calls.append({"cwd": cwd, "window_name": window_name, **kwargs})
        if self.create_response is not None:
            ok, msg = self.create_response
            if not ok:
                return False, msg, "", ""
        name = window_name or Path(cwd).name or "window"
        wid = self.add_window(window_name=name, cwd=cwd)
        return True, f"Created window '{name}' at {cwd}", name, wid

    async def probe_cli_version(
        self, window_id: str, *, timeout: float = 5.0
    ) -> str | None:
        """GH #65 Fix 0: the per-creation in-pane ``--version`` probe."""
        del timeout
        self.probe_calls.append(window_id)
        return self.probe_version_response

    async def launch_claude_in_window(
        self, window_id: str, *, resume_session_id: str | None = None
    ) -> bool:
        """GH #65 Fix 0: the launch a ``defer_launch`` create postponed."""
        del resume_session_id
        self.launch_calls.append(window_id)
        return window_id in self.windows

    async def get_or_create_session(self) -> Any:
        return MagicMock(name="fake-tmux-session")

    async def session_exists(self) -> bool:
        return True

    # Sometimes called by older paths
    async def get_session(self) -> Any:
        return MagicMock(name="fake-tmux-session")


# ──────────────────────────────────────────────────────────────────────────
# v2.1.168 AUQ keystroke-aware fake picker
# ──────────────────────────────────────────────────────────────────────────

_OPT_LINE = re.compile(r"^(\s*)(?:❯ |  )(\d+)\. (.*)$")
_RESOLVED_PANE = "user@host repo % \n"


def render_cursor(pane: str, cursor_number: int) -> str:
    """Return ``pane`` with the ``❯`` cursor relocated onto option ``cursor_number``.

    Models a .168 cursor move: every numbered option line (real options AND the
    ``Type something`` / ``Chat about this`` affordance rows) is re-prefixed with
    ``❯ `` for the target number and ``  `` otherwise. Putting the cursor on an
    affordance number reproduces the affordance-cursor parse (no real option is
    marked ``cursor`` — the wrap-hazard case).
    """
    out: list[str] = []
    for line in pane.split("\n"):
        m = _OPT_LINE.match(line)
        if m:
            indent, num, rest = m.group(1), m.group(2), m.group(3)
            prefix = "❯ " if int(num) == cursor_number else "  "
            out.append(f"{indent}{prefix}{num}. {rest}")
        else:
            out.append(line)
    return "\n".join(out)


@dataclass
class _Screen:
    """One picker screen the :class:`Fake168Picker` can show."""

    pane: str  # the fixture pane text (cursor relocated dynamically)
    n_real: int  # count of REAL (non-affordance) options the screen offers
    n_nav: int  # total navigable numbered rows (real + affordances) for wrap


class Fake168Picker:
    """Keystroke-aware fake of the Claude Code v2.1.168 single-select picker.

    Models the captured .168 keystroke semantics so RED tests can prove the bot's
    dispatch is correct WITHOUT the version-fragile bare digit:

      - ``Up``/``Down`` move the cursor by one navigable row, **wrapping** at the
        edges (``Up`` from option 1 wraps to the last affordance row — NOT clamped).
      - ``Enter`` selects the cursor's REAL option and ADVANCES to the next screen
        (the final screen advancing resolves the tool → a non-picker pane).
      - a bare digit (``literal=True``): in ``variant="A"`` it select+advances (the
        inline picker — used by the over-advance guard); in ``variant="B"`` it only
        moves the cursor (the notes-side-panel variant that broke the bare digit).

    ``capture_pane`` is STATEFUL: it renders the CURRENT screen with the cursor on
    the current position, so a dispatch that navigates then re-captures sees the
    moved cursor, and a post-Enter capture sees the advanced screen.
    """

    def __init__(
        self, window_id: str, screens: list[_Screen], *, variant: str = "A"
    ) -> None:
        self.window_id = window_id
        self.screens = screens
        self.variant = variant
        self.idx = 0
        self.cursor = 1
        self.sent: list[tuple[str, str, bool, bool]] = []

    # ── introspection ──────────────────────────────────────────────────
    def current_pane(self) -> str:
        if self.idx >= len(self.screens):
            return _RESOLVED_PANE
        return render_cursor(self.screens[self.idx].pane, self.cursor)

    @property
    def resolved(self) -> bool:
        return self.idx >= len(self.screens)

    def _advance(self) -> None:
        self.idx += 1
        self.cursor = 1

    # ── tmux_manager interface ─────────────────────────────────────────
    async def capture_pane(
        self, window_id: str, with_ansi: bool = False, scrollback_lines: int = 0
    ) -> str:
        del with_ansi, scrollback_lines
        if window_id != self.window_id:
            return ""
        return self.current_pane()

    async def find_window_by_id(self, window_id: str) -> Any:
        if window_id != self.window_id:
            return None
        return SimpleNamespace(window_id=self.window_id, window_name="repo")

    async def send_keys(
        self, window_id: str, keys: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.sent.append((window_id, keys, enter, literal))
        if window_id != self.window_id or self.resolved:
            return True
        scr = self.screens[self.idx]
        if keys == "Down":
            self.cursor = self.cursor + 1 if self.cursor < scr.n_nav else 1
        elif keys == "Up":
            self.cursor = self.cursor - 1 if self.cursor > 1 else scr.n_nav
        elif keys == "Enter":
            if 1 <= self.cursor <= scr.n_real:
                self._advance()
        elif literal and keys.isdigit():
            d = int(keys)
            if self.variant == "B":
                if 1 <= d <= scr.n_nav:
                    self.cursor = d  # navigate only — the .168 notes-panel break
            elif 1 <= d <= scr.n_real:  # variant A: select + advance
                self.cursor = d
                self._advance()
        return True


# ──────────────────────────────────────────────────────────────────────────
# Fake Telegram Bot
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class _SentMessage:
    """Record of one outbound Telegram call."""

    method: str  # "send_message", "edit_message_text", ...
    kwargs: dict[str, Any]
    message_id: int


class FakeBot:
    """Records all outbound Telegram calls; returns Message-shaped objects.

    Behaves like an ``AsyncMock`` for the Bot methods bot.py uses, but with a
    monotonic ``message_id`` counter and a structured ``sent`` log so scenario
    tests can assert against the conversation transcript.
    """

    def __init__(self, *, bot_id: int = 555_000_001) -> None:
        self.id = bot_id
        self.sent: list[_SentMessage] = []
        self._next_msg_id = 1000

    # ── primary I/O ────────────────────────────────────────────────────
    async def send_message(self, *, chat_id: int, **kwargs: Any) -> Any:
        return self._record("send_message", {"chat_id": chat_id, **kwargs})

    async def edit_message_text(
        self, *, chat_id: int, message_id: int, **kwargs: Any
    ) -> Any:
        return self._record(
            "edit_message_text",
            {"chat_id": chat_id, "message_id": message_id, **kwargs},
            message_id=message_id,
        )

    async def edit_message_caption(
        self, *, chat_id: int, message_id: int, **kwargs: Any
    ) -> Any:
        return self._record(
            "edit_message_caption",
            {"chat_id": chat_id, "message_id": message_id, **kwargs},
            message_id=message_id,
        )

    async def edit_message_reply_markup(
        self, *, chat_id: int, message_id: int, **kwargs: Any
    ) -> Any:
        return self._record(
            "edit_message_reply_markup",
            {"chat_id": chat_id, "message_id": message_id, **kwargs},
            message_id=message_id,
        )

    async def delete_message(self, *, chat_id: int, message_id: int) -> bool:
        self._record("delete_message", {"chat_id": chat_id, "message_id": message_id})
        return True

    async def send_chat_action(
        self, *, chat_id: int, action: str, **kwargs: Any
    ) -> bool:
        self._record(
            "send_chat_action", {"chat_id": chat_id, "action": action, **kwargs}
        )
        return True

    async def send_photo(self, *, chat_id: int, **kwargs: Any) -> Any:
        return self._record("send_photo", {"chat_id": chat_id, **kwargs})

    async def pin_chat_message(
        self, *, chat_id: int, message_id: int, **kwargs: Any
    ) -> bool:
        self._record(
            "pin_chat_message",
            {"chat_id": chat_id, "message_id": message_id, **kwargs},
        )
        return True

    async def send_document(self, *, chat_id: int, **kwargs: Any) -> Any:
        return self._record("send_document", {"chat_id": chat_id, **kwargs})

    async def send_voice(self, *, chat_id: int, **kwargs: Any) -> Any:
        return self._record("send_voice", {"chat_id": chat_id, **kwargs})

    async def answer_callback_query(self, *args: Any, **kwargs: Any) -> bool:
        if args and "callback_query_id" not in kwargs:
            kwargs["callback_query_id"] = args[0]
        self._record("answer_callback_query", kwargs)
        return True

    async def get_file(self, file_id: str) -> Any:
        f = MagicMock()
        f.file_id = file_id
        f.file_path = f"voice/{file_id}.oga"

        async def _download(out_path: Any) -> Any:
            Path(out_path).write_bytes(b"\x00")
            return out_path

        f.download_to_drive = AsyncMock(side_effect=_download)
        return f

    async def get_me(self) -> Any:
        return SimpleNamespace(id=self.id, username="cc_telegram_bot", is_bot=True)

    async def set_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def delete_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def get_my_commands(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    # ── helpers ────────────────────────────────────────────────────────
    def _record(
        self,
        method: str,
        kwargs: dict[str, Any],
        *,
        message_id: int | None = None,
    ) -> Any:
        if message_id is None:
            mid = self._next_msg_id
            self._next_msg_id += 1
        else:
            mid = message_id
        self.sent.append(_SentMessage(method=method, kwargs=kwargs, message_id=mid))
        # Return a Message-like object for handlers that capture the result.
        return SimpleNamespace(
            message_id=mid,
            chat_id=kwargs.get("chat_id"),
            text=kwargs.get("text"),
            caption=kwargs.get("caption"),
            reply_markup=kwargs.get("reply_markup"),
        )

    # Convenience filters for assertions.
    def texts(self) -> list[str]:
        return [
            s.kwargs.get("text") or s.kwargs.get("caption") or ""
            for s in self.sent
            if s.method in ("send_message", "edit_message_text", "edit_message_caption")
        ]

    def methods(self) -> list[str]:
        return [s.method for s in self.sent]


# ──────────────────────────────────────────────────────────────────────────
# Update / CallbackQuery factories — public Telegram seam
# ──────────────────────────────────────────────────────────────────────────


_DEFAULT_USER_ID = 12345
_DEFAULT_CHAT_ID = -1001234567890


def _make_chat(chat_id: int = _DEFAULT_CHAT_ID, chat_type: str = "supergroup") -> Any:
    chat = MagicMock(name="Chat")
    chat.id = chat_id
    chat.type = chat_type
    chat.is_forum = True
    chat.send_action = AsyncMock(return_value=True)
    chat.send_message = AsyncMock()
    return chat


def _make_user(user_id: int = _DEFAULT_USER_ID, *, is_bot: bool = False) -> Any:
    user = MagicMock(name="User")
    user.id = user_id
    user.is_bot = is_bot
    user.first_name = "Test"
    user.username = "tester"
    return user


def _make_message(
    *,
    text: str | None = None,
    caption: str | None = None,
    thread_id: int | None = None,
    chat_id: int = _DEFAULT_CHAT_ID,
    user_id: int = _DEFAULT_USER_ID,
    message_id: int = 100,
    photo: Any = None,
    voice: Any = None,
    document: Any = None,
    media_group_id: str | None = None,
    forum_topic_edited: Any = None,
    forum_topic_closed: Any = None,
    forum_topic_created: Any = None,
    reply_to_message: Any = None,
) -> Any:
    msg = MagicMock(name="Message")
    msg.message_id = message_id
    msg.text = text
    msg.caption = caption
    msg.message_thread_id = thread_id
    msg.is_topic_message = thread_id is not None
    msg.chat = _make_chat(chat_id=chat_id)
    msg.chat_id = chat_id
    msg.from_user = _make_user(user_id=user_id)
    msg.photo = photo or []
    msg.voice = voice
    msg.document = document
    msg.media_group_id = media_group_id
    msg.forum_topic_edited = forum_topic_edited
    msg.forum_topic_closed = forum_topic_closed
    msg.forum_topic_created = forum_topic_created
    msg.reply_to_message = reply_to_message
    # Async I/O on the Message object — make these awaitable so safe_reply /
    # safe_edit work against the real handler stack.
    msg.reply_text = AsyncMock(
        return_value=SimpleNamespace(
            message_id=message_id + 1,
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=None,
        )
    )
    msg.reply_html = AsyncMock(return_value=msg.reply_text.return_value)
    msg.reply_photo = AsyncMock(return_value=msg.reply_text.return_value)
    msg.reply_voice = AsyncMock(return_value=msg.reply_text.return_value)
    msg.reply_document = AsyncMock(return_value=msg.reply_text.return_value)
    msg.edit_text = AsyncMock(return_value=msg.reply_text.return_value)
    msg.edit_caption = AsyncMock(return_value=msg.reply_text.return_value)
    msg.delete = AsyncMock(return_value=True)
    return msg


def make_update_text(
    text: str,
    *,
    thread_id: int | None = None,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
    message_id: int = 100,
) -> Any:
    msg = _make_message(
        text=text,
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
    )
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user(user_id=user_id)
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


def make_update_topic_closed(
    *,
    thread_id: int,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
) -> Any:
    msg = _make_message(
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        forum_topic_closed=MagicMock(name="ForumTopicClosed"),
    )
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user(user_id=user_id)
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


def make_update_topic_renamed(
    new_name: str,
    *,
    thread_id: int,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
) -> Any:
    edited = MagicMock(name="ForumTopicEdited")
    edited.name = new_name
    msg = _make_message(
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        forum_topic_edited=edited,
    )
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user(user_id=user_id)
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


def make_update_callback(
    data: str,
    *,
    thread_id: int | None = None,
    message_id: int = 200,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
) -> Any:
    query = MagicMock(name="CallbackQuery")
    query.id = "cbq-1"
    query.data = data
    query.from_user = _make_user(user_id=user_id)
    query.answer = AsyncMock(return_value=True)
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.delete_message = AsyncMock()
    query.message = _make_message(
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
    )
    update = MagicMock(name="Update")
    update.message = None
    update.callback_query = query
    update.effective_user = _make_user(user_id=user_id)
    update.effective_chat = query.message.chat
    update.effective_message = query.message
    return update


def make_update_real_callback(
    data: str,
    *,
    bot: Any,
    thread_id: int | None = None,
    message_id: int = 200,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
) -> Any:
    """A callback Update built from REAL python-telegram-bot objects.

    ``inbound_telegram._create_and_bind_window`` asserts ``isinstance(query,
    CallbackQuery)``, so the creation flow cannot be driven with the MagicMock
    factory above. The objects are bound to the FakeBot, so ``query.answer`` and
    ``query.edit_message_text`` land in ``FakeBot.sent`` exactly like every other
    outbound call — no library monkeypatching in test bodies.
    """
    from datetime import datetime, timezone

    from telegram import CallbackQuery, Chat, Message, Update
    from telegram import User as TgUser

    tg_user = TgUser(id=user_id, first_name="Test", is_bot=False)
    chat = Chat(id=chat_id, type="supergroup", is_forum=True)
    message = Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=tg_user,
        message_thread_id=thread_id,
        is_topic_message=thread_id is not None,
    )
    message.set_bot(bot)
    query = CallbackQuery(
        id="cbq-real",
        from_user=tg_user,
        chat_instance="chat-instance",
        data=data,
        message=message,
    )
    query.set_bot(bot)
    update = Update(update_id=1, callback_query=query)
    update.set_bot(bot)
    return update


def make_update_command(
    command: str,
    *,
    args: str = "",
    thread_id: int | None = None,
    user_id: int = _DEFAULT_USER_ID,
    chat_id: int = _DEFAULT_CHAT_ID,
    message_id: int = 100,
) -> Any:
    text = f"/{command}" + (f" {args}" if args else "")
    msg = _make_message(
        text=text,
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        message_id=message_id,
    )
    msg.entities = [
        SimpleNamespace(type="bot_command", offset=0, length=len(f"/{command}"))
    ]
    update = MagicMock(name="Update")
    update.message = msg
    update.callback_query = None
    update.effective_user = _make_user(user_id=user_id)
    update.effective_chat = msg.chat
    update.effective_message = msg
    return update


def make_context(
    *,
    bot: Any,
    user_data: dict[str, Any] | None = None,
    user_id: int = _DEFAULT_USER_ID,
) -> Any:
    """Build a python-telegram-bot CallbackContext stand-in."""
    ctx = MagicMock(name="CallbackContext")
    ctx.bot = bot
    ctx.user_data = user_data if user_data is not None else {}
    ctx.chat_data = {}
    ctx.bot_data = {}
    ctx.application = MagicMock(name="Application")
    ctx.application.bot = bot
    ctx.application.user_data = {user_id: ctx.user_data}
    ctx.args = []
    return ctx


# ──────────────────────────────────────────────────────────────────────────
# State reset — clears module-level singletons between scenario tests.
# ──────────────────────────────────────────────────────────────────────────


def _reset_session_manager() -> None:
    """Empty the session_manager singleton's persisted dicts.

    SessionManager is a public dataclass; clearing its fields uses its public
    surface, not internals.
    """
    _real_sm.window_states.clear()
    _real_sm.user_window_offsets.clear()
    _real_sm.thread_bindings.clear()
    _real_sm.window_display_names.clear()
    _real_sm.group_chat_ids.clear()
    _real_sm.dashboards.clear()
    _real_sm.user_settings.clear()
    # GH #50 r2 F2: the module-level stranded-draft brake (the delivery-gate
    # sibling of the tmux quarantine registry) must not leak across tests.
    session_module.reset_stranded_drafts_for_tests()


def _reset_aggregator() -> None:
    agg = inbound_aggregator
    for name in ("_bundles", "_locks"):
        attr = getattr(agg, name, None)
        if isinstance(attr, dict):
            attr.clear()


def _reset_status_polling() -> None:
    sp = status_polling
    for name in (
        "_last_pane_capture",
        "_last_published_ui_hash",
        "_drift_remint_latch",
        "_absent_streak",
        "_prev_run_state",
        "_decision_card_eot_grace",
        "_epm_surface_first_seen_at",
    ):
        attr = getattr(sp, name, None)
        if isinstance(attr, dict):
            attr.clear()


def _reset_all_handler_state() -> None:
    ledger_path = app_dir() / auq_ledger.LEDGER_FILENAME
    try:
        ledger_path.unlink()
    except FileNotFoundError:
        pass
    # D2: unlink the durable pick-intent store by its REAL module constant (the
    # shared CC_TELEGRAM_DIR would otherwise leak rows across tests — and a stale
    # neighbor's intent could make a restart-recovery assertion pass for the wrong
    # reason). Keyed by the current constant, never a literal (test-reset-noop).
    try:
        (app_dir() / pick_intent.STORE_FILENAME).unlink()
    except FileNotFoundError:
        pass
    pending_dir = app_dir() / "auq_pending"
    if pending_dir.is_dir():
        for path in pending_dir.glob("*.json"):
            path.unlink(missing_ok=True)
    # Wave B: Notification-hook side files share the same leak surface.
    notify_dir = app_dir() / "notify_pending"
    if notify_dir.is_dir():
        for path in notify_dir.glob("*.json"):
            path.unlink(missing_ok=True)
    # The hook-written session map is real substrate too (the free-text anchor
    # resolves the window's session generation through it), so a leaked entry
    # would let one test's window resolve another's session.
    (app_dir() / "session_map.json").unlink(missing_ok=True)
    auq_ledger.reset_for_tests()
    route_runtime.reset_for_tests()
    transcript_event_adapter.reset_for_tests()
    # Re-read the CC_TELEGRAM_PERMISSION_PROMPTS gate-detection flag from the
    # environment so a scenario that enabled it (set_permission_prompts_enabled
    # / env) never leaks into the next scenario (the leaf autouse fixture in
    # tests/cctelegram/conftest.py covers unit tests; scenarios live elsewhere).
    terminal_parser.reset_for_tests()
    attention.reset_for_tests()
    message_queue.reset_for_tests()
    interactive_ui.reset_for_tests()
    # Wave A: the in-memory aql: late-answer card registry (R3 reset-seam
    # protocol — co-located reset called by direct module reference).
    late_answer.reset_for_tests()
    # Artifact delivery lane: the in-memory 📎 download-card registry + offer-dedup.
    artifacts.reset_for_tests()
    pick_token.reset_for_tests()
    pick_intent.reset_for_tests()
    auq_source.reset_for_tests()
    dashboard.reset_for_tests()
    # /cost + /usage overlay result cache (co-located reset seam).
    usage_cache.reset_for_tests()
    # GH #65: cancel + drop every folder-trust creation flow (its WAIT task is
    # loop-bound, so a leak across tests would outlive its event loop).
    trust_flow.reset_for_tests()
    # Re-inject the production JSONL-cache getter (bot.post_init wires this
    # once at startup, but post_init doesn't run under test). Without it the
    # ``jsonl_cache`` resolver branch would no-op and the render path would
    # silently lose the in-process ``_last_completed_ask_tool_input`` source —
    # a behavior divergence from production. Tests that need the no-op default
    # (getter-reset isolation) call ``auq_source.reset_for_tests()`` themselves.
    auq_source.set_jsonl_cache_getter(
        lambda wid: interactive_ui._last_completed_ask_tool_input.get(wid)
    )
    # GH #67: the one-per-route timestamp-less-block WARNING guard. Keyed by
    # (user, thread, window) — all of which repeat across scenarios — so a leak
    # would silently suppress the warning a later test asserts on.
    bot_module._TIMESTAMPLESS_BLOCK_WARNED.clear()
    _reset_aggregator()
    _reset_status_polling()
    _reset_session_manager()
    # Wave 3a: per-window send locks live on the tmux_manager singleton and
    # are loop-bound at first acquire — drop them between tests so a lock
    # created under a previous test's event loop never leaks forward.
    _real_tmux.reset_window_send_locks_for_tests()
    # GH #65 r12: the window-lifecycle lock is loop-bound at first acquire, for
    # the same reason the send locks are — drop it so a lock created under a
    # previous test's event loop never leaks forward.
    _real_tmux.reset_lifecycle_lock_for_tests()
    _real_tmux.reset_kill_pending_for_tests()


# ──────────────────────────────────────────────────────────────────────────
# Pytest fixtures
# ──────────────────────────────────────────────────────────────────────────


# Shell metacharacters and whitespace that separate one command word from the
# next — used to scan EVERY token of a shell string for a tmux reference.
_SHELL_TOKEN_RE = re.compile(r"[\s;&|()<>`]+")


class _NoLiveTmuxServer:
    """A libtmux server stand-in that refuses every attribute access."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"a test reached the REAL tmux server (libtmux .{name}). Inject a "
            "fake tmux manager, or request the ``fake_tmux`` fixture — a test "
            "must never address a live pane."
        )


@pytest.fixture(autouse=True)
def _no_live_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """POISON both routes a test could reach a real tmux server through.

    A test that injects a fake into the seam it calls DIRECTLY can still reach
    the real thing through a seam it forgot. That is what bit us: a unit test
    seeded a plausible window id into the live ``session_manager``, the
    completion tail replayed the pending payload through the REAL
    ``tmux_manager``, and the keystrokes landed in a live Claude session on the
    developer's default tmux server.

    Injection is the fix (see ``trust_flow._replay_for``); this is the backstop
    that makes the CLASS unreachable. Poisoning is at the CLASS/MODULE seams, so
    a FRESHLY-CONSTRUCTED ``TmuxManager()`` is covered too (review r9 P2-B — the
    per-instance ``_server`` poison missed every route a new instance takes):

    1. **libtmux**, at ``tmux_manager``'s own ``libtmux.Server`` symbol. Every
       manager — the singleton and any instance a test builds — acquires its
       server through that name, so this covers ``send_keys`` / ``kill_window``
       / ``rename_window`` / ``create_window`` and everything else that reaches
       tmux through ``asyncio.to_thread``. The singleton's already-cached
       ``_server`` is poisoned as well, since it was built before we got here.
    2. **The tmux BINARY**, via ``create_subprocess_exec`` AND
       ``create_subprocess_shell`` — how ``capture_pane`` and the pane-command
       probes actually run. Poisoned by argv, accepting ``str``, ``bytes`` and
       ``PathLike`` program arguments. The shell form does NOT try to identify
       which token is in executable position — it tokenizes shell-aware (so
       ``'tmux'`` quoted counts) and refuses if ANY token names a tmux
       executable. That deliberately over-blocks: ``echo tmux`` is refused too.
       For a test guard that is the correct direction — a false refusal is a
       loud, one-line test fix, while a false pass types into someone's live
       session. Non-tmux subprocesses are otherwise untouched (the
       ``md_capture`` appender benchmark spawns a real interpreter), and the
       tmux suites that patch these same attributes still override us.

    What this does NOT cover, stated plainly: a **synchronous** ``subprocess``
    call (``subprocess.run`` / ``Popen``) made from arbitrary code. Fencing that
    would mean poisoning ``subprocess`` process-wide, which breaks legitimate
    tooling the suite runs; production's tmux access does not take that route,
    so it is a known, named gap rather than a covered one.
    """
    monkeypatch.setattr(_real_tmux, "_server", _NoLiveTmuxServer(), raising=False)

    class _NoLiveTmuxServerFactory:
        """Stands in for ``libtmux.Server`` for every manager instance."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise AssertionError(
                "a test constructed a REAL libtmux Server. Inject a fake tmux "
                "manager, or request the ``fake_tmux`` fixture — a test must "
                "never address a live pane."
            )

    monkeypatch.setattr(
        tmux_mod.libtmux, "Server", _NoLiveTmuxServerFactory, raising=False
    )

    def _is_tmux(program: Any) -> bool:
        raw = program
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return Path(str(raw)).name in {"tmux", "tmux.exe"}

    real_exec = asyncio.create_subprocess_exec
    real_shell = asyncio.create_subprocess_shell

    async def _refuse_tmux_binary(program: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_tmux(program):
            raise RuntimeError("live tmux blocked in tests")
        return await real_exec(program, *args, **kwargs)

    async def _refuse_tmux_shell(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        raw = cmd.decode("utf-8", "replace") if isinstance(cmd, bytes) else str(cmd)
        # BOTH tokenizers, because neither is sufficient alone (review r11 P3):
        # ``shlex`` strips QUOTES (``'tmux' list-windows`` runs tmux just as
        # surely as the bare form) but does NOT split on shell metacharacters,
        # so ``(tmux list-windows)`` survives as the single token ``(tmux``.
        # The metacharacter split covers that but cannot see through quotes. So
        # shlex first, then split every token again on metacharacters, and check
        # all of them. Unbalanced quotes make shlex raise; falling back to the
        # metacharacter split alone is the fail-closed direction.
        try:
            shlex_tokens = shlex.split(raw, comments=False, posix=True)
        except ValueError:
            shlex_tokens = [raw]
        tokens = [
            piece
            for token in shlex_tokens
            for piece in _SHELL_TOKEN_RE.split(token)
            if piece
        ]
        if any(_is_tmux(token) for token in tokens):
            raise RuntimeError("live tmux blocked in tests")
        return await real_shell(cmd, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _refuse_tmux_binary)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _refuse_tmux_shell)


@pytest.fixture
def fake_tmux(monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    """Replace ``tmux_manager`` singleton methods with a fresh in-memory fake.

    Patches the bound methods on the real singleton so every module that
    already cached ``from .tmux_manager import tmux_manager`` sees the fake.
    """
    fake = FakeTmux()
    for name in (
        "list_windows",
        "find_window_by_id",
        "find_window_by_name",
        "kill_window",
        "rename_window",
        "send_keys",
        "capture_pane",
        "capture_pane_cancellation_safe",
        "pane_current_command",
        "create_window",
        "probe_cli_version",
        "launch_claude_in_window",
        "get_or_create_session",
        "get_session",
        "session_exists",
    ):
        if hasattr(fake, name):
            monkeypatch.setattr(_real_tmux, name, getattr(fake, name), raising=False)
    return fake


@pytest.fixture(autouse=True)
def _fast_delivery_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the GH #50 delivery-transaction settles for the whole suite.

    ``deliver_to_window`` reproduces ``send_keys``'s real timing (a 500 ms
    text→Enter settle, a 1 s ``!`` bash-mode settle) plus a 300 ms retry gap on
    an indeterminate frame. Real seconds in unit tests buy nothing; the timing
    ITSELF is pinned by ``test_delivery_gate.py`` against the module constants.
    """
    from cctelegram import delivery as delivery_module
    from cctelegram import session as session_module

    monkeypatch.setattr(session_module, "TEXT_SETTLE_S", 0.0)
    monkeypatch.setattr(session_module, "BASH_MODE_SETTLE_S", 0.0)
    monkeypatch.setattr(session_module, "GATE_RETRY_DELAY_S", 0.0)
    # GH #84: the same rule for the inter-chunk gap — an above-cap payload is
    # typed as N writes, and N-1 real gaps buy nothing in a unit test. The
    # cadence ITSELF is pinned in ``test_delivery_gate.py``.
    monkeypatch.setattr(delivery_module, "CHUNK_SETTLE_S", 0.0)


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture(autouse=True)
def _pin_default_verbosity():
    """Pin the suite-wide default preset to "verbose" (≡ pre-settings
    behavior) for EVERY test, deterministically — PR-2 flipped the
    production default to "standard" (plan v4 §9), and an order-dependent
    pin (reset-seam only) would let a solo-run unit test see the production
    default. Tests exercising the new presets set a stored user setting (or
    monkeypatch ``config.default_verbosity``) explicitly.
    """
    from cctelegram.config import config as _cfg

    prev = _cfg.default_verbosity
    _cfg.default_verbosity = "verbose"
    yield
    _cfg.default_verbosity = prev


@pytest.fixture
def fresh_handler_state() -> Any:
    """Wipe all handler module state before AND after the test.

    Scenario tests use this to start from a clean module surface without
    monkeypatching internals in the test body.
    """
    _reset_all_handler_state()
    yield
    _reset_all_handler_state()


@dataclass
class ScenarioHarness:
    """Driver object wiring together fake tmux, fake bot, and a fresh state.

    Scenario tests typically:

      1. ``h.add_window(...)`` to seed tmux.
      2. ``h.bind_thread(thread_id, window_id)`` to set up an existing topic.
      3. Build an Update via ``make_update_*`` helpers.
      4. Call the real bot handler (``bot_module.text_handler`` etc.) with the
         Update and ``h.context``.
      5. Assert on ``h.bot.sent`` / ``h.tmux.sent_keys`` / state.
    """

    tmux: FakeTmux
    bot: FakeBot
    session_manager: Any
    user_data: dict[str, Any]
    context: Any
    user_id: int = _DEFAULT_USER_ID
    chat_id: int = _DEFAULT_CHAT_ID

    def add_window(
        self,
        *,
        window_id: str | None = None,
        window_name: str,
        cwd: str = "/tmp/test",
        pane_text: str | None = None,
        pane_text_ansi: str = "",
    ) -> str:
        return self.tmux.add_window(
            window_id=window_id,
            window_name=window_name,
            cwd=cwd,
            pane_text=pane_text,
            pane_text_ansi=pane_text_ansi,
        )

    def bind_thread(
        self,
        thread_id: int,
        window_id: str,
        *,
        display_name: str | None = None,
        cwd: str = "/tmp/test",
        session_id: str = "",
    ) -> None:
        self.session_manager.thread_bindings.setdefault(self.user_id, {})[thread_id] = (
            window_id
        )
        if display_name is not None:
            self.session_manager.window_display_names[window_id] = display_name
        from cctelegram.session import WindowState

        self.session_manager.window_states[window_id] = WindowState(
            session_id=session_id,
            cwd=cwd,
            window_name=display_name
            or self.tmux.windows.get(window_id, _PaneWindow(window_id, "")).window_name,
        )
        self.session_manager.group_chat_ids[f"{self.user_id}:{thread_id}"] = (
            self.chat_id
        )
        if session_id:
            # …and the hook-written map, which is the AUTHORITY the GH #50 PR-2
            # free-text anchor reads (round-4 P1: the in-memory WindowState above
            # is only a MIRROR of it, refreshed on the monitor's poll cycle, so a
            # scenario that seeded only the mirror would let the reader resolve a
            # session the map never named). In production ``SessionStart`` writes
            # this BEFORE the session can render anything — so writing it here,
            # beside the binding, is the substrate behaving like the substrate.
            self._write_session_map_entry(window_id, session_id, cwd)

    def _write_session_map_entry(
        self, window_id: str, session_id: str, cwd: str
    ) -> None:
        from cctelegram.config import config
        from cctelegram.utils import app_dir, atomic_write_json

        path = app_dir() / "session_map.json"
        current: dict = {}
        if path.exists():
            try:
                current = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                current = {}
        if not isinstance(current, dict):
            current = {}
        current[f"{config.tmux_session_name}:{window_id}"] = {
            "session_id": session_id,
            "cwd": cwd,
            "window_name": self.session_manager.window_display_names.get(window_id, ""),
        }
        atomic_write_json(path, current)


@pytest.fixture
def scenario(
    fake_tmux: FakeTmux,
    fake_bot: FakeBot,
    fresh_handler_state: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> ScenarioHarness:
    """The Wave A scenario harness.

    Composes ``fake_tmux`` + ``fake_bot`` + a freshly-cleared session_manager
    + handler module state, and provides Update construction helpers.

    Also bypasses ``is_user_allowed`` so the default test user passes the
    allowlist gate without env-var configuration, and stubs
    ``resolve_session_for_window`` so JSONL-file-existence checks don't
    nuke ``window_states[*].session_id`` mid-test (the real path opens an
    on-disk transcript file we don't write in scenarios).
    """
    # `is_user_allowed` is canonically defined in
    # ``cctelegram.handlers.inbound_telegram`` and re-exported from ``bot``.
    # Patch both modules so allowlist bypass takes effect regardless of which
    # module's namespace the caller resolves through.
    monkeypatch.setattr(bot_module, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(inbound_module, "is_user_allowed", lambda _uid: True)

    from cctelegram.session import ClaudeSession

    async def _resolve_session_stub(window_id: str) -> ClaudeSession | None:
        state = _real_sm.window_states.get(window_id)
        if not state or not state.session_id:
            return None
        return ClaudeSession(
            session_id=state.session_id,
            summary="scenario-harness",
            message_count=0,
            file_path="",
        )

    monkeypatch.setattr(_real_sm, "resolve_session_for_window", _resolve_session_stub)

    # P1: the per-message / per-second hot paths now resolve the session PATH
    # (not the full ClaudeSession). The real resolver would glob a tmp app_dir
    # and, finding nothing, CLEAR window_states[*].session_id mid-scenario — the
    # exact nuke this stub prevents. Return "" (the pre-P1 file_path value) so
    # every migrated caller takes its no-usable-path branch without I/O.
    async def _resolve_session_path_stub(window_id: str) -> str | None:
        state = _real_sm.window_states.get(window_id)
        if not state or not state.session_id:
            return None
        return ""

    monkeypatch.setattr(
        _real_sm, "resolve_session_path_for_window", _resolve_session_path_stub
    )

    user_data: dict[str, Any] = {}
    context = make_context(bot=fake_bot, user_data=user_data)
    return ScenarioHarness(
        tmux=fake_tmux,
        bot=fake_bot,
        session_manager=_real_sm,
        user_data=user_data,
        context=context,
    )


# ──────────────────────────────────────────────────────────────────────────
# Re-exports so scenario tests can import factories from this conftest.
# ──────────────────────────────────────────────────────────────────────────


__all__ = [
    "FakeBot",
    "FakeTmux",
    "ScenarioHarness",
    "make_context",
    "make_update_callback",
    "make_update_command",
    "make_update_real_callback",
    "make_update_text",
    "make_update_topic_closed",
    "make_update_topic_renamed",
]
