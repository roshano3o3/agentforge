"""Serve the example RAG adapter over AgentForge's HTTP adapter contract.

    python -m rag_app.http_server --host 127.0.0.1 --port 8100

POST /answer with {"input": "...", "case_key": "..."} returns the
AdapterOutput fields as JSON (see agentforge_sdk/adapter.py). Standard
library only; meant for local testing of the HTTP adapter path, not for
production use. Binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rag_app.adapter import answer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        if self.path != "/answer":
            self.send_error(404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            output = asdict(answer(str(body["input"])))
        except (KeyError, ValueError) as exc:
            self.send_error(400, f"bad request: {exc}")
            return
        payload = json.dumps(output).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # quiet by default
        pass


def make_server(host: str = "127.0.0.1", port: int = 8100) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"rag_app HTTP adapter on http://{args.host}:{args.port}/answer")
    server.serve_forever()
