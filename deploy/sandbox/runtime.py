# deploy/sandbox/runtime.py
"""沙箱执行后端：一次性 gVisor 容器，或仅供本地调试的子进程。

两个后端的安全级别差一个数量级，不要混用：

| 后端 | 隔离 | 用途 |
|---|---|---|
| ``runsc`` | gVisor 用户态内核 + 每执行一个一次性容器 | 生产、多租户 |
| ``process`` | 只有 rlimit 与临时目录 | 本机调试，不得对外 |

容器参数是安全边界的一部分：去掉 ``--network none``、``--read-only``、
``--cap-drop ALL`` 中的任何一条，隔离就出现缺口。修改前请读 docs/SANDBOX.md。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from typing import Any

try:  # resource 只在 POSIX 上有；Windows 开发机要能 import 本模块
    import resource
except ImportError:  # pragma: no cover - 仅 Windows
    resource = None  # type: ignore[assignment]

DEFAULT_IMAGE = "pap-sandbox-runtime:1"
DEFAULT_RUNTIME = "runsc"

# 租户标识会进入容器名与 docker label，必须严格白名单。
# 允许点、下划线、连字符，长度上限 63（docker 标签值上限 4096，容器名有更严限制）。
TENANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

# 语言 -> 解释器参数。刻意不支持 bash：它是最容易突破限制的入口。
LANGUAGES: dict[str, list[str]] = {
    "python": ["python3", "-I", "-B"],
    "javascript": ["node", "--no-warnings"],
}

ENTRYPOINTS = {"python": "main.py", "javascript": "main.js"}


class BackendUnavailable(RuntimeError):
    """后端不可用：docker 不在、运行时缺失、镜像未构建。"""


class BackendRejected(RuntimeError):
    """请求本身不合法，重试无用。"""


@dataclass(frozen=True, slots=True)
class IsolationProfile:
    """单个执行容器的资源上限。"""

    runtime: str = DEFAULT_RUNTIME
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 64
    work_tmpfs_mb: int = 64
    tmp_tmpfs_mb: int = 16
    nofile: int = 128


@dataclass(frozen=True, slots=True)
class Outcome:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    backend: str
    # 代码子进程消耗的 CPU 毫秒；None = 沙箱没测到，不是 0。
    cpu_ms: int | None = None


def validate_tenant(raw: str | None) -> str:
    """租户标识白名单校验。

    没有这一步，``tenant_id`` 会原样进入 ``--label pap.tenant=...``，
    一个带空格的租户名就能改变 docker 命令的含义。
    """
    candidate = (raw or "").strip()

    if not candidate:
        return "default"

    if not TENANT_PATTERN.match(candidate):
        raise BackendRejected(
            "租户标识不合法：只允许字母、数字、点、下划线与连字符，且必须以字母或数字开头。"
        )

    return candidate


def container_name(execution_id: str) -> str:
    return f"pap-sbx-{execution_id}"


def build_container_command(
    *,
    image: str,
    tenant_id: str,
    execution_id: str,
    profile: IsolationProfile,
) -> list[str]:
    """构造 ``docker run`` 参数。

    刻意不出现 ``-v`` / ``--mount``：容器看不到任何宿主路径，
    代码通过 stdin 传进去，产物随容器销毁一起消失。
    """
    return [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--name",
        container_name(execution_id),
        "--runtime",
        profile.runtime,
        # 完全断网：既挡住数据外带，也挡住下载二阶段载荷。
        "--network",
        "none",
        # 根文件系统只读，可写位置只有两块 tmpfs，且都不可执行。
        "--read-only",
        "--tmpfs",
        f"/work:rw,noexec,nosuid,nodev,size={profile.work_tmpfs_mb}m,mode=1777",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={profile.tmp_tmpfs_mb}m,mode=1777",
        # 非 root，且没有可用特权。
        "--user",
        "65534:65534",
        "--workdir",
        "/work",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        # 资源上限走 cgroup，比 rlimit 更难绕过。
        "--pids-limit",
        str(profile.pids_limit),
        "--memory",
        f"{profile.memory_mb}m",
        "--memory-swap",
        f"{profile.memory_mb}m",
        "--cpus",
        f"{profile.cpus:g}",
        "--ulimit",
        f"nofile={profile.nofile}:{profile.nofile}",
        # 标签用于清理孤儿容器，也用于事后按租户对账。
        "--label",
        "pap.sandbox=1",
        "--label",
        f"pap.tenant={tenant_id}",
        "--label",
        f"pap.execution={execution_id}",
        "--env",
        "HOME=/work",
        "--env",
        "LANG=C.UTF-8",
        image,
    ]


def encode_payload(*, language: str, code: str, stdin: str) -> bytes:
    return json.dumps(
        {"language": language, "code": code, "stdin": stdin},
        ensure_ascii=False,
    ).encode("utf-8")


def parse_runner_output(raw: str) -> dict[str, Any] | None:
    """容器内 runner 只输出一行 JSON；解析失败说明容器本身出了问题。"""
    text = raw.strip()

    if not text:
        return None

    # runner 的 stdout 就是结果本身；若混入其它行，取最后一行 JSON。
    for line in reversed(text.splitlines()):
        line = line.strip()

        if not line.startswith("{"):
            continue

        try:
            parsed = json.loads(line)
        except ValueError:
            continue

        if isinstance(parsed, dict) and "status" in parsed:
            return parsed

    return None


async def _docker(
    *args: str,
    timeout: float = 10.0,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise BackendUnavailable(
            f"docker {' '.join(args[:2])} 超过 {timeout:.0f} 秒未返回。"
        ) from exc

    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


async def docker_runtimes() -> set[str]:
    try:
        code, stdout, _ = await _docker("info", "--format", "{{json .Runtimes}}", timeout=15)
    except (FileNotFoundError, BackendUnavailable, OSError):
        return set()

    if code != 0:
        return set()

    try:
        data = json.loads(stdout or "{}")
    except ValueError:
        return set()

    return set(data) if isinstance(data, dict) else set()


async def image_exists(image: str) -> bool:
    try:
        code, _, _ = await _docker("image", "inspect", "--format", "{{.Id}}", image, timeout=15)
    except (FileNotFoundError, BackendUnavailable, OSError):
        return False

    return code == 0


async def probe(profile: IsolationProfile, image: str) -> dict[str, Any]:
    """健康检查用：运行时是否注册、镜像是否就绪。

    只做只读探测，不启动容器——健康检查每分钟被调用，
    不能为它付出一次容器启动的开销。
    """
    runtimes = await docker_runtimes()
    has_runtime = profile.runtime in runtimes
    has_image = await image_exists(image) if has_runtime else False

    detail = "ok"

    if not runtimes:
        detail = "docker 不可用或无法读取运行时列表"
    elif not has_runtime:
        detail = f"未注册运行时 {profile.runtime}，请先执行 deploy/sandbox/install-runsc.sh"
    elif not has_image:
        detail = f"镜像 {image} 不存在，请先执行 deploy/sandbox/build-image.sh"

    return {
        "ready": has_runtime and has_image,
        "runtime": profile.runtime,
        "image": image,
        "runtimes": sorted(runtimes),
        "detail": detail,
    }


async def run_container(
    *,
    language: str,
    code: str,
    stdin: str,
    timeout_seconds: int,
    tenant_id: str,
    execution_id: str,
    image: str,
    profile: IsolationProfile,
) -> Outcome:
    command = build_container_command(
        image=image,
        tenant_id=tenant_id,
        execution_id=execution_id,
        profile=profile,
    )
    payload = encode_payload(language=language, code=code, stdin=stdin)
    started = time.monotonic()

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise BackendUnavailable("找不到 docker 命令，无法启动沙箱容器。") from exc
    except OSError as exc:
        raise BackendUnavailable(f"启动沙箱容器失败：{exc}") from exc

    try:
        # 外层预算比代码预算多 5 秒，留给容器启动与销毁。
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload),
            timeout=timeout_seconds + 5,
        )
    except TimeoutError:
        await destroy_container(execution_id)
        await process.wait()

        return Outcome(
            status="timeout",
            exit_code=None,
            stdout="",
            stderr=f"执行超过 {timeout_seconds} 秒，容器已被销毁。",
            duration_ms=int((time.monotonic() - started) * 1000),
            backend="runsc",
            # 容器已被销毁，拿不到 runner 的 CPU 读数；留空不猜。
            cpu_ms=None,
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    raw_out = stdout.decode("utf-8", "replace")
    raw_err = stderr.decode("utf-8", "replace")

    parsed = parse_runner_output(raw_out)

    if parsed is None:
        # docker 自身失败时不会走到 runner，stderr 里是真正的原因。
        raise BackendUnavailable(
            "沙箱容器未能返回结果"
            f"（exit={process.returncode}）：{raw_err.strip()[:240] or '无输出'}"
        )

    return Outcome(
        status=str(parsed.get("status") or "failed"),
        exit_code=_as_int(parsed.get("exit_code")),
        stdout=str(parsed.get("stdout") or ""),
        stderr=str(parsed.get("stderr") or ""),
        duration_ms=_as_int(parsed.get("duration_ms")) or duration_ms,
        backend="runsc",
        cpu_ms=_as_int(parsed.get("cpu_ms")),
    )


async def destroy_container(execution_id: str) -> None:
    """超时或中断时强制销毁容器。

    ``docker run --rm`` 只在容器正常退出时清理；被 kill 的进程组
    可能留下仍在运行的容器，必须显式删掉。
    """
    name = container_name(execution_id)

    for args in (("kill", name), ("rm", "--force", name)):
        try:
            await _docker(*args, timeout=10)
        except (BackendUnavailable, FileNotFoundError, OSError):
            return


async def sweep_orphans(ttl_seconds: int) -> int:
    """清理崩溃遗留的容器：按标签筛选，只删超过 TTL 的。"""
    try:
        code, stdout, _ = await _docker(
            "ps",
            "--all",
            "--quiet",
            "--filter",
            "label=pap.sandbox=1",
            "--filter",
            f"until={ttl_seconds}s",
            timeout=15,
        )
    except (BackendUnavailable, FileNotFoundError, OSError):
        return 0

    if code != 0:
        return 0

    ids = stdout.split()

    if not ids:
        return 0

    try:
        await _docker("rm", "--force", *ids, timeout=30)
    except (BackendUnavailable, FileNotFoundError, OSError):
        return 0

    return len(ids)


def _apply_limits(cpu_seconds: int, profile: IsolationProfile) -> None:  # pragma: no cover
    """子进程后端用 rlimit 兜底；它挡不住内核提权，只防手滑。"""
    if resource is None:  # Windows 没有 rlimit
        return

    os.setsid()

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NPROC, (profile.pids_limit, profile.pids_limit))
    resource.setrlimit(resource.RLIMIT_NOFILE, (profile.nofile, profile.nofile))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_process(
    *,
    language: str,
    code: str,
    stdin: str,
    timeout_seconds: int,
    profile: IsolationProfile,
) -> Outcome:
    """本地调试后端。

    它**没有**网络隔离、文件系统隔离与内核隔离，绝不能用于多租户环境。
    存在的唯一理由是让开发机不必装 docker 也能跑通工具链路。
    """
    import subprocess

    command = LANGUAGES.get(language)

    if command is None:
        raise BackendRejected(f"不支持的语言：{language}")

    entry = ENTRYPOINTS[language]
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="sandbox-") as workdir:
        with open(os.path.join(workdir, entry), "w", encoding="utf-8") as handle:
            handle.write(code)

        extra: dict[str, Any] = {}

        if os.name == "posix":
            # preexec_fn 在 Windows 上不存在；不设也不能假装有隔离。
            extra["preexec_fn"] = lambda: _apply_limits(timeout_seconds, profile)  # noqa: PLW1509

        try:
            completed = subprocess.run(  # noqa: S603
                [*command, entry],
                cwd=workdir,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "HOME": workdir,
                    "LANG": "C.UTF-8",
                    "PYTHONIOENCODING": "utf-8",
                },
                check=False,
                **extra,
            )
        except subprocess.TimeoutExpired:
            return Outcome(
                status="timeout",
                exit_code=None,
                stdout="",
                stderr=f"执行超过 {timeout_seconds} 秒，已被终止。",
                duration_ms=int((time.monotonic() - started) * 1000),
                backend="process",
            )

    return Outcome(
        status="succeeded" if completed.returncode == 0 else "failed",
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_ms=int((time.monotonic() - started) * 1000),
        backend="process",
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, int):
        return value

    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
