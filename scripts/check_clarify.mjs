// 追问卡片（static/console/assets/clarify.js）的自检脚本。
//
// 用法：node scripts/check_clarify.mjs
// 全部断言通过静默退出 0，任何一条失败打印差异并退出 1。
//
// 为什么放在 scripts 而不是 tests/：这段是纯前端 JS，pytest 环境里没有 DOM。
// 而它负责两件出错就难看的事——把用户的选择拼成一句话，以及把模型给的
// 脏数据洗成安全的 HTML。
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = fs.readFileSync(
  path.join(root, "static/console/assets/clarify.js"),
  "utf8"
);

const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "clarify.js" });
const C = sandbox.window.PAPClarify;
if (!C || typeof C.html !== "function") {
  console.error("clarify.js 未暴露 window.PAPClarify");
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

/* ---- 规范化：模型输出不受控，界面不能跟着失控 ---- */

check("接受 {items:[...]} 包装", C.normalize({ items: [{ question: "问什么", options: [] }] })
  .map((i) => i.question), ["问什么"]);

check("也接受裸数组", C.normalize([{ question: "问什么" }]).map((i) => i.question), ["问什么"]);

check("非数组返回空", C.normalize(null), []);
check("对象缺 items 返回空", C.normalize({ foo: 1 }), []);

check(
  "超量项被截到 3 条",
  C.normalize([1, 2, 3, 4, 5].map((n) => ({ question: "问题" + n }))).length,
  3
);

check(
  "重复问题只留一条",
  C.normalize([{ question: "同一个问题" }, { question: "同一个问题" }]).length,
  1
);

check(
  "空问题被丢弃",
  C.normalize([{ question: "   " }, { question: "有效" }]).map((i) => i.question),
  ["有效"]
);

check(
  "选项超量截到 6 个",
  C.normalize([{
    question: "选哪个",
    options: [1, 2, 3, 4, 5, 6, 7, 8].map((n) => ({ label: "选项" + n }))
  }])[0].options.length,
  6
);

check(
  "value 缺省时等于 label",
  C.normalize([{ question: "q", options: [{ label: "高" }] }])[0].options[0],
  { label: "高", value: "高" }
);

check(
  "纯字符串选项按 label 收下",
  C.normalize([{ question: "q", options: ["低", "中"] }])[0].options.map((o) => o.label),
  ["低", "中"]
);

check(
  "label 重复的选项去掉后一个",
  C.normalize([{
    question: "q",
    options: [{ label: "高", value: "high" }, { label: "高", value: "H" }]
  }])[0].options.length,
  1
);

const longQuestion = C.normalize([{ question: "问".repeat(500) }])[0].question;
ok("超长问题被截断", longQuestion.length <= 180, `实际 ${longQuestion.length} 字`);

/* ---- 渲染 ---- */

const items = C.normalize([
  {
    question: "这句话要用在哪？",
    options: [
      { label: "维护工单", value: "ticket" },
      { label: "停机说明", value: "notice" }
    ]
  },
  { question: "涉及哪个站点？", options: [] }
]);

const single = C.html([items[0]], { index: 3 });
ok("渲染出单选组", single.includes('type="radio"') && single.includes('name="cl-3-0"'));
ok("单选组带序号容器", single.includes('data-clarify="3"'));
ok("单选组有自定义选项", single.includes(C.CUSTOM) && single.includes("其他（自己填写）"));
ok("单选组不显示提交按钮", !single.includes("data-cl-submit"));
ok("单选组提示点一下即回答", single.includes("点一下即作为你的回答"));

const multi = C.html(items, { index: 5 });
ok("多组有提交按钮", multi.includes('data-cl-submit="5"'));
ok("未选齐时提交按钮 disabled", multi.includes("disabled>提交选择"));
ok("多组显示序号", multi.includes('class="cl-n">1<'));
ok("没有候选的项只给输入框", multi.includes('data-cl-input="1"'));

// 没有候选时不该出现「其他（自己填写）」：那行字的意思是「候选之外的另一种」，
// 一个候选都没有的时候它只会让人以为上面还有没显示出来的选项。
const freeOnly = C.html(C.normalize([{ question: "哪个站？", options: [] }]), { index: 0 });
ok("无候选项直接给输入框", freeOnly.includes('data-cl-input="0"') && !freeOnly.includes("hidden"));
ok("无候选项不出现「其他」", !freeOnly.includes("其他（自己填写）"));
ok("无候选项不渲染 radio", !freeOnly.includes('type="radio"'));
// 没有候选时没有东西可点，必须仍给提交按钮，否则用户填完字不知道怎么办。
ok("无候选的单组仍有提交按钮", freeOnly.includes("data-cl-submit"));
ok("无候选的单组提示按回车", freeOnly.includes("填好后按回车"));

const filled = C.html(items, { index: 5, picks: { 0: { choice: "notice" }, 1: { choice: C.CUSTOM, custom: "027" } } });
ok("选齐后提交按钮可用", !filled.includes("disabled>提交选择"));
ok("选中的候选带 is-on", filled.includes("is-on"));
ok("自定义项的输入框不被 hidden", filled.includes('value="027"') && !/data-cl-input="1"[^>]*hidden/.test(filled));

const closed = C.html(items, { index: 5, picks: { 0: { choice: "ticket" } }, closed: true });
ok("已关闭的卡片不可点", closed.includes("disabled") && closed.includes("is-closed"));
ok("已关闭时显示选了什么", closed.includes("已选："));

const evil = C.html([{ question: '<img src=x onerror="alert(1)">', options: [{ label: "<b>x</b>", value: "v" }] }], { index: 0 });
ok("问题文本被转义", !evil.includes("<img src=x"), "问题里的尖括号必须转义");
ok("选项文本被转义", !evil.includes("<b>x</b>"), "选项里的尖括号必须转义");

/* ---- 拼句 ---- */

check(
  "单组只发答案本身",
  C.compose([items[0]], { 0: { choice: "ticket" } }),
  "维护工单"
);

check(
  "单组的自定义答案原样发出",
  C.compose([items[0]], { 0: { choice: C.CUSTOM, custom: "写在控制命令备注里" } }),
  "写在控制命令备注里"
);

check(
  "多组带问题前缀",
  C.compose(items, { 0: { choice: "notice" }, 1: { choice: C.CUSTOM, custom: "027" } }),
  "- 这句话要用在哪？：停机说明\n- 涉及哪个站点？：027"
);

check("没选的不进句子", C.compose(items, { 0: { choice: "notice" } }), "- 这句话要用在哪？：停机说明");

/* ---- 完成度 ---- */

check("选了候选算答完", C.answered(items[0], { choice: "ticket" }), true);
check("选了其他但没打字不算答完", C.answered(items[0], { choice: C.CUSTOM, custom: "" }), false);
check("全选齐了返回 true", C.allAnswered(items, { 0: { choice: "ticket" }, 1: { choice: C.CUSTOM, custom: "027" } }), true);
check("缺一项返回 false", C.allAnswered(items, { 0: { choice: "ticket" } }), false);
check("空 items 不算可提交", C.allAnswered([], {}), false);
check("待选数量", C.pending(items, { 0: { choice: "ticket" } }), 1);

if (failures) {
  console.error(`\n${failures} 项不通过`);
  process.exit(1);
}
