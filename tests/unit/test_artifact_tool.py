# tests/unit/test_artifact_tool.py
"""`save_artifact`：把生成的内容落成产物。

这个工具存在的理由只有一个——「上一轮生成的那张表」原来没有实体。
所以这组用例守的也不是「能存文件」，而是**存下来的东西能不能被找到、
能不能被限定在一个会话里**：

- 会话是硬条件。没有会话就不落库，宁可拒也不留无主产物；
- 后缀白名单而不是通用二进制：产物是给人看的结论，不是可执行内容；
- 文件名里的路径要被剥掉，否则对象键会绕出用户目录。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.models.approval import RiskLevel
from app.models.artifact import Artifact
from app.models.audit import AuditEventType, AuditLog
from app.storage.object_store import get_object_store
from app.tenancy.context import tenant_scope
from app.tools.builtins import artifact_tools
from app.tools.types import ToolExecutionContext

TENANT = "default"
USER = "u-1"
CONV = "conv-1"


@pytest.fixture(autouse=True)
async def _isolated(session, seeded_identity, tmp_path, monkeypatch):
    """对象写到临时目录，元数据清干净。

    不隔离的话每次跑测试都会往 `./data/artifacts` 里堆文件，
    而没人会去数那里多了几个——直到某天有人问「这些是谁的」。
    """
    monkeypatch.setattr("app.storage.object_store._store._root", tmp_path)

    async def _clean() -> None:
        await session.execute(delete(Artifact))
        # 审计也要清：不清的话上一个用例的那条会算进下一个，
        # 于是「写了几条」这类断言变成「前面一共写了几条」。
        await session.execute(
            delete(AuditLog).where(
                AuditLog.event_type == AuditEventType.ARTIFACT_CREATED
            )
        )
        await session.commit()

    await _clean()
    yield
    await _clean()


def _ctx(**over) -> ToolExecutionContext:
    base = dict(
        tenant_id=TENANT,
        user_id=USER,
        thread_id="t-1",
        conversation_id=CONV,
        run_id="r-1",
        request_id="req-1",
        roles=("tester",),
        scopes=("tools:read",),
        agent_name="cs",
        agent_revision=1,
        agent_allowed_tools=None,
        agent_can_write=True,
    )
    base.update(over)
    return ToolExecutionContext(**base)


async def _row(artifact_id: str) -> Artifact | None:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            return await db.get(Artifact, uuid.UUID(artifact_id))


async def _audits() -> list[AuditLog]:
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            return list(
                (
                    await db.scalars(
                        select(AuditLog).where(
                            AuditLog.event_type == AuditEventType.ARTIFACT_CREATED
                        )
                    )
                ).all()
            )


def test_tool_is_actually_registered():
    """工具写了不注册，等于没写。

    这类漏法在单测里完全看不出来：函数能调、行为都对，但模型永远看不到它。
    所以这里直接问注册表，而不是问「函数实现对不对」。
    """
    from app.tools.dependencies import get_tool_gateway

    assert get_tool_gateway().has_tool("save_artifact")


# ---------------------------------------------------------------- 成功路径


async def test_saves_a_text_artifact_bound_to_the_conversation():
    result = await artifact_tools.save_artifact(
        _ctx(),
        {"filename": "巡检报告.md", "content": "# 今日巡检\n\n一切正常。\n"},
    )

    assert result.success and result.verified
    artifact_id = result.result["artifact_id"]

    row = await _row(artifact_id)
    assert row is not None
    # 会话这一列是「按会话保留」的全部依据，写错一位整条策略就失效。
    assert row.conversation_id == CONV
    assert row.user_id == USER
    assert row.content_type == "text/markdown"
    assert row.filename == "巡检报告.md"

    # 元数据与对象都要在：只有行没文件，下载时才暴露成「附件内容不存在」。
    raw = await get_object_store().read(row.object_key)
    assert raw.decode("utf-8").startswith("# 今日巡检")


async def test_saving_is_audited_as_created_not_uploaded():
    """与用户上传分开记。混成一个事件，事后分不清这份文件是谁放进来的。"""
    await artifact_tools.save_artifact(
        _ctx(), {"filename": "a.csv", "content": "a,b\n1,2\n"}
    )

    rows = await _audits()
    assert len(rows) == 1
    assert rows[0].metadata_json["via"] == "save_artifact"
    assert rows[0].metadata_json["conversation_id"] == CONV
    assert rows[0].metadata_json["agent_name"] == "cs"


async def test_two_saves_of_the_same_name_are_two_artifacts():
    """同名不覆盖。

    覆盖会改掉第一次那个 id 指向的内容——而那个 id 可能已经被引用进
    某张审批单或某次委派。多一份版本比悄悄换掉内容容易解释。
    """
    first = await artifact_tools.save_artifact(
        _ctx(), {"filename": "报表.csv", "content": "v1\n"}
    )
    second = await artifact_tools.save_artifact(
        _ctx(), {"filename": "报表.csv", "content": "v2\n"}
    )

    assert first.result["artifact_id"] != second.result["artifact_id"]

    one = await _row(first.result["artifact_id"])
    two = await _row(second.result["artifact_id"])
    assert one is not None and two is not None
    assert await get_object_store().read(one.object_key) == b"v1\n"
    assert await get_object_store().read(two.object_key) == b"v2\n"


def test_tool_is_not_classified_as_a_write_operation():
    """只读 Agent 要能存产物，所以这条工具不能算写操作。

    `can_write` 管的是「能不能改外部系统」，而保存产物只是把已经生成的
    文本落到自己的目录里，外面什么都没变。把它归到 `_WRITE_RISKS`，
    客服这类纯只读 Agent 就再也交不出报告了——而交报告正是它们的产出。

    这里必须断言**工具定义**而不是调一次 handler：handler 不看
    `agent_can_write`，直接调它永远返回成功，那种用例改坏了也不会红
    （`gateway` 才是在 `risk_level` 上拦人的那一层）。
    """
    tool = artifact_tools.build_artifact_tools()[0]

    assert tool.risk_level is RiskLevel.NORMAL
    assert tool.requires_approval is False
    assert tool.name == "save_artifact"


# ---------------------------------------------------------------- 拒绝路径


async def test_refuses_without_a_conversation():
    """没有会话就不落库。

    落了的话它是一条「不属于任何一场对话」的产物：列表里分不清是谁的，
    会话删除也带走不了它——清理时没有任何依据判断它该不该留。
    """
    result = await artifact_tools.save_artifact(
        _ctx(conversation_id=""),
        {"filename": "a.md", "content": "内容"},
    )

    assert not result.success
    assert result.error_code == "ARTIFACT_NO_CONVERSATION"
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            rows = (await db.scalars(select(Artifact))).all()
    assert rows == []


async def test_refuses_empty_content():
    result = await artifact_tools.save_artifact(
        _ctx(), {"filename": "a.md", "content": "   \n\t "}
    )
    assert result.error_code == "ARTIFACT_EMPTY"


@pytest.mark.parametrize("name", ["report.html", "run.sh", "payload.exe", "noext"])
async def test_refuses_suffixes_that_are_not_text(name):
    """不给通用二进制，也不给任何能在浏览器里被解释的类型。

    上游的上传接口已经把 HTML/SVG 强制成下载，这里不能开个新口子；
    而让模型自填 `content_type` 等于把「下载时怎么解释」交给它决定。
    """
    result = await artifact_tools.save_artifact(
        _ctx(), {"filename": name, "content": "<script>alert(1)</script>"}
    )

    assert result.error_code == "ARTIFACT_TYPE_UNSUPPORTED"
    # 文案要直接说出能用哪些后缀，否则模型会换个后缀再试一次。
    assert ".md" in (result.message or "")


async def test_refuses_oversized_content(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.artifact_max_bytes", 64)

    result = await artifact_tools.save_artifact(
        _ctx(), {"filename": "big.md", "content": "x" * 200}
    )

    assert result.error_code == "ARTIFACT_TOO_LARGE"
    # 说清超了多少，才可能拆成几份。
    assert "KB" in (result.message or "")
    async with async_session_factory() as db:
        with tenant_scope(TENANT):
            assert (await db.scalars(select(Artifact))).all() == []


async def test_filename_cannot_escape_the_object_root():
    """文件名里的路径要被剥掉。

    对象键由服务端生成，但文件名参与后缀与展示，不剥的话
    `../../x.md` 这种会先被 `safe_filename` 处理——这条钉住它确实生效。
    """
    result = await artifact_tools.save_artifact(
        _ctx(), {"filename": "../../etc/passwd.md", "content": "x"}
    )

    assert result.success
    row = await _row(result.result["artifact_id"])
    assert row is not None
    assert row.filename == "passwd.md"
    assert ".." not in row.object_key
