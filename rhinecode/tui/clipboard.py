"""
系统剪贴板写入（tui-activity-fold 验收期新增）。

## 为什么不能只靠 Textual 的 `copy_to_clipboard`

那个方法走的是 **OSC 52 转义序列**——由应用把内容编码进一段控制字符发给终端，
再由终端放进系统剪贴板。它的好处是天然支持 SSH（远端应用也能写本地剪贴板），
代价是**终端必须支持且开启它**，而很多终端出于安全考虑**默认关闭**
（应用能往剪贴板塞任意内容是一条真实的攻击面）。

于是用户侧的表现就是「选中了、按了 Ctrl+C、什么也没发生」——**没有任何报错**，
因为应用那一端只是往标准输出写了几个字节，成没成功它根本不知道。

本模块补上另一条路：**直接调操作系统的剪贴板**。两条路一起走，
只要有一条成了就行。

## 各平台的做法与取舍

| 平台 | 做法 | 为什么 |
| --- | --- | --- |
| Windows | `ctypes` 调 Win32 API | **不起子进程**，没有编码与代码页问题（直接给 UTF-16LE） |
| macOS | `pbcopy` | 系统自带，接受 UTF-8 标准输入 |
| Linux | `wl-copy` / `xclip` / `xsel` | 按 Wayland → X11 顺序试，装了哪个用哪个 |

⚠ **全程静默失败**：写剪贴板是锦上添花，任何一步出错都不该影响用户正在做的事
（尤其不能打断「再按一次 Ctrl+C 退出」那条主路径）。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys

# 子进程方式的超时。剪贴板写入是毫秒级操作，给 2 秒是为了在系统卡顿时也能完成；
# 超过就放弃——**绝不能让界面卡在这里**。
_TIMEOUT = 2.0

# Win32 常量
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def copy_text(text: str) -> bool:
    """
    把文本写入系统剪贴板。

    :param text: 要复制的内容；空串直接返回 False（没什么可复制的）
    :returns: 是否写入成功

    副作用：改系统剪贴板；非 Windows 平台会起一个短命子进程。
    """
    if not text:
        return False
    try:
        if sys.platform == "win32":
            return _copy_windows(text)
        if sys.platform == "darwin":
            return _copy_via_command(["pbcopy"], text)
        return _copy_linux(text)
    except Exception:  # noqa: BLE001 —— 见模块 docstring：静默失败
        return False


def _copy_windows(text: str) -> bool:
    """
    Win32 剪贴板 API（`ctypes`，不起子进程）。

    流程是固定的四步：打开剪贴板 → 清空 → 放入一块**全局内存**里的
    UTF-16LE 文本 → 关闭。

    ⚠ 两个容易出错的点：
    1. **内存所有权在 `SetClipboardData` 成功后转移给系统**，此后不能再由我们
       释放；失败时才要自己 `GlobalFree`，否则泄漏。
    2. 必须 `CloseClipboard`，哪怕中途失败——剪贴板是全局互斥资源，
       不关会让**其它程序**也用不了它。
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # ⚠ **必须显式声明类型，否则在 64 位 Windows 上必然失败。**
    #
    # `ctypes` 的默认返回类型是 C 的 `int`（32 位），而这几个函数返回的是
    # **句柄/指针**（64 位）——高 32 位被直接截断，于是拿到一个无效句柄，
    # 后续每一步都在操作垃圾地址。表现是「函数都调了、全都返回失败」，
    # 而且**不抛异常**（本模块整段静默失败，连那点线索都没有）。
    # 实测：不声明时 `copy_text` 恒返回 False。
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    # UTF-16LE + 结尾的空字符（Win32 要求以 NUL 结尾）
    buffer = text.encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(buffer))
    if not handle:
        return False

    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(locked, buffer, len(buffer))
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(_CF_UNICODETEXT, handle):
            # 没接管成功，内存仍归我们，必须自己释放
            kernel32.GlobalFree(handle)
            return False
        # 成功之后**不要**再 GlobalFree：所有权已经转移给系统
        return True
    finally:
        user32.CloseClipboard()


def _copy_linux(text: str) -> bool:
    """
    按 Wayland → X11 的顺序试各个剪贴板工具，装了哪个用哪个。

    ⚠ `xclip` / `xsel` 需要显式指定 `clipboard` 选区：不写的话进的是
    「主选区」（鼠标中键粘贴的那个），而用户按 Ctrl+V 取的是剪贴板选区，
    两者是**不同的东西**——那会表现为「复制了但粘不出来」。
    """
    for command in (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        if _copy_via_command(command, text):
            return True
    return False


def _copy_via_command(command: "list[str]", text: str) -> bool:
    """
    把文本喂给一个读标准输入的剪贴板命令。

    :param command: 命令与参数
    :param text: 内容（按 UTF-8 编码写入）
    :returns: 命令是否存在且以 0 退出

    副作用：起一个短命子进程。
    """
    try:
        result = subprocess.run(
            command,
            input=text.encode("utf-8"),
            timeout=_TIMEOUT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        # 命令不存在 / 超时 / 起不来：换下一个候选，或整体放弃
        return False
