# tests/integration/test_agent_loop_convergence.py
"""推理回路必须收敛，尤其是「上一轮成功、这一轮失败」的组合。

回归 2026-11-04 线上事故：带图提问时计划器每轮都选中同一个只读工具，
而工具在网关前置校验就被拒；失败分支只返回了 final_response，
既没写 observation 也没推进 tool_steps（这两个字段在 AgentState 里没有 reducer，
返回字典不带就等于「本轮从未发生」）。路由于是读到**上一轮**那条成功记录，
判定「可以继续」→ 回 plan_request → 再失败……

结果是 plan ↔ execute_read_tool 无限空转：45 分钟、521 次模型调用，
nginx 早在第 180 秒就 504，客户端断开并不会取消后端协程。

这里的断言是「规划器只被调用两次」：一次成功、一次失败后立刻收尾。
修复前会把预置计划吃光，甚至一直转下去。
"""

from __future__ import annotations

from app.agents.schemas import ActionPlan


async def test_loop_stops_after_tool_fails_on_second_round(client, planner):
    fake = planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="get_station_snapshot",
                arguments={"station_id": "station-001"},
                reason="查询已授权场站。",
            ),
            # 同一工具、不同站点：参数不同所以不会被「同参重放」短路，
            # 必须由网关真的拒绝（station-999 没有授权）。
            ActionPlan(
                action="read_tool",
                tool_name="get_station_snapshot",
                arguments={"station_id": "station-999"},
                reason="查询未授权场站。",
            ),
            # 修复前会继续回到规划器把这条也吃掉；修复后不该走到这里。
            ActionPlan(action="chat", response="不应到达这一轮。"),
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": "thread-loop-converge",
            "message": "查一下这个站点的压力情况",
        },
    )

    body = response.json()
    assert body["status"] == "error"
    assert body["error_category"] == "tool_rejected"
    assert "资源不可访问" in body["answer"]
    # 两次规划：第一轮成功、第二轮失败即收尾。多一次就说明回路没收敛。
    assert fake.calls == 2


async def test_loop_stops_immediately_when_first_tool_fails(client, planner):
    """首次就失败时也不能回到规划器（原本就是收尾，这里锁住行为）。"""
    fake = planner(
        [
            ActionPlan(
                action="read_tool",
                tool_name="get_station_snapshot",
                arguments={"station_id": "station-999"},
                reason="查询未授权场站。",
            ),
            ActionPlan(action="chat", response="不应到达这一轮。"),
        ]
    )

    response = await client.post(
        "/api/v1/chat",
        json={"thread_id": "thread-loop-first-fail", "message": "看看 station-999"},
    )

    assert response.json()["error_category"] == "tool_rejected"
    assert fake.calls == 1
