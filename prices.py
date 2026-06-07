"""Dated price table for the token-burn cost ($) metric.

Cost is FROZEN at capture: each day is priced at the rate effective on that day,
read from prices.json (version-stamped). Unknown / synthetic models return None
(priced at $0 and reported) so they are never silently mispriced.
"""
import json
from pathlib import Path

_DEFAULT_PATH = Path(__file__).parent / "prices.json"


def load_prices(path=None):
    with open(path or _DEFAULT_PATH) as fh:
        return json.load(fh)


def _matches(pattern, model):
    if pattern.endswith("*"):
        return model.startswith(pattern[:-1])
    return model == pattern


def rate_for(prices, model, date):
    """Effective rate dict for (model, date), or None if unpriced/unknown."""
    model = model or ""
    if model in prices.get("unpriced", []):
        return None
    wmult = prices.get("default_cache_write_mult", 1.25)
    rmult = prices.get("default_cache_read_mult", 0.10)
    for r in prices.get("rates", []):
        if not _matches(r["match"], model):
            continue
        frm, to = r.get("from"), r.get("to")
        if frm and date < frm:
            continue
        if to and date >= to:
            continue
        return {"in": r["in"], "out": r["out"],
                "write_mult": r.get("cache_write_mult", wmult),
                "read_mult": r.get("cache_read_mult", rmult)}
    return None
