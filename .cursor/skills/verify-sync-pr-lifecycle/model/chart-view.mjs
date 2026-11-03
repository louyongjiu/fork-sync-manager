// View module for reading SyncPrLifecycle results with /statechart.
export const title = "一个 Fork 的同步 PR 在多次每日运行中的变化";

const prLabel = (pr) => {
  if (!pr.exists) return "还没有同步 PR";
  if (pr.open) return "同步 PR 打开中";
  return pr.merged ? "你合并了同步 PR" : "你关闭了同步 PR";
};

export const state = (v) => {
  if (v.result === "failed") return "上次运行失败";
  if (v.phase === "pr") return "本次运行已备好同步分支";
  return prLabel(v.pr);
};

export const group = (v) => (v.result === "failed" ? "failed" : v.pr.exists ? "pr" : "nopr");

export const colors = { failed: "orange", pr: "blue", nopr: "teal" };

const syncTip = (fork) => {
  const name = Object.keys(fork).find((b) => b !== "main") ?? "main";
  return fork[name].tip;
};

const runPrLabel = {
  create: "运行新建 PR",
  update: "运行更新 PR",
  reopen: "运行重新打开 PR",
  reopen_refused: "GitHub 拒绝重新打开 PR",
  create_refused: "GitHub 拒绝新建 PR",
};

export const step = (action, from, to) => {
  switch (action) {
    case "UpstreamAdvances":
      return "上游推送新提交";
    case "YouCommitToDefaultBranch":
      return "你向默认分支推送提交";
    case "YouMergePr":
      return "你合并 PR";
    case "YouClosePr":
      return "你关闭 PR";
    case "RunFetchesAndPushes":
      if (to.justRan && to.result === "up_to_date") return "运行发现已包含上游";
      if (to.justRan && to.result === "conflict") return "运行遇到合并冲突";
      return to.runHead === syncTip(from.fork) ? "运行复用同步分支" : "运行推送同步分支";
    case "RunUpdatesPr":
      if (to.result === "skipped") return "运行跳过已关闭的 PR";
      return runPrLabel[to.prCall.kind] ?? "运行处理 PR";
    case "Done":
      return null;
    default:
      return action;
  }
};
