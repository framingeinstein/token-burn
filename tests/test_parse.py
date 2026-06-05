import json
from pathlib import Path
from zoneinfo import ZoneInfo

from parse import (
    dedupe_messages, model_class, cost_usd, session_label,
    project_of, is_subagent, local_day, parse_session, build_rollup, scan,
)

UTC = ZoneInfo("UTC")


def _line(mid, block, out, *, inp=1, cc=0, cr=0, model="claude-opus-4-8",
          uuid="u", sidechain=False, cwd="/Users/j/Documents/projects/foo",
          ts="2026-05-20T10:00:00.000Z"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "timestamp": ts,
        "isSidechain": sidechain, "cwd": cwd,
        "message": {"id": mid, "model": model, "content": [{"type": block}],
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_creation_input_tokens": cc,
                              "cache_read_input_tokens": cr}},
    })


def _user(text, meta=False, role="user"):
    return json.dumps({"type": "user", "isMeta": meta,
                       "message": {"role": role, "content": text}})


# --- dedupe_messages ---

def test_collapses_content_block_duplicates_to_one_message():
    raw = "\n".join([
        _line("msg_A", "thinking", out=500, cc=46040, uuid="a1"),
        _line("msg_A", "text", out=500, cc=46040, uuid="a2"),
        _line("msg_A", "tool_use", out=500, cc=46040, uuid="a3"),
    ])
    recs = dedupe_messages(raw)
    assert len(recs) == 1
    assert recs[0]["out"] == 500 and recs[0]["cc"] == 46040 and recs[0]["in"] == 1


def test_distinct_messages_are_all_kept():
    raw = "\n".join([_line("msg_A", "text", out=10), _line("msg_B", "text", out=20)])
    recs = dedupe_messages(raw)
    assert {r["id"] for r in recs} == {"msg_A", "msg_B"}
    assert sum(r["out"] for r in recs) == 30


def test_keeps_max_output_when_duplicate_differs():
    raw = "\n".join([_line("msg_A", "text", out=10), _line("msg_A", "text", out=99)])
    recs = dedupe_messages(raw)
    assert len(recs) == 1 and recs[0]["out"] == 99


def test_skips_non_usage_and_malformed():
    raw = "\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
        "not json",
        _line("msg_A", "text", out=5),
    ])
    recs = dedupe_messages(raw)
    assert len(recs) == 1 and recs[0]["out"] == 5


# --- model_class + cost_usd ---

def test_model_class_maps_families():
    assert model_class("claude-opus-4-8") == "opus"
    assert model_class("claude-sonnet-4-6") == "sonnet"
    assert model_class("claude-haiku-4-5-20251001") == "haiku"
    assert model_class(None) == "sonnet"


def test_cost_usd_matches_deck_weights():
    assert round(cost_usd("claude-opus-4-8", 0, 0, 0, 1_000_000), 4) == 0.5
    assert round(cost_usd("claude-opus-4-8", 0, 1_000_000, 0, 0), 4) == 25.0
    assert round(cost_usd("claude-opus-4-8", 0, 0, 1_000_000, 0), 4) == 6.25


# --- session_label ---

def test_session_label_prefers_custom_then_ai_then_prompt():
    custom = json.dumps({"type": "summary", "customTitle": "My Title"})
    ai = json.dumps({"type": "summary", "aiTitle": "AI Title"})
    assert session_label("\n".join([custom, ai, _user("first prompt")])) == "My Title"
    assert session_label("\n".join([ai, _user("first prompt")])) == "AI Title"
    assert session_label(_user("just the first human prompt here")) == "just the first human prompt here"


def test_session_label_skips_meta_and_tool_results():
    raw = "\n".join([
        _user("<command-stdout>noise</command-stdout>", meta=True),
        _user("real first prompt"),
    ])
    assert session_label(raw) == "real first prompt"


# --- project_of + is_subagent ---

def test_project_of_uses_cwd_last_segment():
    assert project_of("/Users/j/Documents/projects/FramingEinstein") == "FramingEinstein"
    assert project_of("/Users/j/Documents/projects/acme-storage/site") == "site"
    assert project_of(None) == "(unknown)"


def test_is_subagent_by_path_or_sidechain():
    assert is_subagent("/a/b/subagents/agent-x.jsonl", any_sidechain=False) is True
    assert is_subagent("/a/b/session.jsonl", any_sidechain=True) is True
    assert is_subagent("/a/b/session.jsonl", any_sidechain=False) is False


# --- local_day ---

def test_local_day_buckets_in_target_tz():
    assert local_day("2026-05-21T03:30:00.000Z", ZoneInfo("America/Los_Angeles")) == "2026-05-20"
    assert local_day("2026-05-21T19:00:00.000Z", ZoneInfo("America/Los_Angeles")) == "2026-05-21"


def test_local_day_handles_missing_timestamp():
    assert local_day(None, ZoneInfo("America/Los_Angeles")) is None


# --- parse_session ---

def test_parse_session_aggregates_with_label_project_subagent():
    raw = "\n".join([
        json.dumps({"type": "summary", "aiTitle": "Fix the parser"}),
        _user("please fix the parser"),
        _line("msg_A", "text", out=100, cc=2000, cr=5000,
              cwd="/Users/j/Documents/projects/FramingEinstein",
              model="claude-opus-4-8", ts="2026-05-21T19:00:00.000Z"),
        _line("msg_A", "tool_use", out=100, cc=2000, cr=5000,
              cwd="/Users/j/Documents/projects/FramingEinstein",
              model="claude-opus-4-8", ts="2026-05-21T19:00:00.000Z"),
    ])
    s = parse_session(raw, "/x/session.jsonl", UTC)
    assert s["label"] == "Fix the parser"
    assert s["project"] == "FramingEinstein"
    assert s["is_subagent"] is False
    assert len(s["messages"]) == 1
    m = s["messages"][0]
    assert m["day"] == "2026-05-21" and m["mclass"] == "opus"
    assert m["out"] == 100 and m["cr"] == 5000
    assert round(m["cost"], 6) == round(cost_usd("claude-opus-4-8", 1, 100, 2000, 5000), 6)


# --- build_rollup ---

def _sess(project, sub, label, day, mclass, comps, cost):
    i, o, cc, cr = comps
    return {"label": label, "project": project, "is_subagent": sub,
            "messages": [{"day": day, "mclass": mclass, "in": i, "out": o,
                          "cc": cc, "cr": cr, "cost": cost}]}


def test_build_rollup_shape_and_sanity():
    sessions = [
        _sess("FramingEinstein", False, "main work", "2026-05-21", "opus",
              (1, 100, 2000, 5000), 0.20),
        _sess("FramingEinstein", True, "subagent work", "2026-05-21", "opus",
              (0, 50, 1000, 4000), 0.10),
    ]
    roll = build_rollup(sessions, tz_name="UTC", assistant_events=7, unique_messages=2)
    assert roll["meta"]["assistant_events"] == 7
    assert roll["meta"]["unique_messages"] == 2
    day = next(d for d in roll["days"] if d["date"] == "2026-05-21")
    bm = day["byModel"]["opus"]
    assert bm["out"] == 150 and bm["cr"] == 9000
    assert round(bm["cost_usd"], 4) == 0.30
    assert round(day["mainVsSub"]["main"]["out"]) == 100
    assert round(day["mainVsSub"]["sub"]["out"]) == 50
    assert day["topProjects"][0]["project"] == "FramingEinstein"
    assert any(s["label"] == "subagent work" for s in day["sampleSessions"])
    assert round(roll["meta"]["totals"]["cost_usd"], 4) == 0.30


# --- scan (integration) ---

def test_scan_fixture_dir_dedupes_and_flags_subagent():
    root = Path(__file__).parent / "fixtures"
    roll = scan(root, tz_name="UTC")
    assert roll["meta"]["assistant_events"] == 3
    assert roll["meta"]["unique_messages"] == 2
    day = next(d for d in roll["days"] if d["date"] == "2026-05-21")
    assert "opus" in day["byModel"] and "haiku" in day["byModel"]
    assert day["byModel"]["opus"]["out"] == 100
    assert day["mainVsSub"]["sub"]["out"] == 50
