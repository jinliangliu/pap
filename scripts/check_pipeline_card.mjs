// 入库步骤卡的自检（render.js 的 pipelineCard）。
//
// 用法：node scripts/check_pipeline_card.mjs
// 全部通过静默退出 0，失败打印差异并退出 1。
//
// 为什么值得单独验：这张卡替代的是原来那一行「加工链路：解析 → 清洗 → …」。
// 替代品少一环，用户就不会再看到那条信息——而他不知道少了什么，只会觉得
// 「入库到底做了什么」又说不清了。所以每条断言都对着原文字里的一项事实：
// 解析多少字、清洗几张表、压掉多少字、切几片、打几个标、索引刷没刷。
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
if (!R || typeof R.pipelineCard !== "function") {
  console.error("render.js 未暴露 window.PAPRender.pipelineCard");
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

/* 一份真实形状的入库结果（CJ/T 335-2010 那次）。 */
const stages = [
  { key: "parse", label: "解析 md", value: "23,727 字", note: "", status: "done" },
  {
    key: "clean",
    label: "清洗 17 张表",
    value: "61,047 → 23,727 字",
    note: "差额是标签与样式，正文未删减",
    status: "done"
  },
  { key: "structure", label: "结构化", value: "58 节", note: "", status: "done" },
  { key: "chunk", label: "切片", value: "58 片", note: "平均 444 字", status: "done" },
  { key: "tag", label: "打标", value: "5 项", note: "", status: "done" },
  { key: "index", label: "索引", value: "已刷新", note: "", status: "done" }
];

const html = R.pipelineCard(stages);

check("有抬头", html.includes("加工链路"), true);
check("汇总行写步数与状态", html.includes("6 步 · 全部完成"), true);
check(
  "六步都在",
  stages.every((stage) => html.includes(">" + stage.label + "<")),
  true
);
check(
  "每步的量都在",
  stages.filter((s) => s.value).every((s) => html.includes(">" + s.value + "<")),
  true
);
check("清洗那句解释还在（少了它用户以为后半本没进去）", html.includes("差额是标签与样式，正文未删减"), true);
check("切片平均值还在", html.includes("平均 444 字"), true);
check("完成态给绿点", html.includes("pipe-step ok"), true);

/* ---- 某一步没成功时，汇总行不能还写「全部完成」 ---- */
const withWarn = R.pipelineCard([
  { key: "parse", label: "解析", value: "1,000 字", status: "done" },
  { key: "tag", label: "打标", value: "未识别出标签", status: "warn" }
]);
check("混了非完成态时不写全部完成", withWarn.includes("全部完成"), false);
check("汇总写完成数", withWarn.includes("1 步完成"), true);
check("失败态有自己的点", withWarn.includes("pipe-step warn"), true);

/* ---- 没有步骤就不画：宁可少一张卡，也不要空壳 ---- */
check("空数组不画", R.pipelineCard([]), "");
check("非数组不画", R.pipelineCard(null), "");
check("脏数据整条跳过", R.pipelineCard([{ status: "done" }, null]), "");

/* ---- 内容要转义：标题来自文档，里面可能有尖括号 ---- */
const escaped = R.pipelineCard([
  { key: "parse", label: '<img src=x>', value: "", status: "done" }
]);
check("标签被转义", escaped.includes("&lt;img"), true);
check("不产生真标签", escaped.includes("<img"), false);

if (failures) {
  console.error(`\n${failures} 项失败`);
  process.exit(1);
}
