# tests/test_snapshot.py
from pathlib import Path
from prices import load_prices
from ledger import read_ledger
from snapshot import run, _prev_day, _next_day

FIX = Path(__file__).parent / "fixtures"   # all logs dated 2026-05-21 (UTC)
P = load_prices()

def test_prev_day():
    assert _prev_day("2026-06-01") == "2026-05-31"
    assert _prev_day("2026-05-01") == "2026-04-30"
    assert _prev_day("2026-01-01") == "2025-12-31"   # year boundary


def test_next_day():
    assert _next_day("2025-12-31") == "2026-01-01"   # year boundary
    assert _next_day("2026-02-28") == "2026-03-01"
    assert _next_day("2024-02-29") == "2024-03-01"   # leap year

def test_finalizes_completed_day_not_today(tmp_path):
    led = str(tmp_path / "s.jsonl")
    # today = 2026-05-22 → 2026-05-21 is complete and should be frozen
    added, days, meta, existing = run(FIX, "UTC", led, P, today="2026-05-22")
    assert added == 1
    rec = read_ledger(led)["2026-05-21"]
    assert rec["msgs"] == 2 and rec["rates_version"] == P["version"]
    assert "finalized_at" in rec and rec["tz"] == "UTC"

def test_today_is_never_finalized(tmp_path):
    led = str(tmp_path / "s.jsonl")
    # today == the fixtures' day → nothing to finalize
    added, *_ = run(FIX, "UTC", led, P, today="2026-05-21")
    assert added == 0 and read_ledger(led) == {}

def test_idempotent_second_run_adds_zero(tmp_path):
    led = str(tmp_path / "s.jsonl")
    run(FIX, "UTC", led, P, today="2026-05-22")
    added, *_ = run(FIX, "UTC", led, P, today="2026-05-22")
    assert added == 0

def test_refinalize_reappends_and_latest_wins(tmp_path):
    led = str(tmp_path / "s.jsonl")
    run(FIX, "UTC", led, P, today="2026-05-22")
    added, *_ = run(FIX, "UTC", led, P, today="2026-05-22", refinalize=True)
    assert added == 1
    # still one logical day after dedup-on-read
    assert set(read_ledger(led)) == {"2026-05-21"}
