"""Callback data constants for Telegram inline keyboards.

Defines all CB_* prefixes used for routing callback queries in the bot.
Each prefix identifies a specific action or navigation target.

Also a pure-constants leaf (no package imports — stdlib ``dataclasses`` + ``re``
only), so it is the cycle-safe home for the strict pick-callback parser
(``parse_pick_callback``) that disambiguates the legacy token-keyed shape from
the new stateless self-describing shape purely from the wire grammar.

Constants:
  - CB_HISTORY_*: History pagination
  - CB_DIR_*: Directory browser navigation
  - CB_WIN_*: Window picker (bind existing unbound window)
  - CB_SCREENSHOT_*: Screenshot refresh
  - CB_ASK_*: Interactive UI navigation (arrows, enter, esc)
  - CB_KEYS_PREFIX: Screenshot control keys (kb:<key_id>:<window>)

Pick-callback parsing:
  - LegacyPickCallback / StatelessPickCallback: the two parsed pick shapes
  - parse_pick_callback: strict grammar-only disambiguator
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# History pagination
CB_HISTORY_PREV = "hp:"  # history page older
CB_HISTORY_NEXT = "hn:"  # history page newer

# Directory browser
CB_DIR_SELECT = "db:sel:"
CB_DIR_UP = "db:up"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"
CB_DIR_BIND_EXISTING = "db:bind"  # switch to window picker (opt-in)

# Window picker (bind existing unbound window)
CB_WIN_BIND = "wb:sel:"  # wb:sel:<index>
CB_WIN_NEW = "wb:new"  # proceed to directory browser
CB_WIN_CANCEL = "wb:cancel"

# Screenshot
CB_SCREENSHOT_REFRESH = "ss:ref:"

# Interactive UI (aq: prefix kept for backward compatibility)
CB_ASK_UP = "aq:up:"  # aq:up:<window>
CB_ASK_DOWN = "aq:down:"  # aq:down:<window>
CB_ASK_LEFT = "aq:left:"  # aq:left:<window>
CB_ASK_RIGHT = "aq:right:"  # aq:right:<window>
CB_ASK_ESC = "aq:esc:"  # aq:esc:<window>
CB_ASK_ENTER = "aq:enter:"  # aq:enter:<window>
CB_ASK_SPACE = "aq:spc:"  # aq:spc:<window>
CB_ASK_TAB = "aq:tab:"  # aq:tab:<window>
CB_ASK_REFRESH = "aq:ref:"  # aq:ref:<window>
# Structured option pick (PR 2b). The callback carries a short token that
# resolves server-side to the (window, fingerprint, option_number,
# option_label) bound when the keyboard was minted. Token-keyed instead of
# embedding state in the 64-byte callback_data — same shape as the
# attention-card flow in handlers/attention.py. Multi-select toggles use
# the same keyed token shape but dispatch only a bare digit and do not ledger.
#
# Two wire shapes coexist after the prefix (see parse_pick_callback):
#   legacy:    <route_hash>:<fp8>:<opt>:<token>      (token-keyed, in-memory lookup)
#   stateless: <route_hash>:<fp16>:u<owner5>:<opt>   (self-describing, no lookup)
# The constant VALUES are unchanged; the renderer still emits the legacy shape.
CB_ASK_PICK = "aqp:"  # aqp:<route_hash>:<fp8>:<opt>:<token> | aqp:<route_hash>:<fp16>:u<owner5>:<opt>
CB_ASK_TOGGLE = "aqt:"  # aqt:<route_hash>:<fp8>:<opt>:<token> | aqt:<route_hash>:<fp16>:u<owner5>:<opt>

# Session picker (resume existing session)
CB_SESSION_SELECT = "rs:sel:"  # rs:sel:<index>
CB_SESSION_NEW = "rs:new"  # start a new session
CB_SESSION_CANCEL = "rs:cancel"  # cancel

# Effort level picker (intercepts bare `/effort` in Telegram)
# window_id is embedded so a stale button after topic rebind is rejected.
CB_EFFORT = "eff:"  # eff:<level>:<window_id>  e.g. eff:xhigh:@28

# Screenshot control keys
CB_KEYS_PREFIX = "kb:"  # kb:<key_id>:<window>


# ── Strict pick-callback parser (plan v3 §2) ─────────────────────────────────
#
# After the ``aqp:``/``aqt:`` prefix is stripped (caller strips it), the payload
# splits on ``:`` into EXACTLY 4 fields ``(rh8, f2, f3, f4)``. The two shapes are
# disambiguated purely from the wire grammar with no in-memory lookup:
#
#   legacy:    rh8(8hex) : fp8(8hex)   : opt(\d{1,2})    : token(12hex)
#   stateless: rh8(8hex) : fp16(16hex) : u + owner5(5hex): opt(\d{1,2})
#
# Disambiguation is airtight: field-2 length 8 (legacy) vs 16 (stateless); a
# stateless field-3 always starts with ``u`` so an all-digit ``owner5`` still
# parses stateless. A 6-hex legacy token (the v2 wrong assumption) is malformed.
# Lowercase ASCII hex only.
#
# Matched with ``fullmatch`` (NOT ``match`` + ``^...$``): Python ``$`` matches
# *before* a trailing ``\n``, so ``match(r"^...$", "deadbeef\n")`` would wrongly
# accept a field carrying an embedded newline. ``fullmatch`` requires the whole
# field consumed. ``opt`` uses ``[0-9]`` not ``\d`` so a Unicode digit (which
# ``\d`` matches and ``int()`` may accept) cannot slip through. (Codex+Hermes P2.)
_RE_RH8 = re.compile(r"[0-9a-f]{8}")
_RE_FP8 = re.compile(r"[0-9a-f]{8}")
_RE_FP16 = re.compile(r"[0-9a-f]{16}")
_RE_OWNER5 = re.compile(r"u[0-9a-f]{5}")
_RE_OPT = re.compile(r"[0-9]{1,2}")
_RE_TOKEN12 = re.compile(r"[0-9a-f]{12}")


@dataclass(frozen=True)
class LegacyPickCallback:
    """Parsed legacy token-keyed pick callback (server-side token lookup)."""

    route_hash: str  # rh8,  [0-9a-f]{8}
    fp8: str  #            [0-9a-f]{8}
    option_number: int
    token: str  #          [0-9a-f]{12}  (secrets.token_hex(6))


@dataclass(frozen=True)
class StatelessPickCallback:
    """Parsed stateless self-describing pick callback (no in-memory lookup)."""

    route_hash: str  # rh8,  [0-9a-f]{8}
    fp16: str  #           [0-9a-f]{16}  (live_form.fingerprint(), FULL)
    owner5: str  #         the 5 hex AFTER the literal 'u'  ([0-9a-f]{5})
    option_number: int


def parse_pick_callback(
    payload: str,
) -> LegacyPickCallback | StatelessPickCallback | None:
    """Disambiguate a prefix-stripped pick payload by its wire grammar.

    ``payload`` is the callback string AFTER the ``aqp:``/``aqt:`` prefix is
    stripped (the caller strips it). Returns the matching dataclass, or ``None``
    for anything malformed.
    """
    parts = payload.split(":")
    if len(parts) != 4:
        return None
    rh8, f2, f3, f4 = parts
    if not _RE_RH8.fullmatch(rh8):
        return None
    if _RE_FP16.fullmatch(f2) and _RE_OWNER5.fullmatch(f3) and _RE_OPT.fullmatch(f4):
        return StatelessPickCallback(
            route_hash=rh8, fp16=f2, owner5=f3[1:], option_number=int(f4)
        )
    if _RE_FP8.fullmatch(f2) and _RE_OPT.fullmatch(f3) and _RE_TOKEN12.fullmatch(f4):
        return LegacyPickCallback(
            route_hash=rh8, fp8=f2, option_number=int(f3), token=f4
        )
    return None


def checked_callback_data(data: str) -> str:
    """Return callback data unchanged, or raise if it exceeds Telegram's limit.

    Lives in this dependency-free leaf module (not on the heavy
    ``callback_dispatcher`` package facade) so that ``interactive_ui`` and
    ``history`` can validate callback payloads without importing the
    dispatcher package — which would otherwise close the
    interactive_ui ↔ callback_dispatcher ↔ inbound_telegram import cycle.
    Mirrors the ``INTERACTIVE_TOOL_NAMES`` relocation onto ``route_runtime``.
    """
    if len(data.encode("utf-8")) > 64:
        raise RuntimeError(f"callback_data exceeds Telegram 64-byte limit: {data!r}")
    return data
