"""Codec round-trip and registry behavior."""

import pytest

from hashtext.codec import CODECS, ZstdCodec, get_codec


@pytest.fixture
def codec() -> ZstdCodec:
    return ZstdCodec()


def test_name_is_zstd(codec):
    assert codec.name == "zstd"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "short",
        "the quick brown fox " * 500,
        "héllo 🚀 中文\n\tunicode",
        "line1\nline2\n" * 100,
    ],
)
def test_roundtrip(codec, text):
    assert codec.decompress(codec.compress(text.encode("utf-8"))) == text.encode("utf-8")


def test_compresses_repetitive_text(codec):
    data = ("context block " * 1000).encode("utf-8")
    assert len(codec.compress(data)) < len(data) // 2


def test_compress_is_deterministic(codec):
    data = b"same bytes in, same bytes out"
    assert codec.compress(data) == codec.compress(data)


def test_registry_lookup():
    assert isinstance(get_codec("zstd"), ZstdCodec)
    assert set(CODECS) == {"zstd"}


def test_registry_unknown_codec():
    with pytest.raises(KeyError):
        get_codec("nope")
