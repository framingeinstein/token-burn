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

# API $/MTok (validated 2026-06-05 against a companion cost deck).
# cache write = 1.25x input, cache read = 0.10x input.
PRICE = {
    "opus":   {"in": 5.0, "out": 25.0},
    "sonnet": {"in": 3.0, "out": 15.0},
    "haiku":  {"in": 1.0, "out": 5.0},
}


def dedupe_messages(raw_text):
    """One usage record per assistant message.id. Keys: id, model, timestamp,
    cwd, is_sidechain, in, out, cc, cr."""
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
        "sessions": [],
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
    ap = argparse.ArgumentParser(description="Build token-burn data.json from Claude Code logs.")
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


if __name__ == "__main__":
    main()
