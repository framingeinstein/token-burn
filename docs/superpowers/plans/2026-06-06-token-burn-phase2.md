# Token-Burn Phase 2 — Snapshots + Local Server — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make token-burn data durable past Claude Code's ~30-day log retention by freezing each completed day into an append-only archive, and serve that archive merged with a live-parsed "today" from a zero-dependency local server.

**Architecture:** A daily cron runs an idempotent, catch-up snapshotter that appends each completed day (`date < today`, local tz) to `snapshots.jsonl`. A shared core — `parse.day_records()` (logs → per-day rollups) and `ledger.assemble_rollup()` (archive + live today → `{meta, days}`) — feeds three thin entry points: the snapshotter, a stdlib HTTP server, and the offline `build.sh`. Cost is frozen per day at the date-effective rate from a researched `prices.json`.

**Tech Stack:** Python 3 standard library only (`json`, `glob`, `http.server`, `zoneinfo`, `argparse`, `datetime`); pytest for tests; vanilla JS + SVG dashboard (unchanged views).

**Spec:** `docs/superpowers/specs/2026-06-06-token-burn-phase2-design.md`

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `prices.json` | Create | Dated price table (model-id pattern × effective-date range + source). |
| `prices.py` | Create | `load_prices`, `rate_for(prices, model, date)` → effective rate or `None`. |
| `parse.py` | Modify | Date+price-aware `cost_usd`; `model_class` → `"other"` default; `dedupe_messages` occurrence count; `build_days`; `day_records`; `scan` becomes a wrapper. |
| `ledger.py` | Create | `read_ledger` (dedup latest-wins), `append_day`, `today_str`, `assemble_rollup`. |
| `snapshot.py` | Create | cron entry: finalize missing days `< today`; `--backfill`, `--refinalize`, `--since`. |
| `serve.py` | Create | stdlib HTTP server: `/`, `/api/data`, `/healthz`; `choose_port` auto-increment + report. |
| `dashboard.html` | Modify | `const`→`let DATA`; `typeof`-guarded `fetch('/api/data')` bootstrap. |
| `build.sh` | Modify | Build offline file from `assemble_rollup` (archive + live today). |
| `install-cron.sh` | Create | Install/print the daily crontab line. |
| `.gitignore` | Modify | Keep `data.json`/`out/` ignored; ensure `snapshots.jsonl` is **tracked**. |
| `README.md` | Modify | Document snapshots, server, cron, pricing. |
| `tests/test_prices.py` | Create | `rate_for` matching + date selection + unpriced. |
| `tests/test_parse.py` | Modify | Update to new signatures; add `build_days`/`day_records` tests. |
| `tests/test_ledger.py` | Create | `read_ledger`, `append_day`, `assemble_rollup`. |
| `tests/test_snapshot.py` | Create | idempotency, catch-up, today-never-finalized, refinalize. |
| `tests/test_serve.py` | Create | `choose_port`, `build_payload`. |

**Locked function signatures (use these exact names/shapes across all tasks):**

```
prices.load_prices(path=None) -> dict
prices.rate_for(prices, model, date) -> {"in","out","write_mult","read_mult"} | None
parse.model_class(model) -> "opus" | "sonnet" | "haiku" | "other"
parse.cost_usd(prices, model, date, inp, out, cc, cr) -> float
parse.dedupe_messages(raw_text) -> list[ {id,model,timestamp,cwd,is_sidechain,in,out,cc,cr,n} ]
parse.parse_session(raw_text, file_path, tz, prices) -> {label,project,is_subagent,messages[]}
parse.build_days(sessions, since=None, until=None) -> list[day_record]
parse.day_records(root, tz_name, since=None, until=None, prices=None) -> (list[day_record], meta)
parse.scan(root, tz_name) -> {"meta":..., "days":...}
ledger.read_ledger(path) -> {date: record}
ledger.append_day(path, record) -> None
ledger.today_str(tz_name) -> "YYYY-MM-DD"
ledger.assemble_rollup(ledger_path, root, tz_name, today, prices=None) -> {"meta":..., "days":...}
snapshot.run(root, tz_name, ledger_path, prices, today, refinalize=False, since=None) -> (added:int, days:list, meta:dict, existing:dict)
serve.choose_port(start, tries, is_free) -> int | None
serve.build_payload(cfg) -> dict
```

**`day_record` shape** (produced by `build_days`; a ledger line is this + `finalized_at`/`tz`/`rates_version`):

```jsonc
{ "date":"2026-05-21", "events":3, "msgs":2,
  "byModel": { "opus": {"in":0,"out":0,"cc":0,"cr":0,"cost_usd":0.0} },
  "mainVsSub": { "main": {...}, "sub": {...} },
  "topProjects": [ {"project":"...","total":0} ],
  "sampleSessions": [ {"label":"...","project":"...","total":0} ] }
```

---

## Task 1: Dated price table (`prices.json` + `prices.py`)

**Files:**
- Create: `prices.json`
- Create: `prices.py`
- Test: `tests/test_prices.py`

- [ ] **Step 1: Research and write `prices.json`**

Before writing values, confirm current Claude API rates using the **`claude-api` skill** (or `WebSearch` for "Anthropic API pricing Opus 4.8 Sonnet 4.6 Haiku 4.5 2026"). Confirm each of: Opus 4.7 **and** 4.8 = `$5/$25` in/out; Sonnet 4.6 = `$3/$15`; Haiku 4.5 = `$1/$5`; cache **write 1.25×**, cache **read 0.10×**; and that no rate changed between 2026-05-07 and 2026-06-06. Put the source you used in each entry's `source`. If research contradicts a number below, use the researched number instead.

```json
{
  "version": "2026-06-06",
  "default_cache_write_mult": 1.25,
  "default_cache_read_mult": 0.10,
  "rates": [
    { "match": "claude-opus-4-8*",   "in": 5.0, "out": 25.0, "from": "2026-01-01", "to": null, "source": "claude-api skill — Anthropic pricing 2026-06" },
    { "match": "claude-opus-4-7*",   "in": 5.0, "out": 25.0, "from": "2026-01-01", "to": null, "source": "claude-api skill / web — Opus 4.x = $5/$25" },
    { "match": "claude-opus-4-1*",   "in": 15.0, "out": 75.0, "from": "2024-01-01", "to": null, "source": "web — deprecated Opus 4.0/4.1 legacy rate" },
    { "match": "claude-opus-4-0*",   "in": 15.0, "out": 75.0, "from": "2024-01-01", "to": null, "source": "web — deprecated Opus 4.0/4.1 legacy rate" },
    { "match": "claude-sonnet-4-6*", "in": 3.0, "out": 15.0, "from": "2026-01-01", "to": null, "source": "claude-api skill" },
    { "match": "claude-haiku-4-5*",  "in": 1.0, "out": 5.0,  "from": "2026-01-01", "to": null, "source": "claude-api skill" }
  ],
  "unpriced": ["<synthetic>"]
}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_prices.py
from prices import load_prices, rate_for

def _table():
    return {
        "version": "test",
        "default_cache_write_mult": 1.25,
        "default_cache_read_mult": 0.10,
        "rates": [
            {"match": "claude-opus-4-8*", "in": 15.0, "out": 75.0, "from": "2024-01-01", "to": "2026-01-01", "source": "legacy"},
            {"match": "claude-opus-4-8*", "in": 5.0, "out": 25.0, "from": "2026-01-01", "to": None, "source": "current"},
            {"match": "claude-haiku-4-5*", "in": 1.0, "out": 5.0, "from": "2026-01-01", "to": None, "source": "x"},
        ],
        "unpriced": ["<synthetic>"],
    }

def test_rate_for_matches_prefix_and_current_date():
    r = rate_for(_table(), "claude-opus-4-8-20260101", "2026-05-21")
    assert r["in"] == 5.0 and r["out"] == 25.0
    assert r["write_mult"] == 1.25 and r["read_mult"] == 0.10

def test_rate_for_selects_by_date_range():
    r = rate_for(_table(), "claude-opus-4-8", "2025-06-01")  # legacy window
    assert r["in"] == 15.0 and r["out"] == 75.0

def test_rate_for_unknown_model_is_none():
    assert rate_for(_table(), "gpt-5", "2026-05-21") is None

def test_rate_for_synthetic_is_none():
    assert rate_for(_table(), "<synthetic>", "2026-05-21") is None

def test_load_prices_reads_repo_table():
    p = load_prices()
    assert "version" in p and isinstance(p["rates"], list)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_prices.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'prices'`.

- [ ] **Step 4: Write `prices.py`**

```python
"""Dated price table for the token-burn cost ($) metric.

Cost is FROZEN at capture: each day is priced at the rate effective on that day,
read from prices.json (version-stamped). Unknown / synthetic models return None
(priced at $0 and reported) so they are never silently mispriced.
"""
import json
from pathlib import Path

_DEFAULT_PATH = Path(__file__).parent / "prices.json"


def load_prices(path=None):
    with open(path or _DEFAULT_PATH) as fh:
        return json.load(fh)


def _matches(pattern, model):
    if pattern.endswith("*"):
        return model.startswith(pattern[:-1])
    return model == pattern


def rate_for(prices, model, date):
    """Effective rate dict for (model, date), or None if unpriced/unknown."""
    model = model or ""
    if model in prices.get("unpriced", []):
        return None
    wmult = prices.get("default_cache_write_mult", 1.25)
    rmult = prices.get("default_cache_read_mult", 0.10)
    for r in prices.get("rates", []):
        if not _matches(r["match"], model):
            continue
        frm, to = r.get("from"), r.get("to")
        if frm and date < frm:
            continue
        if to and date >= to:
            continue
        return {"in": r["in"], "out": r["out"],
                "write_mult": r.get("cache_write_mult", wmult),
                "read_mult": r.get("cache_read_mult", rmult)}
    return None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_prices.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add prices.py prices.json tests/test_prices.py
git commit -m "feat: dated price table (prices.json + rate_for)"
```

---

## Task 2: Date+price-aware cost in `parse.py`

**Files:**
- Modify: `parse.py` (`model_class`, `cost_usd`, `dedupe_messages`)
- Modify: `tests/test_parse.py` (imports + `model_class`/`cost_usd` tests)

- [ ] **Step 1: Update the failing tests**

Replace the import block (lines 1-8) and the two affected tests in `tests/test_parse.py`:

```python
# top of tests/test_parse.py
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from prices import load_prices
from parse import (
    dedupe_messages, model_class, cost_usd, session_label,
    project_of, is_subagent, local_day, parse_session, build_rollup, scan,
)

UTC = ZoneInfo("UTC")
PRICES = load_prices()
```

> Note: keep importing `build_rollup, scan` here — `build_days`/`day_records` don't exist until Task 3, which swaps this import line. In this task `parse_session` still calls the old `cost_usd`, so `test_parse_session` and `test_scan` will fail in a *full* run; that's expected and Task 3 restores them. Run only the `-k` subset below for this task.

Replace `test_model_class_maps_families` and `test_cost_usd_matches_deck_weights`:

```python
def test_model_class_maps_families():
    assert model_class("claude-opus-4-8") == "opus"
    assert model_class("claude-sonnet-4-6") == "sonnet"
    assert model_class("claude-haiku-4-5-20251001") == "haiku"
    assert model_class("<synthetic>") == "other"   # was mispriced as sonnet in Phase 1
    assert model_class(None) == "other"

def test_cost_usd_matches_deck_weights():
    d = "2026-05-21"
    assert round(cost_usd(PRICES, "claude-opus-4-8", d, 0, 0, 0, 1_000_000), 4) == 0.5
    assert round(cost_usd(PRICES, "claude-opus-4-8", d, 0, 1_000_000, 0, 0), 4) == 25.0
    assert round(cost_usd(PRICES, "claude-opus-4-8", d, 0, 0, 1_000_000, 0), 4) == 6.25

def test_cost_usd_unpriced_model_is_zero():
    assert cost_usd(PRICES, "<synthetic>", "2026-05-21", 1, 1, 1, 1) == 0.0

def test_dedupe_messages_counts_occurrences():
    raw = "\n".join([
        json.dumps({"type":"assistant","uuid":"a1","timestamp":"2026-05-20T10:00:00Z",
                    "message":{"id":"msg_A","model":"claude-opus-4-8","content":[{"type":"text"}],
                               "usage":{"input_tokens":1,"output_tokens":5}}}),
        json.dumps({"type":"assistant","uuid":"a2","timestamp":"2026-05-20T10:00:00Z",
                    "message":{"id":"msg_A","model":"claude-opus-4-8","content":[{"type":"tool_use"}],
                               "usage":{"input_tokens":1,"output_tokens":5}}}),
    ])
    recs = dedupe_messages(raw)
    assert len(recs) == 1 and recs[0]["n"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_parse.py -k "model_class or cost_usd or dedupe_messages_counts" -v`
Expected: FAIL (`cost_usd` signature mismatch / `model_class` returns "sonnet" / no `n` key).

- [ ] **Step 3: Edit `parse.py`**

Replace the `PRICE` dict and `model_class`/`cost_usd` (lines ~18-76) with:

```python
from prices import load_prices, rate_for


def model_class(model):
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "other"  # unknown/synthetic — no longer mispriced as sonnet


def cost_usd(prices, model, date, inp, out, cc, cr):
    r = rate_for(prices, model, date)
    if r is None:
        return 0.0
    return (inp * r["in"] + out * r["out"]
            + cc * r["in"] * r["write_mult"] + cr * r["in"] * r["read_mult"]) / 1e6
```

In `dedupe_messages`, track occurrences. After building `rec` and before the `prev = best.get(key)` block, change the tail to:

```python
        prev = best.get(key)
        if prev is None:
            rec["n"] = 1
            best[key] = rec
        else:
            rec["n"] = prev["n"] + 1
            if rec["out"] >= prev["out"]:
                best[key] = rec          # keep max-output occurrence
            else:
                prev["n"] = rec["n"]     # but always carry the running count
    return list(best.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_parse.py -k "model_class or cost_usd or dedupe" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add parse.py tests/test_parse.py
git commit -m "feat: date+price-aware cost_usd; model_class other default; dedupe occurrence count"
```

---

## Task 3: `day_records` + `build_days` (date-filtered rollups)

**Files:**
- Modify: `parse.py` (`parse_session`, replace `build_rollup`+`scan` internals, add `build_days`/`day_records`)
- Modify: `tests/test_parse.py` (`parse_session`, build/scan tests)

- [ ] **Step 1: Update the failing tests**

First update the `parse` import line in `tests/test_parse.py` to swap `build_rollup` → `build_days, day_records` (drop `build_rollup`, which is replaced this task):

```python
from parse import (
    dedupe_messages, model_class, cost_usd, session_label,
    project_of, is_subagent, local_day, parse_session, build_days, day_records, scan,
)
```

Then in `tests/test_parse.py`, replace `test_parse_session_aggregates_with_label_project_subagent`, the `_sess` helper + `test_build_rollup_shape_and_sanity`, and `test_scan_fixture_dir_dedupes_and_flags_subagent`:

```python
def test_parse_session_aggregates_with_label_project_subagent():
    raw = "\n".join([
        json.dumps({"type":"summary","aiTitle":"Fix the parser"}),
        json.dumps({"type":"user","isMeta":False,"message":{"role":"user","content":"please fix the parser"}}),
        json.dumps({"type":"assistant","uuid":"a1","timestamp":"2026-05-21T19:00:00.000Z",
                    "cwd":"/Users/j/Documents/projects/FramingEinstein",
                    "message":{"id":"msg_A","model":"claude-opus-4-8","content":[{"type":"text"}],
                               "usage":{"input_tokens":1,"output_tokens":100,
                                        "cache_creation_input_tokens":2000,"cache_read_input_tokens":5000}}}),
        json.dumps({"type":"assistant","uuid":"a2","timestamp":"2026-05-21T19:00:00.000Z",
                    "cwd":"/Users/j/Documents/projects/FramingEinstein",
                    "message":{"id":"msg_A","model":"claude-opus-4-8","content":[{"type":"tool_use"}],
                               "usage":{"input_tokens":1,"output_tokens":100,
                                        "cache_creation_input_tokens":2000,"cache_read_input_tokens":5000}}}),
    ])
    s = parse_session(raw, "/x/session.jsonl", UTC, PRICES)
    assert s["label"] == "Fix the parser" and s["project"] == "FramingEinstein"
    assert s["is_subagent"] is False and len(s["messages"]) == 1
    m = s["messages"][0]
    assert m["day"] == "2026-05-21" and m["mclass"] == "opus" and m["n"] == 2
    assert round(m["cost"], 6) == round(cost_usd(PRICES, "claude-opus-4-8", "2026-05-21", 1, 100, 2000, 5000), 6)


def _sess(project, sub, label, day, mclass, comps, cost, n=1):
    i, o, cc, cr = comps
    return {"label": label, "project": project, "is_subagent": sub,
            "messages": [{"day": day, "model": mclass, "mclass": mclass, "in": i, "out": o,
                          "cc": cc, "cr": cr, "cost": cost, "n": n}]}


def test_build_days_shape_and_counts():
    sessions = [
        _sess("FramingEinstein", False, "main work", "2026-05-21", "opus", (1,100,2000,5000), 0.20, n=2),
        _sess("FramingEinstein", True, "subagent work", "2026-05-21", "opus", (0,50,1000,4000), 0.10, n=1),
    ]
    days = build_days(sessions)
    day = next(d for d in days if d["date"] == "2026-05-21")
    assert day["events"] == 3 and day["msgs"] == 2
    bm = day["byModel"]["opus"]
    assert bm["out"] == 150 and bm["cr"] == 9000
    assert round(bm["cost_usd"], 4) == 0.30
    assert round(day["mainVsSub"]["main"]["out"]) == 100
    assert round(day["mainVsSub"]["sub"]["out"]) == 50
    assert day["topProjects"][0]["project"] == "FramingEinstein"
    assert any(s["label"] == "subagent work" for s in day["sampleSessions"])


def test_build_days_filters_by_range():
    sessions = [
        _sess("p", False, "old", "2026-05-01", "opus", (0,1,0,0), 0.0),
        _sess("p", False, "mid", "2026-05-15", "opus", (0,1,0,0), 0.0),
        _sess("p", False, "new", "2026-05-31", "opus", (0,1,0,0), 0.0),
    ]
    dates = [d["date"] for d in build_days(sessions, since="2026-05-10", until="2026-05-20")]
    assert dates == ["2026-05-15"]


def test_day_records_fixture_dir_dedupes_and_flags_subagent():
    root = Path(__file__).parent / "fixtures"
    days, meta = day_records(root, "UTC", prices=PRICES)
    assert meta["assistant_events"] == 3 and meta["unique_messages"] == 2
    day = next(d for d in days if d["date"] == "2026-05-21")
    assert "opus" in day["byModel"] and "haiku" in day["byModel"]
    assert day["byModel"]["opus"]["out"] == 100
    assert day["mainVsSub"]["sub"]["out"] == 50
    assert day["events"] == 3 and day["msgs"] == 2


def test_scan_wrapper_still_returns_meta_and_days():
    root = Path(__file__).parent / "fixtures"
    roll = scan(root, "UTC")
    assert roll["meta"]["unique_messages"] == 2 and len(roll["days"]) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_parse.py -k "parse_session or build_days or day_records or scan_wrapper" -v`
Expected: FAIL (`build_days`/`day_records` not defined; `parse_session` signature mismatch).

- [ ] **Step 3: Edit `parse.py`**

Update `parse_session` to take `prices` and carry `model`/`n`/date-priced cost:

```python
def parse_session(raw_text, file_path, tz, prices):
    recs = dedupe_messages(raw_text)
    any_side = any(r["is_sidechain"] for r in recs)
    cwd = next((r["cwd"] for r in recs if r.get("cwd")), None)
    messages = []
    for r in recs:
        day = local_day(r["timestamp"], tz)
        messages.append({
            "day": day,
            "model": r["model"],
            "mclass": model_class(r["model"]),
            "in": r["in"], "out": r["out"], "cc": r["cc"], "cr": r["cr"],
            "n": r.get("n", 1),
            "cost": cost_usd(prices, r["model"], day or "", r["in"], r["out"], r["cc"], r["cr"]),
        })
    return {
        "label": session_label(raw_text),
        "project": project_of(cwd),
        "is_subagent": is_subagent(file_path, any_side),
        "messages": messages,
    }
```

Replace `build_rollup` and `scan` (and keep `_zero`/`_add`) with `build_days`, `day_records`, and a thin `scan`:

```python
def build_days(sessions, since=None, until=None):
    """Pure aggregation of parsed sessions into per-day records (no I/O)."""
    days = collections.defaultdict(lambda: {
        "events": 0, "msgs": 0,
        "byModel": collections.defaultdict(_zero),
        "mainVsSub": {"main": _zero(), "sub": _zero()},
        "projects": collections.Counter(),
        "sessions": [],
    })
    for s in sessions:
        for m in s["messages"]:
            d = m["day"]
            if d is None or (since and d < since) or (until and d > until):
                continue
            bucket = days[d]
            bucket["events"] += m.get("n", 1)
            bucket["msgs"] += 1
            _add(bucket["byModel"][m["mclass"]], m)
            _add(bucket["mainVsSub"]["sub" if s["is_subagent"] else "main"], m)
            tok = m["in"] + m["out"] + m["cc"] + m["cr"]
            bucket["projects"][s["project"]] += tok
            bucket["sessions"].append((s["label"], s["project"], tok))

    out = []
    for date in sorted(days):
        b = days[date]
        sample = sorted(b["sessions"], key=lambda x: -x[2])[:5]
        out.append({
            "date": date,
            "events": b["events"],
            "msgs": b["msgs"],
            "byModel": {k: dict(v) for k, v in b["byModel"].items()},
            "mainVsSub": b["mainVsSub"],
            "topProjects": [{"project": p, "total": t} for p, t in b["projects"].most_common(5)],
            "sampleSessions": [{"label": l, "project": p, "total": t} for l, p, t in sample],
        })
    return out


def _totals(days):
    t = _zero()
    for d in days:
        for comp in d.get("byModel", {}).values():
            for k in ("in", "out", "cc", "cr"):
                t[k] += comp.get(k, 0)
            t["cost_usd"] += comp.get("cost_usd", 0.0)
    return t


def day_records(root, tz_name, since=None, until=None, prices=None):
    """Glob logs, parse, and return (per-day records in [since,until], meta)."""
    if prices is None:
        prices = load_prices()
    tz = ZoneInfo(tz_name)
    sessions = []
    unpriced = set()
    for f in glob.glob(os.path.join(str(root), "**", "*.jsonl"), recursive=True):
        try:
            raw = open(f, "r", errors="ignore").read()
        except Exception:
            continue
        s = parse_session(raw, f, tz, prices)
        for m in s["messages"]:
            if m["model"] and m["day"] and rate_for(prices, m["model"], m["day"]) is None:
                unpriced.add(m["model"])
        sessions.append(s)
    days = build_days(sessions, since, until)
    meta = {
        "tz": tz_name,
        "assistant_events": sum(d["events"] for d in days),
        "unique_messages": sum(d["msgs"] for d in days),
        "totals": _totals(days),
        "unpriced_models": sorted(unpriced),
    }
    return days, meta


def scan(root, tz_name):
    days, meta = day_records(root, tz_name)
    return {"meta": meta, "days": days}
```

Update `main()` to use the new shape (it still writes a whole-corpus `data.json`):

```python
def main():
    ap = argparse.ArgumentParser(description="Whole-corpus token-burn dump (ad-hoc).")
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--tz", default=_default_tz_name())
    ap.add_argument("--out", default=str(Path(__file__).parent / "data.json"))
    args = ap.parse_args()
    roll = scan(args.root, args.tz)
    with open(args.out, "w") as fh:
        json.dump(roll, fh)
    m = roll["meta"]
    print(f"wrote {args.out}: {len(roll['days'])} days, tz={m['tz']}, "
          f"{m['assistant_events']} events -> {m['unique_messages']} unique msgs, "
          f"${m['totals']['cost_usd']:,.0f} total")
    if m["unpriced_models"]:
        print(f"  unpriced models (cost=0): {m['unpriced_models']}")
```

- [ ] **Step 4: Run the full parser test file**

Run: `python3 -m pytest tests/test_parse.py -v`
Expected: PASS (all tests, including the unchanged dedupe/label/project/local_day ones).

- [ ] **Step 5: Commit**

```bash
git add parse.py tests/test_parse.py
git commit -m "feat: build_days + day_records (date-filtered rollups, per-day counts); scan wrapper"
```

---

## Task 4: Ledger read/append (`ledger.py`)

**Files:**
- Create: `ledger.py`
- Test: `tests/test_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger.py
import json
from ledger import read_ledger, append_day, today_str

def test_append_and_read_roundtrip(tmp_path):
    p = tmp_path / "snap.jsonl"
    append_day(str(p), {"date": "2026-05-21", "msgs": 2})
    append_day(str(p), {"date": "2026-05-22", "msgs": 3})
    led = read_ledger(str(p))
    assert set(led) == {"2026-05-21", "2026-05-22"}
    assert led["2026-05-22"]["msgs"] == 3

def test_read_ledger_latest_wins_for_duplicate_date(tmp_path):
    p = tmp_path / "snap.jsonl"
    append_day(str(p), {"date": "2026-05-21", "rates_version": "old"})
    append_day(str(p), {"date": "2026-05-21", "rates_version": "new"})
    assert read_ledger(str(p))["2026-05-21"]["rates_version"] == "new"

def test_read_ledger_skips_corrupt_lines(tmp_path):
    p = tmp_path / "snap.jsonl"
    p.write_text('{"date":"2026-05-21","msgs":1}\nnot json\n{"date":"2026-05-22"\n')
    led = read_ledger(str(p))
    assert set(led) == {"2026-05-21"}

def test_read_ledger_missing_file_is_empty(tmp_path):
    assert read_ledger(str(tmp_path / "nope.jsonl")) == {}

def test_today_str_is_iso_date():
    assert len(today_str("UTC")) == 10 and today_str("UTC")[4] == "-"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ledger.py -v`
Expected: FAIL (`No module named 'ledger'`).

- [ ] **Step 3: Write `ledger.py` (read/append portion)**

```python
"""Append-only daily snapshot archive (snapshots.jsonl) + assembly.

Archive = permanent, authoritative record. Finalized days (date < today) are
immutable; only 'today' is parsed live. One JSON object per line; on read, the
LAST line per date wins (so --refinalize just appends a newer line).
"""
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from parse import day_records, _totals
from prices import load_prices


def read_ledger(path):
    """Return {date: record}, keeping the last line per date (latest wins)."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue  # skip corrupt / half-written line
            d = rec.get("date")
            if d:
                out[d] = rec
    return out


def append_day(path, record):
    """Append one record as a single line (atomic single write)."""
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def today_str(tz_name):
    return datetime.now(ZoneInfo(tz_name)).date().isoformat()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ledger.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add ledger.py tests/test_ledger.py
git commit -m "feat: ledger read/append (latest-wins, corrupt-skip) + today_str"
```

---

## Task 5: `assemble_rollup` (archive + live today)

**Files:**
- Modify: `ledger.py` (add `assemble_rollup`)
- Modify: `tests/test_ledger.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ledger.py`:

```python
from pathlib import Path
from prices import load_prices

def test_assemble_merges_ledger_and_live_today(tmp_path):
    # ledger has a finalized past day; logs (fixtures) are all dated 2026-05-21
    p = tmp_path / "snap.jsonl"
    append_day(str(p), {"date": "2026-05-10", "events": 9, "msgs": 4,
                        "byModel": {"opus": {"in": 0, "out": 7, "cc": 0, "cr": 0, "cost_usd": 1.0}},
                        "mainVsSub": {"main": {"in":0,"out":7,"cc":0,"cr":0,"cost_usd":1.0},
                                      "sub": {"in":0,"out":0,"cc":0,"cr":0,"cost_usd":0.0}},
                        "topProjects": [], "sampleSessions": []})
    fixtures = Path(__file__).parent / "fixtures"
    roll = assemble_rollup(str(p), fixtures, "UTC", today="2026-05-21", prices=load_prices())
    dates = [d["date"] for d in roll["days"]]
    assert "2026-05-10" in dates           # from ledger
    assert "2026-05-21" in dates           # live today (fixtures)
    assert roll["meta"]["unique_messages"] == 4 + 2   # ledger msgs + live msgs
    assert roll["meta"]["rates_version"] == load_prices()["version"]

def test_assemble_ledger_wins_over_lingering_logs(tmp_path):
    # a ledger entry dated 2026-05-21 (== fixtures day) but with date < today
    # must be used verbatim; live parse only runs for `today`, so no double-count.
    p = tmp_path / "snap.jsonl"
    append_day(str(p), {"date": "2026-05-21", "events": 1, "msgs": 1,
                        "byModel": {"opus": {"in":0,"out":999,"cc":0,"cr":0,"cost_usd":0.0}},
                        "mainVsSub": {"main": {"in":0,"out":999,"cc":0,"cr":0,"cost_usd":0.0},
                                      "sub": {"in":0,"out":0,"cc":0,"cr":0,"cost_usd":0.0}},
                        "topProjects": [], "sampleSessions": []})
    fixtures = Path(__file__).parent / "fixtures"
    roll = assemble_rollup(str(p), fixtures, "UTC", today="2026-06-01", prices=load_prices())
    day = next(d for d in roll["days"] if d["date"] == "2026-05-21")
    assert day["byModel"]["opus"]["out"] == 999   # ledger value, NOT re-parsed 100

def test_assemble_empty_ledger_is_today_only(tmp_path):
    fixtures = Path(__file__).parent / "fixtures"
    roll = assemble_rollup(str(tmp_path / "none.jsonl"), fixtures, "UTC", today="2026-05-21", prices=load_prices())
    assert [d["date"] for d in roll["days"]] == ["2026-05-21"]
```

Add the import at the top of the test file: `from ledger import read_ledger, append_day, today_str, assemble_rollup`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ledger.py -k assemble -v`
Expected: FAIL (`assemble_rollup` not defined).

- [ ] **Step 3: Add `assemble_rollup` to `ledger.py`**

```python
def assemble_rollup(ledger_path, root, tz_name, today, prices=None):
    """Merge finalized archive days (date < today, authoritative) with a live
    parse of `today`. Returns the dashboard payload {meta, days}."""
    if prices is None:
        prices = load_prices()
    ledger = read_ledger(ledger_path)
    days_by_date = {d: rec for d, rec in ledger.items() if d < today}   # immutable past
    live_days, live_meta = day_records(root, tz_name, since=today, until=today, prices=prices)
    for rec in live_days:
        days_by_date[rec["date"]] = rec                                  # today, live
    days = [days_by_date[d] for d in sorted(days_by_date)]
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tz": tz_name,
        "rates_version": prices.get("version"),
        "assistant_events": sum(d.get("events", 0) for d in days),
        "unique_messages": sum(d.get("msgs", 0) for d in days),
        "totals": _totals(days),
        "unpriced_models": live_meta.get("unpriced_models", []),
    }
    return {"meta": meta, "days": days}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ledger.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add ledger.py tests/test_ledger.py
git commit -m "feat: assemble_rollup — archive + live-today merge, ledger wins"
```

---

## Task 6: Snapshotter (`snapshot.py`)

**Files:**
- Create: `snapshot.py`
- Test: `tests/test_snapshot.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_snapshot.py
from pathlib import Path
from prices import load_prices
from ledger import read_ledger
from snapshot import run, _prev_day

FIX = Path(__file__).parent / "fixtures"   # all logs dated 2026-05-21 (UTC)
P = load_prices()

def test_prev_day():
    assert _prev_day("2026-06-01") == "2026-05-31"
    assert _prev_day("2026-05-01") == "2026-04-30"

def test_finalizes_completed_day_not_today(tmp_path):
    led = str(tmp_path / "s.jsonl")
    # today = 2026-05-22 → 2026-05-21 is complete and should be frozen
    added, days, meta, existing = run(FIX, "UTC", led, P, today="2026-05-22")
    assert added == 1
    rec = read_ledger(led)["2026-05-21"]
    assert rec["msgs"] == 2 and rec["rates_version"] == P["version"]
    assert "finalized_at" in rec and rec["tz"] == "UTC"

def test_today_is_never_finalized(tmp_path):
    led = str(tmp_path / "s.jsonl")
    # today == the fixtures' day → nothing to finalize
    added, *_ = run(FIX, "UTC", led, P, today="2026-05-21")
    assert added == 0 and read_ledger(led) == {}

def test_idempotent_second_run_adds_zero(tmp_path):
    led = str(tmp_path / "s.jsonl")
    run(FIX, "UTC", led, P, today="2026-05-22")
    added, *_ = run(FIX, "UTC", led, P, today="2026-05-22")
    assert added == 0

def test_refinalize_reappends_and_latest_wins(tmp_path):
    led = str(tmp_path / "s.jsonl")
    run(FIX, "UTC", led, P, today="2026-05-22")
    added, *_ = run(FIX, "UTC", led, P, today="2026-05-22", refinalize=True)
    assert added == 1
    # still one logical day after dedup-on-read
    assert set(read_ledger(led)) == {"2026-05-21"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_snapshot.py -v`
Expected: FAIL (`No module named 'snapshot'`).

- [ ] **Step 3: Write `snapshot.py`**

```python
"""Daily snapshot capture (cron entry). Idempotent + catch-up.

Finalizes every COMPLETE day (date < today, local tz) not already in the ledger.
  snapshot.py                       # daily: finalize missing complete days
  snapshot.py --backfill            # same engine, full log span (first-run seed)
  snapshot.py --refinalize --since 2026-05-20   # re-freeze a range (rate fix)
"""
import argparse
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from parse import day_records, _default_tz_name
from prices import load_prices
from ledger import read_ledger, append_day, today_str


def _prev_day(today):
    y, m, d = map(int, today.split("-"))
    return (date(y, m, d) - timedelta(days=1)).isoformat()


def run(root, tz_name, ledger_path, prices, today, refinalize=False, since=None):
    existing = read_ledger(ledger_path)
    days, meta = day_records(root, tz_name, since=since, until=_prev_day(today), prices=prices)
    added = 0
    for rec in days:
        d = rec["date"]
        if d >= today:
            continue                              # never finalize today (defensive)
        if not refinalize and d in existing:
            continue                              # idempotent
        line = dict(rec)
        line["finalized_at"] = datetime.now(timezone.utc).isoformat()
        line["tz"] = tz_name
        line["rates_version"] = prices.get("version")
        append_day(ledger_path, line)
        added += 1
    return added, days, meta, existing


def main():
    ap = argparse.ArgumentParser(description="Freeze completed days into snapshots.jsonl.")
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--tz", default=_default_tz_name())
    ap.add_argument("--ledger", default=str(Path(__file__).parent / "snapshots.jsonl"))
    ap.add_argument("--backfill", action="store_true",
                    help="finalize all complete days present in logs (first-run seed)")
    ap.add_argument("--refinalize", action="store_true",
                    help="re-append fresh lines (latest wins on read) for a rate/parser fix")
    ap.add_argument("--since", default=None, help="floor date YYYY-MM-DD for --refinalize/backfill")
    args = ap.parse_args()

    prices = load_prices()
    today = today_str(args.tz)
    since = args.since  # None => full span (backfill is the default catch-up behavior)
    added, days, meta, existing = run(args.root, args.tz, args.ledger, prices, today,
                                      refinalize=args.refinalize, since=since)
    led = read_ledger(args.ledger)
    print(f"snapshot: +{added} day(s); ledger now {len(led)} day(s); tz={args.tz}; "
          f"rates={prices.get('version')}")
    if meta.get("unpriced_models"):
        print(f"  unpriced models (cost=0): {meta['unpriced_models']}")
    # retention guard: warn if logs may have rolled off before first capture
    if existing and days:
        oldest_log = days[0]["date"]
        newest_led = max(existing)
        if oldest_log > _next_day(newest_led):
            print(f"  WARNING: gap — oldest log {oldest_log} is past ledger max {newest_led}; "
                  f"days between may have rolled off uncaptured.")


def _next_day(d):
    y, m, dd = map(int, d.split("-"))
    return (date(y, m, dd) + timedelta(days=1)).isoformat()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_snapshot.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add snapshot.py tests/test_snapshot.py
git commit -m "feat: snapshotter — finalize completed days, idempotent + catch-up + refinalize"
```

---

## Task 7: Local server (`serve.py`)

**Files:**
- Create: `serve.py`
- Test: `tests/test_serve.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_serve.py
from pathlib import Path
from serve import choose_port, build_payload

def test_choose_port_returns_start_when_free():
    assert choose_port(8799, 20, is_free=lambda p: True) == 8799

def test_choose_port_increments_past_busy_ports():
    busy = {8799, 8800}
    assert choose_port(8799, 20, is_free=lambda p: p not in busy) == 8801

def test_choose_port_none_when_all_busy():
    assert choose_port(8799, 3, is_free=lambda p: False) is None

def test_build_payload_assembles_from_ledger_and_logs(tmp_path):
    fixtures = Path(__file__).parent / "fixtures"
    cfg = {"ledger": str(tmp_path / "none.jsonl"), "root": fixtures,
           "tz": "UTC", "today": "2026-05-21"}
    payload = build_payload(cfg)
    assert "meta" in payload and "days" in payload
    assert payload["days"][0]["date"] == "2026-05-21"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_serve.py -v`
Expected: FAIL (`No module named 'serve'`).

- [ ] **Step 3: Write `serve.py`**

```python
"""Zero-dependency local server for the token-burn dashboard.

  /          -> dashboard.html (served so it fetches data live)
  /api/data  -> assemble_rollup(archive + live today) as JSON
  /healthz   -> ok

Binds --port (default 8799); if taken, increments to the next free port and
prints the actual URL.
"""
import argparse
import json
import os
import socket
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from parse import _default_tz_name
from prices import load_prices
from ledger import assemble_rollup, today_str

HERE = Path(__file__).parent


def build_payload(cfg):
    today = cfg.get("today") or today_str(cfg["tz"])
    return assemble_rollup(cfg["ledger"], cfg["root"], cfg["tz"], today, prices=load_prices())


def choose_port(start, tries, is_free):
    for p in range(start, start + tries):
        if is_free(p):
            return p
    return None


def _port_free(p):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *a, cfg=None, **k):
        self.cfg = cfg
        super().__init__(*a, **k)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/data"):
            body = json.dumps(build_payload(self.cfg)).encode()
            self._send(200, body, "application/json")
        elif self.path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description="Serve the token-burn dashboard locally.")
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--tz", default=_default_tz_name())
    ap.add_argument("--ledger", default=str(HERE / "snapshots.jsonl"))
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()

    cfg = {"root": args.root, "tz": args.tz, "ledger": args.ledger, "today": None}
    port = choose_port(args.port, 20, _port_free)
    if port is None:
        raise SystemExit(f"no free port in {args.port}..{args.port + 19}")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, cfg=cfg))
    print(f"serving token-burn at http://127.0.0.1:{port}  (ledger: {args.ledger}, tz: {args.tz})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_serve.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add serve.py tests/test_serve.py
git commit -m "feat: local server (/ + /api/data + /healthz) with port auto-increment"
```

---

## Task 8: Dashboard fetch-vs-inline bootstrap

**Files:**
- Modify: `dashboard.html` (line 64 + the init at line 213)

- [ ] **Step 1: Change the DATA declaration (line 64)**

From:
```javascript
const DATA = "__DATA_PLACEHOLDER__";
```
To:
```javascript
let DATA = "__DATA_PLACEHOLDER__";
```

- [ ] **Step 2: Replace the init kickoff (line 213)**

From:
```javascript
if (DATA && DATA.days) rerender();
```
To:
```javascript
async function boot(){
  if (typeof DATA === "string") {            // not injected by build.sh → live server mode
    DATA = await (await fetch("/api/data")).json();
  }
  if (DATA && DATA.days) rerender();
}
boot();
```

> Why `typeof DATA === "string"`: `build.sh` replaces the single literal `"__DATA_PLACEHOLDER__"` with a JS object (offline file → `DATA` is an object → render directly). When served raw by `serve.py`, the literal remains a string → fetch `/api/data`. Using `typeof` (not an equality check on the literal) keeps the placeholder to a single occurrence so the replace is unambiguous.

- [ ] **Step 3: Manual verify (served mode)**

After Task 11 seeds the ledger, run `python3 serve.py` and confirm the printed URL loads with populated views and the header shows the deduped-message line. (No automated test — view code is verified by eye per the spec.)

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "feat: dashboard auto-detects inlined data vs live /api/data fetch"
```

---

## Task 9: Offline build via `assemble_rollup` (`build.sh`)

**Files:**
- Modify: `build.sh`

- [ ] **Step 1: Replace `build.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 - "$@" <<'PY'
import argparse, json, os, pathlib
from parse import _default_tz_name
from prices import load_prices
from ledger import assemble_rollup, today_str

ap = argparse.ArgumentParser()
ap.add_argument("--tz", default=None)
ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
ap.add_argument("--ledger", default=str(pathlib.Path(__file__).resolve().parent / "snapshots.jsonl"))
args, _ = ap.parse_known_args()

tz = args.tz or _default_tz_name()
data = assemble_rollup(args.ledger, args.root, tz, today_str(tz), prices=load_prices())
json.dump(data, open("data.json", "w"))

tpl = open("dashboard.html").read()
html = tpl.replace('"__DATA_PLACEHOLDER__"', json.dumps(data))
pathlib.Path("out").mkdir(exist_ok=True)
open("out/dashboard.html", "w").write(html)
print("wrote out/dashboard.html (%.0f KB), %d days, $%.0f" %
      (len(html)/1024, len(data["days"]), data["meta"]["totals"]["cost_usd"]))
PY
open out/dashboard.html 2>/dev/null || echo "open out/dashboard.html"
```

- [ ] **Step 2: Manual verify (after Task 11 seeds the ledger)**

Run: `./build.sh`
Expected: prints `wrote out/dashboard.html (... KB), N days, $...`; opening the file renders the dashboard offline (DATA inlined, no fetch).

- [ ] **Step 3: Commit**

```bash
git add build.sh
git commit -m "feat: offline build assembled from archive + live today"
```

---

## Task 10: cron installer, gitignore, README

**Files:**
- Create: `install-cron.sh`
- Modify: `.gitignore`
- Modify: `README.md`

- [ ] **Step 1: Write `install-cron.sh`**

```bash
#!/usr/bin/env bash
# Install a daily token-burn snapshot cron entry (idempotent — replaces any prior one).
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$(command -v python3)"
LINE="0 9 * * * cd '$DIR' && '$PY' snapshot.py >> '$DIR/snapshot.log' 2>&1"
( crontab -l 2>/dev/null | grep -v "token-burn snapshot\|/snapshot.py" ; \
  echo "# token-burn snapshot" ; echo "$LINE" ) | crontab -
echo "installed daily cron (09:00 local):"
echo "  $LINE"
echo "view with: crontab -l   |   logs: $DIR/snapshot.log"
```

- [ ] **Step 2: Make scripts executable**

Run: `chmod +x install-cron.sh build.sh`

- [ ] **Step 3: Update `.gitignore`**

Ensure it contains (the ledger must NOT be ignored):

```
data.json
out/
snapshot.log
__pycache__/
.pytest_cache/
```

Then explicitly confirm tracking intent: `git check-ignore snapshots.jsonl` must print nothing (exit 1).

- [ ] **Step 4: Update `README.md`**

Add a "Phase 2 — durable snapshots + server" section documenting:
- `python3 snapshot.py --backfill` (one-time seed) and `./install-cron.sh` (daily capture at 09:00).
- `python3 serve.py` → live dashboard at the printed `http://127.0.0.1:<port>` (auto-increments if busy).
- `./build.sh` → offline self-contained `out/dashboard.html`.
- `snapshots.jsonl` is the committed archive; `prices.json` is the dated price table; cost is frozen at capture (`--refinalize` to re-price after a rate correction).

- [ ] **Step 5: Commit**

```bash
git add install-cron.sh .gitignore README.md
git commit -m "feat: cron installer, gitignore (track ledger), README Phase 2 docs"
```

---

## Task 11: Seed the archive + end-to-end verification

**Files:**
- Create (data): `snapshots.jsonl` (committed)

- [ ] **Step 1: Confirm the full test suite is green**

Run: `python3 -m pytest tests/ -v`
Expected: PASS (all of prices, parse, ledger, snapshot, serve).

- [ ] **Step 2: Backfill the archive from the real logs**

Run: `python3 snapshot.py --backfill`
Expected: prints `+N day(s)` where N ≈ 30 (2026-05-07 → yesterday); lists any `unpriced models` (expect `['<synthetic>']`). Today is excluded.

- [ ] **Step 3: Sanity-check the seeded ledger**

Run:
```bash
wc -l snapshots.jsonl
python3 -c "from ledger import read_ledger; l=read_ledger('snapshots.jsonl'); import builtins; print('days', len(l), 'span', min(l), '->', max(l))"
```
Expected: ~30 lines; span starts `2026-05-07`, ends yesterday (not today).

- [ ] **Step 4: Verify the server live-merges today**

Run `python3 serve.py` (note the printed port), then in another shell:
```bash
curl -s http://127.0.0.1:<port>/healthz          # -> ok
curl -s http://127.0.0.1:<port>/api/data | python3 -c "import sys,json; d=json.load(sys.stdin); print('days',len(d['days']),'last',d['days'][-1]['date'],'$%.0f'%d['meta']['totals']['cost_usd'],'unpriced',d['meta']['unpriced_models'])"
```
Expected: `days` ≈ 31, `last` == today's date (live slice present), a plausible total. Stop the server (Ctrl-C).

- [ ] **Step 5: Verify offline build**

Run: `./build.sh`
Expected: writes `out/dashboard.html`; opening it renders all four views + metric toggle from inlined data (works offline, no server).

- [ ] **Step 6: Commit the seeded archive**

```bash
git add snapshots.jsonl
git commit -m "data: seed snapshots.jsonl archive (backfill 2026-05-07 onward)"
```

- [ ] **Step 7: Finish the branch**

Use the superpowers:finishing-a-development-branch skill to merge `phase-2-snapshots-server` → `main`.

---

## Self-Review (completed during plan authoring)

**Spec coverage:** D1 Archive (Tasks 4–6) · D2 JSONL latest-wins (Task 4) · D3 cron idempotent+catch-up (Tasks 6, 10) · D4 live server + offline file (Tasks 7, 9) · D5 frozen cost + refinalize (Tasks 1, 6) · D6 port auto-increment+report (Task 7) · D7 commit ledger (Tasks 10, 11) · D8 backfill + dated/researched pricing (Tasks 1, 11). Edge cases: corrupt line (Task 4), ledger-wins-no-double-count (Task 5), today-never-finalized (Task 6), unpriced→$0+flagged (Tasks 2, 3). Dashboard `meta.unique_messages`/`assistant_events` preserved via per-day `events`/`msgs` (Tasks 3, 5).

**Placeholder scan:** none — every step has runnable code/commands. `prices.json` carries concrete numbers to be *verified* by research (Task 1 Step 1), not blanks.

**Type consistency:** signatures match the locked table above across tasks (`cost_usd(prices, model, date, …)`, `day_records(...)→(days, meta)`, `assemble_rollup(...)→{meta,days}`, `run(...)→(added, days, meta, existing)`, `choose_port(start, tries, is_free)`). `day_record` shape (with `events`/`msgs`) is consistent between `build_days`, the ledger line, and `assemble_rollup`'s meta sums.
