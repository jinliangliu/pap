"""巡检脚本的判定逻辑测试。

重点不是「函数能跑」，而是几条容易写错、错了又很难在线上发现的规则：
连续失败才报警、重复提醒有间隔、恢复只在报过之后才发。
"""

from __future__ import annotations

import json

from scripts import watchdog
from scripts.watchdog import (
    CheckResult,
    Config,
    Event,
    evaluate,
    format_message,
    load_state,
    save_state,
)

OK = CheckResult("ready", True, "就绪，http=200")
BAD = CheckResult("ready", False, "status=degraded http=503，失败项：database")


# ---------------- 配置 ----------------


def test_mcp_addr_without_port_gets_default_port() -> None:
    cfg = Config.from_env({"WATCHDOG_MCP_ADDR": "127.0.0.1"})

    assert cfg.mcp_addr == "127.0.0.1:8765"


def test_unknown_webhook_type_falls_back_to_generic() -> None:
    """拼错的类型名不该让告警静默失败，退到 generic 至少还能发出去。"""
    cfg = Config.from_env({"WATCHDOG_WEBHOOK_TYPE": "wechat"})

    assert cfg.webhook_type == "generic"


def test_invalid_number_falls_back_to_default() -> None:
    cfg = Config.from_env({"WATCHDOG_DISK_MAX_PERCENT": "八十五", "WATCHDOG_TIMEOUT_SECONDS": ""})

    assert cfg.disk_max_percent == 85.0
    assert cfg.timeout == 8.0


def test_fail_threshold_is_at_least_one() -> None:
    """阈值配成 0 会让每次失败都立刻报警，等于关掉了防抖。"""
    cfg = Config.from_env({"WATCHDOG_FAIL_THRESHOLD": "0"})

    assert cfg.fail_threshold == 1


# ---------------- 告警判定 ----------------


def test_first_failure_does_not_alert_until_threshold() -> None:
    state, events = evaluate([BAD], {"checks": {}}, now=1000, fail_threshold=2, repeat_seconds=60)

    assert events == []
    assert state["checks"]["ready"]["fails"] == 1


def test_second_consecutive_failure_alerts() -> None:
    state, events = evaluate([BAD], {"checks": {}}, now=1000, fail_threshold=2, repeat_seconds=60)
    state, events = evaluate([BAD], state, now=1060, fail_threshold=2, repeat_seconds=60)

    assert [e.kind for e in events] == ["firing"]
    assert events[0].name == "ready"
    assert events[0].is_repeat is False
    # 报警时刻要记下来，否则下一分钟就会再报一次。
    assert state["checks"]["ready"]["last_alert_at"] == 1060


def test_alert_is_suppressed_within_repeat_interval() -> None:
    state, _ = evaluate([BAD], {"checks": {}}, now=1000, fail_threshold=1, repeat_seconds=1800)
    _, events = evaluate([BAD], state, now=1100, fail_threshold=1, repeat_seconds=1800)

    assert events == []


def test_alert_repeats_after_interval() -> None:
    state, _ = evaluate([BAD], {"checks": {}}, now=1000, fail_threshold=1, repeat_seconds=1800)
    _, events = evaluate([BAD], state, now=2800, fail_threshold=1, repeat_seconds=1800)

    assert [e.kind for e in events] == ["firing"]
    assert events[0].is_repeat is True


def test_recovery_without_prior_alert_is_silent() -> None:
    """一直正常的检查不该每次巡检都报一次平安。"""
    _, events = evaluate([OK], {"checks": {}}, now=1000, fail_threshold=2, repeat_seconds=60)

    assert events == []


def test_recovery_after_alert_is_reported() -> None:
    state, _ = evaluate([BAD], {"checks": {}}, now=1000, fail_threshold=1, repeat_seconds=60)
    state, events = evaluate([OK], state, now=1300, fail_threshold=1, repeat_seconds=60)

    assert [e.kind for e in events] == ["recovered"]
    assert events[0].failing_since == 1000
    # 恢复后清空计数，下次故障要重新累计。
    assert state["checks"]["ready"] == {"fails": 0, "failing_since": None, "last_alert_at": None}


def test_flapping_resets_threshold() -> None:
    """失败一次、恢复、再失败一次，不该在第二次失败时就报警。"""
    state, _ = evaluate([BAD], {"checks": {}}, now=1000, fail_threshold=2, repeat_seconds=60)
    state, _ = evaluate([OK], state, now=1020, fail_threshold=2, repeat_seconds=60)
    state, events = evaluate([BAD], state, now=1040, fail_threshold=2, repeat_seconds=60)

    assert events == []
    assert state["checks"]["ready"]["fails"] == 1


def test_failing_since_is_not_moved_forward() -> None:
    """持续故障时，起始时间必须是第一次失败的时刻。"""
    state, _ = evaluate([BAD], {"checks": {}}, now=1000, fail_threshold=5, repeat_seconds=60)
    state, _ = evaluate([BAD], state, now=1600, fail_threshold=5, repeat_seconds=60)

    assert state["checks"]["ready"]["failing_since"] == 1000


def test_multiple_check_events_are_all_reported() -> None:
    results = [
        CheckResult("ready", False, "不可达"),
        CheckResult("disk", False, "/ 已用 91%"),
        CheckResult("memory", True, "可用 8000 MiB"),
    ]

    _, events = evaluate(results, {"checks": {}}, now=1000, fail_threshold=1, repeat_seconds=60)

    assert {e.name for e in events} == {"ready", "disk"}


# ---------------- 文案 ----------------


def test_message_lists_failing_checks_and_duration() -> None:
    event = Event("firing", "ready", "status=degraded http=503", failing_since=1000)

    text = format_message([event], now=1000 + 4 * 60, host="srv-1")

    assert "srv-1" in text
    assert "ready" in text
    assert "status=degraded" in text
    assert "已持续4 分钟" in text
    # 告警里要给人留下下一步动作，不然收到消息也不知道从哪查起。
    assert "journalctl -u personal-agent-platform" in text


def test_short_duration_reads_as_less_than_a_minute() -> None:
    """刚触发时两个时间戳常落在同一秒，显示「0 秒」不如「不足 1 分钟」。"""
    event = Event("firing", "ready", "不可达", failing_since=1000)

    text = format_message([event], now=1000, host="srv-1")

    assert "已持续不足 1 分钟" in text


def test_message_marks_repeat_as_unresolved() -> None:
    event = Event("firing", "disk", "/ 已用 92%", failing_since=1000, is_repeat=True)

    text = format_message([event], now=2000, host="srv-1")

    assert "持续未恢复" in text


def test_recovery_message_shows_duration() -> None:
    event = Event("recovered", "ready", "就绪，http=200", failing_since=1000)

    text = format_message([event], now=1000 + 90, host="srv-1")

    assert "恢复" in text
    assert "故障持续1 分钟" in text


# ---------------- 检查项 ----------------


def test_disk_check_respects_threshold(tmp_path) -> None:
    loose = watchdog.check_disk(Config(disk_path=str(tmp_path), disk_max_percent=100.0))
    strict = watchdog.check_disk(Config(disk_path=str(tmp_path), disk_max_percent=0.0))

    assert loose.ok is True
    assert strict.ok is False


def test_missing_access_log_is_skipped_not_failed(tmp_path) -> None:
    """没装 Nginx 的机器不该因此一直报红。"""
    result = watchdog.check_http_5xx(Config(access_log=str(tmp_path / "nope.log")))

    assert result.ok is True
    assert "跳过" in result.detail


def test_http_5xx_counts_tail_lines(tmp_path) -> None:
    log = tmp_path / "access.log"
    line_200 = '1.2.3.4 - - [03/Nov/2026:15:00:00 +0800] "GET /ready HTTP/1.1" 200 12 "-" "curl"'
    line_502 = (
        '1.2.3.4 - - [03/Nov/2026:15:00:00 +0800] "GET /api/v1/chat HTTP/1.1" 502 5 "-" "curl"'
    )
    log.write_text("\n".join([line_200, line_502, line_502]) + "\n", encoding="utf-8")

    bad = watchdog.check_http_5xx(Config(access_log=str(log), http_5xx_max=1))
    good = watchdog.check_http_5xx(Config(access_log=str(log), http_5xx_max=5))

    assert bad.ok is False
    assert "2 条 5xx" in bad.detail
    assert good.ok is True


def test_one_broken_check_does_not_stop_the_run(monkeypatch) -> None:
    def boom(cfg: Config) -> CheckResult:
        raise RuntimeError("检查自己炸了")

    ok_check = lambda cfg: CheckResult("disk", True, "ok")  # noqa: E731
    monkeypatch.setattr(watchdog, "CHECKS", (boom, ok_check))

    results = watchdog.run_checks(Config())

    assert [r.name for r in results] == ["boom", "disk"]
    assert results[0].ok is False
    assert "检查自己炸了" in results[0].detail
    assert results[1].ok is True


# ---------------- 状态文件 ----------------


def test_state_roundtrip(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = {"checks": {"ready": {"fails": 2, "failing_since": 1.0, "last_alert_at": 2.0}}}

    save_state(str(path), state)

    assert load_state(str(path)) == state


def test_missing_state_file_starts_empty(tmp_path) -> None:
    assert load_state(str(tmp_path / "absent.json")) == {"checks": {}}


def test_corrupt_state_is_treated_as_empty(tmp_path) -> None:
    """状态文件坏了宁可多重报一次，也不能因为读不动就彻底不报。"""
    path = tmp_path / "state.json"
    path.write_text("{不是 json", encoding="utf-8")

    assert load_state(str(path)) == {"checks": {}}


def test_state_file_is_written_atomically(tmp_path) -> None:
    path = tmp_path / "state.json"

    save_state(str(path), {"checks": {}})

    # 落盘走的是「写临时文件再替换」，不该留下中间产物。
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
    assert json.loads(path.read_text(encoding="utf-8")) == {"checks": {}}


def test_unwritable_state_does_not_raise(tmp_path) -> None:
    """状态存不下只会多报几次，不该让巡检本身失败。"""
    save_state(str(tmp_path / "no-such-dir" / "state.json"), {"checks": {}})


# ---------------- 发送 ----------------


def test_webhook_payload_matches_channel_type() -> None:
    """企业微信/钉钉/飞书的消息体不一样，套错模板对方直接拒收。"""
    cfg = Config(webhook_type="wecom")
    payload = watchdog._WEBHOOK_BODIES["wecom"]
    body = payload("出事了")

    assert cfg.webhook_type == "wecom"
    assert body["msgtype"] == "text"
    assert body["text"]["content"] == "出事了"


def test_dispatch_without_channel_does_not_raise() -> None:
    """没配通道时必须只是少发一条，不能让巡检崩掉。"""
    watchdog.dispatch(Config(), "出事了")
