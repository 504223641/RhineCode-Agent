"""
待办清单与既有各层的接线（todo-list 扩展 T16，覆盖 AC6–AC8、AC18、AC21）。

四条接线各验一处：
- **权限**：不弹面板，但 `deny: todo_write` 拦得住（spec F7）
- **子 Agent**：拿不到这个工具，即使角色白名单点名要（spec F9）
- **系统提示**：那段**恰好注入一次**（`_FILLED` 漏改会加两次）
- **会话切换**：清空清单（spec F17 的数据半边）

⚠ 权限那两条**必须一起看**：只验「不弹面板」的话，一个「直接放行、
根本不调引擎」的实现照样全绿——而那正是 C13 起潜伏到 C15 验收期才被
戳穿的真实缺陷（`deny: run_agent` 写了三章都不生效）。
"""

from __future__ import annotations

import unittest

from rhinecode.agent.prompt import build_default_prompt, collect_environment
from rhinecode.config import Config
from rhinecode.permission.adapter import to_request
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode, Rule
from rhinecode.permission.rules import RuleSet
from rhinecode.subagents.models import AgentSpec
from rhinecode.subagents.toolset import GLOBAL_DENIED_TOOLS, resolve_toolset
from rhinecode.todo.render import render_todo_brief
from rhinecode.todo.store import TodoStore
from rhinecode.tools.path_guard import main_project_root
from rhinecode.tools.todo_write import TodoWriteTool

_CWD = main_project_root()


def _spec(name: str, description: str, **kwargs) -> AgentSpec:
    """构造一份最小角色定义（`body` / `source` / `path` 是必填字段）。"""
    return AgentSpec(
        name=name, description=description, body="", source="test", path=None, **kwargs
    )


def _make_tool() -> TodoWriteTool:
    return TodoWriteTool(TodoStore())


class PermissionTest(unittest.TestCase):
    """
    AC6：不弹面板，但用户写的整工具 deny 规则拦得住。

    ## ⚠ 判据必须跑真实 Agent Loop，不能只问引擎

    `system_serial` 的「判 ASK 按 ALLOW」发生在 **`agent/loop.py` 的决策预扫**
    里，不在引擎里——引擎在缺省档下对这类请求照常给 ASK（④层兜底）。
    只断言 `engine.decide(...) != ASK` 会**必然失败**，而那不是缺陷。

    形态照抄 `tests/test_team_tools.py::SystemSerialPermissionTest`：
    跑一轮循环，看**工具执没执行**、**面板弹了几次**。
    """

    def _run_once(self, rules, mode=None):
        """
        跑一轮真实 Agent Loop，模型在第一轮调 `todo_write`。

        :returns: `(工具是否成功, 清单条数, 确认面板弹了几次)`
        """
        import threading

        from rhinecode.agent.loop import Agent, RunOptions
        from rhinecode.provider.base import StreamChunk, ToolCall
        from rhinecode.tools.registry import ToolRegistry

        store = TodoStore()
        registry = ToolRegistry()
        registry.register(TodoWriteTool(store))

        class _Provider:
            def __init__(self) -> None:
                self.n = 0

            def stream_chat(self, messages, effort, tools=None, system=None):
                self.n += 1
                if self.n == 1:
                    yield StreamChunk(
                        type="tool_call",
                        tool_call=ToolCall(
                            id="c1",
                            name="todo_write",
                            arguments={"todos": [{"title": "第一步"}]},
                        ),
                    )
                else:
                    yield StreamChunk(type="text", content="完")

        ok: list[bool] = []
        # 面板计数器。**这是本类第二重要的判据**——「不弹面板」这条性质丢了，
        # 界面上表现为每次更新待办都要人点一次确认。
        asked: list[str] = []

        def _ask(tc, tool, decision):  # noqa: ARG001
            asked.append(tc.name)
            return True

        for event in Agent(_Provider(), registry).run(
            [], "off", False, "", lambda: "", "m", None,
            PermissionEngine(
                RuleSet(rules=rules), mode=mode or PermissionMode.DEFAULT
            ),
            _ask, None, None, threading.Event(), None, None,
            options=RunOptions(),
        ):
            if event.tool_result is not None:
                ok.append(event.tool_result.ok)
        return (ok[0] if ok else None), len(store.snapshot()), len(asked)

    def test_no_rule_executes_without_a_panel(self) -> None:
        """
        ⚠ **反证，也是最容易被改坏的一条。**

        缺省档下③层未命中 → ④模式层判 ASK。`system_serial` 工具必须把它
        **按 ALLOW 处理**：既要执行，又**一次面板都不能弹**。
        """
        executed, count, asked = self._run_once([])
        self.assertTrue(executed, "没有 deny 规则时照常执行")
        self.assertEqual(count, 1, "清单应当真的被写入")
        self.assertEqual(asked, 0, "更新待办不该弹确认面板")

    def test_deny_rule_stops_it(self) -> None:
        """
        ⚠ **本文件里最要紧的一条。**

        `deny: todo_write`（**不带括号**）必须真的拦住它。

        只验「不弹面板」的话，一个「直接放行、根本不调引擎」的实现照样全绿
        ——而那正是 `deny: run_agent` 从 C13 写到 C15 都不生效的那个缺陷。
        **错误的安全承诺比没有承诺更危险。**
        """
        executed, count, asked = self._run_once(
            [Rule(effect="deny", tool="todo_write", pattern="", source="test")]
        )
        self.assertFalse(executed, "deny 规则必须拦得住它")
        self.assertEqual(count, 0, "被拦下时清单一个字节都不该变")
        self.assertEqual(asked, 0, "被 deny 拦下时不该弹面板")

    def test_parenthesised_rule_does_not_match(self) -> None:
        """
        **反证**（既有语义，非本扩展引入）：本工具落 `other` 分支，
        那个分支只认空模式，因此 `deny: todo_write(*)` **不命中**。

        没有这条的话，上一条用带括号的写法也会「通过」，
        而用户照着那么配会发现规则不生效。
        """
        executed, count, _asked = self._run_once(
            [Rule(effect="deny", tool="todo_write", pattern="*", source="test")]
        )
        self.assertTrue(executed, "带模式的规则不命中 other 类请求（既有语义）")
        self.assertEqual(count, 1)

    def test_request_falls_into_other_kind(self) -> None:
        """
        它不进 `_TOOL_MAP`——不读写文件、不执行命令，没有可映射的
        Bash / Read / Edit / Write 语义。这是上面那条「规则必须不带括号」
        的根据。
        """
        request = to_request(_make_tool(), {"todos": []}, PermissionMode.DEFAULT, _CWD)
        self.assertEqual(request.kind, "other")
        self.assertEqual(request.specifier, "")
        self.assertEqual(request.rule_name, "todo_write")


class SubAgentCannotUseItTest(unittest.TestCase):
    """AC8：子 Agent 拿不到这个工具。"""

    ALL_TOOLS = (
        "read_file",
        "write_file",
        "run_command",
        "todo_write",
        "send_message",
    )

    def test_it_is_in_the_global_deny_list(self) -> None:
        self.assertIn("todo_write", GLOBAL_DENIED_TOOLS)

    def test_inheriting_agent_does_not_get_it(self) -> None:
        """`tools` 省略 = 继承主对话工具集，但这一个仍被摘掉。"""
        spec = _spec("worker", "干活的")
        resolved = resolve_toolset(self.ALL_TOOLS, spec).allowed
        self.assertNotIn("todo_write", resolved)
        self.assertIn("read_file", resolved)

    def test_explicit_whitelist_still_does_not_get_it(self) -> None:
        """
        ⚠ **本层排在角色白名单之前**，因此一条 `tools: [todo_write]`
        的角色定义**也拿不到它**——这正是这一层存在的意义。
        """
        spec = _spec("sneaky", "点名要待办工具", tools=("todo_write", "read_file"))
        resolved = resolve_toolset(self.ALL_TOOLS, spec).allowed
        self.assertNotIn("todo_write", resolved)
        self.assertIn("read_file", resolved, "白名单里别的工具应当照常拿到")


class PromptSlotTest(unittest.TestCase):
    """AC21：那段文本恰好注入一次，且不传时零回归。"""

    def setUp(self) -> None:
        cfg = Config(
            protocol="deepseek",
            model="m-test",
            base_url="http://t",
            api_key="k",
            debug_log=False,
        )
        self.env = collect_environment(cfg, "/proj/root")

    def test_brief_is_injected_exactly_once(self) -> None:
        """
        ⚠ **出现两次说明 `builder._FILLED` 漏改了**——那个槽位会被添加两次
        （一次填了内容、一次是空槽）。空槽在拼装时被整体跳过，
        于是这个遗漏在别处**完全看不出来**。
        """
        assembled = build_default_prompt(self.env, todo_brief="TODO_MARKER_XYZ")
        self.assertEqual(assembled.stable.count("TODO_MARKER_XYZ"), 1)

    def test_not_passing_it_changes_nothing(self) -> None:
        """spec N5 零回归：不启用时输出与本扩展之前逐字一致。"""
        assembled = build_default_prompt(self.env)
        self.assertNotIn("待办清单", assembled.stable)
        self.assertNotIn("todo_write", assembled.stable)

    def test_it_goes_into_the_stable_channel(self) -> None:
        """恒定文本要进稳定通道吃前缀缓存，不能每轮重发。"""
        assembled = build_default_prompt(self.env, todo_brief="TODO_MARKER_XYZ")
        self.assertNotIn("TODO_MARKER_XYZ", assembled.dynamic)

    def test_it_sits_before_the_team_brief(self) -> None:
        """
        槽位次序 133 < 134：先说「自己怎么管进度」，再说「什么时候找别人」。

        两段都恒定、缓存上等价，所以这条钉的是**语义次序**，
        改动它不会有任何功能症状——正因如此才需要一条断言把它定住。
        """
        assembled = build_default_prompt(
            self.env, todo_brief="TODO_MARKER", team_brief="TEAM_MARKER"
        )
        self.assertLess(
            assembled.stable.find("TODO_MARKER"),
            assembled.stable.find("TEAM_MARKER"),
        )

    def test_real_brief_carries_the_lower_bound(self) -> None:
        """真正注入的那段里，可数的下限要在（AC21）。"""
        assembled = build_default_prompt(self.env, todo_brief=render_todo_brief())
        self.assertIn("三步", assembled.stable)


class PlanStageTest(unittest.TestCase):
    """AC7：规划阶段可用。"""

    def test_plan_safe_is_declared(self) -> None:
        self.assertTrue(_make_tool().plan_safe)

    def test_plan_stage_call_succeeds(self) -> None:
        store = TodoStore()
        tool = TodoWriteTool(store)
        result = tool.execute({"todos": [{"title": "调研现状"}]}, plan_stage=True)
        self.assertTrue(result.ok, result.output)
        self.assertEqual(len(store.snapshot()), 1)


class SessionSwitchTest(unittest.TestCase):
    """
    AC18 的数据半边：会话切换清空清单。

    界面半边（收起待办块、复位版本号）在 `tests/test_todo_tui.py`。
    ⚠ **两半必须都有**：只清数据的话，界面上那块要等到下一次覆写才收起；
    只收界面的话，新会话第一次读到的是上一段的清单。
    """

    def test_clear_empties_the_store(self) -> None:
        store = TodoStore()
        store.replace([{"title": "上一段对话的事"}])
        store.clear()
        self.assertEqual(store.snapshot(), ())

    def test_clear_bumps_version_so_the_ui_notices(self) -> None:
        """
        ⚠ 清空必须让版本号变——界面靠它判断要不要重绘。
        不变的话待办块会一直挂在那儿显示上一段对话的内容。
        """
        store = TodoStore()
        store.replace([{"title": "a"}])
        before = store.version()
        store.clear()
        self.assertNotEqual(store.version(), before)


class DisabledIsZeroRegressionTest(unittest.TestCase):
    """
    spec N5：未启用（`todo_store is None`）时一切归零。

    这是「不需要配置开关」的依据——装配层不注册，本扩展就整个不存在。
    """

    class _Manager:
        """只带 `todo_store = None` 的最小协调层替身。"""

        todo_store = None

        from rhinecode.conversation import ConversationManager as _CM

        todo_version = _CM.todo_version
        todo_view = _CM.todo_view
        todo_all_done = _CM.todo_all_done
        todo_all_done_text = _CM.todo_all_done_text

    def test_all_readers_degrade_quietly(self) -> None:
        m = self._Manager()
        self.assertEqual(m.todo_version(), 0)
        self.assertIsNone(m.todo_view())
        self.assertFalse(m.todo_all_done())
        self.assertEqual(m.todo_all_done_text(), "")


if __name__ == "__main__":
    unittest.main()
