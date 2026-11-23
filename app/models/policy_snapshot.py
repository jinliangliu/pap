# app/models/policy_snapshot.py
"""平台策略快照：每个 revision 对应一份**当时生效的完整配置**。

为什么要 revision，而不是「每次读库」：

平台配置是「管理员随时改、改完立刻生效」的。同一条对话里，第一跳读工具、
第二跳写工具，中间管理员按了一下开关——两跳就活在不同的策略下，而事后
审计里只能看到两条各自成立的记录，谁也答不出「这一轮到底按哪版跑的」。
一个单调号把这件事钉死：一次调用记下它用的是哪一版，之后可以原样取回。

为什么整份 JSON 落库，而不是只记变更：

快照要能被**事后原样取回**。存增量就得从头重放，而重放依赖每一版都在——
一次迁移、一次回滚、一次人工修库，链条就断了，断在哪一版事后看不出来。

为什么 revision 是全局单调号，不是每个 key 一个版本号：

一次改动常常牵动多项（收严默认动作的同时关掉观测）。按 key 编号会得到
一组互不相干的小号，没人能说清「第 7 版运行时到底哪几项生效」。

这张表**只增不改**。修剪交给保留策略，不放在写入路径上：写入路径上多做
一次全表统计，就等于让一次开关切换的耗时取决于历史长度。
"""

from __future__ import annotations

import datetime

from sqlalchemy import JSON, BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PolicySnapshot(Base):
    __tablename__ = "policy_snapshots"

    # 单调递增，由 `runtime_settings` 在写入事务里分配。
    # 0 不落库：它表示「从来没有过配置变更，全按 .env 默认」，
    # 这一版由代码合成（见 `runtime_settings.snapshot_at`），不必占一行。
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # 完整的生效值。键与 `runtime_settings.snapshot()` 一一对应，
    # 因此「取回某一版」与「看当前状态」是同一个结构，页面不必分两套解析。
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    # 这一次变更动的是哪个键（只作排障线索，不作为快照的语义）。
    # 一次请求改了两项时会落两行、两行 payload 相同——那是刻意的：
    # 快照描述的是「状态」，不是「动作」。
    note: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
