#!/usr/bin/env python3
"""Drive scripts/sync.py the way the Daily Upstream Sync workflow does, against
local bare repositories and a local GitHub API stand-in (fake_github.py).

Run it with the project venv interpreter:
    .\\.venv\\Scripts\\python.exe .cursor\\skills\\verify-fork-sync\\scripts\\harness.py <command> ...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
FAKE_GITHUB = HERE / "fake_github.py"
SCRATCH_BASE = Path(os.environ.get("FORK_SYNC_VERIFY_SCRATCH") or Path(tempfile.gettempdir()) / "fork-sync-verify")
EVIDENCE_BASE = REPO_ROOT / ".verify" / "evidence"
TOKEN = "verify-token-not-a-secret"
OWNER = "alice"
MANAGER_REPO = f"{OWNER}/fork-sync-manager"

SEED_REPOS: list[dict[str, Any]] = [
    {"full_name": "octo/widget", "default_branch": "main",
     "files": {"README.md": "# Widget\n", "shared.txt": "upstream v1\n"}},
    {"full_name": "octo/gadget", "default_branch": "develop", "files": {"README.md": "# Gadget\n"}},
    {"full_name": "octo/legacy", "default_branch": "main", "files": {"README.md": "# Legacy\n"}},
    {"full_name": "alice/widget", "parent": "octo/widget", "default_branch": "main"},
    {"full_name": "alice/gadget", "parent": "octo/gadget", "default_branch": "develop"},
    {"full_name": "alice/legacy", "parent": "octo/legacy", "default_branch": "main", "archived": True},
    {"full_name": "alice/notes", "default_branch": "main", "files": {"README.md": "# Notes\n"}},
]

STATUS_PREFIXES = ("MERGED:", "UP_TO_DATE:", "CONFLICT:", "SKIPPED:", "FAILED:", "DRY_RUN:", "ERROR:")


class HarnessError(RuntimeError):
    pass


# ---- run bookkeeping --------------------------------------------------------

def scratch_dir(run_id: str) -> Path:
    return SCRATCH_BASE / run_id


def load_run(run_id: str) -> dict[str, Any]:
    path = scratch_dir(run_id) / "run.json"
    if not path.exists():
        raise HarnessError(f"no active run {run_id!r} (missing {path}); start one with `launch`")
    return json.loads(path.read_text(encoding="utf-8"))


def save_run(run: dict[str, Any]) -> None:
    (Path(run["scratch"]) / "run.json").write_text(json.dumps(run, indent=2), encoding="utf-8")


def next_step_dir(run: dict[str, Any], label: str) -> Path:
    run["step"] = int(run.get("step", 0)) + 1
    save_run(run)
    path = Path(run["evidence"]) / f"{run['step']:02d}-{label}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remove_tree(path: Path) -> None:
    def force(func: Any, target: str, _exc: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=force)
        else:
            shutil.rmtree(path, onerror=force)


# ---- environment and processes ---------------------------------------------

def base_env(run: dict[str, Any]) -> dict[str, str]:
    """Environment for every git and sync.py process: the scratch gitconfig only,
    and a dead proxy so any accidental https request to the real GitHub fails."""
    dropped = {"FORK_SYNC_TOKEN", "GIT_DIR", "GIT_WORK_TREE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() not in dropped
        and not key.upper().startswith(("GIT_CONFIG", "GITHUB_"))
    }
    env.update({
        "GIT_CONFIG_GLOBAL": str(Path(run["scratch"]) / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def git(args: list[str], env: dict[str, str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None, env=env,
        text=True, encoding="utf-8", errors="replace", capture_output=True,
    )
    if check and result.returncode != 0:
        raise HarnessError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result


def http(run: dict[str, Any], method: str, path: str, payload: Any = None, token: str | None = None) -> tuple[int, Any]:
    url = f"http://127.0.0.1:{run['port']}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def admin(run: dict[str, Any], path: str, payload: Any = None) -> Any:
    status, body = http(run, "POST" if payload is not None else "GET", path, payload)
    if status != 200:
        raise HarnessError(f"{path} -> HTTP {status}: {body}")
    return body


def is_run_process(pid: int, run_id: str) -> bool:
    # Parallel runs reuse pids quickly, so a recorded pid only counts while its
    # command line still carries this run's --run-id.
    if os.name == "nt":
        query = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
        out = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                             capture_output=True, text=True).stdout
    else:
        out = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout
    tokens = out.split()
    return any(a == "--run-id" and b == run_id for a, b in zip(tokens, tokens[1:]))


def kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        os.kill(pid, 15)


# ---- seed data --------------------------------------------------------------

def bare_path(run: dict[str, Any], slug: str) -> Path:
    owner, name = slug.split("/", 1)
    return Path(run["scratch"]) / "remotes" / owner / f"{name}.git"


def seed_record(slug: str) -> dict[str, Any]:
    for record in SEED_REPOS:
        if record["full_name"].lower() == slug.lower():
            return record
    raise HarnessError(f"unknown repository {slug!r}; known: {', '.join(r['full_name'] for r in SEED_REPOS)}")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def work_dir(run: dict[str, Any], label: str) -> Path:
    path = Path(run["scratch"]) / "work" / f"{label}-{secrets.token_hex(3)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def seed_repositories(run: dict[str, Any], env: dict[str, str]) -> None:
    for record in SEED_REPOS:
        bare = bare_path(run, record["full_name"])
        bare.parent.mkdir(parents=True, exist_ok=True)
        if record.get("parent"):
            git(["clone", "--quiet", "--bare", str(bare_path(run, record["parent"])), str(bare)], env)
            continue
        branch = record["default_branch"]
        git(["init", "--quiet", "--bare", "-b", branch, str(bare)], env)
        work = work_dir(run, "seed")
        git(["init", "--quiet", "-b", branch, str(work)], env)
        for name, text in record["files"].items():
            write_text(work / name, text)
        git(["add", "-A"], env, cwd=work)
        git(["commit", "--quiet", "-m", "Initial commit"], env, cwd=work)
        git(["push", "--quiet", str(bare), f"HEAD:refs/heads/{branch}"], env, cwd=work)
        remove_tree(work)


def snapshot(run: dict[str, Any]) -> dict[str, Any]:
    env = base_env(run)
    refs: dict[str, dict[str, str]] = {}
    for record in SEED_REPOS:
        out = git(["--git-dir", str(bare_path(run, record["full_name"])), "for-each-ref",
                   "--format=%(refname:short) %(objectname)", "refs/heads"], env).stdout
        refs[record["full_name"]] = dict(line.split(" ", 1) for line in out.splitlines() if line.strip())
    return {"refs": refs, "prs": admin(run, "/__verify/prs")}


def api_log_lines(run: dict[str, Any]) -> list[str]:
    path = Path(run["scratch"]) / "api-requests.jsonl"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


# ---- commands ---------------------------------------------------------------

def cmd_launch(args: argparse.Namespace) -> int:
    run_id = args.run_id or f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    scratch = scratch_dir(run_id)
    if scratch.exists():
        raise HarnessError(f"run {run_id!r} already exists at {scratch}")
    evidence = EVIDENCE_BASE / run_id
    for path in (scratch / "remotes", scratch / "work", scratch / "root" / "scripts",
                 scratch / "root" / "config", evidence):
        path.mkdir(parents=True, exist_ok=True)
    run: dict[str, Any] = {"run_id": run_id, "scratch": str(scratch), "evidence": str(evidence),
                           "repo_root": str(REPO_ROOT), "step": 0}
    save_run(run)

    remotes_url = (scratch / "remotes").resolve().as_uri() + "/"
    write_text(scratch / "gitconfig", (
        "[user]\n\tname = verify-harness\n\temail = verify-harness@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n"
        "[core]\n\tautocrlf = false\n"
        f'[url "{remotes_url}"]\n\tinsteadOf = https://github.com/\n'
    ))
    env = base_env(run)
    seed_repositories(run, env)
    seed = [{k: v for k, v in r.items() if k != "files"} for r in SEED_REPOS]
    (scratch / "seed.json").write_text(json.dumps({"repos": seed}, indent=2), encoding="utf-8")
    shutil.copyfile(REPO_ROOT / "config" / "repos.yml", scratch / "root" / "config" / "repos.yml")

    log = (scratch / "server.log").open("ab")
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, str(FAKE_GITHUB), "--scratch", str(scratch), "--token", TOKEN,
         "--run-id", run_id, "--owner", OWNER],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
        creationflags=flags, start_new_session=(os.name != "nt"),
    )
    run["launcher_pid"] = proc.pid
    deadline = time.time() + 20
    server_json = scratch / "server.json"
    while not server_json.exists():
        if time.time() > deadline or proc.poll() is not None:
            raise HarnessError(f"fake GitHub API did not start; see {scratch / 'server.log'}")
        time.sleep(0.2)
    info = json.loads(server_json.read_text(encoding="utf-8"))
    run.update({"port": info["port"], "server_pid": info["pid"]})
    save_run(run)
    ready = admin(run, "/__verify/info")
    if ready.get("run_id") != run_id:
        raise HarnessError(f"port {run['port']} answered for another run: {ready}")

    print(f"run id:    {run_id}")
    print(f"api:       http://127.0.0.1:{run['port']} (pid {run['server_pid']})")
    print(f"scratch:   {scratch}")
    print(f"evidence:  {evidence}")
    print("repos:     " + ", ".join(r["full_name"] for r in SEED_REPOS))
    print("config:    copy of config/repos.yml (dry_run as shipped); change it with `config`")
    print("READY")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    run = load_run(args.run)
    env = base_env(run)
    failures = 0

    def report(ok: bool, text: str) -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {text}")

    try:
        info = admin(run, "/__verify/info")
        report(info.get("run_id") == run["run_id"] and info.get("pid") == run["server_pid"],
               f"fake API on port {run['port']} belongs to run {info.get('run_id')} (pid {info.get('pid')})")
    except Exception as exc:  # noqa: BLE001
        report(False, f"fake API on port {run.get('port')} not answering: {exc}")
        print("doctor: not worth driving; run `cleanup` and `launch` again")
        return 1

    status, body = http(run, "GET", "/repos/alice/widget", token=TOKEN)
    report(status == 200 and body.get("parent", {}).get("full_name") == "octo/widget",
           f"authenticated GET /repos/alice/widget -> {status}")
    status, _ = http(run, "GET", "/repos/alice/widget")
    report(status == 401, f"unauthenticated GET /repos/alice/widget -> {status} (expect 401)")

    remote = git(["ls-remote", "https://github.com/alice/widget.git", "refs/heads/main"], env, check=False)
    local = git(["--git-dir", str(bare_path(run, "alice/widget")), "rev-parse", "refs/heads/main"], env, check=False)
    report(remote.returncode == 0 and local.stdout.strip() != "" and remote.stdout.startswith(local.stdout.strip()),
           "git https://github.com/alice/widget.git resolves to the local bare repo")

    in_venv = Path(sys.prefix).resolve() == (REPO_ROOT / ".venv").resolve()
    report(in_venv, f"interpreter is the project venv: {sys.executable}")
    report(env["HTTPS_PROXY"].endswith(":9"), "real https traffic is routed to a closed local port")

    current = sha256_file(REPO_ROOT / "scripts" / "sync.py")
    last = run.get("last_sync_py_sha256")
    print(f"INFO scripts/sync.py sha256 {current[:16]}; last run used {last[:16] if last else '(no run yet)'}")
    print("doctor: healthy" if failures == 0 else f"doctor: {failures} check(s) failed")
    return 0 if failures == 0 else 1


def cmd_config(args: argparse.Namespace) -> int:
    run = load_run(args.run)
    path = Path(run["scratch"]) / "root" / "config" / "repos.yml"
    if args.reset:
        shutil.copyfile(REPO_ROOT / "config" / "repos.yml", path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = data.setdefault("settings", {}) or {}
    data["settings"] = settings
    if args.dry_run is not None:
        settings["dry_run"] = args.dry_run == "true"
    if args.discover is not None:
        settings["discover_forks"] = args.discover == "true"
    if args.owner is not None:
        settings["owner"] = args.owner
    if args.max_repos is not None:
        settings["max_repos"] = args.max_repos
    if args.exclude is not None:
        settings["exclude"] = [x for x in args.exclude if x]
    if args.settings_json is not None:
        settings.update(json.loads(args.settings_json))
    if args.repos_json is not None:
        data["repos"] = json.loads(args.repos_json)
    write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    run = load_run(args.run)
    env = base_env(run)
    record = seed_record(args.repo)
    branch = args.branch or record["default_branch"]
    work = work_dir(run, "commit")
    git(["clone", "--quiet", "--branch", args.from_branch or branch,
         str(bare_path(run, record["full_name"])), str(work)], env)
    if args.from_branch:
        git(["checkout", "--quiet", "-b", branch], env, cwd=work)
    write_text(work / args.file, args.text + "\n")
    git(["add", "-A"], env, cwd=work)
    git(["commit", "--quiet", "-m", args.message or f"Edit {args.file}"], env, cwd=work)
    git(["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"], env, cwd=work)
    sha = git(["rev-parse", "HEAD"], env, cwd=work).stdout.strip()
    remove_tree(work)
    print(f"{record['full_name']} {branch} -> {sha}")
    return 0


def find_pr(run: dict[str, Any], repo: str, number: int) -> dict[str, Any]:
    for pr in admin(run, "/__verify/prs").get(repo.lower(), []):
        if pr["number"] == number:
            return pr
    raise HarnessError(f"{repo} has no PR #{number}")


def print_prs(prs: dict[str, list[dict[str, Any]]]) -> None:
    if not any(prs.values()):
        print("(no pull requests)")
        return
    for repo, items in sorted(prs.items()):
        for pr in sorted(items, key=lambda item: item["number"]):
            merged = " merged" if pr["merged"] else ""
            sha = (pr["head"]["sha"] or "-")[:12]
            print(f"{repo} #{pr['number']} {pr['state']}{merged} head={pr['head']['ref']}@{sha} "
                  f"base={pr['base']['ref']} title={pr['title']!r}")


def cmd_pr(args: argparse.Namespace) -> int:
    run = load_run(args.run)
    env = base_env(run)
    if args.action == "list":
        print_prs(admin(run, "/__verify/prs"))
        return 0
    if args.repo is None or args.number is None:
        raise HarnessError("pr close and pr merge need --repo and --number")
    record = seed_record(args.repo)
    repo = record["full_name"]
    if args.action == "close":
        admin(run, "/__verify/pr/close", {"repo": repo, "number": args.number})
    else:
        pr = find_pr(run, repo, args.number)
        if pr["state"] != "open":
            raise HarnessError(f"{repo} #{args.number} is not open")
        head_ref, base_ref = pr["head"]["ref"], pr["base"]["ref"]
        bare = bare_path(run, repo)
        work = work_dir(run, "merge")
        git(["clone", "--quiet", "--branch", base_ref, str(bare), str(work)], env)
        if args.method == "merge":
            merged = git(["merge", "--no-ff", "--no-edit", "-m",
                          f"Merge pull request #{args.number} from {OWNER}/{head_ref}", f"origin/{head_ref}"],
                         env, cwd=work, check=False)
        else:
            merged = git(["merge", "--squash", f"origin/{head_ref}"], env, cwd=work, check=False)
            if merged.returncode == 0:
                merged = git(["commit", "--quiet", "-m", f"{pr['title']} (#{args.number})"], env, cwd=work, check=False)
        if merged.returncode != 0:
            remove_tree(work)
            raise HarnessError(f"PR is not mergeable: {(merged.stderr or merged.stdout).strip()}")
        git(["push", "--quiet", "origin", f"HEAD:refs/heads/{base_ref}"], env, cwd=work)
        head_sha = git(["rev-parse", f"origin/{head_ref}"], env, cwd=work).stdout.strip()
        remove_tree(work)
        admin(run, "/__verify/pr/mark-merged", {"repo": repo, "number": args.number, "head_sha": head_sha})
    print_prs({repo.lower(): [find_pr(run, repo, args.number)]})
    return 0


def contains_commit(run: dict[str, Any], slug: str, older: str, newer: str) -> bool:
    result = git(
        ["--git-dir", str(bare_path(run, slug)), "merge-base", "--is-ancestor", older, newer],
        base_env(run),
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = (result.stderr or result.stdout).strip()
    raise HarnessError(f"git merge-base failed: {detail}")


def describe(run: dict[str, Any], before: dict[str, Any], after: dict[str, Any],
             proc: subprocess.CompletedProcess[str], requests: list[str], evidence: Path) -> str:
    lines = [f"sync.py exit code: {proc.returncode}", "status lines:"]
    status_lines = []
    current = "-"
    for line in proc.stdout.splitlines():
        if "] Processing " in line:
            current = line.split("] Processing ", 1)[1].strip()
        elif line.startswith(STATUS_PREFIXES):
            status_lines.append(f"{current}: {line}")
    lines += [f"  {ln}" for ln in status_lines] or ["  (none)"]
    lines.append("fork branches (before -> after):")
    for record in SEED_REPOS:
        slug = record["full_name"]
        if not slug.startswith(f"{OWNER}/"):
            continue
        old, new = before["refs"].get(slug, {}), after["refs"].get(slug, {})
        for branch in sorted(set(old) | set(new)):
            a, b = old.get(branch), new.get(branch)
            tag = " (default branch)" if branch == record["default_branch"] else ""
            if a == b:
                lines.append(f"  {slug} {branch}{tag}: {a[:12]} unchanged")
            else:
                alarm = ""
                if tag and a and b and not contains_commit(run, slug, a, b):
                    alarm = "  <-- DEFAULT BRANCH REWRITTEN"
                lines.append(f"  {slug} {branch}{tag}: {(a or '(absent)')[:12]} -> {(b or '(absent)')[:12]}{alarm}")
    lines.append("pull requests after the run:")
    prs = after.get("prs") or {}
    if not any(prs.values()):
        lines.append("  (none)")
    for repo, items in sorted(prs.items()):
        for pr in sorted(items, key=lambda item: item["number"]):
            merged = " merged" if pr["merged"] else ""
            sha = (pr["head"]["sha"] or "-")[:12]
            lines.append(f"  {repo} #{pr['number']} {pr['state']}{merged} "
                         f"head={pr['head']['ref']}@{sha} base={pr['base']['ref']}")
    lines.append(f"GitHub API requests this run: {len(requests)}")
    lines.append(f"evidence: {evidence}")
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> int:
    run = load_run(args.run)
    root = Path(run["scratch"]) / "root"
    step = next_step_dir(run, f"run-{args.label}")
    source = Path(args.sync_py).resolve() if args.sync_py else REPO_ROOT / "scripts" / "sync.py"
    shutil.copyfile(source, root / "scripts" / "sync.py")
    sync_sha = sha256_file(source)
    run["last_sync_py_sha256"] = sync_sha
    save_run(run)

    env = base_env(run)
    env.update({
        "GITHUB_API_URL": f"http://127.0.0.1:{run['port']}",
        "GITHUB_REPOSITORY": MANAGER_REPO,
        "GITHUB_REPOSITORY_OWNER": OWNER,
        "GITHUB_STEP_SUMMARY": str(step / "step-summary.md"),
    })
    if not args.no_token:
        env["FORK_SYNC_TOKEN"] = args.token or TOKEN

    before = snapshot(run)
    offset = len(api_log_lines(run))
    command = [sys.executable, "scripts/sync.py"]
    proc = subprocess.run(command, cwd=str(root), env=env, text=True, encoding="utf-8",
                          errors="replace", capture_output=True, timeout=900)
    after = snapshot(run)
    requests = api_log_lines(run)[offset:]

    shutil.copyfile(root / "config" / "repos.yml", step / "repos.yml")
    write_text(step / "command.txt", f"cwd: {root}\n{' '.join(command)}\nsync.py source: {source}\n"
                                     f"sync.py sha256: {sync_sha}\n")
    write_text(step / "stdout.txt", proc.stdout)
    write_text(step / "stderr.txt", proc.stderr)
    write_text(step / "exit-code.txt", f"{proc.returncode}\n")
    write_text(step / "state-before.json", json.dumps(before, indent=2))
    write_text(step / "state-after.json", json.dumps(after, indent=2))
    write_text(step / "api-requests.jsonl", "\n".join(requests) + ("\n" if requests else ""))
    summary = describe(run, before, after, proc, requests, step)
    write_text(step / "report.txt", summary + "\n")
    print(summary)
    return proc.returncode


def cmd_state(args: argparse.Namespace) -> int:
    run = load_run(args.run)
    step = next_step_dir(run, f"state-{args.label}")
    state = snapshot(run)
    write_text(step / "state.json", json.dumps(state, indent=2))
    for slug, refs in state["refs"].items():
        for branch, sha in sorted(refs.items()):
            print(f"{slug} {branch} {sha[:12]}")
    print_prs(state["prs"])
    print(f"evidence: {step / 'state.json'}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    scratch = scratch_dir(args.run)
    run_file = scratch / "run.json"
    if not run_file.exists():
        print(f"nothing to clean for {args.run!r} at {scratch}")
        return 0
    run = json.loads(run_file.read_text(encoding="utf-8"))
    evidence = Path(run["evidence"])
    evidence.mkdir(parents=True, exist_ok=True)
    for name in ("server.log", "api-requests.jsonl", "run.json"):
        if (scratch / name).exists():
            shutil.copyfile(scratch / name, evidence / name)

    if run.get("port"):
        try:
            http(run, "POST", "/__verify/shutdown", {})
        except Exception:  # noqa: BLE001
            pass
    deadline = time.time() + 10
    pids = [p for p in (run.get("server_pid"), run.get("launcher_pid")) if p]
    while time.time() < deadline and any(is_run_process(p, run["run_id"]) for p in pids):
        time.sleep(0.3)
    for pid in pids:
        if is_run_process(pid, run["run_id"]):
            kill_pid(pid)
            print(f"stopped pid {pid} (started by this run)")
    remove_tree(scratch)
    kept = sorted(p.name for p in evidence.iterdir())
    print(f"removed {scratch}")
    print(f"evidence kept at {evidence}: {', '.join(kept)}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    if not SCRATCH_BASE.exists() or not any(SCRATCH_BASE.iterdir()):
        print("(no runs)")
        return 0
    for path in sorted(SCRATCH_BASE.iterdir()):
        run_file = path / "run.json"
        if run_file.exists():
            run = json.loads(run_file.read_text(encoding="utf-8"))
            alive = run.get("server_pid") and is_run_process(run["server_pid"], run["run_id"])
            print(f"{run['run_id']} port={run.get('port')} server={'up' if alive else 'down'} scratch={path}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("launch", help="create scratch repos and start the fake GitHub API")
    p.add_argument("--run-id")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("doctor", help="read-only health check of a run")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("config", help="edit the run's copy of config/repos.yml")
    p.add_argument("--run", required=True)
    p.add_argument("--reset", action="store_true", help="start again from the repo's config/repos.yml")
    p.add_argument("--dry-run", choices=["true", "false"])
    p.add_argument("--discover", choices=["true", "false"])
    p.add_argument("--owner")
    p.add_argument("--max-repos", type=int)
    p.add_argument("--exclude", nargs="*")
    p.add_argument("--settings-json", help="JSON object merged into `settings:` as written, e.g. '{\"dry_run\": \"false\"}'")
    p.add_argument("--repos-json", help="JSON list for the top-level `repos:` overrides")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("commit", help="push a commit to a branch of a scratch repo")
    p.add_argument("--run", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--branch")
    p.add_argument("--from", dest="from_branch", help="create --branch from this existing branch")
    p.add_argument("--file", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--message")
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("pr", help="list, close, or merge pull requests")
    p.add_argument("action", choices=["list", "close", "merge"])
    p.add_argument("--run", required=True)
    p.add_argument("--repo")
    p.add_argument("--number", type=int)
    p.add_argument("--method", choices=["merge", "squash"], default="merge")
    p.set_defaults(func=cmd_pr)

    p = sub.add_parser("run", help="run scripts/sync.py like the workflow step and record evidence")
    p.add_argument("--run", required=True)
    p.add_argument("--label", default="sync")
    p.add_argument("--no-token", action="store_true", help="leave FORK_SYNC_TOKEN unset")
    p.add_argument("--token", help="send this FORK_SYNC_TOKEN instead of the one the fake API accepts")
    p.add_argument("--sync-py", help="run this copy of sync.py instead of scripts/sync.py")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("state", help="record branch refs")
    p.add_argument("--run", required=True)
    p.add_argument("--label", default="snapshot")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("cleanup", help="stop the run's API process and delete its scratch dir")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("list", help="list runs with scratch dirs")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args()
    try:
        return args.func(args)
    except HarnessError as exc:
        print(f"harness error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
