# app/observability/state.py
"""把三套状态词表压成一个「这次运行现在什么样」。

平台里有三处各自维护状态，它们的生命周期和写入方都不同：

| 来源 | 取值 |
|---|---|
| `obs_traces.status` | `ok` / `error` / `timeout` / `awaiting_approval` / `cancelled` |
| `approvals.status` | 10 个：`PENDING` … `EXECUTION_UNKNOWN` |
| 运行态（`agent_runs`） | `WAITING_FOR_APPROVAL` / `EXECUTED` / `VERIFIED` / `FAILED` … |

界面上要写「这次运行现在什么样」，三处给三个答案。**在出口派生，不改库里的值**：
任何一处改成「写一个统一的 status 列」，立刻会出现两个真相。

措辞借 A2A 的 TaskState（`input-required` 是**暂停**而不是终态），
因为那张状态机是公开规范里唯一把「等人」当成一等状态的。
映射关系如下。
"""

from __future__ import annotations

# 外部系统「可能已经写了，但确认不了」的几个状态。它们不是失败，
# 但也不能算完成——界面上必须让人去核验，而不是重试。
_UNKNOWN_OUTCOME = {"EXECUTION_UNKNOWN", "RECOVERY_REQUIRED"}

# 委派链在批准期间被上游否决。这不是系统故障：B 可能完全正常，
# 是「当初那一跳现在不该发生了」。分成单独一档，排障方向完全不同。
_REVOKED_CATEGORIES = {
    "DELEGATION_REVOKED",
    "AGENT_UNAVAILABLE",
    "AGENT_BOUNDARY_CHANGED",
}


def display_state(
    *,
    trace_status: str | None,
    approval_status: str | None = None,
    error_category: str | None = None,
) -> str:
    """返回 `working` / `input-required` / `completed` / `failed` / `rejected` / `canceled`。

    顺序是有讲究的，不是随手排的：

    1. **否决优先于失败。** `DELEGATION_REVOKED` 会让 trace 落成 error，但那时 B
       可能完全健康，被收窄的是上游。显示成 `failed` 会把运维引去查一个没问题的
       执行方。这不是理论情况：实机上 SA38 就是这一类。
    2. **失败优先于等待。** 子调用崩在审批之后时，库里会同时有「trace 已 error」
       和「单子还 PENDING」。这不叫等待——没有人会去恢复它。显示成
       `input-required` 会让人以为「点一下确认就好了」。
    3. 之后才看「有没有人卡着」。到这一步还没失败、没被否决的暂停，才是真的在等人。
    """
    trace = str(trace_status or "")
    approval = str(approval_status or "")
    category = str(error_category or "")

    # 1. 被上游否决。**必须排在失败之前**：`DELEGATION_REVOKED` 会让 trace 落成
    #    error，但这时 B 可能完全健康——显示成 failed 会把运维引去查一个没问题的
    #    执行方，而真正该看的是发起方那一边的边界。
    if category in _REVOKED_CATEGORIES:
        return "rejected"

    # 2. 明确失败。`awaiting_approval` 是 trace 自己的暂停标记，不算失败。
    if trace in {"error", "timeout", "abandoned"}:
        return "failed"

    # 3. 外部结果未知：结论是「去核验」，不是「重试」，但界面上归到需要处理的一类。
    if approval in _UNKNOWN_OUTCOME:
        return "failed"

    # 4. 真在等人。
    if approval == "PENDING":
        return "input-required"

    # 5. 在跑。放在等待之后：一个既有 pending 单子又在跑的运行，
    #    对运维来说「有人在等」比「它在跑」更急。
    if trace == "running":
        return "working"

    # 6. 没做成，但也不是失败：用户按下取消，或者单子过期没人管。
    if trace == "cancelled" or approval in {"REJECTED", "EXPIRED"}:
        return "canceled"

    # 7. 剩下的都算完成。`EXECUTED` 是「外部调用返回了但没回读核验」，
    #    比 `VERIFIED` 弱一档，界面上另有一列标注，不在这里拆状态。
    return "completed"


def needs_attention(
    *,
    display_state_value: str,
    approval_status: str | None = None,
    trace_status: str | None = None,
) -> bool:
    """这条运行是不是「已经结束了，但还有事没收拾」。

    唯一的真实场景：trace 已终态而审批单还挂着（或反过来）。
    这种运行在列表里看起来是「完成」，实际留了一张没人管的单子——
    运行视图要能把它挑出来，否则它会在库里躺到过期。
    """
    if display_state_value in {"input-required", "working"}:
        return False
    return str(approval_status or "") == "PENDING" or str(trace_status or "") == "running"
