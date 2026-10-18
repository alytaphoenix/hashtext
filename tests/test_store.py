"""BlockStore behavior: content addressing, refs, integrity, metadata."""

import hashlib
import json

import pytest

from hashtext import (
    AmbiguousRefError,
    BlockStore,
    HashtextError,
    IntegrityError,
    NotFoundError,
    default_store_dir,
)

TEXT = "the quick brown fox jumps over the lazy dog. " * 50


@pytest.fixture
def store(tmp_path) -> BlockStore:
    return BlockStore(tmp_path / "store")


def _meta_path(store: BlockStore, sha: str):
    return store.blocks_dir / sha[:2] / f"{sha}.json"


def _blob_path(store: BlockStore, sha: str):
    return store.blocks_dir / sha[:2] / f"{sha}.zst"


def _forge_meta(store: BlockStore, sha: str, **overrides):
    """Write a metadata file directly (for collision/torn-state tests)."""
    meta = {
        "id": sha[:12],
        "sha256": sha,
        "name": "",
        "description": "",
        "kind": "note",
        "bytes": 1,
        "stored": 1,
        "codec": "zstd",
        "created": "2026-01-01T00:00:00+00:00",
    }
    meta.update(overrides)
    path = _meta_path(store, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta), encoding="utf-8")


# -- basic round trip ------------------------------------------------------


def test_put_get_roundtrip(store):
    result = store.put(TEXT, name="fox", description="pangram", kind="ref")
    assert result.deduped is False
    assert result.meta.name == "fox"
    assert result.meta.description == "pangram"
    assert result.meta.kind == "ref"
    assert result.meta.codec == "zstd"
    assert result.meta.bytes == len(TEXT.encode("utf-8"))
    assert result.meta.stored < result.meta.bytes
    assert len(result.meta.id) == 12
    assert result.meta.id == hashlib.sha256(TEXT.encode()).hexdigest()[:12]
    assert store.get(result.meta.id) == TEXT


def test_unicode_roundtrip(store):
    text = "héllo 🚀 中文\nline two\n"
    result = store.put(text)
    assert store.get(result.meta.sha256) == text


def test_empty_string_roundtrip(store):
    result = store.put("")
    assert store.get(result.meta.id) == ""


def test_kind_defaults_to_note(store):
    assert store.put(TEXT).meta.kind == "note"


# -- content addressing ----------------------------------------------------


def test_dedup_same_content(store):
    first = store.put(TEXT, name="first")
    second = store.put(TEXT)
    assert second.deduped is True
    assert second.meta.id == first.meta.id
    assert second.meta.name == "first"  # empty re-put keeps labels
    blobs = list(store.blocks_dir.glob("*/*.zst"))
    assert len(blobs) == 1


def test_dedup_updates_labels_when_given(store):
    first = store.put(TEXT, name="old", description="old desc")
    updated = store.put(TEXT, name="new", description="new desc", kind="ref")
    assert updated.deduped is True
    assert updated.meta.name == "new"
    assert updated.meta.description == "new desc"
    assert updated.meta.kind == "ref"
    assert updated.meta.created == first.meta.created  # creation time preserved


def test_different_content_different_id(store):
    a = store.put("alpha")
    b = store.put("beta")
    assert a.meta.id != b.meta.id


def test_short_id_escalates_on_prefix_collision(store):
    full = hashlib.sha256(TEXT.encode()).hexdigest()
    # Forge a *different* block that shares the 12-char prefix.
    tail_char = "0" if full[12] != "0" else "1"
    foreign = full[:12] + tail_char + "0" * 51
    assert len(foreign) == 64
    _forge_meta(store, foreign)

    result = store.put(TEXT)
    assert len(result.meta.id) == 16  # escalated past the collision
    assert store.get(result.meta.id) == TEXT
    # the bare 12-char prefix is now genuinely ambiguous
    with pytest.raises(AmbiguousRefError):
        store.resolve(full[:12])


def test_put_rejects_non_str(store):
    with pytest.raises(TypeError):
        store.put(b"bytes")


# -- refs ------------------------------------------------------------------


def test_get_by_prefix(store):
    result = store.put(TEXT)
    assert store.get(result.meta.id[:8]) == TEXT


def test_get_by_full_sha(store):
    result = store.put(TEXT)
    assert store.get(result.meta.sha256) == TEXT


def test_ref_accepts_hash_and_case_and_hash_prefix(store):
    result = store.put(TEXT)
    assert store.get("#" + result.meta.id.upper()) == TEXT


def test_unknown_ref(store):
    with pytest.raises(NotFoundError):
        store.resolve("deadbeef0000")
    with pytest.raises(NotFoundError):
        store.resolve("a" * 64)


@pytest.mark.parametrize("bad", ["", "xyz", "abc", "a bb", "a" * 3])
def test_invalid_ref(store, bad):
    with pytest.raises(HashtextError):
        store.resolve(bad)


def test_ambiguous_prefix(store):
    sha_a = "aaaa" + "1" * 60
    sha_b = "aaaa" + "2" * 60
    _forge_meta(store, sha_a)
    _forge_meta(store, sha_b)
    with pytest.raises(AmbiguousRefError):
        store.resolve("aaaa")
    assert store.resolve(sha_a[:12]).sha256 == sha_a


# -- integrity -------------------------------------------------------------


def test_tampered_payload(store):
    result = store.put(TEXT)
    _blob_path(store, result.meta.sha256).write_bytes(b"not zstd at all")
    with pytest.raises(IntegrityError):
        store.get(result.meta.id)


def test_missing_payload(store):
    result = store.put(TEXT)
    _blob_path(store, result.meta.sha256).unlink()
    with pytest.raises(IntegrityError):
        store.get(result.meta.id)


def test_fingerprint_mismatch(store):
    result = store.put(TEXT)
    other = BlockStore(store.root).default_codec.compress(b"different bytes")
    _blob_path(store, result.meta.sha256).write_bytes(other)
    with pytest.raises(IntegrityError):
        store.get(result.meta.id)


# -- list / remove / stats -------------------------------------------------


def test_list_empty(store):
    assert store.list() == []


def test_list_returns_all(store):
    a = store.put("alpha", name="a")
    b = store.put("beta", name="b")
    assert {m.id for m in store.list()} == {a.meta.id, b.meta.id}
    assert [m.name for m in store.list()][0] in {"a", "b"}  # newest-first, deterministic


def test_remove(store):
    result = store.put(TEXT)
    removed = store.remove(result.meta.id)
    assert removed.id == result.meta.id
    assert store.list() == []
    assert not _blob_path(store, result.meta.sha256).exists()
    with pytest.raises(NotFoundError):
        store.get(result.meta.id)
    with pytest.raises(NotFoundError):
        store.remove(result.meta.id)


def test_stats_empty(store):
    stats = store.stats()
    assert stats.blocks == 0
    assert stats.bytes == 0
    assert stats.corrupt == 0
    assert stats.ratio == 1.0


def test_stats_aggregates(store):
    a = store.put(TEXT, name="a")
    b = store.put("beta " * 20, name="b")
    stats = store.stats()
    assert stats.blocks == 2
    assert stats.bytes == a.meta.bytes + b.meta.bytes
    assert stats.stored == a.meta.stored + b.meta.stored
    assert stats.stored < stats.bytes
    assert stats.tokens_est > 0
    assert 0 < stats.ratio < 1
    assert stats.corrupt == 0


def test_stats_counts_corrupt(store):
    result = store.put(TEXT)
    _blob_path(store, result.meta.sha256).write_bytes(b"junk")
    stats = store.stats()
    assert stats.blocks == 1
    assert stats.corrupt == 1


# -- layout / config -------------------------------------------------------


def test_no_temp_files_left_behind(store):
    store.put(TEXT)
    store.put("another block")
    assert list(store.root.rglob(".tmp-*")) == []


def test_default_store_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HASHTEXT_DIR", str(tmp_path / "custom"))
    assert default_store_dir() == tmp_path / "custom"
    monkeypatch.delenv("HASHTEXT_DIR")
    assert default_store_dir().name == ".hashtext"
