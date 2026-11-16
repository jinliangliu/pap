#!/usr/bin/env python3
"""沙箱隔离自检：直接打服务端协议，验证边界是否真的成立。

用法（在沙箱主机上执行）：

    python3 deploy/sandbox/verify_isolation.py \
        --base-url http://127.0.0.1:8199 \
        --api-key "$(grep PAP_SANDBOX_API_KEY /etc/pap-sandbox.env | cut -d= -f2)"

它检查九件事，任意一条失败就以非零退出：

1. 正常代码能跑通（基线，排除"因为全坏了所以全部拒绝"）；
2. 读不到平台 `.env`；
3. 列不出平台目录；
4. 容器内没有出站网络；
5. 跨租户回读执行记录返回 404；
6. 缺少 API Key 时返回 401；
7. 单租户并发超过配额时被拒绝（429）；
8. 容器内没有 docker socket（不能创建新容器）；
9. 容器内不是 root。

只依赖标准库，不要求在项目虚拟环境里跑。
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.error
import urllib.request

TIMEOUT = 40


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    api_key: str | None = None,
    tenant: str | None = None,
) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")

    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")

    if tenant:
        request.add_header("X-Sandbox-Tenant", tenant)

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read()

        try:
            parsed = json.loads(body or b"{}")
        except ValueError:
            parsed = {"raw": body.decode("utf-8", "replace")}

        return exc.code, parsed


def _run(base_url: str, api_key: str, tenant: str, code: str, timeout: int = 10) -> dict:
    status, body = _request(
        base_url,
        "/v1/execute",
        method="POST",
        payload={
            "language": "python",
            "code": code,
            "stdin": "",
            "timeout_seconds": timeout,
        },
        api_key=api_key,
        tenant=tenant,
    )

    if status != 200:
        raise RuntimeError(f"执行失败：HTTP {status} {body}")

    return body


def _expect_failure(output: str, names: tuple[str, ...]) -> bool:
    return any(name in output for name in names)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8199")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--tenant", default="probe-a")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    api_key = args.api_key
    tenant = args.tenant
    other = f"{tenant}-other"

    checks: list[tuple[str, bool, str]] = []

    # 1. 基线
    result = _run(base_url, api_key, tenant, "print(6 * 7)")
    ok = result.get("status") == "succeeded" and result.get("stdout", "").strip() == "42"
    checks.append(
        ("正常执行", ok, f"status={result.get('status')} stdout={result.get('stdout')!r}")
    )
    execution_id = result.get("execution_id", "")

    # 2/3. 宿主文件系统不可见
    read_env = _run(
        base_url,
        api_key,
        tenant,
        "print(open('/personal-agent-platform/.env').read()[:20])",
    )
    checks.append(
        (
            "读不到平台 .env",
            _expect_failure(read_env.get("stderr", ""), ("Error", "Permission", "No such file")),
            read_env.get("stderr", "").strip().splitlines()[-1] if read_env.get("stderr") else "",
        )
    )

    list_dir = _run(
        base_url,
        api_key,
        tenant,
        "import os\nprint(os.listdir('/personal-agent-platform')[:3])",
    )
    checks.append(
        (
            "列不出平台目录",
            _expect_failure(list_dir.get("stderr", ""), ("Error", "Permission", "No such file")),
            list_dir.get("stderr", "").strip().splitlines()[-1] if list_dir.get("stderr") else "",
        )
    )

    # 4. 无出站网络
    network = _run(
        base_url,
        api_key,
        tenant,
        "import socket\n"
        "socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "print('CONNECTED')\n",
        timeout=8,
    )
    blocked = network.get("status") != "succeeded" and "CONNECTED" not in network.get("stdout", "")
    checks.append(("无出站网络", blocked, (network.get("stderr") or "").strip()[:80]))

    # 5. 跨租户回读
    if execution_id:
        status, _ = _request(
            base_url,
            f"/v1/executions/{execution_id}",
            api_key=api_key,
            tenant=other,
        )
        own_status, _ = _request(
            base_url,
            f"/v1/executions/{execution_id}",
            api_key=api_key,
            tenant=tenant,
        )
        checks.append(
            (
                "跨租户回读 404",
                status == 404 and own_status == 200,
                f"other={status} own={own_status}",
            )
        )
    else:
        checks.append(("跨租户回读 404", False, "没有 execution_id"))

    # 6. 鉴权
    status, _ = _request(
        base_url,
        "/v1/execute",
        method="POST",
        payload={"language": "python", "code": "print(1)", "timeout_seconds": 5},
        tenant=tenant,
    )
    checks.append(("缺少 API Key 返回 401", status == 401, f"http={status}"))

    # 7. 租户并发配额：3 个并发、单租户上限 2、每个跑 7 秒
    results: list[int] = []
    lock = threading.Lock()

    def _slow() -> None:
        status_code, _ = _request(
            base_url,
            "/v1/execute",
            method="POST",
            payload={
                "language": "python",
                "code": "import time\ntime.sleep(7)\nprint('slow')",
                "timeout_seconds": 9,
            },
            api_key=api_key,
            tenant=f"{tenant}-quota",
        )
        with lock:
            results.append(status_code)

    threads = [threading.Thread(target=_slow) for _ in range(3)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    checks.append(("租户并发配额生效", 429 in results, f"codes={sorted(results)}"))

    # 8. 容器内没有 docker socket，代码无法借它创建新容器
    socket_check = _run(
        base_url,
        api_key,
        tenant,
        "import os\nprint(os.path.exists('/var/run/docker.sock'))",
    )
    checks.append(
        (
            "无 docker socket",
            socket_check.get("stdout", "").strip() == "False",
            f"stdout={socket_check.get('stdout', '').strip()!r}",
        )
    )

    # 9. 非 root
    uid_check = _run(base_url, api_key, tenant, "import os\nprint(os.getuid())")
    checks.append(
        (
            "非 root 运行",
            uid_check.get("stdout", "").strip() not in {"", "0"},
            f"uid={uid_check.get('stdout', '').strip()}",
        )
    )

    print()
    failed = 0

    for name, passed, detail in checks:
        mark = "PASS" if passed else "FAIL"
        print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))

        if not passed:
            failed += 1

    print()
    print(f"{len(checks) - failed}/{len(checks)} 通过")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
