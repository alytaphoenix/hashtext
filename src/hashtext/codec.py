"""Codec layer: pluggable compression for block payloads.

V1 ships :class:`ZstdCodec` (lossless). The :class:`BlockCodec` protocol is
the extension point for future strategies (token pruning, summarization —
typically a lossy *text transform* applied before compression); the codec id
is recorded in each block's metadata so readers always pick the right one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import zstandard


@runtime_checkable
class BlockCodec(Protocol):
    """Compresses and decompresses block payloads."""

    name: str

    def compress(self, data: bytes) -> bytes: ...

    def decompress(self, data: bytes) -> bytes: ...


class ZstdCodec:
    """Lossless zstd.

    Level 10 biases toward compress-once/read-many, which matches store
    semantics: blocks are written once and decompressed on every expansion.
    Decompression speed is effectively level-independent.
    """

    name = "zstd"

    def __init__(self, level: int = 10) -> None:
        self.level = level

    def compress(self, data: bytes) -> bytes:
        return zstandard.ZstdCompressor(
            level=self.level, write_content_size=True
        ).compress(data)

    def decompress(self, data: bytes) -> bytes:
        return zstandard.ZstdDecompressor().decompress(data)


CODECS: dict[str, BlockCodec] = {"zstd": ZstdCodec()}


def get_codec(name: str) -> BlockCodec:
    """Look up a codec by the id stored in block metadata."""
    try:
        return CODECS[name]
    except KeyError:
        raise KeyError(
            f"unknown codec {name!r} (known: {', '.join(sorted(CODECS))})"
        ) from None
