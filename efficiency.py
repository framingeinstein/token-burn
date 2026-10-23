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
with a live parse of every unfinalized day (`ledger.live_since` .. today), the
same way `ledger.assemble_rollup` stitches the snapshots ledger (spec Sec5.3 "dashboard
build time" joins) — pure with respect to its inputs; the only IO is the
filesystem/git reads `usage_records.local_usage_records` already does.
"""
import collections
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from ledger import live_since
from metric_guide import classify
from parse import _jsonl_files, _mtime_floor, _read
from prices import rate_for
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


def context_per_call(records):
    """Headline context metric: `(in + cc + cr) / calls` -- tokens read per model
    call across `records`; None when there are no calls."""
    calls = sum(r.get("calls", 0) or 0 for r in records)
    if not calls:
        return None
    ctx = sum((r.get("in", 0) or 0) + (r.get("cc", 0) or 0) + (r.get("cr", 0) or 0)
              for r in records)
    return ctx / calls


_EARLY_BUCKETS = ("1", "2-5")
_LATE_BUCKETS = ("21-50", "51+")


def context_growth_ratio(bucket_totals):
    """Weighted mean context of late calls (buckets 21-50, 51+) over early calls
    (1, 2-5); None when either side has no calls (or the early mean is 0)."""
    def mean(labels):
        calls = sum((bucket_totals.get(l) or {}).get("calls", 0) for l in labels)
        ctx = sum((bucket_totals.get(l) or {}).get("ctx", 0) for l in labels)
        return ctx / calls if calls else None
    early, late = mean(_EARLY_BUCKETS), mean(_LATE_BUCKETS)
    if not early or late is None:
        return None
    return late / early


def cost_mix(records, prices):
    """$ and share of $ by token kind, priced exactly as `parse.cost_usd` does
    (records only carry a total `cost_usd`): input, cache write (`write_mult`),
    cache read (`read_mult`) at the model's input rate, output at its output
    rate. Unpriced models contribute $0. Shares are None when total $ is 0."""
    usd = {"cache_read": 0.0, "cache_write": 0.0, "output": 0.0, "input": 0.0}
    for r in records:
        rate = rate_for(prices, r.get("model"), r.get("day"))
        if rate is None:
            continue
        usd["input"] += (r.get("in", 0) or 0) * rate["in"] / 1e6
        usd["output"] += (r.get("out", 0) or 0) * rate["out"] / 1e6
        usd["cache_write"] += (r.get("cc", 0) or 0) * rate["in"] * rate["write_mult"] / 1e6
        usd["cache_read"] += (r.get("cr", 0) or 0) * rate["in"] * rate["read_mult"] / 1e6
    total = sum(usd.values())
    mix = {k: {"usd": v, "share": (v / total) if total else None}
           for k, v in usd.items()}
    mix["total_usd"] = total  # parts unrounded so they sum to the total exactly
    return mix


def daily_series(records, live_days=()):
    """One row per day: totals, cache hit rate and output share for that day;
    a day in `live_days` (parsed live, not yet frozen) carries `"live": true`."""
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
            **({"live": True} if day in live_days else {}),
        })
    return series


def build_efficiency_payload(records, prices=None, guide=None, guide_error=None,
                             live_days=(), test_run_records=None):
    """The `efficiency` top-level payload key (Ruling R3): cache hit rate and
    output share (overall + per day), the context-growth curve, spend, and the
    "100% of spend" coverage this section always has by construction (spec
    Sec3A) — model fit is NOT built here (moved to T5).

    Additive (metric guide): `context_per_call`, `context_growth_ratio`, and —
    when `prices` is given — `cost_mix`; `verdicts` when a `guide` is loaded
    (else `verdicts_unavailable` carries `guide_error`). `test_run_records`
    (factory test-run folders, already excluded from `records`) are reported
    only as `factory_test_runs_usd`, so excluded spend never silently vanishes."""
    totals = sum_totals(records)
    buckets = sum_ctx_buckets(records)
    payload = {
        "coverage_pct": 100.0,
        "spend_usd": round(spend_usd(records), 6),
        "cache_hit_rate": cache_hit_rate(totals),
        "output_share": output_share(totals),
        "days": daily_series(records, set(live_days)),
        "context_growth": context_growth_curve(buckets),
        "context_per_call": context_per_call(records),
        "context_growth_ratio": context_growth_ratio(buckets),
    }
    if test_run_records is not None:
        payload["factory_test_runs_usd"] = round(spend_usd(test_run_records), 6)
    if prices is not None:
        payload["cost_mix"] = cost_mix(records, prices)
    if guide is not None:
        payload["verdicts"] = {
            mid: classify(guide, mid, payload[key]) for mid, key in (
                ("cache_hit_rate", "cache_hit_rate"),
                ("context_per_call", "context_per_call"),
                ("context_growth", "context_growth_ratio"),
                ("output_share", "output_share"))}
    elif guide_error:
        payload["verdicts_unavailable"] = guide_error
    return payload


# --- assembly: finalized archive + live parse of every unfinalized day -------

def _archive_days(usage_dir):
    if not usage_dir or not Path(usage_dir).is_dir():
        return []
    return [p.stem for p in Path(usage_dir).glob("*.jsonl")]


def usage_live_since(usage_dir, today):
    """First live day for the usage-record pass: the day after the archive's
    last finalized usage/<day>.jsonl (capped, `ledger.live_since`)."""
    return live_since(_archive_days(usage_dir), today)


def _finalized_usage_records(usage_dir, since):
    records = []
    for day in _archive_days(usage_dir):
        if day < since:
            records.extend(read_usage_records(usage_dir, day))
    return records


def _live_usage_records(roots, factory_root, tz_name, prices, actor, since, today,
                        repo_map=None, run=None, repo_cache=None, memo=None,
                        factory_test_folders=None):
    """`memo` (a `parse.SessionMemo`) shares one parse per file with the
    token/cost rollup's live pass and across requests; `repo_cache` (a
    `usage_records.RepoCache`) keeps git to one call per cwd (final review I3)."""
    run = run or subprocess.run
    tz = ZoneInfo(tz_name)
    floor = _mtime_floor(since, tz)
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
                                           repo_cache=repo_cache, since=since, until=today,
                                           recs=recs))
    if factory_root and Path(factory_root).is_dir():
        records.extend(factory_usage_records(factory_root, tz_name, prices, repo_map=repo_map,
                                             since=since, until=today, memo=memo,
                                             test_folders=factory_test_folders))
    return records


def collect_usage_records(usage_dir, roots, tz_name, prices, actor, today,
                          factory_root=None, repo_map=None, run=None, memo=None,
                          factory_test_folders=None):
    """Usage records for the efficiency payload: finalized usage/<day>.jsonl days
    (before the live window, authoritative) plus a live parse of every
    unfinalized day (`usage_live_since` .. today) — the same stitch
    `ledger.assemble_rollup` does for the token/cost rollup. `actor` is None when
    no GitHub login is configured; the live pass is skipped (nothing to
    attribute it to) but the finalized archive still reads."""
    since = usage_live_since(usage_dir, today)
    records = _finalized_usage_records(usage_dir, since)
    if actor:
        repo_cache = shared_repo_cache(usage_dir)
        records.extend(_live_usage_records(
            roots, factory_root, tz_name, prices, actor, since, today, repo_map=repo_map,
            run=run, repo_cache=repo_cache, memo=memo,
            factory_test_folders=factory_test_folders))
        repo_cache.save()
    return records
