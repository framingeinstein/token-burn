# tests/test_prices.py
from prices import load_prices, rate_for

def _table():
    return {
        "version": "test",
        "default_cache_write_mult": 1.25,
        "default_cache_read_mult": 0.10,
        "rates": [
            {"match": "claude-opus-4-8*", "in": 15.0, "out": 75.0, "from": "2024-01-01", "to": "2026-01-01", "source": "legacy"},
            {"match": "claude-opus-4-8*", "in": 5.0, "out": 25.0, "from": "2026-01-01", "to": None, "source": "current"},
            {"match": "claude-haiku-4-5*", "in": 1.0, "out": 5.0, "from": "2026-01-01", "to": None, "source": "x"},
        ],
        "unpriced": ["<synthetic>"],
    }

def test_rate_for_matches_prefix_and_current_date():
    r = rate_for(_table(), "claude-opus-4-8-20260101", "2026-05-21")
    assert r["in"] == 5.0 and r["out"] == 25.0
    assert r["write_mult"] == 1.25 and r["read_mult"] == 0.10

def test_rate_for_selects_by_date_range():
    r = rate_for(_table(), "claude-opus-4-8", "2025-06-01")  # legacy window
    assert r["in"] == 15.0 and r["out"] == 75.0

def test_rate_for_unknown_model_is_none():
    assert rate_for(_table(), "gpt-5", "2026-05-21") is None

def test_rate_for_synthetic_is_none():
    assert rate_for(_table(), "<synthetic>", "2026-05-21") is None

def test_load_prices_reads_repo_table():
    p = load_prices()
    assert "version" in p and isinstance(p["rates"], list)
    assert rate_for(p, "claude-sonnet-4-6-20261101", "2026-06-01") is not None
    assert rate_for(p, "claude-haiku-4-5-20251001", "2025-10-05") is not None


def test_rate_for_none_model_is_none():
    assert rate_for(_table(), None, "2026-05-21") is None


# --- repo table: Anthropic list prices (claude-api reference, cached 2026-10-06) ---

import pytest


@pytest.mark.parametrize("model,inp,out,read_mult", [
    ("claude-opus-5-5", 4.0, 20.0, 0.05),
    ("claude-opus-5-5-20261001", 4.0, 20.0, 0.05),
    ("claude-fable-5-1", 10.0, 50.0, 0.025),
    ("claude-mythos-5-1", 10.0, 50.0, 0.025),
    ("claude-sonnet-5-5", 2.0, 10.0, 0.10),
    ("claude-opus-5", 5.0, 25.0, 0.10),
    ("claude-fable-5", 10.0, 50.0, 0.10),
    ("claude-mythos-5", 10.0, 50.0, 0.10),
    ("claude-sonnet-5", 2.0, 10.0, 0.10),
    ("claude-haiku-4-5", 1.0, 5.0, 0.10),
])
def test_repo_table_rates(model, inp, out, read_mult):
    r = rate_for(load_prices(), model, "2026-10-22")
    assert (r["in"], r["out"]) == (inp, out)
    assert r["read_mult"] == pytest.approx(read_mult)
    assert r["write_mult"] == 1.25


def test_repo_table_version_and_source():
    p = load_prices()
    assert p["version"] == "2026-10-22"
    assert p["source"] == "Anthropic list prices (claude-api reference, cached 2026-10-06)"
