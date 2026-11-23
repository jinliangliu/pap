# app/services/runtime_settings.py
"""运行配置：管理员在控制台改、改完立刻生效的平台级开关。

与 `.env` 的分工：

- `.env` 是**首次默认值**。库里没有这一项时读它，所以「刚部署、还没人配过」
  的环境仍然有一套可预期的行为。
- 库里一旦有记录，**以库为准**。`source()` 会告诉调用方当前值来自哪边，
  页面据此提示「这是默认值，还是管理员设的」。

为什么热路径能同步读：`obs_enabled()` 会在每个 `span()` 里被调用，不能 await。
所以值常驻进程内存，只在两处更新：启动时 `load()` 一次、管理员保存时由
`set_value()` 当场改写。

## revision：把「当时按哪版跑」变成可回答的问题

配置是「随时改、立刻生效」的。同一条对话里两跳工具之间管理员按了一下开关，
两跳就活在不同的策略下，而事后每条审计各自成立——没人答得出这一轮到底按
哪版走的。所以每次生效值真的变了，就落一份完整快照并 +1 一个单调号；
工具调用把它记下来，之后可以按号原样取回（`snapshot_at`）。

号从 0 开始且 0 不落库：0 表示「从来没有过配置变更，全按 .env 默认」，
这一版由代码合成。这样空库的环境不需要在启动时写一行才能自洽。

## 多进程

当前部署是单进程 uvicorn，进程内缓存才是权威。若将来开多 worker，每个进程
都要各自 `load()`，且变更需要广播——那时的做法是每个请求入口比对本进程的
revision 与库里最新值，**不要**指望内存缓存自动同步。写入用进程内锁串行化，
多进程下两个管理员同时保存不同键仍可能撞号（表现为一次 500 且事务回滚，
不会静默丢配置），那时应当把分配改成数据库序列。

读失败一律回落默认值：配置读不出来是「少了点能力」，不能让进程起不来。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select

from app.core.config import settings
from app.db.session import async_session_factory
from app.models.platform_setting import PlatformSetting
from app.models.policy_snapshot import PolicySnapshot

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

# ---------------- 写前备份 ----------------
#
# 三个键合起来才构成「什么值得备、备到多大为止」的完整答案：
#
# - `enabled`：总闸。关掉之后所有备份钩子都不再执行，写操作照常进行；
# - `threshold_mb`：单个对象超过这个大小就不备——一次写操作不该为了留档
#   先把几百 MB 灌一遍库。它是**单个对象**的阈值，不是总量；
# - `max_size_mb`：某个租户已存备份的总量上限。到了就停止新增并如实记录
#   「没备，因为满了」，而不是悄悄覆盖最旧的那份——覆盖会让「原件还在」
#   这个承诺在事后变成假的。
TOOL_BACKUP_ENABLED = "tool.backup_enabled"
TOOL_BACKUP_THRESHOLD_MB = "tool.backup_threshold_mb"
TOOL_BACKUP_MAX_SIZE_MB = "tool.backup_max_size_mb"

_BOOL_KEYS = (OBSERVABILITY_ENABLED, MCP_INBOUND_ENABLED, TOOL_BACKUP_ENABLED)

# 切片上限的合法区间。下限 1 是「至少能存一份」；上限 20000 挡住手滑写成
# 十万——那个量级会真的把每轮检索拖成秒级，而它不会在写入时报错，
# 只会在之后每次提问时慢慢显形。
_CHUNK_LIMIT_MIN = 1
_CHUNK_LIMIT_MAX = 20000

# 备份两个尺寸的合法区间。上限 100GB 只是挡住「把 MB 当字节填」这类手滑：
# 3000 与 3000000000 差三个数量级，写错的一方会让「阈值」变成永远不触发。
_MB_MIN = 1
_MB_MAX = 102_400

# 默认动作只有两个合法取值。不提供 confirm：把「没配过」的工具默认设成
# 每次都要审批，会让第一次接入的二十个工具全部弹卡片，人会条件反射地点通过。
_TOOL_ACTIONS = ("allow", "deny")

_DEFAULTS: dict[str, Any] = {
    OBSERVABILITY_ENABLED: True,
    DEFAULT_TOOL_ACTION: "allow",
    MCP_INBOUND_ENABLED: True,
    # 备份默认开：它的成本是一次 JSON 快照，收益是「原件还在」。
    TOOL_BACKUP_ENABLED: True,
    TOOL_BACKUP_THRESHOLD_MB: 100,
    TOOL_BACKUP_MAX_SIZE_MB: 3000,
}

# 进程内的当前值。只放**已校验过**的值：脏数据不进缓存，
# 于是 `_read` 会自然回落到默认，与「库里没这条」表现一致。
_cache: dict[str, Any] = {}
# 哪些键真的来自库（用于 source()：区分「管理员设的」与「.env 默认」）。
_from_db: set[str] = set()
# 当前生效的策略版本。0 = 从未变过配置，全按 .env。
_revision: int = 0

# 串行化写入：分配 revision 要先读 max 再插行，两个并发写会撞主键。
# 单进程部署下这把锁就够了；多进程见模块文档。
_write_lock = asyncio.Lock()


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


def _coerce_mb(value: Any) -> int | None:
    """把库里的值读成 MB 数。语义同 `_coerce_chunk_limit`：脏值不猜。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not (_MB_MIN <= value <= _MB_MAX):
        return None
    return value


def _coerce_action(value: Any) -> str | None:
    """默认动作只认 allow / deny；其余（含 "confirm"）一律按未配置处理。"""
    if isinstance(value, str) and value in _TOOL_ACTIONS:
        return value
    return None


# 键 → 校验函数。加新键只在这里加一行，不必在 load() 里再抄一段 elif：
# 抄出来的分支最容易漏掉「脏值不进缓存」，于是脏值会以默认值之外的样子生效。
_COERCERS: dict[str, Callable[[Any], Any]] = {
    OBSERVABILITY_ENABLED: _coerce_bool,
    MCP_INBOUND_ENABLED: _coerce_bool,
    TOOL_BACKUP_ENABLED: _coerce_bool,
    DEFAULT_TOOL_ACTION: _coerce_action,
    KNOWLEDGE_MAX_CHUNKS: _coerce_chunk_limit,
    TOOL_BACKUP_THRESHOLD_MB: _coerce_mb,
    TOOL_BACKUP_MAX_SIZE_MB: _coerce_mb,
}


def _read(key: str) -> Any:
    """从一份**显式的**缓存里读生效值。"""
    value = _coerce(key, _cache.get(key)) if key in _cache else None
    if value is None:
        return _fallback(key)
    return value


def _coerce(key: str, value: Any) -> Any:
    coercer = _COERCERS.get(key)
    if coercer is None:
        return value
    return coercer(value)


def _read_from(cache: dict[str, Any], key: str) -> Any:
    """同 `_read`，但作用在传入的缓存副本上（生成历史快照时用）。"""
    value = _coerce(key, cache.get(key)) if key in cache else None
    if value is None:
        return _fallback(key)
    return value


def _payload(cache: dict[str, Any]) -> dict[str, Any]:
    """把一份缓存渲染成完整的生效值。

    键用**点分形式**（就是 `platform_settings.key`），不是接口里的蛇形名：
    快照要能被平台之外的东西原样读走（沙箱子进程按 revision 取回同一份），
    而点分键是存储层唯一稳定的标识；接口重命名字段不该让历史快照失效。

    不含 `source`：那是「当前这一眼从哪来」的注解，不是策略本身。
    """
    return {key: _read_from(cache, key) for key in _COERCERS}


def _read_bool(key: str) -> bool:
    return bool(_read(key))


def obs_enabled() -> bool:
    """是否记录观测。热路径调用，纯内存读。"""
    return _read_bool(OBSERVABILITY_ENABLED)


def default_tool_action() -> str:
    """未配置工具时的默认动作。工具目录过滤与执行拦截共用这一份。"""
    return str(_read(DEFAULT_TOOL_ACTION) or "allow")


def mcp_inbound_enabled() -> bool:
    """对外 MCP 通道是否接受请求。在 ASGI 入口读，同样是纯内存读。"""
    return _read_bool(MCP_INBOUND_ENABLED)


def knowledge_max_chunks() -> int:
    """单篇文档的切片上限。入库时读，纯内存。

    入库路径不是热路径（一份文档只切一次），但也没必要为它 await 一次库：
    缓存里就是生效值，未 load 时回落到 `.env`，与其它项一致。
    """
    return int(_read(KNOWLEDGE_MAX_CHUNKS))


def backup_enabled() -> bool:
    return _read_bool(TOOL_BACKUP_ENABLED)


def backup_threshold_mb() -> int:
    return int(_read(TOOL_BACKUP_THRESHOLD_MB))


def backup_max_size_mb() -> int:
    return int(_read(TOOL_BACKUP_MAX_SIZE_MB))


def source(key: str) -> str:
    """当前值来自哪里：`runtime`（管理员设过）或 `env`（默认值）。"""
    return "runtime" if key in _from_db else "env"


def current_revision() -> int:
    """当前策略版本。工具调用把它记进上下文与审计。"""
    return _revision


def snapshot() -> dict[str, Any]:
    """给接口与页面用的当前状态。"""
    return {
        "revision": _revision,
        "observability_enabled": obs_enabled(),
        "observability_source": source(OBSERVABILITY_ENABLED),
        "default_tool_action": default_tool_action(),
        "default_tool_action_source": source(DEFAULT_TOOL_ACTION),
        "mcp_inbound_enabled": mcp_inbound_enabled(),
        "mcp_inbound_source": source(MCP_INBOUND_ENABLED),
        "knowledge_max_chunks": knowledge_max_chunks(),
        "knowledge_max_chunks_source": source(KNOWLEDGE_MAX_CHUNKS),
        "tool_backup_enabled": backup_enabled(),
        "tool_backup_enabled_source": source(TOOL_BACKUP_ENABLED),
        "tool_backup_threshold_mb": backup_threshold_mb(),
        "tool_backup_threshold_mb_source": source(TOOL_BACKUP_THRESHOLD_MB),
        "tool_backup_max_size_mb": backup_max_size_mb(),
        "tool_backup_max_size_mb_source": source(TOOL_BACKUP_MAX_SIZE_MB),
    }


async def snapshot_at(revision: int) -> dict[str, Any] | None:
    """按号取回**当时**的完整生效值；查不到返回 None。

    revision=0 不落库，由 `_payload({})` 现场合成：它等于「全按 .env 默认」，
    而这正是空库环境里第一条工具调用所处的策略。

    当前版本直接读内存：库里那份是写入时的副本，两者内容相同，
    但读内存不必为一次热路径回表。
    """
    if revision < 0:
        return None

    if revision == 0:
        # 还没发生过变更时，0 就是「当前生效值」——库里有行但值等于默认的情况
        # （管理员把某项设成与 .env 相同的值）读缓存才准；`.env` 后来改过时，
        # 只看缓存会和页面对不上。已经有过变更时，0 只剩「当时的默认」可还原。
        return _payload(_cache if _revision == 0 else {})

    if revision == _revision:
        return _payload(_cache)

    try:
        async with async_session_factory() as session:
            row = await session.get(PolicySnapshot, revision)
    except Exception:
        logger.warning(
            "读取策略快照失败。",
            extra={"event": "policy_snapshot_read_failed", "revision": revision},
            exc_info=True,
        )
        return None

    if row is None:
        return None

    payload = row.payload if isinstance(row.payload, dict) else None
    return dict(payload) if payload else None


async def load() -> None:
    """从库里加载全部运行配置。进程启动时调一次。

    表不存在（还没跑迁移）或查询失败都只记日志：按默认值继续跑。
    """
    global _cache, _from_db, _revision

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
        coercer = _COERCERS.get(row.key)
        if coercer is None:
            # 未知键不报错：可能是旧版本留下的，或新版本先写了、回退了代码。
            cache[row.key] = row.value
            continue

        value = coercer(row.value)
        if value is None:
            # 脏值不进缓存，于是回落到默认，`source()` 也如实说「来自 env」——
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

        cache[row.key] = value
        from_db.add(row.key)

    _cache = cache
    _from_db = from_db
    _revision = await _load_revision()

    logger.info(
        "运行配置已加载。",
        extra={
            "event": "runtime_settings_loaded",
            "observability_enabled": obs_enabled(),
            "observability_source": source(OBSERVABILITY_ENABLED),
            "revision": _revision,
            "keys": sorted(cache),
        },
    )


async def _load_revision() -> int:
    """读库里最新的 revision。

    单独一个 try：`policy_snapshots` 还没迁移时，不该把上面刚读到的
    `platform_settings` 一起作废——那是两件独立的事，一件缺表不该连带另一件失效。
    """
    try:
        async with async_session_factory() as session:
            latest = await session.scalar(select(func.max(PolicySnapshot.revision)))
    except Exception:
        logger.warning(
            "读取策略版本失败，本次按 revision=0 运行。",
            extra={"event": "policy_revision_load_failed"},
            exc_info=True,
        )
        return 0

    return int(latest or 0)


async def set_observability(value: bool, *, actor: str | None) -> bool:
    """写入观测开关并立刻更新内存值。返回是否发生了变化。

    只改内存里的这一个键，不重读全表：管理员保存的语义是「把这一项设成
    这个值」，而不是「把页面上的其它项也一起提交」。
    """
    return await set_bool(OBSERVABILITY_ENABLED, value, actor=actor)


async def set_mcp_inbound_enabled(value: bool, *, actor: str | None) -> bool:
    return await set_bool(MCP_INBOUND_ENABLED, value, actor=actor)


async def set_backup_enabled(value: bool, *, actor: str | None) -> bool:
    return await set_bool(TOOL_BACKUP_ENABLED, value, actor=actor)


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


async def set_backup_threshold_mb(value: int, *, actor: str | None) -> bool:
    return await _set_mb(TOOL_BACKUP_THRESHOLD_MB, value, actor=actor)


async def set_backup_max_size_mb(value: int, *, actor: str | None) -> bool:
    return await _set_mb(TOOL_BACKUP_MAX_SIZE_MB, value, actor=actor)


async def _set_mb(key: str, value: int, *, actor: str | None) -> bool:
    """尺寸项统一校验。**不夹边界**：理由同切片上限。"""
    if not (_MB_MIN <= value <= _MB_MAX):
        raise ValueError(f"取值应在 {_MB_MIN} 到 {_MB_MAX} MB 之间。")

    # 阈值大于上限的配置是自相矛盾的：永远先撞总量上限。
    # 不在这里拦，是因为两项可能分两次保存（先调小上限、再调阈值），
    # 拦了会让一次合法的中间态保存失败。矛盾由页面提示，不由写入拒绝。
    return await _persist(key, value, actor=actor)


async def set_bool(key: str, value: bool, *, actor: str | None) -> bool:
    return await _persist(key, value, actor=actor)


async def _persist(key: str, value: Any, *, actor: str | None) -> bool:
    """写库 + 更新内存 + 在生效值真的变了时落一份快照。

    「变了才落快照」不是省事：把「保存成同一个值」也记成一次变更，历史里
    会塞满空转的版本，按 revision 排查时反而要逐条排除。而值没变时行为
    也没变，本来就没有「哪一版」这个问题。
    """
    global _revision

    # 比较的是「生效值」而不是缓存里有没有记录：库为空时旧值来自默认，
    # 管理员把它设成同一个值时行为没变，不该在审计里记成一次变更。
    previous: Any = _read(key)
    changed = previous != value

    async with _write_lock:
        revision = _revision

        async with async_session_factory() as session:
            async with session.begin():
                row = await session.get(PlatformSetting, key)
                if row is None:
                    row = PlatformSetting(key=key, value=value, updated_by=actor)
                    session.add(row)
                else:
                    row.value = value
                    row.updated_by = actor

                if changed:
                    revision = await _next_revision(session)
                    # 快照按「这次写入之后」的生效值渲染：先改副本再渲染，
                    # 顺序反了会存下一份「不含本次改动」的、看起来却像新版的快照。
                    prospective = dict(_cache)
                    prospective[key] = value
                    session.add(
                        PolicySnapshot(
                            revision=revision,
                            payload=_payload(prospective),
                            note=key,
                            created_by=actor,
                        )
                    )

        _cache[key] = value
        _from_db.add(key)
        if changed:
            _revision = revision

    logger.info(
        "运行配置已更新。",
        extra={
            "event": "runtime_setting_updated",
            "key": key,
            "value": value,
            "actor": actor,
            "changed": changed,
            "revision": _revision,
        },
    )
    return changed


async def _next_revision(session: Any) -> int:
    """在写入事务内分配下一个号。

    并发写在这里串行（进程内锁）；跨进程仍可能撞主键，那时事务整体回滚、
    接口报错——比「两个变更写成同一版」好，后者事后分不出谁覆盖了谁。
    """
    latest = await session.scalar(select(func.max(PolicySnapshot.revision)))
    return int(latest or 0) + 1


def reset_for_tests() -> None:
    """测试用：清掉进程内缓存，回到「还没 load 过」的状态。"""
    global _cache, _from_db, _revision
    _cache = {}
    _from_db = set()
    _revision = 0
