import json
from pathlib import Path
from zoneinfo import ZoneInfo

from prices import load_prices
from parse import (
    dedupe_messages, model_class, cost_usd, session_label,
    project_of, is_subagent, local_day, parse_session, build_days, day_records, scan,
)

UTC = ZoneInfo("UTC")
PRICES = load_prices()


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
    assert model_class("<synthetic>") == "other"   # was mispriced as sonnet in Phase 1
    assert model_class(None) == "other"

def test_cost_usd_matches_deck_weights():
    d = "2026-05-21"
    assert round(cost_usd(PRICES, "claude-opus-4-8", d, 0, 0, 0, 1_000_000), 4) == 0.5
    assert round(cost_usd(PRICES, "claude-opus-4-8", d, 0, 1_000_000, 0, 0), 4) == 25.0
    assert round(cost_usd(PRICES, "claude-opus-4-8", d, 0, 0, 1_000_000, 0), 4) == 6.25

def test_cost_usd_unpriced_model_is_zero():
    assert cost_usd(PRICES, "<synthetic>", "2026-05-21", 1, 1, 1, 1) == 0.0

def test_dedupe_messages_counts_occurrences():
    raw = "\n".join([
        json.dumps({"type":"assistant","uuid":"a1","timestamp":"2026-05-20T10:00:00Z",
                    "message":{"id":"msg_A","model":"claude-opus-4-8","content":[{"type":"text"}],
                               "usage":{"input_tokens":1,"output_tokens":5}}}),
        json.dumps({"type":"assistant","uuid":"a2","timestamp":"2026-05-20T10:00:00Z",
                    "message":{"id":"msg_A","model":"claude-opus-4-8","content":[{"type":"tool_use"}],
                               "usage":{"input_tokens":1,"output_tokens":5}}}),
    ])
    recs = dedupe_messages(raw)
    assert len(recs) == 1 and recs[0]["n"] == 2


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
        json.dumps({"type":"summary","aiTitle":"Fix the parser"}),
        json.dumps({"type":"user","isMeta":False,"message":{"role":"user","content":"please fix the parser"}}),
        json.dumps({"type":"assistant","uuid":"a1","timestamp":"2026-05-21T19:00:00.000Z",
                    "cwd":"/Users/j/Documents/projects/FramingEinstein",
                    "message":{"id":"msg_A","model":"claude-opus-4-8","content":[{"type":"text"}],
                               "usage":{"input_tokens":1,"output_tokens":100,
                                        "cache_creation_input_tokens":2000,"cache_read_input_tokens":5000}}}),
        json.dumps({"type":"assistant","uuid":"a2","timestamp":"2026-05-21T19:00:00.000Z",
                    "cwd":"/Users/j/Documents/projects/FramingEinstein",
                    "message":{"id":"msg_A","model":"claude-opus-4-8","content":[{"type":"tool_use"}],
                               "usage":{"input_tokens":1,"output_tokens":100,
                                        "cache_creation_input_tokens":2000,"cache_read_input_tokens":5000}}}),
    ])
    s = parse_session(raw, "/x/session.jsonl", UTC, PRICES)
    assert s["label"] == "Fix the parser" and s["project"] == "FramingEinstein"
    assert s["is_subagent"] is False and len(s["messages"]) == 1
    m = s["messages"][0]
    assert m["day"] == "2026-05-21" and m["mclass"] == "opus" and m["n"] == 2
    assert m["out"] == 100 and m["cr"] == 5000
    assert round(m["cost"], 6) == round(cost_usd(PRICES, "claude-opus-4-8", "2026-05-21", 1, 100, 2000, 5000), 6)


# --- build_days ---

def _sess(project, sub, label, day, mclass, comps, cost, n=1):
    i, o, cc, cr = comps
    return {"label": label, "project": project, "is_subagent": sub,
            "messages": [{"day": day, "model": mclass, "mclass": mclass, "in": i, "out": o,
                          "cc": cc, "cr": cr, "cost": cost, "n": n}]}


def test_build_days_shape_and_counts():
    sessions = [
        _sess("FramingEinstein", False, "main work", "2026-05-21", "opus", (1,100,2000,5000), 0.20, n=2),
        _sess("FramingEinstein", True, "subagent work", "2026-05-21", "opus", (0,50,1000,4000), 0.10, n=1),
    ]
    days = build_days(sessions)
    day = next(d for d in days if d["date"] == "2026-05-21")
    assert day["events"] == 3 and day["msgs"] == 2
    bm = day["byModel"]["opus"]
    assert bm["out"] == 150 and bm["cr"] == 9000
    assert round(bm["cost_usd"], 4) == 0.30
    assert round(day["mainVsSub"]["main"]["out"]) == 100
    assert round(day["mainVsSub"]["sub"]["out"]) == 50
    assert day["topProjects"][0]["project"] == "FramingEinstein"
    assert any(s["label"] == "subagent work" for s in day["sampleSessions"])


def test_build_days_filters_by_range():
    sessions = [
        _sess("p", False, "old", "2026-05-01", "opus", (0,1,0,0), 0.0),
        _sess("p", False, "mid", "2026-05-15", "opus", (0,1,0,0), 0.0),
        _sess("p", False, "new", "2026-05-31", "opus", (0,1,0,0), 0.0),
    ]
    dates = [d["date"] for d in build_days(sessions, since="2026-05-10", until="2026-05-20")]
    assert dates == ["2026-05-15"]


def test_day_records_fixture_dir_dedupes_and_flags_subagent():
    root = Path(__file__).parent / "fixtures"
    days, meta = day_records(root, "UTC", prices=PRICES)
    assert meta["assistant_events"] == 3 and meta["unique_messages"] == 2
    day = next(d for d in days if d["date"] == "2026-05-21")
    assert "opus" in day["byModel"] and "haiku" in day["byModel"]
    assert day["byModel"]["opus"]["out"] == 100
    assert day["mainVsSub"]["sub"]["out"] == 50
    assert day["events"] == 3 and day["msgs"] == 2


def test_scan_wrapper_still_returns_meta_and_days():
    root = Path(__file__).parent / "fixtures"
    roll = scan(root, "UTC")
    assert roll["meta"]["unique_messages"] == 2 and len(roll["days"]) >= 1


# --- dedupe_messages lower-output-second branch ---

def test_dedupe_messages_counts_occurrences_lower_second():
    raw = "\n".join([
        _line("msg_B", "text", out=99),
        _line("msg_B", "text", out=5),
    ])
    recs = dedupe_messages(raw)
    assert len(recs) == 1 and recs[0]["out"] == 99 and recs[0]["n"] == 2


def test_day_records_reports_unpriced_models(tmp_path):
    line = json.dumps({"type":"assistant","uuid":"s1","timestamp":"2026-05-21T12:00:00.000Z",
                       "cwd":"/Users/j/Documents/projects/foo",
                       "message":{"id":"msg_S","model":"<synthetic>","content":[{"type":"text"}],
                                  "usage":{"input_tokens":1,"output_tokens":2,
                                           "cache_creation_input_tokens":0,"cache_read_input_tokens":0}}})
    (tmp_path / "s.jsonl").write_text(line + "\n")
    days, meta = day_records(tmp_path, "UTC", prices=PRICES)
    assert "<synthetic>" in meta["unpriced_models"]
    # synthetic priced at $0
    assert meta["totals"]["cost_usd"] == 0.0
