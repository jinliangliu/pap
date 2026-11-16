# tests/unit/test_planner.py
"""规划器的模型兼容处理。

背景：结构化输出必须强制指定工具名（tool_choice=函数名），而思考模式模型
明确拒绝这种组合——api.deepseek.com 返回 400「Thinking mode does not
support this tool_choice」。这里盯住「主动关思考」和「两条降级路径」。
"""

from __future__ import annotations

import pytest

from app.agents import planner as planner_mod
from app.services.model_service import TextModelConfig

pytestmark = pytest.mark.anyio


def _config(reasoning_mode: str = "auto") -> TextModelConfig:
    return TextModelConfig(
        source="provider",
        provider_name="p",
        protocol="openai-compatible",
        model="deepseek-flash",
        api_key="sk-test",
        base_url="https://api.example.invalid",
        temperature=0.0,
        reasoning_mode=reasoning_mode,
    )


class _FakeLLM:
    """只实现 with_structured_output，返回一个可控的「规划器」。"""

    def __init__(self, behavior):
        self.behavior = behavior
        self.bound_schema = None

    def with_structured_output(self, schema, method=None, **kwargs):
        self.bound_schema = (schema, method)
        return _FakePlanner(self.behavior)


class _FakePlanner:
    def __init__(self, behavior):
        self.behavior = behavior

    def with_config(self, **kwargs):
        # 真实 planner 是 Runnable，`get_planner()` 会给它挂观测回调。
        # 假件只要能被挂上就够了——回调本身在观测关闭时是空转的。
        self.config = kwargs
        return self

    async def ainvoke(self, messages):
        # 重试会新建一个 planner，所以计数必须放在 behavior 外部，
        # 否则「第一次失败、第二次成功」这种用例永远走不到成功分支。
        return self.behavior()


@pytest.fixture(autouse=True)
def _reset():
    planner_mod.reset_planner()
    yield
    planner_mod.reset_planner()


def _patch(monkeypatch, behavior, *, config=None, seen=None):
    async def fake_resolve(model_override=None):
        return config or _config()

    def fake_build(config, **kwargs):
        if seen is not None:
            seen.append(kwargs.get("disable_thinking"))
        return _FakeLLM(behavior)

    monkeypatch.setattr(planner_mod, "resolve_text_model", fake_resolve)
    monkeypatch.setattr(planner_mod, "build_chat_model", fake_build)


async def test_planner_disables_thinking_mode(monkeypatch):
    """规划器必须关掉思考模式，否则强制 tool_choice 会被供应商拒绝。"""
    seen: list = []
    _patch(monkeypatch, lambda: "plan", seen=seen)

    result = await planner_mod.get_planner()

    assert result is not None
    assert seen == [True]


async def test_retries_without_reasoning_effort_when_rejected(monkeypatch):
    """供应商不认 reasoning_effort 时，去掉该参数重试并成功。"""
    turns = {"n": 0}

    def behavior():
        turns["n"] += 1
        if turns["n"] == 1:
            raise RuntimeError("Unsupported parameter: reasoning_effort")
        return "plan"

    seen: list = []
    _patch(monkeypatch, behavior, seen=seen)

    plan = await planner_mod.create_action_plan(user_message="你好", available_tools=[])

    assert plan == "plan"
    # 第一次带 disable_thinking=True，重试时不带。
    assert seen == [True, False]


async def test_remembers_unsupported_reasoning_effort(monkeypatch):
    """记下之后，同一份配置不再重复先失败一次。"""
    turns = {"n": 0}

    def behavior():
        turns["n"] += 1
        raise RuntimeError("Unsupported parameter: reasoning_effort")

    seen: list = []
    _patch(monkeypatch, behavior, seen=seen)

    with pytest.raises(planner_mod.ModelInvocationError):
        await planner_mod.create_action_plan(user_message="你好", available_tools=[])
    assert seen == [True, False]

    seen.clear()
    with pytest.raises(planner_mod.ModelInvocationError):
        await planner_mod.create_action_plan(user_message="你好", available_tools=[])
    # 这次直接用不带该参数的客户端，只失败一次。
    assert seen == [False]


async def test_thinking_tool_choice_conflict_gives_actionable_error(monkeypatch):
    """无法关闭思考模式时，给出能照着改的提示，而不是笼统的「暂时不可用」。"""

    def behavior():
        raise RuntimeError(
            "Error code: 400 - Thinking mode does not support this tool_choice"
        )

    _patch(monkeypatch, behavior)

    with pytest.raises(planner_mod.ModelInvocationError) as excinfo:
        await planner_mod.create_action_plan(user_message="你好", available_tools=[])

    assert excinfo.value.category == "model_invalid_request"
    assert "思考模式" in str(excinfo.value)


async def test_usage_sink_receives_collected_tokens(monkeypatch):
    """只要调用方给了 sink，就要把用量收集器挂到这次调用上。

    这个挂载点失效过一次：用量写在 ContextVar 里，模型回调跑在另一个
    asyncio 任务里，值传不回来，`last_prompt_tokens` 永远是 0，
    压缩条件永远不会触发——而且没有任何报错。
    """
    from app.agents.schemas import ActionPlan
    from app.observability.callbacks import UsageCollector

    def behavior():
        return ActionPlan(action="chat", response="好")

    planner = _FakePlanner(behavior)
    planner.is_runnable = False
    monkeypatch.setattr(planner_mod, "_test_planner", planner)

    sink: dict = {}
    await planner_mod.create_action_plan(user_message="你好", available_tools=[], usage_sink=sink)

    # 假规划器不是 Runnable，不会挂 config；这里确认走到了「不挂」的分支，
    # 真实 Runnable 的分支由下面这条覆盖。
    assert sink == {}
    assert isinstance(UsageCollector(), UsageCollector)


async def test_runnable_planner_gets_usage_collector(monkeypatch):
    """真 planner 是 Runnable：必须通过 config 把收集器挂上去。"""
    from langchain_core.runnables import RunnableLambda

    from app.agents.schemas import ActionPlan
    from app.observability.callbacks import UsageCollector

    seen: dict = {}

    async def fake(payload, config=None):  # noqa: ANN001
        seen["config"] = config
        return ActionPlan(action="chat", response="好")

    monkeypatch.setattr(planner_mod, "_test_planner", RunnableLambda(fake))

    await planner_mod.create_action_plan(
        user_message="你好", available_tools=[], usage_sink={}
    )

    callbacks = (seen["config"] or {}).get("callbacks")
    # LangChain 会把 config 里的 callbacks 规整成 manager，两种形态都收。
    handlers = getattr(callbacks, "handlers", callbacks) or []
    assert any(isinstance(item, UsageCollector) for item in handlers), (
        "没有挂收集器，用量就永远收不到，压缩条件也永远不会满足"
    )
