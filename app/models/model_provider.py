# app/models/model_provider.py
"""模型供应商配置（平台级全局配置，不参与租户隔离）。

为何不放租户表：模型服务是平台统一能力，密钥由平台管理员在控制台维护，
所有租户共用同一套凭据。该表不挂 RLS 策略，也不继承 TenantScoped。

同一时刻只有一个 is_active=True 的供应商参与对话；其余为备用配置，
切换供应商不需要重启进程。
"""

from __future__ import annotations

import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 接口协议：决定请求体怎么拼。
#   openai-compatible : /chat/completions + Bearer，绝大多数国产网关走这个
#   openai            : 原生 OpenAI，额外允许下发 reasoning_effort
PROTOCOL_OPENAI_COMPATIBLE = "openai-compatible"
PROTOCOL_OPENAI = "openai"
SUPPORTED_PROTOCOLS = (PROTOCOL_OPENAI_COMPATIBLE, PROTOCOL_OPENAI)

# 模型用途。image 仅登记，平台当前没有图片生成能力，不参与任何调用路径。
MODEL_KIND_TEXT = "text"
MODEL_KIND_IMAGE = "image"

# 上下文窗口的合理区间。下限挡单位错误（把 32k 写成 32），
# 上限挡多敲一个零。窗口填小不会报错，只会让历史被过早压缩，事后极难查。
MIN_CONTEXT_WINDOW = 1_000
MAX_CONTEXT_WINDOW = 2_000_000


def clean_context_window(value: object) -> int | None:
    """把 ``models[]`` 里的窗口值读成 int；缺失、脏值、越界一律返回 None。

    None 的含义是「未声明」，由 ``CONTEXT_WINDOW_TOKENS`` 兜底——不是「不限」。
    数据库里的 JSON 可以被手改，读取端不能因为一行脏数据把整条对话路径打挂。
    """
    if value is None or isinstance(value, bool):
        return None

    try:
        window = int(value)
    except (TypeError, ValueError):
        return None

    if window < MIN_CONTEXT_WINDOW or window > MAX_CONTEXT_WINDOW:
        return None

    return window


class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    protocol: Mapped[str] = mapped_column(
        String(32), default=PROTOCOL_OPENAI_COMPATIBLE, nullable=False
    )
    # 明文存储：DB 已受 RLS 与运行角色约束；接口只回 api_key_set，不回显明文。
    api_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # [{"id": "deepseek-flash", "kind": "text"}, ...]
    models: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    default_text_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_image_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # auto 表示不显式下发，交给供应商默认行为。
    reasoning_mode: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
