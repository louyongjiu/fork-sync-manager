#!/usr/bin/env python3
"""Model-check the sync PR lifecycle (model/*.tla) with TLC, compare each
verdict with the expectations below, and report whether the covered code in
scripts/sync.py changed since the model was last confirmed against it.

    .\\.venv\\Scripts\\python.exe .cursor\\skills\\verify-sync-pr-lifecycle\\scripts\\check.py
    .\\.venv\\Scripts\\python.exe .cursor\\skills\\verify-sync-pr-lifecycle\\scripts\\check.py --confirm

Exit code 0: every verdict as expected and covered code unchanged.
Exit code 1: a verdict differs from its expectation, or TLC failed.
Exit code 2: every verdict as expected, but covered code changed since the
             last confirmation; read the code and the model before --confirm.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
MODEL = SKILL / "model"
REPO_ROOT = SKILL.parents[2]
COVERAGE_FILE = SKILL / "coverage.json"
EVIDENCE_BASE = REPO_ROOT / ".verify" / "formal"

TLA_VERSION = "1.7.4"
TLA_URL = f"https://github.com/tlaplus/tlaplus/releases/download/v{TLA_VERSION}/tla2tools.jar"
TLA_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"

COVERED = {"scripts/sync.py": ["GitHubApiError", "api_request", "is_ancestor", "remote_branch_exists",
                               "list_sync_prs", "ensure_pull_request", "sync_one"]}

RULES = ["TypeOK", "NoFailedRun", "DefaultBranchNeverPushed",
         "ClosedPrNotReopenedUnchanged", "UpdatedRunLeavesOpenPrWithUpstream"]

# (config, root module, rule, expected verdict, what the verdict means)
CHECKS: list[tuple[str, str, str, str, str]] = [
    *[("SyncPrLifecycle.cfg", "SyncPrLifecycle", rule, "holds", "") for rule in RULES],
    ("SameBranch.cfg", "SyncPrLifecycle", "TypeOK", "holds", ""),
    ("SameBranch.cfg", "SyncPrLifecycle", "DefaultBranchNeverPushed", "holds", ""),
    ("SameBranch.cfg", "SyncPrLifecycle", "NoFailedRun", "violated",
     "by design: sync_branch equal to the default branch fails the fork's run"),
    ("BreakReopenFallback.cfg", "SyncPrLifecycleVariants", "NoFailedRun", "violated",
     "break caught: without lines 201-211 a run fails after you merge or close a sync PR"),
    ("BreakSyncBranchCheck.cfg", "SyncPrLifecycleVariants", "DefaultBranchNeverPushed", "violated",
     "break caught: without lines 291-295 sync_branch: main pushes the default branch"),
    ("BreakClosedCheck.cfg", "SyncPrLifecycleVariants", "ClosedPrNotReopenedUnchanged", "violated",
     "break caught: without line 196 a PR you closed is reopened unchanged"),
    ("BreakReuseCheck.cfg", "SyncPrLifecycleVariants", "UpdatedRunLeavesOpenPrWithUpstream", "violated",
     "break caught: without line 344 a reused branch misses new upstream commits"),
]


def tools_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".cache")
    return Path(base) / "fork-sync-verify" / "tools"


def find_jar() -> Path:
    override = os.environ.get("TLA2TOOLS")
    if override:
        return Path(override)
    jar = tools_dir() / f"tla2tools-{TLA_VERSION}.jar"
    if jar.exists() and hashlib.sha256(jar.read_bytes()).hexdigest() == TLA_SHA256:
        return jar
    jar.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {TLA_URL}")
    with urllib.request.urlopen(TLA_URL, timeout=120) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != TLA_SHA256:
        raise SystemExit(f"tla2tools.jar checksum mismatch: {digest}")
    jar.write_bytes(data)
    return jar


def covered_fingerprints(sync_py: Path | None = None) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for rel, names in COVERED.items():
        path = sync_py if sync_py and rel == "scripts/sync.py" else REPO_ROOT / rel
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = {node.name: ast.get_source_segment(source, node) or ""
                 for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        result[rel] = {name: (hashlib.sha256(found[name].encode("utf-8")).hexdigest()
                              if name in found else "missing") for name in names}
    return result


def head_commit() -> str | None:
    out = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=str(REPO_ROOT),
                         capture_output=True, text=True)
    return out.stdout.strip() or None


def single_rule_cfg(base: str, rule: str) -> str:
    kept, skipping = [], False
    for line in base.splitlines():
        word = line.strip().split(" ")[0] if line.strip() else ""
        if word in ("INVARIANT", "INVARIANTS"):
            skipping = True
            continue
        if skipping and word in ("SPECIFICATION", "CONSTANT", "CONSTANTS", "PROPERTY", "PROPERTIES",
                                 "CHECK_DEADLOCK", "CONSTRAINT", "CONSTRAINTS", "VIEW", "SYMMETRY"):
            skipping = False
        if not skipping:
            kept.append(line)
    kept.append(f"INVARIANT {rule}")
    return "\n".join(kept) + "\n"


def action_path(output: str) -> list[str]:
    steps = []
    for match in re.finditer(r"^State \d+: <(\w+)[^>]*>", output, re.MULTILINE):
        if match.group(1) != "Initial":
            steps.append(match.group(1))
    if re.search(r"^State 1: <Initial predicate>", output, re.MULTILINE):
        steps.insert(0, "Init")
    return steps


def run_tlc(java: str, jar: Path, work: Path, out_dir: Path, check: tuple[str, str, str, str, str]) -> dict:
    cfg_name, module, rule, expected, note = check
    tag = f"{Path(cfg_name).stem}-{rule}"
    cfg = work / f"{tag}.cfg"
    cfg.write_text(single_rule_cfg((work / cfg_name).read_text(encoding="utf-8"), rule), encoding="utf-8")
    java_tmp = work / "tmp" / tag
    java_tmp.mkdir(parents=True, exist_ok=True)
    cmd = [java, "-XX:+UseParallelGC", f"-Djava.io.tmpdir={java_tmp}", "-cp", str(jar), "tlc2.TLC", "-workers", "1",
           "-metadir", str(work / "states" / tag), "-config", cfg.name, f"{module}.tla"]
    proc = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = proc.stdout + proc.stderr
    (out_dir / f"{tag}.out").write_text(output, encoding="utf-8")
    if re.search(rf"Invariant {rule} is violated", output):
        got = "violated"
    elif "Model checking completed. No error has been found." in output:
        got = "holds"
    elif "Deadlock reached" in output:
        got = "deadlock"
    else:
        got = "error"
    states = re.search(r"(\d+) distinct states found", output)
    return {"config": cfg_name, "rule": rule, "expected": expected, "got": got, "note": note,
            "states": int(states.group(1)) if states else None,
            "path": action_path(output) if got == "violated" else [], "output": str(out_dir / f"{tag}.out")}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true",
                        help="after reading the code and the model, record the current covered code as confirmed")
    parser.add_argument("--sync-py", type=Path,
                        help="compare this copy of sync.py with the confirmed covered code instead of scripts/sync.py")
    args = parser.parse_args()
    if args.confirm and args.sync_py:
        parser.error("--confirm records scripts/sync.py itself; drop --sync-py")

    java = os.environ.get("JAVA") or "java"
    jar = find_jar()
    out_dir = EVIDENCE_BASE / f"{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="sync-pr-lifecycle-"))
    try:
        for path in MODEL.iterdir():
            if path.suffix in (".tla", ".cfg"):
                shutil.copyfile(path, work / path.name)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda c: run_tlc(java, jar, work, out_dir, c), CHECKS))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    mismatches = 0
    print(f"TLC {TLA_VERSION} ({jar})")
    print(f"{'config':<25} {'rule':<36} {'expected':<9} {'got':<9} states")
    for r in results:
        ok = r["got"] == r["expected"]
        mismatches += 0 if ok else 1
        mark = "" if ok else "   <-- MISMATCH"
        print(f"{r['config']:<25} {r['rule']:<36} {r['expected']:<9} {r['got']:<9} {r['states']}{mark}")
        if r["note"]:
            print(f"{'':<25} {r['note']}")
        if r["path"]:
            print(f"{'':<25} shortest path: {' -> '.join(r['path'])}")
        if not ok:
            print(f"{'':<25} TLC output: {r['output']}")
    (out_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    current = covered_fingerprints(args.sync_py)
    if args.sync_py:
        print(f"covered code read from {args.sync_py}")
    recorded = json.loads(COVERAGE_FILE.read_text(encoding="utf-8")) if COVERAGE_FILE.exists() else {}
    changed = [f"{rel}:{name}" for rel, names in current.items() for name, digest in names.items()
               if recorded.get("fingerprints", {}).get(rel, {}).get(name) != digest]
    print(f"sync commit on record: {recorded.get('sync_commit') or 'none'}; HEAD: {head_commit() or 'none'}")
    if changed:
        print("covered code changed since the last confirmation: " + ", ".join(changed))
    else:
        print("covered code unchanged since the last confirmation")
    print(f"evidence: {out_dir}")

    if args.confirm:
        if mismatches:
            print("not confirming: fix the mismatches first")
            return 1
        COVERAGE_FILE.write_text(json.dumps({
            "sync_commit": head_commit(),
            "confirmed_at": f"{datetime.now():%Y-%m-%d %H:%M}",
            "fingerprints": current,
        }, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"recorded {COVERAGE_FILE}")
        return 0
    if mismatches:
        return 1
    return 2 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
