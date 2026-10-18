"""Token estimation. Pluggable so callers can swap in a real tokenizer.

The default is a dependency-free ~4 chars/token heuristic — good enough for
budget guards (residency caps, stats), which only need the right order of
magnitude.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class HeuristicTokenCounter:
    """~4 characters per token (English-ish prose)."""

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)


DEFAULT_TOKEN_COUNTER = HeuristicTokenCounter()
