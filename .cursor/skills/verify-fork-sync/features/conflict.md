# 合并冲突

Fork 默认分支和上游改了同一处内容时，同步任务会放弃这次合并：不推送自动化分支，也不创建或修改 PR。它在日志和摘要里报告 `conflict` 并列出冲突文件，其他仓库照常处理。冲突属于需要人工处理的情况，不会让任务失败。

## Sub-features

- `conflict-report`：状态 `CONFLICT: Conflicting files: <文件列表>`，摘要表格里状态为 `conflict`，计数 `Conflicts: **1**`。
- `conflict-no-push`：自动化分支和默认分支都不变，没有 `POST`、`PATCH` 请求。
- `conflict-exit-0`：只有冲突时，任务退出码为 `0`。
- `conflict-persists`：冲突没解决之前，每次运行都报同样的冲突。

## How to get to it (user POV)

- 用户在自己的 Fork 默认分支上改了某个文件，上游后来也改了同一行；下一次定时运行或手动 `Run workflow` 时出现。

## Driving it with harness.py

Preconditions:

- 刚 launch 好的运行，`doctor` 健康。
- 执行 `& $py $h config --run $run --dry-run false`。

- **制造冲突。** 上游和 Fork 都改 `shared.txt`。命令：`& $py $h commit --run $run --repo octo/widget --file shared.txt --text "upstream v2"`，`& $py $h commit --run $run --repo alice/widget --file shared.txt --text "fork edit"`。
- **运行（`conflict-report`、`conflict-exit-0`）。** 命令：`& $py $h run --run $run --label conflict`。预期 `sync.py exit code: 0`；`alice/widget: CONFLICT: Conflicting files: shared.txt`；`alice/gadget: UP_TO_DATE`。
- **不写入（`conflict-no-push`）。** 输出里 `alice/widget main (default branch): <sha> unchanged`，没有 `automation/upstream-sync` 这一行，PR 显示 `(none)`。命令：`Select-String -Path .verify\evidence\$run\*-run-conflict\api-requests.jsonl -Pattern '"method": "(POST|PATCH)"'`。预期没有任何输出。
- **摘要。** `.verify\evidence\$run\<NN>-run-conflict\step-summary.md` 里有 `- Conflicts: **1**`，表格里 `alice/widget` 的 Status 是 `conflict`。
- **持续（`conflict-persists`）。** 命令：`& $py $h run --run $run --label conflict-again`。结果和上一步相同。

## Gotchas

- `commit --text` 会把整个文件替换成这一行。两边写不同的文本才会冲突，写相同的文本会干净合并。
- squash 合并过同步 PR 的 Fork，之后上游再改同一个文件时也会冲突，因为 squash 提交和上游提交在 git 看来是两份独立的修改。这种冲突是 squash 带来的，不是 Fork 自己改了文件。
- 冲突时如果 git 合并失败但列不出冲突文件（例如历史不相关），状态会变成 `FAILED: merge failed (possibly unrelated histories)`，退出码 `1`。那是另一条路径，不算冲突。
