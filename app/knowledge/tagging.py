# app/knowledge/tagging.py
"""元数据打标：标准号、发布年份、文档类型、领域主题词。

**为什么单独一个模块**

这些标签有三个用处，都不能靠「入库时顺手拼一下」：

1. 检索引用要显示「GB/T 3836.1-2021 · 5.1.6 过滤精度」，光有小节标题不够；
2. 知识老化判定要知道「这份是 2011 版」，同标准号的多个版本并存时要能认出旧的；
3. 治理页要能按文档类型、主题分组看覆盖。

抽取规则必须是**纯函数**：入库时写进切片元数据，已入库的老文档则在读取时
现算——库里现有的十几份文档是在这个模块之前入库的，不可能为了补标签全部重建。
纯函数让两条路径用同一份规则，不会出现「新传的有标签、老的不但有还没有」。

**只从固定词表挑主题词，不做自由抽取**

自由抽词（tf-idf、TextRank）在 13 份同类技术标准上抽出来的全是
「规定」「要求」「应」这类词，看着有标签、实则区分不了任何东西。
词表法会漏，但漏了的代价是「少一个标签」，抽错的代价是「按标签筛出错误集合」。
宁可少。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 标准号：前缀 + 编号 + 可选年份。中文技术资料里这四种写法都有：
# 「GB 27791-2020」「GB/T 3836.1-2021」「T/CGAS 007-2019」「ISO 9001:2015」。
#
# 编号只允许「点分」（3836.1），**不允许短横线**：GB/T 3836.1-2021 里的
# `-2021` 是年份，若把它算进编号，年份那一组永远匹配不到，同一份标准的
# 2020 版与 2011 版就会被当成两个不同编号、无法归并出「多版本并存」。
# 年份限定 19xx/20xx，避免把 `GB 50028-1` 这种分部编号误当年份。
# `TCGAS`（无斜杠）是同一批团体标准的另一种写法：有的资料封面写 `T/CGAS 027-2023`，
# 有的文件名写 `TCGAS 027-2023`。不认它，这类文档就没有 standard_base，
# 同标准的两个版本永远归并不上——而多版本并存正是老化检测最要紧的一条。
_STANDARD = re.compile(
    r"(?<![A-Za-z])"
    r"(GB/T|GBZ|GB|T/CGAS|TCGAS|CGAS|SY/T|SY|AQ|JB/T|JB|HG/T|DL/T|TSG|NB/T|CJJ|CJ/T|ISO|IEC|EN)"
    r"\s*"
    r"(\d{1,5}(?:\.\d{1,4})*)"
    r"(?:\s*[-—–:]\s*((?:19|20)\d{2}))?"
    r"(?![0-9])"
)

# 写法归一：抽出来的前缀统一成规范写法，`standard_base` 才能跨文档比对。
_PREFIX_ALIASES = {"TCGAS": "T/CGAS"}

# 全角转半角只处理这几个符号：标准号里真正会出现的。整段 NFKC 会改变
# 正文里全角标点的形态，进而改变内容哈希——一份已经入过库的资料重传时
# 会被判成「内容变了」，白重建一次。
_FULLWIDTH = str.maketrans({"／": "/", "－": "-", "—": "-", "　": " "})

# 文档类型。顺序有意义：先匹配到的优先，「安全技术规范」不该被判成「规范」
# 之外还兼一个「技术」——所以只取第一个。
_DOCTYPES: tuple[tuple[str, str], ...] = (
    ("强制性国家标准", "标准"),
    ("推荐性国家标准", "标准"),
    ("国家标准", "标准"),
    ("行业标准", "标准"),
    ("团体标准", "标准"),
    ("企业标准", "标准"),
    ("技术规程", "规程"),
    ("操作规程", "规程"),
    ("安全规程", "规程"),
    ("规程", "规程"),
    ("技术规范", "规范"),
    ("规范", "规范"),
    ("手册", "手册"),
    ("指南", "指南"),
    ("说明书", "说明"),
    ("使用说明", "说明"),
    ("作业指导书", "指导书"),
    ("台账", "台账"),
    ("清单", "清单"),
    ("制度", "制度"),
    ("办法", "办法"),
    ("报告", "报告"),
    ("方案", "方案"),
)

# 领域主题词。这批语料的实际主题集中在城镇燃气与调压站，别往里塞
# 「人工智能」「区块链」这类与语料无关的词——词表越大，误标的可能越多。
_TOPICS: tuple[str, ...] = (
    "燃气",
    "调压",
    "调压箱",
    "调压站",
    "阀门",
    "切断",
    "放散",
    "泄漏",
    "报警",
    "防爆",
    "压力",
    "管道",
    "过滤器",
    "计量",
    "检修",
    "维护",
    "巡检",
    "作业",
    "通风",
    "安全",
    "应急",
    "置换",
    "投产",
    "停气",
    "防雷",
    "接地",
)

MAX_TAGS = 8
# 主题词只在标题与正文开头找。一份 448 片的标准全篇都提到「压力」，
# 拿全文统计等于给每份文档打同一套标签，反而失去区分度。
TOPIC_WINDOW = 2000


@dataclass(frozen=True)
class DocTags:
    """一份文档的标签。字段名与写进 JSON 的键一致，便于直接展开。"""

    tags: list[str] = field(default_factory=list)
    # 「GB/T 3836.1-2021」。没识别出来就是空串。
    standard: str = ""
    # 「GB/T 3836.1」。同一标准的多个版本靠它归并。
    standard_base: str = ""
    # 发布/版本年份。空表示原文没写，别拿文件修改时间顶替——
    # 「2026 年上传了一份 2011 年的标准」是两件事。
    year: int | None = None
    doctype: str = ""
    topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "tags": list(self.tags),
            "standard": self.standard,
            "standard_base": self.standard_base,
            "year": self.year,
            "doctype": self.doctype,
            "topics": list(self.topics),
        }


def _normalize(text: str) -> str:
    return (text or "").translate(_FULLWIDTH)


def find_standards(text: str) -> list[tuple[str, str, str, int | None]]:
    """找出文中所有标准号，返回 (全称, 前缀, 编号, 年份)。

    全称统一成「前缀 编号-年份」，中间一个空格：原文里「GB/T3836.1—2021」
    这类混写很常见，不归一化的话同一份标准在不同文档里是三个不同的串。
    """
    found: list[tuple[str, str, str, int | None]] = []
    seen: set[str] = set()

    for match in _STANDARD.finditer(_normalize(text)):
        prefix, number, year_raw = match.group(1), match.group(2), match.group(3)
        prefix = _PREFIX_ALIASES.get(prefix, prefix)
        number = number.replace(" ", "")

        # 年份没写在标准号里时，从编号尾部猜不可靠（「3836.1」不是年份），留空。
        year = int(year_raw) if year_raw else None
        full = f"{prefix} {number}-{year}" if year else f"{prefix} {number}"

        if full in seen:
            continue

        seen.add(full)
        found.append((full, prefix, number, year))

    return found


def _doctype_of(text: str) -> str:
    for keyword, name in _DOCTYPES:
        if keyword in text:
            return name
    return ""


def _topics_of(text: str) -> list[str]:
    return [topic for topic in _TOPICS if topic in text][:4]


def analyze(title: str, text: str = "", *, filename: str | None = None) -> DocTags:
    """从标题与正文开头打标。

    标题权重高于正文：标题里出现的标准号才是「这份资料是哪份标准」，
    正文里的标准号往往是引用别人（一份规程会引用七八份国标），
    把它当自己的编号会让两份不同文档抢同一个标准号。
    """
    heading = _normalize(" ".join(part for part in (title, filename or "") if part))
    body = _normalize((text or "")[:TOPIC_WINDOW])

    standards = find_standards(heading)

    if not standards:
        # 标题没写标准号时才退回正文：正文第一处出现的位置通常就是这份资料的
        # 自我介绍（「本文件按照 GB/T 1.1—2020 起草」是反例，所以只取第一处、
        # 且要求带年份）。不带年份的正文标准号一律不认——引用别人的概率太高。
        standards = [item for item in find_standards(body) if item[3]][:1]

    standard = standards[0][0] if standards else ""
    base = f"{standards[0][1]} {standards[0][2]}" if standards else ""
    year = standards[0][3] if standards else None

    doctype = _doctype_of(heading) or _doctype_of(body)
    topics = _topics_of(heading) or _topics_of(body)

    tags: list[str] = []

    for value in (standard, doctype, *topics):
        if value and value not in tags:
            tags.append(value)

    return DocTags(
        tags=tags[:MAX_TAGS],
        standard=standard,
        standard_base=base,
        year=year,
        doctype=doctype,
        topics=topics,
    )
