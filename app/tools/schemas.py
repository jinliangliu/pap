# app/tools/schemas.py
from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError


class ToolSchemaValidationError(ValueError):
    """
    工具参数不符合其 input_schema。

    对外 API 不应原样暴露 jsonschema 的完整错误链，
    应转换为清晰、有限的参数错误说明。
    """

    def __init__(
        self,
        message: str,
        *,
        field_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.field_path = field_path


def validate_tool_schema(schema: dict[str, Any]) -> None:
    """在工具注册时校验 JSON Schema 本身。"""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ToolSchemaValidationError(
            f"工具输入 Schema 无效：{exc.message}"
        ) from exc


def validate_tool_arguments(
    *,
    schema: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    """
    校验工具调用参数。

    默认采用 Draft 2020-12 JSON Schema。
    工具 Schema 建议明确写：

    {
        "type": "object",
        "properties": {...},
        "required": [...],
        "additionalProperties": false
    }
    """
    try:
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
    except SchemaError as exc:
        raise ToolSchemaValidationError(
            f"工具输入 Schema 无效：{exc.message}"
        ) from exc

    errors = sorted(
        validator.iter_errors(arguments),
        key=lambda item: list(item.absolute_path),
    )

    if not errors:
        return

    first_error = errors[0]

    path_parts = [
        str(part)
        for part in first_error.absolute_path
    ]
    field_path = ".".join(path_parts) or None

    if field_path:
        message = (
            f"参数 {field_path} 无效："
            f"{first_error.message}"
        )
    else:
        message = (
            f"工具参数无效：{first_error.message}"
        )

    raise ToolSchemaValidationError(
        message,
        field_path=field_path,
    )
