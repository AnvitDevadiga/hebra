"""Token counting with a named tiktoken encoding.

Counts are exact for the selected encoding. Models that use a different
tokenizer (for example Anthropic's Claude models) will count differently;
for those, treat the result as an estimate and leave headroom in the budget.

tiktoken downloads each encoding's BPE table once (from
openaipublic.blob.core.windows.net, verified against a pinned SHA-256) and
caches it; set ``TIKTOKEN_CACHE_DIR`` to control the cache location. No
repository content is ever sent anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from hebra.errors import TokenizerError

DEFAULT_ENCODING = "o200k_base"
SUPPORTED_ENCODINGS: dict[str, str] = {
    "o200k_base": "exact for OpenAI GPT-4o / o-series family tokenizers",
    "cl100k_base": "exact for OpenAI GPT-4 / GPT-3.5-turbo family tokenizers",
}


@dataclass(frozen=True)
class TokenCounter:
    encoding_name: str

    def count(self, text: str) -> int:
        # encode_ordinary treats special-token text such as "<|endoftext|>"
        # as plain text instead of raising, which is what repository data needs.
        return len(_load(self.encoding_name).encode_ordinary(text))


@cache
def _load(name: str):  # type: ignore[no-untyped-def]
    import tiktoken

    return tiktoken.get_encoding(name)


def get_token_counter(encoding_name: str = DEFAULT_ENCODING) -> TokenCounter:
    """Return a counter for ``encoding_name``, failing clearly if it cannot load."""
    if encoding_name not in SUPPORTED_ENCODINGS:
        supported = ", ".join(sorted(SUPPORTED_ENCODINGS))
        raise TokenizerError(f"unsupported encoding {encoding_name!r}; choose one of: {supported}")
    try:
        _load(encoding_name)
    except Exception as exc:  # network, cache, or corrupted-download failures
        raise TokenizerError(
            f"could not load tiktoken encoding {encoding_name!r}: {exc}. The encoding file is "
            "downloaded on first use; connect to the network once or point TIKTOKEN_CACHE_DIR "
            "at a directory that already contains it."
        ) from exc
    return TokenCounter(encoding_name)
