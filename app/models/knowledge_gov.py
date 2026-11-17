# app/models/knowledge_gov.py
"""知识治理：使用反馈、评测用例、评测运行。

三张表回答三个不同的问题，不要合并成一张「知识质量表」：

- ``knowledge_feedback`` —— **这一次回答**好不好。事实来自用户，一条回答一行，
  只追加不修改（除了「是否已处理」）。
- ``knowledge_eval_cases`` —— **这一类问题**应该命中哪份资料。这是被维护的
  标注集，会随语料变化而增减，所以可以改、可以停用。
- ``knowledge_eval_runs`` —— 在上面那份标注集上跑出来的一次结果快照。
  指标一旦落库就不能变，否则「上个月召回 62%」这句话会随着重跑而漂移。

反馈与用例之间用 ``case_id`` 单向往来：bad case 可以升格成评测用例，
但删掉用例不该把那条反馈一起抹掉——反馈是一次真实用户遇到的事故记录。

``verdict`` 用字符串而不是 Enum：库里已有 `document_status` 这类
`native_enum=False` 的枚举，持久化的是成员名（大写），出口还得自己转一层。
这一列只有两个值、没有扩展计划，用字符串 + 服务层校验更省事。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped

# 反馈的两种取值。不做 Enum，理由见模块注释。
VERDICT_UP = "up"
VERDICT_DOWN = "down"
VALID_VERDICTS = frozenset({VERDICT_UP, VERDICT_DOWN})

# 踩的常见原因。前端给选项、后端只做白名单校验，不接受任意字符串：
# 这些值是后面做统计的维度，自由文本没法聚合。
DOWN_REASONS = (
    "not_relevant",  # 答非所问
    "wrong_citation",  # 引错资料
    "outdated",  # 内容过期
    "incomplete",  # 没答全
    "not_found_but_had",  # 库里明明有却说没有
    "other",
)
VALID_REASONS = frozenset(DOWN_REASONS)

# 用例来源，用来区分「人工写的」和「从事故捞出来的」。
CASE_SOURCE_MANUAL = "manual"
CASE_SOURCE_FEEDBACK = "feedback"
CASE_SOURCE_IMPORT = "import"
VALID_CASE_SOURCES = frozenset({CASE_SOURCE_MANUAL, CASE_SOURCE_FEEDBACK, CASE_SOURCE_IMPORT})


class KnowledgeFeedback(TenantScoped, Base):
    """一条回答的点赞/踩。

    存的是「当时那一次」的现场：问题、引用、回答开头。不按 message_id 回查，
    是因为会话会被清理（``purge_conversation``），而 bad case 的价值恰恰在于
    会话没了之后还能复盘——「用户当时到底问了什么、我们引了哪几段」必须留在
    自己这张表里。
    """

    __tablename__ = "knowledge_feedback"
    __table_args__ = (
        Index(
            "ix_knowledge_feedback_tenant_verdict_created",
            "tenant_id",
            "verdict",
            "created_at",
        ),
        Index("ix_knowledge_feedback_tenant_message", "tenant_id", "message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # 会话与消息只做弱引用（字符串），不建外键：会话被物理清理时
    # CASCADE 会把事故记录一起删掉，SET NULL 又要允许为空，都不如留着原值。
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 回答前 800 字。完整回答留在消息表里，这里只求「一眼能想起是哪次」。
    answer_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")

    verdict: Mapped[str] = mapped_column(String(8), nullable=False, default=VERDICT_UP)
    # 只有踩才有原因；赞的时候留空。
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 当时的引用切片与来源文档。复盘时要能直接跳到那一段。
    chunk_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    document_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # 处理状态：bad case 列表默认只看没处理过的，否则越积越多、没人翻。
    handled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    handled_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 升格成评测用例后记下 id，避免同一条反馈被反复转成重复用例。
    case_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgeEvalCase(TenantScoped, Base):
    """评测用例：一个问句 + 它应该命中哪些资料。

    ``expect_document`` 存的是切片里的 ``document_id``（即文档的 logical_id），
    不是中文标题：标题会因为重新上传而改，logical_id 不会。

    ``expect_section`` 是可选的加强判据。只标文档的话，「命中同一份标准的
    任意一段」就算对——对 GB 27791 这种 448 片的大部头来说太宽了，
    考前 5 片与考第 300 片是两回事。填了它就必须在同一份文档里命中带该
    关键字的切片，才算这一条命中。
    """

    __tablename__ = "knowledge_eval_cases"
    __table_args__ = (
        Index(
            "ix_knowledge_eval_cases_tenant_enabled",
            "tenant_id",
            "enabled",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    query: Mapped[str] = mapped_column(Text, nullable=False)
    # 期望能答出来（True）还是期望「库里没有、应该明说不知道」（False）。
    # 后者命中才是错，是假阳性——只统计正例会让「什么都答」看起来分很高。
    expect_found: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 空列表 + expect_found=True = 还没标注期望文档，**不参与计分**。
    # 允许存半成品是因为从 bad case 转过来时只有问题、没有答案，
    # 硬卡住会把「先记下来待补」这条路堵死。
    expect_document: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    expect_section: Mapped[str | None] = mapped_column(String(256), nullable=True)

    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CASE_SOURCE_MANUAL, server_default=CASE_SOURCE_MANUAL
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnowledgeEvalRun(TenantScoped, Base):
    """一次评测的结果快照。

    ``detail`` 存每条用例的判分明细（命中了哪几片、rank 多少），是排障的唯一
    依据——只留汇总数字的话，「召回掉了 8 个点」就只能靠重跑猜。

    ``index_version`` 记下当时的索引版本：两次 run 之间重新入库过，
    指标不可比，这一列是唯一能事后发现的线索。
    """

    __tablename__ = "knowledge_eval_runs"
    __table_args__ = (
        Index(
            "ix_knowledge_eval_runs_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    case_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 参与计分的用例数。与 case_total 的差额是「未标注期望文档」的那部分，
    # 分母用错会让指标虚高。
    scored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    recall: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    precision: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hit_at_k: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mrr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    false_hit_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    zero_hit_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    index_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 汇总里那些不便建列的（按类别分组、rerank 回落原因、未标注条数）。
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    detail: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
