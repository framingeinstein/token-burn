# tests/test_serve.py
from pathlib import Path
from serve import choose_port, build_payload

def test_choose_port_returns_start_when_free():
    assert choose_port(8799, 20, is_free=lambda p: True) == 8799

def test_choose_port_increments_past_busy_ports():
    busy = {8799, 8800}
    assert choose_port(8799, 20, is_free=lambda p: p not in busy) == 8801

def test_choose_port_none_when_all_busy():
    assert choose_port(8799, 3, is_free=lambda p: False) is None

def test_build_payload_assembles_from_ledger_and_logs(tmp_path):
    fixtures = Path(__file__).parent / "fixtures"
    cfg = {"ledger": str(tmp_path / "none.jsonl"), "root": fixtures,
           "tz": "UTC", "today": "2026-05-21"}
    payload = build_payload(cfg)
    assert "meta" in payload and "days" in payload
    assert payload["days"][0]["date"] == "2026-05-21"
