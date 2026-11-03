#!/usr/bin/env python3
"""Local stand-in for the GitHub REST endpoints that scripts/sync.py calls.

Repositories live as bare repos under <scratch>/remotes/<owner>/<name>.git; git
reaches them through the url.insteadOf rule in <scratch>/gitconfig. Pull request
rules follow GitHub's documented behavior:
- an open PR's head.sha follows its head branch; a closed PR keeps the sha it had
  when it closed;
- a merged PR cannot be reopened (HTTP 422);
- a closed PR cannot be reopened when its head branch was deleted, force-pushed
  or recreated after closing (HTTP 422);
- creating a PR fails (HTTP 422) when an open PR already exists for the same
  head and base, or when the head has no commits that the base lacks.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
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


def validation_failed(message: str, field: str = "base") -> ApiError:
    return ApiError(422, "Validation Failed", [
        {"resource": "PullRequest", "code": "custom", "field": field, "message": message},
    ])


class World:
    def __init__(self, scratch: Path, token: str, run_id: str, owner: str):
        self.scratch = scratch
        self.remotes = scratch / "remotes"
        self.token = token
        self.run_id = run_id
        self.owner = owner
        self.lock = threading.Lock()
        seed = json.loads((scratch / "seed.json").read_text(encoding="utf-8"))
        self.repos: dict[str, dict[str, Any]] = {r["full_name"].lower(): r for r in seed["repos"]}
        self.prs: dict[str, list[dict[str, Any]]] = {}
        self.clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.log_path = scratch / "api-requests.jsonl"
        self.port = 0

    # ---- git helpers -------------------------------------------------------

    def bare(self, slug: str) -> Path:
        owner, name = slug.split("/", 1)
        return self.remotes / owner / f"{name}.git"

    def git(self, slug: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "--git-dir", str(self.bare(slug)), *args],
            text=True, capture_output=True,
        )

    def tip(self, slug: str, branch: str) -> str | None:
        result = self.git(slug, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
        return result.stdout.strip() or None

    def is_ancestor(self, slug: str, older: str, newer: str) -> bool:
        return self.git(slug, "merge-base", "--is-ancestor", older, newer).returncode == 0

    # ---- time and rendering ------------------------------------------------

    def tick(self) -> str:
        self.clock += timedelta(minutes=1)
        return self.clock.strftime("%Y-%m-%dT%H:%M:%SZ")

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

    def head_sha(self, repo: str, pr: dict[str, Any]) -> str | None:
        if pr["state"] == "open":
            return self.tip(repo, pr["head_ref"])
        return pr["frozen_head_sha"]

    def pr_json(self, repo: str, pr: dict[str, Any]) -> dict[str, Any]:
        owner = repo.split("/", 1)[0]
        return {
            "number": pr["number"],
            "state": pr["state"],
            "merged": pr["merged"],
            "merged_at": pr["merged_at"],
            "title": pr["title"],
            "body": pr["body"],
            "created_at": pr["created_at"],
            "updated_at": pr["updated_at"],
            "closed_at": pr["closed_at"],
            "html_url": f"http://127.0.0.1:{self.port}/{repo}/pull/{pr['number']}",
            "head": {"ref": pr["head_ref"], "label": f"{owner}:{pr['head_ref']}", "sha": self.head_sha(repo, pr)},
            "base": {"ref": pr["base_ref"]},
        }

    def find_repo(self, slug: str) -> dict[str, Any]:
        record = self.repos.get(slug.lower())
        if not record:
            raise ApiError(404, "Not Found")
        return record

    def find_pr(self, repo: str, number: int) -> dict[str, Any]:
        for pr in self.prs.get(repo.lower(), []):
            if pr["number"] == number:
                return pr
        raise ApiError(404, "Not Found")

    # ---- public API --------------------------------------------------------

    def user_repos(self, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        per_page = int(query.get("per_page", ["30"])[0])
        page = int(query.get("page", ["1"])[0])
        owned = [r for r in self.repos.values() if r["full_name"].split("/", 1)[0].lower() == self.owner.lower()]
        owned.sort(key=lambda r: r["full_name"].lower())
        start = (page - 1) * per_page
        return [self.repo_json(r, with_parent=False) for r in owned[start:start + per_page]]

    def list_pulls(self, repo: str, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        self.find_repo(repo)
        state = query.get("state", ["open"])[0]
        head = query.get("head", [None])[0]
        base = query.get("base", [None])[0]
        owner = repo.split("/", 1)[0]
        result = []
        for pr in sorted(self.prs.get(repo.lower(), []), key=lambda p: p["number"], reverse=True):
            if state != "all" and pr["state"] != state:
                continue
            if head and head.lower() != f"{owner}:{pr['head_ref']}".lower():
                continue
            if base and base != pr["base_ref"]:
                continue
            result.append(self.pr_json(repo, pr))
        return result

    def create_pull(self, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.find_repo(repo)
        head = str(payload.get("head", ""))
        head_ref = head.split(":", 1)[1] if ":" in head else head
        base_ref = str(payload.get("base", ""))
        head_tip = self.tip(repo, head_ref)
        base_tip = self.tip(repo, base_ref)
        if not head_tip:
            raise validation_failed(f"head {head!r} is invalid", "head")
        if not base_tip:
            raise validation_failed(f"base {base_ref!r} is invalid", "base")
        for pr in self.prs.get(repo.lower(), []):
            if pr["state"] == "open" and pr["head_ref"] == head_ref and pr["base_ref"] == base_ref:
                raise validation_failed(f"A pull request already exists for {head}.", "head")
        if self.is_ancestor(repo, head_tip, base_tip):
            raise validation_failed(f"No commits between {base_ref} and {head_ref}", "base")
        now = self.tick()
        prs = self.prs.setdefault(repo.lower(), [])
        pr = {
            "number": len(prs) + 1,
            "state": "open",
            "merged": False,
            "merged_at": None,
            "title": payload.get("title", ""),
            "body": payload.get("body", ""),
            "head_ref": head_ref,
            "base_ref": base_ref,
            "frozen_head_sha": None,
            "created_at": now,
            "updated_at": now,
            "closed_at": None,
        }
        prs.append(pr)
        return self.pr_json(repo, pr)

    def update_pull(self, repo: str, number: int, payload: dict[str, Any]) -> dict[str, Any]:
        pr = self.find_pr(repo, number)
        wanted = payload.get("state")
        if wanted == "open" and pr["state"] == "closed":
            if pr["merged"]:
                raise validation_failed("state cannot be changed. The pull request has been merged.", "state")
            tip = self.tip(repo, pr["head_ref"])
            if not tip:
                raise validation_failed(f"state cannot be changed. The {pr['head_ref']} branch has been deleted.", "state")
            if not self.is_ancestor(repo, pr["frozen_head_sha"], tip):
                raise validation_failed(
                    f"state cannot be changed. The {pr['head_ref']} branch was force-pushed or recreated.", "state")
            pr["state"] = "open"
            pr["closed_at"] = None
            pr["frozen_head_sha"] = None
        elif wanted == "closed" and pr["state"] == "open":
            self.close(repo, pr, merged=False)
        for key in ("title", "body"):
            if key in payload:
                pr[key] = payload[key]
        pr["updated_at"] = self.tick()
        return self.pr_json(repo, pr)

    def close(self, repo: str, pr: dict[str, Any], merged: bool, head_sha: str | None = None) -> None:
        now = self.tick()
        pr["frozen_head_sha"] = head_sha or self.tip(repo, pr["head_ref"])
        pr["state"] = "closed"
        pr["closed_at"] = now
        pr["updated_at"] = now
        if merged:
            pr["merged"] = True
            pr["merged_at"] = now

    # ---- harness endpoints (never called by sync.py) -------------------------

    def admin(self, path: str, payload: dict[str, Any]) -> Any:
        if path == "/__verify/info":
            return {"run_id": self.run_id, "pid": os.getpid(), "port": self.port, "scratch": str(self.scratch)}
        if path == "/__verify/prs":
            return {repo: [self.pr_json(repo, pr) for pr in prs] for repo, prs in self.prs.items()}
        repo = str(payload.get("repo", ""))
        if path == "/__verify/pr/close":
            pr = self.find_pr(repo, int(payload["number"]))
            if pr["state"] != "open":
                raise ApiError(409, "pull request is not open")
            self.close(repo, pr, merged=False)
            return self.pr_json(repo, pr)
        if path == "/__verify/pr/mark-merged":
            pr = self.find_pr(repo, int(payload["number"]))
            if pr["state"] != "open":
                raise ApiError(409, "pull request is not open")
            self.close(repo, pr, merged=True, head_sha=str(payload["head_sha"]))
            return self.pr_json(repo, pr)
        if path == "/__verify/branch-deleted":
            branch = str(payload["branch"])
            closed = []
            for pr in self.prs.get(repo.lower(), []):
                if pr["state"] == "open" and pr["head_ref"] == branch:
                    self.close(repo, pr, merged=False, head_sha=str(payload["last_sha"]))
                    closed.append(pr["number"])
            return {"closed": closed}
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
            payload = self._payload() if method in ("POST", "PATCH") else {}
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
                    elif len(parts) == 4 and parts[0] == "repos" and parts[3] == "pulls":
                        repo = f"{parts[1]}/{parts[2]}"
                        if method == "GET":
                            body = world.list_pulls(repo, query)
                        elif method == "POST":
                            body = world.create_pull(repo, payload)
                        else:
                            raise ApiError(404, "Not Found")
                    elif method == "PATCH" and len(parts) == 5 and parts[0] == "repos" and parts[3] == "pulls":
                        body = world.update_pull(f"{parts[1]}/{parts[2]}", int(parts[4]), payload)
                    else:
                        raise ApiError(404, "Not Found")
                status = 201 if method == "POST" else 200
            except ApiError as exc:
                status, body = exc.status, {"message": exc.message, "errors": exc.errors}
            self._log(method, self.path, status, payload)
            self._send(status, body)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PATCH(self) -> None:
            self._dispatch("PATCH")

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
