#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 - "$@" <<'PY'
import argparse, json, os, pathlib
from parse import _default_tz_name
from prices import load_prices
from ledger import assemble_rollup, today_str

ap = argparse.ArgumentParser()
ap.add_argument("--tz", default=None)
ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
ap.add_argument("--ledger", default="snapshots.jsonl")  # cwd is the script dir (cd above); __file__ is undefined in a stdin heredoc
args, _ = ap.parse_known_args()

tz = args.tz or _default_tz_name()
data = assemble_rollup(args.ledger, args.root, tz, today_str(tz), prices=load_prices())
json.dump(data, open("data.json", "w"))

tpl = open("dashboard.html").read()
html = tpl.replace('"__DATA_PLACEHOLDER__"', json.dumps(data))
pathlib.Path("out").mkdir(exist_ok=True)
open("out/dashboard.html", "w").write(html)
print("wrote out/dashboard.html (%.0f KB), %d days, $%.0f" %
      (len(html)/1024, len(data["days"]), data["meta"]["totals"]["cost_usd"]))
PY
open out/dashboard.html 2>/dev/null || echo "open out/dashboard.html"
