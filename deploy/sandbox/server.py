#!/usr/bin/env python3
"""多租户代码沙箱服务（第一阶段：Docker + gVisor）。

设计要点：

- **每次执行一个一次性容器**，容器销毁即数据消失，租户之间没有共享状态；
- **容器无网络、只读根、无宿主挂载**，代码看不到平台文件，也带不出数据；
- **租户来自请求头**，执行记录按租户隔离，跨租户回读一律 404；
- **每租户并发配额**，单个租户无法占满全机算力；
- 启动与执行前都不信任客户端：代码大小、语言、超时都在服务端再校验一次。

内部协议见 docs/SANDBOX.md。生产部署必须用 ``PAP_SANDBOX_BACKEND=runsc``；
``process`` 后端只用于本地联调，没有可用的隔离边界。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

try:  # 作为包导入（pytest / python -m deploy.sandbox.server）
    from . import runtime as backend
    from .runtime import (
        BackendRejected,
        BackendUnavailable,
        IsolationProfile,
        Outcome,
    )
except ImportError:  # uvicorn server:app 在 /opt/pap-sandbox 下直接运行
    import runtime as backend  # type: ignore[no-redef]
    from runtime import (  # type: ignore[no-redef]
        BackendRejected,
        BackendUnavailable,
        IsolationProfile,
        Outcome,
    )

logger = logging.getLogger("pap.sandbox")

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

# 在类体里 `backend` 会被同名字段遮蔽，先取出来避免踩坑。
DEFAULT_IMAGE = backend.DEFAULT_IMAGE
DEFAULT_RUNTIME = backend.DEFAULT_RUNTIME


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _build_profile() -> IsolationProfile:
    return IsolationProfile(
        runtime=os.environ.get("PAP_SANDBOX_RUNTIME", DEFAULT_RUNTIME).strip(),
        memory_mb=_env_int("PAP_SANDBOX_MEMORY_MB", 512),
        cpus=_env_float("PAP_SANDBOX_CPUS", 1.0),
        pids_limit=_env_int("PAP_SANDBOX_PIDS_LIMIT", 64),
        work_tmpfs_mb=_env_int("PAP_SANDBOX_WORK_TMPFS_MB", 64),
        tmp_tmpfs_mb=_env_int("PAP_SANDBOX_TMP_TMPFS_MB", 16),
        nofile=_env_int("PAP_SANDBOX_NOFILE", 128),
    )


@dataclass
class SandboxSettings:
    backend: str = os.environ.get("PAP_SANDBOX_BACKEND", "runsc").strip().lower()
    image: str = os.environ.get("PAP_SANDBOX_IMAGE", DEFAULT_IMAGE).strip()
    api_key: str = (os.environ.get("PAP_SANDBOX_API_KEY") or "").strip()
    max_concurrent: int = _env_int("PAP_SANDBOX_MAX_CONCURRENT", 4)
    max_per_tenant: int = _env_int("PAP_SANDBOX_MAX_PER_TENANT", 2)
    queue_timeout_seconds: float = _env_float("PAP_SANDBOX_QUEUE_TIMEOUT_SECONDS", 5.0)
    execution_ttl_seconds: int = _env_int("PAP_SANDBOX_EXECUTION_TTL_SECONDS", 600)
    idempotency_ttl_seconds: int = _env_int("PAP_SANDBOX_IDEMPOTENCY_TTL_SECONDS", 600)
    container_ttl_seconds: int = _env_int("PAP_SANDBOX_CONTAINER_TTL_SECONDS", 900)
    max_output_chars: int = _env_int("PAP_SANDBOX_MAX_OUTPUT_CHARS", 200_000)
    max_code_bytes: int = _env_int("PAP_SANDBOX_MAX_CODE_BYTES", 64_000)
    sweep_interval_seconds: float = _env_float("PAP_SANDBOX_SWEEP_INTERVAL_SECONDS", 60.0)
    profile: IsolationProfile = field(default_factory=_build_profile)


settings = SandboxSettings()

if settings.backend not in {"runsc", "process"}:
    raise ValueError(
        "PAP_SANDBOX_BACKEND 只能是 runsc 或 process；"
        f"当前为 {settings.backend!r}。process 后端没有隔离，不得用于多租户。"
    )


class ExecuteRequest(BaseModel):
    language: str
    code: str = Field(min_length=1)
    stdin: str = ""
    timeout_seconds: int = Field(default=30, ge=1, le=300)


@dataclass(frozen=True, slots=True)
class StoredRecord:
    created_at: float
    tenant_id: str
    payload: dict[str, Any]
    user_id: str = ""
    conversation_id: str = ""


class QuotaExceeded(RuntimeError):
    """并发配额已满。"""


class Quota:
    """全局 + 租户两级并发闸门。

    只用全局信号量时，一个租户就能把并发占满，其它租户全部排队；
    加一层租户配额后，噪声邻居的影响被限制在自己的额度内。
    """

    def __init__(self, *, max_concurrent: int, max_per_tenant: int) -> None:
        self._max = max(1, max_concurrent)
        self._per_tenant = max(1, max_per_tenant)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._global: asyncio.Semaphore | None = None
        self._tenants: dict[str, asyncio.Semaphore] = {}
        self._active = 0

    def _bind(self) -> None:
        # 信号量绑定事件循环；测试里每个 TestClient 会新建一个循环。
        loop = asyncio.get_running_loop()

        if loop is not self._loop:
            self._loop = loop
            self._global = asyncio.Semaphore(self._max)
            self._tenants = {}
            self._active = 0

    @property
    def active(self) -> int:
        return self._active

    async def acquire(self, tenant_id: str, *, timeout: float) -> None:
        self._bind()
        assert self._global is not None  # noqa: S101

        tenant_slot = self._tenants.setdefault(tenant_id, asyncio.Semaphore(self._per_tenant))

        try:
            async with asyncio.timeout(timeout):
                await tenant_slot.acquire()

                try:
                    await self._global.acquire()
                except BaseException:
                    tenant_slot.release()
                    raise
        except TimeoutError as exc:
            raise QuotaExceeded("沙箱并发配额已满，请稍后重试。") from exc

        self._active += 1

    def release(self, tenant_id: str) -> None:
        tenant_slot = self._tenants.get(tenant_id)

        if tenant_slot is not None:
            tenant_slot.release()

        if self._global is not None:
            self._global.release()

        self._active = max(0, self._active - 1)


quota = Quota(max_concurrent=settings.max_concurrent, max_per_tenant=settings.max_per_tenant)

_records: dict[str, StoredRecord] = {}
_idempotent: dict[str, tuple[float, dict[str, Any]]] = {}
_inflight: set[str] = set()
_probe_cache: tuple[float, dict[str, Any]] | None = None


# ---------------------------------------------------------------- 鉴权与租户


def authorize(request: Request, authorization: str | None) -> None:
    """服务端鉴权。

    配了密钥就强制 Bearer；没配密钥时只允许回环访问——否则一个没设
    密钥的沙箱等于给全网提供免费算力与出网通道（虽然本实现不出网，
    但仍然可以被用来消耗 CPU）。
    """
    if settings.api_key:
        expected = f"Bearer {settings.api_key}"

        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=401,
                detail="沙箱鉴权失败。",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return

    host = request.client.host if request.client else ""

    if host not in LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="沙箱未配置 API Key，仅允许回环地址访问。",
        )


def resolve_tenant(request: Request) -> str:
    """租户标识必须来自可信调用方（平台），并经过白名单校验。"""
    try:
        return backend.validate_tenant(request.headers.get("X-Sandbox-Tenant"))
    except BackendRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")


def resolve_owner(request: Request) -> tuple[str, str]:
    """执行归属（用户、会话）。

    头由平台注入；缺失时为空串（兼容旧调用方），格式非法直接 400，
    避免把任意字符串写进执行记录并用于后续比对。
    """
    values: list[str] = []

    for header in ("X-Sandbox-User", "X-Sandbox-Conversation"):
        raw = (request.headers.get(header) or "").strip()

        if not raw:
            values.append("")
            continue

        if not _OWNER_PATTERN.match(raw):
            raise HTTPException(status_code=400, detail=f"{header} 格式无效。")

        values.append(raw)

    return values[0], values[1]


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= settings.max_output_chars:
        return text, False

    return text[: settings.max_output_chars] + "\n…（输出已截断）", True


# ---------------------------------------------------------------- 执行


async def _dispatch(
    *,
    tenant_id: str,
    execution_id: str,
    language: str,
    code: str,
    stdin: str,
    timeout_seconds: int,
) -> Outcome:
    if settings.backend == "process":
        # run_process 是同步阻塞的，必须放到线程里，否则会卡住事件循环。
        return await asyncio.to_thread(
            backend.run_process,
            language=language,
            code=code,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            profile=settings.profile,
        )

    return await backend.run_container(
        language=language,
        code=code,
        stdin=stdin,
        timeout_seconds=timeout_seconds,
        tenant_id=tenant_id,
        execution_id=execution_id,
        image=settings.image,
        profile=settings.profile,
    )


def _remember(
    execution_id: str,
    tenant_id: str,
    payload: dict[str, Any],
    *,
    user_id: str = "",
    conversation_id: str = "",
) -> None:
    now = time.time()

    if len(_records) >= 512:
        expired = [
            key
            for key, record in _records.items()
            if now - record.created_at > settings.execution_ttl_seconds
        ]

        for key in expired:
            _records.pop(key, None)

        # 仍然是满的就整体清空：这是缓存，不是审计存储。
        if len(_records) >= 512:
            _records.clear()

    _records[execution_id] = StoredRecord(
        created_at=now,
        tenant_id=tenant_id,
        payload=payload,
        user_id=user_id,
        conversation_id=conversation_id,
    )


# ---------------------------------------------------------------- 生命周期


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(settings.sweep_interval_seconds)

        try:
            removed = await backend.sweep_orphans(settings.container_ttl_seconds)
        except Exception:  # pragma: no cover - 清理失败不能拖垮服务
            logger.warning("清理孤儿容器失败。", exc_info=True)
            continue

        if removed:
            logger.info("已清理 %d 个孤儿沙箱容器。", removed)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if settings.backend == "runsc":
        try:
            removed = await asyncio.wait_for(
                backend.sweep_orphans(settings.container_ttl_seconds),
                timeout=20,
            )

            if removed:
                logger.info("启动清理：删除 %d 个上次残留的沙箱容器。", removed)
        except Exception:  # pragma: no cover - docker 不可用时不影响启动
            logger.warning("启动清理未完成（docker 可能不可用）。", exc_info=True)

        sweeper = asyncio.create_task(_sweep_loop())
    else:
        logger.warning("沙箱运行在 process 后端：没有网络、文件系统与内核隔离，仅限本地调试。")
        sweeper = None

    try:
        yield
    finally:
        if sweeper is not None:
            sweeper.cancel()


app = FastAPI(title="pap-sandbox", version="0.2.0", lifespan=lifespan)


# ---------------------------------------------------------------- 路由


@app.get("/v1/health")
async def health(response: Response) -> dict[str, Any]:
    global _probe_cache

    if settings.backend == "process":
        info: dict[str, Any] = {
            "ready": True,
            "runtime": "process",
            "isolation": "none",
            "detail": "process 后端没有隔离，仅限本地调试。",
        }
    else:
        now = time.time()

        if _probe_cache is None or now - _probe_cache[0] > 15:
            _probe_cache = (now, await backend.probe(settings.profile, settings.image))

        info = dict(_probe_cache[1])

    if not info.get("ready"):
        response.status_code = 503

    return {
        "status": "ok" if info.get("ready") else "degraded",
        **info,
        "backend": settings.backend,
        "active_executions": quota.active,
        "max_concurrent": settings.max_concurrent,
        "max_per_tenant": settings.max_per_tenant,
    }


@app.post("/v1/execute")
async def execute(
    payload: ExecuteRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """同步执行并返回终态。

    客户端也支持 202 + 轮询，但本实现直接返回终态，少一次往返。
    """
    authorize(request, authorization)
    tenant_id = resolve_tenant(request)
    user_id, conversation_id = resolve_owner(request)

    if payload.language not in backend.LANGUAGES:
        raise HTTPException(status_code=400, detail=f"不支持的语言：{payload.language}")

    if len(payload.code.encode("utf-8")) > settings.max_code_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"代码超过 {settings.max_code_bytes} 字节上限。",
        )

    cache_key = f"{tenant_id}:{idempotency_key}" if idempotency_key else ""

    if cache_key:
        cached = _idempotent.get(cache_key)

        if cached and time.time() - cached[0] < settings.idempotency_ttl_seconds:
            return cached[1]

        if cache_key in _inflight:
            raise HTTPException(
                status_code=409,
                detail="同一幂等键的请求正在执行，请勿重复提交。",
            )

        _inflight.add(cache_key)

    execution_id = uuid.uuid4().hex

    try:
        await quota.acquire(tenant_id, timeout=settings.queue_timeout_seconds)
    except QuotaExceeded as exc:
        _inflight.discard(cache_key)
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "2"},
        ) from exc

    try:
        outcome = await _dispatch(
            tenant_id=tenant_id,
            execution_id=execution_id,
            language=payload.language,
            code=payload.code,
            stdin=payload.stdin,
            timeout_seconds=payload.timeout_seconds,
        )
    except BackendRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BackendUnavailable as exc:
        # 5xx：平台侧会转成 SANDBOX_UNAVAILABLE，且不自动重试。
        logger.error("沙箱后端不可用：%s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        quota.release(tenant_id)
        _inflight.discard(cache_key)

    stdout, stdout_cut = _clip(outcome.stdout)
    stderr, stderr_cut = _clip(outcome.stderr)

    result: dict[str, Any] = {
        "execution_id": execution_id,
        "status": outcome.status,
        "exit_code": outcome.exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": outcome.duration_ms,
        "truncated": stdout_cut or stderr_cut,
        "backend": outcome.backend,
    }

    _remember(
        execution_id,
        tenant_id,
        result,
        user_id=user_id,
        conversation_id=conversation_id,
    )

    if cache_key:
        if len(_idempotent) > 512:
            _idempotent.clear()

        _idempotent[cache_key] = (time.time(), result)

    return result


@app.get("/v1/executions/{execution_id}")
async def get_execution(
    execution_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict[str, Any]:
    """回读执行记录。

    租户不匹配时返回 404 而不是 403：403 会泄露“这个 ID 存在”，
    跨租户探测就该拿不到任何信息。
    """
    authorize(request, authorization)
    tenant_id = resolve_tenant(request)
    user_id, _ = resolve_owner(request)

    record = _records.get(execution_id)

    if (
        record is None
        or record.tenant_id != tenant_id
        # 同租户内也按用户隔离：猜到别人的 execution_id 同样拿不到内容。
        or (user_id and record.user_id and record.user_id != user_id)
        or time.time() - record.created_at > settings.execution_ttl_seconds
    ):
        raise HTTPException(
            status_code=404,
            detail=f"未找到执行记录 {execution_id}（可能已过期）。",
        )

    return record.payload


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8199)
