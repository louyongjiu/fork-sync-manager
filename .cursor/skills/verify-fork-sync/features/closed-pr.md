# 关闭或合并同步 PR 之后

用户可能会关闭同步 PR（不想要这次同步），也可能会合并它（普通 merge 或 squash）。
- PR 被关闭且自动化分支没有新提交时，任务报 `skipped`，不会每天把它重新打开。
- 之后上游有新提交时，任务会给出一个包含新提交的同步 PR：GitHub 允许时重新打开原来的 PR；GitHub 不允许时（PR 已合并，或分支在关闭后被强制推送过），新建一个 PR。
- 合并之后 Fork 先变成 `up_to_date`；上游再前进时，新建一个同步 PR。

## Sub-features

- `closed-unchanged`：PR 被关闭、分支没变时再运行，状态 `SKIPPED: PR was previously closed and sync branch has not changed: <链接>`，PR 保持关闭。
- `closed-new-upstream`：PR 被关闭后上游有新提交时再运行，状态 `UPDATED: pushed sync branch; created_pr | <链接>/pull/2`。#1 保持关闭，#2 打开，并包含新的上游提交。
- `merged-new-upstream`：普通 merge 合并 PR 后，先报 `UP_TO_DATE`；上游有新提交时报 `UPDATED: pushed sync branch; created_pr | <链接>/pull/2`。
- `squash-merged`：squash 合并 PR 后的下一次运行报 `UPDATED: pushed sync branch; created_pr | <链接>/pull/2`。#2 没有文件改动，只补上合并历史。
- `follow-up-reuse`：新 PR 打开后，再运行报 `UPDATED: reused sync branch; updated_pr | <链接>/pull/2`。

## How to get to it (user POV)

- 在 Fork 的同步 PR 页面点 `Close pull request`。
- 在 Fork 的同步 PR 页面选 `Create a merge commit` 或 `Squash and merge` 合并。如果仓库开启了 `Automatically delete head branches`，合并后 GitHub 会删除自动化分支。
- 之后照常等定时运行，或手动 `Run workflow`。

## Driving it with harness.py

Preconditions:

- 刚 launch 好的运行，`doctor` 健康。
- 执行 `& $py $h config --run $run --dry-run false`。
- 每条路径都需要一个刚创建的同步 PR。下面用 `alice/gadget` 走关闭路径，用 `alice/widget` 走合并路径，两者互不影响。`squash-merged` 要另外 launch 一次运行。

- **建 PR。** 命令：`& $py $h commit --run $run --repo octo/gadget --file g.txt --text "gadget 1"`，`& $py $h commit --run $run --repo octo/widget --file feature.txt --text "widget 1"`，然后 `& $py $h run --run $run --label create`。预期两个 Fork 都是 `UPDATED: pushed sync branch; created_pr`，各自有 `#1 open`。
- **关闭后不变（`closed-unchanged`）。** 命令：`& $py $h pr close --run $run --repo alice/gadget --number 1`，然后 `& $py $h run --run $run --label closed-unchanged`。预期 `alice/gadget: SKIPPED: PR was previously closed and sync branch has not changed`，`alice/gadget #1 closed`，自动化分支 `unchanged`。
- **关闭后上游前进（`closed-new-upstream`）。** 命令：`& $py $h commit --run $run --repo octo/gadget --file g.txt --text "gadget 2"`，然后 `& $py $h run --run $run --label closed-new-upstream`。预期 `alice/gadget: UPDATED: pushed sync branch; created_pr | http://127.0.0.1:<端口>/alice/gadget/pull/2`，退出码 `0`，PR 列表里有 `alice/gadget #1 closed` 和 `alice/gadget #2 open`，#2 的 head 等于新的自动化分支 sha。
- **确认 API 调用。** 命令：`Select-String -Path .verify\evidence\$run\*-run-closed-new-upstream\api-requests.jsonl -Pattern '"(PATCH|POST)", "path": "/repos/alice/gadget/'`。预期先有一条 `PATCH /repos/alice/gadget/pulls/1`，状态码 `422`，再有一条 `POST /repos/alice/gadget/pulls`，状态码 `201`。
- **合并后（`merged-new-upstream`）。** 命令：`& $py $h pr merge --run $run --repo alice/widget --number 1 --method merge`，再执行 `& $py $h run --run $run --label after-merge`，预期 `alice/widget: UP_TO_DATE`。然后执行 `& $py $h commit --run $run --repo octo/widget --file feature.txt --text "widget 2"` 和 `& $py $h run --run $run --label merged-new-upstream`，预期 `alice/widget: UPDATED: pushed sync branch; created_pr | http://127.0.0.1:<端口>/alice/widget/pull/2`。用 `Select-String -Path .verify\evidence\$run\*-run-merged-new-upstream\api-requests.jsonl -Pattern '"(PATCH|POST)", "path": "/repos/alice/widget/'` 检查：只有一条 `POST /repos/alice/widget/pulls`，状态码 `201`，没有发给 `alice/widget` 的 `PATCH`（同一次运行里，`alice/gadget` 的打开 PR 会照常收到 `PATCH`）。
- **新 PR 之后（`follow-up-reuse`）。** 命令：`& $py $h run --run $run --label follow-up`。预期 `alice/widget: UPDATED: reused sync branch; updated_pr | .../alice/widget/pull/2` 和 `alice/gadget: UPDATED: reused sync branch; updated_pr | .../alice/gadget/pull/2`。
- **squash 合并后（`squash-merged`）。** 在新 launch 的运行里依次执行：`config --dry-run false`；`commit --repo octo/widget --file feature.txt --text "w1"`；`run --label create`；`pr merge --repo alice/widget --number 1 --method squash --delete-branch`；`run --label after-squash`。预期 `alice/widget: UPDATED: pushed sync branch; created_pr | .../alice/widget/pull/2`，退出码 `0`。接着执行 `pr merge --repo alice/widget --number 2 --method merge` 和 `run --label after-follow-up`，预期 `alice/widget: UP_TO_DATE`。
- **证明。** 每条路径都看 `report.txt` 的状态行、`exit-code.txt`，以及 `api-requests.jsonl` 里每个 `PATCH`、`POST` 的状态码。

## Gotchas

- 关闭后，自动化分支总是用强制推送重建的，所以 GitHub 几乎总是拒绝重新打开原来的 PR。实际看到的通常是先 `PATCH` 得到 `422`，再新建 #2。新建 PR 时状态行写 `created_pr`，不写 `reopened_pr`。
- 合并过的 PR 不会收到 `PATCH`，任务直接新建 PR。
- squash 合并之后的 #2 没有文件改动，只有合并历史。如果一直不合并 #2，等上游再改同一个文件时就会冲突，见 [conflict.md](./conflict.md)。
- 替身按 GitHub 文档实现了两条 422 规则：已合并的 PR 不能重新打开；关闭后分支被强制推送或重建，PR 也不能重新打开。在真实 GitHub 上，只能用一个可以随意改动的 Fork 来复现。
- 关闭路径和合并路径分别放在两个 Fork 上。如果在同一个 Fork 上先关闭再合并，PR 编号和状态会互相干扰。
