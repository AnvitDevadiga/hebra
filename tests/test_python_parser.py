from __future__ import annotations

from conftest import SAMPLE_PY
from hebra.config import ChunkConfig
from hebra.indexing.chunking import split_lines
from hebra.indexing.python_parser import parse_python

CONFIG = ChunkConfig()


def test_extracts_symbols_with_nested_qualnames_and_kinds() -> None:
    parsed = parse_python(SAMPLE_PY, CONFIG)
    assert parsed is not None
    by_qualname = {s.qualname: s for s in parsed.symbols}
    assert by_qualname["ConfigLoader"].kind == "class"
    assert by_qualname["ConfigLoader.__init__"].kind == "method"
    assert by_qualname["ConfigLoader.load_file"].kind == "method"
    assert by_qualname["ConfigLoader.Cache"].kind == "class"
    assert by_qualname["ConfigLoader.Cache.get"].kind == "method"
    assert by_qualname["fetch_remote_config"].kind == "async_function"
    assert by_qualname["fetch_remote_config.<locals>.parse"].kind == "function"
    assert by_qualname["decorated_helper"].kind == "function"


def test_docstrings_and_line_spans() -> None:
    parsed = parse_python(SAMPLE_PY, CONFIG)
    assert parsed is not None
    lines = split_lines(SAMPLE_PY)
    by_qualname = {s.qualname: s for s in parsed.symbols}

    loader = by_qualname["ConfigLoader"]
    assert loader.docstring == "Load configuration files from disk."
    assert lines[loader.start_line - 1].startswith("class ConfigLoader")
    assert lines[loader.end_line - 1].strip() == "return None"

    load_file = by_qualname["ConfigLoader.load_file"]
    assert load_file.docstring == "Read one config file."
    assert by_qualname["ConfigLoader.__init__"].docstring is None

    # Decorator lines belong to the symbol span.
    helper = by_qualname["decorated_helper"]
    assert lines[helper.start_line - 1] == "@staticmethod"


def test_imports_include_relative_and_aliases() -> None:
    parsed = parse_python(SAMPLE_PY, CONFIG)
    assert parsed is not None
    assert [(i.module, i.name, i.alias, i.line) for i in parsed.imports] == [
        ("os", None, None, 3),
        ("pathlib", "Path", None, 4),
        (".", "helpers", "h", 5),
    ]


def test_chunks_follow_top_level_symbols_and_cover_code() -> None:
    parsed = parse_python(SAMPLE_PY, CONFIG)
    assert parsed is not None
    kinds = [(c.kind, c.symbol) for c in parsed.chunks]
    assert kinds == [
        ("module", None),
        ("class", "ConfigLoader"),
        ("async_function", "fetch_remote_config"),
        ("function", "decorated_helper"),
    ]
    lines = split_lines(SAMPLE_PY)
    covered: set[int] = set()
    for chunk in parsed.chunks:
        assert chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
        span = set(range(chunk.start_line, chunk.end_line + 1))
        assert not covered & span, "chunks must not overlap"
        covered |= span
    for number, line in enumerate(lines, start=1):
        if line.strip():
            assert number in covered, f"non-blank line {number} not in any chunk"


def test_oversized_class_is_split_into_method_chunks() -> None:
    methods = "\n".join(
        f"    def method_{i}(self):\n" + "".join(f"        x = {j}\n" for j in range(10))
        for i in range(20)
    )
    source = f"class Big:\n    attr = 1\n\n{methods}"
    parsed = parse_python(source, ChunkConfig(max_lines=50))
    assert parsed is not None
    method_chunks = [c for c in parsed.chunks if c.kind == "method"]
    assert len(method_chunks) == 20
    assert method_chunks[0].symbol == "Big.method_0"
    class_chunks = [c for c in parsed.chunks if c.kind == "class"]
    assert class_chunks and class_chunks[0].content.startswith("class Big:")
    assert all(c.end_line - c.start_line + 1 <= 50 for c in parsed.chunks)


def test_oversized_function_is_split_into_parts() -> None:
    body = "".join(f"    value_{i} = {i}\n" for i in range(300))
    parsed = parse_python(f"def huge():\n{body}", ChunkConfig(max_lines=100))
    assert parsed is not None
    assert [c.kind for c in parsed.chunks] == ["function-part"] * 4  # 100+100+100+1 lines
    assert all(c.symbol == "huge" for c in parsed.chunks)
    assert parsed.chunks[0].start_line == 1
    assert parsed.chunks[-1].end_line == 301


def test_syntax_error_returns_none() -> None:
    assert parse_python("def broken(:\n    pass\n", CONFIG) is None
    assert parse_python("print 'python 2'\n", CONFIG) is None


def test_deeply_nested_source_does_not_crash() -> None:
    source = "x = " + "(" * 5000 + "1" + ")" * 5000 + "\n"
    assert parse_python(source, CONFIG) is None


def test_empty_and_comment_only_files() -> None:
    empty = parse_python("", CONFIG)
    assert empty is not None
    assert empty.chunks == [] and empty.symbols == []
    comments = parse_python("# just a comment\n", CONFIG)
    assert comments is not None
    assert [c.kind for c in comments.chunks] == ["module"]


def test_defs_inside_conditional_blocks_are_found() -> None:
    source = (
        "import sys\n"
        "if sys.platform == 'win32':\n"
        "    def f():\n"
        "        pass\n"
        "else:\n"
        "    def f():\n"
        "        return 1\n"
    )
    parsed = parse_python(source, CONFIG)
    assert parsed is not None
    assert [s.start_line for s in parsed.symbols if s.name == "f"] == [3, 6]


def test_l1_oversized_single_line_is_split_with_line_provenance() -> None:
    from hebra.indexing.chunking import text_chunks

    config = ChunkConfig(max_chars=1000)
    long_line = "".join(f"token{i}," for i in range(2000))  # ~15k characters
    chunks = text_chunks(f"header\n{long_line}\nfooter\n", config)
    pieces = [c for c in chunks if c.kind == "text-line-part"]
    assert all(len(c.content) <= 1000 for c in chunks)
    assert all((c.start_line, c.end_line) == (2, 2) for c in pieces)
    assert "".join(c.content for c in pieces) == long_line
    assert [c.start_line for c in chunks if c.kind == "text"] == [1, 3]


def test_l1_oversized_line_inside_function_keeps_symbol() -> None:
    source = "def blob():\n    data = '" + "x" * 20_000 + "'\n    return data\n"
    parsed = parse_python(source, ChunkConfig(max_chars=4000))
    assert parsed is not None
    assert all(len(c.content) <= 4000 for c in parsed.chunks)
    assert all(c.symbol == "blob" for c in parsed.chunks)
    assert {c.kind for c in parsed.chunks} == {"function-part", "function-line-part"}
    assert all(c.start_line == c.end_line == 2 for c in parsed.chunks if "line-part" in c.kind)
