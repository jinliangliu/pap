// 审批卡状态判断（static/console/assets/approval.js）的自检脚本。
//
// 用法：node scripts/check_approval_card.mjs
// 全部断言通过静默退出 0，任何一条失败打印差异并退出 1。
//
// 为什么放在 scripts 而不是 tests/：这段是纯前端 JS，pytest 环境里没有 DOM；
// 而它恰恰是最先出问题的地方——审批号服务端一直存着，是前端在还原历史时
// 把它丢了，于是刷新之后那段停在审批上的历史读起来像结论。
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = fs.readFileSync(
  path.join(root, "static/console/assets/approval.js"),
  "utf8"
);

const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "approval.js" });
const A = sandbox.window.PAPApproval;
if (!A || typeof A.fromMeta !== "function") {
  console.error("approval.js 未暴露 window.PAPApproval");
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

const AP_ID = "8f1d2c3b-0000-4000-8000-000000000001";

function batch(items, ok = true) {
  const map = {};
  for (const item of items) map[item.approval_id] = item;
  return { ok, items: map };
}

// ---- 不该有卡的消息 ----
check("普通消息不产生卡片", A.fromMeta({ intent: "chat" }, batch([])), null);
check("没有 metadata 也不产生卡片", A.fromMeta(null, batch([])), null);

// ---- 还在等人：必须出按钮 ----
const pending = A.fromMeta(
  { approval_id: AP_ID },
  batch([
    {
      approval_id: AP_ID,
      status: "PENDING",
      actionable: true,
      tool_name: "create_maintenance_ticket",
      preview: { tool_name: "create_maintenance_ticket", parameters: { a: 1 } },
      expires_at: "2026-11-12T10:00:00Z"
    }
  ])
);
check("待确认的标题", pending.title, "需要审批");
check("待确认可以点", pending.open, true);
check("待确认还没有结论", pending.done, "");
check("待确认能还原参数", pending.preview.parameters, { a: 1 });
check("待确认带上过期时间", pending.expires_at, "2026-11-12T10:00:00Z");

// ---- 已经处理过：不能出按钮 ----
const done = A.fromMeta(
  { approval_id: AP_ID },
  batch([
    {
      approval_id: AP_ID,
      status: "VERIFIED",
      actionable: false,
      tool_name: "create_maintenance_ticket",
      preview: {},
      executed_at: "2026-11-12T09:31:00Z"
    }
  ])
);
check("已处理的标题", done.title, "已处理");
check("已处理不能再点", done.open, false);
check("已处理写结论", done.done, "已执行并核验通过");
check("已处理带上执行时间", done.executed_at, "2026-11-12T09:31:00Z");

// 失败必须同时给出错误码：只说「执行失败」，运维得翻日志才知道该找谁。
const revoked = A.fromMeta(
  { approval_id: AP_ID },
  batch([{ approval_id: AP_ID, status: "FAILED", actionable: false, error_code: "DELEGATION_REVOKED" }])
);
check("失败要带错误码", revoked.done, "执行失败（DELEGATION_REVOKED）");

// 状态还写着 PENDING 但已不可点：清理任务还没跑到的那个窗口。
const stale = A.fromMeta(
  { approval_id: AP_ID },
  batch([
    {
      approval_id: AP_ID,
      status: "PENDING",
      actionable: false,
      preview: {},
      expires_at: "2026-11-12T08:00:00Z"
    }
  ])
);
check("过期的单子说成过期而不是失败", stale.done, "已过期，未执行——需要重新发起");
check("过期的单子不可点", stale.open, false);

// ---- 单子不见了：两种原因不能混 ----
const missing = A.fromMeta({ approval_id: AP_ID }, batch([]));
check("查不到单子", missing.done, "查不到这张审批单（可能已被清理）。");
check("查不到单子不留按钮", missing.open, false);

const offline = A.fromMeta({ approval_id: AP_ID }, { ok: false, items: {} });
check("接口失败不等于单子没了", offline.done, "审批状态读取失败，刷新重试。");
// 读不到状态时也不能给按钮：点下去同样不知道会怎样。
check("读不到状态时不留按钮", offline.open, false);

// ---- 委派单：卡上要写清是谁转交的 ----
const delegated = A.fromMeta(
  { approval_id: AP_ID },
  batch([
    {
      approval_id: AP_ID,
      status: "PENDING",
      actionable: true,
      preview: { tool_name: "create_maintenance_ticket", delegation: { from: "cs", depth: 1 } }
    }
  ])
);
check("委派信息要还原到卡上", delegated.preview.delegation, { from: "cs", depth: 1 });

// ---- 词表 ----
check("未知状态原样显示", A.statusText("SOMETHING_NEW"), "SOMETHING_NEW");
check("没有状态时不写空白", A.statusText(null), "");

// ---- 409：带状态的冲突是结论，不是「这次没提交成功」----
//
// 确认/取消在服务端用 409 表示「当前状态不允许执行」。把它当网络抖动处理，
// 卡片会留着按钮等人再点，而那张单子已经不可能执行了。
function conflict(body) {
  const err = new Error("conflict");
  err.status = 409;
  err.payload = body;
  return A.settledFromError(err);
}

check(
  "执行期过期：卡上写过期并带上服务端那句话",
  conflict({
    status: "EXPIRED",
    success: false,
    message: "该操作预览已过期，请重新生成预览。"
  }),
  "已过期，未执行——需要重新发起 · 该操作预览已过期，请重新生成预览。"
);
check(
  "预检后被人取消：同样按结论处理",
  conflict({ status: "REJECTED", success: false, message: "当前审批状态为 REJECTED，不能执行。" }),
  "已取消，未执行 · 当前审批状态为 REJECTED，不能执行。"
);
check(
  "带错误码的冲突要同时给出错误码",
  conflict({ status: "EXECUTION_UNKNOWN", error_code: "TIMEOUT" }),
  "结果未知，不要重发（TIMEOUT）"
);

// 没有 body 的 409 只能按失败处理：我们确实不知道单子变成了什么，
// 替服务端写结论就是把「不知道」说成了「已确定」。
check("没有 body 的 409 不写结论", conflict(undefined), null);
check("body 里没有状态的 409 不写结论", conflict({ detail: "Bad Gateway" }), null);
check(
  "非 409 一律不算结论",
  (() => {
    const err = new Error("boom");
    err.status = 500;
    err.payload = { status: "EXPIRED" };
    return A.settledFromError(err);
  })(),
  null
);

if (failures) {
  console.error(`\n${failures} 项未通过`);
  process.exit(1);
}
console.log("check_approval_card: all passed");
