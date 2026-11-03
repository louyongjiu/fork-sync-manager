# 同步并开 PR

`dry_run: false` 时，上游有新提交且能干净合并，同步任务会把「Fork 默认分支 + 上游」的普通 merge 推送到 Fork 的 `automation/upstream-sync` 分支，并创建或更新一个指向默认分支的同步 PR，等用户审核。默认分支本身从不被任务改写。

## Sub-features

- `pr-create`：首次发现上游新提交时，推送自动化分支，新建 PR，状态 `UPDATED: pushed sync branch; created_pr | <PR 链接>`。
- `pr-update`：PR 打开期间上游又有新提交时，重建自动化分支，更新同一个 PR，状态 `UPDATED: pushed sync branch; updated_pr | <PR 链接>`。
- `pr-reuse`：没有新变化时再运行，复用现有分支，不推送，状态 `UPDATED: reused sync branch; updated_pr | <PR 链接>`。
- `pr-merged-up-to-date`：用户合并 PR 后再运行，状态 `UP_TO_DATE`。
- `default-branch-untouched`：上面每一步，Fork 默认分支都是 `unchanged`。

## How to get to it (user POV)

- 确认试运行结果后，把 `config/repos.yml` 改成 `dry_run: false` 并提交；之后定时运行或手动 `Run workflow`。
- 用户在 Fork 上打开 `Upstream sync: <上游> (<分支>)` 这个 PR，审核后合并。

## Driving it with harness.py

Preconditions:

- 刚 launch 好的运行，`doctor` 健康。
- 执行 `& $py $h config --run $run --dry-run false`，输出里出现 `dry_run: false`。

- **上游新提交。** 命令：`& $py $h commit --run $run --repo octo/widget --file feature.txt --text "upstream feature 1"`。
- **新建 PR（`pr-create`）。** 命令：`& $py $h run --run $run --label pr-create`。预期退出码 `0`；`alice/widget: UPDATED: pushed sync branch; created_pr | http://127.0.0.1:<端口>/alice/widget/pull/1`；`alice/widget automation/upstream-sync: (absent) -> <sha>`；`alice/widget main (default branch): <sha> unchanged`；PR 列表有 `alice/widget #1 open head=<与自动化分支相同的 sha>`。
- **确认写入。** 命令：`Select-String -Path .verify\evidence\$run\*-run-pr-create\api-requests.jsonl -Pattern '"method": "POST"'`。预期只有一条，路径是 `/repos/alice/widget/pulls`，`"head": "alice:automation/upstream-sync"`，`"base": "main"`，状态码 `201`。
- **复用（`pr-reuse`）。** 什么都不改，再运行一次。命令：`& $py $h run --run $run --label pr-reuse`。预期 `UPDATED: reused sync branch; updated_pr`，自动化分支 `unchanged`，`api-requests.jsonl` 里有一条 `PATCH /repos/alice/widget/pulls/1`，没有 `POST`。
- **上游再推进（`pr-update`）。** 命令：`& $py $h commit --run $run --repo octo/widget --file feature.txt --text "upstream feature 2"`，然后 `& $py $h run --run $run --label pr-update`。预期 `UPDATED: pushed sync branch; updated_pr`；自动化分支从旧 sha 变成新 sha；PR 仍然只有 `#1`，状态 `open`，head 等于新 sha；默认分支 `unchanged`。
- **用户合并后（`pr-merged-up-to-date`）。** 命令：`& $py $h pr merge --run $run --repo alice/widget --number 1 --method merge`，然后 `& $py $h run --run $run --label pr-merged`。预期 `alice/widget: UP_TO_DATE: alice/widget already contains octo/widget@main`，退出码 `0`。
- **证明。** 对比 `pr-create`、`pr-reuse`、`pr-update` 三步的 `state-before.json` 和 `state-after.json`：`alice/widget` 的 `main` 只在 `pr merge` 这一步变化（那是用户的动作），运行中从不变化。

## Gotchas

- 上游再推进后，自动化分支是用强制推送（`--force-with-lease`）重建的，不是在旧分支上追加。新旧 sha 之间没有祖先关系是正常的。
- `pr-reuse` 每次都会 `PATCH` 一次标题和正文，PR 的 `updated_at` 会变化。不要把这当成「有新提交」。
- 合并 PR 之后上游又有新提交时，任务会新建 PR #2，见 [closed-pr.md](./closed-pr.md) 的 `merged-new-upstream`。那是另一条路径，不要写进本功能的通过结论。
- `alice/gadget` 的默认分支是 `develop`，用它验证时 PR 的 `base` 是 `develop`，标题里的上游分支也是 `develop`。
