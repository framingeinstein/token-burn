# Token Burn

A local, offline dashboard of your **Claude Code** token usage over time — a feedback loop for
seeing, and improving, how you actually use AI.

It reads exact numbers from Claude Code's own JSONL logs rather than estimating them, and renders
everything into a single self-contained HTML file. No server required, no account, no network
call, nothing uploaded. Python 3 standard library only.

Inspired by Nate B. Jones' "token burn dashboard" — this one reads the logs instead of
approximating.

---

## Quick start

```bash
git clone https://github.com/framingeinstein/token-burn.git
cd token-burn
python3 snapshot.py    # capture the history sitting in your logs right now
./build.sh             # build and open the dashboard
```

`snapshot.py` reads `**/*.jsonl` under every Claude Code store — `~/.claude/projects`, each `~/.claude-*/projects` (per-workspace `CLAUDE_CONFIG_DIR` logins), and `$CLAUDE_CONFIG_DIR/projects` — and freezes every completed day into a local
archive. `build.sh` turns that archive — plus today, parsed live — into a fully self-contained
`out/dashboard.html`. Double-click that file any time; it works offline, forever.

**Run `snapshot.py` first.** `build.sh` on its own only parses *today* and reads the rest from the
archive, so without that first capture a fresh clone shows a single day. The first `snapshot.py`
run backfills everything still on disk at once.

**That backfill is your starting point, not your whole history.** Claude Code keeps roughly the
last 30 days of logs; days older than that are already gone. From here the archive only grows, so
run `snapshot.py` daily — see [Keeping history](#keeping-history).

Requires Python 3.11+. No dependencies.

---

## What you get

- **Daily burn** — a GitHub-style calendar heatmap, shaded by the metric you pick.
- **Over time (log scale)** — tokens per day on a log axis, which is what it takes to show a range
  from a few million to a billion on one chart.
- **Top 10 days** — your biggest days *and what you were doing on them* (projects and session
  titles).
- **By model & by agent** — the Opus/Sonnet/Haiku split, plus main-thread versus sub-agent work,
  which is usually the surprising one.
- **Cursor activity** — optional; sessions and context-window occupancy by model.

**Metric toggle:** Total compute (`in + out + cache_create + cache_read`) · Generative
(`in + out + cache_create`) · Output only · Cost $ (API-equivalent).

**Source toggle:** Claude Code · Cursor · Combined.

---

## Why the numbers are right: the dedup gotcha

Claude Code writes **one JSONL line per content block** of an assistant turn — a `thinking` line, a
`text` line, one per `tool_use` — and stamps *every one of them* with the same message-level
`usage` object.

Summing raw log lines therefore double-counts by roughly **2×**.

`parse.py` deduplicates by assistant `message.id`, keeping the occurrence with the largest output.
As a sanity check it prints `events -> unique msgs`; a ratio near 2× is expected and correct. This
is the single most important detail in the project — a dashboard that gets it wrong will confidently
tell you that you spend twice what you do. See
[`docs/superpowers/specs/2026-06-05-token-burn-dashboard-design.md`](docs/superpowers/specs/2026-06-05-token-burn-dashboard-design.md).

## Cost

Cost is **API-equivalent**: what the same tokens would have cost at published API rates. If you are
on a Claude subscription this is not your bill — it is a measure of the compute you consumed, which
is the number worth watching.

Rates live in `prices.json` as a dated table (model id × date range, with a source). Cost is
**frozen at capture time**, so correcting a rate never silently rewrites your history; re-price
deliberately with `snapshot.py --refinalize --since YYYY-MM-DD`. Models with no known rate are
counted as `$0` and reported in `meta.unpriced_models` rather than being guessed at.

---

## Keeping history

Claude Code's logs expire (~30 days). To keep a permanent record, `snapshot.py` freezes each
completed day into an append-only local archive, `snapshots.jsonl`.

```bash
python3 snapshot.py     # freeze completed days; run daily
./install-cron.sh       # or install a 09:00 daily cron entry that does it for you
```

The first run seeds everything still on disk; later runs catch up from the last recorded day.
Finalized days are immutable — only *today* is ever parsed live.

**Your archive is yours.** `snapshots.jsonl` is gitignored and never leaves your machine. It
contains your project names and session titles, so treat it the way you would the logs it came
from: do not commit it, and do not paste it into an issue.

### Live server (optional)

```bash
python3 serve.py        # http://127.0.0.1:8799
```

Serves the dashboard with today's data merged live on every load: `/api/data` (archive + live
today) and `/healthz`. Read-only — capture stays with `snapshot.py`. If the port is taken it
increments to the next free one and prints where it landed.

On macOS, `./install-launchd.sh` supervises that server as a LaunchAgent
(`com.token-burn.serve`) so it starts at login and restarts if it dies; `--uninstall` removes it.
Logs land in `~/.config/token-burn/serve.{out,err}`. After editing `serve.py` or `dashboard.html`,
restart it with `launchctl kickstart -k gui/$UID/com.token-burn.serve`.

---

## Remote runners (optional)

Claude Code running elsewhere — e.g. a fleet of headless `claude -p` runners on
Cloud Run — never writes to this machine's stores. If those runners upload their
session JSONL to a bucket laid out as `{runner}/{issue}/{execution}/{session}.jsonl`,
token-burn can account for them too:

```bash
cat > .env.factory <<'ENV'          # gitignored
TOKEN_BURN_FACTORY_BUCKET=gs://your-transcript-bucket
CLOUDSDK_CONFIG=/path/to/gcloud/config   # optional: a config with read access
ENV
./sync-factory.sh                   # incremental mirror -> ~/.token-burn/factory-transcripts
python3 snapshot.py                 # freezes factory days into factory-snapshots.jsonl
```

`install-cron.sh` chains the sync before the daily snapshot. Factory usage is
attributed from the bucket path as `factory:<runner>` (runners share a `/tmp/wt-N`
cwd, so cwd can't be used), counts each `message.id` once across re-uploaded
sessions, and lives in its own archive so backfilling it never re-derives your
local days. The dashboard's "by source" bars split this machine from each runner.

## Cursor (optional)

Cursor support is entirely optional — skip this section and everything else works.

**Local activity.** Cursor's local database records sessions, selected models, and current
context-window occupancy. Its local token-count fields are zero, so these are *activity signals,
not billed tokens*. The database path defaults to macOS
(`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`); elsewhere, pass
`--cursor-db`. If it is missing, the dashboard just reports Cursor as unavailable.

**Billed usage.** Exact billed tokens and charges come from Cursor's personal usage dashboard
endpoint, which requires your session cookie.

> ⚠️ **This endpoint is unofficial.** It is not a documented, supported API: authentication and
> response shapes can change without notice, and this integration may break. The
> `WorkosCursorSessionToken` cookie **is a credential for your Cursor account** — treat it like a
> password.

1. Sign in at `https://cursor.com/dashboard/usage`.
2. Copy the `WorkosCursorSessionToken` cookie from your browser's developer tools.
3. Store it outside the repo, readable only by you:

```bash
mkdir -p ~/.config/token-burn
printf '%s' 'PASTE_COOKIE_VALUE' > ~/.config/token-burn/cursor-session-token
chmod 600 ~/.config/token-burn/cursor-session-token
python3 cursor_usage.py
```

`cursor_usage.py` resumes from the last captured day, fetches paginated events, and appends only
daily aggregate rollups to `cursor-snapshots.jsonl` (also gitignored). It never stores the cookie
or the raw API events. `snapshot.py` does the same capture during the daily run when the token file
exists; Claude snapshots continue even if Cursor capture fails.

Claude cost is API-equivalent; Cursor cost is the amount in Cursor's own `chargedCents`. "Combined"
adds those two explicitly different measures — read it as an order of magnitude, not a bill.

---

## Privacy

Everything stays on your machine. Nothing is uploaded, and there is no telemetry.

Full prompt and session-title detail is deliberately kept, because "what were you doing on your
most expensive day" is the whole point of the tool. That also means the generated artifacts are
sensitive. `snapshots.jsonl`, `cursor-snapshots.jsonl`, `data.json`, and `out/` are all gitignored
for that reason — check before you share a build, a screenshot, or a snapshot file.

---

## Platform support

| | macOS | Linux | Windows |
|---|---|---|---|
| Claude parsing, `build.sh`, `serve.py` | ✅ | ✅ | untested |
| `install-cron.sh` | ✅ | ✅ | — |
| `install-launchd.sh` | ✅ | — | — |
| Cursor local activity | ✅ | `--cursor-db` | `--cursor-db` |

`build.sh` opens the result automatically on macOS; elsewhere it prints the path.

---

## Development

```bash
pip install pytest
python3 -m pytest tests/ -v
```

The tool itself is standard library only; `pytest` is the sole development dependency. Tests run on
Python 3.11–3.13 in CI.

### Layout

| File | Role |
|---|---|
| `parse.py` | logs → per-day rollups (owns the `message.id` dedup) |
| `ledger.py` | archive read/append + merge of frozen history with live today |
| `prices.py` / `prices.json` | dated rate table; cost frozen at capture |
| `snapshot.py` | daily capture entry point (idempotent, catches up) |
| `serve.py` | stdlib HTTP server: `/`, `/api/data`, `/healthz` |
| `build.sh` | self-contained offline `out/dashboard.html` |
| `dashboard.html` | the UI; auto-detects inlined data vs live `fetch` |
| `cursor_local.py` / `cursor_usage.py` | optional Cursor activity and billed usage |

Design notes and implementation plans are in [`docs/superpowers/`](docs/superpowers/).

## License

[MIT](LICENSE)
