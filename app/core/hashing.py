# app/core/hashing.py
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """
    将 JSON 兼容对象转换为稳定字符串。

    - 按 key 排序；
    - 使用紧凑分隔符；
    - 保留 Unicode；
    - 禁止 NaN / Infinity 等非标准 JSON 值。
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def hash_parameters(parameters: dict[str, Any]) -> str:
    """计算写操作参数的 SHA-256 哈希。"""
    payload = canonical_json(parameters).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
