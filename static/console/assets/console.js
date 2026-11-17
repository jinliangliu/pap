/* Personal Agent Platform · 控制台
 *
 * 设计约束：
 *   1. 无构建、无第三方依赖，nginx 直接按目录提供；
 *   2. 每个视图的数据都来自真实接口，没有接口的能力一律显示「待接入」并说明缺什么，
 *      不允许用假数据占位；
 *   3. 所有来自接口的文本都经 esc() 转义后再拼进 HTML。
 */
(function () {
  "use strict";

  /* 审批卡的判断在 approval.js（纯逻辑，可脱离浏览器自检）。
     拿不到就说明脚本没加载上——当场报错，比静默丢掉卡片好排查。 */
  var PAPApproval = window.PAPApproval;

  /* ================= 基础工具 ================= */

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    var pad = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function toast(message, kind) {
    var wrap = $("#toasts");
    if (!wrap) return;
    var node = document.createElement("div");
    node.className = "toast" + (kind === "err" ? " err" : "");
    node.textContent = message;
    wrap.appendChild(node);
    setTimeout(function () { node.remove(); }, kind === "err" ? 6000 : 3200);
  }

  async function api(path, options) {
    var opts = Object.assign({ credentials: "same-origin", headers: {} }, options || {});
    if (opts.body !== undefined && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    var res = await fetch(path, opts);
    var text = await res.text();
    var payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch (e) { payload = { raw: text }; }
    }
    if (!res.ok) {
      var detail = payload && (payload.detail || payload.message);
      if (detail && typeof detail !== "string") detail = JSON.stringify(detail);
      var err = new Error(detail || ("HTTP " + res.status));
      err.status = res.status;
      /* 带上 body：409 里可能装着「单子现在的状态」，那不是失败而是结论
         （见 approval.js 的 settledFromError）。只留一句 message 就分不出来。 */
      err.payload = payload;
      throw err;
    }
    return payload;
  }

  function errText(e) {
    return (e && e.status ? e.status + " " : "") + (e && e.message ? e.message : String(e));
  }

  /* ================= 图标 ================= */

  var ICONS = {
    grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    message: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
    sliders: '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
    users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    spark: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
    layers: '<path d="M12 2 2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>',
    flow: '<rect x="8" y="2" width="8" height="6" rx="1.5"/><rect x="2" y="16" width="8" height="6" rx="1.5"/><rect x="14" y="16" width="8" height="6" rx="1.5"/><path d="M12 8v4"/><path d="M6 16v-1.5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2V16"/>',
    activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    clock: '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/>',
    refresh: '<path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/><path d="M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    // 麦克风：话筒头 + 托架 + 支杆。不要用「声波弧线」代替话筒——
    // 那个形状更像「音量」或「正在播放」，而这里要表达的是「对着它说话」。
    mic: '<rect x="9" y="2.5" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0"/><line x1="12" y1="18" x2="12" y2="21.5"/>',
    // 发送用向上的箭头：圆形底色 + 直箭头是「发出去」的通用写法，
    // 比纸飞机更耐缩放（19px 时纸飞机的折角会糊成一团）。
    "arrow-up": '<line x1="12" y1="20" x2="12" y2="5"/><polyline points="6.5 10.5 12 5 17.5 10.5"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
    edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.1 2.1 0 1 1 3 3L12 15l-4 1 1-4z"/>',
    save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.4 9a2.7 2.7 0 1 1 3.7 2.5c-.7.3-1.1.9-1.1 1.7"/><line x1="12" y1="17" x2="12" y2="17.01"/>',
    // 面板内的提示卡片：错误是三角感叹号，提示是圆圈 i。
    // 两者必须与助手头像区分开——这些卡片不是助手说的话，是平台在报状态。
    alert: '<path d="M10.3 3.6 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.6a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9" x2="12" y2="13.5"/><line x1="12" y1="17" x2="12" y2="17.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16.5"/><line x1="12" y1="7.5" x2="12" y2="7.51"/>',
    back: '<polyline points="15 18 9 12 15 6"/>',
    code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
    search: '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    list: '<line x1="9" y1="6" x2="21" y2="6"/><line x1="9" y1="12" x2="21" y2="12"/><line x1="9" y1="18" x2="21" y2="18"/><line x1="3.5" y1="6" x2="3.51" y2="6"/><line x1="3.5" y1="12" x2="3.51" y2="12"/><line x1="3.5" y1="18" x2="3.51" y2="18"/>',
    // 侧栏开关：一个框 + 靠左三分之一处的分隔线。分隔线必须在框内偏左，
    // 居中的话就变成「左右两栏」，看不出收起来的是左边那栏。
    panel: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><line x1="9" y1="4" x2="9" y2="20"/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
    file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
    close: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    up: '<polyline points="18 15 12 9 6 15"/>',
    down: '<polyline points="6 9 12 15 18 9"/>',
    minus: '<line x1="5" y1="12" x2="19" y2="12"/>',
    right: '<polyline points="9 18 15 12 9 6"/>',
    external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    // 编排：两个节点 + 一条向右的连线，且连线在后半段分叉一次。
    // 用 flow 那个图标会与「智能体」撞脸，两个概念在导航里必须一眼分得开。
    share: '<circle cx="5" cy="12" r="2.6"/><circle cx="18.5" cy="5.5" r="2.6"/><circle cx="18.5" cy="18.5" r="2.6"/><path d="M7.4 10.9 16.2 6.8"/><path d="M7.4 13.1 16.2 17.2"/>',
    logo: '<path d="M12 2l8 4.5v11L12 22l-8-4.5v-11L12 2z"/><path d="M12 8l4 2.2v4.6L12 17l-4-2.2v-4.6L12 8z"/>',
    // 钥匙：柄圈 + 直杆 + 两颗齿。别用 shield（那是「审计」），也别用 link
    // （那是「连接器」）——「令牌」要能被一眼认成凭据，而不是又一个入口。
    key: '<circle cx="7.5" cy="15.5" r="4"/><path d="M10.4 12.6 20 3"/><path d="M16.5 6.5l2.5 2.5"/><path d="M13.5 9.5l2.5 2.5"/>'
  };

  function ico(name) {
    return '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      (ICONS[name] || "") + "</svg>";
  }

  /* ================= 全局状态 ================= */

  var state = {
    me: null,
    ready: null,
    admin: false,
    tools: [],
    tenants: [],
    currentTenant: null,
    chat: { thread: newThreadId(), id: null, messages: [], attachments: [], agent: "" }
  };

  /* 附件上限必须与后端 chat_max_attachments / chat_max_attachment_bytes 一致，
     否则用户能选中却在发送时被拒——「选得上、发不出」比直接禁止更难受。
     后端另有 40MB 的总量校验，这里逐文件先挡一层，让提示更具体。 */
  var ATTACH_LIMIT_COUNT = 6;
  var ATTACH_LIMIT_IMAGE = 10 * 1024 * 1024;
  var ATTACH_LIMIT_PDF = 20 * 1024 * 1024;
  /* 文本附件上限，与后端 `attachment_store._TEXT_MAX_BYTES` 对齐。
     真正的上下文闸在读取时（4 万字符），这里只挡「拿一个超大日志当附件」。 */
  var ATTACH_LIMIT_TEXT = 4 * 1024 * 1024;
  var ATTACH_LIMIT_TOTAL = 40 * 1024 * 1024;
  var ATTACH_ACCEPT = "image/*,application/pdf";
  /* 构建助手另收文本类文件：Markdown、纯文本、日志与结构化配置。
     收哪些不按用途挑——「建知识库 / 排查问题 / 新增配置」在这条链路上
     都是同一件事（读原文给模型看），按用途挑等于让用户先猜自己的文件算哪类。
     accept 必须写扩展名：`.md` / `.log` 在各系统上没有统一的 MIME，
     只写 `text/markdown` 在 Windows 上会把文件灰掉、根本选不上。 */
  var STUDIO_TEXT_ACCEPT = ".md,.markdown,.txt,.text,.rst,.log,.jsonl,.ndjson," +
    ".json,.yaml,.yml,.toml,.ini,.cfg,.conf,.env,.csv,.tsv,.xml";
  var STUDIO_ATTACH_ACCEPT = ATTACH_ACCEPT + "," + STUDIO_TEXT_ACCEPT;

  function newThreadId() {
    var d = new Date();
    var pad = function (n) { return String(n).padStart(2, "0"); };
    return "console-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) +
      "-" + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds());
  }

  /* ================= 页面骨架 ================= */

  function pageHead(opts) {
    return '<div class="page-head"><div>' +
      '<div class="eyebrow">' + esc(opts.eyebrow) + "</div>" +
      '<h1 class="page-title">' + esc(opts.title) + "</h1>" +
      (opts.desc ? '<p class="page-desc">' + esc(opts.desc) + "</p>" : "") +
      "</div>" +
      (opts.actions ? '<div class="actions">' + opts.actions + "</div>" : "") +
      "</div>";
  }

  function cardHead(title, icon, right) {
    return '<div class="card-head"><div class="card-title">' + ico(icon) + esc(title) + "</div>" +
      (right || "") + "</div>";
  }

  function statSkeletons(n) {
    var out = "";
    for (var i = 0; i < n; i += 1) {
      out += '<div class="stat"><div class="skeleton" style="width:46px"></div>' +
        '<div class="skeleton" style="width:24px"></div></div>';
    }
    return out;
  }

  // `note` 是卡片底部的一行小字，用来放「这个数里不含什么」。
  // 数字旁边的脚注比正文里的一句解释更容易被看到，也不会把标签挤长。
  function stat(label, value, dim, note) {
    return '<div class="stat"><span class="stat-label">' + esc(label) + "</span>" +
      '<span class="stat-value' + (dim ? " dim" : "") + '">' + esc(value) + "</span>" +
      (note ? '<span class="stat-note">' + esc(note) + "</span>" : "") + "</div>";
  }

  /* ================= 导航 ================= */

  var NAV = [
    {
      group: "主控",
      items: [
        { id: "overview", label: "概览", icon: "grid" },
        { id: "chat", label: "对话台", icon: "message" },
        { id: "memory", label: "记忆", icon: "layers" },
        { id: "observe", label: "观测", icon: "activity" },
        // 令牌是「我」的凭据，不是平台配置项：换个账号登录，这里就该是另一批。
        // 所以放主控组，不放配置组。
        { id: "tokens", label: "访问令牌", icon: "key" }
      ]
    },
    {
      group: "配置",
      items: [
        // 权限排在工具前面：先定「能不能用、要不要确认」，再去看工具本身长什么样。
        // 两页共用同一份工具注册表，但一个回答策略、一个回答配置。
        { id: "permissions", label: "权限", icon: "shield" },
        { id: "tools", label: "工具", icon: "wrench" },
        { id: "connectors", label: "连接器", icon: "link" },
        { id: "agents", label: "智能体", icon: "flow" },
        { id: "workflows", label: "编排", icon: "share" },
        { id: "skills", label: "技能", icon: "spark" },
        { id: "knowledge", label: "知识库", icon: "book" },
        { id: "runtime", label: "运行配置", icon: "sliders" }
      ]
    },
    {
      group: "管理",
      items: [
        { id: "org", label: "组织与成员", icon: "users", admin: true },
        { id: "audit", label: "审计", icon: "shield", admin: true }
      ]
    },
    {
      group: "帮助",
      items: [
        { id: "help", label: "使用说明", icon: "help" }
      ]
    }
  ];

  /* 导航收起状态。存 localStorage：这是「这台机器上我怎么用」的偏好，
     不该每换一次页面就弹回去。 */
  var NAV_MINI_KEY = "pap.nav.mini";
  var navMini = false;

  function readNavMini() {
    try {
      return localStorage.getItem(NAV_MINI_KEY) === "1";
    } catch (e) {
      // 隐私模式下 localStorage 直接抛。读不到偏好就当展开，不能因此起不来。
      return false;
    }
  }

  function applyNavMini(mini) {
    navMini = mini;
    var app = $(".app");
    if (app) app.classList.toggle("nav-mini", mini);
    var btn = $("#btn-nav");
    if (btn) {
      // aria-expanded 用「导航是否展开」的语义，和视觉状态反过来写容易读错。
      btn.setAttribute("aria-expanded", mini ? "false" : "true");
      btn.title = mini ? "展开导航" : "收起导航";
    }
    // 就绪结论不再挂在这个状态点上：点已经挪进账号菜单、紧跟文字，不需要
    // title 再念一遍。收起态由头像角上那一点表示健康，结论走按钮的 title
    // （见 refreshStatus）——所以这里只剩导航项要处理。
    //
    // 收起后只剩图标，把文字挪到 title：否则鼠标停上去没有任何提示，
    // 只能靠记图标位置——那不是「收起来」，那是「藏起来」。
    $$(".nav-item").forEach(function (node) {
      var label = node.getAttribute("data-label");
      if (mini && label) node.title = label;
      else node.removeAttribute("title");
    });
  }

  function buildNav() {
    var nav = $("#nav");
    if (!nav) return;
    var html = "";
    NAV.forEach(function (group) {
      html += '<div class="nav-group-label">' + esc(group.group) + "</div>";
      group.items.forEach(function (item) {
        var disabled = item.admin && !state.admin;
        html += '<a class="nav-item' + (disabled ? " disabled" : "") +
          '" data-route="' + esc(item.id) + '" data-label="' + esc(item.label) +
          '" href="#/' + esc(item.id) + '">' +
          ico(item.icon) + "<span>" + esc(item.label) + "</span>" +
          (item.flag ? '<span class="nav-flag">' + esc(item.flag) + "</span>" : "") +
          "</a>";
      });
    });
    nav.innerHTML = html;
    // buildNav 会被调用两次（身份读到后重建，好让管理员项解锁），
    // 收起状态必须在重建之后重新贴上去，否则点一次刷新就弹回展开。
    applyNavMini(navMini);
    markNav(currentRoute());
  }

  var lastMarkedRoute = null;

  function markNav(id) {
    var changed = id !== lastMarkedRoute;
    lastMarkedRoute = id;
    var active = null;
    $$(".nav-item").forEach(function (node) {
      var on = node.getAttribute("data-route") === id;
      node.classList.toggle("active", on);
      if (on) active = node;
    });
    if (changed && active) revealNavItem(active);
  }

  /* 窄屏的导航是一条横向条：直接打开 `#/audit` 这类深链时，条子停在最左边，
     当前页的入口在屏幕外，「我在哪」就丢了。把高亮项滚进可见范围。
     只在它真的看不全时才动；桌面端侧栏竖排、不横向溢出，条件不成立，等于
     这段在桌面没有副作用。判「换页了」是为了别在每次重绘时把用户自己滚走
     的条子拽回去。 */
  function revealNavItem(node) {
    var scroller = node.closest(".sidebar");
    if (!scroller || scroller.scrollWidth <= scroller.clientWidth + 1) return;
    var nr = node.getBoundingClientRect();
    var sr = scroller.getBoundingClientRect();
    if (nr.left >= sr.left && nr.right <= sr.right) return;
    scroller.scrollLeft += (nr.left - sr.left) - (sr.width - nr.width) / 2;
  }

  function currentRoute() {
    var raw = (location.hash || "").replace(/^#\/?/, "").split("?")[0];
    return PAGES[raw] ? raw : "overview";
  }

  // 引用要能从跳到知识库页并展开对应切片，hash 里带一段查询串。
  // 路由本身仍然只认页面 id，参数由各页自己读。
  function routeParams() {
    var raw = (location.hash || "").replace(/^#\/?/, "");
    var pos = raw.indexOf("?");
    var out = {};
    if (pos < 0) return out;
    new URLSearchParams(raw.slice(pos + 1)).forEach(function (value, key) {
      out[key] = value;
    });
    return out;
  }

  function go(id, params) {
    var hash = "#/" + id;
    if (params && Object.keys(params).length) {
      hash += "?" + new URLSearchParams(params).toString();
    }
    if (location.hash === hash) {
      render();
    } else {
      location.hash = hash;
    }
  }

  /* ================= 视图注册 ================= */

  var PAGES = {
    overview: {
      eyebrow: "OVERVIEW", title: "应用概览",
      desc: "当前租户的能力边界与依赖健康状况。数据全部来自实时接口，不做本地缓存。",
      mount: mountOverview
    },
    chat: {
      eyebrow: "PLAYGROUND", title: "对话台",
      desc: "以当前身份驱动智能体。可附加图片或 PDF 走 OCR，写操作会先返回审批卡片，确认后才执行。",
      mount: mountChat
    },
    agents: {
      eyebrow: "AGENTS", title: "智能体",
      desc: "组织里有哪些执行主体、各自负责什么、能碰哪些工具。边界只收窄不放宽：有效权限恒为「人的权限 ∩ 智能体边界」。",
      mount: mountAgents
    },
    workflows: {
      eyebrow: "WORKFLOWS", title: "编排",
      desc: "把「谁在第几步做什么」写成版本化的图，用 /workflow 在对话里触发。图只排顺序，不分配能力——每次节点调用仍然逐跳判定边界。",
      mount: mountWorkflows
    },
    permissions: {
      eyebrow: "PERMISSIONS", title: "权限",
      desc: "按租户决定每个工具的执行结局：允许、需确认还是拒绝。改动立刻生效，作用在模型目录与调用前的审批两处。",
      mount: mountPermissions
    },
    tools: {
      eyebrow: "TOOLS", title: "工具",
      desc: "当前身份可见的工具集合，由 ToolGateway 注册、经租户策略与 Scope 过滤后返回。",
      mount: mountTools
    },
    connectors: {
      eyebrow: "CONNECTORS", title: "连接器",
      desc: "外部系统的接入点：端点与认证方式收敛到一处，凭据按租户各自保管。停用连接器会同时让它下面的工具从目录消失。",
      mount: mountConnectors
    },
    knowledge: {
      eyebrow: "KNOWLEDGE", title: "知识库",
      desc: "文档入库、切片预览与检索验证。上传与目录扫描分开管理，检索结果受租户与 Scope 双重过滤。",
      mount: mountKnowledge
    },
    runtime: {
      eyebrow: "RUNTIME", title: "运行配置",
      desc: "运行参数与外部服务接入。配置写入即生效的能力会标注，需要重启的会单独说明。",
      mount: mountRuntime
    },
    tokens: {
      eyebrow: "ACCESS TOKENS", title: "访问令牌",
      desc: "外部宿主用 Bearer 令牌调用 PAP 的 /mcp 通道。令牌代表你本人：审计里的操作人就是你，权限也随你的角色变化。",
      mount: mountTokens
    },
    org: {
      eyebrow: "ORGANIZATION", title: "组织与成员",
      desc: "租户、成员与状态的平台级管理。所有写操作都会落 ADMIN_ACTION 审计。",
      mount: mountOrg
    },
    audit: {
      eyebrow: "AUDIT", title: "审计",
      desc: "按租户查看管理动作与运行事件。审计是排障入口，不是业务数据查询入口。",
      mount: mountAudit
    },
    skills: {
      eyebrow: "SKILLS", title: "技能",
      desc: "把一类活的作业步骤写成可复用的技能：什么时候用、按什么顺序做、用哪些工具。命中后注入规划提示并收窄工具范围，改完保存即生效。",
      mount: mountSkills
    },
    memory: {
      eyebrow: "MEMORY", title: "记忆",
      desc: "跨会话沉淀下来的稳定事实：称呼、偏好、明确决策。每轮对话会挑几条作为背景注入，条数与长度都有上限。",
      mount: mountMemory
    },
    observe: {
      eyebrow: "OBSERVE", title: "观测",
      desc: "每一轮对话的调用链：走了哪些节点、模型与工具各花多久、原文是什么。只在开发期开启，数据保留 14 天。",
      mount: mountObserve
    },
    help: {
      eyebrow: "HELP", title: "使用说明",
      desc: "平台各功能怎么用、边界在哪、出问题先看哪。按主题归总，不再散落在各页面里。",
      mount: mountHelp
    }
  };

  /* ================= 概览 ================= */

  async function mountOverview(root) {
    root.innerHTML = pageHead({
      eyebrow: PAGES.overview.eyebrow,
      title: PAGES.overview.title,
      desc: PAGES.overview.desc,
      actions: '<button class="btn" type="button" id="ov-refresh">' + ico("refresh") + "刷新</button>"
    }) +
      '<div class="card">' +
      cardHead("能力概览", "grid", '<span class="chip" id="ov-scope">加载中</span>') +
      '<div class="stats" id="ov-stats">' + statSkeletons(4) + "</div>" +
      "</div>" +
      '<div class="card"><div id="ov-banner" class="banner"><span class="skeleton" style="width:180px"></span></div></div>' +
      '<div class="grid-2">' +
      '<div class="card">' +
      cardHead("就绪检查", "activity", '<span class="chip">GET /ready</span>') +
      '<div class="check-list" id="ov-checks"><div class="skeleton" style="width:70%"></div></div>' +
      '<p class="hint">全部为 true 才说明数据库、检查点、模型、工具表、限流、沙箱、MCP 都可用。</p>' +
      "</div>" +
      '<div class="card">' +
      cardHead("当前身份", "users", '<span class="chip" id="ov-admin-flag">—</span>') +
      '<dl class="kv" id="ov-identity"><div class="skeleton" style="width:80%"></div></dl>' +
      '<p class="hint">身份由 Nginx Basic Auth 注入，页面无法更改；租户以服务端判定为准。</p>' +
      "</div>" +
      "</div>" +
      '<div class="card">' +
      cardHead("工具构成", "wrench", '<a class="btn sm" href="#/tools">查看全部</a>') +
      '<div class="stats" id="ov-toolstats">' + statSkeletons(4) + "</div>" +
      "</div>";

    $("#ov-refresh").addEventListener("click", function () { mountOverview(root); });

    var results = await Promise.allSettled([
      api("/ready"),
      api("/api/v1/me"),
      api("/api/v1/tools"),
      api("/api/v1/knowledge/status")
    ]);

    var ready = results[0].status === "fulfilled" ? results[0].value : null;
    var me = results[1].status === "fulfilled" ? results[1].value : null;
    var tools = results[2].status === "fulfilled" ? results[2].value : [];
    var kb = results[3].status === "fulfilled" ? results[3].value : null;

    /* --- 能力概览 --- */
    var statBox = $("#ov-stats");
    if (statBox) {
      statBox.innerHTML =
        stat("工具", tools.length) +
        stat("需审批", tools.filter(function (t) { return t.requires_approval; }).length) +
        stat("知识库文档", kb ? kb.documents : "—", !kb) +
        stat("知识库切片", kb ? kb.chunks : "—", !kb);
    }
    var scopeChip = $("#ov-scope");
    if (scopeChip) {
      scopeChip.textContent = me ? ("租户 " + me.tenant_id) : "身份未知";
      scopeChip.className = "chip" + (me ? " ok" : " err");
    }

    /* --- 运行状态横幅 --- */
    var banner = $("#ov-banner");
    if (banner) {
      if (!ready) {
        banner.className = "banner err";
        banner.innerHTML = "<strong>就绪接口不可用</strong> · " + esc(errText(results[0].reason));
      } else {
        var checks = ready.checks || {};
        var names = Object.keys(checks);
        var bad = names.filter(function (k) { return checks[k] !== true; });
        banner.className = "banner" + (bad.length ? " err" : "");
        banner.innerHTML = "<strong>v" + esc(ready.version || "—") + "</strong> · " +
          esc(ready.environment || "—") + " · " + names.length + " 项检查，失败 " +
          bad.length + " 项" + (bad.length ? "（" + esc(bad.join("、")) + "）" : "");
      }
    }

    /* --- 检查明细 --- */
    var checkBox = $("#ov-checks");
    if (checkBox) {
      if (!ready) {
        checkBox.innerHTML = '<div class="check-row"><span class="check-dot bad"></span>' +
          '<span class="name">unreachable</span></div>';
      } else {
        var rows = Object.keys(ready.checks || {}).map(function (k) {
          var ok = ready.checks[k] === true;
          return '<div class="check-row"><span class="check-dot' + (ok ? "" : " bad") + '"></span>' +
            '<span class="name">' + esc(k) + '</span><span class="val">' +
            (ok ? "true" : esc(String(ready.checks[k]))) + "</span></div>";
        });
        checkBox.innerHTML = rows.join("") || '<div class="check-row"><span class="name">无检查项</span></div>';
      }
    }

    /* --- 身份 --- */
    var idBox = $("#ov-identity");
    if (idBox) {
      if (!me) {
        idBox.innerHTML = '<dt>错误</dt><dd class="muted">' + esc(errText(results[1].reason)) + "</dd>";
      } else {
        idBox.innerHTML =
          "<dt>subject</dt><dd>" + esc(me.subject) + "</dd>" +
          "<dt>租户</dt><dd>" + esc(me.tenant_id) + "（" + esc(me.tenant_name || "—") + "）</dd>" +
          "<dt>角色</dt><dd>" + esc((me.roles || []).join(", ") || "—") + "</dd>" +
          "<dt>可切租户</dt><dd>" + esc((me.tenants || []).join(", ") || "—") + "</dd>" +
          "<dt>平台管理员</dt><dd>" + (me.is_platform_admin ? "是" : "否") + "</dd>";
      }
    }
    var adminFlag = $("#ov-admin-flag");
    if (adminFlag) {
      adminFlag.textContent = state.admin ? "platform_admin" : "普通身份";
      adminFlag.className = "chip" + (state.admin ? " dark" : "");
    }

    /* --- 工具构成 --- */
    var toolBox = $("#ov-toolstats");
    if (toolBox) {
      var byKind = {};
      tools.forEach(function (t) { byKind[t.kind] = (byKind[t.kind] || 0) + 1; });
      var kindLabel = { http: "HTTP", mcp: "MCP", builtin: "内置", sandbox: "沙箱", script: "脚本" };
      var items = [stat("只读", tools.filter(function (t) { return !t.requires_approval; }).length)];
      Object.keys(byKind).sort().forEach(function (k) {
        items.push(stat(kindLabel[k] || k, byKind[k]));
      });
      toolBox.innerHTML = items.join("");
    }
  }

  /* ================= 对话台 ================= */

  function isPdf(att) {
    return (att && (att.kind === "pdf" || att.mime === "application/pdf"));
  }

  /* 文本附件判定的两份：`fileIsText` 用于刚选的 File，`isText` 用于托盘/历史里的附件。
     不能只看 MIME：浏览器对 `.md` 常常给空 type，后缀才是稳定线索。
     白名单与后端 `attachment_store._TEXT_SUFFIXES` 一一对应，
     少一个后缀的表现是「选文件框里灰掉」，用户根本走不到报错那一步。 */
  var TEXT_SUFFIX_RE =
    /\.(md|markdown|txt|text|rst|log|jsonl|ndjson|json|yaml|yml|toml|ini|cfg|conf|env|csv|tsv|xml)$/i;

  function fileIsText(file) {
    if (!file) return false;
    return TEXT_SUFFIX_RE.test(file.name || "") ||
      /^text\/(markdown|plain|x-markdown)$/i.test(file.type || "") ||
      /^(application|text)\/(json|x-ndjson|yaml|x-yaml|toml|xml|csv)$/i.test(file.type || "");
  }

  /* data URL 头里写的 MIME。只影响日志与请求体的可读性：
     服务端会按后缀 + 内容重新判定，并不信这一格。
     写准一点的好处是排查时一眼能看出「这份日志是 json 还是 yaml」，
     而不是所有文本都显示成 text/plain。 */
  function fileTextMime(file) {
    var name = (file && file.name) || "";
    if (/\.(md|markdown)$/i.test(name)) return "text/markdown";
    if (/\.(json)$/i.test(name)) return "application/json";
    if (/\.(jsonl|ndjson)$/i.test(name)) return "application/x-ndjson";
    if (/\.(yaml|yml)$/i.test(name)) return "text/yaml";
    if (/\.(toml)$/i.test(name)) return "application/toml";
    if (/\.(csv)$/i.test(name)) return "text/csv";
    if (/\.(tsv)$/i.test(name)) return "text/tab-separated-values";
    if (/\.(xml)$/i.test(name)) return "text/xml";
    return "text/plain";
  }

  function isText(att) {
    if (!att) return false;
    return att.kind === "text" ||
      att.mime === "text/markdown" || att.mime === "text/plain";
  }

  function attSrc(att) {
    // 刚发出去的消息只有本地 data URL；翻历史时（含刷新后）用服务端给的接口地址。
    return att.url || att.dataUrl || "";
  }

  function fmtBytes(n) {
    var size = Number(n) || 0;
    if (size < 1024) return size + " B";
    if (size < 1024 * 1024) return (size / 1024).toFixed(0) + " KB";
    return (size / 1024 / 1024).toFixed(1) + " MB";
  }

  function attName(att, index) {
    if (att.name) return att.name;
    if (isText(att)) return "文本" + (index + 1) + ".txt";
    return (isPdf(att) ? "文档" : "图片") + (index + 1) + (isPdf(att) ? ".pdf" : "");
  }

  /* 气泡内的附件：图片给缩略图，PDF 给内嵌预览，文本给一张文件卡。
     都用同一个 src 逻辑——本地 data URL 与历史接口地址在浏览器看来没有区别。 */
  function attachmentStrip(items) {
    if (!items || !items.length) return "";

    var cells = items.map(function (att, index) {
      var src = attSrc(att);
      if (!src) return "";
      var name = attName(att, index);

      if (isText(att)) {
        // 文本不进 <embed>：浏览器对 text/markdown 的处理各不相同，
        // 有的直接下载、有的当源码列出。给一张卡 + 下载链接，行为可预期。
        return '<div class="att-item text">' +
          '<div class="att-head">' + ico("file") +
          '<span class="att-name" title="' + esc(name) + '">' + esc(name) + "</span>" +
          '<span class="att-size">' + esc(fmtBytes(att.size)) + "</span>" +
          '<a class="att-open" href="' + esc(src) + '" download="' + esc(name) + '">下载</a>' +
          "</div></div>";
      }

      if (isPdf(att)) {
        return '<div class="att-item pdf">' +
          '<div class="att-head">' + ico("file") +
          '<span class="att-name" title="' + esc(name) + '">' + esc(name) + "</span>" +
          '<span class="att-size">' + esc(fmtBytes(att.size)) + "</span>" +
          '<a class="att-open" href="' + esc(src) + '" target="_blank" rel="noopener">新标签打开</a>' +
          "</div>" +
          '<embed class="att-frame" src="' + esc(src) + '" type="application/pdf">' +
          "</div>";
      }

      return '<a class="att-item img" href="' + esc(src) + '"' +
        ' data-lightbox-src="' + esc(src) + '" data-lightbox-name="' + esc(name) + '"' +
        ' title="' + esc(name) + ' · 点击放大">' +
        '<img src="' + esc(src) + '" alt="' + esc(name) + '" loading="lazy">' +
        "</a>";
    }).join("");

    if (!cells) return "";
    return '<div class="att-strip">' + cells + "</div>";
  }

  /* ---------------- 二次确认框 ----------------
     删除类操作必须弹出确认，并且要列清楚会销毁什么。
     原来用的是「同一个删除按钮点两次」：误点两次远比误点一次容易，
     而且列表里看不出删掉会连带清理哪些数据。

     弹层复用 .modal-mask / .modal 体系（模型供应商弹窗同款），
     不另起一套：两套弹层并存，早晚在样式上打架——这次已经撞过一回。 */

  var confirmUI = { mask: null, resolve: null };

  function ensureConfirm() {
    if (confirmUI.mask) return confirmUI.mask;

    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML =
      '<div class="modal confirm" role="dialog" aria-modal="true" aria-labelledby="cf-title">' +
      '<div class="modal-head"><span class="modal-title" id="cf-title"></span>' +
      '<button class="modal-close" type="button" data-cf="cancel" aria-label="关闭">×</button>' +
      "</div>" +
      '<div class="modal-body" id="cf-body"></div>' +
      '<div class="modal-foot">' +
      '<button class="btn" type="button" data-cf="cancel">取消</button>' +
      '<button class="btn danger-fill" type="button" data-cf="ok" id="cf-ok">确认</button>' +
      "</div></div>";

    document.body.appendChild(mask);
    confirmUI.mask = mask;

    mask.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-cf]");
      if (btn) {
        closeConfirm(btn.getAttribute("data-cf") === "ok");
        return;
      }
      // 点遮罩等同取消：破坏性操作只认明确点「确认」。
      if (ev.target === mask) closeConfirm(false);
    });

    return mask;
  }

  function openConfirm(opts) {
    var mask = ensureConfirm();

    $("#cf-title").textContent = opts.title || "确认操作";
    $("#cf-body").innerHTML = opts.body || "";
    $("#cf-ok").textContent = opts.okText || "确认";

    document.addEventListener("keydown", onConfirmKey, true);

    // 焦点默认落在「取消」：连按回车不该把数据按没。
    var cancel = $('.btn[data-cf="cancel"]', mask);
    if (cancel) cancel.focus();

    return new Promise(function (resolve) {
      confirmUI.resolve = resolve;
    });
  }

  function onConfirmKey(ev) {
    if (!confirmUI.mask) return;

    if (ev.key === "Escape") {
      ev.preventDefault();
      closeConfirm(false);
      return;
    }

    if (ev.key !== "Tab") return;

    // 焦点环：卡片里的可聚焦元素不多，手工循环比引第三方库划算。
    var nodes = $$("[data-cf]", confirmUI.mask).filter(function (node) {
      return node.offsetParent !== null;
    });
    if (!nodes.length) return;

    var first = nodes[0];
    var last = nodes[nodes.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
      ev.preventDefault();
      last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
      ev.preventDefault();
      first.focus();
    }
  }

  function closeConfirm(result) {
    var mask = confirmUI.mask;
    if (!mask) return;

    // 整个遮罩摘掉：正文里有会话标题等用户内容，不该留在 DOM 里。
    mask.remove();
    confirmUI.mask = null;
    document.removeEventListener("keydown", onConfirmKey, true);

    var resolve = confirmUI.resolve;
    confirmUI.resolve = null;
    if (resolve) resolve(result === true);
  }

  /* ---------------- 内置图片查看器 ----------------
     气泡里的缩略图最大 220×170，看不清现场记录表上的小字。点开全屏看。
     用 DOM 单例挂到 body：反复开关不累积节点，也不受 .chat-log 重渲染影响。 */

  var LB_MIN_SCALE = 0.2;
  var LB_MAX_SCALE = 8;
  var LB_STEP = 1.25;

  var lightbox = {
    el: null,
    items: [],
    index: 0,
    scale: 1,
    tx: 0,
    ty: 0,
    drag: null
  };

  function lbStage() { return $("#lb-stage"); }
  function lbImg() { return $("#lb-img"); }

  function ensureLightbox() {
    if (lightbox.el) return lightbox.el;

    var el = document.createElement("div");
    el.className = "lightbox";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-label", "图片预览");
    el.innerHTML =
      '<div class="lb-bar">' +
      '<span class="lb-name" id="lb-name"></span>' +
      '<span class="lb-count" id="lb-count"></span>' +
      '<span class="lb-gap"></span>' +
      '<button class="lb-btn" type="button" data-lb="out" title="缩小（滚轮）">' + ico("minus") + "</button>" +
      '<button class="lb-scale" type="button" data-lb="fit" title="适应窗口（也可双击图片）">100%</button>' +
      '<button class="lb-btn" type="button" data-lb="in" title="放大（滚轮）">' + ico("plus") + "</button>" +
      '<a class="lb-btn" id="lb-src" href="#" target="_blank" rel="noopener" ' +
      'title="在新标签打开原图">' + ico("external") + "</a>" +
      '<button class="lb-btn" type="button" data-lb="close" title="关闭（Esc）">' + ico("close") + "</button>" +
      "</div>" +
      '<div class="lb-stage" id="lb-stage">' +
      '<img class="lb-img" id="lb-img" alt="" draggable="false">' +
      '<button class="lb-nav prev" type="button" data-lb="prev" title="上一张（←）">' + ico("back") + "</button>" +
      '<button class="lb-nav next" type="button" data-lb="next" title="下一张（→）">' + ico("right") + "</button>" +
      "</div>";

    document.body.appendChild(el);
    lightbox.el = el;

    el.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-lb]");

      if (btn) {
        var act = btn.getAttribute("data-lb");
        if (act === "close") closeLightbox();
        else if (act === "in") lbZoom(1);
        else if (act === "out") lbZoom(-1);
        else if (act === "fit") lbFit();
        else if (act === "prev") lbGo(-1);
        else if (act === "next") lbGo(1);
        return;
      }

      // 点空白处关闭；点图片本身不关闭
      if (ev.target === el || ev.target === lbStage()) closeLightbox();
    });

    lbStage().addEventListener("wheel", function (ev) {
      ev.preventDefault();
      lbZoom(ev.deltaY > 0 ? -1 : 1, ev.clientX, ev.clientY);
    }, { passive: false });

    lbStage().addEventListener("dblclick", function () {
      if (lightbox.scale > 1.001) lbFit();
      else lbZoom(1);
    });

    // 拖拽平移：只在放大后启用，否则动不了反而误触
    lbStage().addEventListener("pointerdown", function (ev) {
      if (lightbox.scale <= 1.001 || ev.button !== 0) return;
      lightbox.drag = {
        x: ev.clientX, y: ev.clientY, tx: lightbox.tx, ty: lightbox.ty
      };
      lbStage().setPointerCapture(ev.pointerId);
      lbStage().classList.add("dragging");
    });

    lbStage().addEventListener("pointermove", function (ev) {
      if (!lightbox.drag) return;
      lightbox.tx = lightbox.drag.tx + (ev.clientX - lightbox.drag.x);
      lightbox.ty = lightbox.drag.ty + (ev.clientY - lightbox.drag.y);
      lbApply();
    });

    function endDrag() {
      lightbox.drag = null;
      lbStage().classList.remove("dragging");
    }

    lbStage().addEventListener("pointerup", endDrag);
    lbStage().addEventListener("pointercancel", endDrag);

    document.addEventListener("keydown", function (ev) {
      if (!el.classList.contains("on")) return;

      if (ev.key === "Escape") { closeLightbox(); ev.preventDefault(); }
      else if (ev.key === "ArrowLeft") { lbGo(-1); ev.preventDefault(); }
      else if (ev.key === "ArrowRight") { lbGo(1); ev.preventDefault(); }
      else if (ev.key === "+" || ev.key === "=") { lbZoom(1); ev.preventDefault(); }
      else if (ev.key === "-") { lbZoom(-1); ev.preventDefault(); }
      else if (ev.key === "0") { lbFit(); ev.preventDefault(); }
    });

    return el;
  }

  function lbApply() {
    var img = lbImg();
    if (!img) return;

    img.style.transform = "translate(" + lightbox.tx + "px," + lightbox.ty + "px) " +
      "scale(" + lightbox.scale + ")";

    var scaleBtn = lightbox.el.querySelector(".lb-scale");
    if (scaleBtn) scaleBtn.textContent = Math.round(lightbox.scale * 100) + "%";

    if (lbStage()) lbStage().classList.toggle("can-pan", lightbox.scale > 1.001);
  }

  function lbFit() {
    lightbox.scale = 1;
    lightbox.tx = 0;
    lightbox.ty = 0;
    lbApply();
  }

  function lbZoom(dir, clientX, clientY) {
    var next = dir > 0 ? lightbox.scale * LB_STEP : lightbox.scale / LB_STEP;
    next = Math.min(LB_MAX_SCALE, Math.max(LB_MIN_SCALE, next));

    if (next === lightbox.scale) return;

    var stage = lbStage();

    if (clientX === undefined && stage) {
      // 按钮触发时以视口中心为锚点，和滚轮的观感保持一致
      var rect = stage.getBoundingClientRect();
      clientX = rect.left + rect.width / 2;
      clientY = rect.top + rect.height / 2;
    }

    if (clientX !== undefined && stage) {
      // 以光标为锚点缩放：锚点在图片上的相对位置保持不动
      var box = stage.getBoundingClientRect();
      var ox = clientX - (box.left + box.width / 2);
      var oy = clientY - (box.top + box.height / 2);
      var ratio = next / lightbox.scale;
      lightbox.tx = (lightbox.tx - ox) * ratio + ox;
      lightbox.ty = (lightbox.ty - oy) * ratio + oy;
    }

    lightbox.scale = next;
    lbApply();
  }

  function lbRender() {
    var img = lbImg();
    var item = lightbox.items[lightbox.index];
    if (!img || !item) return;

    img.src = item.src;
    img.alt = item.name;

    var nameNode = $("#lb-name");
    if (nameNode) {
      nameNode.textContent = item.name;
      nameNode.title = item.name;
    }

    var countNode = $("#lb-count");
    if (countNode) {
      countNode.textContent = lightbox.items.length > 1
        ? (lightbox.index + 1) + " / " + lightbox.items.length : "";
    }

    var srcLink = $("#lb-src");
    if (srcLink) srcLink.href = item.src;

    var multi = lightbox.items.length > 1;
    lightbox.el.querySelectorAll(".lb-nav").forEach(function (btn) {
      btn.hidden = !multi;
    });

    lbFit();
  }

  function lbGo(delta) {
    if (lightbox.items.length < 2) return;

    var next = lightbox.index + delta;
    if (next < 0) next = lightbox.items.length - 1;
    if (next >= lightbox.items.length) next = 0;

    lightbox.index = next;
    lbRender();
  }

  function openLightbox(items, index) {
    if (!items || !items.length) return;

    var el = ensureLightbox();
    lightbox.items = items;
    lightbox.index = Math.max(0, Math.min(index || 0, items.length - 1));
    el.classList.add("on");
    document.body.classList.add("lb-open");
    lbRender();
  }

  function closeLightbox() {
    if (!lightbox.el) return;

    lightbox.el.classList.remove("on");
    document.body.classList.remove("lb-open");
    lightbox.drag = null;

    // 清掉 src 与清单：关掉的大图立刻从内存里释放，翻历史时不会一路囤积
    var img = lbImg();
    if (img) img.removeAttribute("src");

    lightbox.items = [];
    lightbox.index = 0;
  }

  /* 从被点的缩略图出发，收集同一气泡里的全部图片，支持左右切换。 */
  function lightboxFromThumb(trigger) {
    var scope = trigger.parentNode;
    while (scope && !(scope.classList && scope.classList.contains("att-strip"))) {
      scope = scope.parentNode;
    }

    var nodes = scope ? $$("[data-lightbox-src]", scope) : [trigger];
    if (!nodes.length) return;

    var items = nodes.map(function (node) {
      return {
        src: node.getAttribute("data-lightbox-src"),
        name: node.getAttribute("data-lightbox-name") || "图片"
      };
    });

    var index = 0;
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i] === trigger) { index = i; break; }
    }

    openLightbox(items, index);
  }

  /* 气泡上的时间：当天只给时分，跨天补月日，跨年再补年份。
     完整时刻放 title，悬停即可看到秒。 */
  function msgClock(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var pad = function (n) { return String(n).padStart(2, "0"); };
    var now = new Date();
    var text = pad(d.getHours()) + ":" + pad(d.getMinutes());
    if (d.getFullYear() !== now.getFullYear()) {
      return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
        " " + text;
    }
    if (d.getMonth() !== now.getMonth() || d.getDate() !== now.getDate()) {
      return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + text;
    }
    return text;
  }

  /* 说话人与时间同一行：时间靠右，不挤占正文。
     没有时间就不占位——「思考中」的气泡本来还没有服务端时间。 */
  function msgWho(label, iso) {
    var clock = msgClock(iso);
    if (!clock) return '<div class="who">' + label + "</div>";
    return '<div class="who">' + label +
      '<span class="msg-time" title="' + esc(fmtTime(iso)) + '">' +
      esc(clock) + "</span></div>";
  }

  /* 模型输出统一走渲染层：表格、曲线、流程图都在 assets/render.js 里变成 HTML。
     渲染层异常时退回纯文本——聊天不能因为一张图画不出来就空白。 */
  function richText(text) {
    if (window.PAPRender && typeof window.PAPRender.rich === "function") {
      try {
        return window.PAPRender.rich(text);
      } catch (e) {
        /* 落到下面的纯文本 */
      }
    }
    return esc(text);
  }

  /* 识别原文折叠：默认收起，一份 PDF 不至于把气泡撑到两屏。
     [第 N 页] 单独着色，逐页核对时好定位。 */
  function ocrFold(text) {
    var full = String(text || "");
    var shown = full.length > 4000
      ? full.slice(0, 4000) + "\n…（后略，全文 " + full.length + " 字）"
      : full;
    var body = esc(shown).replace(/\[第\s*\d+\s*页\]/g, function (mark) {
      return '<span class="pg">' + mark + "</span>";
    });
    return '<details class="ocr-fold"><summary>识别原文 · ' + full.length +
      " 字</summary><pre>" + body + "</pre></details>";
  }

  /* ---- 压缩边界的分隔条 ----
   *
   * 被折叠的轮次不再以原文进入模型，但它们在界面上还在。不加标记的话，
   * 用户往下翻会觉得「这些它都看得见」，追问三轮前的细节被答错时也找不到原因。
   *
   * 只画**最新一条**摘要的水位：早期摘要已经被后来那次覆盖，画出来会是
   * 两条「更早的 N 轮」上下叠着，读者得自己算哪个才算数。
   */
  function ctxSeam() {
    var rows = chatUI.summaries || [];
    if (!rows.length || !PAPRender.foldSeam) return null;

    var latest = rows[rows.length - 1];   // 接口按时间正序返回
    var seam = PAPRender.foldSeam(state.chat.messages, latest.through_message_id);
    if (!seam) return null;

    return { index: seam.index, turns: seam.turns, row: latest };
  }

  function ctxSeamHtml(seam) {
    var row = seam.row;
    var meta = [];
    if (row.tokens_before > 0) {
      meta.push(fmtNum(row.tokens_before) + " → " + fmtNum(row.tokens_after) + " token");
    }
    if (row.created_at) meta.push(fmtTime(row.created_at));

    return '<details class="ctx-seam">' +
      '<summary><span class="ctx-seam-title">更早的 ' + seam.turns +
      " 轮已折叠为摘要</span><span class=\"ctx-seam-hint\">看摘要</span></summary>" +
      (meta.length ? '<div class="ctx-seam-meta">' + esc(meta.join(" · ")) + "</div>" : "") +
      '<pre class="ctx-summ">' + esc(row.summary || "") + "</pre>" +
      "</details>";
  }

  function chatLogHtml() {
    var seam = ctxSeam();
    var msgs = state.chat.messages;

    /* 追问卡片一旦"过去"了就不能再点：这条之后出现过用户消息，说明那一轮
       已经被回答（或用户直接打字跳过了）。判据只用消息顺序，不另存状态——
       DOM 顺序就是时间顺序，多存一份就多一处会不同步的地方。 */
    var closedAt = {};
    var seenUserAfter = false;
    for (var i = msgs.length - 1; i >= 0; i--) {
      closedAt[i] = seenUserAfter;
      if (msgs[i].role === "user") seenUserAfter = true;
    }

    var parts = [];
    msgs.forEach(function (msg, i) {
      parts.push(chatBubble(msg, i, closedAt[i]));
      // 插在水位那条**之后**：它本身属于被折叠的那一段。
      if (seam && seam.index === i) parts.push(ctxSeamHtml(seam));
    });
    return parts.join("");
  }

  function chatBubble(msg, index, closed) {
    // data-idx 是刻度导航与搜索跳转的落点：DOM 顺序与 state.chat.messages 一致，
    // 但重渲染后节点会换，所以标识必须挂在元素上而不是留着旧引用。
    var idx = typeof index === "number" ? index : "";
    if (msg.role === "user") {
      var mine = '<div class="msg user" data-idx="' + idx + '">' + msgWho("你", msg.at) +
        attachmentStrip(msg.attachments);
      if (msg.text) mine += '<div class="body">' + esc(msg.text) + "</div>";
      return mine + "</div>";
    }
    var out = '<div class="msg" data-idx="' + idx + '">' + msgWho("Agent", msg.at) +
      '<div class="body rich' +
      (msg.status === "error" ? " muted" : "") + '">' + richText(msg.text) + "</div>";
    // 追问卡片紧跟在正文之后：正文说「需要你定一下哪一项」，卡片就是那几项。
    // 它比识别原文和 meta 行都更该被先看到。
    if (msg.clarify && msg.clarify.length && window.PAPClarify) {
      out += window.PAPClarify.html(msg.clarify, {
        index: index,
        picks: msg.clarifyPicks,
        closed: Boolean(closed)
      });
    }
    if (msg.ocrText) {
      out += ocrFold(msg.ocrText);
    }
    if (msg.meta) {
      out += '<div class="meta-line">' + esc(msg.meta) + "</div>";
    }
    if (msg.citations && msg.citations.length) {
      out += '<ul class="cites">' + msg.citations.map(function (c) {
        var score = typeof c.score === "number" ? c.score.toFixed(3) : c.score;
        var link = "#/knowledge?doc=" + encodeURIComponent(c.document_id || "") +
          "&chunk=" + encodeURIComponent(c.chunk_id || "");
        return '<li><a class="linkish" href="' + esc(link) + '">' +
          esc(c.title || "未命名") + " · " + esc(c.section || "—") + "</a>" +
          " · score=" + esc(score) + "</li>";
      }).join("") + "</ul>";
    }
    if (msg.approval) {
      var a = msg.approval;
      var preview = a.preview || {};
      /* 单子结束后，卡上该看的是结果，不是「还能等到几点」；
         还在等人的时候反过来。同一个位置按状态换内容，而不是两套卡。 */
      var closed = Boolean(a.done);

      /* 卡片结构：抬头（谁要做什么 + 状态）→ 单子本体（由 render.js 把参数读成
         工单卡）→ 流程条（结论就写在条下面，不另占一行）→ 凭据（审批号 / 时间 / 原始 JSON）。

         原始参数没删，只是折进了 <details>：卡面是判读用的，JSON 是核对用的。
         只剩卡面的话，遇到「显示的字段和提交的不一致」这类事故就无从对证。 */
      var body = PAPRender.approvalBody(a);

      out += '<div class="approval' + (closed ? " is-done" : "") + '">' +
        '<div class="a-head"><span class="a-title">' +
        esc(a.title || "需要审批") + " · " +
        /* 当场那张卡的 preview 来自本轮响应，只有 tool_name 与 operation；
           刷新的卡来自审批单，有 tool_display_name。三级兜底，两种卡才长一样——
           否则同一个工具在一处叫「创建维护工单」、在另一处叫 create_maintenance_ticket。 */
        esc(preview.tool_display_name || preview.operation || preview.tool_name || "—") +
        "</span>" + PAPRender.statusChip(a) + "</div>" +
        (preview.delegation && preview.delegation.from
          ? '<div class="a-row">由「' + esc(preview.delegation.from) + "」转交</div>"
          : "") +
        /* 有本体时结论由流程条来说；没有本体（审批单已被清理、状态读不到）
           就只剩这句话能说明情况，必须留着。 */
        (!body && closed ? '<div class="a-row a-done">' + esc(a.done) + "</div>" : "") +
        body +
        '<details class="a-raw"><summary>原始参数</summary><pre class="code">' +
        esc(JSON.stringify(preview.parameters || preview, null, 2)) + "</pre></details>" +
        '<div class="a-foot">' +
        '<span>审批 ID <span class="mono">' + esc(a.approval_id) + "</span></span>" +
        (closed
          ? (a.executed_at ? "<span>执行于 " + esc(fmtTime(a.executed_at)) + "</span>" : "")
          : "<span>过期时间 " + esc(fmtTime(a.expires_at)) + "</span>") +
        "</div>";
      /* 出不出按钮只看 `open`。以前看「有没有结论文字」，结果提交失败时
         写了一句错误信息，按钮就跟着消失，卡片非刷新不可。 */
      if (a.open !== false && !a.done) {
        out += '<div class="actions" style="margin-top:9px">' +
          '<button class="btn primary sm" data-act="confirm" data-id="' + esc(a.approval_id) +
          '" data-idx="' + index + '">确认执行</button>' +
          '<button class="btn danger sm" data-act="cancel" data-id="' + esc(a.approval_id) +
          '" data-idx="' + index + '">取消</button></div>';
      }
      if (a.note) {
        out += '<div class="a-row">' + esc(a.note) + "</div>";
      }
      out += "</div>";
    }
    return out + "</div>";
  }

  function renderChatLog() {
    var log = $("#chat-log");
    if (!log) return;
    if (!state.chat.messages.length) {
      log.classList.add("is-empty");
      log.innerHTML = '<div class="empty"><strong>会话已就绪</strong>' +
        "输入自然语言即可。试试「查一下 station-001 的压力快照」，" +
        "或点左下角 + 附一张现场记录表照片、一份 PDF 报表。</div>";
      railSync();
      return;
    }
    log.classList.remove("is-empty");
    log.innerHTML = chatLogHtml();
    log.scrollTop = log.scrollHeight;

    $$("[data-act]", log).forEach(function (btn) {
      btn.addEventListener("click", function () {
        resolveApproval(btn.getAttribute("data-id"), btn.getAttribute("data-act"),
          parseInt(btn.getAttribute("data-idx"), 10), btn);
      });
    });
    bindClarifyOnce(log);

    // 重渲染会把 mark 冲掉：搜索状态留在 chatUI 里，渲染完再挂回去。
    // 刻度导航自己从 DOM 认轮次，重渲染后重新同步一次即可。
    railSync();
    if (chatUI.findQuery) applyFind();
  }

  /* ---- 追问卡片（radio 选择面板） ----
   *
   * 后端 `intent=clarify` 时带 `metadata.clarify.items`，这里把它变成一张可点选的卡。
   * 三条约定：
   *   1. 单组点一下就是回答（不再要一次「确认」）；多组选齐了点「提交」。
   *   2. 每组末尾的「其他」由前端补，选中后展开输入框——模型只负责给它能枚举的候选。
   *   3. 卡片一旦「过去」了（后面出现了用户消息）就不再可点，改显示选了哪一项。
   *
   * 交互状态全在 DOM + `msg.clarifyPicks` 上，不另设全局表：
   * 面板整块重渲染后，从 `clarifyPicks` 就能还原，不存在两份状态对不上的可能。
   */

  function clarifyPanelOf(node) {
    return node && node.closest ? node.closest(".clarify") : null;
  }

  function clarifyTarget(panel) {
    var idx = parseInt(panel.getAttribute("data-clarify"), 10);
    var msg = state.chat.messages[idx];
    if (!msg || !msg.clarify || !msg.clarify.length) return null;
    return { idx: idx, msg: msg, items: msg.clarify, picks: msg.clarifyPicks || {} };
  }

  // 整块重画而不是各处逐个改：高亮、输入框显隐、提交按钮可用性都由
  // `PAPClarify.html` 一处决定，分头改迟早出现「按钮亮着但没选完」。
  // `focusQi` 是刚选中「其他」的那一组：重画会把光标冲掉，主动放回去。
  function repaintClarify(panel, ctx, focusQi) {
    var C = window.PAPClarify;
    var holder = document.createElement("div");
    holder.innerHTML = C.html(ctx.items, { index: ctx.idx, picks: ctx.msg.clarifyPicks });
    var fresh = holder.firstChild;
    if (!fresh || !panel.parentNode) return;
    panel.parentNode.replaceChild(fresh, panel);

    if (focusQi === null || focusQi === undefined) return;
    var input = $('.cl-input[data-cl-input="' + focusQi + '"]', fresh);
    if (input && !input.hidden) input.focus();
  }

  function submitClarify(ctx) {
    var C = window.PAPClarify;
    if (chatUI.sending) return;
    var picks = ctx.msg.clarifyPicks || {};
    if (!C.allAnswered(ctx.items, picks)) return;
    var text = C.compose(ctx.items, picks);
    if (!text) return;
    deliverChat(text, (state.chat.attachments || []).slice());
  }

  function bindClarifyOnce(log) {
    if (!log || log.dataset.clarifyBound === "1" || !window.PAPClarify) return;
    log.dataset.clarifyBound = "1";
    var C = window.PAPClarify;

    // 事件委托：卡片随每次重渲染整块重建，逐张绑定会跟着失效。
    log.addEventListener("change", function (ev) {
      var input = ev.target;
      if (!input || input.type !== "radio") return;
      var panel = clarifyPanelOf(input);
      var item = input.closest ? input.closest(".cl-item") : null;
      if (!panel || !item) return;
      var ctx = clarifyTarget(panel);
      if (!ctx) return;

      var qi = parseInt(item.getAttribute("data-qi"), 10);
      var picks = Object.assign({}, ctx.msg.clarifyPicks || {});
      picks[qi] = { choice: input.value, custom: (picks[qi] || {}).custom || "" };
      ctx.msg.clarifyPicks = picks;

      // 单组 + 选了具体候选 = 用户已经回答了。再让他点一次「提交」就是多此一举。
      if (ctx.items.length === 1 && input.value !== C.CUSTOM) {
        submitClarify(ctx);
        return;
      }
      repaintClarify(panel, ctx, input.value === C.CUSTOM ? qi : null);
    });

    // 打字不重画（会丢光标），只把按钮的可用性跟上。
    log.addEventListener("input", function (ev) {
      var input = ev.target;
      if (!input || !input.classList || !input.classList.contains("cl-input")) return;
      var panel = clarifyPanelOf(input);
      var ctx = panel ? clarifyTarget(panel) : null;
      if (!ctx) return;

      var qi = parseInt(input.getAttribute("data-cl-input"), 10);
      var picks = Object.assign({}, ctx.msg.clarifyPicks || {});
      picks[qi] = { choice: C.CUSTOM, custom: input.value };
      ctx.msg.clarifyPicks = picks;

      var submit = $("[data-cl-submit]", panel);
      if (!submit) return;
      var ready = C.allAnswered(ctx.items, picks);
      submit.disabled = !ready;
      var hint = $(".cl-hint", panel);
      if (hint) {
        hint.textContent = ready
          ? "共 " + ctx.items.length + " 项，都已选好。"
          : "还在等 " + C.pending(ctx.items, picks) + " 项。";
      }
    });

    // 输入框里回车 = 提交（多组时只在都选好了才生效）。
    log.addEventListener("keydown", function (ev) {
      var input = ev.target;
      if (!input || !input.classList || !input.classList.contains("cl-input")) return;
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      var panel = clarifyPanelOf(input);
      var ctx = panel ? clarifyTarget(panel) : null;
      if (ctx) submitClarify(ctx);
    });

    log.addEventListener("click", function (ev) {
      var btn = ev.target.closest ? ev.target.closest("[data-cl-submit]") : null;
      if (!btn) return;
      var panel = clarifyPanelOf(btn);
      var ctx = panel ? clarifyTarget(panel) : null;
      if (ctx) submitClarify(ctx);
    });
  }

  /* ---- 会话内刻度导航 ----
   *
   * 长会话里滚回某一轮是很费力的事。右侧一条虚线刻度轴，每条 = 一轮提问：
   * 位置按内容比例摆放，所以它同时回答「这是第几轮」和「我还剩多少」。
   * 滚动时高亮跟随，悬停出摘要，点击滚到那一轮。
   *
   * 不做锚点：不写 hash、不改 URL、不靠 :target。控制台的视图切换本身就是 hash 路由，
   * 锚点既会把浏览历史塞满，也会和它抢同一个 hash。
   */

  var RAIL_MIN_ROUNDS = 3;   // 少于 3 轮不渲染：两轮之间不需要地图
  var RAIL_TICK_PAD = 6;     // 首尾刻度距轨道端点的余量
  var RAIL_BAND_TOP = 16;    // 阅读线在容器顶边下方多少像素

  // 纯逻辑在 rail.js：截断、布局、判定当前轮。拆出去的理由与 render.js 相同——
  // 那三件事错了只看截图发现不了，放在这里就能用 node 直接断言。
  var railMath = window.PAPRail || null;

  var chatRail = {
    rounds: [],   // [{ id, node, summary }]
    sig: "",      // 轮次签名：不变就不重建刻度
    active: null,
    mo: null,     // MutationObserver：流式追加时同步轮次
    ro: null,     // ResizeObserver：容器尺寸变化时重量位置
    frame: 0
  };

  function railRoot() { return $("#chat-rail"); }
  function railLog() { return $("#chat-log"); }

  // 一轮 = 一条用户消息。回答跟着提问走，单独给它一条刻度只会让轨道变密。
  function railRoundNodes() {
    var log = railLog();
    if (!log) return [];
    return $$(".msg.user[data-idx]", log);
  }

  /* 从 DOM 里认轮次，不另存一份 messages：气泡就是唯一事实来源。
     列表重渲染、流式追加都以 DOM 为准，导航自动跟上。 */
  function railSync() {
    var log = railLog();
    var root = railRoot();
    // rail.js 没加载成功时宁可没有导航，也不要半截报错把整个对话页带崩。
    if (!railMath || !log || !root) return;

    var nodes = railRoundNodes();
    var sig = nodes.map(function (node) {
      return node.getAttribute("data-idx");
    }).join(",");
    // 光比 data-idx 不够：轮询刷新会把气泡整体重绘，索引一模一样但节点全换了，
    // 沿用旧引用就会拿着已经脱离文档的节点去量位置（rect 全是 0），高亮再也不动。
    var sameNodes = nodes.length === chatRail.rounds.length &&
      nodes.every(function (node, index) { return chatRail.rounds[index].node === node; });

    if (sig !== chatRail.sig || !sameNodes) {
      chatRail.sig = sig;
      chatRail.rounds = nodes.map(function (node, index) {
        var body = $(".body", node);
        return {
          id: "r" + index + "-" + node.getAttribute("data-idx"),
          node: node,
          summary: railMath.summarize(body ? body.textContent : node.textContent)
        };
      });
      railBuild();
      railMeasure();
    }
    railRecompute();
  }

  function railBuild() {
    var root = railRoot();
    if (!root) return;

    if (chatRail.rounds.length < RAIL_MIN_ROUNDS) {
      root.hidden = true;
      root.innerHTML = "";
      chatRail.active = null;
      return;
    }

    var html = '<span class="rail-axis" aria-hidden="true"></span>' +
      '<nav class="rail-nav" aria-label="对话进度导航">';
    chatRail.rounds.forEach(function (round, index) {
      html += '<button class="rail-tick" type="button" data-rail-i="' + index + '"' +
        ' aria-label="第 ' + (index + 1) + " 轮：" + esc(round.summary || "无文字内容") + '">' +
        '<span class="rail-bar"></span>' +
        '<span class="rail-tip" role="tooltip">' +
        '<span class="rail-tip-k">第 ' + (index + 1) + " 轮</span>" +
        '<span class="rail-tip-v">' + esc(round.summary || "（无文字内容）") + "</span>" +
        "</span></button>";
    });
    root.innerHTML = html + "</nav>";
    root.hidden = false;
    chatRail.active = null;
  }

  /* 量位置。用 rect 差值而不是 offsetTop：offsetTop 是相对 offsetParent 的，
     中间只要有一层 position:relative 的包裹，算出来的偏移就整体错位。 */
  function railMeasure() {
    var log = railLog();
    var root = railRoot();
    if (!railMath || !log || !root || root.hidden || !chatRail.rounds.length) return;

    var box = log.getBoundingClientRect();
    var offsets = chatRail.rounds.map(function (round) {
      return round.node.getBoundingClientRect().top - box.top + log.scrollTop;
    });

    // 首尾各留余量：贴住轨道端点的刻度看起来像被裁掉了一半。
    var room = Math.max(0, root.clientHeight - RAIL_TICK_PAD * 2);
    var ys = railMath.layoutTicks(offsets, log.scrollHeight, room);
    var ticks = $$("[data-rail-i]", root);

    for (var i = 0; i < ticks.length; i++) {
      if (ys[i] === undefined) continue;
      ticks[i].style.top = (ys[i] + RAIL_TICK_PAD) + "px";
    }
  }

  /* 判定当前轮。
     这里不缓存任何坐标：滚动、尺寸变化、点击之后都现场读几何。
     踩过的坑是把观察回调里拿到的位置存下来当位置表——回调只在状态变化时触发，
     没进过判定带的轮次会一直停在旧坐标，高亮就再也挪不动。
     两边读同一份实时几何，才不可能给出两个互相矛盾的答案。 */
  function railRecompute() {
    var log = railLog();
    var root = railRoot();
    var rounds = chatRail.rounds;
    if (!railMath || !log || !root || root.hidden || !rounds.length) return;

    var id;
    // 贴底时最后一条可能够不到阅读线（它太短），显式认它，
    // 否则滚到底部却还高亮着前一轮。
    if (log.scrollTop + log.clientHeight >= log.scrollHeight - 8) {
      id = rounds[rounds.length - 1].id;
    } else {
      var bandTop = log.getBoundingClientRect().top + RAIL_BAND_TOP;
      id = railMath.pickActive(rounds.map(function (round) {
        return { id: round.id, top: round.node.getBoundingClientRect().top };
      }), bandTop);
    }

    if (id === chatRail.active) return;
    chatRail.active = id;

    var ticks = $$("[data-rail-i]", root);
    for (var i = 0; i < ticks.length; i++) {
      var on = rounds[i] && rounds[i].id === id;
      ticks[i].classList.toggle("active", !!on);
      if (on) ticks[i].setAttribute("aria-current", "true");
      else ticks[i].removeAttribute("aria-current");
    }
  }

  /* 跳转统一走「算差值改 scrollTop」而不是 scrollIntoView：
     后者会把整页一起带走，控制台里卡片上下还有别的块，页面一跳就找不着北。
     阅读线贴在容器顶边也是同一个理由——点哪一轮，那一轮就落在阅读线上，
     高亮与点击才讲同一个故事（否则会出现「点 6 亮 7」）。 */
  function railGoTo(index) {
    var round = chatRail.rounds[index];
    if (!round || !round.node) return;
    scrollIntoLog(round.node, 10);
    round.node.classList.remove("flash");
    void round.node.offsetWidth;  // 强制重排，连点同一轮也能重放动画
    round.node.classList.add("flash");
    setTimeout(function () { round.node.classList.remove("flash"); }, 1400);
  }

  function onRailKey(ev, index) {
    var step = ev.key === "ArrowDown" ? 1 : (ev.key === "ArrowUp" ? -1 : 0);
    if (!step) return;
    ev.preventDefault();
    var root = railRoot();
    if (!root) return;
    var ticks = $$("[data-rail-i]", root);
    var next = ticks[index + step];
    if (next) next.focus();
  }

  // 跳转统一走「算差值改 scrollTop」：scrollIntoView 会把整个页面也带着滚，
  // 用户点导航时视线正停在对话卡片上，页面一跳就找不着北了。
  function scrollIntoLog(node, extra) {
    var log = $("#chat-log");
    if (!log || !node) return;
    var delta = node.getBoundingClientRect().top - log.getBoundingClientRect().top;
    var target = Math.max(0, log.scrollTop + delta - (extra === undefined ? 12 : extra));
    if (log.scrollTo) log.scrollTo({ top: target, behavior: "smooth" });
    else log.scrollTop = target;
  }

  // 用定时器而不是 requestAnimationFrame 节流：后台标签页 / 无头环境里
  // rAF 可能整段不触发，导航就永远停在上一次的位置。
  function onLogScroll() {
    if (chatUI.scrollTimer) return;
    chatUI.scrollTimer = setTimeout(function () {
      chatUI.scrollTimer = null;
      railMeasure();
      railRecompute();
    }, 50);
  }

  function bindChatRail() {
    var root = railRoot();
    if (!root) return;

    // 事件委托挂在容器上：刻度会随轮次重建，逐条绑定会跟着失效。
    root.addEventListener("click", function (ev) {
      var tick = ev.target.closest ? ev.target.closest("[data-rail-i]") : null;
      if (!tick) return;
      railGoTo(parseInt(tick.getAttribute("data-rail-i"), 10));
    });

    root.addEventListener("keydown", function (ev) {
      var tick = ev.target.closest ? ev.target.closest("[data-rail-i]") : null;
      if (!tick) return;
      onRailKey(ev, parseInt(tick.getAttribute("data-rail-i"), 10));
    });

    var log = railLog();
    if (log) {
      log.addEventListener("scroll", onLogScroll);
      // 流式追加与重渲染都会改内容：合到一帧里同步，别每个 token 都重排。
      if (window.MutationObserver) {
        chatRail.mo = new MutationObserver(function () {
          if (chatRail.frame) return;
          chatRail.frame = setTimeout(function () {
            chatRail.frame = 0;
            railSync();
            railMeasure();
          }, 60);
        });
        chatRail.mo.observe(log, { childList: true, subtree: true, characterData: true });
      }
      // 容器尺寸变化（窗口缩放、对话历史栏开合、字体加载完）都要重量一遍。
      if (window.ResizeObserver) {
        chatRail.ro = new ResizeObserver(function () {
          railMeasure();
          railRecompute();
        });
        chatRail.ro.observe(log);
        if (root) chatRail.ro.observe(root);
      }
    }

    railSync();
  }

  /* ---- 会话内搜索 ----
   *
   * 只在已渲染的气泡里找（用户看到什么就搜什么），命中的文本包成 <mark>。
   * 不改 state.chat.messages：搜索是一次性的视图状态，改数据会让「发出去的话」
   * 与「库里的话」出现两份不同的文本。
   */

  function clearMarks() {
    var log = $("#chat-log");
    if (!log) return;
    $$("mark.hit", log).forEach(function (mark) {
      var parent = mark.parentNode;
      if (!parent) return;
      parent.replaceChild(document.createTextNode(mark.textContent), mark);
      parent.normalize();  // 相邻文本节点并回去，否则反复搜索会把节点切得越来越碎
    });
  }

  function markInNode(node, needle) {
    var text = node.nodeValue || "";
    var lower = text.toLowerCase();
    var from = 0;
    var fragment = document.createDocumentFragment();
    var hit = false;

    while (true) {
      var at = lower.indexOf(needle, from);
      if (at < 0) break;
      hit = true;
      if (at > from) fragment.appendChild(document.createTextNode(text.slice(from, at)));
      var mark = document.createElement("mark");
      mark.className = "hit";
      mark.textContent = text.slice(at, at + needle.length);
      fragment.appendChild(mark);
      from = at + needle.length;
    }

    if (!hit) return;
    if (from < text.length) fragment.appendChild(document.createTextNode(text.slice(from)));
    node.parentNode.replaceChild(fragment, node);
  }

  function applyFind() {
    var log = $("#chat-log");
    var input = $("#chat-find-input");
    if (!log) return;

    clearMarks();

    var query = chatUI.findQuery || (input ? input.value.trim() : "");
    chatUI.findQuery = query;
    chatUI.matches = [];

    if (!query) {
      updateFindCount(0, 0);
      return;
    }

    var needle = query.toLowerCase();
    // TreeWalker 取快照再改：边遍历边替换会让 walker 漏掉节点。
    var walker = document.createTreeWalker(log, NodeFilter.SHOW_TEXT, null);
    var nodes = [];
    var node;
    while ((node = walker.nextNode())) {
      if (!node.nodeValue || !node.nodeValue.trim()) continue;
      // SVG 里的 <text> 不能塞 HTML 的 <mark>，搜到也只能跳过。
      if (node.parentElement && node.parentElement.closest("svg")) continue;
      nodes.push(node);
    }

    nodes.forEach(function (n) { markInNode(n, needle); });

    chatUI.matches = $$("mark.hit", log);
    if (!chatUI.matches.length) {
      chatUI.matchIndex = -1;
      updateFindCount(0, 0);
      return;
    }

    // 命中数变了以后，尽量停在同一序号附近，而不是每次都回到第一条。
    // matchIndex < 0 表示这是新关键词的第一次标记，此时应该滚到第一条命中，
    // 否则用户敲完字只能看到个数字，画面还停在原地。
    var fresh = chatUI.matchIndex < 0;
    if (fresh || chatUI.matchIndex >= chatUI.matches.length) chatUI.matchIndex = 0;
    focusMatch(chatUI.matchIndex, fresh);
  }

  function updateFindCount(done, total) {
    var node = $("#chat-find-count");
    if (!node) return;
    if (!total) {
      node.textContent = chatUI.findQuery ? "0" : "";
      node.className = "chat-find-count" + (chatUI.findQuery ? " none" : "");
      return;
    }
    node.textContent = (done + 1) + "/" + total;
    node.className = "chat-find-count";
  }

  function focusMatch(index, scroll) {
    var marks = chatUI.matches || [];
    if (!marks.length) return;
    chatUI.matchIndex = (index + marks.length) % marks.length;

    marks.forEach(function (m, i) {
      m.classList.toggle("on", i === chatUI.matchIndex);
    });

    var mark = marks[chatUI.matchIndex];
    updateFindCount(chatUI.matchIndex, marks.length);
    if (!scroll) return;

    // 命中折叠面板里的内容时先展开，否则滚过去只看到一行标题。
    var fold = mark.closest("details");
    if (fold && !fold.open) fold.open = true;
    scrollIntoLog(mark, 60);
  }

  function stepMatch(delta) {
    if (!chatUI.matches || !chatUI.matches.length) return;
    focusMatch(chatUI.matchIndex + delta, true);
  }

  function openFind() {
    var box = $("#chat-find");
    var input = $("#chat-find-input");
    if (!box || !input) return;
    box.hidden = false;
    input.focus();
    input.select();
    if (chatUI.findQuery) applyFind();
  }

  function closeFind() {
    var box = $("#chat-find");
    var input = $("#chat-find-input");
    if (box) box.hidden = true;
    if (input) input.value = "";
    chatUI.findQuery = "";
    chatUI.matches = [];
    chatUI.matchIndex = -1;
    clearMarks();
    updateFindCount(0, 0);
  }

  // 换会话 / 新建会话时搜索必须归零：上一个会话的关键词留在框里，
  // 会让人以为新会话「什么都没搜到」，实际是搜错了地方。
  function resetFind() {
    chatUI.findQuery = "";
    chatUI.matches = [];
    chatUI.matchIndex = -1;
    var input = $("#chat-find-input");
    if (input) input.value = "";
    var count = $("#chat-find-count");
    if (count) { count.textContent = ""; count.className = "chat-find-count"; }
  }


  async function resolveApproval(id, action, index, btn) {
    var msg = state.chat.messages[index];
    if (!msg || !msg.approval) return;
    var buttons = $$("[data-idx='" + index + "']");
    buttons.forEach(function (b) { b.disabled = true; });
    btn.textContent = action === "confirm" ? "执行中…" : "取消中…";

    /* 写结论的那几句，200 与 409 共用一份：同一张单子不该因为清理任务有没有
       先跑到，就在卡上长出两种样子。 */
    function settle(text) {
      msg.approval.done = text;
      msg.approval.title = "已处理";
      msg.approval.open = false;
      msg.approval.note = "";
    }

    try {
      var r = await api("/api/v1/approvals/" + encodeURIComponent(id) + "/" + action, {
        method: "POST", body: {}
      });
      /* 卡上写结论，不写字段名。刷新之后这张卡会从批量接口还原成
         「已执行并核验通过（DELEGATION_REVOKED）」这种形状；当场确认的卡
         若显示成 `status=VERIFIED · verified=true`，同一张单子就有了两种样子。

         服务端那句话只取第一行：委派确认时它带着整段结论，塞进卡里会把卡撑破。 */
      var text = PAPApproval.outcome({ status: r.status, error_code: r.error_code });
      if (r.message) text += " · " + String(r.message).split("\n")[0];
      settle(text);
      if (r.result) {
        msg.text = msg.text + "\n\n执行结果：\n" + JSON.stringify(r.result, null, 2);
      }
      toast(action === "confirm" ? "已提交执行" : "已取消", "ok");
    } catch (e) {
      /* 409 且带着状态：单子已经作废（过期、被别处取消），这同样是结论，
         不能按「这次没提交成功」处理——那样卡片会留着按钮等人再点，
         而它点几次都只会再拿一个 409。提示仍用错误色：这次确实没写进去。 */
      var settled = PAPApproval.settledFromError(e);
      if (settled) {
        settle(settled);
        toast("未执行：" + settled, "err");
      } else {
        /* 失败不等于单子结束了：可能是网络抖了一下，也可能 409（别处已经处理过）。
           此时不能写「结论」——写了按钮就会消失，卡片只能靠刷新救回来。
           状态到底变成什么，让人再点一次或刷新，由服务端说了算。 */
        msg.approval.note = "这次没有提交成功：" + errText(e);
        toast("审批操作失败：" + errText(e), "err");
      }
    }
    renderChatLog();
  }

  /* 把历史消息里的审批号换回审批单。

     会话里存的是「这句话属于哪张单子」，卡上的参数、状态都在审批单上，
     所以刷新后必须按号再取一次。一次批量取，不按条数发请求——一条长会话
     可能攒了几十张卡，逐条打就是几十次往返。

     返回值带上 `ok`：取不到和「没有这张单子」是两件事，前者是网络问题、
     刷新就好，后者是单子被清理了、得重新发起。混成一个，用户会一直等一张
     永远不会出现的按钮。 */
  async function loadApprovals(messages) {
    var ids = [];
    messages.forEach(function (m) {
      var id = m.metadata && m.metadata.approval_id;
      if (id && ids.indexOf(id) < 0) ids.push(String(id));
    });
    if (!ids.length) return { items: {}, ok: true };

    try {
      var r = await api("/api/v1/approvals?ids=" + encodeURIComponent(ids.join(",")));
      var items = {};
      (r.items || []).forEach(function (ap) { items[ap.approval_id] = ap; });
      return { items: items, ok: true };
    } catch (e) {
      return { items: {}, ok: false };
    }
  }

  var chatUI = {
    conversations: [], loading: false, renamingId: null,
    // 一轮还在路上时挡住第二次发送：追问卡片点一下就走同一条发送路径，
    // 连点两下会发出两条一模一样的用户消息。
    sending: false,
    // 会话内搜索是视图状态，不落库、不进消息体。
    findQuery: "", matches: [], matchIndex: -1,
    // 当前会话的压缩摘要（接口按时间正序）。只用最新一条定位分隔条。
    summaries: [],
    scrollTimer: null
  };

  /* 会话历史的收起状态是纯界面偏好：存 localStorage，读不出来就当没收。
     存坏了最多是每次进来都展开，不影响任何数据。 */
  var CONV_COLLAPSED_KEY = "pap.chat.history.collapsed";

  function convCollapsed() {
    try {
      return window.localStorage.getItem(CONV_COLLAPSED_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function applyConvCollapsed(collapsed) {
    var shell = $(".chat-shell");
    if (shell) shell.classList.toggle("collapsed", collapsed);

    var btn = $("#conv-toggle");
    if (!btn) return;
    // 收起与展开用同一个图标：按钮本身是位置记忆的一部分，
    // 形状一变，人们会以为点错了地方（或去别处找那个「另一个」按钮）。
    // 状态改由 aria-expanded + 提示语承担，读屏也读得到。
    var label = collapsed ? "打开对话历史" : "收起对话历史";
    btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    btn.title = label;
    btn.setAttribute("aria-label", label);
  }

  function toggleConvCollapsed() {
    var next = !convCollapsed();
    try {
      window.localStorage.setItem(CONV_COLLAPSED_KEY, next ? "1" : "0");
    } catch (e) {
      /* 隐私模式下 localStorage 会抛异常，记住失败不影响本次收起 */
    }
    applyConvCollapsed(next);
  }

  function shortTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    var pad = function (n) { return String(n).padStart(2, "0"); };
    var now = new Date();
    var sameYear = d.getFullYear() === now.getFullYear();
    var date = (sameYear ? "" : d.getFullYear() + "-") +
      pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    return date + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function convItem(c) {
    var active = c.id === state.chat.id ? " active" : "";
    if (chatUI.renamingId === c.id) {
      return '<div class="conv-item editing" data-id="' + esc(c.id) + '">' +
        '<input type="text" class="conv-rename" id="conv-rename-input" value="' +
        esc(c.title) + '" maxlength="120">' +
        '<div class="conv-meta">回车保存 · Esc 取消</div></div>';
    }
    return '<div class="conv-item' + active + '" data-id="' + esc(c.id) + '">' +
      '<div class="conv-line">' +
      '<span class="conv-title" title="' + esc(c.title) + '">' + esc(c.title) + "</span>" +
      '<button class="icon-btn" type="button" data-act="rename" data-id="' + esc(c.id) +
      '" title="重命名">' + ico("edit") + "</button>" +
      '<button class="icon-btn danger" type="button" data-act="delete" data-id="' + esc(c.id) +
      '" title="删除">' + ico("trash") + "</button>" +
      "</div>" +
      '<div class="conv-meta">' + esc(shortTime(c.last_active_at)) + " · " +
      esc(c.message_count) + " 条</div>" +
      "</div>";
  }

  function renderConversationList() {
    var box = $("#conv-list");
    if (!box) return;

    if (chatUI.loading && !chatUI.conversations.length) {
      box.innerHTML = '<div class="skeleton" style="width:80%"></div>';
      return;
    }

    if (!chatUI.conversations.length) {
      box.innerHTML = '<div class="empty sm"><strong>还没有历史会话</strong>' +
        "在这里发第一条消息就会生成记录。</div>";
      return;
    }

    box.innerHTML = chatUI.conversations.map(convItem).join("");

    $$("[data-act]", box).forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var id = btn.getAttribute("data-id");
        if (btn.getAttribute("data-act") === "rename") {
          startRename(id);
        } else {
          askDelete(id);
        }
      });
    });

    $$(".conv-item", box).forEach(function (node) {
      node.addEventListener("click", function () {
        var id = node.getAttribute("data-id");
        if (id && id !== state.chat.id) openConversation(id);
      });
    });

    var renameInput = $("#conv-rename-input");
    if (renameInput) {
      renameInput.focus();
      renameInput.select();
      renameInput.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") { ev.preventDefault(); commitRename(); }
        if (ev.key === "Escape") { chatUI.renamingId = null; renderConversationList(); }
      });
      renameInput.addEventListener("blur", function () { commitRename(); });
    }
  }

  async function loadConversations() {
    chatUI.loading = true;
    renderConversationList();
    try {
      // 显式只要 channel=api：对话台与构建助手面板各看各的。
      // 不加这个参数时两边共用一份列表，助手线程会混进左侧栏，
      // 点开又是一串「新建智能体…」的确认记录，与这里的对话无关。
      var data = await api("/api/v1/conversations?channel=api&limit=100");
      chatUI.conversations = data.items || [];
      // 当前 thread 若已在列表里（刚发过消息），把选中项同步过去。
      if (!state.chat.id) {
        var matched = chatUI.conversations.filter(function (c) {
          return c.thread_id === state.chat.thread;
        })[0];
        if (matched) state.chat.id = matched.id;
      }
    } catch (e) {
      chatUI.conversations = [];
      var box = $("#conv-list");
      if (box) {
        box.innerHTML = '<div class="banner err" style="margin:0">会话列表加载失败：' +
          esc(errText(e)) + "</div>";
      }
      return;
    } finally {
      chatUI.loading = false;
    }
    renderConversationList();
  }

  function applyConversation(conv, messages, approvals, summaries) {
    state.chat.id = conv.id;
    state.chat.thread = conv.thread_id;
    state.chat.attachments = [];
    state.chat.messages = messages
      .filter(function (m) { return m.role === "user" || m.role === "assistant"; })
      .map(function (m) {
        var meta = [];
        if (m.metadata && m.metadata.intent) meta.push("intent=" + m.metadata.intent);
        if (m.metadata && m.metadata.tool_name) meta.push("tool=" + m.metadata.tool_name);
        return {
          // 服务端消息 id 只为一件事保留：定位压缩摘要的水位。
          // 界面上不显示，也不能拿它当渲染 key（新建的消息还没有 id）。
          id: m.id || "",
          role: m.role === "user" ? "user" : "agent",
          text: m.content,
          // 历史消息的附件只有服务端接口地址，没有本地 data URL —— 直接用它回显。
          attachments: m.role === "user" ? (m.attachments || []) : [],
          meta: meta.join(" · "),
          // 识别原文随消息入库，刷新后还能展开核对，不是只在当时那一轮可见。
          ocrText: m.role === "assistant" && m.metadata ? (m.metadata.ocr_text || null) : null,
          /* 追问选项也随消息入库（`metadata.clarify`），刷新后整张卡能原样回来。
             选了什么没有单独存：用户的选择就是紧跟着的那条用户消息，
             多存一份就多一处可能和气泡对不上的地方。 */
          clarify: m.role === "assistant" && m.metadata && m.metadata.clarify && window.PAPClarify
            ? window.PAPClarify.normalize(m.metadata.clarify)
            : null,
          // 时间取服务端记录的创建时刻（UTC → 浏览器本地时区显示）。
          at: m.created_at || null,
          // 停在审批上的那条消息，刷新后靠审批号把卡拼回来。
          // 不拼的话，那条消息读起来像结论，其实还在等人点。
          approval:
            m.role === "assistant"
              ? PAPApproval.fromMeta(m.metadata, approvals)
              : null,
          status: "ok"
        };
      });

    var chip = $("#chat-thread-chip");
    if (chip) chip.textContent = conv.thread_id;
    var mono = $("#chat-thread-mono");
    if (mono) mono.textContent = conv.thread_id;
    var title = $("#chat-current-title");
    if (title) title.textContent = conv.title;

    // 换会话必须换摘要：留着上一条的水位，分隔条会插进别人的消息里。
    chatUI.summaries = summaries || [];

    resetFind();
    renderAttachTray();
    renderChatLog();
    renderConversationList();
  }

  async function openConversation(id) {
    var log = $("#chat-log");
    if (log) {
      log.classList.remove("is-empty");
      log.innerHTML = '<div class="skeleton" style="width:70%"></div>';
    }
    try {
      var detail = await api("/api/v1/conversations/" + encodeURIComponent(id));
      var messages = detail.messages || [];
      // 审批卡要先按号取回来，再交给渲染——否则停在审批上的那段历史
      // 读起来像结论，用户会以为已经办完了。
      var approvals = await loadApprovals(messages);
      applyConversation(detail.conversation, messages, approvals, detail.summaries || []);
    } catch (e) {
      toast("读取会话失败：" + errText(e), "err");
      renderChatLog();
    }
  }

  function startRename(id) {
    chatUI.renamingId = id;
    renderConversationList();
  }

  async function commitRename() {
    var id = chatUI.renamingId;
    if (!id) return;
    var inputNode = $("#conv-rename-input");
    var value = inputNode ? inputNode.value.trim() : "";
    chatUI.renamingId = null;

    if (!value) { renderConversationList(); return; }

    try {
      await api("/api/v1/conversations/" + encodeURIComponent(id), {
        method: "PATCH", body: { title: value }
      });
      var titleNode = $("#chat-current-title");
      if (state.chat.id === id && titleNode) titleNode.textContent = value;
      toast("已重命名", "ok");
      await loadConversations();
    } catch (e) {
      toast("重命名失败：" + errText(e), "err");
      renderConversationList();
    }
  }

  async function askDelete(id) {
    var conv = chatUI.conversations.filter(function (c) { return c.id === id; })[0] || {};
    var title = conv.title || "该会话";
    var count = conv.message_count ? "，共 " + conv.message_count + " 条消息" : "";

    var confirmed = await openConfirm({
      title: "永久删除这个会话？",
      okText: "确认删除",
      body:
        '<p class="modal-lead">「' + esc(title) + "」" + esc(count) + "。删除后以下内容一并销毁，<strong>无法恢复</strong>。</p>" +
        '<ul class="modal-list">' +
        "<li><span class=\"ml-k\">消息记录</span><span class=\"ml-v\">对话原文与时间</span></li>" +
        "<li><span class=\"ml-k\">运行记录</span><span class=\"ml-v\">每轮的输入输出摘要</span></li>" +
        "<li><span class=\"ml-k\">模型对话状态</span><span class=\"ml-v\">该线程的上下文快照</span></li>" +
        "<li><span class=\"ml-k\">附件原文件</span><span class=\"ml-v\">提交的图片与 PDF</span></li>" +
        "<li><span class=\"ml-k\">会话产物</span><span class=\"ml-v\">该会话生成的文件（若有）</span></li>" +
        "</ul>" +
        '<p class="modal-note">审计日志只保留事件与业务 ID，不含对话原文，按设计不随之删除。</p>'
    });
    if (!confirmed) return;

    chatUI.renamingId = null;
    await deleteConversation(id);
  }

  async function deleteConversation(id) {
    try {
      await api("/api/v1/conversations/" + encodeURIComponent(id), { method: "DELETE" });
      if (state.chat.id === id) resetChat();
      toast("会话已删除，附件文件一并销毁", "ok");
      await loadConversations();
    } catch (e) {
      toast("删除失败：" + errText(e), "err");
      renderConversationList();
    }
  }

  function resetChat() {
    state.chat.thread = newThreadId();
    state.chat.id = null;
    state.chat.messages = [];
    state.chat.attachments = [];
    chatUI.summaries = [];

    var chip = $("#chat-thread-chip");
    if (chip) chip.textContent = state.chat.thread;
    var mono = $("#chat-thread-mono");
    if (mono) mono.textContent = state.chat.thread;
    var title = $("#chat-current-title");
    if (title) title.textContent = "新会话";

    resetFind();
    renderAttachTray();
    renderChatLog();
    renderConversationList();
  }

  function fileIsPdf(file) {
    // 有些系统给 .pdf 的 type 是空的，扩展名兜底一次；
    // 最终以服务端的内容嗅探为准，这里只决定提示文案与本地预览方式。
    return file.type === "application/pdf" || /\.pdf$/i.test(file.name || "");
  }

  function readAsAttachment(file) {
    return new Promise(function (resolve) {
      var reader = new FileReader();
      reader.onload = function () {
        var pdf = fileIsPdf(file);
        var text = !pdf && fileIsText(file);
        var dataUrl = String(reader.result || "");
        if (text) {
          // 浏览器给 `.md` 的 type 常常是空串，data URL 头会变成
          // `application/octet-stream`。这里按后缀改写成人能看懂的 MIME：
          // 服务端仍会自己判定，但日志与审计里不会再出现「一个八位字节流」。
          dataUrl = dataUrl.replace(/^data:[^;,]*/, "data:" + fileTextMime(file));
        }
        resolve({
          name: file.name || "",
          mime: file.type || (pdf ? "application/pdf" : (text ? fileTextMime(file) : "")),
          size: file.size || 0,
          kind: pdf ? "pdf" : (text ? "text" : "image"),
          dataUrl: dataUrl
        });
      };
      reader.onerror = function () { resolve(null); };
      reader.readAsDataURL(file);
    });
  }

  /* 托盘内容是主对话与构建助手共用的：两张托盘长得不一样（一个在输入框上方、
     一个在抽屉底部），但「选了什么、多大、怎么删」是同一件事。各画一遍，
     早晚一处忘了转义、一处忘了显示体积。 */
  function attachChips(items) {
    return items.map(function (att, index) {
      var thumb;
      if (isText(att)) {
        thumb = '<span class="chip-thumb text">' + ico("file") + "</span>";
      } else if (isPdf(att)) {
        thumb = '<span class="chip-thumb pdf">' + ico("file") + "</span>";
      } else {
        thumb = '<img class="chip-thumb" src="' + esc(att.dataUrl) + '" alt="">';
      }

      return '<span class="att-chip">' + thumb +
        '<span class="chip-name" title="' + esc(att.name) + '">' +
        esc(att.name || attName(att, index)) + "</span>" +
        '<span class="chip-size">' + esc(fmtBytes(att.size)) + "</span>" +
        '<button type="button" class="chip-x" data-remove="' + index +
        '" title="移除" aria-label="移除附件">' + ico("close") + "</button></span>";
    }).join("");
  }

  function paintTray(tray, items) {
    if (!tray) return;
    if (!items.length) {
      tray.hidden = true;
      tray.innerHTML = "";
      return;
    }
    tray.hidden = false;
    tray.innerHTML = attachChips(items);
  }

  function renderAttachTray() {
    var note = $("#chat-attach");
    var items = state.chat.attachments || [];

    if (note) {
      note.textContent = items.length
        ? "已选 " + items.length + " 个 · 合计 " +
          fmtBytes(items.reduce(function (sum, a) { return sum + (a.size || 0); }, 0))
        : "";
      note.className = items.length ? "attach" : "attach muted";
    }

    paintTray($("#attach-tray"), items);
  }

  function removeAttachment(index) {
    if (!(index >= 0) || index >= state.chat.attachments.length) return;
    state.chat.attachments.splice(index, 1);
    renderAttachTray();
  }

  /* 追加附件：同一个会话可以反复点 + 继续加，不覆盖已选的。
     逐条校验后只把通过的读成 data URL，被拒的给出具体原因。

     `chosen` 与 `done` 让构建助手复用同一份校验与渲染：上限值必须与后端一致，
     只在主对话里写一遍、另一处凭印象再写一遍，必然出现「这边能选、那边发不出」。 */
  function addAttachments(files, chosen, done, opts) {
    chosen = chosen || state.chat.attachments;
    done = done || renderAttachTray;
    // 只有构建助手收 Markdown / 文本：它是「这一问的材料」，读完即删。
    // 对话台的附件是长期材料，读取链路也只对图片 / PDF 做了展示，别在这里放开。
    var allowText = !!(opts && opts.text);
    // 拒绝文案要跟着实际能收的类型走：构建助手说「只支持图片或 PDF」时，
    // 用户手里的 .md 明明能传，却被同一句话劝退了。
    var only = allowText
      ? "只支持图片、PDF 或文本文件（Markdown / 日志 / 配置）"
      : "只支持图片或 PDF";
    /* 提示往哪里去由调用方决定：对话台用浮层，构建助手落进对话流。
       一个函数里写死 toast，会让构建助手的报错在三秒后消失——而它正是
       用户回头核对「刚才那份文件为什么没进去」时唯一要看的东西。 */
    var notify = (opts && opts.notice) || toast;
    var total = chosen.reduce(function (sum, a) { return sum + (a.size || 0); }, 0);
    var accepted = [];
    var problems = [];

    files.forEach(function (file) {
      var pdf = fileIsPdf(file);
      var text = !pdf && fileIsText(file);
      if (text && !allowText) {
        problems.push((file.name || "该文件") + "：" + only);
        return;
      }
      if (!pdf && !text && !/^image\//.test(file.type || "")) {
        problems.push((file.name || "该文件") + "：" + only);
        return;
      }
      if (chosen.length + accepted.length >= ATTACH_LIMIT_COUNT) {
        problems.push("单条消息最多 " + ATTACH_LIMIT_COUNT + " 个附件");
        return;
      }
      var limit = text
        ? ATTACH_LIMIT_TEXT
        : (pdf ? ATTACH_LIMIT_PDF : ATTACH_LIMIT_IMAGE);
      if (file.size > limit) {
        problems.push((file.name || "该文件") + "：超过 " + fmtBytes(limit) + " 上限");
        return;
      }
      if (total + file.size > ATTACH_LIMIT_TOTAL) {
        problems.push("附件合计超过 " + fmtBytes(ATTACH_LIMIT_TOTAL) + " 上限");
        return;
      }
      total += file.size;
      accepted.push(file);
    });

    if (!accepted.length) {
      if (problems.length) notify(problems[0], "err");
      return;
    }

    Promise.all(accepted.map(readAsAttachment)).then(function (loaded) {
      loaded.forEach(function (att) {
        if (att && att.dataUrl) chosen.push(att);
      });
      done();
      if (problems.length) notify(problems[0], "err");
    });
  }

  /* 智能体选择器：只有一个可用项时整块隐藏。
     选项只从「已启用」里出——草稿和已停用的选中会被服务端 409 拒掉，
     与其让人选完再报错，不如根本不给选。

     内置的构建助手不在列表里：它已经是右下角的常驻面板，把它放进下拉
     只会多一个「从对话台打开的助手」，会话却仍然记在 studio 通道上，
     两边都找不到自己刚说过什么。 */
  async function loadChatAgents() {
    var sel = $("#chat-agent");
    var wrap = $("#chat-agent-wrap");
    if (!sel || !wrap) return;

    try {
      var d = await api("/api/v1/agents");
      var usable = (d.items || []).filter(function (item) {
        return item.usable && !item.is_builtin;
      });

      if (usable.length <= 1) {
        wrap.hidden = true;
        state.chat.agent = "";
        return;
      }

      var remembered = "";
      try { remembered = window.localStorage.getItem("pap.chat.agent") || ""; } catch (e) { remembered = ""; }

      sel.innerHTML = usable.map(function (item) {
        return '<option value="' + esc(item.name) + '">' +
          esc(item.display_name || item.name) + "</option>";
      }).join("");

      var known = usable.filter(function (item) { return item.name === remembered; }).length > 0;
      sel.value = known ? remembered : usable[0].name;
      state.chat.agent = sel.value;
      wrap.hidden = false;

      sel.addEventListener("change", function () {
        state.chat.agent = sel.value;
        try { window.localStorage.setItem("pap.chat.agent", sel.value); } catch (e) { /* 隐私模式下写不了，忽略 */ }
        // 换人等于换上下文：把上一轮的问答留在新智能体名下会误导它接手
        // 不是自己产生的结论，所以直接开一个新会话。
        if (state.chat.messages.length) {
          resetChat();
          toast("已切换智能体，并开启新会话");
        }
      });
    } catch (e) {
      // 读不到就退回「不指定」：等效平台默认，不影响对话本身。
      wrap.hidden = true;
      state.chat.agent = "";
    }
  }

  function mountChat(root) {
    // 这一页不再有页头：一屏里最该给的是对话本身，标题与说明占了 90px 却每看一次都读。
    // 「新会话」是操作不是标题，挪进对话卡片头，跟着当前会话走。
    root.innerHTML =
      '<div class="chat-shell">' +
      '<aside class="card conv-card" id="conv-card">' +
      // 这一栏不再自带开关：收起后整栏不参与布局，栏里的按钮会跟着消失，
      // 留在这里等于「收起来就找不到入口」。开关统一放在对话头（见下）。
      '<div class="card-head"><div class="card-title">' + ico("message") +
      '<span class="conv-head-label">会话历史</span></div></div>' +
      '<p class="card-sub">只显示当前身份自己的会话；删除会销毁消息与附件，不可恢复。</p>' +
      '<div class="conv-list" id="conv-list"><div class="skeleton" style="width:80%"></div></div>' +
      "</aside>" +
      '<div class="card chat-card">' +
      '<div class="chat-head"><span class="chat-head-title" id="chat-current-title">新会话</span>' +
      '<span class="faint mono" id="chat-thread-mono" style="font-size:10.5px">' +
      esc(state.chat.thread) + "</span>" +
      '<span class="chat-head-gap"></span>' +
      // 搜索框展开在图标左侧：输入框挤出宽度，图标位置不动，
      // 连点两下不会因为按钮跑位点空。
      '<div class="chat-find" id="chat-find" hidden>' +
      '<input type="search" id="chat-find-input" placeholder="搜索本会话…" autocomplete="off">' +
      '<span class="chat-find-count" id="chat-find-count"></span>' +
      '<button class="icon-btn" type="button" id="chat-find-prev" title="上一个（Shift+Enter）">' +
      ico("up") + "</button>" +
      '<button class="icon-btn" type="button" id="chat-find-next" title="下一个（Enter）">' +
      ico("down") + "</button>" +
      '<button class="icon-btn" type="button" id="chat-find-close" title="关闭搜索（Esc）">' +
      ico("close") + "</button></div>" +
      // 对话历史开关，放在控制栏最左（DeepSeek 那个位置）。
      // 它只能长在对话头上：收起后整栏不参与布局，栏内不会有任何按钮留下来，
      // 缺了这个常驻入口，收起就等于关死。
      '<button class="icon-btn" type="button" id="conv-toggle" aria-expanded="true" ' +
      'aria-controls="conv-card" title="收起对话历史" aria-label="收起对话历史">' +
      ico("panel") + "</button>" +
      '<button class="icon-btn" type="button" id="chat-find-toggle" ' +
      'title="搜索本会话" aria-label="搜索本会话">' + ico("search") + "</button>" +
      '<button class="btn sm" type="button" id="chat-new">' + ico("plus") + "新会话</button>" +
      "</div>" +
      // 消息区与刻度导航同在一个容器里：导航占右侧一条固定宽度的窄条，
      // 不叠在消息上——否则它会压住滚动条，滚动条变成点不动的装饰。
      '<div class="chat-body">' +
      '<div class="chat-log" id="chat-log"></div>' +
      '<div class="chat-rail" id="chat-rail" hidden></div>' +
      "</div>" +
      '<div class="composer">' +
      '<div class="attach-tray" id="attach-tray" hidden></div>' +
      '<textarea id="chat-input" rows="2" placeholder="例如：查一下 station-001 最近一天的压力趋势"></textarea>' +
      '<div class="composer-bar">' +
      '<label class="add-btn" for="chat-file" id="chat-add" title="添加图片或 PDF（可多选）" ' +
      'role="button" tabindex="0" aria-label="添加附件">' + ico("plus") + "</label>" +
      '<input type="file" id="chat-file" accept="' + ATTACH_ACCEPT + '" multiple hidden>' +
      '<span class="attach muted" id="chat-attach"></span>' +
      // 智能体选择器默认隐藏：只有一个可用时不值得占位置，
      // 多选一才有意义（否则每个租户都会看到一个永远只有一项的下拉）。
      '<label class="agent-pick" id="chat-agent-wrap" title="以哪个智能体回答" hidden>' +
      ico("flow") + '<select id="chat-agent" aria-label="选择智能体"></select></label>' +
      '<span style="flex:1"></span>' +
      '<span class="faint" style="font-size:11px">Ctrl / ⌘ + Enter 发送</span>' +
      '<button class="btn primary" type="button" id="chat-send">发送</button>' +
      "</div></div></div>" +
      "</div>";

    var input = $("#chat-input");
    var file = $("#chat-file");

    // 事件委托绑在容器上：气泡是 innerHTML 重建的，逐条绑定会随重渲染失效。
    $("#chat-log").addEventListener("click", function (ev) {
      var thumb = ev.target.closest ? ev.target.closest("[data-lightbox-src]") : null;
      if (!thumb) return;

      // 带修饰键时保持浏览器原生行为（新标签 / 新窗口打开原图）
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button !== 0) return;

      ev.preventDefault();
      lightboxFromThumb(thumb);
    });

    file.addEventListener("change", function () {
      // 先把 files 拷贝出来：清空 input.value 会让 FileList 失效。
      var picked = Array.prototype.slice.call(file.files || []);
      file.value = "";
      if (picked.length) addAttachments(picked);
    });

    // 点 + 键也能触发（label 的 for 已覆盖鼠标点击，这里补键盘可达性）。
    $("#chat-add").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        file.click();
      }
    });

    // 托盘里的删除按钮是动态渲染的，用事件委托，避免每次重建后重新绑定。
    $("#attach-tray").addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-remove]");
      if (!btn) return;
      removeAttachment(parseInt(btn.getAttribute("data-remove"), 10));
    });

    $("#chat-new").addEventListener("click", resetChat);
    $("#conv-toggle").addEventListener("click", toggleConvCollapsed);
    // 刷新按钮去掉了：发消息、重命名、删除、切页都会重新拉列表，
    // 手动刷新在这个页面上换不来任何新信息，那个位置让给折叠更有用。
    applyConvCollapsed(convCollapsed());
    loadChatAgents();

    $("#chat-send").addEventListener("click", sendChat);
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
        ev.preventDefault();
        sendChat();
      }
    });

    bindChatFind();
    bindChatRail();

    renderAttachTray();
    renderChatLog();
    loadConversations();
  }

  function bindChatFind() {
    var toggle = $("#chat-find-toggle");
    var box = $("#chat-find");
    var field = $("#chat-find-input");
    if (!toggle || !box || !field) return;

    toggle.addEventListener("click", function () {
      if (box.hidden) openFind();
      else closeFind();
    });

    $("#chat-find-close").addEventListener("click", closeFind);
    $("#chat-find-prev").addEventListener("click", function () { stepMatch(-1); });
    $("#chat-find-next").addEventListener("click", function () { stepMatch(1); });

    // 每敲一个字就全量标一次代价不大（消息量在几十条的量级），
    // 但连续输入会把光标位置抖没，所以压到输入停下来再标。
    var timer = null;
    field.addEventListener("input", function () {
      chatUI.findQuery = field.value.trim();
      chatUI.matchIndex = -1;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        applyFind();
      }, 120);
    });

    field.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        // 输入防抖还没跑完就回车时，先把标记补上再跳。
        if (!chatUI.matches || !chatUI.matches.length) applyFind();
        else stepMatch(ev.shiftKey ? -1 : 1);
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        closeFind();
        var composer = $("#chat-input");
        if (composer) composer.focus();
      }
    });
  }

  async function sendChat() {
    var input = $("#chat-input");
    if (!input || chatUI.sending) return;
    var text = input.value.trim();
    var outgoing = (state.chat.attachments || []).slice();
    if (!text && !outgoing.length) return;
    // 只发附件不给话时补一句中性指令，避免后端 min_length=1 直接 422 把附件丢掉。
    if (!text) text = "请看附件。";
    input.value = "";
    await deliverChat(text, outgoing);
  }

  /* 真正把一条消息发出去的那一段。

     单独抽出来是因为入口有两个：输入框回车，和追问卡片上的点选。
     两边必须走同一条路——分头写就会一边清了附件托盘、一边没清，
     也会一边处理 rejected 回填、一边漏掉。 */
  async function deliverChat(text, outgoing) {
    if (chatUI.sending) return;
    chatUI.sending = true;
    outgoing = outgoing || [];

    // 时间先用本机时钟占位，响应回来立刻换成服务端落库时刻：
    // 客户端时钟不准时，刚发的气泡才不会和刷新后的历史显示成两个时间。
    var mine = { role: "user", text: text, attachments: outgoing.slice(), at: new Date().toISOString() };
    state.chat.messages.push(mine);
    var placeholder = { role: "agent", text: "思考中…", at: null };
    state.chat.messages.push(placeholder);
    renderChatLog();

    var sendBtn = $("#chat-send");
    if (sendBtn) sendBtn.disabled = true;

    var payload = { thread_id: state.chat.thread, message: text };
    // 不选就不传：省略 agent 等于平台基线，和改造前的行为逐字一致。
    if (state.chat.agent) payload.agent = state.chat.agent;
    if (outgoing.length) {
      payload.attachments = outgoing.map(function (att) {
        return { data_url: att.dataUrl, name: att.name || null };
      });
    }

    // 先清空托盘：附件已随消息发出，留在输入区会让用户以为还没发出去。
    state.chat.attachments = [];
    renderAttachTray();

    try {
      var r = await api("/api/v1/chat", { method: "POST", body: payload });
      placeholder.text = r.answer || "（无文本回答）";
      // 服务端时间优先；审批待办那类没有 assistant 消息，退回本机时刻。
      if (r.user_message_at) mine.at = r.user_message_at;
      placeholder.at = r.assistant_message_at || new Date().toISOString();
      placeholder.ocrText = r.ocr_text || null;
      placeholder.citations = r.citations || [];
      var meta = [];
      if (r.metadata && r.metadata.intent) meta.push("intent=" + r.metadata.intent);
      if (r.metadata && r.metadata.tool_name) meta.push("tool=" + r.metadata.tool_name);
      if (r.status && r.status !== "completed") meta.push("status=" + r.status);
      if (r.error_category) meta.push("error=" + r.error_category);
      placeholder.meta = meta.join(" · ");
      placeholder.status = r.status === "error" ? "error" : "ok";
      if (r.approval) placeholder.approval = r.approval;
      // 追问卡片：这一轮若是反问，就地带选项，用户不用自己组织一句话。
      if (r.metadata && r.metadata.clarify && window.PAPClarify) {
        var ask = window.PAPClarify.normalize(r.metadata.clarify);
        if (ask.length) placeholder.clarify = ask;
      }
      // 服务端拒收（格式/体积/数量）时附件并没有入库，放回托盘让用户改完直接重发，
      // 不用再从磁盘里一个个挑回来。
      if (r.status === "rejected" && outgoing.length && !state.chat.attachments.length) {
        state.chat.attachments = outgoing;
        renderAttachTray();
      }
    } catch (e) {
      placeholder.text = "请求失败：" + errText(e);
      placeholder.status = "error";
      // 请求没成功就没有服务端消息，时间用本机时刻。
      placeholder.at = new Date().toISOString();
      // 请求没成功就把附件放回托盘，重试时不用再逐个重新选。
      if (outgoing.length && !state.chat.attachments.length) {
        state.chat.attachments = outgoing;
        renderAttachTray();
      }
    } finally {
      chatUI.sending = false;
      if (sendBtn) sendBtn.disabled = false;
      renderChatLog();
      // 标题取自第一条消息、活跃时间也会变，发送后同步一次列表。
      loadConversations();
    }
  }

  /* ================= 工具 ================= */

  var KIND_LABEL = { http: "HTTP", mcp: "MCP", builtin: "内置", python: "Python", sandbox: "沙箱", script: "脚本" };
  var SOURCE_LABEL = { builtin: "内置", mcp: "MCP", custom: "自定义" };
  var RISK_LABEL = {
    read: "读取", normal: "普通", write: "写入",
    execute: "执行", destructive: "危险", external: "外部"
  };
  var LOC_LABEL = { path: "Path", query: "Query", body: "Body", header: "Header" };
  var TYPE_LABEL = { string: "字符串", integer: "整数", number: "数字", boolean: "布尔" };
  var METHOD_OPTIONS = ["GET", "POST", "PUT", "PATCH", "DELETE"];

  // 参考图的三分法。这三类正好覆盖后端当前全部 kind（http / mcp / python），
  // 不需要「其他」兜底标签。
  var TOOL_TABS = [
    { kind: "http", label: "HTTP Tool" },
    { kind: "mcp", label: "MCP Tool" },
    { kind: "python", label: "Python Script" }
  ];

  // 页面级 UI 状态。刷新时重建，切换标签只改 kind / mode。
  var toolUI = {
    kind: "http",
    selected: null,
    mode: "detail",          // detail | create | edit | discover
    custom: [],
    mcp: [],                 // 管理员接入的 MCP 工具（配置面，可删）
    customError: null,
    importNote: null,
    importTools: null        // 供「覆盖同名」二次提交
  };

  function toolsOfKind(kind) {
    return (state.tools || []).filter(function (t) { return t.kind === kind; });
  }

  // 已停用的自定义工具不在 /api/v1/tools 里（网关已注销），但必须还能在
  // 列表里找到并重新启用，否则「停用」就变成了单向操作。
  // 同一条理由适用于 MCP：远端临时不可达时接入项仍在库里，网关里却没有它，
  // 不补进来就再也删不掉了。
  function listItems(kind) {
    var items = toolsOfKind(kind).slice();

    if (kind === "http" && state.admin) {
      (toolUI.custom || []).forEach(function (row) {
        if (row.enabled) return;
        if (items.some(function (t) { return t.name === row.name; })) return;
        items.push({
          name: row.name,
          display_name: row.display_name,
          description: row.description,
          kind: "http",
          source: "custom",
          risk_level: row.risk_level,
          timeout_seconds: row.timeout_seconds,
          enabled: false,
          requires_approval: row.requires_approval,
          required_scopes: [],
          allowed_roles: [],
          input_schema: row.input_schema || { type: "object", properties: {} }
        });
      });
    }

    if (kind === "mcp" && state.admin) {
      (toolUI.mcp || []).forEach(function (row) {
        if (items.some(function (t) { return t.name === row.local_name; })) return;
        items.push({
          name: row.local_name,
          display_name: row.display_name,
          description: row.description,
          kind: "mcp",
          source: "mcp",
          risk_level: row.risk_level,
          timeout_seconds: row.timeout_seconds,
          enabled: row.enabled !== false,
          requires_approval: row.requires_approval,
          required_scopes: [],
          allowed_roles: [],
          input_schema: { type: "object", properties: {} }
        });
      });
    }

    return items;
  }

  function customRow(name) {
    return (toolUI.custom || []).filter(function (r) { return r.name === name; })[0] || null;
  }

  // MCP 的接入项是独立一张表（mcp_registered_tools），不在 /admin/tools 里。
  // 详情页要靠它判断「这一项是配置接入的（可删）还是代码白名单里的（只读）」。
  function mcpRow(name) {
    return (toolUI.mcp || []).filter(function (r) { return r.local_name === name; })[0] || null;
  }

  function toolByName(name) {
    var live = (state.tools || []).filter(function (t) { return t.name === name; })[0];
    if (live) return live;
    return listItems(toolUI.kind).filter(function (t) { return t.name === name; })[0] || null;
  }

  function riskChip(risk) {
    return '<span class="chip">' + esc(RISK_LABEL[risk] || risk || "—") + "</span>";
  }

  function sourceChip(source) {
    var cls = source === "custom" ? " ok" : (source === "mcp" ? "" : "");
    return '<span class="chip' + cls + '">' + esc(SOURCE_LABEL[source] || source || "内置") + "</span>";
  }

  /* ---- 左：工具列表 ---- */

  function toolRow(t) {
    var active = t.name === toolUI.selected ? " active" : "";
    var chips = [];
    if (t.enabled === false) chips.push('<span class="chip err">停用</span>');
    if (t.requires_approval) chips.push('<span class="chip warn">审批</span>');
    if (t.source === "custom") chips.push('<span class="chip">自定义</span>');
    return '<button class="tool-row' + active + '" type="button" data-name="' + esc(t.name) + '">' +
      '<div class="tool-row-main">' +
      '<div class="tool-row-name">' + esc(t.display_name) + "</div>" +
      '<div class="tool-row-id">' + esc(t.name) + "</div>" +
      "</div>" +
      '<div class="tool-row-chips">' + chips.join("") + "</div>" +
      "</button>";
  }

  function toolListCard() {
    var kind = toolUI.kind;
    var items = listItems(kind);
    var customCount = items.filter(function (t) { return t.source === "custom"; }).length;

    var subtitle =
      kind === "http"
        ? "自定义工具可在右侧新建、编辑、停用；内置 HTTP 工具由代码注册，只读。"
        : kind === "mcp"
          ? "已接入白名单、模型真正能调用的那一批。远端还有别的工具也不会出现在这里。"
          : "Python 脚本工具由服务端代码注册，页面只展示配置与 Schema。";

    var body = items.length
      ? '<div class="tool-list">' + items.map(toolRow).join("") + "</div>"
      : '<div class="empty sm"><strong>这一类还没有工具</strong>' +
        (kind === "http"
          ? "点右上角「新建」添加一个 HTTP Tool。"
          : kind === "mcp"
            ? "右侧先发现远端 tools，再在白名单登记要暴露给 Agent 的那一个。"
            : "Python 工具由服务端代码注册，控制台不提供新建。") + "</div>";

    var foot = kind === "mcp"
      ? '<button class="btn sm" type="button" id="mcp-open-discover">' + ico("search") +
        "发现远端 tools</button>"
      : "";

    return '<div class="card tool-list-card">' +
      '<div class="card-head"><h3 class="card-title">' + ico("wrench") +
      esc(KIND_LABEL[kind] || kind) + " Tool 列表</h3>" +
      '<span class="chip">' + items.length + " 个" +
      (customCount ? " · 自定义 " + customCount : "") + "</span></div>" +
      '<p class="card-sub">' + esc(subtitle) + "</p>" +
      body +
      (foot ? '<div class="tool-list-foot">' + foot + "</div>" : "") +
      "</div>";
  }

  /* ---- 右：详情 ---- */

  function paramTable(params) {
    if (!params || !params.length) {
      return '<div class="disc-result" style="text-align:left">' +
        "<strong>没有参数</strong>该工具接受空参数对象，调用时不需要额外输入。</div>";
    }
    return '<table class="param-table"><thead><tr>' +
      "<th>名称</th><th>位置</th><th>类型</th><th>必填</th><th>说明</th>" +
      "</tr></thead><tbody>" +
      params.map(function (p) {
        return "<tr><td class=\"mono\">" + esc(p.name) + "</td>" +
          "<td>" + esc(LOC_LABEL[p.location] || p.location || "—") + "</td>" +
          "<td>" + esc(TYPE_LABEL[p.type] || p.type) + "</td>" +
          "<td>" + (p.required ? "是" : "否") + "</td>" +
          "<td class=\"muted\">" + esc(p.description || "—") + "</td></tr>";
      }).join("") +
      "</tbody></table>";
  }

  function kvRows(pairs) {
    return '<dl class="kv">' + pairs.map(function (pair) {
      return "<dt>" + esc(pair[0]) + "</dt><dd>" + pair[1] + "</dd>";
    }).join("") + "</dl>";
  }

  function emptyDetail() {
    return '<div class="card"><div class="empty">' +
      "<strong>选择左侧任一工具查看配置</strong>" +
      "右侧展示来源、风险等级、超时、参数定义与对模型暴露的 JSON Schema。" +
      "</div></div>";
  }

  function detailCard(t, custom, mcp) {
    var isCustom = t.source === "custom";
    var isManagedMcp = t.source === "mcp" && !!mcp;
    // 远端不可达时工具不在 /api/v1/tools 里，描述只剩接入时填的那份。
    var desc = t.description || (mcp && mcp.description) || "";
    var chips = [
      sourceChip(t.source),
      riskChip(t.risk_level),
      '<span class="chip ' + (t.requires_approval ? "warn" : "ok") + '">' +
      (t.requires_approval ? "需审批" : "只读直通") + "</span>",
      '<span class="chip ' + (t.enabled === false ? "err" : "ok") + '">' +
      (t.enabled === false ? "已停用" : "已启用") + "</span>"
    ];

    // 只有「配置面」的工具能在页面上删：自定义 HTTP Tool 与人接入的 MCP Tool。
    // 代码登记的内置 / Python / MCP 白名单删除等于改代码，给个按钮只会骗人。
    var actionButtons = "";
    if (isCustom && custom) {
      actionButtons =
        '<button class="btn sm" type="button" data-tool-act="edit">' + ico("edit") + "编辑</button>" +
        '<button class="btn sm" type="button" data-tool-act="toggle">' +
        ico("activity") + (custom.enabled ? "停用" : "启用") + "</button>" +
        '<button class="btn sm danger" type="button" data-tool-act="delete">' +
        ico("trash") + "删除</button>";
    } else if (isManagedMcp && state.admin) {
      actionButtons =
        '<button class="btn sm danger" type="button" data-tool-act="delete-mcp">' +
        ico("trash") + "删除</button>";
    }

    var pairs = [
      ["工具名", '<span class="mono">' + esc(t.name) + "</span>"],
      ["来源", esc(SOURCE_LABEL[t.source] || t.source || "内置")],
      ["类型", esc(KIND_LABEL[t.kind] || t.kind)],
      ["风险等级", esc(RISK_LABEL[t.risk_level] || t.risk_level)],
      ["超时", esc(t.timeout_seconds || 20) + " 秒"],
      ["Scope", esc((t.required_scopes || []).join(", ") || "—")],
      ["角色限制", esc((t.allowed_roles || []).join(", ") || "不限")]
    ];

    if (isCustom && custom) {
      pairs.splice(1, 0, ["Method", '<span class="mono">' + esc(custom.method) + "</span>"]);
      pairs.splice(2, 0, ["URL", '<span class="mono">' + esc(custom.url) + "</span>"]);
      pairs.push(["注册状态", custom.registered
        ? '<span class="chip ok">网关已注册</span>'
        : '<span class="chip err">未注册</span>']);
      if (custom.updated_at) pairs.push(["更新时间", esc(fmtTime(custom.updated_at))]);
      if (custom.created_by) pairs.push(["创建人", esc(custom.created_by)]);
    }

    if (isManagedMcp) {
      pairs.splice(1, 0, ["远端工具名", '<span class="mono">' + esc(mcp.remote_name) + "</span>"]);
      if (mcp.server_url) {
        pairs.push(["接入端点", '<span class="mono">' + esc(mcp.server_url) + "</span>"]);
      }
      // 令牌只回「有没有」，与后端一致：明文永远不出库。
      pairs.push(["端点令牌", esc(mcp.token_set ? "已配置（仅该端点）" : "未配置")]);
      pairs.push(["注册状态", mcp.registered
        ? '<span class="chip ok">网关已注册</span>'
        : '<span class="chip err">未注册（远端可能不可达）</span>']);
      if (mcp.created_at) pairs.push(["接入时间", esc(fmtTime(mcp.created_at))]);
      if (mcp.created_by) pairs.push(["接入人", esc(mcp.created_by)]);
    }

    var headersBlock = "";
    if (isCustom && custom && custom.headers && Object.keys(custom.headers).length) {
      headersBlock = '<h4 class="detail-h4">静态请求头</h4>' + kvRows(
        Object.keys(custom.headers).map(function (k) {
          return [k, '<span class="mono">' + esc(custom.headers[k]) + "</span>"];
        })
      );
    }

    var noteForBuiltin = "";
    if (!isCustom && !isManagedMcp) {
      noteForBuiltin = '<p class="hint">该工具由服务端代码注册' +
        (t.source === "mcp" ? "（远端 MCP 白名单）" : "") +
        "，页面只读：要下线只能改代码里的登记再重新部署，控制台不提供删除。</p>";
    } else if (isManagedMcp && !state.admin) {
      noteForBuiltin = '<p class="hint">接入项由平台管理员管理；删除需要平台管理员身份。</p>';
    }

    return '<div class="card tool-detail-card">' +
      '<div class="card-head"><h3 class="card-title">' + ico("sliders") +
      esc(t.display_name) + "</h3>" +
      '<div class="actions">' + actionButtons + "</div></div>" +
      '<div class="chips-row">' + chips.join("") + "</div>" +
      '<p class="card-sub">' + esc(desc || "（无描述）") + "</p>" +
      kvRows(pairs) +
      headersBlock +
      '<h4 class="detail-h4">工具参数</h4>' +
      paramTable(isCustom && custom ? custom.params : schemaToParams(t.input_schema)) +
      '<h4 class="detail-h4">对模型暴露的 JSON Schema</h4>' +
      '<pre class="code">' + esc(JSON.stringify(t.input_schema || {}, null, 2)) + "</pre>" +
      (t.verification_hint
        ? '<p class="hint">核验说明：' + esc(t.verification_hint) + "</p>" : "") +
      noteForBuiltin +
      "</div>";
  }

  // 内置工具没有 params 数组，只能从 JSON Schema 反推展示。
  // 位置（path/query/body）在 schema 里没有信息，宁可显示「—」也不猜，
  // 猜错会让人以为参数该放查询串，实际放错了更麻烦。
  function schemaToParams(schema) {
    var props = (schema && schema.properties) || {};
    var required = (schema && schema.required) || [];
    return Object.keys(props).map(function (name) {
      var item = props[name] || {};
      return {
        name: name,
        location: "",
        type: item.type || "string",
        required: required.indexOf(name) >= 0,
        description: item.description || ""
      };
    });
  }

  /* ---- 右：新建 / 编辑表单 ---- */

  function paramRowHtml(p) {
    return '<div class="param-row" data-param>' +
      '<input type="text" class="p-name" placeholder="参数名" value="' + esc(p.name || "") + '">' +
      '<select class="p-loc">' + Object.keys(LOC_LABEL).map(function (k) {
        return '<option value="' + k + '"' + (p.location === k ? " selected" : "") + ">" +
          LOC_LABEL[k] + "</option>";
      }).join("") + "</select>" +
      '<select class="p-type">' + Object.keys(TYPE_LABEL).map(function (k) {
        return '<option value="' + k + '"' + (p.type === k ? " selected" : "") + ">" +
          TYPE_LABEL[k] + "</option>";
      }).join("") + "</select>" +
      '<label class="p-req"><input type="checkbox"' + (p.required ? " checked" : "") +
      "> 必填</label>" +
      '<input type="text" class="p-desc" placeholder="说明（给模型看）" value="' +
      esc(p.description || "") + '">' +
      '<button class="icon-btn danger" type="button" data-remove-param title="移除">' +
      ico("trash") + "</button>" +
      "</div>";
  }

  function formCard(editingRow) {
    var editing = !!editingRow;
    var data = editing ? {
      name: editingRow.name,
      display_name: editingRow.display_name,
      description: editingRow.description,
      method: editingRow.method,
      risk_level: editingRow.risk_level,
      timeout_seconds: editingRow.timeout_seconds,
      url: editingRow.url,
      headers: editingRow.headers || {},
      params: editingRow.params || [],
      enabled: editingRow.enabled
    } : {
      name: "", display_name: "", description: "", method: "GET",
      risk_level: "read", timeout_seconds: 20, url: "", headers: {},
      params: [], enabled: true
    };

    return '<div class="card tool-form-card">' +
      '<div class="card-head"><h3 class="card-title">' + ico(editing ? "edit" : "plus") +
      (editing ? "编辑 HTTP Tool" : "新建 HTTP Tool") + "</h3>" +
      '<button class="icon-btn" type="button" data-tool-act="cancel" title="返回">' +
      ico("back") + "</button></div>" +
      '<p class="card-sub">' + (editing
        ? "工具名不可修改：它已经写进审计与审批记录。保存后立即生效，无需重启。"
        : "保存前必须确认授权目标 URL。保存后立即注册，模型下一次规划即可调用。") + "</p>" +

      '<div class="form-grid">' +
      '<div class="field"><label>Tool Name</label>' +
      '<input type="text" id="tf-name" placeholder="work_order_query" value="' + esc(data.name) + '"' +
      (editing ? " readonly" : "") + ">" +
      '<span class="help">小写字母开头，只用字母、数字、下划线。</span></div>' +

      '<div class="field"><label>展示名称</label>' +
      '<input type="text" id="tf-display" placeholder="工单查询" value="' +
      esc(data.display_name) + '"></div>' +

      '<div class="field"><label>Method</label><select id="tf-method">' +
      METHOD_OPTIONS.map(function (m) {
        return '<option value="' + m + '"' + (data.method === m ? " selected" : "") + ">" + m +
          "</option>";
      }).join("") + "</select></div>" +

      '<div class="field"><label>风险等级</label><select id="tf-risk">' +
      Object.keys(RISK_LABEL).map(function (k) {
        return '<option value="' + k + '"' + (data.risk_level === k ? " selected" : "") + ">" +
          RISK_LABEL[k] + "</option>";
      }).join("") + "</select>" +
      '<span class="help">写入及以上会强制走审批。</span></div>' +

      '<div class="field span2"><label>URL</label>' +
      '<input type="text" id="tf-url" placeholder="https://api.example.com/work-orders" value="' +
      esc(data.url) + '">' +
      '<span class="help">只支持 http / https；不跟随重定向。路径占位符写成 {id} 或 :id。</span></div>' +

      '<div class="field"><label>描述</label>' +
      '<input type="text" id="tf-desc" placeholder="查询工单状态。" value="' +
      esc(data.description) + '"></div>' +

      '<div class="field"><label>超时时间（秒）</label>' +
      '<input type="number" id="tf-timeout" min="1" max="120" value="' +
      esc(data.timeout_seconds) + '"></div>' +
      "</div>" +

      '<div class="param-head"><h4 class="detail-h4" style="margin:0">工具参数</h4>' +
      '<div class="actions">' +
      '<button class="btn sm" type="button" id="tf-add-param">' + ico("plus") + "添加参数</button>" +
      "</div></div>" +
      '<p class="hint" style="margin:4px 0 10px">Path 替换 URL 中的同名占位符；' +
      "Query / Body 按接口定义填写；Header 用于非敏感静态头。</p>" +
      '<div class="param-list" id="tf-params">' +
      ((data.params || []).length
        ? data.params.map(paramRowHtml).join("")
        : '<div class="param-empty" id="tf-param-empty">' +
          "<strong>暂无参数</strong>如果这个接口不需要参数，可以保持为空；" +
          "需要搜索词、ID、路径等输入时请添加参数。</div>") +
      "</div>" +

      '<button class="btn sm" type="button" id="tf-parse-url">' + ico("code") +
      "从 URL 解析</button>" +

      '<details class="curl-box"><summary>粘贴 curl 解析</summary>' +
      '<textarea id="tf-curl" rows="3" placeholder="curl -X POST https://api.example.com/orders -H \'Content-Type: application/json\' -d \'{"id":"1"}\'"></textarea>' +
      '<div class="actions" style="margin-top:8px">' +
      '<button class="btn sm" type="button" id="tf-parse-curl">解析并填入</button>' +
      '<span class="faint" style="font-size:11px">不会执行命令；含凭据的参数会被拒绝。</span>' +
      "</div></details>" +

      '<div class="actions" style="margin-top:14px">' +
      '<button class="btn primary" type="button" id="tf-save">' +
      ico(editing ? "edit" : "plus") + (editing ? "保存修改" : "添加 HTTP Tool") + "</button>" +
      '<button class="btn" type="button" data-tool-act="cancel">取消</button>' +
      '<span class="faint" style="font-size:11.5px" id="tf-msg"></span>' +
      "</div></div>";
  }

  /* ---- 右：MCP 发现 ---- */

  function mcpDiscoverCard() {
    if (!state.admin) {
      return '<div class="card">' +
        '<div class="card-head"><h3 class="card-title">' + ico("search") + "发现 MCP Tool</h3></div>" +
        '<p class="card-sub">先发现远端 tools；未接入的可以就地填本地名与风险等级后接入，接入即进入模型目录。</p>' +
        '<div class="empty" style="margin-top:14px"><strong>需要平台管理员</strong>' +
        "发现会拿令牌去连远端服务，只对平台管理员开放。</div>" +
        "</div>";
    }

    return '<div class="card">' +
      '<div class="card-head"><h3 class="card-title">' + ico("search") + "发现 MCP Tool</h3>" +
      '<span class="chip" id="mcp-runtime-chip">读取中</span></div>' +
      '<p class="card-sub">先发现远端 tools；未接入的可以就地填本地名与风险等级后接入，接入即进入模型目录。</p>' +

      '<div class="field"><label>Transport</label>' +
      '<select id="mcp-transport"><option value="streamable-http">Remote Streamable HTTP</option></select>' +
      '<span class="help">只支持 Streamable HTTP：平台不托管 stdio 子进程。</span></div>' +

      '<div class="field" style="margin-top:12px"><label>Server URL</label>' +
      '<input type="text" id="mcp-url" placeholder="https://mcp.example.com/mcp"></div>' +

      '<div class="disc-2col">' +
      '<div class="field"><label>Bearer Token</label>' +
      '<input type="text" id="mcp-token" placeholder="留空则用当前配置的令牌"></div>' +
      '<div class="field"><label>超时时间（秒）</label>' +
      '<input type="number" id="mcp-timeout" min="1" max="120" value="15"></div>' +
      "</div>" +

      '<label class="check"><input type="checkbox" id="mcp-override">' +
      "<span>允许运行时请求覆盖此 Token；未传时仍使用当前配置的静态 Token。</span></label>" +

      '<div class="disc-result" id="mcp-result">' +
      "<strong>等待发现 tools</strong>确认 Transport 和连接信息后，点击「发现 tools」。</div>" +

      '<button class="btn primary" type="button" id="mcp-discover">' +
      ico("search") + "发现 tools</button>" +
      "</div>";
  }

  function renderDiscoverResult(r, box) {
    box.className = "disc-result";
    if (!r.tools || !r.tools.length) {
      box.innerHTML = "<strong>远端没有返回工具</strong>" + esc(r.message || "");
      return;
    }
    box.innerHTML = '<div class="disc-sum">' + esc(r.message) +
      (r.server_url
        ? '<span class="disc-endpoint mono" title="本次发现的端点">' + esc(r.server_url) + "</span>"
        : "") +
      "</div>" +
      '<div class="disc-list">' + r.tools.map(function (t) {
        // 「已接入」要带上本地工具名：同名远端工具在模型目录里叫的是另一个名字，
        // 只标一个绿点没法让人对着工具页去找。
        var badge = t.local_name
          ? '<span class="chip ok">已接入 · ' + esc(t.local_name) + "</span>"
          : '<span class="chip">未接入</span>';

        // 未接入 → 接入；管理员接入的 → 可取消接入；
        // 代码白名单里的不给按钮：那些只能改代码下线。
        var action = "";
        if (!t.local_name) {
          action = '<button class="btn sm" type="button" data-attach="' + esc(t.name) + '">' +
            ico("plus") + "接入</button>";
        } else if (t.removable) {
          action = '<button class="btn sm danger" type="button" data-detach="' +
            esc(t.local_name) + '">取消接入</button>';
        }

        return '<div class="disc-tool" data-tool="' + esc(t.name) + '" data-desc="' +
          esc(t.description || "") + '">' +
          '<div class="disc-tool-head"><span class="mono">' + esc(t.name) + "</span>" + badge +
          '<span class="disc-gap"></span>' + action + "</div>" +
          (t.description ? '<div class="disc-desc">' + esc(t.description) + "</div>" : "") +
          "</div>";
      }).join("") + "</div>";
  }

  /* 远端名多半是 camelCase（getServerStatus），本地名统一成小写下划线。
     不擅自加 prv_ 之类的前缀：命名是管理员的判断，这里只保证格式合法。 */
  function mcpSuggestLocalName(remote) {
    var name = String(remote || "")
      .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
      .replace(/[^A-Za-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .toLowerCase();

    if (!/^[a-z]/.test(name)) name = "mcp_" + name;
    return name.slice(0, 64);
  }

  function mcpAttachForm(toolName, description) {
    return '<div class="disc-form" data-form="' + esc(toolName) + '">' +
      '<div class="form-grid">' +
      '<div class="field"><label>本地工具名</label>' +
      '<input type="text" data-f="local_name" maxlength="64" value="' +
      esc(mcpSuggestLocalName(toolName)) + '">' +
      '<span class="help">模型看到并调用的名字。小写字母开头，只用字母、数字、下划线。</span></div>' +

      '<div class="field"><label>展示名称</label>' +
      '<input type="text" data-f="display_name" maxlength="128" value="' + esc(toolName) + '">' +
      '<span class="help">用中文写清楚它做什么，会显示在工具页与审批卡片上。</span></div>' +

      '<div class="field span2"><label>说明</label>' +
      '<input type="text" data-f="description" maxlength="2000" value="' + esc(description || "") + '">' +
      '<span class="help">模型据这句话判断什么时候该调它。远端给的是英文时建议改写。</span></div>' +

      '<div class="field"><label>风险等级</label><select data-f="risk_level">' +
      Object.keys(RISK_LABEL).map(function (k) {
        return '<option value="' + k + '">' + RISK_LABEL[k] + "</option>";
      }).join("") + "</select>" +
      '<span class="help">写入及以上强制走审批，没有免审批开关。</span></div>' +

      '<div class="field"><label>超时（秒）</label>' +
      '<input type="number" data-f="timeout_seconds" min="1" max="300" value="60"></div>' +
      "</div>" +

      '<div class="actions" style="margin-top:10px">' +
      '<button class="btn primary sm" type="button" data-attach-ok>确认接入</button>' +
      '<button class="btn sm" type="button" data-attach-cancel>取消</button>' +
      '<span class="faint" style="font-size:11px">接入后立即注册，模型下一次规划即可调用。</span>' +
      "</div></div>";
  }

  async function submitMcpAttach(row, form) {
    var read = function (key) {
      var node = form.querySelector('[data-f="' + key + '"]');
      return node ? String(node.value || "").trim() : "";
    };

    var payload = {
      remote_name: row.getAttribute("data-tool"),
      local_name: read("local_name"),
      display_name: read("display_name"),
      description: read("description"),
      risk_level: read("risk_level") || "read",
      timeout_seconds: parseInt(read("timeout_seconds"), 10) || 60,
      // 与发现用同一个端点与令牌：接入校验必须发生在将来真正调用的服务上。
      server_url: mcpLastDiscover.endpoint || null,
      token: mcpLastDiscover.token || null
    };

    if (!/^[a-z][a-z0-9_]{1,63}$/.test(payload.local_name)) {
      toast("本地工具名不合法：小写字母开头，只用字母、数字、下划线", "err");
      return;
    }
    if (!payload.display_name) {
      toast("展示名称不能为空", "err");
      return;
    }

    var btn = form.querySelector("[data-attach-ok]");
    if (btn) btn.disabled = true;

    try {
      await api("/api/v1/admin/mcp/tools", { method: "POST", body: payload });
      toast(
        "已接入 " + payload.local_name +
          (mcpLastDiscover.endpoint ? "（" + mcpLastDiscover.endpoint + "）" : "") +
          "，立即可用",
        "ok"
      );
      // 顺序不能反：refreshTools 会重渲染整个右栏（发现结果被清掉），
      // 先刷状态再重新发现，用户看到的才是刷新后的结果。
      await refreshTools(payload.local_name);
      await runMcpDiscover();
    } catch (e) {
      toast("接入失败：" + errText(e), "err");
      if (btn) btn.disabled = false;
    }
  }

  async function detachMcpTool(localName) {
    // 发现面板里只有本地名；接入项里有展示名与端点，删之前先取全，
    // 确认框才说得清「删的是哪一条」。取不到也照删，不能因为这一条就卡住。
    var row = mcpRow(localName) ||
      { local_name: localName, display_name: localName, description: "" };
    var removed = await removeMcpTool(row);
    if (removed) await runMcpDiscover();
  }

  async function loadMcpRuntime() {
    var chip = $("#mcp-runtime-chip");
    if (!chip) return;
    try {
      var cfg = await api("/api/v1/admin/mcp");
      chip.className = "chip " + (cfg.enabled ? "ok" : "warn");
      chip.textContent = cfg.enabled ? "已启用" : "未启用";
      var urlInput = $("#mcp-url");
      if (urlInput && cfg.server_url) urlInput.value = cfg.server_url;
      var timeout = $("#mcp-timeout");
      if (timeout && cfg.connect_timeout_seconds) {
        timeout.value = Math.round(cfg.connect_timeout_seconds);
      }
      var card = chip.closest(".card");
      var help = card && card.querySelector(".card-sub");
      if (help && cfg.registered_count) {
        help.textContent =
          "表单里的地址是这次发现的端点；接入时会连同端点一起存库，模型调用走的是那个端点。" +
          (cfg.enabled
            ? "已登记 " + cfg.registered_count + " 个 MCP 工具（含代码白名单）。"
            : "登记了 " + cfg.registered_count + " 个，但 MCP 通道未启用，本次一个都没注册。");
      }
    } catch (e) {
      chip.className = "chip warn";
      chip.textContent = "读取失败";
    }
  }

  /* 上一次「发现」用的端点与令牌。接入必须复用它们：
     照 A 地址发现、拿 B 地址校验，报出来是「远端没有这个工具」，
     工具其实在，只是查错了服务。令牌只留在内存里，不落任何持久化。 */
  var mcpLastDiscover = { endpoint: "", token: "" };

  async function runMcpDiscover() {
    var btn = $("#mcp-discover");
    var box = $("#mcp-result");
    if (!btn || !box) return;

    var url = ($("#mcp-url").value || "").trim();
    var token = ($("#mcp-token").value || "").trim();
    var effectiveToken = $("#mcp-override").checked ? (token || null) : null;
    var timeout = parseInt($("#mcp-timeout").value, 10);
    if (!isFinite(timeout) || timeout < 1) timeout = 15;
    timeout = Math.min(120, timeout);

    btn.disabled = true;
    box.className = "disc-result";
    box.innerHTML = '<span class="faint">正在连接远端…</span>';
    try {
      var result = await api("/api/v1/admin/mcp/discover", {
        method: "POST",
        body: {
          url: url || null,
          // 没勾「允许覆盖」就坚持用服务端已配置的令牌，
          // 免得输入框里残留的旧值悄悄生效。
          token: effectiveToken,
          timeout_seconds: timeout
        }
      });
      // 以后端解析出的地址为准：表单留空时它是 .env 里的默认端点，
      // 前端自己拼不出来。
      mcpLastDiscover = {
        endpoint: result.server_url || url || "",
        token: effectiveToken || ""
      };
      renderDiscoverResult(result, box);
    } catch (e) {
      box.className = "disc-result err";
      box.innerHTML = "<strong>发现失败</strong>" + esc(errText(e));
    } finally {
      btn.disabled = false;
    }
  }

  /* ---- 表单读写 ---- */

  function collectParams() {
    var rows = $$("[data-param]", $("#tf-params"));
    var out = [];
    rows.forEach(function (row) {
      var name = row.querySelector(".p-name").value.trim();
      if (!name) return;
      out.push({
        name: name,
        location: row.querySelector(".p-loc").value,
        type: row.querySelector(".p-type").value,
        required: row.querySelector(".p-req input").checked,
        description: row.querySelector(".p-desc").value.trim()
      });
    });
    return out;
  }

  function readForm() {
    return {
      name: ($("#tf-name").value || "").trim(),
      display_name: ($("#tf-display").value || "").trim(),
      description: ($("#tf-desc").value || "").trim(),
      method: $("#tf-method").value,
      risk_level: $("#tf-risk").value,
      timeout_seconds: parseInt($("#tf-timeout").value, 10) || 20,
      url: ($("#tf-url").value || "").trim(),
      headers: {},
      params: collectParams(),
      enabled: true
    };
  }

  function addParamRow(p) {
    var box = $("#tf-params");
    if (!box) return;
    var empty = $("#tf-param-empty");
    if (empty) empty.remove();
    box.insertAdjacentHTML("beforeend", paramRowHtml(p || {
      name: "", location: "query", type: "string", required: true, description: ""
    }));
    bindParamRows();
  }

  function bindParamRows() {
    $$("[data-remove-param]", $("#tf-params")).forEach(function (btn) {
      if (btn.getAttribute("data-bound")) return;
      btn.setAttribute("data-bound", "1");
      btn.addEventListener("click", function () {
        var row = btn.closest("[data-param]");
        if (row) row.remove();
        if (!$("[data-param]", $("#tf-params")) && $("#tf-params")) {
          $("#tf-params").innerHTML = '<div class="param-empty" id="tf-param-empty">' +
            "<strong>暂无参数</strong>如果这个接口不需要参数，可以保持为空；" +
            "需要搜索词、ID、路径等输入时请添加参数。</div>";
        }
      });
    });
  }

  async function saveTool(editingRow) {
    var msg = $("#tf-msg");
    var payload;
    try {
      payload = readForm();
    } catch (e) {
      toast("表单读取失败：" + errText(e), "err");
      return;
    }

    if (!payload.name || !/^[a-z][a-z0-9_]{1,63}$/.test(payload.name)) {
      setFormMsg(msg, "工具名需小写字母开头，只含字母、数字、下划线。", true);
      return;
    }
    if (!payload.display_name) {
      setFormMsg(msg, "展示名称不能为空。", true);
      return;
    }
    if (!payload.url) {
      setFormMsg(msg, "URL 不能为空。", true);
      return;
    }

    var btn = $("#tf-save");
    if (btn) btn.disabled = true;
    setFormMsg(msg, "保存中…", false);

    try {
      var saved;
      if (editingRow) {
        delete payload.name;
        saved = await api("/api/v1/admin/tools/" + editingRow.id, {
          method: "PUT", body: payload
        });
      } else {
        saved = await api("/api/v1/admin/tools", { method: "POST", body: payload });
      }
      toast(editingRow ? "已保存修改" : "已添加工具 " + saved.name, "ok");
      toolUI.selected = saved.name;
      toolUI.mode = "detail";
      await refreshTools();
    } catch (e) {
      setFormMsg(msg, errText(e), true);
      if (btn) btn.disabled = false;
    }
  }

  function setFormMsg(node, text, isError) {
    if (!node) return;
    node.textContent = text;
    node.className = isError ? "form-msg err" : "form-msg";
  }

  async function toggleTool(row) {
    try {
      await api("/api/v1/admin/tools/" + row.id, {
        method: "PUT", body: { enabled: !row.enabled }
      });
      toast(row.enabled ? "已停用，模型不再看到它" : "已启用", "ok");
      await refreshTools(row.name);
    } catch (e) {
      toast("操作失败：" + errText(e), "err");
    }
  }

  async function deleteTool(row) {
    var confirmed = await openConfirm({
      title: "删除这个工具？",
      okText: "删除",
      body:
        '<p class="modal-lead">「' + esc(row.display_name || row.name) +
        "」会立刻从模型目录里移除，模型不再能看到或调用它。</p>" +
        '<p class="modal-note">本地配置随之删除；审计与审批记录按设计保留。</p>'
    });
    if (!confirmed) return;

    try {
      await api("/api/v1/admin/tools/" + row.id, { method: "DELETE" });
      toast("已删除 " + row.name, "ok");
      toolUI.selected = null;
      toolUI.mode = "detail";
      await refreshTools();
    } catch (e) {
      toast("删除失败：" + errText(e), "err");
    }
  }

  /* 删除一条 MCP 接入项（不是删远端工具：远端服务完全不受影响）。
     fromDetail 决定删完停在哪儿：详情页里删的回到「发现」，发现面板里删的
     留在原地重新发现，两条路径共用同一个确认与错误处理。 */
  async function removeMcpTool(mcp, opts) {
    opts = opts || {};
    var label = mcp.display_name || mcp.local_name;
    var confirmed = await openConfirm({
      title: "删除这项接入？",
      okText: "删除",
      body:
        '<p class="modal-lead">「' + esc(label) + "」（" +
        '<span class="mono">' + esc(mcp.local_name) + "</span>）会从模型目录里移除，"
        + "模型不再能看到或调用它。</p>" +
        '<p class="modal-note">只是本地接入项，远端 MCP 服务上的工具不受影响，随时可以重新接入。</p>'
    });
    if (!confirmed) return false;

    try {
      await api("/api/v1/admin/mcp/tools/" + encodeURIComponent(mcp.local_name),
        { method: "DELETE" });
      toast("已删除 " + mcp.local_name, "ok");
      if (opts.fromDetail) {
        toolUI.selected = null;
        // MCP 页的空白态就是「发现」，删完正好落在能重新接入的地方。
        toolUI.mode = toolUI.kind === "mcp" ? "discover" : "detail";
      }
      await refreshTools();
      return true;
    } catch (e) {
      toast("删除失败：" + errText(e), "err");
      return false;
    }
  }

  /* ---- 导入 / 导出 ---- */

  async function exportTools() {
    if (!state.admin) { toast("导出需要平台管理员", "err"); return; }
    try {
      var data = await api("/api/v1/admin/tools/export");
      if (!data.tools || !data.tools.length) {
        toast("还没有自定义工具可导出", "err");
        return;
      }
      var stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
      downloadJson("pap-tools-" + stamp + ".json", data);
      toast("已导出 " + data.tools.length + " 个工具", "ok");
    } catch (e) {
      toast("导出失败：" + errText(e), "err");
    }
  }

  function downloadJson(filename, payload) {
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function pickImportFile() {
    if (!state.admin) { toast("导入需要平台管理员", "err"); return; }
    var input = $("#tool-import-file");
    if (input) input.click();
  }

  async function doImport(file, mode, tools) {
    var payloadTools = tools;
    if (!payloadTools) {
      var text = await readFileText(file);
      var parsed;
      try {
        parsed = JSON.parse(text);
      } catch (e) {
        toast("文件不是合法 JSON", "err");
        return;
      }
      // 兼容导出文件（{tools:[...]}）与裸数组两种形状。
      payloadTools = Array.isArray(parsed) ? parsed : (parsed.tools || []);
    }

    if (!payloadTools.length) {
      toast("文件里没有工具定义", "err");
      return;
    }

    try {
      var res = await api("/api/v1/admin/tools/import", {
        method: "POST", body: { tools: payloadTools, mode: mode || "merge" }
      });
      toolUI.importNote = res;
      toolUI.importTools = payloadTools;
      toast("导入完成：新建 " + res.created + "，更新 " + res.updated +
        "，跳过 " + res.skipped, "ok");
      await refreshTools();
    } catch (e) {
      toast("导入失败：" + errText(e), "err");
    }
  }

  function readFileText(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(String(reader.result || "")); };
      reader.onerror = function () { reject(new Error("读取文件失败")); };
      reader.readAsText(file);
    });
  }

  function importNoteHtml() {
    var note = toolUI.importNote;
    if (!note) return "";
    var skipped = (note.items || []).filter(function (i) { return i.action === "skipped"; });
    return '<div class="banner import-note">' +
      "<strong>导入结果</strong>新建 " + esc(note.created) + " · 更新 " + esc(note.updated) +
      " · 跳过 " + esc(note.skipped) +
      (skipped.length
        ? '<div class="import-detail">' + skipped.map(function (i) {
          return '<span class="mono">' + esc(i.name) + "</span> " + esc(i.message);
        }).join("<br>") + "</div>"
        : "") +
      (skipped.length
        ? '<div class="actions" style="margin-top:8px">' +
          '<button class="btn sm" type="button" id="import-overwrite">覆盖同名工具并重新导入</button>' +
          '<button class="btn sm" type="button" id="import-dismiss">知道了</button></div>'
        : "") +
      "</div>";
  }

  /* ---- 组装 ---- */

  function syncCreateButton() {
    var btn = $("#tools-create");
    if (!btn) return;
    // 只有 HTTP Tool 支持空白新建：MCP 是「接入远端已有工具」，
    // Python 由代码注册。按钮摆在那里、点了才报错，只会让人以为功能坏了。
    btn.hidden = toolUI.kind !== "http";
  }

  function renderToolBody() {
    var list = $("#tool-list");
    var detail = $("#tool-detail");
    if (!list || !detail) return;

    syncCreateButton();
    list.innerHTML = toolListCard();

    var active = toolUI.selected ? toolByName(toolUI.selected) : null;
    var custom = active ? customRow(active.name) : null;
    var reg = active ? mcpRow(active.name) : null;

    if (toolUI.mode === "create") {
      detail.innerHTML = formCard(null);
    } else if (toolUI.mode === "edit" && custom) {
      detail.innerHTML = formCard(custom);
    } else if (toolUI.mode === "discover" && toolUI.kind === "mcp") {
      detail.innerHTML = mcpDiscoverCard();
    } else if (active) {
      detail.innerHTML = detailCard(active, custom, reg);
    } else {
      detail.innerHTML = emptyDetail();
    }

    var note = $("#tool-import-note");
    if (note) note.innerHTML = importNoteHtml();

    bindToolBody();
  }

  function bindToolBody() {
    $$(".tool-row").forEach(function (row) {
      row.addEventListener("click", function () {
        toolUI.selected = row.getAttribute("data-name");
        toolUI.mode = "detail";
        renderToolBody();
      });
    });

    var discoverBtn = $("#mcp-open-discover");
    if (discoverBtn) {
      discoverBtn.addEventListener("click", function () {
        toolUI.mode = "discover";
        renderToolBody();
      });
    }

    if (toolUI.mode === "discover" && toolUI.kind === "mcp") {
      loadMcpRuntime();
      var btn = $("#mcp-discover");
      if (btn) btn.addEventListener("click", runMcpDiscover);

      // 结果区每次发现都会重渲染，事件必须委托在容器上。
      var result = $("#mcp-result");
      if (result) {
        result.addEventListener("click", function (ev) {
          var attachBtn = ev.target.closest("[data-attach]");
          if (attachBtn) {
            var row = attachBtn.closest(".disc-tool");
            if (!row) return;

            // 同时只展开一个表单：开两个很容易把 A 行的改动填进 B 行。
            $$(".disc-form", result).forEach(function (node) { node.remove(); });

            var holder = document.createElement("div");
            holder.innerHTML = mcpAttachForm(
              row.getAttribute("data-tool"),
              row.getAttribute("data-desc") || ""
            );
            row.appendChild(holder.firstChild);

            var input = row.querySelector('[data-f="local_name"]');
            if (input) { input.focus(); input.select(); }
            return;
          }

          if (ev.target.closest("[data-attach-ok]")) {
            var form = ev.target.closest(".disc-form");
            var host = ev.target.closest(".disc-tool");
            if (form && host) submitMcpAttach(host, form);
            return;
          }

          if (ev.target.closest("[data-attach-cancel]")) {
            var opened = ev.target.closest(".disc-form");
            if (opened) opened.remove();
            return;
          }

          var detachBtn = ev.target.closest("[data-detach]");
          if (detachBtn) detachMcpTool(detachBtn.getAttribute("data-detach"));
        });
      }
    }

    $$("[data-tool-act]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var act = btn.getAttribute("data-tool-act");
        var active = toolUI.selected ? toolByName(toolUI.selected) : null;
        var custom = active ? customRow(active.name) : null;
        var reg = active ? mcpRow(active.name) : null;

        if (act === "edit" && custom) {
          toolUI.mode = "edit";
          renderToolBody();
        } else if (act === "toggle" && custom) {
          toggleTool(custom);
        } else if (act === "delete" && custom) {
          deleteTool(custom);
        } else if (act === "delete-mcp" && reg) {
          removeMcpTool(reg, { fromDetail: true });
        } else if (act === "cancel") {
          toolUI.mode = "detail";
          renderToolBody();
        }
      });
    });

    if (toolUI.mode === "create" || toolUI.mode === "edit") {
      var addBtn = $("#tf-add-param");
      if (addBtn) addBtn.addEventListener("click", function () { addParamRow(); });

      var parseUrlBtn = $("#tf-parse-url");
      if (parseUrlBtn) parseUrlBtn.addEventListener("click", parseUrlIntoForm);

      var parseCurlBtn = $("#tf-parse-curl");
      if (parseCurlBtn) parseCurlBtn.addEventListener("click", parseCurlIntoForm);

      var saveBtn = $("#tf-save");
      var editing = toolUI.mode === "edit" && toolUI.selected
        ? customRow(toolUI.selected) : null;
      if (saveBtn) {
        saveBtn.addEventListener("click", function () { saveTool(editing); });
      }

      bindParamRows();
    }

    var overwriteBtn = $("#import-overwrite");
    if (overwriteBtn) {
      overwriteBtn.addEventListener("click", function () {
        doImport(null, "overwrite", toolUI.importTools);
      });
    }
    var dismissBtn = $("#import-dismiss");
    if (dismissBtn) {
      dismissBtn.addEventListener("click", function () {
        toolUI.importNote = null;
        refreshTools();
      });
    }
  }

  async function parseUrlIntoForm() {
    var url = ($("#tf-url").value || "").trim();
    if (!url) { toast("先填 URL", "err"); return; }
    try {
      var res = await api("/api/v1/admin/tools/parse-url", {
        method: "POST", body: { url: url }
      });
      $("#tf-url").value = res.url;
      mergeParams(res.params);
      toast("解析出 " + res.params.length + " 个路径参数", "ok");
    } catch (e) {
      toast("解析失败：" + errText(e), "err");
    }
  }

  async function parseCurlIntoForm() {
    var curl = ($("#tf-curl").value || "").trim();
    if (!curl) { toast("先粘贴 curl 命令", "err"); return; }
    try {
      var res = await api("/api/v1/admin/tools/parse-curl", {
        method: "POST", body: { curl: curl }
      });
      $("#tf-method").value = res.method;
      $("#tf-url").value = res.url;
      mergeParams(res.params);
      var hint = res.dropped_headers && res.dropped_headers.length
        ? "；已忽略凭据头：" + res.dropped_headers.join("、")
        : "";
      toast("已填入 " + res.method + " 与 " + res.params.length + " 个参数" + hint, "ok");
      if (res.dropped_headers && res.dropped_headers.length) {
        setFormMsg($("#tf-msg"),
          "凭据头不会写入配置，请改用租户工具策略的 credential_ref。" + hint, true);
      }
    } catch (e) {
      toast("解析失败：" + errText(e), "err");
    }
  }

  function mergeParams(params) {
    var existing = collectParams();
    var names = {};
    existing.forEach(function (p) { names[p.name] = true; });

    params.forEach(function (p) {
      if (names[p.name]) return;
      names[p.name] = true;
      addParamRow(p);
    });
  }

  // 详情页的按钮要按「这一项是配置接入的，还是代码登记的」来给，
  // 所以两份管理员数据必须一起取：/admin/tools 是自定义 HTTP，
  // /admin/mcp/tools 是 MCP 接入项。任意一份失败都只降级自己那一类。
  async function loadAdminRows() {
    if (!state.admin) return;

    try {
      var data = await api("/api/v1/admin/tools");
      toolUI.custom = data.items || [];
      toolUI.customError = null;
    } catch (e) {
      toolUI.custom = [];
      toolUI.customError = errText(e);
    }

    try {
      var reg = await api("/api/v1/admin/mcp/tools");
      toolUI.mcp = reg.items || [];
    } catch (e) {
      toolUI.mcp = [];
    }
  }

  async function refreshTools(selectName) {
    try {
      state.tools = await api("/api/v1/tools");
    } catch (e) {
      toast("工具列表加载失败：" + errText(e), "err");
      return;
    }

    await loadAdminRows();

    if (selectName !== undefined) toolUI.selected = selectName;
    renderToolBody();
  }

  async function mountTools(root) {
    root.innerHTML = pageHead({
      eyebrow: PAGES.tools.eyebrow,
      title: PAGES.tools.title,
      desc: PAGES.tools.desc,
      actions:
        '<button class="btn" type="button" id="tools-refresh">' + ico("refresh") + "刷新</button>" +
        '<button class="btn" type="button" id="tools-export">' + ico("download") + "导出</button>" +
        '<button class="btn" type="button" id="tools-import">' + ico("upload") + "导入</button>" +
        '<button class="btn primary" type="button" id="tools-create">' + ico("plus") + "新建</button>"
    }) +
      '<input type="file" id="tool-import-file" accept="application/json,.json" hidden>' +
      '<div class="tabs" id="tool-tabs"></div>' +
      '<div id="tool-import-note"></div>' +
      '<div class="tool-layout">' +
      '<div id="tool-list"><div class="card"><div class="skeleton" style="width:60%"></div></div></div>' +
      '<div id="tool-detail"><div class="card"><div class="skeleton" style="width:70%"></div></div></div>' +
      "</div>";

    toolUI.kind = "http";
    toolUI.mode = "detail";
    toolUI.selected = null;

    try {
      state.tools = await api("/api/v1/tools");
    } catch (e) {
      $("#tool-list").innerHTML = '<div class="card"><div class="banner err">加载失败：' +
        esc(errText(e)) + "</div></div>";
      return;
    }

    await loadAdminRows();

    var tabs = TOOL_TABS.filter(function (t) {
      return t.kind === "mcp" || toolsOfKind(t.kind).length > 0;
    });

    $("#tool-tabs").innerHTML = tabs.map(function (t) {
      return '<button class="tab' + (t.kind === toolUI.kind ? " active" : "") +
        '" type="button" data-kind="' + esc(t.kind) + '">' + esc(t.label) + "</button>";
    }).join("");

    $$(".tab", $("#tool-tabs")).forEach(function (btn) {
      btn.addEventListener("click", function () {
        toolUI.kind = btn.getAttribute("data-kind");
        toolUI.selected = null;
        toolUI.mode = toolUI.kind === "mcp" ? "discover" : "detail";
        $$(".tab", $("#tool-tabs")).forEach(function (b) {
          b.classList.toggle("active", b === btn);
        });
        refreshTools(null);
      });
    });

    $("#tools-refresh").addEventListener("click", function () { mountTools(root); });
    $("#tools-export").addEventListener("click", exportTools);
    $("#tools-import").addEventListener("click", pickImportFile);
    $("#tool-import-file").addEventListener("change", function () {
      var file = this.files && this.files[0];
      if (file) doImport(file, "merge", null);
      this.value = "";
    });
    $("#tools-create").addEventListener("click", function () {
      if (toolUI.kind !== "http") {
        toast("只有 HTTP Tool 支持新建；已切到 HTTP Tool", "err");
        toolUI.kind = "http";
        $$(".tab", $("#tool-tabs")).forEach(function (b) {
          b.classList.toggle("active", b.getAttribute("data-kind") === "http");
        });
      }
      toolUI.mode = "create";
      renderToolBody();
    });

    renderToolBody();
  }

  /* ================= 知识库 ================= */

  // 文档列表一次取这么多。取得比接口上限（500）小是有意的：表格是整表渲染，
  // 几百行会让展开切片变卡；而筛选只在这一批里做，所以真到上限要提示（见 renderDocs）。
  var KB_DOC_LIMIT = 200;

  // 文档列表的三种状态各有各的说法：ready 是能检索，failed 是原文没了或解析
  // 失败（重建过但没成功），deleted 是下架（列表默认不显示，靠开关才看得到）。
  function kbStateChip(status) {
    var map = {
      ready: ["ok", "已就绪"],
      failed: ["err", "失败"],
      deleted: ["", "已下架"],
      pending: ["warn", "待处理"],
      processing: ["warn", "处理中"]
    };
    var item = map[status] || ["", status || "—"];
    return '<span class="chip ' + item[0] + '">' + esc(item[1]) + "</span>";
  }

  function kbScopeChip(scope) {
    var tone = scope === "confidential" ? "warn" : (scope === "public" ? "ok" : "");
    return '<span class="chip ' + tone + '">' + esc(scope || "—") + "</span>";
  }

  function kbChunkBox(chunk) {
    var body = String(chunk.content || "");
    // 切片可以到 800 字符，整段铺开会把展开行撑得很长；先给 600 字，
    // 剩下的留给「看原文」用（详情页的完整内容就在下一行）。
    var preview = body.length > 600 ? body.slice(0, 600) + "……" : body;
    return '<div class="kb-chunk" data-chunk-index="' + esc(String(chunk.chunk_index)) + '">' +
      '<div class="kb-chunk-head"><span class="chip">#' + esc(String(chunk.chunk_index + 1)) + "</span>" +
      '<span class="muted">' + esc(chunk.section || "") + " · " + esc(String(body.length)) + " 字符</span>" +
      (chunk.is_active ? "" : '<span class="chip warn">已停用</span>') +
      "</div>" +
      '<pre class="kb-chunk-body">' + esc(preview) + "</pre>" +
      "</div>";
  }

  function kbDocRow(item, canWrite) {
    var ops = "";
    if (canWrite) {
      ops = '<button class="linkish" type="button" data-act="reindex" data-id="' + esc(item.id) + '"' +
        (item.reindexable && item.status !== "deleted" ? "" : " disabled") + ">重建</button>" +
        '<button class="linkish danger" type="button" data-act="delete" data-id="' + esc(item.id) + '"' +
        (item.status === "deleted" ? " disabled" : "") + ">下架</button>";
    }

    // 失败原因必须露出来：只显示「失败」等于让人去翻日志才知道为什么。
    var reason = item.status === "failed" && item.error_message
      ? '<span class="kb-err">' + esc(item.error_message) + "</span>"
      : "";

    return '<tr data-id="' + esc(item.id) + '" class="clickable">' +
      '<td><div class="kb-title"><span>' + esc(item.title) + "</span>" +
      '<span class="kb-sub mono">' + esc(item.filename || item.source_uri) + "</span>" +
      reason + "</div></td>" +
      "<td>" + (item.source_type === "upload" ? '<span class="chip">上传</span>' : '<span class="chip">目录</span>') + "</td>" +
      "<td>" + kbScopeChip(item.access_scope) + "</td>" +
      '<td class="right">' + esc(String(item.chunk_count)) + "</td>" +
      '<td class="right mono">' + esc(item.size_bytes ? fmtBytes(item.size_bytes) : "—") + "</td>" +
      "<td>" + kbStateChip(item.status) + "</td>" +
      '<td class="mono">' + esc(fmtTime(item.updated_at)) + "</td>" +
      // 没有写权限时整列都不出：留一个全是空白的「操作」列，只会让人以为按钮加载失败。
      (canWrite ? '<td class="right"><div class="kb-ops">' + ops + "</div></td>" : "") +
      "</tr>";
  }

  function kbDocsTable(items, canWrite, query) {
    if (!items.length) {
      // 两种「空」不是一回事：没有文档要去上传/同步，筛不到是关键词不对。
      // 用同一句「还没有文档」会让人以为文档丢了。
      if (query) {
        return '<div class="empty"><strong>没有匹配的文档</strong>' +
          "没有标题、文件名或原始路径包含「" + esc(query) + "」的文档。</div>";
      }
      return '<div class="empty"><strong>还没有文档</strong>' +
        "可以上传文件，或让运维把资料放进知识目录后点「同步目录」。</div>";
    }

    return '<table class="tbl kb-docs"><thead><tr>' +
      "<th>标题</th><th>来源</th><th>分级</th><th class=\"right\">切片</th>" +
      "<th class=\"right\">大小</th><th>状态</th><th>更新时间</th>" +
      (canWrite ? '<th class="right">操作</th>' : "") +
      "</tr></thead><tbody>" + items.map(function (item) {
        return kbDocRow(item, canWrite);
      }).join("") + "</tbody></table>";
  }

  /* ================= 连接器 ================= */

  var connUI = {
    items: [],
    // 当前展开的面板：{ name, kind: "credential" | "discover" | "edit" }
    panel: null,
    discovery: null,
    busy: false
  };

  function connKindLabel(kind) {
    return kind === "mcp" ? "MCP" : "HTTP";
  }

  function connHealth(item) {
    if (item.health_status === "ok") {
      return '<span class="chip ok">连通</span>';
    }
    if (item.health_status === "error") {
      return '<span class="chip err">异常</span>';
    }
    // 没检查过不等于正常。两者在界面上必须分开，
    // 否则「从没配过凭据」和「刚刚测通」看起来一模一样。
    return '<span class="chip">未检查</span>';
  }

  function connAuthText(item) {
    if (item.auth_type === "none") return "无需凭据";
    if (item.auth_type === "bearer") return "Bearer";
    if (item.auth_type === "header") return "请求头 " + (item.auth_header_name || "—");
    if (item.auth_type === "basic") return "Basic";
    return item.auth_type;
  }

  function connCard(item, canManage) {
    var open = connUI.panel && connUI.panel.name === item.name;
    var needsCredential = item.auth_type !== "none";
    var credentialChip = !needsCredential
      ? '<span class="chip">无需凭据</span>'
      : item.credential_set
        ? '<span class="chip ok">凭据已配置</span>'
        : '<span class="chip warn">凭据未配置</span>';

    var ops = [
      '<button class="btn sm" type="button" data-act="check" data-name="' + esc(item.name) + '">检查</button>',
      needsCredential
        ? '<button class="btn sm" type="button" data-act="credential" data-name="' + esc(item.name) + '">' +
          (item.credential_set ? "更换凭据" : "配置凭据") + "</button>"
        : "",
      canManage && item.kind === "mcp"
        ? '<button class="btn sm" type="button" data-act="discover" data-name="' + esc(item.name) + '">发现工具</button>'
        : "",
      canManage
        ? '<button class="btn sm" type="button" data-act="edit" data-name="' + esc(item.name) + '">编辑</button>'
        : "",
      canManage
        ? '<button class="btn sm danger" type="button" data-act="delete" data-name="' + esc(item.name) + '">删除</button>'
        : ""
    ].filter(Boolean).join("");

    return '<article class="conn-card' + (item.enabled ? "" : " off") + '">' +
      '<div class="conn-head">' +
        '<div class="conn-title">' +
          '<strong>' + esc(item.display_name || item.name) + "</strong>" +
          '<code class="conn-name">' + esc(item.name) + "</code>" +
        "</div>" +
        '<div class="conn-chips">' +
          '<span class="chip mono">' + esc(connKindLabel(item.kind)) + "</span>" +
          (item.enabled ? "" : '<span class="chip warn">已停用</span>') +
          connHealth(item) +
        "</div>" +
      "</div>" +
      (item.description ? '<p class="conn-desc">' + esc(item.description) + "</p>" : "") +
      '<dl class="conn-meta">' +
        "<div><dt>端点</dt><dd><code>" + esc(item.base_url || "—（工具自带完整地址）") + "</code></dd></div>" +
        "<div><dt>认证</dt><dd>" + esc(connAuthText(item)) + "</dd></div>" +
        "<div><dt>工具</dt><dd>" + item.tool_count + " 个</dd></div>" +
      "</dl>" +
      '<div class="conn-chips">' + credentialChip + "</div>" +
      (item.health_message
        ? '<p class="conn-health">' + esc(item.health_message) +
          (item.health_checked_at ? ' <span class="muted">· ' + esc(fmtTime(item.health_checked_at)) + "</span>" : "") +
          "</p>"
        : "") +
      (open ? connPanel(item, canManage) : "") +
      (ops ? '<div class="conn-ops">' + ops + "</div>" : "") +
      "</article>";
  }

  function connPanel(item, canManage) {
    if (connUI.panel.kind === "credential") {
      return '<div class="conn-panel">' +
        '<div class="conn-panel-title">配置凭据</div>' +
        '<p class="hint">' + esc(item.name) + " 的凭据只保存在服务端，不会回显。" +
        "填完立刻生效，不需要重启。</p>" +
        (item.auth_type === "basic"
          ? '<label class="field"><span>用户名</span>' +
            '<input type="text" id="conn-user" autocomplete="off" placeholder="Basic 认证的用户名"></label>'
          : "") +
        '<label class="field"><span>凭据</span>' +
        '<input type="password" id="conn-secret" autocomplete="new-password" placeholder="粘贴令牌或密钥"></label>' +
        '<div class="conn-panel-ops">' +
        '<button class="btn primary" type="button" data-act="credential-save" data-name="' + esc(item.name) + '">保存</button>' +
        (item.credential_set
          ? '<button class="btn danger" type="button" data-act="credential-clear" data-name="' + esc(item.name) + '">清除</button>'
          : "") +
        '<button class="btn" type="button" data-act="panel-close">取消</button>' +
        "</div>" +
        "</div>";
    }

    if (connUI.panel.kind === "discover") {
      var data = connUI.discovery;
      var body = '<div class="conn-panel-title">发现远端工具</div>';

      if (!data) {
        body += '<p class="hint">正在从 ' + esc(item.base_url) + " 拉取工具清单…</p>";
      } else {
        body += '<p class="hint">端点 <code>' + esc(data.endpoint) + "</code> 提供 " +
          data.tools.length + " 个工具。勾选后点「接入」，入参结构直接取自远端，本地不编造。</p>";
        if (!data.tools.length) {
          body += '<div class="empty">远端没有返回任何工具。</div>';
        } else {
          body += '<div class="conn-tools">' + data.tools.map(function (tool) {
            var bound = !!tool.bound_local_name;
            return '<label class="conn-tool' + (bound ? " bound" : "") + '">' +
              '<input type="checkbox" data-remote="' + esc(tool.remote_name) + '"' +
              (bound ? " checked disabled" : "") + ">" +
              '<span class="conn-tool-name"><code>' + esc(tool.remote_name) + "</code>" +
              (bound ? '<span class="chip ok">已接入 · ' + esc(tool.bound_local_name) + "</span>" : "") +
              "</span>" +
              (tool.description ? '<span class="conn-tool-desc">' + esc(tool.description) + "</span>" : "") +
              "</label>";
          }).join("") + "</div>";
          body += '<label class="field"><span>接入后的本地名前缀（可留空）</span>' +
            '<input type="text" id="conn-prefix" placeholder="例如 feishu_，便于在工具页区分来源"></label>';
          // 默认「写入」而不是「读取」：一次接二十个工具，没人会逐个判断风险。
          // 默认读取意味着远端某个 delete_* 变成免审批直接执行，代价不对等。
          body += '<label class="field"><span>这批工具的风险等级</span>' +
            '<select id="conn-risk">' +
            ["write", "read", "normal", "execute", "external", "destructive"].map(function (value) {
              return '<option value="' + value + '"' + (value === "write" ? " selected" : "") + ">" +
                (RISK_LABEL[value] || value) + "</option>";
            }).join("") +
            '</select></label>' +
            '<p class="hint">写入及以上调用前会走审批。接入后可以在工具页逐个改，但默认值只在这里定一次。</p>';
        }
      }

      body += '<div class="conn-panel-ops">' +
        (data && data.tools.length
          ? '<button class="btn primary" type="button" data-act="discover-bind" data-name="' + esc(item.name) + '">接入选中</button>'
          : "") +
        '<button class="btn" type="button" data-act="panel-close">关闭</button>' +
        "</div>";

      return '<div class="conn-panel">' + body + "</div>";
    }

    if (connUI.panel.kind === "edit") {
      var draft = connUI.panel.draft || item;
      return '<div class="conn-panel">' +
        '<div class="conn-panel-title">编辑连接器</div>' +
        '<div class="conn-form">' +
        '<label class="field"><span>显示名</span><input type="text" id="conn-display" value="' + esc(draft.display_name || "") + '"></label>' +
        '<label class="field wide"><span>端点地址</span><input type="text" id="conn-base" value="' + esc(draft.base_url || "") + '" placeholder="MCP 填完整端点；HTTP 可填地址前缀"></label>' +
        '<label class="field"><span>认证方式</span>' +
        '<select id="conn-auth">' +
        ["none", "bearer", "header", "basic"].map(function (value) {
          var label = { none: "无需凭据", bearer: "Bearer", header: "自定义请求头", basic: "Basic" }[value];
          return '<option value="' + value + '"' + (draft.auth_type === value ? " selected" : "") + ">" + label + "</option>";
        }).join("") +
        "</select></label>" +
        '<label class="field"><span>请求头名（仅「自定义请求头」需要）</span><input type="text" id="conn-header" value="' + esc(draft.auth_header_name || "") + '"></label>' +
        '<label class="field"><span>超时（秒）</span><input type="number" id="conn-timeout" min="1" max="300" value="' + (draft.timeout_seconds || 30) + '"></label>' +
        '<label class="check"><input type="checkbox" id="conn-enabled"' + (draft.enabled ? " checked" : "") + "><span>启用</span></label>" +
        "</div>" +
        '<div class="conn-panel-ops">' +
        '<button class="btn primary" type="button" data-act="edit-save" data-name="' + esc(item.name) + '">保存</button>' +
        '<button class="btn" type="button" data-act="panel-close">取消</button>' +
        "</div>" +
        "</div>";
    }

    return "";
  }

  function connNewForm() {
    return '<div class="card conn-new" id="conn-new">' +
      cardHead("新建连接器", "plus", '<button class="btn sm" type="button" data-act="new-close">取消</button>') +
      '<p class="hint">连接器是平台级配置：端点与认证方式改一次，所有租户的工具都跟着变。' +
      "凭据按租户各自配置，不在这里填。</p>" +
      '<div class="conn-form">' +
      '<label class="field"><span>标识</span><input type="text" id="new-name" placeholder="小写字母开头，如 feishu_prod"></label>' +
      '<label class="field"><span>显示名</span><input type="text" id="new-display" placeholder="飞书"></label>' +
      '<label class="field"><span>协议</span><select id="new-kind"><option value="mcp">MCP</option><option value="http">HTTP</option></select></label>' +
      '<label class="field wide"><span>端点地址</span><input type="text" id="new-base" placeholder="https://mcp.example.com/mcp"></label>' +
      '<label class="field"><span>认证方式</span><select id="new-auth">' +
      '<option value="bearer">Bearer</option><option value="none">无需凭据</option>' +
      '<option value="header">自定义请求头</option><option value="basic">Basic</option>' +
      "</select></label>" +
      '<label class="field"><span>请求头名（仅「自定义请求头」需要）</span><input type="text" id="new-header" placeholder="X-Api-Key"></label>' +
      "</div>" +
      '<div class="conn-panel-ops"><button class="btn primary" type="button" data-act="new-save">创建</button></div>' +
      "</div>";
  }

  function renderConnectors(root) {
    var grid = $("#conn-grid", root);
    if (!grid) return;

    var canManage = state.admin === true;

    if (!connUI.items.length) {
      grid.innerHTML = '<div class="empty" style="grid-column:1/-1">' +
        "<strong>还没有连接器</strong>" +
        "接入一个外部系统（飞书、Notion、内部 MCP 服务等），把它的接口变成受控工具。" +
        (canManage ? "" : "新建需要平台管理员权限。") +
        "</div>";
      return;
    }

    grid.innerHTML = connUI.items.map(function (item) {
      return connCard(item, canManage);
    }).join("");
  }

  async function loadConnectors(root) {
    try {
      // 这个接口不分页：连接器是平台级配置，数量以「几个」计，
      // 加 limit 反而会让人以为有下一页。
      var data = await api("/api/v1/connectors");
      connUI.items = data.items || [];
      renderConnectors(root);
    } catch (e) {
      var grid = $("#conn-grid", root);
      if (grid) {
        grid.innerHTML = '<div class="banner err" style="grid-column:1/-1">加载失败：' + esc(errText(e)) + "</div>";
      }
    }
  }

  async function mountConnectors(root) {
    connUI.panel = null;
    connUI.discovery = null;

    root.innerHTML = pageHead({
      eyebrow: PAGES.connectors.eyebrow,
      title: PAGES.connectors.title,
      desc: PAGES.connectors.desc,
      actions:
        '<button class="btn" type="button" id="conn-refresh">' + ico("refresh") + "刷新</button>" +
        (state.admin === true
          ? '<button class="btn primary" type="button" id="conn-add">' + ico("plus") + "新建连接器</button>"
          : "")
    }) +
      '<div id="conn-new-slot"></div>' +
      '<div class="conn-grid" id="conn-grid">' +
      '<div class="skeleton" style="height:150px;grid-column:1/-1"></div>' +
      "</div>";

    $("#conn-refresh").addEventListener("click", function () { loadConnectors(root); });

    var addBtn = $("#conn-add");
    if (addBtn) {
      addBtn.addEventListener("click", function () {
        $("#conn-new-slot").innerHTML = connNewForm();
      });
    }

    root.addEventListener("click", function (event) {
      var target = event.target.closest("[data-act]");
      if (!target) return;
      connAction(target.getAttribute("data-act"), target.getAttribute("data-name"), root);
    });

    await loadConnectors(root);
  }

  async function connAction(act, name, root) {
    var item = connUI.items.filter(function (row) { return row.name === name; })[0] || null;

    if (act === "panel-close") {
      connUI.panel = null;
      connUI.discovery = null;
      renderConnectors(root);
      return;
    }

    if (act === "new-close") {
      $("#conn-new-slot").innerHTML = "";
      return;
    }

    if (act === "new-save") {
      var payload = {
        name: $("#new-name").value.trim(),
        display_name: $("#new-display").value.trim(),
        kind: $("#new-kind").value,
        base_url: $("#new-base").value.trim(),
        auth_type: $("#new-auth").value,
        auth_header_name: $("#new-header").value.trim()
      };
      if (!payload.name) { toast("请填写标识", "err"); return; }
      try {
        await api("/api/v1/connectors", { method: "POST", body: payload });
        toast("连接器已创建");
        $("#conn-new-slot").innerHTML = "";
        await loadConnectors(root);
      } catch (e) {
        toast(errText(e), "err");
      }
      return;
    }

    if (!item) return;

    if (act === "check") {
      try {
        var res = await api("/api/v1/connectors/" + encodeURIComponent(name) + "/check", { method: "POST" });
        toast(res.message, res.status === "ok" ? "" : "err");
        await loadConnectors(root);
      } catch (e) {
        toast(errText(e), "err");
      }
      return;
    }

    if (act === "credential") {
      connUI.panel = { name: name, kind: "credential" };
      renderConnectors(root);
      var secret = $("#conn-secret");
      if (secret) secret.focus();
      return;
    }

    if (act === "credential-save") {
      var value = $("#conn-secret").value;
      var userInput = $("#conn-user");
      if (!value) { toast("请填写凭据", "err"); return; }
      try {
        await api("/api/v1/connectors/" + encodeURIComponent(name) + "/credential", {
          method: "PUT",
          body: { secret: value, username: userInput ? userInput.value.trim() : null }
        });
        // 输入框里的值不再保留：即使页面没刷新，也不该在 DOM 里留着明文。
        $("#conn-secret").value = "";
        connUI.panel = null;
        toast("凭据已保存");
        await loadConnectors(root);
      } catch (e) {
        toast(errText(e), "err");
      }
      return;
    }

    if (act === "credential-clear") {
      try {
        var cleared = await api("/api/v1/connectors/" + encodeURIComponent(name) + "/credential", {
          method: "DELETE"
        });
        connUI.panel = null;
        toast(cleared.message);
        await loadConnectors(root);
      } catch (e) {
        toast(errText(e), "err");
      }
      return;
    }

    if (act === "discover") {
      connUI.panel = { name: name, kind: "discover" };
      connUI.discovery = null;
      renderConnectors(root);
      try {
        connUI.discovery = await api("/api/v1/connectors/" + encodeURIComponent(name) + "/discover", {
          method: "POST"
        });
      } catch (e) {
        connUI.discovery = { endpoint: item.base_url, tools: [] };
        toast(errText(e), "err");
      }
      renderConnectors(root);
      return;
    }

    if (act === "discover-bind") {
      var prefix = ($("#conn-prefix") || {}).value || "";
      prefix = prefix.trim();
      // 不填就当「写入」：与后端 bind_mcp_tools 的默认值保持一致，
      // 界面少传一个字段不该变成「免审批」。
      var risk = ($("#conn-risk") || {}).value || "write";
      var picked = $$("#conn-grid input[data-remote]").filter(function (box) {
        return box.checked && !box.disabled;
      });
      if (!picked.length) { toast("没有选择要接入的工具", "err"); return; }

      var items = picked.map(function (box) {
        var remote = box.getAttribute("data-remote");
        var local = (prefix + remote).toLowerCase().replace(/[^a-z0-9_]/g, "_");
        if (!/^[a-z]/.test(local)) local = "t_" + local;
        return { remote_name: remote, local_name: local.slice(0, 64), risk_level: risk };
      });

      try {
        var bound = await api("/api/v1/connectors/" + encodeURIComponent(name) + "/tools", {
          method: "POST",
          body: { items: items }
        });
        toast(bound.message);
        connUI.panel = null;
        await loadConnectors(root);
      } catch (e) {
        toast(errText(e), "err");
      }
      return;
    }

    if (act === "edit") {
      connUI.panel = { name: name, kind: "edit", draft: item };
      renderConnectors(root);
      return;
    }

    if (act === "edit-save") {
      try {
        await api("/api/v1/connectors/" + encodeURIComponent(name), {
          method: "PATCH",
          body: {
            display_name: $("#conn-display").value.trim(),
            base_url: $("#conn-base").value.trim(),
            auth_type: $("#conn-auth").value,
            auth_header_name: $("#conn-header").value.trim(),
            timeout_seconds: parseInt($("#conn-timeout").value, 10) || 30,
            enabled: $("#conn-enabled").checked
          }
        });
        connUI.panel = null;
        toast("已保存");
        await loadConnectors(root);
      } catch (e) {
        toast(errText(e), "err");
      }
      return;
    }

    if (act === "delete") {
      if (!window.confirm("删除连接器「" + name + "」？下面还有工具时会被拒绝。")) return;
      try {
        await api("/api/v1/connectors/" + encodeURIComponent(name), { method: "DELETE" });
        toast("已删除");
        await loadConnectors(root);
      } catch (e) {
        toast(errText(e), "err");
      }
      return;
    }
  }

  async function mountKnowledge(root) {
    // 整页重渲染会丢掉展开状态，所以只有「刷新」重新跑 mountKnowledge；
    // 上传 / 下架 / 重建只重拉列表与状态，检索框里的输入不动。
    root.innerHTML = pageHead({
      eyebrow: PAGES.knowledge.eyebrow,
      title: PAGES.knowledge.title,
      desc: PAGES.knowledge.desc,
      actions: '<button class="btn" type="button" id="kb-refresh">' + ico("refresh") + "刷新</button>" +
        '<button class="btn" type="button" id="kb-sync">' + ico("layers") + "同步目录</button>" +
        '<button class="btn primary" type="button" id="kb-upload">' + ico("upload") + "上传文档</button>" +
        '<input type="file" id="kb-file" multiple hidden>'
    }) +
      '<div class="card">' +
      cardHead("索引状态", "book", '<span class="chip" id="kb-indexed">加载中</span>') +
      '<div class="stats" id="kb-stats">' + statSkeletons(3) + "</div>" +
      '<dl class="kv" id="kb-meta" style="margin-top:14px"></dl>' +
      "</div>" +
      '<div class="card">' +
      cardHead("文档", "file", '<div class="card-ops">' +
        '<span class="chip" id="kb-count">—</span>' +
        '<button class="icon-btn" type="button" id="kb-search-toggle" title="筛选文档" aria-label="筛选文档">' +
        ico("search") + "</button></div>") +
      // 收起时不占高度：大多数时候没人要筛，常年摆一个空输入框只是占地方。
      '<div class="kb-filterbar" id="kb-filterbar" hidden>' +
      '<input type="text" id="kb-filter" autocomplete="off" spellcheck="false"' +
      ' placeholder="按标题、文件名或原始路径筛选">' +
      '<span class="hint" id="kb-filter-note"></span>' +
      '<button class="linkish" type="button" id="kb-filter-clear">清空</button>' +
      "</div>" +
      '<div id="kb-docs"><div class="skeleton" style="width:60%"></div></div>' +
      '<p class="hint" id="kb-hint"></p>' +
      "</div>" +
      '<div class="card">' +
      cardHead("检索验证", "search") +
      '<div class="toolbar" style="margin-bottom:0">' +
      '<input type="text" id="kb-query" class="span2" placeholder="例如：停机前要确认哪些项目" style="flex:1">' +
      '<button class="btn primary" type="button" id="kb-search">检索</button>' +
      "</div>" +
      '<div id="kb-result" style="margin-top:12px">' +
      '<div class="empty"><strong>尚未检索</strong>输入问题后回车，验证召回与打分是否符合预期。</div>' +
      "</div>" +
      "</div>";

    var state = {
      items: [],
      canWrite: false,
      suffixes: [],
      maxBytes: 0,
      query: "",
      uploadCount: 0,
      truncated: false
    };

    var docsBox = root.querySelector("#kb-docs");
    var fileInput = root.querySelector("#kb-file");
    var uploadBtn = root.querySelector("#kb-upload");
    var syncBtn = root.querySelector("#kb-sync");
    var filterBar = root.querySelector("#kb-filterbar");
    var filterInput = root.querySelector("#kb-filter");
    var filterNote = root.querySelector("#kb-filter-note");
    var searchToggle = root.querySelector("#kb-search-toggle");

    root.querySelector("#kb-refresh").addEventListener("click", function () {
      mountKnowledge(root);
    });

    /* ---- 文档筛选 ----
     * 纯前端：列表本来就一次取完（上限 KB_DOC_LIMIT），为筛选再跑一趟接口不值。
     * 代价是只在已加载的这批里筛，所以命中上限时会明说，不假装筛过全部。 */

    function docMatches(item, q) {
      if (!q) return true;
      var hay = [item.title, item.filename, item.source_uri]
        .filter(Boolean).join(" ").toLowerCase();
      return hay.indexOf(q) >= 0;
    }

    function visibleDocs() {
      var q = state.query.trim().toLowerCase();
      return q ? state.items.filter(function (item) { return docMatches(item, q); }) : state.items;
    }

    function renderDocs() {
      var q = state.query.trim();
      var items = visibleDocs();
      docsBox.innerHTML = kbDocsTable(items, state.canWrite, q);

      root.querySelector("#kb-count").textContent = q
        ? items.length + " / " + state.items.length + " 篇"
        : state.items.length + " 篇" + (state.uploadCount ? " · 上传 " + state.uploadCount : "");

      filterNote.textContent = q && state.truncated
        ? "仅在前 " + KB_DOC_LIMIT + " 篇内筛选"
        : "";
    }

    function setQuery(value) {
      state.query = value;
      // 逐字符输入时不要回写 input.value：赋同一个值在某些浏览器里会把光标
      // 跳到末尾，人就没法在中间改字了。
      if (filterInput.value !== value) filterInput.value = value;
      renderDocs();
    }

    function toggleFilter(open) {
      filterBar.hidden = !open;
      searchToggle.classList.toggle("on", open);
      if (open) {
        filterInput.focus();
      } else if (state.query) {
        // 收起等于取消筛选。留着过滤后的结果却藏起输入框，人会以为文档少了。
        setQuery("");
      }
    }

    searchToggle.addEventListener("click", function () {
      toggleFilter(filterBar.hidden);
    });
    filterInput.addEventListener("input", function () { setQuery(this.value); });
    filterInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") {
        ev.preventDefault();
        toggleFilter(false);
      }
    });
    root.querySelector("#kb-filter-clear").addEventListener("click", function () {
      setQuery("");
      filterInput.focus();
    });

    /* ---- 状态 ---- */

    async function loadStatus() {
      try {
        var s = await api("/api/v1/knowledge/status");
        var chip = root.querySelector("#kb-indexed");
        chip.textContent = s.indexed ? "已索引" : "未索引";
        chip.className = "chip " + (s.indexed ? "ok" : "warn");
        root.querySelector("#kb-stats").innerHTML =
          stat("文档", s.documents) + stat("切片", s.chunks) + stat("版本", s.version, true);
        root.querySelector("#kb-meta").innerHTML =
          "<dt>后端</dt><dd>" + esc(s.backend) + "</dd>" +
          "<dt>知识目录</dt><dd class=\"mono\">" + esc(s.knowledge_dir) + "</dd>" +
          "<dt>可检索范围</dt><dd>" + esc((s.allowed_scopes || []).join(", ") || "—") + "</dd>";
      } catch (e) {
        var failChip = root.querySelector("#kb-indexed");
        failChip.className = "chip err";
        failChip.textContent = "读取失败";
        root.querySelector("#kb-meta").innerHTML =
          '<dt>错误</dt><dd class="muted">' + esc(errText(e)) + "</dd>";
      }
    }

    /* ---- 文档列表 ---- */

    async function loadDocs() {
      try {
        var d = await api("/api/v1/knowledge/documents?limit=" + KB_DOC_LIMIT);
        state.items = d.items || [];
        state.canWrite = !!d.can_write;
        state.suffixes = d.allowed_suffixes || [];
        state.maxBytes = d.max_upload_bytes || 0;
        state.uploadCount = (d.by_source && d.by_source.upload) || 0;
        // 拿到满额就可能被截断。接口返回的 total 是「过滤后的条数」，不是库里的总数，
        // 没法用它判断，只能比请求上限。
        state.truncated = state.items.length >= KB_DOC_LIMIT;
        renderDocs();

        // 白名单与上限由后端给：前后端各写一份，迟早会有一边忘了改。
        root.querySelector("#kb-hint").innerHTML = state.canWrite
          ? "支持 " + esc(state.suffixes.join(" ")) + "，单个文件不超过 " +
            esc(fmtBytes(state.maxBytes)) + "。上传的文档与知识目录（<span class=\"mono\">" +
            esc(d.directory) + "</span>）分开存放，互不影响。"
          : "当前身份没有 <span class=\"mono\">knowledge:write</span>，只能查看与检索。";
      } catch (e) {
        docsBox.innerHTML = '<div class="banner err">读取文档失败：' + esc(errText(e)) + "</div>";
      }
    }

    /* ---- 行展开：切片预览 ---- */

    async function toggleDetail(row, focusIndex) {
      var next = row.nextElementSibling;

      if (next && next.classList.contains("kb-detail")) {
        next.remove();
        row.classList.remove("selected");
        return;
      }

      // 详情接口每页 50 条。引用可能指向第 51 条之后的切片，
      // 不知道偏移就永远高亮不到目标，还会误高亮第一段。
      var pageLimit = 50;
      var offset = focusIndex == null ? 0 : Math.floor(focusIndex / pageLimit) * pageLimit;

      docsBox.querySelectorAll("tr.kb-detail").forEach(function (node) { node.remove(); });
      docsBox.querySelectorAll("tr.selected").forEach(function (node) { node.classList.remove("selected"); });

      row.classList.add("selected");
      var detail = document.createElement("tr");
      detail.className = "kb-detail";
      detail.innerHTML = '<td colspan="' + (state.canWrite ? 8 : 7) +
        '"><div class="skeleton" style="width:40%"></div></td>';
      row.insertAdjacentElement("afterend", detail);

      try {
        var url = "/api/v1/knowledge/documents/" + encodeURIComponent(row.getAttribute("data-id")) +
          "?limit=" + pageLimit + "&offset=" + offset;
        var data = await api(url);
        var chunks = data.chunks || [];
        var head = '<div class="muted kb-detail-head">原文 <span class="mono">' +
          esc(data.document.source_uri) + "</span> · 共 " + esc(String(data.chunk_total)) + " 个切片" +
          (offset ? "，当前第 " + (offset + 1) + "–" + (offset + chunks.length) + " 个" :
            (data.chunk_total > chunks.length ? "，这里显示前 " + chunks.length + " 个" : "")) + "</div>";
        detail.querySelector("td").innerHTML = head +
          (chunks.length
            ? chunks.map(kbChunkBox).join("")
            : '<div class="empty sm">这份文档没有切片。</div>');
      } catch (e) {
        detail.querySelector("td").innerHTML =
          '<div class="banner err">读取失败：' + esc(errText(e)) + "</div>";
      }
    }

    /* ---- 上传 ---- */

    async function uploadOne(file) {
      var form = new FormData();
      form.append("file", file);
      // 分级不让人选：页面上传默认 internal，改成 confidential 需要额外 scope，
      // 放一个下拉框只会让人以为随便选。
      form.append("access_scope", "internal");

      // 不能用 api()：它会把非字符串 body 当 JSON 序列化，FormData 会被毁掉。
      var res = await fetch("/api/v1/knowledge/documents", {
        method: "POST",
        body: form,
        credentials: "same-origin"
      });
      var text = await res.text();
      var payload = null;
      if (text) { try { payload = JSON.parse(text); } catch (e) { payload = null; } }

      if (!res.ok) {
        throw new Error((payload && payload.detail) || ("HTTP " + res.status));
      }

      return payload;
    }

    async function uploadFiles(files) {
      var label = uploadBtn.innerHTML;
      uploadBtn.disabled = true;
      var ok = 0;
      var failed = [];

      for (var i = 0; i < files.length; i += 1) {
        uploadBtn.textContent = "上传中 " + (i + 1) + "/" + files.length;

        try {
          await uploadOne(files[i]);
          ok += 1;
        } catch (e) {
          failed.push(files[i].name + "：" + errText(e));
        }
      }

      uploadBtn.disabled = false;
      uploadBtn.innerHTML = label;

      if (ok) toast("已上传 " + ok + " 份文档");
      if (failed.length) toast(failed.join("；"), "err");

      // 上传成功后清掉筛选：新文档的标题大概率不匹配当前关键词，
      // 留着筛选会让人以为「提示成功但列表里没有」。
      if (ok && state.query) setQuery("");
      await loadDocs();
      await loadStatus();
    }

    uploadBtn.addEventListener("click", function () {
      fileInput.click();
    });

    fileInput.addEventListener("change", function () {
      var files = Array.prototype.slice.call(fileInput.files || []);
      fileInput.value = "";

      if (files.length) uploadFiles(files);
    });

    /* ---- 同步 / 重建 / 下架 ---- */

    async function syncDirectory() {
      var label = syncBtn.innerHTML;
      syncBtn.disabled = true;
      syncBtn.textContent = "同步中…";

      try {
        var r = await api("/api/v1/knowledge/reindex", { method: "POST" });
        toast(r.message || "已同步");

        if (r.skipped && r.skipped.length) {
          toast("跳过 " + r.skipped.length + " 个文件：" + r.skipped.slice(0, 3).join("，"), "err");
        }

        await loadDocs();
        await loadStatus();
      } catch (e) {
        toast(errText(e), "err");
      } finally {
        syncBtn.disabled = false;
        syncBtn.innerHTML = label;
      }
    }

    async function removeDoc(id) {
      var item = state.items.filter(function (row) { return row.id === id; })[0];
      var title = item ? item.title : id;

      if (!confirm("下架《" + title + "》？下架后立即不再参与检索，记录会保留以便追溯。")) {
        return;
      }

      try {
        var r = await api("/api/v1/knowledge/documents/" + encodeURIComponent(id), { method: "DELETE" });
        toast(r.message || "已下架");
        await loadDocs();
        await loadStatus();
      } catch (e) {
        toast(errText(e), "err");
      }
    }

    async function reindexDoc(id) {
      try {
        await api("/api/v1/knowledge/documents/" + encodeURIComponent(id) + "/reindex", {
          method: "POST"
        });
        toast("已按原文重建");
        await loadDocs();
        await loadStatus();
      } catch (e) {
        toast(errText(e), "err");
      }
    }

    docsBox.addEventListener("click", function (ev) {
      var action = ev.target.closest("[data-act]");

      if (action) {
        ev.stopPropagation();
        var target = action.getAttribute("data-id");

        if (action.getAttribute("data-act") === "delete") removeDoc(target);
        else reindexDoc(target);

        return;
      }

      var row = ev.target.closest("tr[data-id]");
      if (row) toggleDetail(row);
    });

    /* ---- 引用可点开 ---- */

    // 引用里的 document_id / chunk_id 是逻辑标识，不是 documents 表的主键；
    // 列表项同时带 id 与 logical_id，先按 logical_id 找到行再展开。
    // chunk_id 形如 "<logical_id>-0003"，末段就是切片序号。
    async function openCitation(logicalId, chunkId) {
      var item = state.items.filter(function (row) { return row.logical_id === logicalId; })[0];

      if (!item) {
        toast("引用对应的文档已下架或不在当前列表", "err");
        return;
      }

      // 不能用 CSS.escape 拼属性选择器：它转义的是「标识符」，UUID 常以数字
      // 开头，放进 data-id="..." 的值里反而匹配不上。直接比对 dataset 最稳。
      function findRow() {
        var found = null;
        docsBox.querySelectorAll("tr[data-id]").forEach(function (tr) {
          if (tr.getAttribute("data-id") === String(item.id)) found = tr;
        });
        return found;
      }

      var row = findRow();
      if (!row && state.query) {
        // 引用指向的文档正被筛掉。留着看不见原因的筛选，人会以为「引用打不开」。
        toggleFilter(false);
        row = findRow();
        if (row) toast("已清除筛选以显示引用的文档");
      }
      if (!row) return;

      var match = /-(\d{4})$/.exec(String(chunkId || ""));
      var index = match ? parseInt(match[1], 10) : 0;

      // 已展开时先收起：toggleDetail 是切换语义，且偏移要跟着新目标重算。
      var opened = row.nextElementSibling;
      if (opened && opened.classList.contains("kb-detail")) {
        opened.remove();
        row.classList.remove("selected");
      }
      await toggleDetail(row, index);

      var detail = row.nextElementSibling;
      if (!detail || !detail.classList.contains("kb-detail")) return;

      var target = detail.querySelector('.kb-chunk[data-chunk-index="' + index + '"]');
      if (!target) {
        toast("该切片没有出现在当前展开范围", "err");
        return;
      }

      detail.querySelectorAll(".kb-chunk-focus").forEach(function (node) {
        node.classList.remove("kb-chunk-focus");
      });
      target.classList.add("kb-chunk-focus");
      target.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    root.querySelector("#kb-result").addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-cite-doc]");
      if (!btn) return;
      openCitation(btn.getAttribute("data-cite-doc"), btn.getAttribute("data-cite-chunk"));
    });

    /* ---- 检索验证 ---- */

    async function doSearch() {
      var input = root.querySelector("#kb-query");
      var q = input.value.trim();
      if (!q) return;

      var box = root.querySelector("#kb-result");
      box.innerHTML = '<div class="skeleton" style="width:70%"></div>' +
        '<div class="muted" style="margin-top:6px">检索中；语义重筛最多等待 8 秒，超时自动回落关键词排序。</div>';

      var btn = root.querySelector("#kb-search");
      btn.disabled = true;

      try {
        var r = await api("/api/v1/knowledge/search", { method: "POST", body: { query: q } });
        var rr = r.rerank || {};
        var rrText = rr.applied
          ? "语义重筛已生效 · 剔除 " + esc(String(rr.dropped || 0)) + " / " +
            esc(String(rr.candidates || 0)) + " 条 · " + esc(String(rr.elapsed_ms || 0)) + "ms"
          : "语义重筛未生效（" + esc(rr.fallback || "未返回") + "）";
        var html = '<div class="actions" style="margin-bottom:10px"><span class="chip ' +
          (r.found ? "ok" : "warn") + '">found=' + esc(String(r.found)) + "</span>" +
          '<span class="chip">citations=' + esc(String((r.citations || []).length)) + "</span>" +
          '<span class="chip ' + (rr.applied ? "ok" : "warn") + '">' + rrText + "</span></div>";

        if (r.answer) html += '<pre class="code">' + esc(r.answer) + "</pre>";

        if (r.citations && r.citations.length) {
          html += '<ul class="cites">' + r.citations.map(function (c) {
            var score = typeof c.score === "number" ? c.score.toFixed(3) : c.score;
            return '<li><button class="linkish" type="button" data-cite-doc="' +
              esc(c.document_id || "") + '" data-cite-chunk="' + esc(c.chunk_id || "") + '">' +
              esc(c.title || "—") + " · " + esc(c.section || "—") + "</button>" +
              " · score=" + esc(score) + ' · <span class="mono">' + esc(c.source_path || "") +
              "</span></li>";
          }).join("") + "</ul>";
        }

        box.innerHTML = html;
      } catch (e) {
        box.innerHTML = '<div class="banner err">检索失败：' + esc(errText(e)) + "</div>";
      } finally {
        btn.disabled = false;
      }
    }

    root.querySelector("#kb-search").addEventListener("click", doSearch);
    root.querySelector("#kb-query").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        doSearch();
      }
    });

    syncBtn.addEventListener("click", syncDirectory);

    await loadStatus();
    await loadDocs();

    if (!state.canWrite) {
      uploadBtn.disabled = true;
      syncBtn.disabled = true;
      syncBtn.title = "需要 knowledge:write 权限";
      uploadBtn.title = "需要 knowledge:write 权限";
    }

    // 从对话页的引用跳过来：#/knowledge?doc=<logical_id>&chunk=<chunk_id>
    var cite = routeParams();
    if (cite.doc) await openCitation(cite.doc, cite.chunk);
  }

  /* ================= 权限 ================= */

  // 三种动作不是「开关的三个档位」，而是三种不同的执行结局：
  // 允许 = 只要平台兜底不强制就直接跑；需确认 = 一律先生成审批单；
  // 拒绝 = 目录里看不见、绕过目录直接调用也会被拒。
  var PERM_ACTION_LABEL = { allow: "允许", confirm: "需确认", deny: "拒绝" };
  var PERM_ACTION_TONE = { allow: "ok", confirm: "warn", deny: "err" };
  var PERM_ACTION_HINT = {
    allow: "写入类工具仍会走审批——「允许」不等于「免审批」。",
    confirm: "即便是只读工具，调用前也会先生成审批单。",
    deny: "工具从模型目录消失；模型硬拼工具名调用也会被拒。"
  };

  // 「自定义 + MCP」是默认视图：这两类是接进来的，策略会真的改变行为。
  // 内置工具由代码注册，改策略只影响本租户的可见性与审批，所以收在「全部来源」里。
  var PERM_FILTERS = [
    { id: "managed", label: "自定义 + MCP" },
    { id: "all", label: "全部来源" }
  ];

  var permUI = {
    tenant: "",
    filter: "managed",
    items: [],
    summary: null,
    platform: null,
    selected: null,
    busy: false
  };

  function permActionChip(action) {
    var tone = PERM_ACTION_TONE[action] || "";
    return '<span class="chip' + (tone ? " " + tone : "") + '">' +
      esc(PERM_ACTION_LABEL[action] || action || "—") + "</span>";
  }

  function permKindLabel(item) {
    if (item.kind === "http") return "HTTP";
    if (item.kind === "mcp") return "MCP";
    return "内置";
  }

  function permVisible() {
    if (permUI.filter === "all") return permUI.items;
    return permUI.items.filter(function (item) {
      return item.source === "custom" || item.source === "mcp";
    });
  }

  function permNote(item) {
    // 两句话要分开：「配过什么」决定下次改哪里，「现在生效什么」决定这次会不会弹卡。
    var origin = item.action ? "已配置策略" : "跟随默认动作";
    var risk = RISK_LABEL[item.effective_risk] || item.effective_risk;
    return origin + " · " + (PERM_ACTION_LABEL[item.effective_action] || item.effective_action) +
      " · " + risk + (item.requires_approval ? " · 需审批" : "");
  }

  function permRow(item) {
    var active = permUI.selected === item.name ? " active" : "";
    return '<button class="tool-row perm-row' + active + '" type="button" data-perm="' +
      esc(item.name) + '">' +
      '<div class="tool-row-main">' +
        '<div class="tool-row-name">' + esc(item.display_name) + "</div>" +
        '<div class="tool-row-id">' + esc(item.name) + "</div>" +
        '<div class="perm-note">' + esc(permNote(item)) + "</div>" +
      "</div>" +
      '<div class="tool-row-chips">' +
        '<span class="chip mono">' + esc(permKindLabel(item)) + "</span>" +
        (item.enabled ? "" : '<span class="chip">已停用</span>') +
        permActionChip(item.effective_action) +
        riskChip(item.effective_risk) +
      "</div>" +
      "</button>";
  }

  function renderPermList() {
    var box = $("#perm-list");
    if (!box) return;

    var items = permVisible();

    if (!items.length) {
      box.innerHTML = '<div class="empty"><strong>没有可配置的工具</strong>' +
        "自定义工具与 MCP 工具接入后会出现在这里。</div>";
      return;
    }

    box.innerHTML = items.map(permRow).join("");
  }

  function permEditor(item) {
    if (!item) {
      return '<div class="empty"><strong>选一个工具</strong>' +
        "左边点一条，这里就能改它的动作、风险与可见范围。</div>";
    }

    var actionValue = item.action || "";
    var riskValue = item.risk_override || "";
    // 写入类工具不能选「读取」：下调等于开一条免审批直通，后端也会拒。
    var readDisabled = item.base_risk === "write" || item.base_risk === "execute" ||
      item.base_risk === "destructive" || item.base_risk === "external";
    var configured = !!item.action || !!item.risk_override ||
      (item.allowed_roles || []).length > 0 || !!item.credential_ref;

    return '<div class="perm-form">' +
      '<div class="perm-effective">' +
        "当前生效：" + permActionChip(item.effective_action) +
        '<span class="muted">' + esc(item.action ? "已配置策略" : "未配置，跟随平台默认动作") +
        " · 风险 " + esc(RISK_LABEL[item.effective_risk] || item.effective_risk) +
        (item.requires_approval ? " · 调用前需审批" : " · 直接执行") + "</span>" +
      "</div>" +

      '<label class="field"><span>Tool</span><select id="perm-tool">' +
        permUI.items.map(function (row) {
          return '<option value="' + esc(row.name) + '"' +
            (row.name === item.name ? " selected" : "") + ">" +
            esc(permKindLabel(row) + " · " + row.display_name) + "</option>";
        }).join("") +
      "</select></label>" +

      '<div class="perm-two">' +
        '<label class="field"><span>策略动作</span><select id="perm-action">' +
          '<option value=""' + (actionValue === "" ? " selected" : "") + ">未配置（跟随默认）</option>" +
          ["allow", "confirm", "deny"].map(function (value) {
            return '<option value="' + value + '"' + (actionValue === value ? " selected" : "") +
              ">" + PERM_ACTION_LABEL[value] + "</option>";
          }).join("") +
        "</select></label>" +
        '<label class="field"><span>策略风险</span><select id="perm-risk">' +
          '<option value=""' + (riskValue === "" ? " selected" : "") + ">跟随工具（" +
            esc(RISK_LABEL[item.base_risk] || item.base_risk) + "）</option>" +
          '<option value="read"' + (riskValue === "read" ? " selected" : "") +
            (readDisabled ? " disabled" : "") + ">读取</option>" +
          '<option value="write"' + (riskValue === "write" ? " selected" : "") + ">写入（需审批）</option>" +
        "</select></label>" +
      "</div>" +

      '<p class="hint" id="perm-action-hint">' +
        esc(PERM_ACTION_HINT[actionValue] || "未配置时按平台默认动作处理。") + "</p>" +

      '<details class="perm-adv"><summary>高级：角色与凭据</summary>' +
        '<label class="field"><span>允许的角色（留空 = 不按角色收窄）</span>' +
          '<input type="text" id="perm-roles" value="' +
            esc((item.allowed_roles || []).join(", ")) + '" placeholder="ops, reviewer"></label>' +
        '<label class="field"><span>凭据引用（环境变量名，不是密钥）</span>' +
          '<input type="text" id="perm-cred" value="' + esc(item.credential_ref || "") +
            '" placeholder="STATION_TOKEN"></label>' +
      "</details>" +

      '<div class="perm-ops">' +
        '<button class="btn primary" type="button" id="perm-save">保存</button>' +
        '<button class="btn" type="button" id="perm-reset"' +
          (configured ? "" : " disabled") + ">恢复未配置</button>" +
      "</div>" +
      "</div>";
  }

  function renderPermEditor() {
    var box = $("#perm-detail");
    if (!box) return;

    var item = permUI.items.filter(function (row) {
      return row.name === permUI.selected;
    })[0];

    box.innerHTML = cardHead("设置 Tool 权限", "shield", "") + permEditor(item);

    var toolSelect = $("#perm-tool");
    if (toolSelect) {
      toolSelect.addEventListener("change", function () {
        permUI.selected = this.value;
        renderPermList();
        renderPermEditor();
      });
    }

    var actionSelect = $("#perm-action");
    if (actionSelect) {
      actionSelect.addEventListener("change", function () {
        var hint = $("#perm-action-hint");
        if (hint) hint.textContent = PERM_ACTION_HINT[this.value] || "未配置时按平台默认动作处理。";
      });
    }

    var saveBtn = $("#perm-save");
    if (saveBtn) saveBtn.addEventListener("click", function () { savePermTool(item); });

    var resetBtn = $("#perm-reset");
    if (resetBtn) resetBtn.addEventListener("click", function () { resetPermTool(item); });
  }

  function readPermForm() {
    var roles = ($("#perm-roles") ? $("#perm-roles").value : "").split(",")
      .map(function (part) { return part.trim(); })
      .filter(Boolean);

    var action = $("#perm-action") ? $("#perm-action").value : "";
    var risk = $("#perm-risk") ? $("#perm-risk").value : "";
    var cred = $("#perm-cred") ? $("#perm-cred").value.trim() : "";

    return {
      // 空字符串要变成 null：`""` 会被后端当成非法动作，而 null 的语义是
      // 「恢复未配置」，两者在平台默认动作切成拒绝时行为完全不同。
      action: action || null,
      risk_override: risk || null,
      allowed_roles: roles,
      credential_ref: cred || null
    };
  }

  function permQuery() {
    // 只有管理员切过租户才带参数；默认留空，让后端用当前身份所在租户。
    var own = state.me && state.me.tenant_id ? state.me.tenant_id : "";
    if (!permUI.tenant || permUI.tenant === own) return "";
    return "?tenant=" + encodeURIComponent(permUI.tenant);
  }

  async function savePermTool(item) {
    if (permUI.busy) return;
    var btn = $("#perm-save");
    if (btn) btn.disabled = true;
    permUI.busy = true;

    try {
      await api("/api/v1/admin/permissions/tools/" + encodeURIComponent(item.name) + permQuery(), {
        method: "PUT",
        body: readPermForm()
      });
      toast("已保存：" + item.display_name + "，下一轮调用起生效");
      await loadPermissions();
    } catch (e) {
      toast(errText(e), "err");
    } finally {
      permUI.busy = false;
      if (btn) btn.disabled = false;
    }
  }

  async function resetPermTool(item) {
    if (permUI.busy) return;
    permUI.busy = true;
    try {
      await api("/api/v1/admin/permissions/tools/" + encodeURIComponent(item.name) + permQuery(), {
        method: "DELETE"
      });
      toast("已恢复未配置：" + item.display_name);
      await loadPermissions();
    } catch (e) {
      toast(errText(e), "err");
    } finally {
      permUI.busy = false;
    }
  }

  function permPlatformCard(data) {
    if (!data) return "";
    return '<div class="card perm-platform">' +
      cardHead("平台级策略", "sliders", '<span class="chip">不分租户</span>') +
      '<div class="perm-switch">' +
        '<div class="perm-switch-main">' +
          '<strong>未配置工具的默认动作</strong>' +
          '<span class="muted">没有单独配置策略的工具，默认' +
            (data.default_action === "allow" ? "放行" : "拒绝") + "。" +
            "改成拒绝后，只有显式配成允许或需确认的工具还能被模型看到。" +
            (data.default_action_source === "runtime"
              ? "" : "当前还是 .env 默认值，还没有人改过。") + "</span>" +
        "</div>" +
        '<label class="switch"><input type="checkbox" id="perm-default-deny"' +
          (data.default_action === "deny" ? " checked" : "") + "><span></span></label>" +
      "</div>" +
      '<div class="perm-switch">' +
        '<div class="perm-switch-main">' +
          '<strong>外部通道接入</strong>' +
          '<span class="muted">关闭后 /mcp 整体返回 503，已签发的令牌一并失效。' +
            "这是「立刻切断外部系统」的开关——改 nginx 要重载，删令牌要逐个点。" +
            (data.mcp_inbound_source === "runtime"
              ? "" : "当前还是默认开启，还没有人改过。") + "</span>" +
        "</div>" +
        '<label class="switch"><input type="checkbox" id="perm-inbound"' +
          (data.mcp_inbound_enabled ? " checked" : "") + "><span></span></label>" +
      "</div>" +
      "</div>";
  }

  function renderPermPlatform() {
    var slot = $("#perm-platform-slot");
    if (!slot) return;

    slot.innerHTML = permPlatformCard(permUI.platform);

    var denyToggle = $("#perm-default-deny");
    if (denyToggle) {
      denyToggle.addEventListener("change", function () {
        savePlatformPermissions({ default_action: this.checked ? "deny" : "allow" });
      });
    }

    var inboundToggle = $("#perm-inbound");
    if (inboundToggle) {
      inboundToggle.addEventListener("change", function () {
        savePlatformPermissions({ mcp_inbound_enabled: this.checked });
      });
    }
  }

  async function savePlatformPermissions(patch) {
    var data = permUI.platform;
    if (!data) return;

    var next = {
      default_action: patch.default_action || data.default_action,
      mcp_inbound_enabled: patch.mcp_inbound_enabled === undefined
        ? data.mcp_inbound_enabled
        : patch.mcp_inbound_enabled
    };

    try {
      permUI.platform = await api("/api/v1/admin/permissions/platform" + permQuery(), {
        method: "PUT",
        body: next
      });
      toast("平台策略已更新，立刻生效");
      await loadPermissions();
    } catch (e) {
      toast(errText(e), "err");
      await loadPermissions();
    }
  }

  async function loadPermissions() {
    var listBox = $("#perm-list");
    if (listBox) listBox.innerHTML = '<div class="skeleton" style="width:70%"></div>';

    try {
      var data = await api("/api/v1/admin/permissions" + permQuery());
      permUI.items = data.tools || [];
      permUI.summary = data.summary || null;
      permUI.platform = data;

      if (!permUI.selected && permUI.items.length) {
        // 默认选第一条可见的：打开页面就有一份可读的当前状态，
        // 而不是一块「请选择」的空面板。
        var first = permVisible()[0] || permUI.items[0];
        permUI.selected = first.name;
      }

      var countBox = $("#perm-count");
      if (countBox) {
        var s = permUI.summary || {};
        countBox.textContent = "共 " + (s.total || 0) + " 个工具 · 允许 " + (s.allow || 0) +
          " · 需确认 " + (s.confirm || 0) + " · 拒绝 " + (s.deny || 0) +
          " · 未配置 " + (s.unconfigured || 0);
      }

      renderPermList();
      renderPermEditor();
      // 平台开关也一起重绘：文案里带着「当前值来自哪里」（.env 默认 / 管理员设过），
      // 保存后不重绘会一直停在「还没有人改过」。
      renderPermPlatform();
    } catch (e) {
      if (listBox) {
        listBox.innerHTML = '<div class="banner err">加载失败：' + esc(errText(e)) + "</div>";
      }
    }
  }

  async function mountPermissions(root) {
    if (!state.admin) return adminGate(root, PAGES.permissions);

    permUI.tenant = state.me && state.me.tenant_id ? state.me.tenant_id : "";
    permUI.selected = null;

    var tenantOptions = "";
    if (state.me && (state.me.tenants || []).length > 1) {
      tenantOptions = (state.me.tenants || []).map(function (id) {
        return '<option value="' + esc(id) + '"' +
          (id === permUI.tenant ? " selected" : "") + ">" + esc(id) + "</option>";
      }).join("");
    }

    root.innerHTML = pageHead({
      eyebrow: PAGES.permissions.eyebrow,
      title: PAGES.permissions.title,
      desc: PAGES.permissions.desc,
      actions:
        (tenantOptions
          ? '<select class="perm-tenant" id="perm-tenant" title="策略按租户生效">' + tenantOptions + "</select>"
          : "") +
        '<button class="btn" type="button" id="perm-refresh">' + ico("refresh") + "刷新</button>"
    }) +
      '<div id="perm-platform-slot"></div>' +
      '<p class="hint perm-note-line">系统能力自动提供，无需逐项配置。这里管理的是' +
      "「能不能用、要不要确认、算几级风险」，以及它与当前租户的关系。</p>" +
      '<div class="tool-layout">' +
        '<div class="card tool-list-card">' +
          cardHead("Tool 权限", "wrench", '<span class="muted" id="perm-count">读取中…</span>') +
          '<div class="chips-row" id="perm-filters"></div>' +
          '<div class="tool-list" id="perm-list">' +
            '<div class="skeleton" style="width:70%"></div>' +
          "</div>" +
        "</div>" +
        '<div class="card tool-detail-card" id="perm-detail">' +
          '<div class="skeleton" style="width:60%"></div>' +
        "</div>" +
      "</div>";

    $("#perm-refresh").addEventListener("click", function () { loadPermissions(); });

    var tenantSelect = $("#perm-tenant");
    if (tenantSelect) {
      tenantSelect.addEventListener("change", function () {
        permUI.tenant = this.value;
        permUI.selected = null;
        loadPermissions();
      });
    }

    $("#perm-filters").innerHTML = PERM_FILTERS.map(function (f) {
      return '<button class="chip-btn' + (permUI.filter === f.id ? " active" : "") +
        '" type="button" data-filter="' + esc(f.id) + '">' + esc(f.label) + "</button>";
    }).join("");

    $$("#perm-filters [data-filter]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        permUI.filter = btn.getAttribute("data-filter");
        $$("#perm-filters [data-filter]").forEach(function (node) {
          node.classList.toggle("active", node === btn);
        });
        renderPermList();
      });
    });

    // 列表用事件委托：每次筛选都会重绘，逐个绑定会随重绘次数线性增长。
    $("#perm-list").addEventListener("click", function (ev) {
      var row = ev.target.closest("[data-perm]");
      if (!row) return;
      permUI.selected = row.getAttribute("data-perm");
      renderPermList();
      renderPermEditor();
    });

    await loadPermissions();

    // 平台卡在 loadPermissions 里已随数据渲染过一次；这里不再重复，只补租户下拉。
  }

  /* ================= 运行配置 ================= */

  // 预设只提供「看起来像那么回事」的字段填充，不会自动保存；
  // 模型名以各供应商 GET /models 的实际返回为准，填错会在对话时直接 400。
  var PROVIDER_PRESETS = [
    {
      name: "DeepSeek", protocol: "openai-compatible", baseUrl: "https://api.deepseek.com",
      models: [{ id: "deepseek-flash", kind: "text" }, { id: "deepseek-v4-pro", kind: "text" }],
      defaultText: "deepseek-flash"
    },
    {
      name: "OpenAI", protocol: "openai", baseUrl: "https://api.openai.com/v1",
      models: [{ id: "gpt-4o-mini", kind: "text" }, { id: "gpt-4o", kind: "text" }],
      defaultText: "gpt-4o-mini"
    },
    {
      name: "通义千问", protocol: "openai-compatible",
      baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      models: [{ id: "qwen-vl-max", kind: "text" }, { id: "qwen-plus", kind: "text" }],
      defaultText: "qwen-vl-max"
    },
    {
      name: "智谱 GLM", protocol: "openai-compatible", baseUrl: "https://open.bigmodel.cn/api/paas/v4",
      models: [{ id: "glm-4.6", kind: "text" }, { id: "glm-4v-plus", kind: "text" }],
      defaultText: "glm-4.6"
    },
    { name: "", protocol: "openai-compatible", baseUrl: "", models: [], defaultText: "" }
  ];

  // 弹窗内的临时状态，关闭即丢弃；未保存的改动不应影响页面其它部分。
  var mm = { id: null, models: [], editing: false, mask: null };

  function mountRuntime(root) {
    var adminCard = function (title, icon, body, chip) {
      return '<div class="card">' + cardHead(title, icon, chip || "") + body + "</div>";
    };

    var modelCard = state.admin
      ? '<div class="card" id="mm-card">' +
        cardHead(
          "模型供应商",
          "spark",
          '<div class="actions">' +
          '<select id="mm-preset" class="preset-select" aria-label="从供应商选择">' +
          '<option value="">从供应商选择</option>' +
          PROVIDER_PRESETS.map(function (p, i) {
            return '<option value="' + i + '">' + esc(p.name || "自定义") + "</option>";
          }).join("") +
          "</select>" +
          '<button class="btn" type="button" id="mm-add">' + ico("plus") + "添加供应商</button>" +
          "</div>"
        ) +
        '<p class="card-sub">普通聊天、工具调用与图片理解共用这里选中的模型。' +
        "切换供应商即时生效，不需要重启进程。</p>" +
        '<div id="mm-list"><div class="skeleton" style="width:60%"></div></div>' +
        '<div class="banner" id="mm-effective" style="margin-top:12px"><span class="skeleton" style="width:220px"></span></div>' +
        "</div>"
      : '<div class="card">' + cardHead("模型供应商", "spark", '<span class="chip">需平台管理员</span>') +
        '<div class="empty"><strong>当前身份无法修改平台级配置</strong>' +
        "模型供应商位于 model_providers 全局表，仅 platform_admin 可读写。</div></div>";

    var obsCard = state.admin
      ? '<div class="card" id="rt-obs-card">' +
        cardHead("观测", "activity", '<span class="chip" id="rt-obs-chip">读取中</span>') +
        '<p class="card-sub">把每轮对话的模型调用、工具执行与耗时写进 ' +
        '<span class="mono">obs_traces</span> / <span class="mono">obs_spans</span>，' +
        "排查「这一轮为什么慢、为什么答错」全靠它。当前处在开发调试期，默认开启。</p>" +
        '<label class="switch"><input type="checkbox" id="rt-obs-enabled" disabled>' +
        "<span>记录观测</span></label>" +
        '<p class="hint" id="rt-obs-hint">读取中…</p>' +
        "</div>"
      : '<div class="card">' + cardHead("观测", "activity", '<span class="chip">需平台管理员</span>') +
        '<div class="empty"><strong>当前身份无法修改平台级配置</strong>' +
        "观测开关存在 platform_settings 全局表里，只有 platform_admin 能改。</div></div>";

    root.innerHTML = pageHead({
      eyebrow: PAGES.runtime.eyebrow,
      title: PAGES.runtime.title,
      desc: PAGES.runtime.desc,
      actions: '<button class="btn" type="button" id="rt-refresh">' + ico("refresh") + "刷新</button>"
    }) +
      modelCard +
      obsCard +
      '<div class="card">' +
      cardHead("进程参数", "sliders", '<span class="chip">只读</span>') +
      '<dl class="kv" id="rt-meta"><div class="skeleton" style="width:70%"></div></dl>' +
      '<p class="hint">这些值来自进程本身，没有读取接口，故此处只显示就绪探测结果。' +
      "模型与密钥已迁到上方「模型供应商」，不再由 <span class=\"mono\">.env</span> 决定。" +
      "图片 / PDF 的文字识别由本机 OCR 服务的 MCP 接口 <span class=\"mono\">ocr_file</span> 提供，" +
      "地址取自 <span class=\"mono\">OCR_MCP_URL</span>，平台这边没有开关；连不上时对话里会直接说「OCR 服务不可用」。" +
      "</p>" +
      "</div>" +
      '<div class="card">' +
      cardHead("智能体回路", "activity", '<span class="chip">AGENT_MAX_TOOL_STEPS</span>') +
      '<p class="card-sub" style="margin-bottom:0">单次请求内「工具执行 → 重新规划」的轮次上限。' +
      "设为 1 等于关闭回路（工具结果直接作答，不再回灌模型），代价是模型无法基于工具结果二次决策。" +
      "该值来自环境变量，页面不可改。</p>" +
      "</div>";

    $("#rt-refresh").addEventListener("click", function () { mountRuntime(root); });

    try {
      var ready = null;
      api("/ready").then(function (r) {
        ready = r;
        $("#rt-meta").innerHTML =
          "<dt>版本</dt><dd>" + esc(ready.version) + "</dd>" +
          "<dt>环境</dt><dd>" + esc(ready.environment) + "</dd>" +
          "<dt>模型已配置</dt><dd>" + (ready.checks && ready.checks.model_configured ? "是" : "否") + "</dd>" +
          "<dt>工具表</dt><dd>" + (ready.checks && ready.checks.tool_registry ? "已加载" : "未加载") + "</dd>" +
          "<dt>MCP 通道</dt><dd>" + (ready.checks && ready.checks.mcp_tools ? "可用" : "不可用") + "</dd>";
      }).catch(function (e) {
        $("#rt-meta").innerHTML = '<dt>错误</dt><dd class="muted">' + esc(errText(e)) + "</dd>";
      });
    } catch (e) {
      $("#rt-meta").innerHTML = '<dt>错误</dt><dd class="muted">' + esc(errText(e)) + "</dd>";
    }

    if (state.admin) {
      $("#mm-add").addEventListener("click", function () { openProviderModal(null); });
      $("#mm-preset").addEventListener("change", function () {
        var idx = this.value;
        if (idx === "") return;
        openProviderModal(PROVIDER_PRESETS[parseInt(idx, 10)]);
        this.value = "";
      });

      var obsToggle = $("#rt-obs-enabled");
      if (obsToggle) {
        obsToggle.addEventListener("change", function () { saveObservabilitySetting(this.checked); });
      }
      loadRuntimeSettings();

      loadProviders();
    }
  }

  /* ---------------- 运行配置：观测开关 ---------------- */

  function applyObsState(enabled, source) {
    var input = $("#rt-obs-enabled");
    var chip = $("#rt-obs-chip");
    var hint = $("#rt-obs-hint");
    if (!input) return;

    input.checked = !!enabled;
    input.disabled = false;

    if (chip) {
      chip.textContent = enabled ? "已开启" : "已关闭";
      chip.className = "chip" + (enabled ? " ok" : "");
    }

    if (hint) {
      var effect = enabled
        ? "关闭后不再新增写入；已有数据仍可在观测页查看，并按保留期清理。"
        : "当前不记录新的 trace / span；已经写入的数据不受影响。";
      var origin = source === "runtime"
        ? "当前值由管理员设置，存在 platform_settings。"
        : "当前值来自 .env 的 OBS_ENABLED 默认值，还没有人在控制台改过。";
      hint.textContent = effect + "改动立刻生效，不需要重启进程。" + origin;
    }
  }

  async function loadRuntimeSettings() {
    if (!$("#rt-obs-enabled")) return;
    try {
      var data = await api("/api/v1/admin/runtime-settings");
      applyObsState(data.observability_enabled, data.observability_source);
    } catch (e) {
      var hint = $("#rt-obs-hint");
      if (hint) hint.textContent = "读取失败：" + errText(e);
    }
  }

  async function saveObservabilitySetting(next) {
    var input = $("#rt-obs-enabled");
    if (!input) return;

    input.disabled = true;
    try {
      var data = await api("/api/v1/admin/runtime-settings", {
        method: "PUT",
        body: { observability_enabled: next }
      });
      applyObsState(data.observability_enabled, data.observability_source);
      toast(data.observability_enabled ? "观测已开启，下一轮就会写入" : "观测已关闭，不再新增写入");
    } catch (e) {
      /* 回滚勾选状态：请求失败但开关还停在用户点过的位置，会让人以为改了。 */
      input.checked = !next;
      input.disabled = false;
      toast(errText(e), "err");
    }
  }

  /* ---------------- 供应商列表 ---------------- */

  function emptyProviderHint(eff) {
    // 这里必须回答「现在到底有没有可用模型」，不能只写「尚未添加」：
    // .env 有兜底时对话本来就是正常的，写成「未配置」会让人以为平台默认
    // 不带模型、必须先手动加一条才能用。
    var usable = !!(eff && eff.api_key_set);
    var model = esc((eff && eff.model) || "未知");

    if (!usable) {
      return '<div class="empty"><strong>还没有供应商，也没有可用密钥</strong>' +
        '.env 里没有可用的 <span class="mono">OPENAI_API_KEY</span>，对话会直接失败。' +
        "请添加供应商，或先在 .env 补上密钥。</div>";
    }

    return '<div class="empty"><strong>还没有添加供应商，但对话可以正常使用</strong>' +
      "当前生效的是 " + '<span class="mono">' + model + "</span>" +
      "（来自 .env 兜底），聊天与工具调用都正常。" +
      "添加供应商后可在这里一键切换，不必再改环境变量并重启。</div>";
  }

  async function loadProviders() {
    var box = $("#mm-list");
    if (!box) return;
    try {
      var data = await api("/api/v1/admin/model-providers");
      var items = data.items || [];
      var eff = data.effective;

      box.innerHTML = items.length
        ? items.map(providerRow).join("")
        : emptyProviderHint(eff);

      $$("[data-activate]", box).forEach(function (btn) {
        btn.addEventListener("click", function () { activateProvider(btn.getAttribute("data-activate")); });
      });
      $$("[data-edit]", box).forEach(function (btn) {
        btn.addEventListener("click", function () { editProvider(btn.getAttribute("data-edit")); });
      });
      $$("[data-del]", box).forEach(function (btn) {
        btn.addEventListener("click", function () { deleteProvider(btn.getAttribute("data-del"), btn.getAttribute("data-name")); });
      });

      $("#mm-effective").className = "banner" + (eff.api_key_set ? "" : " warn");
      // 来源为 .env 时不再重复 provider_name：那个名字本身就是「环境变量（.env）」，
      // 再拼一句「来源 环境变量」读起来是同一件事说两遍。
      $("#mm-effective").innerHTML = "<strong>当前生效：" + esc(eff.model) + "</strong>" +
        (eff.source === "provider"
          ? " · " + esc(eff.provider_name) + " · 来源 供应商配置"
          : " · 来源 .env 兜底") +
        (eff.api_key_set ? "" : " · 缺少 API Key，对话会直接失败");
    } catch (e) {
      box.innerHTML = '<div class="banner err">读取失败：' + esc(errText(e)) + "</div>";
    }
  }

  /* 窗口只给一个约数：「128k」比「128000 tokens」好读，
     而这里也不是需要精确对账的地方（精确值在编辑框里）。 */
  function fmtWindow(n) {
    var value = Number(n);
    if (!isFinite(value) || value <= 0) return "";
    return value >= 1000 ? Math.round(value / 1000) + "k" : String(value);
  }

  function providerRow(p) {
    var texts = (p.models || []).filter(function (m) { return m.kind !== "image"; });
    var images = (p.models || []).filter(function (m) { return m.kind === "image"; });
    var chips = texts.map(function (m) {
      var isDefault = m.id === p.default_text_model;
      // 窗口跟着模型名一起显示：这是「为什么历史被压缩得这么早」的第一现场，
      // 要翻进编辑框才能看到的话，排障时根本没人会去翻。
      var win = m.context_window ? " · " + fmtWindow(m.context_window) : "";
      return '<span class="chip' + (isDefault ? " ok" : "") + '">' + esc(m.id) + esc(win) + "</span>";
    }).join("");
    if (images.length) {
      chips += images.map(function (m) {
        return '<span class="chip">' + esc(m.id) + " · 图像</span>";
      }).join("");
    }

    return '<div class="prov-row">' +
      '<div class="prov-mark">' + esc(String(p.name || "?").slice(0, 1).toUpperCase()) + "</div>" +
      '<div class="prov-main">' +
      '<div class="prov-name">' + esc(p.name) +
      (p.is_active ? '<span class="chip ok">使用中</span>' : '<span class="chip">备用</span>') +
      (p.api_key_set ? "" : '<span class="chip warn">缺密钥</span>') +
      "</div>" +
      '<div class="prov-meta">' + esc(p.protocol) +
      (p.base_url ? " · " + esc(p.base_url) : "") +
      " · 温度 " + esc(p.temperature) + " · 思考 " + esc(p.reasoning_mode) + "</div>" +
      '<div class="prov-models">' + (chips || '<span class="faint" style="font-size:11.5px">未添加模型</span>') + "</div>" +
      "</div>" +
      '<div class="prov-actions">' +
      (p.is_active ? "" : '<button class="btn sm" type="button" data-activate="' + esc(p.id) + '">使用</button>') +
      '<button class="btn sm" type="button" data-edit="' + esc(p.id) + '">编辑</button>' +
      '<button class="btn sm danger" type="button" data-del="' + esc(p.id) + '" data-name="' + esc(p.name) + '">删除</button>' +
      "</div></div>";
  }

  async function activateProvider(id) {
    try {
      await api("/api/v1/admin/model-providers/" + encodeURIComponent(id) + "/activate", { method: "POST" });
      toast("已切换，下一条消息即生效", "ok");
      loadProviders();
    } catch (e) {
      toast("切换失败：" + errText(e), "err");
    }
  }

  async function deleteProvider(id, name) {
    if (!window.confirm("删除供应商「" + name + "」？如果它是当前使用中的，会自动切换到剩余的第一条。")) return;
    try {
      var r = await api("/api/v1/admin/model-providers/" + encodeURIComponent(id), { method: "DELETE" });
      toast(r.message || "已删除", "ok");
      loadProviders();
    } catch (e) {
      toast("删除失败：" + errText(e), "err");
    }
  }

  async function editProvider(id) {
    try {
      var data = await api("/api/v1/admin/model-providers");
      var row = (data.items || []).filter(function (p) { return String(p.id) === String(id); })[0];
      if (!row) { toast("供应商已不存在", "err"); return; }
      openProviderModal({
        id: row.id, name: row.name, protocol: row.protocol, baseUrl: row.base_url || "",
        models: row.models || [], defaultText: row.default_text_model || "",
        temperature: row.temperature, reasoningMode: row.reasoning_mode,
        apiKeySet: row.api_key_set
      });
    } catch (e) {
      toast("读取失败：" + errText(e), "err");
    }
  }

  /* ---------------- 添加 / 编辑供应商弹窗 ---------------- */

  function closeModal() {
    if (mm.mask) { mm.mask.remove(); mm.mask = null; }
    mm.id = null;
    mm.models = [];
    mm.editing = false;
  }

  function openProviderModal(preset) {
    closeModal();

    var editing = !!(preset && preset.id);
    mm.id = editing ? preset.id : null;
    mm.editing = editing;
    mm.models = (preset && preset.models ? preset.models : []).map(function (m) {
      // context_window 必须带上：漏了这一步，编辑一次已配好的供应商就把窗口清空，
      // 界面看起来什么都没变，实际已经退回 .env 的默认值。
      return { id: m.id, kind: m.kind || "text", context_window: m.context_window == null ? null : m.context_window };
    });
    mm.apiKeySet = !!(preset && preset.apiKeySet);

    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true">' +
      '<div class="modal-head"><div>' +
      '<div class="modal-eyebrow">MODEL PROVIDER</div>' +
      '<div class="modal-title">' + (editing ? "编辑模型供应商" : "添加模型供应商") + "</div>" +
      "</div><button class=\"modal-close\" type=\"button\" id=\"mm-close\" aria-label=\"关闭\">×</button></div>" +
      '<div class="modal-body">' +
      '<div class="form-grid">' +
      '<div class="field"><label>供应商名称</label>' +
      '<input type="text" id="mm-name" placeholder="例如：DeepSeek">' +
      '<span class="help">同名会冲突，用于在列表里区分。</span></div>' +
      '<div class="field"><label>接口协议</label><select id="mm-protocol">' +
      '<option value="openai-compatible">OpenAI-compatible</option>' +
      '<option value="openai">OpenAI 原生</option></select>' +
      '<span class="help">兼容协议即可对接绝大多数国产网关。</span></div>' +
      '<div class="field span2"><label>API Key</label>' +
      '<input type="password" id="mm-key" placeholder="输入供应商 API Key">' +
      '<span class="help" id="mm-key-hint"></span></div>' +
      '<div class="field span2"><label>Base URL</label>' +
      '<input type="text" id="mm-base" placeholder="官方服务可留空，兼容服务通常填 /v1 地址">' +
      '<span class="help">图片理解、工具调用都走这个地址，填错会直接 400。</span></div>' +
      "</div>" +

      '<div class="model-box">' +
      '<div class="model-box-head"><span class="model-box-title">模型列表</span>' +
      '<button class="btn sm" type="button" id="mm-discover">' + ico("search") + "发现模型</button></div>" +
      '<div class="model-add">' +
      '<input type="text" id="mm-model-input" placeholder="输入模型 ID，回车添加">' +
      '<select id="mm-model-kind" style="width:96px">' +
      '<option value="auto">自动</option><option value="text">文字</option><option value="image">图像</option></select>' +
      '<button class="btn" type="button" id="mm-model-add">添加</button>' +
      "</div>" +
      '<span class="help" id="mm-discover-msg"></span>' +
      '<div class="model-group">' +
      '<div class="model-group-label">文字模型<span class="hint">平时聊天、工具调用和图片理解使用</span></div>' +
      '<div class="model-chips" id="mm-text-chips"></div>' +
      '<select id="mm-text-default" style="max-width:260px"></select>' +
      // 窗口按模型填：同一供应商下 32k 与 128k 的模型常常并存，
      // 平台要按「当前用的是哪个」决定什么时候压缩历史。
      '<div class="model-window" id="mm-windows"></div>' +
      "</div>" +
      '<div class="model-group">' +
      '<div class="model-group-label">图片生成模型<span class="hint">仅生成或改图时使用</span></div>' +
      '<div class="model-chips" id="mm-image-chips"></div>' +
      '<span class="help">平台当前没有图片生成能力，这里仅登记，不参与任何调用。</span>' +
      "</div></div>" +

      '<div class="form-grid">' +
      '<div class="field"><label>Temperature</label>' +
      '<input type="number" id="mm-temp" min="0" max="2" step="0.1" placeholder="0">' +
      '<span class="help">0 最稳定；识别与工具调用建议保持 0。</span></div>' +
      '<div class="field"><label>思考模式</label><select id="mm-reasoning">' +
      '<option value="auto">自动（不下发，交给供应商）</option>' +
      '<option value="low">低</option>' +
      '<option value="medium">中</option>' +
      '<option value="high">高</option></select>' +
      '<span class="help">显式选择会作为 reasoning_effort 下发，兼容服务可能忽略。</span></div>' +
      "</div></div>" +
      '<div class="modal-foot">' +
      '<button class="btn" type="button" id="mm-cancel">取消</button>' +
      '<button class="btn primary" type="button" id="mm-save">保存供应商</button>' +
      "</div></div>";

    document.body.appendChild(mask);
    mm.mask = mask;

    $("#mm-name", mask).value = (preset && preset.name) || "";
    $("#mm-protocol", mask).value = (preset && preset.protocol) || "openai-compatible";
    $("#mm-base", mask).value = (preset && preset.baseUrl) || "";
    $("#mm-temp", mask).value = String(
      preset && preset.temperature !== undefined ? preset.temperature : 0
    );
    $("#mm-reasoning", mask).value = (preset && preset.reasoningMode) || "auto";
    $("#mm-key-hint").textContent = mm.apiKeySet
      ? "已保存密钥，留空表示不修改；填入新值会覆盖。"
      : "";

    mmRenderModels(preset && preset.defaultText);

    $("#mm-close", mask).addEventListener("click", closeModal);
    $("#mm-cancel", mask).addEventListener("click", closeModal);
    mask.addEventListener("click", function (event) { if (event.target === mask) closeModal(); });
    document.addEventListener("keydown", function escClose(event) {
      if (event.key === "Escape" && mm.mask === mask) {
        closeModal();
        document.removeEventListener("keydown", escClose);
      }
    });

    $("#mm-model-add", mask).addEventListener("click", function () { mmAddModel(); });
    $("#mm-model-input", mask).addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); mmAddModel(); }
    });
    $("#mm-discover", mask).addEventListener("click", function () { mmDiscover(mask); });
    $("#mm-save", mask).addEventListener("click", function () { mmSave(mask); });

    $("#mm-name", mask).focus();
  }

  // 模型 ID 里带这些词的，几乎一定是生成类模型而不是对话模型。
  // 猜错的代价是把它当文字模型用，对话时直接报「模型不支持 chat」。
  var IMAGE_MODEL_HINT = /(dall-?e|stable-?diffusion|sdxl|flux|midjourney|imagen|seedream|wanx|t2i|text-to-image|gpt-image|cogview)/i;

  function mmAddModel() {
    var input = $("#mm-model-input", mm.mask);
    var kindSelect = $("#mm-model-kind", mm.mask);
    var raw = (input.value || "").trim();
    if (!raw) return;

    var kind = kindSelect.value;
    if (kind === "auto") kind = IMAGE_MODEL_HINT.test(raw) ? "image" : "text";

    var exists = mm.models.some(function (m) { return m.id === raw; });
    if (!exists) mm.models.push({ id: raw, kind: kind });

    input.value = "";
    kindSelect.value = "auto";
    mmRenderModels();
    input.focus();
  }

  function mmRemoveModel(id) {
    mm.models = mm.models.filter(function (m) { return m.id !== id; });
    mmRenderModels();
  }

  function mmRenderModels(preferredDefault) {
    var mask = mm.mask;
    if (!mask) return;

    var texts = mm.models.filter(function (m) { return m.kind !== "image"; });
    var images = mm.models.filter(function (m) { return m.kind === "image"; });

    var textBox = $("#mm-text-chips", mask);
    textBox.innerHTML = texts.length
      ? texts.map(function (m) {
        return '<span class="model-chip">' + esc(m.id) +
          '<button type="button" data-rm="' + esc(m.id) + '" aria-label="移除">×</button></span>';
      }).join("")
      : '<span class="model-empty">尚未添加文字模型</span>';

    var imageBox = $("#mm-image-chips", mask);
    imageBox.innerHTML = images.length
      ? images.map(function (m) {
        return '<span class="model-chip">' + esc(m.id) +
          '<button type="button" data-rm="' + esc(m.id) + '" aria-label="移除">×</button></span>';
      }).join("")
      : '<span class="model-empty">发现或添加图片生成模型后会显示在这里。</span>';

    var select = $("#mm-text-default", mask);
    // 没有文字模型时这个下拉没有可选项，留着等于给用户一个假控件。
    select.style.display = texts.length ? "" : "none";
    var current = preferredDefault !== undefined && preferredDefault !== null
      ? preferredDefault
      : (select.value || "");
    select.innerHTML = texts.length
      ? texts.map(function (m) {
        return '<option value="' + esc(m.id) + '">' + esc(m.id) + "</option>";
      }).join("")
      : '<option value="">（先添加文字模型）</option>';
    if (current && texts.some(function (m) { return m.id === current; })) {
      select.value = current;
    }

    $$("[data-rm]", mask).forEach(function (btn) {
      btn.addEventListener("click", function () { mmRemoveModel(btn.getAttribute("data-rm")); });
    });

    mmRenderWindows(texts);
  }

  function mmRenderWindows(texts) {
    var box = $("#mm-windows", mm.mask);
    if (!box) return;

    if (!texts.length) {
      box.innerHTML = "";
      box.oninput = null;
      return;
    }

    // 只有文字模型有窗口：图像模型平台根本不调用，给它一个输入框等于邀请误解。
    box.innerHTML = '<div class="model-group-label" style="margin-top:8px">上下文窗口' +
      '<span class="hint">留空表示不声明，按平台默认值压缩</span></div>' +
      texts.map(function (m) {
        return '<label class="win-row"><span class="win-name">' + esc(m.id) + "</span>" +
          '<input type="number" class="win-input" data-win="' + esc(m.id) + '" min="1000" ' +
          'step="1000" inputmode="numeric" placeholder="不声明" value="' +
          (m.context_window == null ? "" : esc(m.context_window)) + '">' +
          '<span class="win-unit">tokens</span></label>';
      }).join("") +
      '<span class="help">例如 32000、128000。填错会静默影响历史压缩，单位是 token 不是字符。</span>';

    // 用赋值而不是 addEventListener：这个盒子每次重绘，反复绑定会让同一个输入框
    // 挂上多个监听器（输一个数字写多次，值一样但逻辑变脆）。
    box.oninput = function (event) {
      var input = event.target;
      if (!input || !input.getAttribute) return;
      var id = input.getAttribute("data-win");
      if (!id) return;

      var target = null;
      mm.models.forEach(function (m) { if (m.id === id) target = m; });
      if (!target) return;

      var raw = (input.value || "").trim();
      if (!raw) {
        target.context_window = null;
        return;
      }

      var parsed = parseInt(raw, 10);
      // 先原样留在输入框里由用户自己改，不在这里回写 0 —— 回写会让光标跳走。
      target.context_window = isNaN(parsed) ? null : parsed;
    };
  }

  async function mmDiscover(mask) {
    var msg = $("#mm-discover-msg", mask);
    var btn = $("#mm-discover", mask);
    msg.textContent = "正在查询…";
    msg.classList.remove("is-err");
    btn.disabled = true;
    try {
      var body = {
        base_url: $("#mm-base", mask).value || null,
        api_key: $("#mm-key", mask).value || null,
        provider_id: mm.id
      };
      var r = await api("/api/v1/admin/model-providers/discover", { method: "POST", body: body });
      var added = 0;
      (r.models || []).forEach(function (id) {
        if (mm.models.some(function (m) { return m.id === id; })) return;
        mm.models.push({ id: id, kind: IMAGE_MODEL_HINT.test(id) ? "image" : "text" });
        added += 1;
      });
      mmRenderModels();
      msg.textContent = r.message + "新增 " + added + " 个，可逐个移除。";
    } catch (e) {
      // 失败原因可能很长（超时 / 401 / 内网不可达各不同），别截断、别缩成一行。
      msg.textContent = "查询失败：" + errText(e);
      msg.classList.add("is-err");
    } finally {
      btn.disabled = false;
    }
  }

  async function mmSave(mask) {
    var name = ($("#mm-name", mask).value || "").trim();
    if (!name) { toast("供应商名称不能为空", "err"); return; }

    var texts = mm.models.filter(function (m) { return m.kind !== "image"; });
    if (!texts.length) { toast("至少添加一个文字模型，否则无法用于对话", "err"); return; }

    // 窗口越界先在这里拦：后端会 422，但那条报错读起来是字段校验，
    // 用户看不出「是我把 128000 打成了 128」。
    var badWindow = null;
    texts.forEach(function (m) {
      if (m.context_window == null) return;
      if (m.context_window < 1000 || m.context_window > 2000000) badWindow = m;
    });
    if (badWindow) {
      toast("「" + badWindow.id + "」的上下文窗口应在 1000–2000000 之间（单位 token）", "err");
      return;
    }

    var payload = {
      name: name,
      protocol: $("#mm-protocol", mask).value,
      base_url: $("#mm-base", mask).value || null,
      models: mm.models,
      default_text_model: $("#mm-text-default", mask).value || texts[0].id,
      temperature: Math.min(2, Math.max(0, parseFloat($("#mm-temp", mask).value) || 0)),
      reasoning_mode: $("#mm-reasoning", mask).value
    };

    var key = $("#mm-key", mask).value;
    if (key) payload.api_key = key;
    else if (mm.editing && !mm.apiKeySet) payload.api_key = null;

    var btn = $("#mm-save", mask);
    btn.disabled = true;
    try {
      if (mm.editing) {
        await api("/api/v1/admin/model-providers/" + encodeURIComponent(mm.id), { method: "PUT", body: payload });
      } else {
        await api("/api/v1/admin/model-providers", { method: "POST", body: payload });
      }
      closeModal();
      toast("供应商已保存", "ok");
      loadProviders();
    } catch (e) {
      toast("保存失败：" + errText(e), "err");
    } finally {
      btn.disabled = false;
    }
  }

  /* ================= 访问令牌 ================= */

  var tkUI = { items: [], mask: null };

  function tkState(row) {
    if (row.revoked_at) return { text: "已撤销", cls: "" };
    if (!row.active) return { text: "已过期", cls: "" };
    return { text: "有效", cls: " ok" };
  }

  function tkRow(row) {
    var st = tkState(row);
    var expired = row.expires_at ? new Date(row.expires_at) : null;
    var ops = row.revoked_at
      ? ""
      : '<button class="btn sm" type="button" data-tk-revoke="' + esc(row.id) + '">' +
        ico("trash") + "撤销</button>";

    return "<tr>" +
      "<td>" + esc(row.name) + "</td>" +
      '<td><span class="mono tk-prefix">' + esc(row.prefix) + "…</span></td>" +
      "<td>" + esc(shortTime(row.created_at)) + "</td>" +
      "<td>" + (expired ? esc(shortTime(row.expires_at)) : '<span class="muted">不过期</span>') + "</td>" +
      "<td>" + (row.last_used_at ? esc(shortTime(row.last_used_at)) : '<span class="muted">从未使用</span>') + "</td>" +
      '<td><span class="chip' + st.cls + '">' + st.text + "</span></td>" +
      '<td class="right">' + ops + "</td>" +
      "</tr>";
  }

  function tkListHtml() {
    if (!tkUI.items.length) {
      return '<div class="empty"><strong>还没有签发过令牌</strong>' +
        "外部宿主（WorkBuddy 连接器等）调用 PAP 时用它证明「我是谁」。" +
        "令牌代表你本人：权限随你的角色，审计里的操作人也是你。</div>";
    }

    return '<div class="tbl-scroll"><table class="tbl"><thead><tr>' +
      "<th>名称</th><th>前缀</th><th>创建</th><th>到期</th><th>最近使用</th><th>状态</th>" +
      '<th class="right">操作</th>' +
      "</tr></thead><tbody>" +
      tkUI.items.map(tkRow).join("") +
      "</tbody></table></div>";
  }

  function tkSnippet() {
    // 用当前站点地址拼，避免用户把 localhost 抄进去再回来问「为什么连不上」。
    var origin = location.origin;
    return '<pre class="tk-snippet">{\n' +
      '  "mcpServers": {\n' +
      '    "pap": {\n' +
      '      "type": "streamable-http",\n' +
      '      "url": "' + esc(origin) + '/mcp",\n' +
      '      "headers": { "Authorization": "Bearer &lt;签发的令牌&gt;" }\n' +
      "    }\n" +
      "  }\n" +
      "}</pre>";
  }

  async function mountTokens(root) {
    root.innerHTML = pageHead({
      eyebrow: PAGES.tokens.eyebrow,
      title: PAGES.tokens.title,
      desc: PAGES.tokens.desc,
      actions: '<button class="btn" type="button" id="tk-refresh">' + ico("refresh") + "刷新</button>"
    }) +
      '<div class="card">' +
      cardHead("我的令牌", "key",
        '<button class="btn primary sm" type="button" id="tk-create">' + ico("plus") + "签发新令牌</button>") +
      '<p class="card-sub">令牌交给外部宿主使用：它替你发起委派，PAP 按<strong>你的</strong>权限判定。' +
      "明文只在签发那一次显示，之后库里只有哈希——忘了就撤销重签，" +
      "不要试图从列表里「再查一次」。</p>" +
      '<div id="tk-list"><div class="skeleton" style="width:60%"></div></div>' +
      '<p class="hint">撤销立即生效：下一次调用就是 401，不等任何缓存过期。' +
      "不用了的那几枚早点收掉——它们和你本人一样能调所有东西。</p>" +
      "</div>" +
      '<div class="card">' +
      cardHead("填到连接器里", "link", '<span class="chip mono">mcp.json</span>') +
      '<p class="card-sub">直连带令牌的端点时用这一段；走连接器市场安装时，' +
      "令牌填在「连接」对话框里，不写进配置文件。</p>" +
      tkSnippet() +
      '<p class="hint">线上整站还有一层 HTTP Basic（浏览器控制台用的），' +
      "但 <span class=\"mono\">/mcp</span> 在反代上单独放行、只认 Bearer——" +
      "所以连接器里<strong>不要</strong>填 Basic 口令。</p>" +
      "</div>";

    $("#tk-refresh").addEventListener("click", function () { mountTokens(root); });
    $("#tk-create").addEventListener("click", openTokenCreate);

    $("#tk-list").addEventListener("click", function (e) {
      var btn = e.target.closest("[data-tk-revoke]");
      if (btn) revokeToken(btn.getAttribute("data-tk-revoke"), btn);
    });

    await loadTokens();
  }

  async function loadTokens() {
    var box = $("#tk-list");
    if (!box) return;
    try {
      var data = await api("/api/v1/me/tokens");
      tkUI.items = data.items || [];
      box.innerHTML = tkListHtml();
    } catch (e) {
      box.innerHTML = '<div class="empty"><strong>读取失败</strong>' + esc(errText(e)) + "</div>";
    }
  }

  function closeTokenMask() {
    if (tkUI.mask) {
      tkUI.mask.remove();
      tkUI.mask = null;
    }
  }

  function tokenMask(inner) {
    closeTokenMask();
    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML = '<div class="modal" role="dialog" aria-modal="true">' + inner + "</div>";
    document.body.appendChild(mask);
    tkUI.mask = mask;

    mask.addEventListener("click", function (e) {
      // 点遮罩空白处关闭；点卡片内部不关。
      if (e.target === mask) closeTokenMask();
    });

    return mask;
  }

  function openTokenCreate() {
    var mask = tokenMask(
      '<div class="modal-head"><div>' +
      '<div class="modal-eyebrow">ACCESS TOKEN</div>' +
      '<div class="modal-title">签发新令牌</div>' +
      "</div><button class=\"modal-close\" type=\"button\" data-tk-close aria-label=\"关闭\">×</button></div>" +
      '<div class="modal-body"><div class="form-grid">' +
      '<div class="field"><label>名称</label>' +
      '<input type="text" id="tk-name" maxlength="64" placeholder="例如：笔记本 WorkBuddy">' +
      '<span class="help">只是用来区分用途。写清楚是给哪台机器 / 哪个宿主用的，将来才敢收。</span></div>' +
      '<div class="field"><label>有效期</label><select id="tk-exp">' +
      '<option value="30">30 天</option>' +
      '<option value="90" selected>90 天</option>' +
      '<option value="180">180 天</option>' +
      '<option value="365">1 年</option>' +
      '<option value="0">不过期</option>' +
      "</select><span class=\"help\">到期不是安全兜底，只是让「签过就忘了」的令牌自己退场。</span></div>" +
      "</div></div>" +
      '<div class="modal-foot">' +
      '<button class="btn" type="button" data-tk-close>取消</button>' +
      '<button class="btn primary" type="button" id="tk-submit">' + ico("key") + "签发</button>" +
      "</div>"
    );

    mask.querySelectorAll("[data-tk-close]").forEach(function (node) {
      node.addEventListener("click", closeTokenMask);
    });
    $("#tk-submit", mask).addEventListener("click", submitTokenCreate);

    var name = $("#tk-name", mask);
    if (name) {
      name.focus();
      // 回车直接签发：这个名字本来就是一句话的事，没必要再挪一次手。
      name.addEventListener("keydown", function (e) {
        if (e.key === "Enter") submitTokenCreate();
      });
    }
  }

  async function submitTokenCreate() {
    var btn = $("#tk-submit");
    if (!btn || btn.disabled) return;

    var name = ($("#tk-name") || {}).value || "";
    var days = parseInt((($("#tk-exp") || {}).value || "90"), 10);

    btn.disabled = true;
    try {
      var created = await api("/api/v1/me/tokens", {
        method: "POST",
        body: { name: name.trim(), expires_in_days: days }
      });
      showTokenPlain(created);
      await loadTokens();
    } catch (e) {
      toast("签发失败：" + errText(e), "err");
      btn.disabled = false;
    }
  }

  function showTokenPlain(created) {
    var mask = tokenMask(
      '<div class="modal-head"><div>' +
      '<div class="modal-eyebrow">只显示这一次</div>' +
      '<div class="modal-title">令牌已签发</div>' +
      "</div></div>" +
      '<div class="modal-body">' +
      '<p class="modal-lead">把下面这串复制到连接器里。' +
      "<strong>关掉这个窗口就再也看不到了</strong>——库里只有哈希。</p>" +
      '<div class="tk-plain-wrap"><input class="tk-plain mono" id="tk-plain" readonly value="' +
      esc(created.token) + '">' +
      '<button class="btn sm" type="button" id="tk-copy">' + ico("file") + "复制</button></div>" +
      '<dl class="kv tk-kv">' +
      "<dt>名称</dt><dd>" + esc(created.name) + "</dd>" +
      "<dt>前缀</dt><dd class=\"mono\">" + esc(created.prefix) + "…</dd>" +
      "<dt>到期</dt><dd>" + (created.expires_at ? esc(fmtTime(created.expires_at)) : "不过期") + "</dd>" +
      "</dl>" +
      '<p class="hint">' + esc(created.hint || "") + "</p>" +
      "</div>" +
      '<div class="modal-foot">' +
      '<button class="btn primary" type="button" id="tk-done">我已保存</button>' +
      "</div>"
    );

    var input = $("#tk-plain", mask);
    if (input) {
      input.focus();
      input.select();
    }

    $("#tk-copy", mask).addEventListener("click", async function () {
      var text = created.token;
      try {
        await navigator.clipboard.writeText(text);
        toast("已复制到剪贴板");
      } catch (e) {
        // 非 HTTPS 或没授权时 clipboard 会直接抛。退回手动选中，
        // 让用户按 Ctrl+C——比弹一句「复制失败」然后什么都不给要好。
        if (input) { input.focus(); input.select(); }
        toast("浏览器不允许自动复制，已选中，请按 Ctrl+C", "err");
      }
    });

    $("#tk-done", mask).addEventListener("click", closeTokenMask);
  }

  async function revokeToken(id, btn) {
    btn.disabled = true;
    try {
      await api("/api/v1/me/tokens/" + encodeURIComponent(id), { method: "DELETE" });
      toast("已撤销，下一次调用即 401");
      await loadTokens();
    } catch (e) {
      toast("撤销失败：" + errText(e), "err");
      btn.disabled = false;
    }
  }

  /* ================= 组织与成员 ================= */

  function adminGate(root, page) {
    root.innerHTML = pageHead({
      eyebrow: page.eyebrow, title: page.title, desc: page.desc
    }) + '<div class="card"><div class="empty"><strong>需要平台管理员身份</strong>' +
      "当前身份不是 platform_admin，该页面依赖 <span class=\"mono\">/api/v1/admin/*</span> 管理通道，" +
      "普通身份调用会返回 403。请在服务端为账号授予 platform 租户下的 platform_admin 角色。</div></div>";
  }

  async function mountOrg(root) {
    if (!state.admin) return adminGate(root, PAGES.org);

    root.innerHTML = pageHead({
      eyebrow: PAGES.org.eyebrow,
      title: PAGES.org.title,
      desc: PAGES.org.desc,
      actions: '<button class="btn" type="button" id="org-refresh">' + ico("refresh") + "刷新</button>"
    }) +
      '<div class="card">' +
      cardHead("租户", "users", '<span class="chip" id="org-count">—</span>') +
      '<div id="org-tenants"><div class="skeleton" style="width:60%"></div></div>' +
      "</div>" +
      '<div class="card">' +
      cardHead("新建租户", "plus") +
      '<div class="form-grid">' +
      '<div class="field"><label>租户 ID</label><input type="text" id="new-tenant-id" placeholder="acme（受限字符集）"></div>' +
      '<div class="field"><label>显示名</label><input type="text" id="new-tenant-name" placeholder="ACME"></div>' +
      '<div class="field"><label>每日对话配额</label><input type="number" id="new-tenant-daily" placeholder="0 = 平台默认"></div>' +
      '<div class="field"><label>每分钟工具调用</label><input type="number" id="new-tenant-tpm" placeholder="0 = 平台默认"></div>' +
      "</div>" +
      '<div class="actions" style="margin-top:13px">' +
      '<button class="btn primary" type="button" id="new-tenant-submit">创建租户</button>' +
      '<span class="faint" style="font-size:11.5px" id="new-tenant-msg"></span>' +
      "</div></div>" +
      '<div class="card" id="members-card" style="display:none">' +
      cardHead("成员", "shield", '<span class="chip" id="members-tenant">—</span>') +
      '<div id="members-body"></div>' +
      "</div>";

    $("#org-refresh").addEventListener("click", function () { mountOrg(root); });
    $("#new-tenant-submit").addEventListener("click", createTenant);

    try {
      state.tenants = await api("/api/v1/admin/tenants");
    } catch (e) {
      $("#org-tenants").innerHTML = '<div class="banner err">加载失败：' + esc(errText(e)) + "</div>";
      return;
    }

    $("#org-count").textContent = state.tenants.length + " 个";
    $("#org-tenants").innerHTML = '<table class="tbl"><thead><tr>' +
      "<th>租户</th><th>名称</th><th>状态</th><th>创建时间</th><th class=\"right\">操作</th>" +
      "</tr></thead><tbody>" +
      state.tenants.map(function (t) {
        var active = t.status === "active";
        return '<tr><td class="mono">' + esc(t.tenant_id) + "</td>" +
          "<td>" + esc(t.name) + "</td>" +
          '<td><span class="chip ' + (active ? "ok" : "err") + '" title="' + esc(t.status) + '">' +
          (active ? "启用" : "停用") + "</span></td>" +
          '<td class="muted nowrap">' + esc(fmtTime(t.created_at)) + "</td>" +
          '<td class="right nowrap">' +
          '<button class="btn sm" data-members="' + esc(t.tenant_id) + '">成员</button> ' +
          '<button class="btn sm" data-toggle="' + esc(t.tenant_id) + '" data-status="' +
          (active ? "disabled" : "active") + '">' + (active ? "停用" : "启用") + "</button>" +
          "</td></tr>";
      }).join("") + "</tbody></table>";

    $$("[data-members]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        loadMembers(btn.getAttribute("data-members"));
      });
    });
    $$("[data-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        toggleTenant(btn.getAttribute("data-toggle"), btn.getAttribute("data-status"), root);
      });
    });
  }

  async function createTenant() {
    var id = $("#new-tenant-id").value.trim();
    var name = $("#new-tenant-name").value.trim();
    var msg = $("#new-tenant-msg");
    if (!id || !name) { msg.textContent = "租户 ID 与显示名必填。"; return; }
    var btn = $("#new-tenant-submit");
    btn.disabled = true;
    try {
      await api("/api/v1/admin/tenants", {
        method: "POST",
        body: {
          tenant_id: id,
          name: name,
          daily_chat_requests: parseInt($("#new-tenant-daily").value, 10) || 0,
          tool_calls_per_minute: parseInt($("#new-tenant-tpm").value, 10) || 0
        }
      });
      toast("租户已创建：" + id, "ok");
      var root = $("#view");
      mountOrg(root);
    } catch (e) {
      msg.textContent = "创建失败：" + errText(e);
    } finally {
      btn.disabled = false;
    }
  }

  async function toggleTenant(id, status, root) {
    try {
      await api("/api/v1/admin/tenants/" + encodeURIComponent(id), {
        method: "PATCH", body: { status: status }
      });
      toast("租户 " + id + " 已置为 " + status, "ok");
      mountOrg(root);
    } catch (e) {
      toast("操作失败：" + errText(e), "err");
    }
  }

  async function loadMembers(tenantId) {
    state.currentTenant = tenantId;
    var card = $("#members-card");
    card.style.display = "";
    $("#members-tenant").textContent = tenantId;
    $("#members-body").innerHTML = '<div class="skeleton" style="width:50%"></div>';

    var body = $("#members-body");
    try {
      var members = await api("/api/v1/admin/tenants/" + encodeURIComponent(tenantId) + "/members");
      var table = members.length
        ? '<table class="tbl"><thead><tr><th>主体</th><th>显示名</th><th>角色</th><th>状态</th>' +
          '<th class="right">操作</th></tr></thead><tbody>' +
          members.map(function (m) {
            return '<tr><td class="mono">' + esc(m.subject) + "</td>" +
              "<td>" + esc(m.display_name) + "</td>" +
              '<td><span class="chip">' + esc(m.role) + "</span></td>" +
              "<td>" + (m.enabled ? '<span class="chip ok">启用</span>' : '<span class="chip err">停用</span>') + "</td>" +
              '<td class="right"><button class="btn sm danger" data-del="' + esc(m.subject) + '">移除</button></td></tr>';
          }).join("") + "</tbody></table>"
        : '<div class="empty"><strong>该租户暂无成员</strong>在下方添加第一个成员。</div>';

      body.innerHTML = table +
        '<div class="form-grid" style="margin-top:14px">' +
        '<div class="field"><label>主体标识（对应 Basic Auth 用户名）</label>' +
        '<input type="text" id="member-subject" placeholder="alice"></div>' +
        '<div class="field"><label>角色</label>' +
        '<input type="text" id="member-role" list="role-hints" placeholder="dev">' +
        '<datalist id="role-hints"><option value="dev"></option><option value="platform_admin"></option></datalist></div>' +
        "</div>" +
        '<div class="actions" style="margin-top:12px">' +
        '<button class="btn primary" type="button" id="member-add">添加 / 更新成员</button>' +
        '<span class="faint" style="font-size:11.5px" id="member-msg"></span>' +
        "</div>";

      $("#member-add").addEventListener("click", function () { addMember(tenantId); });
      $$("[data-del]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          removeMember(tenantId, btn.getAttribute("data-del"));
        });
      });
    } catch (e) {
      body.innerHTML = '<div class="banner err">加载失败：' + esc(errText(e)) + "</div>";
    }
  }

  async function addMember(tenantId) {
    var subject = $("#member-subject").value.trim();
    var role = $("#member-role").value.trim();
    var msg = $("#member-msg");
    if (!subject || !role) { msg.textContent = "主体与角色都必填。"; return; }
    try {
      await api("/api/v1/admin/tenants/" + encodeURIComponent(tenantId) + "/members", {
        method: "PUT", body: { subject: subject, role: role }
      });
      toast("成员已就绪：" + subject, "ok");
      loadMembers(tenantId);
    } catch (e) {
      msg.textContent = "失败：" + errText(e);
    }
  }

  async function removeMember(tenantId, subject) {
    try {
      await api("/api/v1/admin/tenants/" + encodeURIComponent(tenantId) + "/members/" +
        encodeURIComponent(subject), { method: "DELETE" });
      toast("已移除：" + subject, "ok");
      loadMembers(tenantId);
    } catch (e) {
      toast("移除失败：" + errText(e), "err");
    }
  }

  /* ================= 审计 ================= */

  async function mountAudit(root) {
    if (!state.admin) return adminGate(root, PAGES.audit);

    root.innerHTML = pageHead({
      eyebrow: PAGES.audit.eyebrow,
      title: PAGES.audit.title,
      desc: PAGES.audit.desc,
      actions: '<button class="btn" type="button" id="audit-refresh">' + ico("refresh") + "刷新</button>"
    }) +
      '<div class="card">' +
      '<div class="toolbar">' +
      '<div class="field" style="width:200px"><label>租户</label>' +
      '<select id="audit-tenant"><option value="">加载中…</option></select></div>' +
      '<div class="field" style="width:120px"><label>条数</label>' +
      '<select id="audit-limit"><option>50</option><option selected>100</option><option>200</option><option>500</option></select></div>' +
      "</div>" +
      '<div id="audit-body"><div class="skeleton" style="width:60%"></div></div>' +
      "</div>";

    $("#audit-refresh").addEventListener("click", function () { mountAudit(root); });

    try {
      var tenants = await api("/api/v1/admin/tenants");
      var sel = $("#audit-tenant");
      sel.innerHTML = tenants.map(function (t) {
        return '<option value="' + esc(t.tenant_id) + '">' + esc(t.tenant_id) +
          " · " + esc(t.name) + "</option>";
      }).join("");
      if (!tenants.length) {
        $("#audit-body").innerHTML = '<div class="empty"><strong>还没有租户</strong></div>';
        return;
      }
      sel.addEventListener("change", loadAudit);
      $("#audit-limit").addEventListener("change", loadAudit);
      loadAudit();
    } catch (e) {
      $("#audit-body").innerHTML = '<div class="banner err">加载失败：' + esc(errText(e)) + "</div>";
    }
  }

  async function loadAudit() {
    var tenant = $("#audit-tenant").value;
    var limit = $("#audit-limit").value;
    var box = $("#audit-body");
    if (!tenant) return;
    box.innerHTML = '<div class="skeleton" style="width:60%"></div>';
    try {
      var rows = await api("/api/v1/admin/tenants/" + encodeURIComponent(tenant) +
        "/audit?limit=" + encodeURIComponent(limit));
      if (!rows.length) {
        box.innerHTML = '<div class="empty"><strong>没有审计记录</strong>该租户还没有产生管理动作或运行事件。</div>';
        return;
      }
      /* 列表限高滚动：条数可拉到 500，整页跟着长会把上面的租户/条数筛选
         滚出视野，想换租户得先滚回顶部。表头吸顶（.tbl-scroll 里已处理），
         滚到第 300 行时仍知道每列是什么。 */
      box.innerHTML = '<div class="tbl-scroll"><table class="tbl"><thead><tr>' +
        "<th>时间</th><th>事件</th><th>操作者</th><th>资源</th><th>结果</th><th>说明</th>" +
        "</tr></thead><tbody>" +
        rows.map(function (r) {
          return '<tr><td class="muted nowrap">' + esc(fmtTime(r.created_at)) + "</td>" +
            '<td><span class="chip">' + esc(r.event_type) + "</span></td>" +
            '<td class="mono">' + esc(r.actor_id || "—") + "</td>" +
            '<td class="mono">' + esc((r.resource_type || "—") + (r.resource_id ? "/" + r.resource_id : "")) + "</td>" +
            "<td>" + (r.success ? '<span class="chip ok">成功</span>' : '<span class="chip err">失败</span>') + "</td>" +
            '<td class="muted">' + esc(r.message || "—") + PAPRender.auditMeta(r.metadata) + "</td></tr>";
        }).join("") + "</tbody></table></div>";
    } catch (e) {
      box.innerHTML = '<div class="banner err">加载失败：' + esc(errText(e)) + "</div>";
    }
  }

  /* ================= 记忆 ================= */

  /* 长期记忆的读写界面。
     设计上只做四件事：看、加、改、删。不做「自动学习开关」「记忆强度调节」
     这类旋钮——记忆该不该留是人的判断，不是调参问题。 */

  var MEM_TYPES = [
    { id: "profile", label: "身份", cls: "k-graph" },
    { id: "preference", label: "偏好", cls: "k-llm" },
    { id: "decision", label: "决策", cls: "k-tool" },
    { id: "fact", label: "事实", cls: "k-db" },
    { id: "working_note", label: "进展", cls: "" }
  ];

  var mem = { items: [], total: 0, type: "", pendingOnly: false, enabled: true, canTenant: false, mask: null, editing: null, reviews: [], reviewsTotal: 0, stats: null, dreamCfg: null, dreamMask: null };

  function memType(id) {
    for (var i = 0; i < MEM_TYPES.length; i += 1) {
      if (MEM_TYPES[i].id === id) return MEM_TYPES[i];
    }
    return { id: id, label: id, cls: "" };
  }

  async function mountMemory(root) {
    root.innerHTML = pageHead({
      eyebrow: PAGES.memory.eyebrow,
      title: PAGES.memory.title,
      desc: PAGES.memory.desc,
      actions: '<button class="btn" type="button" id="mem-dream">' + ico("spark") + "开始梦境</button>" +
        '<button class="btn primary" type="button" id="mem-add">' + ico("plus") + "新增记忆</button>"
    }) + '<div id="mem-body"><div class="skeleton" style="width:70%"></div></div>';

    $("#mem-add").addEventListener("click", function () { openMemoryModal(null); });
    $("#mem-dream").addEventListener("click", runMemoryDream);
    await loadMemories();
  }

  async function loadMemories() {
    try {
      var query = "/api/v1/memories?status=active&limit=200";
      if (mem.type) query += "&memory_type=" + encodeURIComponent(mem.type);
      // 四块数据互不依赖：梦境建议、统计或配置读失败，不应该让整页打不开。
      var results = await Promise.all([
        api(query),
        api("/api/v1/memories/reviews?status=pending&limit=50").catch(function () {
          return { items: [], total: 0 };
        }),
        api("/api/v1/memories/extraction-stats?days=7").catch(function () { return null; }),
        api("/api/v1/memories/dream/config").catch(function () { return null; })
      ]);
      var r = results[0];
      mem.items = r.items || [];
      mem.total = r.total || 0;
      mem.enabled = r.enabled !== false;
      mem.canTenant = !!r.can_manage_tenant;
      mem.reviews = (results[1] && results[1].items) || [];
      mem.reviewsTotal = (results[1] && results[1].total) || 0;
      mem.stats = results[2] || null;
      mem.dreamCfg = results[3] || null;
      renderMemories();
    } catch (e) {
      $("#mem-body").innerHTML = '<div class="empty">读取失败：' + esc(errText(e)) + "</div>";
    }
  }

  function renderMemories() {
    var body = $("#mem-body");
    var pending = mem.items.filter(function (m) { return !m.is_user_confirmed; }).length;
    var shared = mem.items.filter(function (m) { return m.scope === "tenant"; }).length;

    var html = "";
    if (!mem.enabled) {
      html += '<div class="banner warn">' + ico("activity") +
        "记忆注入已关闭（memory_enabled=false）。这里仍可读写，但不会进入对话。</div>";
    }

    html += '<div class="stats" id="mem-stats">' +
      statCard("记忆条目", String(mem.total)) +
      statCard("待确认", String(pending)) +
      statCard("待整理", String(mem.reviews.length)) +
      statCard("团队共享", String(shared)) +
      "</div>";

    html += renderReviewCard();

    html += '<div class="card">' +
      cardHead("我的记忆", "layers", '<span class="chip">' + esc(mem.type ? memType(mem.type).label : "全部类型") + "</span>") +
      '<div class="toolbar">' +
      '<div class="field" style="width:150px"><label>类型</label><select id="mem-type">' +
      '<option value="">全部</option>' +
      MEM_TYPES.map(function (t) {
        return '<option value="' + esc(t.id) + '"' + (t.id === mem.type ? " selected" : "") + ">" + esc(t.label) + "</option>";
      }).join("") + "</select></div>" +
      '<label class="switch" style="margin-left:auto"><input type="checkbox" id="mem-pending"' +
      (mem.pendingOnly ? " checked" : "") + ">只看待确认</label>" +
      "</div>" +
      '<div id="mem-list">' + renderMemoryRows() + "</div>" +
      "</div>";

    body.innerHTML = html;

    $("#mem-type").addEventListener("change", function () {
      mem.type = this.value;
      loadMemories();
    });
    $("#mem-pending").addEventListener("change", function () {
      // 前端过滤：列表本来就一次取完（上限 200），为这个开关再跑一趟接口不值。
      mem.pendingOnly = this.checked;
      renderMemories();
    });
    bindMemoryRows();
    bindReviewRows();

    var cfgBtn = $("#mem-dream-cfg");
    if (cfgBtn) cfgBtn.addEventListener("click", openDreamConfigModal);

    // 梦境停用时按钮直接禁用。让人先点一次再吃一个 400，
    // 等于把「为什么不能用」藏到下一次交互里。
    var dreamBtn = $("#mem-dream");
    if (dreamBtn) {
      var off = !!(mem.dreamCfg && !mem.dreamCfg.enabled);
      dreamBtn.disabled = off;
      dreamBtn.title = off ? (mem.dreamCfg.disabled_reason || "梦境已停用") : "";
    }
  }

  var REVIEW_ACTIONS = {
    correct: "改写",
    merge: "合并",
    retire: "下架",
    keep: "保留"
  };

  /* 梦境卡片：只显示需要动作的建议。
     keep 不落库——证据不足时模型直接放过，不拿「无需改动」刷满页面。 */
  function renderReviewCard() {
    var cfg = mem.dreamCfg;
    var ops = '<span class="chip">' + mem.reviews.length + " 条</span>";

    // 生效参数直接摆在卡片头上：点「开始梦境」后到底会扫多久、跑几条，
    // 不该让人再点开配置才知道。
    if (cfg && cfg.enabled) {
      ops += '<span class="chip">近 ' + cfg.days + " 天 · 单次上限 " + cfg.max_per_run + " 条</span>";
    } else if (cfg) {
      ops += '<span class="chip warn">已停用</span>';
    }

    ops += '<button class="icon-btn" type="button" id="mem-dream-cfg" title="梦境配置" aria-label="梦境配置">' +
      ico("sliders") + "</button>";

    var head = cardHead("梦境建议", "spark", '<div class="card-ops">' + ops + "</div>");
    var body = mem.reviews.length
      ? '<div id="mem-reviews">' + mem.reviews.map(renderReviewRow).join("") + "</div>"
      : '<div class="empty">还没有梦境建议。点右上角「开始梦境」，让模型对照会话原文检查自动抽取的条目。</div>';

    var stats = "";
    if (mem.stats && mem.stats.auto_total) {
      stats = '<div class="mem-text" style="padding:10px 16px 14px;border-top:1px solid var(--line-soft)">近 ' +
        mem.stats.days + " 天自动抽取 " + mem.stats.auto_total + " 条 · 已确认 " +
        mem.stats.auto_confirmed + " 条 · 待确认 " + mem.stats.auto_pending +
        " 条 · 确认率 " + Math.round((mem.stats.confirmation_rate || 0) * 100) + "%</div>";
    }

    return '<div class="card">' + head + body + stats + "</div>";
  }

  function renderReviewRow(r) {
    var label = REVIEW_ACTIONS[r.action] || r.action;
    var evidence = (r.evidence || []).length;
    var proposal = "";
    if (r.proposed_content) {
      proposal = '<div class="mem-text">建议改为：' + esc(r.proposed_content) + "</div>";
    }
    if (r.action === "merge" && r.merge_target_id) {
      proposal += '<div class="mem-text">合并到另一条未确认记忆（' +
        esc(String(r.merge_target_id).slice(0, 8)) + "…）</div>";
    }
    return '<div class="mem-row" data-review="' + esc(r.id) + '">' +
      '<span class="mem-tag action">' + esc(label) + "</span>" +
      '<div class="mem-main">' +
      '<div class="mem-title">' + esc(r.memory_title || "（原记忆已删除）") + "</div>" +
      '<div class="mem-text">' + esc(r.reason) + "</div>" +
      proposal +
      '<div class="mem-text">证据 ' + evidence + " 条 · 置信度 " +
      Math.round((r.confidence || 0) * 100) + "%" +
      (r.can_apply ? "" : " · 仅本人可处理") + "</div>" +
      "</div>" +
      '<div class="mem-when">' + esc(fmtTime(r.created_at).slice(5, 16)) + "</div>" +
      '<div class="mem-ops">' +
      (r.can_apply
        ? '<button class="btn sm" type="button" data-review-act="apply">应用</button>' +
          '<button class="btn sm" type="button" data-review-act="dismiss">忽略</button>'
        : "") +
      "</div></div>";
  }

  function bindReviewRows() {
    $$("[data-review]").forEach(function (row) {
      var id = row.getAttribute("data-review");
      $$("[data-review-act]", row).forEach(function (btn) {
        btn.addEventListener("click", function () {
          var act = btn.getAttribute("data-review-act");
          if (act === "apply") applyMemoryReview(id, btn);
          else dismissMemoryReview(id, btn);
        });
      });
    });
  }

  /* 梦境是一次模型调用换一条建议，按需触发。
     一次只跑 5 条：跑满 20 条会让按钮转上一分钟，用户以为页面卡死了。 */
  async function runMemoryDream() {
    var btn = $("#mem-dream");
    if (btn.disabled) return;
    var old = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = ico("spark") + "梦境进行中…";
    try {
      var r = await api("/api/v1/memories/dream", {
        method: "POST",
        // 只给「这次跑几条」，回溯天数交给配置——否则界面上的配置永远被这里覆盖。
        body: { limit: 5 }
      });
      toast("梦境完成：新增 " + r.suggested + " 条建议，" + r.kept + " 条无需改动" +
        (r.failed ? "，失败 " + r.failed + " 条" : ""));
      await loadMemories();
    } catch (e) {
      toast("梦境失败：" + errText(e), "err");
    } finally {
      btn.disabled = false;
      btn.innerHTML = old;
    }
  }

  async function applyMemoryReview(id, btn) {
    btn.disabled = true;
    try {
      await api("/api/v1/memories/reviews/" + encodeURIComponent(id) + "/apply", {
        method: "POST"
      });
      toast("已应用建议");
      await loadMemories();
    } catch (e) {
      toast("应用失败：" + errText(e), "err");
      btn.disabled = false;
    }
  }

  async function dismissMemoryReview(id, btn) {
    btn.disabled = true;
    try {
      await api("/api/v1/memories/reviews/" + encodeURIComponent(id) + "/dismiss", {
        method: "POST"
      });
      toast("已忽略");
      await loadMemories();
    } catch (e) {
      toast("忽略失败：" + errText(e), "err");
      btn.disabled = false;
    }
  }

  function statCard(label, value) {
    return '<div class="stat"><div class="stat-label">' + esc(label) + "</div>" +
      '<div class="stat-value">' + esc(value) + "</div></div>";
  }

  function renderMemoryRows() {
    var rows = mem.items.filter(function (m) {
      return !mem.pendingOnly || !m.is_user_confirmed;
    });

    if (!rows.length) {
      return '<div class="empty">' + (mem.pendingOnly
        ? "没有待确认的记忆。自动抽取出来的条目都会先落在这里，确认后才当成确定信息。"
        : "还没有记忆。对话里说明的稳定信息会出现在这里，也可以手动新增。") + "</div>";
    }

    return rows.map(function (m) {
      var meta = memType(m.memory_type);
      var confirmed = m.is_user_confirmed;
      return '<div class="mem-row" data-id="' + esc(m.id) + '">' +
        '<span class="obs-kind ' + meta.cls + '">' + esc(meta.label) + "</span>" +
        '<div class="mem-main">' +
        '<div class="mem-title">' + esc(m.title) +
        (confirmed ? "" : '<span class="mem-tag">待确认</span>') +
        // 来源写清楚：用户自己写的和模型抽出来的，可信度不是一回事。
        (m.source === "auto_extract" ? '<span class="mem-tag auto">自动</span>' : "") +
        (m.scope === "tenant" ? '<span class="mem-tag shared">团队</span>' : "") +
        "</div>" +
        '<div class="mem-text">' + esc(m.content) + "</div>" +
        "</div>" +
        '<div class="mem-when">' + esc(fmtTime(m.updated_at).slice(5, 16)) + "</div>" +
        '<div class="mem-ops">' +
        (confirmed ? "" : '<button class="btn sm" type="button" data-act="confirm">确认</button>') +
        '<button class="btn sm" type="button" data-act="edit">编辑</button>' +
        '<button class="btn sm danger" type="button" data-act="del">删除</button>' +
        "</div></div>";
    }).join("");
  }

  function bindMemoryRows() {
    $$(".mem-row").forEach(function (row) {
      var id = row.getAttribute("data-id");
      var item = mem.items.filter(function (m) { return m.id === id; })[0];
      $$("[data-act]", row).forEach(function (btn) {
        btn.addEventListener("click", function () {
          var act = btn.getAttribute("data-act");
          if (act === "edit") openMemoryModal(item);
          else if (act === "confirm") confirmMemoryItem(id);
          else if (act === "del") deleteMemoryItem(item);
        });
      });
    });
  }

  async function confirmMemoryItem(id) {
    try {
      await api("/api/v1/memories/" + encodeURIComponent(id) + "/confirm", { method: "POST" });
      toast("已确认，之后会作为背景信息使用");
      await loadMemories();
    } catch (e) {
      toast("确认失败：" + errText(e), "err");
    }
  }

  async function deleteMemoryItem(item) {
    var ok = await openConfirm({
      title: "删除这条记忆",
      body: '<p class="modal-lead">将永久删除「<strong>' + esc(item.title) +
        "</strong>」，无法恢复。对话记录不受影响。</p>",
      okText: "删除"
    });
    if (!ok) return;
    try {
      await api("/api/v1/memories/" + encodeURIComponent(item.id), { method: "DELETE" });
      toast("已删除");
      await loadMemories();
    } catch (e) {
      toast("删除失败：" + errText(e), "err");
    }
  }

  /* ---------------- 梦境（dreaming）配置弹窗 ---------------- */

  /* 三个配置项的两层语义，界面上必须说清楚，否则用户会误解：
   *
   *   开关   —— 三态。跟随平台 / 本租户启用 / 本租户停用。
   *             平台层关闭时租户无法自行打开（急停优先），这里直接禁用表单。
   *   天数   —— 空 = 跟随平台默认。不填数字不代表 0 天，而是「不覆盖」。
   *   上限   —— 空 = 跟随平台默认。
   *
   * 因此输入框留空时用 placeholder 显示「当前生效值」，
   * 而不是把生效值直接填进输入框——那会立刻变成一次「明确设置」，
   * 以后平台调默认值这个租户就不跟随了。
   */
  function closeDreamConfigModal() {
    if (mem.dreamMask) { mem.dreamMask.remove(); mem.dreamMask = null; }
  }

  function openDreamConfigModal() {
    var cfg = mem.dreamCfg;
    if (!cfg) {
      toast("配置还没读到，稍后再试", "err");
      return;
    }

    closeDreamConfigModal();

    var locked = !cfg.can_edit || !cfg.platform_enabled;
    var banner = "";
    if (!cfg.platform_enabled) {
      banner = '<div class="banner warn">' + ico("shield") +
        "平台层已关闭梦境，本租户无法自行打开。请联系运维检查 MEMORY_ENABLED / MEMORY_DREAM_ENABLED。</div>";
    } else if (!cfg.can_edit) {
      banner = '<div class="banner warn">' + ico("shield") +
        "只能查看。修改需要 memory:read 权限或平台管理员。</div>";
    }

    var enabledVal = cfg.tenant_enabled === null || cfg.tenant_enabled === undefined
      ? "inherit"
      : (cfg.tenant_enabled ? "on" : "off");
    var inheritLabel = cfg.platform_enabled ? "启用" : "停用";

    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true">' +
      '<div class="modal-head"><div>' +
      '<div class="modal-eyebrow">MEMORY DREAM</div>' +
      '<div class="modal-title">梦境配置</div>' +
      "</div><button class=\"modal-close\" type=\"button\" aria-label=\"关闭\">×</button></div>" +
      '<div class="modal-body">' + banner +
      '<div class="form-grid">' +
      '<div class="field span2"><label>启用梦境</label>' +
      '<select id="mem-dc-enabled"' + (locked ? " disabled" : "") + ">" +
      '<option value="inherit"' + (enabledVal === "inherit" ? " selected" : "") + ">跟随平台（当前" + inheritLabel + "）</option>" +
      '<option value="on"' + (enabledVal === "on" ? " selected" : "") + ">本租户启用</option>" +
      '<option value="off"' + (enabledVal === "off" ? " selected" : "") + ">本租户停用</option>" +
      "</select>" +
      '<span class="help">停用后「开始梦境」会被拒绝，已有建议仍然保留，可以照常应用。</span></div>' +

      '<div class="field"><label>回溯窗口（天）</label>' +
      '<input type="number" id="mem-dc-days" min="' + cfg.days_min + '" max="' + cfg.days_max + '"' +
      ' value="' + (cfg.tenant_days > 0 ? cfg.tenant_days : "") + '"' +
      ' placeholder="跟随平台（' + cfg.days + "）\"" + (locked ? " disabled" : "") + ">" +
      '<span class="help">只扫最近这么多天的用户消息作为证据。留空 = 跟随平台默认。</span></div>' +

      '<div class="field"><label>单次上限（条）</label>' +
      '<input type="number" id="mem-dc-limit" min="' + cfg.max_per_run_min + '" max="' + cfg.max_per_run_max + '"' +
      ' value="' + (cfg.tenant_max_per_run > 0 ? cfg.tenant_max_per_run : "") + '"' +
      ' placeholder="跟随平台（' + cfg.max_per_run + "）\"" + (locked ? " disabled" : "") + ">" +
      '<span class="help">一次梦境最多整理几条记忆。每条要调一次模型，条数越大越慢。</span></div>' +
      "</div></div>" +
      '<div class="modal-foot">' +
      '<button class="btn" type="button" data-dc="cancel">关闭</button>' +
      (locked ? "" : '<button class="btn primary" type="button" data-dc="save">保存</button>') +
      "</div></div>";

    document.body.appendChild(mask);
    mem.dreamMask = mask;

    $(".modal-close", mask).addEventListener("click", closeDreamConfigModal);
    $('[data-dc="cancel"]', mask).addEventListener("click", closeDreamConfigModal);
    var saveBtn = $('[data-dc="save"]', mask);
    if (saveBtn) saveBtn.addEventListener("click", saveDreamConfig);
    mask.addEventListener("click", function (ev) {
      if (ev.target === mask) closeDreamConfigModal();
    });
  }

  async function saveDreamConfig() {
    var mask = mem.dreamMask;
    if (!mask) return;

    var sel = $("#mem-dc-enabled");
    var daysRaw = ($("#mem-dc-days").value || "").trim();
    var limitRaw = ($("#mem-dc-limit").value || "").trim();

    // 空字符串是「跟随平台」，必须转成 0/null 而不是 NaN，否则后端会 422。
    var days = daysRaw === "" ? 0 : Number(daysRaw);
    var limit = limitRaw === "" ? 0 : Number(limitRaw);

    if (isNaN(days) || isNaN(limit)) {
      toast("天数和条数要填数字，留空表示跟随平台默认", "err");
      return;
    }

    var body = {
      enabled: sel.value === "inherit" ? null : sel.value === "on",
      days: days,
      max_per_run: limit
    };

    var btn = $('[data-dc="save"]', mask);
    if (btn) btn.disabled = true;

    try {
      mem.dreamCfg = await api("/api/v1/memories/dream/config", { method: "PUT", body: body });
      closeDreamConfigModal();
      toast("配置已保存");
      renderMemories();
    } catch (e) {
      toast("保存失败：" + errText(e), "err");
      if (btn) btn.disabled = false;
    }
  }

  function closeMemoryModal() {
    if (mem.mask) { mem.mask.remove(); mem.mask = null; }
    mem.editing = null;
  }

  function openMemoryModal(item) {
    closeMemoryModal();
    mem.editing = item || null;
    var editing = !!item;

    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true">' +
      '<div class="modal-head"><div>' +
      '<div class="modal-eyebrow">MEMORY</div>' +
      '<div class="modal-title">' + (editing ? "编辑记忆" : "新增记忆") + "</div>" +
      '</div><button class="modal-close" type="button" aria-label="关闭">×</button></div>' +
      '<div class="modal-body"><div class="form-grid">' +
      '<div class="field"><label>类型</label><select id="mem-f-type">' +
      MEM_TYPES.map(function (t) {
        var cur = editing && item.memory_type === t.id;
        return '<option value="' + esc(t.id) + '"' + (cur ? " selected" : "") + ">" + esc(t.label) + "</option>";
      }).join("") + "</select></div>" +
      (editing
        ? ((item.scope === "tenant")
          ? '<div class="field"><label>范围</label><input type="text" value="团队共享" disabled></div>'
          : '<div class="field"><label>范围</label><input type="text" value="仅自己" disabled></div>')
        : (mem.canTenant
          ? '<div class="field"><label>范围</label><select id="mem-f-scope">' +
            '<option value="self">仅自己</option><option value="tenant">团队共享</option></select></div>'
          : "")) +
      '<div class="field span2"><label>标题</label>' +
      '<input type="text" id="mem-f-title" maxlength="120" placeholder="一句话概括，例如：称呼、常用站点、报告风格" value="' +
      esc(editing ? item.title : "") + '">' +
      '<span class="help">同一类型下标题相同会覆盖旧内容，不会重复堆积。</span></div>' +
      '<div class="field span2"><label>内容</label>' +
      '<textarea id="mem-f-content" rows="4" maxlength="2000" placeholder="写清楚具体是什么，模型会原样读到">' +
      esc(editing ? item.content : "") + "</textarea></div>" +
      "</div></div>" +
      '<div class="modal-foot">' +
      '<button class="btn" type="button" data-mem="cancel">取消</button>' +
      '<button class="btn primary" type="button" data-mem="save">保存</button>' +
      "</div></div>";

    document.body.appendChild(mask);
    mem.mask = mask;

    $(".modal-close", mask).addEventListener("click", closeMemoryModal);
    $('[data-mem="cancel"]', mask).addEventListener("click", closeMemoryModal);
    $('[data-mem="save"]', mask).addEventListener("click", saveMemory);
    mask.addEventListener("click", function (ev) {
      if (ev.target === mask) closeMemoryModal();
    });

    $("#mem-f-title").focus();
  }

  async function saveMemory() {
    var title = ($("#mem-f-title").value || "").trim();
    var content = ($("#mem-f-content").value || "").trim();
    if (!title || !content) {
      toast("标题和内容都要填", "err");
      return;
    }

    var body = {
      memory_type: $("#mem-f-type").value,
      title: title,
      content: content
    };

    try {
      if (mem.editing) {
        await api("/api/v1/memories/" + encodeURIComponent(mem.editing.id), {
          method: "PATCH", body: body
        });
      } else {
        // 团队共享只有管理员能建；普通成员这里一律建自己的。
        body.scope = mem.canTenant && $("#mem-f-scope") ? $("#mem-f-scope").value : "self";
        await api("/api/v1/memories", { method: "POST", body: body });
      }
      closeMemoryModal();
      toast("已保存");
      await loadMemories();
    } catch (e) {
      toast("保存失败：" + errText(e), "err");
    }
  }

  /* ================= 观测 ================= */

  /* 观测页的三条设计取舍：
   *
   *   1. 列表只画汇总列，原文只在详情里取——一轮对话的 prompt 有几万字符，
   *      列表带上它就变成「打开一个页面等三秒」。
   *   2. 详情按阶段拆卡片（技能 / 工具 / 循环 / 答复 / 耗时），不做时间轴瀑布：
   *      排障先问的是「技能选中没、模型看到哪些工具、循环几趟」，而不是「哪条最长」。
   *   3. 流程图自己用 DOM 画盒子 + 连接线，不引图形库：这里只有 span 数组和
   *      parent_span_id，画一层缩进就够了，引一个 trace viewer 不划算。
   */

  var OBS_HOURS = [
    { v: 1, l: "1 小时" },
    { v: 24, l: "24 小时" },
    { v: 168, l: "7 天" },
    { v: 336, l: "14 天" }
  ];

  // `running` 只在这个窗口里才有意义：它表示「刚刚开始、还没收尾」。
  // `abandoned` 是跑到一半进程没了——留着能筛，才查得出某次重启影响了几轮。
  var OBS_STATUS = ["ok", "error", "timeout", "running", "awaiting_approval", "cancelled", "abandoned"];

  var obs = {
    hours: 24,
    status: "",
    q: "",
    skill: "",        // 按技能筛：筛的是「这一轮用没用到」，不是工具
    agent: "",        // 按智能体筛：点基准表的一行就是「只看它」
    // 两级详情，`detail` 优先：从运行页点进某一轮时两个都有值，
    // 返回时才回得到运行页而不是一路弹回列表。
    detail: null,     // 当前打开的 trace_id；null 表示不显示 trace 详情
    operation: null,  // 当前打开的运行标识；非 null 时详情页换成运行页
    payload: null,    // 详情响应
    openSpan: null,   // 当前展开原文的 span_id
    // 有委派的一轮可以在「编排流程」与「耗时泳道」之间切换。
    // 默认给流程：泳道读的是「谁在什么时候在干」，而排障第一问通常是「走了哪条路」。
    flowView: "flow",
    // 概览走势的原始分格。存下来是为了在容器变宽变窄时重画——
    // 重画不能再请求一次接口（换个窗宽就发一次请求，缩放窗口会把它打成洪水）。
    trend: null
  };

  /* ---- L0：现在谁在跑、谁在等人工 ----

     一条横条，5 秒轮一次。它只回答两个问题，不承载排障细节——点进某一条
     才去看那一轮怎么走的。没有在跑也没有在等时整条不渲染：
     留一个「0 个」的空壳只会占掉屏幕最值钱的那一行。 */

  var LIVE_INTERVAL = 5000;
  var liveTimer = null;
  // 概览走势的尺寸观察者。离开观测页时不主动断开也行（节点已销毁，
  // 观察者跟着失效），但重进一次就会多攒一个——所以进页面时先断上一次的。
  var trendRO = null;

  function stopLive() {
    if (liveTimer) {
      clearInterval(liveTimer);
      liveTimer = null;
    }
  }

  async function loadLive() {
    var box = $("#obs-live");
    if (!box) {
      // 已经离开观测页（render 清空了视图）。顺手把定时器收掉。
      stopLive();
      return;
    }

    try {
      var data = await api("/api/v1/observability/live");
      // 再判一次：请求返回时页面可能已经换走了，这时写进去会把别的页面的
      // 节点覆盖成一条横条——异步刷新最容易在这里出错。
      if (!$("#obs-live")) return;
      box.innerHTML = PAPRender.liveStrip(data);
      $$("[data-live-trace]", box).forEach(function (btn) {
        btn.addEventListener("click", function () {
          obs.detail = btn.getAttribute("data-live-trace");
          // 从横条进来的是「这一轮」，不是「上一次打开的那次运行」：
          // 不清掉 operation，返回按钮会弹回一条不相干的运行页。
          obs.operation = null;
          obs.payload = null;
          obs.openSpan = null;
          go("observe");
        });
      });
    } catch (e) {
      // 横条是附加信息，它挂了不该在页面上喊：主表格自己的错误会说明问题。
      box.innerHTML = "";
    }
  }

  function startLive() {
    stopLive();
    loadLive();
    liveTimer = setInterval(loadLive, LIVE_INTERVAL);
  }

  function fmtMs(value) {
    if (value === undefined || value === null) return "—";
    var n = Number(value);
    if (!isFinite(n)) return "—";
    if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 1 : 2) + " s";
    return Math.round(n) + " ms";
  }

  function fmtNum(value) {
    if (value === undefined || value === null) return "—";
    return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function statusChip(status) {
    // `running` 不是错误：它是「这一轮还没结束」。标成红色会让翻列表的人
    // 以为出了问题，而绝大多数时候只是刚点发送。
    var cls = status === "ok" ? "ok" : (status === "awaiting_approval" || status === "running" ? "warn" : "err");
    return '<span class="chip ' + cls + '">' + esc(status || "unknown") + "</span>";
  }

  function obsQuery(extra) {
    var parts = ["hours=" + obs.hours];
    if (obs.status) parts.push("status=" + encodeURIComponent(obs.status));
    if (obs.q) parts.push("q=" + encodeURIComponent(obs.q));
    if (obs.skill) parts.push("skill=" + encodeURIComponent(obs.skill));
    if (obs.agent) parts.push("agent=" + encodeURIComponent(obs.agent));
    (extra || []).forEach(function (p) { parts.push(p); });
    return parts.join("&");
  }

  async function mountObserve(root) {
    if (obs.detail) return mountObsDetail(root);
    if (obs.operation) return mountObsOperation(root);

    root.innerHTML = pageHead({
      eyebrow: PAGES.observe.eyebrow,
      title: PAGES.observe.title,
      desc: PAGES.observe.desc,
      actions: '<button class="btn" type="button" id="obs-refresh">' + ico("refresh") + "刷新</button>"
    }) +
      '<div id="obs-live"></div>' +
      '<div id="obs-off"></div>' +
      '<div class="card">' +
      cardHead("窗口概览", "grid", '<span class="chip" id="obs-scope">…</span>') +
      '<div class="stats" id="obs-stats">' + statSkeletons(6) + "</div>" +
      // 走势紧跟在数字下面：上面回答「多大」，这里回答「什么时候变成这样的」。
      // 放在同一张卡里而不是另起一张，是因为它们读的是同一批行、同一个窗口，
      // 分成两张卡会让人以为是两次统计。
      '<div class="obs-trend" id="obs-trend"></div>' +
      "</div>" +
      // 构成也放一张自己的卡：意图与工具是同一批轮次的两种切法，
      // 并排读才能对上「谁在用、用哪个工具」。
      '<div class="card">' +
      cardHead("窗口构成", "list", '<span class="muted" id="obs-dist-hint"></span>') +
      '<div class="obs-dist">' +
      '<div class="obs-dist-col"><div class="obs-dist-head">按意图</div>' +
      '<div id="obs-intent"><div class="skeleton" style="width:70%"></div></div></div>' +
      '<div class="obs-dist-col"><div class="obs-dist-head">热点工具</div>' +
      '<div id="obs-tools"><div class="skeleton" style="width:70%"></div></div></div>' +
      "</div>" +
      "</div>" +
      '<div class="card">' +
      cardHead("按智能体", "flow", '<span class="muted" id="obs-agent-hint"></span>') +
      '<div id="obs-agents"><div class="skeleton" style="width:50%"></div></div>' +
      "</div>" +
      // 委派关系放在「按智能体」之后：前者回答「谁在被用」，这一张回答
      // 「谁在使唤谁、哪条路走不通」。两张表看的是同一批数据的不同问题。
      '<div class="card">' +
      cardHead("委派关系", "layers", '<span class="muted" id="obs-topo-hint"></span>') +
      '<div id="obs-topo"><div class="skeleton" style="width:50%"></div></div>' +
      "</div>" +
      '<div class="card">' +
      cardHead("调用链", "activity") +
      '<div class="toolbar">' +
      '<div class="field" style="width:118px"><label>时间窗</label>' +
      '<select id="obs-hours">' + OBS_HOURS.map(function (h) {
        return '<option value="' + h.v + '"' + (h.v === obs.hours ? " selected" : "") + ">" + esc(h.l) + "</option>";
      }).join("") + "</select></div>" +
      '<div class="field" style="width:132px"><label>状态</label>' +
      '<select id="obs-status"><option value="">全部</option>' +
      OBS_STATUS.map(function (s) {
        return '<option value="' + esc(s) + '"' + (s === obs.status ? " selected" : "") + ">" + esc(s) + "</option>";
      }).join("") + "</select></div>" +
      '<div class="field" style="width:156px"><label>智能体</label>' +
      '<select id="obs-agent"><option value="">全部</option></select></div>' +
      '<div class="field" style="width:158px"><label>技能</label>' +
      '<select id="obs-skill"><option value="">全部</option></select></div>' +
      '<div class="field" style="flex:1;min-width:180px"><label>搜索原文</label>' +
      '<input id="obs-q" type="search" placeholder="记得那句话，不记得哪一轮（只在时间窗内匹配）" value="' + esc(obs.q) + '">' +
      "</div></div>" +
      '<div id="obs-body"><div class="skeleton" style="width:60%"></div></div>' +
      "</div>";

    $("#obs-refresh").addEventListener("click", function () { loadObs(); loadLive(); });

    $("#obs-hours").addEventListener("change", function () {
      obs.hours = Number(this.value);
      loadObs();
    });
    $("#obs-status").addEventListener("change", function () {
      obs.status = this.value;
      loadObs();
    });
    $("#obs-skill").addEventListener("change", function () {
      obs.skill = this.value;
      loadObs();
    });
    $("#obs-agent").addEventListener("change", function () {
      obs.agent = this.value;
      loadObs();
    });

    var timer = null;
    $("#obs-q").addEventListener("input", function () {
      var value = this.value;
      // 每敲一个字就扫一遍原文库太重，停手 350ms 再查。
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () { obs.q = value.trim(); loadObs(); }, 350);
    });

    // 横条不参与 await：它自己 5 秒一轮，等它反而让主内容晚一拍出现。
    startLive();

    // 收窄窗口、收起侧栏都会改内容宽度。观察容器本身而不是 window：
    // 侧栏折叠时 window 尺寸一点没变，靠 resize 事件等不到那一次重画。
    // 先断开上一次的：重复进观测页不该攒下一堆观察者。
    if (trendRO) trendRO.disconnect();
    trendRO = null;
    if (window.ResizeObserver) {
      var trendBox = $("#obs-trend");
      if (trendBox) {
        trendRO = new ResizeObserver(function () {
          paintObsTrend(false);
        });
        trendRO.observe(trendBox);
      }
    }

    await loadObs();
  }

  async function loadObs() {
    await Promise.all([loadObsOverview(), loadObsAgents(), loadObsTopology(), loadObsList()]);
  }

  /* 委派关系图（L2）。
     与上面两张表用同一个时间窗：一页上出现两套窗口，读数就没法互相对照了。
     窗口太短时图会是空的——空状态里写明了「调大时间窗」，而不是留一片白。 */
  async function loadObsTopology() {
    var box = $("#obs-topo");
    if (!box) return;
    try {
      var data = await api("/api/v1/observability/topology?hours=" + obs.hours);
      // 请求回来时用户可能已经切页，节点早就不在了。
      box = $("#obs-topo");
      if (!box) return;

      box.innerHTML = PAPRender.topologyView(data);

      var hint = $("#obs-topo-hint");
      if (hint) {
        var edgeCount = (data.edges || []).length;
        hint.textContent = data.restricted
          ? "需要 observability:read"
          : "近 " + data.hours + " 小时 · " + (edgeCount ? edgeCount + " 条边" : "没有委派");
      }
    } catch (e) {
      box.innerHTML = '<div class="banner err">委派关系加载失败：' + esc(errText(e)) + "</div>";
    }
  }

  /* 按智能体的运算基准。
     回答的是「要不要给这个 Agent 加配额 / 拆分工 / 调边界」——
     所以同一行里既要用量（轮次、token、耗时），也要治理信号（被拒、驳回）。
     名字是写入时的快照：Agent 删了照样出现在这里，只是状态显示「已删除」，
     历史归属不跟着改写。 */
  function agentBaselineState(item) {
    if (!item.status) return '<span class="chip">已删除</span>';
    var cls = item.status === "active" ? " ok" : (item.status === "draft" ? " warn" : "");
    var label = item.status === "active" ? "启用" : (item.status === "draft" ? "草稿" : "停用");
    return '<span class="chip' + cls + '">' + esc(label) + "</span>";
  }

  /* 用量折线：每一格是一段时间内的轮次，最后一格收在「现在」。
     用柱不用线——周级的样本常常是 0/3/7 这种稀疏值，连成线会画出一堆
     并不存在的「趋势」。被窗口起点截断的最旧一格降低不透明度，免得
     把「只覆盖了半天」读成「那半天用量少」。 */
  function agentSparkline(item) {
    var buckets = item.buckets || [];
    if (!buckets.length) return '<span class="muted">—</span>';

    var w = 88;
    var h = 22;
    var gap = buckets.length > 14 ? 1 : 2;
    var bw = Math.max(1, (w - gap * (buckets.length - 1)) / buckets.length);
    var max = 1;
    buckets.forEach(function (b) {
      if (b.traces > max) max = b.traces;
    });

    var pad = function (n) { return String(n).padStart(2, "0"); };
    var bars = buckets.map(function (b, i) {
      var x = i * (bw + gap);
      if (!b.traces) {
        // 空格也要有个基线刻度，否则「这一格没人用」和「这一格不存在」看起来一样。
        return '<rect class="zero" x="' + x.toFixed(2) + '" y="' + (h - 1) +
          '" width="' + bw.toFixed(2) + '" height="1"></rect>';
      }
      var bh = Math.max(1.5, (h * b.traces) / max);
      var d = new Date(b.t);
      var when = pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
        pad(d.getHours()) + ":" + pad(d.getMinutes());
      var tip = "截至 " + when + "：" + b.traces + " 轮";
      if (b.errors) tip += "，其中 " + b.errors + " 次出错";
      if (b.denials) tip += "，被挡 " + b.denials + " 次";
      if (b.partial) tip += "（这一格被窗口起点截断，只覆盖了一部分时间）";
      return '<rect class="bar' + (b.errors ? " has-err" : "") + (b.partial ? " clipped" : "") +
        '" x="' + x.toFixed(2) + '" y="' + (h - bh).toFixed(2) +
        '" width="' + bw.toFixed(2) + '" height="' + bh.toFixed(2) + '">' +
        "<title>" + esc(tip) + "</title></rect>";
    }).join("");

    return '<svg class="spark" width="' + w + '" height="' + h +
      '" viewBox="0 0 ' + w + " " + h + '" role="img" aria-label="用量走势">' + bars + "</svg>";
  }

  /* 「比上一窗口涨了多少」。算不出比例时给原因，而不是填 0——
     把「没法比」画成「持平」是这类报表最容易骗人的地方。 */
  function agentDelta(item) {
    if (item.change === null || item.change === undefined) {
      var why = {
        no_baseline: "上一窗口没有记录，除不出比例",
        created_after_baseline_start: "这个智能体在上一窗口开始之后才创建，那一段基线不完整",
        low_sample: "任一侧不足 5 轮，比例只是噪声"
      }[item.change_reason];
      if (!why) return '<span class="muted">—</span>';
      return '<span class="muted" title="' + esc(why) + '">不可比</span>';
    }

    var pct = item.change * 100;
    var tip = "上一窗口 " + item.previous_traces + " 轮 → 本窗口 " + item.traces + " 轮";
    if (!pct) return '<span class="muted" title="' + esc(tip) + '">持平</span>';

    var arrow = pct > 0 ? "↑" : "↓";
    return '<span class="delta' + (pct > 0 ? " up" : "") + '" title="' + esc(tip) + '">' +
      arrow + " " + esc(fmtPct(Math.abs(pct))) + "</span>";
  }

  function fillObsAgentFilter(agents) {
    var box = $("#obs-agent");
    if (!box) return;

    var names = agents.map(function (a) { return a.agent_name; });
    if (obs.agent && names.indexOf(obs.agent) < 0) {
      obs.agent = "";
      loadObsList();
    }

    box.innerHTML = '<option value="">全部</option>' + agents.map(function (a) {
      return '<option value="' + esc(a.agent_name) + '"' + (a.agent_name === obs.agent ? " selected" : "") + ">" +
        esc(a.display_name || a.agent_name) + "（" + a.traces + "）</option>";
    }).join("");
  }

  async function loadObsAgents() {
    var box = $("#obs-agents");
    if (!box) return;
    try {
      var data = await api("/api/v1/observability/agents?hours=" + obs.hours);
      fillObsAgentFilter(data.agents || []);

      var hint = $("#obs-agent-hint");
      if (hint) {
        var tail = data.unattributed_traces
          ? "，另有 " + data.unattributed_traces + " 轮没有记录智能体（存量）"
          : "";
        hint.textContent = "窗口内共 " + data.total_traces + " 轮" + tail;
      }

      if (!data.agents.length) {
        box.innerHTML = '<div class="empty"><strong>这个窗口里没有智能体在跑</strong>' +
          "<ul><li>窗口内还没有人发起对话；</li>" +
          "<li>更早的记录没有 `agent_name`（那时还没有这个概念），不会归到任何一行。</li></ul></div>";
        return;
      }

      box.innerHTML = '<table class="tbl"><thead><tr>' +
        "<th>智能体</th><th>状态</th><th>走势</th>" +
        '<th class="right">轮次 / 占比</th><th class="right">被委派</th>' +
        '<th class="right">较上一窗口</th>' +
        '<th class="right">错误率</th><th class="right">P95</th>' +
        '<th class="right">工具调用</th><th class="right">token 入/出</th>' +
        '<th class="right">被拒</th><th class="right">审批（驳回）</th><th>最近使用</th>' +
        "</tr></thead><tbody>" +
        data.agents.map(function (a) {
          // 点一行 = 只看它。回填到上面的筛选器，也能从筛选器切回「全部」。
          var approvals = a.approvals
            ? a.approvals + (a.approvals_rejected ? "（" + a.approvals_rejected + "）" : "")
            : "0";
          return '<tr class="clickable" data-obs-agent="' + esc(a.agent_name) + '">' +
            "<td>" + esc(a.display_name || a.agent_name) +
            ' <span class="muted mono">' + esc(a.agent_name) + (a.revision ? " v" + a.revision : "") + "</span></td>" +
            "<td>" + agentBaselineState(a) + "</td>" +
            "<td>" + agentSparkline(a) + "</td>" +
            '<td class="right mono">' + esc(fmtNum(a.traces)) +
            ' <span class="muted">' + esc(fmtPct(a.share * 100)) + "</span></td>" +
            // 被别的智能体叫去干活的次数。与轮次是两个数：委派的活儿跑在
            // 发起方那条 trace 里，所以它不会出现在这一行自己的轮次里。
            '<td class="right mono">' + (a.delegated_turns
              ? esc(fmtNum(a.delegated_turns))
              : '<span class="muted">0</span>') + "</td>" +
            '<td class="right mono">' + agentDelta(a) + "</td>" +
            '<td class="right mono"' + (a.awaiting_approval
              ? ' title="另有 ' + esc(String(a.awaiting_approval)) + ' 轮停在等人工，未计入错误率"'
              : "") + ">" +
            (a.traces ? esc(fmtPct(a.error_rate * 100)) : "—") + "</td>" +
            '<td class="right mono">' + esc(fmtMs(a.p95_duration_ms)) + "</td>" +
            '<td class="right mono">' + esc(fmtNum(a.tool_calls)) + "</td>" +
            '<td class="right mono">' + esc(fmtNum(a.prompt_tokens)) + " / " + esc(fmtNum(a.completion_tokens)) + "</td>" +
            '<td class="right mono">' + (a.denials
              ? '<span class="chip warn">' + esc(String(a.denials)) + "</span>"
              : '<span class="muted">0</span>') + "</td>" +
            '<td class="right mono">' + esc(approvals) + "</td>" +
            '<td class="muted nowrap">' + (a.last_started_at ? esc(fmtTime(a.last_started_at)) : "—") + "</td>" +
            "</tr>";
        }).join("") + "</tbody></table>" +
        '<div class="obs-foot muted">' +
        "「较上一窗口」比的是轮次，不是 token 或成本——后两者有缺报，" +
        "拿有缺口的量算比例会凭空多出涨幅；不可比时会写明原因，不填 0。" +
        "「被拒」是边界或权限挡下的次数，持续升高说明这个 Agent 的边界配窄了；" +
        "「审批（驳回）」持续被驳说明职责写宽了。" +
        "「被委派」是它被别的智能体叫去干活的次数，与轮次不能相加——" +
        "活儿干在发起方那条 trace 里，所以一个 Agent 可以 0 轮自己的对话、却被叫了 20 次。</div>";

      $$("#obs-agents tr[data-obs-agent]").forEach(function (row) {
        row.addEventListener("click", function () {
          obs.agent = row.getAttribute("data-obs-agent");
          loadObs();
        });
      });
    } catch (e) {
      box.innerHTML = '<div class="banner err">按智能体统计加载失败：' + esc(errText(e)) + "</div>";
    }
  }

  function fillObsSkills(list) {
    var box = $("#obs-skill");
    if (!box) return;

    // 保留当前选择：刷新后选项可能没了（窗口挪动），此时回落到「全部」。
    var names = list.map(function (item) { return item.key; });
    if (obs.skill && names.indexOf(obs.skill) < 0) {
      obs.skill = "";
      loadObsList();
    }

    box.innerHTML = '<option value="">全部</option>' + names.map(function (name) {
      var count = 0;
      list.forEach(function (item) { if (item.key === name) count = item.count; });
      return '<option value="' + esc(name) + '"' + (name === obs.skill ? " selected" : "") + ">" +
        esc(name) + "（" + count + "）</option>";
    }).join("");
  }

  /* 走势图按容器的真实像素宽度画。

     为什么不在 render.js 里靠 viewBox 拉伸：660 宽的坐标系被拉到 1200 时，
     10.5px 的轴标签会一起放大到 19px——一张运维图上出现比正文还大的字。
     量一次真实宽度再画，字就一直是字。 */
  function paintObsTrend(force) {
    var box = $("#obs-trend");
    if (!box || !obs.trend) return;
    var width = Math.round(box.clientWidth || 0);
    // 容器还没布局（宽度 0）时先不画：那时画出来是一张被压到最小宽度的扁图，
    // 等布局完成后的那次重画再补上。
    if (!width) return;
    // 宽度没变就不重画。ResizeObserver 会连着触发好几次，每次都重建 DOM
    // 会让 tooltip 在鼠标下面闪。
    if (!force && box.getAttribute("data-w") === String(width)) return;
    box.setAttribute("data-w", String(width));
    box.innerHTML = PAPRender.obsTrend(obs.trend.buckets, {
      width: width,
      bucketHours: obs.trend.bucketHours
    });
  }

  // 意图的取值来自 `ObsTrace.intent`，是给机器看的枚举。界面上换成人话：
  // 「写操作（需审批）」比 write_tool 更快让人明白为什么有一格是黄的。
  var INTENT_CN = {
    chat: "对话",
    read_tool: "只读工具",
    write_tool: "写操作（需审批）",
    clarify: "反问澄清",
    error: "失败",
    "—": "未记录"
  };

  function paintObsDist(data) {
    var intentBox = $("#obs-intent");
    var toolBox = $("#obs-tools");
    if (!intentBox || !toolBox) return;

    intentBox.innerHTML = PAPRender.obsBars(
      (data.by_intent || []).map(function (item) {
        return {
          label: INTENT_CN[item.key] || item.key,
          value: item.count,
          // 失败单独一色：它在构成里不是「另一种用途」，而是「这些没做成」。
          tone: item.key === "error" ? "#c76a61" : "#9dc3b2"
        };
      }),
      { empty: "窗口内没有记录意图。" }
    );

    // 工具关掉百分比：分母是前 8 名的调用次数之和，不是全部工具调用，
    // 算出来的「占比」会凭空变大，读的人却会当成占全部的比例。
    //
    // 出错的那一截叠在条的右端而不是整条涂红：一个 14 次调用里错 2 次的工作，
    // 整条红会读成「这个工具一直失败」，而它 86% 是成的。条子总长仍是调用次数，
    // 红色只占它该占的那一小段。
    var toolRows = data.top_tools || [];
    var toolErrors = toolRows.reduce(function (sum, item) {
      return sum + (item.errors || 0);
    }, 0);
    toolBox.innerHTML = PAPRender.obsBars(
      toolRows.map(function (item) {
        return {
          label: item.tool_name,
          value: item.calls,
          parts: [
            { value: item.calls - (item.errors || 0), color: "#9dc3b2" },
            { value: item.errors || 0, color: "#c76a61" }
          ],
          note: item.errors ? "其中 " + item.errors + " 次出错" : "没有出错"
        };
      }),
      { empty: "窗口内没有工具调用。", share: false }
    ) + (toolErrors
      ? '<div class="obs-legend"><span><i style="background:#9dc3b2"></i>完成</span>' +
        '<span><i style="background:#c76a61"></i>出错</span></div>'
      : "");

    var hint = $("#obs-dist-hint");
    if (hint) {
      hint.textContent = "意图按轮次计（" + data.traces + " 轮）；工具按调用次数取前 8";
    }
  }

  async function loadObsOverview() {
    var box = $("#obs-stats");
    if (!box) return;
    try {
      var data = await api("/api/v1/observability/overview?hours=" + obs.hours);
      // 请求返回时页面可能已经换走：那时写进去会把别的页面覆盖成一张概览。
      box = $("#obs-stats");
      if (!box) return;
      var scopeChip = $("#obs-scope");
      if (scopeChip) {
        scopeChip.textContent = data.scope === "tenant" ? "整个租户" : "仅本人";
        scopeChip.title = data.scope === "tenant"
          ? "当前身份具备 observability:read，可见本租户全部 trace"
          : "没有 observability:read，只能看到自己发起的对话";
      }
      box.innerHTML = [
        stat("对话轮次", fmtNum(data.traces)),
        // 「等人工」要从错误率里摘出来，但绝不能就这么消失：
        // 那批轮次停在审批上，脚注是它们唯一的去处。
        //
        // 措辞是「曾停在」而不是「在等」：审批处理完之后 trace 仍保留
        // `awaiting_approval`（记录当时的结论，不回写历史），所以这个数
        // 只代表窗口内发生过多少次暂停。**此刻**还有谁在等，看上面的横条——
        // 那个数是查 PENDING 单子得来的。说成「在等」会让人以为系统卡住了。
        stat(
          "错误率",
          data.traces ? (data.error_rate * 100).toFixed(1) + "%" : "—",
          false,
          data.awaiting_approval ? "窗口内 " + data.awaiting_approval + " 轮曾停在等人工" : ""
        ),
        stat("平均耗时", fmtMs(data.avg_duration_ms)),
        stat("P95 耗时", fmtMs(data.p95_duration_ms)),
        stat("模型调用", fmtNum(data.llm_calls)),
        stat("工具调用", fmtNum(data.tool_calls)),
        stat("输入 token", fmtNum(data.prompt_tokens)),
        stat("输出 token", fmtNum(data.completion_tokens))
      ].join("");

      // 走势与构成是附加读数：它们挂了不该把已经画出来的八个数字一起抹掉，
      // 所以各自兜住异常，只在自己的位置上留一句说明。
      obs.trend = {
        buckets: data.buckets || [],
        bucketHours: data.bucket_hours || 1
      };
      paintObsTrend(true);
      paintObsDist(data);

      fillObsSkills(data.by_skill || []);

      var off = $("#obs-off");
      if (off) {
        off.innerHTML = data.enabled
          ? ""
          : '<div class="banner warn"><strong>观测已关闭</strong>' +
            "当前进程 OBS_ENABLED=false，不会产生新数据。下面是历史上开启期间留下的记录；" +
            "要看新一轮的链路，把它改成 true 并重启服务。</div>";
      }
    } catch (e) {
      box.innerHTML = '<div class="banner err">概览加载失败：' + esc(errText(e)) + "</div>";
      // 走势与构成还留着上一轮的数据：窗口已经换了，旧图上的刻度却是新的，
      // 那比空白更容易读错。一并清掉。
      obs.trend = null;
      var stale = $("#obs-trend");
      if (stale) stale.innerHTML = "";
      var intentBox = $("#obs-intent");
      if (intentBox) intentBox.innerHTML = "";
      var toolBox = $("#obs-tools");
      if (toolBox) toolBox.innerHTML = "";
    }
  }

  async function loadObsList() {
    var box = $("#obs-body");
    if (!box) return;
    box.innerHTML = '<div class="skeleton" style="width:60%"></div>';
    try {
      var data = await api("/api/v1/observability/traces?" + obsQuery());
      if (!data.items.length) {
        box.innerHTML = '<div class="empty"><strong>这个窗口里没有记录</strong>' +
          "<ul><li>时间窗内还没有人发起对话，或者观测开关是关的；</li>" +
          "<li>没有 observability:read 时只显示你自己的 trace；</li>" +
          "<li>超过保留期（14 天，错误 30 天）的记录会被清理。</li></ul></div>";
        return;
      }

      box.innerHTML = '<div class="tbl-scroll"><table class="tbl"><thead><tr>' +
        "<th>开始时间</th><th>状态</th><th>意图</th><th>智能体</th><th>技能</th><th>主工具</th>" +
        '<th class="right">耗时</th><th class="right">span</th>' +
        '<th class="right">token 入/出</th><th>trace</th>' +
        "</tr></thead><tbody>" +
        data.items.map(function (t) {
          return '<tr class="clickable" data-trace="' + esc(t.trace_id) + '">' +
            '<td class="muted nowrap">' + esc(fmtTime(t.started_at)) + "</td>" +
            "<td>" + statusChip(t.status) +
            (t.error_category ? ' <span class="chip err">' + esc(t.error_category) + "</span>" : "") + "</td>" +
            '<td class="muted">' + esc(t.intent || "—") + "</td>" +
            "<td>" + (t.agent_name ? '<span class="chip">' + esc(t.agent_name) + "</span>" : '<span class="muted">—</span>') + "</td>" +
            "<td>" + (t.skill_name ? '<span class="chip">' + esc(t.skill_name) + "</span>" : '<span class="muted">—</span>') + "</td>" +
            '<td class="mono">' + esc(t.tool_name || "—") + "</td>" +
            '<td class="right mono">' + esc(fmtMs(t.duration_ms)) + "</td>" +
            '<td class="right mono">' + esc(String(t.span_count)) + "</td>" +
            '<td class="right mono">' + esc(fmtNum(t.prompt_tokens)) + " / " + esc(fmtNum(t.completion_tokens)) + "</td>" +
            '<td class="mono muted">' + esc(t.trace_id.slice(0, 8)) + "</td></tr>";
        }).join("") + "</tbody></table></div>" +
        '<div class="obs-foot muted">共 ' + esc(String(data.total)) + " 条，显示最近 " +
        esc(String(data.items.length)) + " 条。</div>";

      $$("#obs-body tr[data-trace]").forEach(function (row) {
        row.addEventListener("click", function () {
          obs.detail = row.getAttribute("data-trace");
          obs.operation = null;
          obs.payload = null;
          obs.openSpan = null;
          go("observe");
        });
      });
    } catch (e) {
      box.innerHTML = '<div class="banner err">加载失败：' + esc(errText(e)) + "</div>";
    }
  }

  function prettyContent(raw) {
    if (!raw) return "";
    try {
      return JSON.stringify(JSON.parse(raw), null, 2);
    } catch (e) {
      return String(raw);
    }
  }

  /* ---- 编排流程 ----
     「耗时」由上面的分步卡片回答，流程图只管「走了哪条路」：谁调用了谁、
     工具回来后是不是又规划了一次。所以这里按开始时间把树摊平成序列，
     缩进只表示归属，不合并同名节点——回路里同一个节点出现两次，正是要看见的那件事。 */

  var FLOW_X0 = 6;      // 第一条连接线的横坐标
  var FLOW_STEP = 18;   // 每层缩进
  var FLOW_MAX = 6;     // 缩进上限，防止坏数据把盒子挤出卡片
  var FLOW_ROUND = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨"];

  // span 名是给日志看的，流程图上换成中文：读「规划 → 执行工具 → 规划」比
  // 读 plan_request / execute_read_tool 快得多。
  var SPAN_NAME_CN = {
    "graph.invoke": "本轮编排",
    "prepare_context": "准备上下文",
    "begin_turn": "读取会话",
    "plan_request": "规划",
    "execute_read_tool": "执行工具",
    "retrieve_knowledge": "检索知识库",
    "create_and_wait_approval": "等待审批",
    "respond": "组织回答",
    "persist_state": "保存状态",
    "llm.call": "模型调用",
    "tool.call": "工具调用",
    "persist.user_message": "落库用户消息",
    "persist.assistant_message": "落库回答"
  };

  var SPAN_KIND_CN = {
    graph: "编排", node: "节点", llm: "模型", tool: "工具", db: "落库", retrieval: "检索"
  };

  function flowLabel(span) {
    var base = SPAN_NAME_CN[span.name] || span.name;
    var attrs = span.attrs || {};
    // 工具调用光写「工具调用」没有信息量，要的是工具的本地名。
    if (span.name === "tool.call" && attrs.tool) return "工具 · " + attrs.tool;
    if (span.name === "llm.call" && Number(attrs.attempt) > 1) return base + "（重试）";
    return base;
  }

  function flowPlan(spans) {
    var byId = {};
    var order = {};
    var repeats = {};
    spans.forEach(function (s, i) {
      byId[s.span_id] = s;
      order[s.span_id] = i;
      repeats[s.name] = (repeats[s.name] || 0) + 1;
    });

    var kids = {};
    var roots = [];
    spans.forEach(function (s) {
      var parent = s.parent_span_id && byId[s.parent_span_id] ? s.parent_span_id : null;
      // 自引用或父节点缺失都当根节点：宁可画在顶层，也不要让它在页面上消失。
      if (parent && parent !== s.span_id) {
        (kids[parent] = kids[parent] || []).push(s);
      } else {
        roots.push(s);
      }
    });

    function byTime(a, b) {
      var d = new Date(a.started_at).getTime() - new Date(b.started_at).getTime();
      if (d) return d;
      return order[a.span_id] - order[b.span_id];
    }
    roots.sort(byTime);
    Object.keys(kids).forEach(function (key) { kids[key].sort(byTime); });

    var rows = [];
    var used = {};
    var nth = {};

    function walk(span, depth, rails, isLast) {
      if (used[span.span_id]) return;
      used[span.span_id] = true;

      nth[span.name] = (nth[span.name] || 0) + 1;
      rows.push({
        span: span,
        depth: Math.min(depth, FLOW_MAX),
        rails: rails,
        last: isLast,
        // 只给重复出现的节点标序号：一次就够的节点标上「①」只是噪音。
        round: repeats[span.name] > 1 ? nth[span.name] : 0
      });

      var children = (kids[span.span_id] || []).filter(function (c) { return !used[c.span_id]; });
      // 子级要继承「祖先这一列是否还有后续兄弟」，否则连线会在子树上断掉。
      var childRails = rails.concat([!isLast]);
      children.forEach(function (child, i) {
        walk(child, depth + 1, childRails, i === children.length - 1);
      });
    }

    roots.forEach(function (root, i) {
      walk(root, 0, [], i === roots.length - 1);
      if (i < roots.length - 1) rows.push({ gap: true });
    });

    // 环状父引用会留下走不到的节点。它们不正常，但不能凭空少几个 span。
    spans.forEach(function (span) {
      if (used[span.span_id]) return;
      rows.push({ gap: true });
      walk(span, 0, [], true);
    });

    return rows;
  }

  function renderFlow(rows) {
    var out = "";
    rows.forEach(function (row) {
      if (row.gap) {
        out += '<div class="fl-gap" aria-hidden="true"><span class="fl-arrow"></span></div>';
        return;
      }

      var span = row.span;
      var depth = row.depth;
      var html = '<div class="fl-row' + (depth === 0 ? " d0" : "") + '" data-span="' +
        esc(span.span_id) + '">';

      // 祖先列的竖线：只有「后面还有兄弟」的那一列才需要继续往下画。
      for (var d = 0; d < depth - 1; d += 1) {
        if (!row.rails[d]) continue;
        html += '<span class="fl-rail" style="left:' + (FLOW_X0 + d * FLOW_STEP) + 'px"></span>';
      }
      if (depth > 0) {
        var x = FLOW_X0 + (depth - 1) * FLOW_STEP;
        // 最后一个子项只需画到盒子中线，多出来的尾巴会让「这一支到头了」变模糊。
        html += '<span class="fl-rail' + (row.last ? " tail" : "") +
          '" style="left:' + x + 'px"></span>' +
          '<span class="fl-elbow" style="left:' + x + 'px;width:' + (FLOW_STEP - 6) + 'px"></span>';
      }

      html += '<div class="fl-box k-' + esc(span.kind) + (span.status === "ok" ? "" : " bad") + '"' +
        ' style="margin-left:' + (depth * FLOW_STEP) + 'px">' +
        '<span class="fl-dot k-' + esc(span.kind) + '"></span>' +
        '<span class="fl-main">' +
        '<span class="fl-name">' + esc(flowLabel(span)) + "</span>" +
        (row.round ? '<span class="fl-round" title="第 ' + esc(String(row.round)) + ' 次">' +
          (FLOW_ROUND[row.round - 1] || String(row.round)) + "</span>" : "") +
        "</span>" +
        '<span class="fl-ms mono">' + esc(fmtMs(span.duration_ms)) + "</span>" +
        "</div></div>";
      out += html;
    });
    return out;
  }

  function flowLegend(rows) {
    var seen = {};
    var items = [];
    rows.forEach(function (row) {
      if (row.gap || seen[row.span.kind]) return;
      seen[row.span.kind] = true;
      items.push('<span class="fl-lg"><i class="fl-dot k-' + esc(row.span.kind) + '"></i>' +
        esc(SPAN_KIND_CN[row.span.kind] || row.span.kind) + "</span>");
    });
    return '<div class="fl-legend">' + items.join("") + "</div>";
  }

  /* ---- 详情：按阶段拆 ----
     瀑布图回答「哪一段慢」，但排障时最先问的不是这个。真正要问的是：
     技能有没有选中、注入了什么、模型当时看到哪些工具、工具回来后循环了几趟、
     最后说了什么。所以详情按阶段做卡片，耗时单独一张聚合表；
     「走了哪条路」仍由编排流程图回答，两者不重复。 */

  function fmtAgo(iso) {
    if (!iso) return "—";
    var at = new Date(iso).getTime();
    if (isNaN(at)) return "—";
    var diff = Date.now() - at;
    if (diff < 0) return fmtTime(iso);
    if (diff < 60000) return "刚刚";
    if (diff < 3600000) return Math.floor(diff / 60000) + " 分钟前";
    if (diff < 86400000) return Math.floor(diff / 3600000) + " 小时前";
    if (diff < 7 * 86400000) return Math.floor(diff / 86400000) + " 天前";
    return fmtTime(iso);
  }

  function obsStage(opts) {
    return '<section class="obs-stage">' +
      '<div class="obs-stage-head">' +
      '<span class="obs-stage-dot' + (opts.tone ? " " + opts.tone : "") + '"></span>' +
      "<h3>" + esc(opts.title) + "</h3>" +
      '<span class="obs-stage-chips">' + (opts.chips || "") + "</span>" +
      "</div>" +
      (opts.sub ? '<p class="obs-stage-sub">' + opts.sub + "</p>" : "") +
      (opts.body || "") +
      "</section>";
  }

  function obsBox(head, meta, body) {
    return '<div class="obs-box">' +
      '<div class="obs-box-head"><span class="obs-box-title">' + head + "</span>" +
      '<span class="obs-box-meta">' + (meta || "") + "</span></div>" +
      (body || "") +
      "</div>";
  }

  function obsStrip(left, right) {
    return '<div class="obs-strip">' + left + (right || "") + "</div>";
  }

  function obsEmpty(text) {
    return '<div class="empty sm">' + esc(text) + "</div>";
  }

  function planSpan(spans) {
    for (var i = 0; i < spans.length; i += 1) {
      if (spans[i].kind === "llm") return spans[i];
    }
    return null;
  }

  // 模型当时看到的工具清单。存在 llm.call 的 attrs 里（tools.names），
  // 拿不到就是这一轮早于本次升级——不要用当前注册表补，那会显示成「当时看过」。
  function planToolNames(spans) {
    for (var i = 0; i < spans.length; i += 1) {
      if (spans[i].kind !== "llm") continue;
      var raw = (spans[i].attrs || {})["tools.names"];
      if (raw) {
        return String(raw).split(",").filter(function (name) { return !!name; });
      }
    }
    return [];
  }

  // 小于 1% 的别显示成 0%——那会让人以为这一项真的不耗时。
  function fmtPct(pct) {
    if (pct > 0 && pct < 1) return pct.toFixed(1) + "%";
    return Math.round(pct) + "%";
  }

  function sumMs(list) {
    return list.reduce(function (acc, span) {
      return acc + (Number(span.duration_ms) || 0);
    }, 0);
  }

  function agentStage(data) {
    var attrs = data.attrs || {};
    var name = data.trace ? data.trace.agent_name : null;
    if (!name) {
      return obsStage({
        tone: "idle",
        title: "智能体",
        chips: '<span class="chip">未记录</span>',
        sub: "这一轮早于「智能体」上线（或走了平台基线），没有 agent 维度。" +
          "按名字筛链路时也会跳过这类存量记录。"
      });
    }

    var rev = attrs.agent_revision;
    var chips = '<span class="chip">' + esc(name) + "</span>" +
      (rev ? '<span class="chip">v' + esc(String(rev)) + "</span>" : "");

    var hidden = attrs.agent_hidden_tools;
    var sub;
    if (hidden && hidden.length) {
      var preview = hidden.slice(0, 8).map(function (n) {
        return '<span class="chip">' + esc(n) + "</span>";
      }).join(" ");
      sub = "这个智能体的边界把 <strong>" + esc(String(hidden.length)) +
        "</strong> 个工具挡在目录之外：<br>" + preview +
        (hidden.length > 8 ? " 等" : "") +
        "<br>它们不会产生「拒绝」记录——模型根本看不到，只会换种说法说做不了。" +
        "如果这里面有它本该能用的工具，去「配置 → 智能体」放宽边界。";
    } else {
      sub = "本职工作说明已拼进本轮 system prompt；没有工具因边界被裁掉。";
    }

    return obsStage({ tone: "ok", title: "智能体", chips: chips, sub: sub });
  }

  function skillStage(data) {
    var s = data.skill;
    if (!s) {
      return obsStage({
        tone: "idle",
        title: "Skill 选择",
        chips: '<span class="chip">未命中</span>',
        sub: "这一轮没有技能命中，模型按默认规则规划，给它的工具目录没有收窄。" +
          "要让它命中，给技能配触发词，或在消息开头写 /技能名。"
      });
    }

    var way = s.matched_by === "explicit"
      ? "用户显式调用 /" + s.name
      : (s.matched_by === "keyword"
        ? "触发词" + (s.keyword ? "「" + s.keyword + "」" : "") + "命中"
        : "命中方式未记录");
    var sub = "命中 <strong>" + esc(s.name) + "</strong>，" + esc(way) +
      "，作业指令已拼到本轮 system prompt 末尾。";
    if (!s.exists) {
      sub += " 技能已被删除：下面是当时的注入记录，命中统计随删除一并清空。";
    } else if (s.enabled === false) {
      sub += " 技能现在是停用状态，只影响之后的对话。";
    }

    var boxes = "";

    var lenMeta;
    if (s.instruction_chars === null || s.instruction_chars === undefined) {
      lenMeta = "长度未记录（这一轮早于本次升级）";
    } else {
      lenMeta = fmtNum(s.instruction_chars) +
        (s.instruction_limit ? " / " + fmtNum(s.instruction_limit) : "") + " 字符 · " +
        (s.truncated ? "已截断" : "完整");
    }
    boxes += obsBox("作业指令", lenMeta, obsStrip(
      '<span class="muted">已加载到本轮上下文</span>',
      s.truncated ? '<span class="chip warn">超限截断</span>' : '<span class="chip ok">完整注入</span>'
    ));

    var before = s.tools_before;
    var after = s.tools_after;
    if (before === null || before === undefined || after === null || after === undefined) {
      boxes += obsBox("工具白名单", "未记录",
        obsStrip('<span class="muted">这一轮没有记录收窄前后的工具数</span>'));
    } else if (before === after) {
      boxes += obsBox("工具白名单", fmtNum(before) + " 个工具 · 未收窄",
        obsStrip('<span class="muted">技能没配 allowed_tools，模型看到的就是完整目录</span>'));
    } else {
      // 清单不在这里再列一遍：下一张「准备工具」列的就是收窄后的目录，
      // 同一份名单出现两次，读的人还得先比对一下是不是同一个。
      boxes += obsBox(
        "工具白名单",
        fmtNum(before) + " → " + fmtNum(after) + " 个工具",
        obsStrip(
          '<span class="muted">收窄后模型只能看到这 ' + esc(String(after)) +
          " 个，清单见下面「准备工具」</span>",
          after === 0 ? '<span class="chip warn">纯指令技能</span>' : ""
        )
      );
    }

    boxes += obsBox(
      "命中统计",
      s.exists
        ? "近 7 天 " + fmtNum(s.hits_7d) + " 次 · 最近 " + esc(fmtAgo(s.last_hit_at))
        : "命中记录已随技能删除",
      obsStrip(
        '<span class="muted">' +
        (s.recorded ? "这一轮已写进 skill_hits" : "这一轮没有留下命中记录") + "</span>",
        s.recorded ? '<span class="chip ok">已记录</span>' : '<span class="chip warn">未记录</span>'
      )
    );

    return obsStage({
      tone: s.exists === false || s.enabled === false ? "warn" : "ok",
      title: "Skill 选择",
      chips: '<span class="chip ok">已命中</span>' +
        '<span class="chip">' + esc(s.name) + "</span>",
      sub: sub,
      body: boxes
    });
  }

  function toolsStage(data, spans) {
    var attrs = (planSpan(spans) || {}).attrs || {};
    var names = planToolNames(spans);
    var cut = !!attrs["tools.names_cut"];

    // 0 个是合法值（纯指令技能把目录清空了），不能和「没记录」混成一件事：
    // 用 undefined 判断在不在，不用真假判断。
    var recorded = attrs["tools.count"] !== undefined && attrs["tools.count"] !== null;
    if (!recorded) {
      return obsStage({
        tone: "idle",
        title: "准备工具",
        chips: '<span class="chip">未记录</span>',
        sub: "这一轮没有记录工具目录（早于本次升级），只能到规划调用的原文里翻。"
      });
    }

    var count = Number(attrs["tools.count"]) || 0;
    // 名字清单被截断时 count 才是准的；老记录有 count 没 names，退回用名字个数。
    var total = count || names.length;
    var s = data.skill;
    var sub = "本轮给模型的工具目录共 <strong>" + esc(String(total)) + "</strong> 个。";
    if (s && s.tools_before !== null && s.tools_before !== undefined &&
        s.tools_after !== null && s.tools_after !== undefined &&
        s.tools_before !== s.tools_after) {
      sub += " 技能把 " + esc(String(s.tools_before)) + " 个收窄到 " +
        esc(String(s.tools_after)) + " 个——这是从目录里真的拿掉了，" +
        "不是提示词里写一句「只能用某个工具」。";
      if (total === 0) sub += " 一个工具都不给，是纯指令技能。";
    } else {
      sub += " 没有技能收窄，模型看到的就是这个身份的全部可用工具。";
    }

    var body = '<div class="obs-box">' +
      '<div class="obs-box-head"><span class="obs-box-title">可用工具</span>' +
      '<span class="obs-box-meta">' + esc(String(total)) + " 个</span></div>" +
      '<div class="obs-strip"><span class="chip">' + esc(String(total)) + " 个可用工具</span>" +
      (names.length
        ? '<button class="linkish obs-tools-toggle" type="button">展开查看全部工具</button>'
        : "") + "</div>" +
      (names.length || cut
        ? '<div class="obs-chips" id="obs-tool-list" hidden>' +
        (names.length
          ? names.map(function (name) {
            return '<span class="chip">' + esc(name) + "</span>";
          }).join("")
          : '<span class="muted">清单为空</span>') +
        (cut ? '<span class="muted">清单过长，已截断</span>' : "") +
        "</div>"
        : "") +
      "</div>";

    return obsStage({
      tone: "ok",
      title: "准备工具",
      chips: '<span class="chip">' + esc(String(total)) + " 个工具</span>",
      sub: sub,
      body: body
    });
  }

  // 一次「规划 → 工具 → 再规划」算一趟。工具归属它之前最近的那次规划，
  // 所以排序按时间，不按 span 树——工具是挂在 execute_read_tool 节点下的。
  function loopRounds(spans) {
    var ordered = spans.slice().sort(function (a, b) {
      var d = new Date(a.started_at).getTime() - new Date(b.started_at).getTime();
      return d || ((a.seq || 0) - (b.seq || 0));
    });

    var rounds = [];
    ordered.forEach(function (span) {
      if (span.kind === "llm") {
        rounds.push({ llm: span, tools: [] });
      } else if (span.kind === "tool" || span.kind === "mcp") {
        if (!rounds.length) rounds.push({ llm: null, tools: [] });
        rounds[rounds.length - 1].tools.push(span);
      }
    });
    return rounds;
  }

  function loopStage(t, spans) {
    var llms = spans.filter(function (s) { return s.kind === "llm"; });
    var tools = spans.filter(function (s) { return s.kind === "tool" || s.kind === "mcp"; });

    if (!llms.length && !tools.length) {
      return obsStage({
        tone: "idle",
        title: "模型与工具循环",
        chips: '<span class="chip">未发生</span>',
        sub: "这一轮没有调用模型，也没有调用工具。"
      });
    }

    var llmMs = sumMs(llms);
    var toolMs = sumMs(tools);
    var okTools = tools.filter(function (s) { return (s.status || "ok") === "ok"; }).length;

    var sub = "本轮调用模型 " + llms.length + " 次、执行工具 " + tools.length + " 次" +
      (tools.length ? "（成功 " + okTools + " 次）" : "") +
      "；模型请求合计 " + fmtMs(llmMs) + "，工具执行合计 " + fmtMs(toolMs) + "。";
    if (llms.length > 1 && tools.length) {
      sub += " 模型被调用了不止一次，说明工具结果回灌后又规划了一轮。";
    }

    var body = loopRounds(spans).map(function (round, i) {
      var out = '<div class="obs-round">';
      if (round.llm) {
        var attrs = round.llm.attrs || {};
        var hasTokens = attrs.prompt_tokens !== undefined || attrs.completion_tokens !== undefined;
        var tokens = hasTokens
          ? "入 " + fmtNum(attrs.prompt_tokens) + " / 出 " + fmtNum(attrs.completion_tokens)
          : "token 未记录";
        var attempt = Number(attrs.attempt) || 1;
        var purpose = attrs.purpose === "plan" || !attrs.purpose ? "规划" : "模型调用";
        out += '<div class="obs-round-line" data-span="' + esc(round.llm.span_id) + '">' +
          '<span class="obs-round-n">' + esc(FLOW_ROUND[i] || String(i + 1)) + "</span>" +
          '<span class="obs-round-name">' + esc(purpose) +
          (attempt > 1 ? "（重试 " + attempt + "）" : "") + "</span>" +
          '<span class="obs-round-meta mono">' + esc(fmtMs(round.llm.duration_ms)) +
          " · " + esc(tokens) + "</span>" +
          "</div>";
      }
      if (round.tools.length) {
        out += '<div class="obs-round-tools">' + round.tools.map(function (tool) {
          var ok = (tool.status || "ok") === "ok";
          var name = (tool.attrs || {}).tool || "tool";
          return '<div class="obs-round-tool" data-span="' + esc(tool.span_id) + '">' +
            '<span class="mono obs-round-toolname">' + esc(name) + "</span>" +
            '<span class="chip ' + (ok ? "ok" : "err") + '">' + (ok ? "成功" : "失败") + "</span>" +
            '<span class="obs-round-meta mono">' + esc(fmtMs(tool.duration_ms)) + "</span>" +
            "</div>";
        }).join("") + "</div>";
      }
      return out + "</div>";
    }).join("");

    return obsStage({
      tone: tools.length && okTools < tools.length ? "warn" : "ok",
      title: "模型与工具循环",
      chips: '<span class="chip">模型 ' + esc(String(llms.length)) + " 次</span>" +
        '<span class="chip">工具 ' + esc(String(tools.length)) + " 次</span>",
      sub: sub,
      body: body
    });
  }

  function answerStage(data, t, spans) {
    var respond = null;
    var model = "";
    spans.forEach(function (span) {
      if (span.name === "respond" && !respond) respond = span;
      var m = (span.attrs || {}).model;
      if (!model && span.kind === "llm" && m) model = String(m);
    });

    var text = data.output_text || "";
    var meta = [];
    if (model) meta.push("模型 " + model);
    meta.push("回答 " + fmtNum(text.length) + " 字符");
    meta.push("输出 token " + fmtNum(t.completion_tokens));

    var sub = "最终答复在 respond 节点定型：" + esc(meta.join(" · ")) + "。";
    if (!text) {
      sub = "这一轮没有记录最终回答：可能 capture 模式不是 full，或流程在回答前就结束了。";
    }

    return obsStage({
      tone: t.status === "ok" ? "ok" : "err",
      title: "最终答复",
      chips: statusChip(t.status) +
        (respond ? '<span class="chip">' + esc(fmtMs(respond.duration_ms)) + "</span>" : ""),
      sub: sub,
      body: text
        ? '<div class="rich obs-answer">' + richText(text) + "</div>" +
          '<details class="obs-raw"><summary>回答原文（Markdown）</summary>' +
          '<pre class="obs-pre">' + esc(text) + "</pre></details>"
        : obsEmpty("没有可显示的回答原文")
    });
  }

  // 链路耗时：只聚合 node 级 span。llm / tool 是节点的子级，
  // 一起加会把同一段耗时算两遍。
  //
  // 它与编排流程并排：一个回答「时间花在哪」，一个回答「走了哪条路」，
  // 都是看结构用的，放在一起比上下铺省一屏；所以这里出的是整张 card，
  // 与「编排流程」用同一套卡头，并排时才不会一个胖一个瘦。
  function timingCard(t, spans) {
    var total = Number(t.duration_ms) || 0;
    var order = [];
    var agg = {};

    spans.forEach(function (span) {
      if (span.kind !== "node") return;
      if (!agg[span.name]) {
        agg[span.name] = { name: span.name, ms: 0, n: 0, bad: 0 };
        order.push(span.name);
      }
      var row = agg[span.name];
      row.ms += Number(span.duration_ms) || 0;
      row.n += 1;
      if ((span.status || "ok") !== "ok") row.bad += 1;
    });

    if (!total || !order.length) {
      return '<div class="card">' +
        cardHead("链路耗时", "clock", '<span class="chip">无节点</span>') +
        '<p class="obs-stage-sub">这一轮没有记录节点耗时。</p>' +
        "</div>";
    }

    var covered = order.reduce(function (acc, name) { return acc + agg[name].ms; }, 0);
    var rest = Math.max(0, total - covered);

    function row(label, ms, extra, dim) {
      var pct = Math.min(100, ms / total * 100);
      return '<div class="obs-time-row' + (dim ? " dim" : "") + '">' +
        '<span class="obs-time-name">' + label + "</span>" +
        '<span class="obs-time-track"><i style="width:' + pct.toFixed(2) + '%"></i></span>' +
        '<span class="obs-time-ms mono">' + esc(fmtMs(ms)) + "</span>" +
        '<span class="obs-time-pct mono">' + fmtPct(pct) + "</span>" +
        "</div>" +
        (extra || "");
    }

    var body = order.map(function (name) {
      var item = agg[name];
      var label = esc(SPAN_NAME_CN[name] || name) +
        (item.n > 1 ? ' <span class="obs-time-n">×' + item.n + "</span>" : "") +
        (item.bad ? ' <span class="chip err">失败 ' + item.bad + "</span>" : "");
      return row(label, item.ms);
    }).join("");

    if (rest > 20) {
      body += row('<span class="muted">编排与其他</span>', rest, "", true);
    }

    return '<div class="card">' +
      cardHead("链路耗时", "clock", '<span class="chip">' + esc(fmtMs(total)) + "</span>") +
      '<p class="obs-stage-sub">总耗时 ' + esc(fmtMs(total)) +
      "，按节点累计；同一个节点跑了多次就相加（规划 2 次 = 两次之和）。" +
      "要按时间先后看，用左侧的编排流程图。</p>" +
      '<div class="obs-time-list">' + body + "</div>" +
      "</div>";
  }

  /* ---- L3：一次运行 ----

     跨轮、跨智能体的一条时间线。入口只给一个：trace 详情页的「这一次运行」。
     不在列表上加列——九成运行只有一轮，那样每行都会多一个「和本行内容一样」
     的按钮，点进去还多一层。只有真跨轮的时候，这个入口才有东西可看。 */

  async function mountObsOperation(root) {
    var opId = obs.operation;

    root.innerHTML = pageHead({
      eyebrow: "OPERATION",
      title: "运行详情",
      desc: "运行标识 " + opId + "。一次运行可能跨多轮、跨多个智能体；历时、其中等人工、实际在跑是三个数，不是并列的三个指标。",
      actions: '<button class="btn" type="button" id="obs-op-back">' + ico("back") + "返回列表</button>"
    }) + '<div id="obs-op-body"><div class="card"><div class="skeleton" style="width:60%"></div></div></div>';

    $("#obs-op-back").addEventListener("click", function () {
      obs.operation = null;
      obs.detail = null;
      obs.openSpan = null;
      go("observe");
    });

    var data;
    try {
      data = await api("/api/v1/observability/operations/" + encodeURIComponent(opId));
    } catch (e) {
      var failed = $("#obs-op-body");
      if (failed) {
        failed.innerHTML = '<div class="card"><div class="banner err">加载失败：' +
          esc(errText(e)) + "</div></div>";
      }
      return;
    }

    // 请求回来时页面可能已经换走了，这时写进去会把别人的卡片覆盖掉。
    var host = $("#obs-op-body");
    if (!host) return;

    host.innerHTML = PAPRender.operationView(data);

    // 时间线的一行 = 那一轮。点进去看它的调用链；返回时 `obs.operation`
    // 还在，于是回到这条运行，而不是一路弹回列表。
    $$("#obs-op-body .op-step[data-trace]").forEach(function (node) {
      node.addEventListener("click", function () {
        obs.detail = node.getAttribute("data-trace");
        obs.payload = null;
        obs.openSpan = null;
        go("observe");
      });
    });
  }

  async function mountObsDetail(root) {
    root.innerHTML = pageHead({
      eyebrow: "TRACE", title: "调用链详情",
      desc: "先并排看路径与耗时，再按阶段拆：技能、工具、模型循环与最终答复。点任意一行展开它的原文。",
      actions: '<button class="btn" type="button" id="obs-back">' + ico("back") + "返回列表</button>"
    }) + '<div class="card"><div class="skeleton" style="width:60%"></div></div>';

    $("#obs-back").addEventListener("click", function () {
      obs.detail = null;
      obs.payload = null;
      go("observe");
    });

    var data;
    try {
      data = await api("/api/v1/observability/traces/" + encodeURIComponent(obs.detail));
    } catch (e) {
      root.innerHTML = pageHead({
        eyebrow: "TRACE", title: "调用链详情",
        actions: '<button class="btn" id="obs-back">' + ico("back") + "返回列表</button>"
      }) + '<div class="card"><div class="banner err">加载失败：' + esc(errText(e)) + "</div></div>";
      $("#obs-back").addEventListener("click", function () {
        obs.detail = null;
        go("observe");
      });
      return;
    }

    obs.payload = data;
    var t = data.trace;
    var spans = data.spans || [];

    var byId = {};
    spans.forEach(function (s) { byId[s.span_id] = s; });

    var flowRows = flowPlan(spans);
    var flowSteps = flowRows.filter(function (r) { return !r.gap; }).length;

    // 泳道只在真有委派时才画：一轮里从头到尾只有一个智能体，泳道就是一条
    // 通到底的横条，比缩进盒子少回答一半问题。没有委派时连切换按钮都不给，
    // 免得点过去看到一张「等于没画」的图。
    var swimHtml = PAPRender.swimlanes(spans);
    var canSwim = !!swimHtml;
    var view = canSwim && obs.flowView === "swim" ? "swim" : "flow";

    var opActions = t.operation_id
      ? '<button class="btn" type="button" id="obs-to-op">' + ico("layers") + "这一次运行</button>"
      : "";

    root.innerHTML = pageHead({
      eyebrow: "TRACE", title: "调用链详情",
      desc: "先并排看路径与耗时，再按阶段拆：技能、工具、模型循环与最终答复。点任意一行展开它的原文。",
      actions: opActions + '<button class="btn" type="button" id="obs-back">' + ico("back") + "返回列表</button>"
    }) +
      '<div class="card">' +
      cardHead("本轮摘要", "activity", statusChip(t.status)) +
      '<div class="stats" id="obs-summary">' +
      stat("开始时间", fmtTime(t.started_at)) +
      stat("总耗时", fmtMs(t.duration_ms)) +
      stat("intent", t.intent || "—") +
      stat("智能体", t.agent_name || "默认助手") +
      stat("技能", t.skill_name || "未命中") +
      stat("主工具", t.tool_name || "—") +
      stat("span 数", String(t.span_count || spans.length)) +
      stat("模型 / 工具调用", (t.llm_calls || 0) + " / " + (t.tool_calls || 0)) +
      stat("token 入/出", fmtNum(t.prompt_tokens) + " / " + fmtNum(t.completion_tokens)) +
      "</div>" +
      // 上下文那一行放在摘要卡里，不另开卡：它和「耗时」同属「这一轮的外部条件」，
      // 单开一张卡会把下面真正要看的两张挤出首屏。
      (PAPRender.contextNote ? PAPRender.contextNote(spans) : "") +
      (t.error_category
        ? '<div class="banner err" style="margin-top:12px"><strong>' + esc(t.error_category) +
        "</strong>这一轮没有正常结束，下面标红的行是失败点。</div>"
        : "") +
      "</div>" +
      // 摘要是「这一轮是什么」，紧跟它的两张是「怎么走的、时间花在哪」——
      // 并排放是因为排障时这两件事总是一起看，拆成上下铺会多占一屏。
      // 有委派时左卡多一个视图开关：同一批 span 换个读法，不另开一张卡。
      '<div class="obs-pair' + (view === "swim" ? " single" : "") + '" id="obs-pair">' +
      (spans.length
        ? '<div class="card">' +
        '<div class="card-head">' +
        '<div class="card-title">' + ico("flow") +
        '<span id="obs-flow-title">' + esc(view === "swim" ? "耗时泳道" : "编排流程") + "</span></div>" +
        '<div class="obs-flow-tools">' +
        (canSwim
          ? '<span class="tabs obs-view-tabs">' +
          '<button class="tab' + (view === "flow" ? " active" : "") + '" type="button" ' +
          'data-obs-view="flow">流程</button>' +
          '<button class="tab' + (view === "swim" ? " active" : "") + '" type="button" ' +
          'data-obs-view="swim">泳道</button></span>'
          : "") +
        '<span class="chip">' + esc(String(flowSteps)) + " 步</span></div></div>" +
        '<div id="obs-flow-wrap"' + (view === "swim" ? " hidden" : "") + ">" +
        '<div class="obs-flow">' + renderFlow(flowRows) + "</div>" +
        flowLegend(flowRows) +
        "</div>" +
        '<div id="obs-swim-wrap"' + (view === "flow" ? " hidden" : "") + ">" +
        (swimHtml || "") +
        "</div>" +
        "</div>"
        : '<div class="card">' +
        cardHead("编排流程", "flow", '<span class="chip">0 步</span>') +
        '<div class="empty sm"><strong>这一轮没有 span</strong>' +
        "可能是观测在过程中被关闭，或写入被丢弃。</div>" +
        "</div>") +
      timingCard(t, spans) +
      "</div>" +
      '<div class="obs-stages">' +
      agentStage(data) +
      skillStage(data) +
      toolsStage(data, spans) +
      loopStage(t, spans) +
      answerStage(data, t, spans) +
      "</div>" +
      '<div class="card" id="obs-span-card" hidden><div id="obs-span-io"></div></div>' +
      '<div class="card">' +
      cardHead("用户输入", "message") +
      '<pre class="obs-pre">' + esc(data.input_text || "（未记录：capture 模式不是 full）") + "</pre>" +
      "</div>";

    $("#obs-back").addEventListener("click", function () {
      obs.detail = null;
      obs.payload = null;
      // 不清 `obs.operation`：从运行页点进来的，返回就该回那条运行。
      // 从列表进来的 operation 本来就是空的，自然落到列表。
      go("observe");
    });

    if (opActions) {
      $("#obs-to-op").addEventListener("click", function () {
        obs.operation = t.operation_id;
        obs.detail = null;
        obs.payload = null;
        obs.openSpan = null;
        go("observe");
      });
    }

    // 上下文 chip：点一下把摘要全文展开。没有摘要时渲染的是 <span>，
    // 这里取不到按钮就直接返回——不要为了「有交互」硬造一个空面板。
    var ctxBtn = $("#obs-ctx-toggle");
    var ctxBody = $("#obs-ctx-body");
    if (ctxBtn && ctxBody) {
      ctxBtn.addEventListener("click", function () {
        var open = ctxBody.hidden;
        ctxBody.hidden = !open;
        ctxBtn.setAttribute("aria-expanded", open ? "true" : "false");
        ctxBtn.classList.toggle("open", open);
      });
    }

    $$("[data-obs-view]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        obs.flowView = btn.getAttribute("data-obs-view") === "swim" ? "swim" : "flow";
        applyFlowView();
      });
    });

    var toggle = $(".obs-tools-toggle");
    if (toggle) {
      toggle.addEventListener("click", function () {
        var list = $("#obs-tool-list");
        if (!list) return;
        list.hidden = !list.hidden;
        toggle.textContent = list.hidden ? "展开查看全部工具" : "收起工具清单";
      });
    }

    // 循环里的每一行与流程图里的每个盒子都指向同一个 span：
    // 选中态跟着走，别让用户在两处各点一次。
    $$(".obs-round-line, .obs-round-tool, .fl-row").forEach(function (node) {
      node.addEventListener("click", function () {
        var spanId = node.getAttribute("data-span");
        if (!spanId) return;
        obs.openSpan = obs.openSpan === spanId ? null : spanId;
        markSpanOpen();
        renderSpanIO(spans, byId);
      });
    });
  }

  /* 流程 / 泳道切换只动 DOM，不重挂页面。
     重挂会再拉一次接口，还会把已经展开的 span 原文面板收掉——
     用户只是想换个读法，不该丢上下文。 */
  function applyFlowView() {
    var view = obs.flowView === "swim" ? "swim" : "flow";
    $$("[data-obs-view]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-obs-view") === view);
    });

    var flowWrap = $("#obs-flow-wrap");
    var swimWrap = $("#obs-swim-wrap");
    var pair = $("#obs-pair");
    var title = $("#obs-flow-title");

    if (flowWrap) flowWrap.hidden = view === "swim";
    if (swimWrap) swimWrap.hidden = view === "flow";
    // 泳道是定宽图（642px），塞进半栏会被等比缩到七八成，字跟着变小。
    // 切过去时让这张卡独占一行，读完再切回来。
    if (pair) pair.classList.toggle("single", view === "swim");
    if (title) title.textContent = view === "swim" ? "耗时泳道" : "编排流程";
  }

  function markSpanOpen() {
    $$(".obs-round-line, .obs-round-tool, .fl-row").forEach(function (node) {
      node.classList.toggle("open", node.getAttribute("data-span") === obs.openSpan);
    });
  }

  function renderSpanIO(spans, byId) {
    var host = $("#obs-span-io");
    var card = $("#obs-span-card");
    if (!host) return;

    function collapse() {
      host.innerHTML = "";
      if (card) card.hidden = true;
    }

    if (!obs.openSpan) { collapse(); return; }

    var span = byId[obs.openSpan];
    if (!span) { collapse(); return; }

    var meta = [];
    if (span.attrs && Object.keys(span.attrs).length) {
      meta.push('<div class="obs-io-label">attrs</div><pre class="obs-pre sm">' +
        esc(JSON.stringify(span.attrs, null, 2)) + "</pre>");
    }

    host.innerHTML = '<div class="obs-io">' +
      '<div class="obs-io-head">' + esc(span.name) +
      ' <span class="chip">' + esc(span.kind) + "</span>" +
      (span.status !== "ok" ? ' <span class="chip err">' + esc(span.status) + "</span>" : "") +
      ' <span class="muted mono">' + esc(fmtMs(span.duration_ms)) + "</span>" +
      '<button class="btn sm" type="button" id="obs-span-close">收起</button></div>' +
      (span.content
        ? '<pre class="obs-pre">' + esc(prettyContent(span.content)) + "</pre>"
        : '<div class="muted" style="padding:8px 0">这个 span 没有原文（capture 模式不是 full，或只是结构性节点）。</div>') +
      meta.join("") +
      "</div>";

    if (card) {
      card.hidden = false;
      // 面板落在两张图下面：点的是靠上的条目时它可能在视口外，
      // 不滚过来用户会以为没反应。block:"nearest" 在已可见时不动。
      if (card.scrollIntoView) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    var close = $("#obs-span-close");
    if (close) {
      close.addEventListener("click", function () {
        obs.openSpan = null;
        collapse();
        markSpanOpen();
      });
    }
  }

  /* ================= 帮助 ================= */

  // 平台使用说明集中在这里。各功能页面不再重复「使用说明」卡片，
  // 免得同一段文字散落多处、改动时漏掉一半。
  var HELP_SECTIONS = [
    {
      title: "对话台",
      icon: "message",
      intro: "左侧是会话历史，中间是当前会话，会话右侧贴边一条刻度轴（每条刻度 = 一轮提问）。历史只显示你自己的记录，按最近活跃排序。",
      items: [
        "新会话会生成一个新的线程标识；切换历史会话后继续发消息，会接着原来的上下文，不会另起一条记录。",
        "标题栏右侧的放大镜展开会话内搜索：只在已经加载的消息里找，命中逐条高亮并给出 1/5 这样的序号，Enter / Shift+Enter 前后跳，Esc 关闭。它不改动消息本身，刷新后不留痕。",
        "会话右侧的刻度轴按内容比例摆放，每条刻度是一轮提问：位置本身就在回答「读到哪儿了、还剩多少」。滚动时高亮跟随，悬停出这一轮的摘要，点一下滚过去并闪一下；少于 3 轮不显示。它不写 URL、不占浏览历史，也不和 hash 路由抢同一个地址。",
        "重命名只改标题；删除是销毁：消息、运行记录、模型侧图状态（checkpoint）、附件原文件一并清掉，不可恢复。",
        "附件（图片 / PDF）可以多选，最多 6 个、合计 40MB；发送前在输入框上方逐个可移除，同一会话可以反复追加。",
        "附件的链路是「落盘为句柄 → 模型自己决定是否调 ocr_document → 用识别结果继续规划」，纯文本对话不产生识别成本。识别本身不在平台里：ocr_document 把句柄解析成服务器路径，再调本机 OCR 服务的 MCP 工具 ocr_file，图片与 PDF 都支持。",
        "发送后气泡里会显示图片缩略图或 PDF 预览；点图片用内置查看器放大：滚轮缩放、放大后拖拽、同一条消息内左右切换，Esc 关闭，也可以在新标签打开原文件。刷新页面后从会话记录里重新取回。",
        "写操作不会直接执行：先给出预览与审批卡片，确认时才真正调用；确认时参数与预览不一致会被拒绝。",
        "一次请求内「工具执行 → 重新规划」的轮次上限由 AGENT_MAX_TOOL_STEPS 控制，设为 1 等于关闭回路。"
      ]
    },
    {
      title: "工具",
      icon: "wrench",
      intro: "三类工具共用一个注册表，模型只能看到已注册且被租户策略放行的那部分。",
      items: [
        "HTTP Tool：内置的由代码注册、只读；自定义的在右上角「新建」里配置，保存即注册，无需重启。",
        "自定义 HTTP Tool 支持 GET/POST/PUT/PATCH/DELETE，参数分 Path / Query / Body / Header 四种位置；Path 参数必须在 URL 里写成 {name} 或 :name。",
        "风险等级为写入及以上时，调用会强制走审批；这是网关的保守兜底，不依赖工具自己声明。",
        "工具名不能与已有内置 / MCP 工具重名：注册表只有一份，允许重名等于允许配置覆盖代码实现。",
        "「从 URL 解析」只反解路径占位符；「粘贴 curl 解析」会提取方法、URL、请求头与 JSON body 参数，但不会执行命令，也不会接受 Authorization / Cookie 这类凭据。",
        "MCP Tool 在「发现」结果里接入：未接入的远端工具点「接入」，填本地名、中文说明与风险等级，保存即注册；管理员接入的可以随时取消，代码白名单不行。",
        "「导出」只导出自定义 HTTP Tool（不含凭据）；「导入」支持合并与覆盖两种模式，同名内置工具会被跳过。"
      ]
    },
    {
      title: "编排",
      icon: "share",
      intro: "把「谁在第几步做什么」写成版本化的图。图只排顺序，不分配能力：每次节点调用仍然走委派那一套边界判定。",
      items: [
        "触发只有一种：在对话里发 /workflow <标识> <要说的事>。页面上刻意没有「立即运行」——那样跑出来的结论没有归属，用户看不见、审计也查不到。",
        "保存即编译：没有兜底边、有环、引用了不在必经路径上的节点、上限装不下最长路径，都会在保存时拒绝并指出是哪个节点，不会等到对话里才炸。",
        "节点里只能写 Agent 名、任务模板、顺序与条件，不能内联提示词、不能临时给工具——那是绕过边界判定的口子。",
        "两个预算分开算：模型的「每轮最多转交几次」管的是它临场即兴，图里人写死的步数不受它限制；但「最大转交深度」仍然逐跳累积，超了在开跑前就拒。",
        "节点之间只传结论不传对话：上一节点没写进 emit 的东西，下一节点看不到。emit 默认只存「输出前 max_chars 字」——要按类别分流，得在 emit 里显式配 values（枚举）或 pattern（正则，取第一个捕获组），否则条件几乎不会命中。超长输出会被截断，并在运行记录里标「截断」。",
        "停在写操作审批上时，run 记为「等确认」。点完那张单子，流程从停住的节点继续，不会从头重跑；取消或过期则整条流程取消，后面的节点一步都不跑。",
        "「最长几跳」要看得比发起方智能体的「最大转交深度」小，否则这张图一定跑不完——那是个配置问题，不是权限问题。"
      ]
    },
    {
      title: "知识库",
      icon: "book",
      intro: "文档入库、切片预览与检索验证。上传与目录扫描分开管理，检索结果受租户与 Scope 双重过滤。",
      items: [
        "「上传文档」支持 .md .txt .pdf .docx .html，单个文件不超过 5 MB，上传后立即入库、立即可检索。上传需要 knowledge:write 权限。",
        "「同步目录」把服务器知识目录扫进库，是增量同步：内容没变的跳过、变了的重切、目录里已删除的下架。它只动目录来源的文档，不会碰页面上传的那些。",
        "点任意一行展开切片原文，用来核对「检索到的到底是什么」；切片先按 Markdown 标题或中文编号分节，节内按 800 字符切分、相邻保留 120 字符重叠。引用里显示的是真实小节标题（如「停机规程 / 压力阈值」），没有标题的文档才回落到「第 N 段」。",
        "「重建」按原文重新切一次（改过切片参数后用）；原文不在了会标成失败并保留旧切片，不会让资料凭空消失。",
        "「下架」是软删：立即不参与检索，但行与审计保留，便于追溯。",
        "「文档」卡片头的放大镜按标题、文件名或原始路径筛选，输入即时生效，收起即取消筛选。它只筛当前已加载的前 200 篇，不是全库搜索——到上限时筛选条会明说。",
        "「检索验证」返回 found、citations 与每条命中的 score，用来确认召回是否符合预期。点引用标题会跳到对应文档并高亮命中切片；小语料下 BM25 的分数可能接近 0，那是因为 IDF 不可靠，不代表不相关。",
        "检索先由 BM25 召回 12 条，再让当前生效的模型逐条判「是否真正提供了问题所问的信息」，只保留直接相关的条目，剔除字面撞词与「同一主题但不回答」的片段，之后按原分数顺序取 4 条；答复仍是知识库原文摘录，模型不作转述。结果区会显示重筛是否生效、剔除几条与耗时；超时或模型异常会自动回落纯 BM25，不会让检索失败。要完全关闭请在 .env 设 KNOWLEDGE_RERANK_ENABLED=false。"
      ]
    },
    {
      title: "运行配置",
      icon: "sliders",
      intro: "模型供应商与进程参数。模型改完即时生效，进程参数需要重启。",
      items: [
        "模型供应商是平台级配置：同一时刻只有一个生效，切换后聊天与工具调用共用同一套模型。",
        "没有添加供应商时，平台会退回 .env 里的兜底配置；此时对话仍然可用，不是「没有模型」。",
        "思考模式模型（如 deepseek-flash / deepseek-v4-pro）不接受强制 tool_choice；平台已默认关闭思考，若仍报错请换非思考模型。",
        "OCR 不在本页配置：识别由本机 OCR 服务的 MCP 接口（ocr_file）提供，地址取自 .env 的 OCR_MCP_URL。以前那张「OCR 服务」卡片已删除——同一件事维护两份配置，只会出现「页面显示启用、实际连不上」这种查半天的问题。",
        "进程参数（版本、环境、就绪检查项）只读展示，改它们要改 .env 并重启服务。"
      ]
    },
    {
      title: "观测",
      icon: "activity",
      intro: "开发期排障用。每轮对话落一条 trace，每个节点 / 模型调用 / 工具调用落一条 span。",
      items: [
        "开关是 OBS_ENABLED，默认关。关的时候页面顶部会明说「观测已关闭」，不会拿 0 条糊过去；改完要重启服务。",
        "默认只看得到自己发起的对话；有 observability:read 权限才能看整个租户，右上角会标出当前范围。",
        "列表只显示汇总列，原文（用户提问、最终回答、每个 span 的输入输出）在详情里；点循环里的规划 / 工具行，或流程图里的盒子，都能展开该 span 的原文。",
        "详情页上面并排两张：「编排流程」看这一轮走了哪条路，「链路耗时」看时间花在哪个阶段。下面是四张阶段卡：「Skill 选择」看命中方式、注入了多长正文、白名单把工具从几个收窄到几个、近 7 天被用过几次；「准备工具」列出模型当时实际看到的工具清单（可展开）；「模型与工具循环」按趟列出每次规划与它调用的工具；「最终答复」把回答按富文本渲染（表格、曲线、流程图都能显示），要 Markdown 原文时展开它下面的折叠。",
        "「Skill 选择」里的数字来自三处：注入情况取自这一轮的 span 属性（当时的事实，技能后来改正文不影响它），命中次数取自 skill_hits（不受观测开关与保留期影响），技能是否还在取自 skills 当前状态。删掉技能后这一节仍能看，只标「已删除」。",
        "「链路耗时」里的条从 0 起、宽度是该阶段占总耗时的比例，读的是「谁占得多」；要按时间先后看顺序，看左边的「编排流程」——缩进表示调用关系，重复出现的节点（规划 → 工具 → 再规划）会连着出现并标 ①②。",
        "这一轮里有委派（别的智能体被叫来干活）时，「编排流程」的卡头会多一对「流程 / 泳道」开关：流程看走了哪条路，泳道按智能体分道看谁在什么时候在干。没有委派就没有这对开关——泳道画出来只是一条通到底的横条，不如缩进列表。",
        "右上角「这一次运行」进运行页：把同一次运行的几轮和那张审批单串成一条时间线。历时、其中等人工、实际在跑是三个数，不是一个数的三种说法——等了人两小时五十八分和系统跑了两小时五十八分，要改的地方完全不同。点时间线上任一轮回到那一轮的详情，返回时回到这条运行。",
        "保留期 14 天，错误 trace 保留 30 天；初始化时没有 observability:read 的库需要手动补一行授权。"
      ]
    },
    {
      title: "记忆",
      icon: "layers",
      intro: "跨会话沉淀的稳定事实。对话里说过的称呼、偏好、决策会留在这里，下一轮作为背景注入规划 prompt。",
      items: [
        "自动抽取：每轮回答落地后，后台用同一份文字模型判断这一轮有没有值得记的事实。抽出来的是候选，标「自动」并挂着「待确认」，确认后才当成确定信息。",
        "它不记本轮参数（站点、日期、某次查询的数值）、工具返回的内容、模型自己的猜测，也不记任何密钥。拿不准的宁可不记。",
        "同类型同标题只留一条：再说一次是更新内容，不会攒出十条互相打架的「称呼」。",
        "已确认的记忆不会被自动抽取改写——改口由人来改，模型没有这个权限。",
        "注入有预算：最多 40 条、1600 字符，超出的省略并注明。没确认的候选要置信度 ≥0.8 才会进 prompt，其余留在本页等你判断。",
        "「进展」类自动抽取后 30 天过期；过期置为 superseded 而不物理删除，便于追溯「为什么以前那样答」。用户点删除则是物理删除，无法恢复。",
        "关掉 MEMORY_ENABLED 后接口仍可读写但不再注入；只关 MEMORY_EXTRACT_ENABLED 则停止新增候选，已有记忆照常注入。",
        "「开始梦境」不直接改任何东西：它把未确认的自动候选与最近的会话原文对照，产出改写 / 合并 / 下架建议，点「应用」才生效。已确认或手写的记忆不会被碰；证据只取你自己的消息，助手的话不算。",
        "卡片底部的确认率只算仍存在的候选：被物理删除的候选查不到，所以这个数会偏高，别当精确通过率用。梦境按需触发，不挂在每轮对话上。"
      ]
    },
    {
      title: "组织、成员与审计",
      icon: "users",
      intro: "仅平台管理员可见。所有写操作都落 ADMIN_ACTION 审计。",
      items: [
        "成员角色决定能调用哪些工具；租户配额决定每分钟工具调用与每日对话上限。",
        "工具策略按租户 + 工具粒度开关，可限定角色；credential_ref 填的是环境变量名，不是密钥本身。",
        "审计页按租户查看管理动作与运行事件，是排障入口，不提供读取会话正文的能力。"
      ]
    },
    {
      title: "常见问题",
      icon: "help",
      intro: "按「先看哪里」排序，不是穷举。",
      items: [
        "对话返回 400 且提示 tool_choice / thinking：模型是思考模式，换非思考模型或在运行配置里调整。",
        "MCP 发现返回 400：先看令牌是否配置（MCP_TENANT_TOKENS），再看对端是否在同一网络；令牌无效与协议错误是两类问题。",
        "工具调用被拒：依次确认「工具是否注册」「租户策略是否放行」「Scope / 角色是否满足」「站点是否授权」「是否触发限流」。",
        "页面上的数字全部来自实时接口；接口失败会显示错误原文，不会用占位数据糊过去。"
      ]
    }
  ];

  function mountHelp(root) {
    root.innerHTML = pageHead({
      eyebrow: PAGES.help.eyebrow,
      title: PAGES.help.title,
      desc: PAGES.help.desc
    }) +
      '<div class="help-grid">' +
      HELP_SECTIONS.map(function (sec) {
        return '<section class="card help-card">' +
          '<div class="card-head"><h3 class="card-title">' + ico(sec.icon) + esc(sec.title) + "</h3></div>" +
          '<p class="card-sub">' + esc(sec.intro) + "</p>" +
          "<ul>" + sec.items.map(function (item) {
            return "<li>" + esc(item) + "</li>";
          }).join("") + "</ul>" +
          "</section>";
      }).join("") +
      "</div>";
  }

  /* ================= 技能 ================= */

  var skillUI = {
    items: [],
    selected: null,      // 已保存技能的 id
    draft: null,         // 正在编辑的数据；新建时 isNew=true
    dirty: false,
    canWrite: false,
    enabled: true,
    tools: [],
    recall: null,
    // 从模板新建时的临时态：编辑器区域改显示模板列表。
    picking: false,
    templates: []
  };

  var SKILL_MAX_INSTRUCTION = 8000;

  function skillDraft(item) {
    return {
      id: item ? item.id : null,
      name: item ? item.name : "",
      display_name: item ? item.display_name : "",
      description: item ? item.description : "",
      instruction: item ? item.instruction : "",
      trigger_keywords: item ? (item.trigger_keywords || []).slice() : [],
      allowed_tools: item ? item.allowed_tools : null,
      missing_tools: item ? (item.missing_tools || []) : [],
      priority: item ? item.priority : 100,
      enabled: item ? item.enabled : true,
      // 命中评估只用于展示，不参与保存；详情接口才有 recent_hits。
      hits_7d: item ? (item.hits_7d || 0) : 0,
      last_hit_at: item ? (item.last_hit_at || null) : null,
      recent_hits: item ? (item.recent_hits || []) : [],
      isNew: !item
    };
  }

  async function mountSkills(root) {
    skillUI.selected = null;
    skillUI.draft = null;
    skillUI.dirty = false;
    skillUI.recall = null;
    skillUI.picking = false;

    root.innerHTML = pageHead({
      eyebrow: PAGES.skills.eyebrow,
      title: PAGES.skills.title,
      desc: PAGES.skills.desc,
      actions:
        '<button class="btn" type="button" id="sk-refresh">' + ico("refresh") + "刷新</button>" +
        '<button class="btn" type="button" id="sk-export">' + ico("download") + "导出</button>" +
        '<button class="btn" type="button" id="sk-import">' + ico("upload") + "导入</button>" +
        '<button class="btn" type="button" id="sk-tpl">' + ico("spark") + "从模板</button>" +
        '<button class="btn primary" type="button" id="sk-new">' + ico("plus") + "新建</button>"
    }) +
      '<input type="file" id="sk-import-file" accept="application/json,.json" hidden>' +
      '<div class="skill-layout">' +
      '<div class="card skill-pane">' +
      cardHead("Skill 列表", "spark", '<span class="chip" id="sk-count">—</span>') +
      '<div class="skill-list" id="sk-list"><div class="skeleton" style="width:70%"></div></div>' +
      "</div>" +
      '<div class="card skill-pane">' +
      cardHead("编辑 Skill", "edit", '<span class="chip warn" id="sk-state" hidden>未保存</span>') +
      '<div id="sk-editor"><div class="empty">加载中…</div></div>' +
      // 保存按钮放在滚动区外：字段一多就要滚，按钮跟着滚走就找不到了。
      '<div class="skill-foot" id="sk-foot" hidden></div>' +
      "</div>" +
      "</div>" +
      '<div class="card skill-rec" id="sk-rec">' +
      '<button class="skill-rec-head" type="button" id="sk-rec-toggle" aria-expanded="false">' +
      ico("down") + '<span class="skill-rec-title">召回测试</span>' +
      '<span class="skill-rec-sum" id="sk-rec-sum">尚未运行</span>' +
      "</button>" +
      '<div class="skill-rec-body" id="sk-rec-body" hidden>' +
      '<div class="toolbar" style="margin:10px 0 0">' +
      '<input type="text" id="sk-rec-input" placeholder="输入一句真实用户会说的话，看它会命中哪个技能" style="flex:1">' +
      '<button class="btn primary" type="button" id="sk-rec-run">测试</button>' +
      "</div>" +
      '<div id="sk-rec-result"></div>' +
      "</div>" +
      "</div>";

    $("#sk-refresh").addEventListener("click", function () { loadSkills(skillUI.selected); });
    $("#sk-export").addEventListener("click", exportSkills);
    $("#sk-import").addEventListener("click", function () { $("#sk-import-file").click(); });
    $("#sk-import-file").addEventListener("change", function () {
      var file = this.files && this.files[0];
      if (file) importSkills(file);
      this.value = "";
    });
    $("#sk-tpl").addEventListener("click", function () {
      if (!skillUI.canWrite) { toast("没有新建技能的权限", "err"); return; }
      guardDirty(openSkillTemplates);
    });
    $("#sk-new").addEventListener("click", function () {
      if (!skillUI.canWrite) { toast("没有新建技能的权限", "err"); return; }
      if (!guardDirty(function () { startNewSkill(); })) return;
    });
    $("#sk-rec-toggle").addEventListener("click", function () {
      var body = $("#sk-rec-body");
      var open = body.hasAttribute("hidden");
      if (open) body.removeAttribute("hidden");
      else body.setAttribute("hidden", "");
      $("#sk-rec").classList.toggle("open", open);
      $("#sk-rec-toggle").setAttribute("aria-expanded", open ? "true" : "false");
    });
    $("#sk-rec-run").addEventListener("click", runSkillRecall);
    $("#sk-rec-input").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); runSkillRecall(); }
    });

    await loadSkills(null);
  }

  function guardDirty(fn) {
    if (!skillUI.dirty) { fn(); return true; }
    openConfirm({
      title: "放弃未保存的修改",
      body: '<p class="modal-lead">当前编辑的内容还没保存，切换后这些修改会丢失。</p>',
      okText: "放弃修改"
    }).then(function (ok) { if (ok) fn(); });
    return false;
  }

  async function loadSkills(selectId) {
    try {
      var r = await api("/api/v1/skills");
      skillUI.items = r.items || [];
      skillUI.canWrite = !!r.can_write;
      skillUI.enabled = r.enabled !== false;
    } catch (e) {
      $("#sk-list").innerHTML = '<div class="banner err">加载失败：' + esc(errText(e)) + "</div>";
      $("#sk-editor").innerHTML = "";
      return;
    }

    try {
      var tools = await api("/api/v1/tools");
      skillUI.tools = (tools || []).map(function (t) {
        return { name: t.name, display: t.display_name || t.name };
      });
    } catch (e) {
      skillUI.tools = [];
    }

    var target = null;
    if (selectId) {
      target = skillUI.items.filter(function (s) { return s.id === selectId; })[0] || null;
    }
    // 没指定或指定的已被删掉：落到第一个。左侧永远有选中项，
    // 比空着一块编辑区更不容易让人以为页面坏了。
    if (!target && skillUI.items.length) target = skillUI.items[0];

    // 列表接口只给计数，最近命中在详情里；选中时补一次，别让两处各查一遍。
    if (target) target = (await loadSkillDetail(target.id)) || target;

    skillUI.selected = target ? target.id : null;
    skillUI.draft = target ? skillDraft(target) : null;
    skillUI.dirty = false;
    renderSkillList();
    renderSkillEditor();
  }

  async function loadSkillDetail(id) {
    try {
      return await api("/api/v1/skills/" + encodeURIComponent(id));
    } catch (e) {
      // 详情拿不到不影响编辑：退化成只有基础字段。
      return null;
    }
  }

  function startNewSkill() {
    skillUI.picking = false;
    skillUI.selected = null;
    skillUI.draft = skillDraft(null);
    skillUI.dirty = false;
    renderSkillList();
    renderSkillEditor();
    var name = $("#sk-name");
    if (name) name.focus();
  }

  function renderSkillList() {
    var box = $("#sk-list");
    $("#sk-count").textContent = skillUI.items.length ? skillUI.items.length + " 个" : "空";

    if (!skillUI.items.length) {
      box.innerHTML = '<div class="empty"><strong>还没有技能</strong>' +
        "新建一个，把「先做什么、再做什么、用哪个工具」写清楚。列表为空时对话行为与以前完全一致。</div>";
      return;
    }

    box.innerHTML = skillUI.items.map(function (item) {
      var active = !skillUI.draft || !skillUI.draft.isNew
        ? item.id === skillUI.selected
        : false;
      return '<button class="skill-item' + (active ? " active" : "") + '" type="button" data-id="' +
        esc(item.id) + '">' +
        '<span class="skill-badge ' + (item.enabled ? "on" : "off") + '">' +
        (item.enabled ? "已启用" : "已停用") + "</span>" +
        '<span class="skill-item-name">' + esc(item.display_name || item.name) + "</span>" +
        (item.hits_7d
          ? '<span class="skill-item-hits">近 7 天 ' + esc(String(item.hits_7d)) + " 次</span>"
          : "") +
        '<span class="skill-item-desc">' +
        esc(item.description || item.instruction.slice(0, 72) || "没有描述，只能用 /" + item.name + " 显式调用。") +
        "</span></button>";
    }).join("");

    $$(".skill-item", box).forEach(function (node) {
      node.addEventListener("click", function () {
        var id = node.getAttribute("data-id");
        if (id === skillUI.selected && !skillUI.draft.isNew) return;
        guardDirty(async function () {
          var item = skillUI.items.filter(function (s) { return s.id === id; })[0];
          var full = (await loadSkillDetail(id)) || item;
          skillUI.picking = false;
          skillUI.selected = id;
          skillUI.draft = skillDraft(full);
          skillUI.dirty = false;
          renderSkillList();
          renderSkillEditor();
        });
      });
    });
  }

  function renderSkillEditor() {
    var box = $("#sk-editor");
    var chip = $("#sk-state");
    chip.hidden = !skillUI.dirty;

    var foot = $("#sk-foot");
    if (skillUI.picking) {
      foot.hidden = true;
      foot.innerHTML = "";
      return;
    }
    var d = skillUI.draft;
    if (!d) {
      box.innerHTML = '<div class="empty"><strong>还没有选中技能</strong>' +
        "左侧选一个，或点右上角「新建」。保存后的技能会立刻参与对话规划，不需要重启。</div>";
      foot.hidden = true;
      foot.innerHTML = "";
      return;
    }

    var editing = !d.isNew;
    var ro = skillUI.canWrite ? "" : ' disabled';
    var toolsHtml = renderSkillTools(d);

    box.innerHTML =
      (skillUI.canWrite ? "" :
        '<div class="banner warn" style="margin-bottom:12px">只读：当前身份没有 skill:write 权限，可以看但不能改。</div>') +
      '<div class="skill-id-row">' +
      '<div class="field" style="flex:1"><label>Skill ID</label>' +
      '<input type="text" id="sk-name" value="' + esc(d.name) + '"' +
      (editing ? " disabled" : "") + ' placeholder="patrol-compare">' +
      '<span class="help">' + (editing
        ? "保存后不可修改：改名等于换了一个技能，历史调用记录会对不上。"
        : "小写字母开头，可用小写字母、数字、- 和 _。也支持 /" + (d.name || "名字") + " 显式调用。") +
      "</span></div>" +
      '<label class="switch skill-switch"><input type="checkbox" id="sk-enabled"' +
      (d.enabled ? " checked" : "") + ro + ">" +
      '<span id="sk-enabled-text">' + (d.enabled ? "已启用" : "已停用") + "</span></label>" +
      "</div>" +

      '<div class="field skill-full"><label>Skill 全文 <span class="skill-len" id="sk-len">' +
      (d.instruction || "").length + " / " + SKILL_MAX_INSTRUCTION + "</span></label>" +
      '<textarea id="sk-inst" class="skill-text" rows="12" placeholder="# 步骤&#10;1. 先识别附件，取编号与数值&#10;2. 按站点查工单，逐项比对&#10;3. 输出差异表，单位写进表头" style="font-family:var(--mono)">' +
      esc(d.instruction) + "</textarea>" +
      '<span class="help">写给模型看的作业步骤。命中的技能会接在规划提示后面，优先级高于通用规则；但它不能授予额外权限。</span></div>' +

      '<div class="skill-info">' +
      '<div class="field"><label>名称</label><input type="text" id="sk-display" maxlength="64" value="' +
      esc(d.display_name) + '" placeholder="调压站巡检比对"' + ro + "></div>" +
      '<div class="field"><label>描述</label><textarea id="sk-desc" rows="2" maxlength="256" placeholder="一句话说明什么时候用它"' +
      ro + ">" + esc(d.description) + "</textarea></div>" +
      '<div class="field"><label>触发词</label><input type="text" id="sk-words" value="' +
      esc((d.trigger_keywords || []).join(", ")) + '" placeholder="巡检, 比对, 调压站"' + ro + ">" +
      '<span class="help">用逗号分隔，最多 12 个。命中任意一个就进这个技能。</span></div>' +
      '<div class="field"><label>优先级</label><input type="number" id="sk-priority" min="0" max="1000" value="' +
      d.priority + '"' + ro + ">" +
      '<span class="help">多个技能同时命中时取大的。</span></div>' +
      "</div>" +

      toolsHtml +
      renderSkillHits(d);

    foot.hidden = false;
    foot.innerHTML =
      (skillUI.canWrite
        ? '<button class="btn primary" type="button" id="sk-save">' + ico("save") + "保存 Skill</button>" +
          (editing ? '<button class="btn danger" type="button" id="sk-del">' + ico("trash") + "删除</button>" : "")
        : '<span class="skill-foot-note" style="margin-left:0">只读身份，无法保存。</span>') +
      '<span class="skill-foot-note" id="sk-note"></span>';

    $("#sk-inst").addEventListener("input", function () {
      $("#sk-len").textContent = this.value.length + " / " + SKILL_MAX_INSTRUCTION;
      markSkillDirty();
    });
    $("#sk-enabled").addEventListener("change", function () {
      $("#sk-enabled-text").textContent = this.checked ? "已启用" : "已停用";
      markSkillDirty();
    });
    ["sk-display", "sk-desc", "sk-words", "sk-priority", "sk-name"].forEach(function (id) {
      var node = $("#" + id);
      if (node) node.addEventListener("input", markSkillDirty);
    });
    bindSkillTools();

    var obsLink = $("#sk-hits-obs");
    if (obsLink) {
      obsLink.addEventListener("click", function () {
        // 「跳到那一轮」需要会话 id，这里给的是等价入口：
        // 观测页按技能筛，再点具体的 trace 看全文。
        obs.skill = d.name;
        obs.detail = null;
        go("observe");
      });
    }

    if (skillUI.canWrite) {
      $("#sk-save").addEventListener("click", saveSkill);
      var del = $("#sk-del");
      if (del) del.addEventListener("click", deleteSkill);
    }
  }

  function renderSkillTools(d) {
    var unlimited = d.allowed_tools === null;
    var chosen = unlimited ? [] : d.allowed_tools;
    var known = {};
    skillUI.tools.forEach(function (t) { known[t.name] = true; });

    var chips = skillUI.tools.map(function (t) {
      var on = chosen.indexOf(t.name) >= 0;
      return '<button type="button" class="chip skill-tool' + (on ? " on" : "") +
        '" data-tool="' + esc(t.name) + '" title="' + esc(t.name) + '">' + esc(t.display) + "</button>";
    }).join("");

    // 配了但当前身份看不到：明确标出来，不要让它看起来像已生效。
    var missing = (d.missing_tools || []).filter(function (name) { return !known[name]; });
    chips += missing.map(function (name) {
      return '<span class="chip err skill-tool-missing" title="当前身份不可用，不会进入工具目录">' +
        esc(name) + " · 不可用</span>";
    }).join("");

    return '<div class="field skill-tools-field"><label>工具范围</label>' +
      '<div class="skill-tools" id="sk-tools" data-mode="' + (unlimited ? "all" : "pick") + '">' +
      '<label class="skill-tool-all"><input type="checkbox" id="sk-tools-all"' +
      (unlimited ? " checked" : "") + (skillUI.canWrite ? "" : " disabled") +
      ">不限制（全部工具）</label>" +
      '<div class="skill-tool-list">' + chips + "</div>" +
      "</div>" +
      '<span class="help">不限制 = 目录照原样；勾选后只保留选中的工具。' +
      "只能收窄，不能放大——这里勾了当前身份看不到的工具也不会生效。</span></div>";
  }

  function renderSkillHits(d) {
    if (d.isNew) return "";

    var head = '<div class="field skill-hits-field"><label>最近命中' +
      '<span class="skill-len">近 7 天 ' + esc(String(d.hits_7d || 0)) + " 次</span></label>";

    if (!(d.recent_hits || []).length) {
      return head +
        '<div class="skill-hits-empty">还没有命中记录。写好触发词后，可以用下面的「召回测试」验证；' +
        "真实对话命中后这里会出现记录。</div></div>";
    }

    var rows = d.recent_hits.map(function (hit) {
      var how = hit.matched_by === "explicit"
        ? "显式 /" + d.name
        : "触发词「" + (hit.keyword || "") + "」";
      return '<div class="skill-hit">' +
        '<span class="mono muted">' + esc(fmtTime(hit.created_at)) + "</span>" +
        "<span>" + esc(how) + "</span>" +
        (hit.thread_id ? '<span class="mono muted">' + esc(hit.thread_id) + "</span>" : "") +
        "</div>";
    }).join("");

    return head + '<div class="skill-hits">' + rows + "</div>" +
      '<div class="skill-hits-foot">' +
      '<span class="help">只统计真实对话；召回测试不计入，否则调指令时点几十次会把趋势画歪。</span>' +
      '<button class="linkish" type="button" id="sk-hits-obs">在观测里看这个技能</button>' +
      "</div></div>";
  }

  async function openSkillTemplates() {
    skillUI.picking = true;
    $("#sk-editor").innerHTML = '<div class="hint">加载模板…</div>';
    $("#sk-foot").hidden = true;

    try {
      skillUI.templates = await api("/api/v1/skills/templates");
    } catch (e) {
      $("#sk-editor").innerHTML = '<div class="banner err">模板加载失败：' + esc(errText(e)) + "</div>";
      return;
    }
    renderSkillTemplates();
  }

  function renderSkillTemplates() {
    var box = $("#sk-editor");
    var list = skillUI.templates || [];

    box.innerHTML =
      '<div class="skill-tpl-head"><strong>从模板新建</strong>' +
      '<span class="help" style="margin:0">复制成你自己的技能，之后随便改；模板本身不会被改动。</span></div>' +
      list.map(function (t) {
        var meta = [
          '<span class="chip">' + t.trigger_keywords.length + " 个触发词</span>",
          '<span class="chip">' + t.allowed_tools.length + " 个工具</span>"
        ];
        if (t.missing_tools.length) {
          meta.push('<span class="chip err">' + t.missing_tools.length + " 个工具当前不可用</span>");
        }
        if (t.exists) meta.push('<span class="chip warn">已有同名技能</span>');

        return '<div class="skill-tpl">' +
          '<div class="skill-tpl-top"><span class="skill-item-name">' + esc(t.display_name) + "</span>" +
          meta.join("") + "</div>" +
          '<p class="skill-tpl-desc">' + esc(t.description) + "</p>" +
          '<pre class="skill-tpl-code">' + esc(t.instruction.slice(0, 240)) +
          (t.instruction.length > 240 ? "…" : "") + "</pre>" +
          '<div class="skill-tpl-foot">' +
          '<button class="btn primary" type="button" data-tpl="' + esc(t.key) + '"' +
          (t.exists ? " disabled" : "") + ">复制到本租户</button>" +
          '<span class="hint" style="margin:0">复制后默认停用，按你们的口径改完再启用。</span>' +
          "</div></div>";
      }).join("") +
      '<div class="skill-tpl-foot"><button class="btn" type="button" id="sk-tpl-cancel">取消</button></div>';

    $("#sk-tpl-cancel").addEventListener("click", function () {
      skillUI.picking = false;
      renderSkillEditor();
    });
    $$("[data-tpl]", box).forEach(function (btn) {
      btn.addEventListener("click", function () {
        copySkillTemplate(btn.getAttribute("data-tpl"));
      });
    });
  }

  async function copySkillTemplate(key) {
    try {
      var saved = await api("/api/v1/skills/templates/" + encodeURIComponent(key), {
        method: "POST"
      });
      toast("已复制到本租户，改完启用后生效");
      skillUI.picking = false;
      await loadSkills(saved.id);
    } catch (e) {
      toast("复制失败：" + errText(e), "err");
    }
  }

  function bindSkillTools() {
    var all = $("#sk-tools-all");
    if (!all) return;
    all.addEventListener("change", function () {
      var wrap = $("#sk-tools");
      var allOn = this.checked;
      wrap.setAttribute("data-mode", allOn ? "all" : "pick");
      // 勾「不限制」时清掉具体选择：留着会让人以为那些勾还在生效。
      if (allOn) {
        $$(".skill-tool", wrap).forEach(function (btn) { btn.classList.remove("on"); });
      }
      markSkillDirty();
    });
    $$(".skill-tool").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!skillUI.canWrite) return;
        var allBox = $("#sk-tools-all");
        if (allBox.checked) {
          allBox.checked = false;
          $("#sk-tools").setAttribute("data-mode", "pick");
        }
        btn.classList.toggle("on");
        markSkillDirty();
      });
    });
  }

  function markSkillDirty() {
    skillUI.dirty = true;
    var chip = $("#sk-state");
    if (chip) chip.hidden = false;
  }

  function collectSkill() {
    var d = skillUI.draft;
    if (!d) return null;
    var name = $("#sk-name") ? $("#sk-name").value.trim() : d.name;
    var words = ($("#sk-words") ? $("#sk-words").value : "")
      .split(/[,，、\s]+/)
      .map(function (w) { return w.trim(); })
      .filter(function (w) { return !!w; });

    var allowed = null;
    if ($("#sk-tools-all") && !$("#sk-tools-all").checked) {
      allowed = $$(".skill-tool.on").map(function (btn) { return btn.getAttribute("data-tool"); });
    }

    return {
      name: name,
      display_name: ($("#sk-display") ? $("#sk-display").value : "").trim(),
      description: ($("#sk-desc") ? $("#sk-desc").value : "").trim(),
      instruction: $("#sk-inst") ? $("#sk-inst").value : "",
      trigger_keywords: words,
      allowed_tools: allowed,
      priority: parseInt($("#sk-priority") ? $("#sk-priority").value : "100", 10) || 0,
      enabled: $("#sk-enabled") ? $("#sk-enabled").checked : true
    };
  }

  async function saveSkill() {
    var body = collectSkill();
    if (!body) return;
    if (!body.name) { toast("Skill ID 不能为空", "err"); $("#sk-name").focus(); return; }
    if (body.trigger_keywords.length > 12) { toast("触发词最多 12 个", "err"); return; }

    var editing = skillUI.draft && !skillUI.draft.isNew;
    var note = $("#sk-note");
    if (note) note.textContent = "保存中…";

    try {
      var saved;
      if (editing) {
        var payload = Object.assign({}, body);
        delete payload.name;   // 名字不可改，带着它只会 400
        saved = await api("/api/v1/skills/" + encodeURIComponent(skillUI.selected), {
          method: "PATCH", body: payload
        });
      } else {
        saved = await api("/api/v1/skills", { method: "POST", body: body });
      }
      toast(editing ? "已保存，下一轮对话生效" : "已创建，启用后参与规划");
      skillUI.dirty = false;
      skillUI.selected = saved.id;
      await loadSkills(saved.id);
    } catch (e) {
      if (note) note.textContent = "";
      toast("保存失败：" + errText(e), "err");
    }
  }

  async function deleteSkill() {
    var d = skillUI.draft;
    if (!d || d.isNew) return;
    var ok = await openConfirm({
      title: "删除技能",
      body: '<p class="modal-lead">将永久删除「<strong>' + esc(d.display_name || d.name) +
        "</strong>」。之后这句话会走普通规划，工具目录不再收窄。</p>",
      okText: "删除"
    });
    if (!ok) return;
    try {
      await api("/api/v1/skills/" + encodeURIComponent(d.id), { method: "DELETE" });
      toast("已删除");
      skillUI.dirty = false;
      skillUI.selected = null;
      await loadSkills(null);
    } catch (e) {
      toast("删除失败：" + errText(e), "err");
    }
  }

  async function exportSkills() {
    try {
      var data = await api("/api/v1/skills/export");
      if (!data.skills || !data.skills.length) {
        toast("还没有技能可导出", "err");
        return;
      }
      var stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
      downloadJson("pap-skills-" + stamp + ".json", data);
      toast("已导出 " + data.skills.length + " 个技能");
    } catch (e) {
      toast("导出失败：" + errText(e), "err");
    }
  }

  async function importSkills(file) {
    if (!skillUI.canWrite) { toast("没有导入技能的权限", "err"); return; }
    var text;
    try { text = await file.text(); } catch (e) { toast("读取文件失败", "err"); return; }

    var payload;
    try { payload = JSON.parse(text); } catch (e) {
      toast("不是合法的 JSON 文件", "err");
      return;
    }
    if (!payload || !Array.isArray(payload.skills)) {
      toast("文件格式不对：缺少 skills 数组", "err");
      return;
    }

    // 同名覆盖。先问一句，因为覆盖会直接改掉线上正在用的指令。
    var ok = await openConfirm({
      title: "导入技能",
      body: '<p class="modal-lead">文件里有 <strong>' + payload.skills.length +
        "</strong> 个技能。同名技能会被覆盖，其他新增。</p>",
      okText: "导入"
    });
    if (!ok) return;

    try {
      var r = await api("/api/v1/skills/import", { method: "POST", body: payload });
      toast("导入完成：新增 " + r.created + "，覆盖 " + r.updated);
      await loadSkills(skillUI.selected);
    } catch (e) {
      toast("导入失败：" + errText(e), "err");
    }
  }

  async function runSkillRecall() {
    var input = $("#sk-rec-input");
    var message = (input.value || "").trim();
    if (!message) { input.focus(); return; }

    var out = $("#sk-rec-result");
    var sum = $("#sk-rec-sum");
    out.innerHTML = '<div class="hint">测试中…</div>';

    try {
      var r = await api("/api/v1/skills/recall", { method: "POST", body: { message: message } });
      skillUI.recall = r;
      if (!r.matched) {
        sum.textContent = "未命中";
        sum.className = "skill-rec-sum";
        out.innerHTML = '<div class="skill-rec-result">' +
          '<span class="chip">未命中</span>' +
          '<span class="skill-rec-text">这句会走普通规划，工具目录不做收窄。</span></div>';
        return;
      }

      sum.textContent = "命中 " + (r.display_name || r.name);
      sum.className = "skill-rec-sum hit";
      var how = r.matched_by === "explicit" ? "显式 /" + r.name
        : "触发词「" + (r.keyword || "") + "」";
      out.innerHTML = '<div class="skill-rec-result">' +
        '<span class="chip ok">命中</span>' +
        '<span class="skill-rec-text"><strong>' + esc(r.display_name || r.name) + "</strong>" +
        '<span class="skill-rec-why">' + esc(how) + "</span>" +
        '<span class="skill-rec-why">指令 ' + r.instruction_chars + " 字</span>" +
        '<span class="skill-rec-why">可用工具 ' + (r.tools || []).length + " 个</span></span>" +
        "</div>" +
        '<p class="hint">' + esc(r.note || "") + "</p>";
    } catch (e) {
      sum.textContent = "测试失败";
      sum.className = "skill-rec-sum";
      out.innerHTML = '<div class="banner err">测试失败：' + esc(errText(e)) + "</div>";
    }
  }

  /* ================= 侧栏底部：账号与状态 ================= */

  async function refreshStatus() {
    var dot = $("#status-dot");
    var text = $("#status-text");
    var health = "bad";
    try {
      var ready = await api("/ready");
      state.ready = ready;
      var bad = Object.keys(ready.checks || {}).filter(function (k) {
        return ready.checks[k] !== true;
      });
      health = bad.length ? "bad" : "ok";
      if (dot) dot.className = "status-dot" + (bad.length ? " bad" : "");
      if (text) text.textContent = bad.length ? ("就绪异常 " + bad.length + " 项") : "运行正常";
    } catch (e) {
      if (dot) dot.className = "status-dot bad";
      if (text) text.textContent = "就绪接口不可达";
    }
    // 栏底只剩一个头像：状态文字在菜单里，头像角上那一点是它唯一的可见处。
    // data-health 同时驱动那个角标，两处读的是同一个结论，不会一个绿一个红。
    var foot = $("#foot-user");
    if (foot) foot.setAttribute("data-health", health);
    var userBtn = $("#btn-user");
    if (userBtn && text) userBtn.title = text.textContent + " · 账号与状态";
  }

  /* 头像里的字：英文取首字母，中文取首字。别按字节切——中文会切出半个字。 */
  function initialOf(subject) {
    var chars = Array.from(String(subject || "").trim());
    if (!chars.length) return "?";
    return /[a-z]/.test(chars[0]) ? chars[0].toUpperCase() : chars[0];
  }

  async function loadMe() {
    // 登录名只有一处：/api/v1/me 的 subject（nginx Basic Auth 的 $remote_user）。
    // 角色、管理员标记进菜单——栏底那点宽度只够放名字。
    var ident = $("#sidebar-ident");
    var avatar = $("#foot-avatar");
    var menuName = $("#menu-name");
    var menuSub = $("#menu-sub");
    try {
      var me = await api("/api/v1/me");
      state.me = me;
      state.admin = me.is_platform_admin === true ||
        (me.roles || []).indexOf("platform_admin") >= 0;
      var subject = me.subject || "未知身份";
      if (ident) ident.textContent = subject;
      if (avatar) avatar.textContent = initialOf(subject);
      if (menuName) menuName.textContent = subject;
      if (menuSub) {
        var bits = [];
        if ((me.roles || []).length) bits.push(me.roles.join(", "));
        if (state.admin) bits.push("平台管理员");
        menuSub.textContent = bits.join(" · ") || "无角色";
        // 角色多了会被截断：悬停看全文，别让人以为就这几个字。
        menuSub.title = menuSub.textContent;
      }
      $("#chip-tenant").textContent = "租户 " + (me.tenant_id || "—");
    } catch (e) {
      state.admin = false;
      if (ident) ident.textContent = "身份读取失败";
      if (avatar) avatar.textContent = "!";
      if (menuName) menuName.textContent = "身份未知";
      if (menuSub) {
        menuSub.textContent = errText(e);
        // 出错信息可能很长：菜单里截断，悬停看全文。
        menuSub.title = menuSub.textContent;
      }
      $("#chip-tenant").textContent = "租户 —";
    }
  }

  /* ---------------- 账号菜单 ----------------
   *
   * 触发器只有一个（头像那一行），菜单只有一份：两个入口就得保证两处状态同步，
   * 而「从哪点开、从哪收起」走出两种状态是最难查的那类 bug。
   *
   * 位置不用 CSS 定：收起态那一栏只有 58px 宽，任何贴着按钮的绝对定位都会被
   * .sidebar 的 overflow 裁掉半张。菜单是 fixed 的，坐标按按钮实测矩形给出。
   */
  var footMenuOpen = false;

  function placeFootMenu() {
    var btn = $("#btn-user");
    var menu = $("#foot-menu");
    if (!btn || !menu) return;
    var box = btn.getBoundingClientRect();
    var width = menu.offsetWidth || 190;
    // 两个方向都要夹：窄屏时按钮靠右，菜单往左让；收起态按钮在左，left 落到 8。
    var left = Math.min(box.left, window.innerWidth - width - 8);
    menu.style.left = Math.max(8, Math.round(left)) + "px";
    menu.style.bottom = Math.round(window.innerHeight - box.top + 6) + "px";
  }

  function setFootMenu(open) {
    var menu = $("#foot-menu");
    var btn = $("#btn-user");
    if (!menu || !btn) return;
    footMenuOpen = open;
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) return;
    placeFootMenu();
    var first = menu.querySelector(".foot-menu-item");
    if (first) first.focus();
  }

  function closeFootMenu() {
    if (footMenuOpen) setFootMenu(false);
  }

  /* 退出登录。
   *
   * 认证在 nginx（HTTP Basic），服务端没有会话可注销：凭据由浏览器缓存在
   * 「origin + realm」上，活到这个进程结束。刷新页面、清 localStorage 都不会
   * 让它失效。唯一能让它失效的办法，是用一组错凭据把它覆盖掉——浏览器在
   * 下一次请求时才会重新弹登录框。
   *
   * 所以这里跳转到一个带错凭据的地址，而不是「清本地状态、把界面画成未登录」：
   * 后者点完看着像退出了，刷新一下身份又回来了，比不放这个按钮更糟。
   */
  function signOut() {
    if (!window.confirm("退出登录？浏览器会重新弹出登录框，需要再输一次账号密码。")) return;
    // 上一个身份留下的构建助手线程不属于这台机器上的下一个人。
    try { window.localStorage.removeItem(STUDIO_THREAD_KEY); } catch (e) { /* 隐私模式 */ }
    location.href = location.protocol + "//logout:logout@" + location.host + "/";
  }

  var lastRoute = null;

  async function render() {
    var id = currentRoute();
    markNav(id);

    // 页面切换时先把上一条页面的轮询停掉。横条自己也检查容器还在不在，
    // 但那是兜底：靠它停会让定时器在别的页面上白跑 5 秒一次。
    stopLive();

    // 离开观测页就丢掉详情状态：否则从别的页面点「观测」会直接落到
    // 上次打开的那条 trace，而它可能已经被保留期策略清掉了。
    if (id !== "observe" && lastRoute === "observe") {
      obs.detail = null;
      obs.operation = null;
      obs.payload = null;
      obs.openSpan = null;
    }
    lastRoute = id;

    var view = $("#view");
    // 对话页要独占一屏（输入框贴底），其它页照旧留 72px 滚动余量。
    // 类挂在常驻的 #view 上，所以每次渲染都要重新判一次，不能只加不减。
    view.classList.toggle("view-chat", id === "chat");
    view.innerHTML = "";
    var page = PAGES[id];
    try {
      await page.mount(view);
    } catch (e) {
      view.innerHTML = pageHead({
        eyebrow: page.eyebrow, title: page.title, desc: page.desc
      }) + '<div class="card"><div class="banner err">页面渲染失败：' + esc(errText(e)) + "</div></div>";
    }
  }

  /* ================= 智能体 ================= */
  //
  // 这一页要回答「组织里有谁在替人做事、各自能碰什么」，
  // 而不是「配了几条提示词」。所以每行都把边界摊开写清楚：
  // 只显示名字的管理页用起来和一张表格没区别，却会让人误以为
  // 「配了就等于限制住了」——而边界只有落到工具目录与执行拦截上才算数。

  var agentUI = { items: [], canManage: false, counts: {} };

  function agentBoundaryText(item) {
    // 内置助手的边界是固定的：列成「工具 4 个 · 可写」会让人以为它也能调业务工具。
    if (item.is_builtin) return "平台管理工具 · 边界由平台固定";
    var parts = [];
    if (item.allowed_tools === null || item.allowed_tools === undefined) {
      parts.push("工具：跟随身份可见范围");
    } else if (!item.allowed_tools.length) {
      parts.push("工具：无");
    } else {
      parts.push("工具：" + item.allowed_tools.length + " 个");
    }

    if (item.allowed_skills !== null && item.allowed_skills !== undefined) {
      parts.push("技能：" + (item.allowed_skills.length ? item.allowed_skills.length + " 个" : "无"));
    }
    parts.push(item.can_write ? "可写" : "只读");
    // 委派预算只在对它有要求时才占一行：大多数智能体走平台默认，
    // 每张卡都写「默认 2 跳」等于让真正收窄过的那一个看不出来。
    if (item.max_delegation_depth === 0) {
      parts.push("不转交");
    } else if (item.max_delegation_depth > 0) {
      parts.push("转交≤" + item.max_delegation_depth + "跳");
    }
    if (item.delegation_budget === 0) {
      parts.push("本轮不转交");
    } else if (item.delegation_budget > 0) {
      parts.push("每轮≤" + item.delegation_budget + "次");
    }
    return parts.join(" · ");
  }

  function agentStatusChip(status) {
    if (status === "active") return '<span class="chip ok">已启用</span>';
    if (status === "draft") return '<span class="chip warn">草稿</span>';
    return '<span class="chip">已停用</span>';
  }

  /* 卡片头那个字母方块。名字的第一个字比通用图标有用：
     一屏七八张卡时，靠它一眼分辨比读名字快。内置助手用深色块，
     和「它可以改平台」这件事在视觉上区分开。 */
  function agentMonogram(item) {
    var label = (item.display_name || item.name || "?").trim();
    var first = label.slice(0, 1).toUpperCase();
    return '<span class="agent-mono' + (item.is_builtin ? " builtin" : "") +
      '" aria-hidden="true">' + esc(first) + "</span>";
  }

  function agentCard(item) {
    var ops = "";
    // 内置助手不给任何写入口：点了才报 400 是更差的体验，
    // 而且「编辑」弹窗会提交整个边界，后端必然拒绝。
    if (agentUI.canManage && !item.is_builtin) {
      ops += '<button class="linkish" type="button" data-agent-edit="' + esc(item.name) + '">编辑</button>';
      if (!item.is_default) {
        if (item.status === "active") {
          ops += '<button class="linkish danger" type="button" data-agent-status="' +
            esc(item.name) + '" data-to="disabled">停用</button>';
        } else {
          ops += '<button class="linkish" type="button" data-agent-status="' +
            esc(item.name) + '" data-to="active">启用</button>';
        }
        if (item.status === "disabled") {
          ops += '<button class="linkish danger" type="button" data-agent-del="' +
            esc(item.name) + '">删除</button>';
        }
      }
    }

    return '<article class="agent-card' + (item.usable ? "" : " off") + '">' +
      '<div class="agent-card-head">' + agentMonogram(item) +
      '<div class="agent-card-id">' +
      '<div class="agent-title"><span class="agent-name">' +
      esc(item.display_name || item.name) + "</span>" +
      (item.is_default ? '<span class="chip">默认</span>' : "") +
      (item.is_builtin ? '<span class="chip">内置</span>' : "") +
      agentStatusChip(item.status) + "</div>" +
      '<code class="agent-code">' + esc(item.name) + "</code>" +
      "</div></div>" +
      // 说明留三行，超出裁掉：卡片高度靠内容撑，但一屏里长短不一会很难扫。
      '<p class="agent-desc' + (item.description ? "" : " faint") + '">' +
      esc(item.description || "未填写职责说明") + "</p>" +
      '<div class="agent-bound"><span class="agent-bound-text">' +
      esc(agentBoundaryText(item)) + '</span><span class="agent-ver">v' +
      esc(String(item.revision)) + "</span></div>" +
      (ops ? '<div class="agent-ops">' + ops + "</div>" : "") +
      "</article>";
  }

  /* 骨架也做成卡片形状：先竖着三条、再变成卡片，比「一条细线忽然铺成网格」稳。 */
  function agentSkeletons(n) {
    var out = "";
    for (var i = 0; i < n; i += 1) {
      out += '<div class="agent-card"><div class="agent-card-head">' +
        '<div class="skeleton" style="width:30px;height:30px;border-radius:8px"></div>' +
        '<div style="flex:1"><div class="skeleton" style="width:45%"></div>' +
        '<div class="skeleton" style="width:30%;margin-top:7px"></div></div></div>' +
        '<div class="skeleton" style="width:85%;margin-top:12px"></div>' +
        '<div class="skeleton" style="width:60%;margin-top:7px"></div></div>';
    }
    return out;
  }

  function renderAgentList() {
    $("#agent-list").innerHTML = agentUI.items.length
      ? agentUI.items.map(agentCard).join("")
      : '<div class="empty"><strong>还没有智能体</strong>新建一个，或者刷新页面试试。</div>';

    $("#agent-stats").innerHTML =
      stat("全部", String(agentUI.items.length)) +
      stat("已启用", String(agentUI.counts.active || 0)) +
      stat("草稿", String(agentUI.counts.draft || 0), !agentUI.counts.draft) +
      stat("已停用", String(agentUI.counts.disabled || 0), !agentUI.counts.disabled);
  }

  async function loadAgents() {
    var d = await api("/api/v1/agents");
    agentUI.items = d.items || [];
    agentUI.canManage = !!d.can_manage;
    agentUI.counts = d.counts || {};
    renderAgentList();
  }

  function agentPickList(id, rows, selected) {
    if (!rows.length) {
      return '<div class="pick-list" id="' + id + '"><span class="faint">没有可选项。</span></div>';
    }
    return '<div class="pick-list" id="' + id + '">' + rows.map(function (row) {
      var checked = selected && selected.indexOf(row.value) >= 0;
      return '<label><input type="checkbox" value="' + esc(row.value) + '"' +
        (checked ? " checked" : "") + "><span>" + esc(row.label) + "</span>" +
        (row.note ? '<span class="faint">' + esc(row.note) + "</span>" : "") +
        "</label>";
    }).join("") + "</div>";
  }

  function agentChecked(id) {
    var box = $("#" + id);
    if (!box) return [];
    return Array.prototype.slice.call(box.querySelectorAll("input:checked"))
      .map(function (node) { return node.value; });
  }

  /* 数字框的原值与提交值。0 是有意义的配置，不能用 `x || null` 表达：
     那会把「不许转交」静默改成「走默认」。 */
  function agentNumValue(editing, key) {
    if (!editing) return "";
    var v = editing[key];
    return v === null || v === undefined ? "" : String(v);
  }

  function agentNumOrNull(raw) {
    var text = String(raw == null ? "" : raw).trim();
    return text === "" ? null : Number(text);
  }

  function agentSyncLimit(toggleId, listId) {
    var toggle = $("#" + toggleId);
    var list = $("#" + listId);
    if (!toggle || !list) return;
    var unlimited = toggle.checked;
    list.classList.toggle("muted-out", unlimited);
    Array.prototype.forEach.call(list.querySelectorAll("input"), function (node) {
      node.disabled = unlimited;
    });
  }

  async function openAgentModal(name) {
    var editing = null;
    if (name) {
      editing = agentUI.items.filter(function (x) { return x.name === name; })[0] || null;
      if (!editing) { toast("这个智能体已经不在列表里了", "err"); return; }
    }

    // 工具只列「当前身份看得见的」：管理员能在 Agent 上勾的东西
    // 不能超过他本人能用的范围，否则这份清单本身就是一次越权提示。
    var tools = [];
    var skills = [];
    try {
      var res = await Promise.all([
        api("/api/v1/tools"),
        api("/api/v1/skills").catch(function () { return { items: [] }; })
      ]);
      tools = (res[0] || []).map(function (t) {
        return { value: t.name, label: t.display_name || t.name, note: t.risk_level };
      });
      skills = ((res[1] && res[1].items) || []).map(function (sk) {
        return { value: sk.name, label: sk.display_name || sk.name, note: sk.enabled ? "" : "停用" };
      });
    } catch (e) {
      toast("工具/技能清单没读到，边界无法编辑：" + errText(e), "err");
      return;
    }

    var unlimitedTools = !editing || editing.allowed_tools === null ||
      editing.allowed_tools === undefined;
    var unlimitedSkills = !editing || editing.allowed_skills === null ||
      editing.allowed_skills === undefined;

    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true">' +
      '<div class="modal-head"><div>' +
      '<div class="modal-eyebrow">' + (editing ? "EDIT AGENT" : "NEW AGENT") + "</div>" +
      '<div class="modal-title">' + (editing ? "编辑智能体" : "新建智能体") + "</div>" +
      "</div><button class=\"modal-close\" type=\"button\" aria-label=\"关闭\">×</button></div>" +
      '<div class="modal-body">' +
      '<div class="banner">' + ico("shield") +
      "边界只会收窄、不会放宽：智能体看到的工具是「你的权限」与「这里勾选的范围」的交集。" +
      "关闭「可写」后写类工具连目录都不出现；打开也<b>不是</b>审批豁免，写操作照样要人确认。</div>" +
      '<div class="form-grid">' +
      '<div class="field"><label>标识（对话时用它指定）</label>' +
      '<input type="text" id="ag-name" maxlength="64" placeholder="ops" value="' +
      esc(editing ? editing.name : "") + '"' + (editing ? " disabled" : "") + ">" +
      '<span class="help">字母、数字与 - _ .；创建后不可修改，改名会破坏已接入的客户端。</span></div>' +
      '<div class="field"><label>显示名</label>' +
      '<input type="text" id="ag-display" maxlength="64" placeholder="运行值班" value="' +
      esc(editing ? editing.display_name : "") + '"></div>' +
      '<div class="field span2"><label>职责说明（会进提示词）</label>' +
      '<input type="text" id="ag-desc" maxlength="512" placeholder="只回答运行规程相关问题，不代填工单。" value="' +
      esc(editing ? editing.description : "") + '">' +
      '<span class="help">写「你负责什么」，不要写内部备注——它会被模型当成角色设定读。</span></div>' +
      '<div class="field span2"><label>补充指令（可选）</label>' +
      '<textarea id="ag-inst" rows="3" maxlength="4000" placeholder="回答时先给结论，再给依据。">' +
      esc(editing ? editing.instruction : "") + "</textarea></div>" +
      '<div class="field span2"><label class="switch"><input type="checkbox" id="ag-tools-all"' +
      (unlimitedTools ? " checked" : "") + "> 不限制工具（跟随当前身份的可见范围）</label>" +
      agentPickList("ag-tools", tools, editing ? editing.allowed_tools : null) +
      '<span class="help">取消勾选后，只列出的工具可用；一个都不选表示这个智能体没有工具。</span></div>' +
      '<div class="field span2"><label class="switch"><input type="checkbox" id="ag-skills-all"' +
      (unlimitedSkills ? " checked" : "") + "> 不限制技能</label>" +
      agentPickList("ag-skills", skills, editing ? editing.allowed_skills : null) +
      '<span class="help">被排除的技能即使命中也不注入指令、不记命中。</span></div>' +
      '<div class="field"><label>转交深度上限</label>' +
      '<input type="number" id="ag-depth" min="0" max="5" step="1" placeholder="默认 2" value="' +
      agentNumValue(editing, "max_delegation_depth") + '">' +
      '<span class="help">这个智能体最多把事转交几跳。0 = 不转交；留空走平台默认。' +
      "只能更严：整条链取最严的那一环。</span></div>" +
      '<div class="field"><label>单轮转交次数</label>' +
      '<input type="number" id="ag-budget" min="0" max="10" step="1" placeholder="默认 3" value="' +
      agentNumValue(editing, "delegation_budget") + '">' +
      '<span class="help">一轮对话里最多转交几次。0 = 本轮不转交。</span></div>' +
      '<div class="field span2"><label class="switch"><input type="checkbox" id="ag-write"' +
      (editing && editing.can_write ? " checked" : "") + "> 允许写操作</label>" +
      '<span class="help">默认关闭。开启后写类工具才出现在目录里，仍然需要人工确认。</span></div>' +
      "</div></div>" +
      '<div class="modal-foot">' +
      '<button class="btn" type="button" data-agent-cancel>取消</button>' +
      '<button class="btn primary" type="button" id="ag-save">' + ico("save") +
      (editing ? "保存" : "创建为草稿") + "</button></div></div>";

    document.body.appendChild(mask);
    agentSyncLimit("ag-tools-all", "ag-tools");
    agentSyncLimit("ag-skills-all", "ag-skills");
    $("#ag-tools-all").addEventListener("change", function () {
      agentSyncLimit("ag-tools-all", "ag-tools");
    });
    $("#ag-skills-all").addEventListener("change", function () {
      agentSyncLimit("ag-skills-all", "ag-skills");
    });

    function close() { mask.remove(); }
    mask.querySelector(".modal-close").addEventListener("click", close);
    mask.querySelector("[data-agent-cancel]").addEventListener("click", close);
    mask.addEventListener("click", function (ev) { if (ev.target === mask) close(); });

    $("#ag-save").addEventListener("click", async function () {
      var payload = {
        display_name: $("#ag-display").value.trim(),
        description: $("#ag-desc").value.trim(),
        instruction: $("#ag-inst").value.trim(),
        can_write: $("#ag-write").checked,
        // 显式传 null = 不收窄；传数组 = 只允许这些。空数组是「一个都不给」，
        // 与 null 完全相反，所以这里绝不能写成 `picked || null`。
        allowed_tools: $("#ag-tools-all").checked ? null : agentChecked("ag-tools"),
        allowed_skills: $("#ag-skills-all").checked ? null : agentChecked("ag-skills"),
        // 空框传 null = 回到平台默认（不是 0）。这里不能用 `|| null`：
        // 0 是「不许转交」这个开关，被吞掉就会静默变回默认值。
        max_delegation_depth: agentNumOrNull($("#ag-depth").value),
        delegation_budget: agentNumOrNull($("#ag-budget").value)
      };

      try {
        if (editing) {
          await api("/api/v1/agents/" + encodeURIComponent(editing.name), {
            method: "PATCH",
            body: payload
          });
          toast("已保存");
        } else {
          payload.name = $("#ag-name").value.trim();
          await api("/api/v1/agents", { method: "POST", body: payload });
          toast("已创建为草稿，确认边界后点「启用」");
        }
        close();
        await loadAgents();
      } catch (e) {
        toast("保存失败：" + errText(e), "err");
      }
    });

    var nameInput = $("#ag-name");
    if (nameInput && !editing) nameInput.focus();
  }

  function mountAgents(root) {
    root.innerHTML = pageHead({
      eyebrow: PAGES.agents.eyebrow,
      title: PAGES.agents.title,
      desc: PAGES.agents.desc,
      actions: '<button class="btn" type="button" id="agent-refresh">' + ico("refresh") + "刷新</button>" +
        '<button class="btn primary" type="button" id="agent-new">' + ico("plus") + "新建智能体</button>"
    }) +
      '<div class="stats" id="agent-stats">' + statSkeletons(4) + "</div>" +
      // 外层不再是白卡片：卡片本身就是内容，套两层边框会变成「卡里的卡」，
      // 一屏几张的时候看着像嵌错层。
      '<section class="agent-section">' +
      cardHead("智能体清单", "flow", '<span class="chip" id="agent-count">—</span>') +
      '<div id="agent-list" class="agent-grid">' + agentSkeletons(3) + "</div>" +
      '<p class="hint">「默认」那个不能停用也不能删：不带智能体的请求全部落到它身上，' +
      "有两个就等于「不传时行为不确定」。停用智能体后，它已发起的待审批操作也不会再执行。</p>" +
      "</section>";

    $("#agent-refresh").addEventListener("click", function () { mountAgents(root); });
    $("#agent-new").addEventListener("click", function () {
      if (!agentUI.canManage) { toast("缺少 agent:write 权限", "err"); return; }
      openAgentModal(null);
    });

    $("#agent-list").addEventListener("click", async function (ev) {
      var target = ev.target.closest ? ev.target.closest("button") : null;
      if (!target) return;

      var editName = target.getAttribute("data-agent-edit");
      if (editName) { openAgentModal(editName); return; }

      var statusName = target.getAttribute("data-agent-status");
      if (statusName) {
        var to = target.getAttribute("data-to");
        if (to === "disabled" && !window.confirm("停用后该智能体不能再被对话选中，已发起的待审批操作也不会执行。确定？")) return;
        try {
          await api("/api/v1/agents/" + encodeURIComponent(statusName) + "/status", {
            method: "POST", body: { status: to }
          });
          toast(to === "active" ? "已启用" : "已停用");
          await loadAgents();
        } catch (e) { toast("操作失败：" + errText(e), "err"); }
        return;
      }

      var delName = target.getAttribute("data-agent-del");
      if (delName) {
        if (!window.confirm("删除后不可恢复（审计记录保留）。确定删除？")) return;
        try {
          await api("/api/v1/agents/" + encodeURIComponent(delName), { method: "DELETE" });
          toast("已删除");
          await loadAgents();
        } catch (e) { toast("删除失败：" + errText(e), "err"); }
      }
    });

    if (!agentUI.canManage) $("#agent-new").hidden = true;

    loadAgents().then(function () {
      var count = $("#agent-count");
      if (count) count.textContent = agentUI.items.length + " 个";
    }).catch(function (e) {
      $("#agent-list").innerHTML = '<div class="banner err">加载失败：' + esc(errText(e)) + "</div>";
    });
  }

  /* ================= 从文件新建（统一导入） =================
   *
   * 一个入口覆盖三类产物：知识库文档、技能、OpenAPI 工具。
   *
   * 为什么不是三张页面各挂一个上传钮：判断「这份文件该建成什么」的依据是
   * **文件内容**，不是后缀——现场规程和技能都是 `.md`，工具导出和 OpenAPI
   * 都是 `.json`。让人先自己分类，等于把机器的活推给人，判错还要改名重传。
   *
   * 识别结果一律显示出来并允许改：自动识别会判错（比如把带 front matter
   * 的规程认成技能），此时人要能就地改目标，而不是把文件搬来搬去。
   *
   * 预览走后端 `dry_run`，与真正导入是同一个解析函数。前端另算一遍预览，
   * 就会出现「预览 3 个接口、实际存 1 个」——而那是导入之后才发现的错。
   */

  var IMPORT_TARGET_LABEL = {
    auto: "自动识别",
    knowledge: "知识库文档",
    skill: "技能",
    tool: "工具接口"
  };

  var importUI = {
    mask: null,
    rows: [],
    seq: 0,
    busy: false,
    targets: null,
    // 新加文件时用的默认目标。改它只影响之后加进来的文件，
    // 不回头改已经识别过的行——那会让人以为自己刚才的选择被推翻了。
    target: "auto"
  };

  function importTargetLabel(value) {
    return IMPORT_TARGET_LABEL[value] || value;
  }

  /* multipart 不能用 api()：它会把非字符串 body 当 JSON 序列化，
     FormData 会被毁掉（知识库上传那条路踩过同样的坑）。 */
  async function importPost(file, target, dryRun, options) {
    var form = new FormData();
    form.append("file", file, file.name);
    form.append("target", target || "auto");
    form.append("dry_run", dryRun ? "true" : "false");

    if (options && Object.keys(options).length) {
      form.append("options", JSON.stringify(options));
    }

    var res = await fetch("/api/v1/studio/import", {
      method: "POST",
      body: form,
      credentials: "same-origin"
    });
    var text = await res.text();
    var payload = null;
    if (text) { try { payload = JSON.parse(text); } catch (e) { payload = null; } }

    if (!res.ok) {
      var detail = payload && payload.detail;
      if (detail && typeof detail !== "string") detail = JSON.stringify(detail);
      var err = new Error(detail || ("HTTP " + res.status));
      err.status = res.status;
      throw err;
    }

    return payload;
  }

  /* 行的 options 只装「人选过的东西」，不装默认值：
     标题没填就不传 title，让后端按文件名推；一律传空串会把标题清成空的。 */
  function importRowOptions(row) {
    var out = {};
    var picked = row.picked || {};

    if (picked.title && picked.title.trim()) out.title = picked.title.trim();
    if (picked.name && picked.name.trim()) out.name = picked.name.trim();
    if (picked.overwrite === false) out.overwrite = false;

    if (row.preview && row.preview.tools && picked.operations) {
      out.operations = picked.operations;
    }

    return out;
  }

  async function importDetect(row) {
    row.busy = true;
    row.error = "";
    row.result = null;
    importRenderRow(row);

    try {
      var data = await importPost(row.file, row.target, true, importRowOptions(row));
      row.detected = data.detected_target;
      row.preview = data.preview || null;
      row.notes = data.notes || [];

      // 工具默认全选：OpenAPI 里几十个 operation 时，逐个勾选是负担；
      // 但一导入就全注册也不对，所以给一份「先全选、可去掉」的清单。
      if (row.preview && row.preview.tools && !row.picked.operations) {
        row.picked.operations = row.preview.tools.map(function (tool) { return tool.name; });
      }
    } catch (e) {
      row.error = errText(e);
      row.preview = null;
    }

    row.busy = false;
    importRenderRow(row);
    importSyncFoot();
  }

  function importRowHead(row) {
    var detected = row.detected
      ? '<span class="chip sm">识别为 ' + esc(importTargetLabel(row.detected)) + "</span>"
      : "";

    var options = Object.keys(IMPORT_TARGET_LABEL).map(function (value) {
      return '<option value="' + value + '"' +
        (row.target === value ? " selected" : "") + ">" +
        esc(IMPORT_TARGET_LABEL[value]) + "</option>";
    }).join("");

    return '<div class="import-row-head">' +
      '<span class="import-file" title="' + esc(row.file.name) + '">' + esc(row.file.name) + "</span>" +
      '<span class="import-size">' + esc(fmtBytes(row.file.size)) + "</span>" +
      detected +
      '<select class="import-target" data-act="target" aria-label="导入到">' + options + "</select>" +
      '<button class="icon-btn" type="button" data-act="remove" title="移除" aria-label="移除">✕</button>' +
      "</div>";
  }

  function importResolvedTarget(row) {
    if (row.target && row.target !== "auto") return row.target;
    if (row.detected) return row.detected;
    // 还没识别完就按知识库渲染：它是约束最松的一类，闪一下也不会误导，
    // 总比先画出一屏「接口清单」再跳掉好。
    return "knowledge";
  }

  function importNotesHtml(notes) {
    if (!notes || !notes.length) return "";
    return '<ul class="import-notes">' + notes.map(function (note) {
      return "<li>" + esc(note) + "</li>";
    }).join("") + "</ul>";
  }

  function importKnowledgeBody(row) {
    var preview = row.preview || {};
    return '<div class="import-form">' +
      '<div class="field"><label>标题</label>' +
      '<input type="text" data-act="title" value="' + esc(row.picked.title || "") +
      '" placeholder="' + esc(preview.title || "") + '">' +
      '<span class="help">留空按文件名。同一个标题重传同样的内容不会重复入库。</span></div>' +
      "</div>" +
      '<div class="import-meta">' + esc(String(preview.chars || 0)) + " 字 · 分级 " +
      esc(preview.access_scope || "internal") + "</div>" +
      (preview.excerpt
        ? '<pre class="import-excerpt">' + esc(preview.excerpt) + "</pre>"
        : "");
  }

  function importSkillBody(row) {
    var skills = (row.preview && row.preview.skills) || [];
    var skill = skills[0] || {};
    var keywords = (skill.trigger_keywords || []).map(function (word) {
      return '<span class="chip sm">' + esc(word) + "</span>";
    }).join(" ");

    var exists = skill.exists
      ? '<div class="banner warn sm">同名技能已存在，导入会覆盖它（指令、触发词、允许工具）。</div>'
      : "";

    var extra = skills.length > 1
      ? '<div class="import-meta">这份文件里有 ' + skills.length + " 个技能：" +
        esc(skills.map(function (item) { return item.name; }).join("、")) + "</div>"
      : "";

    return exists + extra +
      '<div class="import-form">' +
      '<div class="field"><label>技能 ID</label>' +
      '<input type="text" data-act="name" value="' + esc(row.picked.name || "") +
      '" placeholder="' + esc(skill.name || "") + '">' +
      '<span class="help">留空用 front matter 里的 name。从同一份模板建第二个技能时改这里。</span></div>' +
      "</div>" +
      '<div class="import-meta">' + esc(skill.display_name || "") +
      (skill.description ? " · " + esc(skill.description) : "") +
      " · 正文 " + esc(String(skill.instruction_chars || 0)) + " 字</div>" +
      (keywords ? '<div class="import-keywords">' + keywords + "</div>" : "") +
      (skill.instruction_head
        ? '<pre class="import-excerpt">' + esc(skill.instruction_head) + "</pre>"
        : "");
  }

  function importToolBody(row) {
    var preview = row.preview || {};
    var tools = preview.tools || [];
    var picked = row.picked.operations || [];

    var items = tools.map(function (tool) {
      var on = picked.indexOf(tool.name) >= 0;
      var risk = tool.risk_level === "destructive"
        ? '<span class="chip err sm">不可逆</span>'
        : (tool.risk_level === "write" ? '<span class="chip warn sm">写</span>' : "");

      return '<label class="import-op' + (on ? " on" : "") + '">' +
        '<input type="checkbox" data-act="op" data-op="' + esc(tool.name) + '"' +
        (on ? " checked" : "") + ">" +
        '<span class="import-op-method">' + esc(tool.method) + "</span>" +
        '<span class="import-op-name" title="' + esc(tool.url) + '">' + esc(tool.name) + "</span>" +
        risk +
        '<span class="import-op-params">' + esc(String(tool.params_count)) + " 参数</span>" +
        "</label>";
    }).join("");

    var base = preview.base_url
      ? '<div class="import-meta">服务地址 <span class="mono">' + esc(preview.base_url) + "</span></div>"
      : '<div class="banner warn sm">规格里没有绝对服务地址，导入后工具 URL 会是相对路径，调用必然失败。' +
        "请在规格的 servers[0].url 里写完整地址。</div>";

    return base +
      '<div class="import-ops">' + items + "</div>" +
      '<div class="import-meta">勾选的 ' + picked.length + " / " + tools.length + " 个接口会注册成工具。</div>";
  }

  function importRowBody(row) {
    if (row.busy) return '<div class="skeleton" style="width:52%"></div>';

    if (row.error) {
      return '<div class="banner err">' + esc(row.error) + "</div>";
    }

    if (row.done) {
      var message = (row.result && row.result.message) || "已完成";
      var items = (row.result && row.result.items) || [];
      var lines = items.length
        ? '<ul class="import-notes">' + items.map(function (item) {
            return "<li>" + esc(item.name) + "：" + esc(item.message || item.action) + "</li>";
          }).join("") + "</ul>"
        : "";
      return '<div class="banner">' + esc(message) + "</div>" + lines;
    }

    if (!row.preview) return '<div class="hint">即将识别…</div>';

    // 按**实际会写进去的那一类**渲染，而不是下拉框里存的值：
    // 选「自动识别」时 row.target 是 auto，拿它分支会一律落到工具预览上，
    // 症状是「识别为技能，下面却是接口清单」。
    var kind = importResolvedTarget(row);
    var body = "";
    if (kind === "knowledge") body = importKnowledgeBody(row);
    else if (kind === "skill") body = importSkillBody(row);
    else body = importToolBody(row);

    return body + importNotesHtml(row.notes);
  }

  function importRenderRow(row) {
    var node = importUI.mask && importUI.mask.querySelector('[data-row="' + row.id + '"]');
    if (!node) return;

    node.className = "import-row" + (row.done ? " done" : "") + (row.error ? " bad" : "");
    node.innerHTML = importRowHead(row) +
      '<div class="import-row-body">' + importRowBody(row) + "</div>";
  }

  function importSyncFoot() {
    if (!importUI.mask) return;

    var pending = importUI.rows.filter(function (row) {
      return !row.done && !row.busy && !row.error;
    });

    var btn = importUI.mask.querySelector("#import-run");
    btn.disabled = importUI.busy || !pending.length;
    btn.textContent = importUI.busy
      ? "导入中…"
      : (pending.length ? "导入 " + pending.length + " 份" : "没有待导入的文件");

    var empty = importUI.mask.querySelector("#import-empty");
    empty.hidden = importUI.rows.length > 0;
  }

  async function importAddFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;

    var fresh = [];

    files.forEach(function (file) {
      // 同名同大小视为重复：拖两次同一个文件是最常见的手滑。
      var dup = importUI.rows.some(function (row) {
        return row.file.name === file.name && row.file.size === file.size;
      });
      if (dup) return;

      var row = {
        id: "f" + (++importUI.seq),
        file: file,
        target: importUI.target,
        detected: "",
        preview: null,
        notes: [],
        picked: {},
        error: "",
        busy: false,
        done: false,
        result: null
      };

      importUI.rows.push(row);
      fresh.push(row);

      var node = document.createElement("div");
      node.className = "import-row";
      node.setAttribute("data-row", row.id);
      importUI.mask.querySelector("#import-list").appendChild(node);
      importRenderRow(row);
    });

    importSyncFoot();

    // 串行识别：并行发 N 个 dry_run 在弱网下只会把进度条搅乱，
    // 而这份清单通常只有几个文件。
    for (var i = 0; i < fresh.length; i += 1) {
      await importDetect(fresh[i]);
    }
  }

  function importRemoveRow(row) {
    importUI.rows = importUI.rows.filter(function (item) { return item !== row; });
    var node = importUI.mask.querySelector('[data-row="' + row.id + '"]');
    if (node) node.remove();
    importSyncFoot();
  }

  async function importRunAll() {
    var queue = importUI.rows.filter(function (row) {
      return !row.done && !row.busy && !row.error;
    });

    if (!queue.length) return;

    importUI.busy = true;
    importSyncFoot();

    var ok = 0;
    var failed = 0;

    for (var i = 0; i < queue.length; i += 1) {
      var row = queue[i];
      row.busy = true;
      importRenderRow(row);

      try {
        var data = await importPost(row.file, row.target, false, importRowOptions(row));
        row.result = data.result || null;
        row.done = true;
        ok += 1;
      } catch (e) {
        row.error = errText(e);
        failed += 1;
      }

      row.busy = false;
      importRenderRow(row);
    }

    importUI.busy = false;
    importSyncFoot();

    if (ok) {
      toast("已导入 " + ok + " 份");
      refreshAfterImport();
    }
    if (failed) toast(failed + " 份没导进去，看列表里的原因", "err");
  }

  /* 导入完成后只有这三页会变：别的页面重绘一次既没必要，还会打断人正在看的
     内容（比如对话台的历史滚动位置）。 */
  function refreshAfterImport() {
    var route = currentRoute();
    if (route === "knowledge" || route === "skills" || route === "tools") render();
  }

  function closeImport() {
    if (!importUI.mask) return;
    importUI.mask.remove();
    importUI.mask = null;
    importUI.rows = [];
    importUI.busy = false;
    document.removeEventListener("keydown", onImportKey, true);
  }

  function onImportKey(ev) {
    if (ev.key === "Escape") {
      ev.preventDefault();
      closeImport();
    }
  }

  function openImportDialog() {
    if (importUI.mask) {
      importUI.mask.querySelector("input[type=file]").click();
      return;
    }

    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML =
      '<div class="modal import-modal" role="dialog" aria-modal="true" aria-labelledby="import-title">' +
      '<div class="modal-head"><div>' +
      '<div class="modal-eyebrow">NEW FROM FILE</div>' +
      '<div class="modal-title" id="import-title">从文件新建</div>' +
      "</div>" +
      '<button class="modal-close" type="button" id="import-close" aria-label="关闭">×</button></div>' +
      '<div class="modal-body">' +
      '<div class="import-drop" id="import-drop" tabindex="0" role="button">' +
      '<div class="import-drop-title">把文件拖到这里，或点击选择</div>' +
      '<div class="import-drop-hint">知识库文档 .pdf/.docx/.md/.txt/.html · ' +
      "技能 .md（带 front matter）· 工具 OpenAPI 3 / Swagger 2 的 .json/.yaml</div>" +
      "</div>" +
      '<div class="import-bar">' +
      '<label class="import-bar-label">导入到</label>' +
      '<select id="import-target">' +
      Object.keys(IMPORT_TARGET_LABEL).map(function (value) {
        return '<option value="' + value + '">' + esc(IMPORT_TARGET_LABEL[value]) + "</option>";
      }).join("") +
      "</select>" +
      '<span class="hint" id="import-target-hint">按内容判断，判得不对可以在下面逐个改。</span>' +
      "</div>" +
      '<div id="import-list" class="import-list"></div>' +
      '<div class="empty sm" id="import-empty"><strong>还没有文件</strong>' +
      "一次可以拖多份，各自判断、各自导入。</div>" +
      "</div>" +
      '<div class="modal-foot">' +
      '<button class="btn" type="button" id="import-cancel">关闭</button>' +
      '<button class="btn primary" type="button" id="import-run" disabled>没有待导入的文件</button>' +
      "</div>" +
      '<input type="file" id="import-file" multiple hidden>' +
      "</div>";

    document.body.appendChild(mask);
    importUI.mask = mask;

    var drop = mask.querySelector("#import-drop");
    var picker = mask.querySelector("#import-file");

    mask.querySelector("#import-close").addEventListener("click", closeImport);
    mask.querySelector("#import-cancel").addEventListener("click", closeImport);
    mask.querySelector("#import-run").addEventListener("click", importRunAll);

    mask.querySelector("#import-target").addEventListener("change", function () {
      importUI.target = this.value;
    });

    drop.addEventListener("click", function () { picker.click(); });
    drop.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        picker.click();
      }
    });

    picker.addEventListener("change", function () {
      // 先取快照再清空：`picker.files` 是活引用，`value = ""` 会把它一并清掉，
      // 清完再读就是空列表——表现是「选完文件什么都没发生」。
      var files = Array.prototype.slice.call(picker.files || []);
      picker.value = "";
      importAddFiles(files);
    });

    // dragover 必须 preventDefault，否则浏览器会直接用文件替换整个页面。
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (ev) {
        ev.preventDefault();
        drop.classList.add("over");
      });
    });

    ["dragleave", "dragend"].forEach(function (name) {
      drop.addEventListener(name, function () {
        drop.classList.remove("over");
      });
    });

    drop.addEventListener("drop", function (ev) {
      ev.preventDefault();
      drop.classList.remove("over");
      importAddFiles(ev.dataTransfer && ev.dataTransfer.files);
    });

    // 行内的改动与移除都走委托：行会被整块重绘，绑在行上的监听会跟着丢。
    mask.querySelector("#import-list").addEventListener("click", function (ev) {
      var act = ev.target.closest("[data-act]");
      if (!act) return;

      var rowNode = act.closest("[data-row]");
      var row = importUI.rows.filter(function (item) {
        return item.id === rowNode.getAttribute("data-row");
      })[0];
      if (!row) return;

      if (act.getAttribute("data-act") === "remove") {
        importRemoveRow(row);
      }
    });

    mask.querySelector("#import-list").addEventListener("change", function (ev) {
      var act = ev.target.closest("[data-act]");
      if (!act) return;

      var rowNode = act.closest("[data-row]");
      var row = importUI.rows.filter(function (item) {
        return item.id === rowNode.getAttribute("data-row");
      })[0];
      if (!row) return;

      var kind = act.getAttribute("data-act");

      if (kind === "target") {
        row.target = act.value;
        // 换目标要重新预览：知识库看字符数、技能看正文、工具看接口清单，
        // 三者的预览结构完全不同，拿旧的顶上会显示成另一类东西。
        importDetect(row);
        return;
      }

      if (kind === "op") {
        var name = act.getAttribute("data-op");
        var list = row.picked.operations || [];

        if (act.checked) {
          if (list.indexOf(name) < 0) list.push(name);
        } else {
          list = list.filter(function (item) { return item !== name; });
        }

        row.picked.operations = list;
        act.closest(".import-op").classList.toggle("on", act.checked);
        importRenderRow(row);
        return;
      }
    });

    // 文本输入只在 input 里记值，不重绘：重绘会把光标顶到末尾，
    // 改一个字的标题能被弹回行首。
    mask.querySelector("#import-list").addEventListener("input", function (ev) {
      var act = ev.target.closest("[data-act]");
      if (!act) return;

      var rowNode = act.closest("[data-row]");
      var row = importUI.rows.filter(function (item) {
        return item.id === rowNode.getAttribute("data-row");
      })[0];
      if (!row) return;

      var kind = act.getAttribute("data-act");
      if (kind === "title") row.picked.title = act.value;
      if (kind === "name") row.picked.name = act.value;
    });

    // 不自动弹系统选文件框：拖拽才是最常用的入口，一打开弹两个窗
    // 会让人先去关掉那个框再回来拖。
    mask.querySelector("#import-target").value = importUI.target;

    document.addEventListener("keydown", onImportKey, true);
    importSyncFoot();
  }

  /* ================= 构建助手 =================
   *
   * 常驻在控制台右下角的抽屉（形态参考 Agent Studio 的「构建助手」）。
   * 它不是新入口：走的是同一个 /api/v1/chat，只是固定带 agent=<内置助手>，
   * 因此审计、观测、限流、审批全都自动生效。
   *
   * 确认卡片内联在消息流里，不跳审批页：构建助手是「说一句、改一下」的即时闭环，
   * 每次都要切页面找那一行，人就会开始闭眼点确认——那比没有确认更糟。
   */

  var STUDIO_THREAD_KEY = "pap.studio.thread";
  var STUDIO_SIZE_KEY = "pap.studio.expanded";

  var studioUI = {
    open: false,
    busy: false,
    /* 大窗（占满右侧可用高度）。默认 400px 是为了不遮主内容，
       要对着长清单改配置时才需要放大；两个尺寸都不是「全屏」——
       全屏会让人以为这是另一个页面。 */
    expanded: false,
    /* 有新内容但用户正滚在上面看历史：不把他拽下去，只在刷新按钮上点个记号。 */
    unseen: false,
    messages: [],
    /* 待发附件。和主对话分开存：两个入口各自选各自的文件，
       混用一个数组会让「在对话台选的图」莫名出现在构建助手里。 */
    attachments: [],
    /* 历史只尝试读一次；失败时放回 false，下次打开再试。 */
    historyTried: false,
    focus: "",
    agents: [],
    agentName: ""
  };

  function studioThread() {
    /* 一个浏览器一个固定 thread：接着上次接着聊，而不是每次都从零开始。
       存坏或读不出来就新建一个，最坏结果是丢一段上下文，不涉及任何权限。 */
    try {
      var saved = window.localStorage.getItem(STUDIO_THREAD_KEY);
      if (saved) return saved;
    } catch (e) { /* 隐私模式下 localStorage 会抛异常 */ }

    var d = new Date();
    var pad = function (n) { return String(n).padStart(2, "0"); };
    var id = "studio-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) +
      "-" + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds());
    try { window.localStorage.setItem(STUDIO_THREAD_KEY, id); } catch (e) { /* 同上 */ }
    return id;
  }

  function renderStudioTray() {
    paintTray($("#studio-tray"), studioUI.attachments || []);

    /* 有附件时把占位语换成「说明要做什么」：只发文件不发话会被拦下，
       而输入框空着的时候，占位语正是唯一能提前说清这件事的地方。 */
    var input = $("#studio-input");
    if (input && !input.value) {
      input.placeholder = (studioUI.attachments || []).length
        ? "说说要这份文件做什么…"
        : "今天帮你做些什么？可创建、配置或排查 Agent…";
    }
    // 托盘变化也会改变发送按钮该不该亮（有附件就算有内容）。
    updateStudioSend();
  }

  /* 面板内的提示与报错。

     以前这些都走 toast：三秒后消失，用户回头想核对「它到底报了什么」，
     就只剩一句「刚才好像红了一下」。改成落进消息流的一种独立卡片——
     停在他操作的那一轮旁边，可以回看、可以选中复制。
     卡片不挂「构建助手」的头像与气泡样式：助手并没有说过这句话，
     画成回答会让人以为模型改了配置或者给了结论。 */
  function studioNotice(text, level) {
    var body = String(text == null ? "" : text).trim();
    if (!body) return null;

    var last = studioUI.messages[studioUI.messages.length - 1];
    /* 同一句话连着来（反复点麦克风、连续拖进两个不支持的文件）不堆卡片，
       只把次数记在角上。重复十张一样的红卡会把真正的回答顶出视野。 */
    if (last && last.kind === "notice" && last.text === body) {
      last.count = (last.count || 1) + 1;
      last.at = new Date().toISOString();
      renderStudioLog();
      return last;
    }

    var item = {
      role: "agent", kind: "notice",
      level: level === "err" ? "err" : (level === "ok" ? "ok" : "info"),
      text: body, count: 1, at: new Date().toISOString()
    };
    studioUI.messages.push(item);
    renderStudioLog();
    return item;
  }

  /* ================= 语音输入 =================
   *
   * 用浏览器自带的识别接口（`SpeechRecognition`），不经过本平台后端：
   * 录音与转写都在客户端完成，平台不需要为此存一份音频，也没有额外的密钥。
   * 代价是可用性由浏览器决定——Chrome / Edge 有，Safari 与部分国产浏览器没有；
   * 识别服务在部分网络下也连不上。所以按钮不可用时必须当场说清原因，
   * 而不是让人对着一个没反应的麦克风反复点。
   */
  var studioVoice = {
    rec: null,
    active: false,
    // 按下麦克风时输入框里已有的字。识别结果往后接，不覆盖手动打的字。
    base: "",
    finalText: "",
    /* 上次由本模块写进输入框的完整内容。用来识别「用户中途手动改过」——
       改过就把当前内容当作新的基底，否则下一次识别结果会把手改的字冲掉。 */
    lastWritten: ""
  };

  function studioSpeechCtor() {
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
  }

  /* 打开面板前就把「这个浏览器能不能语音」写在按钮上：
     点了才报错，等于让人白试一次。不直接隐藏按钮——麦克风是这一版承诺的
     功能之一，隐藏会让人以为没做；弱化 + 一行说明才说得清。 */
  function studioVoiceProbe() {
    var btn = $("#studio-mic");
    if (!btn) return;
    if (!studioSpeechCtor()) {
      btn.classList.add("is-off");
      btn.title = "当前浏览器不支持语音输入（Chrome / Edge 可用），也可以直接打字";
      return;
    }
    if (!window.isSecureContext) {
      // 仍允许点：个别浏览器在 http 下也能识别，真失败时会给准确原因。
      btn.title = "语音输入（当前不是 HTTPS，可能被浏览器拒绝）";
    }
  }

  function studioVoiceStop() {
    var was = studioVoice.active;
    studioVoice.active = false;
    if (studioVoice.rec) {
      try { studioVoice.rec.stop(); } catch (e) { /* 已经停了 */ }
    }
    var btn = $("#studio-mic");
    if (btn) {
      btn.classList.remove("recording");
      btn.setAttribute("aria-pressed", "false");
      btn.title = "语音输入";
    }
    if (was) studioAutoGrow();
  }

  function studioVoiceStart() {
    var Ctor = studioSpeechCtor();
    var input = $("#studio-input");
    if (!Ctor || !input) {
      /* 不支持就当场说清「换成什么能用」，而不是只说「不支持」。
         语音转文字用的是浏览器自带的识别接口，装不了、也补不了。 */
      studioNotice("这个浏览器不支持语音输入：用 Chrome 或 Edge 打开本页面即可，也可以直接打字。", "err");
      return;
    }
    // 上一轮还在跑时不接新的话：识别结果会写进一个已经锁住的输入框。
    if (studioUI.busy) {
      studioNotice("等这一轮回答完再语音输入。", "info");
      return;
    }

    var rec = new Ctor();
    rec.lang = "zh-CN";
    // 连续识别：一句话说完停顿时不要立刻结束，人可以接着说。
    rec.continuous = true;
    rec.interimResults = true;

    studioVoice.rec = rec;
    studioVoice.base = input.value;
    studioVoice.finalText = "";
    studioVoice.lastWritten = input.value;
    studioVoice.active = true;

    rec.onresult = function (ev) {
      /* 用户中途手动改过字（识别有笔误，改一下再接着说）：
         把手改后的内容当作新基底，否则这一轮结果会把他的修改整个冲掉。 */
      if (input.value !== studioVoice.lastWritten) {
        studioVoice.base = input.value;
        studioVoice.finalText = "";
      }

      var interim = "";
      for (var i = ev.resultIndex; i < ev.results.length; i++) {
        var item = ev.results[i];
        if (item.isFinal) studioVoice.finalText += item[0].transcript;
        else interim += item[0].transcript;
      }
      input.value = studioVoice.base + studioVoice.finalText + interim;
      studioVoice.lastWritten = input.value;
      studioAutoGrow();
      updateStudioSend();
    };

    rec.onerror = function (ev) {
      var code = (ev && ev.error) || "";
      var insecure = !window.isSecureContext;
      var why = {
        /* 非安全上下文下浏览器一律拒绝麦克风。只说「没有权限」会让人去翻
           站点权限设置，翻了也找不到——真正的原因是 http 访问。 */
        "not-allowed": insecure
          ? "麦克风只能在 HTTPS 或本机（localhost）下使用，当前是 " + window.location.origin + "。"
          : "没有麦克风权限：请在浏览器地址栏里允许后重试。",
        "service-not-allowed": insecure
          ? "麦克风只能在 HTTPS 或本机（localhost）下使用，当前是 " + window.location.origin + "。"
          : "浏览器拒绝了语音服务，请检查站点权限。",
        "network": "语音服务连不上（识别在浏览器厂商那边，部分网络不可达），请改用文字。",
        "no-speech": "没有听到声音，再点一次麦克风试试。",
        "audio-capture": "没有找到可用的麦克风。"
      }[code];
      studioNotice(why || ("语音输入失败：" + code), "err");
      studioVoiceStop();
    };

    rec.onend = function () {
      /* 浏览器在长时间静默后会自动收尾。用户没点停止就继续听，
         否则会出现「说了半句，麦克风自己关了」。 */
      if (!studioVoice.active) return;
      try { rec.start(); } catch (e) { studioVoiceStop(); }
    };

    try {
      rec.start();
    } catch (e) {
      studioNotice("麦克风启动失败：" + errText(e), "err");
      studioVoiceStop();
      return;
    }

    var btn = $("#studio-mic");
    if (btn) {
      btn.classList.add("recording");
      btn.setAttribute("aria-pressed", "true");
      btn.title = "结束语音输入";
    }
    // 光标落在末尾：语音结果接在已有文字后面，人得看得见它往哪加。
    input.focus();
    var end = input.value.length;
    try { input.setSelectionRange(end, end); } catch (e) { /* 不重要 */ }
    studioNotice("正在听，说完点一下麦克风结束。", "ok");
  }

  function studioParamList(params) {
    /* 把参数逐条摊开，而不是丢一段 JSON：确认卡片要让人一眼看出「改了什么」，
       一坨 JSON 只会被跳过。 */
    var keys = Object.keys(params || {});
    if (!keys.length) return "";
    return '<ul class="studio-params">' + keys.map(function (key) {
      var value = params[key];
      var text = value === null ? "（不限制）"
        : (typeof value === "object" ? JSON.stringify(value) : String(value));
      return '<li><span class="mono">' + esc(key) + "</span> " + esc(text) + "</li>";
    }).join("") + "</ul>";
  }

  function studioBubble(msg, index) {
    if (msg.kind === "notice") {
      var tone = msg.level === "err" ? "err" : (msg.level === "ok" ? "ok" : "info");
      return '<div class="msg studio-notice ' + tone + '">' +
        '<div class="n-head">' + ico(tone === "err" ? "alert" : "info") +
        (tone === "err" ? "没能完成这一步" : "提示") +
        (msg.at ? '<span class="n-time">' + esc(fmtTime(msg.at)) + "</span>" : "") +
        "</div>" +
        '<div class="n-body">' + esc(msg.text) + "</div>" +
        (msg.count > 1 ? '<div class="n-count">同类提示 ×' + msg.count + "</div>" : "") +
        "</div>";
    }

    if (msg.role === "user") {
      return '<div class="msg user">' + msgWho("你", msg.at) +
        '<div class="body">' + attachmentStrip(msg.attachments) +
        esc(msg.text) + "</div></div>";
    }

    var out = '<div class="msg">' + msgWho("构建助手", msg.at) +
      '<div class="body rich' + (msg.status === "error" ? " muted" : "") + '">' +
      richText(msg.text) + "</div>";

    if (msg.meta) {
      out += '<div class="meta-line">' + esc(msg.meta) + "</div>";
    }

    if (msg.approval) {
      var a = msg.approval;
      var preview = a.preview || {};
      out += '<div class="approval"><div class="a-title">' +
        esc(a.title || "待确认") + " · " +
        esc(preview.tool_display_name || preview.operation || preview.tool_name || "变更") +
        "</div>" +
        studioParamList(preview.parameters) +
        '<div class="a-row">审批 ID <span class="mono">' + esc(a.approval_id) + "</span></div>";

      // 和聊天页同一个规则：处理完就不再说「还能等到几点」，改说结果。
      if (a.done) {
        out += '<div class="a-row">' + esc(a.done) + "</div>";
      }
      if (a.executed_at) {
        out += '<div class="a-row">执行于 ' + esc(fmtTime(a.executed_at)) + "</div>";
      }
      if (a.open !== false && !a.done) {
        out += '<div class="a-row">过期时间 ' + esc(fmtTime(a.expires_at)) + "</div>";
        out += '<div class="actions" style="margin-top:8px">' +
          '<button class="btn primary sm" data-sact="confirm" data-sid="' +
          esc(a.approval_id) + '" data-idx="' + index + '">确认执行</button>' +
          '<button class="btn danger sm" data-sact="cancel" data-sid="' +
          esc(a.approval_id) + '" data-idx="' + index + '">取消</button></div>';
      }
      if (a.note) {
        out += '<div class="a-row">' + esc(a.note) + "</div>";
      }
      out += "</div>";
    }

    return out + "</div>";
  }

  function studioAtBottom(log) {
    if (!log) return true;
    // 留 4px 余量：小数缩放与边框会让 scrollTop + clientHeight 差一两像素，
    // 严格相等会把「已经贴底」判成「没贴底」，于是永远不自动跟随。
    return log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  }

  function setStudioUnseen(flag) {
    studioUI.unseen = !!flag;
    var btn = $("#studio-refresh");
    if (!btn) return;
    btn.classList.toggle("has-new", studioUI.unseen);
    btn.title = studioUI.unseen ? "有新的对话，点击滚动到最新" : "滚动到最新";
  }

  function studioScrollToLatest(smooth) {
    var log = $("#studio-log");
    if (!log) return;

    var target = log.scrollHeight;
    /* 跨度太大时不做平滑滚动：滚两屏以上要好几秒，期间看着像点了没反应，
       而「点了没反应」正是这个按钮要消除的感觉。小跨度才用动画。 */
    var far = target - log.scrollTop > log.clientHeight * 2;

    if (smooth && !far && log.scrollTo) {
      log.scrollTo({ top: target, behavior: "smooth" });
    } else {
      log.scrollTop = target;
    }
    setStudioUnseen(false);
  }

  /* mode：
     - "follow"：一定滚到最新（刚打开面板 / 自己刚发出一条）
     - "quiet"：什么都不动（点确认卡后原地更新，位置不挪、也不挂记号）
     - 不传：自动——贴着底部就跟，滚在上面就只挂记号 */
  function renderStudioLog(mode) {
    var log = $("#studio-log");
    if (!log) return;

    var follow = mode === "follow" ||
      (mode !== "quiet" && studioAtBottom(log) && !studioUI.unseen);

    if (!studioUI.messages.length) {
      log.innerHTML = '<div class="studio-intro">' +
        "<strong>构建助手</strong><br>" +
        "可以让我查看、新建、修改、停用或删除智能体，也能问平台怎么用。" +
        "改动与删除会先给你一张确认卡，点了才执行。<br>" +
        '<span class="muted">改代码、改数据库、拿主机权限这些不在范围内。</span></div>';
      return;
    }

    log.innerHTML = studioUI.messages.map(studioBubble).join("");

    if (mode === "quiet") {
      // 刻意什么都不做：这里更新的是用户正看着的那张卡。
    } else if (follow) {
      log.scrollTop = log.scrollHeight;
      setStudioUnseen(false);
    } else {
      setStudioUnseen(true);
    }

    $$("[data-sact]", log).forEach(function (btn) {
      btn.addEventListener("click", function () {
        studioResolve(
          btn.getAttribute("data-sid"),
          btn.getAttribute("data-sact"),
          Number(btn.getAttribute("data-idx")),
          btn
        );
      });
    });
  }

  /* 输入框随内容长高，到上限后内部滚动。
     固定两行时写三句话就看不到第一句，而这里常要贴一段配置或报错原文。
     下限 64px 与 CSS 的 min-height 对齐：空着时也要有「可以写几行」的体量，
     一行窄条会让人以为这里只能填一句话。
     上限 180px 而不是无限长：再高就会把对话区挤没，人反而看不见上下文。 */
  var STUDIO_INPUT_MIN = 64;
  var STUDIO_INPUT_MAX = 180;

  function studioAutoGrow() {
    var input = $("#studio-input");
    if (!input) return;
    // 先归零再读 scrollHeight：不重置的话高度只增不减，删完字还留着一大块空白。
    input.style.height = "auto";
    var next = Math.min(Math.max(input.scrollHeight, STUDIO_INPUT_MIN), STUDIO_INPUT_MAX);
    input.style.height = next + "px";
    input.style.overflowY = input.scrollHeight > STUDIO_INPUT_MAX ? "auto" : "hidden";
  }

  function updateStudioSend() {
    var btn = $("#studio-send");
    var input = $("#studio-input");
    /* 没有内容就置灰：可点的空发送只会换来一次「什么也没发生」。
       只要有附件也算有内容——此时点下去会提示补一句话，
       比把按钮置灰让人以为「文件传不上去」要好。 */
    var has = !!(input && input.value.trim()) || (studioUI.attachments || []).length > 0;
    if (btn) {
      btn.disabled = studioUI.busy || !has;
      btn.classList.toggle("busy", studioUI.busy);
    }
    var fab = $("#studio-fab");
    if (fab) fab.classList.toggle("busy", studioUI.busy);
  }

  async function studioResolve(id, action, index, btn) {
    var msg = studioUI.messages[index];
    if (!msg || !msg.approval) return;

    $$("[data-sid='" + id + "']").forEach(function (b) { b.disabled = true; });
    btn.textContent = action === "confirm" ? "执行中…" : "取消中…";

    try {
      var r = await api("/api/v1/approvals/" + encodeURIComponent(id) + "/" + action, {
        method: "POST", body: {}
      });
      // 状态说人话，原始字段照旧留着：排查时要看 success / verified。
      var text = PAPApproval.statusText(r.status);
      if (r.success !== undefined) text += " · success=" + r.success;
      if (r.verified !== undefined) text += " · verified=" + r.verified;
      if (r.message) text += " · " + r.message;
      msg.approval.done = text;
      msg.approval.title = "已处理";
      msg.approval.open = false;
      msg.approval.note = "";
      if (r.result) {
        msg.text = msg.text + "\n\n执行结果：\n" + JSON.stringify(r.result, null, 2);
      }
      studioNotice(action === "confirm" ? "已提交执行" : "已取消", "ok");
      // 配置真的变了：智能体页若正开着，重渲染一次，否则列表会停在旧状态。
      if (currentRoute() === "agents") render();
      studioLoadAgents();
    } catch (e) {
      // 同上：失败不是结论，按钮留着，错误单独一行。
      msg.approval.note = "这次没有提交成功：" + errText(e);
      studioNotice("审批没提交成功：" + errText(e), "err");
    }
    // 更新的是用户正看着的那张卡：不挪位置，也不挂「有新内容」的记号。
    renderStudioLog("quiet");
  }

  async function studioSend() {
    var input = $("#studio-input");
    if (!input || studioUI.busy) return;

    var text = input.value.trim();
    var files = (studioUI.attachments || []).slice();

    /* 只有文件、没有一句话，就不发。
       这里以前补一句「请看附件。」好让请求过校验，但那等于让模型自己猜
       用户要什么：猜错的代价是一整轮识别与一段答非所问，而人只是漏打了
       半句话。附件留在托盘里，补一句就能发。 */
    if (!text) {
      if (files.length) {
        studioNotice("先说要这份文件做什么，再发送。", "info");
        input.focus();
      }
      return;
    }

    // 选中对象作为一句话前缀而不是隐藏参数：后端没有 focus 字段，
    // 悄悄塞进去会让人看不出「它到底在改哪一个」。
    var outgoing = studioUI.focus ? "（当前对象：" + studioUI.focus + "）\n" + text : text;

    // 发送就等于说完了：不收起麦克风的话，识别结果会继续往已清空的输入框里写。
    if (studioVoice.active) studioVoiceStop();

    studioUI.messages.push({
      role: "user", text: text, attachments: files.slice(),
      at: new Date().toISOString()
    });
    var placeholder = { role: "agent", text: "思考中…", at: null };
    studioUI.messages.push(placeholder);

    input.value = "";
    studioAutoGrow();
    // 先清托盘：附件已随这条消息发出，留着会让人以为还没发出去。
    studioUI.attachments = [];
    renderStudioTray();
    studioUI.busy = true;
    // 自己刚发出去一条：一定跟到最新，否则会以为消息没发出去。
    renderStudioLog("follow");
    updateStudioSend();

    try {
      var body = {
        thread_id: studioThread(),
        message: outgoing,
        agent: studioUI.agentName || "studio"
      };
      if (files.length) {
        body.attachments = files.map(function (att) {
          return { data_url: att.dataUrl, name: att.name || null };
        });
      }

      var r = await api("/api/v1/chat", { method: "POST", body: body });

      placeholder.text = r.answer || "（无文本回答）";
      placeholder.ocrText = r.ocr_text || null;
      placeholder.at = r.assistant_message_at || new Date().toISOString();

      var meta = [];
      if (r.status && r.status !== "completed") meta.push("status=" + r.status);
      if (r.error_category) meta.push("error=" + r.error_category);
      placeholder.meta = meta.join(" · ");
      placeholder.status = r.status === "error" ? "error" : "ok";
      if (r.approval) placeholder.approval = r.approval;
      // 服务端按内容嗅探后拒收（类型/体积/数量）时附件并没入库，放回托盘，
      // 让用户换一个文件直接重发——与对话台同一条规则。
      if (r.status === "rejected" && files.length && !studioUI.attachments.length) {
        studioUI.attachments = files;
        renderStudioTray();
      }
    } catch (e) {
      placeholder.text = "请求失败：" + errText(e);
      placeholder.status = "error";
      placeholder.at = new Date().toISOString();
      // 没发出去就把附件放回托盘，重试时不用再从磁盘里挑一遍。
      if (files.length && !studioUI.attachments.length) {
        studioUI.attachments = files;
        renderStudioTray();
      }
    } finally {
      studioUI.busy = false;
      renderStudioLog();
      updateStudioSend();
    }
  }

  /* 把一条历史消息里的审批号还原成一张卡。

     数据库里只存了审批号，卡上的参数、状态都在审批单上——所以要点一次接口。
     状态按**现在**算：可能已经确认过、也可能已过期，不能拿当时的快照糊上去。 */
  async function studioApprovalCard(approvalId, content) {
    try {
      var ap = await api("/api/v1/approvals/" + encodeURIComponent(approvalId));
      var preview = ap.preview || {};

      /* 能不能点由服务端说了算（`actionable`）。浏览器自己按 `expires_at`
         比时钟，客户端差几分钟就会把还能确认的单子说成已过期，反过来更糟：
         显示成可点，点下去拿到 409。 */
      return {
        approval_id: approvalId,
        title: ap.actionable ? "待确认" : "已处理",
        preview: {
          tool_name: ap.tool_name,
          tool_display_name: ap.tool_display_name,
          parameters: preview.parameters || {},
          delegation: preview.delegation
        },
        expires_at: ap.expires_at,
        executed_at: ap.executed_at,
        message: content,
        done: ap.actionable ? "" : PAPApproval.outcome(ap)
      };
    } catch (e) {
      /* 查不到就如实说查不到：编一张「待确认」出来，点下去必然失败。 */
      return {
        approval_id: approvalId,
        title: "已失效",
        preview: {},
        done: "查不到这张审批单（可能已被清理）。"
      };
    }
  }

  /* 打开面板时读回这条线程的历史。

     面板的消息原本只在内存里，刷新就没了，而服务端其实一直存着——
     这条接口就是把它取回来。只读自己的：服务端按 (租户, 用户, 线程) 三重定位。 */
  async function studioLoadHistory() {
    if (studioUI.historyTried) return;
    studioUI.historyTried = true;

    var want = studioThread();

    try {
      var list = await api("/api/v1/conversations?channel=studio&limit=200");
      var mine = (list.items || []).filter(function (c) {
        return c.thread_id === want;
      })[0];

      /* 换了浏览器 / 清过存储就没有旧线程：从空白开始，不是错误。 */
      if (!mine) return;

      var detail = await api("/api/v1/conversations/" + encodeURIComponent(mine.id));
      var restored = [];

      for (var i = 0; i < (detail.messages || []).length; i++) {
        var m = detail.messages[i];
        var meta = m.metadata || {};
        var role = m.role === "user" ? "user" : "agent";
        var item = { role: role, text: m.content, at: m.created_at, status: "ok" };
        // 附件随会话保留，服务端把句柄写进消息元数据，刷新后能点开原文。
        // 这里回填的是接口地址（不是 data URL），attachmentStrip 两种都认。
        if (role === "user" && m.attachments && m.attachments.length) {
          item.attachments = m.attachments;
        }

        if (meta.error_category) {
          item.status = "error";
          item.meta = "error=" + meta.error_category;
        }
        if (role === "agent" && meta.approval_id) {
          item.approval = await studioApprovalCard(meta.approval_id, m.content);
        }
        restored.push(item);
      }

      /* 读历史这几百毫秒里，用户可能已经发出一条：那就别用历史把它盖掉。
         宁可这次不补历史，也不能吃掉他刚打的字。 */
      if (studioUI.messages.length || !restored.length) return;

      studioUI.messages = restored;
      renderStudioLog("follow");
    } catch (e) {
      /* 读不到历史不该挡住新对话：面板照常能用，只是从空开始。
         放回 false，下次打开再试一次。 */
      studioUI.historyTried = false;
    }
  }

  async function studioLoadAgents() {
    try {
      var r = await api("/api/v1/agents");
      studioUI.agents = r.items || [];
    } catch (e) {
      studioUI.agents = [];
    }

    /* 内置助手的 name 由后端决定（`studio`，被占用时是 `studio_assistant`），
       前端不能写死：写死就会在那种租户里 404，而错误看起来像权限问题。 */
    var builtin = studioUI.agents.filter(function (a) { return a.is_builtin; })[0];
    studioUI.agentName = builtin ? builtin.name : "";

    var select = $("#studio-focus");
    if (select) {
      var options = ['<option value="">不指定对象</option>'];
      studioUI.agents.forEach(function (agent) {
        if (agent.is_builtin) return;
        options.push('<option value="' + esc(agent.name) + '">' +
          esc(agent.display_name || agent.name) + " · " + esc(agent.name) + "</option>");
      });
      select.innerHTML = options.join("");
      select.value = studioUI.focus || "";
    }

    if (!builtin) {
      $("#studio-log").innerHTML = '<div class="banner err">' +
        "构建助手尚未就绪（后端未创建内置智能体）。刷新页面会重试一次。</div>";
    }
  }

  /* 机器人头像是同一份 SVG 用在两处（悬浮球、抽屉头部徽标），
     所以坐标写在 48×48 的框里，靠外层 className 控制尺寸。

     部件是分开的组而不是一张整图：变身时头、耳、眼各自插值，
     读起来才是「同一个东西在变形」；换成两张图淡入淡出，
     观感就成了「换了个图标」。 */
  function studioBotSvg() {
    return '<span class="studio-bot" aria-hidden="true">' +
      '<svg class="bot-svg" viewBox="0 0 48 48" fill="none">' +
      /* 天线：变身后收起。火箭的鼻子由身体自身的圆角顶出来，不需要额外零件。 */
      '<g class="bot-antenna">' +
      '<line x1="24" y1="13.4" x2="24" y2="9.8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>' +
      '<circle class="bot-antenna-dot" cx="24" cy="7.4" r="1.9" fill="currentColor"/>' +
      "</g>" +
      /* 侧耳：变身后下移外翻，成了尾翼。 */
      '<rect class="bot-ear bot-ear-l" x="5.2" y="19" width="5" height="10" rx="2.5" fill="currentColor"/>' +
      '<rect class="bot-ear bot-ear-r" x="37.8" y="19" width="5" height="10" rx="2.5" fill="currentColor"/>' +
      /* 头：变身后压窄拉长，成为箭体。 */
      '<rect class="bot-head" x="10" y="13" width="28" height="22" rx="7" fill="currentColor"/>' +
      /* 两只眼睛向中间靠拢并放大，合成一个舷窗。 */
      '<g class="bot-eye bot-eye-l"><circle class="bot-eye-dot" cx="18" cy="22.4" r="2.4" fill="var(--surface)"/></g>' +
      '<g class="bot-eye bot-eye-r"><circle class="bot-eye-dot" cx="30" cy="22.4" r="2.4" fill="var(--surface)"/></g>' +
      '<rect class="bot-mouth" x="20.6" y="27.5" width="6.8" height="1.9" rx=".95" fill="var(--surface)"/>' +
      /* 尾焰：平时透明，起身时才点着。 */
      '<g class="bot-flame">' +
      '<path d="M24 48c-3.4-2.6-5.4-6.2-5.4-9.6 0-1.4 2.4-2.6 5.4-2.6s5.4 1.2 5.4 2.6c0 3.4-2 7-5.4 9.6z" fill="#f0883e"/>' +
      '<path d="M24 45.2c-1.7-1.3-2.7-3.1-2.7-4.8 0-.7 1.2-1.3 2.7-1.3s2.7.6 2.7 1.3c0 1.7-1 3.5-2.7 4.8z" fill="#fbbf24"/>' +
      "</g>" +
      "</svg></span>";
  }

  function studioShell() {
    return '<button class="studio-fab" type="button" id="studio-fab" title="构建助手" ' +
      'aria-label="构建助手">' + studioBotSvg() + "</button>" +
      '<aside class="studio-drawer" id="studio-drawer" hidden>' +
      '<header class="studio-head">' +
      '<span class="studio-badge" aria-hidden="true">' + studioBotSvg() + "</span>" +
      '<div class="studio-title"><strong>构建助手</strong><span>Agent Studio</span></div>' +
      '<div class="studio-acts">' +
      /* 三个按钮的顺序是有意的：读（滚到最新）→ 看（改尺寸）→ 走（关闭）。
         刷新放在最左边，因为它是唯一「点了不改变任何状态」的动作，
         离关闭最远，误点的代价也只是滚一下。 */
      '<button class="btn sm icon" type="button" id="studio-refresh" title="滚动到最新" ' +
      'aria-label="滚动到最新">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/>' +
      '<path d="M20.7 4.6v4.9h-4.9"/></svg>' +
      "</button>" +
      '<button class="btn sm icon" type="button" id="studio-expand" title="放大窗口" ' +
      'aria-label="放大窗口">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path class="ic-expand" d="M4 9V4h5M20 9V4h-5M20 15v5h-5M4 15v5h5"/>' +
      '<path class="ic-restore" d="M9 4v5H4M15 4v5h5M15 20v-5h5M9 20v-5H4"/>' +
      "</svg>" +
      "</button>" +
      '<button class="btn sm icon" type="button" id="studio-close" title="关闭" ' +
      'aria-label="关闭">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M6 6l12 12M18 6L6 18"/></svg>' +
      "</button>" +
      "</div></header>" +
      '<div class="studio-log" id="studio-log"></div>' +
      '<div class="studio-foot">' +
      // 托盘在输入框上方、与输入框同宽：附件是「即将发出的东西」，
      // 贴在底部会看起来像已经发过的一条消息。
      '<div class="attach-tray" id="studio-tray" hidden></div>' +
      // 输入区是一整块卡片：输入框与底部按钮同处一个描边内。
      // 按钮放在框外（旧版）会让「这一点会发出什么」变得不明确——
      // 加号在框左、发送在框右，看起来像两个独立入口。
      '<div class="studio-composer" id="studio-composer">' +
      '<textarea id="studio-input" rows="1" ' +
      'placeholder="今天帮你做些什么？可创建、配置或排查 Agent…"></textarea>' +
      '<div class="studio-tools">' +
      // 加号只有一个去处：随消息发给助手。
      // 这里不提供「直接导进知识库」——构建助手处理的是这一问的材料，
      // 不是长期语料；把两类东西塞进同一个按钮，文件去哪就成了要猜的事。
      // 建知识库走控制台「从文件新建」页，权限与幂等由那一处统一把关。
      // 收 Markdown / 文本是为了「按这份规程/清单干活」；
      // 它们和图片、PDF 一样是这个会话的参考材料，随会话保留、跨轮可读。
      '<label class="studio-tool" for="studio-file" id="studio-add" ' +
      'title="添加图片、PDF、Markdown 或文本（可多选）" role="button" tabindex="0" ' +
      'aria-label="添加附件">' + ico("plus") + "</label>" +
      '<input type="file" id="studio-file" accept="' + STUDIO_ATTACH_ACCEPT + '" multiple hidden>' +
      // 对象选择紧挨加号：它和附件一样，都是「这一条消息要带上的上下文」。
      // 以前放在头部单独一行，读起来像整个面板的全局设置——切了对象，
      // 却以为改的是别处。放进工具行，跟着这一条消息一起读才不歧义。
      '<select id="studio-focus" title="这条消息的当前对象" ' +
      'aria-label="当前对象"></select>' +
      // 语音输入用的是浏览器自带的识别服务，不经过本平台后端；
      // 因此它在不支持该接口或断网的浏览器里不可用，处理见 bindStudioVoice。
      '<button class="studio-tool" type="button" id="studio-mic" ' +
      'title="语音输入" aria-label="语音输入" aria-pressed="false">' + ico("mic") + "</button>" +
      '<button class="studio-send" type="button" id="studio-send" ' +
      'title="发送（Enter）" aria-label="发送">' + ico("arrow-up") + "</button>" +
      "</div></div></div></aside>";
  }

  function setStudioExpanded(expanded, persist) {
    studioUI.expanded = !!expanded;
    var drawer = $("#studio-drawer");
    var btn = $("#studio-expand");
    if (drawer) drawer.classList.toggle("expanded", studioUI.expanded);
    if (btn) {
      btn.title = studioUI.expanded ? "恢复默认尺寸" : "放大窗口";
      btn.setAttribute("aria-label", btn.title);
      btn.setAttribute("aria-pressed", studioUI.expanded ? "true" : "false");
    }

    // 放大时保持「贴在底部」的相对位置：否则窗口变高后视野会停在半截，
    // 看起来像消息丢了一段。
    if (studioUI.open) studioScrollToLatest(true);

    if (persist === false) return;
    try {
      window.localStorage.setItem(STUDIO_SIZE_KEY, studioUI.expanded ? "1" : "0");
    } catch (e) { /* 只影响下次进来的尺寸，不影响功能 */ }
  }

  /* 一次动画的句柄。连点时先清掉上一条链路，
     否则「降落还没结束又点了发射」会把类名互相擦掉，停在半个状态上。 */
  var studioAnimTimer = null;

  /* 动效是装饰，不是功能。系统里开了「减少动态效果」就直接打开，
     不为了好看让人多等 0.8 秒。 */
  function studioMotionOff() {
    return !!(window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function studioClearAnim() {
    if (studioAnimTimer) {
      clearTimeout(studioAnimTimer);
      studioAnimTimer = null;
    }
    var fab = $("#studio-fab");
    if (fab) fab.classList.remove("is-rocket", "is-launch", "is-landing");
  }

  /* 点悬浮球：机器人蹲一下 → 变成火箭 → 冲上去了就打开面板。
     面板和悬浮球在同一个位置，所以「火箭飞出画面」与「面板出现」
     必须是先后关系，不能同时——同时发生会看成火箭没飞走、而是被面板拍掉了。 */
  function studioLaunch() {
    var fab = $("#studio-fab");
    if (!fab || studioMotionOff()) {
      setStudioOpen(true);
      return;
    }

    studioClearAnim();
    fab.classList.add("is-rocket");
    studioAnimTimer = setTimeout(function () {
      fab.classList.add("is-launch");
      studioAnimTimer = setTimeout(function () {
        fab.classList.remove("is-rocket", "is-launch");
        studioAnimTimer = null;
        setStudioOpen(true);
      }, 580);
    }, 200);
  }

  /* 关面板：火箭从天上落回来，再变回机器人。
     只做「消失」的话，下次点开又是凭空冒出来一个球；
     补上降落这一段，开合才有来有回。 */
  function studioLand() {
    var fab = $("#studio-fab");
    if (!fab || studioMotionOff()) {
      if (fab) fab.classList.remove("is-rocket");
      return;
    }

    studioClearAnim();
    fab.classList.add("is-rocket", "is-landing");
    studioAnimTimer = setTimeout(function () {
      fab.classList.remove("is-rocket", "is-landing");
      studioAnimTimer = null;
    }, 520);
  }

  function setStudioOpen(open) {
    studioUI.open = open;
    var drawer = $("#studio-drawer");
    var fab = $("#studio-fab");
    if (drawer) drawer.hidden = !open;
    if (fab) fab.classList.toggle("hidden", open);

    if (open) {
      if (!studioUI.agents.length) studioLoadAgents();
      // 历史是异步补的：不挡输入框，读回来再替换掉空白态。
      studioLoadHistory();
      // 打开就等于「我要看最新的」：跟到底，并清掉刷新按钮上的记号。
      renderStudioLog("follow");
      var input = $("#studio-input");
      if (input) input.focus();
    } else {
      // 收起面板就松开麦克风：让它继续听，等于在一个看不见的窗口里录音。
      if (studioVoice.active) studioVoiceStop();
      studioLand();
    }
  }

  function initStudio() {
    var host = document.createElement("div");
    host.className = "studio-host";
    host.innerHTML = studioShell();
    document.body.appendChild(host);

    $("#studio-fab").addEventListener("click", studioLaunch);
    $("#studio-close").addEventListener("click", function () { setStudioOpen(false); });

    /* 刷新只做一件事：滚到最新。它不重新拉取消息——面板内容来自本地这一份
       messages，重拉会与正在进行的请求抢上下文，让「刷新」变成功夫不负责任的重载。*/
    $("#studio-refresh").addEventListener("click", function () {
      studioScrollToLatest(true);
      this.classList.remove("spinning");
      // 触发一次重绘让动画能重放：连点时不会因为类名已存在而毫无反馈。
      void this.offsetWidth;
      this.classList.add("spinning");
    });

    $("#studio-expand").addEventListener("click", function () {
      setStudioExpanded(!studioUI.expanded);
    });

    var expanded = false;
    try {
      expanded = window.localStorage.getItem(STUDIO_SIZE_KEY) === "1";
    } catch (e) { expanded = false; }
    setStudioExpanded(expanded, false);

    // 用户自己滚回底部时，记号就该消失——否则它会一直挂在上面，
    // 变成一个没人再看的红点。
    var log = $("#studio-log");
    if (log) {
      log.addEventListener("scroll", function () {
        if (studioUI.unseen && studioAtBottom(log)) setStudioUnseen(false);
      });
    }

    $("#studio-focus").addEventListener("change", function () {
      studioUI.focus = this.value;
    });

    $("#studio-send").addEventListener("click", studioSend);

    $("#studio-mic").addEventListener("click", function () {
      if (studioVoice.active) studioVoiceStop();
      else studioVoiceStart();
    });

    // 手动打字也要跟着长高，并即时更新发送按钮的可用状态。
    $("#studio-input").addEventListener("input", function () {
      studioAutoGrow();
      updateStudioSend();
    });
    studioAutoGrow();
    updateStudioSend();
    studioVoiceProbe();

    var studioFile = $("#studio-file");
    studioFile.addEventListener("change", function () {
      // 先拷贝再清空：清空 value 会让 Listener 持有的 FileList 失效。
      var picked = Array.prototype.slice.call(studioFile.files || []);
      studioFile.value = "";
      if (picked.length) {
        addAttachments(picked, studioUI.attachments, renderStudioTray, {
          text: true, notice: studioNotice
        });
      }
    });

    // label 的 for 已覆盖鼠标点击，这里补键盘可达性。
    $("#studio-add").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        studioFile.click();
      }
    });

    // 托盘是动态重建的，删除按钮用事件委托。
    $("#studio-tray").addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-remove]");
      if (!btn) return;
      var index = parseInt(btn.getAttribute("data-remove"), 10);
      if (!(index >= 0) || index >= studioUI.attachments.length) return;
      studioUI.attachments.splice(index, 1);
      renderStudioTray();
    });

    $("#studio-input").addEventListener("keydown", function (e) {
      // Enter 发送、Shift+Enter 换行：与对话台保持一致，
      // 两个入口用不同键位是最容易让人骂街的细节。
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        studioSend();
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !studioUI.open) return;
      setStudioOpen(false);
    });
  }

  /* ================= 编排 =================
   *
   * 这一页要回答三个问题，而不是「配了几张图」：
   *   1. 这张图跑得起来吗（节点数、最长几跳，与智能体的转交深度直接可比）；
   *   2. 最近跑成什么样（暂停 / 失败 / 跑完，各自停在哪）；
   *   3. 每一步到底产出了什么（节点流水）。
   *
   * 刻意没有「立即运行」按钮：触发只能回对话里发 `/workflow <名字> <要说的事>`。
   * 在这里开一个运行入口，结论就不在任何一个会话里——用户看不见、审计查不到、
   * 写操作也落不到他当时确认的那条链上。页面上给的是入口提示，不是执行按钮。
   */

  var WF_STATUS = {
    RUNNING: { text: "进行中", cls: "warn" },
    PAUSED: { text: "等确认", cls: "warn" },
    COMPLETED: { text: "已跑完", cls: "ok" },
    FAILED: { text: "失败", cls: "err" },
    CANCELLED: { text: "已取消", cls: "" }
  };

  var WF_KIND = { agent: "执行", compose: "汇总", parallel: "并行" };

  var WF_NODE_STATUS = {
    ok: { text: "", cls: "ok" },
    paused: { text: "停在审批", cls: "warn" },
    skipped: { text: "跳过", cls: "warn" },
    failed: { text: "失败", cls: "err" }
  };

  var WF_TEMPLATE = {
    entry: "classify",
    max_nodes: 12,
    nodes: [
      {
        id: "classify",
        agent: "cs",
        task: "判断这件事属于哪一类：{{input.text}}",
        next: [{ when: null, to: "answer" }]
      },
      {
        id: "answer",
        kind: "compose",
        task: "结论：{{upstream}}",
        next: []
      }
    ]
  };

  var wfUI = {
    items: [],
    canManage: false,
    /* 展开运行记录的那个流程名。null = 都没展开。 */
    runsOf: null,
    runs: [],
    /* 展开节点流水的 run id。同一条流水只展开一条：同时摊开三条没人看得下去。 */
    openRun: null,
    form: null
  };

  function wfChip(status) {
    var meta = WF_STATUS[status] || { text: status || "—", cls: "" };
    return '<span class="chip' + (meta.cls ? " " + meta.cls : "") + '">' +
      esc(meta.text) + "</span>";
  }

  function wfRunLine(run) {
    /* 一条 run 收成一行：状态、用了几个节点、停在哪、什么时候。
       游标只在没跑完的时候才有意义——跑完的 run 说「停在 answer」只会让人以为它出错了。 */
    var parts = [];
    parts.push(wfChip(run.status));
    parts.push('<span class="wf-meta">跑了 ' + esc(String(run.nodes_used)) + " 步</span>");
    if (run.status === "PAUSED" && run.cursor) {
      parts.push('<span class="wf-meta">停在 <code>' + esc(run.cursor) + "</code></span>");
    }
    if (run.error_code) {
      parts.push('<span class="wf-meta err">' + esc(run.error_code) + "</span>");
    }
    parts.push('<span class="wf-meta">' + esc(fmtTime(run.started_at)) + "</span>");
    return parts.join("");
  }

  function wfNodeStrip(run) {
    /* 节点条：每个节点一格，从左到右就是执行顺序。
       截断的节点单独标出来——下游拿到的是截断后的文本，不标就会有人以为
       它看到了全部，然后去查一个并不存在的「模型漏读」。 */
    var nodes = run.nodes || [];
    if (!nodes.length) {
      return '<p class="hint">这次运行还没有跑过任何节点。</p>';
    }
    var cells = nodes.map(function (node) {
      var meta = WF_NODE_STATUS[node.status] || { text: node.status, cls: "" };
      var title = [
        node.node_id,
        WF_KIND[node.kind] || node.kind,
        node.agent ? "→ " + node.agent : "",
        meta.text ? "（" + meta.text + "）" : "",
        node.truncated ? "输出已截断" : "",
        node.message || ""
      ].filter(Boolean).join(" · ");

      return '<div class="wf-node' + (meta.cls ? " " + meta.cls : "") + '" title="' +
        esc(title) + '">' +
        '<span class="wf-node-id">' + esc(node.node_id) + "</span>" +
        '<span class="wf-node-kind">' +
        esc(node.agent ? node.agent : (WF_KIND[node.kind] || node.kind)) + "</span>" +
        (node.truncated ? '<span class="wf-node-flag">截断</span>' : "") +
        (meta.text ? '<span class="wf-node-flag">' + esc(meta.text) + "</span>" : "") +
        "</div>";
    }).join('<span class="wf-arrow">→</span>');

    var previews = nodes.map(function (node) {
      if (!node.preview) return "";
      return '<div class="wf-preview"><div class="wf-preview-head">' +
        esc(node.node_id) +
        (node.agent ? " · " + esc(node.agent) : "") +
        (node.depth ? " · 第 " + esc(String(node.depth)) + " 跳" : "") +
        (node.missing && node.missing.length ? " · 缺项 " + esc(node.missing.join(", ")) : "") +
        "</div><div class=\"wf-preview-body\">" + esc(node.preview) + "</div></div>";
    }).join("");

    var tail = run.output_text
      ? '<div class="wf-preview"><div class="wf-preview-head">这次运行的结论</div>' +
        '<div class="wf-preview-body">' + esc(run.output_text) + "</div></div>"
      : "";

    return '<div class="wf-strip">' + cells + "</div>" + previews + tail;
  }

  function wfRunsPanel() {
    if (!wfUI.runsOf) return "";
    if (!wfUI.runs.length) {
      return '<div class="card"><div class="card-title">运行记录</div>' +
        '<div class="empty sm">这条流程还没有跑过。回对话里发 ' +
        "<code>/workflow " + esc(wfUI.runsOf) + " &lt;要说的事&gt;</code> 触发一次。</div></div>";
    }

    var rows = wfUI.runs.map(function (run) {
      var open = wfUI.openRun === run.id;
      return '<div class="wf-run' + (open ? " open" : "") + '">' +
        '<button class="wf-run-head" type="button" data-act="run-open" data-run="' +
        esc(run.id) + '">' +
        (open ? ico("down") : ico("right")) +
        "<span>" + wfRunLine(run) + "</span></button>" +
        (open ? '<div class="wf-run-body">' + wfNodeStrip(run) + "</div>" : "") +
        "</div>";
    }).join("");

    return '<div class="card">' +
      cardHead("运行记录", "clock", '<span class="chip">' + esc(wfUI.runsOf) + "</span>") +
      '<div class="wf-runs">' + rows + "</div>" +
      '<p class="hint">停在审批上的运行不会被自动跳过：点完那张审批单，流程会从停住的节点继续，' +
      "不会从头重跑。取消或审批过期则整条流程取消，后面的节点一步都不跑。</p>" +
      "</div>";
  }

  function wfRow(item) {
    var last = item.last_run;
    // 这里不再说「最长 N 跳」：编排的每一跳都以发起方为父，运行时深度恒为 1，
    // 拿节点数当跳数会让人以为「6 节点的图要 6 跳深度」，然后去把深度调高。
    var shape = item.node_count + " 个节点 · 其中 " + item.depth_hops + " 步调用智能体";
    var size = '<span class="wf-meta">' + esc(shape) + "</span>";

    var ops = '<button class="btn sm" data-act="runs" data-name="' + esc(item.name) + '">' +
      ico("list") + "运行记录</button>";
    if (wfUI.canManage) {
      ops += '<button class="btn sm" data-act="edit" data-name="' + esc(item.name) + '">' +
        ico("edit") + "编辑</button>" +
        '<button class="btn sm" data-act="toggle" data-name="' + esc(item.name) +
        '" data-to="' + (item.enabled ? "off" : "on") + '">' +
        ico(item.enabled ? "minus" : "up") + (item.enabled ? "停用" : "启用") + "</button>" +
        '<button class="btn sm danger" data-act="del" data-name="' + esc(item.name) + '">' +
        ico("trash") + "删除</button>";
    }

    return "<tr>" +
      "<td><div class=\"wf-name\">" + esc(item.display_name) + "</div>" +
      '<div class="wf-meta mono">' + esc(item.name) +
      " · 修订 " + esc(String(item.revision)) +
      (item.enabled ? "" : ' · <span class="chip warn">已停用</span>') + "</div>" +
      (item.description ? '<div class="wf-desc">' + esc(item.description) + "</div>" : "") +
      "</td>" +
      "<td>" + size + "</td>" +
      "<td>" + (last ? wfRunLine(last) : '<span class="wf-meta">还没跑过</span>') + "</td>" +
      '<td class="right"><div class="wf-ops">' + ops + "</div></td>" +
      "</tr>";
  }

  function wfListHtml() {
    if (!wfUI.items.length) {
      return '<div class="empty"><strong>还没有配置任何流程</strong>' +
        "流程适合步骤可枚举、顺序有要求、出错要能复盘的活（巡检、工单分派、发布前核验）。" +
        "开放式问答不要套流程，它只会得到一条走不通的路。</div>";
    }

    return '<div class="tbl-scroll"><table class="tbl"><thead><tr>' +
      "<th>流程</th><th>规模</th><th>最近运行</th>" +
      (wfUI.canManage ? '<th class="right">操作</th>' : "") +
      "</tr></thead><tbody>" +
      wfUI.items.map(wfRow).join("") +
      "</tbody></table></div>";
  }

  function wfFormHtml() {
    var form = wfUI.form;
    if (!form) return "";

    var editing = form.mode === "edit";
    var def = editing ? form.definition : WF_TEMPLATE;

    return '<div class="card wf-form">' +
      cardHead(editing ? "编辑流程" : "新建流程", "share",
        '<button class="btn sm" type="button" data-act="form-close">' + ico("close") + "关闭</button>") +
      '<div class="form-grid">' +
      '<div class="field"><label>标识（对话里用它触发，保存后不可改）</label>' +
      '<input type="text" id="wf-name" value="' + esc(form.name || "") + '"' +
      (editing ? " disabled" : "") + ' placeholder="alert_triage"></div>' +
      '<div class="field"><label>显示名</label>' +
      '<input type="text" id="wf-display" value="' + esc(form.display_name || "") +
      '" placeholder="告警排查"></div>' +
      "</div>" +
      '<div class="field"><label>一句话说明</label>' +
      '<input type="text" id="wf-desc" value="' + esc(form.description || "") +
      '" placeholder="分类 → 排查 → 汇总"></div>' +
      '<div class="field"><label>图（保存即编译：没兜底边、有环、引用不可达节点都会被当场拒绝）</label>' +
      '<textarea id="wf-def" class="wf-def" spellcheck="false" rows="14">' +
      esc(JSON.stringify(def, null, 2)) + "</textarea></div>" +
      '<div class="actions">' +
      '<button class="btn primary" type="button" data-act="form-save">' + ico("save") + "保存</button>" +
      '<button class="btn" type="button" data-act="form-template">' + ico("code") + "填入模板</button>" +
      "</div>" +
      '<p class="hint">图里只能写「什么时候轮到谁」，不能内联提示词、不能临时给工具。' +
      "节点的 Agent 名必须是已经存在的智能体；每次调用仍按委派规则逐跳收窄边界。</p>" +
      "</div>";
  }

  function renderWorkflows(root) {
    var body = $("#wf-body", root);
    if (!body) return;
    body.innerHTML = wfListHtml();

    /* 表单只在「打开的是另一张图」时重建。
       每次刷新都重写一遍的话，正在改的 JSON 会在点「运行记录」或刷新时被
       抹回上次保存的内容——那种丢失没有任何提示，比报错难查得多。 */
    var form = $("#wf-form-slot", root);
    if (form) {
      var key = wfUI.form ? wfUI.form.mode + ":" + (wfUI.form.name || "") : "";
      if (form.getAttribute("data-key") !== key) {
        form.setAttribute("data-key", key);
        form.innerHTML = wfFormHtml();
      }
    }

    var panel = $("#wf-runs-slot", root);
    if (panel) panel.innerHTML = wfRunsPanel();

    var count = $("#wf-count", root);
    if (count) count.textContent = wfUI.items.length + " 张";
  }

  function mountWorkflows(root) {
    wfUI.form = null;
    wfUI.runsOf = null;
    wfUI.runs = [];
    wfUI.openRun = null;

    root.innerHTML = pageHead({
      eyebrow: PAGES.workflows.eyebrow,
      title: PAGES.workflows.title,
      desc: PAGES.workflows.desc,
      actions:
        '<button class="btn" type="button" id="wf-refresh">' + ico("refresh") + "刷新</button>" +
        '<button class="btn primary" type="button" id="wf-new">' + ico("plus") + "新建流程</button>"
    }) +
      // 包一层自己的容器再挂事件：`#view` 是常驻节点，往它身上加监听会在
      // 每次进入这页时叠一份，点一次「运行记录」触发三次请求、弹三个提示。
      '<div id="wf-ui">' +
      '<div class="stats" id="wf-stats">' + statSkeletons(3) + "</div>" +
      '<div class="card">' +
      cardHead("流程清单", "share", '<span class="chip" id="wf-count">—</span>') +
      '<div id="wf-body"><div class="skeleton" style="width:60%"></div></div>' +
      '<p class="hint">触发只有一种：在对话里发 <code>/workflow &lt;标识&gt; &lt;要说的事&gt;</code>。' +
      "结论会落在那条会话里，写操作走同一条审批链。这里不提供「立即运行」——" +
      "那样跑出来的结论没有归属，用户看不见、审计也查不到。" +
      "另外，「最长几跳」必须不超过发起方智能体配的「最大转交深度」，超了会在开跑前被拒。</p>" +
      "</div>" +
      '<div id="wf-form-slot"></div>' +
      '<div id="wf-runs-slot"></div>' +
      "</div>";

    $("#wf-refresh").addEventListener("click", function () { loadWorkflows(root); });
    $("#wf-new").addEventListener("click", function () {
      if (!wfUI.canManage) { toast("缺少 agent:write 权限", "err"); return; }
      wfUI.form = { mode: "new", name: "", display_name: "", description: "" };
      renderWorkflows(root);
      var name = $("#wf-name", root);
      if (name) name.focus();
    });

    $("#wf-ui", root).addEventListener("click", function (event) {
      var target = event.target.closest("[data-act]");
      if (!target) return;
      wfAction(target.getAttribute("data-act"), target, root);
    });

    loadWorkflows(root);
  }

  async function loadWorkflows(root) {
    try {
      var data = await api("/api/v1/workflows");
      wfUI.items = data.items || [];
      wfUI.canManage = !!data.can_manage;
    } catch (e) {
      var body = $("#wf-body", root);
      if (body) body.innerHTML = '<div class="banner err">加载失败：' + esc(errText(e)) + "</div>";
      return;
    }

    var paused = wfUI.items.filter(function (row) {
      return row.last_run && row.last_run.status === "PAUSED";
    }).length;
    var enabled = wfUI.items.filter(function (row) { return row.enabled; }).length;

    var stats = $("#wf-stats", root);
    if (stats) {
      stats.innerHTML =
        stat("流程", String(wfUI.items.length),
          false, wfUI.items.length ? "含停用 " + (wfUI.items.length - enabled) : "") +
        stat("启用中", String(enabled)) +
        stat("停在审批", String(paused), paused > 0, "等待人确认写操作");
    }

    var newBtn = $("#wf-new", root);
    if (newBtn) newBtn.hidden = !wfUI.canManage;

    renderWorkflows(root);

    // 展开着运行记录时刷新，要把它重新拉一遍：否则用户会盯着上一次的旧流水。
    if (wfUI.runsOf) await loadRuns(wfUI.runsOf, root, true);
  }

  async function loadRuns(name, root, quiet) {
    try {
      var data = await api("/api/v1/workflows/" + encodeURIComponent(name) + "/runs?limit=20");
      wfUI.runsOf = name;
      wfUI.runs = data.items || [];
      // 刷新之后那条 run 可能已经不在窗口里了，别留一个指向不存在记录的展开态。
      if (!wfUI.runs.some(function (run) { return run.id === wfUI.openRun; })) {
        wfUI.openRun = null;
      }
    } catch (e) {
      if (!quiet) toast("运行记录读取失败：" + errText(e), "err");
      return;
    }
    renderWorkflows(root);
  }

  async function wfAction(act, target, root) {
    var name = target.getAttribute("data-name");

    if (act === "form-close") {
      wfUI.form = null;
      renderWorkflows(root);
      return;
    }

    if (act === "form-template") {
      var area = $("#wf-def", root);
      if (area) area.value = JSON.stringify(WF_TEMPLATE, null, 2);
      return;
    }

    if (act === "form-save") {
      await wfSave(root);
      return;
    }

    if (act === "run-open") {
      var runId = target.getAttribute("data-run");
      wfUI.openRun = wfUI.openRun === runId ? null : runId;
      renderWorkflows(root);
      return;
    }

    if (act === "runs") {
      if (wfUI.runsOf === name) {
        wfUI.runsOf = null;
        wfUI.runs = [];
        wfUI.openRun = null;
        renderWorkflows(root);
        return;
      }
      wfUI.openRun = null;
      await loadRuns(name, root);
      var panel = $("#wf-runs-slot", root);
      if (panel) panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
      return;
    }

    if (act === "edit") {
      if (!wfUI.canManage) { toast("缺少 agent:write 权限", "err"); return; }
      try {
        var detail = await api("/api/v1/workflows/" + encodeURIComponent(name));
        wfUI.form = {
          mode: "edit",
          name: detail.name,
          display_name: detail.display_name,
          description: detail.description || "",
          definition: detail.definition || {}
        };
        renderWorkflows(root);
        var slot = $("#wf-form-slot", root);
        if (slot) slot.scrollIntoView({ block: "nearest", behavior: "smooth" });
      } catch (e) {
        toast("读取流程失败：" + errText(e), "err");
      }
      return;
    }

    if (act === "toggle") {
      var to = target.getAttribute("data-to") === "on";
      try {
        await api("/api/v1/workflows/" + encodeURIComponent(name), {
          method: "PATCH", body: { enabled: to }
        });
        toast(to ? "已启用" : "已停用");
        await loadWorkflows(root);
      } catch (e) {
        toast("操作失败：" + errText(e), "err");
      }
      return;
    }

    if (act === "del") {
      if (!window.confirm("删除后这张图不能再被触发（历史运行记录保留）。确定？")) return;
      try {
        await api("/api/v1/workflows/" + encodeURIComponent(name), { method: "DELETE" });
        toast("已删除");
        if (wfUI.runsOf === name) { wfUI.runsOf = null; wfUI.runs = []; }
        await loadWorkflows(root);
      } catch (e) {
        toast("删除失败：" + errText(e), "err");
      }
    }
  }

  async function wfSave(root) {
    if (!wfUI.canManage) { toast("缺少 agent:write 权限", "err"); return; }

    var form = wfUI.form || {};
    var editing = form.mode === "edit";
    var nameEl = $("#wf-name", root);
    var payload = {
      name: (nameEl ? nameEl.value : form.name || "").trim(),
      display_name: ($("#wf-display", root).value || "").trim(),
      description: ($("#wf-desc", root).value || "").trim() || null,
      enabled: true,
      definition: null
    };

    if (!payload.name) { toast("请填写标识", "err"); return; }
    if (!payload.display_name) { toast("请填写显示名", "err"); return; }

    // 先在本地解一次 JSON：语法错误在这里说「第几行」，比服务端一句
    // 「422 Unprocessable Entity」有用得多。
    var raw = $("#wf-def", root).value;
    try {
      payload.definition = JSON.parse(raw);
    } catch (e) {
      toast("图的 JSON 不成立：" + e.message, "err");
      return;
    }

    try {
      await api("/api/v1/workflows", { method: "POST", body: payload });
      toast(editing ? "已保存（修订号 +1）" : "已创建");
      wfUI.form = null;
      await loadWorkflows(root);
    } catch (e) {
      /* 保存即编译：422 里带着 {code, message}，那是给人照着改的一句话，
         直接显示比翻文档快。 */
      var detail = e.payload && e.payload.detail;
      var msg = detail && detail.message
        ? detail.code + "：" + detail.message
        : errText(e);
      toast("这张图跑不了 —— " + msg, "err");
    }
  }

  async function boot() {
    // 先读偏好再建导航：buildNav 内部会按 navMini 贴类，读晚了第一次渲染就是展开的。
    navMini = readNavMini();
    buildNav();
    await Promise.all([refreshStatus(), loadMe()]);
    buildNav();

    // 折叠只有一个入口（标题行右侧的钮）：两个入口就得保证两处状态同步，
    // 而「从哪点开、从哪收起」走出两种状态是最难查的那类 bug。
    function flipNav() {
      var next = !navMini;
      applyNavMini(next);
      try {
        localStorage.setItem(NAV_MINI_KEY, next ? "1" : "0");
      } catch (e) {
        // 存不下就这次生效：偏好丢了不影响用，没必要弹提示。
      }
    }
    var navBtn = $("#btn-nav");
    if (navBtn) navBtn.addEventListener("click", flipNav);

    // 「从文件新建」常驻侧栏，不挂在任何一条路由上：它的用途恰恰是
    // 「我还不知道该去哪个页面」的时候。
    var newBtn = $("#btn-new-import");
    if (newBtn) newBtn.addEventListener("click", openImportDialog);

    // 构建助手是常驻 UI，不挂在任何一条路由上：在任何页面都能打开它改配置。
    initStudio();

    window.addEventListener("hashchange", function () {
      if (!location.hash) { location.hash = "#/overview"; return; }
      render();
    });

    var userBtn = $("#btn-user");
    if (userBtn) {
      userBtn.addEventListener("click", function () { setFootMenu(!footMenuOpen); });
    }
    var menuRefresh = $("#menu-refresh");
    if (menuRefresh) {
      menuRefresh.addEventListener("click", function () {
        closeFootMenu();
        refreshStatus();
        render();
      });
    }
    var menuSignout = $("#menu-signout");
    if (menuSignout) menuSignout.addEventListener("click", signOut);

    // 点别处、按 Esc、页面滚动都收起：菜单是 fixed 定位的，侧栏一滚它就
    // 和按钮脱开——继续挂在原处比收起更让人困惑。
    document.addEventListener("click", function (e) {
      if (!footMenuOpen) return;
      if (e.target.closest && e.target.closest("#foot-user")) return;
      closeFootMenu();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !footMenuOpen) return;
      closeFootMenu();
      if (userBtn) userBtn.focus();
    });
    window.addEventListener("resize", closeFootMenu);
    window.addEventListener("scroll", closeFootMenu, true);

    if (!location.hash) location.hash = "#/overview";
    await render();
  }

  boot();
})();
