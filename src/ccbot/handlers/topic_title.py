"""Helpers for context-window display + legacy topic-title cleanup.

Originally this module rewrote forum topic titles with a "· Nk/Mk" suffix.
That behavior was reverted on user request — the per-message footer in
``bot._build_context_footer`` is the surviving surface. Two pieces stay:

  - ``strip_ctx_suffix`` — defensive cleanup in ``topic_edited_handler``
    so any titles still bearing the legacy suffix get cleaned the next
    time the user (or the startup migration) edits them.
  - ``format_tokens`` / ``format_max`` — compact "113k", "1M" formatters
    shared by the message footer and the ``/context`` slash command.
"""

from __future__ import annotations

import re

# Matches the legacy trailing suffix this module used to append, e.g.
# " · 113k/200k" or " · 324k/1M". Tolerant of whitespace and the `·`/`•`
# separator. Also matches the original "ctx NN%" form. Used to clean any
# stale titles that still carry the suffix.
_RE_CTX_SUFFIX = re.compile(
    r"\s*[·•]\s*(?:ctx\s*\d{1,3}%|\d+(?:\.\d+)?[km]\s*/\s*\d+(?:\.\d+)?[km])\s*$",
    re.IGNORECASE,
)


def strip_ctx_suffix(name: str) -> str:
    """Remove our trailing ctx suffix from a topic name, if present."""
    return _RE_CTX_SUFFIX.sub("", name).rstrip()


def format_tokens(tokens: int) -> str:
    """Render a token count compactly: ``113k``, ``324k``, ``1.2M``."""
    if tokens >= 1_000_000:
        m = tokens / 1_000_000
        s = f"{m:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"
    return f"{round(tokens / 1000)}k"


def format_max(max_tokens: int) -> str:
    """Render the cap label: ``200k`` or ``1M``."""
    if max_tokens >= 1_000_000:
        return "1M"
    return f"{max_tokens // 1000}k"
