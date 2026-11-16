# app/agents/clarify.py
"""把模型给的分项追问收敛成界面可直接渲染的形状。

为什么要服务端再洗一遍：`clarify_items` 是**模型输出**，它会出现在控制台中
每一个用户的气泡里、并随消息落进 `messages.metadata`。模型给的条数、长度、
重复项都不受控——真出现一条 3000 字的「问题」或四十个选项，界面会被撑爆，
库里也会留一份永远没人看的长文本。

这里的取舍只有三条：

1. **只删不编**：没有候选的项不会被编出候选，只会保留成一个待填的输入框。
2. **超限就截断，不丢整项**：宁可少几个字，也不要让用户看到「有一条被吞了」。
3. **字段名只认白名单**：`field` 由模型填写，若不在本轮 `missing_fields` 里就清空。
   它是给「这条在补哪个槽」做标注的，乱填会让后续补参落到错误的字段上。
"""

from __future__ import annotations

from typing import Any, Iterable

# 与 `ActionPlan.clarify_items` 的描述、system prompt 里的数字保持一致。
MAX_ITEMS = 3
MAX_OPTIONS = 6
MAX_QUESTION_CHARS = 180
MAX_LABEL_CHARS = 60
MAX_VALUE_CHARS = 400


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _as_dict(item: Any) -> dict[str, Any] | None:
    """统一成 dict。

    调用方既可能给 `ActionPlan` 里的 pydantic 对象，也可能给 checkpoint
    反序列化出来的 dict——两条路都得认。只认 dict 的话，pydantic 那侧的
    每一项都会被当成「非字符串」静默丢掉，界面上一张卡都没有，而正文还在说
    「请在下面选一项」。
    """
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except Exception:
            return None
        return data if isinstance(data, dict) else None
    return None


def _options(raw: Any) -> list[dict[str, str]]:
    """候选答案：去空、去重（按 label）、限量。

    去重按 label 而不是 value：界面上用户看到的是 label，两条一样的字
    点了却是两个不同答案，是没人能解释的行为。
    """
    items: Iterable[Any] = raw if isinstance(raw, list) else []
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in items:
        if len(out) >= MAX_OPTIONS:
            break

        data = _as_dict(item)
        if data is not None:
            label = _text(data.get("label"), MAX_LABEL_CHARS)
            value = _text(data.get("value"), MAX_VALUE_CHARS)
        else:
            # 模型偶尔把选项写成纯字符串数组，这不是错误，按 label 收下。
            label = _text(item, MAX_LABEL_CHARS)
            value = ""

        if not label or label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "value": value or label})

    return out


def normalize_items(
    raw: Any,
    *,
    allowed_fields: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """返回 `[{question, field, options: [{label, value}]}]`，空输入返回 `[]`。

    `allowed_fields` 传 `None` 表示不做字段白名单校验。
    """
    if not isinstance(raw, list):
        return []

    allowed = None if allowed_fields is None else {str(f) for f in allowed_fields}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in raw:
        if len(out) >= MAX_ITEMS:
            break

        data = _as_dict(item)
        if data is not None:
            question = _text(data.get("question"), MAX_QUESTION_CHARS)
            field = _text(data.get("field"), 64) or None
            options = _options(data.get("options"))
        else:
            question = _text(item, MAX_QUESTION_CHARS)
            field = None
            options = []

        if not question or question in seen:
            continue
        if field and allowed is not None and field not in allowed:
            field = None

        seen.add(question)
        out.append({"question": question, "field": field, "options": options})

    return out


def has_choices(items: list[dict[str, Any]]) -> bool:
    """是否至少有一项带候选。

    没有候选时前端只剩一排输入框——那种情况下正文必须自己把问题说清楚，
    所以正文的处理方式要跟着这个结果走。
    """
    return any(item.get("options") for item in items)
