# tests/test_usage_records.py — usage-record builder + schema validator (spec §2, §4.1, §4.3)
import json
import subprocess

import pytest

from prices import load_prices
from usage_records import (
    SchemaError,
    append_usage_records,
    ctx_bucket_label,
    factory_usage_records,
    issue_from_branch,
    load_schema,
    local_usage_records,
    read_usage_day,
    read_usage_records,
    resolve_actor,
    resolve_branch,
    resolve_factory_repo,
    resolve_repo,
    run_usage,
    SCHEMA_PATH,
    usage_path,
    validate_usage_record,
)

PRICES = load_prices()


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _git_repo(path, remote=None, branch=None):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "f").write_text("x")
    _git(path, "add", "f")
    _git(path, "commit", "-q", "-m", "init")
    if branch:
        _git(path, "checkout", "-q", "-b", branch)
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


def _session_text(cwd, calls, day="2026-09-25", branch=None):
    """`calls` are `(id, model, out)` or `(id, model, out, gitBranch)`; `branch`
    is the transcript's `gitBranch` for calls that don't name their own (C2:
    Claude Code stamps every line with the branch checked out at that moment)."""
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})]
    for i, call in enumerate(calls):
        mid, model, out = call[:3]
        git_branch = call[3] if len(call) > 3 else branch
        extra = {"gitBranch": git_branch} if git_branch is not None else {}
        lines.append(json.dumps({
            "type": "assistant", "timestamp": f"{day}T00:{i:02d}:00Z", "cwd": cwd, **extra,
            "message": {"id": mid, "model": model,
                        "usage": {"input_tokens": 2, "output_tokens": out,
                                  "cache_creation_input_tokens": 3,
                                  "cache_read_input_tokens": 5}}}))
    return "\n".join(lines) + "\n"


def _factory_session(path, calls, day="2026-09-25", cwd="/tmp/wt-1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_session_text(cwd, calls, day=day))


# --- context-growth buckets (Ruling R1) ---

def test_ctx_bucket_label_edges():
    assert ctx_bucket_label(1) == "1"
    assert ctx_bucket_label(2) == "2-5"
    assert ctx_bucket_label(5) == "2-5"
    assert ctx_bucket_label(6) == "6-20"
    assert ctx_bucket_label(20) == "6-20"
    assert ctx_bucket_label(21) == "21-50"
    assert ctx_bucket_label(50) == "21-50"
    assert ctx_bucket_label(51) == "51+"
    assert ctx_bucket_label(1000) == "51+"


# --- git-derived repo / branch / issue ---

def test_resolve_repo_none_without_remote(tmp_path):
    d = _git_repo(tmp_path / "repo")
    assert resolve_repo(str(d)) is None


def test_resolve_repo_none_without_cwd():
    assert resolve_repo(None) is None


def test_resolve_repo_parses_https_and_ssh_remotes(tmp_path):
    d1 = _git_repo(tmp_path / "a", remote="https://github.com/framingeinstein/token-burn.git")
    assert resolve_repo(str(d1)) == "framingeinstein/token-burn"
    d2 = _git_repo(tmp_path / "b", remote="git@github.com:synkhos/lattice.git")
    assert resolve_repo(str(d2)) == "synkhos/lattice"


def test_resolve_repo_caches_per_cwd(tmp_path):
    d = _git_repo(tmp_path / "repo", remote="https://github.com/o/r.git")
    cache = {}
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.run(cmd, **kw)

    resolve_repo(str(d), run=run, cache=cache)
    resolve_repo(str(d), run=run, cache=cache)
    assert len(calls) == 1


def test_resolve_branch_and_issue_from_branch(tmp_path):
    d = _git_repo(tmp_path / "repo", branch="feat/71-widgets")
    assert resolve_branch(str(d)) == "feat/71-widgets"
    assert issue_from_branch("feat/71-widgets") == 71
    assert issue_from_branch("main") is None
    assert issue_from_branch(None) is None


def test_resolve_branch_none_without_cwd():
    assert resolve_branch(None) is None


# --- actor resolution (no network in tests) ---

def test_resolve_actor_from_config_no_network():
    def boom():
        raise AssertionError("must not call the network")
    assert resolve_actor({"actor": "framingeinstein"}, get_login=boom) == "human:framingeinstein"


def test_resolve_actor_falls_back_to_injected_login():
    assert resolve_actor({}, get_login=lambda: "jason") == "human:jason"


def test_resolve_actor_none_when_no_login():
    assert resolve_actor({}, get_login=lambda: None) is None


# --- factory runner -> repo map (plan decision #5) ---

def test_resolve_factory_repo_strips_wb_impl_prefix():
    assert resolve_factory_repo("wb-impl-lattice") == "synkhos/lattice"


def test_resolve_factory_repo_terpsichore_special_case():
    assert resolve_factory_repo("terpsichore") == "synkhos/terpsichore-core"


def test_resolve_factory_repo_bare_runner_name():
    assert resolve_factory_repo("lattice") == "synkhos/lattice"


def test_resolve_factory_repo_override_map():
    assert resolve_factory_repo("custom", repo_map={"custom": "synkhos/other"}) == "synkhos/other"


# --- building local usage records ---

def test_local_usage_records_no_remote_yields_repo_null(tmp_path):
    d = _git_repo(tmp_path / "repo")
    raw = _session_text(str(d), [("m1", "claude-opus-4-8", 10)])
    recs = local_usage_records(str(tmp_path / "s1.jsonl"), raw, "UTC", PRICES,
                               "human:framingeinstein")
    assert len(recs) == 1
    assert recs[0]["repo"] is None
    validate_usage_record(recs[0])


def test_local_usage_records_fields_and_ctx_buckets(tmp_path):
    d = _git_repo(tmp_path / "repo", remote="https://github.com/framingeinstein/token-burn.git")
    calls = [(f"m{i}", "claude-opus-4-8", 10) for i in range(1, 8)]  # 7 calls
    raw = _session_text(str(d), calls, branch="feat/71-widgets")
    recs = local_usage_records(str(tmp_path / "sess-1.jsonl"), raw, "UTC", PRICES,
                               "human:framingeinstein")
    assert len(recs) == 1
    r = recs[0]
    assert r["repo"] == "framingeinstein/token-burn"
    assert r["branch"] == "feat/71-widgets"
    assert r["issue"] == 71
    assert r["session"] == "sess-1"
    assert r["kind"] == "interactive"
    assert r["actor"] == "human:framingeinstein"
    assert r["on_behalf_of"] is None and r["requester_source"] is None
    assert r["calls"] == 7
    # calls 1..7 -> buckets 1:{1}, 2-5:{2,3,4,5}, 6-20:{6,7}
    assert r["ctx_buckets"]["1"] == {"calls": 1, "ctx": 10}
    assert r["ctx_buckets"]["2-5"]["calls"] == 4
    assert r["ctx_buckets"]["6-20"]["calls"] == 2
    assert r["in"] == 14 and r["cc"] == 21 and r["cr"] == 35  # 7 * (2, 3, 5)
    validate_usage_record(r)


def test_local_usage_records_split_by_model_class(tmp_path):
    d = _git_repo(tmp_path / "repo")
    raw = _session_text(str(d), [("m1", "claude-opus-4-8", 10), ("m2", "claude-sonnet-4-6", 5)])
    recs = local_usage_records(str(tmp_path / "s.jsonl"), raw, "UTC", PRICES, "human:x")
    classes = {r["model_class"] for r in recs}
    assert classes == {"opus", "sonnet"}
    for r in recs:
        validate_usage_record(r)


def test_local_usage_records_untimestamped_calls_are_dropped(tmp_path):
    d = _git_repo(tmp_path / "repo")
    line = json.dumps({"type": "assistant", "cwd": str(d),
                       "message": {"id": "m1", "model": "claude-opus-4-8",
                                   "usage": {"input_tokens": 1, "output_tokens": 1}}})
    recs = local_usage_records(str(tmp_path / "s.jsonl"), line + "\n", "UTC", PRICES, "human:x")
    assert recs == []


# --- building factory usage records ---

def test_factory_usage_records_from_path_and_map(tmp_path):
    _factory_session(tmp_path / "lattice" / "71" / "exec-a" / "sess-a.jsonl",
                     [("m1", "claude-sonnet-4-6", 10)])
    recs = factory_usage_records(tmp_path, "UTC", PRICES)
    assert len(recs) == 1
    r = recs[0]
    assert r["actor"] == "factory:lattice"
    assert r["repo"] == "synkhos/lattice"
    assert r["issue"] == 71
    assert r["kind"] == "unknown"
    assert r["branch"] is None
    assert r["on_behalf_of"] is None and r["requester_source"] is None
    validate_usage_record(r)


def test_factory_usage_records_dedupes_across_executions(tmp_path):
    _factory_session(tmp_path / "hub" / "3" / "exec-a" / "s.jsonl",
                     [("m1", "claude-sonnet-4-6", 10), ("m2", "claude-sonnet-4-6", 10)])
    _factory_session(tmp_path / "hub" / "3" / "exec-b" / "s.jsonl",
                     [("m1", "claude-sonnet-4-6", 10), ("m2", "claude-sonnet-4-6", 10),
                      ("m3", "claude-sonnet-4-6", 10)])  # re-run re-uploads
    recs = factory_usage_records(tmp_path, "UTC", PRICES)
    assert sum(r["calls"] for r in recs) == 3


def test_factory_usage_records_missing_dir_returns_empty(tmp_path):
    assert factory_usage_records(tmp_path / "nope", "UTC", PRICES) == []


def test_factory_usage_records_uses_repo_map_override(tmp_path):
    _factory_session(tmp_path / "custom" / "5" / "e" / "s.jsonl", [("m1", "claude-sonnet-4-6", 1)])
    recs = factory_usage_records(tmp_path, "UTC", PRICES, repo_map={"custom": "synkhos/other"})
    assert recs[0]["repo"] == "synkhos/other"


def test_factory_usage_records_since_skips_files_not_modified_since(tmp_path):
    # same `_mtime_floor` invariant as parse.day_records (test_parse.py) — a file
    # last written before `since`'s local midnight can't hold a message dated
    # on/after `since`, so it's skipped before being read at all. This matters
    # because the factory transcript mirror can be large and `since` is often a
    # narrow "today" window (efficiency.py's live-today pass).
    import os
    _factory_session(tmp_path / "lattice" / "71" / "exec-a" / "s.jsonl",
                     [("m1", "claude-sonnet-4-6", 10)], day="2026-09-25")
    old = tmp_path / "lattice" / "71" / "exec-a" / "s.jsonl"
    os.utime(old, (0, 0))                                   # last written 1970
    recs = factory_usage_records(tmp_path, "UTC", PRICES, since="2026-09-25")
    assert recs == []                                       # skipped by mtime
    recs = factory_usage_records(tmp_path, "UTC", PRICES)   # no since => full scan
    assert len(recs) == 1


# --- schema validation (round trip + rejection) ---

def _valid_record(**overrides):
    base = {
        "v": 1, "day": "2026-09-27", "actor": "human:framingeinstein",
        "on_behalf_of": None, "requester_source": None,
        "repo": "framingeinstein/token-burn", "branch": "feat/71-widgets", "issue": 71,
        "session": "sess-1", "model": "claude-opus-4-8", "model_class": "opus",
        "kind": "interactive", "calls": 1, "in": 1, "out": 1, "cc": 1, "cr": 1,
        "cost_usd": 0.01, "ctx_buckets": {"1": {"calls": 1, "ctx": 3}},
    }
    base.update(overrides)
    return base


def test_valid_record_round_trips():
    validate_usage_record(_valid_record())


def test_unknown_field_is_rejected():
    with pytest.raises(SchemaError):
        validate_usage_record(_valid_record(prompt="free text leaks here"))


def test_free_text_value_is_rejected_by_pattern():
    with pytest.raises(SchemaError):
        validate_usage_record(_valid_record(repo="not a valid repo!! spaces"))


def test_missing_required_field_is_rejected():
    rec = _valid_record()
    del rec["model_class"]
    with pytest.raises(SchemaError):
        validate_usage_record(rec)


def test_bad_kind_enum_is_rejected():
    with pytest.raises(SchemaError):
        validate_usage_record(_valid_record(kind="rework"))


def test_bad_ctx_bucket_key_is_rejected():
    with pytest.raises(SchemaError):
        validate_usage_record(_valid_record(ctx_buckets={"weird": {"calls": 1, "ctx": 1}}))


def test_ctx_bucket_extra_field_is_rejected():
    with pytest.raises(SchemaError):
        validate_usage_record(_valid_record(
            ctx_buckets={"1": {"calls": 1, "ctx": 3, "note": "free text"}}))


def test_ctx_bucket_missing_required_field_is_rejected():
    with pytest.raises(SchemaError):
        validate_usage_record(_valid_record(ctx_buckets={"1": {"calls": 1}}))


def test_ctx_bucket_negative_value_is_rejected():
    with pytest.raises(SchemaError):
        validate_usage_record(_valid_record(ctx_buckets={"1": {"calls": -1, "ctx": 3}}))


def test_null_repo_branch_issue_are_valid():
    validate_usage_record(_valid_record(repo=None, branch=None, issue=None))


# --- requester_source enum (controller ruling R8): the canonical values are
# "requested_by" | "author" | null -- this schema is the contract the Synkhos
# upload door (synkhos/factory#171) validates against, so both values must
# round-trip and the old, wrong "header" value must be rejected. ------------

def test_requester_source_requested_by_round_trips():
    validate_usage_record(_valid_record(requester_source="requested_by"))


def test_requester_source_author_round_trips():
    validate_usage_record(_valid_record(requester_source="author"))


def test_requester_source_header_is_rejected():
    with pytest.raises(SchemaError):
        validate_usage_record(_valid_record(requester_source="header"))


def test_schema_file_is_published_and_loadable():
    assert SCHEMA_PATH.exists()
    schema = load_schema()
    assert schema["title"] == "token-burn usage record"
    validate_usage_record(_valid_record(), schema=schema)


# --- append-only archive: latest line wins ---

def test_append_and_read_latest_wins(tmp_path):
    rec1 = _valid_record(cost_usd=1.0)
    rec2 = dict(rec1, cost_usd=2.0)  # same composite key, refinalized value
    append_usage_records(tmp_path, "2026-09-27", [rec1])
    append_usage_records(tmp_path, "2026-09-27", [rec2])
    got = read_usage_records(tmp_path, "2026-09-27")
    assert len(got) == 1
    assert got[0]["cost_usd"] == 2.0


def test_append_rejects_invalid_record_and_writes_nothing(tmp_path):
    bad = _valid_record()
    bad["extra"] = "nope"
    with pytest.raises(SchemaError):
        append_usage_records(tmp_path, "2026-09-27", [bad])
    assert not usage_path(tmp_path, "2026-09-27").exists()


def test_read_usage_day_missing_file_is_empty(tmp_path):
    assert read_usage_day(tmp_path, "2026-09-27") == {}


# --- finalisation: immutable days, refinalize appends + latest wins ---

def test_run_usage_finalizes_only_completed_days(tmp_path):
    d = _git_repo(tmp_path / "repo")
    root = tmp_path / "logs"
    root.mkdir()
    (root / "s1.jsonl").write_text(_session_text(str(d), [("m1", "claude-opus-4-8", 10)],
                                                  day="2026-09-25"))
    usage_dir = tmp_path / "usage"
    added, failed = run_usage(root, "UTC", usage_dir, "human:framingeinstein", PRICES,
                              today="2026-09-26")
    assert added == {"2026-09-25"}
    assert failed == {}
    assert usage_path(usage_dir, "2026-09-25").exists()


def test_run_usage_never_finalizes_today(tmp_path):
    d = _git_repo(tmp_path / "repo")
    root = tmp_path / "logs"
    root.mkdir()
    (root / "s1.jsonl").write_text(_session_text(str(d), [("m1", "claude-opus-4-8", 10)],
                                                  day="2026-09-25"))
    usage_dir = tmp_path / "usage"
    added, failed = run_usage(root, "UTC", usage_dir, "human:framingeinstein", PRICES,
                              today="2026-09-25")
    assert added == set() and failed == {}
    assert not usage_path(usage_dir, "2026-09-25").exists()


def test_run_usage_is_idempotent(tmp_path):
    d = _git_repo(tmp_path / "repo")
    root = tmp_path / "logs"
    root.mkdir()
    (root / "s1.jsonl").write_text(_session_text(str(d), [("m1", "claude-opus-4-8", 10)],
                                                  day="2026-09-25"))
    usage_dir = tmp_path / "usage"
    run_usage(root, "UTC", usage_dir, "human:framingeinstein", PRICES, today="2026-09-26")
    lines_before = usage_path(usage_dir, "2026-09-25").read_text().count("\n")
    added, failed = run_usage(root, "UTC", usage_dir, "human:framingeinstein", PRICES,
                              today="2026-09-26")
    assert added == set() and failed == {}
    assert usage_path(usage_dir, "2026-09-25").read_text().count("\n") == lines_before


def test_run_usage_refinalize_appends_and_latest_wins(tmp_path):
    d = _git_repo(tmp_path / "repo")
    root = tmp_path / "logs"
    root.mkdir()
    (root / "s1.jsonl").write_text(_session_text(str(d), [("m1", "claude-opus-4-8", 10)],
                                                  day="2026-09-25"))
    usage_dir = tmp_path / "usage"
    run_usage(root, "UTC", usage_dir, "human:framingeinstein", PRICES, today="2026-09-26")
    added, failed = run_usage(root, "UTC", usage_dir, "human:framingeinstein", PRICES,
                              today="2026-09-26", refinalize=True)
    assert added == {"2026-09-25"} and failed == {}
    recs = read_usage_records(usage_dir, "2026-09-25")
    assert len(recs) == 1   # same composite key both times -> latest line wins on read


def test_run_usage_includes_factory_root(tmp_path):
    root = tmp_path / "logs"
    root.mkdir()
    _factory_session(tmp_path / "factory" / "lattice" / "9" / "e" / "s.jsonl",
                     [("m1", "claude-sonnet-4-6", 5)], day="2026-09-25")
    usage_dir = tmp_path / "usage"
    added, failed = run_usage(root, "UTC", usage_dir, "human:framingeinstein", PRICES,
                              today="2026-09-26", factory_root=tmp_path / "factory")
    assert added == {"2026-09-25"} and failed == {}
    recs = read_usage_records(usage_dir, "2026-09-25")
    assert any(r["actor"] == "factory:lattice" for r in recs)


def test_run_usage_isolates_a_bad_day_and_reports_it(tmp_path, monkeypatch):
    """Round 1 fix: a SchemaError for one day's batch must not abort the run —
    the good day still finalizes, the bad day is refused and reported."""
    import usage_records as ur

    root = tmp_path / "logs"
    root.mkdir()
    (root / "s1.jsonl").write_text("irrelevant — local_usage_records is faked below")

    good = _valid_record(day="2026-09-24", session="good-day")
    bad = _valid_record(day="2026-09-25", session="bad-day")
    bad["extra"] = "this field does not exist in the schema"  # additionalProperties: false

    monkeypatch.setattr(ur, "local_usage_records", lambda *a, **kw: [good, bad])

    usage_dir = tmp_path / "usage"
    added, failed = ur.run_usage(root, "UTC", usage_dir, "human:x", PRICES, today="2026-09-26")

    assert added == {"2026-09-24"}
    assert usage_path(usage_dir, "2026-09-24").exists()
    assert read_usage_records(usage_dir, "2026-09-24")[0]["session"] == "good-day"

    assert set(failed) == {"2026-09-25"}
    assert not usage_path(usage_dir, "2026-09-25").exists()
    assert "extra" in failed["2026-09-25"]


# --- controller ruling: an invalid branch is nulled, not an invalid record ---

class _FakeGitResult:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _fake_git(remote=None, branch=None):
    def run(cmd, **kw):
        if "remote" in cmd:
            return _FakeGitResult(0, remote) if remote else _FakeGitResult(1, "")
        if "rev-parse" in cmd:
            return _FakeGitResult(0, branch) if branch else _FakeGitResult(1, "")
        return _FakeGitResult(1, "")
    return run


def test_branch_with_a_space_is_nulled_but_issue_still_extracted(tmp_path):
    raw = _session_text("/fake/repo", [("m1", "claude-opus-4-8", 10)], branch="feat/71-fix a bug")
    run = _fake_git()
    recs = local_usage_records(str(tmp_path / "s.jsonl"), raw, "UTC", PRICES, "human:x", run=run)
    assert len(recs) == 1
    r = recs[0]
    assert r["branch"] is None
    assert r["issue"] == 71
    validate_usage_record(r)


def test_branch_with_at_sign_is_nulled(tmp_path):
    raw = _session_text("/fake/repo", [("m1", "claude-opus-4-8", 10)], branch="feat/71-widgets@2")
    run = _fake_git()
    recs = local_usage_records(str(tmp_path / "s.jsonl"), raw, "UTC", PRICES, "human:x", run=run)
    assert recs[0]["branch"] is None
    validate_usage_record(recs[0])


def test_branch_matching_pattern_is_kept(tmp_path):
    raw = _session_text("/fake/repo", [("m1", "claude-opus-4-8", 10)], branch="feat/71-widgets")
    run = _fake_git()
    recs = local_usage_records(str(tmp_path / "s.jsonl"), raw, "UTC", PRICES, "human:x", run=run)
    assert recs[0]["branch"] == "feat/71-widgets"


# --- C2 (final review): branch per message from the transcript, repo cached persistently ---

def test_two_branches_in_one_session_become_two_records(tmp_path):
    raw = _session_text("/fake/repo", [
        ("m1", "claude-opus-4-8", 10, "feat/71-widgets"),
        ("m2", "claude-opus-4-8", 10, "feat/71-widgets"),
        ("m3", "claude-opus-4-8", 10, "feat/88-other"),
    ])
    recs = local_usage_records(str(tmp_path / "s.jsonl"), raw, "UTC", PRICES, "human:x",
                               run=_fake_git(remote="https://github.com/o/r.git"))
    by_branch = {r["branch"]: r for r in recs}
    assert set(by_branch) == {"feat/71-widgets", "feat/88-other"}
    assert by_branch["feat/71-widgets"]["calls"] == 2
    assert by_branch["feat/71-widgets"]["issue"] == 71
    assert by_branch["feat/88-other"]["calls"] == 1
    assert by_branch["feat/88-other"]["issue"] == 88
    assert all(r["repo"] == "o/r" for r in recs)
    for r in recs:
        validate_usage_record(r)


def test_branch_never_comes_from_the_cwds_current_checkout(tmp_path, monkeypatch):
    import usage_records as ur
    monkeypatch.setattr(ur, "resolve_branch", lambda *a, **k: "feat/999-checked-out-now")
    raw = _session_text("/fake/repo", [("m1", "claude-opus-4-8", 10)], branch="feat/71-widgets")
    recs = local_usage_records(str(tmp_path / "s.jsonl"), raw, "UTC", PRICES, "human:x",
                               run=_fake_git(branch="feat/999-checked-out-now"))
    assert [r["branch"] for r in recs] == ["feat/71-widgets"]
    assert recs[0]["issue"] == 71


def test_a_transcript_without_gitbranch_records_branch_null(tmp_path):
    raw = _session_text("/fake/repo", [("m1", "claude-opus-4-8", 10)])
    recs = local_usage_records(str(tmp_path / "s.jsonl"), raw, "UTC", PRICES, "human:x",
                               run=_fake_git(branch="feat/999-checked-out-now"))
    assert recs[0]["branch"] is None and recs[0]["issue"] is None


def test_detached_head_gitbranch_is_null(tmp_path):
    raw = _session_text("/fake/repo", [("m1", "claude-opus-4-8", 10)], branch="HEAD")
    recs = local_usage_records(str(tmp_path / "s.jsonl"), raw, "UTC", PRICES, "human:x",
                               run=_fake_git())
    assert recs[0]["branch"] is None


def test_repo_cache_persists_across_instances_and_outlives_a_deleted_worktree(tmp_path):
    import shutil
    from usage_records import RepoCache
    wt = _git_repo(tmp_path / "wt", remote="https://github.com/o/r.git")
    path = tmp_path / "usage" / "repo-cache.json"
    cache = RepoCache(path)
    assert resolve_repo(str(wt), cache=cache) == "o/r"
    cache.save()
    shutil.rmtree(wt)                                     # the worktree is gone

    def no_git(*a, **k):
        raise AssertionError("a cached cwd must not shell out to git")

    later = RepoCache(path)                               # e.g. the next process
    assert resolve_repo(str(wt), run=no_git, cache=later) == "o/r"


def test_repo_cache_never_persists_a_null_resolution(tmp_path):
    from usage_records import RepoCache
    path = tmp_path / "repo-cache.json"
    cache = RepoCache(path)
    assert resolve_repo("/no/such/dir", run=_fake_git(), cache=cache) is None
    cache.save()
    assert "/no/such/dir" not in RepoCache(path)


def test_shared_repo_cache_is_process_level_per_path(tmp_path):
    from usage_records import shared_repo_cache
    a = shared_repo_cache(tmp_path / "usage")
    assert shared_repo_cache(tmp_path / "usage") is a
    assert shared_repo_cache(tmp_path / "other") is not a
    assert shared_repo_cache(None) is not shared_repo_cache(None)  # no dir: per-call only


def test_run_usage_resolves_each_cwd_once_and_persists_the_map(tmp_path):
    d = _git_repo(tmp_path / "repo", remote="https://github.com/o/r.git")
    root = tmp_path / "logs"
    root.mkdir()
    for n in range(3):
        (root / f"s{n}.jsonl").write_text(_session_text(
            str(d), [(f"m{n}", "claude-opus-4-8", 10)], day="2026-09-25", branch="main"))
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.run(cmd, **kw)

    usage_dir = tmp_path / "usage"
    run_usage(root, "UTC", usage_dir, "human:x", PRICES, today="2026-09-26", run=run)
    assert len([c for c in calls if "remote" in c]) == 1
    assert json.loads((usage_dir / "repo-cache.json").read_text()) == {str(d): "o/r"}


def test_issue_from_branch_accepts_githubs_create_branch_from_issue_forms_I9():
    assert issue_from_branch("feat/171-x") == 171
    assert issue_from_branch("171-x") == 171
    assert issue_from_branch("hotfix/171-x") == 171
    assert issue_from_branch("a/b/171-x") is None
    assert issue_from_branch("revert-20-x") is None
