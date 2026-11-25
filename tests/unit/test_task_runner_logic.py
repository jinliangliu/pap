# tests/unit/test_task_runner.py
"""事务执行器的纯逻辑：产物提取、回读比对、输入构造。

这些函数决定「什么算成功」，必须能在不连沙箱、不连库的情况下验证。
"""

from __future__ import annotations

import base64
import json

import pytest

from app.models.task import TaskKind
from app.services import task_runner_service as runner


def _docx_payload(text: str = "hello") -> str:
    # 真 docx 是 ZIP；这里只要 PK 头 + 一点内容就能验协议，不必造完整 OOXML。
    raw = b"PK\x03\x04" + (text * 200).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def test_extract_artifact_accepts_single_mark():
    stdout = f"building...\n{runner._ARTIFACT_MARK}{_docx_payload()}\n"
    assert runner.extract_artifact(stdout).startswith(b"PK")


@pytest.mark.parametrize(
    "stdout",
    [
        "",  # 没有产物
        f"{runner._ARTIFACT_MARK}{_docx_payload()}\n{runner._ARTIFACT_MARK}{_docx_payload()}",  # 两份
        f"{runner._ARTIFACT_MARK}not-base64!",  # 非法编码
        f"{runner._ARTIFACT_MARK}{base64.b64encode(b'plain text').decode()}",  # 非 ZIP
    ],
)
def test_extract_artifact_rejects_ambiguous_or_invalid(stdout):
    with pytest.raises(runner.TaskRunnerError):
        runner.extract_artifact(stdout)


def test_extract_artifact_rejects_non_zip_even_with_pk_prefix():
    """PK 头是必要条件不是充分条件：太短的载荷不该被当成有效 docx。

    真正的截断防线是 run_task 里的 `result.truncated`——base64 被截断后
    仍可能解码出 PK 头，单靠产物解析挡不住，那个分支在集成测试里验。
    """
    short = base64.b64encode(b"PK\x03\x04").decode("ascii")
    with pytest.raises(runner.TaskRunnerError, match="疑似截断"):
        runner.extract_artifact(runner._ARTIFACT_MARK + short)


def test_readback_requires_same_execution_and_cpu():
    class Result:
        execution_id = "e-1"
        status = "succeeded"
        cpu_ms = 12
        exit_code = 0

    class Same(Result):
        pass

    class OtherExec(Result):
        execution_id = "e-2"

    class OtherCpu(Result):
        cpu_ms = 13

    class MissingCpu(Result):
        cpu_ms = None

    assert runner.readback_matches(Result(), Same()) is True
    assert runner.readback_matches(Result(), OtherExec()) is False
    assert runner.readback_matches(Result(), OtherCpu()) is False
    assert runner.readback_matches(Result(), MissingCpu()) is False


def test_build_document_input_truncates_content_not_code():
    spec = runner.build_document_input(title="标题", markdown="正文" * 10)
    assert spec["title"] == "标题"
    assert spec["markdown"].startswith("正文")
    assert "print" not in spec["markdown"]


# ---- 编程事务 -------------------------------------------------------------


def test_build_code_input_keeps_files_and_command():
    spec = runner.build_code_input(
        files={"app.py": "x=1\n"},
        base={"app.py": "x=0\n"},
        test_command=["python3", "-m", "pytest", "-q"],
    )
    assert spec["files"] == {"app.py": "x=1\n"}
    assert spec["base"] == {"app.py": "x=0\n"}
    assert spec["test_command"] == ["python3", "-m", "pytest", "-q"]


@pytest.mark.parametrize(
    "files",
    [
        {},  # 空映射
        {"../evil.py": "x"},  # 相对穿越
        {"/etc/passwd": "x"},  # 绝对路径
        {"a/../../b.py": "x"},  # 中段穿越
        {"a.py": 123},  # 内容非文本
    ],
)
def test_validate_code_files_rejects_bad_paths_and_content(files):
    with pytest.raises(runner.CodeInputError):
        runner.validate_code_files(files)


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest",  # 字符串而非 argv
        ["bash", "-c", "rm -rf /"],  # 非白名单解释器
        ["python3", "-m", "os", "system"],  # 非白名单模块
        ["python3", "-m", "pytest", ";rm -rf /"],  # shell 元字符
        ["python3", "-m", "pytest", "-q", "x" * 200],  # 超长参数
    ],
)
def test_validate_test_command_rejects_unsafe(command):
    with pytest.raises(runner.CodeInputError):
        runner.validate_test_command(command)


def test_validate_test_command_allows_empty():
    assert runner.validate_test_command(None) == []
    assert runner.validate_test_command([]) == []


def test_extract_code_patch_allows_empty_diff():
    stdout = f"{runner._ARTIFACT_MARK}\n{runner._TEST_MARK}{{}}\n"
    assert runner.extract_code_patch(stdout) == b""


def test_extract_code_patch_rejects_missing_or_duplicate():
    with pytest.raises(runner.TaskRunnerError):
        runner.extract_code_patch("no artifact here")
    double = f"{runner._ARTIFACT_MARK}{base64.b64encode(b'x').decode()}\n" * 2
    with pytest.raises(runner.TaskRunnerError):
        runner.extract_code_patch(double)


def test_extract_test_result_parses_and_defaults():
    result = {"ran": True, "passed": False, "exit_code": 1, "output": "FAIL"}
    stdout = f"{runner._TEST_MARK}{json.dumps(result)}\n"
    assert runner.extract_test_result(stdout) == result
    assert runner.extract_test_result("")["ran"] is False


def test_extract_test_result_rejects_duplicate():
    line = f"{runner._TEST_MARK}{{}}"
    with pytest.raises(runner.TaskRunnerError):
        runner.extract_test_result(line + "\n" + line)


def test_execution_plan_selects_code_template():
    code, stdin, extractor = runner._execution_plan(
        TaskKind.CODE, {"files": {"a.py": "x=1"}}
    )
    assert "difflib" in code
    assert json.loads(stdin)["files"] == {"a.py": "x=1"}
    assert extractor is runner.extract_code_patch
