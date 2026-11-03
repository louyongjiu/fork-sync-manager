# Fork Sync Manager 验证清单

这里维护 Fork Sync Manager 面向用户的行为。驱动前先读本索引，再按对应的功能文件执行。

## 基线前提

- 已按 [../SKILL.md](../SKILL.md) 执行 `launch`，`doctor` 输出 `doctor: healthy`。
- 当前 PowerShell 会话里已定义 `$py`、`$h`、`$run`。
- 种子数据：`alice/widget`、`alice/gadget` 与各自上游同步，`alice/legacy` 已归档，`alice/notes` 不是 Fork。
- 临时配置是仓库 `config/repos.yml` 的副本（`dry_run: true`）。功能文件需要正式同步时，会先执行 `config --dry-run false`。
- 只驱动本次 launch 出来的 run id。

## 驱动约定

- 每个功能都从刚 launch 好的状态开始，除非文件里的 `Preconditions:` 另有说明。功能之间互相影响（分支状态）时，重新 launch，不要手工去还原。
- 用户动作一律通过 harness 的子命令完成：`config`、`commit`。不要直接改临时目录里的裸仓库或替身状态。
- 命令按原样执行，引号里的文本、分支名、文件名不要改。
- 每次 `run` 都带一个能说明这一步的 `--label`，证据目录按 `NN-run-<label>` 排序。

## 证明与跳过的报告方式

- 一项行为的证明包括：`run` 的屏幕摘要、`NN-run-<label>\` 目录（stdout、退出码、前后分支、API 请求）和 Actions 摘要 `step-summary.md`。
- 涉及写入的行为（推送到目标分支），必须在 `state-after.json` 的分支 sha 里另外确认一次，不能只看状态行。干净合并的 `api-requests.jsonl` 里 method 都是 `GET`。
- 要证明远程分支保持原提交，给出 `api-requests.jsonl` 里每条请求的 method 都是 `GET`，以及所有分支都是 `unchanged`。
- 报告时写明功能 ID（如 `ff-push`）和走的入口。
- 某条路径走不通时，报告尝试的命令和缺少的前提。不要用别的入口代替，然后说「已验证」。

## 功能文件约定

每个功能文件以 H1 标题开头，后面一段话描述用户能看到的行为，然后依次是四个 H2：

1. `Sub-features`：短 ID，每个行为一行。
2. `How to get to it (user POV)`：用户从哪里触发它。
3. `Driving it with harness.py`：以 `Preconditions:` 开头，每条把一个用户动作和一条确切命令、可观察结果配对。
4. `Gotchas`：会浪费时间或让结论失效的陷阱。

清单里不写实现细节，只写用户路径、命令、需要的状态和可观察的证据。

## 功能

- [试运行](./dry-run.md)：`dry_run: true` 下的检测和冲突预演。远程分支保持原提交。冲突时退出码是 `1`。
- [合并到目标分支](./merge.md)：快进或生成合并提交，推送到目标分支。再运行一次变为 `up_to_date`。
- [合并冲突](./conflict.md)：报告冲突文件，目标分支保持原提交，任务退出码 `1`。
- [自动发现与配置](./discovery-config.md)：发现哪些 Fork、归档跳过、`exclude`、`enabled: false`、单仓库覆盖、账号校验、缺少 token。
