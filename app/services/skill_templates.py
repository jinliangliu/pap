# app/services/skill_templates.py
"""内置示例技能：模板在代码里，不在库里。

为什么不做成「每个租户预置两行 `source='builtin'`」：
租户是动态建的，预置要么挂在建租户的流程里（漏了就没数据），
要么每次升级补一遍（补了就可能覆盖用户改过的同名技能）。
做成模板最省事——它不是一个技能，而是一份**可以复制的起点**：

- 用户点「从模板新建」才真正落一行，`source` 记 `template`，之后随便改；
- 没有可删的东西，自然也就满足了「可复制不可删」。

工具白名单只写平台确实有的工具；写错的会被交集静默丢弃，
页面标「不可用」，所以这里的名字必须跟着注册表走。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillTemplate:
    key: str
    name: str
    display_name: str
    description: str
    trigger_keywords: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    priority: int
    instruction: str


PATROL_COMPARE = SkillTemplate(
    key="patrol-compare",
    name="patrol-compare",
    display_name="巡检比对",
    description="把本轮巡检读数与基准值、上一次读数对比，标出越限与漂移，产出可核对的结论。",
    trigger_keywords=("巡检比对", "对比巡检", "核对巡检数据"),
    allowed_tools=(
        "fc_liststations",
        "fc_getstationoverview",
        "fc_querypressure",
        "fc_querypressuretrend",
        "fc_queryalarms",
        "fc_getstationprofile",
    ),
    priority=80,
    instruction="""## 比对口径

一次巡检要回答三个问题，缺一个都不算完成：

1. **现在多少** —— 用 `fc_querypressure` 取实时读数，并写明采样时间。
2. **和谁比** —— 优先和用户给的基准值比；没给基准就取 `fc_querypressuretrend` 里上一次读数。
   两条都没有时，明确说「没有可比基准」，不要自己编一个「正常范围」。
3. **差多少算异常** —— 压力偏差超过 10% 或报警通道有未处理项，判为需关注；否则为正常。

## 顺序

1. 站点不确定先调 `fc_liststations` 取准确标识。
2. `fc_getstationoverview` 拿站点整体状态，`fc_querypressure` 拿进出口压力。
3. 需要看趋势时才调 `fc_querypressuretrend`，别每轮都拉一天的数据。
4. `fc_queryalarms` 检查同期报警，有报警时优先呈现报警而不是压力。
5. 需要设备档案（量程、调压器型号）时调 `fc_getstationprofile`。

## 输出格式

先给一句结论（正常 / 需关注 / 数据不足），再用三行表格给出「项目 / 当前值 / 基准值 / 偏差」。
最后一行写清数据时间与来源工具。没有读到的项写「未取到」，不要留空，也不要估算。

不要复述工具返回的原始 JSON；现场看的是结论，不是报文。""",
)


REPORT_FORMAT = SkillTemplate(
    key="report-format",
    name="report-format",
    display_name="报表排版",
    description="把查询结果整理成固定口径的报表：表头、单位、时间范围、异常标注，必要时导出文件。",
    trigger_keywords=("出报表", "整理报表", "报表排版", "导出报表"),
    allowed_tools=(
        "fc_queryreliefreport",
        "fc_queryreliefsamples",
        "fc_exportstationrecords",
        "fc_downloadreportpdf",
        "get_platform_time",
    ),
    priority=80,
    instruction="""## 报表必须包含的四件事

1. **标题与站点**：报表名 + 站点标识，两者都不能省。
2. **时间范围**：写清起止时刻和时区。范围来自用户；用户没说就默认最近一天，
   并在报表里注明「默认取最近 24 小时」。
3. **单位**：压力 kPa、放散量 m³，逐列标在表头里，不要只在正文提一次。
4. **异常标注**：超出量程或缺失的单元格标「—」并脚注原因，不要填 0。

## 顺序

1. `fc_queryreliefreport` 取汇总，`fc_queryreliefsamples` 取明细；只取需要的范围。
2. 用户要文件时才调 `fc_exportstationrecords` 或 `fc_downloadreportpdf`，
   并回一个可点击的结果；用户只要看数就不要导出。
3. 报表落款时间用 `get_platform_time` 取，不要用你推断的日期。

## 输出格式

聊天里先给汇总表（不超过 8 列、20 行），明细放下载文件里。
数字右对齐，小数位全表统一（压力 2 位、放散量 2 位）。
最后一行写数据来源与生成时间。

不要把工具报错当成「没有数据」：取数失败要写「取数失败 + 原因」，
否则这份报表会被当成「当天零放散」。""",
)


TEMPLATES: tuple[SkillTemplate, ...] = (PATROL_COMPARE, REPORT_FORMAT)


def list_templates() -> list[SkillTemplate]:
    return list(TEMPLATES)


def get_template(key: str) -> SkillTemplate | None:
    for template in TEMPLATES:
        if template.key == key:
            return template
    return None


__all__ = ["SkillTemplate", "TEMPLATES", "get_template", "list_templates"]
