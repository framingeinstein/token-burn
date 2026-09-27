# tests/test_attribution.py
"""Joining spend to PRs, issues and requesters (spec Sec2, Sec3B, Sec3C, Sec5.3)."""
import pytest

from attribution import (
    attribute_records,
    build_join,
    daily_attribution_totals,
    index_prs_by_branch,
    is_durable_merge,
    is_shipped,
    not_planned_issues,
    pr_issue_number,
    prs_by_issue,
    reverted_by_map,
    select_pr_for_branch,
    stale_branches,
)


def usage_record(**over):
    r = {
        "v": 1, "day": "2026-09-01", "actor": "human:jason", "on_behalf_of": None,
        "requester_source": None, "repo": "o/r", "branch": "feat/10-x", "issue": None,
        "session": "s1", "model": "claude-sonnet-4", "model_class": "sonnet",
        "kind": "interactive", "calls": 1, "in": 10, "out": 10, "cc": 0, "cr": 0,
        "cost_usd": 1.0, "ctx_buckets": {},
    }
    r.update(over)
    return r


def pr(**over):
    r = {
        "repo": "o/r", "number": 20, "author": "jason", "head_ref": "feat/10-x",
        "state": "merged", "merged_at": "2026-09-02T00:00:00Z", "closes": [10],
        "reverted_by": None, "reverts": None, "review_rounds": 1,
    }
    r.update(over)
    return r


def issue(**over):
    r = {
        "repo": "o/r", "number": 10, "author": "jason", "requested_by": "ricky",
        "requester_source": "requested_by", "state": "closed", "state_reason": "completed",
        "closed_at": "2026-09-02T00:00:00Z", "reopened_at": [], "pts": 3,
        "pts_source": None, "labels": ["pts:3"], "approved_by": None,
    }
    r.update(over)
    return r


# --- rule 1: local session -> PR by (repo, branch) -------------------------

def test_index_prs_by_branch_keys_by_repo_and_head_ref():
    p = pr()
    idx = index_prs_by_branch([p])
    assert idx[("o/r", "feat/10-x")] == [p]


@pytest.mark.parametrize("branch", ["main", "master", "HEAD", None])
def test_never_join_branches_are_excluded_from_the_pr_index(branch):
    idx = index_prs_by_branch([pr(head_ref=branch)])
    assert idx == {}


def test_select_pr_for_branch_prefers_merged_over_open():
    open_pr = pr(number=19, state="open")
    merged_pr = pr(number=20, state="merged")
    assert select_pr_for_branch([open_pr, merged_pr]) == merged_pr


def test_select_pr_for_branch_picks_most_recent_when_none_merged():
    older = pr(number=5, state="closed")
    newer = pr(number=9, state="closed")
    assert select_pr_for_branch([older, newer]) == newer


def test_select_pr_for_branch_empty_is_none():
    assert select_pr_for_branch([]) is None


# --- rule 2: PR -> issue by closes[], falling back to the branch pattern ----

def test_pr_issue_number_from_closes():
    assert pr_issue_number(pr(closes=[10])) == 10


def test_pr_issue_number_takes_lowest_when_pr_closes_several():
    assert pr_issue_number(pr(closes=[30, 10, 20])) == 10


def test_pr_issue_number_falls_back_to_branch_pattern():
    assert pr_issue_number(pr(closes=[], head_ref="feat/42-thing")) == 42


def test_pr_issue_number_none_when_neither_resolves():
    assert pr_issue_number(pr(closes=[], head_ref="chore/cleanup")) is None


# --- full record attribution --------------------------------------------

def test_local_session_joins_pr_and_issue_and_inherits_requester():
    records = [usage_record()]
    attrs = attribute_records(records, [pr()], [issue()])
    assert attrs == [{
        "pr": ("o/r", 20), "issue": ("o/r", 10),
        "requester": "ricky", "requester_source": "requested_by", "attributed": True,
    }]


def test_requester_fallback_flag_is_carried_through_when_issue_used_the_author():
    records = [usage_record()]
    attrs = attribute_records(records, [pr()], [issue(requested_by="jason", requester_source="author")])
    assert attrs[0]["requester"] == "jason"
    assert attrs[0]["requester_source"] == "author"


@pytest.mark.parametrize("branch", ["main", "master", "HEAD", None])
def test_never_join_branches_never_attribute(branch):
    records = [usage_record(branch=branch)]
    attrs = attribute_records(records, [pr(head_ref=branch)], [issue()])
    assert attrs == [{"pr": None, "issue": None, "requester": None,
                       "requester_source": None, "attributed": False}]


def test_no_repo_never_attributes():
    records = [usage_record(repo=None)]
    attrs = attribute_records(records, [pr()], [issue()])
    assert attrs[0]["attributed"] is False


def test_no_matching_pr_is_unattributed():
    records = [usage_record(branch="feat/999-nope")]
    attrs = attribute_records(records, [pr()], [issue()])
    assert attrs == [{"pr": None, "issue": None, "requester": None,
                       "requester_source": None, "attributed": False}]


def test_pr_joined_but_no_issue_is_unattributed():
    # the PR resolves (rule 1) but rule 2 (closes/branch) resolves nothing
    records = [usage_record(branch="chore/cleanup")]
    attrs = attribute_records(records, [pr(head_ref="chore/cleanup", closes=[])], [])
    assert attrs[0]["pr"] == ("o/r", 20)
    assert attrs[0]["issue"] is None
    assert attrs[0]["attributed"] is False


def test_factory_record_joins_issue_directly_from_its_issue_field():
    records = [usage_record(actor="factory:wb-impl-r", branch=None, kind="unknown", issue=10)]
    attrs = attribute_records(records, [], [issue()])
    assert attrs == [{
        "pr": None, "issue": ("o/r", 10),
        "requester": "ricky", "requester_source": "requested_by", "attributed": True,
    }]


def test_factory_record_with_no_matching_issue_is_unattributed():
    records = [usage_record(actor="factory:wb-impl-r", branch=None, kind="unknown", issue=999)]
    attrs = attribute_records(records, [], [issue()])
    assert attrs[0]["attributed"] is False


def test_local_session_never_falls_back_to_its_own_issue_field_directly():
    # rule 1/2 only, for interactive records -- even if the record carries an
    # `issue` (from a feat/<n>- branch), it must join through an actual PR,
    # not directly, unlike a factory record.
    records = [usage_record(branch="feat/10-x", issue=10)]
    attrs = attribute_records(records, [], [issue()])  # no PR exists yet
    assert attrs[0]["attributed"] is False


# --- Sec3C: "shipped" -----------------------------------------------------

def test_shipped_requires_completed_state_and_a_merged_linked_pr():
    joined = prs_by_issue([pr()])
    assert is_shipped(issue(), joined[("o/r", 10)]) is True


def test_not_shipped_when_state_reason_is_not_completed():
    joined = prs_by_issue([pr()])
    assert is_shipped(issue(state_reason="not_planned"), joined.get(("o/r", 10), [])) is False


def test_not_shipped_when_no_merged_pr_linked():
    joined = prs_by_issue([pr(state="open")])
    assert is_shipped(issue(), joined.get(("o/r", 10), [])) is False


def test_not_shipped_when_no_pr_at_all():
    assert is_shipped(issue(), []) is False


# --- Sec3B: revert / reopen durability --------------------------------------

def test_reverted_by_map_inverts_reverts_among_merged_prs():
    original = pr(number=20)
    revert_pr = pr(number=21, head_ref="revert-20-x", closes=[], reverts=20, state="merged")
    assert reverted_by_map([original, revert_pr]) == {("o/r", 20): 21}


def test_reverted_by_map_ignores_an_unmerged_revert_pr():
    original = pr(number=20)
    revert_pr = pr(number=21, head_ref="revert-20-x", closes=[], reverts=20, state="open")
    assert reverted_by_map([original, revert_pr]) == {}


def test_revert_pr_makes_the_merge_not_durable():
    original = pr(number=20)
    revert_pr = pr(number=21, head_ref="revert-20-x", closes=[], reverts=20, state="merged")
    rmap = reverted_by_map([original, revert_pr])
    assert is_durable_merge(original, issue(), rmap) is False


def test_reopen_within_14_days_of_merge_makes_the_merge_not_durable():
    p = pr(merged_at="2026-09-01T00:00:00Z")
    i = issue(reopened_at=["2026-09-10T00:00:00Z"])  # 9 days later
    assert is_durable_merge(p, i, {}) is False


def test_reopen_after_14_days_of_merge_does_not_affect_durability():
    p = pr(merged_at="2026-09-01T00:00:00Z")
    i = issue(reopened_at=["2026-09-20T00:00:00Z"])  # 19 days later
    assert is_durable_merge(p, i, {}) is True


def test_reopen_exactly_on_the_14_day_boundary_counts_as_within_window():
    p = pr(merged_at="2026-09-01T00:00:00Z")
    i = issue(reopened_at=["2026-09-15T00:00:00Z"])  # exactly 14 days later
    assert is_durable_merge(p, i, {}) is False


def test_no_revert_no_reopen_is_durable():
    assert is_durable_merge(pr(), issue(), {}) is True


def test_unmerged_pr_has_no_durability_verdict():
    assert is_durable_merge(pr(state="open", merged_at=None), issue(), {}) is None


def test_durability_with_no_linked_issue_still_checks_reverts():
    original = pr(number=20)
    revert_pr = pr(number=21, head_ref="revert-20-x", closes=[], reverts=20, state="merged")
    rmap = reverted_by_map([original, revert_pr])
    assert is_durable_merge(original, None, rmap) is False
    assert is_durable_merge(pr(number=30), None, {}) is True


# --- $ conservation: unattributed + attributed == total per day -------------

def test_daily_attribution_totals_conserve_total_dollars():
    records = [
        usage_record(day="2026-09-01", cost_usd=1.0, branch="feat/10-x"),
        usage_record(day="2026-09-01", cost_usd=2.0, branch="chore/nope"),
        usage_record(day="2026-09-02", cost_usd=5.0, branch=None, repo=None),
    ]
    attrs = attribute_records(records, [pr()], [issue()])
    totals = daily_attribution_totals(records, attrs)
    assert totals["2026-09-01"] == {"attributed_usd": 1.0, "unattributed_usd": 2.0, "total_usd": 3.0}
    assert totals["2026-09-02"] == {"attributed_usd": 0.0, "unattributed_usd": 5.0, "total_usd": 5.0}
    for day, t in totals.items():
        day_total = sum(r["cost_usd"] for r in records if r["day"] == day)
        assert t["total_usd"] == pytest.approx(day_total)
        assert t["attributed_usd"] + t["unattributed_usd"] == pytest.approx(t["total_usd"])


def test_daily_attribution_totals_over_many_records_still_conserves(monkeypatch=None):
    records = [usage_record(day="2026-09-03", session=f"s{i}", cost_usd=0.37,
                             branch="feat/10-x" if i % 3 else "chore/other")
               for i in range(10)]
    attrs = attribute_records(records, [pr()], [issue()])
    totals = daily_attribution_totals(records, attrs)
    day_total = sum(r["cost_usd"] for r in records)
    t = totals["2026-09-03"]
    assert t["attributed_usd"] + t["unattributed_usd"] == pytest.approx(t["total_usd"])
    assert t["total_usd"] == pytest.approx(day_total)


# --- dead-end seams for T5 (spec Sec3B "dead-end $" inputs) -----------------

def test_not_planned_issues():
    issues = [issue(number=1, state_reason="completed"),
              issue(number=2, state_reason="not_planned")]
    assert [i["number"] for i in not_planned_issues(issues)] == [2]


def test_stale_branches_flags_a_branch_with_no_merged_pr_14_days_after_last_session():
    records = [usage_record(day="2026-09-01", branch="feat/99-x", repo="o/r")]
    stale = stale_branches(records, prs=[], as_of="2026-09-15")
    assert ("o/r", "feat/99-x") in stale


def test_stale_branches_excludes_a_branch_with_a_merged_pr():
    records = [usage_record(day="2026-09-01", branch="feat/10-x", repo="o/r")]
    stale = stale_branches(records, prs=[pr()], as_of="2026-09-15")
    assert stale == []


def test_stale_branches_excludes_a_branch_within_the_14_day_window():
    records = [usage_record(day="2026-09-01", branch="feat/99-x", repo="o/r")]
    stale = stale_branches(records, prs=[], as_of="2026-09-10")  # only 9 days
    assert stale == []


# --- build_join: top-level pure API ----------------------------------------

def test_build_join_returns_attributions_and_rollups():
    records = [usage_record(cost_usd=4.0)]
    result = build_join(records, issues=[issue()], prs=[pr()], as_of="2026-09-15")
    assert result["attributions"] == attribute_records(records, [pr()], [issue()])
    assert result["issue_rollups"][("o/r", 10)]["attributed_usd"] == 4.0
    assert result["issue_rollups"][("o/r", 10)]["shipped"] is True
    assert result["pr_rollups"][("o/r", 20)]["durable"] is True
    assert result["pr_rollups"][("o/r", 20)]["issue"] == ("o/r", 10)


def test_build_join_issue_rollup_present_even_with_zero_usage():
    # a shipped issue with no attributed usage still reports shipped/durable
    result = build_join([], issues=[issue()], prs=[pr()], as_of="2026-09-15")
    assert result["issue_rollups"][("o/r", 10)]["attributed_usd"] == 0.0
    assert result["issue_rollups"][("o/r", 10)]["shipped"] is True
