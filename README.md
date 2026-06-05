# Token Burn

A personal, **local, offline** dashboard of my Claude Code token usage over time — a feedback
loop for seeing and improving how I use AI (inspired by Nate B. Jones' "token burn dashboard",
but reading exact numbers from Claude Code's own logs instead of approximating).

It parses `~/.claude/projects/**/*.jsonl`, deduplicates by assistant `message.id`, and renders
four views from a single self-contained HTML file.

## Use

```bash
./build.sh           # parse ~/.claude logs, build, and open the dashboard
./build.sh --tz America/Los_Angeles   # bucket days in a different timezone
```

`build.sh` runs `parse.py` (writes `data.json`), inlines that JSON into `dashboard.html`, and
writes a fully self-contained `out/dashboard.html` you can open by double-click — no server, no
network, works offline.

### parse.py flags
- `--root` (default `~/.claude/projects`) — log directory to scan
- `--tz` (default: your local IANA timezone) — day-bucketing timezone
- `--out` (default `data.json`)

## The four views (all driven by the metric toggle)

- **Daily burn** — GitHub-style calendar heatmap, shaded by the selected metric.
- **Over time (log scale)** — tokens/day on a log axis (handles the few-million → ~billion range).
- **Top 10 days** — your biggest days *and what you were doing* (projects + session titles).
- **By model & by agent** — Opus/Sonnet/Haiku split, plus main-thread vs sub-agent (parallel) work.

**Metric toggle:** Total compute (`in+out+cache_create+cache_read`) · Generative (`in+out+cache_create`)
· Output only · Cost $ (API-equivalent). Metrics 1–3 are computed in the browser; cost is
precomputed in `parse.py`.

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
