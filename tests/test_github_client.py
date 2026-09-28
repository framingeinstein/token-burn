# tests/test_github_client.py
"""GitHub REST/GraphQL client: quota floor, ETag/304, watermark paging (spec Sec5.2)."""
import pytest

from github_client import (
    GitHubError,
    GitHubUnreachable,
    Quota,
    QuotaFloorHit,
    _request,
    github_token,
    graphql_closing_issues,
    graphql_original_bodies,
    issue_timeline,
    list_changed_issues,
    list_closed_prs,
    list_owner_repos,
    next_page_url,
    rest_pages,
)


class FakeRun:
    """Stub for subprocess.run, injectable into github_token()."""
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, *a, **kw):
        return self


class FakeTransport:
    """Queue of canned (status, headers, body) responses, matched in call order.
    Records every call so tests can assert what was (and wasn't) invoked."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if not self.responses:
            raise AssertionError(f"no more stubbed responses for {method} {url}")
        return self.responses.pop(0)


def rl(remaining, extra=None):
    h = {"x-ratelimit-remaining": str(remaining)}
    if extra:
        h.update(extra)
    return h


# --- token -------------------------------------------------------------

def test_github_token_from_gh_auth_token():
    assert github_token(run=FakeRun(stdout="ghp_abc123\n")) == "ghp_abc123"


def test_github_token_missing_returns_none():
    assert github_token(run=FakeRun(stdout="", returncode=1)) is None


# --- quota floor ---------------------------------------------------------

def test_quota_allows_first_call_with_unknown_remaining():
    q = Quota(floor_core=1000)
    q.check("core")  # no exception: nothing observed yet


def test_quota_records_remaining_and_blocks_next_call_below_floor():
    q = Quota(floor_core=1000, floor_graphql=1000)
    q.record("core", rl(500))
    with pytest.raises(QuotaFloorHit) as exc:
        q.check("core")
    assert exc.value.kind == "core"
    assert exc.value.remaining == 500


def test_quota_pools_are_independent():
    q = Quota()
    q.record("core", rl(1))
    q.check("graphql")  # graphql pool untouched, still allowed


def test_request_raises_quota_floor_before_making_the_call(monkeypatch):
    q = Quota(floor_core=1000)
    q.record("core", rl(999))
    transport = FakeTransport([(200, rl(998), {})])
    with pytest.raises(QuotaFloorHit):
        _request("core", "GET", "https://api.github.com/x", "tok", q, transport)
    assert transport.calls == []  # never actually called


def test_request_updates_quota_from_response_headers():
    q = Quota()
    transport = FakeTransport([(200, rl(4500), {"ok": True})])
    status, headers, body = _request("core", "GET", "https://api.github.com/x", "tok", q, transport)
    assert status == 200 and body == {"ok": True}
    assert q.remaining["core"] == 4500
    assert q.calls["core"] == 1


def test_request_sends_bearer_token_and_never_logs_it(capsys):
    q = Quota()
    transport = FakeTransport([(200, rl(100), {})])
    _request("core", "GET", "https://api.github.com/x", "sekret-tok", q, transport)
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer sekret-tok"
    assert "sekret-tok" not in capsys.readouterr().out


# --- pagination ------------------------------------------------------------

def test_next_page_url_parses_link_header():
    headers = {"link": '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=9>; rel="last"'}
    assert next_page_url(headers) == "https://api.github.com/x?page=2"


def test_next_page_url_none_when_absent():
    assert next_page_url({}) is None
    assert next_page_url({"link": '<https://x>; rel="last"'}) is None


def test_rest_pages_follows_link_header_until_exhausted():
    q = Quota()
    transport = FakeTransport([
        (200, rl(4999, {"link": '<https://api.github.com/x?page=2>; rel="next"'}), [{"n": 1}]),
        (200, rl(4998), [{"n": 2}]),
    ])
    pages = list(rest_pages("https://api.github.com/x", "tok", q, transport))
    assert [p[1] for p in pages] == [[{"n": 1}], [{"n": 2}]]
    assert len(transport.calls) == 2


def test_rest_pages_304_yields_no_items_and_costs_one_call():
    q = Quota()
    transport = FakeTransport([(304, rl(499), None)])
    pages = list(rest_pages("https://api.github.com/x", "tok", q, transport, etag='"abc"'))
    assert len(pages) == 1
    assert pages[0][0] == 304
    assert pages[0][1] is None
    assert len(transport.calls) == 1
    assert transport.calls[0]["headers"]["If-None-Match"] == '"abc"'
    assert q.calls["core"] == 1


def test_rest_pages_stop_predicate_truncates_and_halts_paging():
    q = Quota()
    transport = FakeTransport([
        (200, rl(100, {"link": '<https://api.github.com/x?page=2>; rel="next"'}),
         [{"updated_at": "2026-09-27T00:00:00Z"}, {"updated_at": "2026-09-20T00:00:00Z"}]),
    ])
    stop = lambda item: item["updated_at"] <= "2026-09-25T00:00:00Z"
    pages = list(rest_pages("https://api.github.com/x", "tok", q, transport, stop=stop))
    assert len(pages) == 1
    assert pages[0][1] == [{"updated_at": "2026-09-27T00:00:00Z"}]
    assert len(transport.calls) == 1  # never fetched page 2


# --- higher-level REST calls -------------------------------------------

def test_list_owner_repos_paginates_and_returns_full_names():
    q = Quota()
    transport = FakeTransport([
        (200, rl(100), [{"full_name": "synkhos/a"}, {"full_name": "synkhos/b"}]),
    ])
    assert list_owner_repos("synkhos", "tok", q, transport) == ["synkhos/a", "synkhos/b"]


def test_list_owner_repos_falls_back_to_users_on_404(monkeypatch):
    # fix round 2: an in-scope name may be a user account, not an org --
    # /orgs/{owner}/repos 404s, so /users/{owner}/repos is tried next.
    q = Quota()
    transport = FakeTransport([
        (404, rl(4999), {"message": "Not Found"}),
        (200, rl(4998), [{"full_name": "framingeinstein/site"}]),
    ])
    result = list_owner_repos("framingeinstein", "tok", q, transport)
    assert result == ["framingeinstein/site"]
    assert transport.calls[0]["url"].startswith("https://api.github.com/orgs/framingeinstein/repos")
    assert transport.calls[1]["url"].startswith("https://api.github.com/users/framingeinstein/repos")


def test_list_owner_repos_does_not_fall_back_on_non_404_error():
    q = Quota()
    transport = FakeTransport([(500, rl(4999), {"message": "boom"})])
    with pytest.raises(GitHubError) as exc:
        list_owner_repos("synkhos", "tok", q, transport)
    assert exc.value.status == 500
    assert len(transport.calls) == 1  # never tried /users


def test_list_changed_issues_unchanged_304_costs_no_extra_quota_and_returns_empty():
    q = Quota()
    transport = FakeTransport([(304, rl(500), None)])
    items, new_etag, unchanged = list_changed_issues(
        "synkhos/a", "tok", q, transport, since="2026-09-20T00:00:00Z", etag='"old-etag"')
    assert items == []
    assert unchanged is True
    assert new_etag == '"old-etag"'
    assert q.calls["core"] == 1


def test_list_changed_issues_filters_out_pull_requests():
    q = Quota()
    transport = FakeTransport([
        (200, rl(500, {"etag": '"new"'}), [
            {"number": 1, "user": {"login": "a"}},
            {"number": 2, "user": {"login": "b"}, "pull_request": {"url": "..."}},
        ]),
    ])
    items, new_etag, unchanged = list_changed_issues("synkhos/a", "tok", q, transport)
    assert [i["number"] for i in items] == [1]
    assert unchanged is False
    assert new_etag == '"new"'


def test_list_closed_prs_stops_paging_at_watermark():
    q = Quota()
    transport = FakeTransport([
        (200, rl(500, {"link": '<https://api.github.com/x?page=2>; rel="next"'}), [
            {"number": 10, "updated_at": "2026-09-27T00:00:00Z"},
            {"number": 9, "updated_at": "2026-09-10T00:00:00Z"},
        ]),
    ])
    prs = list_closed_prs("synkhos/a", "tok", q, transport, since="2026-09-15T00:00:00Z")
    assert [p["number"] for p in prs] == [10]
    assert len(transport.calls) == 1


def test_issue_timeline_paginates():
    q = Quota()
    transport = FakeTransport([(200, rl(500), [{"event": "reopened"}])])
    events = issue_timeline("synkhos/a", 5, "tok", q, transport)
    assert events == [{"event": "reopened"}]


# --- GraphQL: only called where the transport says to ----------------------

def test_graphql_closing_issues_uses_graphql_quota_pool():
    q = Quota()
    transport = FakeTransport([
        (200, rl(50000), {"data": {"repository": {"pullRequest": {
            "closingIssuesReferences": {"nodes": [{"number": 7}, {"number": 3}]}}}}}),
    ])
    result = graphql_closing_issues("synkhos/a", 42, "tok", q, transport)
    assert result == [3, 7]
    assert q.calls == {"core": 0, "graphql": 1}
    assert transport.calls[0]["url"].endswith("/graphql")


def test_graphql_closing_issues_handles_error_response():
    q = Quota()
    transport = FakeTransport([(200, rl(50000), {"errors": [{"message": "nope"}]})])
    assert graphql_closing_issues("synkhos/a", 42, "tok", q, transport) == []


def test_graphql_original_bodies_batches_and_uses_graphql_pool():
    q = Quota()
    transport = FakeTransport([
        (200, rl(50000), {"data": {"repository": {
            "i0": {"body": "current", "userContentEdits": {"nodes": []}},
            "i1": {"body": "current2", "userContentEdits": {"nodes": [{"diff": "original body", "editedAt": "x"}]}},
        }}}),
    ])
    out = graphql_original_bodies("synkhos/a", [1, 2], "tok", q, transport)
    assert out == {1: "current", 2: "original body"}
    assert q.calls == {"core": 0, "graphql": 1}


# --- unreachable -------------------------------------------------------

def test_default_transport_wraps_connection_errors():
    import github_client as gc

    def boom(req, timeout=None):
        from urllib.error import URLError
        raise URLError("nope")

    with pytest.raises(GitHubUnreachable):
        gc.default_transport("GET", "https://api.github.com/x", {}, urlopen=boom,
                             sleep=lambda s: None)


# --- fix round 1: non-2xx surfaces as GitHubError, never as "no items" ------

def test_rest_pages_raises_github_error_on_401():
    q = Quota()
    transport = FakeTransport([(401, rl(4999), {"message": "Bad credentials"})])
    with pytest.raises(GitHubError) as exc:
        list(rest_pages("https://api.github.com/orgs/x/repos", "sekret-tok", q, transport))
    assert exc.value.status == 401
    assert "orgs/x/repos" in exc.value.url
    assert "sekret-tok" not in str(exc.value)  # no token in the surfaced error


def test_rest_pages_raises_github_error_on_500():
    q = Quota()
    transport = FakeTransport([(500, rl(4999), {"message": "Internal error"})])
    with pytest.raises(GitHubError) as exc:
        list(rest_pages("https://api.github.com/repos/o/r/issues", "tok", q, transport))
    assert exc.value.status == 500


def test_rest_pages_raises_github_error_on_404():
    q = Quota()
    transport = FakeTransport([(404, rl(4999), {"message": "Not Found"})])
    with pytest.raises(GitHubError) as exc:
        list(rest_pages("https://api.github.com/orgs/nope/repos", "tok", q, transport))
    assert exc.value.status == 404


def test_list_owner_repos_propagates_github_error():
    q = Quota()
    transport = FakeTransport([(403, rl(4999), {"message": "secondary rate limit"})])
    with pytest.raises(GitHubError):
        list_owner_repos("synkhos", "tok", q, transport)


def test_default_transport_retries_transient_errors_then_succeeds():
    import io
    import github_client as gc
    from urllib.error import URLError
    calls = []

    class Resp(io.BytesIO):
        status = 200
        def getheaders(self):
            return []
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def flaky(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise URLError("reset")
        if len(calls) == 2:
            raise TimeoutError("read timed out")
        return Resp(b"[]")

    status, _, parsed = gc.default_transport("GET", "https://api.github.com/x", {},
                                             urlopen=flaky, sleep=lambda s: None)
    assert (status, parsed, len(calls)) == (200, [], 3)


def test_default_transport_gives_up_after_retries_with_the_reason():
    import github_client as gc

    def dead(req, timeout=None):
        raise TimeoutError("read timed out")

    with pytest.raises(GitHubUnreachable) as exc:
        gc.default_transport("GET", "https://api.github.com/x", {}, urlopen=dead,
                             sleep=lambda s: None)
    assert "timed out" in str(exc.value)
