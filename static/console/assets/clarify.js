/* 追问卡片（radio 选择面板）
 *
 * 后端在 `intent=clarify` 时会给 `metadata.clarify = {items: [...]}`：
 *
 *   [{question, field, options: [{label, value}]}]
 *
 * 这里只做纯逻辑，不碰 DOM：规范化、生成 HTML、把用户的选择拼成一句话。
 * 拆出来的理由与 render.js / approval.js 相同——这几件事错了只看截图发现不了，
 * 放在这里就能用 `node scripts/check_clarify.mjs` 直接断言。
 *
 * 设计口径（对照 WorkBuddy 的分项确认）：
 *   - 一项 = 一个单选组。用户点哪条，哪条就是回答，不需要先"选中"再"发送"。
 *   - 单组时点一下直接发出去；多组时选齐了再点一次「提交」——
 *     多组场景下点第一下就发送，会让用户没法改前面的选择。
 *   - 每组末尾固定一条「其他（自己填写）」，选中后展开输入框。
 *     **这一条由前端补，不占后端的名额**：模型只需要给出它真正能枚举的候选。
 */
(function () {
  "use strict";

  var MAX_ITEMS = 3;
  var MAX_OPTIONS = 6;
  var CUSTOM = "__custom__";

  function text(value, limit) {
    if (typeof value !== "string") return "";
    var trimmed = value.trim();
    return trimmed.length > limit ? trimmed.slice(0, limit) : trimmed;
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* 后端已经洗过一遍（app/agents/clarify.py），这里再洗一遍有两个理由：
     1. 历史消息是库里翻出来的，可能来自更早的版本，结构不一定跟现在一致；
     2. 这是拼 HTML 的地方，长度和条数在这里不受控就是界面事故。 */
  function normalize(raw) {
    var list = raw && raw.items ? raw.items : raw;
    if (!list || typeof list.length !== "number") return [];

    var out = [];
    var seenQuestions = {};

    for (var i = 0; i < list.length && out.length < MAX_ITEMS; i++) {
      var item = list[i];
      if (!item || typeof item !== "object") continue;

      var question = text(item.question, 180);
      if (!question || seenQuestions[question]) continue;
      seenQuestions[question] = true;

      var options = [];
      var seenLabels = {};
      var rawOptions = item.options && typeof item.options.length === "number" ? item.options : [];
      for (var j = 0; j < rawOptions.length && options.length < MAX_OPTIONS; j++) {
        var option = rawOptions[j];
        var label = "";
        var value = "";
        if (option && typeof option === "object") {
          label = text(option.label, 60);
          value = text(option.value, 400);
        } else {
          label = text(option, 60);
        }
        if (!label || seenLabels[label]) continue;
        seenLabels[label] = true;
        options.push({ label: label, value: value || label });
      }

      out.push({
        question: question,
        field: text(item.field, 64) || null,
        options: options
      });
    }

    return out;
  }

  /* 选中项还原。
     picks 形如 {0: {value: "写维护工单"}, 1: {custom: "站号 027"}}。
     返回可直接用于渲染的 {qi: {choice, custom}}。 */
  function normalizePicks(picks) {
    var out = {};
    if (!picks || typeof picks !== "object") return out;
    Object.keys(picks).forEach(function (key) {
      var pick = picks[key];
      if (!pick || typeof pick !== "object") return;
      var choice = pick.choice === CUSTOM ? CUSTOM : text(pick.choice, 400);
      var custom = text(pick.custom, 400);
      if (!choice && !custom) return;
      // 只有字没有选择：按「其他」算。只填不选的项（没有候选）走的正是这条路。
      out[key] = { choice: choice || CUSTOM, custom: custom };
    });
    return out;
  }

  function isCustom(pick) {
    return Boolean(pick && pick.choice === CUSTOM);
  }

  /* 一项算不算答完了：选了具体候选即可；选了「其他」就还得有字。 */
  function answered(item, pick) {
    if (!pick) return false;
    if (isCustom(pick)) return Boolean(pick.custom);
    return Boolean(pick.choice);
  }

  function allAnswered(items, picks) {
    for (var i = 0; i < items.length; i++) {
      if (!answered(items[i], picks[i])) return false;
    }
    return items.length > 0;
  }

  function optionHtml(item, qi, index, pick, closed) {
    // 一项连候选都没有时（站点编号、金额、时间这类只能手填的），
    // 不再摆一行「其他（自己填写）」——它本来是为了在候选之外另写，
    // 一个候选都没有的时候，那行字只会让人以为上面还有没显示出来的选项。
    var freeOnly = item.options.length === 0;

    var html = "";
    for (var i = 0; i < item.options.length; i++) {
      var option = item.options[i];
      var on = pick && !isCustom(pick) && pick.choice === option.value;
      html += '<label class="cl-opt' + (on ? " is-on" : "") + '">' +
        '<input type="radio" name="cl-' + index + "-" + qi + '"' +
        ' value="' + esc(option.value) + '"' +
        (on ? " checked" : "") + (closed ? " disabled" : "") + ">" +
        '<span class="cl-mark" aria-hidden="true"></span>' +
        '<span class="cl-label">' + esc(option.label) + "</span>" +
        "</label>";
    }

    var customOn = isCustom(pick);
    var customValue = customOn ? (pick.custom || "") : "";

    if (!freeOnly) {
      html += '<label class="cl-opt cl-opt-custom' + (customOn ? " is-on" : "") + '">' +
        '<input type="radio" name="cl-' + index + "-" + qi + '"' +
        ' value="' + CUSTOM + '"' + (customOn ? " checked" : "") +
        (closed ? " disabled" : "") + ">" +
        '<span class="cl-mark" aria-hidden="true"></span>' +
        '<span class="cl-label">其他（自己填写）</span>' +
        "</label>";
    }

    // 只填不选的项：未关闭时总有输入框；已关闭时只有当时真填过才回显，
    // 否则会留下一个空白框，像是话没说完。
    var showInput = customOn || (freeOnly && (!closed || Boolean(customValue)));

    html += '<input class="cl-input" type="text" data-cl-input="' + qi + '"' +
      ' placeholder="写下你的说明…"' +
      ' value="' + esc(customValue) + '"' +
      (showInput ? "" : " hidden") + (closed ? " disabled" : "") + ">";

    return html;
  }

  /* closed = 这一轮已经过去了（后面出现了新的用户消息）。
     作答过就显示选了什么，没作答就说"已跳过"——比继续摆一排能点的选项诚实。 */
  function html(items, opts) {
    opts = opts || {};
    if (!items || !items.length) return "";

    var index = opts.index === undefined ? "" : opts.index;
    var picks = normalizePicks(opts.picks);
    var closed = Boolean(opts.closed);
    var single = items.length === 1;

    var body = "";
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var picked = doneText(item, picks[i]);
      body += '<div class="cl-item" data-qi="' + i + '">' +
        '<div class="cl-q">' +
        (single ? "" : '<span class="cl-n">' + (i + 1) + "</span>") +
        '<span class="cl-q-text">' + esc(item.question) + "</span>" +
        (closed && picked ? '<span class="cl-done">已选：' + esc(picked) + "</span>" : "") +
        "</div>" +
        optionHtml(item, i, index, picks[i], closed) +
        "</div>";
    }

    // 只有「单组 + 有候选」才靠点击直接回答。单组但没有候选（只能手填）时
    // 没有东西可点，仍要给出提交按钮，否则用户填完字只能瞎猜能不能回车。
    var clickToAnswer = single && items[0].options.length > 0;
    var ready = allAnswered(items, picks);

    var foot;
    if (closed) {
      var any = false;
      for (var k = 0; k < items.length; k++) {
        if (answered(items[k], picks[k])) { any = true; break; }
      }
      foot = '<div class="cl-foot"><span class="cl-hint">' +
        (any ? "已按上面的选择继续" : "这一轮已跳过，见下方回复") +
        "</span></div>";
    } else if (clickToAnswer) {
      foot = '<div class="cl-foot"><span class="cl-hint">' +
        "点一下即作为你的回答；都不合适就选「其他」自己写。" +
        "</span></div>";
    } else {
      var hint;
      if (ready) {
        hint = "共 " + items.length + " 项，都已选好。";
      } else if (single) {
        hint = "填好后按回车，或点左边的按钮。";
      } else {
        hint = "还在等 " + pendingCount(items, picks) + " 项。";
      }
      foot = '<div class="cl-foot">' +
        '<button class="btn primary sm" type="button" data-cl-submit="' + index + '"' +
        (ready ? "" : " disabled") + ">提交选择</button>" +
        '<span class="cl-hint">' + hint + "</span></div>";
    }

    return '<div class="clarify' + (closed ? " is-closed" : "") +
      '" data-clarify="' + index + '">' +
      '<div class="cl-head">需要你定一下' +
      (single ? "" : '<span class="cl-count">' + items.length + " 项</span>") +
      "</div>" + body + foot + "</div>";
  }

  function pendingCount(items, picks) {
    var n = 0;
    for (var i = 0; i < items.length; i++) {
      if (!answered(items[i], picks[i])) n += 1;
    }
    return n;
  }

  // 一项的最终答案：选了「其他」用输入的字，否则用候选的值。
  function doneText(item, pick) {
    if (!pick) return "";
    if (isCustom(pick)) return pick.custom || "";
    for (var i = 0; i < (item.options || []).length; i++) {
      if (item.options[i].value === pick.choice) return item.options[i].label;
    }
    return pick.choice;
  }

  /* 把选择拼成一条用户消息。
     多组时带上问题本身：只发「维护工单／高／是」这三行，下一轮模型
     未必还记得它们分别对应哪个问题（历史会被压缩）。 */
  function compose(items, picks) {
    var lines = [];
    for (var i = 0; i < items.length; i++) {
      var answer = doneText(items[i], picks[i]);
      if (!answer) continue;
      if (items.length === 1) lines.push(answer);
      else lines.push("- " + items[i].question + "：" + answer);
    }
    return lines.join("\n");
  }

  window.PAPClarify = {
    normalize: normalize,
    normalizePicks: normalizePicks,
    html: html,
    compose: compose,
    allAnswered: allAnswered,
    answered: answered,
    pending: pendingCount,
    CUSTOM: CUSTOM
  };
})();
