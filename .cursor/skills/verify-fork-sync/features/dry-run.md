# 试运行

`dry_run: true` 时，同步任务会照常读取 Fork 和上游，在临时目录里尝试合并，并在日志和摘要里报告每个 Fork 会发生什么。它不推送任何分支。远程目标分支保持原提交。首次部署时，用户就是靠这一步确认选中了哪些 Fork、上游和分支。

## Sub-features

- `dry-detect`：上游有新提交、Fork 没有自己的提交时，报 `DRY_RUN: Would push fast-forward <12 位 sha> to main`。
- `dry-up-to-date`：Fork 已包含上游时报 `UP_TO_DATE`。
- `dry-conflict`：试运行也会尝试合并。遇到冲突时报 `CONFLICT`，详情和正式运行相同。这次运行的退出码是 `1`。
- `dry-no-writes`：所有分支不变。`api-requests.jsonl` 里每条请求的 method 都是 `GET`。

## How to get to it (user POV)

- 首次部署时用 `config/repos.example.yml` 替换 `config/repos.yml`（其中是 `dry_run: true`），然后在 `Actions → Daily Upstream Sync → Run workflow` 手动执行。
- 每天 03:17 UTC 的定时运行在 `dry_run: true` 时行为相同。

## Driving it with harness.py

Preconditions:

- 刚 launch 好的运行，`doctor` 健康。
- 配置是 `config/repos.example.yml` 的副本（`dry_run: true`）。如果改过，先执行 `& $py $h config --run $run --reset`。

- **基线。** 什么都不改，直接运行。命令：`& $py $h run --run $run --label dry-baseline`。预期 `sync.py exit code: 0`；`alice/widget` 和 `alice/gadget` 是 `UP_TO_DATE`；`alice/legacy` 是 `SKIPPED: Fork repository is archived`。
- **上游新提交。** 上游维护者给 `octo/widget` 推送一个提交。命令：`& $py $h commit --run $run --repo octo/widget --file feature.txt --text "upstream feature 1"`。命令会打印新提交的 sha。
- **检测。** 再运行一次。命令：`& $py $h run --run $run --label dry-detect`。预期 `alice/widget: DRY_RUN: Would push fast-forward <12 位 sha> to main`，退出码 `0`，所有 fork 分支都是 `unchanged`。
- **不推送。** 检查这一步的 API 请求。命令：`Select-String -Path .verify\evidence\$run\*-run-dry-detect\api-requests.jsonl -Pattern '"method": "(POST|PATCH)"'`。预期没有任何输出。
- **冲突预演。** 上游和 Fork 改同一个文件。命令：`& $py $h commit --run $run --repo octo/widget --file shared.txt --text "upstream v2"`，`& $py $h commit --run $run --repo alice/widget --file shared.txt --text "fork edit"`，然后 `& $py $h run --run $run --label dry-conflict`。预期 `alice/widget: CONFLICT: Conflicting files: shared.txt. Merge octo/widget@main into main by hand; the next run then reports up_to_date.`，退出码 `1`，分支都是 `unchanged`。
- **摘要。** 打开 `.verify\evidence\$run\<NN>-run-dry-detect\step-summary.md`。表格里 `alice/widget` 一行的 Status 是 `dry_run`。

## Gotchas

- 摘要顶部按 `Merged`、`Up to date`、`Conflicts`、`Skipped`、`Failed`、`Dry run` 计数。试运行里准备推送的仓库计在 `Dry run` 下。
- 试运行里只要有一个仓库冲突，退出码就是 `1`。其他仓库的状态行仍然会打出来。
- `run` 的输出接到 `Select-Object -First` 等管道后面时，`$LASTEXITCODE` 不可靠。以输出里的 `sync.py exit code:` 行或 `exit-code.txt` 为准。
