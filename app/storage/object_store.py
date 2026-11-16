# app/storage/object_store.py
"""对象存储（当前实现：本地目录）。

路径规范：

    tenants/<tenant_id>/users/<user_id>/artifacts/<artifact_id><ext>

三条约束：

1. 键由服务端生成，客户端只能提供文件名（用于取扩展名与展示）；
2. 键里包含租户前缀，服务层在读写前会再校验前缀与当前租户一致；
3. 解析后的真实路径必须仍在根目录下——防止 `../` 穿越到进程可写的任何位置。

对象路径不是权限控制：下载前必须先查元数据表并校验租户与所有者。
换成 S3/OSS 时只需要替换本模块，接口不变。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from pathlib import Path

from app.core.config import settings

_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_ARTIFACT_KEY = re.compile(
    r"^tenants/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/users/([A-Za-z0-9-]{1,64})"
    r"/artifacts/([a-f0-9]{32})(\.[A-Za-z0-9]{1,10})?$"
)


class ObjectStoreError(RuntimeError):
    """对象存储读写失败。"""


class ObjectNotFound(ObjectStoreError):
    """对象不存在。"""


class ObjectTooLarge(ObjectStoreError):
    """超过单文件上限。"""


def safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()

    return suffix if _SAFE_SUFFIX.match(suffix) else ""


def safe_filename(filename: str, *, fallback: str = "artifact") -> str:
    """只保留基名，去掉路径分隔符与控制字符。"""
    base = (filename or "").replace("\\", "/").split("/")[-1]
    cleaned = "".join(char for char in base if char.isprintable() and char not in {'"', "'"})

    return cleaned[:255] or fallback


class LocalObjectStore:
    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or settings.artifact_storage_dir).resolve()

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def build_key(
        *,
        tenant_id: str,
        user_id: str,
        artifact_id: uuid.UUID,
        suffix: str,
    ) -> str:
        return (
            f"tenants/{tenant_id}/users/{user_id}/artifacts/"
            f"{artifact_id.hex}{suffix if _SAFE_SUFFIX.match(suffix or '') else ''}"
        )

    @staticmethod
    def key_belongs_to(key: str, *, tenant_id: str, user_id: str) -> bool:
        match = _ARTIFACT_KEY.match(key)

        return bool(match and match.group(1) == tenant_id and match.group(2) == user_id)

    def path_for(self, key: str) -> Path:
        if not _ARTIFACT_KEY.match(key):
            raise ObjectStoreError("对象键格式无效。")

        candidate = (self._root / key).resolve()

        # 双重校验：正则已限制字符集，这里再确认解析结果没有跳出根目录。
        if self._root not in candidate.parents:
            raise ObjectStoreError("对象路径越界。")

        return candidate

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        max_bytes: int | None = None,
    ) -> tuple[int, str]:
        limit = max_bytes if max_bytes is not None else settings.artifact_max_bytes

        if len(data) > limit:
            raise ObjectTooLarge(f"文件超过 {limit} 字节上限。")

        path = self.path_for(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)

        return len(data), hashlib.sha256(data).hexdigest()

    async def read(self, key: str) -> bytes:
        path = self.path_for(key)

        if not path.is_file():
            raise ObjectNotFound("对象不存在。")

        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self.path_for(key)

        if path.is_file():
            await asyncio.to_thread(path.unlink)


_store = LocalObjectStore()


def get_object_store() -> LocalObjectStore:
    return _store
