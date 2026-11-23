# tests/unit/test_safe_delete.py
"""删除护栏：批量拒绝、回收站布局、三平台后端。

为什么把 Windows / macOS 后端分出来单独测：

- 真正调 `SHFileOperationW` 会往**宿主机回收站**写东西，测试跑一遍就污染一次，
  而且 Windows 上删掉什么取决于跑测试的人。这里只验「传下去的到底是什么」
  （命令行参数与标志位），不验文件系统副作用。
- macOS 的 `FSMoveObjectToTrashSync` 在非 mac 上根本加载不到 CoreServices，
  能验的只有「加载失败时报的是 TrashUnavailable 而不是 ImportError」这类形状。

Linux 的 freedesktop 实现是纯 Python，可以在任何平台按临时目录跑通——
所以它才是验证「回收站里到底留下了什么」的那一个。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import quote

import pytest

from app.sandbox_guard import safe_delete


@pytest.fixture
def trash_home(tmp_path, monkeypatch):
    """把 XDG_DATA_HOME 指到临时目录，回收站就不会写到真实的家目录。"""
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    return data_home / "Trash"


@pytest.fixture(autouse=True)
def _restore_deleters():
    """任何用例装了钩子都必须在结束后摘掉。

    这层是全局补丁（换掉 `os.remove`），漏一次就会影响后面所有用例，
    而且现象是「某个无关的测试开始报奇怪的文件错误」。
    """
    yield
    safe_delete.uninstall()


def test_bulk_guard_rejects_before_moving_anything(tmp_path, trash_home):
    """超过上限时**一个都不动**：拒绝发生在任何移动之前。"""
    files = [tmp_path / f"f{i}.txt" for i in range(3)]
    for file in files:
        file.write_text("x", encoding="utf-8")

    with pytest.raises(safe_delete.BulkDeleteRejected) as excinfo:
        safe_delete.delete_many(files, max_items=2)

    assert excinfo.value.code == "SAFE_DELETE_BULK_REJECTED"
    assert all(file.exists() for file in files)
    assert not (trash_home / "files").exists()


def test_tree_guard_rejects_a_wide_directory(tmp_path, trash_home):
    """整目录删除也要护栏：一次搬走几千个条目不是「清理」，是路径写错了。"""
    tree = tmp_path / "many"
    tree.mkdir()
    for index in range(5):
        (tree / f"{index}.txt").write_text("x", encoding="utf-8")

    with pytest.raises(safe_delete.BulkDeleteRejected):
        safe_delete.safe_trash_tree(tree, max_items=2)

    assert tree.exists()


def test_linux_backend_writes_the_freedesktop_layout(tmp_path, trash_home):
    """`files/` 放原件、`info/` 放 `.trashinfo`——文件管理器认的就是这两处。"""
    source = tmp_path / "report.txt"
    source.write_text("hello", encoding="utf-8")

    moved = safe_delete._linux_trash(source)

    assert not source.exists()
    assert moved.parent == trash_home / "files"
    assert moved.read_text(encoding="utf-8") == "hello"

    info = (trash_home / "info" / "report.txt.trashinfo").read_text(encoding="utf-8")
    assert info.startswith("[Trash Info]")
    # Path 是 URL 编码后的绝对路径。这条用例在 Windows 上跑时源路径带盘符与
    # 反斜杠，所以期望值也要跟着编码——断言里重算一遍，而不是写死一个形态。
    assert f"Path={quote(str(source.resolve()), safe='/')}" in info
    assert "DeletionDate=" in info


def test_same_name_does_not_overwrite_the_earlier_one(tmp_path, trash_home):
    """同名两条都要在。覆盖 info 会让先删的那份变成永远还原不了的孤儿。"""
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()

    first = first_dir / "notes.txt"
    second = second_dir / "notes.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    safe_delete._linux_trash(first)
    safe_delete._linux_trash(second)

    names = sorted(p.name for p in (trash_home / "files").iterdir())

    assert names == ["notes.txt", "notes.txt.2"]
    assert (trash_home / "files" / "notes.txt").read_text(encoding="utf-8") == "first"
    assert (trash_home / "info" / "notes.txt.2.trashinfo").exists()


def test_trashinfo_path_is_url_encoded(tmp_path, trash_home):
    """带空格与中文的名字必须编码，否则 `.trashinfo` 解析不回来。"""
    source = tmp_path / "带 空格 的报告.txt"
    source.write_text("x", encoding="utf-8")

    safe_delete._linux_trash(source)

    info = (
        trash_home / "info" / "带 空格 的报告.txt.trashinfo"
    ).read_text(encoding="utf-8")

    assert "%20" in info
    assert "带 空格" not in info


def test_directory_cannot_go_through_safe_unlink(tmp_path):
    """`os.remove` 删不了目录，这里也不该悄悄替它删掉一棵树。"""
    directory = tmp_path / "dir"
    directory.mkdir()

    with pytest.raises(safe_delete.DeleteTargetInvalid):
        safe_delete.safe_unlink(directory)

    assert directory.exists()


def test_protected_targets_are_refused(trash_home):
    """根目录与家目录本身没有「误删」这回事，只有「清空环境」。"""
    with pytest.raises(safe_delete.DeleteTargetInvalid):
        safe_delete.send_to_trash(Path(os.sep))


def test_windows_backend_asks_for_undo(trash_home, monkeypatch):
    """必须带 FOF_ALLOWUNDO，且 pFrom 是双 NUL 结尾。

    少了 ALLOWUNDO，这个调用就是 `unlink` 换了个名字；少了第二个 NUL，
    SHFileOperation 会去读 pFrom 后面的内存，行为不可预期。
    """
    captured: list = []

    class _FakeShell:
        def SHFileOperationW(self, operation):  # noqa: N802
            captured.append(operation)
            return 0

    monkeypatch.setattr(safe_delete, "_shell32", lambda: _FakeShell())

    target = Path("/tmp/whatever.txt")

    assert safe_delete._win_multi_string(target).endswith("\0\0")

    safe_delete._windows_trash(target)

    struct = captured[0]._obj

    assert struct.wFunc == safe_delete._FO_DELETE
    assert struct.fFlags & safe_delete._FOF_ALLOWUNDO


def test_windows_backend_reports_failure_instead_of_pretending(trash_home, monkeypatch):
    """非零返回必须变成异常：降级成「删掉了」会让调用方以为进了回收站。"""

    class _FailingShell:
        def SHFileOperationW(self, operation):  # noqa: N802
            return 5

    monkeypatch.setattr(safe_delete, "_shell32", lambda: _FailingShell())

    with pytest.raises(safe_delete.TrashUnavailable):
        safe_delete._windows_trash(Path("/tmp/whatever.txt"))


def test_install_is_idempotent_and_restores(tmp_path, trash_home):
    """装两次只有一层包装；卸载后回到原生删除。

    包装叠两层不会报错，只会让异常里的路径越来越长——而这类问题在
    生产里根本不会有人注意，只会觉得「日志有点怪」。
    """
    native_remove = os.remove

    safe_delete.install()
    wrapped = os.remove
    safe_delete.install()

    assert os.remove is wrapped
    assert os.remove is not native_remove

    safe_delete.uninstall()

    assert os.remove is native_remove


def test_sitecustomize_is_on_by_presence_and_only_off_when_told(monkeypatch, tmp_path):
    """镜像里的启动钩子：装了就是开着，只有明确写「关」才不装。

    为什么不是「必须显式打开」：`/opt/pap/runner.py` 起用户代码子进程用
    的是一份写死的最小环境，镜像的 `ENV` 传不到那段代码里。第一版镜像带
    着 `ENV PAP_SAFE_DELETE=1` 构建出来，实测 `os.remove` 上没有被换掉——
    「没写」只能是「开」，否则这层永远不生效。

    按文件路径加载，而不是 `import sitecustomize`：解释器启动时可能已经
    导入过别处的同名模块，`import` 拿到的是那一个——测试会假绿。
    """
    import importlib.util

    hook = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "sandbox"
        / "image"
        / "sitecustomize.py"
    )

    def _load():
        spec = importlib.util.spec_from_file_location("pap_sitecustomize", hook)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # 环境变量不存在（沙箱里的真实情况）→ 装上。
    monkeypatch.delenv("PAP_SAFE_DELETE", raising=False)
    _load()
    assert getattr(os.remove, "_pap_safe_delete", False) is True

    # 明确关掉 → 不再安装。
    safe_delete.uninstall()
    monkeypatch.setenv("PAP_SAFE_DELETE", "0")
    _load()
    assert not getattr(os.remove, "_pap_safe_delete", False)

    safe_delete.uninstall()


def test_a_failed_move_does_not_trash_its_own_bookkeeping(tmp_path, trash_home, monkeypatch):
    """移动失败时撤 `.trashinfo`，撤的动作必须走**原生** unlink。

    这条路径只有在 `install()` 之后才走得到（沙箱容器里就是这样）：
    此时 `os.unlink` 已被换成「送进回收站」，拿它去删自己刚写的记录，
    会在回收站里留下一条指向回收站自己的条目（`.trashinfo.trashinfo`），
    文件管理器里那条永远删不掉，人也说不清它从哪来。

    断言方式是**盯住那个原生引用有没有被调用**，而不是去看回收站里剩下
    什么——`pathlib.Path.unlink` 不一定是 `os.unlink` 的普通调用方：这台
    开发机的宿主沙箱就把 `Path.unlink` 整个换成了自带回收站实现，于是
    「有没有多出条目」这件事取决于跑测试的机器，用例会假绿（实测过）。

    显式钉住 Linux 后端：在 Windows 上跑 `send_to_trash` 会真的往宿主
    回收站写东西，那不是这条用例要验的东西。
    """
    monkeypatch.setattr(safe_delete.sys, "platform", "linux")

    source = tmp_path / "victim.txt"
    source.write_text("内容", encoding="utf-8")

    cleaned: list[str] = []
    monkeypatch.setattr(
        safe_delete, "_NATIVE_UNLINK", lambda p: cleaned.append(str(p))
    )

    safe_delete.install()
    assert getattr(os.remove, "_pap_safe_delete", False), "护栏没装上，这条用例验不到东西"
    try:
        # 只让**搬原件**那一步失败：`Path.replace` 内部也走 os.replace，
        # 无差别地拒绝会把写 `.trashinfo` 也一起弄坏，测不到这条分支。
        native_replace = os.replace

        def _boom(src, *args, **kwargs):  # noqa: ANN001
            if str(src).endswith("victim.txt"):
                raise OSError("模拟跨设备失败")
            return native_replace(src, *args, **kwargs)

        monkeypatch.setattr(safe_delete.os, "replace", _boom)

        with pytest.raises(safe_delete.TrashUnavailable):
            safe_delete.send_to_trash(source)
    finally:
        safe_delete.uninstall()

    # 撤记录这一步必须走原生引用：被替换过的版本会把这条记录再送进回收站。
    assert any(p.endswith("victim.txt.trashinfo") for p in cleaned), cleaned
    assert source.exists(), "移动失败不该把原件弄丢"
    # 原件没被搬走，所以回收站里不该有它的任何痕迹。
    assert not (trash_home / "files" / "victim.txt").exists()


def test_install_respects_the_tree_limit_it_was_given(tmp_path, trash_home):
    """`install(max_items=…)` 必须真的改到 rmtree 用的那个上限。

    镜像里由 `PAP_SAFE_DELETE_MAX_ITEMS` 传进来；这个参数如果只是收下不
    往下传，运维改了环境变量会发现「怎么调都一样」——护栏看着在，阈值
    却是写死的。
    """
    wide = tmp_path / "wide"
    wide.mkdir()
    for index in range(3):
        (wide / f"f{index}.txt").write_text("x", encoding="utf-8")

    safe_delete.install(max_items=2)
    try:
        with pytest.raises(safe_delete.BulkDeleteRejected):
            shutil.rmtree(wide)
    finally:
        safe_delete.uninstall()

    assert wide.is_dir(), "被拒绝时目录必须原样还在"
    assert len(list(wide.iterdir())) == 3


def test_a_loop_of_single_deletes_is_still_caught(tmp_path, trash_home, monkeypatch):
    """逐文件 `os.remove` 也要被算成批量删除。

    模型不会来调 `delete_many`，它会写 `for f in glob('*.log'): os.remove(f)`。
    只看单次参数的话，这种写法每次都合法，护栏永远不响——而它恰恰就是把
    目录清空的那个形状。
    """
    monkeypatch.setattr(safe_delete.sys, "platform", "linux")

    files = []
    for index in range(safe_delete.MAX_BULK_ITEMS + 5):
        item = tmp_path / f"f{index}.log"
        item.write_text("x", encoding="utf-8")
        files.append(item)

    safe_delete.install()
    rejected = 0
    try:
        for item in files:
            try:
                os.remove(item)
            except safe_delete.BulkDeleteRejected:
                rejected += 1
    finally:
        safe_delete.uninstall()

    # 第 51 个开始拒绝，前 50 个已经在回收站里（可还原）。
    assert rejected == 5, rejected
    moved = list((trash_home / "files").iterdir())
    assert len(moved) == safe_delete.MAX_BULK_ITEMS
    assert all(item.exists() for item in files[safe_delete.MAX_BULK_ITEMS:])


def test_a_tree_move_still_works_after_a_bulk_loop(tmp_path, trash_home, monkeypatch):
    """窗口满了之后，整棵目录仍然可以搬。

    实测出来的一处荒谬：脚本刚删完 50 个日志，接着想扔掉自己建的临时
    目录，被「你删得太多了」挡住——而整目录搬家恰恰是更安全的那条路。
    它是一次移动、可整体还原，大小由整树上限单独把关，不该占窗口额度。
    """
    monkeypatch.setattr(safe_delete.sys, "platform", "linux")

    for index in range(safe_delete.MAX_BULK_ITEMS):
        (tmp_path / f"f{index}.log").write_text("x", encoding="utf-8")

    safe_delete.install()
    try:
        for index in range(safe_delete.MAX_BULK_ITEMS):
            os.remove(tmp_path / f"f{index}.log")

        # 窗口此刻是满的：再删一个文件会被拒，但整目录搬运不受影响。
        leftover = tmp_path / "leak.log"
        leftover.write_text("x", encoding="utf-8")
        with pytest.raises(safe_delete.BulkDeleteRejected):
            os.remove(leftover)

        tree = tmp_path / "tmpdir"
        tree.mkdir()
        (tree / "a.txt").write_text("a", encoding="utf-8")
        shutil.rmtree(tree)
    finally:
        safe_delete.uninstall()

    assert not tree.exists()
    names = {p.name for p in (trash_home / "files").iterdir()}
    assert "tmpdir" in names, names
    assert len(names) == safe_delete.MAX_BULK_ITEMS + 1, len(names)
