# scripts/check_mcp_server.py
"""对外 MCP 通道自检。

自建一个只挂 MCP 端点的小应用，起在随机端口上，然后用**官方 mcp 客户端**
走一遍真实握手。这样验证的是「外部宿主真的能连上」，不是「函数能调用」——
后者单元测试已经覆盖了，前者才是这次改造的全部风险所在。

    python -m scripts.check_mcp_server
        # 不用任何已有环境：临时 sqlite + 现签一枚 PAT，
        # 断言 401 / 401 / 200 / 撤销后 401，并列出工具清单

    python -m scripts.check_mcp_server --call "西环站现在什么状态" --token pap_xxx
        # 连真实环境：用真实令牌打一次真实委派（需要库、模型与真实账号）

刻意用真实 HTTP 而不是 ASGI 内联：mount、路径前缀、中间件顺序
（`RequestMiddleware` → `Mount` → `BearerGate`）这几处只有真的过一遍
网络栈才看得出来。ASGI 内联会把这些全绕过去。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="pap-mcp-check-"))

# 默认模式完全隔离：自己的库、自己的令牌、不读 .env 里的任何身份。
# `--call` 模式才让 .env 生效（真实库 + 真实模型）。
DEFAULT_MODE = "--call" not in sys.argv

if DEFAULT_MODE:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP / 'check.db').as_posix()}"
    os.environ["CHECKPOINT_DATABASE_URL"] = ""
    os.environ["MAINTENANCE_ENABLED"] = "false"
    os.environ["MEMORY_EXTRACT_ENABLED"] = "false"
    os.environ["REDIS_URL"] = ""

os.environ.setdefault("MCP_INBOUND_ENABLED", "true")
# 默认模式**不配静态令牌**：那样自检才算真的走过 PAT 路径，
# 而不是被兜底令牌蒙混过关。
os.environ.setdefault("MCP_INBOUND_TOKEN", "" if DEFAULT_MODE else "check-token-0001")
os.environ.setdefault("MCP_INBOUND_SUBJECT", "check-subject")

STATIC_TOKEN = (os.environ.get("MCP_INBOUND_TOKEN") or "").strip()
CHECK_SUBJECT = os.environ.get("MCP_INBOUND_SUBJECT", "check-subject")
CHECK_TENANT = "default"


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _build_app():
    from fastapi import FastAPI

    from app.mcp_server import build_asgi, get_session_manager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        manager = get_session_manager()
        if manager is None:
            raise RuntimeError("会话管理器未初始化：build_asgi() 没有被调用。")
        async with manager.run():
            yield

    api = FastAPI(lifespan=lifespan)
    # 与 app/main.py 保持一致：挂根路径，由 BearerGate 判前缀。
    api.mount("/", app=build_asgi())
    return api


def _serve(port: int) -> threading.Thread:
    import uvicorn

    config = uvicorn.Config(
        _build_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return thread
        except OSError:
            time.sleep(0.1)

    raise RuntimeError("自检服务没能在 20 秒内起来。")


async def _prepare_db() -> tuple[str, str]:
    """建表 + 种一个用户 + 现签一枚 PAT。返回 `(明文令牌, 令牌 id)`。"""
    from sqlalchemy import select

    from app.auth.dependencies import resolve_identity
    from app.db.base import Base
    from app.db.session import async_session_factory, engine
    from app.models.rbac import Role, RolePermission, User, UserRole
    from app.models.tenant import Tenant, TenantStatus
    from app.services import api_token_service

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as session:
        async with session.begin():
            if await session.get(Tenant, CHECK_TENANT) is None:
                session.add(
                    Tenant(id=CHECK_TENANT, name=CHECK_TENANT, status=TenantStatus.ACTIVE)
                )

            role = await session.scalar(select(Role).where(Role.name == "checker"))
            if role is None:
                role = Role(name="checker", description="MCP 自检角色")
                session.add(role)
                await session.flush()
                for permission in (
                    "tools:read",
                    "station.read",
                    "knowledge.read",
                    # 上传端点要 knowledge:write：不给它，自检永远只验到 403。
                    "knowledge:write",
                    "knowledge.internal",
                ):
                    session.add(RolePermission(role_id=role.id, permission=permission))

            user = await session.scalar(
                select(User).where(User.external_subject == CHECK_SUBJECT)
            )
            if user is None:
                user = User(
                    external_subject=CHECK_SUBJECT,
                    display_name=CHECK_SUBJECT,
                    enabled=True,
                )
                session.add(user)
                await session.flush()

            exists = await session.scalar(
                select(UserRole).where(
                    UserRole.tenant_id == CHECK_TENANT,
                    UserRole.user_id == user.id,
                )
            )
            if exists is None:
                session.add(
                    UserRole(tenant_id=CHECK_TENANT, user_id=user.id, role_id=role.id)
                )

        identity = await resolve_identity(
            session, subject=CHECK_SUBJECT, tenant_id=CHECK_TENANT
        )

        # resolve_identity 是只读的，但它会 autobegin 一个新事务；
        # 不先回滚，下一步 session.begin() 会撞上「事务已经开始」。
        await session.rollback()

        async with session.begin():
            row, raw = await api_token_service.issue_token(
                session,
                identity=identity,
                name="自检令牌",
                expires_in_days=1,
            )
            token_id = str(row.id)

    return raw, token_id


def _initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pap-check", "version": "1"},
        },
    }


def _check_auth(url: str, token: str | None) -> list[str]:
    """逐条钉住「没有凭据就是 401」。"""
    import httpx

    problems: list[str] = []
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    with httpx.Client(timeout=10) as client:
        bare = client.post(url, json=_initialize_payload(), headers=headers)
        if bare.status_code != 401:
            problems.append(f"无令牌应返回 401，实际 {bare.status_code}")

        wrong = client.post(
            url,
            json=_initialize_payload(),
            headers={**headers, "Authorization": "Bearer pap_not-a-real-token"},
        )
        if wrong.status_code != 401:
            problems.append(f"错误令牌应返回 401，实际 {wrong.status_code}")

        if token:
            good = client.post(
                url,
                json=_initialize_payload(),
                headers={**headers, "Authorization": f"Bearer {token}"},
            )
            if good.status_code != 200:
                problems.append(f"有效令牌应返回 200，实际 {good.status_code}")

    return problems


def _check_upload(url: str, token: str) -> list[str]:
    """上传端点：真走网络栈。

    单测里 `handle_upload` 是被直接调用的，multipart 由 httpx 在进程内构造，
    走不到 uvicorn 的 body 读取与真实的反代路径。这里补的就是那一段：
    路由分流（`/mcp/upload` 不能被 MCP 协议吞掉）、Bearer 门槛、入库、幂等。
    """
    problems: list[str] = []
    target = url.rstrip("/") + "/upload"
    body = "# 自检文档\n\n上传端点的自检内容，用于确认 multipart 与入库链路。\n".encode()
    files = {"file": ("check-upload.md", body, "text/markdown")}

    import httpx

    with httpx.Client(timeout=30) as client:
        bare = client.post(target, files=files)
        if bare.status_code != 401:
            problems.append(
                f"上传端点无令牌应 401，实际 {bare.status_code}——"
                "它可能没走在 BearerGate 内侧"
            )

        auth = {"Authorization": f"Bearer {token}"}

        first = client.post(
            target,
            files={"file": ("check-upload.md", body, "text/markdown")},
            data={"title": "MCP 上传自检", "access_scope": "internal"},
            headers=auth,
        )

        if first.status_code != 200:
            problems.append(
                f"上传应 200，实际 {first.status_code}：{first.text[:200]}"
            )
            return problems

        created = first.json()
        document = created.get("document") or {}

        if created.get("action") != "created":
            problems.append(f"首次上传应 created，实际 {created.get('action')}")
        if int(document.get("chunk_count") or 0) < 1:
            problems.append(f"入库后切片数为 {document.get('chunk_count')}，应为正数")
        if not document.get("id"):
            problems.append("上传返回里没有 document.id")

        again = client.post(
            target,
            files={"file": ("check-upload.md", body, "text/markdown")},
            data={"title": "MCP 上传自检"},
            headers=auth,
        )
        if again.status_code != 200 or again.json().get("action") != "unchanged":
            problems.append(
                "同一份内容重复上传应返回 unchanged，"
                f"实际 {again.status_code} / {(again.json() or {}).get('action')}"
            )

        bad = client.post(
            target,
            files={"file": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
            headers=auth,
        )
        if bad.status_code != 400:
            problems.append(f"不支持的后缀应 400，实际 {bad.status_code}")

        wrong_method = client.get(target, headers=auth)
        if wrong_method.status_code != 405:
            problems.append(f"GET 上传端点应 405，实际 {wrong_method.status_code}")

    return problems


async def _revoke(token_id: str) -> None:
    from app.db.session import async_session_factory
    from app.services import api_token_service

    from app.auth.dependencies import resolve_identity

    async with async_session_factory() as session:
        identity = await resolve_identity(
            session, subject=CHECK_SUBJECT, tenant_id=CHECK_TENANT
        )
        # 只读解析会 autobegin，先回滚再开写事务（同上）。
        await session.rollback()

        async with session.begin():
            await api_token_service.revoke_token(
                session,
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                token_id=token_id,
            )


async def _list_tools(url: str, token: str) -> tuple[list, str]:
    from httpx2 import AsyncClient
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    ) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                # 返回整个工具对象而不是名字：注解只在这一层看得见，
                # 而注解是宿主决定要不要提醒用户的唯一依据。
                # instructions 同理——模型看不到仓库文档，只看得到这一段。
                return list(listed.tools), (initialized.instructions or "")


def _check_instructions(text: str) -> list[str]:
    """服务说明是模型唯一看得到的「使用方式」。

    它不像文档那样有人读，所以更容易在重构时被悄悄删掉或改坏。
    三个关键词各守一件事：文件怎么交、反问怎么带回、审批去哪确认。
    """
    problems: list[str] = []

    if not text.strip():
        problems.append("initialize 没有返回 instructions，模型不知道该怎么用这个服务")
        return problems

    for needle, why in (
        ("/upload", "没提上传端点，模型遇到交文件会去读正文再塞进 task"),
        ("needs_input", "没提 needs_input，反问会被当成结论转述"),
        ("awaiting_approval", "没提 awaiting_approval，用户不知道该去哪确认"),
    ):
        if needle not in text:
            problems.append(f"instructions 缺少 {needle}：{why}")

    return problems


def _check_capabilities(listed: list) -> list[str]:
    """注解与入参必须真的从网络上回来，而不是只存在于注册代码里。

    `readOnlyHint=false` 是宿主决定「自动执行前要不要提醒」的唯一依据。
    它要是丢了，宿主会把委派当成一次无风险查询静默跑掉——不报错，
    只是那些本该由人确认的写操作从此没有人再看一眼。
    """
    problems: list[str] = []
    by_name = {tool.name: tool for tool in listed}

    delegate = by_name.get("pap_delegate")
    if delegate is None:
        return problems

    annotations = delegate.annotations
    if annotations is None:
        problems.append("pap_delegate 没有 annotations，宿主无法判断副作用")
    else:
        if annotations.read_only_hint is not False:
            problems.append("pap_delegate 的 readOnlyHint 不是 false")
        if annotations.destructive_hint is not False:
            problems.append("pap_delegate 的 destructiveHint 不是 false")
        if annotations.idempotent_hint is not False:
            problems.append("pap_delegate 的 idempotentHint 不是 false")
        if annotations.open_world_hint is not True:
            problems.append("pap_delegate 的 openWorldHint 不是 true")

    # 客户端侧的模型是 snake_case，线上是 camelCase，SDK 负责转换。
    schema = delegate.input_schema or {}
    if "allow_write" not in (schema.get("properties") or {}):
        problems.append("pap_delegate 的入参里没有 allow_write")

    status = by_name.get("pap_task_status")
    if status is not None and status.annotations is not None:
        if status.annotations.read_only_hint is not True:
            problems.append("pap_task_status 的 readOnlyHint 不是 true")

    return problems


async def _delegate(url: str, token: str, task: str) -> str:
    from httpx2 import AsyncClient
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=180.0,
    ) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("pap_delegate", {"task": task})
                return "\n".join(
                    getattr(block, "text", "") for block in result.content
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--call", default=None, help="额外跑一次真实委派（需真实环境）")
    parser.add_argument(
        "--token",
        default=None,
        help="--call 用的真实 PAT；默认模式不需要（脚本自己签一枚）",
    )
    args = parser.parse_args()

    problems: list[str] = []
    token = args.token or STATIC_TOKEN
    token_id: str | None = None

    if args.call and not args.token:
        print("FAIL  --call 必须配合 --token（用控制台「访问令牌」签发的那一枚）")
        return 1

    if DEFAULT_MODE:
        try:
            token, token_id = asyncio.run(_prepare_db())
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  建库或签令牌失败：{type(exc).__name__}: {exc}")
            return 1
        print(f"已生成自检令牌：{token[:12]}…（临时库 {_TMP}）")

    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    _serve(port)

    problems.extend(_check_auth(url, token))

    names: list[str] = []
    if token:
        try:
            listed, instructions = asyncio.run(_list_tools(url, token))
            names = [tool.name for tool in listed]
            problems.extend(_check_capabilities(listed))
            problems.extend(_check_instructions(instructions))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"握手或 tools/list 失败：{type(exc).__name__}: {exc}")

    expected = {"pap_delegate", "pap_task_status"}
    if names and set(names) != expected:
        problems.append(f"工具集不符：期望 {sorted(expected)}，实际 {sorted(names)}")

    # 上传端点排在这里：它必须在撤销之前跑，撤销之后同一枚令牌就该 401 了。
    if token:
        try:
            problems.extend(_check_upload(url, token))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"上传端点自检失败：{type(exc).__name__}: {exc}")

    # 撤销之后必须立刻 401。这是 P1 的全部意义：撤销若不能立即生效，
    # 那它只是「等它过期」的另一种说法。
    if token_id is not None:
        try:
            asyncio.run(_revoke(token_id))
            after = _check_after_revoke(url, token)
            if after is not None:
                problems.append(after)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"撤销验证失败：{type(exc).__name__}: {exc}")

    if args.call:
        try:
            print(f"--- 委派：{args.call}")
            print(asyncio.run(_delegate(url, args.token, args.call)))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"委派调用失败：{type(exc).__name__}: {exc}")

    print(f"tools: {names}")

    if problems:
        for item in problems:
            print(f"FAIL  {item}")
        return 1

    print("OK    鉴权、握手、工具清单、上传端点、撤销即时生效均符合预期")
    return 0


def _check_after_revoke(url: str, token: str) -> str | None:
    import httpx

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }

    with httpx.Client(timeout=10) as client:
        resp = client.post(url, json=_initialize_payload(), headers=headers)

    if resp.status_code != 401:
        return f"撤销后应立即 401，实际 {resp.status_code}"

    return None


if __name__ == "__main__":
    sys.exit(main())
