"""Efficiency section: cache hit rate, context growth and output share (spec Sec3A, Sec6.2).

Exact and 100% of spend — every number here is computed straight from usage
records (schemas/usage-record.schema.json), with no GitHub join, so unlike the
Outcomes section (Sec3C) it never has a partial-coverage caveat.

- Cache hit rate: `cr / (cr + cc + in)`.
- Output share: `out / (in + out + cc + cr)`.
- Context growth: MEAN context tokens per call, bucketed by a call's ordinal
  position within its session (Ruling R1) — `sum(ctx) / sum(calls)` across every
  record's `ctx_buckets`, not an average of each record's own per-bucket mean
  (a session with 1 call and a session with 100 calls don't count equally).

All three formulas divide by zero when there's no volume at all (a day with no
tokens, or an empty bucket); they return `None` rather than raising or
pretending 0.

`collect_usage_records` stitches finalized usage/<day>.jsonl archive days
(day < today) with a live parse of today, the same way `ledger.assemble_rollup`
stitches the snapshots ledger with a live parse of today (spec Sec5.3 "dashboard
build time" joins) — pure with respect to its inputs; the only IO is the
filesystem/git reads `usage_records.local_usage_records` already does.
"""
import collections
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from parse import _jsonl_files, _mtime_floor, _read
from usage_records import (factory_usage_records, local_usage_records, read_usage_records,
                           shared_repo_cache)

CTX_BUCKET_ORDER = ("1", "2-5", "6-20", "21-50", "51+")

_TOTAL_KEYS = ("in", "out", "cc", "cr")


def sum_totals(records):
    """{"in","out","cc","cr"} summed across `records`."""
    totals = {k: 0 for k in _TOTAL_KEYS}
    for r in records:
        for k in _TOTAL_KEYS:
            totals[k] += r.get(k, 0) or 0
    return totals


def cache_hit_rate(totals):
    """`cr / (cr + cc + in)` (spec Sec3A); None when there's no volume to rate."""
    denom = totals["cr"] + totals["cc"] + totals["in"]
    return totals["cr"] / denom if denom else None


def output_share(totals):
    """`out / (in + out + cc + cr)` (spec Sec3A); None when there's no volume."""
    denom = totals["in"] + totals["out"] + totals["cc"] + totals["cr"]
    return totals["out"] / denom if denom else None


def spend_usd(records):
    return sum(r.get("cost_usd", 0) or 0 for r in records)


def sum_ctx_buckets(records):
    """{bucket: {"calls","ctx"}} summed across every record's `ctx_buckets`."""
    totals = {label: {"calls": 0, "ctx": 0} for label in CTX_BUCKET_ORDER}
    for r in records:
        for label, b in (r.get("ctx_buckets") or {}).items():
            if label not in totals:
                continue
            totals[label]["calls"] += b.get("calls", 0) or 0
            totals[label]["ctx"] += b.get("ctx", 0) or 0
    return totals


def context_growth_curve(bucket_totals):
    """The 5-bucket curve, in call-number order, each with the Ruling-R1 mean
    (`sum(ctx) / sum(calls)`, None when the bucket has no calls at all)."""
    curve = []
    for label in CTX_BUCKET_ORDER:
        b = bucket_totals.get(label) or {"calls": 0, "ctx": 0}
        calls, ctx = b.get("calls", 0), b.get("ctx", 0)
        curve.append({
            "bucket": label, "calls": calls, "ctx": ctx,
            "mean_ctx": (ctx / calls) if calls else None,
        })
    return curve


def daily_series(records):
    """One row per day: totals, cache hit rate and output share for that day."""
    by_day = collections.defaultdict(list)
    for r in records:
        by_day[r["day"]].append(r)
    series = []
    for day in sorted(by_day):
        totals = sum_totals(by_day[day])
        series.append({
            "date": day,
            "in": totals["in"], "out": totals["out"], "cc": totals["cc"], "cr": totals["cr"],
            "cache_hit_rate": cache_hit_rate(totals),
            "output_share": output_share(totals),
        })
    return series


def build_efficiency_payload(records):
    """The `efficiency` top-level payload key (Ruling R3): cache hit rate and
    output share (overall + per day), the context-growth curve, spend, and the
    "100% of spend" coverage this section always has by construction (spec
    Sec3A) — model fit is NOT built here (moved to T5)."""
    totals = sum_totals(records)
    return {
        "coverage_pct": 100.0,
        "spend_usd": round(spend_usd(records), 6),
        "cache_hit_rate": cache_hit_rate(totals),
        "output_share": output_share(totals),
        "days": daily_series(records),
        "context_growth": context_growth_curve(sum_ctx_buckets(records)),
    }


# --- assembly: finalized archive (day < today) + live parse of today ---------

def _finalized_usage_records(usage_dir, today):
    if not usage_dir:
        return []
    usage_dir = Path(usage_dir)
    if not usage_dir.is_dir():
        return []
    records = []
    for p in usage_dir.glob("*.jsonl"):
        day = p.stem
        if day < today:
            records.extend(read_usage_records(usage_dir, day))
    return records


def _live_today_usage_records(roots, factory_root, tz_name, prices, actor, today,
                              repo_map=None, run=None, repo_cache=None, memo=None):
    """`memo` (a `parse.SessionMemo`) shares one parse per file with the
    token/cost rollup's live pass and across requests; `repo_cache` (a
    `usage_records.RepoCache`) keeps git to one call per cwd (final review I3)."""
    run = run or subprocess.run
    tz = ZoneInfo(tz_name)
    floor = _mtime_floor(today, tz)
    if repo_cache is None:
        repo_cache = {}
    records = []
    for f in _jsonl_files(roots, floor):
        if memo is not None:
            got = memo.get(f)
            if got is None:
                continue
            raw, recs = None, got[0]
        else:
            raw, recs = _read(f), None
            if raw is None:
                continue
        records.extend(local_usage_records(f, raw, tz_name, prices, actor, run=run,
                                           repo_cache=repo_cache, since=today, until=today,
                                           recs=recs))
    if factory_root and Path(factory_root).is_dir():
        records.extend(factory_usage_records(factory_root, tz_name, prices, repo_map=repo_map,
                                             since=today, until=today, memo=memo))
    return records


def collect_usage_records(usage_dir, roots, tz_name, prices, actor, today,
                          factory_root=None, repo_map=None, run=None, memo=None):
    """Usage records for the efficiency payload: finalized usage/<day>.jsonl days
    (day < today, authoritative) plus a live parse of today — the same stitch
    `ledger.assemble_rollup` does for the token/cost rollup. `actor` is None when
    no GitHub login is configured; the live-today pass is skipped (nothing to
    attribute it to) but the finalized archive still reads."""
    records = _finalized_usage_records(usage_dir, today)
    if actor:
        repo_cache = shared_repo_cache(usage_dir)
        records.extend(_live_today_usage_records(
            roots, factory_root, tz_name, prices, actor, today, repo_map=repo_map, run=run,
            repo_cache=repo_cache, memo=memo))
        repo_cache.save()
    return records
