# app/services/delivery_grant_service.py
"""定时任务的投递授权范围（capability grant）纯逻辑。

形态：**按 schedule 授权**，见 docs/TRANSACTION.md §8 开放项 4。
按人太宽（此人所有定时任务共享），按 workspace 会跨任务串权；按 schedule
一次授权覆盖它的所有未来触发，与「无人值守但仍可撤销」兼容。

grant 里**不存 roles/scopes**：那会把一次授权变成可反复使用的提权凭证。
每次投递都重新解析用户当前 RBAC 权限，收窄立即生效；grant 只负责把
「哪个工具、什么参数」这一层收窄。两层都过才真正出网。

三条纪律：
1. grant 只由平台用 `build_grant` 构造。调用方给不了 scopes/roles，
   也不能把用户传来的 dict 直接当 grant 存。
2. `check_delivery` 是 closed set：参数键必须全在白名单里，不在键集合内的
   直接拒绝——多传一个 `bcc` 就越过了「只允许这 3 个收件人」的承诺。
3. 拒绝信息只说哪个键不对，**不回显参数值**。拒绝原因会进日志与审计，
   把收件人/正文写进去等于绕开「凭据不进日志」的另一条红线。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

DEFAULT_GRANT_DAYS = 90

# 白名单里的通配值：只用于「每次都不同」的参数（正文、主题里的日期等）。
# 必须显式写成 "*"，缺失的键仍然拒绝——不存在「没写就是随便」。
ANY_VALUE = "*"


class DeliveryGrantError(ValueError):
    """授权凭证本身不可用。"""


class DeliveryGrantDenied(DeliveryGrantError):
    """本次投递不在授权范围内。请求没有发往外部系统。"""


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_expiry(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise DeliveryGrantError("授权缺少有效的 expires_at。")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryGrantError("授权 expires_at 不是合法时间。") from exc
    if parsed.tzinfo is None:
        raise DeliveryGrantError("授权 expires_at 必须带时区。")
    return _as_utc(parsed)


def _check_bounds(bounds: Any) -> dict[str, Any]:
    if not isinstance(bounds, dict) or not bounds:
        raise DeliveryGrantError("授权缺少 parameter_bounds。")

    checked: dict[str, Any] = {}
    for key, expected in bounds.items():
        if not isinstance(key, str) or not key or len(key) > 128:
            raise DeliveryGrantError("授权 parameter_bounds 的键不合法。")
        if expected == ANY_VALUE:
            checked[key] = ANY_VALUE
            continue
        if isinstance(expected, (str, int, float, bool)):
            checked[key] = expected
            continue
        if isinstance(expected, (list, tuple)) and all(
            isinstance(item, (str, int, float, bool)) for item in expected
        ):
            # 去重且保序；空列表等于不允许任何值，与「键缺失」区分开。
            checked[key] = list(dict.fromkeys(expected))
            continue
        raise DeliveryGrantError(f"授权参数 {key} 的白名单形态不支持。")
    return checked


def build_grant(
    *,
    tool_name: str,
    parameter_bounds: dict[str, Any],
    created_by: str,
    expires_at: datetime | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """平台侧构造授权。只接受工具名与参数边界，不接 scopes/roles。"""
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise DeliveryGrantError("授权必须指定 tool_name。")
    if len(tool_name) > 255:
        raise DeliveryGrantError("tool_name 过长。")
    if not isinstance(created_by, str) or not created_by:
        raise DeliveryGrantError("授权必须记下 created_by。")

    current = _as_utc(now or datetime.now(UTC))
    expiry = (
        _parse_expiry(expires_at) if isinstance(expires_at, str)
        else _as_utc(expires_at) if expires_at is not None
        else current + timedelta(days=DEFAULT_GRANT_DAYS)
    )
    if expiry <= current:
        raise DeliveryGrantError("授权有效期必须晚于当前时间。")

    return {
        "tool_name": tool_name.strip(),
        "parameter_bounds": _check_bounds(parameter_bounds),
        "expires_at": expiry.isoformat(),
        "created_at": current.isoformat(),
        "created_by": created_by,
        # 撤销不用删行：置上 revoked_at 即失效，审计还能查到当初授了什么。
        "revoked_at": None,
    }


def validate_grant(
    grant: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """校验凭证本身；不检查具体参数。返回的字典是归一化后的副本。"""
    if not isinstance(grant, dict):
        raise DeliveryGrantError("定时任务没有投递授权。")

    if grant.get("revoked_at") is not None:
        raise DeliveryGrantDenied("该投递授权已被撤销。")

    tool_name = grant.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise DeliveryGrantError("授权缺少 tool_name。")

    expiry = _parse_expiry(grant.get("expires_at"))
    current = _as_utc(now or datetime.now(UTC))
    if expiry <= current:
        raise DeliveryGrantDenied("该投递授权已过期。")

    created_by = grant.get("created_by")
    if not isinstance(created_by, str) or not created_by:
        raise DeliveryGrantError("授权缺少 created_by。")

    return {
        "tool_name": tool_name.strip(),
        "parameter_bounds": _check_bounds(grant.get("parameter_bounds")),
        "expires_at": expiry.isoformat(),
        "created_at": str(grant.get("created_at") or ""),
        "created_by": created_by,
        "revoked_at": None,
    }


def check_delivery(
    grant: dict[str, Any] | None,
    *,
    tool_name: str,
    arguments: dict[str, Any] | None,
    now: datetime | None = None,
) -> None:
    """本次调用是否落在授权范围内；不在就抛 `DeliveryGrantDenied`。

    closed set：`arguments` 的键必须全在白名单里。少传键可以（比如可选 cc），
    多传一个键就是越界——「允许这 3 个收件人」不能因为请求顺手加了 bcc 失效。
    """
    checked = validate_grant(grant, now=now)

    if tool_name != checked["tool_name"]:
        raise DeliveryGrantDenied("投递工具不在授权范围内。")

    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise DeliveryGrantDenied("投递参数不是对象。")

    bounds = checked["parameter_bounds"]
    for key, value in arguments.items():
        if key not in bounds:
            # 只说键名，不说值：原因会进审计。
            raise DeliveryGrantDenied(f"投递参数 {key} 不在授权范围内。")

        expected = bounds[key]
        if expected == ANY_VALUE:
            continue

        if isinstance(expected, list):
            if not isinstance(value, (list, tuple)):
                raise DeliveryGrantDenied(f"投递参数 {key} 的形态与授权不符。")
            if any(item not in expected for item in value):
                raise DeliveryGrantDenied(f"投递参数 {key} 含未授权的值。")
            continue

        # 标量同型比较：挡住 1 == True 这类「类型不同但相等」的巧合。
        if type(value) is not type(expected) or value != expected:
            raise DeliveryGrantDenied(f"投递参数 {key} 与授权值不符。")

    # 白名单里声明但本次没传的键是允许的（可选参数），不做必填校验：
    # 「允许发这 3 个人」不等于「每次都必须发给 3 个人」。
