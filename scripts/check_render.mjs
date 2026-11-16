// 会话内容渲染层（static/console/assets/render.js）的自检脚本。
//
// 用法：node scripts/check_render.mjs
// 全部断言通过静默退出 0，任何一条失败打印差异并退出 1。
//
// 为什么放在 scripts 而不是 tests/：渲染层是纯前端 JS，pytest 环境里没有
// DOM 和浏览器；node 直接加载源文件断言输出，是最接近真实行为的方式。
// 图形长相对肉眼负责（手工用 Edge 截图核对），这里只负责安全与结构。
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
if (!R || typeof R.rich !== "function") {
  console.error("render.js 未暴露 window.PAPRender");
  process.exit(1);
}

let failures = 0;

function check(name, condition, detail) {
  if (condition) return;
  failures += 1;
  console.error(`FAIL ${name}${detail ? "\n  " + detail : ""}`);
}

const TABLE = [
  "核对结果：",
  "",
  "| 项目 | 设定值 | 实测值 | 判定 |",
  "|---|---:|---:|---|",
  "| 出口压力 | 2.30 kPa | 2.35 kPa | 超差 |",
  "| 切断压力 | 3.5 kPa | 3.5 kPa | 合格 |"
].join("\n");

{
  const html = R.rich(TABLE);
  check("表格容器", html.includes('<table class="md-table">'), html);
  check("表头行数", (html.match(/<th /g) || []).length === 4, html);
  check("数值列右对齐", html.includes('style="text-align:right"'), html);
  check("无越权标签", !/<script|onclick/i.test(html), html);
}

{
  const html = R.rich("先说结论。\n\n```chart\n{\"type\":\"line\",\"title\":\"压力\",\"unit\":\"kPa\",\"x\":[\"a\",\"b\",\"c\"],\"series\":[{\"name\":\"出口压力\",\"data\":[2.1,2.3,2.2]}]}\n```");
  check("折线卡片", html.includes('class="chart-card"') && html.includes("<svg"), html);
  check("平滑曲线", html.includes(" C"), html);
  check("悬浮提示", html.includes("<title>") && html.includes("2.3"), html);
}

{
  const html = R.rich("```chart\n{\"type\":\"bar\",\"title\":\"缺陷\",\"x\":[\"外观\",\"压力\",\"电气\"],\"series\":[{\"name\":\"数量\",\"data\":[12,7,3]}]}\n```");
  check("柱状数量", (html.match(/<rect /g) || []).length === 3, html);
}

{
  const html = R.rich("```chart\n{\"type\":\"line\",\"series\":[{\"name\":\"空\",\"data\":[]}]}\n```");
  check("空数据降级", html.includes("rich-note"), html);
}

{
  const html = R.rich("```chart\n{not json}\n```");
  check("坏 JSON 降级", html.includes("rich-note") && html.includes("rich-code"), html);
}

{
  const svg = [
    "<svg viewBox=\"0 0 400 120\">",
    "<script>alert(1)</script>",
    "<style>*{color:red}</style>",
    "<foreignObject><img src=x onerror=alert(2)></foreignObject>",
    "<rect x=\"1\" y=\"2\" width=\"10\" height=\"10\" onclick=\"x()\" fill=\"#0b6b4b\"/>",
    "<a href=\"javascript:alert(3)\"><circle r=\"5\"/></a>",
    "<text font-size=\"13\">文字</text>",
    "</svg>"
  ].join("");
  const html = R.sanitizeSvg(svg);
  check("去 script", !/script/i.test(html), html);
  check("去 style", !/style/i.test(html), html);
  check("去 foreignObject", !/foreignobject/i.test(html), html);
  check("去 on* 属性", !/onerror|onclick/i.test(html), html);
  check("去 javascript:", !/javascript:/i.test(html), html);
  check("保留图形", html.includes("<rect") && html.includes("<circle") && html.includes("文字"), html);
  check("自闭合闭合", html.includes("<rect "), html);
}

{
  const html = R.sanitizeSvg("<rect x=\"0\" y=\"0\" width=\"10\" height=\"10\"/>");
  check("无 svg 根时补 viewBox", html.includes("<svg") && html.includes("viewBox"), html);
}

{
  let threw = false;
  try {
    R.sanitizeSvg("<svg><rect x=\"1\"");
  } catch (err) {
    threw = true;
  }
  check("残缺 SVG 抛错", threw);
}

{
  const html = R.rich("- 无序甲 **加粗**\n- 无序乙 `代码`\n1. 有序一\n2. 有序二");
  check("两种列表分开", html.includes("</ul><ol>") || html.includes("</ul>\n<ol>"), html);
  check("行内格式", html.includes("<strong>加粗</strong>") && html.includes("<code>代码</code>"), html);
}

{
  const html = R.rich("### 小标题\n正文**含重点**。");
  check("标题与段落", html.includes('class="rich-h"') && html.includes("<p>正文"), html);
}

{
  const html = R.rich("```\nplain code\n```");
  check("无语言围栏", html.includes("rich-code") && html.includes("plain code"), html);
}

// ---- 审计详情（渲染进表格的租户内容，净化是重点）----
{
  const html = R.auditMeta({ request_reason: "depth", limits: { max_depth: 0 } });
  check("审计详情可折叠", html.includes('<details class="audit-meta">') && html.includes("<summary>详情</summary>"), html);
  check("审计详情带 JSON", html.includes("max_depth"), html);
}

{
  // 元数据里的字符串来自模型与用户，直接拼进 HTML 就是注入。
  const html = R.auditMeta({ note: '<img src=x onerror="alert(1)">' });
  check("审计详情转义标签", !html.includes("<img"), html);
  // JSON 里引号先被 stringify 转成 \"，再被 esc 转成 \&quot;——两处都不能漏。
  check("审计详情转义引号", html.includes('&lt;img src=x onerror=\\&quot;alert(1)\\&quot;&gt;'), html);
}

{
  check("空对象不渲染", R.auditMeta({}) === "", R.auditMeta({}));
  check("null 不渲染", R.auditMeta(null) === "", R.auditMeta(null));
  check("数组不渲染", R.auditMeta([1, 2]) === "", R.auditMeta([1, 2]));
  check("字符串不渲染", R.auditMeta("x") === "", R.auditMeta("x"));
}

{
  // 一条事件的 metadata 可能是整份配置快照，不截断就会把表格撑爆。
  const html = R.auditMeta({ blob: "x".repeat(5000) });
  check("超长内容截断", html.includes("过长已截断") && html.length < 5000, `len=${html.length}`);
}

// ---- 运行横条（L0「现在」）----
{
  // 没有在跑也没有在等：整条不渲染。留一个「0 个」的空壳会占掉屏幕最值钱的一行。
  check("横条没内容不渲染", R.liveStrip({ enabled: true, running: [], waiting: [] }) === "",
    R.liveStrip({ enabled: true, running: [], waiting: [] }));
  check("横条观测关闭不渲染", R.liveStrip({ enabled: false, running: [{ trace_id: "a" }] }) === "",
    R.liveStrip({ enabled: false, running: [{ trace_id: "a" }] }));
  check("横条空数据不渲染", R.liveStrip(null) === "", R.liveStrip(null));
}

{
  const html = R.liveStrip({
    enabled: true,
    stale_after_ms: 900000,
    running: [
      { trace_id: "t1", agent_name: "ops", age_ms: 42000, stale: false },
      { trace_id: "t2", agent_name: null, age_ms: 3600000, stale: true }
    ],
    waiting: [
      { approval_id: "a1", tool_name: "create_maintenance_ticket",
        tool_display_name: "创建维护工单", delegated_from: "cs", waited_ms: 1200000, stale: true }
    ],
    truncated: false
  });
  check("横条区分在跑与在等", html.includes("在跑") && html.includes("在等人工"), html);
  // 只有 stale 的那条带类名；全都带上等于没有标记。
  check("横条只给过期的标 stale",
    (html.match(/live-pill stale/g) || []).length === 1, html);
  // 未归属的 Agent 不能显示成空白。
  check("横条未归属有兜底文案", html.includes("未归属"), html);
  check("横条带 trace 点击键", html.includes('data-live-trace="t1"'), html);
  check("横条时长说人话", html.includes("42 秒") && html.includes("1 小时 0 分"), html);
}

{
  // Agent 名、工具名都是用户可填的字符串，漏一个转义就是注入口子。
  const html = R.liveStrip({
    enabled: true,
    stale_after_ms: 60000,
    running: [{ trace_id: 'x" onmouseover="alert(1)', agent_name: "<img src=x>", age_ms: 1000 }],
    waiting: []
  });
  check("横条转义 Agent 名", !html.includes("<img"), html);
  check("横条转义 trace_id 引号", !html.includes('onmouseover="alert(1)"'), html);
}

// ---- 委派拓扑（L2 关系图）----
{
  check("拓扑空数据不渲染", R.topologyView(null) === "", R.topologyView(null));
  check(
    "拓扑观测关闭说明原因",
    R.topologyView({ enabled: false }).includes("观测已关闭"),
    R.topologyView({ enabled: false })
  );

  // 没权限时给空集 + 一句人话，不给一个看起来坏掉的红框。
  const restricted = R.topologyView({
    enabled: true,
    restricted: true,
    reason: "委派拓扑需要 observability:read"
  });
  check(
    "拓扑受限给原因不给图",
    restricted.includes("需要 observability:read") && !restricted.includes("<svg"),
    restricted
  );
  check(
    "拓扑无节点给空状态",
    R.topologyView({ enabled: true, nodes: [], edges: [] }).includes("没有委派记录"),
    ""
  );

  // 有节点、没有边：不能画一块空画布——空白看起来像「图没加载出来」，
  // 而它实际是「这段时间里没有任何转交」这个明确结论。
  const noEdges = R.topologyView({
    enabled: true,
    nodes: [{ name: "cs", display_name: "客服", depth: null }],
    edges: []
  });
  check("拓扑有节点无边不画画布", noEdges.includes("没有任何委派") && !noEdges.includes("<svg"), noEdges);
}

{
  const data = {
    enabled: true,
    nodes: [
      { name: "cs", display_name: "客服", depth: 0, attempts: 0, traces: 4 },
      { name: "ops", display_name: "运维", depth: 1, attempts: 3, traces: 0, can_write: true },
      { name: "idle", display_name: "闲置专家", depth: null, configured: true, observed: false }
    ],
    edges: [
      { from_agent: "cs", to_agent: "ops", kind: "ok", count: 2 },
      { from_agent: "cs", to_agent: "ops", kind: "paused", count: 1 },
      { from_agent: "cs", to_agent: "ops", kind: "rejected", count: 1 }
    ]
  };
  const html = R.topologyView(data);
  check("拓扑画成 svg", html.includes('<svg viewBox="0 0 '), html.slice(0, 200));
  // 同一对节点之间的三种结局必须是三条线：合成一条就分不出
  // 「一直被拒」和「一直跑通」——那是两种完全不同的架构判断。
  check("拓扑三种边分开画", (html.match(/<path class="topo-edge/g) || []).length === 3, html);
  check(
    "拓扑边带类型",
    ["ok", "paused", "rejected"].every((kind) => html.includes(`topo-edge ${kind}`)),
    html
  );
  check("拓扑可写节点有标记", html.includes("topo-node can-write"), html);
  // 配置了但没被调用：图外的孤岛。混进图里会变成一堆没有边的散点。
  check(
    "拓扑孤立节点在图外",
    html.includes("topo-isolated") && html.includes("闲置专家"),
    html
  );
  check("拓扑孤立节点只出现一次", (html.match(/闲置专家/g) || []).length === 1, html);

  // 一束三条边，三个数字必须各在各的线上、互不压叠。
  // 都摆在中点时它们只隔一个扇开的距离，10.5px 的数字会糊成「5 上面压着 2」——
  // 那就等于没有标注：读不出哪个数是跑通、哪个是被拒。
  const counts = [...html.matchAll(/<text class="topo-count (\w+)" x="(-?\d+)" y="(-?\d+)"/g)];
  check("拓扑数字标注带边类型", counts.length === 3 && counts.every((m) => m[1]), html);
  // 横坐标必须各不相同：同一条束上的数字若挤在同一个 x 上，就只能靠垂直间距分开。
  const xs = new Set(counts.map((m) => m[2]));
  check("拓扑数字标注错开摆放", counts.length === 3 && xs.size === 3, [...xs].join(","));
  let minGap = Infinity;
  for (let i = 0; i < counts.length; i += 1) {
    for (let j = i + 1; j < counts.length; j += 1) {
      const dx = Number(counts[i][2]) - Number(counts[j][2]);
      const dy = Number(counts[i][3]) - Number(counts[j][3]);
      minGap = Math.min(minGap, Math.hypot(dx, dy));
    }
  }
  check("拓扑数字标注不重叠", counts.length === 3 && minGap >= 12, `minGap=${minGap}`);
}

{
  // 节点名、显示名都是用户可填的字符串，漏一个转义就是注入口子。
  const html = R.topologyView({
    enabled: true,
    nodes: [
      { name: '<img src=x onerror="alert(1)">', depth: 0, attempts: 0, traces: 1 },
      { name: "ops", display_name: '"><script>alert(2)</script>', depth: 1, attempts: 1 }
    ],
    edges: [
      { from_agent: '<img src=x onerror="alert(1)">', to_agent: "ops", kind: "ok", count: 1 }
    ]
  });
  check("拓扑转义节点名", !html.includes("<img"), html);
  check("拓扑转义显示名", !/<script/i.test(html), html);
  check("拓扑转义后仍出图", html.includes("<svg") && html.includes("<path"), html);
}

{
  // 边指向一个节点集里没有的 Agent（数据残缺）：宁可不画，
  // 也不画一条飞向空处的线——那种线会让人以为图坏了。
  const html = R.topologyView({
    enabled: true,
    nodes: [{ name: "cs", display_name: "客服", depth: 0, attempts: 0, traces: 1 }],
    edges: [{ from_agent: "cs", to_agent: "ghost", kind: "ok", count: 3 }]
  });
  check("拓扑不画指向缺席节点的线", !html.includes("<path"), html);
}

// ---- 编排声明层 ----
{
  // 图里配了 cs → ops，但窗口内一次都没跑过：这条边必须画出来（点线），
  // 否则「配了没人用」这个结论只能靠翻数据库。ops 的实测 depth 是 null，
  // 深度得按声明图推出来，推不出来就只能飘在 0 层、跟 cs 重叠。
  const html = R.topologyView({
    enabled: true,
    nodes: [
      { name: "cs", display_name: "客服", depth: 0, attempts: 0, traces: 2 },
      { name: "ops", display_name: "运维", depth: null, configured: true, observed: false }
    ],
    edges: [],
    declared_edges: [{ from_agent: "cs", to_agent: "ops", workflows: ["ticket"], observed: false }]
  });
  check("声明边画成点线", html.includes("topo-edge declared"), html.slice(0, 400));
  check("声明边纳入图例", html.includes("图上声明"), html.slice(0, 400));
  // 一条都没跑过时不能落到「没有任何委派」的空态：图是有的，只是还没跑。
  check("有声明边就不走空态", html.includes("<svg"), html.slice(0, 300));
  // 两个节点必须分列两层：`ops` 的实测 depth 是 null，只能按声明图推。
  // 推不出来它就落在第 0 层，与 `cs` 同一列——线会反着画回去（x2 < x1）。
  const declaredPath = /<path class="topo-edge declared" d="M([\d.]+) [\d.]+ C[^"]*? ([\d.]+) [\d.]+"\/>/.exec(html);
  check("声明边从左指向右", !!declaredPath && Number(declaredPath[2]) > Number(declaredPath[1]), html);
  // 声明边不标数字：它是 0 次，标个 0 会让人以为「跑过但计数丢了」。
  check("声明边不标数字", (html.match(/<text class="topo-count[^>]*>0<\/text>/g) || []).length === 0, html);

  // 反向：同一条边走过了 → 不再叠一条点线，否则图上全是重复。
  const both = R.topologyView({
    enabled: true,
    nodes: [
      { name: "cs", depth: 0, attempts: 0, traces: 2 },
      { name: "ops", depth: 1, attempts: 1, traces: 0 }
    ],
    edges: [{ from_agent: "cs", to_agent: "ops", kind: "ok", count: 1 }],
    declared_edges: [{ from_agent: "cs", to_agent: "ops", workflows: ["ticket"], observed: true }]
  });
  check("已观测的声明边不重复画", !both.includes("topo-edge declared"), both);
}

// ---- 泳道（L3 时间分布）----
{
  check("泳道空数据不渲染", R.swimlanes([]) === "", R.swimlanes([]));
  check("泳道 null 不渲染", R.swimlanes(null) === "", R.swimlanes(null));

  // 没有委派的一轮不进泳道：一条横条通到底，比缩进盒子少回答一半问题。
  const flat = [
    { span_id: "s1", name: "plan_request", kind: "node", agent_name: "cs",
      started_at: "2026-11-13T10:00:00Z", duration_ms: 100 },
    { span_id: "s2", name: "llm.call", kind: "llm", agent_name: "cs",
      started_at: "2026-11-13T10:00:01Z", duration_ms: 900 }
  ];
  check("无委派不判为泳道", R.hasDelegation(flat) === false, String(R.hasDelegation(flat)));
  check("无委派不画泳道", R.swimlanes(flat) === "", R.swimlanes(flat));
}

{
  const spans = [
    { span_id: "s1", name: "graph.invoke", kind: "graph", agent_name: "cs",
      started_at: "2026-11-13T10:00:00Z", duration_ms: 3000, status: "ok" },
    { span_id: "s2", name: "delegate", kind: "agent", agent_name: "ops",
      started_at: "2026-11-13T10:00:01Z", duration_ms: 2000, status: "error",
      error_category: "timeout", attrs: { depth: 1, delegated_from: "cs" } },
    { span_id: "s3", name: "tool.call", kind: "tool", agent_name: "ops",
      started_at: "2026-11-13T10:00:02Z", duration_ms: 500, status: "ok" }
  ];
  check("有委派才判为泳道", R.hasDelegation(spans) === true);

  const html = R.swimlanes(spans);
  check("泳道一个智能体一行", (html.match(/class="swim-lane"/g) || []).length === 2, html);
  // 横条只说「在干活」，失败得单独戳出来，否则一条超时的子调用看着像正常跑完。
  check("泳道标出失败点", (html.match(/class="swim-fail"/g) || []).length === 1, html);
  check("泳道画出转交", (html.match(/class="swim-delegate"/g) || []).length === 1, html);
  check("泳道写明第几跳与来源", html.includes("第 1 跳") && html.includes("被 cs 转交"), html);
  // 三个刻度 + 左侧「智能体」标题：再多就是让人去读钟表，而比例才是这张图要回答的。
  check("泳道轴只给三个刻度",
    (html.match(/class="swim-axis-label"/g) || []).length === 4, html);
}

{
  // Agent 名是用户可填的字符串，漏转义就是注入口子。
  const html = R.swimlanes([
    { span_id: "s1", name: "delegate", kind: "agent", agent_name: '<img src=x onerror="alert(1)">',
      started_at: "2026-11-13T10:00:00Z", duration_ms: 100, status: "ok",
      attrs: { depth: 1, delegated_from: '"><script>alert(2)</script>' } }
  ]);
  check("泳道转义智能体名", !html.includes("<img"), html);
  check("泳道转义上级名", !/<script/i.test(html), html);
  check("泳道转义后仍出图", html.includes("<svg"), html);
}

// ---- 运行视图（L3 一次运行）----
{
  check("运行页空数据不渲染", R.operationView(null) === "", R.operationView(null));
  check("运行页观测关闭说明原因",
    R.operationView({ enabled: false, traces: [], approvals: [] }).includes("观测已关闭"));
  check("运行页无记录给空状态",
    R.operationView({ enabled: true, traces: [], approvals: [] }).includes("这次运行没有记录"));
}

{
  const data = {
    enabled: true,
    operation_id: "op-1",
    wall_ms: 10800000,      // 3 小时
    waiting_ms: 10680000,   // 2 小时 58 分
    active_ms: 120000,
    display_state: "completed",
    attention: true,
    traces: [
      {
        trace_id: "t1", agent_name: "cs", started_at: "2026-11-13T10:00:00Z",
        duration_ms: 1000, status: "awaiting_approval",
        delegations: [{ agent: "ops", delegated_from: "cs", depth: 1, duration_ms: 800, status: "completed" }]
      },
      {
        trace_id: "t2", agent_name: "ops", started_at: "2026-11-13T13:00:00Z",
        duration_ms: 500, status: "ok", delegations: []
      }
    ],
    approvals: [{
      approval_id: "a1", tool_name: "update_ticket", tool_display_name: "改工单状态",
      status: "EXECUTED", agent_name: "ops", delegated_from: "cs",
      delegation_chain: ["cs"], reason: "把工单置为已关闭",
      created_at: "2026-11-13T10:00:01Z", waited_ms: 10680000, display_state: "completed"
    }],
    participants: [{
      agent_name: "ops", display_name: "运维", role: "delegate",
      traces: 1, delegated_in: 1, delegated_out: 0, waiting_ms: 10680000
    }],
    steps: [
      { kind: "trace", ref: "t1", at: "2026-11-13T10:00:00Z", state: "input-required",
        agent_name: "cs", duration_ms: 1000 },
      { kind: "approval", ref: "a1", at: "2026-11-13T10:00:01Z", state: "completed",
        agent_name: "ops", tool_display_name: "改工单状态" },
      { kind: "trace", ref: "t2", at: "2026-11-13T13:00:00Z", state: "completed",
        agent_name: "ops", duration_ms: 500 }
    ]
  };
  const html = R.operationView(data);
  check("运行页三个耗时都报",
    html.includes("历时") && html.includes("其中等人工") && html.includes("实际在跑"), html.slice(0, 400));
  // 这三个数不是并列的：把关系写在卡片底部，比在三个数字旁边各写一句更省地方。
  check("运行页写明三者关系", html.includes("历时 = 等人工 + 实际在跑"), html);
  check("运行页报出真实等待占比", html.includes("等人工 99%"), html);
  check("运行页拼出委派链", html.includes("cs → ops"), html);
  // 审批卡上那句话事后滚走了，运行页要重新拼出来——这是回溯时最要紧的一句。
  check("运行页还原审批原文", html.includes("把工单置为已关闭"), html);
  check("运行页保留审批原状态", html.includes("EXECUTED"), html);
  check("运行页时间线可点进某一轮",
    html.includes('data-trace="t1"') && html.includes('data-trace="t2"'), html);
  // 「trace 收尾了但单子还挂着」不能在界面上看起来像彻底完成。
  check("运行页点出还挂着的单子", html.includes("还有一张单子挂着没人处理"), html);
  check("运行页列出参与者角色", html.includes("被叫来干活"), html);
}

{
  const html = R.operationView({
    enabled: true,
    wall_ms: 1000, waiting_ms: 0, active_ms: 1000, display_state: "completed",
    traces: [{ trace_id: 'x" onclick="alert(1)', agent_name: '<img src=x>',
      started_at: "2026-11-13T10:00:00Z", duration_ms: 1000, status: "ok", delegations: [] }],
    approvals: [{ approval_id: "a1", tool_name: "t", tool_display_name: "<img src=y>",
      status: "PENDING", agent_name: "ops", reason: '<script>alert(2)</script>',
      created_at: "2026-11-13T10:00:00Z", waited_ms: 0, display_state: "input-required" }],
    participants: [], steps: [{ kind: "approval", ref: "a1", at: "2026-11-13T10:00:00Z",
      state: "input-required", agent_name: "ops", tool_display_name: "<img src=y>" }]
  });
  check("运行页转义 Agent 名", !html.includes("<img"), html);
  check("运行页转义审批文案", !/<script/i.test(html), html);
  check("运行页转义 trace_id 引号", !html.includes('onclick="alert(1)"'), html);
}

/* ---------------- 上下文（长对话压缩） ---------------- */

{
  // 没有 context.* span 的 trace（老数据、或观测关过的一轮）不该多出一行空白。
  check("无上下文 span 时不渲染 chip", R.contextNote([]) === "");
  check(
    "只有其它 span 时不渲染 chip",
    R.contextNote([{ name: "llm.call", attrs: {} }]) === ""
  );
}

{
  const html = R.contextNote([
    {
      name: "context.measure",
      attrs: { window: 32000, last_prompt_tokens: 17805, action: "compact", kept_turns: 7 },
    },
    {
      name: "context.inject",
      attrs: { turns: 7, summary_turns: 12, summary_chars: 273, summary_injected: true },
    },
    {
      name: "context.compact",
      attrs: { turns: 3, tokens_before: 2421, tokens_after: 236, model: "deepseek-chat" },
      content: JSON.stringify({ output: "【当前目标】盯 3 号工单\n【被否决】无" }),
    },
  ]);

  check("chip 写出实测与窗口", html.includes("上下文 17.8k / 32k"), html);
  check("chip 写出累计覆盖轮数", html.includes("摘要覆盖 12 轮"), html);
  check("chip 写出保留轮数", html.includes("最近 7 轮原文"), html);
  check("chip 写出本轮压缩量", html.includes("本轮压缩 3 轮"), html);
  check("chip 带压缩前后 token", html.includes("2.4k → 236"), html);
  check("chip 展开区域含摘要全文", html.includes("盯 3 号工单"), html);
  check("有摘要时才给展开按钮", html.includes('id="obs-ctx-toggle"'), html);
  check("展开区默认收起", html.includes('id="obs-ctx-body" hidden'), html);
}

{
  // 摘要正文来自模型，必须转义——它会被原样塞进 innerHTML。
  const html = R.contextNote([
    { name: "context.measure", attrs: { window: 32000, last_prompt_tokens: 900, action: "none" } },
    { name: "context.compact", attrs: { turns: 2 }, content: JSON.stringify({ output: '<img src=x onerror="alert(1)">' }) },
  ]);
  check("摘要正文转义", !html.includes("<img"), html);
  check("摘要正文不带可执行属性", !html.includes('onerror="'), html);
}

{
  // 老 trace 没有 summary_turns：不写「覆盖 0 轮」，整段略去。
  const html = R.contextNote([
    { name: "context.measure", attrs: { window: 32000, last_prompt_tokens: 900, action: "none" } },
    { name: "context.inject", attrs: { turns: 2 } },
  ]);
  check("缺 summary_turns 时不写覆盖轮数", !html.includes("摘要覆盖"), html);
  check("无摘要时不渲染展开按钮", !html.includes("obs-ctx-toggle"), html);
}

{
  // content 不是 JSON（观测库里的历史格式）时按「没有摘要」处理，不抛异常。
  let html = "";
  let threw = false;
  try {
    html = R.contextNote([
      { name: "context.compact", attrs: { turns: 1 }, content: "不是 JSON" },
    ]);
  } catch (err) {
    threw = true;
  }
  check("span 原文不是 JSON 时不抛异常", !threw);
  check("span 原文不是 JSON 时仍然出 chip", html.includes("ctx-note"), html);
}

{
  const messages = [
    { id: "m1", role: "user" },
    { id: "m2", role: "agent" },
    { id: "m3", role: "user" },
    { id: "m4", role: "agent" },
    { id: "m5", role: "user" },
  ];

  check("无水位不插分隔条", R.foldSeam(messages, null) === null);
  check("水位不在当前会话时不插", R.foldSeam(messages, "gone") === null);

  const seam = R.foldSeam(messages, "m4");
  check("分隔条插在水位那条之后", seam && seam.index === 3, JSON.stringify(seam));
  check("轮数按界面上的提问数算", seam && seam.turns === 2, JSON.stringify(seam));

  // 水位正好落在用户消息上：那一轮也要算进「已折叠」。
  const onUser = R.foldSeam(messages, "m3");
  check("水位是用户消息时含该轮", onUser && onUser.turns === 2, JSON.stringify(onUser));
}

{
  // 概览走势：结构、空态、截断标记。
  const empty = R.obsTrend([], {});
  check("走势图为空时不出 svg", !empty.includes("<svg"), empty);
  check("走势图为空时给一句说明", empty.includes("obs-empty"), empty);

  // 1 小时窗里没人用时只有一格、且这一格是 0：画一张空白坐标轴会像图坏了。
  const allZero = R.obsTrend(
    [{ t: "2026-11-13T00:00:00Z", traces: 0, errors: 0, awaiting: 0, p95_duration_ms: null }],
    { width: 660, bucketHours: 1 }
  );
  check("全为 0 的格子也走空态", allZero.includes("obs-empty") && !allZero.includes("<svg"), allZero);

  const buckets = [
    { t: "2026-11-13T00:00:00Z", traces: 0, errors: 0, awaiting: 0, p95_duration_ms: null, partial: false },
    { t: "2026-11-13T06:00:00Z", traces: 10, errors: 2, awaiting: 3, p95_duration_ms: 2400, partial: false },
    { t: "2026-11-13T12:00:00Z", traces: 4, errors: 0, awaiting: 0, p95_duration_ms: 900, partial: true },
  ];
  const svg = R.obsTrend(buckets, { width: 660, bucketHours: 6 });

  check("走势图出 svg", svg.includes("<svg"), svg.slice(0, 80));
  check(
    "三层各用一种颜色",
    ["#9dc3b2", "#dcb26a", "#c76a61"].every((c) => svg.includes(c)),
    svg
  );
  check("图例三种都在", svg.includes("正常") && svg.includes("等人工") && svg.includes("错误"));
  // 被窗口起点截断的最旧一格要看得出来，否则「少了一半时间」会被读成「用量低」。
  check("截断的最旧一格降透明度", svg.includes('opacity="0.55"'), svg);
  check("tooltip 带轮次拆分", svg.includes("轮次 10") && svg.includes("等人工 3"), svg);
  check("tooltip 带耗时", svg.includes("耗时 p95"), svg);
  // 有数据的两格各画三层（第二格 3 层、第三格 1 层），另加两格热区。
  check("有数据的格子画满三层", (svg.match(/<rect /g) || []).length >= 6, svg);
  check("走势图不注入脚本", !svg.includes("<script"), svg);

  // 耗时为 0 或缺失时不该画折线：一条贴在底边的线会被读成「耗时归零」。
  const noDur = R.obsTrend(
    [{ t: "2026-11-13T00:00:00Z", traces: 3, errors: 0, awaiting: 0, p95_duration_ms: null }],
    { width: 400, bucketHours: 1 }
  );
  check("没有耗时时不画折线", !noDur.includes("stroke-dasharray"), noDur);

  // 中间空一格：折线必须断开，不能跨过没有数据的格子连出一条假趋势。
  // 真实场景就是 7 天窗里前 11 格全空（系统还没上线）。
  const gap = R.obsTrend(
    [
      { t: "2026-11-13T00:00:00Z", traces: 3, errors: 0, awaiting: 0, p95_duration_ms: 1000 },
      { t: "2026-11-13T06:00:00Z", traces: 0, errors: 0, awaiting: 0, p95_duration_ms: null },
      { t: "2026-11-13T12:00:00Z", traces: 4, errors: 0, awaiting: 0, p95_duration_ms: 2000 },
    ],
    { width: 660, bucketHours: 6 }
  );
  check("空档两侧各只剩一个点，连不成线", !gap.includes("<path "), gap);
  // 相邻两格有值时才连：一条 path，不是两条。
  const joined = R.obsTrend(
    [
      { t: "2026-11-13T00:00:00Z", traces: 3, errors: 0, awaiting: 0, p95_duration_ms: 1000 },
      { t: "2026-11-13T06:00:00Z", traces: 4, errors: 0, awaiting: 0, p95_duration_ms: 2000 },
    ],
    { width: 660, bucketHours: 6 }
  );
  check("相邻两格连成一条线", (joined.match(/<path /g) || []).length === 1, joined);
}

{
  // 分布条：归一化、占比、转义。
  const bars = R.obsBars(
    [
      { label: "<img src=x onerror=alert(1)>", value: 5 },
      { label: "b", value: 10 },
    ],
    {}
  );
  check("分布条转义标签", !bars.includes("<img"), bars);
  check("最大值占满整条", bars.includes("width:100.0%"), bars);
  check("占比按传入项之和算", bars.includes("33%") && bars.includes("67%"), bars);
  check("空数据给说明", R.obsBars([], { empty: "没有工具调用。" }).includes("没有工具调用。"));

  // 工具那栏关掉百分比：分母只是前八名之和，算出来的占比会被当成「占全部」。
  const noShare = R.obsBars([{ label: "a", value: 1 }], { share: false });
  check("关掉占比时不渲染占比小字", !noShare.includes("<em>"), noShare);

  // 分段条：一段之内再分「完成 / 出错」，每段按**组内最大值**归一。
  const seg = R.obsBars(
    [
      {
        label: "t",
        value: 10,
        parts: [
          { value: 8, color: "#0b6b4b" },
          { value: 2, color: "#c76a61" },
        ],
      },
      { label: "u", value: 5 },
    ],
    { share: false }
  );
  check("分段各自按组内最大值归一", seg.includes("width:80.0%") && seg.includes("width:20.0%"), seg);
  check("未分段的条仍按自身值归一", seg.includes("width:50.0%"), seg);
}

/* ---- 审批卡：工单参数读成卡、流程条按状态画 ----
 *
 * 这一屏是唯一「点错了就有副作用」的地方，长相之外还得锁住三件事：
 *   1) 等级从标题里提出来之后，标题里不能再留着；
 *   2) 描述里的换行必须还原（JSON.stringify 把真换行写成字面 \n，
 *      直接塞进 <pre> 就是一段没人读的墙）；
 *   3) 流程条的位置由服务端 status 决定，不从「已执行」这句中文反推。
 */
{
  const params = {
    station_id: "station-001",
    title: "【较大】3 号调压箱出口压力波动及箱内燃气味隐患",
    description: "现场情况：压力表读数持续波动。\n\n等级与理由：判定为「较大」。",
  };
  const pending = {
    status: "PENDING",
    open: true,
    preview: { tool_name: "create_maintenance_ticket", notice: "尚未执行，确认后才会提交。", parameters: params },
  };
  const html = R.approvalBody(pending);

  check("工单卡：等级单独成标", html.includes('<span class="tk-badge tone-warn">较大</span>'), html);
  check("工单卡：标题里不再带【较大】", html.includes(">3 号调压箱") && !html.includes("【较大】"), html);
  check("工单卡：场站单列一栏", html.includes("station-001"), html);
  check(
    "工单卡：描述按空行分块、块首标签提成小标题",
    (html.match(/class="tk-block"/g) || []).length === 2 &&
      html.includes(">现场情况<") && html.includes(">等级与理由<"),
    html
  );
  check(
    "流程条：待确认停在「人工确认」",
    html.includes('<li class="now"><span class="dot"></span><span class="lb">人工确认</span>'),
    html
  );
  check("流程条：未到的两步仍是等", (html.match(/<li class="wait">/g) || []).length === 2, html);

  // 二次转义的换行（模型把 \n 当字面量写进字符串）也要还原，否则分块全丢。
  check(
    "字面的 \\n 也当换行",
    R.textBlocks("现场情况：a\\n\\nb").length === 2,
    JSON.stringify(R.textBlocks("现场情况：a\\n\\nb"))
  );

  // 类别写错时不能猜颜色：认不出的分级保持无色调。
  check("认不出的等级不着色", R.ticketCard({ title: "【X9】某隐患" }).includes("tk-badge\">X9"), "");

  const verified = R.approvalBody({ ...pending, status: "VERIFIED", open: false, done: "已执行并核验通过" });
  check("已核验：四步全绿", (verified.match(/<li class="done">/g) || []).length === 4, verified);
  // 结论句只出现一次：卡头有状态标、条上有位置，再说一遍就是三遍。
  check("已核验：结论不重复成两行", (verified.match(/已执行并核验通过/g) || []).length === 1, verified);

  const failed = R.approvalBody({ ...pending, status: "FAILED", open: false, done: "执行失败（EXTERNAL_WRITE_FAILED）" });
  check("失败：红点在「写入工单」", failed.includes('<li class="fail">'), failed);
  check("失败：错误码留在结论里", failed.includes("EXTERNAL_WRITE_FAILED"), failed);

  const cancelled = R.approvalBody({ ...pending, status: "REJECTED", open: false, done: "已取消，未执行" });
  check("已取消：确认那一步终止、后续两步空置",
    cancelled.includes('<li class="stop">') && (cancelled.match(/<li class="skip">/g) || []).length === 2,
    cancelled);

  // 非工单参数退回逐条罗列：键名原样保留，不去编中文标签。
  const generic = R.approvalBody({
    status: "PENDING",
    open: true,
    preview: { tool_name: "update_agent", parameters: { name: "field-ops", can_write: true, depth: 0 } },
  });
  check("非工单：逐条列出且保留原键名",
    generic.includes("tk-rows standalone") && generic.includes("can_write"), generic);
  check("非工单：布尔值写成中文", generic.includes(">是<"), generic);
  check("非工单：流程用通用说法",
    generic.includes("执行操作") && !generic.includes("写入工单"), generic);

  // 参数与标题都来自租户输入，转义漏一个就是注入口子。
  const nasty = R.approvalBody({
    preview: { parameters: { title: "【较大】<img src=x onerror=alert(1)>", description: "<script>x</script>" } },
  });
  check("工单卡转义标题与描述", !nasty.includes("<img") && !nasty.includes("<script"), nasty);

  // 审批单被清理过：没有参数就没有卡，调用方要能据此决定还写不写结论行。
  check("没有参数时不出卡", R.approvalBody({ preview: {} }) === "", R.approvalBody({ preview: {} }));
  check("状态标：待确认", R.statusChip({ status: "PENDING" }).includes("待确认"), R.statusChip({ status: "PENDING" }));
  check("状态标：没有状态就不编", R.statusChip({}) === "", R.statusChip({}));
}

if (failures) {
  console.error(`\n${failures} 项未通过`);
  process.exit(1);
}
console.log("check_render: all passed");