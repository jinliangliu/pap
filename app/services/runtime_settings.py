# app/services/runtime_settings.py
"""运行配置：管理员在控制台改、改完立刻生效的平台级开关。

与 `.env` 的分工：

- `.env` 是**首次默认值**。库里没有这一项时读它，所以「刚部署、还没人配过」
  的环境仍然有一套可预期的行为。
- 库里一旦有记录，**以库为准**。`source()` 会告诉调用方当前值来自哪边，
  页面据此提示「这是默认值，还是管理员设的」。

为什么热路径能同步读：`obs_enabled()` 会在每个 `span()` 里被调用，不能 await。
所以值常驻进程内存，只在两处更新：启动时 `load()` 一次、管理员保存时由
`set_value()` 当场改写。当前部署是单进程 uvicorn；如果将来开多 worker，
每个进程都要各自 `load()`，且变更需要广播——那时的做法是给这张表加一个
版本号并在每个请求入口比对，**不要**指望内存缓存自动同步。

读失败一律回落默认值：配置读不出来是「少了点能力」，不能让进程起不来。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.platform_setting import PlatformSetting

logger = logging.getLogger(__name__)

# 观测开关：是否把 trace / span 写入 obs_traces、obs_spans。
# 默认开着——PAP 现在的定位是「开发调试期」，观测是排障的主要依据；
# 要关由管理员在控制台决定，而不是让部署脚本替他决定。
OBSERVABILITY_ENABLED = "observability.enabled"

# 未配置工具时的默认动作：allow / deny。
#
# 这不是「默认策略」的另一个名字，而是**没有单独配置过**的工具的结局。
# 设成 deny 后，只有显式配成 allow 或 confirm 的工具还能被模型看到——
# 新接入的连接器带来二十个工具时，默认拒绝是安全基线。
DEFAULT_TOOL_ACTION = "permissions.default_action"

# 对外 MCP 通道总开关。关掉后 `/mcp` 整体返回 503，已签发的令牌一起失效
# （令牌校验仍在，只是通道不再接受请求）。
# 留这个开关是为了有一个「立刻切断外部系统」的动作：改 nginx 要重载，
# 删令牌要逐个点，而这个开关点一下就到。
MCP_INBOUND_ENABLED = "mcp.inbound_enabled"

# 单篇文档的切片上限。它是**容量阈值**，不是质量阈值：
# 一份 56.8 万字的现行国标切 973 片是它应有的颗粒度，而一本被误传的数 MB
# 手册能切出几千片，BM25 每轮检索都要全扫。
#
# 为什么放在运行时配置里而不是只留在 `.env`：这个数字已经两次卡住真实入库
# （400 卡住 448 片的 GB/T 3836.1，800 卡住 973 片的 GB/T 3836.4），
# 而它取决于机器规格与语料规模，不是「改错了」——不该每调一次就发一次版。
# 默认值见 `settings.knowledge_max_chunks_per_document`。
KNOWLEDGE_MAX_CHUNKS = "knowledge.max_chunks_per_document"

_BOOL_KEYS = (OBSERVABILITY_ENABLED, MCP_INBOUND_ENABLED)

# 切片上限的合法区间。下限 1 是「至少能存一份」；上限 20000 挡住手滑写成
# 十万——那个量级会真的把每轮检索拖成秒级，而它不会在写入时报错，
# 只会在之后每次提问时慢慢显形。
_CHUNK_LIMIT_MIN = 1
_CHUNK_LIMIT_MAX = 20000

# 默认动作只有两个合法取值。不提供 confirm：把「没配过」的工具默认设成
# 每次都要审批，会让第一次接入的二十个工具全部弹卡片，人会条件反射地点通过。
_TOOL_ACTIONS = ("allow", "deny")

_DEFAULTS: dict[str, Any] = {
    OBSERVABILITY_ENABLED: True,
    DEFAULT_TOOL_ACTION: "allow",
    MCP_INBOUND_ENABLED: True,
}
# 进程内的当前值。只放**已校验过**的值：脏数据不进缓存，
# 于是 `_read` 会自然回落到默认，与「库里没这条」表现一致。
_cache: dict[str, Any] = {}
# 哪些键真的来自库（用于 source()：区分「管理员设的」与「.env 默认」）。
_from_db: set[str] = set()


def _fallback(key: str) -> Any:
    """库里没有（或值不可用）时的取值。

    观测项回落到 `.env` 的 `OBS_ENABLED` 而不是写死 True：
    这样「想用环境变量压住」的老做法仍然有效，且启动行为可预测。
    切片上限同理回落到 `KNOWLEDGE_MAX_CHUNKS_PER_DOCUMENT`。
    """
    if key == OBSERVABILITY_ENABLED:
        return bool(settings.obs_enabled)
    if key == KNOWLEDGE_MAX_CHUNKS:
        return int(settings.knowledge_max_chunks_per_document)
    return _DEFAULTS.get(key)


def _coerce_bool(value: Any) -> bool | None:
    """把库里的值读成布尔；不可用的值返回 None（= 按未配置处理）。

    `platform_settings.value` 是 JSON，可以被手改成字符串或数字。
    脏值不能猜：`"false"` 猜成 False 还是 True 都有风险，按未配置处理更稳。
    """
    if isinstance(value, bool):
        return value
    return None


def _coerce_chunk_limit(value: Any) -> int | None:
    """把库里的值读成切片上限；越界或非整数返回 None（= 按未配置处理）。

    `isinstance(True, int)` 为真，所以要先挡布尔：库里存了 `true` 时
    不能被当成 1——那会让每一份文档都被判超限。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not (_CHUNK_LIMIT_MIN <= value <= _CHUNK_LIMIT_MAX):
        return None
    return value


def _read_bool(key: str) -> bool:
    value = _coerce_bool(_cache.get(key)) if key in _cache else None
    if value is None:
        return bool(_fallback(key))
    return value


def _coerce_action(value: Any) -> str | None:
    """默认动作只认 allow / deny；其余（含 "confirm"）一律按未配置处理。"""
    if isinstance(value, str) and value in _TOOL_ACTIONS:
        return value
    return None


def obs_enabled() -> bool:
    """是否记录观测。热路径调用，纯内存读。"""
    return _read_bool(OBSERVABILITY_ENABLED)


def default_tool_action() -> str:
    """未配置工具时的默认动作。工具目录过滤与执行拦截共用这一份。"""
    value = _coerce_action(_cache.get(DEFAULT_TOOL_ACTION)) if DEFAULT_TOOL_ACTION in _cache else None
    return value or str(_fallback(DEFAULT_TOOL_ACTION) or "allow")


def mcp_inbound_enabled() -> bool:
    """对外 MCP 通道是否接受请求。在 ASGI 入口读，同样是纯内存读。"""
    return _read_bool(MCP_INBOUND_ENABLED)


def knowledge_max_chunks() -> int:
    """单篇文档的切片上限。入库时读，纯内存。

    入库路径不是热路径（一份文档只切一次），但也没必要为它 await 一次库：
    缓存里就是生效值，未 load 时回落到 `.env`，与其它项一致。
    """
    value = _coerce_chunk_limit(_cache.get(KNOWLEDGE_MAX_CHUNKS))
    if value is None:
        return int(_fallback(KNOWLEDGE_MAX_CHUNKS))
    return value


def source(key: str) -> str:
    """当前值来自哪里：`runtime`（管理员设过）或 `env`（默认值）。"""
    return "runtime" if key in _from_db else "env"


def snapshot() -> dict[str, Any]:
    """给接口与页面用的当前状态。"""
    return {
        "observability_enabled": obs_enabled(),
        "observability_source": source(OBSERVABILITY_ENABLED),
        "default_tool_action": default_tool_action(),
        "default_tool_action_source": source(DEFAULT_TOOL_ACTION),
        "mcp_inbound_enabled": mcp_inbound_enabled(),
        "mcp_inbound_source": source(MCP_INBOUND_ENABLED),
        "knowledge_max_chunks": knowledge_max_chunks(),
        "knowledge_max_chunks_source": source(KNOWLEDGE_MAX_CHUNKS),
    }


async def load() -> None:
    """从库里加载全部运行配置。进程启动时调一次。

    表不存在（还没跑迁移）或查询失败都只记日志：按默认值继续跑。
    """
    global _cache, _from_db

    try:
        async with async_session_factory() as session:
            rows = (await session.scalars(select(PlatformSetting))).all()
    except Exception:
        logger.warning(
            "读取运行配置失败，本次全部按默认值运行。",
            extra={"event": "runtime_settings_load_failed"},
            exc_info=True,
        )
        return

    cache: dict[str, Any] = {}
    from_db: set[str] = set()

    for row in rows:
        if row.key in _BOOL_KEYS:
            value = _coerce_bool(row.value)
            if value is None:
                # 脏值：不写进缓存，于是回落到默认。
                logger.warning(
                    "运行配置项的值不可用，按未配置处理。",
                    extra={
                        "event": "runtime_setting_invalid",
                        "key": row.key,
                        "value_type": type(row.value).__name__,
                    },
                )
                continue
            cache[row.key] = value
            from_db.add(row.key)
        elif row.key == DEFAULT_TOOL_ACTION:
            action = _coerce_action(row.value)
            if action is None:
                logger.warning(
                    "运行配置项的值不可用，按未配置处理。",
                    extra={
                        "event": "runtime_setting_invalid",
                        "key": row.key,
                        "value_type": type(row.value).__name__,
                    },
                )
                continue
            cache[row.key] = action
            from_db.add(row.key)
        elif row.key == KNOWLEDGE_MAX_CHUNKS:
            limit = _coerce_chunk_limit(row.value)
            if limit is None:
                # 越界或类型不对：不进缓存，于是 `knowledge_max_chunks()`
                # 回落到 .env，`source()` 也如实说「来自 env」——
                # 与「库里没这条」表现一致，比「用了一个奇怪的数还标成管理员设的」好。
                logger.warning(
                    "运行配置项的值不可用，按未配置处理。",
                    extra={
                        "event": "runtime_setting_invalid",
                        "key": row.key,
                        "value_type": type(row.value).__name__,
                    },
                )
                continue
            cache[row.key] = limit
            from_db.add(row.key)
        else:
            # 未知键不报错：可能是旧版本留下的，或新版本先写了、回退了代码。
            cache[row.key] = row.value

    _cache = cache
    _from_db = from_db

    logger.info(
        "运行配置已加载。",
        extra={
            "event": "runtime_settings_loaded",
            "observability_enabled": obs_enabled(),
            "observability_source": source(OBSERVABILITY_ENABLED),
            "keys": sorted(cache),
        },
    )


async def set_observability(value: bool, *, actor: str | None) -> bool:
    """写入观测开关并立刻更新内存值。返回是否发生了变化。

    只改内存里的这一个键，不重读全表：管理员保存的语义是「把这一项设成
    这个值」，而不是「把页面上的其它项也一起提交」。
    """
    return await set_bool(OBSERVABILITY_ENABLED, value, actor=actor)


async def set_mcp_inbound_enabled(value: bool, *, actor: str | None) -> bool:
    return await set_bool(MCP_INBOUND_ENABLED, value, actor=actor)


async def set_default_tool_action(value: str, *, actor: str | None) -> bool:
    """写入「未配置工具的默认动作」。非法值直接拒绝，不静默纠正。"""
    if value not in _TOOL_ACTIONS:
        raise ValueError(f"默认动作只能是 {'/'.join(_TOOL_ACTIONS)}。")
    return await _persist(DEFAULT_TOOL_ACTION, value, actor=actor)


async def set_knowledge_max_chunks(value: int, *, actor: str | None) -> bool:
    """写入切片上限。越界直接拒绝，不夹到边界值。

    夹边界看着「更友好」，实际更坏：管理员以为设成了 50000，实际是 20000，
    下次撞墙时看到的数字和他填的对不上，会怀疑是「没保存」。
    """
    if not (_CHUNK_LIMIT_MIN <= value <= _CHUNK_LIMIT_MAX):
        raise ValueError(
            f"切片上限应在 {_CHUNK_LIMIT_MIN} 到 {_CHUNK_LIMIT_MAX} 之间。"
        )
    return await _persist(KNOWLEDGE_MAX_CHUNKS, value, actor=actor)


async def set_bool(key: str, value: bool, *, actor: str | None) -> bool:
    return await _persist(key, value, actor=actor)


async def _persist(key: str, value: Any, *, actor: str | None) -> bool:
    # 比较的是「生效值」而不是缓存里有没有记录：库为空时旧值来自默认，
    # 管理员把它设成同一个值时行为没变，不该在审计里记成一次变更。
    if isinstance(value, bool):
        previous: Any = _read_bool(key)
    elif key == DEFAULT_TOOL_ACTION:
        previous = default_tool_action()
    elif key == KNOWLEDGE_MAX_CHUNKS:
        previous = knowledge_max_chunks()
    else:
        previous = _cache.get(key)
    changed = previous != value

    async with async_session_factory() as session:
        async with session.begin():
            row = await session.get(PlatformSetting, key)
            if row is None:
                row = PlatformSetting(key=key, value=value, updated_by=actor)
                session.add(row)
            else:
                row.value = value
                row.updated_by = actor

    _cache[key] = value
    _from_db.add(key)

    logger.info(
        "运行配置已更新。",
        extra={
            "event": "runtime_setting_updated",
            "key": key,
            "value": value,
            "actor": actor,
            "changed": changed,
        },
    )
    return changed


def reset_for_tests() -> None:
    """测试用：清掉进程内缓存，回到「还没 load 过」的状态。"""
    global _cache, _from_db
    _cache = {}
    _from_db = set()
