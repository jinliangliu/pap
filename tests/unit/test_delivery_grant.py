# tests/unit/test_delivery_grant.py
"""投递授权范围的纯逻辑：closed set、有效期、撤销、不回显参数值。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import delivery_grant_service as grants


def test_build_grant_defaults_to_90_days_and_no_scopes():
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    grant = grants.build_grant(
        tool_name="mail.send",
        parameter_bounds={"to": ["a@example.com", "b@example.com"], "body": "*"},
        created_by="user-1",
        now=now,
    )
    assert grant["tool_name"] == "mail.send"
    assert grant["created_by"] == "user-1"
    assert grant["revoked_at"] is None
    assert datetime.fromisoformat(grant["expires_at"]) == now + timedelta(days=90)
    # 提权字段不在凭证里：调用方想传也传不进去。
    assert "scopes" not in grant and "roles" not in grant
    with pytest.raises(TypeError):
        grants.build_grant(
            tool_name="mail.send",
            parameter_bounds={"to": "*"},
            created_by="user-1",
            scopes=["platform.admin"],  # type: ignore[call-arg]
        )


def test_check_delivery_accepts_subset_and_any_value():
    grant = grants.build_grant(
        tool_name="mail.send",
        parameter_bounds={"to": ["a@example.com", "b@example.com"], "body": "*"},
        created_by="user-1",
    )
    grants.check_delivery(
        grant,
        tool_name="mail.send",
        arguments={"to": ["a@example.com"], "body": "本周完成 X"},
    )
    # 白名单里声明但本次没传的键是允许的。
    grants.check_delivery(grant, tool_name="mail.send", arguments={"body": "hi"})


def test_check_delivery_rejects_extra_key_without_echoing_value():
    grant = grants.build_grant(
        tool_name="mail.send",
        parameter_bounds={"to": ["a@example.com"], "body": "*"},
        created_by="user-1",
    )
    with pytest.raises(grants.DeliveryGrantDenied) as caught:
        grants.check_delivery(
            grant,
            tool_name="mail.send",
            arguments={"to": ["a@example.com"], "bcc": "secret@example.com"},
        )
    message = str(caught.value)
    assert "bcc" in message
    assert "secret@example.com" not in message


def test_check_delivery_rejects_unlisted_tool_and_values():
    grant = grants.build_grant(
        tool_name="mail.send",
        parameter_bounds={"to": ["a@example.com"], "count": 3, "flag": True},
        created_by="user-1",
    )
    with pytest.raises(grants.DeliveryGrantDenied):
        grants.check_delivery(grant, tool_name="mail.other", arguments={})
    with pytest.raises(grants.DeliveryGrantDenied):
        grants.check_delivery(grant, tool_name="mail.send", arguments={"to": ["evil@x"]})
    # 1 == True 在 Python 里成立，但类型不同必须拒绝。
    with pytest.raises(grants.DeliveryGrantDenied):
        grants.check_delivery(grant, tool_name="mail.send", arguments={"flag": 1})
    with pytest.raises(grants.DeliveryGrantDenied):
        grants.check_delivery(grant, tool_name="mail.send", arguments={"count": True})


def test_expired_and_revoked_grants_are_denied():
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    expired = grants.build_grant(
        tool_name="mail.send",
        parameter_bounds={"to": "*"},
        created_by="user-1",
        expires_at=now - timedelta(seconds=1),
        now=now - timedelta(days=1),
    )
    with pytest.raises(grants.DeliveryGrantError):
        grants.build_grant(
            tool_name="mail.send",
            parameter_bounds={"to": "*"},
            created_by="user-1",
            expires_at=now - timedelta(seconds=1),
            now=now,
        )

    # 已存在的凭证过期：是「拒绝」不是「结构错误」。
    stale = dict(expired)
    with pytest.raises(grants.DeliveryGrantDenied):
        grants.check_delivery(
            stale, tool_name="mail.send", arguments={"to": "a@x"}, now=now
        )

    revoked = grants.build_grant(
        tool_name="mail.send",
        parameter_bounds={"to": "*"},
        created_by="user-1",
        now=now,
    )
    revoked["revoked_at"] = now.isoformat()
    with pytest.raises(grants.DeliveryGrantDenied):
        grants.check_delivery(
            revoked, tool_name="mail.send", arguments={"to": "a@x"}, now=now
        )


def test_missing_or_malformed_grant_never_allows_delivery():
    with pytest.raises(grants.DeliveryGrantError):
        grants.check_delivery(None, tool_name="mail.send", arguments={})
    with pytest.raises(grants.DeliveryGrantError):
        grants.check_delivery(
            {"tool_name": "mail.send", "expires_at": "not-a-time"},
            tool_name="mail.send",
            arguments={},
        )
    # 白名单为空等于什么都没授权，不能解释成「不限制」。
    with pytest.raises(grants.DeliveryGrantError):
        grants.build_grant(
            tool_name="mail.send", parameter_bounds={}, created_by="user-1"
        )
