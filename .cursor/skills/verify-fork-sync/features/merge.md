# 合并到目标分支

`dry_run: false` 时，上游有 Fork 还没有的提交，并且两边能干净合并，同步任务把合并结果推到 Fork 的目标分支。目标分支默认是 Fork 的默认分支。Fork 自己没有额外提交时，这次合并是快进，推送之后目标分支和上游指向同一个提交。Fork 自己有提交，而且两边改的不是同一处时，这次合并生成一个合并提交。它的两个父提交分别是运行前的目标分支和上游。推送命令是 `git push origin HEAD:refs/heads/<目标分支>`。

## Sub-features

- `ff-push`：上游前进，Fork 没有自己的提交。状态 `MERGED: pushed fast-forward <12 位 sha> to <目标分支>`。Fork 目标分支的 sha 等于上游同一分支的 sha。退出码 `0`。
- `merge-commit`：Fork 先提交一个上游没改过的文件，上游再提交另一个文件。状态 `MERGED: pushed merge commit <12 位 sha> to <目标分支>`。合并提交有两个父提交，第一个是运行前的 Fork 目标分支，第二个是上游。退出码 `0`。
- `second-run-up-to-date`：上面两种情况再运行一次。状态 `UP_TO_DATE: <fork> already contains <upstream>@<分支>`。退出码 `0`。目标分支 sha 不变。

## How to get to it (user POV)

- 确认试运行结果后，把 `config/repos.yml` 改成 `dry_run: false` 并提交。之后每天 03:17 UTC 的定时运行，或在 `Actions → Daily Upstream Sync → Run workflow` 手动执行，都会走这条路径。

## Driving it with harness.py

Preconditions:

- 刚 launch 好的运行，`doctor` 健康。
- 执行 `& $py $h config --run $run --dry-run false`，输出里出现 `dry_run: false`。

- **快进（`ff-push`）。** 命令：`& $py $h commit --run $run --repo octo/gadget --file g.txt --text "gadget 1"`，然后 `& $py $h run --run $run --label ff`。预期退出码 `0`；`alice/gadget: MERGED: pushed fast-forward <sha> to develop`。`state-after.json` 里 `alice/gadget` 的 `develop` 和 `octo/gadget` 的 `develop` 是同一个 sha。这一步的 `api-requests.jsonl` 里每条请求的 method 都是 `GET`。
- **再运行（`second-run-up-to-date`）。** 命令：`& $py $h run --run $run --label ff-again`。预期 `alice/gadget: UP_TO_DATE: alice/gadget already contains octo/gadget@develop`，`develop` 行是 `unchanged`，退出码 `0`。
- **合并提交（`merge-commit`）。** 在新 launch 的运行里执行 `config --dry-run false`。命令：`& $py $h commit --run $run --repo alice/widget --file fork.txt --text "fork only"`，`& $py $h commit --run $run --repo octo/widget --file upstream.txt --text "upstream only"`，然后 `& $py $h run --run $run --label merge`。预期 `alice/widget: MERGED: pushed merge commit <sha> to main`，退出码 `0`。记下运行前的 `alice/widget main` sha，以及 `state-after.json` 里的新 `main` sha 和 `octo/widget` 的 `main` sha。命令：`git --git-dir "$env:TEMP\fork-sync-verify\$run\remotes\alice\widget.git" log -1 --format="%P" <新 sha>`。输出是两个 sha，第一个是运行前的 Fork `main`，第二个是上游 `main`。
- **合并之后再运行。** 命令：`& $py $h run --run $run --label merge-again`。预期 `alice/widget: UP_TO_DATE: alice/widget already contains octo/widget@main`，`main` 是 `unchanged`。

## Gotchas

- 目标分支的 sha 会变。旧提交是新提交的祖先，所以行尾是 `old -> new`，不带 `<-- DEFAULT BRANCH REWRITTEN`。这个标记只在旧 sha 不是新 sha 的祖先时出现。
- 推送不带 `--force`，也不带 `--force-with-lease`。运行期间如果目标分支上又出现了新提交，git 拒绝这次非快进推送，该仓库记为 `FAILED`。下一次运行会按新的目标分支重新合并。
- 合并写到远程用的是 git push。`api-requests.jsonl` 里对应的是 `GET /user/repos` 和 `GET /repos/...`。
- 两边改了同一个文件时走冲突。那次运行退出码是 `1`，目标分支保持原提交。见 [conflict.md](./conflict.md)。
