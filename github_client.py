"""Low-level GitHub REST/GraphQL client (spec Sec5.2).

REST first, GraphQL only for the two named fallbacks (PR -> closing issues
when REST can't resolve it, and original issue bodies for backfill). GitHub
has two independent primary pools -- REST `core` and `graphql` -- each with
its own `x-ratelimit-remaining`. A shared `Quota` checks the floor BEFORE a
call is made (not after), so the fetcher never overdraws either pool; it's
the caller's job to keep going with what already succeeded when the floor is
hit or GitHub is unreachable.

The HTTP transport is an injected callable `(method, url, headers, body) ->
(status, headers, parsed_json)` so tests never touch the network; the real
`gh auth token` lookup is likewise injectable.
"""
import json
import time
import re
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request
from urllib.request import urlopen as _urlopen

API_ROOT = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"

DEFAULT_FLOOR_CORE = 1000
DEFAULT_FLOOR_GRAPHQL = 1000


class GitHubUnreachable(Exception):
    """Transport-level failure (DNS, connection, timeout) -- not an HTTP error
    response. The caller keeps its cache and watermarks unchanged (spec Sec8)."""


class GitHubError(Exception):
    """A REST response outside {200, 304} -- 401/403 (incl. secondary rate
    limit)/404/5xx/etc. Carries only `status` and the request `url` (never
    headers or the token), so it's always safe to log or report. Fix round 1:
    previously a non-2xx whose body wasn't a JSON list fell through as "no
    items", so a bad token or a typo'd org/repo looked like a clean, empty
    run. Now it's surfaced so the caller can tell "nothing changed" apart
    from "the call failed"."""

    def __init__(self, status, url):
        self.status = status
        self.url = url
        super().__init__(f"GitHub REST {status} from {url}")


class QuotaFloorHit(Exception):
    """Raised by Quota.check() before a call would draw a pool below its floor."""

    def __init__(self, kind, remaining):
        self.kind = kind
        self.remaining = remaining
        super().__init__(f"{kind} quota at {remaining}, below floor")


# --- token -------------------------------------------------------------

def github_token(run=subprocess.run):
    """The machine's GitHub token via `gh auth token` (real network-adjacent
    call; always inject `run` in tests). Never logged or written anywhere."""
    try:
        result = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            token = result.stdout.strip()
            return token or None
    except Exception:
        pass
    return None


# --- transport -----------------------------------------------------------

_RETRY_DELAYS = (1.0, 3.0)   # seconds before the 2nd and 3rd attempt


def default_transport(method, url, headers, body=None, urlopen=_urlopen, sleep=time.sleep):
    """Real HTTP transport (urllib, stdlib only). Returns (status, headers,
    parsed_json) for both success and HTTP-error responses (including 304, which
    urllib raises as an HTTPError); raises GitHubUnreachable for connection-level
    failures so callers can tell "no change" apart from "GitHub is down"."""
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for delay in (0.0,) + _RETRY_DELAYS:
        if delay:
            sleep(delay)
        try:
            return _attempt(Request(url, data=data, headers=headers, method=method), urlopen)
        except (URLError, TimeoutError, ConnectionError) as e:
            # Transient connection-level failure (reset, DNS blip, read timeout --
            # a read timeout is a TimeoutError, not a URLError): retry, then give up.
            last = e
    raise GitHubUnreachable(f"{type(last).__name__}: {last}") from last


def _attempt(req, urlopen):
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            parsed = json.loads(raw) if raw else None
            return resp.status, hdrs, parsed
    except HTTPError as e:
        hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = None
        return e.code, hdrs, parsed


# --- quota -----------------------------------------------------------------

class Quota:
    """Tracks the two independent primary pools (spec Sec5.2): REST `core` and
    `graphql`. `remaining` starts unknown per pool (the first call of a kind is
    always allowed); after each call it's set from `x-ratelimit-remaining`."""

    def __init__(self, floor_core=DEFAULT_FLOOR_CORE, floor_graphql=DEFAULT_FLOOR_GRAPHQL):
        self.remaining = {"core": None, "graphql": None}
        self.floor = {"core": floor_core, "graphql": floor_graphql}
        self.calls = {"core": 0, "graphql": 0}

    def check(self, kind):
        r = self.remaining.get(kind)
        if r is not None and r < self.floor[kind]:
            raise QuotaFloorHit(kind, r)

    def record(self, kind, headers):
        self.calls[kind] += 1
        v = (headers or {}).get("x-ratelimit-remaining")
        if v is not None:
            try:
                self.remaining[kind] = int(v)
            except (TypeError, ValueError):
                pass


def _request(kind, method, url, token, quota, transport, headers=None, body=None):
    """One call against `kind`'s pool: checks the floor first, then updates the
    pool's remaining count from the response headers."""
    quota.check(kind)
    hdrs = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    hdrs.update(headers or {})
    status, resp_headers, parsed = transport(method, url, hdrs, body)
    quota.record(kind, resp_headers)
    return status, resp_headers, parsed


# --- pagination ------------------------------------------------------------

_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


def next_page_url(headers):
    """The `rel="next"` URL from a `Link` response header, or None."""
    link = (headers or {}).get("link")
    if not link:
        return None
    for url, rel in _LINK_RE.findall(link):
        if rel == "next":
            return url
    return None


def rest_pages(url, token, quota, transport, etag=None, stop=None):
    """Yields (status, items, headers) for each page of a paginated REST GET.

    - 304 (etag matched, only meaningful on the first page): yields
      `(304, None, headers)` once and stops -- no quota beyond that one call.
    - `stop(item)`: when it's true for an item, that item and the rest of the
      page are dropped, the truncated page is yielded, and no further page is
      requested (the watermark boundary, spec Sec5.2 row 2).
    - Any other status (401/403/404/5xx/...) raises `GitHubError` -- it is
      never treated as "no items" (fix round 1: that used to hide a bad token
      or a bad org/repo behind an apparently clean, empty run).
    """
    page_url = url
    first = True
    while page_url:
        headers = {"If-None-Match": etag} if (etag and first) else None
        status, resp_headers, parsed = _request("core", "GET", page_url, token, quota, transport, headers)
        first = False
        if status == 304:
            yield status, None, resp_headers
            return
        if status != 200:
            raise GitHubError(status, page_url)
        items = parsed if isinstance(parsed, list) else []
        if stop is not None:
            kept = []
            hit = False
            for item in items:
                if stop(item):
                    hit = True
                    break
                kept.append(item)
            yield status, kept, resp_headers
            if hit:
                return
        else:
            yield status, items, resp_headers
        page_url = next_page_url(resp_headers)


# --- REST calls --------------------------------------------------------

def list_owner_repos(owner, token, quota, transport):
    """Every repo belonging to `owner` (full `owner/name`), spec Sec5.2's
    enumeration -- extended (fix round 2) to cover both shapes an in-scope
    name can be: an org or a user account. Tries `/orgs/{owner}/repos`
    first; on a 404 (not an org) falls back to `/users/{owner}/repos`. Any
    other `GitHubError` (401/403/5xx/...) is NOT retried -- it propagates so
    the caller can isolate it to this one owner."""
    org_url = f"{API_ROOT}/orgs/{owner}/repos?per_page=100&type=all"
    try:
        repos = []
        for _status, items, _headers in rest_pages(org_url, token, quota, transport):
            if items:
                repos.extend(items)
        return [r["full_name"] for r in repos]
    except GitHubError as e:
        if e.status != 404:
            raise

    user_url = f"{API_ROOT}/users/{owner}/repos?per_page=100&type=all"
    repos = []
    for _status, items, _headers in rest_pages(user_url, token, quota, transport):
        if items:
            repos.extend(items)
    return [r["full_name"] for r in repos]


def list_changed_issues(repo, token, quota, transport, since=None, etag=None):
    """`GET /repos/{r}/issues?state=all&since=<watermark>` with `If-None-Match`
    (spec Sec5.2 row 1). Returns `(items, new_etag, unchanged)`; `unchanged` is
    True on a 304 (no quota beyond the one call, and `items` is empty). PRs
    (which this endpoint also returns) are filtered out -- they come from
    `list_closed_prs` instead."""
    params = "state=all&per_page=100&sort=updated&direction=asc"
    if since:
        params += f"&since={since}"
    url = f"{API_ROOT}/repos/{repo}/issues?{params}"
    items = []
    new_etag = etag
    unchanged = False
    for status, page_items, headers in rest_pages(url, token, quota, transport, etag=etag):
        if status == 304:
            unchanged = True
            break
        if headers and headers.get("etag"):
            new_etag = headers["etag"]
        for issue in page_items or []:
            if "pull_request" not in issue:
                items.append(issue)
    return items, new_etag, unchanged


def list_closed_prs(repo, token, quota, transport, since=None):
    """`GET /repos/{r}/pulls?state=closed&sort=updated&direction=desc`, paging
    back only to `since` (spec Sec5.2 row 2)."""
    url = f"{API_ROOT}/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100"
    stop = (lambda pr: pr.get("updated_at", "") <= since) if since else None
    prs = []
    for _status, items, _headers in rest_pages(url, token, quota, transport, stop=stop):
        if items:
            prs.extend(items)
    return prs


def issue_timeline(repo, number, token, quota, transport):
    """All timeline events for an issue or PR (spec Sec5.2 row 3): label actor,
    reopen/close, PR<->issue links, review events. No watermark -- small volume."""
    url = f"{API_ROOT}/repos/{repo}/issues/{number}/timeline?per_page=100"
    events = []
    for _status, items, _headers in rest_pages(url, token, quota, transport):
        if items:
            events.extend(items)
    return events


# --- GraphQL (the two named fallbacks only, spec Sec5.2 rows 5-6) ----------

_CLOSING_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 50) { nodes { number } }
    }
  }
}
"""


def graphql_closing_issues(repo, pr_number, token, quota, transport):
    """`closingIssuesReferences` -- only called when REST (timeline + branch
    pattern) resolves nothing for a PR (spec Sec5.2 row 5)."""
    owner, name = repo.split("/", 1)
    body = {"query": _CLOSING_ISSUES_QUERY,
            "variables": {"owner": owner, "name": name, "number": pr_number}}
    status, _headers, parsed = _request("graphql", "POST", GRAPHQL_URL, token, quota, transport, body=body)
    if status != 200 or not parsed or parsed.get("errors"):
        return []
    pr = (((parsed.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
    nodes = (pr.get("closingIssuesReferences") or {}).get("nodes") or []
    return sorted({n["number"] for n in nodes if n.get("number")})


def graphql_original_bodies(repo, numbers, token, quota, transport):
    """Original (as-filed) issue bodies via `userContentEdits`, batched up to 50
    per query (spec Sec5.2 row 6, backfill only). Not called by the default
    fetch -- the local dashboard only needs the capability behind this function.
    The first edit's `diff` (the pre-edit content) is the original body; an
    issue with no edits is returned as its current body."""
    owner, name = repo.split("/", 1)
    numbers = list(numbers)[:50]
    aliases = "\n".join(
        f'i{idx}: issue(number: {n}) {{ '
        f'body userContentEdits(first: 1, orderBy: {{field: EDITED_AT, direction: ASC}}) '
        f'{{ nodes {{ diff editedAt }} }} }}'
        for idx, n in enumerate(numbers)
    )
    query = "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { " + aliases + " } }"
    body = {"query": query, "variables": {"owner": owner, "name": name}}
    status, _headers, parsed = _request("graphql", "POST", GRAPHQL_URL, token, quota, transport, body=body)
    if status != 200 or not parsed or parsed.get("errors"):
        return {}
    repo_data = (parsed.get("data") or {}).get("repository") or {}
    out = {}
    for idx, n in enumerate(numbers):
        node = repo_data.get(f"i{idx}") or {}
        edits = (node.get("userContentEdits") or {}).get("nodes") or []
        out[n] = edits[0]["diff"] if edits and edits[0].get("diff") is not None else node.get("body")
    return out
