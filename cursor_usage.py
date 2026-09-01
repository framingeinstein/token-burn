"""Fetch Cursor usage events and retain only daily aggregate rollups."""
import argparse
import collections
import json
import os
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from parse import _default_tz_name

API_URL = "https://cursor.com/api/dashboard/get-filtered-usage-events"
DEFAULT_TOKEN_FILE = Path("~/.config/token-burn/cursor-session-token").expanduser()


def aggregate_events(events, tz_name):
    tz = ZoneInfo(tz_name)
    days = collections.defaultdict(lambda: {
        "events": 0,
        "byModel": collections.defaultdict(lambda: {
            "in": 0, "out": 0, "cc": 0, "cr": 0, "cost_usd": 0.0,
            "included_events": 0, "charged_events": 0,
        }),
    })
    for event in events:
        try:
            when = datetime.fromtimestamp(
                int(event["timestamp"]) / 1000, timezone.utc
            ).astimezone(tz)
        except (KeyError, TypeError, ValueError, OSError):
            continue
        model = event.get("model") or "(unknown)"
        tokens = event.get("tokenUsage") or {}
        bucket = days[when.date().isoformat()]
        comp = bucket["byModel"][model]
        bucket["events"] += 1
        comp["in"] += int(tokens.get("inputTokens") or 0)
        comp["out"] += int(tokens.get("outputTokens") or 0)
        comp["cc"] += int(tokens.get("cacheWriteTokens") or 0)
        comp["cr"] += int(tokens.get("cacheReadTokens") or 0)
        comp["cost_usd"] += float(event.get("chargedCents") or 0) / 100
        if event.get("kind") == "USAGE_EVENT_KIND_INCLUDED_IN_BUSINESS":
            comp["included_events"] += 1
        if float(event.get("chargedCents") or 0) > 0:
            comp["charged_events"] += 1

    result = []
    for day in sorted(days):
        bucket = days[day]
        for comp in bucket["byModel"].values():
            comp["cost_usd"] = round(comp["cost_usd"], 6)
        result.append({
            "date": day,
            "events": bucket["events"],
            "byModel": dict(bucket["byModel"]),
        })
    return result


def append_usage_days(path, days):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for day in days:
            fh.write(json.dumps(day, separators=(",", ":")) + "\n")


def load_cursor_usage(path):
    path = Path(path)
    if not path.exists():
        return {
            "status": "unavailable",
            "reason": "Cursor usage has not been captured yet",
            "days": [],
            "totals": {
                "events": 0,
                "included_events": 0,
                "charged_events": 0,
                "cost_usd": 0.0,
            },
        }
    by_date = {}
    with path.open(errors="ignore") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("date"):
                by_date[record["date"]] = record
    days = [by_date[day] for day in sorted(by_date)]
    return {
        "status": "ok",
        "days": days,
        "totals": {
            "events": sum(day.get("events", 0) for day in days),
            "included_events": sum(
                comp.get("included_events", 0)
                for day in days for comp in day.get("byModel", {}).values()
            ),
            "charged_events": sum(
                comp.get("charged_events", 0)
                for day in days for comp in day.get("byModel", {}).values()
            ),
            "cost_usd": round(sum(
                comp.get("cost_usd", 0.0)
                for day in days for comp in day.get("byModel", {}).values()
            ), 6),
        },
    }


def token_from_env_or_file(environ=None, path=DEFAULT_TOKEN_FILE):
    environ = os.environ if environ is None else environ
    token = (environ.get("CURSOR_SESSION_TOKEN") or "").strip()
    if token:
        return token
    try:
        return Path(path).expanduser().read_text().strip() or None
    except OSError:
        return None


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _post_json(url, token, body):
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Cookie": f"WorkosCursorSessionToken={token}",
            "Origin": "https://cursor.com",
        },
        method="POST",
    )
    with urlopen(request, timeout=30, context=_ssl_context()) as response:
        return json.load(response)


def fetch_usage_events(token, start_ms, end_ms, page_size=100, post_json=None):
    post_json = post_json or _post_json
    events = []
    page = 1
    while True:
        payload = post_json(API_URL, token, {
            "startDate": str(start_ms),
            "endDate": str(end_ms),
            "page": page,
            "pageSize": page_size,
        })
        batch = payload.get("usageEventsDisplay") or []
        events.extend(batch)
        raw_total = payload.get("totalUsageEventsCount")
        total = int(raw_total) if raw_total not in (None, "") else None
        if not batch:
            break
        if total and len(events) >= total:
            break
        if not total and len(batch) < page_size:
            break
        page += 1
    return events


def capture_usage(token, ledger_path, tz_name, since=None, now=None, fetcher=None):
    fetcher = fetcher or fetch_usage_events
    existing = load_cursor_usage(ledger_path)
    if since is None:
        since = (
            existing["days"][-1]["date"]
            if existing["days"]
            else "2026-01-01"
        )
    start = datetime.fromisoformat(since).replace(
        tzinfo=ZoneInfo(tz_name)
    ).astimezone(timezone.utc)
    if isinstance(now, str):
        end = datetime.fromisoformat(now)
    else:
        end = now or datetime.now(timezone.utc)
    events = fetcher(
        token,
        str(int(start.timestamp() * 1000)),
        str(int(end.timestamp() * 1000)),
    )
    days = aggregate_events(events, tz_name)
    append_usage_days(ledger_path, days)
    return {"events": len(events), "days": len(days)}


def main():
    ap = argparse.ArgumentParser(description="Capture Cursor billed usage rollups.")
    ap.add_argument("--out", default=str(Path(__file__).parent / "cursor-snapshots.jsonl"))
    ap.add_argument("--tz", default=_default_tz_name())
    ap.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    ap.add_argument(
        "--since",
        default=None,
        help="YYYY-MM-DD floor; default resumes from the latest captured day",
    )
    args = ap.parse_args()

    token = token_from_env_or_file(path=args.token_file)
    if not token:
        raise SystemExit(
            "Cursor token missing: set CURSOR_SESSION_TOKEN or write "
            f"{args.token_file}"
        )
    captured = capture_usage(
        token, args.out, args.tz, since=args.since
    )
    print(
        f"cursor usage: captured {captured['events']} events "
        f"across {captured['days']} day(s)"
    )


if __name__ == "__main__":
    main()
