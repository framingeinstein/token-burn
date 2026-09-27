"""Team upload: send each finalized day's validated usage records to the
factory's house-auth upload door (spec Amendment 2026-09-27 A-1, A-3;
controller ruling R11).

PROVISIONAL -- synkhos/factory#171 (the "E1" door) is not built and has no
published wire contract yet. Everything below (the request shape, the auth
header, the `{error, message, code}` refusal body) is this repo's best
guess, built against an injectable HTTP `transport` and a pluggable
`credential_provider` so the real adapter can be swapped in for free once
#171 ships its actual contract. See synkhos/factory#171.

- **Config.** No team configured (no door URL + tenant) sends nothing and
  changes nothing. Config comes from env vars (`TOKEN_BURN_TEAM_DOOR_URL`,
  `TOKEN_BURN_TEAM_TENANT`) or else a gitignored local JSON file -- the same
  convention as `sync-factory.sh`'s `.env.factory` and `cursor_usage.py`'s
  token file.
- **Auth.** The door authenticates the caller as a house member from the
  credential (provisionally `gh auth token`, via `github_client.github_token`
  -- the only house-identified credential this repo has today). The
  uploader's identity never rides in the payload itself.
- **Request (provisional).** `POST <door_url>` with JSON
  `{"tenant": <tenant>, "day": "YYYY-MM-DD", "records": [...]}`. A 2xx
  response is acceptance. A non-2xx response with a JSON
  `{error, message, code}` body (HOUSE-29) is a refusal. A transport-level
  failure or timeout is "door unreachable" -- never raised out of this
  module; it's recorded and the caller (snapshot.py's daily run) moves on.
- **Validation.** Every record is re-validated against
  `schemas/usage-record.schema.json` (T1's validator) before sending; a day
  with any invalid record is refused locally -- the door would refuse the
  whole batch anyway -- and reported, never sent (spec §8).
- **Only this machine's own records.** A day file also holds `factory:*`
  records (from the local factory-transcript mirror) and could hold another
  human's; the factory reports its own records (Amendment A-2 / spec §9), so
  only records whose `actor` is this machine's human actor (`actor=`) are
  sent (final review I7). No actor configured sends nothing.
- **Idempotence.** A small local upload-state file
  (`~/.token-burn/team-upload-state.json` by default) records, per day,
  whether it was accepted and a content hash of what was sent. A day
  already `sent` with an unchanged hash is skipped. A `refused`,
  `unreachable`, or `invalid` day is retried on the next run regardless of
  its hash. A day whose content changed since it was last sent (a late
  `--refinalize` appending a newer line, spec §4.3) is re-sent even though
  it was previously accepted.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request
from urllib.request import urlopen as _urlopen

from github_client import github_token
from usage_records import SchemaError, read_usage_records, validate_usage_record

DEFAULT_STATE_PATH = Path("~/.token-burn/team-upload-state.json").expanduser()
DEFAULT_CONFIG_PATH = Path("~/.token-burn/team.json").expanduser()
# Provisional -- the console's Factory -> Efficiency view (synkhos/lattice#403).
# Configurable per deployment via TOKEN_BURN_CONSOLE_EFFICIENCY_URL.
DEFAULT_CONSOLE_EFFICIENCY_URL = "https://console.synkhos.ai/factory/efficiency"


class DoorUnreachable(Exception):
    """Transport-level failure talking to the upload door (DNS, connection,
    timeout) -- not a refusal response. The day is retried on the next run;
    local usage archives and the local dashboard are never touched."""


# --- config: no team configured => nothing is sent, nothing changes --------

def load_team_config(env=None, config_path=DEFAULT_CONFIG_PATH):
    """`{"door_url", "tenant"}`, or None when no team is configured.

    Env vars (`TOKEN_BURN_TEAM_DOOR_URL` / `TOKEN_BURN_TEAM_TENANT`) win;
    otherwise a gitignored local JSON file with the same two keys is tried
    (mirrors `sync-factory.sh`'s `.env.factory` / `cursor_usage.py`'s token
    file). Both fields are required -- a partial config is unconfigured, so
    a half-set-up machine never sends a batch to the wrong place."""
    env = os.environ if env is None else env
    door_url = env.get("TOKEN_BURN_TEAM_DOOR_URL")
    tenant = env.get("TOKEN_BURN_TEAM_TENANT")
    if not door_url or not tenant:
        path = Path(config_path)
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except Exception:
                data = {}
            door_url = door_url or data.get("door_url")
            tenant = tenant or data.get("tenant")
    if not door_url or not tenant:
        return None
    return {"door_url": door_url, "tenant": tenant}


def console_efficiency_url(env=None):
    env = os.environ if env is None else env
    return env.get("TOKEN_BURN_CONSOLE_EFFICIENCY_URL", DEFAULT_CONSOLE_EFFICIENCY_URL)


def default_credential_provider():
    """The signed-in house member's credential -- provisionally the
    machine's `gh auth token`, since the door authenticates the caller as a
    house member and that's the only house-identified credential this repo
    resolves today (R11: pluggable, swap in factory#171's real credential
    source once it's published)."""
    return github_token()


# --- transport: same injectable shape as github_client.default_transport ---

def default_transport(method, url, headers, body=None, urlopen=_urlopen):
    """Real HTTP transport (urllib, stdlib only). Returns `(status, headers,
    parsed_json)` for both success and HTTP-error (non-2xx) responses;
    raises `DoorUnreachable` for connection-level failures so callers can
    tell "refused" apart from "the door is down".

    Fix round 1 (review): a 2xx body that isn't valid JSON is treated as
    accepted-but-unparsed (`parsed=None`) rather than raising -- the spec's
    rule is "success = 2xx" (§ the brief), the status code is what the
    caller acts on, and the door already accepted the batch by the time its
    body is read. Only the non-2xx path's `{error,message,code}` body needs
    to parse for the caller to show a reason; that path already degraded to
    `parsed=None` on a bad body."""
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            try:
                parsed = json.loads(raw) if raw else None
            except Exception:
                parsed = None
            return resp.status, hdrs, parsed
    except HTTPError as e:
        hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = None
        return e.code, hdrs, parsed
    except URLError as e:
        raise DoorUnreachable(str(e)) from e
    except TimeoutError as e:
        raise DoorUnreachable(str(e)) from e


# --- upload-state: atomic read/write, same convention as outcomes.py -------

def read_upload_state(path):
    path = Path(path)
    if not path.exists():
        return {"days": {}}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"days": {}}


def write_upload_state(path, state):
    """Atomic write (tmp + rename) so a crash mid-write never corrupts the
    state file (same convention as outcomes.py's state.json)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _content_hash(records):
    """A stable hash of a day's validated records, independent of read
    order, so a `--refinalize` that actually changes the day's content
    (T1's "latest line per key wins", spec §4.3) is detected and re-sent,
    while an unchanged day is not."""
    serialized = sorted(json.dumps(r, sort_keys=True) for r in records)
    return hashlib.sha256("\n".join(serialized).encode()).hexdigest()


def _validate_day(records):
    """None if every record validates; else the first `SchemaError`'s
    message (spec §8: a batch with any invalid record is refused whole --
    the door would refuse it anyway, so this repo never even sends it)."""
    for r in records:
        try:
            validate_usage_record(r)
        except SchemaError as exc:
            return str(exc)
    return None


def _usage_days(usage_dir):
    usage_dir = Path(usage_dir)
    if not usage_dir.is_dir():
        return []
    return sorted(p.stem for p in usage_dir.glob("*.jsonl"))


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# --- the upload pass: called from snapshot.py, after local finalization ----

def upload_pending_days(usage_dir, config, *, actor, state_path=DEFAULT_STATE_PATH,
                        transport=default_transport,
                        credential_provider=default_credential_provider,
                        now=None):
    """Send every finalized usage day not yet accepted by the door.

    Returns None (no-op) when `config` is falsy: no team configured sends
    nothing and leaves everything -- including this module's own state file
    -- unchanged (spec Amendment A-1; controller ruling R11).

    Otherwise returns `{"sent": [...days], "refused": {day: reason},
    "invalid": {day: reason}, "unreachable": {day: reason}}` -- a day
    appears in at most one bucket. Only records whose `actor` equals `actor`
    (this machine's `human:<login>`, final review I7) are validated, hashed
    and sent; a day with none of them is skipped. A falsy `actor` sends
    nothing and touches no state (`skipped_reason` says why). Never raises: a failure for one day
    (including one this function didn't anticipate -- a raising
    `credential_provider`, a transport bug, ...) is recorded and the run
    moves on to the next day. `usage_dir`'s finalized archives are only ever
    read here, never written -- local finalization (T1/snapshot.py) is
    completely unaffected by anything that happens in this function.

    Fix round 1 (review): state is persisted after EVERY day, not once
    after the whole loop -- so a day already accepted earlier in the same
    run is never lost (and re-sent next run) just because a later day in
    the same run hit an unexpected error."""
    if not config:
        return None
    result = {"sent": [], "refused": {}, "invalid": {}, "unreachable": {}}
    if not actor:
        result["skipped_reason"] = "no actor login -- can't tell this machine's records apart"
        return result
    now = now or _now_iso()
    state = read_upload_state(state_path)
    days_state = state.setdefault("days", {})

    for day in _usage_days(usage_dir):
        try:
            records = [r for r in read_usage_records(usage_dir, day)
                       if r.get("actor") == actor]
            if not records:
                continue

            invalid_reason = _validate_day(records)
            if invalid_reason:
                days_state[day] = {"status": "invalid", "reason": invalid_reason,
                                   "last_attempt_at": now}
                result["invalid"][day] = invalid_reason
                continue

            content_hash = _content_hash(records)
            entry = days_state.get(day)
            if entry and entry.get("status") == "sent" and entry.get("content_hash") == content_hash:
                continue  # already accepted, unchanged: sent once (R11)

            credential = credential_provider()
            if not credential:
                reason = "no house credential available"
                days_state[day] = {"status": "unreachable", "reason": reason,
                                   "content_hash": content_hash, "last_attempt_at": now}
                result["unreachable"][day] = reason
                continue

            headers = {"Content-Type": "application/json",
                      "Authorization": f"Bearer {credential}"}
            payload = {"tenant": config["tenant"], "day": day, "records": records}
            try:
                status, _hdrs, parsed = transport("POST", config["door_url"], headers, payload)
            except DoorUnreachable as exc:
                reason = str(exc)
                days_state[day] = {"status": "unreachable", "reason": reason,
                                   "content_hash": content_hash, "last_attempt_at": now}
                result["unreachable"][day] = reason
                continue

            if 200 <= status < 300:
                days_state[day] = {"status": "sent", "content_hash": content_hash,
                                   "last_success_at": now}
                result["sent"].append(day)
            else:
                reason = parsed if isinstance(parsed, dict) else {
                    "error": "unknown", "message": f"HTTP {status}", "code": None}
                days_state[day] = {"status": "refused", "reason": reason,
                                   "content_hash": content_hash, "last_attempt_at": now}
                result["refused"][day] = reason
        except Exception as exc:
            # Anything this function didn't anticipate (a raising
            # credential_provider, a transport bug that isn't
            # DoorUnreachable, ...): record it and move on to the next day
            # rather than letting it propagate -- "unreachable" because the
            # failure is this module's or the door's, not necessarily the
            # record's, so it's retried next run rather than parked as
            # unfixably "invalid".
            reason = str(exc)
            days_state[day] = {"status": "unreachable", "reason": reason,
                               "last_attempt_at": now}
            result["unreachable"][day] = reason
        finally:
            # Persisted after every day (not once after the whole loop) so
            # a day already accepted earlier in this run is never lost --
            # and never re-sent next run -- just because a later day raised.
            write_upload_state(state_path, state)

    return result


# --- dashboard status: read-only, never uploads or calls the network -------

def team_upload_status(config, state_path=DEFAULT_STATE_PATH,
                       console_url=DEFAULT_CONSOLE_EFFICIENCY_URL):
    """The new top-level `team_upload` /api/data key. Reads the local
    upload-state file only -- the actual send happens out of band in
    snapshot.py's daily run, same convention as `outcomes.py`'s cache being
    read-only from `serve.py`. Absent/empty (`{"configured": False}`) when
    no team is configured, so the rest of the payload is unaffected."""
    if not config:
        return {"configured": False}
    state = read_upload_state(state_path).get("days", {})
    last_success_at = None
    pending, refused = [], {}
    for day, entry in state.items():
        status = entry.get("status")
        if status == "sent":
            ts = entry.get("last_success_at")
            if ts and (last_success_at is None or ts > last_success_at):
                last_success_at = ts
        elif status == "refused":
            refused[day] = entry.get("reason")
        else:  # "invalid" or "unreachable" -- attempted, not yet accepted
            pending.append(day)
    return {
        "configured": True,
        "last_success_at": last_success_at,
        "pending": sorted(pending),
        "refused": refused,
        "console_url": console_url,
    }
