# tests/test_factory_layout.py — factory mirror attribution for the runner layout
# (execution names old `wb-<verb>-<repo>-<id>` / new `wb-<repo>-<verb>-<id>`,
# non-repo test folders, chunked `<session>.parts/NNNNNN.jsonl` sessions).
import json

import pytest

from factory_layout import (
    DEFAULT_FACTORY_TEST_FOLDERS,
    attribute,
    is_test_folder,
    parse_execution,
    split_test_runs,
)
from parse import factory_day_records
from prices import load_prices
from usage_records import factory_usage_records, validate_usage_record

PRICES = load_prices()


@pytest.mark.parametrize("name,expected", [
    ("wb-factory-review-xlsg5", ("factory", "review")),
    ("wb-memory-review-pct94", ("memory", "review")),
    ("wb-spine-review-lrv4j", ("spine", "review")),
    ("wb-factory-implement-r7rqb", ("factory", "implement")),
    ("wb-factory-rework-v94tx", ("factory", "rework")),
    ("wb-factory-ci-fix-ab12c", ("factory", "ci-fix")),
    ("wb-lattice-conflict-fix-ab12c", ("lattice", "conflict-fix")),
    ("wb-impl-factory-ztbwc", ("factory", "impl")),
    ("wb-ci-fix-hub-ab12c", ("hub", "ci-fix")),
    ("wb-conflict-nexus-ab12c", ("nexus", "conflict")),
    ("wb-impl-terpsichore-ab12c", ("terpsichore", "impl")),
    ("exec-a", (None, None)),
    ("e", (None, None)),
    (None, (None, None)),
])
def test_parse_execution_both_shapes(name, expected):
    assert parse_execution(name) == expected


@pytest.mark.parametrize("verb,kind", [
    ("implement", "impl"), ("impl", "impl"), ("review", "review"), ("rework", "rework"),
    ("ci-fix", "ci-fix"), ("conflict", "conflict"), ("conflict-fix", "conflict"),
])
def test_kind_from_verb(verb, kind):
    name = f"wb-factory-{verb}-ab12c"
    assert attribute(("factory", "5", name, "s.jsonl"))["kind"] == kind


def test_kind_unknown_without_a_parsable_execution():
    assert attribute(("hub", "3", "exec-a", "s.jsonl"))["kind"] == "unknown"


def test_repo_from_execution_name_overrides_folder():
    a = attribute(("runner-a", "42", "wb-impl-factory-p96kr", "s.jsonl"))
    assert a["repo"] == "synkhos/factory"
    assert a["repo_name"] == "factory"


def test_repo_terpsichore_override():
    a = attribute(("terpsichore", "5", "wb-terpsichore-implement-ab12c", "s.jsonl"))
    assert a["repo"] == "synkhos/terpsichore-core"
    assert a["actor"] == "factory:terpsichore"


def test_repo_falls_back_to_known_repo_folder_only():
    assert attribute(("hub", "3", "exec-a", "s.jsonl"))["repo"] == "synkhos/hub"
    assert attribute(("runner-a", "3", "exec-a", "s.jsonl"))["repo"] is None
    assert attribute(("mystery", "3", "exec-a", "s.jsonl"))["repo"] is None


def test_repo_map_keys_count_as_known_folders():
    a = attribute(("custom", "5", "e", "s.jsonl"), repo_map={"custom": "synkhos/other"})
    assert a["repo"] == "synkhos/other"


def test_folder_issue_only_when_top_folder_is_the_repo():
    assert attribute(("factory", "1164", "wb-factory-rework-ggs5n", "s.jsonl"))["folder_issue"] == 1164
    assert attribute(("runner-a", "42", "wb-impl-factory-p96kr", "s.jsonl"))["folder_issue"] is None
    assert attribute(("test-runner", "91380", "wb-impl-factory-k75vs", "s.jsonl"))["folder_issue"] is None
    assert attribute(("hub", "0", "exec-a", "s.jsonl"))["folder_issue"] is None   # schema minimum 1


def test_actor_aggregates_runners_per_repo_and_falls_back_to_folder():
    assert attribute(("factory", "1", "wb-factory-review-xlsg5", "s.jsonl"))["actor"] == "factory:factory"
    assert attribute(("mystery", "1", "exec-a", "s.jsonl"))["actor"] == "factory:mystery"


def test_test_folders_are_flagged_and_keep_their_folder_actor():
    for top in ("runner-a", "runner-e", "signed-in-run", "test-runner"):
        a = attribute((top, "42", "wb-impl-factory-p96kr", "s.jsonl"))
        assert a["is_test"] is True
        assert a["actor"] == f"factory:{top}"   # stays identifiable once archived
    assert attribute(("factory", "42", "wb-impl-factory-p96kr", "s.jsonl"))["is_test"] is False


def test_test_folder_list_is_configurable():
    assert is_test_folder("runner-z")
    assert not is_test_folder("runner-z", ["sandbox"])
    assert is_test_folder("sandbox", ["sandbox"])
    assert attribute(("factory", "1", "e", "s.jsonl"), test_folders=["factory"])["is_test"] is True
    assert "runner-*" in DEFAULT_FACTORY_TEST_FOLDERS


def test_session_id_of_plain_and_parts_files():
    assert attribute(("factory", "1", "e", "abc.jsonl"))["session"] == "abc"
    assert attribute(("factory", "1", "e", "abc.parts", "000002.jsonl"))["session"] == "abc"


def test_split_test_runs_by_actor():
    recs = [{"actor": "factory:runner-a"}, {"actor": "factory:factory"},
            {"actor": "human:jason"}, {"actor": "factory:test-runner"}]
    prod, test = split_test_runs(recs)
    assert [r["actor"] for r in prod] == ["factory:factory", "human:jason"]
    assert [r["actor"] for r in test] == ["factory:runner-a", "factory:test-runner"]


# --- end to end over a fixture mirror ---------------------------------------

def _line(mid, day="2026-10-21", minute=0, branch=None, out=10):
    obj = {"type": "assistant", "timestamp": f"{day}T12:{minute:02d}:00Z", "cwd": "/tmp/wt-1",
           "message": {"id": mid, "model": "claude-sonnet-5",
                       "usage": {"input_tokens": 1, "output_tokens": out,
                                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}
    if branch:
        obj["gitBranch"] = branch
    return json.dumps(obj)


def _write(root, rel, lines):
    p = root.joinpath(*rel.split("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")


def _mirror(root):
    # production, new shape, chunked into parts (part 2 overlaps part 1 at m2)
    _write(root, "factory/1164/wb-factory-rework-ggs5n/sess-rw.parts/000001.jsonl",
           [_line("m1", minute=1), _line("m2", minute=2)])
    _write(root, "factory/1164/wb-factory-rework-ggs5n/sess-rw.parts/000002.jsonl",
           [_line("m2", minute=2), _line("m3", minute=3)])
    # production, old shape, in a repo folder; branch names another issue
    _write(root, "spine/58/wb-impl-spine-ab12c/sess-impl.jsonl",
           [_line("s1", branch="feat/77-thing")])
    # production review
    _write(root, "memory/61/wb-memory-review-pct94/sess-rev.jsonl", [_line("r1")])
    # test runs: runner-a and runner-c hold the SAME session (dedupe by message.id)
    for runner in ("runner-a", "runner-c"):
        _write(root, f"{runner}/42/wb-impl-factory-p96kr/sess-t.jsonl",
               [_line("t1", branch="feat/407-x"), _line("t2", minute=1, branch="feat/407-x")])
    _write(root, "test-runner/91380/wb-impl-factory-k75vs/sess-tr.jsonl",
           [_line("u1", branch="feat/380-the-knitr-runner-can")])


def test_factory_usage_records_on_the_runner_layout(tmp_path):
    _mirror(tmp_path)
    recs = factory_usage_records(tmp_path, "UTC", PRICES)
    for r in recs:
        validate_usage_record(r)
    by_session = {}
    for r in recs:
        by_session.setdefault(r["session"], []).append(r)
    # parts of one session -> ONE record (no key collision), overlap counted once
    [rw] = by_session["sess-rw"]
    assert rw["calls"] == 3 and rw["kind"] == "rework"
    assert rw["actor"] == "factory:factory" and rw["repo"] == "synkhos/factory"
    assert rw["issue"] == 1164
    assert rw["ctx_buckets"] == {"1": {"calls": 1, "ctx": 1}, "2-5": {"calls": 2, "ctx": 2}}
    [impl] = by_session["sess-impl"]
    assert impl["kind"] == "impl" and impl["issue"] == 77 and impl["repo"] == "synkhos/spine"
    assert impl["branch"] is None
    [rev] = by_session["sess-rev"]
    assert rev["kind"] == "review" and rev["issue"] == 61
    # duplicate test session counted once, under the first runner folder
    t = by_session["sess-t"]
    assert sum(r["calls"] for r in t) == 2
    assert {r["actor"] for r in t} == {"factory:runner-a"}
    assert {r["issue"] for r in t} == {407}
    assert {r["repo"] for r in t} == {"synkhos/factory"}
    [tr] = by_session["sess-tr"]
    assert tr["issue"] == 380                 # from the branch, never 91380


def test_factory_usage_records_live_window_reads_every_part_of_a_session(tmp_path):
    import os
    _mirror(tmp_path)
    old = tmp_path / "factory/1164/wb-factory-rework-ggs5n/sess-rw.parts/000001.jsonl"
    os.utime(old, (0, 0))                      # an old chunk of a still-live session
    recs = factory_usage_records(tmp_path, "UTC", PRICES, since="2026-10-21")
    [rw] = [r for r in recs if r["session"] == "sess-rw"]
    assert rw["calls"] == 3                    # call numbering spans all parts


def test_factory_day_records_excludes_test_runs_and_uses_repo_actor(tmp_path):
    _mirror(tmp_path)
    days, meta = factory_day_records(tmp_path, "UTC", prices=PRICES)
    d = days[0]
    assert set(d["bySource"]) == {"factory:factory", "factory:spine", "factory:memory"}
    assert d["bySource"]["factory:factory"]["out"] == 30
    assert meta["unique_messages"] == 5
    assert any(s["label"].startswith("#1164") for s in d["sampleSessions"])


def test_factory_day_records_test_folders_configurable(tmp_path):
    _mirror(tmp_path)
    _, meta = factory_day_records(tmp_path, "UTC", prices=PRICES, test_folders=[])
    assert meta["unique_messages"] == 5 + 3   # t1, t2 (once), u1 now counted
