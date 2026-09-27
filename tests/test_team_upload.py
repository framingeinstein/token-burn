# tests/test_team_upload.py — T6: usage reaches the team through the factory door
# (spec Amendment 2026-09-27 A-1/A-3; controller ruling R11)
#
# synkhos/factory#171 (the door) isn't built and has no published wire contract:
# these tests stub the door via an injected transport + credential provider, per
# R11. The request/response shapes exercised here are this repo's PROVISIONAL
# guess (see team_upload.py's module docstring) and are marked as such.
import json

import pytest

from usage_records import append_usage_records
import team_upload as tu


def _valid_record(**overrides):
    base = {
        "v": 1, "day": "2026-09-25", "actor": "human:framingeinstein",
        "on_behalf_of": None, "requester_source": None,
        "repo": "framingeinstein/token-burn", "branch": "feat/71-widgets", "issue": 71,
        "session": "sess-1", "model": "claude-opus-4-8", "model_class": "opus",
        "kind": "interactive", "calls": 1, "in": 1, "out": 1, "cc": 1, "cr": 1,
        "cost_usd": 0.01, "ctx_buckets": {"1": {"calls": 1, "ctx": 3}},
    }
    base.update(overrides)
    return base


class ScriptedTransport:
    """Records every call; returns canned (status, headers, body) responses in
    order per day, or raises when a day has no more scripted responses and
    `unreachable_days` names it."""

    def __init__(self, responses=None, unreachable_days=()):
        self.responses = {k: list(v) for k, v in (responses or {}).items()}
        self.unreachable_days = set(unreachable_days)
        self.calls = []

    def __call__(self, method, url, headers, body=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        day = body["day"]
        if day in self.unreachable_days:
            raise tu.DoorUnreachable("connection refused")
        queue = self.responses.get(day)
        if not queue:
            raise AssertionError(f"unstubbed call for day {day}")
        return queue.pop(0)


CONFIG = {"door_url": "https://door.example/upload", "tenant": "framingeinstein"}


def _cred():
    return "house-token"


# --- config: no team configured => nothing sent, nothing changes -----------

def test_load_team_config_none_when_unconfigured(tmp_path):
    assert tu.load_team_config(env={}, config_path=tmp_path / "missing.json") is None


def test_load_team_config_from_env():
    env = {"TOKEN_BURN_TEAM_DOOR_URL": "https://door.example/upload",
           "TOKEN_BURN_TEAM_TENANT": "framingeinstein"}
    assert tu.load_team_config(env=env, config_path="/nonexistent") == CONFIG


def test_load_team_config_from_file_when_env_unset(tmp_path):
    path = tmp_path / "team.json"
    path.write_text(json.dumps(CONFIG))
    assert tu.load_team_config(env={}, config_path=path) == CONFIG


def test_load_team_config_partial_env_is_unconfigured(tmp_path):
    env = {"TOKEN_BURN_TEAM_DOOR_URL": "https://door.example/upload"}  # no tenant
    assert tu.load_team_config(env=env, config_path=tmp_path / "missing.json") is None


def test_upload_pending_days_no_team_configured_sends_nothing(tmp_path):
    usage_dir = tmp_path / "usage"
    append_usage_records(usage_dir, "2026-09-25", [_valid_record()])
    transport = ScriptedTransport()
    state_path = tmp_path / "state.json"
    result = tu.upload_pending_days(usage_dir, None, state_path=state_path,
                                     transport=transport, credential_provider=_cred)
    assert result is None
    assert transport.calls == []
    assert not state_path.exists()
    # local archive untouched
    assert (usage_dir / "2026-09-25.jsonl").read_text().count("\n") == 1


# --- sent once, not re-sent after success -----------------------------------

def test_day_sent_once_and_not_resent_after_success(tmp_path):
    usage_dir = tmp_path / "usage"
    append_usage_records(usage_dir, "2026-09-25", [_valid_record()])
    state_path = tmp_path / "state.json"
    transport = ScriptedTransport({"2026-09-25": [(202, {}, None)]})

    r1 = tu.upload_pending_days(usage_dir, CONFIG, state_path=state_path,
                                 transport=transport, credential_provider=_cred)
    assert r1["sent"] == ["2026-09-25"]
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST" and call["url"] == CONFIG["door_url"]
    assert call["headers"]["Authorization"] == "Bearer house-token"
    assert call["body"] == {"tenant": "framingeinstein", "day": "2026-09-25",
                            "records": [_valid_record()]}

    # second run: door not called again for this day, "sent" is empty
    r2 = tu.upload_pending_days(usage_dir, CONFIG, state_path=state_path,
                                 transport=transport, credential_provider=_cred)
    assert r2["sent"] == []
    assert len(transport.calls) == 1  # unchanged


# --- refusal reported with reason, retried next run -------------------------

def test_refusal_is_reported_with_reason_and_retried_next_run(tmp_path):
    usage_dir = tmp_path / "usage"
    append_usage_records(usage_dir, "2026-09-25", [_valid_record()])
    state_path = tmp_path / "state.json"
    refusal_body = {"error": "invalid_tenant", "message": "unknown tenant", "code": "E_TENANT"}
    transport = ScriptedTransport({"2026-09-25": [(422, {}, refusal_body), (202, {}, None)]})

    r1 = tu.upload_pending_days(usage_dir, CONFIG, state_path=state_path,
                                 transport=transport, credential_provider=_cred)
    assert r1["refused"] == {"2026-09-25": refusal_body}
    assert r1["sent"] == []

    status = tu.team_upload_status(CONFIG, state_path=state_path)
    assert status["refused"] == {"2026-09-25": refusal_body}
    assert status["last_success_at"] is None

    # retried next run (transport called again) and this time accepted
    r2 = tu.upload_pending_days(usage_dir, CONFIG, state_path=state_path,
                                 transport=transport, credential_provider=_cred)
    assert r2["sent"] == ["2026-09-25"]
    assert len(transport.calls) == 2


# --- door unreachable leaves local archives/dashboard unaffected -----------

def test_door_unreachable_is_recorded_and_does_not_raise(tmp_path):
    usage_dir = tmp_path / "usage"
    append_usage_records(usage_dir, "2026-09-25", [_valid_record()])
    state_path = tmp_path / "state.json"
    transport = ScriptedTransport(unreachable_days={"2026-09-25"})

    result = tu.upload_pending_days(usage_dir, CONFIG, state_path=state_path,
                                     transport=transport, credential_provider=_cred)
    assert "2026-09-25" in result["unreachable"]
    # local usage archive is untouched
    from usage_records import read_usage_records
    assert len(read_usage_records(usage_dir, "2026-09-25")) == 1

    status = tu.team_upload_status(CONFIG, state_path=state_path)
    assert status["configured"] is True
    assert status["last_success_at"] is None
    assert "2026-09-25" in status["pending"]


# --- a day with any invalid record is refused whole, locally, and reported -

def test_invalid_day_is_not_sent_and_is_reported(tmp_path):
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    bad = _valid_record()
    bad["extra_field"] = "free text should never leave the machine"
    (usage_dir / "2026-09-25.jsonl").write_text(json.dumps(bad) + "\n")
    state_path = tmp_path / "state.json"
    transport = ScriptedTransport()

    result = tu.upload_pending_days(usage_dir, CONFIG, state_path=state_path,
                                     transport=transport, credential_provider=_cred)
    assert transport.calls == []  # never sent
    assert "2026-09-25" in result["invalid"]
    assert "extra_field" in result["invalid"]["2026-09-25"]


# --- idempotence: a late refinalize (content changes) is re-sent -----------

def test_refinalized_day_with_changed_content_is_resent(tmp_path):
    usage_dir = tmp_path / "usage"
    append_usage_records(usage_dir, "2026-09-25", [_valid_record(session="sess-1")])
    state_path = tmp_path / "state.json"
    transport = ScriptedTransport({"2026-09-25": [(202, {}, None), (202, {}, None)]})

    r1 = tu.upload_pending_days(usage_dir, CONFIG, state_path=state_path,
                                 transport=transport, credential_provider=_cred)
    assert r1["sent"] == ["2026-09-25"]

    # --refinalize appends a newer line for the same day with different content
    append_usage_records(usage_dir, "2026-09-25", [_valid_record(session="sess-2")])
    r2 = tu.upload_pending_days(usage_dir, CONFIG, state_path=state_path,
                                 transport=transport, credential_provider=_cred)
    assert r2["sent"] == ["2026-09-25"]
    assert len(transport.calls) == 2


# --- dashboard status: absent/empty when unconfigured -----------------------

def test_team_upload_status_unconfigured():
    assert tu.team_upload_status(None) == {"configured": False}


def test_team_upload_status_includes_console_link(tmp_path):
    status = tu.team_upload_status(CONFIG, state_path=tmp_path / "state.json",
                                    console_url="https://console.example/factory/efficiency")
    assert status["configured"] is True
    assert status["console_url"] == "https://console.example/factory/efficiency"
    assert status["pending"] == []
    assert status["refused"] == {}
    assert status["last_success_at"] is None


# --- credential provider is pluggable; missing credential doesn't crash ----

def test_missing_credential_is_recorded_as_unreachable_not_a_crash(tmp_path):
    usage_dir = tmp_path / "usage"
    append_usage_records(usage_dir, "2026-09-25", [_valid_record()])
    state_path = tmp_path / "state.json"
    transport = ScriptedTransport()

    result = tu.upload_pending_days(usage_dir, CONFIG, state_path=state_path,
                                     transport=transport, credential_provider=lambda: None)
    assert transport.calls == []
    assert "2026-09-25" in result["unreachable"]
