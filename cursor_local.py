"""Read local Cursor composer activity without treating it as billable usage."""
import collections
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


# Half-open key range == prefix 'composerData:' (';' is the next ASCII char after ':').
# Unlike LIKE (case-insensitive, so it can't use the key index) this is an index SEARCH:
# Cursor's state.vscdb runs to many GB, and a full scan made every dashboard load ~20s.
COMPOSER_QUERY = (
    "SELECT value FROM cursorDiskKV "
    "WHERE key >= 'composerData:' AND key < 'composerData;'"
)


def _unavailable(reason):
    return {
        "status": "unavailable",
        "reason": reason,
        "days": [],
        "totals": {"sessions": 0, "context_tokens": 0},
    }


def read_cursor_activity(db_path, tz_name):
    """Return session counts and latest context occupancy from Cursor's DB."""
    path = Path(db_path).expanduser()
    if not path.exists():
        return _unavailable("Cursor state database not found")

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = con.execute(COMPOSER_QUERY).fetchall()
        con.close()
    except (OSError, sqlite3.Error) as exc:
        return _unavailable(f"Cursor state database could not be read: {exc}")

    composers = []
    subagent_ids = set()
    for (raw,) in rows:
        try:
            obj = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        composer_id = obj.get("composerId")
        created_at = obj.get("createdAt")
        if not composer_id or composer_id == "empty-state-draft" or not created_at:
            continue
        subagent_ids.update(obj.get("subagentComposerIds") or [])
        composers.append(obj)

    tz = ZoneInfo(tz_name)
    days = collections.defaultdict(lambda: {
        "sessions": 0,
        "context_tokens": 0,
        "byModel": collections.defaultdict(
            lambda: {"sessions": 0, "context_tokens": 0}
        ),
        "mainVsSub": {"main": 0, "sub": 0},
    })
    for obj in composers:
        try:
            created = datetime.fromtimestamp(
                float(obj["createdAt"]) / 1000, timezone.utc
            ).astimezone(tz)
        except (TypeError, ValueError, OSError):
            continue
        day = created.date().isoformat()
        model = (obj.get("modelConfig") or {}).get("modelName") or "(default)"
        context_tokens = int(obj.get("contextTokensUsed") or 0)
        bucket = days[day]
        bucket["sessions"] += 1
        bucket["context_tokens"] += context_tokens
        bucket["byModel"][model]["sessions"] += 1
        bucket["byModel"][model]["context_tokens"] += context_tokens
        agent_type = "sub" if obj["composerId"] in subagent_ids else "main"
        bucket["mainVsSub"][agent_type] += 1

    out = []
    for day in sorted(days):
        bucket = days[day]
        bucket["byModel"] = dict(bucket["byModel"])
        out.append({"date": day, **bucket})
    return {
        "status": "ok",
        "days": out,
        "totals": {
            "sessions": sum(d["sessions"] for d in out),
            "context_tokens": sum(d["context_tokens"] for d in out),
        },
    }
