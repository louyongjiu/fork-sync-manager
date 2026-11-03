#!/usr/bin/env python3
"""Replay model paths of SyncPrLifecycle against the real scripts/sync.py,
through the verify-fork-sync harness (local bare repos + local GitHub API
stand-in), and compare what the code does with what the model predicts.

    .\\.venv\\Scripts\\python.exe .cursor\\skills\\verify-sync-pr-lifecycle\\scripts\\replay.py
    .\\.venv\\Scripts\\python.exe .cursor\\skills\\verify-sync-pr-lifecycle\\scripts\\replay.py --sync-py <copy of sync.py>
    .\\.venv\\Scripts\\python.exe .cursor\\skills\\verify-sync-pr-lifecycle\\scripts\\replay.py --only close-then-upstream

Exit code 0 when every step matches the model, 1 otherwise.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import secrets
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
HARNESS = REPO_ROOT / ".cursor" / "skills" / "verify-fork-sync" / "scripts" / "harness.py"
_spec = importlib.util.spec_from_file_location("verify_fork_sync_harness", HARNESS)
HARNESS_MODULE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HARNESS_MODULE)
FORK = "alice/widget"
UPSTREAM = "octo/widget"

CREATED = r"UPDATED: pushed sync branch; created_pr \| \S+/pull/1$"
CREATED_SECOND = r"UPDATED: pushed sync branch; created_pr \| \S+/pull/2$"
REUSED = r"UPDATED: reused sync branch; updated_pr \| \S+/pull/2$"


def upstream_commit(text: str) -> tuple[str, list[str]]:
    return ("commit", ["--repo", UPSTREAM, "--file", "feature.txt", "--text", text])


# Each scenario is one path of the model, from Init, as harness commands.
# A "run" step names the model's result for the fork and, where the model
# says so, that the default branch is written or that the open PR contains
# the upstream commit the run fetched.  Every other run must leave the
# default branch unchanged.
SCENARIOS: dict[str, dict[str, Any]] = {
    "squash-merged": {
        "model": "SyncPrLifecycle.cfg, NoFailedRun after YouMergePr squash (BreakReopenFallback.cfg shortest path)",
        "steps": [
            ("config", ["--dry-run", "false"]),
            upstream_commit("w1"),
            ("run", "create", {"status": CREATED}),
            ("pr", ["merge", "--repo", FORK, "--number", "1", "--method", "squash"]),
            ("run", "after-squash", {"status": CREATED_SECOND, "pr_has_upstream": True}),
            ("run", "next-day", {"status": REUSED, "pr_has_upstream": True}),
        ],
    },
    "merge-then-upstream": {
        "model": "SyncPrLifecycle.cfg, NoFailedRun after YouMergePr merge, UpstreamAdvances",
        "steps": [
            ("config", ["--dry-run", "false"]),
            upstream_commit("w1"),
            ("run", "create", {"status": CREATED}),
            ("pr", ["merge", "--repo", FORK, "--number", "1", "--method", "merge"]),
            ("run", "after-merge", {"status": r"UP_TO_DATE: "}),
            upstream_commit("w2"),
            ("run", "upstream-again", {"status": CREATED_SECOND, "pr_has_upstream": True}),
            ("run", "next-day", {"status": REUSED, "pr_has_upstream": True}),
        ],
    },
    "close-then-upstream": {
        "model": "SyncPrLifecycle.cfg, ClosedPrNotReopenedUnchanged (skipped), NoFailedRun after YouClosePr, UpstreamAdvances",
        "steps": [
            ("config", ["--dry-run", "false"]),
            upstream_commit("w1"),
            ("run", "create", {"status": CREATED}),
            ("pr", ["close", "--repo", FORK, "--number", "1"]),
            ("run", "closed-unchanged", {"status": r"SKIPPED: PR was previously closed and sync branch has not changed"}),
            upstream_commit("w2"),
            ("run", "closed-then-upstream", {"status": CREATED_SECOND, "pr_has_upstream": True}),
        ],
    },
    "upstream-while-open": {
        "model": "SyncPrLifecycle.cfg, UpdatedRunLeavesOpenPrWithUpstream holds (rebuild and reuse)",
        "steps": [
            ("config", ["--dry-run", "false"]),
            upstream_commit("w1"),
            ("run", "create", {"status": CREATED, "pr_has_upstream": True}),
            upstream_commit("w2"),
            ("run", "rebuild", {"status": r"UPDATED: pushed sync branch; updated_pr", "pr_has_upstream": True}),
            ("run", "reuse", {"status": r"UPDATED: reused sync branch; updated_pr", "pr_has_upstream": True}),
        ],
    },
    "sync-branch-is-default": {
        "model": "SameBranch.cfg, DefaultBranchNeverPushed holds, NoFailedRun broken by design",
        "steps": [
            ("config", ["--dry-run", "false", "--sync-branch", "main"]),
            upstream_commit("w1"),
            ("run", "refused", {"status": r"FAILED: settings.sync_branch 'main' is the target branch of alice/widget"}),
        ],
    },
}


def harness(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(HARNESS), *args], cwd=str(REPO_ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def fork_line(output: str) -> str:
    for line in output.splitlines():
        if line.startswith(f"  {FORK}: "):
            return line.strip()[len(FORK) + 2:]
    return "(no status line)"


def pr_has_upstream(run_id: str, step_dir: Path) -> tuple[bool, str]:
    state = json.loads((step_dir / "state-after.json").read_text(encoding="utf-8"))
    prs = state["prs"].get(FORK, [])
    open_prs = [p for p in prs if p["state"] == "open"]
    if not open_prs:
        return False, "no open PR"
    head = open_prs[-1]["head"]["sha"]
    upstream_tip = state["refs"][UPSTREAM]["main"]
    bare = HARNESS_MODULE.scratch_dir(run_id) / "remotes" / "alice" / "widget.git"
    ok = subprocess.run(["git", "--git-dir", str(bare), "merge-base", "--is-ancestor", upstream_tip, head],
                        capture_output=True).returncode == 0
    return ok, f"open PR head {head[:12]} {'contains' if ok else 'lacks'} upstream {upstream_tip[:12]}"


def play(name: str, scenario: dict[str, Any], sync_py: str | None) -> dict[str, Any]:
    run_id = f"replay-{name}-{datetime.now():%H%M%S}-{secrets.token_hex(2)}"
    rows: list[dict[str, Any]] = []
    launched = harness(["launch", "--run-id", run_id])
    if launched.returncode != 0 or "READY" not in launched.stdout:
        return {"name": name, "run_id": run_id, "ok": False,
                "rows": [{"step": "launch", "ok": False, "got": launched.stderr.strip() or launched.stdout.strip()}]}
    try:
        for step in scenario["steps"]:
            kind = step[0]
            if kind != "run":
                result = harness(["pr", step[1][0], "--run", run_id, *step[1][1:]] if kind == "pr"
                                 else [kind, "--run", run_id, *step[1]])
                ok = result.returncode == 0
                rows.append({"step": f"{kind} {' '.join(step[1])}", "ok": ok,
                             "got": "" if ok else (result.stderr or result.stdout).strip()})
                if not ok:
                    break
                continue
            label, expect = step[1], step[2]
            args = ["run", "--run", run_id, "--label", label]
            if sync_py:
                args += ["--sync-py", sync_py]
            result = harness(args)
            got = fork_line(result.stdout)
            checks = [("status", bool(re.search(expect["status"], got)), got)]
            written = any(line.strip().startswith(f"{FORK} main (default branch):") and "DEFAULT BRANCH WRITTEN" in line
                          for line in result.stdout.splitlines())
            want_written = expect.get("default_branch_written", False)
            checks.append(("default branch", written == want_written,
                           "written" if written else "unchanged"))
            if expect.get("pr_has_upstream"):
                step_dir = Path(result.stdout.rsplit("evidence: ", 1)[1].strip())
                ok, detail = pr_has_upstream(run_id, step_dir)
                checks.append(("open PR has upstream", ok, detail))
            rows.append({"step": f"run {label}", "ok": all(c[1] for c in checks),
                         "expected": expect, "got": "; ".join(f"{c[0]}: {c[2]}" for c in checks)})
    finally:
        cleanup = harness(["cleanup", "--run", run_id])
    evidence = REPO_ROOT / ".verify" / "evidence" / run_id
    return {"name": name, "run_id": run_id, "model": scenario["model"], "ok": all(r["ok"] for r in rows),
            "rows": rows, "evidence": str(evidence), "cleanup": cleanup.stdout.strip().splitlines()[:1]}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sync-py", help="replay against this copy of sync.py instead of scripts/sync.py")
    parser.add_argument("--only", nargs="*", choices=sorted(SCENARIOS), help="replay only these scenarios")
    args = parser.parse_args()
    sync_py = str(Path(args.sync_py).resolve()) if args.sync_py else None
    names = args.only or list(SCENARIOS)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda n: play(n, SCENARIOS[n], sync_py), names))

    print(f"sync.py: {sync_py or REPO_ROOT / 'scripts' / 'sync.py'}")
    for res in results:
        print(f"\n[{'MATCH' if res['ok'] else 'MISMATCH'}] {res['name']}  ({res.get('model', '')})")
        for row in res["rows"]:
            print(f"  {'ok ' if row['ok'] else 'BAD'} {row['step']}" + (f"  ->  {row['got']}" if row.get("got") else ""))
            if not row["ok"] and row.get("expected"):
                print(f"      expected: {row['expected']}")
        print(f"  evidence: {res.get('evidence', '-')}")
    bad = [r["name"] for r in results if not r["ok"]]
    print("\nall scenarios match the model" if not bad else f"\nmismatch in: {', '.join(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
