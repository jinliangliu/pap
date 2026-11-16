# app/services/delegation_limits.py
"""委派预算：默认值、硬上限，以及沿链取最严的那一条。

为什么单独一个文件：这条规则要在三处用同一份实现——图入口（把 Agent 的配置折进
生效上限）、工具执行（判超限）、子调用（把上限往下传）。写在任何一个调用点，
另外两处就会各写一份，然后慢慢漂移成「入口能过、子图不能过」。

## 为什么是「取最严」而不是「用当前这一环的配置」

深度上限是安全阀，不是配额。A→B→C 里若 B 把上限调成 5，用它自己那一环判定，
整条链就能悄悄跑 5 跳——而 A 从来没有同意过这个深度。

所以：**配置只能让这一环更严，不能让整条链更宽。**

- 一个 Agent 配 `max_delegation_depth=1`，它下游的任何链路都不会超过 1 跳；
- 想让整条链更深，链上每一环都要放宽——这刻意做得很麻烦，因为「一次请求跑
  十几层」的代价是整条链路一起付的。

这条语义与平台的其他边界一致：`allowed_tools` / `can_write` 也都是只收窄。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 平台默认。三个值都不是拍的（见 `docs/A2A.md` §8）：
# - 深度 2 覆盖「客服 → 技术 → 现场」这类真实链路，再多就是流程编排了；
# - 单轮 3 次足够模型在答不好时换一个目标重试一次，再多就是在烧钱。
DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_PER_TURN = 3

# 配置的硬天花板。不是性能考虑：再高就等于「不设上限」，
# 而这张表存在的理由正是给「一次请求跑十几层」踩刹车。
MAX_DEPTH_CEILING = 5
MAX_PER_TURN_CEILING = 10


@dataclass(frozen=True)
class DelegationLimits:
    """一条委派链上实际生效的上限。

    冻结对象：它会跟着子调用往下传，中途被改写就等于上限失效。
    """

    max_depth: int = DEFAULT_MAX_DEPTH
    max_per_turn: int = DEFAULT_MAX_PER_TURN

    def narrowed_by(
        self,
        *,
        max_depth: int | None = None,
        budget: int | None = None,
    ) -> DelegationLimits:
        """把某一环的配置折进来。None = 这一环没配，保持原值。

        `None` 与 `0` 是两件事：前者是「没特别要求」，后者是「一次都不许」。
        用 `x or default` 会把 0 当成没配，于是「禁止转交」的开关静默失效。
        """
        depth = self.max_depth if max_depth is None else min(self.max_depth, int(max_depth))
        per_turn = (
            self.max_per_turn if budget is None else min(self.max_per_turn, int(budget))
        )
        return DelegationLimits(max_depth=depth, max_per_turn=per_turn)

    @classmethod
    def for_root(cls, *, max_depth: int | None = None, budget: int | None = None) -> DelegationLimits:
        """链的起点：以本 Agent 的配置为基准，平台默认只是兜底。

        与 `narrowed_by` 的区别只有一处，但很关键：这里**允许**比平台默认更宽
        （在硬天花板之内）。不这么做的后果是「最大转交深度 = 5」这个配置项
        永远不起作用——根节点从默认 2 出发，之后每一环只能取 min，整条链
        再也回不到 5。`docs/A2A.md` §8 写的是「想让整条链更深，链上每一环都要
        放宽」，那前提就是根能放宽。

        为什么不是漏洞：放宽只能由**这个 Agent 自己的配置**发起，而改配置本身
        是治理动作（要 `agent:write`、会留审计）。链上后续环节仍然只收窄。
        """
        return cls(
            max_depth=_clamp(max_depth, DEFAULT_MAX_DEPTH, MAX_DEPTH_CEILING),
            max_per_turn=_clamp(budget, DEFAULT_MAX_PER_TURN, MAX_PER_TURN_CEILING),
        )

    def as_state(self) -> dict[str, int]:
        """进 checkpoint 的形态。字段名用短名：它在每个子图里都会出现一次。"""
        return {"max_depth": self.max_depth, "max_per_turn": self.max_per_turn}

    @classmethod
    def from_state(cls, raw: Any) -> DelegationLimits:
        """从 checkpoint 还原。

        容忍缺键与脏值：这一格可能来自旧版本的 checkpoint，也可能被人手改过。
        越界值夹到合法区间而不是抛异常——为了一个诊断字段让整轮对话打不开，
        代价远大于收益。夹的方向是「不放大」：上限只在 0..天花板 之间取。
        """
        if not isinstance(raw, dict):
            return cls()

        return cls(
            max_depth=_clamp(raw.get("max_depth"), DEFAULT_MAX_DEPTH, MAX_DEPTH_CEILING),
            max_per_turn=_clamp(
                raw.get("max_per_turn"), DEFAULT_MAX_PER_TURN, MAX_PER_TURN_CEILING
            ),
        )

    def depth_refusal(self) -> str:
        """深度超限时给人看的那句话。

        0 要单独说：配置成 0 是「这个智能体不转交」，说成「最多 0 跳」
        只会让人去查是不是配错了。
        """
        if self.max_depth <= 0:
            return "当前智能体被配置为不转交任务，这件事需要它自己处理。"
        return f"转交层级已到上限（最多 {self.max_depth} 跳），这件事需要当前智能体自己处理。"

    def budget_refusal(self) -> str:
        if self.max_per_turn <= 0:
            return "当前智能体被配置为不转交任务，请直接基于已有信息作答。"
        return f"本轮转交次数已达上限（{self.max_per_turn} 次），请基于已有结果作答。"


def _clamp(value: Any, fallback: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, min(number, ceiling))


def check_limit(label: str, value: Any, *, ceiling: int) -> int | None:
    """配置入口的取值范围校验。`None` 原样返回（= 用平台默认）。

    `None` 不归一成默认值，否则界面上「没配」与「配成和默认一样」会被压成
    同一个数——管理员以为自己设了 2，而平台某天把默认改成 1 时，
    他会以为自己从没动过这一项。

    抛 `ValueError`：这一层不认识 HTTP，也不认识智能体的业务异常类型。
    调用方各自翻译（`agent_service` 转成 `AgentValidationError`）。
    """
    if value is None:
        return None
    if value < 0:
        raise ValueError(f"{label}不能是负数。")
    if value > ceiling:
        raise ValueError(f"{label}最多 {ceiling}。")
    return value
