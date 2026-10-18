# hashtext

**Content-addressed context cache for AI agents.** Keep the conversation hot;
keep the bulk cold.

[![ci](https://github.com/alytaphoenix/hashtext/actions/workflows/ci.yml/badge.svg)](https://github.com/alytaphoenix/hashtext/actions/workflows/ci.yml)

`hashtext` stores context blocks under the **sha256 fingerprint of their
plaintext**, compressed with zstd (lossless), and reconstructs them on demand.
An agent sees a compact index, declares what it needs, and gets exactly that —
an outline, a focused excerpt, a line range, or the full text — while the
transcript keeps only a one-line marker.

The core is a dependency-light Python library + JSON CLI. The reference consumer
is the [`forseti`](https://github.com/alytaphoenix/forseti) **pi plugin**, which
reconstructs resident blocks before every model call. The same on-disk format is
implemented by [`context-hydra`](https://github.com/alytaphoenix/context-hydra),
so a pi session, the CLI and an MCP client can share one body store.

- **v0.1.0** · MIT · Python 3.10+ · 66 tests
- Only runtime dependency: `zstandard`

---

## Contents

- [The problem](#the-problem)
- [The idea: two tiers](#the-idea-two-tiers)
- [How it works](#how-it-works)
- [Quickstart](#quickstart)
- [Design principles](#design-principles)
- [Store format](#store-format)
- [CLI reference](#cli-reference)
- [Python library](#python-library)
- [The pi plugin](#the-pi-plugin)
- [Telemetry, evaluation and tuning](#telemetry-evaluation-and-tuning)
- [Shared store with context-hydra](#shared-store-with-context-hydra)
- [Operations](#operations)
- [Security and trust model](#security-and-trust-model)
- [Limits and roadmap](#limits-and-roadmap)
- [Development](#development)
- [License](#license)

---

## The problem

Long agent sessions accumulate context linearly: old reasoning, file contents,
error traces, design notes. Most of it is not needed *right now*, but it stays
in the window — inflating cost, degrading attention, and forcing compaction that
throws information away.

Existing answers are unsatisfying:

- **Summarize/compact** — lossy, irreversible, and a hidden decision.
- **Retrieval (embeddings/BM25)** — silent: the model gets whatever the retriever
  guessed, with no way to ask for more.
- **Truncate** — loses the tail.
- **Checkout-style MCP tools** — explicit and portable, but the model has to
  remember to *check in*, and the body stays in the transcript until it does.

## The idea: two tiers

```
┌───────────────────────────────────────────────────────────────┐
│ HOT   the normal conversation                                 │
│       system prompt · recent turns · resident blocks only     │
├───────────────────────────────────────────────────────────────┤
│ COLD  fingerprinted blocks, compressed on disk                │
│       ref | name | description | expansion cost               │
│       expanded on demand, at the size the task needs          │
└───────────────────────────────────────────────────────────────┘
```

Three rules make it work:

1. **Identity is content.** A block's ref is the sha256 prefix of its plaintext.
   Re-storing identical text dedups; changed text is a new ref, so there is
   nothing to invalidate.
2. **The agent declares what it needs.** No hidden retrieval. The model sees the
   index and asks for an expansion.
3. **Materialize only what is used, at the granularity needed.** An outline, a
   focused excerpt, or a line range is usually enough — and costs a fraction of
   the full body.

## How it works

```
            put (text)                      index (system prompt)
                 │                                   │
                 ▼                                   ▼
      ┌─────────────────────┐            ┌──────────────────────────┐
      │ sha256 → ref        │            │ 3b29d5cb ci-log — … · 10k│
      │ zstd → blocks/ab/…  │            │ 7b1e22aa auth-note — …   │
      │ meta → blocks/ab/…  │            │ … 129 more               │
      └─────────────────────┘            └──────────────────────────┘
                 │                                   │
                 │                     hashtext_expand(ref, mode)
                 │                                   │
                 ▼                                   ▼
      ┌─────────────────────┐            ┌──────────────────────────┐
      │ BlockStore          │◀───────────│ marker in transcript      │
      │ get / lines / grep  │  view      │ [hashtext 3b29d5cb …]    │
      └─────────────────────┘            └──────────────────────────┘
                                                 │
                              before every LLM call (pi `context` event)
                                                 ▼
                                    outline / focus / range / full
```

The transcript never carries the body: expanding leaves a compact marker, and
the consumer substitutes the block's view per request. Evicting stops the
substitution; the marker remains as a one-line stub.

## Quickstart

### Install

```sh
# CLI + library (uv or pipx)
uv tool install /path/to/hashtext        # or: pipx install /path/to/hashtext

# from a checkout
git clone https://github.com/alytaphoenix/hashtext
cd hashtext
uv sync
uv run hashtext --help
```

Store location: `$HASHTEXT_DIR`, default `~/.hashtext`.

### CLI in five commands

```sh
# store a file or stdin
hashtext put build.log --name "ci log" --desc "failing build output"
cat design.md | hashtext put - --name "design notes"

# the map of cold blocks (this is what goes in front of the model)
hashtext list
# 3b29d5cb… | ci log | failing build output | 41.2KB→7.9KB

# reconstruct: exact text, a line range, or a search
hashtext show 3b29d5cb
hashtext show 3b29d5cb --lines 40:80
hashtext grep "ERROR|timeout" --max 20

# stats and housekeeping
hashtext stats
hashtext gc --dry-run
hashtext rm 3b29d5cb
```

### pi plugin

The reference consumer lives in `forseti/hashtext-plugin`:

```sh
cd ~/repos/hashtext && uv tool install .          # the store CLI
cp ~/repos/forseti/hashtext-plugin/index.ts ~/.pi/agent/extensions/hashtext.ts
```

Then, in pi:

```
/hash put ~/build.log "ci log"
/hash find what broke the deploy
/hash expand a3f2c1b9 focus="token refresh"
/hash evict a3f2c1b9
```

The plugin sees the same `HASHTEXT_DIR`.

## Design principles

| Principle | Consequence |
|---|---|
| Content-addressed identity | dedup for free, invalidation for free, integrity verifiable |
| Lossless first | zstd v1; lossy/summarizing codecs are opt-in, never silent |
| Agent-declared expansion | no hidden retrieval, no silent degradation |
| Progressive disclosure | outline → focus → range → full; the model controls cost |
| No daemon | a library + files; the CLI and plugins open the store directly |
| Fail-open consumers | a missing CLI, corrupt block, or dead model never breaks a turn |
| One format, many front-ends | CLI, pi plugin, MCP server (context-hydra) share blocks |

## Store format

### Layout

```
$HASHTEXT_DIR/                  # default ~/.hashtext
├── blocks/
│   └── ab/                     # first two hex chars of the sha256
│       ├── ab3f…<64 hex>.zst   # zstd payload
│       └── ab3f…<64 hex>.json  # BlockMeta
├── resident/                   # per-session residency (written by consumers)
│   └── <session>.json
├── decisions.jsonl             # telemetry (pi plugin; rotated at 8 MB)
└── calibration.json            # optional confidence calibration (pi plugin)
```

### BlockMeta

```json
{
  "id": "3b29d5cbf079",
  "sha256": "3b29d5cbf079…",
  "name": "ci log",
  "description": "failing build output",
  "kind": "log",
  "bytes": 41234,
  "stored": 7921,
  "codec": "zstd",
  "created": "2026-10-18T12:00:00+00:00",
  "tokens_est": 10309
}
```

- `id` is the shortest unambiguous sha256 prefix (12 hex, escalating to
  16/20/24/32 on collision).
- `codec` is stored per block; readers pick the right decompressor.
- `tokens_est` is `ceil(chars/4)`, stored at put time so `stats` is metadata-only.
- Labels (`name`, `description`) are sanitized at write time: control characters
  and whitespace runs are folded, `|`, `<`, `>` are replaced, lengths capped.

### Identity, refs, integrity

- `sha256(plaintext)` is the identity. Storing identical text returns the
  existing block (dedup); labels update only when provided.
- A ref may be the full sha256, the short id, or any unambiguous hex prefix of
  **≥ 4 characters**. Ambiguous prefixes fail loudly (never guess).
- Reads decompress and re-hash the payload. A mismatch raises `IntegrityError`;
  silent corruption is impossible.
- Zstd frames include the **content size** so `ZstdDecompressor.decompress()`
  works without streaming. (This is part of the wire contract; the Rust
  implementation in context-hydra matches it.)

### Durability and GC

- Writes are atomic: temp file → `fsync` → rename → directory `fsync`.
- Delete removes **metadata first**, then the payload: a crash cannot leave a
  listed block whose body is gone.
- `hashtext gc` removes orphan payloads, dangling metadata, and stale temp files
  (older than one hour).
- Readers are lock-free. Concurrent writers of the same content are idempotent
  (same target paths, atomic replace).

## CLI reference

All commands accept `--json` and return JSON **objects**, so the contract can
grow without breaking consumers. Errors print `{"error": "..."}` and exit `1`.

| Command | Purpose |
|---|---|
| `hashtext put <file\|-\|omitted> [--name N] [--desc D] [--kind K]` | store text |
| `hashtext list` | index lines, newest first |
| `hashtext show <ref>… [--lines START[:END]]` | decompress; one ref prints raw text |
| `hashtext grep <pattern> [--ref R]… [--max N] [--case-sensitive]` | regex across blocks |
| `hashtext rm <ref>…` | delete blocks |
| `hashtext gc [--dry-run]` | clean orphans/temps |
| `hashtext stats` | sizes, ratio, token estimate, corrupt count |

### JSON contracts

```jsonc
put   -> {"block": <meta>, "deduped": bool}
list  -> {"blocks": [<meta>, …]}
show  -> {"blocks": [<meta + "text">, …]}
grep  -> {"matches": [{"ref","sha256","name","line","text"}, …], "count": n}
rm    -> {"removed": [<meta>, …]}
gc    -> {"gc": {"dry_run", "orphan_blobs", "dangling_meta", "temp_files"}}
stats -> {"stats": {…, "ratio": 0.19}}
error -> {"error": "…"}                              // exit code 1
```

`show` with a single ref and no `--json` writes the exact bytes (pipe-friendly).
Multiple refs are framed with `=== <id> <name> ===` headers.

## Python library

```python
from hashtext import BlockStore

store = BlockStore()                        # or BlockStore("/tmp/scratch")

result = store.put(
    text,
    name="q3",
    description="Q3 revenue details",
    kind="doc",
)
result.meta.id          # "3b29d5cbf079"
result.deduped          # False; True when the content already existed

store.get(result.meta.id)          # full text (prefix refs work)
store.resolve("3b29")              # BlockMeta
store.lines(ref, 40, 80)           # exact line range
store.grep("ERROR", max_matches=20)
store.list()                       # newest first
store.stats()
store.remove(ref)
store.gc(dry_run=True)
```

Extension points:

- `BlockCodec` protocol (`compress`/`decompress`) — ship `ZstdCodec`; lossy or
  dictionary-backed codecs can be added without migrating the store, because the
  codec id is stored per block.
- `TokenCounter` protocol — the default is a dependency-free ~4 chars/token
  heuristic, used for stats and budget guards.

## The pi plugin

Source: [`forseti/hashtext-plugin`](https://github.com/alytaphoenix/forseti/tree/main/hashtext-plugin).
Installed at `~/.pi/agent/extensions/hashtext.ts`.

### Commands and tools

| Surface | What it does |
|---|---|
| `/hash list` | compact index + residency |
| `/hash find <query>` | BM25 shortlist → optional Laya pick |
| `/hash put <file> [name]` | store a file cold |
| `/hash expand <ref> [mode] [query]` | auto · full · outline · focus · range |
| `/hash evict <ref>` | drop from the hot set (marker remains) |
| `/hash read <ref> [start] [end]` | numbered line range |
| `/hash grep <pattern>` | regex across cold blocks |
| `/hash stats` · `gc` · `archive on\|off` · `auto on\|off` · `log [n]` · `clear` | operations |
| `hashtext_put` · `list` · `find` · `expand` · `evict` · `read` · `grep` | model tools |

### The projection loop

Expanding does **not** paste the body into the transcript. The tool result is a
marker:

```
[hashtext 3b29d5cb resident]
```

Before every model call, the plugin's `context` handler walks the transcript and
replaces resident markers with the block's **view**, wrapped in a nonce-tagged
fence:

```
[hashtext 3b29d5cb "ci log" — outline (601 lines) #9f1c2a]
outline — 601 lines:
L1-3 BUILD LOG — stage report
L4-12 …
[/hashtext 3b29d5cb #9f1c2a]
```

pi clones the transcript per call, so the marker survives on disk and is
re-materialized every turn. A second marker for the same block becomes a
back-reference (`resident → see above`), so content is injected once per
request. Evicting removes it from residency; the marker stays as a one-line stub.

### Progressive disclosure

| Mode | Returns |
|---|---|
| `auto` (default) | outline for blocks > 6 KB, full otherwise |
| `outline` | headings (or paragraph starts) with line numbers |
| `focus` | matching lines ± 2 with `… lines a–b omitted …` |
| `range` | an explicit line range |
| `full` | the entire body |

This is the main reason a small context can act large: a 40 KB log enters at
~100–300 tokens, and the model drills in only where it needs to.

### Budget and residency

- Expansions are injected in residency order while they fit under **90%** of the
  context window (`ctx.getContextUsage()`); blocks that do not fit are withheld
  with a visible note.
- Residency is capped at **8** blocks; the oldest is evicted and reported.
- `HASHTEXT_TTL_TURNS` enables optional age-based demotion (default off).
- Residency persists per session under `<store>/resident/<session>.json`.

### Request-time archival

Tool results over **8 KB** are stored cold at `turn_end` (up to 4 per turn),
and the `context` projection replaces them with a one-line stub carrying the
ref. The session file keeps the original; the request does not. `hashtext_*`
results are excluded. Disable with `/hash archive off`.

### Laya assist (opt-in)

`/hash auto on` enables the local [Laya](https://huggingface.co/convaiinnovations/laya)
decision endpoint (`FORSETI_LAYA_URL`, default `127.0.0.1:8751`):

- `find`: lexical/BM25 shortlist (≤7) → Laya bounded choice with explicit
  `other`. Correct picks at 0.88–1.00 on topical probes; abstains on unrelated
  prompts; ~30–300 ms warm.
- `auto`: calibrated confidence ≥ 0.85 expands; 0.60–0.85 appends a `likely:`
  suggestion to the index.
- janitor: at `turn_end`, with ≥3 residents, Laya picks the least-needed block
  (or `keep all`); ≥ 0.80 evicts it.

The plugin gates on **`answer_confidence`** (max probability), not `confidence`
(normalized entropy — a decisiveness score). On this distribution the base
model is under-confident, so an optional isotonic calibration map
(`<store>/calibration.json`) is applied to the gating value. Accuracy is
unchanged by a monotone map; only thresholds move.

## Telemetry, evaluation and tuning

The plugin logs every pick to `<store>/decisions.jsonl`:

```jsonc
{"kind":"find","query":"what broke the build?","candidates":[…],
 "pick":"a3f2c1b9d4e0","confidence":0.37,"entropy_confidence":0.16,
 "calibrated":0.72,"latency_ms":38,"probabilities":{…}}
{"kind":"outcome","action":"expand","ref":"a3f2c1b9d4e0","matched_pick":true}
{"kind":"outcome","action":"evict","ref":"…","resident_turns":2,"matched_pick":false}
```

Five scripts (in `forseti/scripts`) turn that into a dataset and a scorecard,
reusing the plugin's **exact** shortlist and Laya client:

```sh
# labelled rows: synthetic (vocab/decision + content/retrieval), authored TSV,
# or telemetry outcomes
bun run scripts/hashtext-dataset.ts --synthetic 200 --out ~/.hashtext/eval.jsonl
bun run scripts/hashtext-label.ts --review ~/.hashtext/review.tsv

# free-LLM data: natural queries from block content, then judge acceptable answers
bun run scripts/hashtext-generate.ts --per-block 3 --out ~/.hashtext/generated.jsonl
bun run scripts/hashtext-judge.ts --data ~/.hashtext/generated.jsonl

# scorecard: recall@k · lexical top-1 · pick accuracy · abstention · ECE · latency
bun run scripts/hashtext-eval.ts --data ~/.hashtext/generated.judged.jsonl
bun run scripts/hashtext-calibrate.ts --write
```

### Measured internally (2026-10-18, Apple Silicon; directional, not a benchmark)

| What | Result |
|---|---|
| zstd (level 10) on docs/code/logs | ~3–5× smaller (README 4.1 KB→1.8 KB; code 0.31; logs 0.19) |
| Flat 200-block index | 2,294 tokens/turn |
| Tiered index, 200 blocks | ~180 tokens/turn |
| CLI `list` / `show` | 37.6 ms / 30 ms (interpreter startup) |
| Native reads (metadata / decompress+scan 302 KB) | 4.7 ms / 8.3 ms |
| Progressive view of a 40 KB log | ~100–300 tokens |
| 136-block LLM-generated eval, judged labels | recall@7 **100%**, lexical top-1 **80%** |
| Laya on the same rows | **68%** (laya-only 0, lexical-only 3); abstention recall 42.9% |
| Isotonic calibration (32 decisions, in-sample) | ECE **0.273 → 0.068**; at 0.85: 65.6% coverage, 95.2% precision |

**Verdict recorded:** a Laya fine-tune is **not justified** on this evidence.
The ranking gap is real (recall 100% vs top-1 80%), but the base model never
fixed a lexical mistake and broke three. The next step is a cheap reranker over
BM25 features on a larger judged set — not a 421M head-only fine-tune.

## Shared store with context-hydra

[`context-hydra`](https://github.com/alytaphoenix/context-hydra) is an MCP
server (Rust) that keeps an ACID matrix, salience/pin/reap, checkout/checkin and
a nonce trust fence. Its bodies now use this exact block format:

- `sha256` id, `blocks/{sha[:2]}/{sha}.zst|.json`
- zstd frames with the content size in the header
- fingerprint verification, atomic writes, prefix refs
- legacy plaintext bodies migrate at startup

Point both at the same `HASHTEXT_DIR` and the CLI, the pi extension and any MCP
client share one body store. The two front-ends keep their own interaction
models: pi projects content per request; hydra exposes explicit tools. Hydra's
`cargo test` cross-checks reads and writes against this CLI in both directions.

## Operations

### Environment

| Variable | Used by | Meaning |
|---|---|---|
| `HASHTEXT_DIR` | all | store root (default `~/.hashtext`) |
| `HASHTEXT_BIN` | pi plugin | CLI path (default `hashtext`, then common install locations) |
| `FORSETI_LAYA_URL` | pi plugin | Laya endpoint (default `http://127.0.0.1:8751`) |
| `HASHTEXT_TTL_TURNS` | pi plugin | optional age-based residency demotion (default 0/off) |

### Backup and restore

The store is immutable-by-content: copying `$HASHTEXT_DIR/blocks` is a valid
backup. Restore by copying back; refs remain valid. `resident/`,
`decisions.jsonl` and `calibration.json` are session state and can be dropped.

### Performance expectations

- Reads are a metadata read + one zstd frame; ~5 ms for 200 blocks natively.
- Writes are one compression + two atomic writes; ~0.4 ms per block in-process.
- `list` is O(blocks); fine to a few thousand blocks. The native reader also
  builds a term index lazily for BM25 (~8 ms for 200 blocks, ~50 ms for 1k).
- Large blocks are decompressed whole for ranged reads; a chunked/seekable
  format is future work.

### Corruption and recovery

- Payload mismatch/missing → `IntegrityError` (reads fail loudly, never return
  garbage).
- `hashtext gc` reports and removes dangling metadata/orphan blobs.
- A lost `blocks/` directory means lost cold data; nothing else depends on it.

## Security and trust model

- **Stored content is untrusted data.** The pi plugin wraps expansions in a
  nonce-tagged fence and interpolates labels only after sanitizing control
  characters and structural delimiters. This makes delimiter breakout hard; it
  does **not** make the model immune to instructions inside the content. Treat
  retrieved blocks as data, not instructions.
- **No network, no daemon.** The store is local files; the CLI never contacts
  anything. The optional Laya call is loopback-only and fail-open.
- **Permissions.** Files are created `0600` in the user's home; the store is
  private by default. There is no encryption at rest — use an encrypted volume if
  the material is sensitive.
- **Prompt injection is a consumer concern.** The plugin logs decisions and
  exposes provenance, but cannot certify what a block contains. Prefer ingesting
  material you trust, and keep the archive threshold conservative.

## Limits and roadmap

Known limits:

- No semantic/lossy codec in v1 (the `BlockCodec` seam exists).
- No multi-writer index or global lock; residency is per session.
- The pi plugin is pi-specific; other agents use the CLI or hydra's MCP server.
- Token estimates are heuristic (~4 chars/token), not a real tokenizer.
- No chunked/seekable storage for very large blocks.

Next, in order of evidence:

1. A cheap reranker over BM25 features on a larger judged set (before any L2
   fine-tune).
2. zstd dictionary training (measured 42–61% on realistic small blocks).
3. CDC packfile for evolving artifacts (measured ~45% on log revisions).
4. Project/tag scoping and optional stable-prefix projection.
5. Hydra progressive views (`checkout view=outline|focus|range`).

## Development

```
hashtext/
├── pyproject.toml          # hatchling; zstandard; pytest dev group
├── uv.lock
├── src/hashtext/
│   ├── codec.py            # BlockCodec protocol + ZstdCodec
│   ├── store.py            # BlockStore: identity, dedup, atomic I/O, gc, grep
│   ├── index.py            # index-line rendering
│   ├── tokens.py           # TokenCounter heuristic
│   └── cli.py              # JSON CLI
└── tests/                  # 66 cases: codec, store semantics, CLI contract
```

```sh
uv sync
uv run pytest            # 66 tests
uv run hashtext stats
```

The consumer gate lives in forseti:

```sh
bun run scripts/hashtext-smoke.ts   # real store + CLI, stubbed pi API
```

It covers the compact index, all expansion modes, single materialization per
request, eviction, budget withholding, read/grep, dedup, request-time archival,
Laya find/auto/janitor, `/hash evict`, native-vs-CLI parity, missing-CLI
degradation, dataset generation, calibration and telemetry labelling.

The Rust backend gate:

```sh
cd context-hydra
cargo test                            # cross-language both directions
python3 scripts/smoke_block_backend.py # MCP offload → checkout → checkin → forget
```

## License

MIT — see [LICENSE](LICENSE).
