#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 parse.py "$@"
python3 - <<'PY'
import json, pathlib
tpl = open("dashboard.html").read()
data = json.load(open("data.json"))
html = tpl.replace('"__DATA_PLACEHOLDER__"', json.dumps(data))
pathlib.Path("out").mkdir(exist_ok=True)
open("out/dashboard.html", "w").write(html)
print("wrote out/dashboard.html (%.0f KB)" % (len(html)/1024))
PY
open out/dashboard.html 2>/dev/null || echo "open out/dashboard.html"
