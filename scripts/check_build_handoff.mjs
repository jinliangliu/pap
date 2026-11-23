// `/build` 的识别自检（chatcmd.js 的 buildBrief）。
//
// 用法：node scripts/check_build_handoff.mjs
// 全部通过静默退出 0，失败打印差异并退出 1。
//
// 这三十行纯函数值得单独验，是因为它有两份实现：前端这份决定「要不要就地转过去」，
// 后端 `chat_service.is_build_command` 决定「要不要 409」。**下面这组用例与
// `tests/unit/test_chat_build_command.py` 是同一份清单**，改动时两边一起改：
// 前端认得宽、后端认得窄，用户会看到「已转到构建助手」然后什么也没发生；
// 反过来，这句话会被原样发出去、被 409 打回来。
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = fs.readFileSync(
  path.join(root, "static/console/assets/chatcmd.js"),
  "utf8"
);

const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "chatcmd.js" });

const C = sandbox.window.PAPChatCommand;
if (!C || typeof C.buildBrief !== "function") {
  console.error("chatcmd.js 未暴露 window.PAPChatCommand.buildBrief");
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

/* ---- 认得的：返回要带过去的那句话 ---- */
check("只有命令词：打开面板、不带话", C.buildBrief("/build"), "");
check(
  "命令词 + 描述",
  C.buildBrief("/build 压力高了先查规程再决定关不关阀"),
  "压力高了先查规程再决定关不关阀"
);
check("大小写不敏感", C.buildBrief("/BUILD 大写也算"), "大写也算");
check("前后空格", C.buildBrief("  /build 前后空格  "), "前后空格");
check("描述可以跨行", C.buildBrief("/build\n第一行\n第二行"), "第一行\n第二行");
// 全角空格：中文输入法下先打空格再打描述是常见操作。
check("全角空格也算空白", C.buildBrief("/build　中文空格"), "中文空格");

/* ---- 不认的：返回 null，当普通消息发 ---- */
check("不是命令，是一个以 /build 开头的单词", C.buildBrief("/builder 是什么"), null);
check("/buildall", C.buildBrief("/buildall"), null);
check("命令不在开头", C.buildBrief("请用 /build 建一个流程"), null);
check("没有斜杠", C.buildBrief("build 一个流程"), null);
// `/workflow` 的第二个词是流程名，而 build 恰好是合法名字——那条走流程，不走这里。
check("是 /workflow 的流程名", C.buildBrief("/workflow build 处理一下"), null);
check("空输入", C.buildBrief(""), null);
check("null 安全", C.buildBrief(null), null);

/* ---- 它与后端那侧的共同前提 ---- */
check(
  "返回值可以是空串，不能把空串与 null 混成一个",
  [C.buildBrief("/build"), C.buildBrief("/builder")],
  ["", null]
);

if (failures) {
  console.error(`\n${failures} 项失败`);
  process.exit(1);
}
