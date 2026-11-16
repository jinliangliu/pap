# tests/unit/test_sandbox_runtime.py
"""沙箱容器参数的回归测试。

这些断言是安全边界本身：任何一条被删掉，隔离就出现缺口。
所以它们不是"顺手测一下"，而是防止后续重构悄悄削弱隔离。
"""

from __future__ import annotations

import pytest

from deploy.sandbox import runtime


def _command(tenant: str = "tenant-a") -> list[str]:
    return runtime.build_container_command(
        image="pap-sandbox-runtime:1",
        tenant_id=tenant,
        execution_id="abc123",
        profile=runtime.IsolationProfile(),
    )


def test_validate_tenant_accepts_normal_ids():
    assert runtime.validate_tenant("tenant-a") == "tenant-a"
    assert runtime.validate_tenant("t_1.2-3") == "t_1.2-3"
    assert runtime.validate_tenant("") == "default"
    assert runtime.validate_tenant(None) == "default"


@pytest.mark.parametrize(
    "tenant",
    [
        "tenant a",  # 空格会切断 docker 参数
        "tenant-a --privileged",
        "-tenant",  # 不能以连字符开头
        "tenant;rm -rf /",
        "tenant/../x",
        "租户",  # 非 ASCII 一律拒绝，避免标签编码歧义
        "a" * 64,
    ],
)
def test_validate_tenant_rejects_injection(tenant: str):
    with pytest.raises(runtime.BackendRejected):
        runtime.validate_tenant(tenant)


def test_container_isolation_flags_are_present():
    command = _command()

    # 运行时必须是 gVisor，且不能退化成默认 runc。
    assert command[command.index("--runtime") + 1] == "runsc"
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert command[command.index("--user") + 1] == "65534:65534"

    # 资源上限必须显式给出，不能依赖 docker 默认值。
    assert command[command.index("--memory") + 1].endswith("m")
    assert command[command.index("--pids-limit") + 1] == "64"
    assert command[command.index("--cpus") + 1] == "1"


def test_container_has_no_host_mounts():
    """宿主目录一旦挂进容器，租户就能读到平台文件。"""
    command = _command()

    assert "-v" not in command
    assert "--volume" not in command
    assert "--mount" not in command
    assert "--privileged" not in command


def test_container_is_labeled_for_cleanup_and_accounting():
    command = _command("tenant-a")

    labels = [command[index + 1] for index, item in enumerate(command) if item == "--label"]

    assert "pap.sandbox=1" in labels
    assert "pap.tenant=tenant-a" in labels
    assert "pap.execution=abc123" in labels


def test_container_name_is_derived_from_execution_id():
    assert runtime.container_name("abc123") == "pap-sbx-abc123"


def test_parse_runner_output_takes_last_json_line():
    raw = 'warning: something\n{"status": "succeeded", "exit_code": 0, "stdout": "ok"}\n'

    parsed = runtime.parse_runner_output(raw)

    assert parsed is not None
    assert parsed["stdout"] == "ok"


@pytest.mark.parametrize("raw", ["", "not json", "<html>502</html>", '{"foo": 1}'])
def test_parse_runner_output_rejects_garbage(raw: str):
    assert runtime.parse_runner_output(raw) is None


def test_encode_payload_keeps_code_intact():
    payload = runtime.encode_payload(language="python", code="print('中文')", stdin="")

    assert "中文" in payload.decode("utf-8")
