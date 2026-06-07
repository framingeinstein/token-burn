"""Parse Claude Code session logs into a token-burn rollup (data.json).

Claude Code writes one JSONL line per content block of an assistant turn, each
stamped with the SAME message-level usage, so raw line-summing inflates tokens
~2x. We dedupe by assistant message.id (keep the max-output occurrence).

Stdlib only. Run `python3 parse.py` to write data.json from ~/.claude/projects.
"""
import argparse
import collections
import glob
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from prices import load_prices, rate_for


def dedupe_messages(raw_text):
    """One usage record per assistant message.id. Keys: id, model, timestamp,
    cwd, is_sidechain, in, out, cc, cr, n (occurrence count across content blocks)."""
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


def project_of(cwd):
    if not cwd:
        return "(unknown)"
    return os.path.basename(cwd.rstrip("/")) or "(unknown)"


def is_subagent(file_path, any_sidechain):
    return ("/subagents/" in str(file_path)) or bool(any_sidechain)


def local_day(ts_iso, tz):
    if not ts_iso:
        return None
    s = ts_iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    return dt.astimezone(tz).date().isoformat()


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


def _zero():
    return {"in": 0, "out": 0, "cc": 0, "cr": 0, "cost_usd": 0.0}


def _add(acc, m):
    for k in ("in", "out", "cc", "cr"):
        acc[k] += m[k]
    acc["cost_usd"] += m["cost"]


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
            with open(f, "r", errors="ignore") as fh:
                raw = fh.read()
        except Exception:
            continue
        s = parse_session(raw, f, tz, prices)
        for m in s["messages"]:
            d = m["day"]
            if not (m["model"] and d):
                continue
            if (since and d < since) or (until and d > until):
                continue
            if rate_for(prices, m["model"], d) is None:
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


def _default_tz_name():
    """Best-effort local IANA tz name (macOS/Linux /etc/localtime); fallback UTC."""
    try:
        p = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in p:
            return p.split("/zoneinfo/")[-1]
    except Exception:
        pass
    return "UTC"


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


if __name__ == "__main__":
    main()
