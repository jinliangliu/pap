/* Personal Agent Platform · 会话内容渲染
 *
 * 模型回答里允许三类「表达块」，本文件负责把它们变成安全的 HTML：
 *   1) Markdown 子集：标题 / 列表 / 引用 / 表格 / 行内强调与代码；
 *   2) fenced ```chart 里的 JSON → 内联 SVG 折线（平滑曲线）或柱状图；
 *   3) fenced ```svg 里的原始图形 → 白名单净化后内联，嵌在气泡里。
 *
 * 为什么自己写而没有引第三方库：
 *   - 控制台没有构建步骤，也不允许从外网 CDN 取脚本（内网部署直接白屏）；
 *   - 这里渲染的是模型输出，任何拼接前都必须自己净化一遍，
 *     现成库的默认配置未必挡住 <foreignObject>、on* 属性这些口子。
 *
 * 所有对外函数都不抛异常：渲染失败就退回代码块，聊天不能因为一张图挂掉。
 */
(function (global) {
  "use strict";

  /* ---------------- 基础 ---------------- */

  var ENT = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return ENT[ch];
    });
  }

  function isBlank(line) {
    return !String(line || "").trim();
  }

  /* ---------------- 行内 ---------------- */

  /* 先转义再套格式：顺序反过来的话，模型写 <script> 会被当成标签拼进 DOM。 */
  function inline(raw) {
    var s = esc(raw);
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(
      /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );
    return s;
  }

  /* ---------------- 表格 ---------------- */

  function splitRow(line) {
    var body = String(line || "").trim();
    if (body.charAt(0) === "|") body = body.slice(1);
    if (body.charAt(body.length - 1) === "|") body = body.slice(0, -1);
    return body.split("|").map(function (cell) {
      return cell.trim();
    });
  }

  /* |---|:--:| 这种分隔行：既是表格的判定依据，也决定列对齐。 */
  function isSeparator(line) {
    return /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(String(line || ""));
  }

  function looksNumeric(value) {
    var text = String(value || "").replace(/[,\s%]/g, "");
    if (!text) return false;
    return /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(text);
  }

  function columnAligns(header, rows, sep) {
    var cells = splitRow(sep);
    return header.map(function (_, index) {
      var spec = cells[index] || "";
      if (/^:-+:$/.test(spec)) return "center";
      if (/^-+:$/.test(spec)) return "right";
      // 分隔行没写明时按内容判断：数值列右对齐，读起来才对得上小数点。
      var values = rows.map(function (row) {
        return row[index];
      }).filter(function (v) {
        return String(v || "").trim() !== "";
      });
      var numeric = values.length > 0 && values.every(looksNumeric);
      return numeric ? "right" : "left";
    });
  }

  function renderTable(header, rows, sep) {
    var aligns = columnAligns(header, rows, sep);
    var head = header.map(function (cell, index) {
      return '<th style="text-align:' + aligns[index] + '">' + inline(cell) + "</th>";
    }).join("");
    var body = rows.map(function (row) {
      var cells = [];
      for (var i = 0; i < header.length; i += 1) {
        cells.push(
          '<td style="text-align:' + aligns[i] + '">' + inline(row[i] || "") + "</td>"
        );
      }
      return "<tr>" + cells.join("") + "</tr>";
    }).join("");

    return '<div class="table-wrap"><table class="md-table"><thead><tr>' + head +
      "</tr></thead><tbody>" + body + "</tbody></table></div>";
  }

  /* ---------------- 图表 ---------------- */

  var CHART_W = 660;
  var CHART_H = 244;
  var CHART_PAD = { l: 56, r: 16, t: 14, b: 36 };
  var PALETTE = ["#0b6b4b", "#96631a", "#3f6fa8", "#8a3f63", "#4b7a6f", "#7a6320"];
  var chartSeq = 0;

  function num(value) {
    var n = Number(value);
    if (!isFinite(n)) return null;
    return n;
  }

  /* 刻度取整：只用 1 / 2 / 5 的整数倍。2.5 会在小数轴上造出 2.275
     这种刻度值，显示时四舍五入还会和真实位置对不上。 */
  function niceStep(span, count) {
    var rough = span / Math.max(1, count);
    if (!isFinite(rough) || rough <= 0) return 1;
    var mag = Math.pow(10, Math.floor(Math.log(rough) / Math.LN10));
    var norm = rough / mag;
    var mult = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
    return mult * mag;
  }

  function fmtNum(value) {
    var abs = Math.abs(value);
    var digits = abs >= 1000 ? 0 : abs >= 100 ? 1 : abs >= 10 ? 1 : 2;
    var text = value.toFixed(digits);
    if (text.indexOf(".") >= 0) text = text.replace(/\.?0+$/, "");
    return text;
  }

  function round2(value) {
    return Math.round(value * 100) / 100;
  }

  /* 平滑曲线：Catmull-Rom 转三次贝塞尔。直线插值在运维趋势图上看着太硬。 */
  function smoothPath(points) {
    if (points.length < 2) return "";
    var d = "M" + round2(points[0][0]) + "," + round2(points[0][1]);
    for (var i = 0; i < points.length - 1; i += 1) {
      var p0 = points[i - 1] || points[i];
      var p1 = points[i];
      var p2 = points[i + 1];
      var p3 = points[i + 2] || p2;
      var c1x = p1[0] + (p2[0] - p0[0]) / 6;
      var c1y = p1[1] + (p2[1] - p0[1]) / 6;
      var c2x = p2[0] - (p3[0] - p1[0]) / 6;
      var c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += " C" + round2(c1x) + "," + round2(c1y) + " " + round2(c2x) + "," +
        round2(c2y) + " " + round2(p2[0]) + "," + round2(p2[1]);
    }
    return d;
  }

  function normalizeSeries(spec) {
    var raw = spec.series;
    if (!raw && spec.data) raw = [{ name: spec.name || "", data: spec.data }];
    if (!Array.isArray(raw)) return [];
    return raw.map(function (item, index) {
      var points = Array.isArray(item) ? item : item && item.data;
      return {
        name: (!Array.isArray(item) && item && item.name) || "数值" + (index + 1),
        data: (points || []).map(num),
        color: (!Array.isArray(item) && item && item.color) || PALETTE[index % PALETTE.length]
      };
    }).filter(function (item) {
      return item.data.length > 0;
    });
  }

  function chartCard(spec, forcedType) {
    var type = String(spec.type || forcedType || "line").toLowerCase();
    if (type !== "bar") type = "line";

    var series = normalizeSeries(spec);
    var all = [];
    series.forEach(function (item) {
      item.data.forEach(function (value) {
        if (value !== null) all.push(value);
      });
    });
    if (!series.length || !all.length) {
      return '<div class="rich-note">图表没有可用数据（series.data 全为空）。</div>';
    }

    var xs = Array.isArray(spec.x) ? spec.x : Array.isArray(spec.labels) ? spec.labels : [];
    var count = series.reduce(function (max, item) {
      return Math.max(max, item.data.length);
    }, xs.length);

    var lo = Math.min.apply(null, all);
    var hi = Math.max.apply(null, all);
    if (lo === hi) {
      lo -= Math.abs(lo) * 0.1 + 1;
      hi += Math.abs(hi) * 0.1 + 1;
    }
    var spread = hi - lo;
    lo -= spread * 0.12;
    hi += spread * 0.12;
    // 柱状图从 0 起算，否则柱子的高度差会被放大成误导。
    if (type === "bar" && Math.min.apply(null, all) >= 0) lo = 0;

    var step = niceStep(hi - lo, 4);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    if (hi === lo) hi = lo + step;

    var plotLeft = CHART_PAD.l;
    var plotRight = CHART_W - CHART_PAD.r;
    var plotTop = CHART_PAD.t;
    var plotBottom = CHART_H - CHART_PAD.b;
    var plotH = plotBottom - plotTop;

    function yOf(value) {
      return plotTop + (1 - (value - lo) / (hi - lo)) * plotH;
    }

    var grid = "";
    var axis = "";
    for (var tick = lo; tick <= hi + step * 0.001; tick += step) {
      var y = round2(yOf(tick));
      grid += '<line x1="' + plotLeft + '" y1="' + y + '" x2="' + plotRight +
        '" y2="' + y + '" stroke="#eef1ef" stroke-width="1"/>';
      axis += '<text x="' + (plotLeft - 9) + '" y="' + round2(y + 3.5) +
        '" text-anchor="end" font-size="10.5" fill="#98a09a">' + esc(fmtNum(tick)) + "</text>";
    }

    var uid = "papchart" + (chartSeq += 1);
    var body = "";
    var labels = "";

    if (type === "bar") {
      var slot = (plotRight - plotLeft) / Math.max(1, count);
      var barW = Math.max(6, Math.min(30, (slot * 0.62) / series.length));
      var showValue = count * series.length <= 12;
      series.forEach(function (item, sIdx) {
        item.data.forEach(function (value, i) {
          if (value === null) return;
          var center = plotLeft + slot * (i + 0.5);
          var x = center - (barW * series.length) / 2 + barW * sIdx;
          var top = round2(yOf(value));
          var h = Math.max(1, round2(plotBottom - top));
          body += '<rect x="' + round2(x) + '" y="' + top + '" width="' + round2(barW) +
            '" height="' + h + '" rx="2" fill="' + item.color + '"><title>' +
            esc(labelOf(xs, i) + " · " + item.name + " " + fmtNum(value) + unitOf(spec)) +
            "</title></rect>";
          if (showValue) {
            body += '<text x="' + round2(center) + '" y="' + round2(top - 5) +
              '" text-anchor="middle" font-size="10" fill="#6c736e">' +
              esc(fmtNum(value)) + "</text>";
          }
        });
      });
    } else {
      var gap = count > 1 ? (plotRight - plotLeft) / (count - 1) : 0;
      series.forEach(function (item, sIdx) {
        var points = [];
        item.data.forEach(function (value, i) {
          if (value === null) return;
          var x = count > 1 ? plotLeft + gap * i : (plotLeft + plotRight) / 2;
          points.push([x, yOf(value)]);
        });
        if (!points.length) return;
        var d = smoothPath(points);
        if (series.length === 1) {
          var area = d + " L" + round2(points[points.length - 1][0]) + "," + plotBottom +
            " L" + round2(points[0][0]) + "," + plotBottom + " Z";
          body += '<defs><linearGradient id="' + uid + '" x1="0" y1="0" x2="0" y2="1">' +
            '<stop offset="0" stop-color="' + item.color + '" stop-opacity="0.14"/>' +
            '<stop offset="1" stop-color="' + item.color + '" stop-opacity="0"/>' +
            "</linearGradient></defs>" +
            '<path d="' + area + '" fill="url(#' + uid + ')"/>';
        }
        body += '<path d="' + d + '" fill="none" stroke="' + item.color +
          '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>';
        if (points.length <= 24) {
          points.forEach(function (point, i) {
            var value = item.data[i];
            body += '<circle cx="' + round2(point[0]) + '" cy="' + round2(point[1]) +
              '" r="2.8" fill="#fff" stroke="' + item.color + '" stroke-width="1.8"><title>' +
              esc(labelOf(xs, i) + " · " + item.name + " " + fmtNum(value) + unitOf(spec)) +
              "</title></circle>";
          });
        }
      });
    }

    var skip = Math.ceil(count / 9);
    for (var i = 0; i < count; i += 1) {
      if (i % skip !== 0 && i !== count - 1) continue;
      var cx = type === "bar"
        ? plotLeft + ((plotRight - plotLeft) / Math.max(1, count)) * (i + 0.5)
        : count > 1 ? plotLeft + ((plotRight - plotLeft) / (count - 1)) * i : (plotLeft + plotRight) / 2;
      labels += '<text x="' + round2(cx) + '" y="' + (plotBottom + 18) +
        '" text-anchor="middle" font-size="10.5" fill="#98a09a">' +
        esc(labelOf(xs, i)) + "</text>";
    }

    var legend = series.length > 1
      ? '<div class="chart-legend">' + series.map(function (item) {
        return '<span class="lg"><i style="background:' + item.color + '"></i>' +
          esc(item.name) + "</span>";
      }).join("") + "</div>"
      : "";

    var unit = unitOf(spec);
    return '<figure class="chart-card">' +
      '<div class="chart-head"><span class="chart-title">' + esc(spec.title || "数据图表") +
      "</span>" + (unit ? '<span class="chart-unit">单位 ' + esc(unit) + "</span>" : "") +
      "</div>" +
      '<svg viewBox="0 0 ' + CHART_W + " " + CHART_H + '" role="img" aria-label="' +
      esc(spec.title || "数据图表") + '">' + grid + body + axis + labels + "</svg>" +
      legend + "</figure>";
  }

  function labelOf(xs, index) {
    if (xs && index < xs.length && String(xs[index]).trim() !== "") return String(xs[index]);
    return String(index + 1);
  }

  function unitOf(spec) {
    return String(spec.unit || "").trim();
  }

  /* ---------------- SVG 净化 ---------------- */

  var SVG_TAGS = {
    svg: "svg", g: "g", path: "path", rect: "rect", circle: "circle", ellipse: "ellipse",
    line: "line", polyline: "polyline", polygon: "polygon", text: "text", tspan: "tspan",
    defs: "defs", lineargradient: "linearGradient", radialgradient: "radialGradient",
    stop: "stop", marker: "marker", title: "title", desc: "desc", clippath: "clipPath"
  };

  // 连内容一起丢掉的标签：留着 innerHTML 会把脚本正文当文字显示出来。
  var DROP_WITH_CONTENT = { script: 1, style: 1, foreignobject: 1, iframe: 1, image: 1 };

  var SVG_ATTRS = {
    d: 1, x: 1, y: 1, x1: 1, x2: 1, y1: 1, y2: 1, cx: 1, cy: 1, r: 1, rx: 1, ry: 1,
    width: 1, height: 1, points: 1, viewbox: 1, preserveaspectratio: 1, transform: 1,
    fill: 1, "fill-opacity": 1, "fill-rule": 1, stroke: 1, "stroke-width": 1,
    "stroke-linecap": 1, "stroke-linejoin": 1, "stroke-dasharray": 1, "stroke-opacity": 1,
    opacity: 1, "font-size": 1, "font-family": 1, "font-weight": 1, "letter-spacing": 1,
    "text-anchor": 1, "dominant-baseline": 1, dx: 1, dy: 1, id: 1, offset: 1,
    "stop-color": 1, "stop-opacity": 1, "marker-end": 1, "marker-start": 1,
    "marker-mid": 1, "markerwidth": 1, "markerheight": 1, "markerunits": 1, refx: 1,
    refy: 1, orient: 1, "clip-path": 1, "clip-rule": 1, "paint-order": 1,
    "vector-effect": 1
  };

  function findTagEnd(src, from) {
    var quote = "";
    for (var i = from + 1; i < src.length; i += 1) {
      var ch = src.charAt(i);
      if (quote) {
        if (ch === quote) quote = "";
        continue;
      }
      if (ch === '"' || ch === "'") {
        quote = ch;
        continue;
      }
      if (ch === ">") return i;
    }
    return -1;
  }

  function attrValueUnsafe(value) {
    var lower = String(value).toLowerCase().replace(/[\s\u0000-\u001f]/g, "");
    if (lower.indexOf("javascript:") >= 0) return true;
    if (lower.indexOf("expression(") >= 0) return true;
    if (lower.indexOf("data:text/html") >= 0) return true;
    // url(#id) 是渐变/裁剪的正常用法，url(http...) 才是外链。
    if (lower.indexOf("url(") >= 0 && lower.indexOf("url(#") < 0) return true;
    return false;
  }

  function parseAttrs(raw) {
    var out = "";
    var re = /([A-Za-z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
    var match;
    while ((match = re.exec(raw)) !== null) {
      var name = match[1].toLowerCase();
      var value = match[2] !== undefined ? match[2] : match[3];
      if (/^on/.test(name)) continue;
      if (!SVG_ATTRS[name]) continue;
      if (attrValueUnsafe(value)) continue;
      out += " " + match[1] + '="' + esc(value) + '"';
    }
    return out;
  }

  /* 逐字符扫描而不是用 DOMParser：DOMParser 在非浏览器环境不存在，
     而这段逻辑要能在 node 里跑测试（scripts/check_render.mjs）。 */
  function sanitizeSvg(src) {
    var text = String(src == null ? "" : src);
    if (!text.trim()) throw new Error("empty svg");

    var out = [];
    var i = 0;
    var opened = 0;
    var sawSvg = /<\s*svg[\s>]/i.test(text);

    if (!sawSvg) out.push('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 680 240">');

    while (i < text.length) {
      var lt = text.indexOf("<", i);
      if (lt < 0) {
        out.push(esc(text.slice(i)));
        break;
      }
      if (lt > i) out.push(esc(text.slice(i, lt)));

      if (text.substr(lt, 4) === "<!--") {
        var commentEnd = text.indexOf("-->", lt + 4);
        if (commentEnd < 0) throw new Error("unterminated comment");
        i = commentEnd + 3;
        continue;
      }
      if (text.substr(lt, 9) === "<![CDATA[") {
        var cdataEnd = text.indexOf("]]>", lt + 9);
        if (cdataEnd < 0) throw new Error("unterminated cdata");
        out.push(esc(text.slice(lt + 9, cdataEnd)));
        i = cdataEnd + 3;
        continue;
      }
      if (text.charAt(lt + 1) === "?" || text.charAt(lt + 1) === "!") {
        var declEnd = text.indexOf(">", lt);
        if (declEnd < 0) throw new Error("unterminated declaration");
        i = declEnd + 1;
        continue;
      }

      var gt = findTagEnd(text, lt);
      if (gt < 0) throw new Error("unterminated tag");
      var raw = text.slice(lt + 1, gt);
      i = gt + 1;

      if (raw.charAt(0) === "/") {
        var closeName = raw.slice(1).trim().toLowerCase();
        if (SVG_TAGS[closeName]) {
          out.push("</" + SVG_TAGS[closeName] + ">");
          opened -= 1;
        }
        continue;
      }

      var selfClosing = /\/\s*$/.test(raw);
      var body = selfClosing ? raw.replace(/\/\s*$/, "") : raw;
      var nameMatch = body.match(/^([A-Za-z][\w:.-]*)/);
      if (!nameMatch) continue;
      var name = nameMatch[1];
      var lower = name.toLowerCase();

      if (DROP_WITH_CONTENT[lower]) {
        // 跳到配对的结束标签，连同中间的内容一起丢掉
        var closeRe = new RegExp("</\\s*" + lower + "\\s*>", "i");
        var rest = text.slice(i);
        var found = closeRe.exec(rest);
        if (found) i += found.index + found[0].length;
        continue;
      }

      if (!SVG_TAGS[lower]) {
        // 不认识的标签：丢掉标签本身，保留子节点（比如自定义包裹层）
        continue;
      }

      out.push("<" + SVG_TAGS[lower] + parseAttrs(body.slice(nameMatch[1].length)) +
        (selfClosing ? "/>" : ">"));
      if (!selfClosing) opened += 1;
    }

    if (!sawSvg) out.push("</svg>");
    if (!out.length || (opened > 0 && !sawSvg)) throw new Error("bad svg");
    return out.join("");
  }

  /* ---------------- 块解析 ---------------- */

  function renderFence(lang, body) {
    var key = String(lang || "").toLowerCase();

    if (key === "chart" || key.indexOf("chart:") === 0) {
      var spec;
      try {
        spec = JSON.parse(body);
      } catch (err) {
        return '<div class="rich-note err">图表数据不是合法 JSON，已按源码显示。</div>' +
          '<pre class="rich-code"><code>' + esc(body) + "</code></pre>";
      }
      try {
        return chartCard(spec || {}, key.indexOf("chart:") === 0 ? key.slice(6) : "");
      } catch (err) {
        return '<div class="rich-note err">图表渲染失败，已按源码显示。</div>' +
          '<pre class="rich-code"><code>' + esc(body) + "</code></pre>";
      }
    }

    if (key === "svg") {
      try {
        return '<figure class="svg-figure">' + sanitizeSvg(body) + "</figure>";
      } catch (err) {
        return '<div class="rich-note err">图形无法解析，已按源码显示。</div>' +
          '<pre class="rich-code"><code>' + esc(body) + "</code></pre>";
      }
    }

    if (key === "mermaid") {
      return '<div class="rich-note">暂不支持 mermaid 语法，流程图请用 ```svg 输出。</div>' +
        '<pre class="rich-code"><code>' + esc(body) + "</code></pre>";
    }

    var label = key ? '<span class="code-lang">' + esc(key) + "</span>" : "";
    return '<div class="code-block">' + label + '<pre class="rich-code"><code>' +
      esc(body) + "</code></pre></div>";
  }

  function startsBlock(lines, index) {
    var line = lines[index] || "";
    if (/^\s*```/.test(line)) return true;
    if (/^#{1,6}\s+/.test(line)) return true;
    if (/^\s*>\s?/.test(line)) return true;
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) return true;
    if (/^\s*([-*_]\s*){3,}$/.test(line)) return true;
    if (line.indexOf("|") >= 0 && isSeparator(lines[index + 1])) return true;
    return false;
  }

  function rich(input) {
    var text = String(input == null ? "" : input).replace(/\r\n?/g, "\n");
    if (!text.trim()) return "";

    var lines = text.split("\n");
    var out = [];
    var i = 0;

    while (i < lines.length) {
      var line = lines[i];

      if (isBlank(line)) {
        i += 1;
        continue;
      }

      var fence = line.match(/^\s*```\s*([A-Za-z0-9_+:-]*)\s*$/);
      if (fence) {
        var body = [];
        i += 1;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        i += 1;
        out.push(renderFence(fence[1], body.join("\n")));
        continue;
      }

      if (/^\s*([-*_]\s*){3,}$/.test(line)) {
        out.push("<hr>");
        i += 1;
        continue;
      }

      var heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        var level = Math.min(3, heading[1].length + 2);
        out.push("<h" + level + ' class="rich-h">' + inline(heading[2]) + "</h" + level + ">");
        i += 1;
        continue;
      }

      if (line.indexOf("|") >= 0 && isSeparator(lines[i + 1])) {
        var header = splitRow(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].indexOf("|") >= 0 && !isBlank(lines[i])) {
          rows.push(splitRow(lines[i]));
          i += 1;
        }
        out.push(renderTable(header, rows, lines[i - rows.length - 1]));
        continue;
      }

      var item = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
      if (item) {
        var ordered = /^\d/.test(item[1]);
        var items = [];
        while (i < lines.length) {
          var match = lines[i].match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
          if (!match) {
            // 缩进续行并入上一项，列表里换行不该断成两段
            if (/^\s{2,}\S/.test(lines[i]) && items.length) {
              items[items.length - 1] += " " + lines[i].trim();
              i += 1;
              continue;
            }
            break;
          }
          // 有序和无序是两张列表，混在一串里渲染会丢掉编号语义
          if (/^\d/.test(match[1]) !== ordered) break;
          items.push(match[2]);
          i += 1;
        }
        var tagName = ordered ? "ol" : "ul";
        out.push("<" + tagName + ">" + items.map(function (entry) {
          return "<li>" + inline(entry) + "</li>";
        }).join("") + "</" + tagName + ">");
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        var quote = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*>\s?/, ""));
          i += 1;
        }
        out.push("<blockquote>" + quote.map(inline).join("\n") + "</blockquote>");
        continue;
      }

      var buffer = [line];
      i += 1;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines, i)) {
        buffer.push(lines[i]);
        i += 1;
      }
      out.push("<p>" + buffer.map(inline).join("\n") + "</p>");
    }

    return out.join("");
  }

  /* 审计条目的结构化细节（拒绝原因、生效上限、转交来源、改前改后）。
   *
   * 折在 <details> 里而不是铺成一列：多数行读一句说明就够，需要追问「为什么」
   * 的时候再展开。用原生 details，不写脚本、不加状态。
   *
   * 放这里而不是 console.js 的理由和其他纯逻辑一样：内容由租户产生，
   * 拼进 HTML 之前必须净化，而这段能被 node 直接断言（scripts/check_render.mjs）。
   */
  function auditMeta(meta) {
    if (!meta || typeof meta !== "object" || Array.isArray(meta)) return "";
    if (!Object.keys(meta).length) return "";

    var text;
    try {
      text = JSON.stringify(meta, null, 2);
    } catch (e) {
      // 循环引用之类的坏数据不该让整张表崩掉：宁可显示一行提示。
      return '<details class="audit-meta"><summary>详情</summary><pre>（内容无法序列化）</pre></details>';
    }
    if (text.length > 4000) text = text.slice(0, 4000) + "\n…（过长已截断）";

    return '<details class="audit-meta"><summary>详情</summary><pre>' + esc(text) + "</pre></details>";
  }

  /* ---------------- 审批卡：把「一坨 JSON」读成一张单子 ----------------

     审批卡原来把 `parameters` 整个丢进 <pre>：键是英文、换行被 JSON.stringify
     转义成字面的 \n、几段文字连成一片。而这一屏恰恰是整套系统里唯一「点错了
     就有副作用」的地方——读不懂就等于没有审批，用户只能凭标题点确认。

     这里做三件事，顺序也是降级顺序：
       1) 认出字段：工单参数的键是固定的，映射成中文标签；
       2) 认出结构：多段文本按空行切块，块首的「xxx：」提成小标题；
       3) 认出流程：底部画一条流程条，标出这张单子走到哪一步、下一步是什么。
     认不出来就退回「键 → 值」逐条罗列，再认不出来退回原文 JSON（由调用方
     折在 <details> 里）。宁可朴素，不可丢失：卡上显示的参数必须与执行时提交的
     是同一份，把读不懂的字段吞掉比显示得难看危险得多。
   */

  // 只映射确实认识的键。其余一律原样显示——编一个中文标签，读者会以为
  // 那就是工具文档里的定义。
  var PARAM_LABEL = {
    station_id: "场站", station: "场站", site: "场站",
    title: "标题", subject: "标题", summary: "摘要",
    description: "说明", detail: "说明", details: "说明", reason: "理由",
    ticket_id: "工单号", external_id: "外部编号",
    severity: "等级", level: "等级", priority: "优先级",
    assignee: "责任人", owner: "责任人", due_at: "时限", deadline: "时限",
    target: "对象", agent: "智能体", name: "名称"
  };

  function paramLabel(key) {
    return PARAM_LABEL[String(key || "")] || String(key || "");
  }

  function safeJson(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch (err) {
      return "（内容无法序列化）";
    }
  }

  /* 标题里的【较大】不是标题的一部分，是等级。留在标题里会让人读成
     「这个工单叫较大」；提出来单独成 badge，扫一眼就知道该多急。 */
  function titleParts(title) {
    var raw = String(title == null ? "" : title).trim();
    var matched = raw.match(/^[【\[]+\s*([^】\]]{1,10}?)\s*[】\]]\s*([\s\S]*)$/);
    if (!matched) return { badge: "", text: raw };
    return { badge: matched[1], text: matched[2].trim() };
  }

  /* 中文分级没有统一词表：隐患爱用「一般 / 较大 / 重大」，工单爱用 P0–P3。
     认不出就返回空——给一个猜出来的颜色，比不给颜色更糟。 */
  function severityTone(text) {
    var value = String(text || "");
    if (/重大|特别|P0|一级|I级/i.test(value)) return "err";
    if (/较大|严重|P1|P2|二级|II级/i.test(value)) return "warn";
    if (/一般|轻微|低|P3|三级|III级/i.test(value)) return "ok";
    return "";
  }

  /* 多段文本 → 块。模型写的工单描述几乎都是「现场情况：…\n\n等级与理由：…」
     这种形状；不切块的话，整段会挤成一个看不出边界的文字墙。

     字面的 \n 也要还原：模型偶尔会把换行写成反斜杠 n（转义了两次），
     那时 JSON.parse 出来的就是一个真正的反斜杠加 n。 */
  function textBlocks(raw) {
    var text = String(raw == null ? "" : raw)
      .replace(/\\r\\n|\\n|\\r/g, "\n")
      .replace(/\r\n?/g, "\n")
      .trim();
    if (!text) return [];

    return text.split(/\n{2,}/).map(function (chunk) {
      // 标签只认「行首、短、第一个冒号之前」：正文里带冒号的句子很常见，
      // 放开长度限制会把整句话提成标题。
      var matched = chunk.match(/^([^\n：:]{2,14})[：:][ \t]*/);
      if (!matched) return { label: "", text: chunk.trim() };
      return { label: matched[1].trim(), text: chunk.slice(matched[0].length).trim() };
    }).filter(function (block) {
      return block.label || block.text;
    });
  }

  function looksLikeTicket(params) {
    if (!params || typeof params !== "object" || Array.isArray(params)) return false;
    return !!params.title && !!(params.description || params.station_id);
  }

  /* 一个值怎么显示。长文本走块（保留换行），短值走行内，
     结构化值仍给 JSON——数组和对象是「原样是什么」比「读起来顺」更要紧。 */
  function paramValue(value) {
    if (value === null || value === undefined || value === "") {
      return '<span class="tk-empty">（空）</span>';
    }
    if (typeof value === "boolean") return esc(value ? "是" : "否");
    if (typeof value === "number") return '<span class="mono">' + esc(String(value)) + "</span>";
    if (typeof value === "string") {
      if (value.length > 80 || value.indexOf("\n") >= 0) {
        return '<p class="tk-text">' + esc(value.replace(/\\n/g, "\n")) + "</p>";
      }
      return esc(value);
    }
    if (Array.isArray(value)) {
      if (!value.length) return '<span class="tk-empty">（空）</span>';
      return value.map(function (item) {
        var text = item && typeof item === "object" ? safeJson(item) : String(item);
        return '<span class="tk-tag">' + esc(text) + "</span>";
      }).join("");
    }
    return '<pre class="tk-json">' + esc(safeJson(value)) + "</pre>";
  }

  function paramRow(key, value) {
    return '<div class="tk-row"><span class="tk-k">' + esc(paramLabel(key)) + "</span>" +
      '<div class="tk-v">' + paramValue(value) + "</div></div>";
  }

  // 工单卡里已经单独呈现的键，不再重复列一遍。
  var TICKET_CORE = { title: 1, description: 1, detail: 1, details: 1, station_id: 1, station: 1, site: 1 };

  function ticketCard(params) {
    var parts = titleParts(params.title);
    var explicit = params.severity || params.level || params.priority;
    var badge = parts.badge || (typeof explicit === "string" ? explicit : "");
    var tone = severityTone(badge);
    var station = params.station_id || params.station || params.site || "";
    var description = params.description || params.detail || params.details || "";

    var meta = [];
    if (station) {
      meta.push('<span class="tk-meta-item"><span class="tk-k">场站</span>' +
        '<span class="mono">' + esc(String(station)) + "</span></span>");
    }

    var rows = Object.keys(params).filter(function (key) {
      return !TICKET_CORE[key];
    }).map(function (key) {
      return paramRow(key, params[key]);
    }).join("");

    var blocks = textBlocks(description).map(function (block) {
      return '<div class="tk-block">' +
        (block.label ? '<span class="tk-k">' + esc(block.label) + "</span>" : "") +
        '<p class="tk-text">' + esc(block.text) + "</p></div>";
    }).join("");

    return '<article class="tk-card' + (tone ? " tone-" + tone : "") + '">' +
      '<div class="tk-head">' +
      (badge ? '<span class="tk-badge' + (tone ? " tone-" + tone : "") + '">' +
        esc(badge) + "</span>" : "") +
      '<span class="tk-title">' + esc(parts.text || "（未命名工单）") + "</span></div>" +
      (meta.length ? '<div class="tk-meta">' + meta.join("") + "</div>" : "") +
      (blocks ? '<div class="tk-body">' + blocks + "</div>" : "") +
      (rows ? '<div class="tk-rows">' + rows + "</div>" : "") +
      "</article>";
  }

  function paramsCard(params) {
    var keys = Object.keys(params || {});
    if (!keys.length) return "";
    return '<div class="tk-rows standalone">' + keys.map(function (key) {
      return paramRow(key, params[key]);
    }).join("") + "</div>";
  }

  /* 流程条。

     为什么画这一条：确认的人真正要回答的不是「参数对不对」，而是「点下去以后
     会发生什么、现在停在哪」。卡片给前者，这条给后者。

     只画本平台真能负责的四步。写之后由工单系统派单、处理、验收，那一段
     不在这里画——画了就是在暗示平台知道那些状态，而它并不知道。 */
  var FLOW_STEPS = {
    ticket: ["提交审批", "人工确认", "写入工单", "回读核验"],
    write: ["提交审批", "人工确认", "执行操作", "回读核验"]
  };

  var FLOW_MARK = {
    pending: ["done", "now", "wait", "wait"],
    verified: ["done", "done", "done", "done"],
    executed: ["done", "done", "done", "wait"],
    cancelled: ["done", "stop", "skip", "skip"],
    failed: ["done", "done", "fail", "wait"],
    closed: ["done", "done", "wait", "wait"]
  };

  // 状态取服务端那份，不在前端猜：`done` 是一句中文，从中文反推状态
  // 迟早会错（「已执行，尚未核验」里也有「已执行」）。
  function flowState(a) {
    var status = String((a && a.status) || "").toUpperCase();
    if (status === "VERIFIED") return "verified";
    if (status === "EXECUTED") return "executed";
    if (status === "REJECTED" || status === "EXPIRED") return "cancelled";
    if (status === "FAILED" || status === "RECOVERY_REQUIRED" ||
        status === "EXECUTION_UNKNOWN") return "failed";
    if (a && a.open === false) return "closed";
    return "pending";
  }

  /* 结论那一句就写在流程条下面，不再单独占一行：卡头有状态标、条上有走到哪一步，
     中间再插一句「已执行并核验通过」，同一件事会被读三遍。

     但服务端那句结论不能丢——失败时它带着错误码（「执行失败（DELEGATION_REVOKED）」），
     那正是该去找谁的唯一线索。所以有就用它，没有才用这里的兜底说法。 */
  function flowNote(state, preview, done) {
    var precise = String(done || "").trim();
    if (state === "pending") {
      var notice = preview && preview.notice;
      return String(notice || "尚未执行，确认后才会提交。");
    }
    if (state === "verified") {
      return (precise || "已执行并核验通过") +
        "；之后的派单与处理在工单系统里流转，不再经过本平台。";
    }
    if (state === "executed") {
      return (precise || "已执行") + "；还没有回读核验，核验之前不能当作已完成。";
    }
    if (state === "cancelled") return precise || "已取消，没有提交任何内容。";
    if (state === "failed") {
      return (precise || "执行失败") + "；请按错误码确认是否已经落库，不要直接重发。";
    }
    return precise || "这张单子已经结束，当前只保留了结论。";
  }

  function flowBar(a, kind) {
    var labels = FLOW_STEPS[kind] || FLOW_STEPS.write;
    var state = flowState(a);
    var marks = FLOW_MARK[state] || FLOW_MARK.pending;

    var items = labels.map(function (label, index) {
      var mark = marks[index] || "wait";
      return '<li class="' + mark + '"><span class="dot"></span>' +
        '<span class="lb">' + esc(label) + "</span></li>";
    }).join("");

    return '<div class="ap-flow-wrap">' +
      '<ol class="ap-flow">' + items + "</ol>" +
      '<div class="ap-flow-note">' +
      esc(flowNote(state, (a && a.preview) || {}, a && a.done)) + "</div></div>";
  }

  var AP_STATUS = {
    PENDING: { text: "待确认", tone: "warn" },
    EXECUTING: { text: "执行中", tone: "warn" },
    APPROVED: { text: "待执行", tone: "warn" },
    EXECUTED: { text: "待核验", tone: "warn" },
    VERIFIED: { text: "已核验", tone: "ok" },
    REJECTED: { text: "已取消", tone: "" },
    EXPIRED: { text: "已过期", tone: "" },
    FAILED: { text: "执行失败", tone: "err" },
    RECOVERY_REQUIRED: { text: "需回读", tone: "err" },
    EXECUTION_UNKNOWN: { text: "结果未知", tone: "err" }
  };

  /* 卡头右上角那个小标。没有 status 的老卡片（审批单已被清理）不编状态，
     返回空串——那句「查不到这张审批单」已经在正文里说清楚了。 */
  function statusChip(a) {
    var status = String((a && a.status) || "").toUpperCase();
    var meta = AP_STATUS[status];
    if (!meta) return "";
    return '<span class="chip ' + meta.tone + '">' + esc(meta.text) + "</span>";
  }

  /* 审批卡正文。入参就是 console.js 里那个 `msg.approval`
     （{ preview, status, open, done }），不另造一层结构。 */
  function approvalBody(a) {
    var preview = (a && a.preview) || {};
    var params = preview.parameters;

    // 参数不是对象（老卡片、被清理的单子、或工具本来就没参数）时，
    // 只画流程条。空壳卡片比一张只有标题的卡更让人怀疑是不是加载失败。
    if (!params || typeof params !== "object" || Array.isArray(params)) {
      return preview.tool_name || preview.operation ? flowBar(a, "write") : "";
    }

    var isTicket = looksLikeTicket(params);
    var card = isTicket ? ticketCard(params) : paramsCard(params);
    return card + flowBar(a, isTicket ? "ticket" : "write");
  }

  /* ---------------- 运行横条（L0「现在」） ----------------

     放在这里而不是 console.js 里：它是纯 HTML 拼装，能直接用 node 跑断言
     （scripts/check_render.mjs）。横条上出现的是 Agent 名与工具名——
     用户可填的字符串，转义漏一个就是注入口子。 */

  // 「已跑 42 秒 / 3 分钟 / 2 小时 5 分」。这一层看的是量级，
  // 精确到毫秒的数反而要人多读两位。
  function fmtAge(ms) {
    var seconds = Math.max(0, Math.round(Number(ms || 0) / 1000));
    if (seconds < 60) return seconds + " 秒";
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + " 分钟";
    return Math.floor(minutes / 60) + " 小时 " + (minutes % 60) + " 分";
  }

  function liveStrip(data) {
    if (!data || data.enabled === false) return "";

    var running = data.running || [];
    var waiting = data.waiting || [];
    // 没有在跑、没有在等：整条不渲染。留一个「0 个」的空壳只会占掉
    // 屏幕最值钱的那一行，而且看久了就没人再看它。
    if (!running.length && !waiting.length) return "";

    var parts = [];

    if (running.length) {
      parts.push('<div class="live-group"><span class="live-label">在跑</span>' +
        running.map(function (r) {
          // stale 不等于死了：可能是模型在慢慢想，也可能是进程没了而清扫任务
          // 还没轮到。区别只在颜色与提示，点进去才分得清。
          var hint = (r.agent_name || "未归属") + " · 已跑 " + fmtAge(r.age_ms) +
            (r.stale ? " · 超过 " + fmtAge(data.stale_after_ms) + "，可能已经卡住" : "") +
            " · 点开看这一轮";
          return '<button class="live-pill' + (r.stale ? " stale" : "") +
            '" type="button" data-live-trace="' + esc(r.trace_id) + '" title="' + esc(hint) + '">' +
            '<span class="dot"></span>' + esc(r.agent_name || "未归属") +
            '<span class="mono">' + esc(fmtAge(r.age_ms)) + "</span></button>";
        }).join("") +
        (data.truncated ? '<span class="live-more">…</span>' : "") +
        "</div>");
    }

    if (waiting.length) {
      parts.push('<div class="live-group"><span class="live-label">在等人工</span>' +
        waiting.map(function (w) {
          // 「谁该去点确认」是这张视图存在的主要理由，所以转交来源写在提示里。
          var who = w.delegated_from ? "由 " + w.delegated_from + " 转交" : "用户直接触发";
          var hint = (w.tool_display_name || w.tool_name) + " · " + who +
            " · 已等 " + fmtAge(w.waited_ms) +
            (w.expires_at ? " · 到期后自动作废" : "") +
            " · 在对话台确认";
          return '<span class="live-pill wait' + (w.stale ? " stale" : "") +
            '" title="' + esc(hint) + '">' +
            '<span class="dot"></span>' + esc(w.tool_display_name || w.tool_name) +
            '<span class="mono">' + esc(fmtAge(w.waited_ms)) + "</span></span>";
        }).join("") +
        (data.truncated ? '<span class="live-more">…</span>' : "") +
        "</div>");
    }

    return parts.join("");
  }

  /* ---------------- 委派拓扑（L2 关系图） ----------------

     纯 SVG 字符串拼装，放这里的理由和运行横条一样：节点名、Agent 名都是
     用户可填的字符串，而且坐标算法能在 node 里跑断言（scripts/check_render.mjs）。

     为什么是固定分层，不是力导向：箭头方向就是权限流向（边界单调收窄），
     「发起方在左、被委派方在右」这一条已经表达了全部语义；力导向会让同一份
     数据每次刷新都换个位置，看的人会先怀疑图，而不是先读图。 */

  var TOPO_COL_W = 244;
  var TOPO_NODE_W = 182;
  var TOPO_NODE_H = 52;
  var TOPO_GAP = 20;
  var TOPO_PAD = 12;

  // 同一对节点之间可能有三条边（成功 / 等人工 / 被拒）。扇开一点，
  // 否则它们会叠成一条线，看起来像「只有一种结局」。
  // 12px 是「数字自己带白边压上去」之后仍不会糊掉的最小间距。
  var TOPO_EDGE_SPREAD = 12;

  // 同一条束里的数字沿各自的曲线错开摆放：都摆在中点的话，两条边只差 9px，
  // 10.5px 的数字会上下叠在一起，读出来的是「5 上面压着 2」而不是「哪条边几个」。
  // 错开之后横向走的是同一条曲线，所以数字永远压在自己那根线上。
  var TOPO_COUNT_SPAN = 0.4;

  // 三次贝塞尔上的取点（控制点固定为 (mid, y1) 与 (mid, y2)，见下面的 d 属性）。
  function topoCurveAt(t, x1, y1, mid, y2, x2) {
    var u = 1 - t;
    var a = u * u * u;
    var b = 3 * u * u * t;
    var c = 3 * u * t * t;
    var d = t * t * t;
    return {
      x: a * x1 + (b + c) * mid + d * x2,
      y: (a + b) * y1 + (c + d) * y2
    };
  }

  // 连线的绘制顺序：成功的画在中间，异常的分列两侧。
  var TOPO_KIND_ORDER = { ok: 0, paused: 1, failed: 2, rejected: 3 };

  function topoKey(name) {
    // 没有名字的那一端（平台基线）与任何字符串都不同。
    return name === null || name === undefined ? "\u0000" : String(name);
  }

  function topoLabel(node) {
    if (!node) return "未归属";
    return node.display_name || node.name || "未归属";
  }

  // 按**字符**截断，不按字节：SVG 的 <text> 不会换行，超长会直接压到隔壁节点上；
  // 而按字节算会把「运维专家」截成「运」。
  function clipText(text, limit) {
    var value = String(text === null || text === undefined ? "" : text);
    return value.length > limit ? value.slice(0, limit - 1) + "…" : value;
  }

  function topologyView(data) {
    if (!data) return "";

    if (data.enabled === false) {
      return '<div class="empty"><strong>观测已关闭</strong>' +
        "<ul><li>委派拓扑只能从观测数据推出来，关掉开关就没有可画的边；</li>" +
        "<li>打开 <code>OBS_ENABLED</code> 并跑出几次委派后，这张图才会出现。</li></ul></div>";
    }

    if (data.restricted) {
      // 空集 + 一句人话。403 会把「你没权限」说成「接口坏了」。
      return '<div class="banner warn">' + esc(data.reason || "需要 observability:read") +
        "</div>";
    }

    var nodes = data.nodes || [];
    var edges = data.edges || [];
    if (!nodes.length) {
      return '<div class="empty"><strong>这个窗口里没有委派记录</strong>' +
        "<ul><li>把任务转交给别的智能体（<code>delegate_to_agent</code>）才会产生边；</li>" +
        "<li>时间窗调大一点再试——拓扑看的是周级的量，一天里没有转交是正常的。</li></ul></div>";
    }

    var byKey = {};
    nodes.forEach(function (node) { byKey[topoKey(node.name)] = node; });

    var linked = {};
    edges.forEach(function (edge) {
      linked[topoKey(edge.from_agent)] = true;
      linked[topoKey(edge.to_agent)] = true;
    });

    /* 声明层（编排里配的边）。它不是事实，只是「应该怎样」，所以：
       1. 只画**没有实测对应**的那几条——两边都跑过的边再叠一条虚线，
          图上除了噪音什么也没多；
       2. 用稀疏点线，跟 paused 的虚线分得开（那条是「真的发生过、停在人手上」）；
       3. 端点按声明图推深度，推不出来就不画——飞向空处的线会让人以为图坏了。 */
    var observedPairs = {};
    edges.forEach(function (edge) {
      observedPairs[topoKey(edge.from_agent) + "\u0001" + edge.to_agent] = true;
    });

    var declared = (data.declared_edges || []).filter(function (edge) {
      return edge && edge.from_agent && edge.to_agent && byKey[topoKey(edge.from_agent)] &&
        byKey[topoKey(edge.to_agent)];
    });

    var declaredOnly = declared.filter(function (edge) {
      return !observedPairs[topoKey(edge.from_agent) + "\u0001" + edge.to_agent];
    });

    // 只在声明里出现过的节点，实测 depth 是 null。按声明图从根往下推：
    // 起点（没有声明入边，或本身有实测 depth）定住，其余取「父级最深 +1」。
    // 迭代次数封顶，成环时宁可少推一层也不死循环。
    var declaredDepth = {};
    for (var pass = 0; pass < 8; pass += 1) {
      var moved = false;
      declaredOnly.forEach(function (edge) {
        var fromKey = topoKey(edge.from_agent);
        var toKey = topoKey(edge.to_agent);
        var parent = byKey[fromKey];
        var base = parent && typeof parent.depth === "number"
          ? parent.depth
          : (typeof declaredDepth[fromKey] === "number" ? declaredDepth[fromKey] : 0);
        if (typeof declaredDepth[toKey] !== "number" || declaredDepth[toKey] < base + 1) {
          if (declaredDepth[toKey] !== base + 1) moved = true;
          declaredDepth[toKey] = base + 1;
        }
      });
      if (!moved) break;
    }

    declaredOnly.forEach(function (edge) {
      linked[topoKey(edge.from_agent)] = true;
      linked[topoKey(edge.to_agent)] = true;
    });

    // 按 depth 分层。没有 depth（配置了但从没被调用）的节点不在图上，
    // 它们在下面单独列出来——混进图里会变成一堆没有边的孤岛。
    var columns = {};
    Object.keys(linked).forEach(function (name) {
      var node = byKey[name];
      if (!node) return;
      var depth = typeof node.depth === "number"
        ? node.depth
        : (typeof declaredDepth[name] === "number" ? declaredDepth[name] : 0);
      (columns[depth] = columns[depth] || []).push(node);
    });

    var depths = Object.keys(columns).map(Number).sort(function (a, b) { return a - b; });
    var positions = {};
    var rows = {};
    var maxRows = 0;

    // 第一遍：每列按「被叫次数 / 轮次」排，作为重心法的起点。
    depths.forEach(function (depth) {
      var list = columns[depth].slice().sort(function (a, b) {
        return (b.attempts - a.attempts) || (b.traces - a.traces) ||
          String(a.name || "").localeCompare(String(b.name || ""));
      });
      columns[depth] = list;
      if (list.length > maxRows) maxRows = list.length;
      list.forEach(function (node, index) { rows[topoKey(node.name)] = index; });
    });

    var parentsOf = {};
    edges.forEach(function (edge) {
      var child = topoKey(edge.to_agent);
      (parentsOf[child] = parentsOf[child] || []).push(topoKey(edge.from_agent));
    });

    // 每个节点「父节点的平均行号」。没有可参照的父节点时沉到本列底部，
    // 而不是冒到顶上假装它最相关。
    function barycenter(name) {
      var list = parentsOf[name] || [];
      var sum = 0;
      var count = 0;
      list.forEach(function (parent) {
        if (typeof rows[parent] === "number") {
          sum += rows[parent];
          count += 1;
        }
      });
      return count ? sum / count : Number.MAX_SAFE_INTEGER;
    }

    // 第二遍：按父节点的平均行号重排，**只做一次，不迭代**。
    //
    // 固定分层的代价是边会交叉：cs 的两个下游原本一个在第 1 行、一个在第 3 行，
    // 于是那条线斜穿整个第二列。一步重心调整就能把同一父节点的子节点排到一起。
    // 不做迭代是因为：多次迭代会让同一份数据在「看起来差不多」的两个解之间跳，
    // 而这张图的价值恰恰是「刷新两次位置一样」。
    depths.slice(1).forEach(function (depth) {
      var list = columns[depth];
      var rank = {};
      list.forEach(function (node, index) { rank[topoKey(node.name)] = index; });

      columns[depth] = list.slice().sort(function (a, b) {
        var keyA = topoKey(a.name);
        var keyB = topoKey(b.name);
        return (barycenter(keyA) - barycenter(keyB)) || (rank[keyA] - rank[keyB]);
      });
      columns[depth].forEach(function (node, index) { rows[topoKey(node.name)] = index; });
    });

    depths.forEach(function (depth, columnIndex) {
      columns[depth].forEach(function (node, rowIndex) {
        positions[topoKey(node.name)] = {
          x: TOPO_PAD + columnIndex * TOPO_COL_W,
          y: TOPO_PAD + rowIndex * (TOPO_NODE_H + TOPO_GAP)
        };
      });
    });

    var width = TOPO_PAD * 2 + Math.max(0, depths.length - 1) * TOPO_COL_W + TOPO_NODE_W;
    var height = TOPO_PAD * 2 + Math.max(1, maxRows) * (TOPO_NODE_H + TOPO_GAP) - TOPO_GAP;

    var pairs = {};
    edges.forEach(function (edge) {
      var key = topoKey(edge.from_agent) + "\u0001" + edge.to_agent;
      (pairs[key] = pairs[key] || []).push(edge);
    });

    var paths = [];
    var counts = [];

    Object.keys(pairs).forEach(function (key) {
      var group = pairs[key].slice().sort(function (a, b) {
        return (TOPO_KIND_ORDER[a.kind] || 0) - (TOPO_KIND_ORDER[b.kind] || 0);
      });
      // 一组里每条边错开的步长：两端留出 0.3 的余量，数字不会跑到节点头上。
      var step = group.length > 1 ? TOPO_COUNT_SPAN / (group.length - 1) : 0;

      group.forEach(function (edge, index) {
        var from = positions[topoKey(edge.from_agent)];
        var to = positions[topoKey(edge.to_agent)];
        // 边指向一个没画出来的节点：宁可不画，也不画一条飞向空处的线。
        if (!from || !to) return;

        var spread = (index - (group.length - 1) / 2) * TOPO_EDGE_SPREAD;
        var x1 = from.x + TOPO_NODE_W;
        var y1 = from.y + TOPO_NODE_H / 2 + spread;
        var x2 = to.x;
        var y2 = to.y + TOPO_NODE_H / 2 + spread;
        var mid = (x1 + x2) / 2;

        paths.push('<path class="topo-edge ' + esc(edge.kind) + '" d="M' + x1 + " " + y1 +
          " C" + mid + " " + y1 + " " + mid + " " + y2 + " " + x2 + " " + y2 + '"/>');

        var t = 0.5 + (index - (group.length - 1) / 2) * step;
        var at = topoCurveAt(t, x1, y1, mid, y2, x2);
        // 数字带上边的类型：只有数字的话，一束里「5」到底是被拒还是跑通没人知道。
        counts.push('<text class="topo-count ' + esc(edge.kind) + '" x="' + Math.round(at.x) +
          '" y="' + (Math.round(at.y) - 4) + '" text-anchor="middle">' +
          esc(String(edge.count)) + "</text>");
      });
    });

    // 声明了、但窗口内一次都没跑过的边。**不标数字**：标了会让人以为那是
    // 调用次数，而它恰恰是 0 次——正是这条线要说明的事。
    declaredOnly.forEach(function (edge) {
      var from = positions[topoKey(edge.from_agent)];
      var to = positions[topoKey(edge.to_agent)];
      if (!from || !to) return;

      var x1 = from.x + TOPO_NODE_W;
      var y1 = from.y + TOPO_NODE_H / 2;
      var x2 = to.x;
      var y2 = to.y + TOPO_NODE_H / 2;
      var mid = (x1 + x2) / 2;

      paths.push('<path class="topo-edge declared" d="M' + x1 + " " + y1 +
        " C" + mid + " " + y1 + " " + mid + " " + y2 + " " + x2 + " " + y2 + '"/>');
    });

    var boxes = Object.keys(linked).map(function (name) {
      var node = byKey[name];
      var at = positions[name];
      if (!node || !at) return "";

      var sub = [];
      // 「转交」而不是「被叫」：这个数含被拒的尝试，而 `/agents` 里那个
      // 同名概念数的是 span。两个数不相等是正常的，标签上就别让人以为是同一个。
      if (node.attempts) sub.push("转交 " + node.attempts + " 次");
      if (node.traces) sub.push("自己 " + node.traces + " 轮");
      // 只在声明里出现的节点：说清它是「配了没跑」还是「真没跟人来往」，
      // 否则读者分不出这条虚线是「还没用起来」还是「用坏了」。
      if (!node.attempts && !node.traces && node.configured) sub.push("还没跑过");

      return '<g class="topo-node' + (node.can_write ? " can-write" : "") + '">' +
        '<rect x="' + at.x + '" y="' + at.y + '" width="' + TOPO_NODE_W +
        '" height="' + TOPO_NODE_H + '" rx="8"/>' +
        '<text class="topo-name" x="' + (at.x + 10) + '" y="' + (at.y + 20) + '">' +
        esc(clipText(topoLabel(node), 16)) + "</text>" +
        (sub.length
          ? '<text class="topo-sub" x="' + (at.x + 10) + '" y="' + (at.y + 36) + '">' +
            esc(sub.join(" · ")) + "</text>"
          : "") +
        "</g>";
    });

    var svg = "";
    if (edges.length || declaredOnly.length) {
      // 显式给出像素宽高，再由 CSS 的 max-width:100% 兜住窄屏：
      // 只写 viewBox 会把整张图拉到卡片宽度，字号跟着放大到两倍，
      // 跟页面上其余文字对不上——图看起来「被放大了」而不是「画得清楚」。
      svg = '<div class="topo-canvas"><svg viewBox="0 0 ' + width + " " + height +
        '" width="' + width + '" height="' + height +
        '" role="img" aria-label="智能体委派关系图">' +
        paths.join("") + counts.join("") + boxes.join("") + "</svg></div>";
    } else {
      // 有节点、没有边：不画一块空画布。空白画布看起来像图没加载出来，
      // 而这实际上是「这段时间里没有任何转交」这个明确结论。
      svg = '<div class="empty"><strong>这个窗口里没有任何委派</strong>' +
        "<ul><li>把任务转交给别的智能体（<code>delegate_to_agent</code>）才会产生边；</li>" +
        "<li>时间窗调大一点再试——拓扑看的是周级的量，一天里没有转交是正常的。</li></ul></div>";
    }

    // 配置了但没有委派关系的节点：图外的孤岛。
    // 「我建的这个专家到底有没有用」这张图能回答，靠的就是这一列。
    var isolated = nodes.filter(function (node) { return !linked[topoKey(node.name)]; });
    var isolatedBlock = isolated.length
      ? '<div class="topo-isolated"><span class="topo-legend-label">没有委派关系：</span>' +
        isolated.map(function (node) {
          var why = node.observed
            ? "有轮次，但没跟别的智能体来往"
            : (node.configured ? "配置了，窗口内一次都没被调用" : "只见于历史记录");
          return '<span class="chip" title="' + esc(why) + '">' + esc(topoLabel(node)) +
            (node.can_write ? ' <span class="mono">W</span>' : "") + "</span>";
        }).join("") + "</div>"
      : "";

    // 虚线只在真有「声明了没跑过」的边时才进图例：一进来就常驻，
    // 会让人以为这张图总该有虚线，看不到就开始怀疑数据没加载。
    var declaredLegend = declaredOnly.length
      ? '<span><i class="topo-swatch declared"></i>图上声明，窗口内没跑过</span>'
      : "";

    var legend = '<div class="topo-legend">' +
      '<span><i class="topo-swatch ok"></i>跑通</span>' +
      '<span><i class="topo-swatch paused"></i>停在等人工</span>' +
      '<span><i class="topo-swatch failed"></i>子调用失败</span>' +
      '<span><i class="topo-swatch rejected"></i>被边界拒绝</span>' +
      declaredLegend +
      '<span class="topo-legend-label">W = 可写</span>' +
      "</div>";

    return legend + svg + isolatedBlock +
      '<div class="obs-foot muted">' +
      "实线是<strong>观测到的</strong>委派调用；点线是编排里<strong>声明</strong>的边。" +
      "两者不一致时，两个方向都有信息量：声明了却从没跑过，说明配了没人用；" +
      "跑过却没在任何图里，说明模型在替人做路由——那要么该固化成流程，要么说明边界太宽。" +
      "被拒的边单独成色：「一直被拒」和「一直跑通」是两种完全不同的架构问题，" +
      "合并成一条只会让人去修一个配得好好的边界。</div>";
  }

  /* ================= L3：泳道与运行页 ================= */

  // display_state 的六个取值是封闭集合（`app/observability/state.py`）。
  // 中文只在这一层出现：接口里再存一份中文，两处迟早各说各话。
  var STATE_LABEL = {
    working: "在跑",
    "input-required": "等人工",
    completed: "完成",
    failed: "失败",
    rejected: "被否决",
    canceled: "已取消"
  };

  function stateChip(state) {
    var label = STATE_LABEL[state] || state || "未知";
    var cls = state === "completed" ? "ok"
      : (state === "working" || state === "input-required") ? "warn"
        : (state === "canceled" ? "" : "err");
    return '<span class="chip ' + cls + '">' + esc(label) + "</span>";
  }

  function fmtClock(iso) {
    var ms = Date.parse(iso);
    if (!isFinite(ms)) return "—";
    var at = new Date(ms);
    function pad(n) { return String(n).length < 2 ? "0" + n : String(n); }
    return pad(at.getHours()) + ":" + pad(at.getMinutes()) + ":" + pad(at.getSeconds());
  }

  // 毫秒 → 「820ms / 6.9s / 2 分钟」。横轴上比的是量级，多给两位小数只是噪声。
  function fmtSpan(ms) {
    if (ms === null || ms === undefined) return "—";
    var n = Math.max(0, Number(ms));
    if (!isFinite(n)) return "—";
    if (n < 1000) return Math.round(n) + "ms";
    if (n < 60000) return (Math.round(n / 100) / 10) + "s";
    return fmtAge(n);
  }

  function opStat(label, value, note) {
    return '<div class="stat"><span class="stat-label">' + esc(label) + "</span>" +
      '<span class="stat-value">' + esc(value) + "</span>" +
      (note ? '<span class="stat-note">' + esc(note) + "</span>" : "") + "</div>";
  }

  var SWIM_LABEL_W = 112;
  var SWIM_PLOT_W = 520;
  var SWIM_LANE_H = 34;
  var SWIM_LANE_GAP = 6;
  var SWIM_TOP = 34;
  var SWIM_PAD = 10;

  function spanStart(span) {
    var ms = Date.parse(span.started_at);
    return isFinite(ms) ? ms : null;
  }

  function spanEnd(span) {
    var start = spanStart(span);
    if (start === null) return null;
    var ms = Number(span.duration_ms);
    return isFinite(ms) ? start + ms : start;
  }

  // 有委派才切泳道。没有委派的一轮仍然用缩进盒子列表——在这个规模上它比
  // 泳道好读：一条竖线串起七八个节点，比一行一行的横条更接近「走了哪条路」。
  // 视图形态跟着数据形态走，不搞「统一成一种」。
  function hasDelegation(spans) {
    return (spans || []).some(function (span) {
      var attrs = span.attrs || {};
      return span.kind === "agent" && typeof attrs.depth === "number" && attrs.depth >= 1;
    });
  }

  function swimlanes(spans) {
    var list = spans || [];
    if (!hasDelegation(list)) return "";

    // 按 agent_name 分道。`null` 归「未归属」：存量 span 是这种情况，
    // 不并到发起方身上——那会把「这一段是谁干的」抹掉。
    var lanes = [];
    var index = {};
    list.forEach(function (span) {
      var start = spanStart(span);
      if (start === null) return;
      var key = span.agent_name || "\u0000";
      var lane = index[key];
      if (!lane) {
        lane = { name: span.agent_name || null, first: start, last: start, depth: null, from: null };
        index[key] = lane;
        lanes.push(lane);
      }
      lane.first = Math.min(lane.first, start);
      lane.last = Math.max(lane.last, spanEnd(span) === null ? start : spanEnd(span));
      if (span.kind === "agent") {
        var attrs = span.attrs || {};
        if (typeof attrs.depth === "number") lane.depth = attrs.depth;
        if (attrs.delegated_from) lane.from = String(attrs.delegated_from);
      }
    });
    if (!lanes.length) return "";

    lanes.sort(function (a, b) { return a.first - b.first; });
    lanes.forEach(function (lane, i) { lane.slot = i; });

    var t0 = lanes[0].first;
    var t1 = lanes.reduce(function (most, lane) { return Math.max(most, lane.last); }, t0);
    var total = Math.max(1, t1 - t0);

    var width = SWIM_LABEL_W + SWIM_PLOT_W + SWIM_PAD;
    var height = SWIM_TOP + lanes.length * (SWIM_LANE_H + SWIM_LANE_GAP) - SWIM_LANE_GAP + SWIM_PAD;

    function x(at) { return SWIM_LABEL_W + ((at - t0) / total) * SWIM_PLOT_W; }
    function laneTop(i) { return SWIM_TOP + i * (SWIM_LANE_H + SWIM_LANE_GAP); }
    function laneMid(i) { return laneTop(i) + SWIM_LANE_H / 2; }

    var parts = [];

    // 轴：只标三个点。这条轴回答的是「谁先谁后、差多少倍」，
    // 不是「精确到毫秒几点几分」，刻度给多了反而读不出比例。
    [0, 0.5, 1].forEach(function (ratio) {
      var at = x(t0 + total * ratio);
      parts.push('<line class="swim-tick" x1="' + at + '" y1="' + (SWIM_TOP - 8) +
        '" x2="' + at + '" y2="' + (SWIM_TOP - 2) + '"/>');
      parts.push('<text class="swim-axis-label" x="' + at + '" y="' + (SWIM_TOP - 14) +
        '" text-anchor="' + (ratio === 0 ? "start" : (ratio === 1 ? "end" : "middle")) + '">' +
        esc(ratio === 0 ? "0" : "+" + fmtSpan(total * ratio)) + "</text>");
    });
    parts.push('<text class="swim-axis-label" x="0" y="' + (SWIM_TOP - 14) + '">智能体</text>');

    lanes.forEach(function (lane, i) {
      var top = laneTop(i);
      var left = x(lane.first);
      var right = x(lane.last);

      parts.push('<line class="swim-rule" x1="' + SWIM_LABEL_W + '" y1="' + laneMid(i) +
        '" x2="' + (SWIM_LABEL_W + SWIM_PLOT_W) + '" y2="' + laneMid(i) + '"/>');

      // 一条横条 = 这个智能体在这段时间里活跃的那一段（它名下所有 span 的并集）。
      // 不给每个 span 各画一条：同一层的 node / llm / tool 起点几乎相同，
      // 叠出来是一块实心色块，反而看不出「谁在什么时候在干」。
      parts.push('<g class="swim-lane"><title>' +
        esc((lane.name || "未归属") + " · 活跃 " + fmtSpan(lane.last - lane.first)) + "</title>" +
        '<rect x="' + left + '" y="' + (laneMid(i) - 6) + '" width="' +
        Math.max(2, right - left) + '" height="12" rx="3"/></g>');

      // 失败点用竖线戳出来：横条只说明「在干活」，不说明「干砸了」。
      list.forEach(function (item) {
        if ((item.agent_name || "\u0000") !== (lane.name || "\u0000")) return;
        if (item.status === "ok" || !item.status) return;
        var at = x(spanStart(item));
        parts.push('<line class="swim-fail" x1="' + at + '" y1="' + (laneMid(i) - 10) +
          '" x2="' + at + '" y2="' + (laneMid(i) + 10) + '"><title>' +
          esc(item.name + " · " + (item.error_category || item.status)) + "</title></line>");
      });

      var sub;
      if (typeof lane.depth === "number" && lane.depth >= 1) {
        sub = "第 " + lane.depth + " 跳" + (lane.from ? " · 被 " + lane.from + " 转交" : "");
      } else {
        sub = "发起";
      }
      parts.push('<text class="swim-name" x="0" y="' + (top + 14) + '">' +
        esc(clipText(lane.name || "未归属", 10)) + "</text>");
      parts.push('<text class="swim-sub" x="0" y="' + (top + 27) + '">' + esc(sub) + "</text>");
    });

    // 委派：一条虚线从上一位发起方那一行掉到被委派那一行，落点就是它开始干活的时刻。
    lanes.forEach(function (lane) {
      if (typeof lane.depth !== "number" || lane.depth < 1) return;
      var parent = lane.from ? index[lane.from] : null;
      if (!parent) return;
      var at = x(lane.first);
      parts.push('<path class="swim-delegate" d="M' + at + " " + (laneMid(parent.slot) + 6) +
        " L" + at + " " + (laneMid(lane.slot) - 9) + '"/>');
      parts.push('<circle class="swim-dot" cx="' + at + '" cy="' + (laneMid(lane.slot) - 6) +
        '" r="2.6"><title>' + esc((parent.name || "未归属") + " → " + (lane.name || "未归属")) +
        "</title></circle>");
    });

    return '<div class="swim"><svg viewBox="0 0 ' + width + " " + height +
      '" width="' + width + '" height="' + height +
      '" role="img" aria-label="按智能体分行的耗时泳道">' + parts.join("") + "</svg></div>" +
      '<div class="swim-legend muted">' +
      '横条 = 这个智能体活跃的那一段（起点差就是等待与调度）；' +
      '竖虚线 = 一次转交，落点是被委派方开始干活的时刻；' +
      '<i class="swim-fail-swatch"></i> = 失败点。' +
      "</div>";
  }

  function operationView(data) {
    if (!data) return "";

    if (data.enabled === false) {
      return '<div class="card"><div class="empty"><strong>观测已关闭</strong>' +
        "<ul><li>运行视图靠 trace 串起时间线，关掉观测就只剩审批单，看不出「怎么走的」；</li>" +
        "<li>打开 <code>OBS_ENABLED</code> 并跑一次写操作，这次运行才会出现。</li></ul></div></div>";
    }

    var traces = data.traces || [];
    var approvals = data.approvals || [];
    var steps = data.steps || [];
    var participants = data.participants || [];

    if (!traces.length && !approvals.length) {
      return '<div class="card"><div class="empty"><strong>这次运行没有记录</strong>' +
        "<ul><li>可能查的不是运行标识，或者这条运行已经过了保留期；</li>" +
        "<li>观测保留期默认 14 天，错误 trace 30 天。</li></ul></div></div>";
    }

    // 三个耗时是同一条总长的三种切法，所以给一根比例条：
    // 「等人工」占多少，一眼就看得出该去改流程还是改代码。
    var wall = Number(data.wall_ms) || 0;
    var waiting = Math.min(Number(data.waiting_ms) || 0, wall);
    var share = wall > 0 ? Math.round((waiting / wall) * 100) : 0;

    var head = '<div class="card">' +
      '<div class="card-head"><div><h3>这一次运行</h3></div>' +
      stateChip(data.display_state) + "</div>" +
      '<div class="stats">' +
      opStat("历时", fmtSpan(wall), "从第一轮开始算，到最后一轮结束") +
      opStat("其中等人工", fmtSpan(waiting), "挂在审批上、等人点确认的时间") +
      opStat("实际在跑", fmtSpan(Number(data.active_ms) || 0), "历时减去等人工——系统真正干活的时间") +
      opStat("轮次", String(traces.length), traces.length > 1 ? "跨 " + traces.length + " 轮" : "") +
      opStat("审批单", String(approvals.length), "") +
      "</div>" +
      (wall > 0
        ? '<div class="op-split" title="等人工占历时的 ' + share + '%">' +
          '<i style="width:' + share + '%"></i></div>' +
          '<div class="op-split-legend muted">等人工 ' + share + "% · 在跑 " + (100 - share) + "%</div>"
        : "") +
      '<div class="obs-foot muted">' +
      "三个数不是并列的：<strong>历时 = 等人工 + 实际在跑</strong>。" +
      "一次运行历时 3 小时、其中 2 小时 58 分在等人，与「系统跑了 3 小时」是两个完全不同的结论——" +
      "前者要改的是审批路径，后者要改的是模型或工具。" +
      (data.attention
        ? "另外：这次运行已经有结论了，但还有一张单子挂着没人处理。"
        : "") +
      "</div></div>";

    // ---- 时间线 ----
    var roundOf = {};
    traces.forEach(function (trace, i) { roundOf[trace.trace_id] = i + 1; });

    var stepHtml = steps.map(function (step) {
      if (step.kind === "trace") {
        var delegations = (traces[roundOf[step.ref] - 1] || {}).delegations || [];
        var extra = delegations.map(function (item) {
          return '<span class="op-hop">→ ' + esc(item.agent) +
            (item.depth ? "（第 " + esc(String(item.depth)) + " 跳）" : "") +
            (item.duration_ms !== null ? " · " + esc(fmtSpan(item.duration_ms)) : "") +
            "</span>";
        }).join("");
        return '<li class="op-step" data-trace="' + esc(step.ref) + '">' +
          '<span class="op-at mono">' + esc(fmtClock(step.at)) + "</span>" +
          '<span class="op-kind">第 ' + esc(String(roundOf[step.ref] || 0)) + " 轮</span>" +
          '<span class="op-main">' + esc(step.agent_name || "默认助手") +
          '<span class="muted mono"> · ' + esc(fmtSpan(step.duration_ms)) + "</span>" +
          extra + "</span>" + stateChip(step.state) + "</li>";
      }
      return '<li class="op-step">' +
        '<span class="op-at mono">' + esc(fmtClock(step.at)) + "</span>" +
        '<span class="op-kind">等确认</span>' +
        '<span class="op-main">' + esc(step.tool_display_name || "写操作") +
        '<span class="muted"> · ' + esc(step.agent_name || "默认助手") + "</span></span>" +
        stateChip(step.state) + "</li>";
    }).join("");

    var timeline = '<div class="card">' +
      '<div class="card-head"><div><h3>这一次是怎么走的</h3></div>' +
      '<span class="muted">' + esc(String(steps.length)) + " 步</span></div>" +
      '<ul class="op-steps">' + stepHtml + "</ul>" +
      '<div class="obs-foot muted">点「第 N 轮」看那一轮的完整调用链。' +
      "中间的「等确认」就是人在这里停下的那一段——它不算系统耗时。</div></div>";

    // ---- 参与者 ----
    var roleLabel = { initiator: "发起", delegate: "被叫来干活", both: "又发起又转交" };
    var partHtml = participants.map(function (item) {
      return '<tr><td>' + esc(item.display_name || item.agent_name || "默认助手") +
        (item.agent_name ? ' <span class="mono muted">' + esc(item.agent_name) + "</span>" : "") +
        "</td><td>" + esc(roleLabel[item.role] || item.role) + "</td>" +
        '<td class="right mono">' + esc(String(item.traces)) + "</td>" +
        '<td class="right mono">' + esc(String(item.delegated_in)) + "</td>" +
        '<td class="right mono">' + esc(String(item.delegated_out)) + "</td>" +
        '<td class="right mono">' + esc(fmtSpan(item.waiting_ms)) + "</td></tr>";
    }).join("");

    var participantsCard = participants.length
      ? '<div class="card">' +
        '<div class="card-head"><div><h3>参与者</h3></div>' +
        '<span class="muted">谁做的决定、谁干的活</span></div>' +
        '<table class="tbl"><thead><tr>' +
        "<th>智能体</th><th>角色</th>" +
        '<th class="right">自己的轮次</th><th class="right">被转交进来</th>' +
        '<th class="right">转交出去</th><th class="right">它名下的等待</th>' +
        "</tr></thead><tbody>" + partHtml + "</tbody></table>" +
        '<div class="obs-foot muted">「被转交进来」数的是子调用，不含被拒的尝试；' +
        "被拒的分布看委派关系图。等待记在<strong>要执行</strong>的那个智能体账上，" +
        "不是发起方。</div></div>"
      : "";

    // ---- 审批单 ----
    var approvalHtml = approvals.map(function (item) {
      var chain = (item.delegation_chain || []).join(" → ");
      return '<div class="op-approval">' +
        '<div class="op-approval-head">' +
        "<strong>" + esc(item.tool_display_name || item.tool_name) + "</strong>" +
        stateChip(item.display_state) +
        '<span class="muted mono">' + esc(item.status) + "</span>" +
        '<span class="muted">等 ' + esc(fmtSpan(item.waited_ms)) + "</span></div>" +
        '<div class="op-reason">' + esc(item.reason || "（没有记录这次要做什么）") + "</div>" +
        '<div class="op-approval-foot muted">' +
        (chain ? "委派链：" + esc(chain) + " → " + esc(item.agent_name || "—") : "由用户直接触发") +
        (item.approved_by ? " · 由 " + esc(item.approved_by) + " 确认" : "") +
        (item.error_code ? " · " + esc(item.error_code) : "") +
        "</div></div>";
    }).join("");

    var approvalsCard = approvals.length
      ? '<div class="card">' +
        '<div class="card-head"><div><h3>审批单</h3></div>' +
        '<span class="muted">卡上那句话已经滚走了，这里重新拼一遍</span></div>' +
        approvalHtml +
        '<div class="obs-foot muted">事后回溯时最要紧的是「当时到底要做什么」和「谁批的」。' +
        "`EXECUTED` 与 `VERIFIED` 的差别是「回读过没有」，原文照登，不做归一。</div></div>"
      : "";

    return head + timeline + participantsCard + approvalsCard;
  }

  /* ---------------- 上下文（长对话压缩） ----------------
   *
   * 压缩是唯一会**静默改变模型输入**的机制：用户问「它怎么忘了我刚说的」，
   * 光看回答看不出发生过什么。这两处呈现就是把它摆到明面上：
   *   - trace 详情：一行 chip 说清「输入的底子有多大、多少轮已折成摘要」；
   *   - 会话页：在水位处插一条分隔条，说明上面那段不再以原文进入模型。
   *
   * 纯逻辑放在这里，是因为两条都绕不开「字段可能不存在」：老 trace 没有
   * `summary_turns`，观测关过的那一轮连 span 都没有。这种降级截图看不出来，
   * 只能靠断言锁住。
   */

  function fmtTokens(value) {
    var n = Number(value || 0);
    if (!n) return "0";
    if (n < 1000) return String(Math.round(n));
    var k = Math.round((n / 1000) * 10) / 10;
    return (k % 1 === 0 ? k.toFixed(0) : k.toFixed(1)) + "k";
  }

  function spanByName(spans, name) {
    var list = spans || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].name === name) return list[i];
    }
    return null;
  }

  // span 原文是 `{"output": ...}` 的 JSON；解析失败就当没有——
  // 观测库里的历史格式改过几次，不为读不出来的一行报错。
  function spanOutput(span) {
    if (!span || !span.content) return "";
    try {
      var parsed = JSON.parse(span.content);
      if (typeof parsed === "string") return parsed;
      if (parsed && typeof parsed.output === "string") return parsed.output;
    } catch (err) {
      return "";
    }
    return "";
  }

  function contextNote(spans) {
    var measure = spanByName(spans, "context.measure");
    var inject = spanByName(spans, "context.inject");
    var compact = spanByName(spans, "context.compact");
    if (!measure && !inject && !compact) return "";

    var m = (measure && measure.attrs) || {};
    var j = (inject && inject.attrs) || {};
    var k = (compact && compact.attrs) || {};

    var window = Number(m.window || 0);
    var measured = Number(m.last_prompt_tokens || 0);
    var kept = Number(j.turns != null ? j.turns : m.kept_turns || 0);
    // 只有存在摘要时服务端才写这个数；没有就整段不显示。写 0 会读成
    // 「一轮都没折」，而实际情况可能是「折了几轮、但没写摘要」。
    var covered = Number(j.summary_turns || m.summary_turns || 0);
    var action = String(m.action || "none");

    var bits = ["上下文 " + (measured > 0 ? fmtTokens(measured) : "未实测") +
      " / " + (window > 0 ? fmtTokens(window) : "未知")];
    if (covered > 0) bits.push("摘要覆盖 " + covered + " 轮");
    if (kept > 0) bits.push("最近 " + kept + " 轮原文");
    if (action === "compact" && Number(k.turns || 0) > 0) {
      bits.push("本轮压缩 " + Number(k.turns) + " 轮");
    }
    if (action === "hard") bits.push("硬截断");
    var label = bits.join(" · ");

    var summary = spanOutput(compact);
    if (!summary) return '<div class="ctx-note"><span class="chip">' + esc(label) + "</span></div>";

    var meta = [];
    if (Number(k.tokens_before || 0) > 0) {
      meta.push(fmtTokens(k.tokens_before) + " → " + fmtTokens(k.tokens_after));
    }
    if (k.model) meta.push(String(k.model));
    if (summary.length) meta.push(summary.length + " 字");

    return '<div class="ctx-note">' +
      '<button class="chip ctx-chip" type="button" id="obs-ctx-toggle" ' +
      'aria-expanded="false" aria-controls="obs-ctx-body">' + esc(label) + "</button>" +
      '<div class="ctx-body" id="obs-ctx-body" hidden>' +
      (meta.length ? '<div class="ctx-body-meta">' + esc(meta.join(" · ")) + "</div>" : "") +
      '<pre class="ctx-summ">' + esc(summary) + "</pre>" +
      "</div></div>";
  }

  /* 会话页的分隔条插在哪。
   *
   * 水位是「到这条消息为止已折进摘要」，所以要插在它**之后**。
   * 轮数按界面上真实可见的用户消息数算，不用库里的 `turns_covered`：
   * 那个数是压缩时被挤出窗口的轮次，与用户眼里的「几轮」不是同一个口径，
   * 拿它当文案会出现「更早的 4 轮」下面明明排着 6 条提问。
   */
  function foldSeam(messages, watermarkId) {
    var list = messages || [];
    if (!watermarkId) return null;

    var index = -1;
    for (var i = 0; i < list.length; i++) {
      if (list[i] && String(list[i].id || "") === String(watermarkId)) {
        index = i;
        break;
      }
    }
    if (index < 0) return null;

    var turns = 0;
    for (var j = 0; j <= index; j++) {
      if (list[j] && list[j].role === "user") turns += 1;
    }
    if (!turns) return null;

    return { index: index, turns: turns };
  }

  /* ---------------- 观测：窗口走势与分布条 ----------------

     两组图共用的取色。与 chip 的绿/黄/红是同一套语义（正常 / 等人 / 出错），
     但饱和度压低：这里是面积不是文字，用满色的大块绿会把整页压沉，
     排障时要看的是那几个例外，底色不该抢眼。 */
  var OBS_TONE = {
    ok: "#9dc3b2",
    awaiting: "#dcb26a",
    error: "#c76a61"
  };

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  /* 一格的时间范围。横轴标签与 tooltip 用同一套写法：
     一处写「07-12 18时」、另一处写「18:00」，读的人就要先在脑子里换算一次。 */
  function obsClock(date, spanHours) {
    var text = pad2(date.getHours()) + ":" + pad2(date.getMinutes());
    if (spanHours <= 48) return text;
    return date.getMonth() + 1 + "/" + date.getDate() + " " + text;
  }

  /* 窗口走势：一格一根堆叠柱，另叠一条耗时 p95 折线。

     为什么堆叠柱：三层加起来正好是这一格的总轮次，柱子的高度本身就是总量，
     不必再画一条「总量」线和三层对照（那种图最难读的正是这两者的对不齐）。

     为什么不是饼图：饼只能画一个截面，而这里要回答的是「什么时候变成这样的」。
     概览上那八个数字已经回答了「多大」，缺的正是时间这一维。

     为什么耗时另起右轴：毫秒和轮次没有共同的量纲，共轴就得二选一，
     而被舍弃的那个方向会被人按图上的位置直接读成另一个量。右轴标了单位，
     折线用虚线——它只说方向，精读请看 tooltip 里的数。 */
  function obsTrend(buckets, opts) {
    opts = opts || {};
    var list = Array.isArray(buckets) ? buckets : [];
    // 有格子但一格轮次都没有，和「没有格子」是同一件事：1 小时窗里没人用时
    // length 是 1，画出来是一张空白坐标轴加一个图例，读的人会以为图坏了。
    var hasData = list.some(function (b) {
      return (b.traces || 0) > 0;
    });
    if (!hasData) {
      return '<div class="obs-empty">这个窗口里还没有轮次，走势无从画起。</div>';
    }

    var bucketHours = Number(opts.bucketHours) || 1;
    var width = Math.max(280, Number(opts.width) || 660);
    var height = 158;
    var pad = { l: 36, r: 44, t: 12, b: 24 };
    var plotL = pad.l;
    var plotR = width - pad.r;
    var plotT = pad.t;
    var plotB = height - pad.b;
    var plotW = plotR - plotL;
    var plotH = plotB - plotT;

    var maxTraces = 1;
    var maxP95 = 0;
    list.forEach(function (b) {
      if ((b.traces || 0) > maxTraces) maxTraces = b.traces;
      if (b.p95_duration_ms && b.p95_duration_ms > maxP95) maxP95 = b.p95_duration_ms;
    });
    // 纵轴取整到 1/2/5 的整数倍：轴上出现 6.4 这种刻度，柱子高度和数字对不上。
    var step = niceStep(maxTraces, 3);
    var top = Math.max(step, Math.ceil(maxTraces / step) * step);

    function yOf(value) {
      return plotB - (value / top) * plotH;
    }

    var slot = plotW / list.length;
    // 格数多时柱子必须变窄，但再窄也留 2px——1px 的柱在普通屏上会被抹掉，
    // 那一格看起来就成了「没有数据」。
    var barW = Math.max(2, slot - (slot > 14 ? Math.min(6, slot * 0.28) : 1.5));

    var grid = "";
    for (var tick = 0; tick <= top + step * 0.001; tick += step) {
      var gy = round2(yOf(tick));
      grid += '<line x1="' + plotL + '" y1="' + gy + '" x2="' + plotR + '" y2="' + gy +
        '" stroke="#eef1ef" stroke-width="1"/>' +
        '<text x="' + (plotL - 7) + '" y="' + round2(gy + 3.5) +
        '" text-anchor="end" font-size="10.5" fill="#98a09a">' + esc(fmtNum(tick)) + "</text>";
    }

    var bars = "";
    var labels = "";
    var points = [];

    // 横轴标签按**像素**抽稀，不按格数比例：同一个 28 格窗口，宽屏上每格 40px
    // 能全标，窄屏上每格 18px 就只能标几个。按比例算的写法在窄屏会把
    // 「11/13 21:39」叠成一团黑。
    var every = Math.max(1, Math.ceil(list.length / Math.max(2, Math.floor(plotW / 82))));

    list.forEach(function (b, i) {
      var traces = Number(b.traces) || 0;
      var errors = Number(b.errors) || 0;
      var awaiting = Number(b.awaiting) || 0;
      // 「正常」是减出来的，不是另存的字段：三层必须刚好加回 traces，
      // 否则柱子高度和左边轴上的数字对不上，而那种错没人会去核对。
      var ok = Math.max(0, traces - errors - awaiting);

      var x = round2(plotL + slot * i + (slot - barW) / 2);
      var cursor = plotB;
      var opacity = b.partial ? 0.55 : 1;

      [
        { value: ok, color: OBS_TONE.ok },
        { value: awaiting, color: OBS_TONE.awaiting },
        { value: errors, color: OBS_TONE.error }
      ].forEach(function (layer) {
        if (!layer.value) return;
        // 最小 2px：一格里 1 轮错误在总量 50 的柱子上只有不到 1px，
        // 那正是最需要看见的东西，不能因为量小而消失。
        var h = Math.max(2, (layer.value / top) * plotH);
        cursor -= h;
        bars += '<rect x="' + x + '" y="' + round2(cursor) + '" width="' + round2(barW) +
          '" height="' + round2(h) + '" fill="' + layer.color +
          '" opacity="' + opacity + '"/>';
      });

      // 整列一个透明热区，把这一格的全部数字都放进一个 tooltip：
      // 三段各自太窄，鼠标要点中 3px 的红段基本不可能。
      var end = new Date(b.t);
      var begin = new Date(end.getTime() - bucketHours * 3600000);
      var range = obsClock(begin, bucketHours) + " – " + obsClock(end, bucketHours);
      var tip = range + "（" + (b.partial ? "被窗口起点截断" : bucketHours + " 小时") + "）" +
        "\n轮次 " + traces + " · 正常 " + ok + " · 等人工 " + awaiting + " · 错误 " + errors;
      if (b.p95_duration_ms) tip += "\n耗时 p95 " + fmtSpan(b.p95_duration_ms);

      bars += '<rect x="' + round2(plotL + slot * i) + '" y="' + plotT + '" width="' +
        round2(slot) + '" height="' + plotH + '" fill="transparent">' +
        "<title>" + esc(tip) + "</title></rect>";

      // 横轴标签：抽稀之后最后一格（= 现在）仍要标出来——那是读图时的定位点。
      if (i % every === 0 || i === list.length - 1) {
        labels += '<text x="' + round2(plotL + slot * (i + 0.5)) + '" y="' + (plotB + 15) +
          '" text-anchor="middle" font-size="10.5" fill="#98a09a">' +
          esc(obsClock(end, bucketHours * list.length)) + "</text>";
      }

      if (b.p95_duration_ms) {
        points.push({
          x: plotL + slot * (i + 0.5),
          y: plotB - (b.p95_duration_ms / maxP95) * plotH,
          i: i
        });
      }
    });

    // 折线**分段**连，不跨过没有轮次的格子。
    //
    // 这一条是被真实数据逼出来的：7 天窗口里前 11 格是空的（系统那时还没上线），
    // 一口气连过去，平滑曲线会在空白区画出一个漂亮的「V 形回升」——那几格
    // 一条数据都没有，图上却显示耗时从 12s 降到 3s 又升回去。稀疏数据上的
    // 连续折线不是「趋势」，是插值编出来的故事。
    var line = "";
    if (maxP95 && points.length) {
      var segments = [];
      var run = [];
      points.forEach(function (p) {
        if (run.length && p.i !== run[run.length - 1].i + 1) {
          segments.push(run);
          run = [];
        }
        run.push(p);
      });
      if (run.length) segments.push(run);

      segments.forEach(function (seg) {
        if (seg.length > 1) {
          var pts = seg.map(function (p) { return [p.x, p.y]; });
          // 点少时画直线。Catmull-Rom 在三个点上会明显过冲——真实数据里
          // 24 小时窗只有白天三个格有数，平滑曲线会被拉出一个先冲到 0
          // 再弹回顶端的 V 形，而那条弧线一个真实读数都不对应。
          // 点多了才值得平滑：那时曲线只是把密集的点连顺。
          var d = seg.length <= 4
            ? "M" + pts.map(function (p) { return round2(p[0]) + "," + round2(p[1]); }).join(" L")
            : smoothPath(pts);
          line += '<path d="' + d + '" fill="none" stroke="#6c736e" stroke-width="1.4"' +
            ' stroke-dasharray="4 3" stroke-linejoin="round"/>';
        }
      });
      points.forEach(function (p) {
        // 点画小一号：28 个点铺满一条线时，4px 的圆点会和柱子上边缘混在一起，
        // 看着像每个柱顶都长了疙瘩。它只是「这里有读数」的标记。
        line += '<circle cx="' + round2(p.x) + '" cy="' + round2(p.y) +
          '" r="1.7" fill="#fff" stroke="#8c948e" stroke-width="1.1"/>';
      });
    }

    // 右轴只给两个刻度：它是折线的量纲锚点，不是拿来逐格读数的。
    var rightAxis = maxP95
      ? '<text x="' + (plotR + 7) + '" y="' + (plotT + 4) +
        '" font-size="10" fill="#98a09a">' + esc(fmtSpan(maxP95)) + "</text>" +
        '<text x="' + (plotR + 7) + '" y="' + (plotB + 4) +
        '" font-size="10" fill="#98a09a">0</text>'
      : "";

    return '<svg class="obs-trend" width="' + width + '" height="' + height +
      '" viewBox="0 0 ' + width + " " + height + '" role="img" aria-label="窗口内轮次与耗时走势">' +
      grid + bars + line + labels + rightAxis + "</svg>" +
      '<div class="obs-legend">' +
      '<span><i style="background:' + OBS_TONE.ok + '"></i>正常</span>' +
      '<span><i style="background:' + OBS_TONE.awaiting + '"></i>等人工</span>' +
      '<span><i style="background:' + OBS_TONE.error + '"></i>错误</span>' +
      (maxP95 ? '<span><i class="dash"></i>耗时 p95（右轴，虚线）</span>' : "") +
      // 一格是多久必须写出来：横轴只有时间刻度，看不出两格之间隔了多少，
      // 而「一格 1 小时」和「一格 12 小时」读出来的结论完全不同。
      '<span class="obs-legend-note">每格 ' + bucketHours + " 小时</span>" +
      "</div>";
  }

  /* 横向分布条。用于「意图构成」与「热点工具」这类长尾分类。

     不用饼：分类常有六七个且尾巴很长，饼图切出来的角度没法比大小，
     也没有地方写名字——图例里再对一遍色，读一次要看两处。
     横条可以把名字直接放在条的左边，长度就是比例。 */
  function obsBars(items, opts) {
    opts = opts || {};
    var list = (Array.isArray(items) ? items : []).filter(function (item) {
      return item && item.value !== undefined && item.value !== null;
    });
    // 全是 0 与空数组同理：画一排空槽不如直说这个窗口里没有记录。
    var hasValue = list.some(function (item) {
      return Number(item.value) > 0;
    });
    if (!list.length || !hasValue) {
      return '<div class="obs-empty">' + esc(opts.empty || "窗口内没有记录。") + "</div>";
    }

    var total = 0;
    var max = 1;
    list.forEach(function (item) {
      var v = Number(item.value) || 0;
      total += v;
      if (v > max) max = v;
    });

    return '<ul class="bar-list">' + list.map(function (item) {
      var value = Number(item.value) || 0;
      var width = Math.max(value ? 2 : 0, (value / max) * 100);
      var tone = item.tone || OBS_TONE.ok;
      var share = total ? Math.round((value / total) * 100) : 0;

      // 分段：一段之内还可以再分。工具条用它区分「成功」与「出错」——
      // 一个 14 次调用 2 次出错的工作，整条涂红会读成「这个工具一直失败」，
      // 而它其实 86% 是成的。整条长度仍然是调用次数，红段只是其中最右一小截。
      var parts = (Array.isArray(item.parts) ? item.parts : []).filter(function (part) {
        return part && Number(part.value) > 0;
      });
      // 分段宽度按**外侧**的 max 算，不按 value 算：条子的长度是全组共用的
      // 一把尺，按自身归一的话每条都占满，横条就退化成了一列满格方块。
      var fill = parts.length
        ? parts.map(function (part) {
          return '<i style="width:' + (Math.max(0, (Number(part.value) / max) * 100)).toFixed(1) +
            "%;background:" + (part.color || tone) + '"></i>';
        }).join("")
        : '<i style="width:' + width.toFixed(1) + "%;background:" + tone + '"></i>';

      var tip = (item.label || "") + "：" + fmtNum(value) +
        (item.note ? "，" + item.note : "");
      return '<li class="bar-row" title="' + esc(tip) + '">' +
        '<span class="bar-label">' + esc(item.label || "—") + "</span>" +
        '<span class="bar-track">' + fill + "</span>" +
        '<span class="bar-value">' + esc(fmtNum(value)) +
        (opts.share === false ? "" : ' <em>' + share + "%</em>") + "</span></li>";
    }).join("") + "</ul>";
  }

  global.PAPRender = {
    rich: rich,
    chart: chartCard,
    sanitizeSvg: sanitizeSvg,
    auditMeta: auditMeta,
    approvalBody: approvalBody,
    statusChip: statusChip,
    ticketCard: ticketCard,
    textBlocks: textBlocks,
    liveStrip: liveStrip,
    topologyView: topologyView,
    hasDelegation: hasDelegation,
    swimlanes: swimlanes,
    operationView: operationView,
    contextNote: contextNote,
    foldSeam: foldSeam,
    obsTrend: obsTrend,
    obsBars: obsBars,
    inline: inline
  };
})(typeof window === "undefined" ? globalThis : window);
