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

from parse import _default_tz_name
from prices import load_prices
from ledger import assemble_rollup, today_str

HERE = Path(__file__).parent


def build_payload(cfg):
    today = cfg.get("today") or today_str(cfg["tz"])
    return assemble_rollup(cfg["ledger"], cfg["root"], cfg["tz"], today, prices=load_prices())


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
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--tz", default=_default_tz_name())
    ap.add_argument("--ledger", default=str(HERE / "snapshots.jsonl"))
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()

    cfg = {"root": args.root, "tz": args.tz, "ledger": args.ledger, "today": None}
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
