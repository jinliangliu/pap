// 沙箱事务配额入口的自检脚本。
//
// 用法：node scripts/check_sandbox_quota.mjs
// 依赖 jsdom，装在仓库外：mkdir -p /tmp/pap-jsdom && cd /tmp/pap-jsdom && npm i jsdom
//
// 验的不是「字段能显示」这么简单：
// - 非管理员连入口都不该看到，后端另有 require_platform_admin 兜底；
// - 页面填 MB、接口存字节，换算错了不会报错，只会让上限差 1048576 倍；
// - 保存必须把三项沙箱配额一起提交，只改常规项会让「管理员配过」变成假象。
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

const TENANTS = [
  {
    tenant_id: "acme",
    name: "ACME",
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
  },
];

const QUOTA = {
  tenant_id: "acme",
  daily_chat_requests: 1000,
  tool_calls_per_minute: 60,
  knowledge_max_documents: 500,
  max_concurrent_tasks: 2,
  daily_cpu_seconds: 1800,
  workspace_max_bytes: 512 * 1024 * 1024,
};

function reply(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  };
}

function makeDom(admin) {
  const html = fs.readFileSync(path.join(root, "static/console/index.html"), "utf8");
  const dom = new JSDOM(html, {
    url: "http://localhost/#/org",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const calls = [];

  window.fetch = async (url, init) => {
    const href = String(url);
    calls.push({ href, init });

    if (href.indexOf("/api/v1/me") === 0) {
      return reply({
        subject: "alex",
        tenant_id: "default",
        roles: admin ? ["platform_admin"] : ["member"],
        is_platform_admin: admin,
      });
    }
    if (href.indexOf("/api/v1/admin/tenants") === 0) {
      // GET 列表与 GET/PUT 单个租户配额共用前缀，按后续路径区分。
      const tail = href.slice("/api/v1/admin/tenants".length);
      if (tail.indexOf("/quota") >= 0) {
        if (init && init.method === "PUT") {
          return reply({ tenant_id: "acme", ...JSON.parse(init.body) });
        }
        return reply(QUOTA);
      }
      if (tail.indexOf("/members") >= 0) return reply([]);
      return reply(TENANTS);
    }
    return reply({ items: [] });
  };

  for (const file of ["approval.js", "render.js", "clarify.js", "rail.js", "console.js"]) {
    window.eval(fs.readFileSync(path.join(root, "static/console/assets", file), "utf8"));
  }
  return { window, calls };
}

const wait = (ms = 80) => new Promise((resolve) => setTimeout(resolve, ms));
let failures = 0;

function check(name, condition, detail) {
  if (condition) return;
  failures += 1;
  console.error(`FAIL ${name}${detail ? "\n  " + detail : ""}`);
}

// ---------- 管理员 ----------
const adminDom = makeDom(true);
const { window } = adminDom;
const $ = (sel) => window.document.querySelector(sel);
const $$ = (sel) => Array.from(window.document.querySelectorAll(sel));
const text = (sel) => {
  const node = $(sel);
  return node ? node.textContent.trim() : "";
};

await wait(140);

const quotaBtn = $('[data-quota="acme"]');
check("管理员能看到每个租户的配额入口", !!quotaBtn);
check("配额按钮只有一个", $$("[data-quota]").length === 1, String($$("[data-quota]").length));

if (quotaBtn) {
  quotaBtn.click();
  await wait(100);

  check("点击后打开配额弹窗", !!$("#q-close"), "");
  check("弹窗是宽版", !!$(".modal.modal-wide"), "");
  check("沙箱三项字段都在", !!$("#q-concurrent") && !!$("#q-cpu") && !!$("#q-workspace"), "");
  check(
    "沙箱分组标明仅管理员",
    window.document.body.textContent.indexOf("仅平台管理员") >= 0,
    ""
  );

  check("并发任务数回显", ($("#q-concurrent") || {}).value === "2", ($("#q-concurrent") || {}).value);
  check("CPU 秒回显", ($("#q-cpu") || {}).value === "1800", ($("#q-cpu") || {}).value);
  check("工作区按 MB 回显", ($("#q-workspace") || {}).value === "512", ($("#q-workspace") || {}).value);
  check("常规配额也回显", ($("#q-daily") || {}).value === "1000", ($("#q-daily") || {}).value);

  $("#q-concurrent").value = "4";
  $("#q-cpu").value = "3600";
  $("#q-workspace").value = "256";
  $("#q-docs").value = "700";
  $("#q-save").click();
  await wait(100);

  const put = adminDom.calls.find(
    (c) => c.href.indexOf("/quota") >= 0 && c.init && c.init.method === "PUT"
  );
  check("确实发了 PUT", !!put, JSON.stringify(adminDom.calls.map((c) => c.href)));
  if (put) {
    const body = JSON.parse(put.init.body);
    check("并发任务数提交正确", body.max_concurrent_tasks === 4, JSON.stringify(body));
    check("CPU 秒提交正确", body.daily_cpu_seconds === 3600, JSON.stringify(body));
    check("MB 换算成字节", body.workspace_max_bytes === 256 * 1024 * 1024, JSON.stringify(body));
    check("常规配额一起提交", body.knowledge_max_documents === 700, JSON.stringify(body));
    check("六项都提交", Object.keys(body).length === 6, JSON.stringify(Object.keys(body)));
  }
  check("保存后弹窗关闭", !$("#q-close"), "");
}

// ---------- 普通租户 ----------
const plainDom = makeDom(false);
const plain = plainDom.window.document;
await wait(140);

check("普通租户没有配额入口", plain.querySelector("[data-quota]") === null, plain.body.textContent.slice(0, 200));
check("普通租户看不到沙箱字段", plain.querySelector("#q-concurrent") === null, "");
check(
  "普通租户页面不提沙箱事务配额",
  plain.body.textContent.indexOf("沙箱事务配额") < 0,
  plain.body.textContent.slice(0, 300)
);
check(
  "普通租户没有组织管理导航",
  Array.from(plain.querySelectorAll(".nav-item, .nav-link, [data-page]")).every(
    (n) => n.textContent.indexOf("组织与成员") < 0
  ),
  Array.from(plain.querySelectorAll(".nav-item, .nav-link, [data-page]"))
    .map((n) => n.textContent.trim())
    .join("|")
    .slice(0, 300)
);
check(
  "普通租户没有触发管理接口",
  plainDom.calls.every((c) => c.href.indexOf("/api/v1/admin/") < 0),
  JSON.stringify(plainDom.calls.map((c) => c.href))
);

if (failures) {
  console.error(`check_sandbox_quota: ${failures} 项失败`);
  process.exit(1);
}

console.log("check_sandbox_quota: all passed");
