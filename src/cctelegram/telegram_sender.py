"""Message splitting utility for Telegram's 4096-character limit.

Provides:
  - split_message(): splits long text into Telegram-safe chunks (≤4096 chars),
    preferring newline boundaries and preserving code block integrity.
"""

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
_FENCE_CLOSE = "\n```"


def split_message(
    text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split a message into chunks that fit Telegram's length limit.

    Tries to split on newlines when possible to preserve formatting. When a
    split occurs inside a fenced code block, the block is closed at the end of
    the current chunk and re-opened at the start of the next chunk.

    Fence bookkeeping uses the state before and after each source line
    separately. An apparent delimiter longer than the entire chunk budget is
    treated as plain text because keeping it intact would violate the hard
    length invariant. At pathological budgets where close/re-open overhead
    leaves no payload room, continuation chunks degrade to unfenced text.
    """
    if max_length <= 0:
        raise ValueError("max_length must be positive")
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current = ""
    current_fenced = False
    in_code_block = False
    code_fence = ""

    def joined(line: str) -> str:
        return f"{current}\n{line}" if current else line

    def flush_current() -> None:
        nonlocal current, current_fenced
        if not current:
            return
        chunk = current
        if current_fenced and len(chunk) + len(_FENCE_CLOSE) <= max_length:
            chunk += _FENCE_CLOSE
        # If even the close overhead cannot fit, preserving the hard Telegram
        # budget wins and this chunk is deliberately emitted unfenced.
        chunks.append(chunk)
        current = ""
        current_fenced = False

    def reopen_prefix() -> str | None:
        prefix = f"{code_fence}\n"
        if len(prefix) + len(_FENCE_CLOSE) >= max_length:
            return None
        return prefix

    def force_split_plain_line(line: str, *, inside_fence: bool) -> None:
        """Split an overlong plain line, balancing pieces when affordable."""
        flush_current()
        prefix = reopen_prefix() if inside_fence else None
        if prefix is None:
            chunks.extend(
                line[offset : offset + max_length]
                for offset in range(0, len(line), max_length)
            )
            return

        payload_budget = max_length - len(prefix) - len(_FENCE_CLOSE)
        for offset in range(0, len(line), payload_budget):
            piece = line[offset : offset + payload_budget]
            chunks.append(f"{prefix}{piece}{_FENCE_CLOSE}")

    for line in text.split("\n"):
        pre_line_in_fence = in_code_block
        apparent_delimiter = line.strip().startswith("```")
        # An over-budget apparent delimiter cannot remain atomic. Reclassify
        # it as plain text, leave fence state untouched, and force-split it.
        delimiter = apparent_delimiter and len(line) <= max_length
        post_line_in_fence = not pre_line_in_fence if delimiter else pre_line_in_fence

        if not delimiter and len(line) > max_length:
            force_split_plain_line(line, inside_fence=pre_line_in_fence)
            in_code_block = post_line_in_fence
            continue

        if not current and pre_line_in_fence:
            prefix = reopen_prefix()
            reopened = f"{prefix or ''}{line}"
            if delimiter and prefix is not None and len(reopened) <= max_length:
                current = reopened
                current_fenced = False
            elif (
                not delimiter
                and prefix is not None
                and len(reopened) + len(_FENCE_CLOSE) <= max_length
            ):
                current = reopened
                current_fenced = True
            else:
                # Balanced reopen overhead cannot fit; keep the source state
                # but emit this continuation without Markdown fencing.
                chunks.append(line)
            in_code_block = post_line_in_fence
            continue

        candidate = joined(line)
        close_reserve = len(_FENCE_CLOSE) if pre_line_in_fence else 0
        opening_needs_own_close_room = delimiter and not pre_line_in_fence
        if len(candidate) + close_reserve > max_length or (
            opening_needs_own_close_room
            and len(candidate) + len(_FENCE_CLOSE) > max_length
        ):
            flush_current()

            if pre_line_in_fence:
                prefix = reopen_prefix()
                if delimiter:
                    reopened = f"{prefix or ''}{line}"
                    if prefix is not None and len(reopened) <= max_length:
                        current = reopened
                        current_fenced = False
                    else:
                        # Reopening cannot fit beside this closing delimiter.
                        # Emit the delimiter as a degraded, unfenced chunk.
                        current = line
                elif prefix is not None and (
                    len(prefix) + len(line) + len(_FENCE_CLOSE) <= max_length
                ):
                    current = f"{prefix}{line}"
                    current_fenced = True
                else:
                    # The line fits, but balanced fence overhead does not.
                    chunks.append(line)
            else:
                current = line
                if delimiter:
                    current_fenced = True
        else:
            current = candidate
            if delimiter:
                if pre_line_in_fence:
                    current_fenced = False
                else:
                    current_fenced = True
                    code_fence = line.strip()

        if delimiter and not pre_line_in_fence:
            code_fence = line.strip()
        in_code_block = post_line_in_fence

    flush_current()
    assert all(len(chunk) <= max_length for chunk in chunks)
    return chunks
