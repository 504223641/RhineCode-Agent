"""
「现在是谁在调用」（c15 T21 的前置）。

## 要解决的问题

协作工具（发消息、认领任务）需要知道调用方是谁：一条消息要填发件人，
一次认领要填认领人。但**工具实例是全进程共享的**——注册中心里只有一份
`SendMessageTool`，主 Agent 与所有队员用的是同一个对象。
构造时把名字固定进去是行不通的。

## 做法：线程本地身份

每一条 Agent Loop 都跑在自己的线程里（主对话在 TUI 的 Worker 线程，
每个子 Agent 在自己的 daemon 线程），因此「当前身份」天然是线程本地的量。

这与 trace 记录器的作用域机制是同一套做法（`recorder.bind_scope` 也用
`threading.local`），本项目已经验证过它在并发子 Agent 下互不干扰。

## ⚠ 绑定是每条运行的**强制**步骤

- 主对话：协调层在每次运行开头绑 `main`；
- 子 Agent：运行器在线程开头绑它自己的名字。

漏绑不会报错，只会让那条运行以 `main` 的身份发消息与认领任务——
表现是「worker-a 认领的任务显示成 main 认领的」，而没有任何错误提示。
`tests/test_team_tools.py` 与 `tests/test_team_wake.py` 各有一条护栏
钉住真实路径上的绑定。

缺省值取 `main` 而不是抛异常：工具在测试与非工具模式下也会被构造，
而那些场景里「就当是主对话」是唯一合理的解释。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from rhinecode.team.models import MAIN_NAME

_local = threading.local()


def bind_identity(name: str) -> None:
    """
    把当前线程的协作身份绑成 `name`。

    :param name: 队员名字，或 `main`

    副作用：改线程本地状态（只影响调用它的那条线程）。
    """
    _local.name = (name or MAIN_NAME).strip() or MAIN_NAME


def current_identity() -> str:
    """
    当前线程的协作身份。

    :returns: 绑定过的名字；未绑定时返回 `main`

    副作用：无。
    """
    return getattr(_local, "name", MAIN_NAME)


@contextmanager
def identity(name: str):
    """
    临时切换身份的上下文管理器（测试与嵌套场景用）。

    产品代码里用 `bind_identity` 就够了——一条线程的生命周期就是一次运行，
    不需要「出来自动恢复」。本函数存在是为了让测试能在同一条线程里
    模拟多个身份而不互相污染。
    """
    previous = current_identity()
    bind_identity(name)
    try:
        yield
    finally:
        bind_identity(previous)


__all__ = ["bind_identity", "current_identity", "identity"]
