---
name: verify-fork-sync
description: 在本机按 GitHub Actions「Daily Upstream Sync」的方式运行 Fork Sync Manager 的 scripts/sync.py，用本地裸仓库和本地 GitHub API 替身驱动同步、冲突、手工处理冲突后的再次运行、开发分支上的审查 Pull Request、自动发现与配置等用户路径，并留下 stdout、退出码、分支前后对比、Pull Request 列表和 API 请求记录作为证据。修改 scripts/sync.py、config/repos.yml、.github/workflows/daily-sync.yml 之后，需要证明行为时使用。
---

# 验证 Fork Sync Manager

用户接触的界面只有一个：GitHub Actions 每天（或手动 `Run workflow`）在管理仓库里执行 `python scripts/sync.py`。用户能看到的结果有：
- Actions 日志里的状态行（`MERGED:`、`UP_TO_DATE:`、`CONFLICT:`、`SKIPPED:`、`FAILED:`、`DRY_RUN:`、`ERROR:`）；
- 任务摘要（`GITHUB_STEP_SUMMARY` 写入的 Markdown）；
- 任务退出码：`0` 表示没有仓库失败或冲突；`1` 表示有仓库失败或冲突，或列出 Fork 时出错；`2` 表示配置错误或缺少 token；
- 各个 Fork 的目标分支。干净合并之后，这个分支包含上游提交。
- 配置了开发分支时，从目标分支开向开发分支的 Pull Request。自动合入的上游提交在目标分支上。开发分支还不存在时，脚本用合并前的目标提交创建它。之后的新提交来自这个 Pull Request，或者来自手工推送。

本 skill 用 `scripts/harness.py` 在本机复现这条路径，具体分工如下：

| 部分 | 本机验证时 |
|---|---|
| `scripts/sync.py` | 真实代码。每次 `run` 都把当前文件原样复制到临时根目录再执行，证据里记录 sha256 |
| git、Python、PyYAML | 真实程序，使用项目 `.venv` |
| GitHub REST API | `fake_github.py` 替身，通过 `GITHUB_API_URL` 接入（workflow 运行时同样读取这个变量） |
| `https://github.com/<owner>/<repo>.git` | 通过临时 `GIT_CONFIG_GLOBAL` 里的 `url.insteadOf` 指到本地裸仓库 |
| 真实网络 | `HTTPS_PROXY` 指向 `127.0.0.1:9`，任何误发到 github.com 的请求都会立即失败 |

替身回答 `scripts/sync.py` 实际调用的接口：
- `GET /user/repos` 返回管理账号名下的仓库。本机只有 `alice` 的 4 个仓库，一页。
- `GET /repos/{owner}/{repo}` 返回仓库元数据。Fork 的响应里带 `parent`。
- `GET /repos/{owner}/{repo}/pulls` 按 `state`、`head`、`base` 过滤。`state=all` 同时返回打开和关闭的。结果按编号从大到小。
- `POST /repos/{owner}/{repo}/pulls` 创建 Pull Request。同一个 head 和 base 已经有打开的 Pull Request，或者 head 上没有 base 缺少的提交时，返回 422。
- 打开的 Pull Request，`head.sha` 跟着 head 分支走。关闭之后，`head.sha` 停在关闭时的提交。
- 请求没有带约定的 token，或 token 不对，返回 401，正文是 `Bad credentials`。
- 每个对外请求追加一行到 `api-requests.jsonl`，记下方法、路径、状态码和请求体字段。

git 推送不经过这个 HTTP 替身。推送结果写在本地裸仓库的分支上。

## 前置

在仓库根目录执行（Windows PowerShell 5.1）。首次需要先建 venv：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

下面所有命令都用这两个变量：

```powershell
$py = ".\.venv\Scripts\python.exe"
$h  = ".cursor\skills\verify-fork-sync\scripts\harness.py"
```

## Launch

```powershell
& $py $h launch --run-id <自取的 id>
$run = "<同一个 id>"
```

不带 `--run-id` 时会自动生成一个，并打印在 `run id:` 那一行。看到最后一行 `READY`，才说明替身 API 已经在回答请求，这时可以开始驱动。一次 launch 大约 10 秒，会从空目录建好以下内容：

| 仓库 | 说明 |
|---|---|
| `octo/widget` | 上游，默认分支 `main`，文件 `README.md`、`shared.txt`（内容 `upstream v1`） |
| `octo/gadget` | 上游，默认分支 `develop` |
| `octo/legacy` | 上游，默认分支 `main` |
| `alice/widget` | `octo/widget` 的 Fork，和上游同步 |
| `alice/gadget` | `octo/gadget` 的 Fork，默认分支 `develop`，和上游同步 |
| `alice/legacy` | `octo/legacy` 的 Fork，已归档 |
| `alice/notes` | `alice` 自己的普通仓库，不是 Fork |

管理账号固定为 `alice`：`GITHUB_REPOSITORY=alice/fork-sync-manager`，`GITHUB_REPOSITORY_OWNER=alice`。临时根目录里放的是仓库 `config/repos.yml` 的副本，状态与仓库里一致，也就是 `dry_run: true`。

## Doctor

任何结果看起来不对时先跑这一条。它只读，不改任何状态：

```powershell
& $py $h doctor --run $run
```

全部为 `OK` 并以 `doctor: healthy` 结尾，才值得继续驱动。它检查的内容：
- 端口上的进程属于本次运行（run id 和 pid 都对得上）；
- 带 token 访问返回 200，不带 token 返回 401；
- git 访问 `https://github.com/alice/widget.git` 时落到本地裸仓库；
- 解释器是项目 `.venv`；
- 真实 https 流量被拦住。

最后一行 `INFO` 会给出当前 `scripts/sync.py` 的 sha256，以及上次 `run` 用的 sha256，用来确认跑的是你改过的那一版。

## Drive

所有命令都以 `& $py $h <子命令> --run $run` 开头：

| 用户或外部动作 | 命令 |
|---|---|
| 修改管理仓库的 `config/repos.yml` | `config --dry-run true\|false`、`--exclude <名> ...`、`--owner <名>`、`--max-repos <n>`、`--discover true\|false`、`--settings-json '<JSON 对象>'`（原样合并进 `settings:`，用来写字符串布尔值或未知键）、`--repos-json '<JSON 列表>'`、`--reset`（重新复制仓库里的版本，可以和其他参数一起用） |
| 设置开发分支 | `variable --dev-branch <名字>`。空字符串清除。这个值留在本次运行的状态里，之后每次 `run` 都作为 `FORK_SYNC_DEV_BRANCH` 传给 `scripts/sync.py`，并写进该步的 `command.txt` |
| 上游维护者或用户推送提交 | `commit --repo <owner/repo> [--branch <分支>] [--from <已有分支>] --file <路径> --text <整行内容>`（文件整体替换为这一行） |
| 列出 Pull Request | `pr list` |
| 关闭 Pull Request | `pr close --repo <owner/repo> --number <编号>` |
| 合并 Pull Request | `pr merge --repo <owner/repo> --number <编号> --method merge` 或 `--method squash`。`merge` 在 base 上生成合并提交。`squash` 在 base 上生成一个提交，head 分支不动 |
| Actions 运行一次同步 | `run --label <标签> [--no-token \| --token <错误的值>] [--sync-py <另一份 sync.py>]` |
| 只看当前状态 | `state --label <标签>` |

`run` 的输出包括：
- `sync.py exit code`；
- 每个仓库的状态行；
- 每个 Fork 各分支在运行前后的 sha。默认分支变到一个不包含旧提交的提交时，行尾出现 `<-- DEFAULT BRANCH REWRITTEN`。判断方法是对本地裸仓库跑 `git merge-base --is-ancestor`，看旧 sha 是不是新 sha 的祖先。
- 本次运行的 API 请求数。
- 运行结束时的 Pull Request。没有时这一节是 `(none)`。

harness 本身的退出码等于 `sync.py` 的退出码；harness 自己出错时返回 `3`。

具体路径怎么走，见 [features/README.md](features/README.md) 和其中每个功能文件。

## 行为快照（重构时用）

`scripts/pin.py` 把 22 个固定场景在真实 `sync.py` 上各跑一遍，记成一份规范化文本。每个场景单独 launch 一次运行，4 个并行，结束后自动 cleanup，整体约 3 分钟。场景覆盖：
- 试运行，其中冲突那一步退出码是 `1`；
- Fork 自己有提交时生成合并提交，再运行变为 `up_to_date`；
- 纯镜像 Fork 快进，目标和上游指向同一个提交；
- 冲突持续两次；
- 冲突之后把文件改成和上游相同的文本，再运行得到 `merged`；
- 排除和停用；
- 单仓库覆盖上游分支；
- 账号校验；
- 缺少 token；
- 错误的 token；
- 停用但访问不到的仓库；
- 未知键；
- 字符串布尔值；
- `max_repos`；
- 开发分支从合并前的目标提交创建，并打开审查 Pull Request。已经打开的 Pull Request 保持打开，脚本不另建一个，head 跟着目标分支走；
- 关闭或 Squash 合并之后，目标分支还停在当时的提交上时，脚本不开新的 Pull Request。目标分支前进后才开下一个；
- 开发分支已经有自己的提交时，这次运行不创建分支，也不改那个提交，审查 Pull Request 的 base 是这个分支；
- `FORK_SYNC_DEV_BRANCH` 和某个 Fork 的目标分支同名时，该仓库失败，其他仓库继续；
- 试运行只在详情里写出将要创建的开发分支和将要打开的 Pull Request，请求只有 `GET`；
- 不合法的 `FORK_SYNC_DEV_BRANCH` 以退出码 `2` 结束；
- 配置文件里的 `dev_branch` 是未知键，以退出码 `2` 结束。

```powershell
& $py .cursor\skills\verify-fork-sync\scripts\pin.py --out .verify\pin\before.txt
& $py .cursor\skills\verify-fork-sync\scripts\pin.py --sync-py <另一份 sync.py> --out .verify\pin\after.txt
& $py .cursor\skills\verify-fork-sync\scripts\pin.py --compare .verify\pin\before.txt .verify\pin\after.txt
```

记录里每一步都有：
- `sync.py` 的退出码、stdout、stderr 和任务摘要；
- 每个 API 请求；
- 每个分支指向的提交、树和父提交；
- 每个 Pull Request 的编号、状态、head 分支和提交、base 分支、标题。没有 Pull Request 时这一节是 `(none)`。

commit 的 sha 每次运行都不同，所以每个场景里按首次出现的顺序把它们改名成 `C1`、`C2`……树的 sha 只由内容决定，保持原样（大写），合并结果的内容有任何不同都能看出来。同一份代码连跑两次，记录应当完全一致；`--compare` 在两份记录有差异时以退出码 1 结束，并打印差异。要对比改动前的版本，可以用 `git show <提交>:scripts/sync.py` 存一份副本，再传给 `--sync-py`。

## Evidence

证据目录是 `.verify\evidence\<run id>\`，已经在 `.gitignore` 中。每次 `run` 写入一个 `NN-run-<标签>\` 子目录，包含：

- `command.txt`：工作目录、命令行、所用 `sync.py` 的路径和 sha256。本次运行设了 `FORK_SYNC_DEV_BRANCH` 时，文件末尾还有这一行变量
- `repos.yml`：本次运行使用的配置
- `stdout.txt`、`stderr.txt`、`exit-code.txt`
- `step-summary.md`：Actions 任务摘要的原文
- `state-before.json`、`state-after.json`：所有仓库的分支和 sha
- `api-requests.jsonl`：本次运行发出的每个 API 请求（方法、路径、状态码、请求体字段）
- `report.txt`：和屏幕输出相同的摘要

`state` 写入 `NN-state-<标签>\state.json`。`cleanup` 时会再把 `server.log`、完整的 `api-requests.jsonl` 和 `run.json` 复制到证据目录根下。

证明一项行为时：
- 走用户真实会走的路径：改配置、推送提交，然后 `run`。不要直接改替身的内部状态。
- 同时保留动作和结果：每一步都有自己的 `NN-` 目录，按顺序对得上。
- 验证副作用：比对 `state-before.json` 和 `state-after.json` 里的分支 sha。干净合并的 `api-requests.jsonl` 里每条请求的 method 都是 `GET`，推送发生在 git。冲突时退出码是 `1`，目标分支的 sha 不变，请求同样只有 `GET`。
- `dry_run` 的证明要包含 `api-requests.jsonl` 里只有 `GET`，以及所有分支 `unchanged`。试运行里的冲突同样让退出码变成 `1`。

## Cleanup

```powershell
& $py $h cleanup --run $run
```

它会：
- 让本次运行的替身进程自行退出；如果进程不退出，只结束 `run.json` 记录的那两个 pid。不会按进程名去结束进程。
- 删除 `%TEMP%\fork-sync-verify\<run id>\`。
- 保留证据目录，并列出目录里的内容。

失败的尝试之后也要跑 `cleanup`。`& $py $h list` 可以列出还留在本机的运行。

## 隔离

每次运行有独立的 run id、临时目录、端口和 git 配置，多次运行可以同时进行。只驱动自己 launch 出来的 run id，不要对别人的 run id 执行 `run`、`config` 或 `cleanup`。临时目录可以用 `FORK_SYNC_VERIFY_SCRATCH` 改到别处。

## 本机替身没有覆盖的部分

以下内容只有在真实 GitHub 上运行 workflow 时才能证明。本机通过，不代表这些也通过：

- Fine-grained PAT 的真实权限、过期，以及跨组织私有上游的访问
- 真实 `/user/repos` 的分页和授权范围（替身只返回 `alice` 名下的 4 个仓库，只有一页）
- 分支保护。本地裸仓库接受对目标分支的推送，保护规则拒绝 Token 的情况要在真实 GitHub 上才能看到
- Pull Request 的批准。替身不检查审查。GitHub 不允许作者批准自己的 Pull Request，脚本开出的 Pull Request 作者是 PAT 所有者
- 真实网络、限速和超时
- `ubuntu-latest` 运行器、`actions/setup-python@v5` 和 Python 3.12（本机是 Windows，Python 版本以 `.venv` 为准）
- workflow 的定时触发和 `concurrency` 分组

在真实环境验证的入口：在管理仓库里把 `config/repos.yml` 保持为 `dry_run: true`，在 `Actions → Daily Upstream Sync → Run workflow` 手动触发，或者执行 `gh workflow run "Daily Upstream Sync"`，然后在运行页面看摘要。origin 是一个空的 GitHub 仓库，本机提交还没有推上去，所以这条路径还没有在 GitHub 上跑过。
