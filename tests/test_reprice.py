# tests/test_reprice.py — correcting ALREADY-FROZEN costs from usage-record tokens,
# without re-parsing raw logs (retention may have deleted them).
import json

import pytest

from ledger import append_day, read_ledger
from parse import cost_usd
from prices import load_prices
from reprice import apply_plan, main, plan_reprice
from usage_records import append_usage_records, read_usage_day, usage_path

NEW = load_prices()
OLD_OPUS_55 = {"in": 5.0, "out": 25.0, "read_mult": 0.10, "write_mult": 1.25}   # generic opus-5


def _old_cost(inp, out, cc, cr):
    r = OLD_OPUS_55
    return round((inp * r["in"] + out * r["out"] + cc * r["in"] * r["write_mult"]
                  + cr * r["in"] * r["read_mult"]) / 1e6, 6)


def _rec(day, model, mclass, session, toks, cost, actor="human:jason"):
    inp, out, cc, cr = toks
    return {"v": 1, "day": day, "actor": actor, "on_behalf_of": None, "requester_source": None,
            "repo": None, "branch": None, "issue": None, "session": session, "model": model,
            "model_class": mclass, "kind": "interactive" if actor.startswith("human:") else "impl",
            "calls": 1, "in": inp, "out": out, "cc": cc, "cr": cr, "cost_usd": cost,
            "ctx_buckets": {"1": {"calls": 1, "ctx": inp + cc + cr}}}


def _comp(toks, cost):
    inp, out, cc, cr = toks
    return {"in": inp, "out": out, "cc": cc, "cr": cr, "cost_usd": cost}


T55 = (1000, 2000, 30000, 400000)       # opus-5-5 tokens
TS = (100, 200, 300, 4000)              # sonnet-5 tokens (unaffected model)
F55 = (10, 20, 300, 5000)               # factory opus-5-5 tokens


def _fixture(tmp_path):
    usage = tmp_path / "usage"
    led, fled = tmp_path / "snapshots.jsonl", tmp_path / "factory-snapshots.jsonl"
    d1, d2 = "2026-10-10", "2026-10-11"
    o = _old_cost(*T55)
    s = round(cost_usd(NEW, "claude-sonnet-5", d1, *TS), 6)
    f = _old_cost(*F55)
    append_usage_records(usage, d1, [
        _rec(d1, "claude-opus-5-5", "opus", "a", T55, o),
        _rec(d1, "claude-sonnet-5", "sonnet", "b", TS, s),
        _rec(d1, "claude-opus-5-5", "opus", "c", F55, f, actor="factory:factory"),
    ])
    append_usage_records(usage, d2, [_rec(d2, "claude-sonnet-5", "sonnet", "d", TS, s)])
    main_cost, sub_cost = (o + s) * 0.75, (o + s) * 0.25
    append_day(str(led), {
        "date": d1, "events": 2, "msgs": 2, "rates_version": "2026-08-13",
        "byModel": {"opus": _comp(T55, o), "sonnet": _comp(TS, s)},
        "mainVsSub": {"main": _comp((0, 0, 0, 0), main_cost), "sub": _comp((0, 0, 0, 0), sub_cost)},
        "bySource": {"local": _comp((0, 0, 0, 0), o + s)},
        "topProjects": [], "sampleSessions": []})
    append_day(str(led), {
        "date": d2, "events": 1, "msgs": 1, "rates_version": "2026-08-13",
        "byModel": {"sonnet": _comp(TS, s)},
        "mainVsSub": {"main": _comp((0, 0, 0, 0), s), "sub": _comp((0, 0, 0, 0), 0.0)},
        "bySource": {"local": _comp((0, 0, 0, 0), s)},
        "topProjects": [], "sampleSessions": []})
    append_day(str(fled), {
        "date": d1, "events": 1, "msgs": 1, "rates_version": "2026-08-13",
        "byModel": {"opus": _comp(F55, f)},
        "mainVsSub": {"main": _comp((0, 0, 0, 0), f), "sub": _comp((0, 0, 0, 0), 0.0)},
        "bySource": {"factory:factory": _comp((0, 0, 0, 0), f)},
        "topProjects": [], "sampleSessions": []})
    return usage, led, fled, (o, s, f)


def _snapshot_files(tmp_path):
    return {p.relative_to(tmp_path).as_posix(): p.read_bytes()
            for p in tmp_path.rglob("*") if p.is_file()}


def _plan(usage, led, fled, since="2026-10-01"):
    return plan_reprice(usage, led, fled, NEW, since=since)


def test_dry_run_changes_nothing_on_disk_and_reports_the_delta(tmp_path, capsys):
    usage, led, fled, (o, s, f) = _fixture(tmp_path)
    before = _snapshot_files(tmp_path)
    main(["--usage-dir", str(usage), "--ledger", str(led), "--factory-ledger", str(fled),
          "--since", "2026-10-01"])
    assert _snapshot_files(tmp_path) == before
    out = capsys.readouterr().out
    new_o = cost_usd(NEW, "claude-opus-5-5", "2026-10-10", *T55)
    assert "2026-10-10" in out and "dry run" in out.lower()
    plan = _plan(usage, led, fled)
    assert plan["totals"]["local"]["delta"] == pytest.approx(new_o - o, abs=1e-6)
    assert plan["totals"]["local"]["delta"] < 0          # opus 5.5 is cheaper than opus 5


def test_apply_writes_corrected_lines_and_backups(tmp_path):
    usage, led, fled, (o, s, f) = _fixture(tmp_path)
    new_o = cost_usd(NEW, "claude-opus-5-5", "2026-10-10", *T55)
    new_f = cost_usd(NEW, "claude-opus-5-5", "2026-10-10", *F55)
    plan = _plan(usage, led, fled)
    written = apply_plan(plan, usage, led, fled, stamp="20261022T000000Z")
    # backups of exactly the touched files, byte-identical to the originals
    assert sorted(p.name for p in written["backups"]) == sorted([
        "2026-10-10.jsonl.bak-reprice-20261022T000000Z",
        "snapshots.jsonl.bak-reprice-20261022T000000Z",
        "factory-snapshots.jsonl.bak-reprice-20261022T000000Z"])
    # usage archive: corrected lines appended only for changed records; latest wins
    recs = {r["session"]: r for r in read_usage_day(usage, "2026-10-10").values()}
    assert recs["a"]["cost_usd"] == pytest.approx(round(new_o, 6))
    assert recs["c"]["cost_usd"] == pytest.approx(round(new_f, 6))
    assert recs["b"]["cost_usd"] == pytest.approx(s)
    assert len(usage_path(usage, "2026-10-10").read_text().splitlines()) == 3 + 2
    # local ledger: opus cost corrected, tokens unchanged, sonnet untouched
    day = read_ledger(str(led))["2026-10-10"]
    assert day["byModel"]["opus"]["cost_usd"] == pytest.approx(new_o, abs=1e-6)
    assert day["byModel"]["opus"]["cr"] == T55[3]
    assert day["byModel"]["sonnet"]["cost_usd"] == pytest.approx(s)
    assert day["bySource"]["local"]["cost_usd"] == pytest.approx(new_o + s, abs=1e-6)
    assert day["rates_version"] == NEW["version"]
    # mainVsSub split preserved (75/25) and sums to the new day total
    m, sb = day["mainVsSub"]["main"]["cost_usd"], day["mainVsSub"]["sub"]["cost_usd"]
    assert m + sb == pytest.approx(new_o + s, abs=1e-6)
    assert m / (m + sb) == pytest.approx(0.75)
    # factory ledger: same, from factory records
    fday = read_ledger(str(fled))["2026-10-10"]
    assert fday["byModel"]["opus"]["cost_usd"] == pytest.approx(new_f, abs=1e-6)
    assert fday["bySource"]["factory:factory"]["cost_usd"] == pytest.approx(new_f, abs=1e-6)


def test_second_apply_is_a_no_op(tmp_path):
    usage, led, fled, _ = _fixture(tmp_path)
    apply_plan(_plan(usage, led, fled), usage, led, fled, stamp="a")
    after_first = _snapshot_files(tmp_path)
    plan = _plan(usage, led, fled)
    assert not plan["usage"] and not plan["ledger"] and not plan["factory"]
    written = apply_plan(plan, usage, led, fled, stamp="b")
    assert written["backups"] == []
    assert _snapshot_files(tmp_path) == after_first


def test_day_with_only_unaffected_models_is_untouched(tmp_path):
    usage, led, fled, _ = _fixture(tmp_path)
    plan = _plan(usage, led, fled)
    assert "2026-10-11" not in plan["usage"]
    assert "2026-10-11" not in plan["ledger"]
    apply_plan(plan, usage, led, fled, stamp="x")
    assert len(usage_path(usage, "2026-10-11").read_text().splitlines()) == 1
    assert [json.loads(l)["date"] for l in led.read_text().splitlines()].count("2026-10-11") == 1


def test_since_bounds_the_range(tmp_path):
    usage, led, fled, _ = _fixture(tmp_path)
    plan = _plan(usage, led, fled, since="2026-10-11")
    assert not plan["usage"] and not plan["ledger"] and not plan["factory"]


def test_ledger_day_already_frozen_at_current_rates_is_not_corrected_again(tmp_path):
    # e.g. `snapshot.py --refinalize` re-froze the day at the new rates: the
    # usage archive is stale but the ledger is already right.
    usage, led, fled, (o, s, f) = _fixture(tmp_path)
    fresh = dict(read_ledger(str(led))["2026-10-10"], rates_version=NEW["version"])
    append_day(str(led), fresh)
    plan = _plan(usage, led, fled)
    assert "2026-10-10" in plan["usage"]
    assert "2026-10-10" not in plan["ledger"]


def test_crash_between_ledger_and_usage_writes_does_not_double_correct(tmp_path):
    usage, led, fled, (o, s, f) = _fixture(tmp_path)
    new_o = cost_usd(NEW, "claude-opus-5-5", "2026-10-10", *T55)
    plan = _plan(usage, led, fled)
    apply_plan(dict(plan, usage={}), usage, led, fled, stamp="1")    # ledgers only
    apply_plan(_plan(usage, led, fled), usage, led, fled, stamp="2") # rerun
    day = read_ledger(str(led))["2026-10-10"]
    assert day["byModel"]["opus"]["cost_usd"] == pytest.approx(new_o, abs=1e-6)


def test_rounding_noise_is_not_a_price_change(tmp_path):
    # a stored cost is round(sum of per-call costs, 6); recomputing from summed
    # tokens can differ by ~1e-6 -- that must not produce corrections.
    usage, led = tmp_path / "usage", tmp_path / "snapshots.jsonl"
    d = "2026-10-12"
    s = round(cost_usd(NEW, "claude-sonnet-5", d, *TS), 6) + 2e-6
    append_usage_records(usage, d, [_rec(d, "claude-sonnet-5", "sonnet", "n", TS, s)])
    append_day(str(led), {"date": d, "events": 1, "msgs": 1, "rates_version": "2026-08-13",
                          "byModel": {"sonnet": _comp(TS, s)},
                          "mainVsSub": {"main": _comp((0, 0, 0, 0), s), "sub": _comp((0, 0, 0, 0), 0)},
                          "topProjects": [], "sampleSessions": []})
    plan = plan_reprice(usage, led, None, NEW)
    assert not plan["usage"] and not plan["ledger"]
