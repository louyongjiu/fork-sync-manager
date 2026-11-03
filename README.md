# Fork Sync Manager — 自动发现 Fork 的集中管理版

**不用维护 50 份 GitHub Actions，也不用手动填写 50 个上游地址。**

创建一个管理仓库，上传这些文件，配置一次跨仓库 GitHub Token，即可每天自动扫描你账号下授权的 Fork。对每个 Fork 的默认分支，尝试普通 Git merge 上游代码，在自动化分支提交结果并创建/更新 PR，待你审核合并。如果发现冲突，不推送、不覆盖默认分支，在运行报告中标记。

## 初次部署（一次即可）

1. 在 GitHub 个人账号创建 `fork-sync-manager` 仓库，推荐设为 Private。
2. 将本 ZIP 解压，**把里面的 `.github`、`config`、`scripts` 等完整目录上传到仓库根目录**，并提交到默认分支。不要直接上传 ZIP 文件本身。
3. 创建 [GitHub Fine-grained Personal Access Token](https://github.com/settings/personal-access-tokens/new)：
   - `Resource owner`：你的 GitHub 个人账号。
   - `Repository access`：选择需要同步的所有 Fork 仓库。首次部署先只选 1–2 个测试仓库，确认后再逐步扩大到 50 个。
   - `Repository permissions`：`Contents — Read and write`、`Pull requests — Read and write`、`Metadata — Read-only`。
   - 如果私有 upstream 不属于此账号，请确认 Token 对这些上游是否有访问权限；跨组织私有 upstream 可能需要改用 GitHub App 认证方案。
4. 在**管理仓库** `Settings → Secrets and variables → Actions → New repository secret` 增加 `FORK_SYNC_TOKEN`，值填刚才的 Token；**不要把 Token 放进文件**。
5. 第一次运行时保持 `config/repos.yml` 中的 `dry_run: true`。进入管理仓库 `Actions → Daily Upstream Sync → Run workflow` 手动执行，在运行报告里检查选中的 Fork、上游和分支。
6. 确认正确后将 `dry_run: false`，提交，手动执行一次。以后每天自动运行（workflow 配置为每天 03:17 UTC）。

## 自动发现原理

- `GET /user/repos?affiliation=owner&per_page=100&page=N` 分页取得 Token 授权的个人账号仓库。
- 仅选择 API 标记为 `fork: true` 的仓库，忽略普通仓库和不属于管理账号的仓库。
- 对每个 Fork 读取 `parent.full_name`，默认以上游的默认分支为来源。
- 如果某个 Fork 的 parent 不可访问、权限不足或仓库被归档，会在报告中失败/跳过，不影响其他仓库。
- 不必手动维护列表。新增加的 Fork 只要纳入 Token 授权，下一次定时任务便会被扫描到。

## 关键安全策略

- **永远不推送 Fork 的默认分支**。脚本只推送 `automation/upstream-sync` 分支，更新由你审核 PR 后完成。
- 使用普通 merge，保留 Fork 的个人提交；合并冲突时中止，不推送本轮内容，也不会强制覆盖默认分支。
- **自动化分支是由机器人专用的**，脚本可能使用 `--force-with-lease` 重建该分支。不要在这个分支手动开发。
- 旧的 PR 被你关闭时，如果自动化分支头部没有变化，脚本不会每天自动重开。后续产生新的上游同步提交时，脚本会尝试重新打开它；GitHub 不允许重新打开时（PR 已合并，或自动化分支在关闭后被重建），脚本新建一个同步 PR。
- 自动无冲突不等于逻辑兼容，请保留 PR 审核与 CI 测试。
- 历史不相关的仓库不能进行普通 merge，需要检查 upstream 是否指向正确源仓库。
- 私有仓库数量过多可能受 GitHub Actions 运行时间、API 限速和 Token 权限影响；50 个中小仓库通常可以先在串行模式测试。workflow 超时配置是 90 分钟。
- Fine-grained PAT 可能过期，过期后需要更新管理仓库 Secret。

## 配置（只需一个文件）

`config/repos.yml`：

```yaml
settings:
  dry_run: true  # 测试后改 false
  discover_forks: true
  max_repos: 100
  sync_branch: automation/upstream-sync
  pr_title_prefix: Upstream sync
  exclude: []
repos: []
```

`settings` 只接受上面这些键，外加可选的 `owner`。`dry_run` 和 `discover_forks` 必须写 `true` 或 `false`，`pr_title_prefix` 必须是字符串。

`sync_branch` 必须与每个 Fork 的默认分支（或下面覆盖的 `target_branch`）不同名。同名时该 Fork 报 `failed`，不推送任何分支。

只同步某些 Fork：在 `settings.exclude` 列出其他仓库，或只在 PAT 中授权指定仓库。`max_repos` 限制的是去掉 `exclude` 之后最终要处理的仓库数。

针对某个 Fork 覆盖上游/分支，或停用它：

```yaml
repos:
  - fork: YOUR_NAME/special-fork
    upstream: SOURCE_OWNER/special-upstream
    target_branch: main
    upstream_branch: develop
  - fork: YOUR_NAME/paused-fork
    enabled: false
```

`repos` 的每一项只接受 `fork`、`enabled`、`upstream`、`target_branch`、`upstream_branch`。`enabled` 必须写 `true` 或 `false`；`enabled: false` 的仓库报 `skipped`，不调用 GitHub API。`upstream` 必须是 `owner/repo` 格式，分支名必须是合法的 Git 分支名。

可以在 `repos` 显式加入非 Fork 镜像仓库，但它必须有可访问的上游地址且归属于同一个账号。

任务启动时先检查整个配置文件，再调用 GitHub API。写错的键名（例如 `dryrun`、`upstream_brnach`）、写成字符串的 `"false"`、不合法的 `sync_branch` 或分支名，都会让任务以退出码 `2` 结束，不处理任何仓库。去掉 `exclude` 之后仓库数超过 `max_repos`，或一个仓库都没有选中，也以退出码 `2` 结束。

## 每日结果

管理仓库的 `Actions → Daily Upstream Sync` 运行详情中会看到每个仓库状态和报告：

- `updated` — 已推送同步分支并新建、更新或重新打开 PR（或复用现有分支更新 PR）
- `up_to_date` — 你的默认分支已经包含该上游提交
- `conflict` — 本轮发生冲突，报告列出冲突文件，没有推送默认分支
- `skipped` — 归档、在配置里写了 `enabled: false`，或相应 PR 被用户关闭且没有新提交
- `failed` — 权限、网络、仓库不存在或 API 错误；会使本次 Actions Job 失败
- `dry_run` — 只做检测、尝试合并，不推送/创建 PR

退出码：`0` 表示没有仓库失败；`1` 表示有仓库失败，或列出 Fork 时出错（网络、权限、API 返回的数据不对）；`2` 表示缺少 `FORK_SYNC_TOKEN` 或配置错误。

## 其他

本项目通过 Python 标准库 + PyYAML 和系统 Git 工作，依赖已经列在 `requirements.txt` 中。GitHub Actions 运行器已预装 Git。启用定时任务需要 workflow 文件位于管理仓库默认分支。`GITHUB_TOKEN` 默认只能访问管理仓库，因此**必须**配置可以访问目标 Fork 的 `FORK_SYNC_TOKEN`。

本项目实现并经过本地语法与配置测试，但没有用你的真实 GitHub 仓库实测。请坚持先 dry-run、从少量测试仓库开始，并确认分支保护和合并策略。
