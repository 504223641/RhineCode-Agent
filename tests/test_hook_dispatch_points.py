"""
十二个事件分发点的结构护栏（c12 T35，对应 checklist 第二节 / spec AC2、AC9）。

本文件不验「Hook 做了什么」（那是 test_hook_manager / test_hook_actions 的事），
只验**接线在不在**：每个事件是否真的从它该在的那个位置被分发出来。

这类护栏的价值在于「漏接不报错」——少接一处分发点，界面上、日志里、
既有测试里全都看不出任何异常，只是那个事件永远不触发，而用户会以为是
自己的条件写错了。

统一 fixture 与 `test_bootstrap.py` 同构：临时工作区 + os.chdir + 临时 user_dir。
"""

import os
import tempfile
import unittest
from pathlib import Path

from rhinecode.bootstrap import build_app
from rhinecode.config import Config
from rhinecode.context.manager import ContextManager
from rhinecode.conversation import ConversationManager
from rhinecode.hooks.models import EMPTY_DISPATCH, HookEventType
from rhinecode.provider.base import BaseProvider, Message, StreamChunk
from rhinecode.tools import path_guard

E = HookEventType


class RecordingHooks:
    """记录全部分发的假 manager；`listen` 决定它对哪些事件「有监听者」。"""

    enabled = True
    rules: list = []
    warnings: list = []

    def __init__(self, listen=None):
        self.listen = set(listen) if listen is not None else set(HookEventType)
        self.seen: list[tuple[str, dict]] = []
        self.injections: list[str] = []
        self.context: dict = {}

    def bind_context(self, session_id="", cwd=""):
        if session_id:
            self.context["session_id"] = session_id
        if cwd:
            self.context["cwd"] = cwd

    def has_listeners(self, event):
        return event in self.listen

    def dispatch(self, event, payload_factory=None, cwd=None):
        self.seen.append((event.value, payload_factory() if payload_factory else {}))
        return EMPTY_DISPATCH

    def consume_injections(self):
        texts, self.injections = self.injections, []
        return "\n\n".join(texts)

    def report(self):
        return ""

    def project_notice(self):
        return None

    def events(self):
        return [name for name, _ in self.seen]

    def payload(self, event):
        """取某事件最近一次的负载。"""
        for name, fields in reversed(self.seen):
            if name == event.value:
                return fields
        raise AssertionError(f"事件 {event.value} 从未被分发")


class QuietProvider(BaseProvider):
    """一轮就自然完成的假 Provider。"""

    def __init__(self, text: str = "好的"):
        self.text = text
        self.systems: list[str] = []
        self.rounds = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.rounds += 1
        # 把本轮注入的 <system-reminder> 收下来，供注入通道用例断言
        for m in messages:
            if m.role == "system":
                self.systems.append(m.content or "")
        yield StreamChunk(type="text", content=self.text)
        yield StreamChunk(type="done")


class ErrorProvider(BaseProvider):
    """直接产出流错误的假 Provider（验非正常终止下 turn_end 照样产出）。"""

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        yield StreamChunk(type="error", content="模拟的流错误")


def _cfg(**over) -> Config:
    base = dict(
        protocol="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="fake-key-for-test",
        debug_log=False,
        context_window=65536,
    )
    base.update(over)
    return Config(**base)


class WorkspaceFixture(unittest.TestCase):
    """临时工作区 + 临时 user_dir（与 test_bootstrap.py 同构）。"""

    def setUp(self) -> None:
        self._work = tempfile.TemporaryDirectory()
        self._user = tempfile.TemporaryDirectory()
        self.work = Path(self._work.name).resolve()
        self.user_dir = Path(self._user.name).resolve()
        self._cwd = os.getcwd()
        os.chdir(self.work)
        path_guard.clear_read_roots()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        path_guard.clear_read_roots()
        for d in (self._work, self._user):
            try:
                d.cleanup()
            except OSError:
                pass

    def manager(self, hooks, provider=None) -> ConversationManager:
        m = ConversationManager(
            provider or QuietProvider(),
            _cfg(),
            registry=None,
            user_dir=self.user_dir,
            hook_manager=hooks,
        )
        self.addCleanup(m.memory_manager.close)
        return m


class TurnEventsTest(WorkspaceFixture):
    """回合级两事件（AC2）。"""

    def test_turn_start_and_end_around_a_normal_run(self):
        hooks = RecordingHooks()
        m = self.manager(hooks)
        list(m.submit_user_message("你好"))
        events = hooks.events()
        self.assertEqual(events[0], "turn_start")
        self.assertEqual(events[-1], "turn_end")

    def test_turn_start_payload(self):
        hooks = RecordingHooks()
        m = self.manager(hooks)
        list(m.submit_user_message("你好"))
        fields = hooks.payload(E.TURN_START)
        self.assertEqual(fields["scope"], "main")
        self.assertEqual(fields["trigger"], "user")

    def test_turn_end_carries_stop_reason_and_last_text(self):
        hooks = RecordingHooks()
        m = self.manager(hooks, QuietProvider("这是结论"))
        list(m.submit_user_message("你好"))
        fields = hooks.payload(E.TURN_END)
        self.assertEqual(fields["stop_reason"], "completed")
        self.assertEqual(fields["last_text"], "这是结论")

    def test_turn_end_fires_on_stream_error(self):
        """
        非正常终止下 `turn_end` **照样产出**。

        这条靠的是生成器 `finally` 的语义——若将来有人把分发从 `finally` 里
        挪出来，这条会红。取消与迭代上限走的是同一条路径。
        """
        hooks = RecordingHooks()
        m = self.manager(hooks, ErrorProvider())
        list(m.submit_user_message("你好"))
        self.assertIn("turn_end", hooks.events())
        self.assertEqual(hooks.payload(E.TURN_END)["stop_reason"], "stream_error")

    def test_last_text_not_accumulated_when_nobody_listens(self):
        """spec N7：没人监听 `turn_end` 时不白攒一整回合的正文。"""
        hooks = RecordingHooks(listen={E.TURN_START})
        m = self.manager(hooks, QuietProvider("很长的正文" * 100))
        list(m.submit_user_message("你好"))
        self.assertEqual(hooks.events(), ["turn_start"])


class InjectionChannelTest(WorkspaceFixture):
    """`prompt` 动作的注入通道（AC9）。"""

    def test_injected_text_appears_once_in_the_next_request(self):
        hooks = RecordingHooks()
        hooks.injections.append("记得先跑测试")
        provider = QuietProvider()
        m = self.manager(hooks, provider)

        list(m.submit_user_message("第一句"))
        first = "\n".join(provider.systems)
        self.assertIn("记得先跑测试", first)

        provider.systems.clear()
        list(m.submit_user_message("第二句"))
        second = "\n".join(provider.systems)
        self.assertNotIn(
            "记得先跑测试", second, "注入是一次性的——取走即清，不该出现在下一回合"
        )


class SessionEventsTest(WorkspaceFixture):
    """会话级两事件（AC2）。"""

    def test_clear_sends_end_then_start(self):
        hooks = RecordingHooks()
        m = self.manager(hooks)
        m.clear()
        self.assertEqual(hooks.events(), ["session_end", "session_start"])
        self.assertEqual(hooks.payload(E.SESSION_END)["reason"], "clear")
        self.assertEqual(hooks.payload(E.SESSION_START)["source"], "clear")

    def test_session_id_is_rebound_after_clear(self):
        """
        换档后必须重绑 `session_id`。漏了不报错，只是此后所有负载里的
        `session_id` 都是旧档——而那是排查「这条 hook 是哪次会话触发的」时
        唯一能对上的字段。
        """
        hooks = RecordingHooks()
        m = self.manager(hooks)
        before = hooks.context.get("session_id")
        m.clear()
        after = hooks.context.get("session_id")
        self.assertTrue(before and after)
        self.assertNotEqual(before, after)


class CompactEventsTest(unittest.TestCase):
    """压缩两事件只挂 LLM 摘要（AC2）。"""

    def _manager(self, hooks, tmp: Path) -> ContextManager:
        return ContextManager(QuietProvider(), "m", 65536, hook_manager=hooks)

    def test_manual_compact_sends_both(self):
        hooks = RecordingHooks()
        with tempfile.TemporaryDirectory() as tmp:
            cm = self._manager(hooks, Path(tmp))
            cm.manual_compact([Message(role="user", content="x")])
        self.assertEqual(hooks.events(), ["pre_compact", "post_compact"])
        self.assertEqual(hooks.payload(E.PRE_COMPACT)["trigger"], "manual")

    def test_post_compact_fires_even_when_nothing_to_summarize(self):
        """
        内层有四条 return 路径，这条走的是「无早段」那条。

        外壳的存在就是为了保证每条 return 都过它——逐条手写必漏一条，
        而漏了不报错，只是某种结局下事件凭空消失。
        """
        hooks = RecordingHooks()
        with tempfile.TemporaryDirectory() as tmp:
            cm = self._manager(hooks, Path(tmp))
            notice = cm.manual_compact([])
        self.assertEqual(notice.kind, "noop")
        self.assertEqual(hooks.events(), ["pre_compact", "post_compact"])
        self.assertIs(hooks.payload(E.POST_COMPACT)["ok"], False)

    def test_a_request_that_needs_no_compaction_fires_nothing(self):
        """
        没发生压缩就不该有任何 Hook 事件。

        ⚠ 这条原名 `test_offload_layer_does_not_fire`，验的是「c8 第一层存盘
        不挂 Hook」（那一层每轮可能发生若干次，挂上去只会产生噪音）。第一层已于
        2026-09-17 整层删除，判据因此收窄成更朴素的一条——但**仍然要留着**：
        `before_request` 现在是「够不着触发线就什么都不做」，而「什么都不做」
        必须真的一个事件都不发，否则用户的 `pre_compact` Hook 会在每一轮空转。
        """
        hooks = RecordingHooks()
        with tempfile.TemporaryDirectory() as tmp:
            cm = self._manager(hooks, Path(tmp))
            cm.before_request([Message(role="user", content="x")], allow_summary=False)
        self.assertEqual(hooks.events(), [])


class BootstrapSessionTest(WorkspaceFixture):
    """
    装配层的会话级两事件（AC2、AC21、AC24）。

    这一组走**真实 `build_app`**、读真实 `hooks.yaml`——它是唯一能验到
    「配置文件真的被读到了、事件真的从装配层发出来了」的层次。
    上面那些用假 manager 的用例验的是分发点位置，验不到这一段。
    """

    def _write_project_hooks(self, body: str) -> None:
        path = self.work / ".rhinecode" / "hooks.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def _build(self):
        result = build_app(
            _cfg(),
            user_dir=self.user_dir,
            provider_factory=lambda cfg: QuietProvider(),
        )
        self.addCleanup(result.cleanup)
        return result

    def test_no_config_yields_the_null_implementation(self):
        """spec F13 缺省零行为：两层配置都不存在时连 manager 都是空实现。"""
        result = self._build()
        self.assertFalse(result.manager._hooks.enabled)

    def test_session_start_fires_at_startup(self):
        self._write_project_hooks(
            "hooks:\n"
            "  - name: 开局提醒\n"
            "    event: session_start\n"
            "    action:\n"
            "      type: prompt\n"
            "      text: 当前分支是 main\n"
        )
        result = self._build()
        self.assertTrue(result.manager._hooks.enabled)
        self.assertIn("触发：1 次", result.manager.hooks_report())

    def test_session_end_fires_on_cleanup(self):
        self._write_project_hooks(
            "hooks:\n"
            "  - name: 收尾\n"
            "    event: session_end\n"
            "    action:\n"
            "      type: prompt\n"
            "      text: 再见\n"
        )
        result = self._build()
        self.assertIn("从未触发", result.manager.hooks_report())
        result.cleanup()
        self.assertIn("触发：1 次", result.manager.hooks_report())

    def test_project_notice_lists_each_rule_in_full(self):
        """
        spec F9.1：项目级规则逐条列出，**命令串完整**。

        ⚠ 它**不在** `startup_notice` 里，而是经 `hooks_project_notice()` 单独交给
        界面层的醒目通道（`show_warning` → 橙色粗体）。混进 `startup_notice` 会让它
        跟着走 `[dim]`，比普通提示还不显眼——而它是「这些命令会直接执行」的警告。
        """
        long_cmd = "curl -X POST https://example.com/hook --data @report.json"
        self._write_project_hooks(
            "hooks:\n"
            "  - name: 上报\n"
            "    event: session_start\n"
            "    action:\n"
            "      type: command\n"
            f"      command: '{long_cmd}'\n"
        )
        result = self._build()
        notice = result.manager.hooks_project_notice() or ""
        self.assertIn("项目级 Hook 规则", notice)
        self.assertIn("直接执行", notice)
        self.assertIn(long_cmd, notice, "命令串必须完整展示，不能截断")
        self.assertNotIn(
            "**", notice,
            "上屏文本里不能有 Markdown 星号——Textual 只认 [bold]，`**` 会显示成字面星号",
        )

    def test_project_notice_appears_on_every_startup(self):
        """刻意不做「只提示一次」的持久化——新拉进来的规则不能被静默吞掉。"""
        self._write_project_hooks(
            "hooks:\n"
            "  - name: 上报\n"
            "    event: turn_start\n"
            "    action: {type: prompt, text: hi}\n"
        )
        first = self._build().manager.hooks_project_notice() or ""
        second = self._build().manager.hooks_project_notice() or ""
        self.assertIn("项目级 Hook 规则", first)
        self.assertIn("项目级 Hook 规则", second)

    def test_user_level_rules_produce_no_project_notice(self):
        (self.user_dir / "hooks.yaml").write_text(
            "hooks:\n"
            "  - name: 我自己的\n"
            "    event: turn_start\n"
            "    action: {type: prompt, text: hi}\n",
            encoding="utf-8",
        )
        result = self._build()
        self.assertTrue(result.manager._hooks.enabled)
        self.assertIsNone(result.manager.hooks_project_notice())

    def test_load_warning_reaches_the_startup_notice(self):
        """坏配置不阻断启动，但必须**可见**（spec F8 的补偿手段）。"""
        self._write_project_hooks(
            "hooks:\n"
            "  - name: 写坏了\n"
            "    event: on_monday\n"
            "    action: {type: prompt, text: hi}\n"
        )
        result = self._build()
        notice = result.manager.startup_notice or ""
        self.assertIn("Hook 规则加载提示", notice)
        self.assertIn("写坏了", notice)


if __name__ == "__main__":
    unittest.main()
