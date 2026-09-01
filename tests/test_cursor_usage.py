import json

from cursor_usage import (
    aggregate_events,
    append_usage_days,
    capture_usage,
    fetch_usage_events,
    load_cursor_usage,
    token_from_env_or_file,
)


EVENTS = [
    {
        "timestamp": "1787824800000",
        "model": "claude-4.6-opus-high-thinking",
        "kind": "USAGE_EVENT_KIND_USAGE_BASED",
        "tokenUsage": {
            "inputTokens": 3,
            "outputTokens": 20,
            "cacheWriteTokens": 100,
            "cacheReadTokens": 400,
            "totalCents": 1.21,
        },
        "chargedCents": 1.54,
    },
    {
        "timestamp": "1787828400000",
        "model": "composer-2.5-fast",
        "kind": "USAGE_EVENT_KIND_INCLUDED_IN_BUSINESS",
        "tokenUsage": {
            "inputTokens": 10,
            "outputTokens": 30,
            "cacheWriteTokens": 0,
            "cacheReadTokens": 50,
            "totalCents": 0,
        },
        "chargedCents": 0,
    },
]


def test_aggregates_cursor_events_without_storing_raw_payloads():
    days = aggregate_events(EVENTS, "UTC")

    assert len(days) == 1
    day = days[0]
    assert day["date"] == "2026-08-27"
    assert day["events"] == 2
    assert day["byModel"]["claude-4.6-opus-high-thinking"] == {
        "in": 3,
        "out": 20,
        "cc": 100,
        "cr": 400,
        "cost_usd": 0.0154,
        "included_events": 0,
        "charged_events": 1,
    }
    assert day["byModel"]["composer-2.5-fast"]["included_events"] == 1


def test_cursor_usage_ledger_is_latest_wins(tmp_path):
    ledger = tmp_path / "cursor-snapshots.jsonl"
    first = aggregate_events(EVENTS[:1], "UTC")
    second = aggregate_events(EVENTS, "UTC")

    append_usage_days(ledger, first)
    append_usage_days(ledger, second)
    usage = load_cursor_usage(ledger)

    assert len(usage["days"]) == 1
    assert usage["days"][0]["events"] == 2
    assert usage["totals"] == {
        "events": 2,
        "included_events": 1,
        "charged_events": 1,
        "cost_usd": 0.0154,
    }


def test_token_prefers_environment_and_strips_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n")

    assert token_from_env_or_file({"CURSOR_SESSION_TOKEN": "from-env"}, token_file) == "from-env"
    assert token_from_env_or_file({}, token_file) == "from-file"
    assert token_from_env_or_file({}, tmp_path / "missing") is None


def test_usage_ledger_contains_rollups_not_auth_or_raw_events(tmp_path):
    ledger = tmp_path / "cursor-snapshots.jsonl"
    append_usage_days(ledger, aggregate_events(EVENTS, "UTC"))

    stored = json.loads(ledger.read_text().splitlines()[0])
    assert "usageEventsDisplay" not in stored
    assert "token" not in stored


def test_fetch_usage_events_paginates_and_sends_date_range():
    requests = []

    def post_json(url, token, body):
        requests.append((url, token, body))
        event = dict(EVENTS[body["page"] - 1])
        return {"totalUsageEventsCount": 2, "usageEventsDisplay": [event]}

    events = fetch_usage_events(
        "secret",
        "1787788800000",
        "1787875199999",
        page_size=1,
        post_json=post_json,
    )

    assert events == EVENTS
    assert [request[2]["page"] for request in requests] == [1, 2]
    assert requests[0][2]["startDate"] == "1787788800000"
    assert requests[0][2]["endDate"] == "1787875199999"


def test_fetch_usage_events_uses_full_page_as_has_more_without_total():
    requests = []

    def post_json(url, token, body):
        requests.append(body)
        if body["page"] == 1:
            return {"usageEventsDisplay": [EVENTS[0]]}
        return {"usageEventsDisplay": []}

    events = fetch_usage_events(
        "secret", "1", "2", page_size=1, post_json=post_json
    )

    assert events == EVENTS[:1]
    assert [request["page"] for request in requests] == [1, 2]


def test_capture_refetches_latest_day_and_appends_rollup(tmp_path):
    ledger = tmp_path / "cursor-snapshots.jsonl"
    append_usage_days(ledger, aggregate_events(EVENTS[:1], "UTC"))
    calls = []

    def fetcher(token, start_ms, end_ms):
        calls.append((token, start_ms, end_ms))
        return EVENTS

    captured = capture_usage(
        "secret",
        ledger,
        "UTC",
        now="2026-08-28T00:00:00+00:00",
        fetcher=fetcher,
    )

    assert captured == {"events": 2, "days": 1}
    assert calls[0][0] == "secret"
    assert calls[0][1] == "1787788800000"  # 2026-08-27 00:00 UTC
    assert load_cursor_usage(ledger)["days"][0]["events"] == 2
