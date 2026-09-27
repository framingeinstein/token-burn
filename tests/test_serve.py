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

import pytest


@pytest.fixture(autouse=True)
def _never_read_the_real_team_config(monkeypatch):
    """Hermetic (final review): no test here may read the real env or
    ~/.token-burn/team.json -- tests pass `team_config` explicitly."""
    import serve

    def boom(*a, **k):
        raise AssertionError("test read the real team config; pass cfg['team_config']")

    monkeypatch.setattr(serve, "load_team_config", boom)


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
           "cursor_ledger": str(tmp_path / "missing.jsonl"),
           "team_config": {}}
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
    assert build_team_upload_section({"team_config": {}}) == {"configured": False}


def test_build_team_upload_section_loads_config_only_when_none_was_given(monkeypatch):
    import serve
    monkeypatch.setattr(serve, "load_team_config", lambda: None)
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


def test_build_outcomes_section_without_a_login_is_unavailable_not_the_team_view(tmp_path):
    cache_dir = tmp_path / "outcomes"
    paths = cache_paths(cache_dir)
    append_jsonl(paths["issues"], {"repo": "o/r", "number": 10, "state": "open"})
    result = build_outcomes_section(cache_dir, [], "2026-09-15", None)
    assert result["available"] is False
    assert "login" in result["reason"]


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
           "cursor_ledger": str(tmp_path / "missing.jsonl"),
           "team_config": {}}
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
           "cursor_ledger": str(tmp_path / "missing.jsonl"),
           "team_config": {}}
    payload = build_payload(cfg)
    assert payload["days"][0]["date"] == "2026-05-21"


# --- I3 (final review): one parse of today shared across passes and requests -----

import os
import time


def _write_session(path, day, n_calls, cwd, branch="feat/7-x", start=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "summary", "aiTitle": f"T {path.stem}"})]
    for i in range(n_calls):
        mid = f"{path.stem}-m{start + i}"
        base = {"type": "assistant", "timestamp": f"{day}T{(i // 60) % 24:02d}:{i % 60:02d}:00Z",
                "cwd": cwd, "gitBranch": branch,
                "message": {"id": mid, "model": "claude-sonnet-4-6",
                            "usage": {"input_tokens": 3, "output_tokens": 50,
                                      "cache_creation_input_tokens": 100,
                                      "cache_read_input_tokens": 2000}}}
        # two content-block lines per message, as Claude Code writes them
        lines.append(json.dumps(dict(base, uuid=f"{mid}-a")))
        lines.append(json.dumps(dict(base, uuid=f"{mid}-b")))
    path.write_text("\n".join(lines) + "\n")


def _memo_cfg(tmp_path, root, today, memo=None):
    cfg = {"ledger": str(tmp_path / "none.jsonl"), "root": [root],
           "tz": "UTC", "today": today,
           "cursor_db": str(tmp_path / "missing.vscdb"),
           "cursor_ledger": str(tmp_path / "missing.jsonl"),
           "usage_dir": str(tmp_path / "usage"), "actor": "human:jason",
           "team_config": {}, "team_upload_state": str(tmp_path / "team.json")}
    if memo is not None:
        cfg["session_memo"] = memo
    return cfg


def _strip_generated_at(payload):
    payload = json.loads(json.dumps(payload))
    payload["meta"].pop("generated_at", None)
    return payload


def test_build_payload_second_call_with_unchanged_files_reuses_the_memo(tmp_path):
    from parse import SessionMemo
    today = time.strftime("%Y-%m-%d", time.gmtime())
    root = tmp_path / "logs"
    for n in range(3):
        _write_session(root / f"s{n}.jsonl", today, 5, str(tmp_path))
    memo = SessionMemo()
    first = build_payload(_memo_cfg(tmp_path, root, today, memo))
    misses_after_first = memo.misses
    assert misses_after_first == 3            # each file read+parsed ONCE across both passes
    second = build_payload(_memo_cfg(tmp_path, root, today, memo))
    assert memo.misses == misses_after_first  # nothing re-read
    assert _strip_generated_at(first) == _strip_generated_at(second)

    # a file that changed is re-read on the next call; the others are not
    _write_session(root / "s0.jsonl", today, 6, str(tmp_path))
    os.utime(root / "s0.jsonl", (time.time() + 5, time.time() + 5))
    third = build_payload(_memo_cfg(tmp_path, root, today, memo))
    assert memo.misses == misses_after_first + 1
    assert third["efficiency"]["days"][0]["out"] == first["efficiency"]["days"][0]["out"] + 50


def test_build_payload_with_memo_matches_without_memo(tmp_path):
    from parse import SessionMemo
    today = time.strftime("%Y-%m-%d", time.gmtime())
    root = tmp_path / "logs"
    for n in range(3):
        _write_session(root / f"s{n}.jsonl", today, 7, str(tmp_path))
    plain = build_payload(_memo_cfg(tmp_path, root, today))
    memoised = build_payload(_memo_cfg(tmp_path, root, today, SessionMemo()))
    assert _strip_generated_at(plain) == _strip_generated_at(memoised)


def test_build_payload_fixture_scale_budget(tmp_path):
    """spec §10: /api/data under 5 s -- a regression budget at fixture scale
    (roughly the real heaviest live window seen: 300 sessions, ~108k transcript
    lines), cold and warm; the warm call must not re-read anything."""
    from parse import SessionMemo
    today = time.strftime("%Y-%m-%d", time.gmtime())
    root = tmp_path / "logs"
    cwds = []
    for c in range(6):                        # six working dirs, none a git repo
        d = tmp_path / "work" / f"w{c}"
        d.mkdir(parents=True)
        cwds.append(str(d))
    for n in range(300):
        _write_session(root / f"p{n % 6}" / f"s{n}.jsonl", today, 180, cwds[n % 6],
                       branch=f"feat/{n}-x")
    memo = SessionMemo()
    cfg = _memo_cfg(tmp_path, root, today, memo)
    t0 = time.perf_counter()
    cold = build_payload(cfg)
    cold_s = time.perf_counter() - t0
    misses = memo.misses
    t0 = time.perf_counter()
    build_payload(cfg)
    warm_s = time.perf_counter() - t0
    assert cold["efficiency"]["days"][0]["out"] == 300 * 180 * 50
    assert cold_s < 5.0, f"cold build_payload {cold_s:.2f}s over the 5 s budget"
    assert memo.misses == misses              # warm: no file re-read or re-parsed
    assert warm_s < 5.0, f"warm build_payload {warm_s:.2f}s over the 5 s budget"
