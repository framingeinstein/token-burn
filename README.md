# Token Burn

A personal, **local, offline** dashboard of my Claude Code token usage over time — a feedback
loop for seeing and improving how I use AI (inspired by Nate B. Jones' "token burn dashboard",
but reading exact numbers from Claude Code's own logs instead of approximating).

It parses `~/.claude/projects/**/*.jsonl`, deduplicates by assistant `message.id`, reads local
Cursor activity, and can capture Cursor's billed usage into daily rollups. The dashboard renders
all sources from a single self-contained HTML file.

## Use

```bash
./build.sh           # parse ~/.claude logs, build, and open the dashboard
./build.sh --tz America/Los_Angeles   # bucket days in a different timezone
```

`build.sh` assembles the Claude ledger plus live today and the available Cursor rollups, writes
`data.json`, inlines that JSON into `dashboard.html`, and writes a fully self-contained
`out/dashboard.html` you can open by double-click — no server, no network, works offline.

### parse.py flags
- `--root` (default `~/.claude/projects`) — log directory to scan
- `--tz` (default: your local IANA timezone) — day-bucketing timezone
- `--out` (default `data.json`)

## Dashboard views

- **Daily burn** — GitHub-style calendar heatmap, shaded by the selected metric.
- **Over time (log scale)** — tokens/day on a log axis (handles the few-million → ~billion range).
- **Top 10 days** — your biggest days *and what you were doing* (projects + session titles).
- **By model & by agent** — Opus/Sonnet/Haiku split, plus main-thread vs sub-agent (parallel) work.
- **Cursor local activity** — sessions and context-window occupancy by model. These are activity
  signals, not billed token counts.

**Metric toggle:** Total compute (`in+out+cache_create+cache_read`) · Generative (`in+out+cache_create`)
· Output only · Cost $ (API-equivalent). Metrics 1–3 are computed in the browser; cost is
precomputed in `parse.py`.

**Source toggle:** Claude Code · Cursor · Combined. Claude cost is API-equivalent; Cursor cost is
the amount in Cursor's `chargedCents`. Combined cost adds those two explicitly labeled measures.

## Cursor usage

Cursor's local database records sessions, selected models, and current context-window occupancy,
but its local token-count fields are zero. Exact billed tokens and charges therefore come from
Cursor's unofficial personal dashboard endpoint.

1. Sign in at `https://cursor.com/dashboard/usage`.
2. In browser developer tools, copy the `WorkosCursorSessionToken` cookie.
3. Store it locally (the file is outside this repo and should be readable only by you):

```bash
mkdir -p ~/.config/token-burn
printf '%s' 'PASTE_COOKIE_VALUE' > ~/.config/token-burn/cursor-session-token
chmod 600 ~/.config/token-burn/cursor-session-token
python3 cursor_usage.py
```

`cursor_usage.py` resumes from the latest captured day, fetches paginated events, and appends only
daily aggregate model/token/cost rollups to `cursor-snapshots.jsonl`. It never stores the cookie or
raw API events. Because the endpoint is unofficial, authentication or response shapes may change.
`snapshot.py` performs the same incremental Cursor capture during the daily cron run when the token
file exists; Claude snapshots continue even if Cursor capture fails.

## The dedup gotcha (why this is accurate)

Claude Code writes **one JSONL line per content block** of an assistant turn (a `thinking` line,
a `text` line, one per `tool_use`), each stamped with the *same* message-level `usage`. Summing
raw lines therefore double-counts ~2×. `parse.py` dedupes by `message.id` (keeping the max-output
occurrence). Sanity check: the parse output prints `events -> unique msgs` — expect roughly **2×**.
See `docs/superpowers/specs/2026-06-05-token-burn-dashboard-design.md`.

## Cost rates

API-equivalent, validated 2026-06-05: Opus `$5/$25` per MTok, Sonnet `$3/$15`, Haiku `$1/$5`;
cache write `1.25×` input, cache read `0.10×` input. *(Future option: weight the `ephemeral_1h`
cache-write portion at 2× — currently flat 1.25× to match the companion cost deck.)*

## Privacy

Everything stays on your machine. Full prompt/title detail is kept (it's private). Nothing is
uploaded. `data.json` and `out/` are gitignored.

## Tests

```bash
python3 -m pytest tests/ -v   # parser unit + integration tests
```

## Phase 2 — durable snapshots + server

Phase 2 adds a committed snapshot archive so daily history survives Claude Code's ~30-day log
retention, a live HTTP server, and a self-contained build that includes today's data.

### Commands

- **`python3 snapshot.py`** — freezes each completed Claude day into `snapshots.jsonl` and, when
  configured, refreshes `cursor-snapshots.jsonl` (both append-only).
  The first run seeds the full span still on disk; later runs catch up from the last recorded day.
  Run it daily.

- **`./install-cron.sh`** — installs a daily 09:00 cron entry that runs the snapshotter and
  appends output to `snapshot.log`. Idempotent — safe to run again after updates. View the
  installed entry with `crontab -l`.

- **`python3 serve.py`** — serves the live dashboard at the `http://127.0.0.1:<port>` printed on
  startup (defaults to 8799; auto-increments to the next free port if taken).
  - `/api/data` — merges the frozen `snapshots.jsonl` archive with a live parse of today's logs.
  - `/healthz` — health check.

- **`./install-launchd.sh`** — installs `serve.py` as a macOS LaunchAgent
  (`com.token-burn.serve`) so the dashboard is always up at `http://127.0.0.1:8799`: starts at
  login, restarted by launchd if it dies. Idempotent — re-run after moving the repo. Logs land in
  `~/.config/token-burn/serve.{out,err}`. Restart after editing `serve.py` / `dashboard.html`
  with `launchctl kickstart -k gui/$UID/com.token-burn.serve`; `--uninstall` removes it. Refuses
  to install if an un-supervised server already answers on 8799 (it would otherwise be pushed to
  8800 silently).

- **`./build.sh`** — writes a fully self-contained `out/dashboard.html` (frozen archive + live
  today inlined) openable by double-click, no server needed.

### The archive

`snapshots.jsonl` is the committed, append-only archive — it's the durable history that survives
log expiry. `prices.json` is the dated price table; cost is **frozen at capture time**. After a
rate correction, re-price history with:

```bash
python3 snapshot.py --refinalize --since YYYY-MM-DD
```
