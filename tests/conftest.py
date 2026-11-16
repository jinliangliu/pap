# tests/conftest.py
"""测试环境装配。

注意：环境变量必须在导入 app 之前设置，因为 Settings 在导入时实例化。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="pap-test-"))

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["DATABASE_ECHO"] = "false"
os.environ["CHECKPOINT_DATABASE_URL"] = ""  # 测试用进程内 checkpointer，不连 PG
os.environ["OPENAI_API_KEY"] = ""
os.environ["OPENAI_MODEL"] = "test-model"
os.environ["MAINTENANCE_ENABLED"] = "false"
# 记忆自动抽取在每轮对话结束时起一个后台任务。测试里默认关掉：
# 它要调模型、还要在测试库上再开一个 session，跟用例本身无关只会制造噪音。
# 需要验证抽取自身行为的用例会显式打开开关并注入假的模型调用。
os.environ["MEMORY_EXTRACT_ENABLED"] = "false"
os.environ["REDIS_URL"] = ""
os.environ["STATION_PLATFORM_BASE_URL"] = ""
# 沙箱默认关闭：未配置地址时 execute_code 不应被注册，
# 这样测试里的工具目录与“没接沙箱”的部署保持一致。
os.environ["SANDBOX_BASE_URL"] = ""
os.environ["SANDBOX_API_KEY"] = ""
os.environ["KNOWLEDGE_DIR"] = str(_TMP / "knowledge")
# 上传目录必须与扫描目录分开，且测试里要落在临时目录：
# 否则「上传 → 目录同步」的用例会互相踩，也会往仓库的 data/ 里写文件。
os.environ["KNOWLEDGE_UPLOAD_DIR"] = str(_TMP / "knowledge_uploads")
os.environ["KNOWLEDGE_STORE_FILE"] = str(_TMP / "chunks.json")
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["DEV_SUBJECT"] = "test-user"
os.environ["ARTIFACT_STORAGE_DIR"] = str(_TMP / "artifacts")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agents.planner import reset_planner, set_planner_for_tests  # noqa: E402
from app.agents.schemas import ActionPlan  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.checkpoint import (  # noqa: E402
    close_checkpointer,
    get_checkpointer,
    init_checkpointer,
)
from app.db.session import async_session_factory, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.rbac import (  # noqa: E402
    Role,
    RolePermission,
    StationGrant,
    User,
    UserRole,
)
from app.models.tenant import Tenant, TenantStatus  # noqa: E402

TEST_TENANT = "default"
OTHER_TENANT = "tenant-b"
PLATFORM_TENANT = "platform"

TEST_SUBJECT = "test-user"
OTHER_SUBJECT = "other-user"
ADMIN_SUBJECT = "platform-admin"

TEST_STATION = "station-001"

TEST_PERMISSIONS = (
    "tools:read",
    "knowledge.read",
    "knowledge.public",
    "knowledge.internal",
    "station.read",
    "maintenance.ticket.create",
    # 连接器凭据：外部账号属于组织，租户成员里的管理员配自己的那一份。
    # 连接器**定义**仍然要求平台管理员（见 connectors 路由）。
    "connector:write",
)

OTHER_PERMISSIONS = ("tools:read", "knowledge.read", "knowledge.internal")

PLATFORM_ADMIN_PERMISSIONS = ("platform.admin", "tools:read")


class FakePlanner:
    """按顺序返回预置 ActionPlan，避免测试触达真实模型。"""

    def __init__(self, plans: list[ActionPlan]) -> None:
        self._plans = list(plans)
        self.calls = 0

    async def ainvoke(self, messages) -> ActionPlan:  # noqa: ANN001
        self.calls += 1
        if not self._plans:
            return ActionPlan(action="chat", response="没有更多预置计划。")
        return self._plans.pop(0)


async def _ensure_tenant(session, tenant_id: str, name: str) -> None:
    if await session.get(Tenant, tenant_id) is None:
        session.add(Tenant(id=tenant_id, name=name, status=TenantStatus.ACTIVE))
        await session.flush()


async def _ensure_role(session, name: str, permissions: tuple[str, ...]) -> Role:
    role = await session.scalar(select(Role).where(Role.name == name))

    if role is None:
        role = Role(name=name, description=name)
        session.add(role)
        await session.flush()

        for permission in permissions:
            session.add(RolePermission(role_id=role.id, permission=permission))

    return role


async def _ensure_member(
    session,
    *,
    subject: str,
    tenant_id: str,
    role: Role,
) -> User:
    user = await session.scalar(select(User).where(User.external_subject == subject))

    if user is None:
        user = User(external_subject=subject, display_name=subject, enabled=True)
        session.add(user)
        await session.flush()

    exists = await session.scalar(
        select(UserRole).where(
            UserRole.tenant_id == tenant_id,
            UserRole.user_id == user.id,
        )
    )

    if exists is None:
        session.add(UserRole(tenant_id=tenant_id, user_id=user.id, role_id=role.id))
        await session.flush()

    return user


@pytest.fixture(scope="session", autouse=True)
async def _database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


@pytest.fixture(scope="session", autouse=True)
async def _runtime(_database):
    from app.agents.graph import init_agent_graph

    await init_checkpointer()
    init_agent_graph(get_checkpointer())

    yield

    await close_checkpointer()


@pytest.fixture
async def session():
    async with async_session_factory() as db:
        yield db


@pytest.fixture
async def seeded_identity(session):
    """建立三个租户、三个身份，覆盖同租户/跨租户/平台管理员三种视角。"""
    await _ensure_tenant(session, TEST_TENANT, "默认租户")
    await _ensure_tenant(session, OTHER_TENANT, "另一个租户")
    await _ensure_tenant(session, PLATFORM_TENANT, "平台")

    tester = await _ensure_role(session, "tester", TEST_PERMISSIONS)
    other = await _ensure_role(session, "other", OTHER_PERMISSIONS)
    admin = await _ensure_role(session, "platform_admin", PLATFORM_ADMIN_PERMISSIONS)

    user = await _ensure_member(
        session, subject=TEST_SUBJECT, tenant_id=TEST_TENANT, role=tester
    )
    await _ensure_member(
        session, subject=OTHER_SUBJECT, tenant_id=OTHER_TENANT, role=other
    )
    await _ensure_member(
        session, subject=ADMIN_SUBJECT, tenant_id=PLATFORM_TENANT, role=admin
    )

    grant = await session.scalar(
        select(StationGrant).where(
            StationGrant.tenant_id == TEST_TENANT,
            StationGrant.user_id == user.id,
            StationGrant.station_id == TEST_STATION,
        )
    )

    if grant is None:
        session.add(
            StationGrant(
                tenant_id=TEST_TENANT,
                user_id=user.id,
                station_id=TEST_STATION,
                can_read=True,
                can_write=True,
            )
        )

    await session.commit()

    return {"user_id": str(user.id), "subject": TEST_SUBJECT, "tenant_id": TEST_TENANT}


def _client(headers: dict[str, str]) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers=headers,
    )


@pytest.fixture
async def client(seeded_identity):
    async with _client(
        {
            "X-Authenticated-Subject": TEST_SUBJECT,
            "X-Authenticated-Tenant": TEST_TENANT,
        }
    ) as http_client:
        yield http_client


@pytest.fixture
async def other_client(seeded_identity):
    async with _client(
        {
            "X-Authenticated-Subject": OTHER_SUBJECT,
            "X-Authenticated-Tenant": OTHER_TENANT,
        }
    ) as http_client:
        yield http_client


@pytest.fixture
async def admin_client(seeded_identity):
    async with _client(
        {
            "X-Authenticated-Subject": ADMIN_SUBJECT,
            "X-Authenticated-Tenant": PLATFORM_TENANT,
        }
    ) as http_client:
        yield http_client


@pytest.fixture
def planner():
    def _install(plans: list[ActionPlan]) -> FakePlanner:
        fake = FakePlanner(plans)
        set_planner_for_tests(fake)
        return fake

    yield _install
    reset_planner()
