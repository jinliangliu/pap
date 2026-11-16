# 长对话右侧刻度导航

> 已合入主线（2026-11-13）：控制台的实现是原生 JS 版，纯逻辑在
> `static/console/assets/rail.js`（自检 `scripts/check_chat_rail.mjs`），
> DOM 接线在 `console.js` 的「会话内刻度导航」一节。
> 控制台没有构建步骤，所以没有把 React 运行时带进主线；这里的 React 版留作参考实现。
> 与本文的一处差别：主线用 `scrollIntoLog` 改 scrollTop 而不是 `scrollIntoView`，
> 因为它会把整页一起滚走；高亮判定也改用 scroll 事件现读几何，不再依赖 IntersectionObserver。

React + Tailwind 实现：聊天区右侧悬浮一条虚线刻度轴，每条刻度 = 一轮用户提问。
滚动时高亮跟随，悬停出气泡（前 20 字摘要），点击平滑滚到该轮。
**不写锚点**——不碰 hash、不改 URL、不用 `:target`，当前轮次完全由 `IntersectionObserver` 驱动。

```bash
npm install
npm run dev     # http://127.0.0.1:5180
npm run check   # 纯逻辑断言（截断 / 几何 / 判定规则），不启浏览器
```

演示页右上角两个开关：`只留 2 轮`（验证 <3 轮隐藏）、`右侧栏`（验证让位）。

## 用法

```jsx
import ChatTickRail from "./ChatTickRail.jsx";

function Chat() {
  const scrollerRef = useRef(null);
  const [sideOpen, setSideOpen] = useState(false);

  return (
    <div className="relative flex min-h-0 flex-1">   {/* 需要定位上下文 */}
      <div className="relative min-w-0 flex-1">
        <div ref={scrollerRef} className="h-full overflow-y-auto">
          {messages.map((m) =>
            m.role === "user" ? (
              <div data-rail-item data-rail-id={m.id} key={m.id}>{m.text}</div>
            ) : (
              <div key={m.id}>{m.text}</div>
            ),
          )}
        </div>

        {/* 侧栏挤压布局时不需要任何偏移；浮层侧栏传 rightInset={抽屉宽度} */}
        <ChatTickRail scrollerRef={scrollerRef} rightInset={sideOpen && overlay ? 320 : 0} />
      </div>

      <aside className={`shrink-0 transition-[width] ${sideOpen ? "w-80" : "w-0 overflow-hidden"}`}>
        …
      </aside>
    </div>
  );
}
```

轮次从 DOM 里认，不用再传一份 `messages`：给用户气泡加 `data-rail-item`、
加一个稳定的 `data-rail-id` 就够了。消息追加、编辑、虚拟列表重渲染都会自动跟上
（内部用 `MutationObserver` 重扫）。气泡里除了正文还有时间戳之类的噪声时，
用 `data-rail-summary` 指定摘要来源。

| prop | 默认 | 说明 |
| --- | --- | --- |
| `scrollerRef` | 必填 | 滚动容器。判定用它做 IO 的 root。 |
| `rightInset` | `0` | 从容器右边缘再让开多少 px。**浮层侧栏**用它；参与布局的侧栏不用传。 |
| `minRounds` | `3` | 少于这么多轮直接不渲染。 |
| `summaryLimit` | `20` | 气泡摘要的字符数。 |
| `itemAttribute` | `data-rail-item` | 认轮次的属性名。 |

已内置的样式细节：跳转落点补偿 `scroll-margin-top: 12px`（`index.css`）、
`prefers-reduced-motion` 时改用 `behavior: "auto"`。

## 判据：阅读线在哪儿

高亮 = **已经越过阅读线、且离它最近的那一条**。

阅读线定在**容器顶边下方 16px**，不是「屏幕三分之一处」。这条看着无关紧要，
但它决定了「点第 6 轮会不会亮第 6 轮」：

- 线在顶边：`scrollIntoView` 把第 6 轮顶到容器顶部 → 它就在线上方最近处 → 亮 6。
- 线在 35%：消息顶到容器顶部后，35% 那条线已经落到第 7 轮上了 → 点 6 亮 7。

也就是说，点击与滚动必须共用同一条线，否则两套判定会给出互相矛盾的答案。

## 四个刻意的取舍

**刻度按内容比例摆，不是等距。** 这样刻度轴是一张地图：位置本身回答「我还剩多少」，
而不只是「第几轮」。代价是长短悬殊时会挤在一起，所以加了最小间距（先自上而下推开，
再从底部回推，顺序永不变）。轨道短到装不下时退化为等距——两条刻度叠在同一个 y 上，
点哪条都是碰运气，不如承认轨道太小。

**「小于 3 轮就隐藏」是按轮次算，不是按消息条数。** 三轮 = 六条消息，这时候
轴的密度还撑不起导航，反而挡住正文。

**摘要按字符截断。** `slice(0, 20)` 数的是 UTF-16 码元，会把 👍 劈成半个代理对渲染成 �；
用 `Array.from` 按码点切，20 个字符就是 20 个字符。

**悬停气泡走 CSS，不进 state。** `group-hover` / `group-focus-visible` 足够，
键盘用户聚焦刻度也能看到；塞进 React state 每次移动鼠标都会重渲染。

## 两个踩过的坑（都留了回归断言）

**① IntersectionObserver 只能当触发器，不能当位置来源。**
IO 只在「相交状态变化」时回调。把回调里的 `boundingClientRect` 存成位置表，
那些从头到尾都没进过判定带的轮次会一直保留旧坐标——实测一次跳转后
`u1–u4` 仍停在 +411/+651/+890，在判定里全被当成「在阅读线下方」，高亮再也挪不动。
现在 IO 只负责「有人跨线了」，真正的坐标在重算时现读。

**② 阅读线落在两条提问之间是常态，不是边界情况。**
按下发带相交来判定，线在一条长回答的正文中间时没有任何提问与带相交，
结果是一条都选不出来——表现为「滚到某处突然没有高亮」。
判定只看位置：越线最近者胜，一条都没越线时退到即将进入的那条。

## 验证

- `npm run check`：24 条纯逻辑断言，含 200 组随机数据的几何不变式（有界 / 保序 / 够间距）。
- CDP 交互探针（真实鼠标事件，不是构造 state）跑了 22 条：初始高亮、滚动跟随
  （与独立计算比对）、悬停气泡可见性与 20 字截断、点击落点与高亮、末条滚到底、
  侧栏展开左移 320 且不被遮挡、<3 轮整体消失、恢复后重新出现、无 hash 变更、无控制台告警。
