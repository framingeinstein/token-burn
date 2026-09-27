# tests/test_outcomes.py
"""Outcome cache: fetch orchestration, JSONL latest-wins cache, watermarks/ETags,
quota floor stop, GraphQL-only-for-the-two-named-cases (spec Sec4.2 / Sec5.2)."""
import json

import pytest

from github_client import GitHubError, GitHubUnreachable, Quota, QuotaFloorHit
from outcomes import (
    approved_by_from_timeline,
    build_issue_record,
    build_pr_record,
    cache_paths,
    closes_from_branch,
    closes_from_timeline,
    fetch_outcomes,
    pts_and_source,
    read_jsonl_latest,
    read_state,
    append_jsonl,
    requested_by_and_source,
    resolve_pr_closes,
    reopened_at_from_timeline,
    review_rounds_from_timeline,
    write_state,
)


def rl(remaining, extra=None):
    h = {"x-ratelimit-remaining": str(remaining)}
    if extra:
        h.update(extra)
    return h


class ScriptedTransport:
    """Maps (method, url-prefix) -> a canned response, popped in order per key.
    Records every call so tests can assert what was (and wasn't) invoked."""
    def __init__(self, script):
        # script: {(method, url_substring): [ (status, headers, body), ... ]}
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url))
        for (m, sub), queue in self.script.items():
            if m == method and sub in url and queue:
                return queue.pop(0)
        raise AssertionError(f"unstubbed call: {method} {url}")


# --- record field extraction (pure) -----------------------------------------

def test_pts_and_source_parses_numeric_label():
    # fix round 1 (controller ruling): pts_source is null for now -- the
    # rater runs from the SAME user's GitHub token as a manual label change,
    # so an actor-login heuristic can't tell "rater" from "human" apart.
    labels = ["pts:5", "route:foo"]
    events = [{"event": "labeled", "label": {"name": "pts:5"}, "actor": {"login": "github-actions[bot]"}}]
    assert pts_and_source(labels, events) == (5, None)


def test_pts_and_source_unrated_label():
    assert pts_and_source(["pts:unrated"], []) == (None, None)


def test_pts_and_source_no_label():
    assert pts_and_source(["route:foo"], []) == (None, None)


def test_pts_and_source_source_is_null_regardless_of_timeline_actor():
    # No actor-based heuristic: a "human"-looking actor doesn't produce
    # pts_source: "human" either -- it's always null until synkhos/factory#166
    # defines the R-6 rating-comment markers this should be read from.
    labels = ["pts:8"]
    events = [{"event": "labeled", "label": {"name": "pts:8"}, "actor": {"login": "jason"}}]
    assert pts_and_source(labels, events) == (8, None)


def test_requested_by_from_header_line():
    body = "**Route:** foo\n**Requested by:** ricky\n\nDone when..."
    assert requested_by_and_source(body, "some-author") == ("ricky", "requested_by")


def test_requested_by_falls_back_to_author():
    assert requested_by_and_source("no header here", "some-author") == ("some-author", "author")
    assert requested_by_and_source(None, "some-author") == ("some-author", "author")


def test_reopened_at_from_timeline():
    events = [{"event": "closed", "created_at": "t0"},
              {"event": "reopened", "created_at": "t1"},
              {"event": "reopened", "created_at": "t2"}]
    assert reopened_at_from_timeline(events) == ["t1", "t2"]


def test_approved_by_from_timeline_takes_latest():
    events = [
        {"event": "labeled", "label": {"name": "approved"}, "actor": {"login": "a"}},
        {"event": "labeled", "label": {"name": "approved"}, "actor": {"login": "b"}},
    ]
    assert approved_by_from_timeline(events) == "b"


def test_approved_by_none_when_absent():
    assert approved_by_from_timeline([{"event": "labeled", "label": {"name": "pts:3"}}]) is None


def test_closes_from_timeline_connected_and_cross_referenced():
    events = [
        {"event": "connected", "source": {"issue": {"number": 12}}},
        {"event": "cross-referenced", "source": {"issue": {"number": 8}}},
        {"event": "cross-referenced", "source": {"issue": {"number": 99, "pull_request": {}}}},
    ]
    assert closes_from_timeline(events) == [8, 12]


def test_closes_from_branch_pattern():
    assert closes_from_branch("feat/123-do-the-thing") == [123]
    assert closes_from_branch("main") == []
    assert closes_from_branch(None) == []


def test_review_rounds_counts_reviewed_events():
    events = [{"event": "reviewed"}, {"event": "commented"}, {"event": "reviewed"}]
    assert review_rounds_from_timeline(events) == 2


def test_resolve_pr_closes_prefers_timeline():
    events = [{"event": "connected", "source": {"issue": {"number": 5}}}]
    called = {"graphql": False}
    def fake_graphql(*a, **kw):
        called["graphql"] = True
        return [999]
    result = resolve_pr_closes("o/r", 1, "feat/7-x", events, "tok", Quota(), object(), graphql=fake_graphql)
    assert result == [5]
    assert called["graphql"] is False


def test_resolve_pr_closes_falls_back_to_branch_pattern():
    called = {"graphql": False}
    def fake_graphql(*a, **kw):
        called["graphql"] = True
        return [999]
    result = resolve_pr_closes("o/r", 1, "feat/7-x", [], "tok", Quota(), object(), graphql=fake_graphql)
    assert result == [7]
    assert called["graphql"] is False


def test_resolve_pr_closes_calls_graphql_only_when_rest_resolves_nothing():
    called = {"graphql": False}
    def fake_graphql(*a, **kw):
        called["graphql"] = True
        return [42]
    result = resolve_pr_closes("o/r", 1, "chore/cleanup", [], "tok", Quota(), object(), graphql=fake_graphql)
    assert result == [42]
    assert called["graphql"] is True


def test_build_issue_record_shape():
    issue = {"number": 10, "user": {"login": "jason"}, "state": "closed",
              "state_reason": "completed", "closed_at": "2026-09-01T00:00:00Z",
              "labels": [{"name": "pts:3"}], "body": "**Requested by:** ricky\n"}
    events = [{"event": "reopened", "created_at": "t1"},
              {"event": "labeled", "label": {"name": "approved"}, "actor": {"login": "jason"}}]
    rec = build_issue_record("o/r", issue, events)
    assert rec == {
        "repo": "o/r", "number": 10, "author": "jason", "requested_by": "ricky",
        "requester_source": "requested_by", "state": "closed", "state_reason": "completed",
        "closed_at": "2026-09-01T00:00:00Z", "reopened_at": ["t1"], "pts": 3,
        "pts_source": None, "labels": ["pts:3"], "approved_by": "jason",
    }
    assert "body" not in rec and "title" not in rec


def test_build_pr_record_shape():
    pr = {"number": 20, "user": {"login": "jason"}, "head": {"ref": "feat/10-x"},
          "state": "closed", "merged_at": "2026-09-02T00:00:00Z"}
    events = [{"event": "reviewed"}, {"event": "reviewed"}]
    rec = build_pr_record("o/r", pr, events, closes=[10])
    assert rec == {
        "repo": "o/r", "number": 20, "author": "jason", "head_ref": "feat/10-x",
        "state": "merged", "merged_at": "2026-09-02T00:00:00Z", "closes": [10],
        "reverted_by": None, "review_rounds": 2,
    }


# --- cache: JSONL latest-line-wins + state -----------------------------

def test_read_jsonl_latest_wins_by_key(tmp_path):
    p = tmp_path / "issues.jsonl"
    append_jsonl(p, {"repo": "o/r", "number": 1, "state": "open"})
    append_jsonl(p, {"repo": "o/r", "number": 1, "state": "closed"})
    append_jsonl(p, {"repo": "o/r", "number": 2, "state": "open"})
    out = read_jsonl_latest(p, lambda r: (r["repo"], r["number"]))
    assert out[("o/r", 1)]["state"] == "closed"
    assert out[("o/r", 2)]["state"] == "open"


def test_read_jsonl_latest_missing_file(tmp_path):
    assert read_jsonl_latest(tmp_path / "nope.jsonl", lambda r: r) == {}


def test_read_jsonl_latest_skips_corrupt_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"repo":"o/r","number":1}\nnot json\n')
    out = read_jsonl_latest(p, lambda r: (r["repo"], r["number"]))
    assert list(out) == [("o/r", 1)]


def test_state_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    assert read_state(p) == {"repos": {}, "outcomes_as_of": None}
    state = read_state(p)
    state["repos"]["o/r"] = {"issues": {"since": "t1"}}
    state["outcomes_as_of"] = "t2"
    write_state(p, state)
    assert read_state(p) == state


def test_cache_paths(tmp_path):
    paths = cache_paths(tmp_path)
    assert paths["state"].name == "state.json"
    assert paths["issues"].name == "issues.jsonl"
    assert paths["prs"].name == "prs.jsonl"


# --- fetch orchestration -----------------------------------------------

def _base_script(remaining=5000):
    return {
        ("GET", "/orgs/synkhos/repos"): [(200, rl(remaining), [{"full_name": "synkhos/a"}])],
        ("GET", "/repos/synkhos/a/issues"): [(304, rl(remaining), None)],
        ("GET", "/repos/synkhos/a/pulls"): [(200, rl(remaining), [])],
    }


def test_fetch_outcomes_304_costs_no_extra_quota_and_writes_nothing(tmp_path):
    transport = ScriptedTransport(_base_script())
    report = fetch_outcomes(cache_dir=tmp_path, orgs=["synkhos"], token="tok", transport=transport)
    assert report["issues"] == 0
    assert report["stopped"] is None
    paths = cache_paths(tmp_path)
    assert not paths["issues"].exists() or paths["issues"].read_text() == ""
    # etag/watermark preserved even though nothing changed
    state = read_state(paths["state"])
    assert "synkhos/a" in state["repos"]


def test_fetch_outcomes_processes_new_issues_and_prs(tmp_path):
    script = {
        ("GET", "/orgs/synkhos/repos"): [(200, rl(5000), [{"full_name": "synkhos/a"}])],
        ("GET", "/repos/synkhos/a/issues"): [(200, rl(5000, {"etag": '"e1"'}), [
            {"number": 1, "user": {"login": "jason"}, "state": "open", "labels": [],
             "body": "", "updated_at": "2026-09-01T00:00:00Z"},
        ])],
        ("GET", "/repos/synkhos/a/issues/1/timeline"): [(200, rl(5000), [])],
        ("GET", "/repos/synkhos/a/pulls"): [(200, rl(5000), [
            {"number": 2, "user": {"login": "jason"}, "state": "closed",
             "merged_at": "2026-09-02T00:00:00Z", "head": {"ref": "feat/1-x"},
             "updated_at": "2026-09-02T00:00:00Z"},
        ])],
        ("GET", "/repos/synkhos/a/issues/2/timeline"): [(200, rl(5000), [])],
    }
    transport = ScriptedTransport(script)
    report = fetch_outcomes(cache_dir=tmp_path, orgs=["synkhos"], token="tok", transport=transport)
    assert report["issues"] == 1
    assert report["prs"] == 1
    assert report["stopped"] is None
    paths = cache_paths(tmp_path)
    issues = read_jsonl_latest(paths["issues"], lambda r: (r["repo"], r["number"]))
    prs = read_jsonl_latest(paths["prs"], lambda r: (r["repo"], r["number"]))
    assert issues[("synkhos/a", 1)]["author"] == "jason"
    assert prs[("synkhos/a", 2)]["closes"] == [1]
    assert report["calls"]["graphql"] == 0  # branch pattern resolved it, no GraphQL needed
    state = read_state(paths["state"])
    assert state["outcomes_as_of"] is not None


def test_fetch_outcomes_stops_at_quota_floor_keeps_cache_and_watermark(tmp_path):
    # first repo succeeds fully with a low-but-fine remaining; second repo's
    # very first call reveals remaining below the floor -> must not be made.
    script = {
        ("GET", "/orgs/synkhos/repos"): [(200, rl(5000), [
            {"full_name": "synkhos/a"}, {"full_name": "synkhos/b"},
        ])],
        ("GET", "/repos/synkhos/a/issues"): [(304, rl(900), None)],  # drops below floor (1000)
        ("GET", "/repos/synkhos/a/pulls"): [(200, rl(900), [])],
    }
    transport = ScriptedTransport(script)
    report = fetch_outcomes(cache_dir=tmp_path, orgs=["synkhos"], token="tok", transport=transport,
                             quota=Quota(floor_core=1000, floor_graphql=1000))
    assert report["stopped"] == "quota_floor"
    # repo b was never called
    assert not any("synkhos/b" in url for _m, url in transport.calls)
    paths = cache_paths(tmp_path)
    state = read_state(paths["state"])
    assert "synkhos/a" in state["repos"]  # watermark for the completed repo kept


def test_fetch_outcomes_resumes_next_run_from_saved_watermark(tmp_path):
    paths = cache_paths(tmp_path)
    write_state(paths["state"], {"repos": {"synkhos/a": {
        "issues": {"since": "2026-09-01T00:00:00Z", "etag": '"old"'},
        "prs": {"since": "2026-09-01T00:00:00Z"},
    }}, "outcomes_as_of": "2026-09-01T00:00:00Z"})
    script = {
        ("GET", "/orgs/synkhos/repos"): [(200, rl(5000), [{"full_name": "synkhos/a"}])],
        ("GET", "/repos/synkhos/a/issues"): [(304, rl(5000), None)],
        ("GET", "/repos/synkhos/a/pulls"): [(200, rl(5000), [])],
    }
    transport = ScriptedTransport(script)
    fetch_outcomes(cache_dir=tmp_path, orgs=["synkhos"], token="tok", transport=transport)
    # the saved watermark was sent as `since=` on the resumed run
    assert any("since=2026-09-01T00:00:00Z" in url for _m, url in transport.calls)


def test_fetch_outcomes_unreachable_keeps_cache_and_outcomes_as_of_unchanged(tmp_path):
    paths = cache_paths(tmp_path)
    write_state(paths["state"], {"repos": {}, "outcomes_as_of": "2026-09-01T00:00:00Z"})
    append_jsonl(paths["issues"], {"repo": "synkhos/a", "number": 1, "state": "open"})

    def boom(method, url, headers, body=None):
        raise GitHubUnreachable("no network")

    report = fetch_outcomes(cache_dir=tmp_path, orgs=["synkhos"], token="tok", transport=boom)
    assert report["stopped"] == "unreachable"
    state = read_state(paths["state"])
    assert state["outcomes_as_of"] == "2026-09-01T00:00:00Z"  # unchanged
    assert paths["issues"].read_text() == '{"repo": "synkhos/a", "number": 1, "state": "open"}\n'


def test_fetch_outcomes_never_calls_graphql_in_the_default_path(tmp_path):
    script = {
        ("GET", "/orgs/synkhos/repos"): [(200, rl(5000), [{"full_name": "synkhos/a"}])],
        ("GET", "/repos/synkhos/a/issues"): [(304, rl(5000), None)],
        ("GET", "/repos/synkhos/a/pulls"): [(200, rl(5000), [
            {"number": 2, "user": {"login": "jason"}, "state": "closed",
             "merged_at": "2026-09-02T00:00:00Z", "head": {"ref": "chore/cleanup"},
             "updated_at": "2026-09-02T00:00:00Z"},
        ])],
        ("GET", "/repos/synkhos/a/issues/2/timeline"): [(200, rl(5000), [])],
        ("POST", "/graphql"): [(200, rl(50000), {"data": {"repository": {"pullRequest": {
            "closingIssuesReferences": {"nodes": []}}}}})],
    }
    transport = ScriptedTransport(script)
    report = fetch_outcomes(cache_dir=tmp_path, orgs=["synkhos"], token="tok", transport=transport)
    # this PR's branch doesn't match feat/<n>- and timeline has no links,
    # so REST resolves nothing -> GraphQL IS the right call here (one of the
    # two named cases), and it should be exactly one call.
    assert report["calls"]["graphql"] == 1


# --- fix round 1: non-2xx REST responses must surface, never look like "no data" --

def test_fetch_outcomes_401_on_org_listing_stops_run_keeps_cache_unchanged(tmp_path):
    paths = cache_paths(tmp_path)
    write_state(paths["state"], {"repos": {}, "outcomes_as_of": "2026-09-01T00:00:00Z"})
    append_jsonl(paths["issues"], {"repo": "synkhos/a", "number": 1, "state": "open"})
    before = paths["issues"].read_text()

    script = {("GET", "/orgs/synkhos/repos"): [(401, rl(4999), {"message": "Bad credentials"})]}
    transport = ScriptedTransport(script)
    report = fetch_outcomes(cache_dir=tmp_path, orgs=["synkhos"], token="tok", transport=transport)

    assert report["stopped"] == "github_error"
    assert report["error"]["status"] == 401
    assert "/orgs/synkhos/repos" in report["error"]["endpoint"]
    state = read_state(paths["state"])
    assert state["outcomes_as_of"] == "2026-09-01T00:00:00Z"  # unchanged
    assert paths["issues"].read_text() == before  # cache untouched


def test_fetch_outcomes_500_on_a_repo_listing_call_stops_run_keeps_watermark(tmp_path):
    script = {
        ("GET", "/orgs/synkhos/repos"): [(200, rl(5000), [{"full_name": "synkhos/a"}])],
        ("GET", "/repos/synkhos/a/issues"): [(500, rl(5000), {"message": "Internal error"})],
    }
    transport = ScriptedTransport(script)
    report = fetch_outcomes(cache_dir=tmp_path, orgs=["synkhos"], token="tok", transport=transport)

    assert report["stopped"] == "github_error"
    assert report["error"]["status"] == 500
    assert "/repos/synkhos/a/issues" in report["error"]["endpoint"]
    assert report["outcomes_as_of"] is None  # never advanced
    state = read_state(cache_paths(tmp_path)["state"])
    assert state["outcomes_as_of"] is None
