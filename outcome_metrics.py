"""Outcomes section metric math (spec Sec3C, Sec6.3, Sec6.5, Sec8).

Pure functions only -- no I/O, no GitHub, no filesystem. `serve.py` reads the
outcome cache (spec Sec4.2: `outcomes/state.json` + `issues.jsonl`/`prs.jsonl`
via `outcomes.cache_paths`/`read_jsonl_latest`/`read_state`) and hands the
resulting lists to `build_outcomes_payload` here, alongside the SAME usage
records `efficiency.collect_usage_records` already collected for the
`efficiency` payload key (no second collection pass -- serve.py's latency
budget, spec Sec10 "Performance").

This module is deliberately import-only-what-you-need from `attribution.py`
(T4) rather than re-implementing the joins: `attribute_records`/`build_join`
for the PR->issue->requester chain, `not_planned_issues`/`stale_branches` for
the dead-end inputs. The Synkhos factory snapshot job (synkhos/factory#172)
is meant to import this module directly so its numbers agree with this
dashboard's.

Amendment 2026-09-27 (controller ruling R9): team data lives in the Synkhos
console, so THIS local dashboard shows the local user's "Me" view
(`build_outcomes_payload(..., scope=<login>)`; `scope=None` is the unscoped
team view, final review I6) -- their own
hands-on spend, plus factory work they commissioned (a factory record whose
resolved requester, per `attribution.attribute_record`, is them). A factory
record commissioned by someone else, or one whose requester can't be resolved
(unjoined), is out of scope for a personal dashboard: it is dropped rather
than folded into "unattributed" here (`scope_to_me`). Every $-shaped metric
below is computed over the SCOPED population; `shipped`/`durable` verdicts
(properties of the issue/PR themselves, not of who paid) still come from the
full T4 join across every known issue and PR.

Controller ruling R10 (fix round 1): scoping dollars isn't enough -- WHICH
issues are in scope for "Me" is a separate question (`me_issue_population`).
Without it, a teammate's issue that happens to be `shipped`/`rated`/
`not_planned` in the same outcome cache would still surface in every
issue-level list (with `$0` from me diluting $/pt, polluting the Spearman
population, and showing up in the dead-end list) even though `scope_to_me`
correctly zeroed its dollars. `build_outcomes_payload` restricts
`issue_rollups` (and everything derived from it: shipped rows, $/pt,
per-repo table, points shipped, validity, autonomy, model fit, dead-end
list) to `me_issue_population`'s key set before computing anything.

Ruling R10a (same fix round): `durable_merge_rate` needed the same treatment
on the PR side -- `me_pr_population` scopes it to PRs "Me" authored, UNION
PRs linked to a "Me" issue, so a teammate's reverted PR that never touches
any of "Me"'s issues can't lower a personal durable-merge rate.

Ruling R5: `pts:` labels don't exist yet on live issues (the factory rater,
synkhos/factory#166, isn't built) -- $/pt coverage on real data will be near
0%, honestly reported via `coverage()`, never fabricated or hidden as an
error (spec Sec8 "Missing, unrated or rater-error pts -> excluded from $/pt,
included in coverage").

Rework share (ci-fix/conflict $ over impl $) has NO DATA until the factory
turn kind is known (synkhos/factory#109) -- `build_outcomes_payload`'s
`tiles.rework_share` is always `{"share": None, ...}` with an explicit note,
never a made-up number. Likewise "dead-end $" only ever counts issues closed
`not_planned` and stale local branches (T4's own seams); factory
timeouts/escalations have no data source yet and are named as excluded.
"""
import math
from collections import defaultdict
from datetime import date, datetime

from attribution import (
    attribute_records,
    build_join,
    not_planned_issues,
    pr_issue_number,
    stale_branches,
)

_MODEL_FIT_MODEL_CLASSES = ("opus", "fable")
_MODEL_FIT_PTS_CEILING = 2
_VALIDITY_WINDOW_DAYS = 90
_VALIDITY_MIN_RHO = 0.3
_VALIDITY_MIN_N = 15
_DEAD_END_TOP_N = 10
_REWORK_SHARE_NOTE = ("no data yet -- factory turn kind (impl/ci-fix/conflict) is unknown "
                      "until synkhos/factory#109 lands")
_DEAD_END_EXCLUDED_NOTE = ("factory turn timeouts/escalations have no data yet "
                           "(synkhos/factory#109); only issues closed not_planned and "
                           "stale local branches (no merged PR 14 days after the last "
                           "session) are counted")


# --- distributions: median / p90 (spec principle: distributions, never means) ----------

def percentile(values, p):
    """Linear-interpolation percentile (the standard/numpy-default method); `None`
    for an empty list."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n == 1:
        return s[0]
    idx = p * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def median(values):
    return percentile(values, 0.5)


def p90(values):
    return percentile(values, 0.9)


def coverage(counted, total):
    """The Sec3C/Sec8 coverage line: how many of the eligible population actually
    carried a rating/verdict. `pct` is `None` (not 0) when there's no population
    at all to rate -- an honest "nothing to cover" distinct from "0% covered"."""
    return {"counted": counted, "total": total,
            "pct": round(100 * counted / total, 1) if total else None}


# --- Spearman rho with ties (average-rank method) ---------------------------------------

def _ranks(values):
    """1-based ranks, tied values sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def spearman_rho(xs, ys):
    """`(rho, n)` -- Spearman's rank correlation (Pearson correlation of the
    average ranks, so ties are handled without a separate tie-correction
    formula). `rho` is `None` when there are fewer than 2 points or either
    side has zero variance (e.g. every `pts` value is the same) -- undefined,
    not zero."""
    n = len(xs)
    if n != len(ys) or n < 2:
        return None, n
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = sum((a - mx) ** 2 for a in rx)
    deny = sum((b - my) ** 2 for b in ry)
    if denx == 0 or deny == 0:
        return None, n
    return num / math.sqrt(denx * deny), n


def validity_badge(rho, n):
    """spec Sec3C: warn when `rho < 0.3` or `n < 15`; an undefined `rho` (no
    variance, or too few points) always warns."""
    return rho is None or rho < _VALIDITY_MIN_RHO or n < _VALIDITY_MIN_N


# --- ISO week (Throughput / autonomy trend / "this week" tiles) ------------------------

def iso_week(day):
    """`"YYYY-Www"` for a `"YYYY-MM-DD"`-or-longer ISO date/timestamp string."""
    y, w, _ = date.fromisoformat(day[:10]).isocalendar()
    return f"{y}-W{w:02d}"


# --- Amendment A-3: scope usage records + their attributions to "Me" -------------------

def actor_login(actor):
    """`"human:jason"` -> `"jason"`; `None` through untouched. Usage records and
    the `--actor` config carry the `human:<login>` prefix (spec Sec4.1); an
    issue's `requested_by` (spec Sec4.2) is the bare login, so this bridges
    the two for the `scope_to_me` comparison."""
    if not actor:
        return None
    return actor.split(":", 1)[1] if ":" in actor else actor


def me_issue_population(issues, scoped_attrs, me_login):
    """Controller ruling R10 (fix round 1): the local user's ISSUE population --
    not just their dollars. An issue belongs to "Me" when its resolved
    requester (`requested_by`, falling back to the issue `author` as a
    defensive belt-and-braces check -- `outcomes.requested_by_and_source`
    already applies this same fallback at fetch time, so `requested_by`
    should never actually be `None` here) is `me_login`, UNION any issue the
    user's own `scope_to_me`-filtered records attribute ANY $ to (e.g.
    hands-on review/fixup work on a teammate's branch, with no `requested_by`
    match). Every issue-level metric and list in `build_outcomes_payload`
    (shipped rows, $/pt + its coverage, the per-repo table, points
    shipped/throughput, validity rho, autonomy, model fit, the dead-end list)
    is restricted to this population -- a teammate's shipped/rated/not_planned
    issue must never surface just because it happens to exist in the same
    outcome cache.

    Pure and exported. Only the "Me" view calls it: the unscoped team view
    is `build_outcomes_payload(..., scope=None)`, which skips this filter
    entirely (final review I6) -- `me_login=None` here means "no requester
    match", NOT "everyone"."""
    keys = set()
    if me_login:
        for issue in issues:
            requester = issue.get("requested_by") or issue.get("author")
            if requester == me_login:
                keys.add((issue["repo"], issue["number"]))
    for attr in scoped_attrs:
        if attr.get("issue"):
            keys.add(attr["issue"])
    return keys


def scope_to_me(records, attributions, me_login):
    """Amendment A-3 / controller ruling R9: keep a hands-on (`interactive`)
    record only when it is `me_login`'s own (`actor == "human:<me_login>"`,
    final review I5 -- an interactive record is NOT mine by construction once
    records from other machines are in the same list), plus a factory record
    only when its resolved requester (T4's `attribute_record`) is `me_login`.
    Everything else (a teammate's hands-on work, someone else's commissioned
    factory work, or factory work with no resolved requester) is dropped
    rather than folded into "unattributed" -- a personal dashboard has no
    business showing it at all. With no `me_login` nothing is kept.
    Returns `(records, attributions)`, aligned and filtered together."""
    scoped_records, scoped_attrs = [], []
    if not me_login:
        return scoped_records, scoped_attrs
    me_actor = f"human:{me_login}"
    for record, attr in zip(records, attributions):
        if record.get("kind") == "interactive":
            if record.get("actor") == me_actor:
                scoped_records.append(record)
                scoped_attrs.append(attr)
            continue
        if attr.get("requester") == me_login:
            scoped_records.append(record)
            scoped_attrs.append(attr)
    return scoped_records, scoped_attrs


# --- shipped issues: pts/closed_at onto the T4 issue_rollups ---------------------------

def shipped_issue_rows(issue_rollups, issues):
    """One row per SHIPPED issue (spec Sec3C: closed `completed` with a merged
    PR linked, per T4's `is_shipped`): `{"repo","number","pts","closed_at",
    "attributed_usd"}`. `pts`/`closed_at` come from the raw issue record;
    `attributed_usd`/`shipped` from the (possibly Me-rescoped) rollup."""
    issues_by_key = {(i["repo"], i["number"]): i for i in issues}
    rows = []
    for key, rollup in issue_rollups.items():
        if not rollup.get("shipped"):
            continue
        issue = issues_by_key.get(key, {})
        rows.append({
            "repo": key[0], "number": key[1],
            "pts": issue.get("pts"),
            "closed_at": issue.get("closed_at"),
            "attributed_usd": rollup.get("attributed_usd", 0.0),
        })
    return rows


# --- $/pt: unrated/missing pts excluded from the ratio, counted in coverage ------------

def cost_per_point_stats(rows):
    """spec Sec3C: attributed $ / pts per issue, median and p90 of that ratio
    across `rows` (a distribution over issues, never a sum-of-$/sum-of-pts).
    Issues with `pts` unrated/missing are excluded from the ratio but counted
    in `coverage` (spec Sec8)."""
    rated = [r for r in rows if r.get("pts") is not None]
    ratios = [r["attributed_usd"] / r["pts"] for r in rated if r["pts"]]
    return {
        "median": median(ratios),
        "p90": p90(ratios),
        "coverage": coverage(len(rated), len(rows)),
    }


def _closed_within_window(closed_at, as_of, window_days):
    if not closed_at or not as_of:
        return False
    try:
        closed = datetime.fromisoformat(closed_at.replace("Z", "+00:00")).date()
    except Exception:
        return False
    as_of_date = date.fromisoformat(as_of[:10])
    return closed <= as_of_date and (as_of_date - closed).days <= window_days


def points_validity(rows, as_of=None, window_days=_VALIDITY_WINDOW_DAYS):
    """spec Sec3C: Spearman rho between `pts` and attributed $ over the last
    `window_days` of shipped issues (default 90). Unrated rows are excluded
    from the correlation the same way they're excluded from $/pt."""
    pool = rows
    if as_of:
        pool = [r for r in rows if _closed_within_window(r.get("closed_at"), as_of, window_days)]
    rated = [r for r in pool if r.get("pts") is not None]
    rho, n = spearman_rho([r["pts"] for r in rated], [r["attributed_usd"] for r in rated])
    return {"rho": rho, "n": n, "badge": validity_badge(rho, n)}


def per_repo_table(rows, as_of=None, window_days=_VALIDITY_WINDOW_DAYS):
    """spec Sec6.3 per-repo table: points shipped, $/pt (median/p90 + coverage),
    validity rho + its warning badge -- grouped by repo, repos alphabetical."""
    by_repo = defaultdict(list)
    for row in rows:
        by_repo[row["repo"]].append(row)
    table = []
    for repo in sorted(by_repo):
        repo_rows = by_repo[repo]
        table.append({
            "repo": repo,
            "points_shipped": sum(r["pts"] for r in repo_rows if r.get("pts") is not None),
            "cost_per_point": cost_per_point_stats(repo_rows),
            "validity": points_validity(repo_rows, as_of=as_of, window_days=window_days),
        })
    return table


# --- durable-merge rate: spec Sec3B ------------------------------------------------------

def me_pr_population(prs, me_keys, me_login):
    """Controller ruling R10a (fix round 1 addendum): the local user's PR
    population for `durable_merge_rate` -- PRs `me_login` authored, UNION PRs
    linked (T4's `pr_issue_number`: `closes[]`, falling back to the
    `feat/<n>-` branch pattern) to an issue in `me_keys` (the `me_issue_
    population` result). Without this, a teammate's reverted PR that never
    touches any of "Me"'s issues would still drag down a personal
    durable-merge rate just because it exists in the same outcome cache.

    Pure/exported and parameterized like `me_issue_population`: the caller
    decides scope. `build_outcomes_payload` filters `pr_rollups` down to this
    key set before computing `durable_merge_rate`; the team/factory variant
    simply calls `durable_merge_rate` on the FULL unfiltered `pr_rollups`
    (this function is never invoked, not passed a universal population) --
    scoping is which `pr_rollups` dict you hand to `durable_merge_rate`, not
    a flag inside it."""
    keys = set()
    for pr in prs:
        key = (pr["repo"], pr["number"])
        if me_login and pr.get("author") == me_login:
            keys.add(key)
            continue
        num = pr_issue_number(pr)
        if num is not None and (pr["repo"], num) in me_keys:
            keys.add(key)
    return keys


def durable_merge_rate(pr_rollups):
    """merged PRs with no revert and no reopen within 14 days, over merged PRs
    (T4's `is_durable_merge`; `None` durability means the PR never merged and
    is excluded, not counted as a failure). Coverage is the PRs actually known
    to the outcome cache -- "PRs in configured orgs" per spec Sec3B -- or, when
    the caller passes an already `me_pr_population`-filtered `pr_rollups`
    (ruling R10a), the PRs known to be "Me"'s."""
    verdicts = [row["durable"] for row in pr_rollups.values() if row.get("durable") is not None]
    if not verdicts:
        return {"rate": None, "coverage": coverage(0, 0)}
    durable = sum(1 for v in verdicts if v)
    return {"rate": durable / len(verdicts), "coverage": coverage(len(verdicts), len(verdicts))}


# --- dead-end $: not_planned issues + stale local branches, linked, top 10 -------------

def issue_url(repo, number):
    return f"https://github.com/{repo}/issues/{number}"


def branch_url(repo, branch):
    return f"https://github.com/{repo}/tree/{branch}"


def dead_end_payload(issue_rollups, not_planned, records, stale_branch_keys, attributions=None):
    """spec Sec3B "dead-end $": issues closed `not_planned` (their rescoped
    attributed $) plus local branches with no merged PR 14 days after their
    last session (the hands-on $ actually spent on that branch). Factory
    timeouts/escalations have no data source yet (spec Sec8) and are named,
    not silently omitted, in `excluded_note`.

    Final review I4: a stale branch whose (closed, unmerged) PR links to a
    `not_planned` issue has its records attributed to that issue, so they're
    already in the issue's $ -- branch $ counts only records NOT attributed
    to a `not_planned` issue (`attributions`, aligned with `records`), so no
    dollar is counted twice."""
    items = []
    total = 0.0
    not_planned_keys = {(i["repo"], i["number"]) for i in not_planned}
    if attributions is None:
        attributions = [{}] * len(records)
    for issue in not_planned:
        key = (issue["repo"], issue["number"])
        usd = issue_rollups.get(key, {}).get("attributed_usd", 0.0)
        total += usd
        items.append({"type": "issue", "repo": issue["repo"], "number": issue["number"],
                      "usd": round(usd, 6), "url": issue_url(issue["repo"], issue["number"])})

    stale_set = set(stale_branch_keys)
    per_branch = defaultdict(float)
    for r, attr in zip(records, attributions):
        if r.get("kind") != "interactive":
            continue
        if (attr or {}).get("issue") in not_planned_keys:
            continue                              # already counted as that issue's $
        key = (r.get("repo"), r.get("branch"))
        if key in stale_set:
            per_branch[key] += r.get("cost_usd", 0) or 0
    for (repo, branch), usd in per_branch.items():
        total += usd
        items.append({"type": "branch", "repo": repo, "branch": branch,
                      "usd": round(usd, 6), "url": branch_url(repo, branch)})

    items.sort(key=lambda x: x["usd"], reverse=True)
    return {
        "usd": round(total, 6),
        "top": items[:_DEAD_END_TOP_N],
        "excluded_note": _DEAD_END_EXCLUDED_NOTE,
    }


# --- points shipped this week (Throughput tile) -----------------------------------------

def points_shipped_this_week(rows, as_of):
    """spec Sec6.3 tile: rated points among issues shipped in `as_of`'s ISO
    week; unrated issues shipped that week count in coverage, not in the
    points total."""
    week = iso_week(as_of)
    in_week = [r for r in rows if r.get("closed_at") and iso_week(r["closed_at"]) == week]
    rated = [r for r in in_week if r.get("pts") is not None]
    return {
        "points": sum(r["pts"] for r in rated),
        "coverage": coverage(len(rated), len(in_week)),
    }


# --- autonomy trend: factory share of points shipped, human $/factory-shipped point -----

def autonomy_trend(records, attributions, rows):
    """spec Sec3C "Autonomy": per ISO week, the factory's share of rated points
    shipped, and human hands-on $ spent on issues the factory touched (per
    factory-shipped point) -- team-mode's per-requester breakdown (spec Sec6.3)
    is out of scope for this single-user local dashboard (Amendment A-3)."""
    rows_by_key = {(r["repo"], r["number"]): r for r in rows}
    factory_touched = {a["issue"] for r, a in zip(records, attributions)
                       if a.get("issue") and r.get("kind") != "interactive"}

    weeks = defaultdict(lambda: {"factory_pts": 0, "total_pts": 0, "human_usd_on_factory_issues": 0.0})
    for key, row in rows_by_key.items():
        if row.get("pts") is None or not row.get("closed_at"):
            continue
        week = iso_week(row["closed_at"])
        w = weeks[week]
        w["total_pts"] += row["pts"]
        if key in factory_touched:
            w["factory_pts"] += row["pts"]

    for record, attr in zip(records, attributions):
        key = attr.get("issue")
        if key in factory_touched and record.get("kind") == "interactive" and key in rows_by_key:
            row = rows_by_key[key]
            if row.get("closed_at"):
                week = iso_week(row["closed_at"])
                weeks[week]["human_usd_on_factory_issues"] += record.get("cost_usd", 0) or 0

    out = []
    for week in sorted(weeks):
        w = weeks[week]
        out.append({
            "week": week,
            "factory_pts": w["factory_pts"], "total_pts": w["total_pts"],
            "factory_share": (w["factory_pts"] / w["total_pts"]) if w["total_pts"] else 0.0,
            "human_usd_per_factory_pt": (round(w["human_usd_on_factory_issues"] / w["factory_pts"], 6)
                                        if w["factory_pts"] else None),
        })
    return out


# --- model fit: Opus/Fable $ joined to an issue rated <=2 pts (spec Sec3A, moved to T5) --

def model_fit_candidates(records, attributions, issues):
    """spec Sec3A "Model fit": Opus/Fable spend on sessions joined to an issue
    rated `<= 2` pts; unjoined sessions are excluded. A table of candidates,
    not a score -- the dashboard/caller decides what (if anything) to do with
    an expensive model on a small card."""
    issues_by_key = {(i["repo"], i["number"]): i for i in issues}
    out = []
    for record, attr in zip(records, attributions):
        if record.get("model_class") not in _MODEL_FIT_MODEL_CLASSES:
            continue
        key = attr.get("issue")
        if not key:
            continue
        issue = issues_by_key.get(key)
        if not issue or issue.get("pts") is None or issue["pts"] > _MODEL_FIT_PTS_CEILING:
            continue
        out.append({
            "repo": key[0], "issue": key[1], "pts": issue["pts"],
            "model_class": record["model_class"],
            "cost_usd": round(record.get("cost_usd", 0) or 0, 6),
            "session": record.get("session"),
        })
    return out


# --- unattributed slice: always shown with its $ share (spec Sec6.5) -------------------

def unattributed_summary(records, attributions):
    total = sum(r.get("cost_usd", 0) or 0 for r in records)
    unattributed = sum(r.get("cost_usd", 0) or 0 for r, a in zip(records, attributions)
                       if not a.get("attributed"))
    return {
        "unattributed_usd": round(unattributed, 6),
        "total_usd": round(total, 6),
        "pct": round(100 * unattributed / total, 1) if total else None,
    }


# --- top-level assembly: the `outcomes` payload key (T5 output contract) --------------

def _rescope_issue_dollars(issue_rollups, scoped_records, scoped_attrs):
    """T4's `issue_rollups` sum `attributed_usd` over EVERY usage record; the
    "Me" view needs it summed over the SCOPED population only. `shipped` (and
    any other non-dollar fact) is carried through unchanged -- it's a property
    of the issue/PR, not of who paid."""
    out = {key: dict(row, attributed_usd=0.0) for key, row in issue_rollups.items()}
    for record, attr in zip(scoped_records, scoped_attrs):
        key = attr.get("issue")
        if key is None:
            continue
        if key not in out:
            out[key] = {"attributed_usd": 0.0, "shipped": False,
                       "requester": attr.get("requester"), "requester_source": attr.get("requester_source")}
        out[key]["attributed_usd"] += record.get("cost_usd", 0) or 0
    for row in out.values():
        row["attributed_usd"] = round(row["attributed_usd"], 6)
    return out


def build_outcomes_payload(records, issues, prs, as_of, outcomes_as_of, scope=None):
    """The `outcomes` top-level payload key (controller ruling R3). `records`
    is the SAME usage-record list `efficiency.collect_usage_records` already
    built for the `efficiency` key (spec Sec10 performance budget: no second
    collection pass). `issues`/`prs` are the outcome cache's structured
    records (spec Sec4.2); `as_of` is a `"YYYY-MM-DD"` day string.

    `scope` (final review I6) picks the population explicitly:
    - `scope="<login>"` (bare GitHub login, `actor_login`) -- the Amendment
      A-3 "Me" view: records via `scope_to_me`, issues via
      `me_issue_population`, PRs via `me_pr_population`. The local dashboard.
    - `scope=None` -- the unscoped TEAM view: every record, issue and PR,
      e.g. for the Synkhos factory snapshot job (synkhos/factory#172). It
      yields team totals only; per-actor / per-requester team breakdowns are
      the console's job (it groups the same records by `actor` /
      `on_behalf_of`), not this function's.

    Availability ("outcomes unavailable, with a reason" — spec Sec8) is a
    server-side concern (serve.py checks whether the cache exists at all)
    and is layered on top of this function's return value, not inside it.
    """
    full_attrs = attribute_records(records, prs, issues)
    joined = build_join(records, issues, prs, as_of=as_of)
    if scope is None:
        return _assemble_outcomes(records, full_attrs, joined["issue_rollups"], issues,
                                  joined["pr_rollups"], prs, as_of, outcomes_as_of)

    me_login = scope
    scoped_records, scoped_attrs = scope_to_me(records, full_attrs, me_login)

    # shipped/durable are facts about the issue/PR itself (spec Sec3C/Sec3B),
    # so they come from T4's join over the FULL population; dollar amounts are
    # rebuilt over the scoped population only (Amendment A-3). Ruling R10:
    # WHICH issues are in scope is a separate question from their dollars --
    # `me_keys` below restricts every issue-level metric/list to "Me"'s own
    # population, not just the $ that flow through it.
    issue_rollups = _rescope_issue_dollars(joined["issue_rollups"], scoped_records, scoped_attrs)
    pr_rollups = joined["pr_rollups"]

    me_keys = me_issue_population(issues, scoped_attrs, me_login)
    issue_rollups = {key: row for key, row in issue_rollups.items() if key in me_keys}
    scoped_issues = [i for i in issues if (i["repo"], i["number"]) in me_keys]

    # ruling R10a: the durable-merge-rate tile also scopes to "Me"'s own PRs --
    # authored by me, or linked to a "Me" issue -- so a teammate's reverted PR
    # never drags down a personal rate just because it's in the same cache.
    me_pr_keys = me_pr_population(prs, me_keys, me_login)
    pr_rollups = {key: row for key, row in pr_rollups.items() if key in me_pr_keys}

    return _assemble_outcomes(scoped_records, scoped_attrs, issue_rollups, scoped_issues,
                              pr_rollups, prs, as_of, outcomes_as_of)


def _assemble_outcomes(records, attrs, issue_rollups, issues, pr_rollups, prs, as_of,
                       outcomes_as_of):
    """Every tile/table/list from an already-scoped population (the "Me" view's
    or the unscoped team view's -- `build_outcomes_payload` decides)."""
    rows = shipped_issue_rows(issue_rollups, issues)
    stale = stale_branches(records, prs, as_of)
    dead_end = dead_end_payload(issue_rollups, not_planned_issues(issues), records, stale,
                                attributions=attrs)

    tiles = {
        "points_shipped_this_week": points_shipped_this_week(rows, as_of),
        "cost_per_point": cost_per_point_stats(rows),
        "durable_merge_rate": durable_merge_rate(pr_rollups),
        "rework_share": {"share": None, "coverage": coverage(0, 0), "note": _REWORK_SHARE_NOTE},
        "dead_end_usd": {"usd": dead_end["usd"], "note": dead_end["excluded_note"]},
    }

    return {
        "outcomes_as_of": outcomes_as_of,
        "tiles": tiles,
        "per_repo": per_repo_table(rows, as_of=as_of),
        "autonomy_trend": autonomy_trend(records, attrs, rows),
        "dead_end_list": dead_end["top"],
        "model_fit": model_fit_candidates(records, attrs, issues),
        "unattributed": unattributed_summary(records, attrs),
    }
