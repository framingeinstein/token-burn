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


def test_main_passes_this_machines_actor_to_the_team_upload(tmp_path, monkeypatch):
    """I7: snapshot.py hands its resolved human actor to the team upload, which
    sends only that actor's records."""
    import sys
    import snapshot
    import usage_records

    monkeypatch.setattr(usage_records, "default_login", lambda *a, **k: None)
    monkeypatch.setattr(snapshot, "load_team_config",
                        lambda: {"door_url": "https://door.example/upload", "tenant": "t"})
    seen = {}

    def fake_upload(usage_dir, config, **kw):
        seen.update(kw)
        return {"sent": [], "refused": {}, "invalid": {}, "unreachable": {}}

    monkeypatch.setattr(snapshot, "upload_pending_days", fake_upload)
    monkeypatch.setattr(sys, "argv", [
        "snapshot.py", "--root", str(FIX), "--tz", "UTC",
        "--ledger", str(tmp_path / "s.jsonl"), "--skip-cursor", "--skip-factory",
        "--usage-dir", str(tmp_path / "usage"),
        "--team-upload-state", str(tmp_path / "team-state.json"),
        "--actor", "jason-m",
    ])
    snapshot.main()
    assert seen["actor"] == "human:jason-m"


def test_main_freezes_factory_days_without_test_runs_unless_configured(tmp_path, monkeypatch):
    import json
    import sys
    import snapshot
    import usage_records

    monkeypatch.setattr(usage_records, "default_login", lambda *a, **k: None)
    monkeypatch.setattr(snapshot, "load_team_config", lambda: None)
    froot = tmp_path / "fac"
    for rel, mid, out in (("factory/5/wb-factory-implement-ab12c/p.jsonl", "p1", 1),
                          ("runner-a/42/wb-impl-factory-p96kr/t.jsonl", "t1", 10),
                          ("sandbox/1/wb-impl-factory-zz9zz/s.jsonl", "s1", 100)):
        p = froot / rel
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({
            "type": "assistant", "timestamp": "2026-05-21T12:00:00Z", "cwd": "/tmp/wt",
            "message": {"id": mid, "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 1, "output_tokens": out}}}) + "\n")

    def run(extra, ledger):
        monkeypatch.setattr(sys, "argv", [
            "snapshot.py", "--root", str(tmp_path / "nologs"), "--tz", "UTC",
            "--ledger", str(tmp_path / "s.jsonl"), "--skip-cursor", "--skip-usage",
            "--skip-team-upload", "--factory-root", str(froot),
            "--factory-ledger", str(ledger), *extra])
        snapshot.main()
        return read_ledger(str(ledger))["2026-05-21"]

    default = run([], tmp_path / "f1.jsonl")
    assert default["bySource"]["factory:factory"]["out"] == 1 + 100   # runner-a excluded
    custom = run(["--factory-test-folder", "sandbox"], tmp_path / "f2.jsonl")
    assert custom["bySource"]["factory:factory"]["out"] == 1 + 10     # sandbox excluded


def test_refinalize_only_factory_touches_only_the_factory_ledger(tmp_path, monkeypatch):
    """The factory mirror keeps every file (late syncs undercount frozen days),
    so factory days can be re-derived; local days cannot (raw logs deleted)."""
    import json
    import sys
    import snapshot
    import usage_records

    monkeypatch.setattr(usage_records, "default_login", lambda *a, **k: None)
    monkeypatch.setattr(snapshot, "load_team_config", lambda: None)
    froot = tmp_path / "fac"
    sess = froot / "factory" / "5" / "wb-factory-implement-ab12c" / "p.jsonl"
    sess.parent.mkdir(parents=True)

    def write(n):
        sess.write_text("".join(json.dumps({
            "type": "assistant", "timestamp": "2026-05-21T12:00:00Z", "cwd": "/tmp/wt",
            "message": {"id": f"p{i}", "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 1, "output_tokens": 10}}}) + "\n"
            for i in range(n)))

    paths = {"ledger": tmp_path / "s.jsonl", "factory": tmp_path / "f.jsonl",
             "usage": tmp_path / "usage"}

    def run(*extra):
        monkeypatch.setattr(sys, "argv", [
            "snapshot.py", "--root", str(FIX), "--tz", "UTC",
            "--ledger", str(paths["ledger"]), "--skip-cursor", "--skip-team-upload",
            "--factory-root", str(froot), "--factory-ledger", str(paths["factory"]),
            "--usage-dir", str(paths["usage"]), "--actor", "jason-m", *extra])
        snapshot.main()

    write(1)                                  # the mirror synced late: 1 of 3 turns
    run()
    assert read_ledger(str(paths["factory"]))["2026-05-21"]["msgs"] == 1
    local_before = paths["ledger"].read_bytes()
    usage_before = {p.name: p.read_bytes() for p in paths["usage"].iterdir()}
    write(3)                                  # the rest arrives
    run("--refinalize", "--only", "factory", "--since", "2026-05-01")
    assert read_ledger(str(paths["factory"]))["2026-05-21"]["msgs"] == 3
    assert paths["ledger"].read_bytes() == local_before
    assert {p.name: p.read_bytes() for p in paths["usage"].iterdir()} == usage_before
