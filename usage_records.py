"""Usage records: the one record type that leaves a machine (spec §2, §4.1, §4.3).

One JSON object per (day, actor, on_behalf_of, repo, branch, issue, session,
model_class, kind). No free text — every field is a number, an enum, or a
pattern-constrained identifier — so a record is safe to leave the machine
(uploaded to a team bucket, read by another consumer such as Synkhos).

- `actor` is `human:<github-login>` for local sessions (the login is injectable/
  configurable so no test ever calls the network) or `factory:<runner>` for
  factory transcripts (runner from the bucket path).
- `repo`/`branch` are resolved from the session's `cwd` via local `git` calls
  (no network) and cached per cwd; a cwd with no git remote yields `repo: null`,
  never an error. `issue` comes from a `feat/<n>-...` branch, or from the
  factory bucket path via a runner -> repo map (plan decision #5).
- `on_behalf_of`/`requester_source` (the requester slot) are always null here;
  T4 fills them in from the issue.
- `kind` is "interactive" for local sessions; factory turns are "unknown"
  until factory#109 lands (Ruling R2).
- ctx_buckets is `{bucket: {"calls": int, "ctx": int}}` (Ruling R1), bucketed by
  the call's ordinal position within the session (1, 2-5, 6-20, 21-50, 51+), so
  records can be summed across machines and context growth reported as a mean.

Records are archived the same way as the existing ledgers (append-only,
finalized days immutable, `--refinalize` appends and the latest line per
composite key wins on read) — see usage/<day>.jsonl, written by `run_usage`.

Stdlib only; the schema is validated by a small hand-rolled JSON-Schema subset
(no `jsonschema` package) against schemas/usage-record.schema.json, published
so other consumers (Synkhos) can read the contract directly.
"""
import collections
import json
import re
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from parse import _jsonl_files, _mtime_floor, _read, cost_usd, dedupe_messages, local_day, model_class

SCHEMA_PATH = Path(__file__).parent / "schemas" / "usage-record.schema.json"

_CTX_BUCKETS = (("1", 1, 1), ("2-5", 2, 5), ("6-20", 6, 20), ("21-50", 21, 50), ("51+", 51, None))

_KEY_FIELDS = ("day", "actor", "on_behalf_of", "repo", "branch", "issue", "session",
              "model_class", "kind")

_ISSUE_BRANCH_RE = re.compile(r"^feat/(\d+)-")

# Same shape as the schema's `branch` pattern (schemas/usage-record.schema.json) —
# duplicated here (not loaded from the file) so it stays a fast, dependency-free
# check on the hot build path; the schema is the source of truth and a mismatch
# would surface immediately as a validation failure in tests.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

_FACTORY_REPO_OVERRIDES = {"terpsichore": "terpsichore-core"}


def ctx_bucket_label(call_number):
    """Which context-growth bucket a 1-indexed call number within a session falls in."""
    for label, lo, hi in _CTX_BUCKETS:
        if call_number >= lo and (hi is None or call_number <= hi):
            return label
    return None  # unreachable: the last bucket has no upper bound


# --- git-derived repo / branch / issue -------------------------------------

def _owner_repo_from_url(url):
    url = url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        _, _, path = url.partition(":")           # git@github.com:owner/repo
    elif "://" in url:
        path = url.split("://", 1)[1].split("/", 1)[-1]  # https://github.com/owner/repo
    else:
        path = url
    parts = [p for p in path.split("/") if p]
    return f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else None


def resolve_repo(cwd, run=subprocess.run, cache=None):
    """`owner/repo` from `git remote get-url origin` at `cwd`, or None if there's
    no remote (never raises). Cached per cwd when `cache` (a dict) is given."""
    if not cwd:
        return None
    if cache is not None and cwd in cache:
        return cache[cwd]
    repo = None
    try:
        result = run(["git", "-C", cwd, "remote", "get-url", "origin"],
                     capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            repo = _owner_repo_from_url(result.stdout)
    except Exception:
        repo = None
    if cache is not None:
        cache[cwd] = repo
    return repo


def resolve_branch(cwd, run=subprocess.run):
    """Current branch name at `cwd`, or None (detached HEAD, no cwd, or error)."""
    if not cwd:
        return None
    try:
        result = run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                     capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch and branch != "HEAD":
                return branch
    except Exception:
        pass
    return None


def issue_from_branch(branch):
    """Issue number from a `feat/<n>-...` branch, else None."""
    if not branch:
        return None
    m = _ISSUE_BRANCH_RE.match(branch)
    return int(m.group(1)) if m else None


def sanitize_branch(branch):
    """A branch that fails the record schema's own pattern is recorded as
    `branch: null` rather than producing an invalid record (controller ruling,
    T1 round 1) — git branch names may contain characters (spaces, `@`, ...)
    the schema's identifier pattern doesn't allow."""
    if branch and _BRANCH_RE.match(branch):
        return branch
    return None


# --- actor resolution (never called in tests without an injected login) ----

def default_login(run=subprocess.run):
    """The collecting machine's GitHub login, via `gh api user` (real network call;
    always inject `get_login` in tests)."""
    try:
        result = run(["gh", "api", "user", "--jq", ".login"],
                     capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            login = result.stdout.strip()
            return login or None
    except Exception:
        pass
    return None


def resolve_actor(config=None, get_login=default_login):
    """`human:<login>` from the `actor` config key, else an injected login getter."""
    config = config or {}
    login = config.get("actor") or get_login()
    return f"human:{login}" if login else None


# --- factory runner -> repo map (plan decision #5) --------------------------

def resolve_factory_repo(runner, repo_map=None, org="synkhos"):
    """Runner -> `org/repo`: one runner per repo, `wb-impl-<repo>` -> `org/<repo>`,
    `terpsichore` -> `org/terpsichore-core`. `repo_map` overrides/extends this."""
    if repo_map and runner in repo_map:
        return repo_map[runner]
    name = runner
    if name.startswith("wb-impl-"):
        name = name[len("wb-impl-"):]
    name = _FACTORY_REPO_OVERRIDES.get(name, name)
    return f"{org}/{name}"


# --- record assembly (pure) -------------------------------------------------

def _call_entries(recs, tz, prices):
    """One entry per deduped assistant call, numbered 1..n by position within
    the session (the numbering context-growth bucketing needs)."""
    entries = []
    for i, r in enumerate(recs):
        day = local_day(r["timestamp"], tz)
        model = r["model"]
        cost = cost_usd(prices, model, day or "", r["in"], r["out"], r["cc"], r["cr"])
        entries.append({
            "call_number": i + 1, "day": day, "id": r.get("id"),
            "model": model, "model_class": model_class(model),
            "in": r["in"], "out": r["out"], "cc": r["cc"], "cr": r["cr"],
            "cost": cost, "ctx": r["in"] + r["cc"] + r["cr"],
        })
    return entries


def _build_records(entries, *, actor, repo, branch, issue, session_id, kind,
                   since=None, until=None):
    groups = collections.defaultdict(list)
    for e in entries:
        d = e["day"]
        if d is None or (since and d < since) or (until and d > until):
            continue
        groups[(d, e["model_class"])].append(e)

    records = []
    for (day, mclass) in sorted(groups):
        es = groups[(day, mclass)]
        buckets = collections.defaultdict(lambda: {"calls": 0, "ctx": 0})
        totals = {"in": 0, "out": 0, "cc": 0, "cr": 0}
        cost = 0.0
        for e in es:
            b = buckets[ctx_bucket_label(e["call_number"])]
            b["calls"] += 1
            b["ctx"] += e["ctx"]
            for k in ("in", "out", "cc", "cr"):
                totals[k] += e[k]
            cost += e["cost"]
        records.append({
            "v": 1, "day": day, "actor": actor,
            "on_behalf_of": None, "requester_source": None,
            "repo": repo, "branch": branch, "issue": issue, "session": session_id,
            "model": es[0]["model"], "model_class": mclass, "kind": kind,
            "calls": len(es),
            "in": totals["in"], "out": totals["out"], "cc": totals["cc"], "cr": totals["cr"],
            "cost_usd": round(cost, 6),
            "ctx_buckets": {k: dict(v) for k, v in buckets.items()},
        })
    return records


def local_usage_records(file_path, raw_text, tz_name, prices, actor, run=subprocess.run,
                        repo_cache=None, since=None, until=None):
    """Usage records for one local Claude Code session transcript. `kind` is
    always "interactive"; `repo`/`branch`/`issue` come from git at the
    session's cwd (spec §4.1)."""
    tz = ZoneInfo(tz_name)
    recs = dedupe_messages(raw_text)
    entries = _call_entries(recs, tz, prices)
    cwd = next((r["cwd"] for r in recs if r.get("cwd")), None)
    repo = resolve_repo(cwd, run=run, cache=repo_cache)
    branch = resolve_branch(cwd, run=run)
    issue = issue_from_branch(branch)          # from the raw branch, before sanitizing
    branch = sanitize_branch(branch)           # invalid shape -> null, never an invalid record
    session_id = Path(file_path).stem
    return _build_records(entries, actor=actor, repo=repo, branch=branch, issue=issue,
                          session_id=session_id, kind="interactive", since=since, until=until)


def factory_usage_records(root, tz_name, prices, repo_map=None, since=None, until=None):
    """Usage records for factory runner transcripts laid out
    {runner}/{issue}/{execution}/{session}.jsonl. `kind` is always "unknown"
    (Ruling R2, factory#109 not landed); `repo` from the runner map, `issue`
    from the path. A message re-uploaded under a re-run execution counts once,
    same convention as `parse.factory_day_records`.

    Files last modified before `since`'s local midnight are skipped before
    reading (same `_mtime_floor` invariant `parse.day_records`/`factory_day_records`
    use: a file that old can't hold a message dated on/after `since`) — the
    factory transcript mirror can be large, and a narrow `since` (e.g. a live
    "today" pass) would otherwise read and parse the whole tree on every call."""
    tz = ZoneInfo(tz_name)
    root = Path(root)
    if not root.is_dir():
        return []
    seen_ids = set()
    records = []
    for f in sorted(_jsonl_files(root, _mtime_floor(since, tz))):
        parts = Path(f).relative_to(root).parts
        if len(parts) < 2:
            continue
        raw = _read(f)
        if raw is None:
            continue
        runner, issue_part = parts[0], parts[1]
        issue = int(issue_part) if issue_part.isdigit() else None
        repo = resolve_factory_repo(runner, repo_map)
        entries = _call_entries(dedupe_messages(raw), tz, prices)
        fresh = []
        for e in entries:
            mid = e["id"]
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            fresh.append(e)
        session_id = Path(f).stem
        records.extend(_build_records(
            fresh, actor=f"factory:{runner}", repo=repo, branch=None, issue=issue,
            session_id=session_id, kind="unknown", since=since, until=until))
    return records


# --- schema validation (stdlib only) ----------------------------------------

class SchemaError(ValueError):
    """A usage record does not satisfy schemas/usage-record.schema.json."""


def load_schema(path=None):
    with open(path or SCHEMA_PATH) as fh:
        return json.load(fh)


_default_schema = None


def _schema():
    global _default_schema
    if _default_schema is None:
        _default_schema = load_schema()
    return _default_schema


def _resolve_ref(ref, root):
    assert ref.startswith("#/"), f"unsupported $ref: {ref!r}"
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _type_ok(value, types):
    for t in [types] if isinstance(types, str) else types:
        if t == "null" and value is None:
            return True
        if t == "string" and isinstance(value, str):
            return True
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if t == "boolean" and isinstance(value, bool):
            return True
        if t == "object" and isinstance(value, dict):
            return True
        if t == "array" and isinstance(value, list):
            return True
    return False


def _validate_node(value, schema, root, path):
    if "$ref" in schema:
        schema = _resolve_ref(schema["$ref"], root)
    if "const" in schema and value != schema["const"]:
        raise SchemaError(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "type" in schema and not _type_ok(value, schema["type"]):
        raise SchemaError(f"{path}: expected type {schema['type']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} not in enum {schema['enum']!r}")
    if "pattern" in schema and value is not None:
        if not isinstance(value, str) or not re.match(schema["pattern"], value):
            raise SchemaError(f"{path}: {value!r} does not match pattern {schema['pattern']!r}")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        raise SchemaError(f"{path}: {value!r} below minimum {schema['minimum']!r}")
    if isinstance(value, dict) and (schema.get("type") == "object" or "properties" in schema):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                raise SchemaError(f"{path}: missing required field {req!r}")
        additional = schema.get("additionalProperties", True)
        for key, val in value.items():
            if key in props:
                _validate_node(val, props[key], root, f"{path}.{key}")
            elif additional is False:
                raise SchemaError(f"{path}: unknown field {key!r}")


def validate_usage_record(record, schema=None):
    """Raise SchemaError if `record` doesn't satisfy the usage-record schema
    (round-trip contract test); returns `record` on success."""
    _validate_node(record, schema or _schema(), schema or _schema(), "record")
    return record


# --- archive: append-only, finalized days immutable, latest line wins ------

def record_key(record):
    return tuple(record.get(k) for k in _KEY_FIELDS)


def usage_path(usage_dir, day):
    return Path(usage_dir) / f"{day}.jsonl"


def append_usage_records(usage_dir, day, records):
    """Validate then append `records` for `day` as one line each. Refuses (and
    writes nothing) if any record fails validation (spec §8: whole batch refused)."""
    if not records:
        return 0
    for r in records:
        validate_usage_record(r)
    path = usage_path(usage_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return len(records)


def read_usage_day(usage_dir, day):
    """{composite_key: record} for `day`; the latest line per key wins."""
    path = usage_path(usage_dir, day)
    out = {}
    if not path.exists():
        return out
    with open(path, errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            out[record_key(rec)] = rec
    return out


def read_usage_records(usage_dir, day):
    return list(read_usage_day(usage_dir, day).values())


def run_usage(root, tz_name, usage_dir, actor, prices, today, factory_root=None,
             refinalize=False, since=None, repo_map=None, run=subprocess.run):
    """Build and append usage records for every finalized day (date < today), the
    same idempotent/--refinalize convention as snapshot.run: a day already
    present in usage/<day>.jsonl is left alone unless `refinalize`, in which
    case a fresh line is appended and the latest line wins on read.

    Each day is finalized independently: a day whose batch fails schema
    validation is refused (nothing written for that day) without blocking any
    other day in the run. Returns `(added, failed)` — `added` is the set of
    days actually (re)written; `failed` maps a refused day to its failure
    reason."""
    usage_dir = Path(usage_dir)
    pre_existing = {p.stem for p in usage_dir.glob("*.jsonl")} if usage_dir.is_dir() else set()

    by_day = collections.defaultdict(list)
    for f in _jsonl_files(root):
        raw = _read(f)
        if raw is None:
            continue
        for r in local_usage_records(f, raw, tz_name, prices, actor, run=run, since=since):
            by_day[r["day"]].append(r)
    if factory_root and Path(factory_root).is_dir():
        for r in factory_usage_records(factory_root, tz_name, prices, repo_map=repo_map,
                                       since=since):
            by_day[r["day"]].append(r)

    added, failed = set(), {}
    for day, recs in by_day.items():
        if day is None or day >= today:
            continue
        if day in pre_existing and not refinalize:
            continue
        try:
            append_usage_records(usage_dir, day, recs)
        except SchemaError as exc:
            failed[day] = str(exc)
            continue
        added.add(day)
    return added, failed
