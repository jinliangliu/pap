// 「发现 / 管理远端工具」弹窗的自检脚本。
//
// 用法：node scripts/check_connector_discover.mjs
// 依赖 jsdom，装在仓库外：mkdir -p /tmp/pap-jsdom && cd /tmp/pap-jsdom && npm i jsdom
//
// 为什么需要它：这一改挡住了「工具名与说明挤在 300px 卡片里」和
// 「已接入的行点不动、接进去就退不出来」两个问题——数据接口照旧返回全部字段，
// pytest 全绿，但人还是看不清、也改不了。jsdom 不做布局，所以结构层之外，
// 还会读一次 CSS 源确认三列网格还在（说明退回上下堆叠时这条会 FAIL）。
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

const LONG_DESC =
  "生成控制预览（**本工具不向设备下发任何指令**）。\n" +
  "它检查站点、动作白名单、回路启用、参数与未决命令，通过后创建一张 5 分钟有效的预览。\n" +
  "action 取值：close_main / close_backup / high_pressure / low_pressure。";

const TOOLS = {
  endpoint: "http://127.0.0.1:8765/mcp",
  tools: [
    {
      remote_name: "prepareStationControl",
      description: LONG_DESC,
      bound_local_name: "",
    },
    {
      remote_name: "queryIOAlarms",
      description: "查询 IO 报警记录，支持按站点与时间段过滤。",
      bound_local_name: "",
    },
    {
      remote_name: "listStations",
      description: "列出当前账号可见的站点。",
      bound_local_name: "prv_listStations",
    },
  ],
};

// 本地绑定表比 discover 多一条 legacy_ping：远端已经下架，
// 它只在这份记录里，界面必须给出解除入口。
const BOUND = [
  {
    local_name: "prv_listStations",
    remote_name: "listStations",
    display_name: "listStations",
    risk_level: "write",
    enabled: true,
    registered: true,
  },
  {
    local_name: "legacy_ping",
    remote_name: "ping",
    display_name: "ping",
    risk_level: "read",
    enabled: true,
    registered: true,
  },
];

const CONNECTORS = {
  items: [
    {
      id: 2,
      name: "prv",
      display_name: "FiCloud",
      description: "",
      kind: "mcp",
      base_url: "http://127.0.0.1:8765/mcp",
      auth_type: "bearer",
      auth_header_name: "",
      timeout_seconds: 30,
      enabled: true,
      health_status: "ok",
      health_message: "连接正常，远端提供 3 个工具。",
      health_checked_at: "2026-11-19T05:35:08Z",
      credential_set: true,
      tool_count: 2,
    },
  ],
};

const posts = [];
const deletes = [];

function reply(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  };
}

const html = fs.readFileSync(path.join(root, "static/console/index.html"), "utf8");
const dom = new JSDOM(html, {
  url: "http://localhost/#/connectors",
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
      roles: ["platform_admin"],
      is_platform_admin: true,
    });
  }
  if (href.indexOf("/api/v1/connectors/prv/discover") === 0) return reply(TOOLS);
  if (href.indexOf("/api/v1/connectors/prv/tools/") === 0 && method === "DELETE") {
    deletes.push(href.split("/").pop());
    return reply({ message: "已取消接入" });
  }
  if (href.indexOf("/api/v1/connectors/prv/tools") === 0) {
    if (method === "POST") {
      posts.push({ url: href, method, body: JSON.parse((init && init.body) || "{}") });
      return reply({ connector: "prv", bound: ["pk_preparestationcontrol"], message: "已接入 1 个工具" });
    }
    return reply(BOUND);
  }
  if (href.indexOf("/api/v1/connectors") === 0) return reply(CONNECTORS);
  return reply({ items: [] });
};

for (const file of ["approval.js", "render.js", "clarify.js", "rail.js", "console.js"]) {
  window.eval(fs.readFileSync(path.join(root, "static/console/assets", file), "utf8"));
}

const wait = (ms = 50) => new Promise((resolve) => setTimeout(resolve, ms));

let failures = 0;

function check(name, condition, detail) {
  if (condition) return;
  failures += 1;
  console.error(`FAIL ${name}${detail ? "\n  " + detail : ""}`);
}

const $ = (sel) => window.document.querySelector(sel);
const $$ = (sel) => Array.from(window.document.querySelectorAll(sel));

await wait(90);

const openBtn = $('[data-act="discover"]');
check("卡片上有「发现工具」入口", !!openBtn);
if (!openBtn) {
  console.error("后续断言无法进行。");
  process.exit(1);
}

check("发现工具不再内联在卡片里", !$("#conn-grid .conn-panel"));

openBtn.click();
await wait(90);

const mask = $(".modal-mask");
const modal = mask && mask.querySelector(".modal");
check("点击后弹出弹窗", !!modal);
check("弹窗按宽版排版", !!(modal && modal.classList.contains("modal-wide")));

const head = mask && mask.querySelector(".disc-head");
check("工具有表头", !!head);
if (head) {
  const labels = Array.from(head.querySelectorAll("span")).map((n) => n.textContent.trim());
  check(
    "表头分「工具名称」「说明」「风险等级」三列",
    labels[1] === "工具名称" && labels[2] === "说明" && labels[3] === "风险等级",
    labels.join(" | ")
  );
}

const rows = $$(".disc-row");
// 3 条来自远端清单 + 1 条「远端已不提供」的本地遗留。
check("工具逐行列出（含远端已下架的那条）", rows.length === 4, `实际 ${rows.length} 行`);

if (rows.length) {
  const first = rows[0];
  const nameCell = first.querySelector(".disc-name");
  const descCell = first.querySelector(".disc-desc");
  check("名称与说明是两个并列节点，不是父子嵌套", !!nameCell && !!descCell && !nameCell.contains(descCell));
  check("长说明完整保留", descCell && descCell.textContent.indexOf("5 分钟有效的预览") >= 0);
  check(
    "说明里的 markdown 强调符去掉",
    descCell && descCell.textContent.indexOf("**") === -1,
    descCell && descCell.textContent.slice(0, 40)
  );
  check(
    "说明保留原文换行",
    descCell && /\n/.test(descCell.textContent),
    "换行被压掉了，分条的动作取值会挤成一段"
  );
}

const boundRow = $$(".disc-row.bound").find((row) => row.textContent.indexOf("prv_listStations") >= 0);
check("已接入的行有标记", !!boundRow);
if (boundRow) {
  const box = boundRow.querySelector("input");
  check("已接入的行默认勾选", box.checked === true);
  // 这条是本次修复的核心：禁用就再也取消不了。
  check("已接入的行可以取消勾选", box.disabled === false);
  check("已接入的行写明本地名", boundRow.textContent.indexOf("已接入 · prv_listStations") >= 0);
}

// ---- 逐行风险等级 ----
// 一个整批值不够用：三十个工具共用一档时，「查一次压力」与「下发关阀」
// 只能同档，前者天天弹审批。所以每行一个下拉，初值按工具名推断。
const riskOf = (remote) => {
  const row = $$(".disc-row").find((r) => {
    const box = r.querySelector("input[data-tool]");
    return !!box && box.getAttribute("data-tool") === remote;
  });
  const sel = row && row.querySelector("select[data-risk]");
  return { row, sel, value: sel ? sel.value : null };
};

const prep = riskOf("prepareStationControl");
check("未接入的行有风险下拉", !!prep.sel);
check("下拉只作用于这一行", prep.sel && prep.sel.getAttribute("data-risk") === "prepareStationControl");
check(
  "写着 control/prepare 的工具默认「写入」，不擅自降到读取",
  prep.value === "write",
  String(prep.value)
);
check(
  "查询类工具默认「读取」，不再一律写入",
  riskOf("queryIOAlarms").value === "read",
  String(riskOf("queryIOAlarms").value)
);
check("已接入的行不给下拉", !boundRow || !boundRow.querySelector("select[data-risk]"));
check(
  "已接入的行显示当前档位",
  !!boundRow && boundRow.textContent.indexOf("写入") >= 0,
  boundRow && boundRow.textContent.slice(0, 60)
);
check(
  "下拉的选项与详情页同序同数",
  prep.sel && prep.sel.options.length === 6,
  prep.sel && String(prep.sel.options.length)
);

const staleRow = $(".disc-row.stale");
check("远端已下架的工具也列出来", !!staleRow, "它只存在于本地绑定表，不给入口就永远解除不了");
if (staleRow) {
  check("下架行默认勾选", staleRow.querySelector("input").checked === true);
  check("下架行说明原因", staleRow.textContent.indexOf("远端已不提供") >= 0);
  check("下架行不带风险下拉", !staleRow.querySelector("select[data-risk]"));
  check(
    "下架行显示本地记录的档位",
    staleRow.textContent.indexOf("读取") >= 0,
    staleRow.textContent.slice(0, 80)
  );
}

// 样式源这一层挡的是「改回上下堆叠」：jsdom 不算网格，只能看声明。
const css = fs.readFileSync(path.join(root, "static/console/assets/console.css"), "utf8");
check(
  ".disc-row 声明了四列网格（勾选 + 名称 + 说明 + 风险）",
  /\.disc-head,\s*\.disc-row\s*\{[^}]*grid-template-columns:\s*22px[^;]*1fr\s+96px/s.test(css),
  "风险列被挤掉时，下拉会掉到下一行、三十行看起来像六十行"
);
check("旧的内联工具列表样式已移除", css.indexOf(".conn-tool-desc") === -1);

const bindBtn = $("#disc-bind");
const countLine = $("#disc-count");
check("未改勾选时「保存变更」不可点", !!bindBtn && bindBtn.disabled === true, countLine && countLine.textContent);
check(
  "计数写清当前接入数与变更",
  !!countLine && countLine.textContent === "已接入 2 个 · 勾选状态与当前一致",
  countLine && countLine.textContent
);

const firstBox = rows[0] && rows[0].querySelector("input");
if (firstBox) {
  firstBox.checked = true;
  firstBox.dispatchEvent(new window.Event("change", { bubbles: true }));
  await wait();
}
check("勾选新工具后按钮可用", !!bindBtn && bindBtn.disabled === false);
check("只有新增时按钮说「接入选中」", bindBtn.textContent === "接入选中", bindBtn.textContent);
check(
  "计数写出本次新增数与风险分布",
  countLine.textContent === "已接入 2 个 · 本次新增 1（写入 1）",
  countLine.textContent
);

// 批量设置：一次接三十个时，逐个点三十次下拉不现实；但落点必须是逐行下拉，
// 这样批量之后还能逐条反悔。
const batchRisk = $("#conn-risk");
check("有「批量设为」下拉", !!batchRisk);
check("批量下拉默认「不批量改动」，打开弹窗不会悄悄改档", batchRisk.value === "", batchRisk.value);
batchRisk.value = "read";
$("#disc-risk-apply").click();
await wait();
check("批量应用落到这一行的下拉上", prep.sel.value === "read", prep.sel.value);
check(
  "计数按档位重新分组",
  countLine.textContent === "已接入 2 个 · 本次新增 1（读取 1）",
  countLine.textContent
);

// 逐行改回来：批量只是起点，最终值以每行下拉为准。
prep.sel.value = "normal";
prep.sel.dispatchEvent(new window.Event("change", { bubbles: true }));
await wait();
check(
  "逐行改档后计数跟着变",
  countLine.textContent === "已接入 2 个 · 本次新增 1（普通 1）",
  countLine.textContent
);

const prefixInput = $("#conn-prefix");
if (prefixInput) prefixInput.value = "pk_";
bindBtn.click();
await wait(140);

check("接入发到了 /tools", posts.length === 1 && posts[0].method === "POST", JSON.stringify(posts));
if (posts.length) {
  const sent = posts[0].body.items || [];
  check("只提交新增的那一个", sent.length === 1, JSON.stringify(sent));
  check("本地名带上前缀", sent[0] && sent[0].local_name === "pk_preparestationcontrol", sent[0] && sent[0].local_name);
  check(
    "提交的是这一行选中的档位，不是整批默认值",
    sent[0] && sent[0].risk_level === "normal",
    sent[0] && sent[0].risk_level
  );
}

check("保存成功后弹窗自动关闭", !$(".modal-mask"));

// 解除路径：再开一次，取消一条已接入的。
$('[data-act="discover"]').click();
await wait(90);

const again = $$(".disc-row.bound").find((row) => row.textContent.indexOf("prv_listStations") >= 0);
check("再次打开仍能看到已接入的行", !!again);
if (again) {
  const box = again.querySelector("input");
  box.checked = false;
  box.dispatchEvent(new window.Event("change", { bubbles: true }));
  await wait();

  check("取消勾选后按钮说「解除选中」", $("#disc-bind").textContent === "解除选中", $("#disc-bind").textContent);
  check(
    "计数写出本次解除数",
    $("#disc-count").textContent === "已接入 2 个 · 本次解除 1",
    $("#disc-count").textContent
  );

  $("#disc-bind").click();
  await wait(140);

  check("解除发的是 DELETE", deletes.length === 1 && deletes[0] === "prv_listStations", JSON.stringify(deletes));
  check("解除时不会顺手再 POST", posts.length === 1, JSON.stringify(posts));
  check("解除成功后弹窗关闭", !$(".modal-mask"));
}

// 批量入口：一次接进三十个工具之后，逐个点三十下反悔不合理。
$('[data-act="discover"]').click();
await wait(90);
check("能再次打开弹窗", !!$(".modal-mask"));

const bulkNone = $('[data-bulk="none"]');
check("有「全不选」入口", !!bulkNone);
if (bulkNone) {
  bulkNone.click();
  await wait();
  check(
    "全不选后没有勾选项",
    $$("input[data-tool]").every((box) => !box.checked),
    $$("input[data-tool]").map((box) => box.getAttribute("data-tool")).join(",")
  );
  check("全不选后按钮说「解除选中」", $("#disc-bind").textContent === "解除选中", $("#disc-bind").textContent);
  check(
    "全不选后计数写出解除数",
    $("#disc-count").textContent === "已接入 2 个 · 本次解除 2",
    $("#disc-count").textContent
  );
}

window.document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape" }));
await wait();
check("Esc 能关闭弹窗", !$(".modal-mask"));

// 停用的连接器：发现/接入都还能做，但工具不会进目录。不提示的话，
// 人接完三十个工具去对话里问，会以为「接入没成功」。
CONNECTORS.items[0].enabled = false;
window.location.hash = "#/tools";
await wait(120);
window.location.hash = "#/connectors";
await wait(150);
$('[data-act="discover"]').click();
await wait(90);
const disabledHint = $(".modal-body .banner.warn");
check(
  "停用连接器在弹窗里给出提示",
  !!disabledHint && disabledHint.textContent.includes("已停用"),
  disabledHint ? disabledHint.textContent.slice(0, 40) : "(没有提示)"
);
window.document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape" }));
await wait();

// 一次点击只该开一层、点一次就该关干净。
//
// `mountConnectors` 把事件委托挂在常驻的 `#view` 上，而 `render()` 只清
// `innerHTML`、元素本身不换——于是每进一次连接器页就多挂一份监听器。
// 挂了几份，点一下「发现工具」就叠几层遮罩；关窗时点掉最上面那层，下面
// 那层还在，用户看到的就是「这个窗口要点好几次才关得掉」。
// 这里模拟两次进出，再点一次。
window.location.hash = "#/tools";
await wait(120);
window.location.hash = "#/connectors";
await wait(150);

$('[data-act="discover"]').click();
await wait(90);
const stacked = $$(".modal-mask").length;
check("一次点击只开一层弹窗", stacked === 1, `叠了 ${stacked} 层遮罩`);

const anyClose = $(".modal-mask .modal-close");
if (anyClose) anyClose.click();
await wait();
check(
  "点一次关闭就关干净",
  $$(".modal-mask").length === 0,
  `还剩 ${$$(".modal-mask").length} 层没关`
);

if (failures) {
  console.error(`\n${failures} 条断言失败。`);
  process.exit(1);
}

console.log("connector discover modal: OK");
