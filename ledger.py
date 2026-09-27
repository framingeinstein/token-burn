"""Append-only daily snapshot archive (snapshots.jsonl) + assembly.

Archive = permanent, authoritative record. Finalized days (date < today) are
immutable; only 'today' is parsed live. One JSON object per line; on read, the
LAST line per date wins (so --refinalize just appends a newer line).
"""
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import collections

from parse import day_records, factory_day_records, _totals, _zero
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


def _sum_comp(a, b):
    out = _zero()
    for c in (a or {}), (b or {}):
        for k in out:
            out[k] += c.get(k, 0) or 0
    return out


def _sum_map(a, b):
    a, b = a or {}, b or {}
    return {k: _sum_comp(a.get(k), b.get(k)) for k in sorted(set(a) | set(b))}


def _by_source(day):
    """Days frozen before sources existed are all local Claude Code usage."""
    if "bySource" in day:
        return day["bySource"]
    local = _zero()
    for comp in (day.get("byModel") or {}).values():
        local = _sum_comp(local, comp)
    return {"local": local}


def merge_days(a, b):
    """Sum two per-day records for the same date (local + factory). Pure."""
    projects = collections.Counter()
    for p in (a.get("topProjects") or []) + (b.get("topProjects") or []):
        projects[p["project"]] += p["total"]
    sample = sorted((a.get("sampleSessions") or []) + (b.get("sampleSessions") or []),
                    key=lambda x: -x["total"])[:5]
    return {
        "date": a.get("date") or b.get("date"),
        "events": a.get("events", 0) + b.get("events", 0),
        "msgs": a.get("msgs", 0) + b.get("msgs", 0),
        "byModel": _sum_map(a.get("byModel"), b.get("byModel")),
        "mainVsSub": _sum_map(a.get("mainVsSub"), b.get("mainVsSub")),
        "bySource": _sum_map(_by_source(a), _by_source(b)),
        "topProjects": [{"project": p, "total": t} for p, t in projects.most_common(5)],
        "sampleSessions": sample,
    }


def _stitch(ledger_path, live_days, today):
    days_by_date = {d: rec for d, rec in read_ledger(ledger_path).items() if d < today}
    for rec in live_days:
        days_by_date[rec["date"]] = rec
    return days_by_date


def assemble_rollup(ledger_path, root, tz_name, today, prices=None,
                    factory_ledger_path=None, factory_root=None, memo=None):
    """Merge finalized archive days (date < today, authoritative) with a live
    parse of `today`, for local Claude Code stores and (optionally) the remote
    factory runners' separate archive. Returns the dashboard payload {meta, days}.
    `memo` (a `parse.SessionMemo`) shares the live parse with other passes and
    requests; it never changes the result."""
    if prices is None:
        prices = load_prices()
    live_days, live_meta = day_records(root, tz_name, since=today, until=today, prices=prices,
                                       memo=memo)
    days_by_date = _stitch(ledger_path, live_days, today)
    if factory_ledger_path or factory_root:
        fac_live, fac_meta = ([], {}) if not factory_root else factory_day_records(
            factory_root, tz_name, since=today, until=today, prices=prices, memo=memo)
        fac = _stitch(factory_ledger_path or "", fac_live, today)
        for d, rec in fac.items():
            days_by_date[d] = merge_days(days_by_date[d], rec) if d in days_by_date else rec
        live_meta = dict(live_meta, unpriced_models=sorted(
            set(live_meta.get("unpriced_models", [])) | set(fac_meta.get("unpriced_models", []))))
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
