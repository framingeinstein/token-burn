# Token-Burn Phase 2 — Snapshots + Local Server — Design Spec

- **Date:** 2026-06-06
- **Status:** Draft — awaiting user review
- **Owner:** Jason Morgan
- **Repo:** `~/Documents/projects/token-burn` (standalone, not part of the FramingEinstein company workspace)
- **Builds on:** `docs/superpowers/specs/2026-06-05-token-burn-dashboard-design.md` (Phase 1)

## 1. Summary & Goal

Phase 1 parses the **whole Claude Code corpus on demand** into a throwaway `data.json` and inlines it
into a static, offline `out/dashboard.html`. That works, but it has one fatal limitation for a *feedback
loop over time*: **Claude Code only retains ~30 days of logs.** Once a day rolls off
`~/.claude/projects/**/*.jsonl`, its usage is gone forever — so the dashboard can never show more than a
trailing month, no matter how long you use it.

Phase 2 makes the data **durable and incremental**:

1. A **daily cron** freezes each completed day into an **append-only archive** (`snapshots.jsonl`) before
   its logs roll off. The corpus shrinks; the archive grows without bound.
2. A **local HTTP server** serves the archive **merged with a live-parsed _today_**, so the dashboard
   always shows today's burn accumulating in real time — not just through the last snapshot.
3. A **backfill** seeds the archive from the full span currently on disk (2026-05-07 → yesterday),
   pricing each historical day at the **rate in effect on that day** (best-effort researched).
4. Phase 1's **offline self-contained file stays** — built from the same archive + live-today merge.

**The dashboard's data contract is unchanged** (`{ meta, days[] }`), so the four views and the metric
toggle are untouched. The only frontend change is detecting whether data is inlined (offline file) or must
be fetched (server).

## 2. Decisions (locked during brainstorming, 2026-06-06)

| # | Decision | Choice |
|---|---|---|
| D1 | **Snapshot role** | **Archive** — permanent, authoritative record. Finalized days are *immutable*; only "today" is re-parsed live. |
| D2 | **Storage format** | **Single append-only JSONL ledger** (`snapshots.jsonl`), one line per day, dedup-by-date on read (latest line wins). |
| D3 | **Daily trigger** | **cron** — one documented crontab line. Snapshotter is idempotent + catch-up, so missed fires self-heal within the retention window. |
| D4 | **Server** | **Live server _and_ keep the offline file.** Server serves `/` + `/api/data` (archive + live today). `build.sh` still emits the self-contained offline file. |
| D5 | **Cost** | **Frozen at capture** — each ledger line stores `cost_usd` stamped with `rates_version`; a rate correction is a deliberate `--refinalize`, not an automatic reprice. |
| D6 | **Port** | Default **8799**; if taken, auto-increment to the next free port; **report the actual bound port**. |
| D7 | **Ledger in git** | **Committed.** `snapshots.jsonl` is the durable history. `data.json` / `out/` stay gitignored. |
| D8 | **Backfill + historical pricing** | A backfill generates daily snapshots back to the start of the current logs, pricing each day at its **date-effective** rate via a **best-effort researched dated price table** (`prices.json`). |

## 3. Data Source (unchanged from Phase 1)

`~/.claude/projects/**/*.jsonl`, including nested `*/subagents/agent-*.jsonl`. Same fields, same dedup
rule (group assistant events by `message.id`, keep the max-`output_tokens` occurrence — see Phase-1 §3.1).

**Current corpus survey (2026-06-06):**

- **Span:** 2026-05-07 → 2026-06-06 (UTC), ~31 days.
- **Models present** (by usage-line count): `claude-opus-4-7` (62.6K), `claude-opus-4-8` (40.1K),
  `claude-sonnet-4-6` (7.6K), `claude-haiku-4-5-20251001` (5.6K), `<synthetic>` (143).
- `<synthetic>` is Claude Code's internal/system message marker, **not a billable model**.

## 4. Architecture

One shared core, three thin entry points. The snapshotter (cron), the server, and the offline build all
call the same two functions, so there is exactly **one** definition of "what a day's burn looks like" and
**one** definition of "how history + today merge."

```
token-burn/
├── parse.py          # REFACTORED — day_records(root, tz, since, until) → per-day rollup entries.
│                     #   cost_usd(model, date, components) becomes date-aware (reads prices.json).
│                     #   scan() kept as a thin whole-corpus wrapper for ad-hoc use.
├── prices.py         # NEW — load prices.json; rate_for(model, date) → effective rate; flag unknowns.
├── prices.json       # NEW — dated price table (model-id pattern × effective-date range + source).
├── ledger.py         # NEW — read_ledger(path) (dedup latest-wins), append_day(path, record),
│                     #   assemble_rollup(ledger, root, tz, today) → { meta, days[] }.
├── snapshot.py       # NEW — cron entry. Finalizes missing complete days (< today). Idempotent +
│                     #   catch-up. Flags: --backfill, --refinalize [--since DATE], --root/--tz/--ledger.
├── serve.py          # NEW — stdlib http.server. / → dashboard, /api/data → assemble_rollup, /healthz.
│                     #   Port auto-increment + report.
├── dashboard.html    # MODIFIED — if inlined DATA present use it; else fetch('/api/data').
├── build.sh          # MODIFIED — offline file now built from assemble_rollup (ledger + live today).
├── install-cron.sh   # NEW — installs/prints the documented daily crontab line.
├── snapshots.jsonl   # the archive — COMMITTED to git.
├── data.json         # generated, gitignored (intermediate for build.sh)
├── out/dashboard.html# generated, gitignored, self-contained offline file
└── tests/
```

### 4.1 Shared core

- **`parse.day_records(root, tz, since=None, until=None) -> list[day_record]`**
  Walks the logs, dedups by `message.id`, buckets to local-tz days, and returns the per-day rollup
  entries (the Phase-1 `days[]` shape) for days within `[since, until]`. `until` is *inclusive of the
  date string*; callers pass `until=yesterday` (snapshotter) or `since=until=today` (server live slice).
  **Dated pricing is applied per message** (where the exact model id and date are known) via
  `rate_for(model, date)`, then components are collapsed into **class** buckets for storage/display — so
  `cost_usd` is precise even when two same-class models (e.g. `opus-4-7` vs `opus-4-8`) ever differ in
  price, while the stored shape stays Phase-1-compatible.

- **`ledger.assemble_rollup(ledger_path, root, tz, today) -> { meta, days[] }`**
  1. `read_ledger` → finalized day records for every `date < today` (authoritative; **immutable**).
  2. `parse.day_records(since=today, until=today)` → today's live record (priced at *current* rates).
  3. Merge: ledger wins for any `date < today` even though logs still exist within retention — so
     finalized days never shift and today is never double-counted.
  4. Recompute `meta.totals` and sanity counts across the merged set.

### 4.2 `prices.py` / `prices.json` (dated, researched)

`prices.json` is the single source of pricing truth, inspectable and version-stamped:

```jsonc
{
  "version": "2026-06-06",
  "default_cache_write_mult": 1.25,
  "default_cache_read_mult": 0.10,
  "rates": [
    { "match": "claude-opus-4-8*",  "in": 5.0, "out": 25.0, "from": "2026-01-01", "to": null,
      "source": "claude-api skill / anthropic pricing 2026-06" },
    { "match": "claude-opus-4-7*",  "in": 5.0, "out": 25.0, "from": "2026-01-01", "to": null,
      "source": "<researched>" },
    { "match": "claude-sonnet-4-6*","in": 3.0, "out": 15.0, "from": "2026-01-01", "to": null, "source": "..." },
    { "match": "claude-haiku-4-5*", "in": 1.0, "out": 5.0,  "from": "2026-01-01", "to": null, "source": "..." }
  ],
  "unpriced": ["<synthetic>"]
}
```

- **`rate_for(model, date)`** matches the model id against `match` patterns where `from <= date < to`
  (`to: null` = open-ended). Cache multipliers default to the table-level values unless an entry overrides.
- **Unknown model OR `<synthetic>` → cost 0**, and the model id is collected into `meta.unpriced_models`.
  This fixes a latent Phase-1 bug where `model_class()` defaulted unknown models to **"sonnet"** and thus
  mispriced them. Token components for unpriced models still count toward metrics 1–3 (compute), only their
  **$** is 0. For **display bucketing** such models also move to an **`other`** class instead of the
  `sonnet` default, so they don't inflate Sonnet's compute share; `drawModels` already renders whatever
  classes are present, so no view code changes.
- **Population is best-effort research** done during implementation: current rates from the `claude-api`
  skill; any historical/older-model rates and change-dates via web search; every `rates[]` entry carries a
  `source`. Within the current 31-day window prices are expected to be flat per model — the date-range
  dimension matters mainly when history later extends past a real price change.

### 4.3 `snapshot.py` (cron entry)

```
python3 snapshot.py                      # daily: finalize any complete day (< today) not yet in ledger
python3 snapshot.py --backfill           # finalize ALL complete days present in logs (first-run seed)
python3 snapshot.py --refinalize --since 2026-05-20   # re-append fresh lines for a date range (rate fix)
```

- **Finalize policy:** a day is finalizable only when `date < today` (local tz). Today is never frozen.
- **Idempotent:** reads the ledger first; appends only days not already present. A no-op run adds 0.
- **Catch-up:** backfills *any* missing complete day still present in logs (covers laptop-asleep gaps up to
  ~30-day retention). `--backfill` is the same engine with the date floor set to the oldest log day; the
  ordinary daily run already does this, so the two share one code path.
- **Each appended line** is stamped `finalized_at`, `tz`, and `rates_version = prices.json.version`, with
  `cost_usd` frozen at the date-effective rate.
- **Retention guard:** if the ledger is non-empty and the oldest log day is newer than
  `latest_ledger_day + 1` (a gap that may have rolled off), print a warning. History begins at first
  capture / earliest log present at first run.
- **Atomic append:** each ledger line is written with a single `write()` of `json.dumps(record) + "\n"`.

### 4.4 `serve.py` (local server)

- Stdlib `http.server` + `socketserver`. Flags: `--port` (default 8799), `--root`, `--tz`, `--ledger`.
- **Routes:** `/` → `dashboard.html` (served so it fetches data) · `/api/data` → `assemble_rollup` JSON ·
  `/healthz` → `200 ok`.
- **Read-only:** the server does **not** run the snapshotter (cron owns capture — D3). Concerns stay
  decoupled.
- **Port handling (D6):** attempt to bind `--port`; on `OSError`/in-use, increment and retry up to a small
  bound (e.g. +20); on success print `serving token-burn at http://127.0.0.1:<actual_port>`; if none free,
  exit non-zero with a clear message.

### 4.5 `dashboard.html` / `build.sh`

- `dashboard.html` gains a tiny bootstrap: if the inlined `DATA` placeholder was filled (offline file),
  render from it; otherwise `fetch('/api/data')` then render. No view code changes.
- `build.sh` calls `assemble_rollup` (ledger + live today) → writes `data.json` → inlines into
  `out/dashboard.html`. The offline file therefore reflects the **full archive**, not just the trailing
  ~30 days.

## 5. Ledger line schema

One JSON object per line — a Phase-1 `days[]` entry plus provenance:

```jsonc
{ "date": "2026-05-25",
  "finalized_at": "2026-05-26T07:02:00Z",
  "tz": "America/New_York",
  "rates_version": "2026-06-06",
  "byModel": {
    "opus": { "in": 0, "out": 0, "cc": 0, "cr": 0, "cost_usd": 0.0 }   // class-keyed; components + FROZEN cost
  },
  "mainVsSub": { "main": { /* components + cost_usd */ }, "sub": { /* ... */ } },
  "topProjects": [ { "project": "FramingEinstein", "total": 0 } ],
  "sampleSessions": [ { "label": "Resume MR-3a cost-ledger", "project": "ai-llm-research", "total": 0 } ] }
```

- **Raw components** (`in/out/cc/cr`) are retained so metrics 1–3 stay client-side and so a `--refinalize`
  can re-derive cost without the original logs (precise within a price-stable class; exact per-model-id
  re-pricing needs the logs, which are present within retention).
- `byModel` is **class-keyed** (`opus`/`sonnet`/`haiku`/`other`) — unchanged from Phase 1, so the dashboard
  is untouched. Precise dated pricing is applied per message *before* collapsing to class (§4.1).
- `cost_usd` is **frozen** at `rates_version`.

## 6. Data flow (summary)

- **Capture (cron, daily):** `snapshot.py` → read ledger → for each complete day `< today` not present,
  `day_records()` → append line.
- **Serve (live):** `serve.py /api/data` → `assemble_rollup` = ledger (`< today`, authoritative) + live
  today → merged `{ meta, days[] }`.
- **Offline build:** `build.sh` → `assemble_rollup` → inline → `out/dashboard.html`.
- **Backfill (one-time seed):** `snapshot.py --backfill` → all complete days 2026-05-07 → yesterday, each
  date-priced.

## 7. Token metric & cost definitions

Metrics 1–3 (Total compute / Generative / Output) are unchanged from Phase-1 §6 and computed client-side
from components. Metric 4 (**Cost $**) is **frozen per day** in the ledger via `cost_usd(model, date,
components)`:

```
cost_usd = ( in·Pin + out·Pout + cc·Pin·Wwrite + cr·Pin·Wread ) / 1e6
```

where `Pin/Pout/Wwrite/Wread = prices.rate_for(model, date)`. Today's live slice uses
`rate_for(model, today)`; it freezes when the day is finalized.

## 8. Edge cases & error handling

- **Empty ledger** (pre-first-run) → `assemble_rollup` returns today-only. **No logs today** → today omitted.
- **Corrupt / half-written ledger line** → skip + warn (does not abort the read).
- **Duplicate date lines** in the ledger → latest line wins (D2).
- **tz** is recorded per line; warn if the current `--tz` differs from the ledger's recorded tz.
- **Unknown / `<synthetic>` model** → `cost_usd = 0`, id added to `meta.unpriced_models` (never defaulted
  to another model's price).
- **Finalized day with lingering logs** (still within retention) → ledger wins; logs for that day ignored.
- **Port exhaustion** → clear non-zero exit (D6).
- **DST / local-midnight boundaries** → handled by `zoneinfo` as in Phase 1.

## 9. Testing (TDD, stdlib + pytest)

Parser/ledger/pricing are pure logic and get real tests; the HTML is verified by eye.

- `day_records` date-filtering: only days in `[since, until]`; today excluded when called for finalization.
- `rate_for`: picks the entry whose `[from, to)` contains the date; pattern match by model id; unknown /
  `<synthetic>` → 0 + flagged.
- `cost_usd` date-awareness: same model, two dates spanning a (hypothetical) rate change → different cost.
- `read_ledger` dedup: same date twice → latest wins.
- snapshotter **idempotency**: run twice → second run appends 0.
- snapshotter **catch-up**: ledger missing a middle day → backfilled on next run.
- **today never finalized**: snapshot run leaves today out of the ledger.
- `--refinalize`: re-appends; read picks the latest; `cost_usd` reflects the new table.
- `assemble_rollup` merge: finalized day with lingering logs → ledger wins, no double-count; live today
  appended.
- **frozen cost preserved**: changing `prices.json` does *not* alter already-finalized ledger `cost_usd`
  (only `--refinalize` does).
- **backfill span**: `--backfill` over a fixture corpus covers every complete day, excludes today.
- server: `assemble_rollup` handler returns valid merged JSON; port auto-increment picks a free port and
  reports it.

## 10. Non-goals (unchanged) / Future

**Still out of scope:** multi-tool ingestion (Codex/Cursor/ChatGPT); deploy / hosting / domain; auth /
multi-user / sharing; redaction (local + private).

**Deferred:** per-model time-series lines; week-over-week trend view; richer historical price coverage as
the archive extends past real rate changes.
