// MCP 接入项「风险等级」下拉的自检脚本。
//
// 用法：node scripts/check_mcp_risk.mjs
// 依赖 jsdom，装在仓库外：mkdir -p /tmp/pap-jsdom && cd /tmp/pap-jsdom && npm i jsdom
//
// 为什么需要它：接入是批量做的（连接器「发现工具」一次接几十个），只能填一个
// 统一的保守值，于是连「查一次压力」都弹审批单。风险等级要能按工具改回来。
// 后端有 PATCH，但如果页面上没有入口、或者改完不刷新，人只会以为「改不动」——
// pytest 全绿也发现不了。这里盯三件事：
//   1. 详情页对管理员给出下拉，对非管理员不给；
//   2. 改一下真的发了 PATCH，body 只有 risk_level；
//   3. 改完重新拉了一次接入项（否则页面写着「读取」、网关还是「写入」）。
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

const LIVE_TOOLS = [
  {
    name: "fc_querypressure",
    display_name: "查询压力",
    description: "查询站点实时压力。",
    kind: "mcp",
    source: "mcp",
    risk_level: "read",
    requires_approval: false,
    enabled: true,
    timeout_seconds: 30,
    input_schema: { type: "object", properties: { station: { type: "string" } } },
  },
  {
    name: "fc_executestationcontrol",
    display_name: "下发控制",
    description: "提交控制指令。",
    kind: "mcp",
    source: "mcp",
    risk_level: "write",
    requires_approval: true,
    enabled: true,
    timeout_seconds: 30,
    input_schema: { type: "object", properties: {} },
  },
];

const REGISTERED = {
  items: [
    {
      id: 1,
      local_name: "fc_querypressure",
      remote_name: "queryPressure",
      display_name: "查询压力",
      description: "查询站点实时压力。",
      risk_level: "read",
      requires_approval: false,
      timeout_seconds: 30,
      enabled: true,
      registered: true,
      token_set: false,
      // 经连接器接入：令牌在这一行上永远是空的，凭据按租户存在连接器那边。
      token_source: "connector",
      connector_name: "prv",
      connector_credential_set: true,
      created_at: "2026-11-19T06:00:00Z",
    },
    {
      id: 2,
      local_name: "fc_executestationcontrol",
      remote_name: "executeStationControl",
      display_name: "下发控制",
      description: "提交控制指令。",
      risk_level: "write",
      requires_approval: true,
      timeout_seconds: 30,
      enabled: true,
      registered: true,
      token_set: false,
      // 同一批工具，但本租户在这条连接器上还没配凭据——这才是真的调不通。
      token_source: "connector",
      connector_name: "prv",
      connector_credential_set: false,
      created_at: "2026-11-19T06:00:00Z",
    },
  ],
};

function buildBox(asAdmin) {
  const patches = [];
  let registeredHits = 0;

  function reply(payload, status = 200) {
    return {
      ok: status >= 200 && status < 300,
      status,
      text: async () => JSON.stringify(payload),
    };
  }

  const html = fs.readFileSync(path.join(root, "static/console/index.html"), "utf8");
  const dom = new JSDOM(html, {
    url: "http://localhost/#/tools",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });

  const { window } = dom;
  window.fetch = async (url, init) => {
    const href = String(url);
    const method = (init && init.method) || "GET";

    if (href.indexOf("/api/v1/me") === 0) {
      return reply({
        subject: "alex",
        tenant_id: "default",
        roles: asAdmin ? ["platform_admin"] : ["member"],
        is_platform_admin: asAdmin,
      });
    }
    if (href.indexOf("/api/v1/admin/mcp/tools/") === 0 && method === "PATCH") {
      patches.push({ url: href, body: JSON.parse((init && init.body) || "{}") });
      return reply(Object.assign({}, REGISTERED.items[0], { risk_level: "normal" }));
    }
    if (href.indexOf("/api/v1/admin/mcp/tools") === 0) {
      registeredHits += 1;
      return reply(REGISTERED);
    }
    if (href.indexOf("/api/v1/admin/tools") === 0) return reply({ items: [] });
    if (href.indexOf("/api/v1/tools") === 0) return reply(LIVE_TOOLS);
    return reply({ items: [] });
  };

  for (const file of ["approval.js", "render.js", "clarify.js", "rail.js", "console.js"]) {
    window.eval(fs.readFileSync(path.join(root, "static/console/assets", file), "utf8"));
  }

  return { window, patches, hits: () => registeredHits };
}

const wait = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

let failures = 0;
function check(name, condition, detail) {
  if (condition) return;
  failures += 1;
  console.error(`FAIL ${name}${detail ? "\n  " + detail : ""}`);
}

async function openMcpDetail(box) {
  const { window } = box;
  await wait(120);

  const mcpTab = Array.from(window.document.querySelectorAll("#tool-tabs .tab")).find(
    (btn) => btn.getAttribute("data-kind") === "mcp"
  );
  if (mcpTab) {
    mcpTab.click();
    await wait();
  }

  const row = Array.from(window.document.querySelectorAll(".tool-row")).find(
    (item) => item.getAttribute("data-name") === "fc_querypressure"
  );
  if (row) {
    row.click();
    await wait(80);
  }
  return row;
}

// ---- 管理员：有下拉，改一下真的发 PATCH ----
const admin = buildBox(true);
const row = await openMcpDetail(admin);
check("MCP 列表里有接入项", !!row, "没渲染出 .tool-row，后续断言无从进行");

const select = admin.window.document.querySelector("[data-mcp-risk]");
check("详情页给出风险等级下拉", !!select);
if (select) {
  check("下拉带着当前值", select.value === "read", select.value);
  check("下拉只作用于这一条接入项", select.getAttribute("data-mcp-risk") === "fc_querypressure");

  const before = admin.hits();
  select.value = "normal";
  select.dispatchEvent(new admin.window.Event("change", { bubbles: true }));
  await wait(140);

  check("改一下发了 PATCH", admin.patches.length === 1, `实际 ${admin.patches.length} 次`);
  if (admin.patches.length) {
    const call = admin.patches[0];
    check("PATCH 打到这条工具上", call.url.indexOf("/tools/fc_querypressure") > 0, call.url);
    check(
      "body 只带 risk_level（别的字段不动）",
      JSON.stringify(Object.keys(call.body)) === '["risk_level"]',
      JSON.stringify(call.body)
    );
    check("新值原样发出", call.body.risk_level === "normal", call.body.risk_level);
  }
  check("改完重新拉了接入项", admin.hits() > before, "不重拉的话页面写着改过了、网关还是旧值");
}

// ---- 令牌来源：经连接器接入时不能说「未配置」 ----
// 行上没有令牌是设计如此（端点与凭据都由连接器接管），写成「未配置」会和
// 「连接器停用 / 本租户没配凭据」混成一句——后者才是真的调不通。
const detail = admin.window.document.querySelector(".tool-detail-card");
const detailText = detail ? detail.textContent : "";
check("详情页说清令牌由哪个连接器提供", detailText.indexOf("由连接器「prv」提供") >= 0, detailText);
check("连接器凭据已配置时给出明确结论", detailText.indexOf("本租户已配置") >= 0);
check(
  "不再出现「端点令牌：未配置」那种把设计说成故障的文案",
  detailText.indexOf("端点令牌") === -1 && detailText.indexOf("未配置") === -1,
  detailText
);

const controlRow = Array.from(admin.window.document.querySelectorAll(".tool-row")).find(
  (item) => item.getAttribute("data-name") === "fc_executestationcontrol"
);
if (controlRow) {
  controlRow.click();
  await wait(80);
  const text = admin.window.document.querySelector(".tool-detail-card").textContent;
  check(
    "本租户没配凭据时如实标红",
    text.indexOf("本租户未配置凭据") >= 0,
    "这条才是真的调不通，不能和「设计如此」显示成同一句话"
  );
}

// ---- 非管理员：不给入口 ----
const member = buildBox(false);
await openMcpDetail(member);
check(
  "非管理员看不到风险等级下拉",
  !member.window.document.querySelector("[data-mcp-risk]"),
  "改风险级要平台管理员，页面上给个点不动的控件只会骗人"
);

if (failures) {
  console.error(`\n${failures} 项不符。`);
  process.exit(1);
}
console.log("ok：风险等级下拉可用，PATCH 正确，改完会重拉。");
