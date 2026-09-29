"""Metric guide: is higher or lower better, and is this value good / watch / poor?

Pure module -- the only I/O is `load_guide` reading `metric-guide.json`, where
every threshold lives as DATA (lo/hi with explicit inclusive flags, direction,
label, meaning, action) so bands are tunable without code. The dashboard and the
Synkhos factory snapshot job (synkhos/factory#172) read the same file and call
the same functions, so their verdicts agree.

- `classify(guide, metric_id, value)` -> {"verdict", "direction", "label", "note",
  "meaning", "action"}; verdict is good | watch | poor | no_data | info.
- `compare_to_baseline(current, baseline, direction, tolerance=0.10)` for the
  relative metrics ($/pt, throughput) -> {"result": better | same | worse |
  not_enough_data, "change"}.
- `classify_relative(guide, metric_id, current, baseline)` wraps the above into
  the same verdict-dict shape (better -> good, same -> info, worse -> watch).
"""
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "metric-guide.json"

BANDED = ("cache_hit_rate", "context_per_call", "context_growth", "durable_merge_rate",
          "rework_share", "dead_end_share", "validity_rho")
INFO = ("output_share",)
RELATIVE = ("cost_per_point", "throughput")
KNOWN_METRICS = BANDED + INFO + RELATIVE

_DIRECTIONS = ("higher_is_better", "lower_is_better")
_BAND_VERDICTS = ("good", "watch", "poor")
_RELATIVE_VERDICT = {"better": "good", "same": "info", "worse": "watch",
                     "not_enough_data": "no_data"}
_EPS = 1e-12


class GuideError(ValueError):
    """The guide file is missing, unreadable, or fails validation."""


# --- loading + validation ----------------------------------------------------

def load_guide(path=None):
    path = Path(path or DEFAULT_PATH)
    try:
        guide = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise GuideError(f"cannot load metric guide {path.name}: {e}") from e
    validate_guide(guide)
    return guide


def _check_text(mid, m, key, required=True):
    val = m.get(key)
    if val is None and not required:
        return
    if not isinstance(val, str) or (required and key != "action" and not val.strip()):
        raise GuideError(f"{mid}: '{key}' must be a non-empty string")


def _bound(x):
    return x is None or (isinstance(x, (int, float)) and not isinstance(x, bool))


def _validate_bands(mid, bands):
    if not isinstance(bands, list) or sorted(b.get("verdict") for b in bands) != \
            sorted(_BAND_VERDICTS):
        raise GuideError(f"{mid}: bands must be exactly one each of {list(_BAND_VERDICTS)}")
    for b in bands:
        if not (_bound(b.get("lo")) and _bound(b.get("hi"))):
            raise GuideError(f"{mid}: band bounds must be numbers or null")
        lo, hi = b.get("lo"), b.get("hi")
        if lo is not None and hi is not None and lo > hi:
            raise GuideError(f"{mid}: band {b['verdict']} has lo > hi")
    ordered = sorted(bands, key=lambda b: (b.get("lo") is not None, b.get("lo") or 0))
    if ordered[0].get("lo") is not None or ordered[-1].get("hi") is not None:
        raise GuideError(f"{mid}: bands must cover -inf..+inf (gap at an open end)")
    for a, b in zip(ordered, ordered[1:]):
        if a.get("hi") is None or b.get("lo") is None:
            raise GuideError(f"{mid}: bands overlap (unbounded side meets another band)")
        if a["hi"] > b["lo"]:
            raise GuideError(f"{mid}: bands {a['verdict']}/{b['verdict']} overlap")
        if a["hi"] == b["lo"]:
            a_in, b_in = bool(a.get("hi_inclusive")), bool(b.get("lo_inclusive"))
            if a_in and b_in:
                raise GuideError(f"{mid}: bands {a['verdict']}/{b['verdict']} overlap at "
                                 f"{a['hi']}")
            if not a_in and not b_in:
                raise GuideError(f"{mid}: gap at {a['hi']} between {a['verdict']}/"
                                 f"{b['verdict']}")
        else:
            raise GuideError(f"{mid}: gap between {a['verdict']} and {b['verdict']}")


def validate_guide(guide):
    metrics = guide.get("metrics") if isinstance(guide, dict) else None
    if not isinstance(metrics, dict):
        raise GuideError("guide needs a 'metrics' object")
    for mid in metrics:
        if mid not in KNOWN_METRICS:
            raise GuideError(f"unknown metric id: {mid}")
    for mid in KNOWN_METRICS:
        if mid not in metrics:
            raise GuideError(f"metric missing from guide: {mid}")
    for mid, m in metrics.items():
        want_kind = "banded" if mid in BANDED else "info" if mid in INFO else "relative"
        if m.get("kind") != want_kind:
            raise GuideError(f"{mid}: kind must be '{want_kind}'")
        for key in ("label", "meaning"):
            _check_text(mid, m, key)
        _check_text(mid, m, "action")
        _check_text(mid, m, "note", required=False)
        if want_kind == "info":
            if m.get("direction") != "none":
                raise GuideError(f"{mid}: direction must be 'none'")
            continue
        if m.get("direction") not in _DIRECTIONS:
            raise GuideError(f"{mid}: direction must be one of {list(_DIRECTIONS)}")
        if want_kind == "banded":
            _validate_bands(mid, m.get("bands"))
        else:
            tol = m.get("tolerance", 0.10)
            if not isinstance(tol, (int, float)) or tol < 0:
                raise GuideError(f"{mid}: tolerance must be a non-negative number")


# --- classification ----------------------------------------------------------

def _entry(guide, metric_id):
    try:
        return guide["metrics"][metric_id]
    except (KeyError, TypeError):
        raise GuideError(f"unknown metric id: {metric_id}") from None


def _result(m, verdict):
    return {
        "verdict": verdict, "direction": m["direction"], "label": m["label"],
        "note": m.get("note", ""), "meaning": m["meaning"],
        "action": m.get("action", "") if verdict in ("watch", "poor") else "",
    }


def _in_band(b, value):
    lo, hi = b.get("lo"), b.get("hi")
    if lo is not None and (value < lo or (value == lo and not b.get("lo_inclusive"))):
        return False
    if hi is not None and (value > hi or (value == hi and not b.get("hi_inclusive"))):
        return False
    return True


def classify(guide, metric_id, value):
    m = _entry(guide, metric_id)
    if m["kind"] == "info":
        return _result(m, "info")
    if m["kind"] == "relative":
        raise GuideError(f"{metric_id} is relative; use classify_relative")
    if value is None:
        return _result(m, "no_data")
    for b in m["bands"]:
        if _in_band(b, value):
            return _result(m, b["verdict"])
    return _result(m, "no_data")          # unreachable for a validated guide


def compare_to_baseline(current, baseline, direction, tolerance=0.10):
    """better / same / worse vs `baseline`; within +-`tolerance` (inclusive) of it
    is "same". `change` is the relative change (current - baseline) / baseline,
    None when the baseline is 0 or a side is missing."""
    if current is None or baseline is None:
        return {"result": "not_enough_data", "change": None}
    if baseline == 0:
        if current == 0:
            return {"result": "same", "change": None}
        rose = current > 0
        good = rose if direction == "higher_is_better" else not rose
        return {"result": "better" if good else "worse", "change": None}
    change = (current - baseline) / abs(baseline)
    if abs(change) <= tolerance + _EPS:
        return {"result": "same", "change": change}
    rose = change > 0
    good = rose if direction == "higher_is_better" else not rose
    return {"result": "better" if good else "worse", "change": change}


def classify_relative(guide, metric_id, current, baseline):
    m = _entry(guide, metric_id)
    cmp_ = compare_to_baseline(current, baseline, m["direction"], m.get("tolerance", 0.10))
    out = _result(m, _RELATIVE_VERDICT[cmp_["result"]])
    out.update(comparison=cmp_["result"], current=current, baseline=baseline,
               change=cmp_["change"])
    return out
