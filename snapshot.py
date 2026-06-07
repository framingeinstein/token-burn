"""Daily snapshot capture (cron entry). Idempotent + catch-up.

Finalizes every COMPLETE day (date < today, local tz) not already in the ledger.
The first run on an empty ledger backfills the full log span still on disk;
later runs add only missing days, so a missed run self-heals within retention.
  snapshot.py                                   # daily run AND first-run backfill
  snapshot.py --refinalize --since 2026-05-20   # re-freeze a date range (rate fix)
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


def _next_day(d):
    y, m, dd = map(int, d.split("-"))
    return (date(y, m, dd) + timedelta(days=1)).isoformat()


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
    ap.add_argument("--refinalize", action="store_true",
                    help="re-append fresh lines (latest wins on read) for a rate/parser fix")
    ap.add_argument("--since", default=None, help="floor date YYYY-MM-DD for --refinalize")
    args = ap.parse_args()

    prices = load_prices()
    today = today_str(args.tz)
    since = args.since  # None => full span (the default catch-up behavior)
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


if __name__ == "__main__":
    main()
