"""Join spend to PRs, issues and requesters (spec Sec2, Sec3B, Sec3C, Sec5.3).

Pure functions only -- no I/O. A thin loader elsewhere (e.g. combining
`usage_records.read_usage_records` with `outcomes.read_jsonl_latest`) reads the
usage records / issue records / PR records and hands the resulting lists here;
nothing in this module touches the filesystem or the network.

Join chain (spec Sec5.3):

1. Local session -> PR by `(repo, branch) == (pr.repo, pr.head_ref)`.
   Branches `main`, `master`, `HEAD` and `null` never join (spec, verbatim).
2. PR -> issue by `pr.closes[]` (the lowest issue number when a PR closes
   several -- not spec-fixed, a T4 choice so each record still resolves to
   exactly ONE issue for $ conservation), falling back to the issue-branch
   pattern (`feat/<n>-`, `<n>-`, `<prefix>/<n>-`) on the PR's own branch.
3. Factory record -> issue directly from the record's own `issue` field (the
   bucket-path issue T1 already resolved) -- this is factory-only; a local
   session with an `issue` field but no matching PR stays unattributed rather
   than joining directly, since #1/#2 are how local work is meant to prove it
   shipped.
4. Issue -> requester: read straight off the issue record's own
   `requested_by`/`requester_source` (spec Sec2; `outcomes.requested_by_and_source`
   already resolved this at fetch time, header line else issue author).
5. Anything that doesn't reach an issue is unattributed ("planning, review and
   ops"). Nothing is spread across PRs, nothing is dropped -- every dollar
   lands in exactly one of attributed or unattributed for its day
   (`daily_attribution_totals`).

"Shipped" (spec Sec3C): the issue is closed `completed` AND at least one
merged PR joins to it via rule 2 (from either direction -- `prs_by_issue` is
the same `pr_issue_number` used for `attribute_record`, so "shipped" and
"attributed" agree on which PR belongs to which issue).

Durability (spec Sec3B): a merged PR is durable unless a merged PR that
`reverts` it exists (T3's `outcomes.build_pr_record` `reverts` field, R7,
inverted here into `reverted_by`), or its linked issue was reopened within 14
days after the merge. Durability is undefined (`None`) for a PR that never
merged.
"""
from collections import defaultdict
from datetime import datetime, timedelta

from outcomes import closes_from_branch

_NEVER_JOIN_BRANCHES = {None, "main", "master", "HEAD"}
_DURABILITY_WINDOW_DAYS = 14


def _parse_ts(ts):
    """A full ISO-8601 timestamp (e.g. `merged_at`, `reopened_at[]`) -> aware
    `datetime`, or `None` -- never raises (same convention as
    `parse.local_day`)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# --- indices -----------------------------------------------------------------

def index_issues(issues):
    """`{(repo, number): issue record}`."""
    return {(i["repo"], i["number"]): i for i in issues}


def index_prs_by_branch(prs):
    """`{(repo, head_ref): [pr, ...]}`; PRs on a never-join branch (spec
    Sec5.3 rule 1: `main`/`master`/`HEAD`/null) are excluded entirely."""
    idx = defaultdict(list)
    for pr in prs:
        if pr.get("repo") is None or pr.get("head_ref") in _NEVER_JOIN_BRANCHES:
            continue
        idx[(pr["repo"], pr["head_ref"])].append(pr)
    return dict(idx)


def select_pr_for_branch(candidates):
    """When several PRs share a `(repo, branch)` key (a reused branch name),
    prefer a merged one -- that's the PR whose durability/shipped status
    actually matters -- else the highest PR number (most recent). `None` for
    an empty list."""
    if not candidates:
        return None
    merged = [p for p in candidates if p.get("state") == "merged"]
    pool = merged or candidates
    return max(pool, key=lambda p: p["number"])


# --- rule 2: PR -> issue -------------------------------------------------

def pr_issue_number(pr):
    """The issue this PR joins to (spec Sec5.3 rule 2): the lowest number in
    `closes[]`, falling back to the issue-branch pattern (`feat/<n>-`,
    `<n>-`, `<prefix>/<n>-`) on the PR's own
    `head_ref` (`outcomes.closes_from_branch`, the same pattern T3 uses as its
    own REST-fallback). `None` when neither resolves."""
    closes = pr.get("closes") or []
    if closes:
        return min(closes)
    branch_closes = closes_from_branch(pr.get("head_ref"))
    return branch_closes[0] if branch_closes else None


def prs_by_issue(prs):
    """`{(repo, number): [pr, ...]}`, inverted from `pr_issue_number` -- every
    PR that joins to a given issue (spec Sec5.3 rule 2, used by `is_shipped`)."""
    out = defaultdict(list)
    for pr in prs:
        num = pr_issue_number(pr)
        if num is not None:
            out[(pr["repo"], num)].append(pr)
    return dict(out)


# --- per-record attribution --------------------------------------------------

def attribute_record(record, pr_index, issue_index):
    """One usage record's attribution (spec Sec5.3): `{"pr", "issue",
    "requester", "requester_source", "attributed"}`. `pr`/`issue` are
    `(repo, number)` keys or `None`."""
    pr = None
    repo, branch = record.get("repo"), record.get("branch")
    if repo and branch not in _NEVER_JOIN_BRANCHES:
        pr = select_pr_for_branch(pr_index.get((repo, branch), []))

    issue_key = None
    if pr is not None:
        num = pr_issue_number(pr)
        if num is not None:
            issue_key = (pr["repo"], num)
    if issue_key is None and record.get("kind") != "interactive":
        # rule 3: factory record -> issue straight from its own `issue` field
        if repo and record.get("issue") is not None:
            issue_key = (repo, record["issue"])

    issue = issue_index.get(issue_key) if issue_key else None
    return {
        "pr": (pr["repo"], pr["number"]) if pr is not None else None,
        "issue": issue_key if issue is not None else None,
        "requester": issue.get("requested_by") if issue else None,
        "requester_source": issue.get("requester_source") if issue else None,
        "attributed": issue is not None,
    }


def attribute_records(records, prs, issues):
    """Attribution for every usage record, in the same order (spec Sec5.3)."""
    pr_index = index_prs_by_branch(prs)
    issue_index = index_issues(issues)
    return [attribute_record(r, pr_index, issue_index) for r in records]


def daily_attribution_totals(records, attributions):
    """`{day: {"attributed_usd", "unattributed_usd", "total_usd"}}` -- the
    Sec5.3 conservation invariant (attributed + unattributed == total for
    every day), one row per day seen in `records`."""
    totals = defaultdict(lambda: {"attributed_usd": 0.0, "unattributed_usd": 0.0})
    for record, attr in zip(records, attributions):
        day = record.get("day")
        cost = record.get("cost_usd", 0) or 0
        bucket = "attributed_usd" if attr["attributed"] else "unattributed_usd"
        totals[day][bucket] += cost
    out = {}
    for day, t in totals.items():
        out[day] = {
            "attributed_usd": round(t["attributed_usd"], 6),
            "unattributed_usd": round(t["unattributed_usd"], 6),
            "total_usd": round(t["attributed_usd"] + t["unattributed_usd"], 6),
        }
    return out


# --- Sec3C: "shipped" ---------------------------------------------------

def is_shipped(issue, joined_prs):
    """spec Sec3C: the issue is closed `completed` with a merged PR linked
    (any PR in `joined_prs`, e.g. from `prs_by_issue`, that actually merged)."""
    if not issue or issue.get("state") != "closed" or issue.get("state_reason") != "completed":
        return False
    return any(pr.get("state") == "merged" for pr in joined_prs)


# --- Sec3B: durability (revert / reopen) ------------------------------------

def reverted_by_map(prs):
    """`{(repo, reverted_pr_number): reverting_pr_number}`, inverted from
    `reverts` (T3's `outcomes.build_pr_record`, R7). Only a MERGED revert PR
    counts (spec Sec3B: "no revert PR" refers to a landed revert)."""
    out = {}
    for pr in prs:
        if pr.get("state") != "merged":
            continue
        target = pr.get("reverts")
        if target is not None:
            out[(pr["repo"], target)] = pr["number"]
    return out


def _reopened_within_window(issue, merged_at, window_days=_DURABILITY_WINDOW_DAYS):
    merge_dt = _parse_ts(merged_at)
    if merge_dt is None or not issue:
        return False
    deadline = merge_dt + timedelta(days=window_days)
    for ts in issue.get("reopened_at") or []:
        dt = _parse_ts(ts)
        if dt is not None and merge_dt <= dt <= deadline:
            return True
    return False


def is_durable_merge(pr, issue, reverted_map, window_days=_DURABILITY_WINDOW_DAYS):
    """spec Sec3B: `False` if a merged PR reverts `pr`, or `issue` was
    reopened within `window_days` of the merge; `True` otherwise; `None` when
    `pr` never merged (durability isn't defined for it yet)."""
    if pr.get("state") != "merged":
        return None
    if (pr["repo"], pr["number"]) in reverted_map:
        return False
    if _reopened_within_window(issue, pr.get("merged_at"), window_days):
        return False
    return True


# --- dead-end seams for T5 (spec Sec3B "dead-end $" inputs; not built here) --

def not_planned_issues(issues):
    """Issues closed as `not_planned` (spec Sec3B dead-end input; T5 sums
    their attributed $ into "dead-end $")."""
    return [i for i in issues if i.get("state") == "closed" and i.get("state_reason") == "not_planned"]


def _shift_day(day, delta_days):
    return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=delta_days)).strftime("%Y-%m-%d")


def stale_branches(records, prs, as_of, window_days=_DURABILITY_WINDOW_DAYS):
    """`[(repo, branch), ...]` for local sessions with no merged PR, whose most
    recent session was at least `window_days` before `as_of` (spec Sec3B
    dead-end input for T5). `as_of` and every record's `day` are "YYYY-MM-DD"
    strings, ordered lexicographically like the rest of the codebase (e.g.
    `efficiency.collect_usage_records`'s `today` cutoff)."""
    merged_branches = {(pr["repo"], pr["head_ref"]) for pr in prs if pr.get("state") == "merged"}
    last_session = {}
    for r in records:
        if r.get("kind") != "interactive":
            continue
        repo, branch, day = r.get("repo"), r.get("branch"), r.get("day")
        if not repo or branch in _NEVER_JOIN_BRANCHES or not day:
            continue
        key = (repo, branch)
        if key not in last_session or day > last_session[key]:
            last_session[key] = day
    cutoff = _shift_day(as_of, window_days)
    return sorted(key for key, last_day in last_session.items()
                  if key not in merged_branches and last_day <= cutoff)


# --- top-level pure join API (T4 output contract) --------------------------

def build_join(records, issues, prs, as_of=None):
    """The full Sec5.3 join, pure and importable by T5 / the Synkhos factory
    snapshot job: per-record attribution plus per-issue and per-PR rollups.

    `as_of` is a "YYYY-MM-DD" day string. Durability and shipped-ness only
    depend on the recorded merge/reopen timestamps, never on "now" -- `as_of`
    is threaded through for T5's time-relative rollups (e.g. `stale_branches`)
    that do need it, and is otherwise unused here.

    Returns `{"attributions": [...], "issue_rollups": {(repo, number): {...}},
    "pr_rollups": {(repo, number): {...}}}`.
    """
    pr_index = index_prs_by_branch(prs)
    issue_index = index_issues(issues)
    attributions = [attribute_record(r, pr_index, issue_index) for r in records]

    by_issue_prs = prs_by_issue(prs)
    issue_rollups = {}
    for key, issue in issue_index.items():
        issue_rollups[key] = {
            "attributed_usd": 0.0,
            "shipped": is_shipped(issue, by_issue_prs.get(key, [])),
            "requester": issue.get("requested_by"),
            "requester_source": issue.get("requester_source"),
        }
    for record, attr in zip(records, attributions):
        key = attr["issue"]
        if key is None:
            continue
        issue_rollups.setdefault(key, {
            "attributed_usd": 0.0, "shipped": False,
            "requester": attr["requester"], "requester_source": attr["requester_source"],
        })
        issue_rollups[key]["attributed_usd"] += record.get("cost_usd", 0) or 0
    for row in issue_rollups.values():
        row["attributed_usd"] = round(row["attributed_usd"], 6)

    reverted_map = reverted_by_map(prs)
    pr_rollups = {}
    for pr in prs:
        key = (pr["repo"], pr["number"])
        num = pr_issue_number(pr)
        issue_key = (pr["repo"], num) if num is not None else None
        linked_issue = issue_index.get(issue_key)
        pr_rollups[key] = {
            "issue": issue_key,
            "reverted_by": reverted_map.get(key),
            "durable": is_durable_merge(pr, linked_issue, reverted_map),
        }

    return {
        "attributions": attributions,
        "issue_rollups": issue_rollups,
        "pr_rollups": pr_rollups,
    }
