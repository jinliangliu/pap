/* 输入框里的斜杠命令：只做识别，不做副作用。
 *
 * 目前只有 `build` 一个。放成独立文件而不是塞在 console.js 里，是因为
 * console.js 是整页 IIFE（要 DOM 才能加载），而这三十行是纯函数——单独一份
 * 才能用 node + vm 直接自检（`scripts/check_build_handoff.mjs`）。
 *
 * **判据必须与服务端 `chat_service.is_build_command` 一致。** 两边分叉的表现是
 * 一侧认得、另一侧不认：前端认了而后端不认，用户会看到「已转到构建助手」然后
 * 什么也没发生；后端认了而前端不认，这句话会被原样发出去、被 409 打回来。
 */
(function () {
  "use strict";

  /* 返回 null 表示「不是 /build 命令」，返回字符串表示「要带过去的描述」。
   *
   * 空串与 null 必须分开：`/build` 独占一条消息是合法的（只是打开面板），
   * 而 `/builder` 不是命令。两者混成 falsy 会让后者也去开面板。
   *
   * 只认开头，且命令词后面必须是空白或结尾：`/buildall`、`请用 /build` 都不算。
   * 尾部描述允许跨行——用户常常先粘贴一段需求再回车。 */
  function buildBrief(text) {
    var value = String(text == null ? "" : text).trim();
    if (!/^\/build(?=\s|$)/i.test(value)) return null;
    return value.replace(/^\/build\s*/i, "").trim();
  }

  window.PAPChatCommand = { buildBrief: buildBrief };
})();
