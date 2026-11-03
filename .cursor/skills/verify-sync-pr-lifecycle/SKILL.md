---
name: verify-sync-pr-lifecycle
description: 用 TLA+ 模型和 TLC 模型检查器，验证 scripts/sync.py 在多次每日运行中处理同步 PR 和自动化分支的承诺：运行不因以前的同步 PR 而失败、从不推送 Fork 默认分支、用户关闭的 PR 不会原样重开、报告 updated 时一定有包含上游的打开 PR；并把模型路径放到真实 sync.py 上重放。修改 sync_one、ensure_pull_request、list_sync_prs、api_request、is_ancestor、remote_branch_exists，或修改 settings.sync_branch 的处理之后使用。
---

# 验证同步 PR 的生命周期

## 验证什么

`scripts/sync.py` 每天跑一次。每个 Fork 都会经历这样一串事件：上游有新提交 → 运行推送 `automation/upstream-sync` 并开 PR → 用户合并、关闭或不管 → 上游又有提交 → 再运行……单元测试很难覆盖这些事件的各种先后顺序。本 skill 把这条生命周期写成一个状态机模型，让 TLC 在给定界限内穷举所有事件顺序，检查下面四条承诺。

| 规则 | 一句话 | 违反时的后果 |
|---|---|---|
| `NoFailedRun` | 只要 GitHub 和网络正常，运行不会因为以前开过的同步 PR 而失败。 | 这个 Fork 每天都报 `failed`，Actions 任务一直失败，新的上游提交再也进不了 PR。 |
| `DefaultBranchNeverPushed` | 自动化从不推送 Fork 的默认分支。 | 上游代码不经审核就进了默认分支。 |
| `ClosedPrNotReopenedUnchanged` | 用户关闭的 PR，在自动化分支还停在关闭时那个提交时，不会被重新打开。 | 用户关掉的 PR 每天又冒出来。 |
| `UpdatedRunLeavesOpenPrWithUpstream` | 运行报告 `updated` 时，Fork 上有一个打开的同步 PR，并且包含这次运行取到的上游提交。 | 日志说已同步，实际 PR 缺新提交或根本不存在。 |

`TypeOK` 检查变量的取值范围，`NoDeadlock` 由 TLC 自动检查。

## 工具和原因

- **TLA+ / TLC 1.7.4**：问题的核心是“事件以什么顺序发生时会出错”，模型检查器最擅长回答这类问题。TLC 只需要 Java 11 及以上（本机是 Java 17）。`scripts/check.py` 会把 jar 下载到 `%LOCALAPPDATA%\fork-sync-verify\tools\`，并校验 sha256。
- **在真实代码上重放**：`scripts/replay.py` 用 [`verify-fork-sync`](../verify-fork-sync/SKILL.md) 的 harness，把模型里的路径一步步放到真实 `sync.py` 上执行（本地裸仓库加 GitHub API 替身），对比每一步的结果。模型和代码一旦对不上，这里会报出来。

## 覆盖的代码与同步点

模型描述的是 `scripts/sync.py` 里的这些代码：`GitHubApiError`、`api_request`、`is_ancestor`、`remote_branch_exists`、`list_sync_prs`、`ensure_pull_request`、`sync_one`（只描述其中 `dry_run: false` 的路径）。

- 同步提交：**无**。仓库目前没有任何提交。上次确认时 `scripts/sync.py` 整个文件的 sha256 为 `d60d0ef628c6f5e2…`，确认时间写在 [`coverage.json`](coverage.json) 的 `confirmed_at` 里。
- 每个函数和类源码的 sha256 也记在 `coverage.json` 里，`check.py` 每次都会对比。
- 确认模型确实描述了新代码之后，执行 `check.py --confirm`，把 `coverage.json` 更新为当前代码，并记下当时的 `HEAD`。没有读完代码和模型，不要执行 `--confirm`。

## 模型和代码的对应

| 模型（`model/SyncPrLifecycle.tla`） | 代码或 GitHub 规则 |
|---|---|
| `IsAncestor(a, b)` | `is_ancestor()`：`git merge-base --is-ancestor` |
| `SyncBranchIsTarget` | `sync_one` 第 291–295 行：`sync_branch == target_branch` 时抛出 `SyncError`，发生在所有 git 命令之前 |
| `RunFetchesAndPushes`：上游已在目标分支里 | `sync_one` 第 322 行，返回 `up_to_date` |
| `Reusable(t, s)` | `sync_one` 第 340–345 行，变量 `reusable` |
| `RunFetchesAndPushes`：复用分支 | 第 347–349 行，不推送 |
| `RunFetchesAndPushes`：冲突 | 第 357–365 行，`merge --abort` 后返回 `conflict` |
| `RunFetchesAndPushes`：重建并推送 | 第 351–386 行：`checkout -B` 目标分支，`merge --no-ff` 上游，然后推送（分支已存在时用 `--force-with-lease`） |
| `RunUpdatesPr` / `EnsurePr` | `sync_one` 第 391–425 行和 `ensure_pull_request` 第 170–224 行 |
| `pr` 只记最新一个 PR | `list_sync_prs` 按 head 和 base 过滤，`ensure_pull_request` 按 `updated_at` 倒序，只取第一个打开的或第一个关闭的。新建的 PR 是最新的，之后总是被先取到 |
| `ClosedPrUnchanged` | 第 196 行 `old_sha == head_sha` |
| `CanReopen` 分支 | 第 201–208 行：只对没有 `merged_at` 的 PR 发 `PATCH state=open`。GitHub 规则：已合并的 PR 不能重新打开；关闭后分支被删除、强制推送或重建，也不能重新打开（422） |
| `WhenReopenImpossible` = `CreatePr` | 第 201 行跳过已合并的 PR，第 209–211 行吞掉 422，然后走到第 213–224 行的 `POST /pulls` |
| `NoCommitsBetween` | GitHub 规则：head 相对 base 没有新提交时，`POST /pulls` 返回 422 |
| `Finish("failed", …)` | 其他 HTTP 错误由 `api_request` 抛出 `GitHubApiError`（它是 `SyncError` 的子类），`main()` 记为 `failed` |
| `YouMergePr("merge" / "squash", deleteBranch)` | GitHub 的 “Create a merge commit” / “Squash and merge”，以及 “Automatically delete head branches” |
| `YouClosePr` | 用户关闭 PR，GitHub 固定 `head.sha` |

## 界限与假设

- **界限**（`SyncPrLifecycle.cfg`）：上游最多 2 个新提交，用户最多向默认分支推 1 个提交，最多 4 次运行，共 12270 个状态。通过只代表在这个界限内成立。
- **原子步骤**：一次运行分成两步。第一步是全部 git 操作（取分支、判断、推送），第二步是 PR 的列表请求，加上紧随其后的 `PATCH` 和 `POST`。用户和上游的动作可以插在这两步之间，但不会插进某一步内部（这两种情况在代码里都只隔几毫秒到几秒）。
- **只建模一个 Fork**：各个 Fork 在 `main()` 里依次独立处理。workflow 的 `concurrency` 分组保证同一时间只有一次运行。
- **冲突**：模型不记录文件内容，每次合并都可能冲突，所以模型里的冲突比现实多。这只会增加行为，不会掩盖问题。
- **GitHub 行为**：上表里的 GitHub 规则来自 GitHub 的公开行为（422 `state cannot be changed`、`No commits between`），本地替身也按这些规则实现。如果真实 GitHub 不同，第 201–211 行的分支需要重新确认。
- **没有建模的部分**：`dry_run: true`（不推送也不调用 PR 接口）、自动发现和配置解析、网络和权限错误、GitHub 限速、用户手动往自动化分支推送、用户手动为同一个分支另开 PR、非 Fork 的显式配置仓库。

## 运行

在仓库根目录，用项目 venv 运行（首次需要联网下载 TLC）：

```powershell
.\.venv\Scripts\python.exe .cursor\skills\verify-sync-pr-lifecycle\scripts\check.py
.\.venv\Scripts\python.exe .cursor\skills\verify-sync-pr-lifecycle\scripts\replay.py
```

- `check.py` 大约 10–20 秒。它按 `CHECKS` 表把每个配置的每条规则单独跑一遍 TLC，打印“预期 / 实际 / 状态数 / 最短路径”。
  - 退出码 `0`：全部符合预期，被覆盖的代码没有变。
  - 退出码 `1`：有结论和预期不一致，或者 TLC 出错。
  - 退出码 `2`：结论都符合预期，但被覆盖的代码变了，需要重新对照。
  - TLC 的完整输出保存在 `.verify\formal\<时间>\`。
- `replay.py` 大约 1 分钟。它并行跑 5 个场景，每个场景单独 launch 一套本地环境，跑完自动 cleanup，证据保存在 `.verify\evidence\replay-<场景>-…\`。退出码 `0` 表示每一步都和模型一致。
- 两个脚本都支持 `--sync-py <副本>`，用来检查一份修改过的 `sync.py`，不用动工作区里的文件。`replay.py --only <场景>` 只跑指定的场景。

当前应当看到的结果：

| 配置 | 规则 | 结论 | 含义 |
|---|---|---|---|
| `SyncPrLifecycle.cfg` | 五条全部 | 成立 | 当前代码在界限内满足四条承诺 |
| `SameBranch.cfg` | `DefaultBranchNeverPushed` | 成立 | `sync_branch` 设为 `main` 时，默认分支不被推送 |
| `SameBranch.cfg` | `NoFailedRun` | 违反 | 设计如此：这种配置会让该 Fork 报 `failed` |
| `BreakReopenFallback.cfg` | `NoFailedRun` | 违反 | 去掉第 201–211 行时能抓到 |
| `BreakSyncBranchCheck.cfg` | `DefaultBranchNeverPushed` | 违反 | 去掉第 291–295 行时能抓到 |
| `BreakClosedCheck.cfg` | `ClosedPrNotReopenedUnchanged` | 违反 | 去掉第 196 行时能抓到 |
| `BreakReuseCheck.cfg` | `UpdatedRunLeavesOpenPrWithUpstream` | 违反 | 去掉第 344 行时能抓到 |

## 当前结论

当前代码在界限内满足四条承诺。`replay.py` 在真实代码上逐步确认的路径：

| 场景 | 路径 | 真实代码的结果 |
|---|---|---|
| `squash-merged` | 建 PR #1 → squash 合并 → 运行 → 再运行 | 新建 #2（包含上游）→ 复用分支更新 #2 |
| `merge-then-upstream` | 建 #1 → 普通合并 → 运行 → 上游前进 → 运行 → 再运行 | `up_to_date` → 新建 #2（包含上游）→ 复用分支更新 #2 |
| `close-then-upstream` | 建 #1 → 关闭 → 运行 → 上游前进 → 运行 | `skipped` → `PATCH` 得到 422 后新建 #2（包含上游） |
| `upstream-while-open` | 建 #1 → 上游前进 → 运行 → 再运行 | 重建分支更新 #1 → 复用分支更新 #1，#1 始终包含最新上游 |
| `sync-branch-is-default` | `sync_branch: main` → 上游前进 → 运行 | `FAILED: settings.sync_branch 'main' is the target branch of alice/widget`，默认分支不变，没有写入请求 |

每次运行都会额外检查 `alice/widget` 的默认分支有没有被运行改写。

## 改了代码之后

1. **改动是否碰到了覆盖的代码？** 跑 `check.py`。退出码 `2` 并列出函数名，说明覆盖的代码变了。除了函数名，也要看有没有新代码写到同样的状态：推送分支、调用 PR 接口、改变 `reusable` 或 `head_sha` 的取值、改变 `api_request` 抛出的异常。函数改名或拆分之后，仍然算覆盖范围内的代码，`COVERED` 也要跟着改。
2. **先重放。** 跑 `replay.py`（如果改动还没写进工作区，就加 `--sync-py`）。`MISMATCH` 说明代码已经不再按模型走了。
3. **按代码更新模型，不要按结果去调。** 对照上面的对应表，修改对应的动作或运算，并同步更新模型注释里的行号。代码里一个不可分的步骤，模型里也只能是一步。只有在改动有明确意图（提交说明、README、测试里写明承诺变了）时，才去改规则，并在本文件里写下旧说法、新说法和依据。
4. **重跑 `check.py`。**
   - 规则违反时，用 `/statechart` 读最短路径，再用 harness 一步步重放到真实代码上。路径上的每一步代码都真的会做，就是 bug：报告路径和后果，不要改代码，除非用户要求。有一步代码不会做，就是模型错了：修模型，并说明错在哪里。
   - 规则通过时，在 `model/SyncPrLifecycleVariants.tla` 里加一个去掉新检查的变体，再加一个 `Break*.cfg` 和一行 `CHECKS`，确认 TLC 能抓到；同时在 `sync.py` 副本里去掉这项检查，用 `replay.py --sync-py` 确认重放也能抓到。
5. **改预期。** 行为有意变化时，相应的 `CHECKS` 和 `replay.py` 里的 `SCENARIOS` 预期都要改成新代码的行为，同时更新本文件的“当前结论”。
6. **记录同步点。** 全部对上之后，执行 `check.py --confirm`，并更新本文件“覆盖的代码与同步点”一节。

## 用 /statechart 读结果

每个 TLC 结果都用 pstack 的 `/statechart` skill 来读。它会把违反规则的情况画成用户能看懂的最短路径；规则都成立时，它会画出状态之间怎么转换。本 skill 为它准备的文件：

- `model/chart-view.mjs`：状态和步骤的中文说法。
- `model/Overview.cfg`：更小的界限（121 个状态），能画出完整的状态图。`SyncPrLifecycle.cfg` 有 12270 个状态，超出它的 500 状态画图上限。规则都成立时，它不会为 `SyncPrLifecycle.cfg` 画图，只给出报告。
- 读某个 `Break*.cfg` 的最短路径时，把 `SyncPrLifecycleVariants.tla` 作为规格文件传给它。

`/statechart` 的用法以它自己的 SKILL.md 为准，命令和路径不要抄进这里。2026-11-02 在这台 Windows 机器上用的是 pstack 1ba91fc 版本，碰到两点：它的外层启动程序要用 `/bin/sh` 结束进程组，Windows 上没有；它按 `\n` 切分 TLC 输出，而 Windows 上的 Java 默认输出 `\r\n`。下次如果在 Windows 上又遇到，先确认这两点。

## 证明规则能失败

每条规则都在一个去掉对应检查的变体上被抓到过：

| 规则 | 变体 | TLC 给出的最短路径 | 在真实代码副本上（`replay.py --sync-py`） |
|---|---|---|---|
| `NoFailedRun` | `BreakReopenFallback.cfg`：去掉第 201–211 行的 `merged_at` 判断和 422 回退 | 建 PR → 你合并 → 运行推送同步分支 → 重新打开被拒，运行失败 | `squash-merged`、`merge-then-upstream`、`close-then-upstream` 三个场景都出现 `MISMATCH`，得到 `FAILED ... HTTP 422` |
| `DefaultBranchNeverPushed` | `BreakSyncBranchCheck.cfg`：去掉第 291–295 行，`sync_branch` 设为 `main` | 上游推进 → 运行推送到 `main` | `sync-branch-is-default` 出现 `MISMATCH`，默认分支被改写 |
| `ClosedPrNotReopenedUnchanged` | `BreakClosedCheck.cfg`：去掉第 196 行 | 建 PR → 你关闭 → 运行复用分支 → 重新打开 | `close-then-upstream` 在 `closed-unchanged` 这一步出现 `MISMATCH`，得到 `reopened_pr` |
| `UpdatedRunLeavesOpenPrWithUpstream` | `BreakReuseCheck.cfg`：去掉第 344 行 | 上游推进 → 运行推送 → 上游再推进 → 运行复用旧分支并报告 `updated` | 没有在副本上重放过 |
