# app/schemas/model_provider.py
from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.models.model_provider import (
    MAX_CONTEXT_WINDOW,
    MIN_CONTEXT_WINDOW,
    clean_context_window,
)
from app.schemas.common import APIModel

ModelKind = Literal["text", "image"]
# 不提供「关闭」：OpenAI 兼容协议没有可靠的关闭手段，
# 摆一个点了也不生效的选项比不给更糟。
ReasoningMode = Literal["auto", "low", "medium", "high"]
Protocol = Literal["openai-compatible", "openai"]


class ModelEntry(APIModel):
    """供应商下的一个模型。kind=image 仅登记，平台不调用。"""

    id: str = Field(min_length=1, max_length=128)
    kind: ModelKind = "text"
    # 上下文窗口（token）。**留空表示不知道**，由 CONTEXT_WINDOW_TOKENS 兜底——
    # 不设默认值：默认 32000 会让「没填」与「填了 32000」在库里长得一样，
    # 以后想区分「供应商没声明」和「运维声明了 32k」就无从判断。
    # 下限 1000 挡的是把「32」（想写 32k）这种单位错误直接存进去：
    # 窗口填小不会报错，只会让历史被过早压缩，事后很难查。
    context_window: int | None = Field(
        default=None, ge=MIN_CONTEXT_WINDOW, le=MAX_CONTEXT_WINDOW
    )


class ProviderResponse(APIModel):
    """供应商配置（读）。API Key 只回是否已设置。"""

    id: int
    name: str
    protocol: str = "openai-compatible"
    api_key_set: bool = False
    base_url: str | None = None
    models: list[ModelEntry] = Field(default_factory=list)
    default_text_model: str | None = None
    default_image_model: str | None = None
    temperature: float = 0.0
    reasoning_mode: str = "auto"
    is_active: bool = False
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row) -> ProviderResponse:
        entries: list[ModelEntry] = []
        for item in row.models or []:
            # 兼容两种历史写法：纯字符串，或 {id, kind} 字典。
            if isinstance(item, str):
                entries.append(ModelEntry(id=item, kind="text"))
            elif isinstance(item, dict) and item.get("id"):
                entries.append(
                    ModelEntry(
                        id=str(item["id"]),
                        kind=item.get("kind") or "text",
                        # 历史行里可能没有这个键，也可能是脏值（手改过 JSON）。
                        # 脏值按「没填」处理：读取端不能因为一行脏数据就 500。
                        context_window=clean_context_window(item.get("context_window")),
                    )
                )

        return cls(
            id=row.id,
            name=row.name,
            protocol=row.protocol or "openai-compatible",
            api_key_set=bool(row.api_key),
            base_url=row.base_url,
            models=entries,
            default_text_model=row.default_text_model,
            default_image_model=row.default_image_model,
            temperature=float(row.temperature or 0.0),
            reasoning_mode=row.reasoning_mode or "auto",
            is_active=bool(row.is_active),
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )


class EffectiveModelResponse(APIModel):
    """当前真正生效的模型来源，用于页面上直接回答「现在到底用的是哪个」。"""

    source: Literal["provider", "env"]
    provider_name: str
    protocol: str
    model: str
    base_url: str | None = None
    temperature: float = 0.0
    reasoning_mode: str = "auto"
    api_key_set: bool = False


class ProviderListResponse(APIModel):
    items: list[ProviderResponse] = Field(default_factory=list)
    effective: EffectiveModelResponse


class ProviderCreate(APIModel):
    name: str = Field(min_length=1, max_length=64)
    protocol: Protocol = "openai-compatible"
    api_key: str | None = Field(default=None, max_length=512)
    base_url: str | None = Field(default=None, max_length=512)
    models: list[ModelEntry] = Field(default_factory=list)
    default_text_model: str | None = Field(default=None, max_length=128)
    default_image_model: str | None = Field(default=None, max_length=128)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    reasoning_mode: ReasoningMode = "auto"

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value.rstrip("/") or None

    # 必须用 model_validator：field_validator 在「字段取默认值」时不会触发
    # （validate_default 默认为 False），而这里要处理的正是「调用方没传
    # default_text_model」这一情形。少了这层推导，API 会存下 null，页面显示
    # 「未设置」而实际跑的是代码里的兜底模型，两边说的不是一件事。
    @model_validator(mode="after")
    def _require_text_model(self) -> ProviderCreate:
        text_models = [m.id for m in self.models if m.kind == "text"]
        if self.default_text_model:
            if text_models and self.default_text_model not in text_models:
                raise ValueError("默认文字模型必须在模型列表内。")
            return self
        # 未显式指定时取列表里第一个文字模型，避免保存出「没有可用模型」的供应商。
        if text_models:
            self.default_text_model = text_models[0]
        return self


class ProviderUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    protocol: Protocol | None = None
    api_key: str | None = Field(default=None, max_length=512)
    base_url: str | None = Field(default=None, max_length=512)
    models: list[ModelEntry] | None = None
    default_text_model: str | None = Field(default=None, max_length=128)
    default_image_model: str | None = Field(default=None, max_length=128)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    reasoning_mode: ReasoningMode | None = None

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value.rstrip("/") or None


class DiscoverModelsRequest(APIModel):
    """「发现模型」：直接问供应商有哪些模型，避免手抄模型名抄错。"""

    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=512)
    # 编辑既有供应商时留空 api_key，用库里的密钥去探测。
    provider_id: int | None = None


class DiscoverModelsResponse(APIModel):
    models: list[str] = Field(default_factory=list)
    message: str = ""
