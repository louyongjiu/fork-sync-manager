#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env or git_env(),
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


def ref_exists(cwd: Path, ref: str) -> bool:
    result = git(["show-ref", "--verify", "--quiet", ref], cwd, check=False)
    return result.returncode == 0


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
    return result if isinstance(result, list) else []


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


def sync_one(
    item: dict[str, Any],
    settings: dict[str, Any],
    dry_run: bool,
) -> tuple[str, str]:
    fork = validate_repo_slug(item.get("fork"), "fork")
    fork_meta = repo_metadata(fork)

    if not item.get("enabled", True):
        return "skipped", "disabled in config"
    if fork_meta.get("archived"):
        return "skipped", "Fork repository is archived"

    upstream = item.get("upstream")
    if upstream:
        upstream = validate_repo_slug(upstream, "upstream")
    else:
        parent = fork_meta.get("parent") or {}
        upstream = parent.get("full_name")
        if not upstream:
            raise SyncError(
                f"{fork} has no GitHub parent metadata; add `upstream: owner/repo` in config"
            )
        upstream = validate_repo_slug(upstream, "detected upstream")

    upstream_meta = repo_metadata(upstream)

    target_branch = validate_branch(
        item.get("target_branch") or fork_meta.get("default_branch") or "",
        "target_branch",
    )
    upstream_branch = validate_branch(
        item.get("upstream_branch") or upstream_meta.get("default_branch") or "",
        "upstream_branch",
    )
    sync_branch = validate_branch(
        settings.get("sync_branch", "automation/upstream-sync"),
        "sync_branch",
    )
    if sync_branch == target_branch:
        raise SyncError(
            f"settings.sync_branch {sync_branch!r} is the target branch of {fork}; "
            "use a separate automation branch"
        )

    title_prefix = settings.get("pr_title_prefix", "Upstream sync")
    title = f"{title_prefix}: {upstream} ({upstream_branch})"

    with tempfile.TemporaryDirectory(prefix="fork-sync-") as tmp:
        workdir = Path(tmp)
        git(["init"], workdir)
        git(["config", "user.name", "github-actions[bot]"], workdir)
        git(["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], workdir)
        git(["remote", "add", "origin", f"https://github.com/{fork}.git"], workdir)
        git(["remote", "add", "upstream", f"https://github.com/{upstream}.git"], workdir)

        git(
            ["fetch", "--no-tags", "origin",
             f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}"],
            workdir,
        )
        git(
            ["fetch", "--no-tags", "upstream",
             f"+refs/heads/{upstream_branch}:refs/remotes/upstream/{upstream_branch}"],
            workdir,
        )

        target_ref = f"refs/remotes/origin/{target_branch}"
        upstream_ref = f"refs/remotes/upstream/{upstream_branch}"

        if is_ancestor(workdir, upstream_ref, target_ref):
            return "up_to_date", f"{fork} already contains {upstream}@{upstream_branch}"

        # Fetch the previous automation branch, if it exists.
        has_remote_sync_branch = remote_branch_exists(workdir, fork, sync_branch)
        sync_ref = f"refs/remotes/origin/{sync_branch}"
        old_sync_sha = None

        if has_remote_sync_branch:
            git(
                ["fetch", "--no-tags", "origin",
                 f"+refs/heads/{sync_branch}:{sync_ref}"],
                workdir,
            )
            old_sync_sha = git(["rev-parse", sync_ref], workdir).stdout.strip()

        # Reuse the branch if it already includes both the latest target and
        # latest upstream. This avoids creating a new merge commit every day.
        reusable = (
            has_remote_sync_branch
            and ref_exists(workdir, sync_ref)
            and is_ancestor(workdir, target_ref, sync_ref)
            and is_ancestor(workdir, upstream_ref, sync_ref)
        )

        if reusable:
            git(["checkout", "-B", sync_branch, sync_ref], workdir)
            pushed = False
        else:
            git(["checkout", "-B", sync_branch, target_ref], workdir)
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
                    return "conflict", "Conflicting files: " + ", ".join(conflicts[:30])
                detail = (merge.stderr or merge.stdout).strip()
                raise SyncError(f"merge failed (possibly unrelated histories): {detail[-2000:]}")

            if dry_run:
                sha = git(["rev-parse", "HEAD"], workdir).stdout.strip()
                return "dry_run", f"Would push merge {sha[:12]} and open/update PR"

            if has_remote_sync_branch and old_sync_sha:
                git(
                    ["push",
                     f"--force-with-lease=refs/heads/{sync_branch}:{old_sync_sha}",
                     "origin",
                     f"HEAD:refs/heads/{sync_branch}"],
                    workdir,
                )
            else:
                git(
                    ["push", "origin", f"HEAD:refs/heads/{sync_branch}"],
                    workdir,
                )
            pushed = True

        if dry_run:
            return "dry_run", "Would ensure PR for existing automation branch"

        head_sha = git(["rev-parse", "HEAD"], workdir).stdout.strip()
        pr_body = f"""## Automated upstream sync

This PR was generated by [{MANAGER_REPO}](https://github.com/{MANAGER_REPO}).

| Item | Value |
|---|---|
| Fork | `{fork}` |
| Target branch | `{target_branch}` |
| Upstream | `{upstream}` |
| Upstream branch | `{upstream_branch}` |
| Upstream commit | `{git(['rev-parse', upstream_ref], workdir).stdout.strip()}` |
| Sync branch | `{sync_branch}` |
| Sync commit | `{head_sha}` |

The sync uses a regular Git merge. Existing commits on the Fork's target branch are retained.
If a merge conflict is detected, this automation does not push a new sync branch for that run.

Please review the diff and CI checks before merging.
"""

        pr_status, pr_url = ensure_pull_request(
            fork=fork,
            target_branch=target_branch,
            sync_branch=sync_branch,
            title=title,
            body=pr_body,
            head_sha=head_sha,
        )

        action = "reused" if reusable else ("pushed" if pushed else "checked")
        url_text = f" | {pr_url}" if pr_url else ""
        if pr_status == "closed_by_user":
            return "skipped", f"PR was previously closed and sync branch has not changed: {pr_url}"
        return "updated", f"{action} sync branch; {pr_status}{url_text}"


def discover_forks(owner: str, max_repos: int) -> list[dict[str, Any]]:
    """Find permitted Forks owned by the manager account, paginating all results.

    With a fine-grained PAT, this API generally returns repositories selected
    when the token was created. Only enabled Forks owned by `owner` are used.
    """
    discovered: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({
            "affiliation": "owner", "per_page": 100, "page": page,
        })
        batch = api_request("GET", f"/user/repos?{query}")
        if not isinstance(batch, list):
            raise SyncError("GitHub /user/repos returned an unexpected result")
        for repo in batch:
            repo_owner = ((repo.get("owner") or {}).get("login") or "")
            if repo.get("fork") and repo_owner.lower() == owner.lower():
                discovered.append({"fork": validate_repo_slug(repo.get("full_name"), "fork")})
                if len(discovered) > max_repos:
                    raise SyncError(
                        f"Found more than max_repos={max_repos} Forks. "
                        "Increase settings.max_repos only after checking your Token's repository access."
                    )
        if len(batch) < 100:
            break
        page += 1
        if page > 100:
            raise SyncError("Pagination exceeded 100 pages")
    return discovered


def load_config() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not CONFIG_FILE.exists():
        raise SyncError(f"Config file not found: {CONFIG_FILE}")
    with CONFIG_FILE.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise SyncError("config/repos.yml must contain a YAML object")

    settings = config.get("settings") or {}
    configured_repos = config.get("repos") or []
    if not isinstance(settings, dict):
        raise SyncError("settings must be a YAML object")
    if not isinstance(configured_repos, list):
        raise SyncError("repos must be a YAML list")

    owner = (settings.get("owner") or os.environ.get("GITHUB_REPOSITORY_OWNER") or
             MANAGER_REPO.split("/", 1)[0])
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise SyncError(f"Invalid settings.owner: {owner!r}")

    max_repos = settings.get("max_repos", 100)
    if not isinstance(max_repos, int) or isinstance(max_repos, bool) or not (1 <= max_repos <= 1000):
        raise SyncError("settings.max_repos must be an integer from 1 to 1000")

    excluded = settings.get("exclude") or []
    if not isinstance(excluded, list) or any(not isinstance(x, str) for x in excluded):
        raise SyncError("settings.exclude must be a list of repository names")
    exclude_slugs = {
        x.lower() if "/" in x else f"{owner}/{x}".lower()
        for x in excluded
    }

    # Additional entries can override the discovered Fork's upstream and branch.
    # They also allow explicitly including repositories without fork metadata.
    override_map: dict[str, dict[str, Any]] = {}
    for item in configured_repos:
        if not isinstance(item, dict):
            raise SyncError("Each repos entry must be a YAML object")
        slug = validate_repo_slug(item.get("fork"), "fork")
        if slug.split("/", 1)[0].lower() != owner.lower():
            raise SyncError(f"Configured repository does not belong to {owner}: {slug}")
        override_map[slug.lower()] = dict(item)

    selected: dict[str, dict[str, Any]] = {}
    if settings.get("discover_forks", True):
        for item in discover_forks(owner, max_repos):
            slug = item["fork"].lower()
            if slug not in exclude_slugs:
                selected[slug] = item

    for slug, item in override_map.items():
        if slug not in exclude_slugs:
            selected[slug] = {**selected.get(slug, {}), **item}

    result = list(selected.values())
    if len(result) > max_repos:
        raise SyncError(f"Found {len(result)} repositories; max_repos={max_repos}")
    if not result:
        raise SyncError(
            f"No Forks found for {owner}. Check FORK_SYNC_TOKEN repository access, "
            "settings.owner, and settings.discover_forks."
        )
    log(f"Discovered {len(result)} repositories under {owner}")
    return settings, result


def main() -> int:
    if not TOKEN:
        log("ERROR: missing FORK_SYNC_TOKEN environment variable")
        return 2

    try:
        settings, repos = load_config()
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 2

    dry_run = bool(settings.get("dry_run", False))
    log(f"Starting sync for {len(repos)} configured repositories; dry_run={dry_run}")

    rows: list[dict[str, str]] = []
    counts = dict.fromkeys(STATUS_LABELS, 0)

    for index, item in enumerate(repos, start=1):
        if not isinstance(item, dict):
            row = {"repo": f"entry-{index}", "status": "failed", "details": "entry must be a YAML object"}
            rows.append(row)
            counts["failed"] += 1
            log(f"[{index}/{len(repos)}] FAILED: {row['details']}")
            continue

        repo_name = str(item.get("fork", f"entry-{index}"))
        log(f"\n[{index}/{len(repos)}] Processing {repo_name}")
        try:
            status, details = sync_one(item, settings, dry_run)
            counts[status] += 1
            rows.append({"repo": repo_name, "status": status, "details": details})
            log(f"{status.upper()}: {details}")
        except Exception as exc:
            counts["failed"] += 1
            rows.append({"repo": repo_name, "status": "failed", "details": str(exc)})
            log(f"FAILED: {exc}")

    append_summary(rows, counts)

    log("\n=== Final summary ===")
    for key, value in counts.items():
        log(f"{key}: {value}")

    # Conflicts are expected manual-review cases; they do not stop other repos.
    # Actual API/network/permission/config errors make the workflow fail.
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
