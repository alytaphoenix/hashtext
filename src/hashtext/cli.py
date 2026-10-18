"""``hashtext`` CLI — JSON-first, built to be shelled out to by agent plugins.

Every command has ``--json``; all JSON payloads are objects so the contract can
grow without breaking consumers:

    put    -> {"block": <meta>, "deduped": bool}
    list   -> {"blocks": [<meta>, ...]}
    show   -> {"blocks": [<meta + "text">, ...]}
    rm     -> {"removed": [<meta>, ...]}
    stats  -> {"stats": {..., "ratio": float}}
    error  -> {"error": "<message>"}   (exit code 1)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .index import human_bytes, render_index
from .store import BlockMeta, BlockStore, HashtextError


def _emit(payload: dict, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    elif text:
        print(text)


def _put_summary(meta: BlockMeta, deduped: bool) -> str:
    verb = "deduped to existing" if deduped else "stored"
    return (
        f"{verb} {meta.id} ({meta.name or '(unnamed)'}) "
        f"{human_bytes(meta.bytes)}→{human_bytes(meta.stored)}"
    )


def cmd_put(store: BlockStore, args: argparse.Namespace) -> int:
    if not args.path or args.path == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.path).read_text(encoding="utf-8")
    result = store.put(text, name=args.name, description=args.desc, kind=args.kind)
    _emit(
        {"block": result.meta.to_json(), "deduped": result.deduped},
        args.json,
        _put_summary(result.meta, result.deduped),
    )
    return 0


def cmd_list(store: BlockStore, args: argparse.Namespace) -> int:
    metas = store.list()
    _emit(
        {"blocks": [meta.to_json() for meta in metas]},
        args.json,
        render_index(metas),
    )
    return 0


def cmd_show(store: BlockStore, args: argparse.Namespace) -> int:
    start = end = None
    if args.lines:
        try:
            lo, _, hi = args.lines.partition(":")
            start = int(lo) if lo else None
            end = int(hi) if hi else start
        except ValueError:
            raise HashtextError(f"invalid --lines {args.lines!r} (use START or START:END)")
    blocks = []
    for ref in args.refs:
        meta = store.resolve(ref)
        text = store.lines(ref, start, end) if args.lines else store.load(meta)
        blocks.append({**meta.to_json(), "text": text})
    if args.json:
        _emit({"blocks": blocks}, True, "")
        return 0
    if len(blocks) == 1:
        # exact content, no framing — pipe-friendly
        sys.stdout.write(blocks[0]["text"])
    else:
        for i, block in enumerate(blocks):
            if i:
                print()
            print(f"=== {block['id']} {block['name'] or '(unnamed)'} ===")
            sys.stdout.write(block["text"])
            if not block["text"].endswith("\n"):
                print()
    return 0


def cmd_grep(store: BlockStore, args: argparse.Namespace) -> int:
    matches = store.grep(
        args.pattern,
        refs=args.refs or None,
        ignore_case=not args.case_sensitive,
        max_matches=args.max,
    )
    text = "\n".join(
        f"{m['ref']}:{m['line']}: {m['text']}" for m in matches
    ) or "no matches"
    _emit({"matches": matches, "count": len(matches)}, args.json, text)
    return 0


def cmd_gc(store: BlockStore, args: argparse.Namespace) -> int:
    result = store.gc(dry_run=args.dry_run)
    text = (
        f"{'would remove' if result['dry_run'] else 'removed'}: "
        f"{result['orphan_blobs']} orphan blobs, "
        f"{result['dangling_meta']} dangling metadata, "
        f"{result['temp_files']} stale temp files"
    )
    _emit({"gc": result}, args.json, text)
    return 0


def cmd_rm(store: BlockStore, args: argparse.Namespace) -> int:
    removed = [store.remove(ref).to_json() for ref in args.refs]
    text = "\n".join(
        f"removed {meta['id']} ({meta['name'] or '(unnamed)'})" for meta in removed
    )
    _emit({"removed": removed}, args.json, text)
    return 0


def cmd_stats(store: BlockStore, args: argparse.Namespace) -> int:
    stats = store.stats()
    text = "\n".join(
        [
            f"store:    {stats.root}",
            f"blocks:   {stats.blocks}",
            f"original: {human_bytes(stats.bytes)} (~{stats.tokens_est} tokens)",
            f"stored:   {human_bytes(stats.stored)} ({stats.ratio * 100:.0f}% of original)",
            f"corrupt:  {stats.corrupt}",
        ]
    )
    _emit({"stats": stats.to_json()}, args.json, text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hashtext",
        description=(
            "Content-addressed context cache: store text as fingerprinted, "
            "zstd-compressed blocks and expand them on demand."
        ),
    )
    parser.add_argument(
        "--dir",
        help="store directory (default: $HASHTEXT_DIR or ~/.hashtext)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    put = sub.add_parser("put", help="store text as a block (file or stdin)")
    put.add_argument("path", nargs="?", help="file to store ('-' or omitted: stdin)")
    put.add_argument("--name", default="", help="short label shown in the index")
    put.add_argument("--desc", default="", help="one-line description for the index")
    put.add_argument("--kind", default=None, help="block kind (default: note)")
    put.add_argument("--json", action="store_true", help="emit JSON")
    put.set_defaults(func=cmd_put)

    lst = sub.add_parser("list", help="list blocks as index lines")
    lst.add_argument("--json", action="store_true", help="emit JSON")
    lst.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="decompress and print one or more blocks")
    show.add_argument("refs", nargs="+", help="block refs (full sha256 or prefix)")
    show.add_argument(
        "--lines",
        default=None,
        help="line range: START or START:END (1-based, inclusive)",
    )
    show.add_argument("--json", action="store_true", help="emit JSON")
    show.set_defaults(func=cmd_show)

    grep = sub.add_parser("grep", help="regex search across blocks (line-addressed)")
    grep.add_argument("pattern", help="regular expression")
    grep.add_argument("--ref", dest="refs", action="append", help="limit to a block (repeatable)")
    grep.add_argument("--case-sensitive", action="store_true", help="case-sensitive match")
    grep.add_argument("--max", type=int, default=50, help="max matches (default 50)")
    grep.add_argument("--json", action="store_true", help="emit JSON")
    grep.set_defaults(func=cmd_grep)

    gc = sub.add_parser("gc", help="remove orphan payloads, dangling metadata, stale temps")
    gc.add_argument("--dry-run", action="store_true", help="report without deleting")
    gc.add_argument("--json", action="store_true", help="emit JSON")
    gc.set_defaults(func=cmd_gc)

    rm = sub.add_parser("rm", help="delete one or more blocks")
    rm.add_argument("refs", nargs="+", help="block refs (full sha256 or prefix)")
    rm.add_argument("--json", action="store_true", help="emit JSON")
    rm.set_defaults(func=cmd_rm)

    stats = sub.add_parser("stats", help="store size and compression summary")
    stats.add_argument("--json", action="store_true", help="emit JSON")
    stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = BlockStore(args.dir)
    try:
        return args.func(store, args)
    except (HashtextError, OSError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"hashtext: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
