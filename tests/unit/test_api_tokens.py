# tests/unit/test_api_tokens.py
"""个人访问令牌：签发、校验、撤销、隔离。

这里守的四件事，全都是「不报错但会出事」的类型：

1. 库里出现明文令牌（存了就能被任何读库的人直接拿去用）；
2. 撤销之后还能用（那撤销就是装饰品）；
3. 跨用户 / 跨租户能读到或撤销别人的令牌（这张表没有 RLS，靠代码自己带条件）；
4. 过期判断在 sqlite 上永远不会生效（naive / aware 比较抛异常被吞掉）。
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import delete, select

from app.auth.dependencies import resolve_identity
from app.mcp_server import auth
from app.models.api_token import ApiToken
from app.services import api_token_service
from app.services.api_token_service import MAX_ACTIVE_TOKENS, TokenLimitError


async def _identity(session, seeded_identity) -> object:
    return await resolve_identity(
        session,
        subject=seeded_identity["subject"],
        tenant_id=seeded_identity["tenant_id"],
    )


@pytest.fixture(autouse=True)
async def _clean_tokens(session):
    """每个用例从空表开始。

    令牌表是全库共享的（没有 RLS），而上限是按用户数「有效枚数」——
    不清理的话，哪个用例先跑会决定另一个用例撞不撞上限。
    """
    await session.execute(delete(ApiToken))
    await session.commit()
    yield


# ---------------- 签发 ----------------


async def test_issue_stores_hash_not_plaintext(session, seeded_identity):
    identity = await _identity(session, seeded_identity)
    row, raw = await api_token_service.issue_token(
        session, identity=identity, name="笔记本"
    )
    await session.commit()

    assert raw.startswith(api_token_service.TOKEN_PREFIX)
    assert row.token_hash == api_token_service.hash_token(raw)
    # 明文不能出现在行里任何一个字段上。
    assert raw not in (row.token_hash, row.prefix, row.name)
    assert row.prefix == raw[: api_token_service.DISPLAY_PREFIX_CHARS]


async def test_issue_defaults_unexpired_when_zero_days(session, seeded_identity):
    identity = await _identity(session, seeded_identity)
    row, _ = await api_token_service.issue_token(
        session, identity=identity, name="长期", expires_in_days=None
    )
    await session.commit()

    assert row.expires_at is None
    assert row.is_active() is True


async def test_issue_caps_active_tokens(session, seeded_identity):
    """上限挡的是「签过几十枚、每枚都能用，而用户以为旧的早废了」。"""
    identity = await _identity(session, seeded_identity)
    for i in range(MAX_ACTIVE_TOKENS):
        await api_token_service.issue_token(session, identity=identity, name=f"t{i}")
    await session.commit()

    with pytest.raises(TokenLimitError):
        await api_token_service.issue_token(session, identity=identity, name="overflow")


# ---------------- 校验 ----------------


async def test_resolve_token_returns_identity(session, seeded_identity):
    identity = await _identity(session, seeded_identity)
    _, raw = await api_token_service.issue_token(session, identity=identity, name="x")
    await session.commit()

    resolved = await api_token_service.resolve_token(session, raw)

    assert resolved is not None
    assert resolved.user_id == seeded_identity["user_id"]
    assert resolved.tenant_id == seeded_identity["tenant_id"]


async def test_resolve_token_rejects_unknown_and_empty(session, seeded_identity):
    assert await api_token_service.resolve_token(session, "pap_nope") is None
    assert await api_token_service.resolve_token(session, "") is None


async def test_resolve_token_rejects_expired(session, seeded_identity):
    identity = await _identity(session, seeded_identity)
    row, raw = await api_token_service.issue_token(
        session, identity=identity, name="过期", expires_in_days=1
    )
    row.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=1
    )
    await session.commit()

    assert await api_token_service.resolve_token(session, raw) is None


async def test_resolve_token_rejects_revoked(session, seeded_identity):
    identity = await _identity(session, seeded_identity)
    row, raw = await api_token_service.issue_token(session, identity=identity, name="撤销")
    await session.commit()

    await api_token_service.revoke_token(
        session,
        user_id=identity.user_id,
        tenant_id=identity.tenant_id,
        token_id=row.id,
    )
    await session.commit()

    assert await api_token_service.resolve_token(session, raw) is None


# ---------------- 门槛：PAT 走通 + 撤销立即失效 ----------------


async def _gate_status(token: str | None) -> tuple[int, object]:
    seen = {"called": False, "identity": None}

    async def downstream(scope, receive, send):
        seen["called"] = True
        seen["identity"] = (scope.get("state") or {}).get(auth.STATE_KEY)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    gate = auth.BearerGate(downstream)

    scope = {
        "type": "http",
        "path": "/mcp",
        "method": "POST",
        "headers": (
            [(b"authorization", f"Bearer {token}".encode())] if token else []
        ),
    }
    captured: dict[str, int] = {}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await gate(scope, receive, send)
    return captured["status"], seen["identity"]


async def test_gate_accepts_pat_and_carries_identity(session, seeded_identity, monkeypatch):
    # 静态令牌关掉，确保走的是 PAT 路径，而不是被兜底令牌蒙混过关。
    monkeypatch.setattr(auth.settings, "mcp_inbound_token", "")

    identity = await _identity(session, seeded_identity)
    _, raw = await api_token_service.issue_token(session, identity=identity, name="gate")
    await session.commit()

    status_code, cached = await _gate_status(raw)

    assert status_code == 200
    assert cached is not None
    assert cached.user_id == seeded_identity["user_id"]


async def test_gate_rejects_right_after_revoke(session, seeded_identity, monkeypatch):
    """撤销必须**立刻**生效：下一次调用就 401，不等任何缓存。"""
    monkeypatch.setattr(auth.settings, "mcp_inbound_token", "")

    identity = await _identity(session, seeded_identity)
    row, raw = await api_token_service.issue_token(session, identity=identity, name="r")
    await session.commit()

    assert (await _gate_status(raw))[0] == 200

    await api_token_service.revoke_token(
        session,
        user_id=identity.user_id,
        tenant_id=identity.tenant_id,
        token_id=row.id,
    )
    await session.commit()

    assert (await _gate_status(raw))[0] == 401


# ---------------- 接口 ----------------


async def test_create_returns_plaintext_once_and_list_hides_it(client):
    created = await client.post("/api/v1/me/tokens", json={"name": "连接器"})
    assert created.status_code == 201
    body = created.json()
    assert body["token"].startswith(api_token_service.TOKEN_PREFIX)
    assert body["prefix"] == body["token"][: api_token_service.DISPLAY_PREFIX_CHARS]

    listed = await client.get("/api/v1/me/tokens")
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert items and "token" not in items[0]


async def test_delete_is_idempotent(client):
    """重复撤销返回 204，不是 404。

    DELETE 的语义是「确保它不能用」，第二次调用达到的状态与第一次相同。
    返回 404 会让脚本里「批量撤销」在重试时误判成「令牌不存在了」，
    从而去追一个并不存在的问题。
    """
    created = await client.post("/api/v1/me/tokens", json={"name": "一次性"})
    token_id = created.json()["id"]

    first = await client.delete(f"/api/v1/me/tokens/{token_id}")
    assert first.status_code == 204

    second = await client.delete(f"/api/v1/me/tokens/{token_id}")
    assert second.status_code == 204


async def test_other_user_cannot_revoke(client, other_client):
    """这张表没有 RLS，隔离全靠带上 user_id。漏掉一个条件就是跨租户撤销。"""
    created = await client.post("/api/v1/me/tokens", json={"name": "我的"})
    token_id = created.json()["id"]

    resp = await other_client.delete(f"/api/v1/me/tokens/{token_id}")

    assert resp.status_code == 404


async def test_other_user_list_is_empty(client, other_client):
    await client.post("/api/v1/me/tokens", json={"name": "我的"})

    listed = await other_client.get("/api/v1/me/tokens")

    assert listed.status_code == 200
    assert listed.json()["items"] == []


async def test_token_row_visible_after_revoke_in_list(client):
    """撤销过的仍要出现在列表里，且 active=false。

    否则用户会以为那枚还在某台机器上跑着，从而不敢确定该不该再签一枚。
    """
    created = await client.post("/api/v1/me/tokens", json={"name": "旧"})
    token_id = created.json()["id"]
    await client.delete(f"/api/v1/me/tokens/{token_id}")

    listed = await client.get("/api/v1/me/tokens")
    row = [item for item in listed.json()["items"] if item["id"] == token_id][0]

    assert row["active"] is False
    assert isinstance(row["name"], str)


async def test_stored_rows_never_contain_plaintext(session, client):
    created = await client.post("/api/v1/me/tokens", json={"name": "审计"})
    raw = created.json()["token"]

    rows = list(await session.scalars(select(ApiToken)))
    dump = " ".join(
        f"{r.name}|{r.prefix}|{r.token_hash}|{r.tenant_id}|{r.user_id}" for r in rows
    )

    assert raw not in dump
