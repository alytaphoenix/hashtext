"""Content-addressed block store.

Layout::

    <root>/blocks/<sha[:2]>/<sha>.zst    compressed payload
    <root>/blocks/<sha[:2]>/<sha>.json   BlockMeta (human-readable)
    <root>/resident/<slug>.json          residency state (written by consumers,
                                         e.g. the forseti pi plugin)

The sha256 of the plaintext *is* the block identity: equal content always
maps to one stored blob (dedup), changed content gets a new fingerprint
(invalidation for free). Short refs are hex prefixes of it — ``store.get``
accepts any unambiguous prefix down to 4 chars.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .codec import BlockCodec, ZstdCodec, get_codec
from .tokens import DEFAULT_TOKEN_COUNTER, TokenCounter

#: Prefix lengths tried in order when the 12-char ref collides with a
#: different block that shares the prefix.
REF_LENGTHS = (12, 16, 20, 24, 32)
MIN_REF_LENGTH = 4
SHA256_HEX_LENGTH = 64


class HashtextError(Exception):
    """Base error for the hashtext package."""


class NotFoundError(HashtextError):
    """No block matches the given ref."""


class AmbiguousRefError(HashtextError):
    """A ref prefix matches more than one block."""


class IntegrityError(HashtextError):
    """A stored payload is missing, unreadable, or fingerprint-mismatched."""


@dataclass(frozen=True)
class BlockMeta:
    """On-disk metadata for one block (the `<sha>.json` file)."""

    id: str
    sha256: str
    name: str
    description: str
    kind: str
    bytes: int  # plaintext size
    stored: int  # compressed size on disk
    codec: str
    created: str  # ISO-8601 UTC

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PutResult:
    meta: BlockMeta
    deduped: bool


@dataclass(frozen=True)
class StoreStats:
    root: str
    blocks: int
    bytes: int
    stored: int
    tokens_est: int
    corrupt: int

    @property
    def ratio(self) -> float:
        return round(self.stored / self.bytes, 4) if self.bytes else 1.0

    def to_json(self) -> dict:
        data = asdict(self)
        data["ratio"] = self.ratio
        return data


def default_store_dir() -> Path:
    """``$HASHTEXT_DIR`` if set, else ``~/.hashtext``."""
    env = os.environ.get("HASHTEXT_DIR")
    return Path(env).expanduser() if env else Path.home() / ".hashtext"


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via temp file + rename so readers never see a torn file."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class BlockStore:
    """A directory-backed store of fingerprinted context blocks."""

    def __init__(
        self,
        root: str | Path | None = None,
        codec: BlockCodec | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.root = Path(root).expanduser() if root is not None else default_store_dir()
        self.default_codec = codec or ZstdCodec()
        self.token_counter = token_counter or DEFAULT_TOKEN_COUNTER

    @property
    def blocks_dir(self) -> Path:
        return self.root / "blocks"

    # -- write ------------------------------------------------------------

    def put(
        self,
        text: str,
        *,
        name: str = "",
        description: str = "",
        kind: str | None = None,
    ) -> PutResult:
        """Store ``text`` as a block; returns its metadata and dedup flag.

        Re-storing identical content returns the existing block (dedup) and
        updates ``name``/``description``/``kind`` only when the new values
        are non-empty, so a plain re-put never clobbers labels.
        """
        if not isinstance(text, str):
            raise TypeError(f"put() expects str, got {type(text).__name__}")

        data = text.encode("utf-8")
        full = hashlib.sha256(data).hexdigest()
        blob = self.default_codec.compress(data)

        shard = self.blocks_dir / full[:2]
        shard.mkdir(parents=True, exist_ok=True)
        blob_path = shard / f"{full}.zst"
        meta_path = shard / f"{full}.json"

        deduped = blob_path.exists()
        existing = self._read_meta(meta_path) if meta_path.exists() else None

        if not deduped:
            _atomic_write(blob_path, blob)

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if existing is not None:
            meta = BlockMeta(
                id=existing.id,
                sha256=full,
                name=name or existing.name,
                description=description or existing.description,
                kind=kind or existing.kind,
                bytes=len(data),
                stored=len(blob),
                codec=self.default_codec.name,
                created=existing.created,
            )
        else:
            meta = BlockMeta(
                id=self._short_id(full),
                sha256=full,
                name=name,
                description=description,
                kind=kind or "note",
                bytes=len(data),
                stored=len(blob),
                codec=self.default_codec.name,
                created=now,
            )
        _atomic_write(
            meta_path,
            json.dumps(meta.to_json(), indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        return PutResult(meta=meta, deduped=deduped)

    def remove(self, ref: str) -> BlockMeta:
        """Delete the block ``ref`` (payload + metadata). Raises if missing."""
        meta = self.resolve(ref)
        shard = self.blocks_dir / meta.sha256[:2]
        (shard / f"{meta.sha256}.zst").unlink(missing_ok=True)
        (shard / f"{meta.sha256}.json").unlink(missing_ok=True)
        try:
            shard.rmdir()
        except OSError:
            pass
        return meta

    # -- read -------------------------------------------------------------

    def resolve(self, ref: str) -> BlockMeta:
        """Resolve a full or partial ref to its metadata.

        Accepts the full sha256 or any unambiguous hex prefix (>= 4 chars).
        """
        ref = str(ref).strip().lower().lstrip("#")
        if not ref:
            raise HashtextError("empty ref")
        if any(c not in "0123456789abcdef" for c in ref):
            raise HashtextError(f"invalid ref {ref!r}: not a hex fingerprint")
        if len(ref) < MIN_REF_LENGTH:
            raise HashtextError(f"ref {ref!r} too short (min {MIN_REF_LENGTH} chars)")
        if len(ref) == SHA256_HEX_LENGTH:
            meta_path = self.blocks_dir / ref[:2] / f"{ref}.json"
            if meta_path.exists():
                return self._read_meta(meta_path)
            raise NotFoundError(f"no block with id {ref}")

        shard = self.blocks_dir / ref[:2]
        matches = sorted(shard.glob(f"{ref}*.json")) if shard.is_dir() else []
        if not matches:
            raise NotFoundError(f"no block matching {ref!r}")
        if len(matches) > 1:
            ids = ", ".join(p.stem[:16] for p in matches[:5])
            raise AmbiguousRefError(
                f"ref {ref!r} matches {len(matches)} blocks ({ids}) "
                f"— use a longer prefix"
            )
        return self._read_meta(matches[0])

    def get(self, ref: str) -> str:
        """Resolve ``ref``, decompress, verify the fingerprint, return text."""
        return self.load(self.resolve(ref))

    def load(self, meta: BlockMeta) -> str:
        """Decompress a known block's payload with fingerprint verification."""
        blob_path = self.blocks_dir / meta.sha256[:2] / f"{meta.sha256}.zst"
        if not blob_path.exists():
            raise IntegrityError(f"block {meta.id}: payload missing")
        try:
            codec = get_codec(meta.codec)
            data = codec.decompress(blob_path.read_bytes())
        except Exception as exc:
            raise IntegrityError(f"block {meta.id}: payload unreadable ({exc})") from exc
        if hashlib.sha256(data).hexdigest() != meta.sha256:
            raise IntegrityError(f"block {meta.id}: fingerprint mismatch (corrupt payload)")
        return data.decode("utf-8")

    def list(self) -> list[BlockMeta]:
        """All blocks, newest first. Empty list if the store doesn't exist."""
        if not self.blocks_dir.is_dir():
            return []
        metas = [self._read_meta(p) for p in sorted(self.blocks_dir.glob("*/*.json"))]
        metas.sort(key=lambda m: (m.created, m.id), reverse=True)
        return metas

    def stats(self) -> StoreStats:
        """Aggregate size/token stats; unreadable blocks are counted, not fatal."""
        blocks = total_bytes = total_stored = corrupt = tokens = 0
        for meta in self.list():
            blocks += 1
            total_bytes += meta.bytes
            total_stored += meta.stored
            try:
                tokens += self.token_counter.count(self.load(meta))
            except HashtextError:
                corrupt += 1
        return StoreStats(
            root=str(self.root),
            blocks=blocks,
            bytes=total_bytes,
            stored=total_stored,
            tokens_est=tokens,
            corrupt=corrupt,
        )

    # -- internals ---------------------------------------------------------

    def _short_id(self, full: str) -> str:
        """Shortest ref length that uniquely identifies this fingerprint."""
        shard = self.blocks_dir / full[:2]
        for n in REF_LENGTHS:
            prefix = full[:n]
            if not shard.is_dir():
                return prefix
            collision = any(
                p.stem != full for p in shard.glob(f"{prefix}*.json")
            )
            if not collision:
                return prefix
        return full

    def _read_meta(self, meta_path: Path) -> BlockMeta:
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            return BlockMeta(
                id=raw["id"],
                sha256=raw["sha256"],
                name=raw.get("name", ""),
                description=raw.get("description", ""),
                kind=raw.get("kind", "note"),
                bytes=int(raw["bytes"]),
                stored=int(raw["stored"]),
                codec=raw["codec"],
                created=raw["created"],
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as exc:
            raise HashtextError(f"corrupt metadata {meta_path}: {exc}") from exc
