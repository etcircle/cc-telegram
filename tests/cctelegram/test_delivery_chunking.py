"""GH #84 — the byte-capped chunk planner (``delivery.chunk_literal``).

CC >= 2.1.246 keeps only the LAST <= 1022-byte pty read of a single
``tmux send-keys -l`` burst and silently discards everything before it (a
1429-char voice transcription arrived as ``payload[1022:]``). Both inbound seams
therefore type an above-cap payload as several small writes.

The planner is a pure function, so it is pinned exhaustively rather than through
the seams: the properties below hold for EVERY payload it accepts, and the two
seam-level behaviours (the chunk cadence, the ``payload_too_large`` refusal) are
pinned in ``test_delivery_gate.py``.

``hypothesis`` is not a dependency of this repo, so the property sweep is a
SEEDED ``random`` generator — deterministic, and its corpus deliberately mixes
the shapes the edge repairs exist for: digit-heavy prose, ``\\n`` runs,
whitespace-only lines, emoji and CJK.
"""

from __future__ import annotations

import random

import pytest

from cctelegram import delivery


CAP = delivery.LITERAL_WRITE_MAX_BYTES
HARD = delivery.LITERAL_WRITE_HARD_MAX_BYTES


# ── The invariants every accepted plan must satisfy ──────────────────────


def _assert_plan_is_sound(text: str, chunks: list[str]) -> None:
    assert "".join(chunks) == text, "the plan must reassemble the payload exactly"

    n = len(text)
    # At or below the cap the planner returns ``[text]`` BYTE-IDENTICALLY — the
    # pre-#84 single write, including an all-newlines payload. The edge repairs
    # are properties of the CHUNKING path only.
    chunked = len(text.encode()) > CAP
    offset = 0
    for chunk in chunks:
        assert chunk or text == "", "only the empty payload may plan an empty write"
        assert len(chunk.encode()) <= HARD
        # Python string indices ARE character boundaries, so this can only fail
        # if a chunk were ever built from bytes.
        assert chunk.encode().decode() == chunk
        assert not (chunked and all(c == "\n" for c in chunk)), (
            "a newlines-only write is a run of bare Enters at the pty"
        )

        # No chunk may carry a bare-digit LINE the payload did not already have
        # at that offset: a bare digit is a live HOTKEY and the seam's refusal
        # reads the RAW payload, so a MINTED one would slip past it.
        pos = offset
        for line in chunk.split("\n"):
            start, end = pos, pos + len(line)
            if len(line) == 1 and line.isascii() and line.isdigit():
                original_full_line = (start == 0 or text[start - 1] == "\n") and (
                    end == n or text[end] == "\n"
                )
                assert original_full_line, (
                    f"minted a bare-digit line at {start}:{end} of {text!r}"
                )
            pos = end + 1  # + the "\n" the split consumed
        offset += len(chunk)


def _random_payload(rng: random.Random) -> str:
    alphabets = [
        "abcdefghijklmnopqrstuvwxyz ",
        "0123456789 ",
        "0123456789",
        "日本語テキスト",
        "🙂🚀✅",
        "   \t".replace("\t", " "),
    ]
    parts: list[str] = []
    for _ in range(rng.randint(1, 60)):
        roll = rng.random()
        if roll < 0.30:
            parts.append("\n" * rng.randint(1, 30))
        elif roll < 0.40:
            parts.append(" " * rng.randint(1, 8) + "\n")
        elif roll < 0.50:
            parts.append(str(rng.randint(0, 9)) + "\n")
        else:
            alphabet = rng.choice(alphabets)
            parts.append(
                "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 90)))
            )
    return "".join(parts)


def test_generated_payloads_plan_soundly() -> None:
    rng = random.Random(84)
    planned = 0
    above_cap = 0
    for _ in range(2500):
        text = _random_payload(rng)
        chunks = delivery.chunk_literal(text)
        if chunks is None:
            # Only an untypable newline shape may be refused.
            assert "\n" in text
            continue
        planned += 1
        if len(text.encode()) > CAP:
            above_cap += 1
        _assert_plan_is_sound(text, chunks)
    assert planned > 2000
    assert above_cap > 500, "the corpus must actually exercise the chunking path"


# ── The byte-identical fast paths ────────────────────────────────────────


def test_empty_payload_keeps_todays_single_no_op_write() -> None:
    assert delivery.chunk_literal("") == [""]


@pytest.mark.parametrize(
    "text",
    [
        "hello claude",
        "> Re: the card\n>\n> which colour?\n\nTeal, please.\n",
        "x" * CAP,
        "\n" * 100,  # all-newlines BELOW the cap — passes through as today
        "日本語" * 50,
    ],
)
def test_at_or_below_cap_is_one_byte_identical_write(text: str) -> None:
    assert len(text.encode()) <= CAP
    assert delivery.chunk_literal(text) == [text]


# ── The refusals (``payload_too_large``) ─────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "\n" * 901 + "x",  # a newline run no chunk can carry under the hard max
        "\n" * 600,  # all newlines, above the cap
    ],
)
def test_untypable_newline_shapes_are_refused(text: str) -> None:
    assert len(text.encode()) > CAP
    assert delivery.chunk_literal(text) is None


def test_the_cap_is_a_parameter_not_a_hard_coded_512() -> None:
    text = "abcdefghij" * 10
    chunks = delivery.chunk_literal(text, 25)
    assert chunks is not None
    assert [len(c) for c in chunks] == [25, 25, 25, 25]
    assert "".join(chunks) == text


def test_a_newline_run_that_still_fits_the_hard_max_is_planned() -> None:
    text = "\n" * 600 + "x"
    chunks = delivery.chunk_literal(text)
    assert chunks is not None
    _assert_plan_is_sound(text, chunks)
    assert chunks == [text]  # one chunk, grown past the soft cap by the repair
    assert len(chunks[0].encode()) <= HARD


# ── The edge repairs, spelled out ────────────────────────────────────────


def test_a_trailing_newline_run_is_absorbed_by_the_last_chunk() -> None:
    text = "a" * 1000 + "\n" * 40
    chunks = delivery.chunk_literal(text)
    assert chunks is not None
    _assert_plan_is_sound(text, chunks)
    assert not any(all(c == "\n" for c in chunk) for chunk in chunks)


def test_a_cut_inside_a_newline_run_is_allowed() -> None:
    """Rig R6: leading / trailing / mid-payload blank runs all committed exactly
    with ONE Enter, so the planner does NOT keep a run atomic."""
    text = "a" * 500 + "\n" * 20 + "b" * 600
    chunks = delivery.chunk_literal(text)
    assert chunks is not None
    _assert_plan_is_sound(text, chunks)
    assert chunks[0].endswith("\n")
    assert chunks[1].startswith("\n")


def test_a_mid_line_digit_is_never_cut_into_a_lone_digit_line() -> None:
    # Engineered so the greedy 512-byte cut lands immediately before the "7",
    # which is followed by a newline: cutting there would MINT the line "7".
    text = "a" * 511 + "x7\n" + "b" * 600
    assert delivery.lone_hotkey_line_free_text(text) is None
    chunks = delivery.chunk_literal(text)
    assert chunks is not None
    _assert_plan_is_sound(text, chunks)
    assert delivery.lone_hotkey_line_free_text(chunks[1]) is None


def test_a_chunk_never_closes_on_a_digit_whose_line_continues() -> None:
    text = "a" * 500 + "\n" + "7abcdef" * 100
    assert delivery.lone_hotkey_line_free_text(text) is None
    chunks = delivery.chunk_literal(text)
    assert chunks is not None
    _assert_plan_is_sound(text, chunks)


def test_an_original_bare_digit_line_survives_intact() -> None:
    """Not the planner's problem: the seam's raw-line hotkey check refuses the
    whole payload first, with today's reason and copy."""
    text = "a" * 600 + "\n7\n" + "b" * 600
    chunks = delivery.chunk_literal(text)
    assert chunks is not None
    _assert_plan_is_sound(text, chunks)
    assert delivery.lone_hotkey_line(text) == "7"


# ── The bash-mode composition ────────────────────────────────────────────


def test_plan_gate_segments_reproduces_the_bash_two_step() -> None:
    assert delivery.plan_gate_segments("!") == ["!"]
    assert delivery.plan_gate_segments("!echo hi") == ["!", "echo hi"]


def test_plan_gate_segments_chunks_the_bash_remainder() -> None:
    rest = "z" * 1500
    segments = delivery.plan_gate_segments("!" + rest)
    assert segments is not None
    assert segments[0] == "!"
    assert "".join(segments[1:]) == rest
    assert all(len(s.encode()) <= CAP for s in segments)


def test_plan_gate_segments_propagates_the_refusal() -> None:
    assert delivery.plan_gate_segments("!" + "\n" * 901 + "x") is None


# ── The hotkey rules (gate vs free-text lane) ────────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected"),
    [("1", "1"), ("!1", "1"), ("a\n2\nb", "2"), ("12", None)],
)
def test_lone_hotkey_line_is_unchanged_by_gh84(payload: str, expected) -> None:
    assert delivery.lone_hotkey_line(payload) == expected


def test_the_free_text_variant_drops_the_bash_split_only() -> None:
    # The lane never emits a lone "!", so "!1" is prose there…
    assert delivery.lone_hotkey_line_free_text("!1") is None
    # …but a bare-digit LINE is still a live hotkey.
    assert delivery.lone_hotkey_line_free_text("1") == "1"
    assert delivery.lone_hotkey_line_free_text("a\n2\nb") == "2"


# ── The two real payload shapes from the incident + the rig ──────────────

_PROSE = "The quick brown fox jumps over the lazy dog. "


@pytest.mark.parametrize(("length", "chunks"), [(1429, 3), (9135, 18)])
def test_the_incident_payload_sizes_chunk_as_the_rig_measured(
    length: int, chunks: int
) -> None:
    """1429 = the truncated voice note (rig: committed as ``payload[1022:]`` in a
    single burst); 9135 = the worst historic payload (rig R2: 18 chunks, 9135
    committed exactly, ONE Enter)."""
    text = (_PROSE * (length // len(_PROSE) + 1))[:length]
    planned = delivery.chunk_literal(text)
    assert planned is not None
    _assert_plan_is_sound(text, planned)
    assert len(planned) == chunks
