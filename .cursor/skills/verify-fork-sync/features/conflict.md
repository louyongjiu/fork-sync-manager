# 合并冲突

Fork 的目标分支和上游改了同一处内容时，同步任务中止这次合并。远程目标分支保持运行开始时的提交。日志和摘要里状态是 `conflict`，详情列出冲突文件，并写明要在本地把上游合进目标分支。其他仓库照常处理。只要有仓库是 `conflict`，任务退出码就是 `1`。

## Sub-features

- `conflict-report`：状态 `CONFLICT: Conflicting files: shared.txt. Merge octo/widget@main into main by hand; the next run then reports up_to_date.` 摘要表格里状态为 `conflict`，计数 `Conflicts: **1**`。
- `conflict-no-push`：目标分支 sha 不变。`api-requests.jsonl` 里每条请求的 method 都是 `GET`。
- `conflict-exit-1`：有冲突时，任务退出码为 `1`。
- `conflict-persists`：冲突还在时，下一次运行报同样的冲突，退出码仍是 `1`，目标分支 sha 仍不变。

## How to get to it (user POV)

- 用户在 Fork 目标分支上改了某个文件，上游后来也改了同一行。下一次定时运行或手动 `Run workflow` 时出现。

## Driving it with harness.py

Preconditions:

- 刚 launch 好的运行，`doctor` 健康。
- 执行 `& $py $h config --run $run --dry-run false`。

- **制造冲突。** 上游和 Fork 都改 `shared.txt`。命令：`& $py $h commit --run $run --repo octo/widget --file shared.txt --text "upstream v2"`，`& $py $h commit --run $run --repo alice/widget --file shared.txt --text "fork edit"`。
- **运行（`conflict-report`、`conflict-exit-1`）。** 命令：`& $py $h run --run $run --label conflict`。预期 `sync.py exit code: 1`；`alice/widget: CONFLICT: Conflicting files: shared.txt. Merge octo/widget@main into main by hand; the next run then reports up_to_date.`；`alice/gadget: UP_TO_DATE`。
- **不推送（`conflict-no-push`）。** 输出里 `alice/widget main (default branch): <sha> unchanged`。命令：`Select-String -Path .verify\evidence\$run\*-run-conflict\api-requests.jsonl -Pattern '"method": "(POST|PATCH)"'`。预期没有任何输出。
- **摘要。** `.verify\evidence\$run\<NN>-run-conflict\step-summary.md` 里有 `- Conflicts: **1**`，表格里 `alice/widget` 的 Status 是 `conflict`。
- **持续（`conflict-persists`）。** 命令：`& $py $h run --run $run --label conflict-again`。退出码仍是 `1`，状态行和上一步相同，`main` 仍是 `unchanged`。
- **把文件改成和上游一样后再运行。** 命令：`& $py $h commit --run $run --repo alice/widget --file shared.txt --text "upstream v2"`，然后 `& $py $h run --run $run --label resolved`。预期退出码 `0`，`alice/widget: MERGED: pushed merge commit <sha> to main`。上游那个提交还不是 Fork 历史的祖先，所以这一步是合并提交，不是 `UP_TO_DATE`。

## Gotchas

- `commit --text` 会把整个文件替换成这一行。两边写不同的文本才会冲突，写相同的文本会干净合并。
- 冲突时如果 git 合并失败但列不出冲突文件（例如历史不相关），状态会变成 `FAILED: merge failed (possibly unrelated histories)`，退出码 `1`。那是另一条路径，不算 `conflict`。
- 在本地把上游提交合并进目标分支并推送之后，下一次运行报 `UP_TO_DATE`。上一步只是把文件内容改成一样，所以得到的是 `MERGED`。
