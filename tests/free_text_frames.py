"""The GH #50 PR-2 rig frames, grouped by the CARD GENERATION they belong to.

THE POINT OF THIS MODULE. The free-text executor commits the user's prose into a
LIVE card, and the one thing it must never do is commit it into a DIFFERENT card
than the one the message was answering — another controller (the poller, an AFK
auto-resolve, a button tap, Claude itself) can resolve card A and render card B
while the executor is navigating or typing. Every other proof in the transaction
(the dim placeholder, the typed-state SGR-2 flip, the payload tail, the row-active
footer) is satisfied *just as well* by card B holding our text.

So the tests must be able to hand the executor **a different real card, mid
transaction**. The rig corpus already contains exactly that, because the captures
were taken across several sessions:

    AUQ card X   options [Blue, Green, Red]      pretype · typed_large · overflow
    AUQ card Y   options [Red, Blue, Green]      single_picker · typed · identical
    EPM plan P   ~/.claude/plans/…idempotent-pond      pretype · typed_large
    EPM plan Q   ~/.claude/plans/fancy-wibbling-quasar typed · identical
    EPM plan S   ~/.claude/plans/…warm-sedgewick       gate_epm

A happy path therefore has to chain ONE generation end-to-end (mixing them is the
bug), and a wrong-card test hands the verifier the OTHER generation's REAL bytes.

TWO FRAMES ARE DERIVED, not captured — the corpus has no "card X with the cursor
on row 1" and no "card X with a SHORT answer typed". Both derivations are
mechanical, byte-level, and reproduce exactly what Claude Code itself does:

    _move_cursor_to_row  — the ❯ glyph moves between numbered rows, and the SGR-2
                           dim is dropped from the row it leaves (CC dims the
                           placeholder ONLY while it is the selected row).
    _type_into_row       — the row's dim placeholder becomes the user's text at
                           normal intensity: ``ESC[2m ESC[39m <label> ESC[0m``
                           becomes ``ESC[39m <text>``.

Both are PINNED byte-identical against the REAL captured frames in
``test_free_text_parser.py`` — the derivation is not trusted, it is verified.
"""

from __future__ import annotations

import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "cctelegram" / "fixtures"
V = "v2.1.207"

_RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_RE_ROW = re.compile(r"^\s*[❯›▶*]?\s*(\d+)\.\s")

# The exact byte template Claude Code renders for a selected row's ❯ (identical
# on both surfaces — verified against the AUQ and EPM pretype captures).
CURSOR_GLYPH = "\x1b[38;5;153m❯\x1b[39m "
# The dim (SGR-2) placeholder label of an untyped affordance row.
_RE_DIM_LABEL = re.compile(r"\x1b\[2m\x1b\[39m(?P<label>[^\x1b]*)\x1b\[0m")


def fx(name: str) -> str:
    return (FIXTURES / name).read_text()


def plain(ansi: str) -> str:
    """The plain-text view the executor parses — the SAME seam it uses.

    The frames are ANSI captures (SGR-2 is the typed-state proof), so any test
    that calls a PARSER directly must clean them exactly as ``free_text._plain``
    does; the executor itself is handed the raw ANSI and cleans internally.
    """
    from cctelegram import terminal_parser

    return terminal_parser.clean_ghost_input_text(ansi)


def _row_number(line: str) -> int | None:
    m = _RE_ROW.match(_RE_ANSI.sub("", line))
    return int(m.group(1)) if m else None


def move_cursor_to_row(ansi: str, number: int) -> str:
    """Move the live ❯ onto numbered row ``number``.

    De-cursoring swaps the glyph template back for the two spaces it replaced
    (which restores the row's original indent exactly — 5 spaces on EPM, 2 on
    AUQ) and drops the SGR-2, because CC dims the placeholder only while it is
    selected. Cursoring does the inverse. Pure byte surgery on real captures.
    """
    out: list[str] = []
    for line in ansi.split("\n"):
        n = _row_number(line)
        if n is None:
            out.append(line)
            continue
        if CURSOR_GLYPH in line:
            line = line.replace(CURSOR_GLYPH, "  ", 1).replace("\x1b[2m", "", 1)
        if n == number:
            esc = line.index("\x1b")
            line = line[: esc - 2] + CURSOR_GLYPH + line[esc:]
        out.append(line)
    return "\n".join(out)


def type_into_row(ansi: str, number: int, text: str) -> str:
    """Replace row ``number``'s DIM placeholder with ``text`` at normal intensity.

    Exactly what typing does: the label becomes the user's text and the SGR-2
    goes away. Single-visual-line only (a wrapped multi-line draft is covered by
    the REAL ``*_typed_large_*`` captures, so no derivation is needed there).
    """
    first = next((ln for ln in text.split("\n") if ln.strip()), text)
    out: list[str] = []
    for line in ansi.split("\n"):
        if _row_number(line) == number and "\x1b[2m" in line:
            line = _RE_DIM_LABEL.sub("\x1b[39m" + first.replace("\\", "\\\\"), line)
        out.append(line)
    return "\n".join(out)


# ── AUQ card X — the generation with a pretype, a big typed, and an overflow ──

AUQ_X_LANDED = fx(f"auq_freetext_row_selected_pretype_{V}.ansi.txt")  # ❯4, DIM
AUQ_X_LIVE = move_cursor_to_row(AUQ_X_LANDED, 1)  # derived: ❯1, pre-nav
AUQ_X_ANSWER = "teal, actually"
AUQ_X_TYPED = type_into_row(AUQ_X_LANDED, 4, AUQ_X_ANSWER)  # derived: ❯4, PLAIN
AUQ_X_TYPED_BIG = fx(f"auq_freetext_row_typed_large_{V}.ansi.txt")  # the 947-char
AUQ_X_OVERFLOW = fx(f"auq_freetext_overflow_{V}.txt")  # option block gone

# ── AUQ card Y — a DIFFERENT question with the SAME 3-option geometry ─────────

AUQ_Y_LIVE = fx(f"auq_single_picker_{V}.txt")  # ❯1
AUQ_Y_TYPED = fx(f"auq_freetext_row_typed_{V}.ansi.txt")  # ❯4, PLAIN

AUQ_RESOLVED = fx(f"auq_after_answer_t5_{V}.txt")  # the surface is GONE

# ── EPM plan P / plan Q / plan S ─────────────────────────────────────────────

# The plan-file slugs the real footers carry. They are the path half of the EPM
# surface anchor; the CONTENT of the file at that path is the other half (see
# ``free_text._epm_plan_generation``), which is what makes the anchor an
# OCCURRENCE token rather than a mere name.
EPM_P_PLAN_PATH = "~/.claude/plans/write-a-one-paragraph-plan-idempotent-pond.md"
EPM_Q_PLAN_PATH = "~/.claude/plans/fancy-wibbling-quasar.md"
EPM_S_PLAN_PATH = "~/.claude/plans/make-a-very-short-warm-sedgewick.md"

EPM_P_LANDED = fx(f"epm_freetext_row_selected_pretype_{V}.ansi.txt")  # ❯4, DIM
EPM_P_LIVE = move_cursor_to_row(EPM_P_LANDED, 1)  # derived: ❯1
EPM_P_ANSWER = "please name it farewell.txt instead"
EPM_P_TYPED = type_into_row(EPM_P_LANDED, 4, EPM_P_ANSWER)  # derived: ❯4, PLAIN
EPM_P_TYPED_BIG = fx(f"epm_freetext_row_typed_large_{V}.ansi.txt")

EPM_Q_TYPED = fx(f"epm_freetext_row_typed_{V}.ansi.txt")  # a DIFFERENT plan
EPM_S_LIVE = fx(f"gate_epm_{V}.txt")  # a THIRD plan, ❯1

EPM_OVERFLOW = fx(f"epm_freetext_overflow_{V}.ansi.txt")  # footer gone
EPM_RESOLVED = fx(f"epm_after_approve_t5_{V}.txt")


_RE_PLAN_PATH = re.compile(r"~/\.claude/plans/\S+\.md")


def retarget_plan_path(ansi: str, new_path: str) -> str:
    """Rewrite the ``~/.claude/plans/<slug>.md`` path in an EPM footer.

    THE ROUND-2 P1's REAL SHAPE. Re-entering ExitPlanMode after REVISING the same
    plan commonly keeps the SAME slug — Claude rewrites the file in place — so
    plan P and its revision render an IDENTICAL footer path. Every EPM also
    renders the SAME three real options, so the pane identity is identical too.
    The corpus's plan Q is a genuinely different real capture (different body,
    different slug); pointing its footer at plan P's path reproduces exactly the
    successor a revision produces, byte-for-byte real everywhere else.
    """
    return _RE_PLAN_PATH.sub(new_path, ansi, count=1)


# Plan Q's REAL bytes, wearing plan P's slug: the pane identity AND the footer
# path both match plan P. Only the plan file's CONTENT tells them apart.
EPM_Q_TYPED_AT_P_PATH = retarget_plan_path(EPM_Q_TYPED, EPM_P_PLAN_PATH)

# The owner's real shape: a 947-char, 9-line voice note carrying a REPLY-CONTEXT
# quote (rig-captured; Enter committed all 947 chars, JSONL-verified). This is
# the exact text whose typed render IS ``*_typed_large_*``.
BIG_ANSWER = (
    '> Re: "the picker card you posted a moment ago"\n>\n'
    "> Claude asked: What's your favorite color?\n\n"
    "OK so about the colour question, I have been thinking about this for a "
    "while and I want to give you the full reasoning rather than just picking "
    "one of the three options you offered, because none of them is quite right "
    "on its own.\n\n"
    "I would actually prefer a deep teal, somewhere between blue and green, "
    "because it reads well on both light and dark backgrounds and it does not "
    "fight with the orange accent we already use in the header. Blue on its own "
    "is too corporate and cold, green on its own reads too much like a success "
    "state, and red is completely out of the question for anything that is not "
    "an error.\n\n"
    "So please go with teal as the primary, keep the existing orange as the "
    "accent, and make sure the contrast ratio stays above four point five to "
    "one for body text. If teal is impossible for some reason, fall back to "
    "blue, but tell me why first.\n"
)

# The tail of the ~5.3 k answer whose typed render IS ``auq_freetext_overflow``.
OVERFLOW_ANSWER = (
    "Paragraph six. I want to walk you through the reasoning in detail "
    "because the short answer is misleading and I would rather you "
    "understand the constraints than guess at them from a single word. The "
    "palette has to work on light and dark, it has to survive being printed "
    "in greyscale, and it has to keep a contrast ratio above four point five "
    "to one for body text everywhere it is used, including the small print "
    "in the footer."
)
