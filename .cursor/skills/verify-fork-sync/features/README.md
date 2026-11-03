# Fork Sync Manager 验证清单

这里维护 Fork Sync Manager 面向用户的行为。驱动前先读本索引，再按对应的功能文件执行。

## 基线前提

- 已按 [../SKILL.md](../SKILL.md) 执行 `launch`，`doctor` 输出 `doctor: healthy`。
- 当前 PowerShell 会话里已定义 `$py`、`$h`、`$run`。
- 种子数据：`alice/widget`、`alice/gadget` 与各自上游同步，`alice/legacy` 已归档，`alice/notes` 不是 Fork。
- 临时配置是仓库 `config/repos.yml` 的副本（`dry_run: true`）。功能文件需要正式同步时，会先执行 `config --dry-run false`。
- 只驱动本次 launch 出来的 run id。

## 驱动约定

- 每个功能都从刚 launch 好的状态开始，除非文件里的 `Preconditions:` 另有说明。功能之间互相影响（PR 编号、分支状态）时，重新 launch，不要手工去还原。
- 用户动作一律通过 harness 的子命令完成：`config`、`commit`、`pr`、`delete-branch`。不要直接改临时目录里的裸仓库或替身状态。
- 命令按原样执行，引号里的文本、分支名、文件名不要改。
- 每次 `run` 都带一个能说明这一步的 `--label`，证据目录按 `NN-run-<label>` 排序。

## 证明与跳过的报告方式

- 一项行为的证明包括：`run` 的屏幕摘要、`NN-run-<label>\` 目录（stdout、退出码、前后分支、PR 状态、API 请求）和 Actions 摘要 `step-summary.md`。
- 涉及写入的行为（推送、建 PR、改 PR），必须在 `state-after.json` 或 `api-requests.jsonl` 里另外确认一次，不能只看状态行。
- 声称「没有写入」时，要给出 `api-requests.jsonl` 里没有 `POST`、`PATCH` 的检索结果，以及所有分支都是 `unchanged`。
- 报告时写明功能 ID（如 `pr-create`）和走的入口。
- 某条路径走不通时，报告尝试的命令和缺少的前提。不要用别的入口代替，然后说「已验证」。

## 功能文件约定

每个功能文件以 H1 标题开头，后面一段话描述用户能看到的行为，然后依次是四个 H2：

1. `Sub-features`：短 ID，每个行为一行。
2. `How to get to it (user POV)`：用户从哪里触发它。
3. `Driving it with harness.py`：以 `Preconditions:` 开头，每条把一个用户动作和一条确切命令、可观察结果配对。
4. `Gotchas`：会浪费时间或让结论失效的陷阱。

清单里不写实现细节，只写用户路径、命令、需要的状态和可观察的证据。

## 功能

- [试运行](./dry-run.md)：`dry_run: true` 下的检测、冲突预演，以及不写入任何东西。
- [同步并开 PR](./sync-pr.md)：推送自动化分支、新建/更新/复用同步 PR、合并后变为 `up_to_date`、默认分支不被改写。
- [合并冲突](./conflict.md)：冲突时报告冲突文件、不推送，任务仍然成功退出。
- [关闭或合并同步 PR 之后](./closed-pr.md)：用户关闭、合并、squash 合并同步 PR 后，后续运行的结果。
- [自动发现与配置](./discovery-config.md)：发现哪些 Fork、归档跳过、`exclude`、`enabled: false`、单仓库覆盖、账号校验、缺少 token、`sync_branch`。
