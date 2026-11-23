# tests/unit/test_build_invariants.py
"""建流程这条路上两条口径，以及记忆不该记什么。

来源是一次线上现象：用户 `/clear` 之后说一句「压力高了先查规程再决定关不关阀」，
构建助手回了一段散文——「这条处置顺序已经记下来了……对应的编排流程 check_pressure
之前也定过……要我重新起草一版，还是直接保存？」——图一张没交，还反问了一句。

三处都要修，缺一处就能重犯：

1. 助手把「起草」当成要用户点头的事。起草不写库、不弹卡，问了只是白等一轮；
2. 助手说「之前定过」时没有依据。那次库里 `workflows` 是 0 行（12:43 那张图在
   确认卡上被取消），它复述的是长期记忆里的一条「决策」；
3. 那条记忆本身是错抽取：12:36 助手回答里的「这张图能存。步骤：A→B→C」被记成了
   「高压处置编排流程为 A→B→C」。平台里有什么是可查的事实，不该进记忆。
"""

import re

from app.agents.prompts import CHAT_SYSTEM_PROMPT
from app.services.agent_service import MAX_INSTRUCTION_CHARS, STUDIO_INSTRUCTION
from app.services.memory_extract import EXTRACT_SYSTEM_PROMPT
from app.tools.builtins.studio_admin_tools import build_studio_admin_tools


def _flat(text: str) -> str:
    """去掉所有空白再比对。

    这些句子在提示词里是手折的，断行位置会随编辑变动（「缺站点、缺读数」与
    「也不是理由」可能被折到两行）。按补丁式地钉住断行处，改一次排版就红一次，
    而真正要钉住的是「有没有这句话」。
    """
    return re.sub(r"\s+", "", text)


def _draft_description() -> str:
    for spec in build_studio_admin_tools():
        if spec.name == "studio_workflow_draft":
            return spec.description
    raise AssertionError("studio_workflow_draft 不在工具清单里")


def test_draft_is_never_asked_about() -> None:
    # 「要不要我起草」与「给你看还是直接存」都要被点名禁掉：模型对具体句式比抽象规则敏感。
    assert "「要不要我起草」这句不问" in _flat(STUDIO_INSTRUCTION)
    assert "还是直接保存" in _flat(STUDIO_INSTRUCTION)


def test_tool_description_carries_the_same_rule() -> None:
    # 模型对工具描述的服从度高于系统提示：同一句话必须两份都在。
    assert "不要先问「要不要我起草一版」" in _flat(_draft_description())


def test_missing_site_is_not_a_reason_to_ask() -> None:
    # 第二轮线上：它没问「要不要起草」，改成问「你没说哪个站」——同一件事换了马甲。
    # 起草一张图用不着现场数据，查询步骤的站点是运行时的 {{input.text}}。
    assert "缺站点、缺读数也不是理由" in _flat(STUDIO_INSTRUCTION)
    assert "缺站点、缺读数也照交" in _flat(_draft_description())


def test_two_steps_are_a_graph_even_for_one_agent() -> None:
    # 第三轮线上：它绕到了另一条退路上——「本身只有一步判断，没有接力，所以不起草」。
    # 判据要写死：有两个以上动作、有先后或分流就是图，都由同一个智能体做也算。
    flat = _flat(STUDIO_INSTRUCTION)
    assert "有两个以上动作、有先后或分流就是图" in flat
    # 读过一次、库里没有，就不许再拿记忆里的步骤当这张图。
    assert "读不到就是没有" in flat


def test_empty_list_is_a_signal_not_an_answer() -> None:
    # 第四轮线上：它查完流程库（0 张），回了一句「当前租户还没有任何编排流程。
    # 新建用 studio_workflow_save mode=create……」。空清单被当成了结论。
    assert "空不是结论" in _flat(_draft_description())


def test_svg_block_is_not_a_substitute_for_the_graph() -> None:
    # 第五轮线上：它改成了在回答里手画 ```svg 流程图——看着像交图，其实库里什么都没有，
    # 也没法点保存。基础提示词里「流程用 svg 画」是对普通问答说的，这里必须被顶掉。
    flat = _flat(_draft_description())
    assert "```svg" in flat and "只是示意图" in flat


def test_existing_workflow_must_be_read_not_remembered() -> None:
    assert "说「之前定过」之前先查一眼" in _flat(STUDIO_INSTRUCTION)
    # 查哪个资源要写死，否则「查一眼」会被理解成查记忆。
    flat = _flat(STUDIO_INSTRUCTION)
    assert "studio_describe" in flat and "resource=workflow" in flat


def test_memory_extraction_refuses_platform_state() -> None:
    assert "平台里现在有什么" in EXTRACT_SYSTEM_PROMPT
    # 起草 / 待审 / 取消都不算「已定」——这条正是那次错记忆的入口。
    assert "不算「已定」" in EXTRACT_SYSTEM_PROMPT
    assert "库里查得到吗" in EXTRACT_SYSTEM_PROMPT


def test_memory_extraction_still_keeps_its_old_guards() -> None:
    # 加规则不能顶掉原有的几条：密钥、无信息量的概括、重复条目。
    for phrase in ("密钥", "没有信息量的概括", "known_memory_titles"):
        assert phrase in EXTRACT_SYSTEM_PROMPT


def test_chat_prompt_still_hands_off_instead_of_planning() -> None:
    # 业务助手那一侧的口径不变：不替用户排步骤，提示 `/build`。
    assert "/build" in CHAT_SYSTEM_PROMPT
    assert "不要在这里替他排步骤" in CHAT_SYSTEM_PROMPT


def test_studio_instruction_fits_the_editable_limit() -> None:
    # 这段是写进 agents.instruction 的正文：超过上限时，管理员在「补充指令」里
    # 点一次保存就会被校验拒掉，而界面上那份看起来完全正常。
    assert len(STUDIO_INSTRUCTION) <= MAX_INSTRUCTION_CHARS
