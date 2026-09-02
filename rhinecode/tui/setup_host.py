"""
首次启动配置向导的**启动期宿主**（first-run-setup 扩展 T1/T14）。

## 这个模块解决什么问题

向导必须跑在 `bootstrap.build_app` **之前**——`build_app` 第 ⑨ 步要拿
`cfg.api_key` 去造 Provider，而向导存在的理由恰恰是「那个 key 还没有」。
但那时 `RhineApp` 还不存在，也就没有任何东西能把一个 Textual `Screen`
推上屏幕。

于是这里起一个**只做一件事的最小 App**：挂载后立刻把向导 Screen 推上去，
拿到结果就退出。它与随后的 `RhineApp` 是**同一个进程里先后跑的两个 App**。

## ⚠ 「同进程连跑两个 Textual App」是本项目从没做过的事（T1 已验通）

`App.run()` 内部走 `asyncio.run()`，每次调用创建并关闭一个全新的事件循环，
因此先后跑两个 App 在原理上成立。T1 把它实测验过了（连跑 2 次、3 次都成功），
护栏见 `tests/test_setup_host.py`。

## ⚠ `run_setup` 只能从同步上下文调用（T1 实测）

`App.run()` 内部是 `asyncio.run()`，而它**从一个运行中的事件循环里调用会直接
抛 `RuntimeError`**。`__main__.main()` 是同步的，所以启动期那次没问题；
但运行中的 `RhineApp` 活在自己的事件循环里，**`/setup` 因此不能走本模块**，
只能往那个已经活着的 App 上 `push_screen`。

plan 里「两个入口共用同一个 Screen、但宿主只给启动期用」的分工不是风格
选择，是这条约束逼出来的。

## ⚠ 向导自己出故障时不许拖垮启动

`run_setup` 把 App 跑挂的情形兜住，返回 `ABANDONED` 并在 stderr 留一行。
用户此刻只是想配置一下程序，让向导的 bug 变成「装完就用不了」是不成比例的。
走 `ABANDONED` 分支的话，调用方会打印模板路径、告诉他手填也行——
那条路一直是通的。
"""

import sys
from pathlib import Path
from typing import Any, Callable, Optional

from rhinecode.setup.models import SetupAction, SetupDraft, SetupMode, SetupOutcome


def run_setup(
    config_path: Path,
    prefill: Optional[SetupDraft] = None,
    mode: SetupMode = SetupMode.FIRST_RUN,
    *,
    headless: bool = False,
    auto_pilot: Optional[Callable[..., Any]] = None,
) -> SetupOutcome:
    """
    起一个最小宿主跑完配置向导，返回结果。

    :param config_path: 要写到哪个配置文件
    :param prefill: 预填草稿（`/setup` 用；启动期一般是 None）
    :param mode: `FIRST_RUN` / `RERUN`
    :param headless: **仅供测试**：不接管真实终端。测试进程里没有终端，
        但走的仍是 `run()` 这条真路径（不是 `run_test()` 那条 async pilot 路径）
    :param auto_pilot: **仅供测试**：自动驱动按键的协程
    :returns: `SetupOutcome`；向导自身出故障时返回 `ABANDONED`

    副作用：接管终端并跑一个完整的 Textual 生命周期；向导内部可能发起
    两次网络请求并写配置文件。

    ⚠ 只能从**同步**上下文调用，理由见模块 docstring。
    """
    # ⚠ 延迟 import：本模块被 `__main__` 顶层 import，而 `setup_screen`
    # 会把整套 Textual 部件拉起来。只是「判一下该不该弹向导」的启动路径
    # （绝大多数次启动）不该付这笔钱。
    from textual.app import App, ComposeResult

    from rhinecode.tui.setup_screen import SetupScreen

    class _SetupHost(App[SetupOutcome]):
        """只做一件事的宿主：推向导、拿结果、退出。"""

        def compose(self) -> ComposeResult:
            # 宿主自己不画任何东西——屏幕整个交给向导。
            return []

        def on_mount(self) -> None:
            self.push_screen(
                SetupScreen(config_path, prefill=prefill, mode=mode), self._on_done
            )

        def _on_done(self, outcome: Optional[SetupOutcome]) -> None:
            # outcome 为 None 只可能来自「Screen 被强制关掉」，按放弃处理。
            self.exit(outcome or SetupOutcome(action=SetupAction.ABANDONED))

    try:
        result = _SetupHost().run(headless=headless, auto_pilot=auto_pilot)
    except Exception as exc:  # noqa: BLE001 —— 见模块 docstring：不许拖垮启动
        print(f"配置向导没能启动（{exc}），改用手工填写。", file=sys.stderr)
        return SetupOutcome(action=SetupAction.ABANDONED)

    return result or SetupOutcome(action=SetupAction.ABANDONED)
