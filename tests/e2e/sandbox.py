"""
一次性工作区与用户级目录：创建、**可丢弃校验**、三步清理。

## 为什么要有「可丢弃校验」（spec F8 第一条）

宿主会在结束时 `rmtree` 掉整个工作区。这个动作一旦对错目录执行，后果是不可逆的
——想象一下它跑在你的项目根上。所以在 `chdir` 进去之前必须先证明「这确实是本设施
造出来的、可以随时丢弃的目录」。

判据是**两条同时成立**：

1. `path.resolve()` 落在 `tempfile.gettempdir()` 之下；
2. 目录内存在本设施写下的标记文件 `.rhine-e2e-workspace`。

用标记文件而不是「本进程创建过的目录集合」，是因为**宿主进程与测试进程可能不是
同一个**——测试用 `subprocess` 起宿主，宿主自己建目录、自己清理，测试进程的内存
里根本没有那个集合。标记文件是落在磁盘上的、跨进程可见的同一份证据。

⚠️ **本校验与「目录是否为空」完全无关**。预置（`seeding.py`）跑完之后工作区里就有
Skill、RHINE.md、git 历史了，非空是常态。spec AC18 把「临时空目录通过」与
「临时非空目录仍通过」两条放在一起验，就是为了钉死这一点。

## 清理三步的顺序（实测定的，不可调）

    ① build_app 的 cleanup()  →  ② os.chdir(previous_cwd)  →  ③ shutil.rmtree(path)

- ① 记录文件的句柄由 `cleanup` 的第五步（`recorder.close()`）关闭。不先关，
  Windows 下删不掉正在被打开的文件。
- ② **实测**：当前工作目录位于待删目录**之内**时，`rmtree` 必抛
  `PermissionError: [WinError 32] 另一个程序正在使用此文件，进程无法访问`。
  进程自己的 cwd 就是那个「另一个程序」。必须先退出来。

AC24「连续起停若干次不因目录占用失败」抓的正是这两条。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Union


# 标记文件名。改它等于让此前留下的目录全部变成「不可丢弃」，谨慎。
MARKER = ".rhine-e2e-workspace"

# 清理的重试次数与退避基数（秒）。总耐心 ≈ 0.4+0.8+…+2.8 ≈ 11 秒，
# 足够覆盖实测到的 Windows 句柄释放延迟；见 `cleanup_workspace` 第③步的注释。
CLEANUP_ATTEMPTS = 8
CLEANUP_BACKOFF = 0.4


class NotDisposableError(RuntimeError):
    """目标目录不满足「可随时丢弃」的判据。消息里会写明是哪一条不满足。"""


def _write_marker(path: Path, role: str) -> None:
    """
    在目录里落下标记文件。

    内容写创建时间与创建者 pid，纯粹为了排障——某天你在系统临时目录里看到一堆
    `rhine_e2e_ws_*`，可以直接 cat 标记文件知道它是什么时候、被哪个进程留下的。
    """
    path.joinpath(MARKER).write_text(
        f"role={role}\ncreated_at={time.strftime('%Y-%m-%d %H:%M:%S')}\ncreator_pid={os.getpid()}\n",
        encoding="utf-8",
    )


def create_workspace(prefix: str = "rhine_e2e_ws_") -> Path:
    """
    新建一个一次性工作区（宿主会 `chdir` 进去，它就是 RhineCode 眼里的项目根）。

    :returns: 已落好标记文件的目录路径
    副作用：在系统临时目录下创建目录并写入标记文件。
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _write_marker(path, "workspace")
    return path


def create_user_dir(prefix: str = "rhine_e2e_user_") -> Path:
    """
    新建一个一次性用户级目录（顶替真实的 `~/.rhinecode`，落实 spec F17 的隔离）。

    :returns: 已落好标记文件的目录路径
    副作用：同 `create_workspace`。
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _write_marker(path, "user_dir")
    return path


def assert_disposable(path: Union[str, Path]) -> None:
    """
    证明 `path` 是本设施造出来的、可以随时删除的目录。

    :raises NotDisposableError: 两条判据中的任意一条不满足；消息写明是哪一条。

    这是 `rmtree` 之前唯一的闸门，**不要为了让某个场景跑通而绕过它**。
    """
    path = Path(path)
    resolved = path.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()

    # 判据①：位于系统临时目录之下
    try:
        resolved.relative_to(temp_root)
    except ValueError:
        raise NotDisposableError(
            f"判据①不满足：{resolved} 不在系统临时目录 {temp_root} 之下。\n"
            f"本设施只会删自己在临时目录里造的目录，绝不碰别处。"
        ) from None

    # 判据②：目录内有本设施的标记文件
    marker = resolved / MARKER
    if not marker.is_file():
        raise NotDisposableError(
            f"判据②不满足：{resolved} 里没有标记文件 {MARKER}。\n"
            f"该目录不是本设施创建的（或标记已被删除），拒绝把它当一次性目录处理。"
        )


def _clear_readonly(func, path, _exc_info) -> None:
    """
    `shutil.rmtree` 的错误处理钩子：把只读位清掉再重试一次。

    **为什么必需**（实测）：git 会把 `.git/objects` 下的对象文件标成**只读**，
    Windows 上 `rmtree` 撞到只读文件直接抛 `PermissionError`，于是一个预置过
    git 历史的工作区**永远删不掉**——留下的残骸里只剩半个 `.git`，
    看起来像「清理成功了但目录还在」。

    只在这里处理只读位这一种情形；别的错误照常抛出（不吞，见 spec N7）。
    """
    import stat

    os.chmod(path, stat.S_IWRITE)
    func(path)


def force_rmtree(path: Union[str, Path]) -> None:
    """
    尽力删掉一个目录，**供测试的兜底清理使用**（不做可丢弃校验、不切工作目录）。

    ⚠️ **测试里凡是要 `rmtree` 一个沙箱目录，都必须走本函数，不要直接写
    `shutil.rmtree(path, ignore_errors=True)`。**

    实测教训：`ignore_errors=True` 撞上 git 留下的**只读** `.git/objects` 会
    「删一半」——目录还在、内容没了，看起来像清理成功了其实没有。
    这种残骸只在全量测试里出现（单跑那条用例时 git 的句柄早已释放），
    追起来极费劲：现象是「系统临时目录里莫名多出一个只剩 `.git` 的空壳」，
    而报错一个都没有。
    """
    path = Path(path)
    for attempt in range(CLEANUP_ATTEMPTS):
        if not path.exists():
            return
        try:
            shutil.rmtree(str(path), onerror=_clear_readonly)
            return
        except OSError:
            time.sleep(CLEANUP_BACKOFF * (attempt + 1))
    shutil.rmtree(str(path), ignore_errors=True)


def cleanup_workspace(path: Union[str, Path], *, previous_cwd: Union[str, Path]) -> None:
    """
    清理一次性目录——三步中的第②③步。

    :param path: 待删目录
    :param previous_cwd: 进入工作区**之前**的工作目录，用于先退出来
    :raises NotDisposableError: 目标不可丢弃（先校验再删，永远如此）
    :raises OSError: 删不掉。**刻意不用 `ignore_errors=True`**——
        删不掉说明有句柄没关或 cwd 没退出，那正是 AC24 要抓的东西；
        吞掉它会让这条护栏形同虚设，然后在某次连跑里以「临时目录攒了 40 个」的形式
        浮出水面（spec N7）。

    ⚠️ **调用方必须先跑过 `build_app` 的 `cleanup`**（三步中的第①步）。
    它在本函数之外，因为本模块不认识 `build_result`。顺序理由见模块 docstring。

    副作用：切换进程工作目录、递归删除 `path`。
    """
    assert_disposable(path)
    # 第②步：退出待删目录，否则 Windows 下 rmtree 必抛 WinError 32
    os.chdir(str(previous_cwd))
    # 第③步：真正删除。
    #
    # `onerror` 只处理「只读位」这一种情形（见 `_clear_readonly`）。
    # 外面再套一层**有界重试**：Windows 上刚结束的子进程（尤其是 git）与后台扫描
    # 会短暂持有文件与目录句柄，第一次 `rmtree` 常常删掉了大部分内容却在
    # 某个目录上抛 `PermissionError`，留下一个空骨架。实测在全量测试的并发负载下
    # 才会出现，单独跑必成功——所以判据不是「重写逻辑」而是「等一会儿再试」。
    #
    # **最后一次仍然让异常抛出来**：删不掉要暴露，不能吞（spec N7）。
    for attempt in range(CLEANUP_ATTEMPTS):
        try:
            shutil.rmtree(str(path), onerror=_clear_readonly)
            return
        except OSError:
            if attempt == CLEANUP_ATTEMPTS - 1:
                raise
            time.sleep(CLEANUP_BACKOFF * (attempt + 1))
