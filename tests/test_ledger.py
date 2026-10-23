# tests/test_ledger.py
import json
from pathlib import Path
from ledger import read_ledger, append_day, today_str, assemble_rollup
from prices import load_prices

def test_append_and_read_roundtrip(tmp_path):
    p = tmp_path / "snap.jsonl"
    append_day(str(p), {"date": "2026-05-21", "msgs": 2})
    append_day(str(p), {"date": "2026-05-22", "msgs": 3})
    led = read_ledger(str(p))
    assert set(led) == {"2026-05-21", "2026-05-22"}
    assert led["2026-05-22"]["msgs"] == 3

def test_read_ledger_latest_wins_for_duplicate_date(tmp_path):
    p = tmp_path / "snap.jsonl"
    append_day(str(p), {"date": "2026-05-21", "rates_version": "old"})
    append_day(str(p), {"date": "2026-05-21", "rates_version": "new"})
    assert read_ledger(str(p))["2026-05-21"]["rates_version"] == "new"

def test_read_ledger_skips_corrupt_lines(tmp_path):
    p = tmp_path / "snap.jsonl"
    p.write_text('{"date":"2026-05-21","msgs":1}\nnot json\n{"date":"2026-05-22"\n')
    led = read_ledger(str(p))
    assert set(led) == {"2026-05-21"}

def test_read_ledger_missing_file_is_empty(tmp_path):
    assert read_ledger(str(tmp_path / "nope.jsonl")) == {}

def test_today_str_is_iso_date():
    from datetime import date
    s = today_str("UTC")
    assert date.fromisoformat(s).isoformat() == s   # exact YYYY-MM-DD round-trip


def test_assemble_merges_ledger_and_live_today(tmp_path):
    # ledger has a finalized past day; logs (fixtures) are all dated 2026-05-21
    p = tmp_path / "snap.jsonl"
    append_day(str(p), {"date": "2026-05-10", "events": 9, "msgs": 4,
                        "byModel": {"opus": {"in": 0, "out": 7, "cc": 0, "cr": 0, "cost_usd": 1.0}},
                        "mainVsSub": {"main": {"in":0,"out":7,"cc":0,"cr":0,"cost_usd":1.0},
                                      "sub": {"in":0,"out":0,"cc":0,"cr":0,"cost_usd":0.0}},
                        "topProjects": [], "sampleSessions": []})
    fixtures = Path(__file__).parent / "fixtures"
    roll = assemble_rollup(str(p), fixtures, "UTC", today="2026-05-21", prices=load_prices())
    dates = [d["date"] for d in roll["days"]]
    assert "2026-05-10" in dates           # from ledger
    assert "2026-05-21" in dates           # live today (fixtures)
    assert roll["meta"]["unique_messages"] == 4 + 2   # ledger msgs + live msgs
    assert roll["meta"]["rates_version"] == load_prices()["version"]

def test_assemble_ledger_wins_over_lingering_logs(tmp_path):
    # a ledger entry dated 2026-05-21 (== fixtures day) but with date < today
    # must be used verbatim; live parse only runs for `today`, so no double-count.
    p = tmp_path / "snap.jsonl"
    append_day(str(p), {"date": "2026-05-21", "events": 1, "msgs": 1,
                        "byModel": {"opus": {"in":0,"out":999,"cc":0,"cr":0,"cost_usd":0.0}},
                        "mainVsSub": {"main": {"in":0,"out":999,"cc":0,"cr":0,"cost_usd":0.0},
                                      "sub": {"in":0,"out":0,"cc":0,"cr":0,"cost_usd":0.0}},
                        "topProjects": [], "sampleSessions": []})
    fixtures = Path(__file__).parent / "fixtures"
    roll = assemble_rollup(str(p), fixtures, "UTC", today="2026-06-01", prices=load_prices())
    day = next(d for d in roll["days"] if d["date"] == "2026-05-21")
    assert day["byModel"]["opus"]["out"] == 999   # ledger value, NOT re-parsed 100

def test_assemble_empty_ledger_is_today_only(tmp_path):
    fixtures = Path(__file__).parent / "fixtures"
    roll = assemble_rollup(str(tmp_path / "none.jsonl"), fixtures, "UTC", today="2026-05-21", prices=load_prices())
    assert [d["date"] for d in roll["days"]] == ["2026-05-21"]


# --- every unfinalised day is live (not just today) -------------------------

import pytest
from ledger import live_since, LIVE_DAYS_CAP

_ZC = {"in": 0, "out": 0, "cc": 0, "cr": 0, "cost_usd": 0.0}


def _frozen(date, out=1):
    comp = dict(_ZC, out=out)
    return {"date": date, "events": 1, "msgs": 1, "byModel": {"opus": comp},
            "mainVsSub": {"main": comp, "sub": dict(_ZC)},
            "topProjects": [], "sampleSessions": []}


def _live_log(root, day, mid, out=10, name="s"):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}-{mid}.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": f"{day}T12:00:00Z", "cwd": "/x/proj",
        "message": {"id": mid, "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 1, "output_tokens": out}}}) + "\n")


def test_live_since_is_day_after_last_finalised():
    assert live_since(["2026-10-19", "2026-10-20"], "2026-10-22") == "2026-10-21"
    assert live_since(["2026-10-21"], "2026-10-22") == "2026-10-22"


def test_live_since_ignores_ledger_days_on_or_after_today():
    assert live_since(["2026-10-20", "2026-10-22"], "2026-10-22") == "2026-10-21"


def test_live_since_capped_at_seven_days():
    assert LIVE_DAYS_CAP == 7
    assert live_since(["2026-09-01"], "2026-10-22") == "2026-10-16"   # 16..22 = 7 days


def test_live_since_empty_ledger_is_today_only():
    assert live_since([], "2026-10-22") == "2026-10-22"


def test_assemble_ledger_two_days_back_parses_yesterday_and_today_live(tmp_path):
    p = tmp_path / "snap.jsonl"
    append_day(str(p), _frozen("2026-10-20"))
    logs = tmp_path / "logs"
    _live_log(logs, "2026-10-21", "y1")
    _live_log(logs, "2026-10-22", "t1")
    roll = assemble_rollup(str(p), [logs], "UTC", today="2026-10-22", prices=load_prices())
    by = {d["date"]: d for d in roll["days"]}
    assert set(by) == {"2026-10-20", "2026-10-21", "2026-10-22"}
    assert by["2026-10-21"]["live"] is True and by["2026-10-22"]["live"] is True
    assert "live" not in by["2026-10-20"]
    assert by["2026-10-21"]["byModel"]["sonnet"]["out"] == 10
    assert roll["meta"]["live_days"] == ["2026-10-21", "2026-10-22"]


def test_assemble_ledger_yesterday_frozen_only_today_live(tmp_path):
    p = tmp_path / "snap.jsonl"
    append_day(str(p), _frozen("2026-10-21", out=999))
    logs = tmp_path / "logs"
    _live_log(logs, "2026-10-21", "y1")
    _live_log(logs, "2026-10-22", "t1")
    roll = assemble_rollup(str(p), [logs], "UTC", today="2026-10-22", prices=load_prices())
    by = {d["date"]: d for d in roll["days"]}
    assert by["2026-10-21"]["byModel"]["opus"]["out"] == 999       # frozen, verbatim
    assert "live" not in by["2026-10-21"]
    assert by["2026-10-22"]["live"] is True
    assert roll["meta"]["live_days"] == ["2026-10-22"]


def test_assemble_live_window_respects_cap(tmp_path):
    p = tmp_path / "snap.jsonl"
    append_day(str(p), _frozen("2026-10-01"))
    logs = tmp_path / "logs"
    _live_log(logs, "2026-10-10", "old")        # outside the 7-day cap
    _live_log(logs, "2026-10-16", "in")
    roll = assemble_rollup(str(p), [logs], "UTC", today="2026-10-22", prices=load_prices())
    dates = [d["date"] for d in roll["days"]]
    assert "2026-10-10" not in dates and "2026-10-16" in dates


def test_assemble_factory_ledger_has_its_own_live_window(tmp_path):
    local, fac = tmp_path / "l.jsonl", tmp_path / "f.jsonl"
    append_day(str(local), _frozen("2026-10-21"))
    append_day(str(fac), dict(_frozen("2026-10-20"), bySource={"factory:hub": dict(_ZC, out=1)}))
    empty = tmp_path / "empty"; empty.mkdir()
    froot = tmp_path / "fac" / "hub" / "9" / "wb-hub-implement-abcde"
    _live_log(froot, "2026-10-21", "f1")
    roll = assemble_rollup(str(local), [empty], "UTC", "2026-10-22", prices=load_prices(),
                           factory_ledger_path=str(fac), factory_root=tmp_path / "fac")
    by = {d["date"]: d for d in roll["days"]}
    # yesterday: local frozen + factory live -> merged day is live
    assert by["2026-10-21"]["live"] is True
    assert by["2026-10-21"]["bySource"]["factory:hub"]["out"] == 10
    assert by["2026-10-21"]["bySource"]["local"]["out"] == 1
    assert "live" not in by["2026-10-20"]
