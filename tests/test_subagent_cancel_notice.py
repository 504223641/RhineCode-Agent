"""
`Esc` 之后的「子 Agent 还在跑」提示（todo #1，方案 A）。

## 这一条修的是什么

`Esc` 只置位**主循环**的取消信号，子 Agent 线程照常跑到底。这是 C13
「委派永不阻塞」契约内的行为，但**与用户直觉不符**——真实验收里用户按下
`Esc` 之后，子 Agent 又跑了 7 轮、写了文件、`git commit`、留下一个新的隔离
工作区与分支（`docs/c14/acceptance/live-model.md` 第四节 A）。

用户拍板的修法是**保持语义、把话说清**（方案 A）：不动契约、不动任何并发
路径，只在按下 `Esc` 时告诉用户「还有几个在后台跑、怎么停」。

因此本文件的判据分两层：

1. **数字必须是真的**（`ConversationManager.request_cancel` 的返回值与实际
   运行中的条数一致）——报一个假数字比不报更坏，用户会据此以为已经停干净了；
2. **零个在跑时不提示**（反证）——少了这条，一个「无论如何都提示」的实现
   也能让第一层通过，而那种实现会在每次 `Esc` 后都吓用户一跳。

## 为什么数字由 `request_cancel` 给出，而不是让 TUI 自己去查

`request_cancel` 有两个调用方：真人按键（`tui/app.py`）与端到端驱动器的
`cancel`（`tests/e2e/control.py`，其 docstring 声称「等价于真人按 Esc」）。
把「查条数」留在调用方，两条路径迟早会分叉——而分叉处恰恰是验收依据。
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from rhinecode.bootstrap import build_app
from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider, StreamChunk, ToolCall

_ROLE = """---
name: finder
description: 需要在项目里查找信息时用它。
tools: read_file, glob_files
---
你是查找员。最后一段必须是自包含的结论。
"""

_ROLE_BODY_HEAD = "你是查找员"

# 子 Agent 闸门的兜底上限（秒）：判据是「主对话返回时它还在跑」这个**顺序**，
# 与机器快慢无关；这个数字只防死锁，不是判据的一部分。
_GATE_TIMEOUT = 5.0


class _GatedProvider(BaseProvider):
    """
    主对话第 1 轮以 `background=true` 委派，之后说话；子 Agent 卡在闸门上。

    子 Agent 一直不结束，于是「运行中的条数」在测试里是**确定的 1**，
    不必靠 sleep 去赌时序。
    """

    def __init__(self) -> None:
        self.turns = 0
        self._gate = threading.Event()

    def release(self) -> None:
        self._gate.set()

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.turns += 1
        if (system or "").startswith(_ROLE_BODY_HEAD):
            self._gate.wait(timeout=_GATE_TIMEOUT)
            yield StreamChunk(type="text", content="结论：找到了。")
            yield StreamChunk(type="done")
            return

        if self.turns == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(
                    id="c1",
                    name="run_agent",
                    arguments={
                        "type": "role",
                        "agent": "finder",
                        "task": "找出所有 X",
                        # 必须是后台委派：前台（awaited）会让主对话停下来等它，
                        # 那样就没有「主对话已结束而子 Agent 仍在跑」这个场景了。
                        "background": True,
                    },
                ),
            )
            yield StreamChunk(type="done")
            return

        yield StreamChunk(type="text", content="收到。")
        yield StreamChunk(type="done")


class RequestCancelCountTest(unittest.TestCase):
    """领域层：`request_cancel` 返回的条数必须是真的。"""

    def _build(self, provider):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        user_dir = Path(tmp.name) / "user"
        (user_dir / "agents").mkdir(parents=True)
        (user_dir / "agents" / "finder.md").write_text(_ROLE, encoding="utf-8")

        cfg = Config(protocol="deepseek", model="m", api_key="k", base_url="")
        result = build_app(
            cfg, user_dir=user_dir, provider_factory=lambda c: provider
        )
        self.addCleanup(result.cleanup, "normal_exit")
        return result

    def test_returns_the_number_still_running(self) -> None:
        """
        委派一个跑不完的子 Agent，`Esc` 之后它**仍然在跑**，返回值为 1。

        这同时钉住了方案 A 的核心语义：`Esc` **不**取消子 Agent。
        如果哪天有人顺手改成方案 B（连带取消），这条会红——那不是回归，
        是语义变更，届时应当先改文档与 todo #1 的结论，而不是改这条断言。
        """
        provider = _GatedProvider()
        manager = self._build(provider).manager
        list(manager.submit_user_message("找一下"))

        remaining = manager.request_cancel()

        self.assertEqual(remaining, 1, "提示里的条数必须与实际运行中的条数一致")
        self.assertEqual(
            manager.running_subagent_count(),
            1,
            "Esc 不该停下子 Agent（方案 A：保持 C13「委派永不阻塞」的契约）",
        )

        provider.release()
        self._settle(manager)

    def test_returns_zero_when_nothing_is_running(self) -> None:
        """
        **反证。** 没有子 Agent 在跑时返回 0，界面据此不提示。

        少了这条，一个 `return 1` 的假实现也能让上一条通过。
        """
        provider = _GatedProvider()
        provider.release()  # 子 Agent 不卡了，跑完就走
        manager = self._build(provider).manager
        list(manager.submit_user_message("找一下"))
        self._settle(manager)

        self.assertEqual(manager.request_cancel(), 0)

    def _settle(self, manager, timeout: float = 10.0) -> None:
        """等到全部子 Agent 终态；超时**明确失败**，不静默返回。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tasks = manager.subagent_service.tasks.snapshot()
            if tasks and all(t.status.is_terminal for t in tasks):
                return
            time.sleep(0.01)
        self.fail(f"等了 {timeout} 秒，子 Agent 仍未走到终态")


class EscapeNoticeTest(unittest.IsolatedAsyncioTestCase):
    """
    界面层：按 `Esc` 时那句提示到底出不出。

    复用 `test_command_tui` 的 `_make_app`（Fake Manager + 真 App + Pilot），
    **不另造一套替身**——RhineApp 的 `on_mount` 接触面不小，抄第二份替身的话，
    将来 App 多用一个方法就要同时改两处，而漏改的表现是「测试挂了但产品没坏」。
    """

    def _app_with_remaining(self, remaining: int):
        from tests.test_command_tui import _make_app

        app, manager = _make_app()
        manager.request_cancel = lambda: remaining
        # 只有「流式运行中」那条分支才处理 Esc（其余分支归确认面板 / 命令面板）。
        app._stream_active = True
        return app, manager

    async def test_notice_shows_the_real_count(self) -> None:
        from tests.test_command_tui import _history_text

        app, _ = self._app_with_remaining(3)
        async with app.run_test() as pilot:
            await pilot.press("escape")
            text = _history_text(app)

        self.assertIn("3 个子 Agent", text, "条数要照实说")
        self.assertIn("/agents cancel all", text, "必须告诉用户怎么停")

    async def test_no_notice_when_none_running(self) -> None:
        """
        **反证。** 一个子 Agent 都没有时，`Esc` 不该多出任何一行。

        少了这条，一个「无论如何都提示」的实现照样能让上一条通过——
        而那种实现会在每次 `Esc` 之后都吓用户一跳（「什么？还有东西在跑？」）。
        """
        from tests.test_command_tui import _history_text

        app, _ = self._app_with_remaining(0)
        async with app.run_test() as pilot:
            await pilot.press("escape")
            text = _history_text(app)

        self.assertNotIn("子 Agent", text)
        self.assertNotIn("/agents cancel", text)


if __name__ == "__main__":
    unittest.main()
