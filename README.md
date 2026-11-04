# Fork Sync Manager — 自动发现 Fork 的集中管理版

**不用给每个 Fork 单独维护 GitHub Actions，也不用手动填写上游地址。**

创建一个管理仓库，上传这些文件，配置一次跨仓库 GitHub Token，即可每天自动同步你账号下授权的 Fork。脚本用普通 Git merge 把上游合进每个 Fork 的目标分支并直接推送，目标分支默认是 Fork 的默认分支。合并有冲突时，脚本不推送，本次运行失败，由你在本地手工合并（见「处理冲突」）。配置了开发分支时，脚本再开一个从目标分支到开发分支的 Pull Request。开发分支要等这个 Pull Request 合并之后才包含上游的改动。

## 初次部署（一次即可）

1. 在 GitHub 个人账号创建 `fork-sync-manager` 仓库，推荐设为 Private。
2. 把本项目的文件推送或上传到这个仓库的默认分支。`.github`、`config`、`scripts` 目录必须位于仓库根目录，因为 GitHub 只运行默认分支里 `.github/workflows/` 下的定时任务。
3. 创建 [GitHub Fine-grained Personal Access Token](https://github.com/settings/personal-access-tokens/new)：
   - `Resource owner`：你的 GitHub 个人账号。
   - `Repository access`：选择需要同步的所有 Fork 仓库。首次部署先只选 1–2 个测试仓库，确认后再逐步加入其他 Fork。
   - `Repository permissions`：`Contents: Read and write`、`Pull requests: Read and write`、`Workflows: Read and write`、`Metadata: Read-only`。上游提交经常新增或修改 `.github/workflows/` 里的文件。Token 缺少 `Workflows: Read and write` 时，GitHub 会拒绝推送这类提交。配置了开发分支时，脚本要创建 Pull Request，所以还需要 `Pull requests: Read and write`。
   - 如果私有 upstream 不属于此账号，请确认 Token 对这些上游是否有访问权限；跨组织私有 upstream 可能需要改用 GitHub App 认证方案。
4. 在**管理仓库** `Settings → Secrets and variables → Actions → New repository secret` 增加 `FORK_SYNC_TOKEN`，值填刚才的 Token；**不要把 Token 放进文件**。
5. 如需把目标分支的更新送进开发分支，在**管理仓库** `Settings → Secrets and variables → Actions → Variables` 新建仓库变量 `FORK_SYNC_DEV_BRANCH`，值填开发分支名。用 Variables 而不是 Secrets，因为 Actions 会把 Secret 的值在日志里打成 `***`，状态行里的分支名会被盖住。
6. 如需按仓库覆盖上游或分支，或停用某个 Fork，在**管理仓库** `Settings → Secrets and variables → Actions → Variables` 新建仓库变量 `FORK_SYNC_REPOS`，值填多行 YAML 列表。每一项的键见「配置」。不设置、留空或只含空白时，没有这些条目。用 Variables 而不是 Secrets，因为 Actions 会把 Secret 的值在日志里打成 `***`，状态行里的仓库名和分支名会被盖住。
7. 第一次运行时保持 `config/repos.yml` 中的 `dry_run: true`。进入管理仓库 `Actions → Daily Upstream Sync → Run workflow` 手动执行，在运行报告里检查选中的 Fork、上游和分支。试运行也会在临时目录里真实合并一次，有冲突的 Fork 记为 `conflict`，这次运行会显示失败。按「处理冲突」解决，或先在 `FORK_SYNC_REPOS` 里给它写 `enabled: false`。
8. 确认正确后将 `dry_run: false`，提交，手动执行一次。以后每天自动运行（workflow 配置为每天 03:17 UTC）。

## 运行流程

workflow 每天 03:17 UTC 执行一次 `python scripts/sync.py`，也可以在 `Actions → Daily Upstream Sync → Run workflow` 手动执行。一次运行按下面的顺序进行：

1. 检查 `FORK_SYNC_TOKEN`、`FORK_SYNC_DEV_BRANCH`、`FORK_SYNC_REPOS` 和 `config/repos.yml`。缺少 Token、`FORK_SYNC_DEV_BRANCH` 不是合法分支名、`FORK_SYNC_REPOS` 无法解析，或配置写错时，以退出码 `2` 结束，不处理任何仓库。
2. 选出要处理的仓库：自动发现的 Fork 加上 `FORK_SYNC_REPOS` 里的项，去掉 `exclude`，再检查 `max_repos`。超过 `max_repos` 或一个都没选中时，以退出码 `2` 结束。列出 Fork 时出错，以退出码 `1` 结束。
3. 逐个处理仓库。一个仓库出错只记为 `failed`，其他仓库照常处理。
4. 在运行摘要里列出每个仓库的状态和详情。有仓库记为 `failed` 或 `conflict` 时退出码为 `1`，否则为 `0`。

每个仓库在一个临时目录里按这个顺序处理：

1. 配置里写了 `enabled: false` 或者仓库已归档时，记为 `skipped`，到此结束。
2. 读取 Fork 和上游的仓库信息，确定上游、目标分支、上游分支和开发分支。开发分支来自 `FORK_SYNC_DEV_BRANCH`。这个名字和目标分支同名时记为 `failed`，不执行任何 git 命令。
3. 拉取目标分支和上游分支。
4. 配置了开发分支、而 Fork 上还没有这个分支时，用第 3 步拉到的目标分支提交创建它。这一步在推送目标分支之前进行。即使本次运行在推送目标分支之后中断，下一次运行也不会从已经含有未审查上游提交的目标分支创建开发分支。试运行不创建。
5. 目标分支已经包含上游时，记为 `up_to_date`。否则在目标分支上执行 `git merge --ff --no-edit`：
   - Fork 没有自己的提交时走快进，推送后目标分支和上游指向同一个提交。Fork 有自己的提交时生成合并提交。
   - 有冲突时中止合并，记为 `conflict`。目标分支保持原提交，第 6 步跳过。
   - 试运行不推送，记为 `dry_run`。
   - 其余情况推送目标分支，记为 `merged`。
6. 配置了开发分支时，处理审查 Pull Request：
   - 开发分支已经包含目标分支时，不做任何事。
   - 试运行只在详情里写明会开 Pull Request，不调用 Pull Request 接口。
   - 已有打开的 Pull Request 时直接复用。它的来源分支就是目标分支，会自动带上目标分支的新提交。
   - 最近一个关掉的 Pull Request 停在目标分支当前的提交上时，不开新的。目标分支有新提交之后，脚本才再开一个。
   - 其他情况新建 Pull Request，标题是 `Upstream sync: merge <目标分支> into <开发分支>`。
   - 这一步出错时，该仓库记为 `failed`。如果本次已经推送了目标分支，详情里会写明。下一次运行会重新检查，并补开 Pull Request。

## 自动发现原理

- `GET /user/repos?affiliation=owner&per_page=100&page=N` 分页取得 Token 授权的个人账号仓库。
- 仅选择 API 标记为 `fork: true` 的仓库，忽略普通仓库和不属于管理账号的仓库。
- 对每个 Fork 读取 `parent.full_name`，默认以上游的默认分支为来源。
- 某个 Fork 的 parent 不可访问或权限不足时，该仓库记为 `failed`。仓库已归档时记为 `skipped`。两种情况都不影响其他仓库。
- 不必手动维护列表。新增加的 Fork 只要纳入 Token 授权，下一次定时任务便会被扫描到。

## 关键安全策略

- 脚本用普通 `git merge` 把上游合进 Fork 的目标分支。Fork 自己的提交留在历史里。
- 推送命令是 `git push origin HEAD:refs/heads/<目标分支>`。运行期间如果有人把新提交推上同一分支，Git 会因非快进拒绝这次推送，该仓库记为 `failed`。下一次运行会基于新的目标分支重新合并，再推一次。
- 合并冲突时，脚本执行 `git merge --abort`。远程目标分支保持这次运行开始时的提交。该仓库记为 `conflict`，本次运行以退出码 1 结束。
- 如果某个 Fork 启用了 Actions，并且它的某个 workflow 的 `on: push` 覆盖目标分支，脚本的推送会触发那个 workflow，例如部署任务。
- 目标分支的保护规则不允许这个 Token 推送时，GitHub 拒绝推送，该仓库记为 `failed`。
- 没有冲突只说明 Git 能自动合并文本，不说明合并后的代码能构建和运行。要发现这类问题，在 Fork 上配置 CI。
- 脚本只在开发分支还不存在时创建它，之后不把新提交直接推上开发分支。开发分支上的新提交来自审查 Pull Request，或者来自你自己的推送。
- 合并审查 Pull Request 时选择 **Create a merge commit**。Squash 合并会让下一次 Pull Request 再次列出同样的提交。
- 要让别人先看过再合并，给开发分支加上需要审查的保护规则。GitHub 不允许 Pull Request 的作者批准自己的 Pull Request。这些 Pull Request 的作者是这个 PAT 的所有者，所以要求批准时需要另一位开发者，或者由管理员绕过。
- 要暂停某个 Fork，在 `FORK_SYNC_REPOS` 里给它写 `enabled: false`，之后的运行把它记为 `skipped`。处理完后删掉这一项，或改回 `enabled: true`。
- 两边历史不相关时，Git 无法做普通 merge，该仓库记为 `failed`。这时检查 `upstream` 是否指向正确的源仓库。
- 脚本逐个处理仓库，每个仓库都要拉取目标分支和上游分支的完整历史。仓库多或体积大时，要留意 workflow 的 90 分钟超时和 GitHub API 限速。
- Fine-grained PAT 可能过期，过期后需要更新管理仓库 Secret。

## 配置

`config/repos.yml` 只放 `settings`：

```yaml
settings:
  dry_run: true  # 测试后改 false
  discover_forks: true
  max_repos: 100
  exclude: []
```

`settings` 只接受上面这些键，外加可选的 `owner`。`dry_run` 和 `discover_forks` 必须写 `true` 或 `false`。省略 `dry_run` 时按 `false` 处理，脚本会真实推送。`max_repos` 是 1 到 1000 的整数，默认 `100`。`exclude` 的每一项可以只写仓库名，也可以写 `owner/repo`。

只同步某些 Fork：在 `settings.exclude` 列出其他仓库，或只在 PAT 中授权指定仓库。`max_repos` 限制的是去掉 `exclude` 之后最终要处理的仓库数。

按仓库覆盖上游或分支，或停用某个 Fork，把下面的列表写进仓库变量 `FORK_SYNC_REPOS`：

```yaml
- fork: YOUR_NAME/special-fork
  upstream: SOURCE_OWNER/special-upstream
  target_branch: main
  upstream_branch: develop
- fork: YOUR_NAME/paused-fork
  enabled: false
```

`FORK_SYNC_REPOS` 的每一项只接受 `fork`、`enabled`、`upstream`、`target_branch`、`upstream_branch`。`fork` 必须属于 `settings.owner` 这个账号。`enabled` 必须写 `true` 或 `false`。`enabled: false` 的仓库报 `skipped`，不调用 GitHub API。`upstream` 必须是 `owner/repo` 格式，分支名必须是合法的 Git 分支名。不设置、留空或只含空白时，没有这些条目。写成 JSON 列表也可以。

修改变量不会留在 git 历史里，写错时下一次运行以退出码 `2` 结束。

`FORK_SYNC_DEV_BRANCH` 是开发分支的名字，每个 Fork 用同一个值。不设置、留空或只含空白时，脚本只更新目标分支，不处理审查 Pull Request。某个 Fork 的目标分支和这个变量同名时，该仓库记为 `failed`，详情是 `FORK_SYNC_DEV_BRANCH '<名字>' is the target branch of <fork>; set target_branch for this repo or exclude it`。在 `FORK_SYNC_REPOS` 里给这个仓库写 `target_branch`，或把它放进 `exclude`。其他仓库照常处理。

可以在 `FORK_SYNC_REPOS` 显式加入不是 Fork 的镜像仓库。这类仓库没有 GitHub parent，必须写 `upstream`，否则该仓库记为 `failed`。

任务启动时先检查 `config/repos.yml` 和 `FORK_SYNC_REPOS`，再调用 GitHub API。写错的键名（例如 `dryrun`、`upstream_brnach`）、写成字符串的 `"false"`、不合法的分支名、不属于 `owner` 的 `fork`，以及 `config/repos.yml` 里除 `settings` 以外的顶层键，都会让任务以退出码 `2` 结束，不处理任何仓库。去掉 `exclude` 之后仓库数超过 `max_repos`，或一个仓库都没有选中，也以退出码 `2` 结束。

## 每日结果

管理仓库的 `Actions → Daily Upstream Sync` 运行详情中会看到每个仓库状态和报告：

- `merged`：已把合并结果推到目标分支。详情里的 `fast-forward` 表示这次是快进，`merge commit` 表示 Fork 有自己的提交，所以生成了合并提交。
- `up_to_date`：目标分支已经包含该上游提交。
- `conflict`：合并发生冲突。详情列出冲突文件，并说明要在本地把上游合进目标分支。远程目标分支保持原提交。这个状态会使本次 Actions Job 失败。
- `skipped`：仓库已归档，或配置里写了 `enabled: false`。
- `failed`：权限不足、网络或 API 错误、仓库不存在、找不到上游、两边历史不相关、开发分支和目标分支同名、建审查 Pull Request 出错，或推送被拒。推送被拒的常见原因是保护规则、Token 缺少 `Workflows` 权限，或运行期间目标分支被别人更新。这个状态会使本次 Actions Job 失败。
- `dry_run`：在临时目录里完成合并，不推送。详情写明将要推到目标分支的方式、提交和分支名。试运行遇到冲突时记为 `conflict`，不记为 `dry_run`。

配置了开发分支时，详情在上面的结果后面用 `; ` 接上开发分支的处理结果。下面以目标分支 `main`、开发分支 `develop` 为例：

- `created develop at abcdefabcdef`：本次创建了开发分支。
- `opened PR #1 from main into develop: <链接>`：本次新建了审查 Pull Request。
- `PR #1 from main into develop is open: <链接>`：已有的审查 Pull Request 还开着，本次没有改动它。
- `develop already contains main`：开发分支已经包含目标分支，不需要 Pull Request。
- `PR #1 was closed at abcdefabcdef; a new PR opens after main moves`：最近一个审查 Pull Request 已关闭，目标分支还停在关闭时的提交上。
- `would create develop at abcdefabcdef`、`would open a PR from main into develop`：试运行时说明会做什么。

一条完整的详情例如 `pushed fast-forward 0123456789ab to main; created develop at abcdefabcdef; opened PR #1 from main into develop: https://github.com/YOUR_NAME/REPO/pull/1`。

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

目标分支和开发分支如果合不到一起，冲突出现在审查 Pull Request 上，不记成同步任务的 `conflict`。可以在 GitHub 的冲突编辑器里改，也可以在本地把目标分支合并进开发分支再推送。

## 其他

本项目通过 Python 标准库 + PyYAML 和系统 Git 工作，依赖已经列在 `requirements.txt` 中。GitHub Actions 运行器已预装 Git。启用定时任务需要 workflow 文件位于管理仓库默认分支。`GITHUB_TOKEN` 默认只能访问管理仓库，因此**必须**配置可以访问目标 Fork 的 `FORK_SYNC_TOKEN`。

本项目在本机用本地裸仓库和 GitHub API 替身测试过合并、快进、冲突、试运行、配置校验，以及开发分支上的审查 Pull Request。测试工具在 `.cursor/skills/verify-fork-sync/`。它还没有在真实 GitHub 上运行过。先用 `dry_run: true` 和少量测试仓库运行，并确认目标分支和开发分支的保护规则。
