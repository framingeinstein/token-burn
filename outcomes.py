"""The local outcome cache: PRs and issues for the in-scope orgs, fetched
incrementally REST-first within a quota floor (spec Sec4.2 / Sec5.2).

This builds the fetcher for the LOCAL personal dashboard (2026-09-27 scope
amendment A-2). The shared team bucket is produced by the Synkhos factory
itself and is out of scope here; this module's cache format is the same
Sec4.2 contract (issues/prs as JSONL, latest line per key wins, watermarks
and ETags kept separately in state.json) so the factory can reuse this code.

Only structured fields are cached -- never issue/PR bodies or titles. A body
is read in memory only long enough to pull `requested_by` out of it.
"""
import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from github_client import (
    GitHubError,
    GitHubUnreachable,
    Quota,
    QuotaFloorHit,
    default_transport,
    github_token,
    graphql_closing_issues,
    issue_timeline,
    list_changed_issues,
    list_closed_prs,
    list_org_repos,
)

DEFAULT_CACHE_DIR = Path("~/.token-burn/outcomes").expanduser()
DEFAULT_ORGS = ("synkhos", "FramingEinsteinInc", "framingeinstein")

_PTS_LABEL_RE = re.compile(r"^pts:(unrated|\d+)$")
_ISSUE_BRANCH_RE = re.compile(r"^feat/(\d+)-")
_REQUESTED_BY_RE = re.compile(r"^\*\*Requested by:\*\*\s*([A-Za-z0-9-]{1,39})\s*$", re.MULTILINE)


# --- cache: JSONL latest-line-wins + state.json (watermarks/ETags) ---------

def cache_paths(cache_dir):
    """{"state", "issues", "prs"} -> Path, inside `cache_dir` (spec Sec4.2)."""
    cache_dir = Path(cache_dir)
    return {
        "state": cache_dir / "state.json",
        "issues": cache_dir / "issues.jsonl",
        "prs": cache_dir / "prs.jsonl",
    }


def read_jsonl_latest(path, key_fn):
    """{key_fn(record): record}, latest line per key wins (same convention as
    ledger.py's snapshots)."""
    out = {}
    path = Path(path)
    if not path.exists():
        return out
    with open(path, errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue  # skip corrupt / half-written line
            out[key_fn(rec)] = rec
    return out


def append_jsonl(path, record):
    """Append one record as a single line (atomic single write)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def read_state(path):
    """{"repos": {repo: {"issues": {"since","etag"}, "prs": {"since"}}},
    "outcomes_as_of": iso-or-None}."""
    path = Path(path)
    if not path.exists():
        return {"repos": {}, "outcomes_as_of": None}
    with open(path) as fh:
        return json.load(fh)


def write_state(path, state):
    """Atomic write (tmp + rename) so a crash mid-write never corrupts state.json."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


# --- record field extraction (pure) -----------------------------------------

def _label_names(labels):
    return [l["name"] if isinstance(l, dict) else l for l in (labels or [])]


def pts_and_source(labels, events):
    """`(pts, pts_source)` from the `pts:N` / `pts:unrated` label (spec Sec4.2,
    R-5).

    `pts_source` is `None` for now (fix round 1, controller ruling
    2026-09-27): the rating skill runs from sessions using the SAME user's
    GitHub token as a manual label change, so a timeline-actor heuristic
    (e.g. a `[bot]`-suffixed login) can't actually tell "rater" apart from
    "human" -- it mislabels rater-applied labels as human. `pts` is still
    read from the label as-is.

    SEAM for synkhos/factory#166: once that issue defines the R-6
    rating-comment markers, derive `pts_source` from them instead --
    "rater" or "backfill" read from the marker itself, "human" when a
    `labeled` timeline event for this label postdates the marker's comment
    (i.e. the label was changed after the rating). `events` is kept as a
    parameter for that future seam; it is unused today.
    """
    label_name = next((l for l in labels if l.startswith("pts:")), None)
    if not label_name:
        return None, None
    m = _PTS_LABEL_RE.match(label_name)
    if not m:
        return None, None
    val = m.group(1)
    if val == "unrated":
        return None, None
    return int(val), None


def requested_by_and_source(body, author):
    """(`requested_by`, `requester_source`) per spec Sec2 / R-10: the
    `**Requested by:** <login>` header line in the body, else the author."""
    m = _REQUESTED_BY_RE.search(body or "")
    if m:
        return m.group(1), "requested_by"
    return author, "author"


def reopened_at_from_timeline(events):
    """`created_at` of every `reopened` timeline event, in order."""
    return [ev["created_at"] for ev in events
            if ev.get("event") == "reopened" and ev.get("created_at")]


def approved_by_from_timeline(events):
    """The actor of the (latest) `approved` label event, for reference only
    (spec Sec2: "not used for attribution")."""
    actor = None
    for ev in events:
        if ev.get("event") == "labeled" and (ev.get("label") or {}).get("name") == "approved":
            a = (ev.get("actor") or {}).get("login")
            if a:
                actor = a
    return actor


def review_rounds_from_timeline(events):
    """Count of `reviewed` timeline events."""
    return sum(1 for ev in events if ev.get("event") == "reviewed")


def closes_from_timeline(events):
    """Issue numbers a PR closes, from `connected` / `cross-referenced` timeline
    events (spec Sec5.2 row 5, REST first attempt)."""
    closes = set()
    for ev in events:
        if ev.get("event") not in ("connected", "cross-referenced"):
            continue
        src = (ev.get("source") or {}).get("issue") or {}
        if src.get("number") and "pull_request" not in src:
            closes.add(src["number"])
    return sorted(closes)


def closes_from_branch(head_ref):
    """`feat/<n>-...` branch pattern fallback (spec Sec5.2 row 5, REST second
    attempt)."""
    m = _ISSUE_BRANCH_RE.match(head_ref or "")
    return [int(m.group(1))] if m else []


def resolve_pr_closes(repo, pr_number, head_ref, events, token, quota, transport,
                       graphql=graphql_closing_issues):
    """REST first (timeline, then branch pattern); GraphQL only when both
    resolve nothing (spec Sec5.2 row 5 -- the one PR-side named GraphQL case)."""
    closes = closes_from_timeline(events)
    if closes:
        return closes
    closes = closes_from_branch(head_ref)
    if closes:
        return closes
    return graphql(repo, pr_number, token, quota, transport)


def build_issue_record(repo, issue, events):
    """The Sec4.2 issue record: only structured fields, never body or title."""
    labels = _label_names(issue.get("labels"))
    pts, pts_source = pts_and_source(labels, events)
    requested_by, requester_source = requested_by_and_source(
        issue.get("body"), (issue.get("user") or {}).get("login"))
    return {
        "repo": repo,
        "number": issue["number"],
        "author": (issue.get("user") or {}).get("login"),
        "requested_by": requested_by,
        "requester_source": requester_source,
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "closed_at": issue.get("closed_at"),
        "reopened_at": reopened_at_from_timeline(events),
        "pts": pts,
        "pts_source": pts_source,
        "labels": labels,
        "approved_by": approved_by_from_timeline(events),
    }


def build_pr_record(repo, pr, events, closes):
    """The Sec4.2 PR record. `reverted_by` is always null here -- T4 computes
    reverts. `state` is "merged" when `merged_at` is set (more informative
    than GitHub's raw "closed" for a merged PR), else GitHub's own state."""
    return {
        "repo": repo,
        "number": pr["number"],
        "author": (pr.get("user") or {}).get("login"),
        "head_ref": (pr.get("head") or {}).get("ref"),
        "state": "merged" if pr.get("merged_at") else pr.get("state"),
        "merged_at": pr.get("merged_at"),
        "closes": closes,
        "reverted_by": None,
        "review_rounds": review_rounds_from_timeline(events),
    }


# --- fetch orchestration -----------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _process_issues(repo, repo_state, token, quota, transport, paths, report):
    """Fetch + record changed issues for `repo`; advances the repo's issue
    watermark/ETag only past what was actually processed, even if a
    QuotaFloorHit/GitHubUnreachable cuts the loop short partway through."""
    issues_state = repo_state.setdefault("issues", {})
    max_seen = issues_state.get("since")
    items, new_etag, unchanged = list_changed_issues(
        repo, token, quota, transport,
        since=issues_state.get("since"), etag=issues_state.get("etag"))
    try:
        for issue in items:
            events = issue_timeline(repo, issue["number"], token, quota, transport)
            append_jsonl(paths["issues"], build_issue_record(repo, issue, events))
            report["issues"] += 1
            upd = issue.get("updated_at")
            if upd and (not max_seen or upd > max_seen):
                max_seen = upd
    finally:
        if not unchanged and max_seen:
            issues_state["since"] = max_seen
        if new_etag:
            issues_state["etag"] = new_etag


def _process_prs(repo, repo_state, token, quota, transport, paths, report):
    """Fetch + record closed/merged PRs for `repo`, resolving `closes[]`
    REST-first; advances the repo's PR watermark only past what was actually
    processed."""
    prs_state = repo_state.setdefault("prs", {})
    since = prs_state.get("since")
    max_seen = since
    prs = list_closed_prs(repo, token, quota, transport, since=since)
    try:
        for pr in prs:
            events = issue_timeline(repo, pr["number"], token, quota, transport)
            closes = resolve_pr_closes(
                repo, pr["number"], (pr.get("head") or {}).get("ref"),
                events, token, quota, transport)
            append_jsonl(paths["prs"], build_pr_record(repo, pr, events, closes))
            report["prs"] += 1
            upd = pr.get("updated_at")
            if upd and (not max_seen or upd > max_seen):
                max_seen = upd
    finally:
        if max_seen:
            prs_state["since"] = max_seen


def fetch_outcomes(cache_dir=DEFAULT_CACHE_DIR, orgs=DEFAULT_ORGS, token=None,
                    get_token=github_token, transport=default_transport, quota=None):
    """Fetch the outcome cache for `orgs` (REST-first, spec Sec5.2).

    Stops cleanly at the quota floor, when GitHub is unreachable, or on a
    surfaced non-2xx REST error (401/403/404/5xx/... -- fix round 1: these
    used to fall through as "no items" and could report a clean run with
    nothing actually verified), keeping whatever cache and watermarks already
    exist; the next run resumes from there. `outcomes_as_of` only advances on
    a run that finishes every repo without being cut short (spec Sec8: every
    failure row keeps "outcomes as of" as it was).

    Returns a report: {"issues": n, "prs": n, "calls": {"core": x, "graphql": y},
    "stopped": None | "quota_floor" | "unreachable" | "github_error",
    "repos_seen": [...], "outcomes_as_of": iso-or-None,
    "error": {"status": int, "endpoint": str} (only when stopped == "github_error")}.
    """
    paths = cache_paths(cache_dir)
    state = read_state(paths["state"])
    quota = quota or Quota()
    token = token or get_token()
    if not token:
        raise RuntimeError("no GitHub token (gh auth token failed); configure one explicitly")

    report = {"issues": 0, "prs": 0, "calls": quota.calls, "stopped": None,
              "repos_seen": [], "outcomes_as_of": state.get("outcomes_as_of")}

    try:
        repos = []
        for org in orgs:
            repos.extend(list_org_repos(org, token, quota, transport))
    except QuotaFloorHit:
        report["stopped"] = "quota_floor"
        report["calls"] = dict(quota.calls)
        return report
    except GitHubUnreachable:
        report["stopped"] = "unreachable"
        report["calls"] = dict(quota.calls)
        return report
    except GitHubError as e:
        report["stopped"] = "github_error"
        report["error"] = {"status": e.status, "endpoint": e.url}
        report["calls"] = dict(quota.calls)
        return report

    for repo in repos:
        report["repos_seen"].append(repo)
        repo_state = state["repos"].setdefault(repo, {})
        try:
            _process_issues(repo, repo_state, token, quota, transport, paths, report)
            _process_prs(repo, repo_state, token, quota, transport, paths, report)
        except QuotaFloorHit:
            write_state(paths["state"], state)
            report["stopped"] = "quota_floor"
            report["calls"] = dict(quota.calls)
            return report
        except GitHubUnreachable:
            write_state(paths["state"], state)
            report["stopped"] = "unreachable"
            report["calls"] = dict(quota.calls)
            return report
        except GitHubError as e:
            write_state(paths["state"], state)
            report["stopped"] = "github_error"
            report["error"] = {"status": e.status, "endpoint": e.url}
            report["calls"] = dict(quota.calls)
            return report
        write_state(paths["state"], state)

    state["outcomes_as_of"] = _now_iso()
    write_state(paths["state"], state)
    report["outcomes_as_of"] = state["outcomes_as_of"]
    report["calls"] = dict(quota.calls)
    return report


# --- CLI ---------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Fetch the local GitHub outcome cache (issues + PRs).")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    ap.add_argument("--org", action="append", dest="orgs", default=None,
                     help="org to fetch (repeatable); default: %s" % ", ".join(DEFAULT_ORGS))
    ap.add_argument("--floor-core", type=int, default=None)
    ap.add_argument("--floor-graphql", type=int, default=None)
    args = ap.parse_args()

    quota_kwargs = {}
    if args.floor_core is not None:
        quota_kwargs["floor_core"] = args.floor_core
    if args.floor_graphql is not None:
        quota_kwargs["floor_graphql"] = args.floor_graphql
    quota = Quota(**quota_kwargs) if quota_kwargs else None

    report = fetch_outcomes(cache_dir=args.cache_dir, orgs=args.orgs or DEFAULT_ORGS, quota=quota)
    print(json.dumps(report, indent=2))
    if report["stopped"]:
        raise SystemExit(1 if report["stopped"] in ("unreachable", "github_error") else 0)


if __name__ == "__main__":
    main()
