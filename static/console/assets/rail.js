// 会话内刻度导航的纯逻辑。不碰 DOM，可单独在 node 里断言
// （scripts/check_chat_rail.mjs）。
//
// 与 render.js 分开的理由一样：这三件事都容易写错，而且错了只看截图很难发现——
// 截断把 emoji 劈成半个、刻度顺序反了、端点跑到轨道外。
// console.js 只负责量几何与挂事件，凡是能算的都放这里。
(function () {
  "use strict";

  /** 相邻两条刻度的最小中心距（px）。再小两枚按钮就叠在一起，点哪条都是碰运气。 */
  var MIN_GAP = 10;

  /** 气泡里显示的摘要字数。 */
  var SUMMARY_LIMIT = 20;

  /**
   * 取一轮提问的摘要：压平空白后按「字符」截断。
   *
   * 用 Array.from 而不是 slice：JS 的 length 数的是 UTF-16 码元，
   * 「👍」占两个码元，slice 会把它劈成半个代理对，渲染出乱码方块。
   */
  function summarize(text, limit) {
    var cap = limit === undefined ? SUMMARY_LIMIT : limit;
    var flat = String(text === undefined || text === null ? "" : text)
      .replace(/\s+/g, " ").trim();
    if (!flat) return "";
    var chars = Array.from(flat);
    return chars.length <= cap ? flat : chars.slice(0, cap).join("") + "…";
  }

  /**
   * 把每一轮的内容偏移换算成轨道上的纵坐标。
   *
   * 按内容比例摆放而不是等距：这样刻度和滚动条讲的是同一件事——
   * 这一轮在整段对话的什么位置。等距只能回答「第几轮」，
   * 而长对话里人真正想知道的是「我还剩多少」。
   *
   * 代价是长短悬殊时刻度会挤在一起，所以加一道最小间距：先自上而下推开，
   * 再从底部回推。顺序永远不变——刻度跳序比刻度挤更难接受，
   * 那会让「越往下越靠后」这个唯一的阅读前提失效。
   */
  function layoutTicks(offsets, contentHeight, railHeight, minGap) {
    var count = offsets.length;
    var gap = minGap === undefined ? MIN_GAP : minGap;
    if (!count || railHeight <= 0) return [];

    // 轨道短到装不下 n 条带间距的刻度时退化为等距。
    // 另一条路是先满足间距再把溢出的压回边界，那样会有几条重合在同一个 y 上，
    // 两条刻度叠在一起，点哪条都是碰运气。
    if (railHeight < (count - 1) * gap) {
      return offsets.map(function (_, index) {
        return count === 1 ? 0 : (index / (count - 1)) * railHeight;
      });
    }

    var span = Math.max(1, contentHeight || 0);
    var ys = offsets.map(function (offset) {
      var y = (offset / span) * railHeight;
      return Math.min(railHeight, Math.max(0, y));
    });

    for (var i = 1; i < count; i++) {
      ys[i] = Math.max(ys[i], ys[i - 1] + gap);
    }
    if (ys[count - 1] > railHeight) {
      ys[count - 1] = railHeight;
      for (var j = count - 2; j >= 0; j--) {
        ys[j] = Math.min(ys[j], ys[j + 1] - gap);
      }
    }
    return ys;
  }

  /**
   * 选出「正在读的那一轮」。
   *
   * 判据不是可见面积占比：长回答的占比天然比短提问小，
   * 一条 200 字的回答永远赢不过一条 10 字的提问，高亮就会一直停在前者。
   *
   * 也不用「是否与阅读线相交」：阅读线常常落在两条提问之间
   * （比如某条回答的正中间），此时没有任何提问与带相交，按相交判就一条都选不出来，
   * 表现为「滚到某处突然没有高亮」——这不是边界情况，是长对话的常态。
   *
   * 所以只看位置：已经越过阅读线、且离它最近的那条。一条都没越过时退到即将进入的那条。
   */
  function pickActive(entries, bandTop) {
    var above = null;
    var below = null;
    for (var i = 0; i < entries.length; i++) {
      var entry = entries[i];
      if (!entry || typeof entry.top !== "number" || isNaN(entry.top)) continue;
      if (entry.top <= bandTop) {
        if (!above || entry.top > above.top) above = entry;
      } else if (!below || entry.top < below.top) {
        below = entry;
      }
    }
    var winner = above || below;
    return winner ? winner.id : null;
  }

  window.PAPRail = {
    MIN_GAP: MIN_GAP,
    SUMMARY_LIMIT: SUMMARY_LIMIT,
    summarize: summarize,
    layoutTicks: layoutTicks,
    pickActive: pickActive
  };
})();
