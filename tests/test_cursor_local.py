import json
import sqlite3
from datetime import datetime, timezone

from cursor_local import read_cursor_activity


def _millis(iso):
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_reads_cursor_sessions_by_day_model_and_agent_type(tmp_path):
    db = tmp_path / "state.vscdb"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cursorDiskKV (key TEXT, value BLOB)")
    con.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "composerData:main",
            json.dumps({
                "composerId": "main",
                "createdAt": _millis("2026-08-27T10:00:00"),
                "contextTokensUsed": 1200,
                "modelConfig": {"modelName": "grok-4.6"},
                "subagentComposerIds": ["sub"],
            }),
        ),
    )
    con.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "composerData:sub",
            json.dumps({
                "composerId": "sub",
                "createdAt": _millis("2026-08-27T11:00:00"),
                "contextTokensUsed": 300,
                "modelConfig": {"modelName": "composer-2.5-fast"},
            }),
        ),
    )
    con.commit()
    con.close()

    activity = read_cursor_activity(db, "UTC")

    assert activity["status"] == "ok"
    assert activity["totals"] == {"sessions": 2, "context_tokens": 1500}
    day = activity["days"][0]
    assert day["date"] == "2026-08-27"
    assert day["byModel"]["grok-4.6"] == {"sessions": 1, "context_tokens": 1200}
    assert day["mainVsSub"] == {"main": 1, "sub": 1}


def test_missing_cursor_database_returns_unavailable(tmp_path):
    activity = read_cursor_activity(tmp_path / "missing.vscdb", "UTC")

    assert activity["status"] == "unavailable"
    assert activity["days"] == []


def test_composer_query_uses_key_index_not_full_scan(tmp_path):
    # Cursor's real state.vscdb is multi-GB; a full-table SCAN is what made /api/data slow.
    from cursor_local import COMPOSER_QUERY
    con = sqlite3.connect(tmp_path / "s.vscdb")
    con.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    plan = " ".join(str(r[-1]) for r in con.execute("EXPLAIN QUERY PLAN " + COMPOSER_QUERY))
    assert "SEARCH" in plan and "SCAN" not in plan


def test_only_composer_data_keys_are_read(tmp_path):
    db = tmp_path / "state.vscdb"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    row = lambda cid: json.dumps({"composerId": cid, "createdAt": _millis("2026-08-27T10:00:00")})
    for key in ("composerData:a", "composerData;x", "composerDataX", "bubbleId:a:b", "composerdata:z"):
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (key, row(key)))
    con.commit(); con.close()
    assert read_cursor_activity(db, "UTC")["totals"]["sessions"] == 1
