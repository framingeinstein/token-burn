#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 - "$@" <<'PY'
import argparse, json, os, pathlib
from parse import _default_tz_name
from serve import DEFAULT_CURSOR_DB, build_payload

ap = argparse.ArgumentParser()
ap.add_argument("--tz", default=None)
ap.add_argument("--root", action="append", default=None)  # default: every Claude store
ap.add_argument("--ledger", default="snapshots.jsonl")  # cwd is the script dir (cd above); __file__ is undefined in a stdin heredoc
ap.add_argument("--cursor-db", default=str(DEFAULT_CURSOR_DB))
ap.add_argument("--cursor-ledger", default="cursor-snapshots.jsonl")
args, _ = ap.parse_known_args()

tz = args.tz or _default_tz_name()
data = build_payload({
    "ledger": args.ledger,
    "root": args.root,
    "tz": tz,
    "today": None,
    "cursor_db": args.cursor_db,
    "cursor_ledger": args.cursor_ledger,
})
json.dump(data, open("data.json", "w"))

tpl = open("dashboard.html").read()
html = tpl.replace('"__DATA_PLACEHOLDER__"', json.dumps(data))
pathlib.Path("out").mkdir(exist_ok=True)
open("out/dashboard.html", "w").write(html)
print("wrote out/dashboard.html (%.0f KB), %d days, $%.0f" %
      (len(html)/1024, len(data["days"]), data["meta"]["totals"]["cost_usd"]))
PY
open out/dashboard.html 2>/dev/null || echo "open out/dashboard.html"
