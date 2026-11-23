// 编排图上的「谁做这一步」自检（render.js 的 flowDiagram）。
//
// 用法：node scripts/check_flow_labels.mjs
// 全部通过静默退出 0，失败打印差异并退出 1。
//
// 为什么值得单独验：图里存的是标识（`assistant`，运行时按它取 Agent），而用户
// 嘴里说的是显示名（「通用助手」）。后端 `_agent_labels` 会多带一个 `agent_label`，
// 这里决定它画在哪——画错了不是崩，是**看着像另一个人**，比不画更难发现。
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = fs.readFileSync(
  path.join(root, "static/console/assets/render.js"),
  "utf8"
);

const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "render.js" });

const R = sandbox.window.PAPRender;
if (!R || typeof R.flowDiagram !== "function") {
  console.error("render.js 未暴露 window.PAPRender.flowDiagram");
  process.exit(1);
}

let failures = 0;

function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures += 1;
    console.error(`FAIL ${name}`);
    console.error(`  期望 ${JSON.stringify(expected)}`);
    console.error(`  实际 ${JSON.stringify(actual)}`);
  }
}

/* 单节点图：只关心它画了什么字。 */
function solo(node) {
  return R.flowDiagram({ entry: node.id, nodes: [node], edges: [] });
}

/* ---- 显示名写在副标题，标识留在悬停提示 ---- */
const labelled = solo({
  id: "rule",
  kind: "agent",
  agent: "assistant",
  agent_label: "通用助手"
});
check("副标题是显示名", labelled.includes(">通用助手</text>"), true);
check("正文里不再出现标识", labelled.includes(">assistant</text>"), false);
check("悬停里两者都在（排查按标识搜日志）", labelled.includes("通用助手 / assistant"), true);

/* ---- 没有显示名时退回标识：图仍然可读 ---- */
const bare = solo({ id: "check", kind: "agent", agent: "field-ops" });
check("退回标识", bare.includes(">field-ops</text>"), true);
check("不出现空括号", bare.includes("（）"), false);

/* ---- 显示名与标识相同时不重复一遍 ---- */
const same = solo({ id: "check", kind: "agent", agent: "ops", agent_label: "ops" });
check("同值时不写成 ops / ops", same.includes("ops / ops"), false);
check("同值时只写一次", same.includes("（ops）"), true);

/* ---- 汇总节点仍然是「汇总」，不被 agent 覆盖 ---- */
const compose = solo({ id: "report", kind: "compose", sub: "汇总" });
check("汇总节点", compose.includes(">汇总</text>"), true);

/* ---- 并行节点：副标题是「并行 N 路」，不是某个分支的人 ---- */
const parallel = R.flowDiagram({
  entry: "fan",
  nodes: [
    {
      id: "fan",
      kind: "parallel",
      sub: "并行 2 路",
      agents: ["ops", "field-ops"],
      agent_labels: ["运维", "现场作业"]
    }
  ],
  edges: []
});
check("并行不写成分支里的人", parallel.includes(">并行 2 路</text>"), true);

if (failures) {
  console.error(`\n${failures} 项失败`);
  process.exit(1);
}
