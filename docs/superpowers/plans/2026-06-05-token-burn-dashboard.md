# Token-Burn Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A personal, local, offline dashboard that parses Claude Code session logs and renders four Tufte-style SVG views (calendar heatmap, log-scale time series, top-10 days, model distribution) of token usage, with a metric toggle (total / generative / output / cost-$).

**Architecture:** A Python-stdlib parser (`parse.py`) walks `~/.claude/projects/**/*.jsonl`, deduplicates by assistant `message.id` (Claude Code logs one line per content block — naive summing inflates ~2×), buckets by local-timezone day × model × component, precomputes per-model `cost_usd`, and emits `data.json`. `build.sh` inlines that JSON into the self-contained `dashboard.html` template (pure SVG + vanilla JS, no network) and opens the result.

**Tech Stack:** Python 3 (stdlib only: `json`, `glob`, `pathlib`, `datetime`, `zoneinfo`, `collections`, `argparse`), pytest (tests), HTML/SVG/vanilla JS (view).

**Spec:** `docs/superpowers/specs/2026-06-05-token-burn-dashboard-design.md`

**Reuse note:** The dedup approach and price table are already validated in `a companion cost deck` and `cost-model.json` (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5; cache-write 1.25× input, cache-read 0.10× input). This plan reimplements the same logic stdlib-only (the dashboard is a standalone repo, no cross-repo imports) so the dashboard's cost numbers agree with the corrected deck.

---

## File Structure

```
token-burn/
├── parse.py                 # stdlib parser → data.json (all logic lives here)
├── dashboard.html           # template: pure SVG + vanilla JS, /*__DATA__*/ placeholder
├── build.sh                 # parse → inline data → write out/dashboard.html → open
├── out/dashboard.html       # generated, self-contained (gitignored)
├── data.json                # generated rollup (gitignored)
├── tests/
│   ├── conftest.py          # makes parse.py importable
│   ├── fixtures/            # hand-written .jsonl fixtures
│   └── test_parse.py        # all parser unit tests
└── README.md
```

`parse.py` is intentionally one focused module: a handful of small pure functions
(`dedupe_messages`, `model_class`, `cost_usd`, `session_label`, `project_of`,
`local_day`) composed by `parse_session` → `build_rollup` → `main`. Each pure
function is independently tested.

---

## Task 1: Scaffold + `dedupe_messages`

**Files:**
- Create: `parse.py`
- Create: `tests/conftest.py`
- Create: `tests/test_parse.py`

- [ ] **Step 1: Make parse.py importable from tests**

Create `tests/conftest.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_parse.py`:

```python
import json
from parse import dedupe_messages


def _line(mid, block, out, *, inp=1, cc=0, cr=0, model="claude-opus-4-8",
          uuid="u", sidechain=False, cwd="/Users/j/Documents/projects/foo",
          ts="2026-05-20T10:00:00.000Z"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "timestamp": ts,
        "isSidechain": sidechain, "cwd": cwd,
        "message": {"id": mid, "model": model, "content": [{"type": block}],
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_creation_input_tokens": cc,
                              "cache_read_input_tokens": cr}},
    })


def test_collapses_content_block_duplicates_to_one_message():
    raw = "\n".join([
        _line("msg_A", "thinking", out=500, cc=46040, uuid="a1"),
        _line("msg_A", "text", out=500, cc=46040, uuid="a2"),
        _line("msg_A", "tool_use", out=500, cc=46040, uuid="a3"),
    ])
    recs = dedupe_messages(raw)
    assert len(recs) == 1
    assert recs[0]["out"] == 500 and recs[0]["cc"] == 46040 and recs[0]["in"] == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_parse.py::test_collapses_content_block_duplicates_to_one_message -v`
Expected: FAIL — `ImportError: cannot import name 'dedupe_messages'` (or ModuleNotFoundError: parse).

- [ ] **Step 4: Write minimal implementation**

Create `parse.py`:

```python
"""Parse Claude Code session logs into a token-burn rollup (data.json).

Claude Code writes one JSONL line per content block of an assistant turn, each
stamped with the same message-level usage, so raw line-summing inflates tokens
~2x. We dedupe by assistant message.id (keep max-output occurrence).
"""
import json


def dedupe_messages(raw_text):
    """One usage record per assistant message.id. Record keys:
    id, model, timestamp, in, out, cc, cr, cwd, is_sidechain."""
    best = {}
    for line in raw_text.splitlines():
        if '"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        key = msg.get("id") or ("uuid:" + str(obj.get("uuid")))
        rec = {
            "id": msg.get("id"),
            "model": msg.get("model"),
            "timestamp": obj.get("timestamp"),
            "cwd": obj.get("cwd"),
            "is_sidechain": bool(obj.get("isSidechain")),
            "in": usage.get("input_tokens", 0) or 0,
            "out": usage.get("output_tokens", 0) or 0,
            "cc": usage.get("cache_creation_input_tokens", 0) or 0,
            "cr": usage.get("cache_read_input_tokens", 0) or 0,
        }
        prev = best.get(key)
        if prev is None or rec["out"] > prev["out"]:
            best[key] = rec
    return list(best.values())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_parse.py -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Add the remaining dedupe tests**

Append to `tests/test_parse.py`:

```python
def test_distinct_messages_are_all_kept():
    raw = "\n".join([_line("msg_A", "text", out=10), _line("msg_B", "text", out=20)])
    recs = dedupe_messages(raw)
    assert {r["id"] for r in recs} == {"msg_A", "msg_B"}
    assert sum(r["out"] for r in recs) == 30


def test_keeps_max_output_when_duplicate_differs():
    raw = "\n".join([_line("msg_A", "text", out=10), _line("msg_A", "text", out=99)])
    recs = dedupe_messages(raw)
    assert len(recs) == 1 and recs[0]["out"] == 99


def test_skips_non_usage_and_malformed():
    raw = "\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        "not json",
        _line("msg_A", "text", out=5),
    ])
    recs = dedupe_messages(raw)
    assert len(recs) == 1 and recs[0]["out"] == 5
```

Run: `python3 -m pytest tests/test_parse.py -v` → Expected: PASS (4 passed).

- [ ] **Step 7: Commit**

```bash
cd ~/Documents/projects/token-burn
git add parse.py tests/
git commit -m "feat: dedupe_messages — one usage record per assistant message.id"
```

---

## Task 2: `model_class` + `cost_usd`

**Files:**
- Modify: `parse.py`
- Test: `tests/test_parse.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parse.py`:

```python
from parse import model_class, cost_usd


def test_model_class_maps_families():
    assert model_class("claude-opus-4-8") == "opus"
    assert model_class("claude-sonnet-4-6") == "sonnet"
    assert model_class("claude-haiku-4-5-20251001") == "haiku"
    assert model_class(None) == "sonnet"  # safe default


def test_cost_usd_matches_deck_weights():
    # opus: in $5, out $25, cache-write 1.25x in = $6.25, cache-read 0.10x in = $0.50 (per MTok)
    # 1M cache reads at opus = $0.50
    assert round(cost_usd("claude-opus-4-8", 0, 0, 0, 1_000_000), 4) == 0.5
    # 1M output at opus = $25
    assert round(cost_usd("claude-opus-4-8", 0, 1_000_000, 0, 0), 4) == 25.0
    # 1M cache-write at opus = $6.25
    assert round(cost_usd("claude-opus-4-8", 0, 0, 1_000_000, 0), 4) == 6.25
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_parse.py -k "model_class or cost_usd" -v`
Expected: FAIL — `ImportError: cannot import name 'model_class'`.

- [ ] **Step 3: Implement**

Append to `parse.py`:

```python
# API $/MTok (validated 2026-06-05 against a companion cost deck).
# cache write = 1.25x input, cache read = 0.10x input.
PRICE = {
    "opus":   {"in": 5.0, "out": 25.0},
    "sonnet": {"in": 3.0, "out": 15.0},
    "haiku":  {"in": 1.0, "out": 5.0},
}


def model_class(model):
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "sonnet"  # safe default for unknown


def cost_usd(model, inp, out, cc, cr):
    p = PRICE[model_class(model)]
    return (inp * p["in"] + out * p["out"]
            + cc * p["in"] * 1.25 + cr * p["in"] * 0.10) / 1e6
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_parse.py -v` → Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add parse.py tests/test_parse.py
git commit -m "feat: model_class + cost_usd (deck-validated price weights)"
```

---

## Task 3: `session_label`

**Files:**
- Modify: `parse.py`
- Test: `tests/test_parse.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parse.py`:

```python
from parse import session_label


def _user(text, meta=False, role="user"):
    return json.dumps({"type": "user", "isMeta": meta,
                       "message": {"role": role, "content": text}})


def test_session_label_prefers_custom_then_ai_then_prompt():
    custom = json.dumps({"type": "summary", "customTitle": "My Title"})
    ai = json.dumps({"type": "summary", "aiTitle": "AI Title"})
    raw_all = "\n".join([custom, ai, _user("first prompt")])
    assert session_label(raw_all) == "My Title"

    raw_ai = "\n".join([ai, _user("first prompt")])
    assert session_label(raw_ai) == "AI Title"

    raw_prompt = "\n".join([_user("just the first human prompt here")])
    assert session_label(raw_prompt) == "just the first human prompt here"


def test_session_label_skips_meta_and_tool_results():
    raw = "\n".join([
        _user("<command-stdout>noise</command-stdout>", meta=True),
        _user("real first prompt"),
    ])
    assert session_label(raw) == "real first prompt"
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_parse.py -k session_label -v`
Expected: FAIL — `ImportError: cannot import name 'session_label'`.

- [ ] **Step 3: Implement**

Append to `parse.py`:

```python
def _first_user_prompt(raw_text):
    for line in raw_text.splitlines():
        if '"role":"user"' not in line and '"role": "user"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("isMeta") or obj.get("isCompactSummary"):
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "user":
            continue
        c = msg.get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = next((b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text"), "")
        else:
            text = ""
        text = " ".join(text.split())
        if text and not text.startswith("<"):
            return text[:120]
    return ""


def session_label(raw_text):
    """customTitle || aiTitle || first non-meta user prompt (truncated)."""
    custom = ai = None
    for line in raw_text.splitlines():
        if '"customTitle"' in line and custom is None:
            try:
                custom = json.loads(line).get("customTitle")
            except Exception:
                pass
        if '"aiTitle"' in line and ai is None:
            try:
                ai = json.loads(line).get("aiTitle")
            except Exception:
                pass
    return custom or ai or _first_user_prompt(raw_text) or "(untitled session)"
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_parse.py -v` → Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add parse.py tests/test_parse.py
git commit -m "feat: session_label (customTitle > aiTitle > first prompt)"
```

---

## Task 4: `project_of` + `is_subagent`

**Files:**
- Modify: `parse.py`
- Test: `tests/test_parse.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parse.py`:

```python
from parse import project_of, is_subagent


def test_project_of_uses_cwd_last_segment():
    assert project_of("/Users/j/Documents/projects/FramingEinstein") == "FramingEinstein"
    assert project_of("/Users/j/Documents/projects/acme-storage/site") == "site"
    assert project_of(None) == "(unknown)"


def test_is_subagent_by_path_or_sidechain():
    assert is_subagent("/a/b/subagents/agent-x.jsonl", any_sidechain=False) is True
    assert is_subagent("/a/b/session.jsonl", any_sidechain=True) is True
    assert is_subagent("/a/b/session.jsonl", any_sidechain=False) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_parse.py -k "project_of or is_subagent" -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

Append to `parse.py`:

```python
import os


def project_of(cwd):
    if not cwd:
        return "(unknown)"
    return os.path.basename(cwd.rstrip("/")) or "(unknown)"


def is_subagent(file_path, any_sidechain):
    return ("/subagents/" in str(file_path)) or bool(any_sidechain)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_parse.py -v` → Expected: PASS (11 passed).

- [ ] **Step 5: Commit**

```bash
git add parse.py tests/test_parse.py
git commit -m "feat: project_of + is_subagent"
```

---

## Task 5: `local_day`

**Files:**
- Modify: `parse.py`
- Test: `tests/test_parse.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parse.py`:

```python
from zoneinfo import ZoneInfo
from parse import local_day


def test_local_day_buckets_in_target_tz():
    # 03:30 UTC on the 21st is still the 20th in US Pacific (UTC-7/8)
    assert local_day("2026-05-21T03:30:00.000Z", ZoneInfo("America/Los_Angeles")) == "2026-05-20"
    # noon UTC stays same calendar day in Pacific
    assert local_day("2026-05-21T19:00:00.000Z", ZoneInfo("America/Los_Angeles")) == "2026-05-21"


def test_local_day_handles_missing_timestamp():
    assert local_day(None, ZoneInfo("America/Los_Angeles")) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_parse.py -k local_day -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

Append to `parse.py`:

```python
from datetime import datetime


def local_day(ts_iso, tz):
    if not ts_iso:
        return None
    s = ts_iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    return dt.astimezone(tz).date().isoformat()
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_parse.py -v` → Expected: PASS (13 passed).

- [ ] **Step 5: Commit**

```bash
git add parse.py tests/test_parse.py
git commit -m "feat: local_day tz-aware bucketing"
```

---

## Task 6: `parse_session`

**Files:**
- Modify: `parse.py`
- Test: `tests/test_parse.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parse.py`:

```python
from zoneinfo import ZoneInfo
from parse import parse_session

UTC = ZoneInfo("UTC")


def test_parse_session_aggregates_with_label_project_subagent():
    raw = "\n".join([
        json.dumps({"type": "summary", "aiTitle": "Fix the parser"}),
        _user("please fix the parser"),
        _line("msg_A", "text", out=100, cc=2000, cr=5000,
              cwd="/Users/j/Documents/projects/FramingEinstein",
              model="claude-opus-4-8", ts="2026-05-21T19:00:00.000Z"),
        _line("msg_A", "tool_use", out=100, cc=2000, cr=5000,  # dup block
              cwd="/Users/j/Documents/projects/FramingEinstein",
              model="claude-opus-4-8", ts="2026-05-21T19:00:00.000Z"),
    ])
    s = parse_session(raw, "/x/session.jsonl", UTC)
    assert s["label"] == "Fix the parser"
    assert s["project"] == "FramingEinstein"
    assert s["is_subagent"] is False
    # one message (deduped), bucketed to its UTC day
    assert len(s["messages"]) == 1
    m = s["messages"][0]
    assert m["day"] == "2026-05-21" and m["mclass"] == "opus"
    assert m["out"] == 100 and m["cr"] == 5000
    assert round(m["cost"], 6) == round(cost_usd("claude-opus-4-8", 1, 100, 2000, 5000), 6)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_parse.py -k parse_session -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

Append to `parse.py`:

```python
def parse_session(raw_text, file_path, tz):
    recs = dedupe_messages(raw_text)
    any_side = any(r["is_sidechain"] for r in recs)
    cwd = next((r["cwd"] for r in recs if r.get("cwd")), None)
    messages = []
    for r in recs:
        messages.append({
            "day": local_day(r["timestamp"], tz),
            "mclass": model_class(r["model"]),
            "in": r["in"], "out": r["out"], "cc": r["cc"], "cr": r["cr"],
            "cost": cost_usd(r["model"], r["in"], r["out"], r["cc"], r["cr"]),
        })
    return {
        "label": session_label(raw_text),
        "project": project_of(cwd),
        "is_subagent": is_subagent(file_path, any_side),
        "messages": messages,
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_parse.py -v` → Expected: PASS (14 passed).

- [ ] **Step 5: Commit**

```bash
git add parse.py tests/test_parse.py
git commit -m "feat: parse_session integrates dedupe/label/project/cost"
```

---

## Task 7: `build_rollup`

**Files:**
- Modify: `parse.py`
- Test: `tests/test_parse.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parse.py`:

```python
from parse import build_rollup


def _sess(project, sub, label, day, mclass, comps, cost):
    i, o, cc, cr = comps
    return {"label": label, "project": project, "is_subagent": sub,
            "messages": [{"day": day, "mclass": mclass, "in": i, "out": o,
                          "cc": cc, "cr": cr, "cost": cost}]}


def test_build_rollup_shape_and_sanity():
    sessions = [
        _sess("FramingEinstein", False, "main work", "2026-05-21", "opus",
              (1, 100, 2000, 5000), 0.20),
        _sess("FramingEinstein", True, "subagent work", "2026-05-21", "opus",
              (0, 50, 1000, 4000), 0.10),
    ]
    roll = build_rollup(sessions, tz_name="UTC", assistant_events=7, unique_messages=2)
    assert roll["meta"]["assistant_events"] == 7
    assert roll["meta"]["unique_messages"] == 2
    day = next(d for d in roll["days"] if d["date"] == "2026-05-21")
    bm = day["byModel"]["opus"]
    assert bm["out"] == 150 and bm["cr"] == 9000
    assert round(bm["cost_usd"], 4) == 0.30
    assert round(day["mainVsSub"]["main"]["out"]) == 100
    assert round(day["mainVsSub"]["sub"]["out"]) == 50
    assert day["topProjects"][0]["project"] == "FramingEinstein"
    assert any(s["label"] == "subagent work" for s in day["sampleSessions"])
    assert round(roll["meta"]["totals"]["cost_usd"], 4) == 0.30
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_parse.py -k build_rollup -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement**

Append to `parse.py`:

```python
import collections


def _zero():
    return {"in": 0, "out": 0, "cc": 0, "cr": 0, "cost_usd": 0.0}


def _add(acc, m):
    for k in ("in", "out", "cc", "cr"):
        acc[k] += m[k]
    acc["cost_usd"] += m["cost"]


def build_rollup(sessions, tz_name, assistant_events, unique_messages):
    days = collections.defaultdict(lambda: {
        "byModel": collections.defaultdict(_zero),
        "mainVsSub": {"main": _zero(), "sub": _zero()},
        "projects": collections.Counter(),
        "sessions": [],  # (label, project, total) tuples
    })
    totals = _zero()
    for s in sessions:
        for m in s["messages"]:
            d = m["day"]
            if d is None:
                continue
            bucket = days[d]
            _add(bucket["byModel"][m["mclass"]], m)
            _add(bucket["mainVsSub"]["sub" if s["is_subagent"] else "main"], m)
            tok = m["in"] + m["out"] + m["cc"] + m["cr"]
            bucket["projects"][s["project"]] += tok
            bucket["sessions"].append((s["label"], s["project"], tok))
            _add(totals, m)

    out_days = []
    for date in sorted(days):
        b = days[date]
        top_projects = [{"project": p, "total": t}
                        for p, t in b["projects"].most_common(5)]
        sample = sorted(b["sessions"], key=lambda x: -x[2])[:5]
        out_days.append({
            "date": date,
            "byModel": {k: dict(v) for k, v in b["byModel"].items()},
            "mainVsSub": b["mainVsSub"],
            "topProjects": top_projects,
            "sampleSessions": [{"label": l, "project": p, "total": t}
                               for l, p, t in sample],
        })
    return {
        "meta": {"tz": tz_name, "assistant_events": assistant_events,
                 "unique_messages": unique_messages, "totals": dict(totals)},
        "days": out_days,
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_parse.py -v` → Expected: PASS (15 passed).

- [ ] **Step 5: Commit**

```bash
git add parse.py tests/test_parse.py
git commit -m "feat: build_rollup → data.json day×model×component shape"
```

---

## Task 8: `main()` CLI + real-data smoke run

**Files:**
- Modify: `parse.py`
- Test: `tests/test_parse.py` (fixture-dir integration test)
- Create: `tests/fixtures/session_a.jsonl`, `tests/fixtures/subagents/agent_b.jsonl`

- [ ] **Step 1: Create fixtures**

`tests/fixtures/session_a.jsonl` (one real-shaped session, message logged as 2 blocks):

```
{"type":"summary","aiTitle":"Fixture A"}
{"type":"user","isMeta":false,"message":{"role":"user","content":"do the thing"}}
{"type":"assistant","uuid":"a1","timestamp":"2026-05-21T19:00:00.000Z","cwd":"/Users/j/Documents/projects/FramingEinstein","message":{"id":"msg_A","model":"claude-opus-4-8","content":[{"type":"text"}],"usage":{"input_tokens":1,"output_tokens":100,"cache_creation_input_tokens":2000,"cache_read_input_tokens":5000}}}
{"type":"assistant","uuid":"a2","timestamp":"2026-05-21T19:00:00.000Z","cwd":"/Users/j/Documents/projects/FramingEinstein","message":{"id":"msg_A","model":"claude-opus-4-8","content":[{"type":"tool_use"}],"usage":{"input_tokens":1,"output_tokens":100,"cache_creation_input_tokens":2000,"cache_read_input_tokens":5000}}}
```

`tests/fixtures/subagents/agent_b.jsonl` (a sidechain/subagent message):

```
{"type":"assistant","uuid":"b1","timestamp":"2026-05-21T20:00:00.000Z","isSidechain":true,"cwd":"/Users/j/Documents/projects/FramingEinstein","message":{"id":"msg_B","model":"claude-haiku-4-5-20251001","content":[{"type":"text"}],"usage":{"input_tokens":1,"output_tokens":50,"cache_creation_input_tokens":1000,"cache_read_input_tokens":4000}}}
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_parse.py`:

```python
from pathlib import Path
from parse import scan


def test_scan_fixture_dir_dedupes_and_flags_subagent():
    root = Path(__file__).parent / "fixtures"
    roll = scan(root, tz_name="UTC")
    # 3 assistant lines (2 are dup blocks of msg_A), 2 unique messages
    assert roll["meta"]["assistant_events"] == 3
    assert roll["meta"]["unique_messages"] == 2
    day = next(d for d in roll["days"] if d["date"] == "2026-05-21")
    assert "opus" in day["byModel"] and "haiku" in day["byModel"]
    assert day["byModel"]["opus"]["out"] == 100  # deduped, not 200
    # haiku message came from a subagent file
    assert day["mainVsSub"]["sub"]["out"] == 50
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 -m pytest tests/test_parse.py -k scan -v`
Expected: FAIL — `ImportError: cannot import name 'scan'`.

- [ ] **Step 4: Implement `scan` + `main`**

Append to `parse.py`:

```python
import glob
import argparse
from zoneinfo import ZoneInfo


def scan(root, tz_name):
    tz = ZoneInfo(tz_name)
    sessions = []
    assistant_events = 0
    unique_messages = 0
    for f in glob.glob(os.path.join(str(root), "**", "*.jsonl"), recursive=True):
        try:
            raw = open(f, "r", errors="ignore").read()
        except Exception:
            continue
        assistant_events += sum(
            1 for ln in raw.splitlines()
            if '"usage"' in ln and '"type":"assistant"' in ln.replace(" ", "")
        )
        s = parse_session(raw, f, tz)
        unique_messages += len(s["messages"])
        sessions.append(s)
    return build_rollup(sessions, tz_name, assistant_events, unique_messages)


def main():
    ap = argparse.ArgumentParser(description="Build token-burn data.json from Claude Code logs.")
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--tz", default=datetime.now().astimezone().tzinfo.key
                    if hasattr(datetime.now().astimezone().tzinfo, "key") else "UTC")
    ap.add_argument("--out", default=str(Path(__file__).parent / "data.json"))
    args = ap.parse_args()
    roll = scan(args.root, args.tz)
    with open(args.out, "w") as fh:
        json.dump(roll, fh)
    m = roll["meta"]
    print(f"wrote {args.out}: {len(roll['days'])} days, "
          f"{m['assistant_events']} events -> {m['unique_messages']} unique msgs, "
          f"${m['totals']['cost_usd']:,.0f} total")


if __name__ == "__main__":
    from pathlib import Path  # noqa: needed for default --out
    main()
```

Note: move `from pathlib import Path` to the top imports of `parse.py` (with the others) so it is available to both `scan` and `main`; the inline import above is a reminder — delete it once Path is imported at module top.

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m pytest tests/test_parse.py -v` → Expected: PASS (16 passed).

- [ ] **Step 6: Smoke-run on real logs**

Run: `python3 parse.py`
Expected: prints e.g. `wrote .../data.json: NN days, 115xxx events -> 55xxx unique msgs, $XX,XXX total`. **Verify `assistant_events` is roughly ~2× `unique_messages`** (confirms dedup is active — this is the regression guard from the spec).

- [ ] **Step 7: Commit**

```bash
git add parse.py tests/
git commit -m "feat: scan + main CLI; writes data.json from ~/.claude logs"
```

---

## Task 9: `dashboard.html` — shell + metric toggle + calendar heatmap

**Files:**
- Create: `dashboard.html`

The committed `dashboard.html` is a template containing the literal token `/*__DATA__*/`
where `build.sh` injects `const DATA = {...}`. For development, temporarily replace it with
real data via Step 3.

- [ ] **Step 1: Create the template shell**

Create `dashboard.html` with: a `<head>` (Tufte-ish CSS: system font, restrained palette
`--navy:#1B2A4A; --gold:#F0A500; --ink:#333; --muted:#8a93a6`), a data block
`<script>const DATA = /*__DATA__*/ null;</script>`, a metric toggle (segmented radio:
`total | generative | output | cost`), and four `<section>` placeholders with `<svg>` hosts:
`#heatmap`, `#timeseries`, `#topdays`, `#models`. Add a JS `metricValue(comp, metric)` helper:

```html
<script>
function metricValue(c, metric) {
  if (metric === 'total')      return c.in + c.out + c.cc + c.cr;
  if (metric === 'generative') return c.in + c.out + c.cc;
  if (metric === 'output')     return c.out;
  if (metric === 'cost')       return c.cost_usd; // precomputed per spec
  return 0;
}
function dayComponents(day) { // sum byModel into one component bag for the day
  const acc = {in:0,out:0,cc:0,cr:0,cost_usd:0};
  for (const m of Object.values(day.byModel))
    for (const k in acc) acc[k] += m[k];
  return acc;
}
let METRIC = 'total';
function rerender(){ drawHeatmap(); drawTimeseries(); drawTopDays(); drawModels(); }
</script>
```

- [ ] **Step 2: Implement `drawHeatmap()`**

A GitHub-style calendar: columns = ISO weeks, rows = weekday (Mon–Sun), each cell a `<rect>`
filled by intensity = `metricValue(dayComponents(day), METRIC)` mapped through a 5-step
quantile scale (light→navy). Build a `date -> day` map from `DATA.days`; iterate from the
first to last date filling empty days as blank cells. Add a `<title>` per cell
(`${date}: ${fmt(value)}`). Wire the metric toggle's `change` event to set `METRIC` and call
`rerender()`. Include a `fmt(n)` helper (`1.2B / 3.4M / 56K`).

- [ ] **Step 3: Verify by eye**

Run: `python3 parse.py && python3 - <<'PY'
import json,pathlib
d=open('dashboard.html').read().replace('/*__DATA__*/', json.dumps(json.load(open('data.json'))))
pathlib.Path('out').mkdir(exist_ok=True); open('out/dashboard.html','w').write(d)
print('open out/dashboard.html')
PY`
Then `open out/dashboard.html`. **Confirm:** the heatmap renders a calendar of your real
usage; toggling the metric visibly changes cell intensities; hover shows date+value.

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "feat: dashboard shell + metric toggle + calendar heatmap"
```

---

## Task 10: Log-scale time series

**Files:**
- Modify: `dashboard.html`

- [ ] **Step 1: Implement `drawTimeseries()`**

Plot one point/bar per `DATA.days` entry: x = date (linear), y = `metricValue(dayComponents(day), METRIC)`
on a **log10 Y axis** (`y = (log10(v) - log10(min))/(log10(max)-log10(min))`, clamp v≥1).
Draw axis ticks at powers of 10, a polyline through points, and light gridlines. Skip/clamp
zero values. Respects `METRIC` (cost uses `cost_usd`).

- [ ] **Step 2: Verify by eye**

Re-run the Step-3 inline builder from Task 9, `open out/dashboard.html`. **Confirm:** the series
spans your range on a log axis (early small days and recent large days both legible); toggling
to `cost` switches to dollars.

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: log-scale time-series view"
```

---

## Task 11: Top-10 days

**Files:**
- Modify: `dashboard.html`

- [ ] **Step 1: Implement `drawTopDays()`**

Sort `DATA.days` by `metricValue(dayComponents(day), METRIC)` desc, take 10. Render an HTML
table (not SVG): columns = Date · Value (`fmt`) · What I did (join `day.topProjects` names +
the top 1–2 `day.sampleSessions[].label`). Re-sorts on metric change.

- [ ] **Step 2: Verify by eye**

Rebuild + open. **Confirm:** the top-10 list shows your biggest days with recognizable
project names and session titles; switching metric re-orders it.

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: top-10 days view with what-I-did labels"
```

---

## Task 12: Model distribution + main-vs-subagent

**Files:**
- Modify: `dashboard.html`

- [ ] **Step 1: Implement `drawModels()`**

Aggregate across all `DATA.days`: per-model totals (`metricValue` over each `byModel[m]`) →
a horizontal stacked/!grouped bar (opus/sonnet/haiku). Below it, a second bar splitting
`mainVsSub.main` vs `mainVsSub.sub`. Show percentages. Respects `METRIC`.

- [ ] **Step 2: Verify by eye**

Rebuild + open. **Confirm:** model split looks right (Opus-dominant), and the main-vs-subagent
bar shows a meaningful subagent slice on metric `total`.

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: model distribution + main-vs-subagent view"
```

---

## Task 13: `build.sh` end-to-end

**Files:**
- Create: `build.sh`
- Create: `.gitignore` (if not present: `data.json`, `out/`)

- [ ] **Step 1: Implement build.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 parse.py "$@"
python3 - <<'PY'
import json, pathlib
tpl = open("dashboard.html").read()
data = json.load(open("data.json"))
html = tpl.replace("/*__DATA__*/", json.dumps(data))
pathlib.Path("out").mkdir(exist_ok=True)
open("out/dashboard.html", "w").write(html)
print("wrote out/dashboard.html")
PY
open out/dashboard.html 2>/dev/null || echo "open out/dashboard.html"
```

- [ ] **Step 2: Make executable + run**

```bash
chmod +x build.sh && ./build.sh
```
Expected: prints `wrote .../data.json ...` then `wrote out/dashboard.html`, then opens the
dashboard. **Confirm all four views render from a single double-click of `out/dashboard.html`
(no server, works offline).**

- [ ] **Step 3: Commit**

```bash
git add build.sh .gitignore
git commit -m "feat: build.sh — parse, inline data, open self-contained dashboard"
```

---

## Task 14: README + final verification

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README**

Document: what it is (1 paragraph), `./build.sh` to refresh + open, `--tz`/`--root`/`--out`
flags, the metric toggle meanings, the dedup gotcha (one line + link to spec), and that it's
local/offline/private.

- [ ] **Step 2: Full verification**

Run: `python3 -m pytest tests/ -v` → Expected: all green (16 passed).
Run: `./build.sh` → Expected: opens a working dashboard; sanity-check `assistant_events ≈ 2× unique_messages` in the parse output.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: README + usage"
```

---

## Self-Review (completed by plan author)

- **Spec coverage:** parser (T1–T8), dedup correctness anchor (T1, T8 sanity), exact tokens/cwd/labels (T3–T6), 4 metrics incl. precomputed cost (T2, T9 `metricValue`), 4 views (T9–T12), build/refresh (T13), offline self-contained inlining (T9 Step 3, T13), TDD fixtures incl. dedup/no-usage/subagent/tz/labels (T1, T3, T5, T8). All spec sections map to a task.
- **Deferred from spec (noted, not built):** the `ephemeral_1h` 2× cache-write refinement (spec §6.1) — this plan uses the deck's flat 1.25× so dashboard ↔ deck agree; record as a future option in README.
- **Type consistency:** record keys (`in/out/cc/cr/cost`), `mclass`, `day`, rollup `byModel/mainVsSub/topProjects/sampleSessions`, and JS `metricValue(c, metric)` / `dayComponents(day)` are used consistently across tasks.
- **Placeholder scan:** no TBD/TODO; every code step shows full code. (HTML view tasks are verified-by-eye per spec, not unit-tested — explicitly called out.)
