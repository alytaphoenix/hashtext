"""CLI contract tests: exit codes, JSON payloads, raw text output."""

import io
import json

import pytest

from hashtext.cli import main

TEXT = "the quick brown fox " * 100


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def store_dir(tmp_path):
    return str(tmp_path / "store")


def test_put_stdin_json(store_dir, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(TEXT))
    code, out, _ = run(capsys, "--dir", store_dir, "put", "-", "--json", "--name", "fox")
    assert code == 0
    payload = json.loads(out)
    assert payload["deduped"] is False
    assert payload["block"]["name"] == "fox"
    assert payload["block"]["kind"] == "note"
    assert payload["block"]["stored"] < payload["block"]["bytes"]

    # re-put: deduped, same ref
    monkeypatch.setattr("sys.stdin", io.StringIO(TEXT))
    code, out, _ = run(capsys, "--dir", store_dir, "put", "--json")
    assert code == 0
    payload2 = json.loads(out)
    assert payload2["deduped"] is True
    assert payload2["block"]["id"] == payload["block"]["id"]


def test_put_file_text_mode(store_dir, tmp_path, capsys):
    source = tmp_path / "log.txt"
    source.write_text(TEXT, encoding="utf-8")
    code, out, err = run(
        capsys, "--dir", store_dir, "put", str(source), "--name", "build log"
    )
    assert code == 0
    assert "stored" in out
    assert "build log" in out
    assert err == ""


def test_put_missing_file(store_dir, tmp_path, capsys):
    code, out, err = run(capsys, "--dir", store_dir, "put", str(tmp_path / "nope"))
    assert code == 1
    assert "hashtext:" in err

    code, out, _ = run(
        capsys, "--dir", store_dir, "put", str(tmp_path / "nope"), "--json"
    )
    assert code == 1
    assert "error" in json.loads(out)


def test_list_text_and_json(store_dir, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(TEXT))
    run(capsys, "--dir", store_dir, "put", "--json", "--name", "fox")
    monkeypatch.setattr("sys.stdin", io.StringIO("another block entirely"))
    run(capsys, "--dir", store_dir, "put", "--json", "--name", "other")

    code, out, _ = run(capsys, "--dir", store_dir, "list", "--json")
    assert code == 0
    blocks = json.loads(out)["blocks"]
    assert {b["name"] for b in blocks} == {"fox", "other"}

    code, out, _ = run(capsys, "--dir", store_dir, "list")
    assert code == 0
    assert "fox" in out
    assert "→" in out  # size→stored column
    for block in blocks:
        assert block["id"] in out


def test_list_empty(store_dir, capsys):
    code, out, _ = run(capsys, "--dir", store_dir, "list")
    assert code == 0
    assert "no blocks" in out


def test_show_raw_single_ref_is_exact(store_dir, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(TEXT))
    code, out, _ = run(capsys, "--dir", store_dir, "put", "--json")
    ref = json.loads(out)["block"]["id"]

    code, out, err = run(capsys, "--dir", store_dir, "show", ref)
    assert code == 0
    assert out == TEXT  # byte-exact, no framing
    assert err == ""


def test_show_json_has_meta_and_text(store_dir, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(TEXT))
    code, out, _ = run(capsys, "--dir", store_dir, "put", "--json", "--name", "fox")
    ref = json.loads(out)["block"]["id"]

    code, out, _ = run(capsys, "--dir", store_dir, "show", ref[:8], "--json")
    assert code == 0
    block = json.loads(out)["blocks"][0]
    assert block["text"] == TEXT
    assert block["name"] == "fox"
    assert block["id"] == ref


def test_show_multiple_refs_framed(store_dir, capsys, monkeypatch):
    refs = []
    for text, name in (("alpha content", "alpha"), ("beta content", "beta")):
        monkeypatch.setattr("sys.stdin", io.StringIO(text))
        code, out, _ = run(capsys, "--dir", store_dir, "put", "--json", "--name", name)
        refs.append(json.loads(out)["block"]["id"])

    code, out, _ = run(capsys, "--dir", store_dir, "show", *refs)
    assert code == 0
    for ref, text, name in zip(refs, ("alpha content", "beta content"), ("alpha", "beta")):
        assert f"=== {ref} {name} ===" in out
        assert text in out


def test_show_missing_ref(store_dir, capsys):
    code, out, err = run(capsys, "--dir", store_dir, "show", "deadbeef0000")
    assert code == 1
    assert "hashtext:" in err

    code, out, _ = run(capsys, "--dir", store_dir, "show", "deadbeef0000", "--json")
    assert code == 1
    assert "no block matching" in json.loads(out)["error"]


def test_rm_json_and_missing(store_dir, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(TEXT))
    code, out, _ = run(capsys, "--dir", store_dir, "put", "--json", "--name", "fox")
    ref = json.loads(out)["block"]["id"]

    code, out, _ = run(capsys, "--dir", store_dir, "rm", ref, "--json")
    assert code == 0
    assert json.loads(out)["removed"][0]["id"] == ref

    code, out, err = run(capsys, "--dir", store_dir, "rm", ref)
    assert code == 1
    assert "hashtext:" in err


def test_stats_json(store_dir, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(TEXT))
    run(capsys, "--dir", store_dir, "put", "--json")

    code, out, _ = run(capsys, "--dir", store_dir, "stats", "--json")
    assert code == 0
    stats = json.loads(out)["stats"]
    assert stats["blocks"] == 1
    assert stats["stored"] < stats["bytes"]
    assert 0 < stats["ratio"] < 1
    assert stats["tokens_est"] > 0

    code, out, _ = run(capsys, "--dir", store_dir, "stats")
    assert code == 0
    assert "blocks:   1" in out


def test_env_dir_is_used(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HASHTEXT_DIR", str(tmp_path / "envstore"))
    monkeypatch.setattr("sys.stdin", io.StringIO("env based"))
    code, out, _ = run(capsys, "put", "--json")
    assert code == 0
    assert (tmp_path / "envstore" / "blocks").is_dir()
