# 试运行

`dry_run: true` 时，同步任务会照常读取 Fork 和上游、尝试合并，并在日志和摘要里报告每个 Fork 会发生什么。它不推送任何分支，也不创建或修改 PR。首次部署时，用户就是靠这一步确认选中了哪些 Fork、上游和分支。

## Sub-features

- `dry-detect`：上游有新提交时报 `DRY_RUN: Would push merge <sha> and open/update PR`。
- `dry-up-to-date`：Fork 已包含上游时报 `UP_TO_DATE`。
- `dry-conflict`：试运行也会尝试合并，遇到冲突时报 `CONFLICT` 并列出冲突文件。
- `dry-no-writes`：所有分支不变，没有 `POST`、`PATCH` 请求，也没有 PR。

## How to get to it (user POV)

- 首次部署时保持 `config/repos.yml` 里的 `dry_run: true`，然后在 `Actions → Daily Upstream Sync → Run workflow` 手动执行。
- 每天 03:17 UTC 的定时运行在 `dry_run: true` 时行为相同。

## Driving it with harness.py

Preconditions:

- 刚 launch 好的运行，`doctor` 健康。
- 配置是仓库副本（`dry_run: true`）。如果改过，先执行 `& $py $h config --run $run --reset`。

- **基线。** 什么都不改，直接运行。命令：`& $py $h run --run $run --label dry-baseline`。预期 `sync.py exit code: 0`；`alice/widget` 和 `alice/gadget` 是 `UP_TO_DATE`；`alice/legacy` 是 `SKIPPED: Fork repository is archived`；pull requests 显示 `(none)`。
- **上游新提交。** 上游维护者给 `octo/widget` 推送一个提交。命令：`& $py $h commit --run $run --repo octo/widget --file feature.txt --text "upstream feature 1"`。命令会打印新提交的 sha。
- **检测。** 再运行一次。命令：`& $py $h run --run $run --label dry-detect`。预期 `alice/widget: DRY_RUN: Would push merge <12 位 sha> and open/update PR`，退出码 `0`，所有 fork 分支都是 `unchanged`，pull requests 显示 `(none)`。
- **不写入。** 检查这一步的 API 请求。命令：`Select-String -Path .verify\evidence\$run\*-run-dry-detect\api-requests.jsonl -Pattern '"method": "(POST|PATCH)"'`。预期没有任何输出。
- **冲突预演。** 上游和 Fork 改同一个文件。命令：`& $py $h commit --run $run --repo octo/widget --file shared.txt --text "upstream v2"`，`& $py $h commit --run $run --repo alice/widget --file shared.txt --text "fork edit"`，然后 `& $py $h run --run $run --label dry-conflict`。预期 `alice/widget: CONFLICT: Conflicting files: shared.txt`，退出码 `0`，分支都是 `unchanged`。
- **摘要。** 打开 `.verify\evidence\$run\<NN>-run-dry-detect\step-summary.md`。表格里 `alice/widget` 一行的 Status 是 `dry_run`。

## Gotchas

- 摘要顶部的计数只有 Updated、Up to date、Conflicts、Skipped、Failed 五项，没有 dry_run 计数。`dry_run` 只出现在表格行里，不要因为计数为 0 判定失败。
- Fork 上已经有一个同时包含上游和默认分支的自动化分支时，试运行报的是 `Would ensure PR for existing automation branch`，不是 `Would push merge`。
- 试运行不调用任何 PR 接口，所以「PR 已被用户关闭」这类判断在试运行里看不到。这部分要用 `dry_run: false` 去验证，见 [closed-pr.md](./closed-pr.md)。
- `run` 的输出接到 `Select-Object -First` 等管道后面时，`$LASTEXITCODE` 不可靠。以输出里的 `sync.py exit code:` 行或 `exit-code.txt` 为准。
