#!/usr/bin/env python3
"""Local stand-in for the GitHub REST endpoints that scripts/sync.py calls.

Repositories live as bare repos under <scratch>/remotes/<owner>/<name>.git; git
reaches them through the url.insteadOf rule in <scratch>/gitconfig. This server
answers GET /user/repos and GET /repos/{owner}/{repo}. A missing or wrong token
gets HTTP 401. Every public request is appended to api-requests.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


class ApiError(Exception):
    def __init__(self, status: int, message: str, errors: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors or []


class World:
    def __init__(self, scratch: Path, token: str, run_id: str, owner: str):
        self.scratch = scratch
        self.token = token
        self.run_id = run_id
        self.owner = owner
        self.lock = threading.Lock()
        seed = json.loads((scratch / "seed.json").read_text(encoding="utf-8"))
        self.repos: dict[str, dict[str, Any]] = {r["full_name"].lower(): r for r in seed["repos"]}
        self.log_path = scratch / "api-requests.jsonl"
        self.port = 0

    def repo_json(self, record: dict[str, Any], with_parent: bool) -> dict[str, Any]:
        owner, name = record["full_name"].split("/", 1)
        body: dict[str, Any] = {
            "full_name": record["full_name"],
            "name": name,
            "owner": {"login": owner},
            "fork": bool(record.get("parent")),
            "archived": bool(record.get("archived")),
            "disabled": False,
            "default_branch": record["default_branch"],
            "html_url": f"http://127.0.0.1:{self.port}/{record['full_name']}",
        }
        if with_parent and record.get("parent"):
            parent = self.repos[record["parent"].lower()]
            body["parent"] = {"full_name": parent["full_name"], "default_branch": parent["default_branch"]}
        return body

    def find_repo(self, slug: str) -> dict[str, Any]:
        record = self.repos.get(slug.lower())
        if not record:
            raise ApiError(404, "Not Found")
        return record

    def user_repos(self, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        per_page = int(query.get("per_page", ["30"])[0])
        page = int(query.get("page", ["1"])[0])
        owned = [r for r in self.repos.values() if r["full_name"].split("/", 1)[0].lower() == self.owner.lower()]
        owned.sort(key=lambda r: r["full_name"].lower())
        start = (page - 1) * per_page
        return [self.repo_json(r, with_parent=False) for r in owned[start:start + per_page]]

    def admin(self, path: str, _payload: dict[str, Any]) -> Any:
        if path == "/__verify/info":
            return {"run_id": self.run_id, "pid": os.getpid(), "port": self.port, "scratch": str(self.scratch)}
        raise ApiError(404, "Not Found")


def make_handler(world: World, server_ref: dict[str, ThreadingHTTPServer]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send(self, status: int, body: Any) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _payload(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _log(self, method: str, path: str, status: int, payload: dict[str, Any]) -> None:
            entry = {"method": method, "path": path, "status": status}
            if payload:
                entry["payload"] = {k: (v if k != "body" else f"<{len(str(v))} chars>") for k, v in payload.items()}
            with world.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")

        def _dispatch(self, method: str) -> None:
            split = urlsplit(self.path)
            path = split.path
            query = parse_qs(split.query)
            payload = self._payload() if method == "POST" else {}
            if path.startswith("/__verify/"):
                if path == "/__verify/shutdown":
                    self._send(200, {"stopping": True})
                    threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()
                    return
                try:
                    with world.lock:
                        self._send(200, world.admin(path, payload))
                except ApiError as exc:
                    self._send(exc.status, {"message": exc.message, "errors": exc.errors})
                return

            status, body = 500, {"message": "unhandled"}
            try:
                if self.headers.get("Authorization") != f"Bearer {world.token}":
                    raise ApiError(401, "Bad credentials")
                parts = [p for p in path.split("/") if p]
                with world.lock:
                    if method == "GET" and parts == ["user", "repos"]:
                        body = world.user_repos(query)
                    elif method == "GET" and len(parts) == 3 and parts[0] == "repos":
                        body = world.repo_json(world.find_repo(f"{parts[1]}/{parts[2]}"), with_parent=True)
                    else:
                        raise ApiError(404, "Not Found")
                status = 200
            except ApiError as exc:
                status, body = exc.status, {"message": exc.message, "errors": exc.errors}
            self._log(method, self.path, status, payload)
            self._send(status, body)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--token", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()

    world = World(args.scratch, args.token, args.run_id, args.owner)
    server_ref: dict[str, ThreadingHTTPServer] = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(world, server_ref))
    server_ref["server"] = server
    world.port = server.server_address[1]
    info = {"run_id": args.run_id, "pid": os.getpid(), "port": world.port}
    (args.scratch / "server.json").write_text(json.dumps(info), encoding="utf-8")
    print(f"fake GitHub API for run {args.run_id} listening on 127.0.0.1:{world.port}", flush=True)
    server.serve_forever()
    print("fake GitHub API stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
