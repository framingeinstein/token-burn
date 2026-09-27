# tests/test_efficiency.py — efficiency-section formulas (spec Sec3A, Sec6.2; Ruling R1: mean not median)
import json
from pathlib import Path

from prices import load_prices
from usage_records import append_usage_records
from efficiency import (
    CTX_BUCKET_ORDER,
    build_efficiency_payload,
    cache_hit_rate,
    collect_usage_records,
    context_growth_curve,
    daily_series,
    output_share,
    spend_usd,
    sum_ctx_buckets,
    sum_totals,
)

PRICES = load_prices()


def _rec(day="2026-05-21", session="s1", in_=0, out=0, cc=0, cr=0, cost=0.0, buckets=None,
        actor="human:x"):
    return {
        "v": 1, "day": day, "actor": actor, "on_behalf_of": None, "requester_source": None,
        "repo": None, "branch": None, "issue": None, "session": session,
        "model": "claude-opus-4-8", "model_class": "opus", "kind": "interactive",
        "calls": sum(b["calls"] for b in (buckets or {}).values()) or 1,
        "in": in_, "out": out, "cc": cc, "cr": cr, "cost_usd": cost,
        "ctx_buckets": buckets or {},
    }


# --- cache hit rate: cr / (cr + cc + in) ---

def test_cache_hit_rate_hand_computed():
    assert cache_hit_rate({"in": 10, "out": 0, "cc": 20, "cr": 70}) == 0.7


def test_cache_hit_rate_zero_tokens_is_none():
    assert cache_hit_rate({"in": 0, "out": 0, "cc": 0, "cr": 0}) is None


# --- output share: out / (in + out + cc + cr) ---

def test_output_share_hand_computed():
    assert output_share({"in": 10, "out": 30, "cc": 20, "cr": 40}) == 0.3


def test_output_share_zero_tokens_is_none():
    assert output_share({"in": 0, "out": 0, "cc": 0, "cr": 0}) is None


# --- sum_totals / sum_ctx_buckets: pure aggregation across records ---

def test_sum_totals_across_records():
    recs = [_rec(in_=1, out=2, cc=3, cr=4), _rec(in_=10, out=20, cc=30, cr=40)]
    assert sum_totals(recs) == {"in": 11, "out": 22, "cc": 33, "cr": 44}


def test_sum_ctx_buckets_across_records():
    recs = [
        _rec(buckets={"1": {"calls": 1, "ctx": 100}}),
        _rec(buckets={"1": {"calls": 3, "ctx": 30}, "2-5": {"calls": 2, "ctx": 20}}),
    ]
    totals = sum_ctx_buckets(recs)
    assert totals["1"] == {"calls": 4, "ctx": 130}
    assert totals["2-5"] == {"calls": 2, "ctx": 20}


# --- context growth curve: Ruling R1 — mean = sum(ctx) / sum(calls), NOT mean-of-means ---

def test_context_growth_curve_is_weighted_mean_not_average_of_per_record_means():
    # record A: bucket "1", 1 call, ctx 100 -> per-record mean 100
    # record B: bucket "1", 3 calls, ctx 30  -> per-record mean 10
    # naive mean-of-means would be (100+10)/2 = 55; Ruling R1 wants sum/sum = 130/4 = 32.5
    recs = [
        _rec(buckets={"1": {"calls": 1, "ctx": 100}}),
        _rec(buckets={"1": {"calls": 3, "ctx": 30}}),
    ]
    curve = context_growth_curve(sum_ctx_buckets(recs))
    by_bucket = {c["bucket"]: c for c in curve}
    assert by_bucket["1"]["mean_ctx"] == 32.5
    assert by_bucket["1"]["calls"] == 4
    assert by_bucket["1"]["ctx"] == 130


def test_context_growth_curve_orders_all_five_buckets_even_when_absent():
    curve = context_growth_curve(sum_ctx_buckets([_rec(buckets={"6-20": {"calls": 2, "ctx": 40}})]))
    assert [c["bucket"] for c in curve] == list(CTX_BUCKET_ORDER)
    assert [c["bucket"] for c in curve] == ["1", "2-5", "6-20", "21-50", "51+"]
    by_bucket = {c["bucket"]: c for c in curve}
    assert by_bucket["6-20"]["mean_ctx"] == 20
    assert by_bucket["1"]["mean_ctx"] is None       # no calls in this bucket -> undefined, not 0
    assert by_bucket["1"]["calls"] == 0


# --- daily series: one row per day, cache hit rate / output share per day ---

def test_daily_series_hand_computed_two_days():
    recs = [
        _rec(day="2026-05-20", in_=10, out=10, cc=0, cr=80),   # hit=80/90, share=10/100
        _rec(day="2026-05-21", in_=1, out=9, cc=0, cr=0),      # hit=0/1, share=9/10
    ]
    series = daily_series(recs)
    assert [d["date"] for d in series] == ["2026-05-20", "2026-05-21"]
    assert round(series[0]["cache_hit_rate"], 6) == round(80 / 90, 6)
    assert round(series[0]["output_share"], 2) == 0.1
    assert series[1]["cache_hit_rate"] == 0.0
    assert round(series[1]["output_share"], 1) == 0.9


def test_daily_series_zero_token_day_is_none_not_crash():
    recs = [_rec(day="2026-05-22", in_=0, out=0, cc=0, cr=0)]
    series = daily_series(recs)
    assert series[0]["cache_hit_rate"] is None
    assert series[0]["output_share"] is None


# --- spend_usd: sum of cost_usd ---

def test_spend_usd_sums_cost():
    recs = [_rec(cost=1.5), _rec(cost=2.25)]
    assert spend_usd(recs) == 3.75


# --- build_efficiency_payload: the whole `efficiency` shape (Ruling R3) ---

def test_build_efficiency_payload_shape_and_values():
    recs = [
        _rec(day="2026-05-20", in_=10, out=10, cc=0, cr=80, cost=1.0,
             buckets={"1": {"calls": 1, "ctx": 100}}),
        _rec(day="2026-05-21", in_=1, out=9, cc=0, cr=0, cost=0.5,
             buckets={"1": {"calls": 3, "ctx": 30}}),
    ]
    payload = build_efficiency_payload(recs)
    assert payload["coverage_pct"] == 100.0          # spec Sec3A: exact, 100% of spend
    assert payload["spend_usd"] == 1.5
    assert round(payload["cache_hit_rate"], 4) == round(80 / (80 + 0 + 11), 4)
    assert round(payload["output_share"], 4) == round(19 / (11 + 19 + 0 + 80), 4)
    assert len(payload["days"]) == 2
    assert [c["bucket"] for c in payload["context_growth"]] == list(CTX_BUCKET_ORDER)
    by_bucket = {c["bucket"]: c for c in payload["context_growth"]}
    assert by_bucket["1"]["mean_ctx"] == 32.5


def test_build_efficiency_payload_empty_records_degrades_gracefully():
    payload = build_efficiency_payload([])
    assert payload["coverage_pct"] == 100.0
    assert payload["spend_usd"] == 0.0
    assert payload["cache_hit_rate"] is None
    assert payload["output_share"] is None
    assert payload["days"] == []
    assert all(c["mean_ctx"] is None for c in payload["context_growth"])


# --- collect_usage_records: stitches finalized archive (day < today) + live today ---

def test_collect_usage_records_stitches_finalized_and_live_today(tmp_path):
    usage_dir = tmp_path / "usage"
    finalized = _rec(day="2026-05-10", session="old-session")
    append_usage_records(usage_dir, "2026-05-10", [finalized])
    fixtures = Path(__file__).parent / "fixtures"
    records = collect_usage_records(usage_dir, fixtures, "UTC", PRICES, "human:x",
                                    today="2026-05-21")
    days = {r["day"] for r in records}
    assert days == {"2026-05-10", "2026-05-21"}
    assert any(r["session"] == "old-session" for r in records)


def test_collect_usage_records_ignores_archived_today_uses_live_instead(tmp_path):
    # a record archived FOR today (e.g. stale re-run) must not double up with the
    # live parse of today — only days strictly before today are read from the archive.
    usage_dir = tmp_path / "usage"
    stale_today = _rec(day="2026-05-21", session="stale")
    append_usage_records(usage_dir, "2026-05-21", [stale_today])
    fixtures = Path(__file__).parent / "fixtures"
    records = collect_usage_records(usage_dir, fixtures, "UTC", PRICES, "human:x",
                                    today="2026-05-21")
    assert "stale" not in {r["session"] for r in records}


def test_collect_usage_records_no_actor_skips_live_but_keeps_archive(tmp_path):
    usage_dir = tmp_path / "usage"
    finalized = _rec(day="2026-05-10", session="old-session")
    append_usage_records(usage_dir, "2026-05-10", [finalized])
    fixtures = Path(__file__).parent / "fixtures"
    records = collect_usage_records(usage_dir, fixtures, "UTC", PRICES, None,
                                    today="2026-05-21")
    assert {r["day"] for r in records} == {"2026-05-10"}


def test_collect_usage_records_missing_usage_dir_is_empty_archive(tmp_path):
    fixtures = Path(__file__).parent / "fixtures"
    records = collect_usage_records(tmp_path / "no-such-dir", fixtures, "UTC", PRICES,
                                    "human:x", today="2026-05-21")
    assert {r["day"] for r in records} == {"2026-05-21"}


def test_collect_usage_records_includes_live_factory_today(tmp_path):
    factory_root = tmp_path / "factory"
    sess = factory_root / "lattice" / "71" / "exec-a" / "sess-a.jsonl"
    sess.parent.mkdir(parents=True)
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})]
    lines.append(json.dumps({
        "type": "assistant", "timestamp": "2026-05-21T00:00:00Z", "cwd": "/tmp/wt-1",
        "message": {"id": "m1", "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 2, "output_tokens": 3,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}))
    sess.write_text("\n".join(lines) + "\n")
    records = collect_usage_records(tmp_path / "usage", tmp_path / "empty-root", "UTC", PRICES,
                                    "human:x", today="2026-05-21", factory_root=factory_root)
    assert any(r["actor"] == "factory:lattice" for r in records)
