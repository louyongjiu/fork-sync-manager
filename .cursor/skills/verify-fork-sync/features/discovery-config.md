# 自动发现与配置

用户不需要维护 Fork 列表。任务会列出 token 能看到的、管理账号名下的仓库，只处理其中的 Fork，归档的会跳过。停用某个 Fork、覆盖上游或分支，写在仓库变量 `FORK_SYNC_REPOS`。`settings` 里的 `exclude` 是未知键，运行以退出码 `2` 结束。配置文件和这个变量都在调用 GitHub API 之前检查完。配置错误和缺少 token 会直接让任务以退出码 `2` 结束。列出 Fork 时出错（例如 token 无效）以退出码 `1` 结束。

## Sub-features

- `discover-forks`：只处理 `alice` 名下的 Fork（`alice/widget`、`alice/gadget`、`alice/legacy`），`alice/notes` 不出现在状态行里。
- `archived-skip`：`alice/legacy` 报 `SKIPPED: Fork repository is archived`。
- `disable`：`FORK_SYNC_REPOS` 里写 `enabled: false`，状态为 `SKIPPED: disabled in FORK_SYNC_REPOS`，不请求这个仓库的 GitHub API。仓库不存在或 token 看不到时也一样。
- `override-branch`：`FORK_SYNC_REPOS` 里给 Fork 指定 `upstream_branch`，合并使用这个上游分支，结果推到 Fork 的目标分支。
- `owner-mismatch`：`FORK_SYNC_REPOS` 里写了别的账号的仓库，报 `ERROR: Configured repository does not belong to alice: <仓库>`，退出码 `2`。
- `unknown-keys`：`settings` 或 `FORK_SYNC_REPOS` 某一项里有不认识的键，报 `ERROR: Unknown keys in settings: <键>` 或 `ERROR: Unknown keys in FORK_SYNC_REPOS[<仓库>]: <键>`，退出码 `2`，不请求 GitHub API。`dev_branch` 和 `exclude` 也是不认识的键。开发分支名写在 `FORK_SYNC_DEV_BRANCH`，见 [审查 Pull Request](./review-pr.md)。
- `repos-file-key`：`config/repos.yml` 里出现 `repos`，报 `ERROR: Unknown keys in config/repos.yml: repos`，退出码 `2`，不请求 GitHub API。
- `repos-bad-yaml`：`FORK_SYNC_REPOS` 不是合法 YAML，报 `ERROR: FORK_SYNC_REPOS is not valid YAML: <原因>`，退出码 `2`，不请求 GitHub API。
- `repos-not-list`：`FORK_SYNC_REPOS` 解析出来不是列表，报 `ERROR: FORK_SYNC_REPOS must be a YAML list`，退出码 `2`，不请求 GitHub API。
- `repos-multiline`：`FORK_SYNC_REPOS` 用多行 YAML 给 `alice/gadget` 指定 `upstream_branch: release`。状态、分支、Pull Request 和请求 method 与 `override-branch` 相同。
- `strict-bools`：`dry_run`、`discover_forks`、`enabled` 不是 `true` / `false`（例如字符串 `"false"`），报 `ERROR: settings.dry_run must be true or false; got: 'false'` 这类错误，退出码 `2`，不请求 GitHub API。
- `max-repos`：`settings.max_repos` 限制选出的仓库数，停用项也计入。超过时报 `ERROR: Found <数> repositories; max_repos=<上限>. Increase settings.max_repos only after checking your Token's repository access.`，退出码 `2`。
- `missing-token`：没有 `FORK_SYNC_TOKEN`，报 `ERROR: missing FORK_SYNC_TOKEN environment variable`，退出码 `2`。
- `bad-token`：token 无效时，列出 Fork 的请求失败，报 `ERROR: GitHub API GET /user/repos?affiliation=owner&per_page=100&page=1 failed: HTTP 401: ...`，退出码 `1`。

## How to get to it (user POV)

- 编辑管理仓库 `config/repos.yml` 里的 `settings` 并提交，下一次运行生效。
- 按仓库的条目写在管理仓库 `Settings → Secrets and variables → Actions → Variables` 的 `FORK_SYNC_REPOS`。这次修改不进 git 历史。写错时，下一次运行以退出码 `2` 结束。
- 管理仓库的 `Settings → Secrets and variables → Actions` 里没有配置 `FORK_SYNC_TOKEN` 时，就会出现 `missing-token`。

## Driving it with harness.py

Preconditions:

- 刚 launch 好的运行，`doctor` 健康。
- 临时 `config/repos.yml` 是 `config/repos.example.yml` 的副本：`dry_run: true`、`discover_forks: true`、`max_repos: 100`。
- 下面的命令按顺序执行。`config` 改 `settings`。`variable --repos` 改按仓库的条目。空字符串清除 `FORK_SYNC_REPOS`。`config --reset` 写回基线，不清除这个变量。

- **发现（`discover-forks`、`archived-skip`）。** 命令：`& $py $h run --run $run --label discover`。预期状态行依次是 `alice/gadget: UP_TO_DATE`、`alice/legacy: SKIPPED: Fork repository is archived`、`alice/widget: UP_TO_DATE`，没有 `alice/notes`。`stdout.txt` 里有 `Discovered 3 repositories under alice`。
- **停用（`disable-entries`）。** 命令：`& $py $h variable --run $run --repos '[{"fork": "alice/gadget", "enabled": false}, {"fork": "alice/widget", "enabled": false}]'`，然后 `& $py $h run --run $run --label disable-entries`。预期 `alice/gadget: SKIPPED: disabled in FORK_SYNC_REPOS`、`alice/legacy: SKIPPED: Fork repository is archived`、`alice/widget: SKIPPED: disabled in FORK_SYNC_REPOS`，退出码 `0`。`api-requests.jsonl` 里没有 `GET /repos/alice/gadget`，也没有 `GET /repos/alice/widget`。
- **覆盖上游分支（`override-branch`）。** 命令：`& $py $h commit --run $run --repo octo/gadget --branch release --from develop --file r.txt --text "release only"`，`& $py $h config --run $run --reset --dry-run false`，`& $py $h variable --run $run --repos '[{"fork": "alice/gadget", "upstream_branch": "release"}]'`，然后 `& $py $h run --run $run --label override`。预期 `alice/gadget: MERGED: pushed fast-forward <sha> to develop`，退出码 `0`。`state-after.json` 里 `alice/gadget` 的 `develop` 和 `octo/gadget` 的 `release` 是同一个 sha。`octo/gadget` 的 `develop` 仍是原来的提交。这一步的 `api-requests.jsonl` 里 method 都是 `GET`。
- **账号校验（`owner-mismatch`）。** 命令：`& $py $h variable --run $run --repos '[{"fork": "bob/widget"}]'`，然后 `& $py $h run --run $run --label owner-mismatch`。预期 `sync.py exit code: 2`，状态行 `ERROR: Configured repository does not belong to alice: bob/widget`。
- **缺少 token（`missing-token`）。** 命令：`& $py $h config --run $run --reset`，然后 `& $py $h run --run $run --label no-token --no-token`。预期 `sync.py exit code: 2`，状态行 `ERROR: missing FORK_SYNC_TOKEN environment variable`，`api-requests.jsonl` 为空。
- **无效 token（`bad-token`）。** 命令：`& $py $h config --run $run --reset`，`& $py $h variable --run $run --repos ''`，然后 `& $py $h run --run $run --label bad-token --token not-the-token`。预期 `sync.py exit code: 1`，状态行 `ERROR: GitHub API GET /user/repos?affiliation=owner&per_page=100&page=1 failed: HTTP 401: {"message": "Bad credentials", "errors": []}`，`api-requests.jsonl` 只有这一条请求。
- **未知键（`unknown-keys`）。** 命令：`& $py $h config --run $run --reset --settings-json '{"dryrun": true}'`，`& $py $h variable --run $run --repos '[{"fork": "alice/gadget", "upstream_brnach": "release"}]'`，然后 `& $py $h run --run $run --label unknown-keys`。预期 `sync.py exit code: 2`，状态行 `ERROR: Unknown keys in settings: dryrun`，`api-requests.jsonl` 为空，没有运行报告。再执行 `& $py $h variable --run $run --repos ''`，`& $py $h config --run $run --reset --settings-json '{"exclude": []}'`，然后 `& $py $h run --run $run --label exclude-key`。预期退出码 `2`，状态行 `ERROR: Unknown keys in settings: exclude`，`api-requests.jsonl` 为空。
- **布尔值（`strict-bools`）。** 命令：`& $py $h config --run $run --reset --settings-json '{"dry_run": "false"}'`，`& $py $h variable --run $run --repos '[{"fork": "alice/gadget", "enabled": "false"}]'`，然后 `& $py $h run --run $run --label string-bools`。预期 `sync.py exit code: 2`，状态行 `ERROR: settings.dry_run must be true or false; got: 'false'`，`api-requests.jsonl` 为空。
- **停用看不到的仓库（`disable`）。** 命令：`& $py $h config --run $run --reset`，`& $py $h variable --run $run --repos '[{"fork": "alice/ghost", "enabled": false}]'`，然后 `& $py $h run --run $run --label disabled-unreachable`。预期最后一行状态 `alice/ghost: SKIPPED: disabled in FORK_SYNC_REPOS`，退出码 `0`，`api-requests.jsonl` 里没有 `/repos/alice/ghost`。
- **仓库数上限（`max-repos`）。** 命令：`& $py $h config --run $run --reset --max-repos 2`，`& $py $h variable --run $run --repos ''`，然后 `& $py $h run --run $run --label max-repos-2`。预期 `sync.py exit code: 2`，状态行 `ERROR: Found 3 repositories; max_repos=2. Increase settings.max_repos only after checking your Token's repository access.`，`api-requests.jsonl` 只有一条 `GET /user/repos`。再执行 `& $py $h config --run $run --max-repos 2`，`& $py $h variable --run $run --repos '[{"fork": "alice/gadget", "enabled": false}]'`，然后 `& $py $h run --run $run --label max-repos-2-disabled`。预期仍是退出码 `2` 和 `Found 3 repositories`，停用的 `alice/gadget` 仍计入。然后 `& $py $h config --run $run --max-repos 3`，再 `& $py $h run --run $run --label max-repos-3`。预期退出码 `0`。状态行是 `alice/gadget: SKIPPED: disabled in FORK_SYNC_REPOS`、`alice/legacy: SKIPPED: Fork repository is archived`、`alice/widget: UP_TO_DATE`。
- **文件里的 `repos`（`repos-file-key`）。** 命令：`& $py $h config --run $run --repos-json '[{"fork": "alice/widget"}]'`，然后 `& $py $h run --run $run --label file-repos-key`。预期退出码 `2`，状态行 `ERROR: Unknown keys in config/repos.yml: repos`，`api-requests.jsonl` 为空。
- **接着，YAML 语法（`repos-bad-yaml`）。** 命令：`& $py $h config --run $run --reset`，`& $py $h variable --run $run --repos 'fork: [unclosed'`，然后 `run`。预期退出码 `2`，stdout 含 `FORK_SYNC_REPOS is not valid YAML:`，`api-requests.jsonl` 为空。
- **接着，不是列表（`repos-not-list`）。** 命令：`& $py $h variable --run $run --repos '{"fork": "alice/widget"}'`，然后 `run`。预期退出码 `2`，状态行 `ERROR: FORK_SYNC_REPOS must be a YAML list`，`api-requests.jsonl` 为空。
- **多行 YAML（`repos-multiline`）。** 新 launch。命令与 `override-branch` 相同，只是 `variable --repos` 的值换成多行 YAML：`- fork: alice/gadget` 下一行两个空格再写 `upstream_branch: release`。预期与 `override-branch` 相同。

## Gotchas

- `--repos-json` 把 `repos:` 写进临时配置。`sync.py` 看到这个键就以退出码 `2` 结束。按仓库的条目用 `variable --repos`。
- `settings` 里的 `exclude` 是未知键，报 `Unknown keys in settings: exclude`，退出码 `2`。
- `config --reset` 写回 harness 基线，不清除 `FORK_SYNC_REPOS`。`variable --repos ''` 清除它。停用项计入 `max_repos`。
- `config` 只改临时副本。仓库里的 `config/repos.yml` 不变。
- 替身的 `/user/repos` 只返回一页结果，`max_repos` 的分页上限在本机测不到。
