/* Personal Agent Platform · 审批卡的状态判断
 *
 * 单独成文件的理由和 render.js 一样：这些是纯逻辑，pytest 里没有浏览器，
 * 而它们恰恰是最容易出错的地方——审批号服务端一直存着，是前端自己把它丢了，
 * 于是刷新之后那段历史读起来像结论，其实还在等人点。
 *
 * 约定：这里不碰 DOM，也不发请求。能不能点、该写哪句话，全部由入参决定，
 * 这样 node 直接加载就能断言（scripts/check_approval_card.mjs）。
 *
 * 「能不能点」不在这一层判断，由服务端给的 `actionable` 决定：过期与否取决于
 * 时钟，浏览器差几分钟就会把还能确认的单子说成已过期，反过来更糟——显示成可点，
 * 点下去只拿到 409。
 */
(function () {
  "use strict";

  var TEXT = {
    APPROVED: "已确认，等待执行",
    REJECTED: "已取消，未执行",
    EXPIRED: "已过期，未执行——需要重新发起",
    EXECUTING: "执行中",
    EXECUTED: "已执行，尚未核验",
    VERIFIED: "已执行并核验通过",
    FAILED: "执行失败",
    RECOVERY_REQUIRED: "执行异常，需按幂等键回读",
    EXECUTION_UNKNOWN: "结果未知，不要重发"
  };

  /* 服务端的状态机比这里丰富，没列到的一律原样显示，
     免得新增一个状态时界面上出现空白。 */
  function statusText(status) {
    return TEXT[status] || status || "";
  }

  /* 一张已经不能再点的单子，卡上该写哪句话。
   *
   * 先给结论，再给原因：`status` 说「执行失败」，`error_code` 说
   * `DELEGATION_REVOKED`——只写结论，运维得翻日志才知道该去找谁；
   * 只看错误码，又说不清到底执行了没有。这两者必须同时出现。
   */
  function outcome(ap) {
    var status = ap && ap.status ? String(ap.status) : "";
    var text = status === "PENDING"
      // 还能点的时候不会走到这里；走到这里只剩一个解释：过期时间到了，
      // 而清理任务还没跑到。
      ? "已过期，未执行——需要重新发起"
      : statusText(status);
    if (ap && ap.error_code) text += "（" + ap.error_code + "）";
    return text || "已处理";
  }

  /* 历史消息 → 卡片数据。
   *
   * 数据库里只存了「这句话属于哪张单子」，卡上的参数与当前状态都在审批单上，
   * 所以要由调用方按号取回来再传进来。`approvals` 的形状是
   * `{ ok: boolean, items: { 审批号: 审批单 } }`。
   *
   * 取不到与「没有这张单子」是两件事，必须分开：前者刷新就好，后者得重新发起。
   * 混成一个，用户会一直等一张永远不会出现的按钮。 */
  function fromMeta(meta, approvals) {
    var id = meta && meta.approval_id;
    if (!id) return null;
    id = String(id);

    if (!approvals || !approvals.ok) {
      return {
        approval_id: id,
        title: "状态未知",
        preview: {},
        open: false,
        done: "审批状态读取失败，刷新重试。"
      };
    }

    var ap = approvals.items ? approvals.items[id] : null;
    if (!ap) {
      // 单子被保留期清理掉了。这时不能说「待确认」：那张卡点了只会 404。
      return {
        approval_id: id,
        title: "已失效",
        preview: {},
        open: false,
        done: "查不到这张审批单（可能已被清理）。"
      };
    }

    var preview = ap.preview || {};
    if (!preview.tool_name) preview.tool_name = ap.tool_name;
    if (!preview.parameters) preview.parameters = {};

    return {
      approval_id: id,
      title: ap.actionable ? "需要审批" : "已处理",
      preview: preview,
      /* 原始状态要留住。`done` 是给人读的一句话，反推不出「卡在流程哪一步」——
         「已执行，尚未核验」里也含「已执行」。流程条按这个字段画。 */
      status: ap.status || "",
      expires_at: ap.expires_at,
      executed_at: ap.executed_at,
      // 「还能不能点」与「有没有结论」是两件事，必须分开。
      // 合成一个字段（有结论就没按钮）看着省事，代价是：提交失败时只能写一句
      // 结论，按钮跟着消失，卡片变成点不动的死卡，非刷新不可。
      open: Boolean(ap.actionable),
      done: ap.actionable ? "" : outcome(ap)
    };
  }

  /* 409 也可能是结论。
   *
   * 确认/取消这两个动作，服务端会用 409 表示「当前状态不允许执行」——已过期、
   * 已取消、执行中。它和网络抖动的区别是：body 里带着审批单的**现状**
   * （`status` 字段）。当失败处理会留下最糟的一种界面：卡片还能再点，
   * 而那张单子已经不可能执行了，点几次都是 409。
   *
   * 没有 body 的 409（网关、别处的中间件）仍然返回 null，交给失败分支——
   * 那种情况下我们确实不知道单子变成了什么，不能替服务端写结论。
   */
  function settledFromError(err) {
    if (!err || err.status !== 409) return null;
    var body = err.payload;
    if (!body || typeof body !== "object" || !body.status) return null;
    var text = outcome({ status: body.status, error_code: body.error_code });
    if (body.message) text += " · " + String(body.message).split("\n")[0];
    return text;
  }

  window.PAPApproval = {
    statusText: statusText,
    outcome: outcome,
    settledFromError: settledFromError,
    fromMeta: fromMeta
  };
})();
