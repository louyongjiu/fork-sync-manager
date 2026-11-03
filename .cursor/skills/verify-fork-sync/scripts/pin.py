#!/usr/bin/env python3
"""Record what scripts/sync.py does across a fixed set of user scenarios as
normalized text, so two versions of sync.py can be compared with a diff.

    .\\.venv\\Scripts\\python.exe .cursor\\skills\\verify-fork-sync\\scripts\\pin.py --out .verify\\pin\\before.txt
    .\\.venv\\Scripts\\python.exe .cursor\\skills\\verify-fork-sync\\scripts\\pin.py --sync-py <copy> --out .verify\\pin\\after.txt
    .\\.venv\\Scripts\\python.exe .cursor\\skills\\verify-fork-sync\\scripts\\pin.py --compare .verify\\pin\\before.txt .verify\\pin\\after.txt

Commits carry timestamps, so their shas differ on every run. Each scenario
renames them C1, C2, ... (c1, c2, ... for short shas never seen in full) in
order of first appearance. Tree shas depend only on content and stay as they
are, in upper case.
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import re
import secrets
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "harness.py"
_spec = importlib.util.spec_from_file_location("verify_fork_sync_harness", HARNESS)
HARNESS_MODULE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HARNESS_MODULE)
REPO_ROOT = HARNESS_MODULE.REPO_ROOT

LIVE = ("config", ["--dry-run", "false"])


def commit(repo: str, file: str, text: str, *extra: str) -> tuple[str, list[str]]:
    return ("commit", ["--repo", repo, "--file", file, "--text", text, *extra])


def run(label: str, *extra: str) -> tuple[str, str, list[str]]:
    return ("run", label, list(extra))


SCENARIOS: dict[str, list[tuple]] = {
    "dry-run": [
        run("baseline"),
        commit("octo/widget", "feature.txt", "upstream feature 1"),
        run("detect"),
        commit("octo/widget", "shared.txt", "upstream v2"),
        commit("alice/widget", "shared.txt", "fork edit"),
        run("conflict"),
    ],
    "own-commits-merge": [
        LIVE,
        commit("alice/widget", "fork.txt", "fork only"),
        commit("octo/widget", "upstream.txt", "upstream only"),
        run("merge"),
        run("again"),
    ],
    "mirror-fast-forward": [
        LIVE,
        commit("octo/gadget", "g.txt", "gadget 1"),
        run("ff"),
    ],
    "conflict-live": [
        LIVE,
        commit("octo/widget", "shared.txt", "upstream v2"),
        commit("alice/widget", "shared.txt", "fork edit"),
        run("conflict"),
        run("conflict-again"),
    ],
    "conflict-then-resolve": [
        LIVE,
        commit("octo/widget", "shared.txt", "upstream v2"),
        commit("alice/widget", "shared.txt", "fork edit"),
        run("conflict"),
        commit("alice/widget", "shared.txt", "upstream v2"),
        run("resolved"),
    ],
    "exclude-disable": [
        ("config", ["--exclude", "widget", "--repos-json", '[{"fork": "alice/gadget", "enabled": false}]']),
        run("exclude-disable"),
    ],
    "override-branch": [
        commit("octo/gadget", "r.txt", "release only", "--branch", "release", "--from", "develop"),
        ("config", ["--dry-run", "false", "--repos-json", '[{"fork": "alice/gadget", "upstream_branch": "release"}]']),
        run("override"),
    ],
    "owner-mismatch": [
        ("config", ["--repos-json", '[{"fork": "bob/widget"}]']),
        run("owner-mismatch"),
    ],
    "no-token": [run("no-token", "--no-token")],
    "bad-token": [run("bad-token", "--token", "not-the-token")],
    "disabled-unreachable": [
        ("config", ["--repos-json", '[{"fork": "alice/ghost", "enabled": false}]']),
        run("disabled-unreachable"),
    ],
    "unknown-keys": [
        commit("octo/gadget", "r.txt", "release only", "--branch", "release", "--from", "develop"),
        ("config", ["--settings-json", '{"dryrun": true}',
                    "--repos-json", '[{"fork": "alice/gadget", "upstream_brnach": "release"}]']),
        run("unknown-keys"),
    ],
    "string-bools": [
        ("config", ["--settings-json", '{"dry_run": "false"}',
                    "--repos-json", '[{"fork": "alice/gadget", "enabled": "false"}]']),
        commit("octo/gadget", "g.txt", "gadget 1"),
        run("string-bools"),
    ],
    "max-repos": [
        ("config", ["--max-repos", "2"]),
        run("max-repos-2"),
        ("config", ["--max-repos", "2", "--exclude", "widget"]),
        run("max-repos-2-excluding-one"),
    ],
}


class Shas:
    def __init__(self) -> None:
        self.full: dict[str, str] = {}
        self.short: dict[str, str] = {}

    def name_full(self, sha: str) -> str:
        if sha not in self.full:
            self.full[sha] = f"C{len(self.full) + len(self.short) + 1}"
        return self.full[sha]

    def name_short(self, sha: str) -> str:
        for full, token in self.full.items():
            if full.startswith(sha):
                return token
        if sha not in self.short:
            self.short[sha] = f"c{len(self.full) + len(self.short) + 1}"
        return self.short[sha]

    def normalize(self, text: str) -> str:
        text = re.sub(r"\b[0-9a-f]{40}\b", lambda m: self.name_full(m.group()), text)
        text = re.sub(r"\b[0-9a-f]{12}\b", lambda m: self.name_short(m.group()), text)
        text = re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", text)
        return re.sub(r"[A-Za-z]:\\[^\s'\"]*?fork-sync-[^\s'\"]*", "<tmp>", text)


def harness(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(HARNESS), *args], cwd=str(REPO_ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def describe_refs(run_id: str, state: dict[str, Any]) -> list[str]:
    lines = []
    scratch = HARNESS_MODULE.scratch_dir(run_id)
    for slug in sorted(state["refs"]):
        owner, name = slug.split("/", 1)
        bare = scratch / "remotes" / owner / f"{name}.git"
        for branch, sha in sorted(state["refs"][slug].items()):
            info = subprocess.run(["git", "--git-dir", str(bare), "log", "-1", "--format=%T %P", sha],
                                  capture_output=True, text=True).stdout.split()
            tree, parents = (info[0].upper(), ",".join(info[1:])) if info else ("?", "")
            lines.append(f"{slug} {branch} = {sha} tree={tree} parents={parents or '-'}")
    return lines


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def play(name: str, steps: list[tuple], sync_py: str | None) -> str:
    run_id = f"pin-{name}-{secrets.token_hex(3)}"
    shas = Shas()
    out = [f"### scenario {name}"]
    launched = harness(["launch", "--run-id", run_id])
    if "READY" not in launched.stdout:
        harness(["cleanup", "--run", run_id])
        return "\n".join(out + ["launch failed", launched.stdout, launched.stderr]) + "\n"
    try:
        for index, step in enumerate(steps, start=1):
            kind = step[0]
            if kind != "run":
                result = harness([kind, "--run", run_id, *step[1]])
                body = result.stdout if kind != "config" else ""
                out.append(f"## {index:02d} {kind} {' '.join(step[1])} -> exit {result.returncode}")
                out += [shas.normalize(line) for line in (body + result.stderr).splitlines() if line.strip()]
                continue
            label, extra = step[1], step[2]
            args = ["run", "--run", run_id, "--label", label, *extra]
            if sync_py:
                args += ["--sync-py", sync_py]
            result = harness(args)
            step_dir = Path(result.stdout.rsplit("evidence: ", 1)[1].strip()) if "evidence: " in result.stdout else None
            out.append(f"## {index:02d} run {label} {' '.join(extra)}".rstrip())
            if step_dir is None:
                out += ["harness failed", result.stdout, result.stderr]
                continue
            state = json.loads(read(step_dir / "state-after.json"))
            block = [f"exit: {read(step_dir / 'exit-code.txt').strip()}", "-- stdout"]
            block += read(step_dir / "stdout.txt").splitlines()
            block += ["-- stderr"] + read(step_dir / "stderr.txt").splitlines()
            block += ["-- summary"] + read(step_dir / "step-summary.md").splitlines()
            block += ["-- api"] + read(step_dir / "api-requests.jsonl").splitlines()
            block += ["-- refs"] + describe_refs(run_id, state)
            out += [shas.normalize(line) for line in block]
    finally:
        harness(["cleanup", "--run", run_id])
    return "\n".join(out) + "\n"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sync-py", help="pin this copy of sync.py instead of scripts/sync.py")
    parser.add_argument("--out", type=Path, help="write the transcript here")
    parser.add_argument("--only", nargs="*", choices=list(SCENARIOS))
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"),
                        help="diff two transcripts; exit 1 when they differ")
    args = parser.parse_args()

    if args.compare:
        before, after = ([line for line in p.read_text(encoding="utf-8").splitlines()
                          if not line.startswith("# pin of ")] for p in args.compare)
        diff = list(difflib.unified_diff(before, after, str(args.compare[0]), str(args.compare[1]), lineterm=""))
        print("\n".join(diff) if diff else "transcripts are identical")
        return 1 if diff else 0

    sync_py = str(Path(args.sync_py).resolve()) if args.sync_py else None
    names = args.only or list(SCENARIOS)
    with ThreadPoolExecutor(max_workers=4) as pool:
        parts = list(pool.map(lambda n: play(n, SCENARIOS[n], sync_py), names))
    source = Path(sync_py) if sync_py else REPO_ROOT / "scripts" / "sync.py"
    transcript = "".join(parts)
    header = f"# pin of {source.name} sha256 {HARNESS_MODULE.sha256_file(source)[:16]}\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(header + transcript, encoding="utf-8", newline="\n")
        print(f"wrote {args.out} ({len(names)} scenarios, {transcript.count(chr(10))} lines)")
    else:
        print(header + transcript, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
