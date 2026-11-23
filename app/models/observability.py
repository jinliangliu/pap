# app/models/observability.py
"""自研观测的两张表：一条 trace = 一轮对话，一条 span = 一个节点/模型/工具调用。

与 `agent_runs` 的分工：

- `agent_runs` 是**业务事实**：谁、什么时候、结果如何。不因观测退役而消失。
- 这两张表是**诊断明细**：这一轮内部怎么走的、慢在哪。可清理、可丢弃、可整体关闭。

用 `run_id` 关联但不建外键：观测数据会被定期删除，业务记录不能跟着被约束卡住。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TenantScoped


class ObsTrace(TenantScoped, Base):
    """一轮对话的汇总行。列表页与聚合查询只扫这张表。"""

    __tablename__ = "obs_traces"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # OTel 规范的 32 位 hex。现在不依赖 OTel SDK，但将来要导 Tempo/Jaeger 不用改字段。
    trace_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    # 一次「运行」的分组键，不是树边。
    # 开新 trace 时若无值可继承，就等于自己的 trace_id；审批恢复的那一轮
    # 从审批单上继承了发起方的值。于是「同一次运行」= 同 operation_id 的集合，
    # 而这个列不构成父子关系，`by_agent` 之和仍等于 `traces`。
    operation_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    user_id: Mapped[str] = mapped_column(String(128), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # 与 agent_runs.id 同值，不建外键（见模块说明）。
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ok | error | timeout | awaiting_approval | cancelled
    status: Mapped[str] = mapped_column(String(24), default="ok", index=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 本轮主工具。单独成列而不是放 attrs：列表页要按工具过滤，
    # 扫 jsonb 的代价比多一列大得多。
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 本轮命中的技能，同样成列（理由同 tool_name）。
    # 未命中为 NULL，与「命中了一个叫空字符串的技能」区分开。
    skill_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 执行这一轮的是哪个智能体。与 skill_name 同级而不是塞进 attrs：
    # 按 Agent 聚合调用量 / 错误率是运营的基本动作，需要能走索引。
    agent_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    span_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 未配单价时为 NULL，不写 0：0 会被读成「免费」。
    cost_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # full 模式下的完整原文（见文档 §7）。summary/off 模式下分别是摘要与 NULL。
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    attrs: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_obs_traces_tenant_started", "tenant_id", "started_at"),
        Index("ix_obs_traces_status_started", "status", "started_at"),
        Index("ix_obs_traces_intent_started", "intent", "started_at"),
        Index("ix_obs_traces_conv_started", "conversation_id", "started_at"),
        Index("ix_obs_traces_skill_started", "skill_name", "started_at"),
        Index("ix_obs_traces_operation", "operation_id", "started_at"),
    )


class ObsSpan(TenantScoped, Base):
    """一条 trace 内的单个 span。父子关系靠 parent_span_id，前端自己建树。"""

    __tablename__ = "obs_spans"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    span_id: Mapped[str] = mapped_column(String(16))
    parent_span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 同一 trace 内的展示顺序。并列 span（如同一轮两个工具）靠它保持稳定，
    # 不依赖 started_at（可能完全相同）。
    seq: Mapped[int] = mapped_column(SmallInteger, default=0)

    name: Mapped[str] = mapped_column(String(64))
    # run | graph | node | llm | tool | mcp | db | retrieval | approval | agent
    kind: Mapped[str] = mapped_column(String(16), index=True)

    # 这个 span 属于哪个智能体。委派时子图里的节点归被委派方，而不是 trace
    # 的发起方——没有这一列，A→B 一轮里 B 的规划与工具调用认不出是谁的。
    # 成列而不是放 attrs：委派拓扑要按它 GROUP BY，扫 JSON 会随数据量线性退化，
    # 且 `->>` 与 `json_extract` 不通用（p95 已经因此吃过一次亏）。
    # 存量行为 NULL，含义是「未归属」，不当作「平台基线」来解释。
    agent_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="ok")
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # full 模式下的完整输入/输出（JSON 文本：{"input": ..., "output": ...}）。
    # 不放 attrs：大字段进 jsonb 会让列表查询跟着变慢。
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("trace_id", "span_id", name="uq_obs_span_trace_span"),
        Index("ix_obs_spans_trace_seq", "trace_id", "seq"),
        Index("ix_obs_spans_kind_started", "kind", "started_at"),
        Index("ix_obs_spans_agent_started", "agent_name", "started_at"),
    )
