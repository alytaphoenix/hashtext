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
import re
import tempfile
import time
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
    tokens_est: int = 0  # ~4 chars/token, stored at put time

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
    """Write via temp file + fsync + rename so readers never see a torn file."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Best-effort directory fsync so the rename itself is durable.
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")


def _clean_label(value: str, limit: int) -> str:
    """Sanitize agent-supplied labels before they reach a prompt or an index.

    Newlines/control characters would forge index lines, and the column and
    section delimiters are structural, so they never survive a label.
    """
    text = _CONTROL_RE.sub(" ", str(value or ""))
    text = _SPACE_RE.sub(" ", text).strip()
    return text.replace("|", "/").replace("<", "(").replace(">", ")")[:limit]


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
        tokens_est = self.token_counter.count(text)
        clean_name = _clean_label(name, 80) if name else ""
        clean_desc = _clean_label(description, 240) if description else ""
        if existing is not None:
            meta = BlockMeta(
                id=existing.id,
                sha256=full,
                name=clean_name or existing.name,
                description=clean_desc or existing.description,
                kind=kind or existing.kind,
                bytes=len(data),
                stored=len(blob),
                codec=self.default_codec.name,
                created=existing.created,
                tokens_est=tokens_est,
            )
        else:
            meta = BlockMeta(
                id=self._short_id(full),
                sha256=full,
                name=clean_name,
                description=clean_desc,
                kind=kind or "note",
                bytes=len(data),
                stored=len(blob),
                codec=self.default_codec.name,
                created=now,
                tokens_est=tokens_est,
            )
        _atomic_write(
            meta_path,
            json.dumps(meta.to_json(), indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        return PutResult(meta=meta, deduped=deduped)

    def remove(self, ref: str) -> BlockMeta:
        """Delete the block ``ref`` (metadata first, then payload).

        Metadata is removed first so a crash between the unlinks cannot leave a
        listed block whose payload is gone; a stray blob is recoverable by
        :meth:`gc` instead.
        """
        meta = self.resolve(ref)
        shard = self.blocks_dir / meta.sha256[:2]
        (shard / f"{meta.sha256}.json").unlink(missing_ok=True)
        (shard / f"{meta.sha256}.zst").unlink(missing_ok=True)
        try:
            shard.rmdir()
        except OSError:
            pass
        return meta

    def gc(self, *, dry_run: bool = False) -> dict:
        """Remove orphan payloads, metadata without a payload, and stale temps.

        Returns counts (and the affected paths when ``dry_run``).
        """
        removed = {"orphan_blobs": [], "dangling_meta": [], "temp_files": []}
        if not self.blocks_dir.is_dir():
            return {"dry_run": dry_run, **{k: 0 for k in removed}}
        cutoff = time.time() - 3600
        for shard in sorted(self.blocks_dir.iterdir()):
            if not shard.is_dir():
                continue
            shas = {p.stem for p in shard.glob("*.zst")}
            metas = {p.stem for p in shard.glob("*.json")}
            for sha in sorted(shas - metas):
                removed["orphan_blobs"].append(str(shard / f"{sha}.zst"))
            for sha in sorted(metas - shas):
                removed["dangling_meta"].append(str(shard / f"{sha}.json"))
            for tmp in shard.glob(".tmp-*"):
                try:
                    if tmp.stat().st_mtime < cutoff:
                        removed["temp_files"].append(str(tmp))
                except OSError:
                    pass
            if not dry_run:
                for path in (
                    removed["orphan_blobs"] + removed["dangling_meta"] + removed["temp_files"]
                ):
                    if Path(path).parent == shard:
                        Path(path).unlink(missing_ok=True)
                try:
                    shard.rmdir()
                except OSError:
                    pass
        if dry_run:
            return {"dry_run": True, **{k: len(v) for k, v in removed.items()}, "paths": removed}
        return {"dry_run": False, **{k: len(v) for k, v in removed.items()}}

    def lines(self, ref: str, start: int | None = None, end: int | None = None) -> str:
        """Return lines ``start``..``end`` (1-based, inclusive) of a block."""
        text = self.get(ref)
        if start is None and end is None:
            return text
        rows = text.splitlines(keepends=True)
        lo = max(1, start or 1)
        hi = min(len(rows), end or len(rows))
        if lo > hi:
            return ""
        return "".join(rows[lo - 1 : hi])

    def grep(
        self,
        pattern: str,
        *,
        refs: list[str] | None = None,
        ignore_case: bool = True,
        max_matches: int = 50,
    ) -> list[dict]:
        """Regex search across blocks (or the given refs), line-addressed."""
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise HashtextError(f"invalid pattern: {exc}") from exc
        metas = [self.resolve(ref) for ref in refs] if refs else self.list()
        out: list[dict] = []
        for meta in metas:
            for number, line in enumerate(self.load(meta).splitlines(), start=1):
                if rx.search(line):
                    out.append(
                        {
                            "ref": meta.id,
                            "sha256": meta.sha256,
                            "name": meta.name,
                            "line": number,
                            "text": line,
                        }
                    )
                    if len(out) >= max_matches:
                        return out
        return out

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
        """Aggregate size/token stats from metadata only (no decompression)."""
        blocks = total_bytes = total_stored = corrupt = tokens = 0
        for meta in self.list():
            blocks += 1
            total_bytes += meta.bytes
            total_stored += meta.stored
            tokens += meta.tokens_est
            if not (self.blocks_dir / meta.sha256[:2] / f"{meta.sha256}.zst").exists():
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
            size = int(raw["bytes"])
            return BlockMeta(
                id=raw["id"],
                sha256=raw["sha256"],
                name=raw.get("name", ""),
                description=raw.get("description", ""),
                kind=raw.get("kind", "note"),
                bytes=size,
                stored=int(raw["stored"]),
                codec=raw["codec"],
                created=raw["created"],
                # Older metadata predates the stored estimate; derive it once.
                tokens_est=int(raw.get("tokens_est") or max(1, (size + 3) // 4)) if size else 0,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as exc:
            raise HashtextError(f"corrupt metadata {meta_path}: {exc}") from exc
