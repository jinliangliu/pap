// 「策略版本」卡片（运行配置页）的自检脚本。
//
// 用法：node scripts/check_policy_revision.mjs
// 依赖 jsdom，装在仓库外：mkdir -p /tmp/pap-jsdom && cd /tmp/pap-jsdom && npm i jsdom
//
// 为什么值得单独验：
// - 这张卡只有编号没有内容时等于没做。工具审计里写着 `policy_revision: 7`，
//   人点开这里必须能真取回第 7 版的值，否则「按号追溯」还是靠猜。
// - 查不到的版本必须说成「这一版不存在」，不能落成一句通用的读取失败——
//   接口刻意返回 200 + found=false 就是为了区分这两件事。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const jsdomRoot = process.env.PAP_JSDOM || path.join(os.tmpdir(), "pap-jsdom");
const require = createRequire(path.join(jsdomRoot, "noop.js"));

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (e) {
  console.error(`找不到 jsdom（${jsdomRoot}）。见文件头的安装命令。`);
  process.exit(2);
}

const SNAPSHOT_7 = {
  revision: 7,
  found: true,
  settings: {
    "knowledge.max_chunks_per_document": 1600,
    "permissions.default_action": "allow",
    "tool.backup_enabled": true,
    "tool.backup_max_size_mb": 3000,
    "tool.backup_threshold_mb": 100,
  },
};

function reply(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  };
}

const asked = [];

const html = fs.readFileSync(path.join(root, "static/console/index.html"), "utf8");
const dom = new JSDOM(html, {
  url: "http://localhost/#/runtime",
  runScripts: "outside-only",
  pretendToBeVisual: true,
});

const { window } = dom;

window.fetch = async (url) => {
  const href = String(url);

  if (href.indexOf("/api/v1/me") === 0) {
    return reply({
      subject: "alex",
      tenant_id: "default",
      roles: ["platform_admin"],
      is_platform_admin: true,
    });
  }

  if (href.indexOf("/api/v1/admin/runtime-settings/snapshots/") === 0) {
    const revision = parseInt(href.split("/").pop(), 10);
    asked.push(revision);
    if (revision === 7) return reply(SNAPSHOT_7);
    return reply({ revision, settings: {}, found: false });
  }

  if (href.indexOf("/api/v1/admin/runtime-settings") === 0) {
    return reply({
      observability_enabled: true,
      observability_source: "runtime",
      default_tool_action: "allow",
      default_tool_action_source: "env",
      mcp_inbound_enabled: true,
      mcp_inbound_source: "env",
      knowledge_max_chunks: 2000,
      knowledge_max_chunks_source: "runtime",
      revision: 7,
      tool_backup_enabled: true,
      tool_backup_enabled_source: "env",
      tool_backup_threshold_mb: 100,
      tool_backup_threshold_mb_source: "env",
      tool_backup_max_size_mb: 3000,
      tool_backup_max_size_mb_source: "env",
    });
  }

  return reply({ items: [] });
};

for (const file of ["approval.js", "render.js", "clarify.js", "rail.js", "console.js"]) {
  window.eval(fs.readFileSync(path.join(root, "static/console/assets", file), "utf8"));
}

const wait = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

let failures = 0;

function check(name, condition, detail) {
  if (condition) return;
  failures += 1;
  console.error(`FAIL ${name}${detail ? "\n  " + detail : ""}`);
}

const $ = (sel) => window.document.querySelector(sel);
const text = (sel) => {
  const node = $(sel);
  return node ? node.textContent.trim() : "";
};

await wait(120);

const card = $("#rt-rev-card");
check("运行配置页有「策略版本」卡", !!card);
if (!card) {
  console.error("后续断言无法进行。");
  process.exit(1);
}

check("卡片标题写明策略版本", text("#rt-rev-card .card-sub").indexOf("真的改变") > 0, text("#rt-rev-card .card-sub"));
check("chip 显示当前版本号", text("#rt-rev-chip") === "第 7 版", text("#rt-rev-chip"));
check("输入框默认填当前版本", ($("#rt-rev-input") || {}).value === "7", ($("#rt-rev-input") || {}).value);

// 一屏之内同一件事只出现一次：版本卡不应与容量卡合并或重复出现。
check(
  "只出现一张版本卡",
  window.document.querySelectorAll("#rt-rev-card").length === 1,
  String(window.document.querySelectorAll("#rt-rev-card").length)
);

// 位置：紧跟在入库容量之后。放在最上面会让人以为它是总开关，
// 放到只读区之后就再也找不到「按号取回」这个动作了。
const capCard = $("#rt-cap-card");
check("入库容量卡也在同一页", !!capCard);
if (capCard) {
  const relation = capCard.compareDocumentPosition(card);
  check(
    "版本卡排在容量卡之后",
    (relation & window.Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
    String(relation)
  );
}

// ---- 取回某一版 ----
$("#rt-rev-input").value = "7";
$("#rt-rev-load").click();
await wait(90);

check("确实按号请求了快照", asked.indexOf(7) >= 0, JSON.stringify(asked));

const rows = Array.from(window.document.querySelectorAll("#rt-rev-out dt")).map((n) => n.textContent.trim());
check("取回后列出配置项", rows.length === 5, JSON.stringify(rows));
check("列表按点名给出", rows[0] === "knowledge.max_chunks_per_document", JSON.stringify(rows));
check(
  "值来自那一版而不是当前值",
  text("#rt-rev-out").indexOf("1600") > 0,
  text("#rt-rev-out")
);
check("提示说明这是整份快照", text("#rt-rev-hint").indexOf("整份") > 0, text("#rt-rev-hint"));

// ---- 查一个不存在的版本 ----
$("#rt-rev-input").value = "99";
$("#rt-rev-load").click();
await wait(90);

check("不存在的版本不残留上一次的结果", $("#rt-rev-out").innerHTML.trim() === "", $("#rt-rev-out").innerHTML);
check("提示说成「不存在」而不是通用失败", text("#rt-rev-hint").indexOf("第 99 版不存在") >= 0, text("#rt-rev-hint"));
check("没有把它说成接口错误", text("#rt-rev-hint").indexOf("读取失败") < 0, text("#rt-rev-hint"));

// ---- 非法输入不发请求 ----
const before = asked.length;
$("#rt-rev-input").value = "";
$("#rt-rev-load").click();
await wait(60);
check("空值不发请求", asked.length === before, JSON.stringify(asked));
check("空值给出可照做的提示", text("#rt-rev-hint").indexOf("请输入一个版本号") >= 0, text("#rt-rev-hint"));

if (failures) {
  console.error(`check_policy_revision: ${failures} 项失败`);
  process.exit(1);
}

console.log("check_policy_revision: all passed");
