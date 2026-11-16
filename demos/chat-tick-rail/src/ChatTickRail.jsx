import { useCallback, useEffect, useRef, useState } from "react";
import { layoutTicks, pickActive, summarize } from "./railMath.js";

/**
 * 阅读线在容器顶边下方多少像素。
 *
 * 定在顶边而不是「屏幕三分之一处」，是为了让两件事讲同一个故事：
 * 点击刻度用 scrollIntoView(block:'start') 会把那一轮顶到容器顶部，
 * 若阅读线在 35%，滚动一停线就落到下一轮身上了——变成「点 6 亮 7」。
 * 线贴着顶边，含义就明确成「这一屏是从哪一轮开始的」。
 */
const BAND_TOP = 16;
/** 首尾刻度距轨道端点的余量。 */
const TICK_PAD = 6;

/**
 * 长对话右侧刻度导航。
 *
 * 定位是「地图」，不是「目录」：刻度按内容比例摆放，所以滑块的位置本身
 * 就在回答「我还剩多少」。为此不做锚点——不写 hash、不改 URL、不靠 :target，
 * 点击只调 scrollIntoView；当前轮次完全由 IntersectionObserver 推出来。
 *
 * 用法：
 *   <div className="relative">            // 需要定位上下文
 *     <div ref={scrollerRef} className="h-full overflow-y-auto">
 *       {messages.map(...)}               // 用户气泡加 data-rail-item 与 data-rail-id
 *     </div>
 *     <ChatTickRail scrollerRef={scrollerRef} rightInset={sidebarOpen ? 320 : 0} />
 *   </div>
 *
 * 摘要默认取气泡的 textContent；气泡里还有时间戳之类的噪声时，
 * 用 data-rail-summary 覆盖。
 */
export default function ChatTickRail({
  scrollerRef,
  itemAttribute = "data-rail-item",
  rightInset = 0,
  minRounds = 3,
  summaryLimit = 20,
}) {
  const [rounds, setRounds] = useState([]);
  const [ticks, setTicks] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const railRef = useRef(null);

  /* ---- ① 从 DOM 里认轮次 -------------------------------------------------
     不要求调用方再传一份 messages：气泡就是唯一事实来源。
     列表虚拟化、消息流式追加、编辑后重渲染，都以 DOM 为准，导航自动跟上。 */
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return undefined;

    let frame = 0;
    const sync = () => {
      const nodes = Array.from(scroller.querySelectorAll(`[${itemAttribute}]`));
      setRounds((prev) => {
        const next = nodes.map((node, index) => ({
          id: node.dataset.railId || `r${index}`,
          node,
          summary: summarize(node.dataset.railSummary || node.textContent, summaryLimit),
        }));
        const same =
          prev.length === next.length &&
          prev.every((item, index) => item.id === next[index].id && item.node === next[index].node);
        // 返回同一个引用：否则每条流式 token 都会触发一次重排重渲染。
        return same ? prev : next;
      });
    };

    sync();
    const observer = new MutationObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(sync);
    });
    observer.observe(scroller, { childList: true, subtree: true, characterData: true });
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [scrollerRef, itemAttribute, summaryLimit]);

  /* ---- ② 量位置 ----------------------------------------------------------
     用 rect 差值而不是 offsetTop：offsetTop 是相对 offsetParent 的，
     中间只要有一层 position:relative 的包裹，算出来的偏移就整体错位。 */
  const measure = useCallback(() => {
    const scroller = scrollerRef.current;
    const rail = railRef.current;
    if (!scroller || !rail) return;
    const box = scroller.getBoundingClientRect();
    const offsets = rounds.map(
      (round) => round.node.getBoundingClientRect().top - box.top + scroller.scrollTop,
    );
    // 首尾各留 6px：第一轮/最后一轮的刻度若正好压在轨道端点上，
    // 看起来像被裁掉了一半。留出余量后它们才是「完整的一格」。
    const room = Math.max(0, rail.clientHeight - TICK_PAD * 2);
    setTicks(layoutTicks(offsets, scroller.scrollHeight, room).map((y) => y + TICK_PAD));
  }, [rounds, scrollerRef]);

  useEffect(() => {
    measure();
  }, [measure]);

  // 容器尺寸变化（窗口缩放、右侧栏开合挤压、字体加载完）都要重量一遍。
  useEffect(() => {
    const scroller = scrollerRef.current;
    const rail = railRef.current;
    if (!scroller || !rail) return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    observer.observe(rail);
    return () => observer.disconnect();
  }, [measure, scrollerRef]);

  /* ---- ③ 判定当前轮 ------------------------------------------------------
     判据只有一条：越过阅读线且离它最近的那条。

     IO 与滚动事件都只是「触发器」，真正的位置在 recompute 里现读。
     踩过的坑：把 IO 回调里的 boundingClientRect 存下来当位置表是错的——
     IO 只在「相交状态变化」时回调，一次跳转后，那些从头到尾都没进过
     判定带的轮次会一直保留旧坐标（实测 u1–u4 停在 +411/+651/+890），
     在判定里全被当成「在阅读线下方」，高亮就再也挪不动。
     两边读同一份实时几何，才不可能给出两个互相矛盾的答案。 */
  const recompute = useCallback(() => {
    const scroller = scrollerRef.current;
    if (!scroller || !rounds.length) return;
    const box = scroller.getBoundingClientRect();
    // 贴底时最后一条可能够不到阅读线（它太短），显式认它，
    // 否则滚到底部却还高亮着倒数第二条。
    const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 8;
    if (atBottom) {
      setActiveId(rounds[rounds.length - 1].id);
      return;
    }
    const entries = rounds.map((round) => ({
      id: round.id,
      top: round.node.getBoundingClientRect().top,
    }));
    setActiveId(pickActive(entries, box.top + BAND_TOP));
  }, [rounds, scrollerRef]);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller || rounds.length < minRounds) return undefined;

    const observer = new IntersectionObserver(
      () => {
        // 回调内容不用看：真正需要的坐标在 recompute 里现读。
        // 这里只借它「某轮跨过阅读线」这个事件。
        recompute();
      },
      {
        root: scroller,
        // 判定带贴着容器顶边，所以只观察上半部分：
        // 屏幕下方的进出与胜负无关，回调越少越不会引起无谓重算。
        rootMargin: "0px 0px -80% 0px",
        threshold: [0],
      },
    );
    rounds.forEach((round) => observer.observe(round.node));

    let frame = 0;
    const onScroll = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        recompute();
      });
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });

    return () => {
      observer.disconnect();
      scroller.removeEventListener("scroll", onScroll);
      cancelAnimationFrame(frame);
    };
  }, [rounds, minRounds, recompute, scrollerRef]);

  /* ---- ④ 跳转 ------------------------------------------------------------ */
  const goTo = useCallback(
    (round) => {
      if (!round?.node) return;
      const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      // 不写 hash：锚点会把历史塞满、还会和路由打架，这里只要滚动。
      round.node.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    },
    [],
  );

  const focusNeighbor = (event, index) => {
    const step = event.key === "ArrowDown" ? 1 : event.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const buttons = event.currentTarget.parentElement.querySelectorAll("[data-tick]");
    const next = buttons[index + step];
    if (next) next.focus();
  };

  if (rounds.length < minRounds) return null;

  return (
    <div
      // 收起侧栏 / 展开侧栏时整条导航平移过去，而不是突然跳位。
      className="pointer-events-none absolute inset-y-0 z-30 transition-[right] duration-300 ease-out"
      style={{ right: rightInset + 8 }}
    >
      <div ref={railRef} className="absolute inset-y-4 right-0 w-6" aria-hidden="false">
        {/* 轴线：虚线，本身就是「这是一把尺」的视觉信号 */}
        <span className="absolute left-1/2 top-0 h-full w-px -translate-x-1/2 border-l border-dashed border-slate-300/90" />

        <nav aria-label="对话进度导航" className="absolute inset-0">
          {rounds.map((round, index) => {
            const active = round.id === activeId;
            const top = ticks[index];
            if (top == null) return null;
            return (
              <button
                key={round.id}
                type="button"
                data-tick={index}
                aria-current={active ? "true" : undefined}
                aria-label={`第 ${index + 1} 轮：${round.summary}`}
                onClick={() => goTo(round)}
                onKeyDown={(event) => focusNeighbor(event, index)}
                style={{ top }}
                className="group pointer-events-auto absolute left-1/2 flex h-4 w-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-slate-400"
              >
                <span
                  className={[
                    "block h-[3px] rounded-full transition-all duration-200",
                    active
                      ? "w-6 bg-emerald-600 shadow-[0_0_0_3px_rgba(5,150,105,0.14)]"
                      : "w-3 bg-slate-300 group-hover:w-[18px] group-hover:bg-slate-500",
                  ].join(" ")}
                />

                {/* 气泡：靠 group-hover / group-focus-visible 出现，
                    不额外存 hover 状态——悬停是纯视觉，走 CSS 更省事也不会掉帧。 */}
                <span
                  role="tooltip"
                  className="pointer-events-none absolute right-full top-1/2 mr-2.5 min-w-[7rem] max-w-[13rem] -translate-y-1/2 translate-x-1 rounded-lg bg-slate-900/95 px-2.5 py-1.5 text-left opacity-0 shadow-lg ring-1 ring-black/5 transition duration-150 group-hover:translate-x-0 group-hover:opacity-100 group-focus-visible:translate-x-0 group-focus-visible:opacity-100"
                >
                  <span className="mb-0.5 block text-[10px] font-medium leading-none text-slate-400">
                    第 {index + 1} 轮
                  </span>
                  <span className="block text-[11px] leading-snug text-white">
                    {round.summary || "（无文字内容）"}
                  </span>
                  <span className="absolute -right-[3px] top-1/2 h-1.5 w-1.5 -translate-y-1/2 rotate-45 bg-slate-900/95" />
                </span>
              </button>
            );
          })}
        </nav>
      </div>
    </div>
  );
}
