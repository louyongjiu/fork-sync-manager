---
name: verify-fork-sync
description: 在本机按 GitHub Actions「Daily Upstream Sync」的方式运行 Fork Sync Manager 的 scripts/sync.py，用本地裸仓库和本地 GitHub API 替身驱动同步、建 PR、冲突、关闭/合并 PR、自动发现与配置等用户路径，并留下 stdout、退出码、分支前后对比、PR 状态和 API 请求记录作为证据。修改 scripts/sync.py、config/repos.yml、.github/workflows/daily-sync.yml 之后，需要证明行为时使用。
---

# 验证 Fork Sync Manager

用户接触的界面只有一个：GitHub Actions 每天（或手动 `Run workflow`）在管理仓库里执行 `python scripts/sync.py`。用户能看到的结果有：
- Actions 日志里的状态行（`UPDATED:`、`UP_TO_DATE:`、`CONFLICT:`、`SKIPPED:`、`FAILED:`、`DRY_RUN:`、`ERROR:`）；
- 任务摘要（`GITHUB_STEP_SUMMARY` 写入的 Markdown）；
- 任务退出码：`0` 表示成功；`1` 表示有仓库失败；`2` 表示配置错误或缺少 token；
- 各个 Fork 上的 `automation/upstream-sync` 分支和同步 PR。

本 skill 用 `scripts/harness.py` 在本机复现这条路径，具体分工如下：

| 部分 | 本机验证时 |
|---|---|
| `scripts/sync.py` | 真实代码。每次 `run` 都把当前文件原样复制到临时根目录再执行，证据里记录 sha256 |
| git、Python、PyYAML | 真实程序，使用项目 `.venv` |
| GitHub REST API | `fake_github.py` 替身，通过 `GITHUB_API_URL` 接入（workflow 运行时同样读取这个变量） |
| `https://github.com/<owner>/<repo>.git` | 通过临时 `GIT_CONFIG_GLOBAL` 里的 `url.insteadOf` 指到本地裸仓库 |
| 真实网络 | `HTTPS_PROXY` 指向 `127.0.0.1:9`，任何误发到 github.com 的请求都会立即失败 |

替身实现了 GitHub 的这些 PR 规则：
- 打开的 PR 的 `head.sha` 跟随分支变化；关闭后固定为关闭时的值。
- 已合并的 PR 不能重新打开（返回 422）。
- 分支在关闭后被删除、强制推送或重建，PR 就不能重新打开（返回 422）。
- 同一个 head 和 base 已经有打开的 PR 时，不能再创建（返回 422）；head 相对 base 没有新提交时，也不能创建（返回 422）。

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
| 修改管理仓库的 `config/repos.yml` | `config --dry-run true\|false`、`--sync-branch <名>`、`--exclude <名> ...`、`--owner <名>`、`--max-repos <n>`、`--discover true\|false`、`--repos-json '<JSON 列表>'`、`--reset`（重新复制仓库里的版本，可以和其他参数一起用） |
| 上游维护者或用户推送提交 | `commit --repo <owner/repo> [--branch <分支>] [--from <已有分支>] --file <路径> --text <整行内容>`（文件整体替换为这一行） |
| 用户在 GitHub 上关闭 PR | `pr close --repo alice/<repo> --number <n>` |
| 用户合并 PR | `pr merge --repo alice/<repo> --number <n> --method merge\|squash [--delete-branch]` |
| 用户删除分支 | `delete-branch --repo alice/<repo> --branch <分支>`（如果这个分支上有打开的 PR，替身会像 GitHub 一样把它关闭） |
| Actions 运行一次同步 | `run --label <标签> [--no-token] [--sync-py <另一份 sync.py>]` |
| 只看当前状态 | `state --label <标签>`、`pr list` |

`run` 的输出包括：
- `sync.py exit code`；
- 每个仓库的状态行；
- 每个 Fork 各分支在运行前后的 sha，默认分支被改写时行尾会出现 `<-- DEFAULT BRANCH WRITTEN`；
- 运行后的 PR 列表；
- 本次运行的 API 请求数。

harness 本身的退出码等于 `sync.py` 的退出码；harness 自己出错时返回 `3`。

具体路径怎么走，见 [features/README.md](features/README.md) 和其中每个功能文件。

## Evidence

证据目录是 `.verify\evidence\<run id>\`，已经在 `.gitignore` 中。每次 `run` 写入一个 `NN-run-<标签>\` 子目录，包含：

- `command.txt`：工作目录、命令行、所用 `sync.py` 的路径和 sha256
- `repos.yml`：本次运行使用的配置
- `stdout.txt`、`stderr.txt`、`exit-code.txt`
- `step-summary.md`：Actions 任务摘要的原文
- `state-before.json`、`state-after.json`：所有仓库的分支和 sha，以及 PR 状态
- `api-requests.jsonl`：本次运行发出的每个 API 请求（方法、路径、状态码、请求体字段）
- `report.txt`：和屏幕输出相同的摘要

`state` 写入 `NN-state-<标签>\state.json`。`cleanup` 时会再把 `server.log`、完整的 `api-requests.jsonl` 和 `run.json` 复制到证据目录根下。

证明一项行为时：
- 走用户真实会走的路径：改配置、推送提交、在 PR 上操作，然后 `run`。不要直接改替身的内部状态。
- 同时保留动作和结果：每一步都有自己的 `NN-` 目录，按顺序对得上。
- 验证副作用：比对 `state-before.json` 和 `state-after.json` 中的分支，并在 `api-requests.jsonl` 里确认发出了（或没有发出）`POST`、`PATCH`。只看状态行不够。
- `dry_run` 的证明必须包含 `api-requests.jsonl` 里只有 `GET`，以及所有分支 `unchanged`。

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
- GitHub 真实的 PR 规则，包括合并检查、分支保护、自动删除分支等设置（替身只实现上面列出的那几条）
- 真实网络、限速和超时
- `ubuntu-latest` 运行器、`actions/setup-python@v5` 和 Python 3.12（本机是 Windows，Python 版本以 `.venv` 为准）
- workflow 的定时触发和 `concurrency` 分组

在真实环境验证的入口：在管理仓库里把 `config/repos.yml` 保持为 `dry_run: true`，在 `Actions → Daily Upstream Sync → Run workflow` 手动触发，或者执行 `gh workflow run "Daily Upstream Sync"`，然后在运行页面看摘要。这个本地仓库目前没有配置远端，也没有提交，所以这条路径还没有跑过。
