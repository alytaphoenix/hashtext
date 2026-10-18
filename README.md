# hashtext

Content-addressed context cache. Store context blocks under the **sha256
fingerprint of their plaintext**, compressed with a pluggable codec (zstd out
of the box), then reconstruct them on demand.

The idea: an agent's context window splits into two tiers.

```
┌──────────────────────────────────────────────────────────┐
│ HOT  general context        system prompt, recent turns, │
│                             whatever is currently needed │
├──────────────────────────────────────────────────────────┤
│ COLD fingerprinted blocks   ref | name | desc | 4KB→1.1KB │
│                             stored compressed, expanded   │
│                             into context only when used   │
└──────────────────────────────────────────────────────────┘
```

The model sees a cheap **index** of available blocks and declares an expansion
for the ones the current task needs — no silent retrieval, no lossy summarizer
in the loop. Because identity is the content hash, dedup is automatic and
invalidation is free: changed content simply gets a new fingerprint.

The agent-side hot-swap loop lives in the pi plugin
([`forseti/hashtext-plugin`](https://github.com/alytaphoenix/forseti)):
cold markers stay in the transcript, and the plugin substitutes decompressed
content per turn.

## Install

```sh
uv tool install .        # or: pipx install .
```

Store location: `$HASHTEXT_DIR`, default `~/.hashtext`.

## CLI

Every command has `--json` and returns objects, so it is safe to shell out to:

```sh
# store a file or stdin
hashtext put build.log --name "build log" --desc "failing CI output"
cat notes.md | hashtext put - --name notes

# the map of cold blocks (this is what goes in front of the model)
hashtext list
# a3f2c1b9d4e0 | build log | failing CI output | 41.2KB→7.9KB

# reconstruct
hashtext show a3f2c1              # raw text, exact
hashtext show a3f2c1 --lines 40:80
hashtext show a3f2c1 --json       # {"blocks": [{..., "text": "..."}]}

# search cold blocks without expanding them whole
hashtext grep "ERROR|timeout" --max 20

# remove blocks / clean up after a crash
hashtext rm a3f2c1
hashtext gc --dry-run
hashtext stats
```

Refs are sha256 prefixes — any unambiguous prefix of ≥ 4 chars works. If two
blocks ever share a 12-char prefix, refs escalate automatically and ambiguous
prefixes fail loudly.

### JSON contract

```jsonc
put   -> {"block": {"id", "sha256", "name", "description", "kind",
                    "bytes", "stored", "codec", "created", "tokens_est"},
          "deduped": bool}
list  -> {"blocks": [<meta>, ...]}                 // newest first
show  -> {"blocks": [<meta + "text">, ...]}
grep  -> {"matches": [{"ref", "sha256", "name", "line", "text"}, ...], "count": n}
rm    -> {"removed": [<meta>, ...]}
gc    -> {"gc": {"dry_run", "orphan_blobs", "dangling_meta", "temp_files"}}
stats -> {"stats": {..., "ratio": 0.19}}
error -> {"error": "..."}                          // exit code 1
```

## Store layout

```
~/.hashtext/
├── blocks/ab/ab3f….zst   compressed payload
├── blocks/ab/ab3f….json  metadata (human-readable)
└── resident/<slug>.json  residency state (written by consumers)
```

Writes are atomic (temp file + fsync + rename), metadata is removed before the
payload on delete (a crash cannot leave a listed block without its body), and
`hashtext gc` clears orphan payloads, dangling metadata, and stale temp files.
Reads verify the decompressed bytes against the fingerprint, so silent
corruption is impossible: it fails as `IntegrityError`.

Labels (`name`, `description`) are sanitized at write time — control
characters, newlines, and the `|`/`<`/`>` delimiters are folded — because they
are rendered into agent prompts. Token estimates are stored in metadata at put
time, so `stats` is metadata-only (no full-store decompression).

## Library

```python
from hashtext import BlockStore

store = BlockStore()                       # or BlockStore("/tmp/scratch")
result = store.put(text, name="q3", description="Q3 revenue details")
store.get(result.meta.id)                  # -> original text
store.get("a3f2")                          # prefix works too
store.list()                               # [BlockMeta, ...]
```

- `BlockCodec` is the extension point for compression strategies — v1 ships
  lossless `ZstdCodec`; the codec id is stored per block so lossy transforms
  can be added later without migrating the store.
- `TokenCounter` is pluggable; the default is a dependency-free ~4 chars/token
  heuristic used for stats and budget guards.
- Only runtime dependency: `zstandard`.

## Development

```sh
uv sync
uv run pytest
```

## License

MIT
