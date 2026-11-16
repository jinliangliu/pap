// 会话内刻度导航（static/console/assets/rail.js）的自检脚本。
//
// 用法：node scripts/check_chat_rail.mjs
// 全部断言通过静默退出 0，任何一条失败打印差异并退出 1。
//
// 只测不碰 DOM 的那部分，但它恰好是最容易错、又最难从截图上发现的部分：
// 截断把 emoji 劈成半个、刻度顺序反了、端点跑到轨道外、
// 阅读线落在两条提问之间时一条都选不出来。
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = fs.readFileSync(
  path.join(root, "static/console/assets/rail.js"),
  "utf8"
);

const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "rail.js" });
const R = sandbox.window.PAPRail;
if (!R || typeof R.layoutTicks !== "function") {
  console.error("rail.js 未暴露 window.PAPRail");
  process.exit(1);
}

const { summarize, layoutTicks, pickActive, MIN_GAP } = R;
let failures = 0;

function check(name, condition, detail) {
  if (condition) return;
  failures += 1;
  console.error(`FAIL ${name}${detail ? "\n  " + detail : ""}`);
}

/* ---- 截断 ---- */
const twenty = "一二三四五六七八九十一二三四五六七八九十";
check("20 字不截断", summarize(twenty) === twenty);
check("21 字截成 20 + 省略号", summarize(twenty + "二") === twenty + "…", summarize(twenty + "二"));
check("压平多余空白", summarize("  你好\n\n 世界  ") === "你好 世界", summarize("  你好\n\n 世界  "));
check("空内容给空串", summarize("   \n ") === "");
check("null 不炸", summarize(null) === "");

// UTF-16 陷阱：slice 按码元切，会把代理对劈成半个。
const emoji = "👍".repeat(25);
const emojiOut = summarize(emoji);
check("emoji 按字符截断", Array.from(emojiOut).length === 21, Array.from(emojiOut).length);
check("emoji 没有被劈开", !emojiOut.includes("\uFFFD") && emojiOut.endsWith("…"));

/* ---- 布局 ---- */
const ys12 = layoutTicks(
  [0, 320, 700, 980, 1500, 1900, 2600, 3000, 3400, 3900, 4400, 4900],
  5200,
  560
);
check("12 条都排上了", ys12.length === 12);
check("顺序单调", ys12.every((y, i) => i === 0 || y > ys12[i - 1]), ys12.join(","));
check("不越界", ys12.every((y) => y >= 0 && y <= 560), ys12.join(","));
check(
  "间距不小于 MIN_GAP",
  ys12.every((y, i) => i === 0 || y - ys12[i - 1] >= MIN_GAP - 1e-9),
  ys12.join(",")
);

// 内容极短但消息很多：按比例算会全挤在顶部，最小间距要能把它们推开。
const crowded = layoutTicks([0, 1, 2, 3, 4, 5], 6000, 560);
check(
  "挤在一起时被推开",
  crowded.every((y, i) => i === 0 || y - crowded[i - 1] >= MIN_GAP - 1e-9),
  crowded.join(",")
);
check("推开后仍在轨道内", crowded[crowded.length - 1] <= 560, crowded[crowded.length - 1]);

// 轨道太短：退化为等距，但必须仍然保序、保界。
const tiny = layoutTicks([0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100], 1200, 40);
check("轨道过短时退化为等距", tiny.length === 12 && tiny[0] === 0 && Math.abs(tiny[11] - 40) < 1e-9);
check("退化后仍然严格递增", tiny.every((y, i) => i === 0 || y > tiny[i - 1]), tiny.join(","));

check("空输入返回空数组", layoutTicks([], 1000, 500).length === 0);
check("轨道高为 0 不产出坐标", layoutTicks([0, 10, 20], 1000, 0).length === 0);

/* ---- 随机不变式 ---- */
let seed = 20261113;
const rand = () => {
  seed = (seed * 1103515245 + 12345) % 2147483648;
  return seed / 2147483648;
};
let invariantOk = true;
let invariantCase = "";
for (let round = 0; round < 200; round += 1) {
  const count = 1 + Math.floor(rand() * 40);
  const contentHeight = 200 + Math.floor(rand() * 20000);
  const railHeight = 20 + Math.floor(rand() * 800);
  const offsets = Array.from(
    { length: count },
    () => Math.floor(rand() * contentHeight * (0.6 + rand() * 0.5))
  ).sort((a, b) => a - b);
  const out = layoutTicks(offsets, contentHeight, railHeight);
  const inRange = out.every((y) => y >= 0 && y <= railHeight);
  const ordered = out.every((y, i) => i === 0 || y >= out[i - 1]);
  const roomy = railHeight >= (count - 1) * MIN_GAP;
  const spaced = !roomy || out.every((y, i) => i === 0 || y - out[i - 1] >= MIN_GAP - 1e-9);
  if (!inRange || !ordered || !spaced) {
    invariantOk = false;
    invariantCase = `count=${count} content=${contentHeight} rail=${railHeight} out=${out.join(",")}`;
    break;
  }
}
check("200 组随机数据：有界、保序、够间距", invariantOk, invariantCase);

/* ---- 当前轮判定 ---- */
const band = 400;
check(
  "越过阅读线的最近一条胜出",
  pickActive(
    [
      { id: "u0", top: 120 },
      { id: "u1", top: 380 },
      { id: "u2", top: 760 },
    ],
    band
  ) === "u1"
);
check(
  "长消息不会因为占比小而输",
  pickActive(
    [
      { id: "long", top: 60 },
      { id: "short", top: 420 },
    ],
    band
  ) === "long"
);
check(
  "一条都没越过线时取即将进入的那条",
  pickActive(
    [
      { id: "u0", top: 500 },
      { id: "u1", top: 800 },
    ],
    band
  ) === "u0"
);
// 回归：阅读线落在两条提问之间（某条长回答的正文里）时，谁都不与线相交，
// 但正确答案是「上面那一轮」，不是「没有」。
check(
  "阅读线落在两条提问之间时仍能判定",
  pickActive(
    [
      { id: "u0", top: -420 },
      { id: "u1", top: 2600 },
    ],
    band
  ) === "u0"
);
check("没有位置的条目不参与", pickActive([{ id: "broken", top: undefined }, { id: "u1", top: 900 }], band) === "u1");
check("空列表返回 null", pickActive([], band) === null);

if (failures) {
  console.error(`\n${failures} 条断言未通过`);
  process.exit(1);
}
