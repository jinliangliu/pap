import { useEffect, useMemo, useRef, useState } from "react";
import ChatTickRail from "./ChatTickRail.jsx";
import { ROUNDS } from "./demoData.js";

// 截图/联调用的开关：?sidebar=1 右侧栏默认展开、?rounds=2 只留两轮、?hover=3 强制第 3 条刻度的气泡出现。
const params = new URLSearchParams(window.location.search);

export default function App() {
  const scrollerRef = useRef(null);
  const [sidebar, setSidebar] = useState(params.get("sidebar") === "1");
  const [short, setShort] = useState(params.get("rounds") === "2");

  const rounds = useMemo(() => (short ? ROUNDS.slice(0, 2) : ROUNDS), [short]);
  const messages = useMemo(
    () =>
      rounds.flatMap((round, index) => [
        { id: `u${index}`, role: "user", text: round.q, round: index },
        { id: `a${index}`, role: "assistant", text: round.a, round: index },
      ]),
    [rounds],
  );

  // 探针：只为了截一张「气泡展开」的图。组件本身不认这个参数。
  useEffect(() => {
    const index = params.get("hover");
    if (index == null) return undefined;
    const style = document.createElement("style");
    style.textContent = `[data-tick="${index}"] [role="tooltip"]{opacity:1!important;transform:translate(0,-50%)!important;}`;
    document.head.appendChild(style);
    return () => style.remove();
  }, []);

  return (
    <div className="flex h-full flex-col">
      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-slate-200 bg-white px-5">
        <div className="grid h-7 w-7 place-items-center rounded-lg bg-emerald-600 text-[13px] font-semibold text-white">
          压
        </div>
        <div className="leading-tight">
          <div className="text-[13px] font-semibold text-slate-800">调压站压力核查</div>
          <div className="text-[11px] text-slate-500">{rounds.length} 轮对话 · 右侧刻度跟随阅读位置</div>
        </div>

        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShort((value) => !value)}
            className={`rounded-md border px-2.5 py-1.5 text-[12px] transition ${
              short
                ? "border-amber-300 bg-amber-50 text-amber-700"
                : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
            }`}
          >
            {short ? "恢复 12 轮" : "只留 2 轮"}
          </button>
          <button
            type="button"
            onClick={() => setSidebar((value) => !value)}
            aria-expanded={sidebar}
            className={`rounded-md border px-2.5 py-1.5 text-[12px] transition ${
              sidebar
                ? "border-emerald-300 bg-emerald-50 text-emerald-700"
                : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
            }`}
          >
            右侧栏
          </button>
        </div>
      </header>

      <div className="relative flex min-h-0 flex-1">
        {/* 聊天面板：刻度导航挂在它的定位上下文里，贴着自己的右边缘。 */}
        <div className="relative min-w-0 flex-1">
          <div ref={scrollerRef} className="h-full overflow-y-auto px-6 py-5">
            <div className="mx-auto max-w-3xl space-y-4">
              {messages.map((message) =>
                message.role === "user" ? (
                  <div key={message.id} className="flex flex-col items-end gap-1 pt-2">
                    <span className="pr-1 text-[10px] font-medium text-slate-400">
                      第 {message.round + 1} 轮
                    </span>
                    {/* data-rail-item 是刻度导航认轮的锚：一个用户气泡 = 一条刻度。 */}
                    <div
                      data-rail-item
                      data-rail-id={message.id}
                      className="max-w-[85%] rounded-2xl rounded-tr-sm bg-emerald-600 px-4 py-2.5 text-[13px] leading-relaxed text-white shadow-sm"
                    >
                      {message.text}
                    </div>
                  </div>
                ) : (
                  <div key={message.id} className="flex gap-2.5">
                    <div className="mt-0.5 h-6 w-6 shrink-0 rounded-md bg-slate-200 text-center text-[11px] leading-6 text-slate-600">
                      助
                    </div>
                    <div className="max-w-[85%] whitespace-pre-line rounded-2xl rounded-tl-sm border border-slate-200 bg-white px-4 py-3 text-[13px] leading-[1.75] text-slate-700 shadow-sm">
                      {message.text}
                    </div>
                  </div>
                ),
              )}
              <div className="h-10" />
            </div>
          </div>

          {/* 侧栏参与布局：面板被挤窄，导航贴着自己的右边缘一起左移，
              不需要传任何偏移。若你的侧栏是浮层（盖在面板上），
              传 rightInset={抽屉宽度} 让它主动让开。 */}
          <ChatTickRail scrollerRef={scrollerRef} />
        </div>

        <aside
          className={`shrink-0 overflow-hidden border-l bg-white transition-[width] duration-300 ${
            sidebar ? "w-80 border-slate-200" : "w-0 border-transparent"
          }`}
        >
          <div className="h-full w-80">
            <Panel />
          </div>
        </aside>
      </div>
    </div>
  );
}

function Panel() {
  return (
    <div className="flex h-full flex-col gap-4 p-5">
      <div>
        <div className="text-[12px] font-semibold text-slate-800">本次排查</div>
        <div className="mt-1 text-[11px] leading-relaxed text-slate-500">
          侧栏展开时面板被挤窄，右侧刻度条随之左移——它属于聊天区，不跟着被挤没。
        </div>
      </div>

      <ul className="space-y-2 text-[12px] text-slate-600">
        {["3 号站零点核对（明天上午）", "取压管吹扫，作业票已提", "5 号站过冲观察（今晚 05:30）"].map(
          (item) => (
            <li key={item} className="flex gap-2 rounded-lg bg-slate-50 px-3 py-2">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
              <span>{item}</span>
            </li>
          ),
        )}
      </ul>

      <div className="mt-auto rounded-lg border border-dashed border-slate-200 p-3 text-[11px] leading-relaxed text-slate-400">
        若侧栏是浮层而非挤压布局，给导航传 rightInset={"{抽屉宽度}"} 即可让它主动让开。
      </div>
    </div>
  );
}
