// 连接器表单的自检脚本（认证方式 → 请求头名输入框的联动）。
//
// 用法：node scripts/check_connector_form.mjs
// 依赖 jsdom，装在仓库外（本项目无构建、无第三方依赖）：
//   mkdir -p /tmp/pap-jsdom && cd /tmp/pap-jsdom && npm i jsdom
// 通过后用 PAP_JSDOM=/tmp/pap-jsdom 指过来，默认找 /tmp/pap-jsdom/node_modules。
//
// 为什么需要它：这一改动挡的是「把令牌贴进请求头名」——被挡住时界面上完全
// 没有异常，只有抓包或翻库才看得出。纯 pytest 碰不到 DOM，pytest 里也就
// 一条断言都没有。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// Windows 的 /tmp 与 node 眼里的 /tmp 不是同一个目录，所以默认用 os.tmpdir()。
const jsdomRoot = process.env.PAP_JSDOM || path.join(os.tmpdir(), "pap-jsdom");
const require = createRequire(path.join(jsdomRoot, "noop.js"));

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (e) {
  console.error(`找不到 jsdom（${jsdomRoot}）。见文件头的安装命令。`);
  process.exit(2);
}

const TOKEN = "pk_mcp_9f2b7c1d4e5a6b7c8d9e0f1a2b3c4d5e";

const CONNECTORS = {
  items: [
    {
      id: 1,
      name: "part_keeper",
      display_name: "PartKeeper",
      description: "",
      kind: "mcp",
      base_url: "http://127.0.0.1:8000/mcp",
      auth_type: "bearer",
      // 历史行：令牌就躺在这个字段里。编辑弹窗不该把它当设置显示出来。
      auth_header_name: TOKEN,
      timeout_seconds: 30,
      enabled: true,
      health_status: "error",
      health_message: "尚未配置凭据，无法校验。",
      health_checked_at: null,
      credential_set: true,
      tool_count: 0,
    },
  ],
};

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

window.fetch = async (url) => {
  const text = String(url);
  if (text.indexOf("/api/v1/me") === 0 || text.indexOf("/api/v1/me?") === 0) {
    return reply({
      subject: "alex",
      tenant_id: "default",
      roles: ["platform_admin"],
      is_platform_admin: true,
    });
  }
  if (text.indexOf("/api/v1/connectors") === 0) return reply(CONNECTORS);
  return reply({ items: [] });
};

for (const file of ["approval.js", "render.js", "clarify.js", "rail.js", "console.js"]) {
  window.eval(fs.readFileSync(path.join(root, "static/console/assets", file), "utf8"));
}

const wait = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));

let failures = 0;

function check(name, condition, detail) {
  if (condition) return;
  failures += 1;
  console.error(`FAIL ${name}${detail ? "\n  " + detail : ""}`);
}

const $ = (sel) => window.document.querySelector(sel);

await wait(60);

check("连接器页渲染出来了", !!$("#conn-add"), "页面上没有「新建连接器」按钮");

// 新建表单：默认认证方式是 bearer，头名输入框必须是禁用且空的。
$("#conn-add").click();
await wait();

const newAuth = $("#new-auth");
const newHeader = $("#new-header");
check("新建表单出现", !!newAuth && !!newHeader);

if (newAuth && newHeader) {
  check("新建时默认认证方式是 bearer", newAuth.value === "bearer", newAuth.value);
  check("bearer 下头名输入框禁用", newHeader.disabled === true);

  newAuth.value = "header";
  newAuth.dispatchEvent(new window.Event("change", { bubbles: true }));
  await wait();
  check("切到「自定义请求头」后解禁", newHeader.disabled === false);

  newHeader.value = "X-Api-Key";
  newAuth.value = "bearer";
  newAuth.dispatchEvent(new window.Event("change", { bubbles: true }));
  await wait();
  check("切回 bearer 后重新禁用", newHeader.disabled === true);
  check("切回 bearer 后清空已填内容", newHeader.value === "", newHeader.value);
}

// 编辑面板：历史行里的令牌不能回显到一个「看起来像设置」的框里。
const editBtn = window.document.querySelector('[data-act="edit"]');
check("卡片上有「编辑」入口", !!editBtn);

if (editBtn) {
  editBtn.click();
  await wait();
  const connAuth = $("#conn-auth");
  const connHeader = $("#conn-header");
  check("编辑面板出现", !!connAuth && !!connHeader);

  if (connAuth && connHeader) {
    check("bearer 行编辑时头名禁用", connHeader.disabled === true);
    check(
      "bearer 行编辑时不回显 auth_header_name",
      connHeader.value === "",
      `实际值：${connHeader.value}`
    );

    connAuth.value = "header";
    connAuth.dispatchEvent(new window.Event("change", { bubbles: true }));
    await wait();
    check("编辑面板切到 header 后解禁", connHeader.disabled === false);
  }
}

if (failures) {
  console.error(`\n${failures} 条断言失败。`);
  process.exit(1);
}
