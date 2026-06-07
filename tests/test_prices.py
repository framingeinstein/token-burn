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
