# app/knowledge/synonyms.py
"""查询侧领域同义词扩展。

这是**廉价补丁，不是语义检索**：只覆盖人工登记过的词对，救不了没登记的说法。
索引侧绝不调用——把同义词灌进语料会让 avgdl、IDF 与长度惩罚全部失真，
还会让「命中」变成「命中了一个我们自己塞进去的词」。

只作用在查询侧，且默认关闭（``KNOWLEDGE_QUERY_EXPANSION_ENABLED=false``）。
开启后召回池会变宽、也可能带进更多假阳性，由语义重筛在下游剔除；
效果要用 ``scripts/eval_knowledge.py`` 的 A/B 对照量出来，不能凭感觉开。
"""

from __future__ import annotations

# 整句/口语化说法 → 资料里实际使用的词。这类问题与语料往往零字面交集，
# 是 BM25 召回上限最典型的表现。
PHRASE_ALIASES: dict[str, tuple[str, ...]] = {
    "你会做什么": ("平台能力", "功能清单", "平台用途"),
    "你能做什么": ("平台能力", "功能清单", "平台用途"),
    "能做什么": ("平台能力", "功能清单", "平台用途"),
    "描述一下你的能力": ("平台能力", "功能清单"),
    "系统有什么功能": ("平台能力", "功能清单", "平台用途"),
}

# 领域术语 → 同一份资料里可能采用的另一种写法。
TERM_SYNONYMS: dict[str, tuple[str, ...]] = {
    "压力阈值": ("报警值", "设定值", "上下限", "阈值"),
    "审批有效期": ("有效期", "300秒", "五分钟"),
    "密钥": ("APIKey", "Token", "私钥", "凭据"),
    "停机": ("停机规程", "阀位反馈", "确认步骤"),
    "能力": ("功能", "用途", "平台能力"),
    "混合检索": ("BM25", "向量检索", "RRF"),
    "索引": ("chunks.json", "重建索引"),
    "资料不足": ("未找到足够依据", "不编造答案"),
}


def expansion_terms(query: str) -> list[str]:
    """返回该查询要追加的词，保持登记顺序、去重，不含原查询本身。"""
    normalized = query.lower().replace(" ", "")
    terms: list[str] = []
    seen: set[str] = set()

    for table in (PHRASE_ALIASES, TERM_SYNONYMS):
        for key, values in table.items():
            if key.lower() not in normalized:
                continue
            for value in values:
                if value not in seen:
                    seen.add(value)
                    terms.append(value)

    return terms


def expand_query(query: str) -> str:
    """同义词以空格分隔追加在原查询后面，避免中文词直接粘连被 jieba 切错。

    没有登记词时原样返回，调用方可以据此判断「这次扩展有没有实际发生」。
    """
    terms = expansion_terms(query)
    return f"{query} {' '.join(terms)}" if terms else query
