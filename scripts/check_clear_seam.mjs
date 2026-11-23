// 清空分隔条（render.js 的 clearSeams / clearSeamHtml）自检。
//
// 用法：node scripts/check_clear_seam.mjs
// 全部通过静默退出 0，失败打印差异并退出 1。
//
// 为什么要单独验这两件事：
// - 插在哪：判错了会把分隔条插到 `/clear` 那条**用户消息**下面，读起来像
//   「上面那句被清掉了」，而实际被清的是它之后的回答；
// - 长什么样：如果它被做成可展开的 <details>，用户会以为上下文还能恢复。
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
if (!R || typeof R.clearSeams !== "function") {
  console.error("render.js 未暴露 window.PAPRender.clearSeams");
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

function ok(name, value, hint) {
  if (!value) {
    failures += 1;
    console.error(`FAIL ${name}${hint ? "：" + hint : ""}`);
  }
}

/* ---- 插在哪 ---- */

check("没有标记时没有任何分隔", R.clearSeams([
  { role: "user", text: "问" },
  { role: "agent", text: "答" }
]), {});

check("标记落在回复上", R.clearSeams([
  { role: "user", text: "问" },
  { role: "agent", text: "答" },
  { role: "user", text: "/clear" },
  { role: "agent", text: "已清空", contextCleared: true }
]), { 3: true });

// 清两次就是两个点：只画最后一个会让前一段历史看起来「还在上下文里」。
check("多次清空各插一条", R.clearSeams([
  { role: "user", text: "问一" },
  { role: "agent", text: "答一" },
  { role: "user", text: "/clear" },
  { role: "agent", text: "已清空", contextCleared: true },
  { role: "user", text: "问二" },
  { role: "agent", text: "答二" },
  { role: "user", text: "/clear" },
  { role: "agent", text: "已清空", contextCleared: true }
]), { 3: true, 7: true });

// 刷新后走的是同一条判据：标记来自服务端消息元数据，不依赖前端状态。
check("只认 truthy，不认字符串", R.clearSeams([
  { role: "agent", text: "答", contextCleared: "false" }
]), { 0: true });

check("空值与 null 安全", R.clearSeams(null), {});
check("空数组", R.clearSeams([]), {});
check("中间有空洞不炸", R.clearSeams([null, { contextCleared: true }]), { 1: true });

/* ---- 长什么样 ---- */

const html = R.clearSeamHtml();
ok("带专门的 class", html.includes("chat-seam-clear"), html);
ok("写明不再参与推理", html.includes("不再参与推理"), html);
// 关键：不是 <details>。可展开会让人以为上下文能从这里恢复。
ok("不可展开", !html.includes("<details"), html);
ok("不是压缩分隔条", !html.includes("ctx-seam"), html);
ok("带文字容器（CSS 靠 span 遮住中间那段虚线）", html.includes("<span>"));

/* ---- 清空会不会让老摘要失效（与压缩分隔条的关系）---- */

const S = R.clearSuppressesSeam;
ok("暴露了 clearSuppressesSeam", typeof S === "function");

// 没清过时必须显示摘要：空标记表里 `Math.min` 是 Infinity，
// 写成内联比较会让没清过的会话里所有折叠条一起消失。
check("没清过不抑制", S({}, 3), false);
check("没清过且无下标", S({}, -1), false);
check("清空点在水位之后 → 抑制", S({ 5: true }, 3), true);
check("清空点在水位之前 → 不抑制", S({ 1: true }, 3), false);
check("清空点正好在水位上 → 不抑制", S({ 3: true }, 3), false);
check("多次清空取最近一次的位置", S({ 1: true, 7: true }, 3), true);
check("null 安全", S(null, 3), false);

if (failures) {
  console.error(`\n${failures} 条断言失败`);
  process.exit(1);
}
