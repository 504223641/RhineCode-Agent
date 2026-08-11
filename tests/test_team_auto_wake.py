"""
自动唤起的单测（c15 T41，覆盖 AC21、AC25–AC27）。

重点三处：
- **主对话正在跑时绝不触发**（plan 风险 3：`exclusive=True` 的 Worker
  会被新的挤掉，用户正在等的回答凭空消失）；
- **连锁上限兜得住**，且用户一说话就复位（AC25）；
- **上限提示只出现一次**，不刷屏。

界面层用一个**假 App** 验证判定逻辑：真实 `RhineApp` 要起 Textual 事件循环，
而这里要验的是「什么条件下该起 Worker」，那是纯判定，不需要真界面。
`tests/test_tui_*.py` 里另有真实 App 的接线护栏。
"""

from __future__ import annotations

import unittest

from rhinecode.team import TeamService
from rhinecode.team.models import MAIN_NAME
from rhinecode.tui.app import RhineApp


class _FakeManager:
    """只实现自动唤起判定要用到的那几个方法。"""

    def __init__(self, team: TeamService) -> None:
        self.team = team
        self.auto_wake_runs = 0

    # —— 协作查询（转发给真实的 TeamService，验的是真行为）——
    def team_has_unread_for_main(self) -> bool:
        return self.team.has_unread_for_main()

    def team_can_auto_wake(self) -> bool:
        return self.team.can_auto_wake()

    def team_bump_auto_wake(self) -> int:
        return self.team.bump_auto_wake()

    def team_auto_wake_limit(self) -> int:
        return self.team.max_auto_wake_chain

    def team_reset_auto_wake(self) -> None:
        self.team.reset_auto_wake()

    def team_drain_notices(self) -> tuple:
        return self.team.drain_notices()

    def run_auto_wake(self):
        self.auto_wake_runs += 1
        return iter(())

    # —— 轮询里其余会被调到的 ——
    def drain_subagent_notifications(self) -> tuple:
        return ()

    def running_subagent_count(self) -> int:
        return 0


class _Harness:
    """
    把 `RhineApp._maybe_auto_wake` 挂到一组可控状态上。

    刻意**不**继承 `RhineApp`——那会牵进 Textual 的构造链。
    这里只借它那一个方法，验的是判定逻辑本身。
    """

    def __init__(self, team: TeamService) -> None:
        self._manager = _FakeManager(team)
        self._stream_active = False
        self._pending_interaction = None
        self._session_panel_active = False
        self._auto_wake_limit_notified = False
        self.messages: list[str] = []
        self.started: list = []

    def show_message(self, text: str) -> None:
        self.messages.append(text)

    def show_event(self, text: str) -> None:
        """
        事件级通道（tui-display 扩展 F19）。

        自动唤起的两条提示都改走它了——「这段是我不在的时候程序自己跑的」
        是本项目里最不该被渲染成 dim 的消息之一。这里与 `show_message` 收进
        同一个列表：本文件验的是**说没说**与**说了什么**，不是用哪一档说。
        """
        self.messages.append(text)

    def _start_stream_worker(self, gen) -> None:
        self.started.append(gen)

    # 借用真实实现
    _maybe_auto_wake = RhineApp._maybe_auto_wake


def _build(max_chain: int = 5) -> tuple[_Harness, TeamService]:
    team = TeamService(max_auto_wake_chain=max_chain)
    team.register_member("worker-a", "explorer")
    return _Harness(team), team


class TriggerTest(unittest.TestCase):
    def test_fires_when_idle_with_unread(self) -> None:
        """AC21：主对话空闲 + 有未读 → 自动跑一轮，用户零输入。"""
        harness, team = _build()
        team.send("worker-a", MAIN_NAME, "需要你决定")
        harness._maybe_auto_wake()
        self.assertEqual(len(harness.started), 1)
        self.assertEqual(harness._manager.auto_wake_runs, 1)

    def test_does_not_fire_without_unread(self) -> None:
        harness, _team = _build()
        harness._maybe_auto_wake()
        self.assertEqual(harness.started, [])

    def test_does_not_fire_while_streaming(self) -> None:
        """
        ⚠⚠ **plan 风险 3**：流式 Worker 是 `exclusive=True` 的。

        不判空闲就起新 Worker，会把**正在跑的那个挤掉**——用户正在等的回答
        凭空消失，而界面上只表现为「AI 说到一半不说了」，没有任何报错。

        这条用例请勿删改。
        """
        harness, team = _build()
        team.send("worker-a", MAIN_NAME, "需要你决定")
        harness._stream_active = True
        harness._maybe_auto_wake()
        self.assertEqual(harness.started, [], "主对话正在跑时绝不能起新 Worker")

    def test_does_not_fire_while_a_panel_waits(self) -> None:
        """
        确认面板挂着时用户显然在场，自动跑一轮会让两个运行争同一个面板。
        """
        harness, team = _build()
        team.send("worker-a", MAIN_NAME, "需要你决定")
        harness._pending_interaction = object()
        harness._maybe_auto_wake()
        self.assertEqual(harness.started, [])

    def test_does_not_fire_while_session_panel_is_open(self) -> None:
        harness, team = _build()
        team.send("worker-a", MAIN_NAME, "需要你决定")
        harness._session_panel_active = True
        harness._maybe_auto_wake()
        self.assertEqual(harness.started, [])

    def test_announces_the_round(self) -> None:
        """AC26：用户回来要能看出哪一段是自动跑的。"""
        harness, team = _build()
        team.send("worker-a", MAIN_NAME, "需要你决定")
        harness._maybe_auto_wake()
        self.assertTrue(harness.messages)
        self.assertIn("自动唤起", harness.messages[0])
        self.assertIn("1/5", harness.messages[0])


class ChainLimitTest(unittest.TestCase):
    def test_stops_at_the_limit(self) -> None:
        """
        AC25：没有上限的话，两个 Agent 能互相唤醒到天亮、把额度烧光，
        而界面上看起来只是「一直在动」。
        """
        harness, team = _build(max_chain=3)
        for _ in range(5):
            team.send("worker-a", MAIN_NAME, "又一条")
            harness._maybe_auto_wake()
        self.assertEqual(len(harness.started), 3, "达到上限后不再起新 Worker")

    def test_limit_notice_appears_once(self) -> None:
        """
        ⚠ 本方法每 0.5 秒被调一次。不设标志的话，一条「已达上限」的提示
        会在一分钟内刷出 120 行。
        """
        harness, team = _build(max_chain=1)
        for _ in range(20):
            team.send("worker-a", MAIN_NAME, "又一条")
            harness._maybe_auto_wake()
        notices = [m for m in harness.messages if "已达上限" in m]
        self.assertEqual(len(notices), 1)

    def test_user_message_resets_the_chain(self) -> None:
        """
        AC25 后半：用户一说话，计数复位、自动唤起恢复可用。

        （产品里这一步在 `on_input_bar_input_submitted` 里做，
        本用例直接调它转发到的那个方法。）
        """
        harness, team = _build(max_chain=2)
        for _ in range(3):
            team.send("worker-a", MAIN_NAME, "又一条")
            harness._maybe_auto_wake()
        self.assertEqual(len(harness.started), 2)

        harness._manager.team_reset_auto_wake()
        harness._auto_wake_limit_notified = False
        team.send("worker-a", MAIN_NAME, "再来一条")
        harness._maybe_auto_wake()
        self.assertEqual(len(harness.started), 3, "复位后应当能再次自动唤起")


class DisabledTest(unittest.TestCase):
    def test_no_team_means_no_auto_wake(self) -> None:
        """
        spec N5：不启用协作能力时，轮询里这一段是零成本空操作。
        """

        class _NoTeamManager(_FakeManager):
            def team_has_unread_for_main(self) -> bool:
                return False

            def team_can_auto_wake(self) -> bool:
                return False

        harness, team = _build()
        harness._manager = _NoTeamManager(team)
        team.send("worker-a", MAIN_NAME, "有未读")
        harness._maybe_auto_wake()
        self.assertEqual(harness.started, [])


if __name__ == "__main__":
    unittest.main()
