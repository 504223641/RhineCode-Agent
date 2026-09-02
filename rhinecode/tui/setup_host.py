"""
首次启动配置向导的**启动期宿主**（first-run-setup 扩展 T1/T14）。

## 这个模块解决什么问题

向导必须跑在 `bootstrap.build_app` **之前**——`build_app` 第 ⑨ 步要拿
`cfg.api_key` 去造 Provider，而向导存在的理由恰恰是「那个 key 还没有」。
但那时 `RhineApp` 还不存在，也就没有任何东西能把一个 Textual `Screen`
推上屏幕。

于是这里起一个**只做一件事的最小 App**：挂载后立刻把向导 Screen 推上去，
拿到结果就退出。它与随后的 `RhineApp` 是**同一个进程里先后跑的两个 App**。

## ⚠ 「同进程连跑两个 Textual App」是本项目从没做过的事

`App.run()` 内部走 `asyncio.run()`，每次调用创建并关闭一个全新的事件循环，
因此先后跑两个 App 在原理上是成立的。但「原理上成立」和「这个版本的
Textual 上真的成立」是两件事，所以本模块的第一个版本**只包含一个立刻
自我关闭的占位 Screen**，配一条护栏用例把它钉住（`tests/test_setup_host.py`）。

真正的四屏向导在 T11–T13 落地后由 T14 换进来。**先验证最大的未知，
再往上盖东西**——跑不通的话整个方案要换成「让 build_app 容忍空 key」，
那是另一套设计，越早知道越好。

## ⚠ `run_setup` 只能从同步上下文调用（T1 实测）

`App.run()` 内部是 `asyncio.run()`，而它**从一个运行中的事件循环里调用会直接
抛 `RuntimeError`**。`__main__.main()` 是同步的，所以启动期那次没问题；
但运行中的 `RhineApp` 活在自己的事件循环里，**`/setup` 因此不能走本模块**，
只能往那个已经活着的 App 上 `push_screen`。

plan 里「两个入口共用同一个 Screen、但宿主只给启动期用」的分工不是风格
选择，是这条约束逼出来的。护栏见 `tests/test_setup_host.py` 的
`HostThenNormalAppTest` 类注释。

## 为什么测试要传 headless=True

`App.run()` 缺省要接管真实终端（切备用屏、进 raw 模式）。测试进程里没有
终端，`headless=True` 让 Textual 用无头驱动跑完整生命周期——**它跑的仍是
`run()` 这条真路径**（不是 `run_test()` 那条 async pilot 路径），所以
「两个 App 能不能先后跑」这个问题才真的被验到了。
"""

from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static


class _ProbeScreen(ModalScreen[str]):
    """
    T1 的占位 Screen：挂载后立刻自我关闭。

    它**不代表最终形态**，只用来回答一个问题：一个 `ModalScreen` 能不能
    在最小宿主里被推上去、把返回值交回来。T14 会用真的 `SetupScreen`
    把它替换掉。
    """

    def compose(self) -> ComposeResult:
        # 内容无关紧要，但必须有一个可挂载的子部件——空的 Screen 在某些
        # Textual 版本上布局阶段会走到不同分支，那不是我们想验的东西。
        yield Static("RhineCode 正在准备首次设置…")

    def on_mount(self) -> None:
        # 立刻交回结果。放在 on_mount 而不是 compose 里：compose 阶段
        # Screen 还没挂上，dismiss 的回调链尚未建立。
        self.dismiss("ok")


class _ProbeHost(App[str]):
    """T1 的最小宿主 App：推一个 Screen，拿到结果就退出。"""

    def on_mount(self) -> None:
        self.push_screen(_ProbeScreen(), self._on_done)

    def _on_done(self, result: str | None) -> None:
        """
        Screen 关闭时的回调。

        :param result: `dismiss()` 交回的值；Screen 被强制关闭时可能是 None
        """
        # exit 的参数就是 `App.run()` 的返回值——这条链路正是本次要验的。
        self.exit(result)


def run_setup_probe(*, headless: bool = False) -> str | None:
    """
    跑一次最小宿主，返回占位 Screen 交回的值。

    :param headless: 真则用无头驱动（测试进程里没有终端时必须传真）
    :returns: 占位 Screen 的返回值，正常情况下是 "ok"

    副作用：接管终端并跑一个完整的 Textual 生命周期（headless 时不接管）。
    """
    return _ProbeHost().run(headless=headless)
