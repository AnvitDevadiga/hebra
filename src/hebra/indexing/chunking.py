"""Chunk types and line-window splitting shared by all parsers."""

from __future__ import annotations

from dataclasses import dataclass

from hebra.config import ChunkConfig


@dataclass(frozen=True)
class Chunk:
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    kind: str  # "function", "class", "method", "module", "text", ...
    symbol: str | None  # qualified name of the enclosing symbol, if any
    content: str


def split_lines(text: str) -> list[str]:
    """Split normalised text into lines; line ``n`` is ``lines[n - 1]``."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def window_chunks(
    lines: list[str],
    start_line: int,
    end_line: int,
    kind: str,
    symbol: str | None,
    config: ChunkConfig,
    window_lines: int | None = None,
) -> list[Chunk]:
    """Split ``lines[start_line..end_line]`` into bounded, non-overlapping chunks.

    A window closes at ``window_lines`` lines or ``config.max_chars``
    characters, whichever comes first. A single line longer than
    ``max_chars`` becomes its own chunk rather than being cut mid-line.
    Windows consisting only of blank lines are dropped.
    """
    limit = window_lines or config.max_lines
    chunks: list[Chunk] = []
    current: list[str] = []
    current_start = start_line
    chars = 0

    def flush(last_line: int) -> None:
        if current and any(line.strip() for line in current):
            chunks.append(Chunk(current_start, last_line, kind, symbol, "\n".join(current)))

    for line_no in range(start_line, end_line + 1):
        line = lines[line_no - 1]
        if current and (len(current) >= limit or chars + len(line) + 1 > config.max_chars):
            flush(line_no - 1)
            current = []
            current_start = line_no
            chars = 0
        current.append(line)
        chars += len(line) + 1
    flush(end_line)

    if len(chunks) > 1 and kind != "text":
        # Mark pieces of a symbol that was too large to keep whole.
        return [
            Chunk(c.start_line, c.end_line, f"{kind}-part", c.symbol, c.content) for c in chunks
        ]
    return chunks


def text_chunks(text: str, config: ChunkConfig) -> list[Chunk]:
    """Plain line-window chunking for files without a structural parser."""
    lines = split_lines(text)
    if not lines:
        return []
    return window_chunks(lines, 1, len(lines), "text", None, config, config.text_window_lines)
