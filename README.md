# Fork Sync Manager — 自动发现 Fork 的集中管理版

**不用维护 50 份 GitHub Actions，也不用手动填写 50 个上游地址。**

创建一个管理仓库，上传这些文件，配置一次跨仓库 GitHub Token，即可每天自动扫描你账号下授权的 Fork。脚本对每个 Fork 的目标分支做普通 Git merge，把上游合进来。目标分支默认是这个 Fork 的默认分支。要改用别的分支，在 `config/repos.yml` 里给该仓库写 `target_branch`。能快进时直接快进。Fork 自己没有额外提交时，快进后目标分支和上游指向同一个提交。合并没有冲突时，脚本把结果推上目标分支。合并有冲突时，脚本中止合并，目标分支保持原提交，本次运行失败，由你在本地手工把上游合进目标分支。

## 初次部署（一次即可）

1. 在 GitHub 个人账号创建 `fork-sync-manager` 仓库，推荐设为 Private。
2. 将本 ZIP 解压，**把里面的 `.github`、`config`、`scripts` 等完整目录上传到仓库根目录**，并提交到默认分支。不要直接上传 ZIP 文件本身。
3. 创建 [GitHub Fine-grained Personal Access Token](https://github.com/settings/personal-access-tokens/new)：
   - `Resource owner`：你的 GitHub 个人账号。
   - `Repository access`：选择需要同步的所有 Fork 仓库。首次部署先只选 1–2 个测试仓库，确认后再逐步扩大到 50 个。
   - `Repository permissions`：`Contents: Read and write`、`Workflows: Read and write`、`Metadata: Read-only`。上游提交经常新增或修改 `.github/workflows/` 里的文件。Token 缺少 `Workflows: Read and write` 时，GitHub 会拒绝推送这类提交。
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

- 脚本用普通 `git merge` 把上游合进 Fork 的目标分支。Fork 自己的提交留在历史里。
- Fork 的目标分支上没有自己的提交时，合并走快进。快进之后，目标分支和上游指向同一个提交。
- 推送命令是 `git push origin HEAD:refs/heads/<目标分支>`。运行期间如果有人把新提交推上同一分支，Git 会因非快进拒绝这次推送，该仓库记为 `failed`。下一次运行会基于新的目标分支重新合并，再推一次。
- 合并冲突时，脚本执行 `git merge --abort`。远程目标分支保持这次运行开始时的提交。该仓库记为 `conflict`，本次运行以退出码 1 结束。
- 脚本用 PAT 把合并结果推到目标分支。这个 Fork 启用了 Actions，并且某个 workflow 的 `on: push` 覆盖该分支时，GitHub 会运行那个 workflow。目标分支默认就是这个 Fork 的默认分支。
- 目标分支的保护规则不允许这个 Token 推送时，GitHub 拒绝推送，该仓库记为 `failed`。
- 干净合并表示 Git 能自动合上文件。合进去的代码能不能用，由 Fork 上的 CI 检查。
- 某个 Fork 要先手工处理时，在 `repos` 里给它写 `enabled: false`。运行把它记为 `skipped`。
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
  exclude: []
repos: []
```

`settings` 只接受上面这些键，外加可选的 `owner`。`dry_run` 和 `discover_forks` 必须写 `true` 或 `false`。

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

任务启动时先检查整个配置文件，再调用 GitHub API。写错的键名（例如 `dryrun`、`upstream_brnach`）、写成字符串的 `"false"`、不合法的分支名，都会让任务以退出码 `2` 结束，不处理任何仓库。去掉 `exclude` 之后仓库数超过 `max_repos`，或一个仓库都没有选中，也以退出码 `2` 结束。

## 每日结果

管理仓库的 `Actions → Daily Upstream Sync` 运行详情中会看到每个仓库状态和报告：

- `merged`：已把合并结果推到目标分支。详情里的 `fast-forward` 表示这次是快进，`merge commit` 表示 Fork 上原有提交，所以生成了合并提交。
- `up_to_date`：目标分支已经包含该上游提交。
- `conflict`：合并发生冲突。详情列出冲突文件，并说明要在本地把上游合进目标分支。远程目标分支保持原提交。这个状态会使本次 Actions Job 失败。
- `skipped`：仓库已归档，或配置里写了 `enabled: false`。
- `failed`：权限不足、网络错误、仓库不存在、保护规则拒绝这个 Token 的推送，或其他 API 错误。这个状态会使本次 Actions Job 失败。
- `dry_run`：在临时目录里完成合并。详情写明将要推到目标分支的方式、提交和分支名。远程目标分支保持原提交。

退出码 `0` 表示没有任何仓库记为 `failed` 或 `conflict`。`1` 表示有仓库记为 `failed` 或 `conflict`，或列出 Fork 时出错，例如网络、权限或 API 返回的数据不对。`2` 表示缺少 `FORK_SYNC_TOKEN`，或配置错误。

## 其他

本项目通过 Python 标准库 + PyYAML 和系统 Git 工作，依赖已经列在 `requirements.txt` 中。GitHub Actions 运行器已预装 Git。启用定时任务需要 workflow 文件位于管理仓库默认分支。`GITHUB_TOKEN` 默认只能访问管理仓库，因此**必须**配置可以访问目标 Fork 的 `FORK_SYNC_TOKEN`。

本项目实现并经过本地语法与配置测试，但没有用你的真实 GitHub 仓库实测。请坚持先 dry-run、从少量测试仓库开始，并确认目标分支的保护规则。
