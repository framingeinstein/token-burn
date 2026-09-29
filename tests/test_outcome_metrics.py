# tests/test_outcome_metrics.py — Outcomes section metric math (spec §3, §6.3, §6.5;
# Amendment 2026-09-27: the local dashboard shows the "Me" view — hands-on spend plus
# factory work this developer commissioned; everything else is out of scope, per
# controller ruling R9).
import math

import pytest

from outcome_metrics import (
    actor_login,
    autonomy_trend,
    build_outcomes_payload,
    coverage,
    cost_per_point_stats,
    dead_end_payload,
    durable_merge_rate,
    iso_week,
    me_issue_population,
    me_pr_population,
    median,
    model_fit_candidates,
    p90,
    per_repo_table,
    points_shipped_this_week,
    points_validity,
    scope_to_me,
    shipped_issue_rows,
    spearman_rho,
    unattributed_summary,
    validity_badge,
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
        "repo": "o/r", "number": 10, "author": "jason", "requested_by": "jason",
        "requester_source": "requested_by", "state": "closed", "state_reason": "completed",
        "closed_at": "2026-09-02T00:00:00Z", "reopened_at": [], "pts": 3,
        "pts_source": None, "labels": ["pts:3"], "approved_by": None,
    }
    r.update(over)
    return r


# --- median / p90: hand-computed, standard linear-interpolation percentile -----------

def test_median_even_count_averages_middle_two():
    assert median([1, 2, 3, 4]) == 2.5


def test_median_odd_count():
    assert median([1, 2, 3]) == 2


def test_median_empty_is_none():
    assert median([]) is None


def test_p90_hand_computed():
    assert p90(list(range(1, 11))) == pytest.approx(9.1)


def test_p90_single_value():
    assert p90([5]) == 5


# --- Spearman rho with ties -----------------------------------------------------------

def test_spearman_rho_no_ties_hand_computed():
    # ranks x=[1,2,3], ranks y=[1,3,2] -> rho = 0.5 (hand-derived, see module docstring math)
    rho, n = spearman_rho([1, 2, 3], [10, 30, 20])
    assert rho == pytest.approx(0.5)
    assert n == 3


def test_spearman_rho_with_ties_uses_average_rank():
    # x=[1,1,2] ties at rank 1.5/1.5, y=[10,20,30] no ties -> rho = 1.5/sqrt(3)
    rho, n = spearman_rho([1, 1, 2], [10, 20, 30])
    assert rho == pytest.approx(1.5 / math.sqrt(3))
    assert n == 3


def test_spearman_rho_perfect_correlation():
    rho, n = spearman_rho([1, 2, 3, 4], [10, 20, 30, 40])
    assert rho == pytest.approx(1.0)


def test_spearman_rho_no_variance_is_none():
    rho, n = spearman_rho([1, 1, 1], [10, 20, 30])
    assert rho is None
    assert n == 3


def test_spearman_rho_too_few_points_is_none():
    rho, n = spearman_rho([1], [10])
    assert rho is None
    assert n == 1


# --- validity badge: rho < 0.3 or n < 15 -----------------------------------------------

def test_validity_badge_low_rho_warns():
    assert validity_badge(0.2, 20) is True


def test_validity_badge_low_n_warns():
    assert validity_badge(0.9, 10) is True


def test_validity_badge_none_rho_warns():
    assert validity_badge(None, 3) is True


def test_validity_badge_clean():
    assert validity_badge(0.5, 20) is False


# --- coverage --------------------------------------------------------------------------

def test_coverage_line():
    assert coverage(3, 10) == {"counted": 3, "total": 10, "pct": 30.0}


def test_coverage_zero_total_is_none_pct():
    assert coverage(0, 0) == {"counted": 0, "total": 0, "pct": None}


# --- shipped_issue_rows: pulls pts/closed_at onto the T4 issue_rollups ------------------

def test_shipped_issue_rows_only_includes_shipped():
    rollups = {
        ("o/r", 10): {"attributed_usd": 4.0, "shipped": True, "requester": "jason", "requester_source": "requested_by"},
        ("o/r", 11): {"attributed_usd": 1.0, "shipped": False, "requester": None, "requester_source": None},
    }
    rows = shipped_issue_rows(rollups, [issue(number=10, pts=3), issue(number=11, pts=5, state_reason="not_planned")])
    assert [r["number"] for r in rows] == [10]
    assert rows[0]["pts"] == 3
    assert rows[0]["attributed_usd"] == 4.0
    assert rows[0]["closed_at"] == "2026-09-02T00:00:00Z"


# --- $/pt: unrated/missing pts excluded from the ratio but counted in coverage ---------

def test_cost_per_point_excludes_unrated_but_counts_coverage():
    rows = [
        {"repo": "o/r", "number": 1, "pts": 2, "attributed_usd": 4.0, "closed_at": "2026-09-01T00:00:00Z"},
        {"repo": "o/r", "number": 2, "pts": None, "attributed_usd": 9.0, "closed_at": "2026-09-01T00:00:00Z"},
    ]
    stats = cost_per_point_stats(rows)
    assert stats["median"] == 2.0  # only the rated row (4.0/2) enters the ratio
    assert stats["coverage"] == {"counted": 1, "total": 2, "pct": 50.0}


def test_cost_per_point_median_and_p90_over_several_issues():
    rows = [
        {"repo": "o/r", "number": i, "pts": 1, "attributed_usd": v, "closed_at": "2026-09-01T00:00:00Z"}
        for i, v in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], start=1)
    ]
    stats = cost_per_point_stats(rows)
    assert stats["median"] == pytest.approx(5.5)
    assert stats["p90"] == pytest.approx(9.1)


def test_cost_per_point_no_rated_rows_is_none():
    rows = [{"repo": "o/r", "number": 1, "pts": None, "attributed_usd": 4.0, "closed_at": None}]
    stats = cost_per_point_stats(rows)
    assert stats["median"] is None
    assert stats["p90"] is None
    assert stats["coverage"] == {"counted": 0, "total": 1, "pct": 0.0}


# --- points validity: Spearman over the population, badge on rho<0.3 or n<15 -----------

def test_points_validity_excludes_unrated_rows():
    rows = [
        {"repo": "o/r", "number": 1, "pts": 1, "attributed_usd": 10.0, "closed_at": "2026-09-01T00:00:00Z"},
        {"repo": "o/r", "number": 2, "pts": None, "attributed_usd": 999.0, "closed_at": "2026-09-01T00:00:00Z"},
        {"repo": "o/r", "number": 3, "pts": 2, "attributed_usd": 20.0, "closed_at": "2026-09-01T00:00:00Z"},
    ]
    result = points_validity(rows, as_of="2026-09-15")
    assert result["n"] == 2
    assert result["rho"] == pytest.approx(1.0)
    assert result["badge"] is True  # n < 15


def test_points_validity_windows_to_90_days():
    rows = [
        {"repo": "o/r", "number": 1, "pts": 1, "attributed_usd": 10.0, "closed_at": "2026-01-01T00:00:00Z"},
        {"repo": "o/r", "number": 2, "pts": 2, "attributed_usd": 20.0, "closed_at": "2026-09-01T00:00:00Z"},
    ]
    result = points_validity(rows, as_of="2026-09-15", window_days=90)
    assert result["n"] == 1  # the January issue falls outside the 90-day window


# --- per_repo_table -----------------------------------------------------------------

def test_per_repo_table_groups_and_flags_validity():
    rows = [
        {"repo": "o/a", "number": 1, "pts": 1, "attributed_usd": 10.0, "closed_at": "2026-09-01T00:00:00Z"},
        {"repo": "o/a", "number": 2, "pts": 2, "attributed_usd": 20.0, "closed_at": "2026-09-01T00:00:00Z"},
        {"repo": "o/b", "number": 3, "pts": 3, "attributed_usd": 9.0, "closed_at": "2026-09-01T00:00:00Z"},
    ]
    table = per_repo_table(rows, as_of="2026-09-15")
    assert [r["repo"] for r in table] == ["o/a", "o/b"]
    a = table[0]
    assert a["points_shipped"] == 3
    assert a["validity"]["badge"] is True  # n=2 < 15
    assert a["cost_per_point"]["median"] == pytest.approx(10.0)


# --- durable merge rate ----------------------------------------------------------------

def test_durable_merge_rate_hand_computed():
    pr_rollups = {
        ("o/r", 1): {"durable": True}, ("o/r", 2): {"durable": True}, ("o/r", 3): {"durable": False},
        ("o/r", 4): {"durable": None},  # never merged -- excluded from the rate
    }
    result = durable_merge_rate(pr_rollups)
    assert result["rate"] == pytest.approx(2 / 3)
    assert result["coverage"] == {"counted": 3, "total": 3, "pct": 100.0}


def test_durable_merge_rate_no_merged_prs_is_none():
    result = durable_merge_rate({("o/r", 1): {"durable": None}})
    assert result["rate"] is None


# --- dead-end $: not_planned issues + stale local branches, top 10 by $, linked -------

def test_dead_end_payload_combines_and_sorts_and_links():
    issue_rollups = {("o/r", 5): {"attributed_usd": 12.0}}
    not_planned = [issue(number=5, state_reason="not_planned")]
    records = [usage_record(repo="o/r", branch="feat/99-x", cost_usd=3.0, kind="interactive")]
    stale = [("o/r", "feat/99-x")]
    result = dead_end_payload(issue_rollups, not_planned, records, stale)
    assert result["usd"] == pytest.approx(15.0)
    assert result["top"][0]["usd"] == pytest.approx(12.0)
    assert result["top"][0]["url"] == "https://github.com/o/r/issues/5"
    assert result["top"][1]["url"] == "https://github.com/o/r/tree/feat/99-x"
    assert "no data yet" in result["excluded_note"] or "factory#109" in result["excluded_note"]


def test_dead_end_payload_does_not_double_count_a_stale_branch_linked_to_a_not_planned_issue_I4():
    # A stale branch whose closed-unmerged PR closes a not_planned issue: its
    # $15 is attributed to that issue (counted there) and must NOT be counted
    # again as branch $. A second record on the same branch that is NOT
    # attributed to the not_planned issue still counts as branch $.
    records = [usage_record(repo="o/r", branch="feat/5-x", cost_usd=15.0),
               usage_record(repo="o/r", branch="feat/5-x", cost_usd=2.0)]
    attrs = [{"attributed": True, "issue": ("o/r", 5)},
             {"attributed": False, "issue": None}]
    issue_rollups = {("o/r", 5): {"attributed_usd": 15.0}}
    not_planned = [issue(number=5, state_reason="not_planned")]
    result = dead_end_payload(issue_rollups, not_planned, records, [("o/r", "feat/5-x")],
                              attributions=attrs)
    assert result["usd"] == pytest.approx(17.0)          # not 32
    by_type = {d["type"]: d["usd"] for d in result["top"]}
    assert by_type == {"issue": pytest.approx(15.0), "branch": pytest.approx(2.0)}


def test_build_outcomes_payload_dead_end_probe_is_not_double_counted_I4():
    records = [usage_record(actor="human:jason", repo="o/r", branch="feat/5-x",
                            day="2026-08-01", cost_usd=15.0)]
    issues = [issue(number=5, pts=None, requested_by="jason", state_reason="not_planned",
                    closed_at="2026-08-10T00:00:00Z")]
    prs = [pr(number=50, head_ref="feat/5-x", state="closed", merged_at=None, closes=[5])]
    payload = build_outcomes_payload(records, issues, prs, as_of="2026-09-15",
                                     outcomes_as_of=None, scope="jason")
    assert payload["tiles"]["dead_end_usd"]["usd"] == pytest.approx(15.0)
    assert [d["type"] for d in payload["dead_end_list"]] == ["issue"]


def test_dead_end_payload_caps_at_ten():
    not_planned = [issue(number=n, state_reason="not_planned") for n in range(1, 13)]
    issue_rollups = {("o/r", n): {"attributed_usd": float(n)} for n in range(1, 13)}
    result = dead_end_payload(issue_rollups, not_planned, [], [])
    assert len(result["top"]) == 10
    assert result["top"][0]["usd"] == 12.0  # highest first


# --- points shipped this week -----------------------------------------------------------

def test_points_shipped_this_week_only_counts_the_current_iso_week():
    rows = [
        {"repo": "o/r", "number": 1, "pts": 3, "attributed_usd": 1.0, "closed_at": "2026-09-14T00:00:00Z"},  # Monday of the week containing 2026-09-15
        {"repo": "o/r", "number": 2, "pts": 5, "attributed_usd": 1.0, "closed_at": "2026-08-01T00:00:00Z"},  # a different week
    ]
    result = points_shipped_this_week(rows, as_of="2026-09-15")
    assert result["points"] == 3


def test_points_shipped_this_week_unrated_counted_in_coverage_not_points():
    rows = [
        {"repo": "o/r", "number": 1, "pts": None, "attributed_usd": 1.0, "closed_at": "2026-09-14T00:00:00Z"},
    ]
    result = points_shipped_this_week(rows, as_of="2026-09-15")
    assert result["points"] == 0
    assert result["coverage"] == {"counted": 0, "total": 1, "pct": 0.0}


def test_iso_week_format():
    assert iso_week("2026-09-15") == "2026-W38"


# --- autonomy trend: factory share of points shipped + human $ per factory-shipped pt --

def test_autonomy_trend_factory_share_and_human_cost_per_point():
    records = [
        usage_record(actor="factory:wb-impl-r", kind="unknown", branch=None, issue=10, repo="o/r", cost_usd=2.0),
        usage_record(actor="human:jason", kind="interactive", branch="feat/10-x", repo="o/r", cost_usd=1.0),
    ]
    attributions = [
        {"pr": None, "issue": ("o/r", 10), "requester": "jason", "requester_source": "requested_by", "attributed": True},
        {"pr": ("o/r", 20), "issue": ("o/r", 10), "requester": "jason", "requester_source": "requested_by", "attributed": True},
    ]
    rows = [{"repo": "o/r", "number": 10, "pts": 4, "attributed_usd": 3.0, "closed_at": "2026-09-02T00:00:00Z"}]
    trend = autonomy_trend(records, attributions, rows)
    assert len(trend) == 1
    week = trend[0]
    assert week["factory_pts"] == 4
    assert week["total_pts"] == 4
    assert week["factory_share"] == 1.0
    assert week["human_usd_per_factory_pt"] == pytest.approx(1.0 / 4)


def test_autonomy_trend_non_factory_issue_has_zero_share():
    rows = [{"repo": "o/r", "number": 10, "pts": 4, "attributed_usd": 3.0, "closed_at": "2026-09-02T00:00:00Z"}]
    trend = autonomy_trend([], [], rows)
    assert trend[0]["factory_pts"] == 0
    assert trend[0]["factory_share"] == 0.0
    assert trend[0]["human_usd_per_factory_pt"] is None


# --- model fit: Opus/Fable $ joined to an issue rated <=2 pts, unjoined excluded --------

def test_model_fit_candidates_filters_by_model_class_and_pts():
    records = [
        usage_record(model_class="opus", cost_usd=5.0, session="s-opus"),
        usage_record(model_class="sonnet", cost_usd=5.0, session="s-sonnet"),  # wrong model class
        usage_record(model_class="fable", cost_usd=2.0, session="s-fable"),
    ]
    attributions = [
        {"pr": None, "issue": ("o/r", 10), "requester": "jason", "requester_source": "requested_by", "attributed": True},
        {"pr": None, "issue": ("o/r", 10), "requester": "jason", "requester_source": "requested_by", "attributed": True},
        {"pr": None, "issue": ("o/r", 11), "requester": "jason", "requester_source": "requested_by", "attributed": True},
    ]
    issues = [issue(number=10, pts=2), issue(number=11, pts=5)]  # 11 is too complex, excluded
    candidates = model_fit_candidates(records, attributions, issues)
    assert len(candidates) == 1
    assert candidates[0]["session"] == "s-opus"
    assert candidates[0]["pts"] == 2


def test_model_fit_candidates_excludes_unjoined_sessions():
    records = [usage_record(model_class="opus", cost_usd=5.0)]
    attributions = [{"pr": None, "issue": None, "requester": None, "requester_source": None, "attributed": False}]
    assert model_fit_candidates(records, attributions, [issue()]) == []


# --- unattributed slice: always shown with its $ share ----------------------------------

def test_unattributed_summary_hand_computed():
    records = [usage_record(cost_usd=3.0), usage_record(cost_usd=1.0)]
    attributions = [{"attributed": True}, {"attributed": False}]
    result = unattributed_summary(records, attributions)
    assert result == {"unattributed_usd": 1.0, "total_usd": 4.0, "pct": 25.0}


def test_unattributed_summary_zero_spend_is_none_pct():
    assert unattributed_summary([], [])["pct"] is None


# --- Amendment A-3 "Me" scoping: hands-on always in, factory only when I requested it ---

def test_actor_login_strips_human_prefix():
    assert actor_login("human:jason") == "jason"
    assert actor_login(None) is None


def test_scope_to_me_keeps_all_interactive_records():
    records = [usage_record(kind="interactive")]
    attrs = [{"attributed": False, "requester": None}]
    scoped_r, scoped_a = scope_to_me(records, attrs, "jason")
    assert scoped_r == records


def test_scope_to_me_keeps_factory_records_i_commissioned():
    records = [usage_record(actor="factory:wb-impl-r", kind="unknown")]
    attrs = [{"attributed": True, "requester": "jason"}]
    scoped_r, scoped_a = scope_to_me(records, attrs, "jason")
    assert scoped_r == records


def test_scope_to_me_drops_factory_records_someone_else_commissioned():
    records = [usage_record(actor="factory:wb-impl-r", kind="unknown")]
    attrs = [{"attributed": True, "requester": "ricky"}]
    scoped_r, scoped_a = scope_to_me(records, attrs, "jason")
    assert scoped_r == []


def test_scope_to_me_drops_unattributed_factory_records():
    # can't confirm it's mine -- excluded rather than shown as unattributed noise
    records = [usage_record(actor="factory:wb-impl-r", kind="unknown")]
    attrs = [{"attributed": False, "requester": None}]
    scoped_r, scoped_a = scope_to_me(records, attrs, "jason")
    assert scoped_r == []


def test_scope_to_me_no_login_keeps_nothing():
    # I5: with no login nothing can be confirmed as "mine" -- not even an
    # interactive record (it may be a teammate's, e.g. from a shared archive).
    records = [usage_record(kind="interactive"), usage_record(actor="factory:x", kind="unknown")]
    attrs = [{"attributed": False, "requester": None}, {"attributed": True, "requester": "jason"}]
    scoped_r, scoped_a = scope_to_me(records, attrs, None)
    assert scoped_r == [] and scoped_a == []


def test_scope_to_me_drops_a_teammates_interactive_records_I5():
    records = [usage_record(actor="human:jason", kind="interactive", cost_usd=1.0),
               usage_record(actor="human:ricky", kind="interactive", cost_usd=5.0)]
    attrs = [{"attributed": False, "requester": None}, {"attributed": False, "requester": None}]
    scoped_r, _ = scope_to_me(records, attrs, "jason")
    assert [r["actor"] for r in scoped_r] == ["human:jason"]


def test_build_outcomes_payload_me_view_excludes_a_teammates_interactive_spend_I5():
    records = [usage_record(actor="human:jason", cost_usd=1.0, branch="main"),
               usage_record(actor="human:ricky", cost_usd=5.0, branch="main")]
    payload = build_outcomes_payload(records, [], [], as_of="2026-09-15",
                                     outcomes_as_of=None, scope="jason")
    assert payload["unattributed"]["total_usd"] == pytest.approx(1.0)


# --- me_issue_population: WHICH issues are in scope, not just their $ (ruling R10) -------

def test_me_issue_population_includes_issues_i_requested():
    issues = [issue(number=10, requested_by="jason"), issue(number=11, requested_by="ricky")]
    keys = me_issue_population(issues, scoped_attrs=[], me_login="jason")
    assert keys == {("o/r", 10)}


def test_me_issue_population_falls_back_to_author_when_no_requested_by():
    issues = [issue(number=10, requested_by=None, author="jason")]
    keys = me_issue_population(issues, scoped_attrs=[], me_login="jason")
    assert keys == {("o/r", 10)}


def test_me_issue_population_unions_in_issues_my_scoped_records_attribute_to():
    issues = [issue(number=11, requested_by="ricky")]  # not mine by requester...
    scoped_attrs = [{"attributed": True, "issue": ("o/r", 11), "requester": "ricky"}]  # ...but I have $ logged on it
    keys = me_issue_population(issues, scoped_attrs, me_login="jason")
    assert keys == {("o/r", 11)}


def test_me_issue_population_excludes_a_teammates_untouched_issue():
    issues = [issue(number=11, requested_by="ricky")]
    keys = me_issue_population(issues, scoped_attrs=[], me_login="jason")
    assert keys == set()


def test_me_issue_population_no_login_is_union_only():
    issues = [issue(number=10, requested_by="jason")]
    keys = me_issue_population(issues, scoped_attrs=[], me_login=None)
    assert keys == set()


# --- me_pr_population: WHICH PRs feed durable_merge_rate (ruling R10a) -------------------

def test_me_pr_population_includes_prs_i_authored():
    prs = [pr(number=20, author="jason"), pr(number=21, author="ricky")]
    keys = me_pr_population(prs, me_keys=set(), me_login="jason")
    assert keys == {("o/r", 20)}


def test_me_pr_population_unions_prs_linked_to_my_issues():
    # ricky authored it, but it closes an issue that's in MY issue population
    prs = [pr(number=21, author="ricky", closes=[11])]
    keys = me_pr_population(prs, me_keys={("o/r", 11)}, me_login="jason")
    assert keys == {("o/r", 21)}


def test_me_pr_population_excludes_a_teammates_unlinked_pr():
    prs = [pr(number=21, author="ricky", closes=[])]
    keys = me_pr_population(prs, me_keys=set(), me_login="jason")
    assert keys == set()


def test_me_pr_population_no_login_is_union_only():
    prs = [pr(number=20, author="jason", closes=[10])]
    keys = me_pr_population(prs, me_keys=set(), me_login=None)
    assert keys == set()


# --- build_outcomes_payload: full assembly, rescoped dollars, honest coverage ------------

def test_build_outcomes_payload_rescopes_dollars_to_me():
    # jason's own session ($1) plus a factory session HE commissioned ($2) plus a
    # factory session RICKY commissioned on the same repo ($100, must not leak in).
    records = [
        usage_record(actor="human:jason", kind="interactive", branch="feat/10-x", repo="o/r", cost_usd=1.0),
        usage_record(actor="factory:wb-impl-r", kind="unknown", branch=None, issue=10, repo="o/r", cost_usd=2.0),
        usage_record(actor="factory:wb-impl-r", kind="unknown", branch=None, issue=11, repo="o/r", cost_usd=100.0),
    ]
    issues = [issue(number=10, pts=2, requested_by="jason"),
              issue(number=11, pts=2, requested_by="ricky")]
    prs = [pr(number=20, closes=[10])]
    payload = build_outcomes_payload(records, issues, prs, as_of="2026-09-15",
                                      outcomes_as_of="2026-09-15T00:00:00Z", scope="jason")
    assert payload["outcomes_as_of"] == "2026-09-15T00:00:00Z"
    # issue 10 (mine): $1 + $2 = $3 attributed; issue 11 (ricky's, dropped) never appears
    per_repo = payload["per_repo"][0]
    assert per_repo["repo"] == "o/r"
    # $/pt median over rated rows: issue 10 -> 3.0/2 = 1.5 ; issue 11 excluded entirely
    assert per_repo["cost_per_point"]["median"] == pytest.approx(1.5)
    assert per_repo["cost_per_point"]["coverage"]["total"] == 1  # only issue 10 shipped+scoped


def test_build_outcomes_payload_excludes_a_teammates_shipped_rated_issue_R10():
    # Controller ruling R10 regression (fix round 1) -- exact reviewer repro:
    # my shipped/rated issue (#10) + a teammate's SHIPPED/rated issue (#11,
    # $0 from me) + a teammate's not_planned issue (#12). Before the fix,
    # `shipped_issue_rows` pulled #11's pts into the per-repo ratio/coverage/
    # validity pool and #12 into the dead-end list just because `scope_to_me`
    # zeroed (rather than removed) their dollars -- the issue itself still
    # flowed through unscoped.
    records = [
        usage_record(actor="human:jason", kind="interactive", branch="feat/10-x",
                     repo="o/r", cost_usd=1.0),
        usage_record(actor="factory:wb-impl-r", kind="unknown", branch=None,
                     issue=10, repo="o/r", cost_usd=2.0),
        usage_record(actor="factory:wb-impl-r", kind="unknown", branch=None,
                     issue=11, repo="o/r", cost_usd=50.0),  # ricky's factory spend, not mine
    ]
    issues = [
        issue(number=10, pts=2, requested_by="jason", closed_at="2026-09-14T00:00:00Z"),
        issue(number=11, pts=5, requested_by="ricky", closed_at="2026-09-14T00:00:00Z"),
        issue(number=12, pts=8, requested_by="ricky", state_reason="not_planned", closed_at=None),
    ]
    prs = [
        pr(number=20, closes=[10], head_ref="feat/10-x"),
        pr(number=21, closes=[11], head_ref="feat/11-x"),
    ]
    payload = build_outcomes_payload(records, issues, prs, as_of="2026-09-15",
                                      outcomes_as_of="2026-09-15T00:00:00Z", scope="jason")

    per_repo = payload["per_repo"][0]
    assert per_repo["cost_per_point"]["median"] == pytest.approx(1.5)   # only #10: $3 / 2pts
    assert per_repo["cost_per_point"]["coverage"] == {"counted": 1, "total": 1, "pct": 100.0}
    assert per_repo["validity"]["n"] == 1                                # #11 excluded entirely, not just unrated

    assert payload["tiles"]["points_shipped_this_week"]["points"] == 2  # not 2 + 5

    dead_end_keys = {(d["repo"], d.get("number")) for d in payload["dead_end_list"]}
    assert ("o/r", 12) not in dead_end_keys                              # ricky's not_planned issue excluded


def test_build_outcomes_payload_includes_an_issue_i_requested_even_with_zero_dollars_so_far():
    # Union clause 1 of ruling R10: an issue I requested belongs to "Me" even
    # when none of my (scoped) usage records have attributed anything to it
    # yet (e.g. the factory transcript for the commissioning session hasn't
    # been mirrored locally) -- inclusion must not depend on $ > 0.
    issues = [issue(number=13, pts=3, requested_by="jason", closed_at="2026-09-14T00:00:00Z")]
    prs = [pr(number=23, closes=[13], head_ref="feat/13-x")]
    payload = build_outcomes_payload([], issues, prs, as_of="2026-09-15",
                                      outcomes_as_of="2026-09-15T00:00:00Z", scope="jason")
    per_repo = payload["per_repo"][0]
    assert per_repo["points_shipped"] == 3
    assert per_repo["cost_per_point"]["coverage"] == {"counted": 1, "total": 1, "pct": 100.0}
    assert per_repo["cost_per_point"]["median"] == 0.0  # $0 attributed so far, still counted (not excluded)


def test_build_outcomes_payload_durable_merge_rate_scoped_to_me_R10a():
    # Controller ruling R10a regression: MY reverted PR (#20, reverted by my
    # own #30) must lower MY durable-merge rate; a TEAMMATE's reverted PR
    # (#40, reverted by ricky's own #41), never linked to any of my issues,
    # must not -- it's simply not in my PR population at all.
    issues = [issue(number=10, pts=2, requested_by="jason",
                    closed_at="2026-09-14T00:00:00Z")]
    prs = [
        pr(number=20, author="jason", closes=[10], head_ref="feat/10-x", state="merged"),
        pr(number=30, author="jason", closes=[], head_ref="revert-20-x", state="merged",
           reverts=20),
        pr(number=40, author="ricky", closes=[], head_ref="feat/99-x", state="merged"),
        pr(number=41, author="ricky", closes=[], head_ref="revert-40-x", state="merged",
           reverts=40),
    ]
    payload = build_outcomes_payload([], issues, prs, as_of="2026-09-15",
                                      outcomes_as_of="2026-09-15T00:00:00Z", scope="jason")
    rate = payload["tiles"]["durable_merge_rate"]
    # only MY two PRs (#20, #30) count -- ricky's #40/#41 are excluded entirely,
    # so ricky's revert never drags down my rate.
    assert rate["coverage"] == {"counted": 2, "total": 2, "pct": 100.0}
    assert rate["rate"] == pytest.approx(0.5)  # #20 reverted (not durable), #30 durable -> 1/2


def test_build_outcomes_payload_shape_has_all_sections():
    payload = build_outcomes_payload([], [], [], as_of="2026-09-15", outcomes_as_of=None, scope="jason")
    for key in ("outcomes_as_of", "tiles", "per_repo", "autonomy_trend", "dead_end_list", "model_fit", "unattributed"):
        assert key in payload
    assert payload["tiles"]["rework_share"]["share"] is None
    assert "no data yet" in payload["tiles"]["rework_share"]["note"] or "factory#109" in payload["tiles"]["rework_share"]["note"]


# --- I6 (final review): an explicit scope -- None is the unscoped team view ----------

def _team_fixture():
    records = [
        usage_record(actor="human:jason", kind="interactive", branch="feat/10-x", cost_usd=1.0),
        usage_record(actor="human:ricky", kind="interactive", branch="main", cost_usd=5.0),
        usage_record(actor="factory:wb-impl-r", kind="unknown", branch=None, issue=10, cost_usd=2.0),
        usage_record(actor="factory:wb-impl-r", kind="unknown", branch=None, issue=11, cost_usd=3.0),
        usage_record(actor="factory:wb-impl-r", kind="unknown", branch=None, issue=None, cost_usd=4.0),
    ]
    issues = [issue(number=10, pts=2, requested_by="jason", closed_at="2026-09-14T00:00:00Z"),
              issue(number=11, pts=3, requested_by="ricky", closed_at="2026-09-14T00:00:00Z")]
    prs = [pr(number=20, closes=[10], head_ref="feat/10-x"),
           pr(number=21, author="ricky", closes=[11], head_ref="feat/11-x")]
    return records, issues, prs


def test_build_outcomes_payload_team_view_counts_every_record_I6():
    records, issues, prs = _team_fixture()
    payload = build_outcomes_payload(records, issues, prs, as_of="2026-09-15",
                                     outcomes_as_of=None, scope=None)
    assert payload["unattributed"]["total_usd"] == pytest.approx(sum(r["cost_usd"] for r in records))
    per_repo = payload["per_repo"][0]
    assert per_repo["points_shipped"] == 5                      # both issues, not just mine
    assert per_repo["cost_per_point"]["coverage"]["total"] == 2
    assert payload["tiles"]["durable_merge_rate"]["coverage"]["total"] == 2


def test_build_outcomes_payload_me_view_is_unchanged_by_the_scope_parameter_I6():
    records, issues, prs = _team_fixture()
    payload = build_outcomes_payload(records, issues, prs, as_of="2026-09-15",
                                     outcomes_as_of=None, scope="jason")
    # my interactive $1 + the factory $2 I commissioned; ricky's $5/$3 and the
    # unattributed factory $4 are not mine
    assert payload["unattributed"]["total_usd"] == pytest.approx(3.0)
    assert payload["per_repo"][0]["points_shipped"] == 2
    assert payload["per_repo"][0]["cost_per_point"]["median"] == pytest.approx(1.5)


# --- metric guide: weekly baselines, dead_end_share, verdicts -------------------------

from metric_guide import load_guide
from outcome_metrics import weekly_baselines


def _row(closed, pts, usd, number=1):
    return {"repo": "o/r", "number": number, "pts": pts, "closed_at": closed + "T12:00:00Z",
            "attributed_usd": usd}


def test_weekly_baselines_hand_computed():
    # 2026-09-14..20 is ISO W38 (latest); W34..W37 are the 4 prior weeks.
    rows = [
        _row("2026-09-15", 2, 10.0, 1), _row("2026-09-16", 4, 8.0, 2),   # W38: $/pt 5, 2 ; pts 6
        _row("2026-09-08", 2, 20.0, 3),                                   # W37: 10 ; pts 2
        _row("2026-08-27", 1, 6.0, 4), _row("2026-08-27", 3, 6.0, 5),     # W35: 6, 2 ; pts 4
        _row("2026-08-01", 1, 99.0, 6),                                   # W31: outside window
        {"repo": "o/r", "number": 7, "pts": None, "closed_at": "2026-09-16T00:00:00Z",
         "attributed_usd": 50.0},                                         # unrated: ignored
    ]
    b = weekly_baselines(rows, "2026-09-20")
    assert b["week"] == "2026-W38"
    assert b["cost_per_point"]["current"] == median([5.0, 2.0])         # 3.5
    assert b["cost_per_point"]["baseline"] == median([10.0, 6.0, 2.0])  # 6.0
    assert b["throughput"]["current"] == 6
    assert b["throughput"]["baseline"] == (2 + 4) / 4                   # mean over 4 weeks, zeros count


def test_weekly_baselines_empty_sides_are_none():
    only_latest = [_row("2026-09-15", 2, 10.0)]
    b = weekly_baselines(only_latest, "2026-09-20")
    assert b["cost_per_point"]["baseline"] is None and b["throughput"]["baseline"] is None
    assert b["cost_per_point"]["current"] == 5.0
    empty = weekly_baselines([], "2026-09-20")
    assert empty["week"] is None
    assert empty["cost_per_point"] == {"current": None, "baseline": None}


def test_weekly_baselines_ignores_weeks_after_as_of():
    rows = [_row("2026-09-30", 2, 10.0), _row("2026-09-15", 2, 10.0)]
    assert weekly_baselines(rows, "2026-09-20")["week"] == "2026-W38"


def _guide_payload(records, issues=None, prs=None, guide=True, **kw):
    return build_outcomes_payload(records, issues or [], prs or [], as_of="2026-09-15",
                                  outcomes_as_of="x", scope="jason",
                                  guide=load_guide() if guide else None, **kw)


def test_dead_end_share_is_dead_end_over_scoped_spend():
    records = [
        usage_record(actor="human:jason", branch="feat/10-x", issue=None, cost_usd=90.0,
                     day="2026-09-14"),
        usage_record(actor="human:jason", branch="feat/dead", issue=None, cost_usd=10.0,
                     day="2026-08-01"),
        usage_record(actor="human:other", branch="feat/z", cost_usd=1000.0),   # not Me
    ]
    prs = [pr(number=30, head_ref="feat/dead", state="closed", merged_at=None, closes=[])]
    p = _guide_payload(records, prs=prs)
    assert p["tiles"]["dead_end_usd"]["usd"] == pytest.approx(10.0)
    assert p["dead_end_share"] == pytest.approx(0.1)
    assert p["verdicts"]["dead_end_share"]["verdict"] == "watch"


def test_dead_end_share_none_without_spend():
    p = _guide_payload([])
    assert p["dead_end_share"] is None
    assert p["verdicts"]["dead_end_share"]["verdict"] == "no_data"


def test_outcomes_verdicts_cover_every_metric_and_null_degrades():
    p = _guide_payload([usage_record(cost_usd=1.0)])
    assert set(p["verdicts"]) == {"durable_merge_rate", "rework_share", "dead_end_share",
                                  "validity_rho", "cost_per_point", "throughput"}
    assert p["verdicts"]["durable_merge_rate"]["verdict"] == "no_data"
    assert p["verdicts"]["rework_share"]["verdict"] == "no_data"
    assert p["verdicts"]["validity_rho"]["verdict"] == "no_data"
    assert p["verdicts"]["cost_per_point"]["comparison"] == "not_enough_data"
    assert p["validity"]["n"] == 0


def test_outcomes_durable_verdict_uses_the_rate():
    records = [usage_record(branch="feat/10-x", cost_usd=1.0)]
    issues = [issue(number=10, pts=2)]
    prs = [pr(number=20, closes=[10])]
    p = _guide_payload(records, issues, prs)
    assert p["tiles"]["durable_merge_rate"]["rate"] == 1.0
    assert p["verdicts"]["durable_merge_rate"]["verdict"] == "good"


def test_validity_verdict_keeps_low_confidence_flag_for_small_n():
    v = _guide_payload([usage_record()])["validity"]
    assert v["badge"] is True                    # n < 15 -> existing warning semantics


def test_no_guide_means_no_verdict_keys_but_dead_end_share_stays():
    p = _guide_payload([usage_record()], guide=False, guide_error="missing")
    assert "verdicts" not in p and p["verdicts_unavailable"] == "missing"
    assert "dead_end_share" in p


def test_unscoped_team_view_also_gets_verdicts():
    p = build_outcomes_payload([usage_record()], [], [], as_of="2026-09-15",
                               outcomes_as_of="x", scope=None, guide=load_guide())
    assert "verdicts" in p
