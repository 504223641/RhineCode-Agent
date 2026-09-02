"""
启动期宿主的护栏（first-run-setup 扩展 T1/T14）。

本文件回答一个问题、而且主要回答这一个：**同一个进程里能不能先后跑
两个 Textual App？**

首次启动配置向导的整套设计压在这个前提上——先跑一个最小宿主把向导推上屏，
拿到结果后再照常 `bootstrap.build_app` 跑 `RhineApp`。前提不成立的话方案
要换成「让 build_app 容忍空 key」，那要动装配顺序（`CLAUDE.md` 里
「装配顺序一处不动」是硬不变量），是完全另一套设计。

⚠ **这些用例走的是 `App.run()` 这条真路径**（传 `headless=True` 只是不接管
真实终端），不是 `run_test()` 那条 async pilot 路径。区别要紧：
`run_test()` 自己管事件循环，用它来验「两个 App 能不能连跑」等于没验——
真正会打架的恰恰是 `run()` 内部那次 `asyncio.run()`。

⚠ **T1 阶段这里曾有一个专门的占位 Screen（`_ProbeScreen`），T14 已删。**
留着它等于在产品代码里养一段只有测试用得到的死代码，而现在驱动的是
**真的向导**——按 Esc 放弃，判据同样清晰，还顺带验了「宿主 → 向导 →
拿到结果」这条链是通的。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from rhinecode.setup.models import SetupAction
from rhinecode.tui.setup_host import run_setup


async def _press_escape(pilot) -> None:
    """自动驾驶：等界面稳定后按一次 Esc，让向导走「放弃」那条路。"""
    await pilot.pause()
    await pilot.press("escape")
    await pilot.pause()


def _run_once(path: Path):
    return run_setup(path, headless=True, auto_pilot=_press_escape)


class TwoAppsInOneProcessTest(unittest.TestCase):
    """同进程连跑两个 Textual App。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.yaml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_run_returns_screen_result(self):
        """一次基线：向导的结果确实经 App.run() 交了回来。"""
        outcome = _run_once(self.path)
        self.assertEqual(outcome.action, SetupAction.ABANDONED)

    def test_two_sequential_runs_both_succeed(self):
        """
        连跑两次都要成功——这是本文件的核心判据。

        ⚠ **两次都断言，不能只断言第二次**：只看第二次的话，「第一次就没
        跑起来」与「两次都跑起来了」给出的结果相同，而那正是要区分的两种情况。
        """
        first = _run_once(self.path)
        second = _run_once(self.path)
        self.assertEqual(first.action, SetupAction.ABANDONED)
        self.assertEqual(second.action, SetupAction.ABANDONED)

    def test_three_runs_still_succeed(self):
        """
        三次。

        两次能过而三次不过的失败形态是存在的（某些全局状态在第二次被建立、
        第三次才冲突），而真实使用里「宿主 + RhineApp」之后还可能有 `/setup`
        再推一次 Screen，所以这条不是凑数。
        """
        actions = [_run_once(self.path).action for _ in range(3)]
        self.assertEqual(actions, [SetupAction.ABANDONED] * 3)

    def test_abandoning_writes_nothing(self):
        """放弃就是放弃：一个文件都不许留下。"""
        _run_once(self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])


class HostThenNormalAppTest(unittest.TestCase):
    """
    更贴近真实形态：先跑一次宿主（`run()`），再跑一个普通 App。

    真实启动路径就是这个形状——宿主用 `run()`，随后的 `RhineApp` 也用 `run()`；
    这里第二个换成 `run_test()` 是因为本文件不该把整个 `RhineApp` 拖进来
    （那会让一条本该只验「两个 App 能连跑」的用例，变成一条会被界面改动
    连带弄红的用例）。

    ⚠ **本类刻意不用 `IsolatedAsyncioTestCase`，这是实测踩出来的。**
    那个基类让每条用例本身跑在一个事件循环里，而 `App.run()` 内部会调
    `asyncio.run()`——从运行中的循环里调它直接抛
    `RuntimeError: asyncio.run() cannot be called from a running event loop`。

    这条约束同时是一条**产品侧的事实**，值得记在护栏里：`run_setup()` 只能
    从**同步**上下文调用（`__main__.main()` 正是同步的）。运行中的
    `RhineApp` 里那次 `/setup` 因此**不能**走这个函数，只能往已经活着的 App
    上 `push_screen`——plan 里那个「两个入口共用同一个 Screen、但宿主只给
    启动期用」的分工不是风格选择，是这条约束逼出来的。
    """

    def test_host_then_another_app(self):
        with tempfile.TemporaryDirectory() as d:
            outcome = run_setup(
                Path(d) / "config.yaml", headless=True, auto_pilot=_press_escape
            )
        self.assertEqual(outcome.action, SetupAction.ABANDONED)

        from textual.app import App, ComposeResult
        from textual.widgets import Static

        class _Plain(App):
            def compose(self) -> ComposeResult:
                yield Static("after")

        async def _drive() -> int:
            app = _Plain()
            async with app.run_test() as pilot:
                await pilot.pause()
                return len(app.query(Static))

        self.assertEqual(asyncio.run(_drive()), 1)


class FailureDoesNotBreakStartupTest(unittest.TestCase):
    """
    **向导自己出故障时不许拖垮启动。**

    用户此刻只是想配置一下程序，让向导的 bug 变成「装完就用不了」是不成
    比例的。走 ABANDONED 分支的话，调用方会打印模板路径、告诉他手填也行——
    那条路一直是通的。
    """

    def test_crashing_app_returns_abandoned(self):
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as d:
            with mock.patch(
                "textual.app.App.run", side_effect=RuntimeError("界面炸了")
            ):
                outcome = run_setup(Path(d) / "config.yaml", headless=True)
        self.assertEqual(outcome.action, SetupAction.ABANDONED)


if __name__ == "__main__":
    unittest.main()
