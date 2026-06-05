# Token-Burn Dashboard — Design Spec

- **Date:** 2026-06-05
- **Status:** Draft — awaiting user review
- **Owner:** Jason Morgan
- **Repo:** `~/Documents/projects/token-burn` (standalone, not part of the FramingEinstein company workspace)

## 1. Summary & Goal

A **personal, local, offline** dashboard that visualizes my own Claude Code AI usage ("token burn")
over time, so I can *see and improve how I use AI* — a feedback loop, not a brag sheet.

Inspired by Nate B. Jones' "token burn dashboard" video
(*"My Codex Ran 800 Million Tokens in a Day"*, https://youtu.be/l8BloTSLK6M). His version reads
**Codex** logs and has to *approximate* Claude usage because Claude chat/web doesn't expose tokens.
**Our version is the inverse and strictly easier:** Claude Code writes **exact** per-message token
usage to local JSONL logs, so we get precise numbers with zero inference.

The dashboard renders four views, all driven by a single metric toggle, from a one-pass parse of
the local logs.

## 2. Non-Goals (YAGNI)

Explicitly **out of scope** for this build:

- No ingestion of other tools (Codex / Cursor / ChatGPT) — none exist on this machine; single source only.
- No hosting, deploy, domain, or DNS (Nate's `tokenburn.*` site). Local file only.
- No sharing / "Talent Board" / public accountability features.
- No live server, daemon, or file watcher. Refresh is an on-demand script.
- No auth, multi-user, or productization. (If that changes later, it's a separate spec.)
- No redaction. It's local and private; full prompt/title detail is kept (per Nate's advice for
  private dashboards).

## 3. Data Source

**Single source:** `~/.claude/projects/**/*.jsonl`, including nested `*/subagents/agent-*.jsonl`.

Survey of this machine (2026-06-05): **122 project dirs, 1,322 session logs**, spanning ~May→June 2026.

Each line is a JSON event. Relevant fields (verified empirically against real logs):

| Field | Where | Use |
|---|---|---|
| `type` | top-level | `"assistant"` events carry usage; filter the rest |
| `message.usage` | assistant msgs | `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` (+ `cache_creation.ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens`) |
| `message.id` | assistant msgs | **dedup key** (e.g. `msg_01XWP...`) |
| `message.model` | assistant msgs | e.g. `claude-opus-4-7`, `claude-haiku-4-5-20251001` |
| `timestamp` | top-level | ISO8601 UTC; bucket to **local-tz day** |
| `cwd` | every event | real working dir → project label (last path segment) |
| `gitBranch` | every event | optional secondary dimension |
| `isSidechain` | events | `true` ⇒ subagent/sidechain message |
| `customTitle` / `aiTitle` | session events | human-legible session title for "what I did" |
| `message.content` | user msgs | fallback session label = first non-meta user prompt |
| `isMeta`, `isCompactSummary`, `userType` | events | filter non-user / compaction noise when picking a session label |

### 3.1 Critical correctness issue — deduplication

Assistant messages are logged **multiple times per `message.id`**. Measured on one real session:
**3,684 assistant events → only 1,754 unique `message.id`s** (many appear 3×). Naive summation
inflates totals by **~2×**.

**Rule:** group assistant events by `message.id`; keep exactly **one** usage record per id — the
occurrence with the **greatest `output_tokens`** (the final/cumulative streaming state). The parser
must emit a sanity stat (`assistant_events` vs `unique_messages`) so inflation regressions are visible.

## 4. Architecture

Three small, independently-testable units. One transform: `JSONL → (day × model × component) rollup → static view`.

```
token-burn/
├── parse.py            # stdlib-only parser → data.json
├── dashboard.html      # template: pure SVG + vanilla JS, no network
├── build.sh            # parse.py → inline data into dashboard → open result
├── out/dashboard.html  # generated, self-contained, double-click to open (gitignored)
├── data.json           # generated rollup (gitignored)
├── tests/
│   ├── fixtures/*.jsonl
│   └── test_parse.py
└── docs/superpowers/specs/2026-06-05-token-burn-dashboard-design.md
```

### 4.1 `parse.py` (Python **stdlib only** — no pip deps)

**Input:** glob of `~/.claude/projects/**/*.jsonl` (configurable root; `--since` / `--tz` flags).
**Output:** `data.json`.

**Algorithm:**
1. Walk all `*.jsonl`. For each `assistant` event with `message.usage`, extract a raw record:
   `{message_id, ts, model, input, output, cache_create, cache_read, cache_create_1h, cache_create_5m,
   cwd, git_branch, is_subagent, session_id, file}`. `is_subagent = isSidechain == true OR file under /subagents/`.
2. **Dedup** by `message_id`, keeping max-`output` record (§3.1).
3. Bucket each record to a **local-tz day** (`YYYY-MM-DD`).
4. In a second pass over **user** events (skipping `isMeta`/`isCompactSummary`/tool-result), collect per
   session: `session_label = customTitle || aiTitle || first_user_prompt[:120]`, and project = last
   segment of `cwd`.
5. Roll up into `data.json` (§5).

### 4.2 `dashboard.html` (pure SVG + vanilla JS, offline)

- **No CDN, no fetch.** `build.sh` inlines `data.json` as `const DATA = {...}` so the file opens from
  `file://` with a double-click (avoids `file://` fetch/CORS failure). The committed `dashboard.html`
  is the template with a `/*__DATA__*/` placeholder; the generated `out/dashboard.html` is self-contained.
- Tufte aesthetic: high data-ink ratio, restrained palette, minimal chrome, readable at a glance.
- A single **metric toggle** (radio/segmented control) re-renders all four views client-side from `DATA`.

### 4.3 `build.sh`

`python3 parse.py` → inject `data.json` into `dashboard.html` → write `out/dashboard.html` → `open` it.
This is the entire "refresh" workflow. On-demand, no daemon.

## 5. `data.json` Schema

```jsonc
{
  "meta": {
    "generated_at": "2026-06-05T...Z",
    "tz": "America/...",
    "assistant_events": 0,        // pre-dedup count (sanity)
    "unique_messages": 0,         // post-dedup count (sanity)
    "totals": { "total": 0, "generative": 0, "output": 0, "cost_usd": 0.0 }
  },
  "days": [
    {
      "date": "2026-06-05",
      "byModel": {
        "claude-opus-4-8": { "input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "cost_usd": 0.0 }
      },
      "mainVsSub": { "main": { /* component totals */ }, "sub": { /* component totals */ } },
      "topProjects": [ { "project": "FramingEinstein", "total": 0 } ],
      "sampleSessions": [ { "label": "Resume MR-3a cost-ledger", "project": "ai-llm-research", "total": 0 } ]
    }
  ]
}
```

**Metric computation split (avoids ambiguity):** metrics 1–3 are computed **client-side** from the
four components (`input/output/cache_create/cache_read`) per the selected metric. Metric 4 (**cost**) is
**precomputed at parse time** into `byModel[*].cost_usd` (and summed into `meta.totals.cost_usd`),
because it needs the price table *and* the `ephemeral_1h`/`ephemeral_5m` cache split — both available only
during parsing. The browser reads `cost_usd` directly; it never recomputes cost. The rollup therefore
keeps raw components for metrics 1–3 and a precomputed scalar for metric 4.

## 6. Token Metric Definitions

Headline metric = **Total compute touched**; the toggle exposes all four. Per message, per model:

1. **Total compute touched** = `input + output + cache_create + cache_read` *(headline; ~Nate's number)*
2. **Generative work** = `input + output + cache_create` *(excludes cache re-reads)*
3. **Output only** = `output`
4. **Cost-weighted ($)** = `input·Pin + cache_create·Pin·Wwrite + cache_read·Pin·Wread + output·Pout`
   (all `tokens/1e6 · price_per_M`), summed per model.

### 6.1 Cost-weighting source (metric #4)

- Reuse the **shape** of `loom/src/lib/cost-utils.ts` (`{input, output}` $/M table), but:
  - it omits cache weighting → add Anthropic cache multipliers: **`Wread ≈ 0.10`**, **`Wwrite ≈ 1.25`**
    for 5-minute cache; use **`2.0`** for the `ephemeral_1h` portion when present (the usage breaks
    `cache_creation` into `ephemeral_1h` / `ephemeral_5m`).
  - it lists Opus **4.6**, not the **Opus 4.8 / Sonnet 4.6 / Haiku 4.5** actually used in Claude Code →
    pull **current** Claude prices from the canonical `claude-api` skill at build time.
- Unknown models fall back to a documented default and are flagged in `meta`.
- *(Open: if "the 10 Fed cost analysis deck" is a specific artifact, lift its weights instead — not located on disk yet.)*

## 7. The Four Views (all respond to the metric toggle)

1. **GitHub-style calendar heatmap** — one cell per day, intensity = selected metric. Hand-rolled SVG grid.
2. **Log-scale time series** — tokens/day over time, log Y axis (handles few-million → ~billion range).
   Optional per-model lines.
3. **Top-10 days** — date, value, **and what I did**: top projects that day + a few `sampleSessions`
   labels. The actionable view.
4. **Model distribution** — share by model, plus a **main-thread vs subagent** split (surfaces parallel
   Synkhos/`/workflows` runs as their own band).

## 8. Edge Cases

- **Dedup by `message.id`, keep max-output** (§3.1) — highest-impact correctness item.
- Skip malformed JSON lines, events without `message.usage`, and non-assistant events for token math.
- **Local-tz day bucketing** (configurable `--tz`); late-night UTC sessions must land on the right local day.
- Subagent attribution: `isSidechain==true` or `/subagents/` path → `is_subagent`, rolled up under the
  same project/day as `sub`.
- Session-label selection skips `isMeta` / `isCompactSummary` / tool-result user events.
- Days with zero usage are omitted from `days` but rendered as empty cells in the heatmap.
- Empty/partial logs and `cost_usd` for `0/0` price models → `0.0`, no crash.

## 9. Testing (TDD)

The parser is pure logic and gets real tests; the HTML is verified by eye against a known `data.json`.

Fixtures in `tests/fixtures/`:
- `dup_messages.jsonl` — same `message.id` 3×, ascending `output_tokens` → assert counted **once** at max.
- `no_usage.jsonl` — assistant event without `usage`, plus user/tool events → assert skipped.
- `subagent.jsonl` — `isSidechain:true` and a `/subagents/` record → assert `mainVsSub.sub` rollup.
- `tz_boundary.jsonl` — UTC timestamp that crosses local midnight → assert correct local day.
- `labels.jsonl` — `customTitle` vs `aiTitle` vs first-prompt precedence → assert chosen label.

`tests/test_parse.py` asserts the rollup, the dedup stat (`assistant_events` > `unique_messages`),
and cost math for a known price table.

## 10. Open Questions

1. **"10 Fed cost analysis deck"** — exact artifact for metric-#4 weights not found on disk; using
   loom shape + `claude-api` current prices unless pointed at it.
2. **Per-model line set** in the time series — start with aggregate; add per-model lines if the single
   line hides the model-mix story.

## 11. Future (deferred, not built now)

Deploy to a domain; multi-tool ingestion; shareable/exportable view. Each is a separate spec.
