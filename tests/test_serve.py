# tests/test_serve.py
import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from outcomes import append_jsonl, cache_paths, write_state
from serve import (
    Handler,
    build_outcomes_section,
    build_team_upload_section,
    choose_port,
    build_payload,
)
from team_upload import write_upload_state

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
    # Ruling R3: efficiency payload lives under a NEW top-level key; with no
    # usage_dir/actor configured (not passed in cfg) it degrades gracefully
    # rather than raising, same as the cursor sub-payloads above.
    assert payload["efficiency"]["coverage_pct"] == 100.0
    assert payload["efficiency"]["days"] == []
    # Ruling R3: outcomes payload also lives under a NEW top-level key; with no
    # outcomes_cache_dir configured (not passed in cfg) it degrades to
    # "unavailable, with a reason" (spec Sec8) rather than raising.
    assert payload["outcomes"]["available"] is False
    assert "reason" in payload["outcomes"]
    # T6: team_upload lives under its own NEW top-level key; with no team
    # configured (env/local config file untouched here) it's absent/empty
    # rather than raising -- existing keys above are unaffected.
    assert payload["team_upload"] == {"configured": False}


# --- Team upload: reads the local upload-state file only; never uploads ----

def test_build_team_upload_section_unconfigured_is_absent_empty():
    assert build_team_upload_section({}) == {"configured": False}


def test_build_team_upload_section_reads_state_when_configured(tmp_path):
    state_path = tmp_path / "team-upload-state.json"
    write_upload_state(state_path, {"days": {
        "2026-09-25": {"status": "sent", "content_hash": "x", "last_success_at": "2026-09-26T00:00:00Z"},
        "2026-09-26": {"status": "refused", "reason": {"error": "e", "message": "m", "code": "C"},
                      "content_hash": "y", "last_attempt_at": "2026-09-26T01:00:00Z"},
    }})
    cfg = {"team_config": {"door_url": "https://door.example/upload", "tenant": "fe"},
          "team_upload_state": state_path,
          "console_efficiency_url": "https://console.example/factory/efficiency"}
    result = build_team_upload_section(cfg)
    assert result["configured"] is True
    assert result["last_success_at"] == "2026-09-26T00:00:00Z"
    assert result["refused"] == {"2026-09-26": {"error": "e", "message": "m", "code": "C"}}
    assert result["console_url"] == "https://console.example/factory/efficiency"


# --- Outcomes: reads the cache only, never GitHub; unavailable degrades honestly -------

def test_build_outcomes_section_unavailable_with_no_cache_dir():
    result = build_outcomes_section(None, [], "2026-09-15", "jason")
    assert result == {"available": False, "reason": "no outcomes cache configured"}


def test_build_outcomes_section_unavailable_when_cache_dir_has_no_files(tmp_path):
    result = build_outcomes_section(tmp_path / "outcomes", [], "2026-09-15", "jason")
    assert result["available"] is False
    assert "no outcome cache yet" in result["reason"]


def test_build_outcomes_section_available_reads_cache_and_scopes_to_me(tmp_path):
    cache_dir = tmp_path / "outcomes"
    paths = cache_paths(cache_dir)
    append_jsonl(paths["issues"], {
        "repo": "o/r", "number": 10, "author": "jason", "requested_by": "jason",
        "requester_source": "requested_by", "state": "closed", "state_reason": "completed",
        "closed_at": "2026-09-14T00:00:00Z", "reopened_at": [], "pts": 3,
        "pts_source": None, "labels": ["pts:3"], "approved_by": None,
    })
    append_jsonl(paths["prs"], {
        "repo": "o/r", "number": 20, "author": "jason", "head_ref": "feat/10-x",
        "state": "merged", "merged_at": "2026-09-14T00:00:00Z", "closes": [10],
        "reverted_by": None, "reverts": None, "review_rounds": 1,
    })
    write_state(paths["state"], {"repos": {}, "outcomes_as_of": "2026-09-15T00:00:00Z"})

    records = [{
        "v": 1, "day": "2026-09-14", "actor": "human:jason", "on_behalf_of": None,
        "requester_source": None, "repo": "o/r", "branch": "feat/10-x", "issue": None,
        "session": "s1", "model": "claude-sonnet-4", "model_class": "sonnet",
        "kind": "interactive", "calls": 1, "in": 10, "out": 10, "cc": 0, "cr": 0,
        "cost_usd": 6.0, "ctx_buckets": {},
    }]
    result = build_outcomes_section(cache_dir, records, "2026-09-15", "jason")
    assert result["available"] is True
    assert result["outcomes_as_of"] == "2026-09-15T00:00:00Z"
    assert result["tiles"]["cost_per_point"]["median"] == 2.0  # $6 / 3 pts
    assert result["tiles"]["cost_per_point"]["coverage"] == {"counted": 1, "total": 1, "pct": 100.0}


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
