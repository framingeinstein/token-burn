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
