# tests/test_serve.py
import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from serve import Handler, choose_port, build_payload

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
           "tz": "UTC", "today": "2026-05-21",
           "cursor_db": str(tmp_path / "missing.vscdb"),
           "cursor_ledger": str(tmp_path / "missing.jsonl")}
    payload = build_payload(cfg)
    assert "meta" in payload and "days" in payload
    assert payload["days"][0]["date"] == "2026-05-21"
    assert payload["cursor"]["local"]["status"] == "unavailable"
    assert payload["cursor"]["billed"]["status"] == "unavailable"


def test_dashboard_and_api_disable_http_caching(tmp_path):
    fixtures = Path(__file__).parent / "fixtures"
    cfg = {"ledger": str(tmp_path / "none.jsonl"), "root": fixtures,
           "tz": "UTC", "today": "2026-05-21",
           "cursor_db": str(tmp_path / "missing.vscdb"),
           "cursor_ledger": str(tmp_path / "missing.jsonl")}
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, cfg=cfg))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/") as response:
            assert response.headers["Cache-Control"] == "no-store"
        with urlopen(base + "/api/data") as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert json.load(response)["days"][-1]["date"] == "2026-05-21"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_build_payload_without_root_discovers_every_store(tmp_path, monkeypatch):
    import serve
    fixtures = Path(__file__).parent / "fixtures"
    monkeypatch.setattr(serve, "default_roots", lambda: [fixtures])
    cfg = {"ledger": str(tmp_path / "none.jsonl"), "root": None,
           "tz": "UTC", "today": "2026-05-21",
           "cursor_db": str(tmp_path / "missing.vscdb"),
           "cursor_ledger": str(tmp_path / "missing.jsonl")}
    payload = build_payload(cfg)
    assert payload["days"][0]["date"] == "2026-05-21"
