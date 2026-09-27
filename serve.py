"""Zero-dependency local server for the token-burn dashboard.

  /          -> dashboard.html (served so it fetches data live)
  /api/data  -> assemble_rollup(archive + live today) as JSON
  /healthz   -> ok

Binds --port (default 8799); if taken, increments to the next free port and
prints the actual URL.
"""
import argparse
import json
import os
import socket
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from parse import DEFAULT_FACTORY_ROOT, SessionMemo, _default_tz_name, default_roots
from prices import load_prices
from ledger import assemble_rollup, today_str
from cursor_local import read_cursor_activity
from cursor_usage import load_cursor_usage
from efficiency import build_efficiency_payload, collect_usage_records
from usage_records import resolve_actor as resolve_usage_actor
from outcomes import DEFAULT_CACHE_DIR, cache_paths, read_jsonl_latest, read_state
from outcome_metrics import actor_login, build_outcomes_payload
from team_upload import (
    DEFAULT_STATE_PATH as DEFAULT_TEAM_UPLOAD_STATE,
    console_efficiency_url,
    load_team_config,
    team_upload_status,
)

HERE = Path(__file__).parent
DEFAULT_CURSOR_DB = Path(
    "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
).expanduser()


def build_outcomes_section(cache_dir, records, today, me_login):
    """The `outcomes` top-level payload key (controller ruling R3). serve.py
    only READS the outcome cache here -- never GitHub (spec Sec5.2: fetching
    is `outcomes.py`'s job, run out-of-band by cron). No cache yet (or no
    cache configured) degrades to `{"available": False, "reason": ...}`
    rather than raising (spec Sec8), same convention as the Cursor
    sub-payloads above; the rest of the dashboard is unaffected."""
    if not cache_dir:
        return {"available": False, "reason": "no outcomes cache configured"}
    paths = cache_paths(cache_dir)
    if not paths["issues"].exists() and not paths["prs"].exists():
        return {"available": False,
                "reason": "no outcome cache yet -- run python3 outcomes.py to fetch it"}
    if not me_login:
        # I6: scope=None is the unscoped TEAM view -- never shown on this
        # personal dashboard just because no login is configured.
        return {"available": False,
                "reason": "no GitHub login configured -- pass --actor or configure gh"}
    state = read_state(paths["state"])
    issues = list(read_jsonl_latest(paths["issues"], lambda r: (r["repo"], r["number"])).values())
    prs = list(read_jsonl_latest(paths["prs"], lambda r: (r["repo"], r["number"])).values())
    payload = build_outcomes_payload(
        records, issues, prs, as_of=today, outcomes_as_of=state.get("outcomes_as_of"),
        scope=me_login)
    payload["available"] = True
    return payload


def build_team_upload_section(cfg):
    """The `team_upload` top-level payload key (T6, controller ruling R11).
    READ-ONLY here -- never uploads or calls the door (that's snapshot.py's
    job, run out of band by cron); mirrors build_outcomes_section's
    read-only-cache convention above. No team configured (nothing in `cfg`
    or in env/the local config file) degrades to `{"configured": False}`
    rather than raising -- existing payload keys are unaffected either way."""
    team_config = cfg.get("team_config")
    if team_config is None:
        team_config = load_team_config()
    return team_upload_status(
        team_config,
        state_path=cfg.get("team_upload_state") or DEFAULT_TEAM_UPLOAD_STATE,
        console_url=cfg.get("console_efficiency_url") or console_efficiency_url(),
    )


def build_payload(cfg):
    # I3: one parse of each live file, shared by the rollup's and the efficiency
    # section's passes -- and, through cfg["session_memo"] (serve.main keeps one
    # for the process), across requests, so a warm request only re-reads files
    # that changed. A fresh memo per call when none is configured.
    memo = cfg.get("session_memo") or SessionMemo()
    memo.begin()
    try:
        return _build_payload(cfg, memo)
    finally:
        memo.end()


def _build_payload(cfg, memo):
    today = cfg.get("today") or today_str(cfg["tz"])
    prices = load_prices()
    payload = assemble_rollup(
        cfg["ledger"], cfg.get("root") or default_roots(), cfg["tz"], today,
        prices=prices,
        factory_ledger_path=cfg.get("factory_ledger"),
        factory_root=cfg.get("factory_root"),
        memo=memo,
    )
    payload["cursor"] = {
        "local": read_cursor_activity(cfg["cursor_db"], cfg["tz"]),
        "billed": load_cursor_usage(cfg["cursor_ledger"]),
    }
    usage_records = collect_usage_records(
        cfg.get("usage_dir"), cfg.get("root") or default_roots(), cfg["tz"], prices,
        cfg.get("actor"), today, factory_root=cfg.get("factory_root"),
        repo_map=cfg.get("repo_map"), memo=memo,
    )
    payload["efficiency"] = build_efficiency_payload(usage_records)
    payload["outcomes"] = build_outcomes_section(
        cfg.get("outcomes_cache_dir"), usage_records, today, actor_login(cfg.get("actor")))
    payload["team_upload"] = build_team_upload_section(cfg)
    return payload


def choose_port(start, tries, is_free):
    for p in range(start, start + tries):
        if is_free(p):
            return p
    return None


def _port_free(p):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *a, cfg=None, **k):
        self.cfg = cfg
        super().__init__(*a, **k)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/data"):
            body = json.dumps(build_payload(self.cfg)).encode()
            self._send(200, body, "application/json")
        elif self.path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description="Serve the token-burn dashboard locally.")
    ap.add_argument("--root", action="append", default=None,
                    help="transcript root (repeatable); default: every Claude store, "
                         "re-discovered per request")
    ap.add_argument("--tz", default=_default_tz_name())
    ap.add_argument("--ledger", default=str(HERE / "snapshots.jsonl"))
    ap.add_argument("--cursor-db", default=str(DEFAULT_CURSOR_DB))
    ap.add_argument("--cursor-ledger", default=str(HERE / "cursor-snapshots.jsonl"))
    ap.add_argument("--factory-ledger", default=str(HERE / "factory-snapshots.jsonl"))
    ap.add_argument("--factory-root", default=str(DEFAULT_FACTORY_ROOT))
    ap.add_argument("--usage-dir", default=str(HERE / "usage"),
                    help="where finalized usage/<day>.jsonl records are read from")
    ap.add_argument("--actor", default=None,
                    help="GitHub login for human:<login> usage records; default: gh api user")
    ap.add_argument("--outcomes-cache-dir", default=str(DEFAULT_CACHE_DIR),
                    help="where the outcome cache (issues.jsonl/prs.jsonl/state.json, "
                         "written by outcomes.py) is read from -- never fetched here")
    ap.add_argument("--team-upload-state", default=str(DEFAULT_TEAM_UPLOAD_STATE),
                    help="where the team-door upload-state file (written by "
                         "snapshot.py/team_upload.py) is read from -- never uploaded here")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()

    # resolved once at startup (not per /api/data request): a single `gh api user`
    # call, same convention as snapshot.py's usage-record pass.
    actor = resolve_usage_actor({"actor": args.actor} if args.actor else None)
    if not actor:
        print("efficiency section: no actor login (pass --actor or configure gh); "
              "live-today usage records skipped, finalized archive still reads")

    # resolved once at startup, not per request -- same convention as `actor` above;
    # a local env/file read, never the network (T6, controller ruling R11).
    team_config = load_team_config()

    cfg = {
        "root": args.root,
        "tz": args.tz,
        "ledger": args.ledger,
        "cursor_db": args.cursor_db,
        "cursor_ledger": args.cursor_ledger,
        "factory_ledger": args.factory_ledger,
        "factory_root": args.factory_root,
        "usage_dir": args.usage_dir,
        "actor": actor,
        "outcomes_cache_dir": args.outcomes_cache_dir,
        "team_config": team_config,
        "team_upload_state": args.team_upload_state,
        "today": None,
        "session_memo": SessionMemo(),   # I3: per-file parse memo shared across requests
    }
    port = choose_port(args.port, 20, _port_free)
    if port is None:
        raise SystemExit(f"no free port in {args.port}..{args.port + 19}")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, cfg=cfg))
    print(f"serving token-burn at http://127.0.0.1:{port}  (ledger: {args.ledger}, tz: {args.tz})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
