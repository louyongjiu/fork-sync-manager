# 自动发现与配置

用户不需要维护 Fork 列表。任务会列出 token 能看到的、管理账号名下的仓库，只处理其中的 Fork，归档的会跳过。用户只通过 `config/repos.yml` 做调整：排除某些仓库、停用某个 Fork、覆盖上游或分支、改自动化分支名。整个配置文件在调用 GitHub API 之前检查完。配置错误和缺少 token 会直接让任务以退出码 `2` 结束；列出 Fork 时出错（例如 token 无效）以退出码 `1` 结束。

## Sub-features

- `discover-forks`：只处理 `alice` 名下的 Fork（`alice/widget`、`alice/gadget`、`alice/legacy`），`alice/notes` 不出现在状态行里。
- `archived-skip`：`alice/legacy` 报 `SKIPPED: Fork repository is archived`。
- `exclude`：`settings.exclude` 里写短名 `widget`，`alice/widget` 就不再被处理。
- `disable`：`repos` 里写 `enabled: false`，状态为 `SKIPPED: disabled in config`，不请求这个仓库的 GitHub API。仓库不存在或 token 看不到时也一样。
- `override-branch`：`repos` 里给 Fork 指定 `upstream_branch`，同步 PR 就来自这个上游分支。
- `owner-mismatch`：`repos` 里写了别的账号的仓库，报 `ERROR: Configured repository does not belong to alice: <仓库>`，退出码 `2`。
- `unknown-keys`：`settings` 或 `repos` 某一项里有不认识的键，报 `ERROR: Unknown keys in settings: <键>` 或 `ERROR: Unknown keys in repos[<仓库>]: <键>`，退出码 `2`，不请求 GitHub API。
- `strict-bools`：`dry_run`、`discover_forks`、`enabled` 不是 `true` / `false`（例如字符串 `"false"`），报 `ERROR: settings.dry_run must be true or false; got: 'false'` 这类错误，退出码 `2`，不请求 GitHub API。
- `max-repos`：`settings.max_repos` 限制去掉 `exclude` 之后的仓库数。超过时报 `ERROR: Found <数> repositories; max_repos=<上限>. Increase settings.max_repos only after checking your Token's repository access.`，退出码 `2`。
- `missing-token`：没有 `FORK_SYNC_TOKEN`，报 `ERROR: missing FORK_SYNC_TOKEN environment variable`，退出码 `2`。
- `bad-token`：token 无效时，列出 Fork 的请求失败，报 `ERROR: GitHub API GET /user/repos?affiliation=owner&per_page=100&page=1 failed: HTTP 401: ...`，退出码 `1`。
- `sync-branch`：`settings.sync_branch` 决定自动化分支名。名字不是合法的 Git 分支名时报 `ERROR: Invalid settings.sync_branch: '<名>'`，退出码 `2`，不请求 GitHub API。它和某个 Fork 的目标分支同名时，这个 Fork 报 `FAILED: settings.sync_branch '<名>' is the target branch of <仓库>; use a separate automation branch`，不推送任何分支。

## How to get to it (user POV)

- 编辑管理仓库的 `config/repos.yml` 并提交，下一次运行生效。
- 管理仓库的 `Settings → Secrets and variables → Actions` 里没有配置 `FORK_SYNC_TOKEN` 时，就会出现 `missing-token`。

## Driving it with harness.py

Preconditions:

- 刚 launch 好的运行，`doctor` 健康。
- 下面的命令按顺序执行，每一步都用 `config` 覆盖上一步的设置。

- **发现（`discover-forks`、`archived-skip`）。** 命令：`& $py $h run --run $run --label discover`。预期状态行依次是 `alice/gadget: UP_TO_DATE`、`alice/legacy: SKIPPED: Fork repository is archived`、`alice/widget: UP_TO_DATE`，没有 `alice/notes`。`stdout.txt` 里有 `Discovered 3 repositories under alice`。
- **排除与停用（`exclude`、`disable`）。** 命令：`& $py $h config --run $run --exclude widget --repos-json '[{"fork": "alice/gadget", "enabled": false}]'`，然后 `& $py $h run --run $run --label exclude-disable`。预期只剩 `alice/gadget: SKIPPED: disabled in config` 和 `alice/legacy: SKIPPED: Fork repository is archived`，没有 `alice/widget`，退出码 `0`，`api-requests.jsonl` 里没有 `GET /repos/alice/gadget`。
- **覆盖上游分支（`override-branch`）。** 命令：`& $py $h commit --run $run --repo octo/gadget --branch release --from develop --file r.txt --text "release only"`，`& $py $h config --run $run --reset --dry-run false --repos-json '[{"fork": "alice/gadget", "upstream_branch": "release"}]'`，然后 `& $py $h run --run $run --label override`。预期 `alice/gadget: UPDATED: pushed sync branch; created_pr`，PR 列表里 `alice/gadget #1 open`，`base=develop`，标题 `Upstream sync: octo/gadget (release)`（用 `& $py $h pr list --run $run` 查看）。
- **账号校验（`owner-mismatch`）。** 命令：`& $py $h config --run $run --repos-json '[{"fork": "bob/widget"}]'`，然后 `& $py $h run --run $run --label owner-mismatch`。预期 `sync.py exit code: 2`，状态行 `ERROR: Configured repository does not belong to alice: bob/widget`。
- **缺少 token（`missing-token`）。** 命令：`& $py $h config --run $run --reset`，然后 `& $py $h run --run $run --label no-token --no-token`。预期 `sync.py exit code: 2`，状态行 `ERROR: missing FORK_SYNC_TOKEN environment variable`，`api-requests.jsonl` 为空。
- **无效 token（`bad-token`）。** 命令：`& $py $h config --run $run --reset`，然后 `& $py $h run --run $run --label bad-token --token not-the-token`。预期 `sync.py exit code: 1`，状态行 `ERROR: GitHub API GET /user/repos?affiliation=owner&per_page=100&page=1 failed: HTTP 401: {"message": "Bad credentials", "errors": []}`，`api-requests.jsonl` 只有这一条请求。
- **未知键（`unknown-keys`）。** 命令：`& $py $h config --run $run --reset --settings-json '{"dryrun": true}' --repos-json '[{"fork": "alice/gadget", "upstream_brnach": "release"}]'`，然后 `& $py $h run --run $run --label unknown-keys`。预期 `sync.py exit code: 2`，状态行 `ERROR: Unknown keys in settings: dryrun`，`api-requests.jsonl` 为空，没有运行报告。
- **布尔值（`strict-bools`）。** 命令：`& $py $h config --run $run --reset --settings-json '{"dry_run": "false"}' --repos-json '[{"fork": "alice/gadget", "enabled": "false"}]'`，然后 `& $py $h run --run $run --label string-bools`。预期 `sync.py exit code: 2`，状态行 `ERROR: settings.dry_run must be true or false; got: 'false'`，`api-requests.jsonl` 为空。
- **停用看不到的仓库（`disable`）。** 命令：`& $py $h config --run $run --reset --repos-json '[{"fork": "alice/ghost", "enabled": false}]'`，然后 `& $py $h run --run $run --label disabled-unreachable`。预期最后一行状态 `alice/ghost: SKIPPED: disabled in config`，退出码 `0`，`api-requests.jsonl` 里没有 `/repos/alice/ghost`。
- **仓库数上限（`max-repos`）。** 命令：`& $py $h config --run $run --reset --max-repos 2`，然后 `& $py $h run --run $run --label max-repos-2`。预期 `sync.py exit code: 2`，状态行 `ERROR: Found 3 repositories; max_repos=2. Increase settings.max_repos only after checking your Token's repository access.`，`api-requests.jsonl` 只有一条 `GET /user/repos`。再执行 `& $py $h config --run $run --max-repos 2 --exclude widget` 和 `& $py $h run --run $run --label max-repos-2-excluding-one`，预期退出码 `0`，状态行是 `alice/gadget: UP_TO_DATE` 和 `alice/legacy: SKIPPED: Fork repository is archived`。
- **不合法的自动化分支名（`sync-branch`）。** 命令：`& $py $h config --run $run --reset --sync-branch bad..name`，然后 `& $py $h run --run $run --label sync-branch-invalid`。预期 `sync.py exit code: 2`，状态行 `ERROR: Invalid settings.sync_branch: 'bad..name'`，`api-requests.jsonl` 为空。
- **自动化分支名（`sync-branch`）。** 在新 launch 的运行里执行：`config --dry-run false --sync-branch main --exclude gadget`；`commit --repo octo/widget --file feature.txt --text "w1"`；`run --label sync-branch-main`。预期 `alice/widget: FAILED: settings.sync_branch 'main' is the target branch of alice/widget; use a separate automation branch`，`sync.py exit code: 1`，`alice/widget main (default branch): <sha> unchanged`，这一步的 `api-requests.jsonl` 里没有 `POST` 或 `PATCH`。

## Gotchas

- `sync-branch` 那一步加了 `--exclude gadget`，是为了让输出里只剩 `alice/widget`。`alice/gadget` 的默认分支是 `develop`，和 `main` 不同名，不排除的话它会照常同步到一个叫 `main` 的自动化分支。
- `exclude` 里的短名会补成 `<owner>/<名>`。写成 `alice/widget` 效果相同，写成 `octo/widget` 不会排除任何 Fork。
- `--repos-json` 会替换整个 `repos:` 列表，`--exclude` 会替换整个 `exclude:` 列表。要回到仓库原样，用 `--reset`。
- `config` 写回的 YAML 不带原文件的注释，只影响临时副本，仓库里的 `config/repos.yml` 不变。
- 替身的 `/user/repos` 只返回一页结果，`max_repos` 的分页上限在本机测不到。
