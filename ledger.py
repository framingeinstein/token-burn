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
