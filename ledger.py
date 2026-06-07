"""Append-only daily snapshot archive (snapshots.jsonl) + assembly.

Archive = permanent, authoritative record. Finalized days (date < today) are
immutable; only 'today' is parsed live. One JSON object per line; on read, the
LAST line per date wins (so --refinalize just appends a newer line).
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo


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
