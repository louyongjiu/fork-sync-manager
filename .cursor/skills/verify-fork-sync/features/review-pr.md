# 审查 Pull Request

配置了开发分支时，同步任务先处理目标分支，再把目标分支的更新送进开发分支。送法是开一个 Pull Request，head 是目标分支，base 是开发分支。自动合入的上游提交落在目标分支上。开发分支还不存在时，脚本用本次合并前从远程取到的目标提交创建它。试运行只把这两件事写进详情，不创建分支，也不调用 Pull Request 接口。

`settings.dev_branch` 是默认名字。`repos` 里的 `dev_branch` 只覆盖那一个 Fork。两处都不写时，这次运行只更新目标分支。

## Sub-features

- `dev-create-pr`：上游有新提交。状态 `MERGED`，详情里有 `created <开发分支> at <合并前目标提交>` 和 `opened PR #1 from <目标分支> into <开发分支>`。开发分支的提交等于运行前的目标分支。退出码 `0`。
- `dev-open-follows`：Pull Request 还开着，上游又前进了。状态 `MERGED`。详情写 `PR #1 ... is open`。这个 Pull Request 的 `head.sha` 等于新的目标分支。没有 `POST /repos/.../pulls`。
- `dev-merge-contains`：用合并提交合掉这个 Pull Request 后再运行。状态 `UP_TO_DATE`。详情写 `<开发分支> already contains <目标分支>`。没有 `POST`。
- `dev-next-pr`：上面这一步之后上游再前进。状态 `MERGED`，打开 `PR #2`。
- `dev-closed`：关掉 Pull Request 后，目标分支还停着，再运行。详情写 `PR #1 was closed at <提交>; a new PR opens after <目标分支> moves`。没有 `POST`。目标分支再前进，才打开 `PR #2`。
- `dev-squash`：用 Squash 合掉 Pull Request 后再运行。目标分支没变。详情同样是 closed-at 那一句。没有新的 Pull Request。
- `dev-override`：全局名字是 `develop`，这个 Fork 写成 `dev`，而且 `dev` 上已经有自己的提交。运行不创建 `develop`，也不改 `dev` 的提交。Pull Request 的 base 是 `dev`。
- `dev-same-as-target`：`dev_branch` 和目标分支同名。该仓库 `FAILED`，详情含 `is the target branch of`。它的分支不变。同一次运行里其他 Fork 继续。退出码 `1`。
- `dev-dry-run`：详情含 `would create` 和 `would open a PR from <目标分支> into <开发分支>`。请求的 method 都是 `GET`。开发分支没有被创建。
- `dev-invalid`：`dev_branch` 不是合法分支名。退出码 `2`，日志含 `Invalid settings.dev_branch`。

## How to get to it (user POV)

- 在 `config/repos.yml` 的 `settings.dev_branch` 或某个 `repos` 项的 `dev_branch` 里写上开发分支，`dry_run` 设为 `false`，提交后手动或定时运行。
- `alice/gadget` 的默认分支是 `develop`。全局 `dev_branch: develop` 会让这个仓库同名失败。要单独看别的 Fork，把它写进 `exclude`，或只在那个 Fork 上设置 `dev_branch`。

## Driving it with harness.py

Preconditions:

- 刚 launch 好的运行，`doctor` 健康。
- 下面每条路径都从这一份新 launch 开始，除非写了「接着」。

- **创建并打开（`dev-create-pr`）。** 命令：`& $py $h config --run $run --dry-run false --repos-json '[{"fork": "alice/widget", "dev_branch": "develop"}]'`，`& $py $h commit --run $run --repo octo/widget --file feature.txt --text "upstream 1"`，然后 `& $py $h run --run $run --label merge-1`。预期退出码 `0`。`alice/widget` 的状态行含 `MERGED`、`created develop at`、`opened PR #1 from main into develop`。`state-after.json` 里 `alice/widget` 的 `develop` 等于运行前的 `main`。`develop` 是 `main` 上新提交的父提交。
- **接着，已打开（`dev-open-follows` 的前一半）。** 什么都不改，再运行。命令：`& $py $h run --run $run --label pr-open`。预期 `UP_TO_DATE`，详情含 `PR #1 from main into develop is open`。这一步的 `api-requests.jsonl` 里没有 `POST`。
- **接着，head 跟着走。** 命令：`& $py $h commit --run $run --repo octo/widget --file feature.txt --text "upstream 2"`，然后 `& $py $h run --run $run --label merge-2`。预期 `MERGED`，详情仍是 `PR #1 ... is open`。Pull Request 的 `head.sha` 等于新的 `alice/widget main`。没有 `POST /repos/alice/widget/pulls`。
- **接着，合并提交之后（`dev-merge-contains`）。** 命令：`& $py $h pr merge --run $run --repo alice/widget --number 1 --method merge`，然后 `& $py $h run --run $run --label after-merge`。预期 `UP_TO_DATE`，详情含 `develop already contains main`。没有 `POST`。
- **接着，下一个 Pull Request（`dev-next-pr`）。** 命令：`& $py $h commit --run $run --repo octo/widget --file feature.txt --text "upstream 3"`，然后 `& $py $h run --run $run --label pr-2`。预期 `MERGED`，详情含 `opened PR #2 from main into develop`。

- **关掉后保持关闭（`dev-closed`）。** 新 launch。命令：`& $py $h config --run $run --dry-run false --exclude gadget --settings-json '{"dev_branch": "develop"}'`，上游提交一次，`run`，然后 `& $py $h pr close --run $run --repo alice/widget --number 1`，再 `run`。预期 `UP_TO_DATE`，详情含 `was closed at` 和 `a new PR opens after main moves`。没有 `POST`。再推一个上游提交并 `run`。预期打开 `PR #2`。
- **Squash（`dev-squash`）。** 新 launch，配置与上一条相同。打开 `PR #1` 后执行 `& $py $h pr merge --run $run --repo alice/widget --number 1 --method squash`，再 `run`。预期没有新的 Pull Request，详情含 `was closed at`。
- **已有分支（`dev-override`）。** 新 launch。命令：`& $py $h config --run $run --dry-run false --exclude gadget --settings-json '{"dev_branch": "develop"}' --repos-json '[{"fork": "alice/widget", "dev_branch": "dev"}]'`，`& $py $h commit --run $run --repo alice/widget --branch dev --from main --file local.txt --text "on dev"`，再给 `octo/widget` 一个上游提交，然后 `run`。预期详情里没有 `created`。`alice/widget` 没有 `develop`。`dev` 的 sha 与 `commit` 打印的相同。Pull Request 的 base 是 `dev`。
- **同名（`dev-same-as-target`）。** 新 launch。命令：`& $py $h config --run $run --dry-run false --settings-json '{"dev_branch": "develop"}'`，给 `octo/widget` 和 `octo/gadget` 各推一个提交，然后 `run`。预期退出码 `1`。`alice/gadget` 为 `FAILED`，详情含 `dev_branch 'develop' is the target branch of alice/gadget`。`alice/gadget` 的 `develop` 仍是运行前的提交。`alice/widget` 创建 `develop` 并打开 `PR #1`。
- **试运行（`dev-dry-run`）。** 新 launch。命令：`& $py $h config --run $run --dry-run true --exclude gadget --settings-json '{"dev_branch": "develop"}'`，上游提交，然后 `run`。预期 `DRY_RUN`，详情含 `would create develop at` 和 `would open a PR from main into develop`。`api-requests.jsonl` 里每条 method 都是 `GET`。`alice/widget` 没有 `develop`。
- **非法名字（`dev-invalid`）。** 新 launch。命令：`& $py $h config --run $run --settings-json '{"dev_branch": "bad..name"}'`，然后 `run`。预期退出码 `2`，stdout 含 `Invalid settings.dev_branch`。

## Gotchas

- 开发分支要在推送目标分支之前创建。这样一次运行如果在推送之后中断，下一次不会把已经含有未审查提交的目标分支拿去当开发分支的起点。
- 打开的 Pull Request 会跟着 head 分支更新。目标分支被推送之后，不需要再 `POST` 一次。
- Squash 合并不会把目标分支接进开发分支的历史。下一次运行看到的还是同一个关闭的 Pull Request，直到目标分支出现新提交。
- `alice/gadget` 的默认分支叫 `develop`。全局也写 `develop` 时，这个仓库会同名失败。
- 同名检查用的是这个 Fork 的目标分支。归档仓库在这之前就记为 `skipped`。
- 目标分支和开发分支之间的冲突留在 Pull Request 里。同步任务的 `conflict` 只表示上游合不进目标分支。
