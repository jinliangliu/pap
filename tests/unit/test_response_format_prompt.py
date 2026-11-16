# tests/unit/test_response_format_prompt.py
"""系统提示词里的排版契约。

前端把 Markdown 表格 / ```chart / ```svg 渲染成图形，这份约定写在
SYSTEM_PROMPT 里。这里锁住关键字段名：提示词改动时如果丢了某种
块类型，模型就不会再产出对应图形，前端渲染器等于白写。
"""

from app.agents.prompts import SYSTEM_PROMPT


def test_prompt_states_the_three_rendered_blocks() -> None:
    assert "Markdown 表格" in SYSTEM_PROMPT
    assert "```chart" in SYSTEM_PROMPT
    assert "```svg" in SYSTEM_PROMPT


def test_chart_contract_is_complete() -> None:
    # type/x/series/data 是前端解析的字段名，写错一个图就是空的
    for field in ("line", "bar", '"type"', '"x"', '"series"', '"data"'):
        assert field in SYSTEM_PROMPT


def test_svg_contract_forbids_active_content() -> None:
    assert "<foreignObject>" in SYSTEM_PROMPT
    assert "<script>" in SYSTEM_PROMPT
