# tests/test_ledger.py
import json
from ledger import read_ledger, append_day, today_str

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
    assert len(today_str("UTC")) == 10 and today_str("UTC")[4] == "-"
