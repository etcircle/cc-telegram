"""Scenario: /cost is a bot-owned interceptor, registered before the forwarder.

/cost is an alias of /usage on Claude Code 2.1.206 (identical full-screen
overlay that writes nothing to JSONL and matches no UI pattern). It MUST be
handled bot-side rather than forwarded raw into the pane — where it would open
the overlay invisibly and freeze the topic. PTB dispatches in registration
order within a group, so the /cost CommandHandler must precede the catch-all
command forwarder.
"""

from __future__ import annotations

import pytest
from telegram.ext import CommandHandler, MessageHandler

from cctelegram import bot as bot_module

pytestmark = pytest.mark.scenario


def test_cost_handler_registered_before_command_forwarder():
    app = bot_module.create_bot()
    handlers = app.handlers[0]
    cost_idx = next(
        (
            i
            for i, h in enumerate(handlers)
            if isinstance(h, CommandHandler) and "cost" in h.commands
        ),
        None,
    )
    assert cost_idx is not None, "/cost CommandHandler is not registered"
    fwd_idx = next(
        i
        for i, h in enumerate(handlers)
        if isinstance(h, MessageHandler)
        and h.callback is bot_module.forward_command_handler
    )
    assert cost_idx < fwd_idx
