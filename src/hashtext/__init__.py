"""hashtext — content-addressed context cache.

Stores context blocks keyed by the sha256 fingerprint of their plaintext,
compressed with a pluggable codec (zstd out of the box). Consumers (an agent
extension, a CLI pipe, another tool) keep a small hot context and swap cold
blocks in per turn via :class:`BlockStore`.
"""

from .codec import BlockCodec, ZstdCodec, get_codec
from .store import (
    AmbiguousRefError,
    BlockMeta,
    BlockStore,
    HashtextError,
    IntegrityError,
    NotFoundError,
    PutResult,
    StoreStats,
    default_store_dir,
)
from .tokens import DEFAULT_TOKEN_COUNTER, HeuristicTokenCounter, TokenCounter

__version__ = "0.1.0"

__all__ = [
    "AmbiguousRefError",
    "BlockCodec",
    "BlockMeta",
    "BlockStore",
    "DEFAULT_TOKEN_COUNTER",
    "HeuristicTokenCounter",
    "HashtextError",
    "IntegrityError",
    "NotFoundError",
    "PutResult",
    "StoreStats",
    "TokenCounter",
    "ZstdCodec",
    "__version__",
    "default_store_dir",
    "get_codec",
]
