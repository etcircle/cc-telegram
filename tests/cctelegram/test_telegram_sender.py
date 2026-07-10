"""Tests for telegram_sender.split_message."""

import pytest

from cctelegram.telegram_sender import split_message


class TestSplitMessage:
    @pytest.mark.parametrize(
        "text, expected",
        [
            pytest.param("hello world", ["hello world"], id="short_text"),
            pytest.param("", [""], id="empty_string"),
            pytest.param("a" * 4096, ["a" * 4096], id="exactly_4096_chars"),
        ],
    )
    def test_single_chunk_returned(self, text: str, expected: list[str]):
        assert split_message(text) == expected

    def test_split_on_newline_boundaries(self):
        line = "x" * 2000
        text = f"{line}\n{line}\n{line}"
        chunks = split_message(text)
        assert len(chunks) == 2
        assert chunks[0] == f"{line}\n{line}"
        assert chunks[1] == line

    def test_single_long_line_force_split(self):
        text = "a" * 8192
        chunks = split_message(text)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 4096
        assert chunks[1] == "a" * 4096

    def test_custom_max_length(self):
        text = "aaaa\nbbbb\ncccc"
        chunks = split_message(text, max_length=10)
        assert chunks == ["aaaa\nbbbb", "cccc"]

    def test_custom_max_length_force_split(self):
        text = "a" * 120
        chunks = split_message(text, max_length=50)
        assert len(chunks) == 3
        assert chunks[0] == "a" * 50
        assert chunks[1] == "a" * 50
        assert chunks[2] == "a" * 20

    def test_trailing_newline_handling(self):
        text = "line1\nline2"
        chunks = split_message(text, max_length=50)
        assert len(chunks) == 1
        assert chunks[0] == "line1\nline2"

    def test_mixed_lines_grouping(self):
        short = "short"
        long_line = "x" * 60
        text = f"{short}\n{long_line}\n{short}"
        chunks = split_message(text, max_length=50)
        assert len(chunks) == 4
        assert chunks[0] == short
        assert chunks[1] == "x" * 50
        assert chunks[2] == "x" * 10
        assert chunks[3] == short

    @pytest.mark.parametrize(
        "text, max_len, expected_count",
        [
            pytest.param("a\nb\nc\nd\ne", 4, 3, id="many_short_lines"),
            pytest.param("ab\ncd\nef\ngh", 6, 2, id="pairs_of_lines"),
        ],
    )
    def test_chunk_count(self, text: str, max_len: int, expected_count: int):
        chunks = split_message(text, max_length=max_len)
        assert len(chunks) == expected_count

    def test_all_chunks_within_max_length(self):
        lines = [f"line-{i:04d} " + "x" * 40 for i in range(100)]
        text = "\n".join(lines)
        chunks = split_message(text, max_length=200)
        for chunk in chunks:
            assert len(chunk) <= 200

    def test_code_block_split_closes_and_reopens(self):
        """When splitting inside a code block, close with ``` and reopen."""
        code = "```python\n" + "\n".join(f"line{i}" for i in range(20)) + "\n```"
        chunks = split_message(code, max_length=60)
        assert len(chunks) > 1
        # First chunk should end with ``` (closing the block)
        assert chunks[0].endswith("```")
        # Second chunk should start with ```python (reopening)
        assert chunks[1].startswith("```python")
        # Last chunk should end with ``` (the original close)
        assert chunks[-1].rstrip().endswith("```")

    def test_code_block_not_split_fits(self):
        """Code block that fits in one chunk should not be modified."""
        code = "```python\nprint('hi')\n```"
        chunks = split_message(code, max_length=100)
        assert chunks == [code]

    def test_text_before_and_after_code_block(self):
        """Text around a code block that gets split."""
        text = "before\n```js\nvar x = 1;\nvar y = 2;\n```\nafter"
        chunks = split_message(text, max_length=30)
        # Every chunk should have balanced ``` pairs or none
        for chunk in chunks:
            fence_count = chunk.count("```")
            assert fence_count % 2 == 0, f"Unbalanced fences in: {chunk!r}"

    def test_multiple_code_blocks(self):
        """Multiple code blocks should each be handled independently."""
        text = "text\n```py\na=1\n```\nmid\n```sh\nls\n```\nend"
        chunks = split_message(text, max_length=30)
        for chunk in chunks:
            fence_count = chunk.count("```")
            assert fence_count % 2 == 0, f"Unbalanced fences in: {chunk!r}"

    @pytest.mark.parametrize(
        "text,max_length,forced_payload",
        [
            pytest.param(
                "```py\n" + "a" * 24 + "\nb\n```",
                32,
                None,
                id="plain-line-overflow-inside-fence",
            ),
            pytest.param(
                "prefix-line\n```python\nvalue = 1\n```",
                16,
                None,
                id="overflow-on-opening-fence",
            ),
            pytest.param(
                "```py\n" + "x" * 18 + "\n```\nafter",
                27,
                None,
                id="overflow-on-closing-fence",
            ),
            pytest.param(
                "```text\n" + "z" * 90 + "\n```",
                32,
                "z",
                id="overlong-line-inside-fence",
            ),
            pytest.param(
                "```python\n" + "\n".join("x" * 19 for _ in range(8)) + "\n```",
                32,
                None,
                id="long-fence-and-near-budget-lines",
            ),
        ],
    )
    def test_fence_boundary_regressions(
        self, text: str, max_length: int, forced_payload: str | None
    ):
        chunks = split_message(text, max_length=max_length)

        assert all(len(chunk) <= max_length for chunk in chunks)
        assert all(chunk.count("```") % 2 == 0 for chunk in chunks)
        if forced_payload is not None:
            assert all(
                chunk.count("```") == 2 for chunk in chunks if forced_payload in chunk
            )

    @pytest.mark.parametrize("max_length", [32, 100, 4096])
    @pytest.mark.parametrize(
        "text",
        [
            "before\n```py\n" + "x" * 5000 + "\n```\nafter",
            "```python\n" + "\n".join("y" * 95 for _ in range(60)) + "\n```",
            "lead\n```\nalpha\n```\nmid\n```js\nbeta\n```\ntail",
            # This apparent delimiter cannot be preserved whole at the
            # smallest budget. The splitter deliberately treats it as plain
            # text; Markdown balance is disclosed as degraded in that case.
            "```" + "language" * 20 + "\npayload\n```",
        ],
    )
    def test_adversarial_fence_budget_invariant(self, text: str, max_length: int):
        chunks = split_message(text, max_length=max_length)
        apparent_delimiter_over_budget = any(
            line.strip().startswith("```") and len(line) > max_length
            for line in text.split("\n")
        )

        assert all(len(chunk) <= max_length for chunk in chunks)
        assert all(
            chunk.count("```") % 2 == 0 or apparent_delimiter_over_budget
            for chunk in chunks
        )
