# app/sandbox_guard/safe_delete.py
"""删除走回收站，而不是 unlink。

## 为什么要这层

一次 `os.remove` 是不可逆的：内容没了，谁也说不清删掉的是什么。而模型的
代码里出现 `shutil.rmtree(...)` 的概率并不低——它常常是「清理中间产物」
这一句的自然写法。等到人发现删错了，能找回的前提是**当初没有真删**。

所以这里把「删除」重新定义成「移到回收站」：语义上仍然是删掉了（目录里
看不见了、后续代码不会读到它），但原件还在系统回收站里等着人捞。

回收站用**各平台自己的**实现，不自己造一个目录：

- Windows：`SHFileOperationW` + `FOF_ALLOWUNDO`——进资源管理器回收站，
  用户按熟悉的路径就能还原；
- macOS：`FSMoveObjectToTrashSync`——进「废纸篓」；
- Linux：freedesktop Trash 规范（`$XDG_DATA_HOME/Trash/{files,info}`）——
  文件管理器认得，`gio trash --list` 也认得。

自己造一个 `.pap-trash/` 目录看起来更简单，但它把「怎么还原」变成了只有
PAP 知道的私事：用户在系统里翻不到，磁盘满了也没人清理。

## 批量护栏

两条，覆盖面不同：

- **一次调用里给出太多路径**（`delete_many`）直接拒绝，**在任何移动之前**；
- **短时间内逐个删太多个**（默认 60 秒窗口内 50 个）从第 51 个开始拒绝。

第二条存在的理由是模型的真实写法：它不会去调 `delete_many`，而是写
`for f in glob('*.log'): os.remove(f)`。只看单次参数的话，这种逐文件删除
每次都是「一」，护栏永远不响——而它恰恰就是把目录清空的那个形状。窗口
计数记的是「这一次执行正在干什么」，不是「参数里有几个」。

**整棵目录的移动不计入窗口**：`shutil.rmtree` 是一次移动、可整体还原，
大小由整树上限单独把关。把它也记进去会出现很荒谬的一幕——脚本刚删完
50 个日志，接着想扔掉自己建的临时目录，却被「你删得太多了」挡住，
而那条路径恰恰更安全。

两条的保证不同，别混为一谈：第二条拒绝时，前 50 个**已经**在回收站里
（拒绝不回滚），只是它们可还原；第一条能做到「一个都没动」，是因为它
一开始就知道总量。

拒绝抛 `BulkDeleteRejected`（`SAFE_DELETE_BULK_REJECTED`）。这不是限流，
而是**形状识别**：人删东西通常一次删几个；一分钟内删几十个的，几乎总是
通配符写错了，或者模型把「清理临时文件」理解成了「清空这个目录」。

回收站也不是垃圾桶的无限版本：把几万个文件搬进去，既慢又占双份空间，
最后还得人去清。

要把整个目录送进回收站请用 `safe_trash_tree`：它是**一次**移动（一个条目），
因此不受条目数护栏约束——但那正是「删一个目录」和「删一目录的文件」
的区别，前者可还原，后者是一堆碎片。

## 边界

这层只在 `install()` 之后生效，且只覆盖 Python 侧的删除入口
（`os.remove` / `os.unlink` / `shutil.rmtree`）。`rm -rf` 是另一个进程，
拦不到——沙箱的隔离边界负责那件事，不要指望这里。
"""

from __future__ import annotations

import ctypes
import datetime
import logging
import os
import shutil
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

# 原生 unlink 的引用，在模块导入时就抓住。实现内部要删自己刚写下的临时文件
# 与 info 时只能用这个：`install()` 之后 `os.unlink` 已经是覆盖过的版本，
# 那时候拿它去清理，等于把「失败的删除」留下的残片再送进回收站一次——
# 回收站里多出一条指向回收站自己的条目，最坏还会绕回来递归。
# 单测里没有容器，这条路径只有真跑起来才走得到。
_NATIVE_UNLINK = os.unlink

# 单次删除的条目上限。50 这个数取的是「人不会一次删这么多、而一次误删
# 通常远超这个数」之间的空档；它可以在 install() 时按沙箱规格调整。
MAX_BULK_ITEMS = 50

# 整棵目录的上限。比 50 宽二十倍，因为「删一个目录」是一次可整体还原的
# 操作；但一万个条目进回收站既搬不动也没人清，那时更可能是路径写错了。
MAX_TREE_ITEMS = 1000

# 时间窗与其内的删除条数上限。看的是**这一个进程正在干什么**：
# `for f in glob('*.log'): os.remove(f)` 每一次调用都只有一个路径，
# 单看参数永远合法，只有按时间聚起来才看得出那是「清空目录」。
BULK_WINDOW_SECONDS = 60
_recent_deletions: deque[float] = deque()


def _count_deletion(max_items: int = MAX_BULK_ITEMS) -> None:
    """给这次删除记账；窗口内超量就拒绝。

    记账发生在真正移动**之前**，但拒绝只挡当前这一个：前面的已经在回收站
    里了。要「一个都没动」得先知道总量，那是 `delete_many` 的事。
    """
    now = time.monotonic()
    while _recent_deletions and now - _recent_deletions[0] > BULK_WINDOW_SECONDS:
        _recent_deletions.popleft()
    _recent_deletions.append(now)

    if len(_recent_deletions) > max_items:
        raise BulkDeleteRejected(
            f"{BULK_WINDOW_SECONDS} 秒内已删除 {max_items} 个条目，后续删除已拒绝。"
            "清理整个目录请用 safe_trash_tree（一次移动，可整体还原），"
            "或者确认这是一次有意的批量清理。"
        )

# 拒绝删除的目标：删掉它们不是「误删一个文件」，是把整个环境清零。
# 只放在这里而不做成配置项——没有人需要「允许删根目录」这个能力。
_PROTECTED = (
    Path("/"),
    Path(os.path.expanduser("~")),
)


class SafeDeleteError(RuntimeError):
    """删除护栏的统一异常基类。`code` 进日志与工具返回值。"""

    code = "SAFE_DELETE_ERROR"


class BulkDeleteRejected(SafeDeleteError):
    """一次要删的条目数超过上限。**移动之前**抛出，不存在删了一半。"""

    code = "SAFE_DELETE_BULK_REJECTED"


class DeleteTargetInvalid(SafeDeleteError):
    """目标不可删（根目录、家目录本身、不存在）。"""

    code = "SAFE_DELETE_INVALID_TARGET"


class TrashUnavailable(SafeDeleteError):
    """这台机器上没法把东西放进回收站。

    **不降级为直接删除**：降级之后，「安全删除」这个名字就成了假话，
    而调用方从返回值上分辨不出来。
    """

    code = "SAFE_DELETE_UNAVAILABLE"


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def _reject_if_protected(path: Path) -> None:
    resolved = path.resolve()
    for protected in _PROTECTED:
        if resolved == protected.resolve():
            raise DeleteTargetInvalid(f"拒绝删除 {resolved}：它是整个环境本身。")


def _count_tree(path: Path) -> int:
    """数一棵树里有多少条目（文件 + 目录）。用于批量护栏的预判。

    用 `os.scandir` 手动走而不 `os.walk`：大目录下 `walk` 的元组开销
    与我们要的信息量不匹配，而这里只需要一个计数。
    """
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    total += 1
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except (PermissionError, FileNotFoundError):
            # 读不到的子树不计入：拒绝的理由是「要删的太多了」，
            # 而不是「有一个子目录没权限」——后者应当由移动时的错误说明。
            continue
    return total


# ---------------- Windows ----------------


def _shell32() -> object:
    """取 shell32。单独一层是为了让测试能在不碰真实文件系统的前提下
    断言「传了什么标志」——直接引 `ctypes.windll` 就没法替换了。"""
    return ctypes.windll.shell32  # type: ignore[attr-defined]


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


_FO_DELETE = 0x0003
# FOF_ALLOWUNDO：进回收站而不是真删。少了它，这个函数就是 unlink 的别名。
# FOF_NOCONFIRMATION / FOF_SILENT / FOF_NOERRORUI：沙箱里没有人能点对话框，
# 弹窗会把一次删除卡到超时。
_FOF_ALLOWUNDO = 0x0040
_FOF_NOCONFIRMATION = 0x0010
_FOF_SILENT = 0x0004
_FOF_NOERRORUI = 0x0400


def _win_multi_string(path: Path) -> str:
    """`SHFileOperation` 的 pFrom 是双 NUL 结尾的多字符串。

    单独一个函数是为了让「两个 NUL」这件事能被断言：赋给 `c_wchar_p` 之后
    再从结构体读回来，字符串会在第一个 NUL 处截断，测试根本看不见它。
    少一个 NUL 时 SHFileOperation 会去读 pFrom 后面的内存，行为不可预期。
    """
    return str(path.resolve()) + "\0\0"


def _windows_trash(path: Path) -> Path:
    struct = _SHFILEOPSTRUCTW()
    struct.wFunc = _FO_DELETE
    struct.pFrom = _win_multi_string(path)
    struct.fFlags = (
        _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    )

    result = _shell32().SHFileOperationW(ctypes.byref(struct))  # type: ignore[attr-defined]

    if result != 0:
        raise TrashUnavailable(
            f"无法把 {path} 移入回收站（SHFileOperation 返回 {result}）。"
        )

    if struct.fAnyOperationsAborted:
        raise TrashUnavailable(f"移入回收站被中止：{path}")

    return path


# ---------------- macOS ----------------


class _FSRef(ctypes.Structure):
    # FSRef 是不透明结构，实际 80 字节。按字节数组声明就够：
    # 我们不读它，只把它原样传回 CoreServices。
    _fields_ = [("opaque", ctypes.c_ubyte * 80)]


def _coreservices() -> object:
    return ctypes.CDLL(
        "/System/Library/Frameworks/CoreServices.framework/CoreServices"
    )


def _macos_trash(path: Path) -> Path:
    """走 `FSMoveObjectToTrashSync`（Carbon 时代的老 API，但仍是 Finder 回收站
    的官方入口，且不依赖 Objective-C 运行时）。

    为什么不用 `NSFileManager.trashItemAtURL`：那需要 pyobjc，而沙箱镜像里
    多一个重依赖只为删个文件不值得。ctypes 调 CoreServices 是零依赖的。
    """
    library = _coreservices()

    fsref = _FSRef()
    # kFSPathMakeRefDoNotFollowLeafSymlink = 0x01：软链本身要进回收站，
    # 而不是它指向的东西。
    make_ref = library.FSPathMakeRefWithOptions
    make_ref.argtypes = [
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.POINTER(_FSRef),
        ctypes.POINTER(ctypes.c_ubyte),
    ]
    make_ref.restype = ctypes.c_int32

    to_trash = library.FSMoveObjectToTrashSync
    to_trash.argtypes = [
        ctypes.POINTER(_FSRef),
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    to_trash.restype = ctypes.c_int32

    status = make_ref(
        str(path.resolve()).encode("utf-8"),
        0x01,
        ctypes.byref(fsref),
        None,
    )
    if status != 0:
        raise TrashUnavailable(f"无法解析路径 {path}（FSRef status={status}）。")

    status = to_trash(ctypes.byref(fsref), None, 0)
    if status != 0:
        raise TrashUnavailable(f"无法把 {path} 移入废纸篓（status={status}）。")

    return path


# ---------------- Linux (freedesktop) ----------------


def _linux_home_trash() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "Trash"


def _free_name(directory: Path, name: str) -> str:
    """在同名已存在时找一个不冲突的名字。

    规范里允许覆盖同名 info 之外的做法，但覆盖会让「后删的」把「先删的」
    记录顶掉——而两条都还在 `files/` 里，于是有一条永远无法还原。
    """
    candidate = name
    index = 2
    while (directory / candidate).exists():
        candidate = f"{name}.{index}"
        index += 1
    return candidate


def _write_trashinfo(info_dir: Path, name: str, original: Path) -> None:
    """写 `.trashinfo`。格式是规范的一部分，不是我们自定的：

        [Trash Info]
        Path=/absolute/path
        DeletionDate=YYYY-MM-DDThh:mm:ss

    `Path` 按 URL 编码（空格变 %20 等），因为它要能表示任意文件名。
    """
    info_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "[Trash Info]\n"
        f"Path={quote(str(original.resolve()), safe='/')}\n"
        f"DeletionDate={_now().strftime('%Y-%m-%dT%H:%M:%S')}\n"
    )
    # 先写临时文件再 rename：中途失败时不会留下半份 info，
    # 而半份 info 会让 `files/` 里那一条变成孤儿（永远还原不了）。
    temp = info_dir / f".{name}.tmp"
    temp.write_text(body, encoding="utf-8")
    temp.replace(info_dir / f"{name}.trashinfo")


def _linux_trash_dir_for(path: Path) -> Path:
    """选一个回收站目录：家目录回收站优先，跨设备时退到该挂载点的顶层回收站。

    跨设备直接 `rename` 会 EXDEV。规范给的解法不是「copy 过去」（那会丢
    元数据且慢），而是用文件所在挂载点的 `.Trash/$uid` 或 `.Trash-$uid`。
    """
    home_trash = _linux_home_trash()
    try:
        home_trash.mkdir(parents=True, exist_ok=True)
        if os.stat(home_trash).st_dev == os.stat(path.parent).st_dev:
            return home_trash
    except OSError:
        pass

    mount_root = path.parent.resolve()
    device = os.stat(path).st_dev
    while True:
        parent = mount_root.parent
        if parent == mount_root:
            break
        try:
            if os.stat(parent).st_dev != device:
                break
        except OSError:
            break
        mount_root = parent

    shared = mount_root / ".Trash"
    if shared.is_dir() and shared.stat().st_mode & 0o1000:
        candidate = shared / str(os.getuid())
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    fallback = mount_root / f".Trash-{os.getuid()}"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _linux_trash(path: Path) -> Path:
    trash_dir = _linux_trash_dir_for(path)
    files_dir = trash_dir / "files"
    info_dir = trash_dir / "info"
    files_dir.mkdir(parents=True, exist_ok=True)

    name = _free_name(files_dir, path.name)
    _write_trashinfo(info_dir, name, path)

    try:
        os.replace(path, files_dir / name)
    except OSError as exc:
        # 移不过去就把刚写的 info 撤掉：留着它会指向一个不存在的东西，
        # 文件管理器会显示一条永远删不掉的「幽灵条目」。
        # 用原生 unlink（见文件顶部 `_NATIVE_UNLINK`）：这里删的是回收站
        # 自己的记录，它不该再被送进回收站。
        try:
            _NATIVE_UNLINK(info_dir / f"{name}.trashinfo")
        except OSError:
            pass
        raise TrashUnavailable(f"无法把 {path} 移入回收站：{exc}") from exc

    return files_dir / name


# ---------------- 分发 ----------------


def send_to_trash(path: str | os.PathLike[str]) -> Path:
    """把单个文件或目录（整棵）送进系统回收站。返回它在回收站里的位置。

    目录是**一次移动**，不是逐文件搬运：可还原的单位是那个目录本身，
    这也正是 `safe_trash_tree` 与 `delete_many` 的区别所在。
    """
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        raise DeleteTargetInvalid(f"目标不存在：{target}")

    _reject_if_protected(target)

    if sys.platform == "win32":
        return _windows_trash(target)
    if sys.platform == "darwin":
        return _macos_trash(target)
    return _linux_trash(target)


def delete_many(
    paths: list[str | os.PathLike[str]],
    *,
    max_items: int = MAX_BULK_ITEMS,
) -> list[Path]:
    """批量送进回收站。**先判断总量再动手**，不会删一半才拒绝。"""
    if len(paths) > max_items:
        raise BulkDeleteRejected(
            f"一次要删除 {len(paths)} 个条目，超过上限 {max_items}，已拒绝。"
            "要删除整个目录请用 safe_trash_tree（一次移动，可整体还原）。"
        )

    return [send_to_trash(path) for path in paths]


def safe_unlink(path: str | os.PathLike[str]) -> Path:
    """`os.remove` 的替代：文件进回收站。软链移动的是链本身。"""
    target = Path(path)
    if target.is_dir() and not target.is_symlink():
        raise DeleteTargetInvalid(f"{target} 是目录，请用 safe_trash_tree。")
    return send_to_trash(target)


def safe_trash_tree(
    path: str | os.PathLike[str],
    *,
    max_items: int = MAX_TREE_ITEMS,
) -> Path:
    """整棵目录一次进回收站。

    树里的条目数超过 `max_items` 直接拒绝。这不是「不让你清理」，而是
    「先看清自己在删什么」：一次要搬走一千个条目的目录，几乎不是清理中间
    产物，而更像把项目根目录当成了临时目录。

    拒绝发生在移动之前，所以不存在删了一半的树。
    """
    target = Path(path)
    if not target.is_dir():
        raise DeleteTargetInvalid(f"{target} 不是目录。")

    count = _count_tree(target)
    if count > max_items:
        raise BulkDeleteRejected(
            f"目录 {target} 下有 {count} 个条目，超过单次上限 {max_items}，已拒绝。"
            "先确认它是不是那个要删的目录；确实需要清理就分成几批，或逐个处理。"
        )

    return send_to_trash(target)


# 原生实现。install() 要把它们换掉，uninstall() 要换回来；
# 存成模块级变量而不是闭包，是为了让「换回来」这件事只在一处发生。
_NATIVE = {
    "rmtree": shutil.rmtree,
    "unlink": os.unlink,
    "remove": os.remove,
}


def install(*, max_items: int = MAX_TREE_ITEMS) -> None:
    """把 `os.remove` / `os.unlink` / `shutil.rmtree` 换成走回收站的版本。

    幂等：重复调用不会重复包装（否则异常里的路径会被套娃，调用栈也会
    一层层变长）。

    **先换 `shutil.rmtree`**：它内部对每个文件调 `os.unlink`，先换 unlink
    的话，一次 rmtree 会变成几百次独立入站，回收站里多出几百个条目，
    而原本那棵目录结构再也拼不回来。

    `max_items` 是整棵目录的上限（`rmtree` 走的就是这条），按镜像规格
    可调；单文件的删除不受它影响。
    """
    if getattr(shutil.rmtree, "_pap_safe_delete", False):
        return

    def _rmtree(path, ignore_errors=False, onerror=None):  # noqa: ANN001
        # `ignore_errors=True` 的语义是「失败就算了」：保留它，
        # 否则一个清理脚本会因为某个子项没权限而整体失败。
        try:
            # 整棵目录**不计入**窗口：它是一次移动、可整体还原，大小由
            # `max_items` 单独把关。记进来的话会出现很荒谬的一幕——脚本
            # 刚删完 50 个日志，接着想把自己建的临时目录整体扔掉，却被
            # 「你删得太多了」挡住，而那条路径恰恰是更安全的那条。
            safe_trash_tree(path, max_items=max_items)
        except BulkDeleteRejected:
            # 批量护栏不是「这一步失败」，是「这个动作不该发生」。
            # 吞掉它等于让护栏在 ignore_errors=True 的调用点上失效。
            raise
        except SafeDeleteError:
            if ignore_errors:
                return
            raise
        except OSError as exc:
            if ignore_errors:
                return
            if onerror is not None:
                onerror(_rmtree, str(path), (type(exc), exc, exc.__traceback__))
                return
            raise

    def _unlink(path, *args, **kwargs):  # noqa: ANN001
        # 记账在这一层而不是 `send_to_trash`：直接调库（测试、维护脚本）
        # 不该被「这个进程刚才删得有点多」牵连。
        _count_deletion()
        safe_unlink(path)

    _rmtree._pap_safe_delete = True  # type: ignore[attr-defined]
    _unlink._pap_safe_delete = True  # type: ignore[attr-defined]

    shutil.rmtree = _rmtree  # type: ignore[assignment]
    os.unlink = _unlink  # type: ignore[assignment]
    os.remove = _unlink  # type: ignore[assignment]

    logger.info(
        "删除已改为走回收站。",
        extra={"event": "safe_delete_installed"},
    )


def uninstall() -> None:
    """恢复原生删除。只给测试用：生产里没有「关掉护栏」这个动作。"""
    shutil.rmtree = _NATIVE["rmtree"]  # type: ignore[assignment]
    os.unlink = _NATIVE["unlink"]  # type: ignore[assignment]
    os.remove = _NATIVE["remove"]  # type: ignore[assignment]
    # 窗口计数跟着一起清零：它是「这一次执行删了多少」的记忆，
    # 护栏摘掉之后还留着，下一次 install 会平白背上旧账。
    _recent_deletions.clear()
