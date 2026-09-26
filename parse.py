"""Parse Claude Code session logs into a token-burn rollup (data.json).

Claude Code writes one JSONL line per content block of an assistant turn, each
stamped with the SAME message-level usage, so raw line-summing inflates tokens
~2x. We dedupe by assistant message.id (keep the max-output occurrence).

Claude Code may run with several config stores (CLAUDE_CONFIG_DIR per workspace:
~/.claude, ~/.claude-fe, ~/.claude-10fed, ...), each with its own projects/ dir.
default_roots() finds them all; every entry point reads the union.

Stdlib only. Run `python3 parse.py` to write data.json from every store's projects/.
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
    if "fable" in m:
        return "fable"
    if "mythos" in m:
        return "mythos"
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
            "id": r["id"],
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
        "source": "local",
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
        "bySource": collections.defaultdict(_zero),
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
            _add(bucket["bySource"][s.get("source", "local")], m)
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
            "bySource": {k: dict(v) for k, v in b["bySource"].items()},
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


def default_roots(home=None, env=None):
    """Every Claude Code transcript root: ~/.claude/projects, each ~/.claude-*/projects,
    and $CLAUDE_CONFIG_DIR/projects. Stores without projects/ are skipped; roots that
    resolve to the same directory (symlinked overlay) are kept once."""
    home = Path(home) if home is not None else Path.home()
    env = os.environ if env is None else env
    candidates = [home / ".claude" / "projects"]
    candidates += sorted(home.glob(".claude-*/projects"))
    if env.get("CLAUDE_CONFIG_DIR"):
        candidates.append(Path(env["CLAUDE_CONFIG_DIR"]).expanduser() / "projects")
    roots, seen = [], set()
    for c in candidates:
        if not c.is_dir():
            continue
        real = os.path.realpath(c)
        if real in seen:
            continue
        seen.add(real)
        roots.append(c)
    return roots


def _as_roots(root):
    return [root] if isinstance(root, (str, os.PathLike)) else list(root)


def _mtime_floor(since, tz):
    """Epoch seconds of local midnight starting `since` (None => no floor). A file
    last written before then cannot hold a message dated on/after `since`."""
    if not since:
        return None
    y, m, d = map(int, since.split("-"))
    return datetime(y, m, d, tzinfo=tz).timestamp()


def _jsonl_files(roots, modified_since=None):
    seen = set()
    for r in _as_roots(roots):
        for f in glob.glob(os.path.join(str(r), "**", "*.jsonl"), recursive=True):
            if modified_since is not None:
                try:
                    if os.path.getmtime(f) < modified_since:
                        continue
                except OSError:
                    continue
            real = os.path.realpath(f)
            if real not in seen:
                seen.add(real)
                yield f


def _records(sessions_iter, tz_name, since, until, prices):
    sessions = []
    unpriced = set()
    for s in sessions_iter:
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


def _read(f):
    try:
        with open(f, "r", errors="ignore") as fh:
            return fh.read()
    except Exception:
        return None


def day_records(root, tz_name, since=None, until=None, prices=None):
    """Glob logs under one root or a list of roots, parse, and return
    (per-day records in [since,until], meta)."""
    if prices is None:
        prices = load_prices()
    tz = ZoneInfo(tz_name)

    def sessions():
        for f in _jsonl_files(root, _mtime_floor(since, tz)):
            raw = _read(f)
            if raw is not None:
                yield parse_session(raw, f, tz, prices)
    return _records(sessions(), tz_name, since, until, prices)


DEFAULT_FACTORY_ROOT = Path("~/.token-burn/factory-transcripts").expanduser()


def factory_day_records(root, tz_name, since=None, until=None, prices=None):
    """Remote factory runner transcripts, mirrored from the bucket laid out as
    {runner}/{issue}/{execution}/{session}.jsonl. Attribution comes from the path
    (every runner's cwd is the same /tmp/wt-N), and a message.id counts once across
    files because a re-run turn re-uploads the session under a new execution."""
    if prices is None:
        prices = load_prices()
    tz = ZoneInfo(tz_name)
    root = Path(root)

    def sessions():
        seen = set()
        for f in sorted(_jsonl_files(root, _mtime_floor(since, tz))):
            parts = Path(f).relative_to(root).parts
            if len(parts) < 2:
                continue
            raw = _read(f)
            if raw is None:
                continue
            s = parse_session(raw, f, tz, prices)
            fresh = []
            for m in s["messages"]:
                if m["id"] and m["id"] in seen:
                    continue
                seen.add(m["id"])
                fresh.append(m)
            runner, issue = parts[0], parts[1]
            s["messages"] = fresh
            s["project"] = s["source"] = f"factory:{runner}"
            s["label"] = f"#{issue} {s['label']}"
            yield s
    if not root.is_dir():
        return _records(iter(()), tz_name, since, until, prices)
    return _records(sessions(), tz_name, since, until, prices)


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
    ap.add_argument("--root", action="append", default=None,
                    help="transcript root (repeatable); default: every Claude store")
    ap.add_argument("--tz", default=_default_tz_name())
    ap.add_argument("--out", default=str(Path(__file__).parent / "data.json"))
    args = ap.parse_args()
    roll = scan(args.root or default_roots(), args.tz)
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
