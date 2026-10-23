"""Correct ALREADY-FROZEN costs after a price fix -- without re-parsing raw logs.

Raw transcripts older than Claude Code's retention window are gone, so a
`snapshot.py --refinalize` over those days would LOSE data. Instead this tool
reprices from the tokens the archives already hold:

a. usage archive (usage/<day>.jsonl): every record in range gets `cost_usd`
   recomputed from its own tokens (in, out, cc, cr) at the CURRENT price of
   its exact model; a corrected line is appended only where the cost changed
   (latest line per key wins on read, as everywhere else).
b. local ledger (snapshots.jsonl): per frozen day, per model CLASS, the delta
   = new - frozen cost summed over that day's LOCAL usage records (actor
   `human:*`) is added to `byModel[class].cost_usd`; `bySource.local` gets the
   day's delta and `mainVsSub` main/sub are scaled together so their split is
   kept. Tokens are unchanged. A corrected day record is appended (latest line
   per date wins).
c. factory ledger (factory-snapshots.jsonl): same, from factory usage records
   (`factory:*`) whose actor is one of that day's `bySource` keys -- so a
   test-run folder left out of a day's freeze is left out of its correction --
   with `bySource[actor]` corrected exactly.

Idempotent and crash-safe: a ledger correction records the cumulative delta it
applied (`reprice.delta_by_class`) relative to the cost the usage records were
FROZEN with (the cost before any reprice line, which is token-identical to the
line before it), so re-running only applies what is still missing. A ledger
day whose latest line was frozen at the current `rates_version` (e.g. a
refinalize after the price change) is already right and is left alone.

  reprice.py --since 2026-09-01            # dry run (default): prints the table
  reprice.py --since 2026-09-01 --apply    # backs up each touched file, then appends

`--apply` first copies every file it will touch to `<file>.bak-reprice-<UTC stamp>`.
"""
import argparse
import copy
import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from ledger import append_day, read_ledger
from parse import cost_usd
from prices import load_prices
from usage_records import append_usage_records, record_key, usage_path

HERE = Path(__file__).parent
_EPS = 1e-9
# a stored record cost is round(sum of per-call costs, 6); recomputing it from the
# summed tokens can differ by a few 1e-6 of float/rounding noise. Only a larger
# difference is a real price change (any real one on a real record is far larger).
COST_TOLERANCE = 1e-5


def _usage_days(usage_dir, since=None, until=None):
    d = Path(usage_dir)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.jsonl")
                  if (not since or p.stem >= since) and (not until or p.stem <= until))


def _without_cost(rec):
    return {k: v for k, v in rec.items() if k != "cost_usd"}


def read_usage_with_baseline(usage_dir, day):
    """`{key: (latest_record, frozen_cost)}` for `day`. `frozen_cost` is the cost
    the record was frozen with: a line identical to the previous line for its key
    except `cost_usd` is a reprice correction and doesn't move the baseline."""
    out = {}
    path = usage_path(usage_dir, day)
    with open(path, errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            key = record_key(rec)
            prev = out.get(key)
            if prev is not None and _without_cost(prev[0]) == _without_cost(rec):
                out[key] = (rec, prev[1])          # reprice line: keep the frozen cost
            else:
                out[key] = (rec, rec.get("cost_usd", 0) or 0)
    return out


def _new_cost(prices, rec):
    return cost_usd(prices, rec.get("model"), rec["day"], rec.get("in", 0) or 0,
                    rec.get("out", 0) or 0, rec.get("cc", 0) or 0, rec.get("cr", 0) or 0)


def _day_cost(day_rec):
    return sum((c or {}).get("cost_usd", 0) or 0 for c in (day_rec.get("byModel") or {}).values())


def _correct_ledger_day(latest, target_by_class, target_by_source, prices, now):
    """A corrected copy of ledger day `latest` so that its cumulative applied
    delta equals `target_*`, or None when nothing is left to apply."""
    applied = (latest.get("reprice") or {})
    applied_cls = applied.get("delta_by_class") or {}
    applied_src = applied.get("delta_by_source") or {}
    inc_cls = {c: target_by_class.get(c, 0.0) - applied_cls.get(c, 0.0)
               for c in set(target_by_class) | set(applied_cls)}
    inc_cls = {c: v for c, v in inc_cls.items() if abs(v) > _EPS}
    if not inc_cls:
        return None
    new = copy.deepcopy(latest)
    by_model = new.get("byModel") or {}
    kept_cls = {}
    for c, inc in inc_cls.items():
        if c not in by_model:
            continue                               # class not in the frozen day: nothing to correct
        by_model[c]["cost_usd"] = (by_model[c].get("cost_usd", 0) or 0) + inc
        kept_cls[c] = inc
    if not kept_cls:
        return None
    day_inc = sum(kept_cls.values())
    by_source = new.get("bySource")
    if by_source:
        inc_src = {s: target_by_source.get(s, 0.0) - applied_src.get(s, 0.0)
                   for s in set(target_by_source) | set(applied_src)}
        for s, inc in inc_src.items():
            if s in by_source and abs(inc) > _EPS:
                by_source[s]["cost_usd"] = (by_source[s].get("cost_usd", 0) or 0) + inc
    mvs = new.get("mainVsSub") or {}
    old_total = sum((mvs.get(k) or {}).get("cost_usd", 0) or 0 for k in ("main", "sub"))
    if old_total > 0:
        scale = (old_total + day_inc) / old_total
        for k in ("main", "sub"):
            if k in mvs:
                mvs[k]["cost_usd"] = (mvs[k].get("cost_usd", 0) or 0) * scale
    elif "main" in mvs:
        mvs["main"]["cost_usd"] = (mvs["main"].get("cost_usd", 0) or 0) + day_inc
    new["reprice"] = {
        "from_rates_version": applied.get("from_rates_version") or latest.get("rates_version"),
        "delta_by_class": {c: applied_cls.get(c, 0.0) + kept_cls.get(c, 0.0)
                           for c in set(applied_cls) | set(kept_cls)},
        "delta_by_source": {s: target_by_source.get(s, 0.0) for s in target_by_source}
                           if by_source else {},
    }
    new["rates_version"] = prices.get("version")
    new["repriced_at"] = now
    return new


def plan_reprice(usage_dir, ledger_path, factory_ledger_path, prices, since=None, until=None):
    """Everything a reprice would write, computed without touching disk:
    {"usage": {day: [corrected records]}, "ledger": {date: day record},
     "factory": {date: day record}, "rows": [per-day before/after], "totals": {...}}."""
    now = datetime.now(timezone.utc).isoformat()
    ledgers = {"ledger": read_ledger(str(ledger_path)) if ledger_path else {},
               "factory": read_ledger(str(factory_ledger_path)) if factory_ledger_path else {}}
    plan = {"usage": {}, "ledger": {}, "factory": {}, "rows": []}
    for day in _usage_days(usage_dir, since, until):
        corrected = []
        usage_before = usage_after = 0.0
        # target cumulative delta vs the frozen cost, per ledger / class / source
        target = {"ledger": (defaultdict(float), defaultdict(float)),
                  "factory": (defaultdict(float), defaultdict(float))}
        fac_sources = set((ledgers["factory"].get(day) or {}).get("bySource") or {})
        for latest, frozen_cost in read_usage_with_baseline(usage_dir, day).values():
            new = _new_cost(prices, latest)
            new_rounded = round(new, 6)
            usage_before += latest.get("cost_usd", 0) or 0
            if abs(new_rounded - (latest.get("cost_usd", 0) or 0)) > COST_TOLERANCE:
                corrected.append(dict(latest, cost_usd=new_rounded))
                usage_after += new_rounded
            else:
                usage_after += latest.get("cost_usd", 0) or 0
            delta = new_rounded - frozen_cost
            if abs(delta) <= COST_TOLERANCE:
                continue                           # priced right when frozen: no ledger delta
            actor = latest.get("actor") or ""
            if actor.startswith("human:"):
                which, source = "ledger", "local"
            elif actor.startswith("factory:") and actor in fac_sources:
                which, source = "factory", actor
            else:
                continue
            by_cls, by_src = target[which]
            by_cls[latest.get("model_class")] += delta
            by_src[source] += delta
        if corrected:
            plan["usage"][day] = corrected
        row = {"day": day, "usage_before": usage_before, "usage_after": usage_after}
        for which in ("ledger", "factory"):
            latest = ledgers[which].get(day)
            row[f"{which}_before"] = row[f"{which}_after"] = _day_cost(latest) if latest else None
            if not latest:
                continue
            if latest.get("rates_version") == prices.get("version") and not latest.get("reprice"):
                continue                           # frozen at the current rates already
            fixed = _correct_ledger_day(latest, dict(target[which][0]), dict(target[which][1]),
                                        prices, now)
            if fixed is not None:
                plan[which][day] = fixed
                row[f"{which}_after"] = _day_cost(fixed)
        plan["rows"].append(row)
    plan["totals"] = {}
    for name, key in (("usage", "usage"), ("local", "ledger"), ("factory", "factory")):
        before = sum(r[f"{key}_before"] or 0 for r in plan["rows"])
        after = sum(r[f"{key}_after"] or 0 for r in plan["rows"])
        plan["totals"][name] = {"before": before, "after": after, "delta": after - before}
    return plan


def apply_plan(plan, usage_dir, ledger_path, factory_ledger_path, stamp=None):
    """Back up every file the plan touches (`<file>.bak-reprice-<stamp>`), then
    append its corrected lines. Returns {"backups": [paths], "written": [paths]}."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    targets = [usage_path(usage_dir, day) for day in sorted(plan["usage"])]
    if plan["ledger"]:
        targets.append(Path(ledger_path))
    if plan["factory"]:
        targets.append(Path(factory_ledger_path))
    backups = []
    for t in targets:                              # every backup before any write
        b = t.with_name(f"{t.name}.bak-reprice-{stamp}")
        shutil.copy2(t, b)
        backups.append(b)
    # ledgers first: if interrupted, a rerun applies only what is still missing
    for which, path in (("ledger", ledger_path), ("factory", factory_ledger_path)):
        for day in sorted(plan[which]):
            append_day(str(path), plan[which][day])
    for day in sorted(plan["usage"]):
        append_usage_records(usage_dir, day, plan["usage"][day])
    return {"backups": backups, "written": targets}


def _fmt(v):
    return "        -" if v is None else f"{v:9.2f}"


def format_plan(plan):
    lines = [f"{'day':10}  {'usage $ before':>14} {'after':>9}  {'local $ before':>14} {'after':>9}"
             f"  {'factory $ before':>16} {'after':>9}"]
    for r in plan["rows"]:
        changed = (r["day"] in plan["usage"] or r["day"] in plan["ledger"]
                   or r["day"] in plan["factory"])
        if not changed:
            continue
        lines.append(f"{r['day']:10}  {_fmt(r['usage_before']):>14} {_fmt(r['usage_after'])}"
                     f"  {_fmt(r['ledger_before']):>14} {_fmt(r['ledger_after'])}"
                     f"  {_fmt(r['factory_before']):>16} {_fmt(r['factory_after'])}")
    t = plan["totals"]
    lines.append("")
    for name in ("usage", "local", "factory"):
        lines.append(f"{name:8} total ${t[name]['before']:,.2f} -> ${t[name]['after']:,.2f} "
                     f"(delta {t[name]['delta']:+,.2f})")
    lines.append(f"days changed: usage {len(plan['usage'])}, local ledger {len(plan['ledger'])}, "
                 f"factory ledger {len(plan['factory'])}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reprice already-frozen days from archived tokens.")
    ap.add_argument("--since", default=None, help="first day to reprice, YYYY-MM-DD")
    ap.add_argument("--until", default=None, help="last day to reprice, YYYY-MM-DD")
    ap.add_argument("--usage-dir", default=str(HERE / "usage"))
    ap.add_argument("--ledger", default=str(HERE / "snapshots.jsonl"))
    ap.add_argument("--factory-ledger", default=str(HERE / "factory-snapshots.jsonl"))
    ap.add_argument("--prices", default=None, help="prices.json to reprice with (default: repo)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="print the before/after table only (default)")
    mode.add_argument("--apply", action="store_true",
                      help="back up each touched file, then append the corrected lines")
    args = ap.parse_args(argv)
    prices = load_prices(args.prices)
    plan = plan_reprice(args.usage_dir, args.ledger, args.factory_ledger, prices,
                        since=args.since, until=args.until)
    print(f"reprice at rates {prices.get('version')}"
          + (f" since {args.since}" if args.since else "")
          + (" -- APPLY" if args.apply else " -- dry run (nothing written; --apply to write)"))
    print(format_plan(plan))
    if args.apply:
        result = apply_plan(plan, args.usage_dir, args.ledger, args.factory_ledger)
        for b in result["backups"]:
            print(f"  backup: {b}")
        print(f"applied: {len(result['written'])} file(s) appended")
    return plan


if __name__ == "__main__":
    main()
