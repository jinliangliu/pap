/**
 * 刻度导航的纯逻辑：不碰 DOM、不依赖 React。
 *
 * 拆出来的原因很实际——这三件事都容易写错，而且错了只看截图很难发现：
 * 顺序是否单调、刻度有没有跑出轨道、截断会不会把 emoji 劈成半个。
 * 放在这里就能用 node 直接跑断言（scripts/check_rail.mjs）。
 */

/** 相邻两条刻度的最小中心距（px）。再小两枚按钮就叠在一起，点哪条都是碰运气。 */
export const MIN_GAP = 10;

/**
 * 取一轮提问的摘要：压平空白后按「字符」截断到 limit 个。
 *
 * 用 Array.from 而不是 slice：JS 的 length 数的是 UTF-16 码元，
 * 「👍」是两个码元，slice(0, 20) 会把它劈成半个代理对，渲染出 �。
 */
export function summarize(text, limit = 20) {
  const flat = String(text ?? "").replace(/\s+/g, " ").trim();
  if (!flat) return "";
  const chars = Array.from(flat);
  return chars.length <= limit ? flat : chars.slice(0, limit).join("") + "…";
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

/**
 * 把每一轮的文档偏移换算成轨道上的纵坐标。
 *
 * 按内容比例摆放，不是等距：这样刻度和滚动条讲的是同一件事——
 * 「这一轮在整段对话的什么位置」。等距只能回答「第几轮」，
 * 而长对话里人真正想知道的是「我还剩多少」。
 *
 * 代价是消息长短悬殊时刻度会挤在一起。所以加一道最小间距：
 * 先自上而下推开，再从底部回推。顺序永远不变——刻度跳序比刻度挤更难接受，
 * 那会让「越往下越靠后」这个唯一的阅读前提失效。
 */
export function layoutTicks(offsets, contentHeight, railHeight, minGap = MIN_GAP) {
  const count = offsets.length;
  if (!count || railHeight <= 0) return [];

  // 轨道短到装不下 n 条带间距的刻度时，直接退化为等距。
  // 另一条路是「先满足最小间距、再把溢出的压回边界」，但那样会有好几条
  // 重合在同一个 y 上——两条刻度叠在一起，点哪条都是碰运气，
  // 不如承认「这轨道太小了」，至少保序、保可达。
  if (railHeight < (count - 1) * minGap) {
    return offsets.map((_, index) => (count === 1 ? 0 : (index / (count - 1)) * railHeight));
  }

  const span = Math.max(1, contentHeight);
  const ys = offsets.map((offset) => clamp((offset / span) * railHeight, 0, railHeight));

  // ① 自上而下：每条至少比上一条低一个间距。
  for (let i = 1; i < count; i += 1) {
    ys[i] = Math.max(ys[i], ys[i - 1] + minGap);
  }

  // ② 推开后可能溢出轨道底部：从最后一条往回压。
  //    上一段的退化分支已经保证了 (count-1)*minGap <= railHeight，
  //    所以压回来一定能落到 0 以上的位置。
  if (ys[count - 1] > railHeight) {
    ys[count - 1] = railHeight;
    for (let i = count - 2; i >= 0; i -= 1) {
      ys[i] = Math.min(ys[i], ys[i + 1] - minGap);
    }
  }

  return ys;
}

/**
 * 从已观测到的消息里选出「正在读的那一轮」。
 *
 * 判据不是「可见面积占比」：长消息的占比天然比短消息小，
 * 一条 200 字的回答永远赢不过一条 10 字的提问，高亮就会一直停在前者。
 *
 * 也不用「是否与阅读线相交」：阅读线常常落在两条用户提问之间
 * （比如某条回答的正中间），此时没有任何提问与带相交，按相交判就一条都选不出来，
 * 表现为「滚到某处突然没有高亮」——这不是边界情况，是长对话的常态。
 *
 * 所以只看位置：已经越过阅读线、且离它最近的那条，就是正在读的。
 * 一条都没越过（滚到顶部之下、或轨道被清空）时，退到即将进入的那条。
 *
 * @param {Array<{id: string, top: number}>} entries 消息相对视口的顶边坐标
 * @param {number} bandTop 阅读线的视口纵坐标
 */
export function pickActive(entries, bandTop) {
  let above = null;
  let below = null;
  for (const entry of entries) {
    if (!entry || typeof entry.top !== "number" || Number.isNaN(entry.top)) continue;
    if (entry.top <= bandTop) {
      if (!above || entry.top > above.top) above = entry;
    } else if (!below || entry.top < below.top) {
      below = entry;
    }
  }
  const winner = above || below;
  return winner ? winner.id : null;
}
