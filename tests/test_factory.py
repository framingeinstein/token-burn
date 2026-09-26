# tests/test_factory.py — remote factory runner transcripts (GCS mirror)
import json
from pathlib import Path

from prices import load_prices
from parse import day_records, factory_day_records
from ledger import merge_days, assemble_rollup, append_day

PRICES = load_prices()


def _session(path, msg_ids, out=10, day="2026-09-25", cwd="/tmp/wt-7"):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "user", "timestamp": f"{day}T11:00:00Z",
                         "message": {"role": "user", "content": "implement issue 7"}})]
    for mid in msg_ids:
        lines.append(json.dumps({"type": "assistant", "timestamp": f"{day}T12:00:00Z", "cwd": cwd,
                                 "message": {"id": mid, "model": "claude-sonnet-4-6",
                                             "usage": {"input_tokens": 1, "output_tokens": out}}}))
    path.write_text("\n".join(lines) + "\n")


def test_factory_attributes_by_runner_path_not_cwd(tmp_path):
    _session(tmp_path / "lattice" / "7" / "wb-impl-lattice-abc" / "s1.jsonl", ["m1"])
    _session(tmp_path / "nexus" / "7" / "wb-impl-nexus-def" / "s2.jsonl", ["m2"], out=5)
    days, meta = factory_day_records(tmp_path, "UTC", prices=PRICES)
    d = days[0]
    assert {p["project"] for p in d["topProjects"]} == {"factory:lattice", "factory:nexus"}
    assert set(d["bySource"]) == {"factory:lattice", "factory:nexus"}
    assert d["bySource"]["factory:lattice"]["out"] == 10
    assert any("#7" in s["label"] for s in d["sampleSessions"])


def test_factory_dedupes_reuploaded_messages_across_executions(tmp_path):
    _session(tmp_path / "hub" / "3" / "exec-a" / "s.jsonl", ["m1", "m2"])
    _session(tmp_path / "hub" / "3" / "exec-b" / "s.jsonl", ["m1", "m2", "m3"])  # re-run re-uploads
    days, meta = factory_day_records(tmp_path, "UTC", prices=PRICES)
    assert meta["unique_messages"] == 3
    assert days[0]["bySource"]["factory:hub"]["out"] == 30


def test_local_days_carry_local_source(tmp_path):
    _session(tmp_path / "proj" / "s.jsonl", ["m1"], cwd="/x/proj")
    days, _ = day_records(tmp_path, "UTC", prices=PRICES)
    assert set(days[0]["bySource"]) == {"local"}


def _comp(out, cost):
    return {"in": 0, "out": out, "cc": 0, "cr": 0, "cost_usd": cost}


def test_merge_days_sums_components_and_backfills_legacy_source():
    legacy = {"date": "2026-09-25", "events": 2, "msgs": 1, "byModel": {"opus": _comp(5, 1.0)},
              "mainVsSub": {"main": _comp(5, 1.0), "sub": _comp(0, 0)},
              "topProjects": [{"project": "loom", "total": 5}],
              "sampleSessions": [{"label": "a", "project": "loom", "total": 5}]}
    fac = {"date": "2026-09-25", "events": 1, "msgs": 1, "byModel": {"sonnet": _comp(9, 2.0)},
           "mainVsSub": {"main": _comp(9, 2.0), "sub": _comp(0, 0)},
           "bySource": {"factory:hub": _comp(9, 2.0)},
           "topProjects": [{"project": "factory:hub", "total": 9}],
           "sampleSessions": [{"label": "b", "project": "factory:hub", "total": 9}]}
    m = merge_days(legacy, fac)
    assert m["events"] == 3 and m["msgs"] == 2
    assert m["byModel"]["opus"]["out"] == 5 and m["byModel"]["sonnet"]["out"] == 9
    assert m["mainVsSub"]["main"]["cost_usd"] == 3.0
    assert m["bySource"]["local"]["out"] == 5 and m["bySource"]["factory:hub"]["out"] == 9
    assert [p["project"] for p in m["topProjects"]] == ["factory:hub", "loom"]
    assert legacy["byModel"]["opus"]["out"] == 5            # inputs not mutated


def test_assemble_rollup_merges_factory_ledger(tmp_path):
    local_led, fac_led = tmp_path / "l.jsonl", tmp_path / "f.jsonl"
    empty = tmp_path / "empty"; empty.mkdir()
    append_day(str(local_led), {"date": "2026-09-24", "events": 1, "msgs": 1,
                                "byModel": {"opus": _comp(5, 1.0)},
                                "mainVsSub": {"main": _comp(5, 1.0), "sub": _comp(0, 0)},
                                "topProjects": [], "sampleSessions": []})
    append_day(str(fac_led), {"date": "2026-09-24", "events": 1, "msgs": 1,
                              "byModel": {"sonnet": _comp(7, 2.0)},
                              "mainVsSub": {"main": _comp(7, 2.0), "sub": _comp(0, 0)},
                              "bySource": {"factory:hub": _comp(7, 2.0)},
                              "topProjects": [], "sampleSessions": []})
    _session(tmp_path / "fac" / "hub" / "9" / "e" / "s.jsonl", ["t1"], day="2026-09-25")
    roll = assemble_rollup(str(local_led), [empty], "UTC", "2026-09-25", prices=PRICES,
                           factory_ledger_path=str(fac_led), factory_root=tmp_path / "fac")
    by = {d["date"]: d for d in roll["days"]}
    assert by["2026-09-24"]["bySource"]["factory:hub"]["out"] == 7
    assert by["2026-09-24"]["bySource"]["local"]["out"] == 5
    assert by["2026-09-25"]["bySource"]["factory:hub"]["out"] == 10   # today, live
    live = by["2026-09-25"]["byModel"]["sonnet"]["cost_usd"]
    assert abs(roll["meta"]["totals"]["cost_usd"] - (3.0 + live)) < 1e-9
