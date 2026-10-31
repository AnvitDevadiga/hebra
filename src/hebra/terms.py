"""Term extraction shared by indexing and query construction.

SQLite's ``unicode61`` tokenizer already splits on ``_``, ``.``, ``/`` and
other punctuation, but keeps ``camelCase`` identifiers whole. The helpers
here add the camel-case sub-words so that a task mentioning "config loader"
can match ``ConfigLoader``.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[^\W_]+")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")

MAX_QUERY_TERMS = 64

# Common English function words. Kept deliberately small: BM25's IDF already
# discounts frequent terms, and over-aggressive filtering hides real intent.
_STOPWORD_TEXT = """
    a an and are as at be been but by can could do does for from had has have how i if in
    into is it its it's me my no not of on or our should so than that the their them then
    there these they this those to too us was we were what when where which while who why
    will with would you your
"""
STOPWORDS: frozenset[str] = frozenset(_STOPWORD_TEXT.split())


def camel_parts(word: str) -> list[str]:
    """Split ``"parseHTTPResponse2"`` into ``["parse", "http", "response", "2"]``.

    Non-ASCII words are returned whole; case-splitting rules are ASCII-only.
    """
    if not word.isascii():
        return [word.lower()]
    return [p.lower() for p in _CAMEL_PART.findall(word)]


def identifier_subwords(text: str) -> str:
    """Camel-case sub-words of identifiers in ``text`` that ``unicode61`` would miss."""
    extra: set[str] = set()
    for ident in set(_IDENTIFIER.findall(text)):
        for piece in ident.split("_"):
            parts = camel_parts(piece)
            if len(parts) > 1:
                extra.update(parts)
    return " ".join(sorted(extra))


def query_terms(text: str) -> list[str]:
    """Lower-case search terms for ``text`` in first-seen order, without duplicates.

    Not truncated; callers apply ``MAX_QUERY_TERMS`` and report truncation.
    """
    seen: dict[str, None] = {}
    for word in _WORD.findall(text):
        candidates = [word.lower()]
        parts = camel_parts(word)
        if len(parts) > 1:
            candidates.extend(parts)
        for term in candidates:
            if len(term) < 2 or term in STOPWORDS:
                continue
            seen.setdefault(term, None)
    return list(seen)


def word_set(text: str) -> set[str]:
    """Lower-case word tokens of ``text`` plus camel-case sub-words."""
    words: set[str] = set()
    for word in _WORD.findall(text):
        words.add(word.lower())
        words.update(camel_parts(word))
    return words
