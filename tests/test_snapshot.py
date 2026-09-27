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


# --- C1 (final review): snapshot.py main() smoke test with --actor -----------

def test_main_with_actor_writes_ledger_and_usage_records_without_gh(tmp_path, monkeypatch):
    """The cron line passes `--actor <login>` (resolved at install time), so the
    usage-record pass must run without ever calling `gh api user`."""
    import json
    import sys
    import snapshot
    import usage_records

    def no_gh(*a, **k):
        raise AssertionError("gh api user must not be called when --actor is given")

    monkeypatch.setattr(usage_records, "default_login", no_gh)
    monkeypatch.setattr(snapshot, "load_team_config", lambda: None)
    usage_dir = tmp_path / "usage"
    monkeypatch.setattr(sys, "argv", [
        "snapshot.py", "--root", str(FIX), "--tz", "UTC",
        "--ledger", str(tmp_path / "s.jsonl"),
        "--cursor-ledger", str(tmp_path / "c.jsonl"), "--skip-cursor",
        "--factory-root", str(tmp_path / "no-factory"),
        "--factory-ledger", str(tmp_path / "f.jsonl"),
        "--usage-dir", str(usage_dir),
        "--team-upload-state", str(tmp_path / "team-state.json"),
        "--actor", "jason-m",
    ])
    snapshot.main()
    assert "2026-05-21" in read_ledger(str(tmp_path / "s.jsonl"))
    lines = (usage_dir / "2026-05-21.jsonl").read_text().splitlines()
    recs = [json.loads(l) for l in lines if l.strip()]
    assert recs and {r["actor"] for r in recs} == {"human:jason-m"}
