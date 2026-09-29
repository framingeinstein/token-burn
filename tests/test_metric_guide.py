# tests/test_metric_guide.py -- thresholds live in metric-guide.json; metric_guide.py is pure
import copy
import json

import pytest

import metric_guide
from metric_guide import (GuideError, classify, classify_relative, compare_to_baseline,
                          load_guide, validate_guide)

GUIDE = load_guide()


def v(metric, value):
    return classify(GUIDE, metric, value)["verdict"]


# (metric, value, expected) -- both sides of every edge, exactly as the brief writes them
BOUNDARIES = [
    ("cache_hit_rate", 0.95, "good"), ("cache_hit_rate", 0.9499, "watch"),
    ("cache_hit_rate", 0.85, "watch"), ("cache_hit_rate", 0.8499, "poor"),
    ("cache_hit_rate", 1.0, "good"), ("cache_hit_rate", 0.0, "poor"),
    ("context_per_call", 79999, "good"), ("context_per_call", 80000, "watch"),
    ("context_per_call", 150000, "watch"), ("context_per_call", 150001, "poor"),
    ("context_growth", 1.5, "good"), ("context_growth", 1.5001, "watch"),
    ("context_growth", 2.5, "watch"), ("context_growth", 2.5001, "poor"),
    ("durable_merge_rate", 0.95, "good"), ("durable_merge_rate", 0.9499, "watch"),
    ("durable_merge_rate", 0.90, "watch"), ("durable_merge_rate", 0.8999, "poor"),
    ("rework_share", 0.15, "good"), ("rework_share", 0.1501, "watch"),
    ("rework_share", 0.30, "watch"), ("rework_share", 0.3001, "poor"),
    ("dead_end_share", 0.0499, "good"), ("dead_end_share", 0.05, "watch"),
    ("dead_end_share", 0.15, "watch"), ("dead_end_share", 0.1501, "poor"),
    ("validity_rho", 0.5, "good"), ("validity_rho", 0.4999, "watch"),
    ("validity_rho", 0.3, "watch"), ("validity_rho", 0.2999, "poor"),
    ("validity_rho", -0.4, "poor"),
]


@pytest.mark.parametrize("metric,value,expected", BOUNDARIES)
def test_classify_at_every_boundary(metric, value, expected):
    assert v(metric, value) == expected


@pytest.mark.parametrize("metric", ["cache_hit_rate", "context_per_call", "context_growth",
                                    "durable_merge_rate", "rework_share", "dead_end_share",
                                    "validity_rho", "output_share"])
def test_null_is_no_data_except_info(metric):
    want = "info" if metric == "output_share" else "no_data"
    assert v(metric, None) == want


def test_output_share_is_info_with_no_direction_band():
    r = classify(GUIDE, "output_share", 0.02)
    assert r["verdict"] == "info" and r["direction"] == "none"
    assert "agentic" in r["note"]


def test_classify_carries_direction_label_meaning_action():
    r = classify(GUIDE, "context_per_call", 171000)
    assert r["verdict"] == "poor" and r["direction"] == "lower_is_better"
    assert r["label"] == "Context per call"
    assert "sub-agents" in r["action"] and r["meaning"]
    assert "idle gaps" in classify(GUIDE, "cache_hit_rate", 0.5)["action"]
    assert "floor, not a target" in classify(GUIDE, "cache_hit_rate", 0.99)["note"]


def test_good_verdict_has_no_action():
    assert classify(GUIDE, "cache_hit_rate", 0.99)["action"] == ""


def test_classify_unknown_metric_raises():
    with pytest.raises(GuideError):
        classify(GUIDE, "nope", 1)


# --- relative metrics ---

@pytest.mark.parametrize("cur,base,direction,want", [
    (89, 100, "lower_is_better", "better"), (90, 100, "lower_is_better", "same"),
    (110, 100, "lower_is_better", "same"), (111, 100, "lower_is_better", "worse"),
    (111, 100, "higher_is_better", "better"), (110, 100, "higher_is_better", "same"),
    (90, 100, "higher_is_better", "same"), (89, 100, "higher_is_better", "worse"),
    (5, 0, "higher_is_better", "better"), (5, 0, "lower_is_better", "worse"),
    (0, 0, "lower_is_better", "same"),
])
def test_compare_to_baseline(cur, base, direction, want):
    assert compare_to_baseline(cur, base, direction)["result"] == want


def test_compare_to_baseline_not_enough_data():
    assert compare_to_baseline(None, 5, "lower_is_better")["result"] == "not_enough_data"
    assert compare_to_baseline(5, None, "lower_is_better")["result"] == "not_enough_data"


def test_compare_custom_tolerance():
    assert compare_to_baseline(120, 100, "lower_is_better", tolerance=0.25)["result"] == "same"


def test_classify_relative_maps_to_verdicts_and_carries_numbers():
    r = classify_relative(GUIDE, "cost_per_point", 5.0, 10.0)
    assert r["comparison"] == "better" and r["verdict"] == "good"
    assert r["current"] == 5.0 and r["baseline"] == 10.0 and r["change"] == -0.5
    assert classify_relative(GUIDE, "throughput", 10, 10)["verdict"] == "info"
    assert classify_relative(GUIDE, "throughput", 5, 10)["verdict"] == "watch"
    assert classify_relative(GUIDE, "throughput", None, 10)["verdict"] == "no_data"


# --- validation on load ---

def _g():
    return copy.deepcopy(GUIDE)


def test_shipped_guide_is_valid():
    validate_guide(_g())


def test_unknown_metric_id_rejected():
    g = _g(); g["metrics"]["bogus"] = g["metrics"]["cache_hit_rate"]
    with pytest.raises(GuideError, match="bogus"):
        validate_guide(g)


def test_overlapping_bands_rejected():
    g = _g()
    g["metrics"]["cache_hit_rate"]["bands"][1]["hi"] = 0.96   # watch overlaps good
    with pytest.raises(GuideError, match="overlap"):
        validate_guide(g)


def test_touching_inclusive_edges_rejected():
    g = _g()
    g["metrics"]["cache_hit_rate"]["bands"][1]["hi_inclusive"] = True   # 0.95 in watch AND good
    with pytest.raises(GuideError, match="overlap"):
        validate_guide(g)


def test_gap_between_bands_rejected():
    g = _g()
    g["metrics"]["cache_hit_rate"]["bands"][1]["lo_inclusive"] = False
    with pytest.raises(GuideError, match="gap"):
        validate_guide(g)


def test_bad_direction_and_missing_text_rejected():
    g = _g(); g["metrics"]["cache_hit_rate"]["direction"] = "sideways"
    with pytest.raises(GuideError):
        validate_guide(g)
    g = _g(); del g["metrics"]["cache_hit_rate"]["meaning"]
    with pytest.raises(GuideError):
        validate_guide(g)


def test_missing_metric_rejected():
    g = _g(); del g["metrics"]["throughput"]
    with pytest.raises(GuideError, match="throughput"):
        validate_guide(g)


def test_load_guide_missing_or_invalid_file(tmp_path):
    with pytest.raises(GuideError):
        load_guide(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    with pytest.raises(GuideError):
        load_guide(bad)
    ok = tmp_path / "ok.json"; ok.write_text(json.dumps(GUIDE))
    assert load_guide(ok)["metrics"]["cache_hit_rate"]["direction"] == "higher_is_better"
