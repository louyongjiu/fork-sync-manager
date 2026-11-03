# Fork Sync Manager — 自动发现 Fork 的集中管理版

**不用给每个 Fork 单独维护 GitHub Actions，也不用手动填写上游地址。**

创建一个管理仓库，上传这些文件，配置一次跨仓库 GitHub Token，即可每天自动扫描你账号下授权的 Fork。脚本用普通 Git merge 把上游合进每个 Fork 的目标分支，目标分支默认是 Fork 的默认分支。合并没有冲突时，脚本直接把结果推到目标分支。Fork 没有自己的提交时走快进，推送后目标分支和上游指向同一个提交。合并有冲突时，脚本中止合并，目标分支保持原提交，本次运行失败，由你在本地手工合并（见「处理冲突」）。配置了开发分支时，脚本再开一个 Pull Request，把目标分支送进开发分支，等你审查后合并。自动合入的上游提交落在目标分支上。开发分支要等这个 Pull Request 合并之后才包含这些提交。

## 初次部署（一次即可）

1. 在 GitHub 个人账号创建 `fork-sync-manager` 仓库，推荐设为 Private。
2. 把本项目的文件推送或上传到这个仓库的默认分支。`.github`、`config`、`scripts` 目录必须位于仓库根目录，因为 GitHub 只运行默认分支里 `.github/workflows/` 下的定时任务。
3. 创建 [GitHub Fine-grained Personal Access Token](https://github.com/settings/personal-access-tokens/new)：
   - `Resource owner`：你的 GitHub 个人账号。
   - `Repository access`：选择需要同步的所有 Fork 仓库。首次部署先只选 1–2 个测试仓库，确认后再逐步加入其他 Fork。
   - `Repository permissions`：`Contents: Read and write`、`Pull requests: Read and write`、`Workflows: Read and write`、`Metadata: Read-only`。上游提交经常新增或修改 `.github/workflows/` 里的文件。Token 缺少 `Workflows: Read and write` 时，GitHub 会拒绝推送这类提交。配置了开发分支时，脚本要创建 Pull Request，所以还需要 `Pull requests: Read and write`。
   - 如果私有 upstream 不属于此账号，请确认 Token 对这些上游是否有访问权限；跨组织私有 upstream 可能需要改用 GitHub App 认证方案。
4. 在**管理仓库** `Settings → Secrets and variables → Actions → New repository secret` 增加 `FORK_SYNC_TOKEN`，值填刚才的 Token；**不要把 Token 放进文件**。
5. 第一次运行时保持 `config/repos.yml` 中的 `dry_run: true`。进入管理仓库 `Actions → Daily Upstream Sync → Run workflow` 手动执行，在运行报告里检查选中的 Fork、上游和分支。试运行也会在临时目录里真实合并一次，有冲突的 Fork 记为 `conflict`，这次运行会显示失败。按「处理冲突」解决，或先给它写 `enabled: false`。
6. 确认正确后将 `dry_run: false`，提交，手动执行一次。以后每天自动运行（workflow 配置为每天 03:17 UTC）。

## 自动发现原理

- `GET /user/repos?affiliation=owner&per_page=100&page=N` 分页取得 Token 授权的个人账号仓库。
- 仅选择 API 标记为 `fork: true` 的仓库，忽略普通仓库和不属于管理账号的仓库。
- 对每个 Fork 读取 `parent.full_name`，默认以上游的默认分支为来源。
- 某个 Fork 的 parent 不可访问或权限不足时，该仓库记为 `failed`。仓库已归档时记为 `skipped`。两种情况都不影响其他仓库。
- 不必手动维护列表。新增加的 Fork 只要纳入 Token 授权，下一次定时任务便会被扫描到。

## 关键安全策略

- 脚本用普通 `git merge` 把上游合进 Fork 的目标分支。Fork 自己的提交留在历史里。
- Fork 的目标分支上没有自己的提交时，合并走快进。快进之后，目标分支和上游指向同一个提交。
- 推送命令是 `git push origin HEAD:refs/heads/<目标分支>`。运行期间如果有人把新提交推上同一分支，Git 会因非快进拒绝这次推送，该仓库记为 `failed`。下一次运行会基于新的目标分支重新合并，再推一次。
- 合并冲突时，脚本执行 `git merge --abort`。远程目标分支保持这次运行开始时的提交。该仓库记为 `conflict`，本次运行以退出码 1 结束。
- 如果某个 Fork 启用了 Actions，并且它的某个 workflow 的 `on: push` 覆盖目标分支，脚本的推送会触发那个 workflow，例如部署任务。
- 目标分支的保护规则不允许这个 Token 推送时，GitHub 拒绝推送，该仓库记为 `failed`。
- 没有冲突只说明 Git 能自动合并文本，不说明合并后的代码能构建和运行。要发现这类问题，在 Fork 上配置 CI。
- 脚本只在开发分支还不存在时创建它。创建用的提交是本次合并前从远程取到的目标分支。之后脚本不把新提交直接推上开发分支。开发分支上的新提交来自这个 Pull Request，或者来自你自己的推送。
- 合并这个 Pull Request 时选择 **Create a merge commit**。Squash 合并会让下一次 Pull Request 再次列出同样的提交。
- 关掉的 Pull Request 保持关闭。目标分支出现新提交之后，脚本才再开一个。
- 要让别人先看过再合并，给开发分支加上需要审查的保护规则。GitHub 不允许 Pull Request 的作者批准自己的 Pull Request。这些 Pull Request 的作者是这个 PAT 的所有者，所以要求批准时需要另一位开发者，或者由管理员绕过。
- 要暂停某个 Fork，在 `repos` 里给它写 `enabled: false`，之后的运行把它记为 `skipped`。处理完后删掉这一项，或改回 `enabled: true`。
- 两边历史不相关时，Git 无法做普通 merge，该仓库记为 `failed`。这时检查 `upstream` 是否指向正确的源仓库。
- 脚本逐个处理仓库，每个仓库都要拉取目标分支和上游分支的完整历史。仓库多或体积大时，要留意 workflow 的 90 分钟超时和 GitHub API 限速。
- Fine-grained PAT 可能过期，过期后需要更新管理仓库 Secret。

## 配置（只需一个文件）

`config/repos.yml`：

```yaml
settings:
  dry_run: true  # 测试后改 false
  discover_forks: true
  max_repos: 100
  # dev_branch: develop
  exclude: []
repos: []
```

`settings` 只接受上面这些键，外加可选的 `owner` 和 `dev_branch`。`dry_run` 和 `discover_forks` 必须写 `true` 或 `false`。省略 `dry_run` 时按 `false` 处理，脚本会真实推送。`max_repos` 是 1 到 1000 的整数，默认 `100`。`exclude` 的每一项可以只写仓库名，也可以写 `owner/repo`。`dev_branch` 是开发分支的名字，要写成合法的 Git 分支名。

只同步某些 Fork：在 `settings.exclude` 列出其他仓库，或只在 PAT 中授权指定仓库。`max_repos` 限制的是去掉 `exclude` 之后最终要处理的仓库数。

针对某个 Fork 覆盖上游/分支，或停用它：

```yaml
repos:
  - fork: YOUR_NAME/special-fork
    upstream: SOURCE_OWNER/special-upstream
    target_branch: main
    upstream_branch: develop
    dev_branch: develop
  - fork: YOUR_NAME/paused-fork
    enabled: false
```

`repos` 的每一项只接受 `fork`、`enabled`、`upstream`、`target_branch`、`upstream_branch`、`dev_branch`。`fork` 必须属于 `settings.owner` 这个账号。`enabled` 必须写 `true` 或 `false`。`enabled: false` 的仓库报 `skipped`，不调用 GitHub API。`upstream` 必须是 `owner/repo` 格式，分支名必须是合法的 Git 分支名。

`settings.dev_branch` 对每个 Fork 生效。某一项里的 `dev_branch` 只覆盖那一个 Fork。两处都不写时，脚本只更新目标分支。

开发分支在 Fork 上还不存在时，脚本用本次合并前从远程取到的目标分支提交创建它。试运行不创建，详情里写 `would create <开发分支> at <12 位提交>`。

`dev_branch` 和这个 Fork 的目标分支同名时，该仓库记为 `failed`。详情是 `dev_branch '<名字>' is the target branch of <fork>; set target_branch or dev_branch for this repo`。脚本在对这个仓库执行任何 git 命令之前就停下来。其他仓库照常处理。

可以在 `repos` 显式加入不是 Fork 的镜像仓库。这类仓库没有 GitHub parent，必须写 `upstream`，否则该仓库记为 `failed`。

任务启动时先检查整个配置文件，再调用 GitHub API。写错的键名（例如 `dryrun`、`upstream_brnach`）、写成字符串的 `"false"`、不合法的分支名、不属于 `owner` 的 `fork`，都会让任务以退出码 `2` 结束，不处理任何仓库。去掉 `exclude` 之后仓库数超过 `max_repos`，或一个仓库都没有选中，也以退出码 `2` 结束。

## 每日结果

管理仓库的 `Actions → Daily Upstream Sync` 运行详情中会看到每个仓库状态和报告：

- `merged`：已把合并结果推到目标分支。详情里的 `fast-forward` 表示这次是快进，`merge commit` 表示 Fork 有自己的提交，所以生成了合并提交。
- `up_to_date`：目标分支已经包含该上游提交。
- `conflict`：合并发生冲突。详情列出冲突文件，并说明要在本地把上游合进目标分支。远程目标分支保持原提交。这个状态会使本次 Actions Job 失败。
- `skipped`：仓库已归档，或配置里写了 `enabled: false`。
- `failed`：权限不足、网络或 API 错误、仓库不存在、找不到上游、两边历史不相关，或推送被拒。推送被拒的常见原因是保护规则、Token 缺少 `Workflows` 权限，或运行期间目标分支被别人更新。这个状态会使本次 Actions Job 失败。
- `dry_run`：在临时目录里完成合并，不推送。详情写明将要推到目标分支的方式、提交和分支名。试运行遇到冲突时记为 `conflict`，不记为 `dry_run`。

配置了开发分支时，详情在上面的结果后面用 `; ` 接上这次对开发分支做的事。例如 `pushed fast-forward 0123456789ab to main; created develop at abcdefabcdef; opened PR #1 from main into develop: https://github.com/YOUR_NAME/REPO/pull/1`。目标分支没有新提交、已有的 Pull Request 还开着时，详情类似 `alice/widget already contains octo/widget@main; PR #1 from main into develop is open: https://github.com/YOUR_NAME/REPO/pull/1`。开发分支已经包含目标分支时，详情类似 `alice/widget already contains octo/widget@main; develop already contains main`。关掉的 Pull Request 如果还指着目标分支当前的提交，详情里会写 `PR #1 was closed at abcdefabcdef; a new PR opens after main moves`。试运行的详情类似 `Would push fast-forward 0123456789ab to main; would create develop at abcdefabcdef; would open a PR from main into develop`。试运行不调用 Pull Request 接口，也不创建开发分支。

退出码 `0` 表示没有任何仓库记为 `failed` 或 `conflict`。`1` 表示有仓库记为 `failed` 或 `conflict`，或列出 Fork 时出错，例如网络、权限或 API 返回的数据不对。`2` 表示缺少 `FORK_SYNC_TOKEN`，或配置错误。

## 处理冲突

运行报告里出现 `conflict` 时，详情形如 `Merge SOURCE_OWNER/REPO@main into main by hand`，其中给出了上游仓库、上游分支和目标分支。在本地把上游合进目标分支，再推上去。下面以上游分支和目标分支都是 `main` 为例：

```powershell
git clone https://github.com/YOUR_NAME/REPO.git
cd REPO
git remote add upstream https://github.com/SOURCE_OWNER/REPO.git
git fetch upstream
git merge upstream/main
```

按 Git 的提示改好冲突文件，然后提交并推送：

```powershell
git add .
git commit --no-edit
git push origin main
```

下一次运行把这个仓库记为 `up_to_date`。如果上游在这期间又有新提交并且能干净合并，就记为 `merged`。

目标分支和开发分支如果合不到一起，冲突出现在这个 Pull Request 上，不记成同步任务的 `conflict`。可以在 GitHub 的冲突编辑器里改，也可以在本地把目标分支合并进开发分支再推送。

## 其他

本项目通过 Python 标准库 + PyYAML 和系统 Git 工作，依赖已经列在 `requirements.txt` 中。GitHub Actions 运行器已预装 Git。启用定时任务需要 workflow 文件位于管理仓库默认分支。`GITHUB_TOKEN` 默认只能访问管理仓库，因此**必须**配置可以访问目标 Fork 的 `FORK_SYNC_TOKEN`。

本项目在本机用本地裸仓库和 GitHub API 替身测试过合并、快进、冲突、试运行、配置校验，以及开发分支上的审查 Pull Request。测试工具在 `.cursor/skills/verify-fork-sync/`。它还没有在真实 GitHub 上运行过。先用 `dry_run: true` 和少量测试仓库运行，并确认目标分支和开发分支的保护规则。
