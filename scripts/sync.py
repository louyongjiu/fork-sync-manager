#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "repos.yml"
API_BASE = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
TOKEN = os.environ.get("FORK_SYNC_TOKEN", "").strip()
MANAGER_REPO = os.environ.get("GITHUB_REPOSITORY", "YOUR_GITHUB_USERNAME/fork-sync-manager")
API_VERSION = "2022-11-28"
STATUS_LABELS: dict[str, str] = {
    "updated": "Updated / PR",
    "up_to_date": "Up to date",
    "conflict": "Conflicts",
    "skipped": "Skipped",
    "failed": "Failed",
    "dry_run": "Dry run",
}

metadata_cache: dict[str, dict[str, Any]] = {}


class SyncError(RuntimeError):
    pass


class GitHubApiError(SyncError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class SelectionError(SyncError):
    pass


@dataclass(frozen=True)
class Settings:
    owner: str
    dry_run: bool
    discover_forks: bool
    max_repos: int
    sync_branch: str
    pr_title_prefix: str
    exclude: frozenset[str]


@dataclass(frozen=True)
class RepoSpec:
    fork: str
    enabled: bool = True
    upstream: str | None = None
    target_branch: str | None = None
    upstream_branch: str | None = None


@dataclass(frozen=True)
class ResolvedRepo:
    fork: str
    upstream: str
    target_branch: str
    upstream_branch: str
    sync_branch: str
    title: str

    def __post_init__(self) -> None:
        if self.sync_branch == self.target_branch:
            raise SyncError(
                f"settings.sync_branch {self.sync_branch!r} is the target branch of {self.fork}; "
                "use a separate automation branch"
            )


@dataclass(frozen=True)
class RepoResult:
    status: str
    details: str


@dataclass(frozen=True)
class SyncCommit:
    reusable: bool
    head_sha: str
    upstream_sha: str


def log(message: str) -> None:
    print(message, flush=True)


def api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url = API_BASE + path
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "fork-sync-manager",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if body is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubApiError(
            f"GitHub API {method} {path} failed: HTTP {exc.code}: {detail[:1500]}", exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"GitHub API connection failed for {path}: {exc}") from exc


def validate_repo_slug(value: str, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise SyncError(f"{field} must be in owner/repo format; got: {value!r}")
    return value


def validate_branch(branch: str, field: str) -> str:
    if not isinstance(branch, str) or not branch.strip():
        raise SyncError(f"{field} must be a non-empty branch name")
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SyncError(f"Invalid {field}: {branch!r}")
    return branch


def repo_metadata(slug: str) -> dict[str, Any]:
    if slug not in metadata_cache:
        metadata_cache[slug] = api_request("GET", f"/repos/{slug}")
    return metadata_cache[slug]


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Supply the token through an HTTP extraheader instead of embedding it in remote URLs.
    if TOKEN:
        basic = base64.b64encode(f"x-access-token:{TOKEN}".encode("utf-8")).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    return env


def git(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=git_env(),
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SyncError(f"git {' '.join(args)} failed: {detail[-2500:]}")
    return result


def is_ancestor(cwd: Path, older: str, newer: str) -> bool:
    result = git(["merge-base", "--is-ancestor", older, newer], cwd, check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = (result.stderr or result.stdout).strip()
    raise SyncError(f"git merge-base failed: {detail}")


def remote_branch_exists(cwd: Path, repo_slug: str, branch: str) -> bool:
    result = git(
        ["ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
        cwd,
        check=False,
    )
    if result.returncode == 0:
        return True
    # ls-remote returns 2 when the requested ref does not exist.
    if result.returncode == 2:
        return False
    detail = (result.stderr or result.stdout).strip()
    raise SyncError(f"Unable to inspect automation branch in {repo_slug}: {detail}")


def list_sync_prs(fork: str, head: str, base: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({
        "state": "all",
        "head": head,
        "base": base,
        "per_page": 100,
    })
    result = api_request("GET", f"/repos/{fork}/pulls?{query}")
    if not isinstance(result, list):
        raise SyncError(f"GitHub /repos/{fork}/pulls returned an unexpected result")
    return result


def ensure_pull_request(
    fork: str,
    target_branch: str,
    sync_branch: str,
    title: str,
    body: str,
    head_sha: str,
) -> tuple[str, str | None]:
    owner = fork.split("/", 1)[0]
    prs = list_sync_prs(fork, f"{owner}:{sync_branch}", target_branch)
    prs.sort(key=lambda item: item.get("updated_at", ""), reverse=True)

    open_pr = next((pr for pr in prs if pr.get("state") == "open"), None)
    if open_pr:
        updated = api_request(
            "PATCH",
            f"/repos/{fork}/pulls/{open_pr['number']}",
            {"title": title, "body": body},
        )
        return "updated_pr", updated.get("html_url")

    closed_pr = next((pr for pr in prs if pr.get("state") == "closed"), None)
    if closed_pr:
        # Do not reopen a PR that the user closed unless the automation branch
        # now contains new commits compared with the closed PR's head.
        old_sha = ((closed_pr.get("head") or {}).get("sha") or "")
        if old_sha == head_sha:
            return "closed_by_user", closed_pr.get("html_url")
        # GitHub never reopens a merged PR, and answers 422 to reopening a PR
        # whose head branch was force-pushed or recreated after it closed.
        # Both cases get a new PR for the same head and base.
        if not closed_pr.get("merged_at"):
            try:
                reopened = api_request(
                    "PATCH",
                    f"/repos/{fork}/pulls/{closed_pr['number']}",
                    {"state": "open", "title": title, "body": body},
                )
                return "reopened_pr", reopened.get("html_url")
            except GitHubApiError as exc:
                if exc.status != 422:
                    raise

    created = api_request(
        "POST",
        f"/repos/{fork}/pulls",
        {
            "title": title,
            "body": body,
            "head": f"{owner}:{sync_branch}",
            "base": target_branch,
            "maintainer_can_modify": True,
        },
    )
    return "created_pr", created.get("html_url")


def append_summary(rows: list[dict[str, str]], counts: dict[str, int]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## Fork upstream sync summary",
        "",
        *[
            f"- {label}: **{counts[status]}**"
            for status, label in STATUS_LABELS.items()
        ],
        "",
        "| Repository | Status | Details |",
        "|---|---|---|",
    ]
    for row in rows:
        repo = row["repo"].replace("|", "\\|")
        status = row["status"].replace("|", "\\|")
        details = row["details"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{repo}` | {status} | {details} |")
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def sync_one(repo: RepoSpec, settings: Settings) -> RepoResult:
    if not repo.enabled:
        return RepoResult("skipped", "disabled in config")
    fork_meta = repo_metadata(repo.fork)
    if fork_meta.get("archived"):
        return RepoResult("skipped", "Fork repository is archived")

    upstream = repo.upstream
    if upstream is None:
        parent = fork_meta.get("parent") or {}
        upstream = parent.get("full_name")
        if not upstream:
            raise SyncError(
                f"{repo.fork} has no GitHub parent metadata; add `upstream: owner/repo` in config"
            )
        upstream = validate_repo_slug(upstream, "detected upstream")

    upstream_meta = repo_metadata(upstream)

    target_branch = repo.target_branch or validate_branch(
        fork_meta.get("default_branch") or "", "target_branch"
    )
    upstream_branch = repo.upstream_branch or validate_branch(
        upstream_meta.get("default_branch") or "", "upstream_branch"
    )
    resolved = ResolvedRepo(
        fork=repo.fork,
        upstream=upstream,
        target_branch=target_branch,
        upstream_branch=upstream_branch,
        sync_branch=settings.sync_branch,
        title=f"{settings.pr_title_prefix}: {upstream} ({upstream_branch})",
    )
    commit = advance_sync_branch(resolved, settings.dry_run)
    if isinstance(commit, RepoResult):
        return commit
    return publish_sync(resolved, commit)


def advance_sync_branch(repo: ResolvedRepo, dry_run: bool) -> RepoResult | SyncCommit:
    with tempfile.TemporaryDirectory(prefix="fork-sync-") as tmp:
        workdir = Path(tmp)
        git(["init"], workdir)
        git(["config", "user.name", "github-actions[bot]"], workdir)
        git(["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], workdir)
        git(["remote", "add", "origin", f"https://github.com/{repo.fork}.git"], workdir)
        git(["remote", "add", "upstream", f"https://github.com/{repo.upstream}.git"], workdir)

        target_ref = f"refs/remotes/origin/{repo.target_branch}"
        upstream_ref = f"refs/remotes/upstream/{repo.upstream_branch}"
        sync_ref = f"refs/remotes/origin/{repo.sync_branch}"
        git(["fetch", "--no-tags", "origin", f"+refs/heads/{repo.target_branch}:{target_ref}"], workdir)
        git(["fetch", "--no-tags", "upstream", f"+refs/heads/{repo.upstream_branch}:{upstream_ref}"], workdir)

        if is_ancestor(workdir, upstream_ref, target_ref):
            return RepoResult(
                "up_to_date", f"{repo.fork} already contains {repo.upstream}@{repo.upstream_branch}"
            )

        has_remote_sync_branch = remote_branch_exists(workdir, repo.fork, repo.sync_branch)
        old_sync_sha = None
        if has_remote_sync_branch:
            git(["fetch", "--no-tags", "origin", f"+refs/heads/{repo.sync_branch}:{sync_ref}"], workdir)
            old_sync_sha = git(["rev-parse", sync_ref], workdir).stdout.strip()

        reusable = (
            has_remote_sync_branch
            and is_ancestor(workdir, target_ref, sync_ref)
            and is_ancestor(workdir, upstream_ref, sync_ref)
        )

        if reusable:
            git(["checkout", "-B", repo.sync_branch, sync_ref], workdir)
            if dry_run:
                return RepoResult("dry_run", "Would ensure PR for existing automation branch")
        else:
            git(["checkout", "-B", repo.sync_branch, target_ref], workdir)
            merge = git(
                ["merge", "--no-ff", "--no-edit", upstream_ref],
                workdir,
                check=False,
            )
            if merge.returncode != 0:
                conflicts = git(
                    ["diff", "--name-only", "--diff-filter=U"],
                    workdir,
                    check=False,
                ).stdout.strip().splitlines()
                if conflicts:
                    git(["merge", "--abort"], workdir, check=False)
                    return RepoResult("conflict", "Conflicting files: " + ", ".join(conflicts[:30]))
                detail = (merge.stderr or merge.stdout).strip()
                raise SyncError(f"merge failed (possibly unrelated histories): {detail[-2000:]}")

            if dry_run:
                sha = git(["rev-parse", "HEAD"], workdir).stdout.strip()
                return RepoResult("dry_run", f"Would push merge {sha[:12]} and open/update PR")

            if has_remote_sync_branch:
                git(
                    ["push",
                     f"--force-with-lease=refs/heads/{repo.sync_branch}:{old_sync_sha}",
                     "origin",
                     f"HEAD:refs/heads/{repo.sync_branch}"],
                    workdir,
                )
            else:
                git(["push", "origin", f"HEAD:refs/heads/{repo.sync_branch}"], workdir)

        return SyncCommit(
            reusable=reusable,
            head_sha=git(["rev-parse", "HEAD"], workdir).stdout.strip(),
            upstream_sha=git(["rev-parse", upstream_ref], workdir).stdout.strip(),
        )


def publish_sync(repo: ResolvedRepo, commit: SyncCommit) -> RepoResult:
    body = f"""## Automated upstream sync

This PR was generated by [{MANAGER_REPO}](https://github.com/{MANAGER_REPO}).

| Item | Value |
|---|---|
| Fork | `{repo.fork}` |
| Target branch | `{repo.target_branch}` |
| Upstream | `{repo.upstream}` |
| Upstream branch | `{repo.upstream_branch}` |
| Upstream commit | `{commit.upstream_sha}` |
| Sync branch | `{repo.sync_branch}` |
| Sync commit | `{commit.head_sha}` |

The sync uses a regular Git merge. Existing commits on the Fork's target branch are retained.
If a merge conflict is detected, this automation does not push a new sync branch for that run.

Please review the diff and CI checks before merging.
"""
    pr_status, pr_url = ensure_pull_request(
        fork=repo.fork,
        target_branch=repo.target_branch,
        sync_branch=repo.sync_branch,
        title=repo.title,
        body=body,
        head_sha=commit.head_sha,
    )
    if pr_status == "closed_by_user":
        return RepoResult("skipped", f"PR was previously closed and sync branch has not changed: {pr_url}")
    action = "reused" if commit.reusable else "pushed"
    url_text = f" | {pr_url}" if pr_url else ""
    return RepoResult("updated", f"{action} sync branch; {pr_status}{url_text}")


def discover_forks(owner: str) -> list[str]:
    discovered: list[str] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({
            "affiliation": "owner", "per_page": 100, "page": page,
        })
        # A fine-grained PAT lists only the repositories selected in its repository access.
        batch = api_request("GET", f"/user/repos?{query}")
        if not isinstance(batch, list):
            raise SyncError("GitHub /user/repos returned an unexpected result")
        for repo in batch:
            repo_owner = ((repo.get("owner") or {}).get("login") or "")
            if repo.get("fork") and repo_owner.lower() == owner.lower():
                discovered.append(validate_repo_slug(repo.get("full_name"), "fork"))
        if len(batch) < 100:
            break
        page += 1
        if page > 100:
            raise SyncError("Pagination exceeded 100 pages")
    return discovered


def reject_unknown_keys(raw: dict[Any, Any], cls: type, where: str) -> None:
    unknown = set(raw) - {field.name for field in fields(cls)}
    if unknown:
        raise SyncError(f"Unknown keys in {where}: {', '.join(sorted(map(str, unknown)))}")


def read_bool(raw: dict[Any, Any], key: str, default: bool, name: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise SyncError(f"{name} must be true or false; got: {value!r}")
    return value


def parse_config(path: Path) -> tuple[Settings, tuple[RepoSpec, ...]]:
    if not path.exists():
        raise SyncError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise SyncError("config/repos.yml must contain a YAML object")

    raw_settings = config.get("settings") or {}
    raw_repos = config.get("repos") or []
    if not isinstance(raw_settings, dict):
        raise SyncError("settings must be a YAML object")
    if not isinstance(raw_repos, list):
        raise SyncError("repos must be a YAML list")
    reject_unknown_keys(raw_settings, Settings, "settings")

    owner = (raw_settings.get("owner") or os.environ.get("GITHUB_REPOSITORY_OWNER") or
             MANAGER_REPO.split("/", 1)[0])
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise SyncError(f"Invalid settings.owner: {owner!r}")

    max_repos = raw_settings.get("max_repos", 100)
    if not isinstance(max_repos, int) or isinstance(max_repos, bool) or not (1 <= max_repos <= 1000):
        raise SyncError("settings.max_repos must be an integer from 1 to 1000")

    excluded = raw_settings.get("exclude") or []
    if not isinstance(excluded, list) or any(not isinstance(x, str) for x in excluded):
        raise SyncError("settings.exclude must be a list of repository names")

    dry_run = read_bool(raw_settings, "dry_run", False, "settings.dry_run")
    discover = read_bool(raw_settings, "discover_forks", True, "settings.discover_forks")
    sync_branch = validate_branch(
        raw_settings.get("sync_branch", "automation/upstream-sync"), "settings.sync_branch"
    )
    title_prefix = raw_settings.get("pr_title_prefix", "Upstream sync")
    if not isinstance(title_prefix, str):
        raise SyncError(f"settings.pr_title_prefix must be a string; got: {title_prefix!r}")

    settings = Settings(
        owner=owner,
        dry_run=dry_run,
        discover_forks=discover,
        max_repos=max_repos,
        sync_branch=sync_branch,
        pr_title_prefix=title_prefix,
        exclude=frozenset(x.lower() if "/" in x else f"{owner}/{x}".lower() for x in excluded),
    )

    specs: list[RepoSpec] = []
    for item in raw_repos:
        if not isinstance(item, dict):
            raise SyncError("Each repos entry must be a YAML object")
        fork = validate_repo_slug(item.get("fork"), "fork")
        if fork.split("/", 1)[0].lower() != owner.lower():
            raise SyncError(f"Configured repository does not belong to {owner}: {fork}")
        reject_unknown_keys(item, RepoSpec, f"repos[{fork}]")
        enabled = read_bool(item, "enabled", True, f"repos[{fork}].enabled")
        upstream = item.get("upstream")
        target_branch = item.get("target_branch")
        upstream_branch = item.get("upstream_branch")
        specs.append(RepoSpec(
            fork=fork,
            enabled=enabled,
            upstream=validate_repo_slug(upstream, "upstream") if upstream else None,
            target_branch=validate_branch(target_branch, "target_branch") if target_branch else None,
            upstream_branch=validate_branch(upstream_branch, "upstream_branch") if upstream_branch else None,
        ))
    return settings, tuple(specs)


def select_repos(settings: Settings, specs: tuple[RepoSpec, ...]) -> tuple[RepoSpec, ...]:
    selected: dict[str, RepoSpec] = {}
    if settings.discover_forks:
        for fork in discover_forks(settings.owner):
            selected[fork.lower()] = RepoSpec(fork=fork)
    for spec in specs:
        selected[spec.fork.lower()] = spec
    repos = tuple(spec for slug, spec in selected.items() if slug not in settings.exclude)

    if len(repos) > settings.max_repos:
        raise SelectionError(
            f"Found {len(repos)} repositories; max_repos={settings.max_repos}. "
            "Increase settings.max_repos only after checking your Token's repository access."
        )
    if not repos:
        raise SelectionError(
            f"No Forks found for {settings.owner}. Check FORK_SYNC_TOKEN repository access, "
            "settings.owner, and settings.discover_forks."
        )
    return repos


def main() -> int:
    if not TOKEN:
        log("ERROR: missing FORK_SYNC_TOKEN environment variable")
        return 2

    try:
        settings, specs = parse_config(CONFIG_FILE)
    except Exception as exc:
        log(f"ERROR: {exc}")
        if not isinstance(exc, SyncError):
            log(traceback.format_exc().rstrip())
        return 2

    try:
        repos = select_repos(settings, specs)
    except SelectionError as exc:
        log(f"ERROR: {exc}")
        return 2
    except Exception as exc:
        log(f"ERROR: {exc}")
        if not isinstance(exc, SyncError):
            log(traceback.format_exc().rstrip())
        return 1

    log(f"Discovered {len(repos)} repositories under {settings.owner}")
    log(f"Starting sync for {len(repos)} configured repositories; dry_run={settings.dry_run}")

    rows: list[dict[str, str]] = []
    counts = dict.fromkeys(STATUS_LABELS, 0)

    for index, repo in enumerate(repos, start=1):
        log(f"\n[{index}/{len(repos)}] Processing {repo.fork}")
        try:
            result = sync_one(repo, settings)
            counts[result.status] += 1
            rows.append({"repo": repo.fork, "status": result.status, "details": result.details})
            log(f"{result.status.upper()}: {result.details}")
        except Exception as exc:
            counts["failed"] += 1
            rows.append({"repo": repo.fork, "status": "failed", "details": str(exc)})
            log(f"FAILED: {exc}")
            if not isinstance(exc, SyncError):
                log(traceback.format_exc().rstrip())

    append_summary(rows, counts)

    log("\n=== Final summary ===")
    for key, value in counts.items():
        log(f"{key}: {value}")

    # Conflicts are expected manual-review cases; they do not stop other repos.
    # Actual API/network/permission/config errors make the workflow fail.
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
