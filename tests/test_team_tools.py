"""
五个协作工具的单测（c15 T25，覆盖 AC5–AC10、AC12–AC14、AC28、AC30、AC34）。

重点三处：**认领走原子路径**（而不是 `update(owner=...)`）、
**规划阶段的收件人限制**、以及 **`SameVoiceTest`**——工具描述与注入消息的
标记块必须同口径，那是本项目第四次面对同一个坑。
"""

from __future__ import annotations

import threading
import unittest

from rhinecode.team import TeamService
from rhinecode.team.identity import bind_identity, current_identity, identity
from rhinecode.team.models import MAIN_NAME, TaskState
from rhinecode.team.render import render_incoming
from rhinecode.tools.send_message import SendMessageTool
from rhinecode.tools.team_tasks import (
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    build_board_tools,
)


def _build():
    service = TeamService()
    service.register_member("worker-a", "explorer", read_only=True)
    service.register_member("worker-b", "general", read_only=False)
    create, listing, get, update = build_board_tools(service)
    return service, create, listing, get, update, SendMessageTool(service)


class ToolMetadataTest(unittest.TestCase):
    """
    工具标志位的取值符合设计——它们决定工具走哪条执行路径、进不进权限管线。
    改错不报错，只是行为悄悄变了。
    """

    def setUp(self) -> None:
        self.service, self.create, self.listing, self.get, self.update, self.send = _build()

    def test_write_tools_are_system_serial(self) -> None:
        """
        `system_serial=True` 同时意味着「不进权限管线」。

        论证：这些工具不读写文件、不执行命令，副作用限于改本进程内存，
        没有可映射的 Bash / Read / Edit / Write 语义（与 run_agent、
        load_skill 同先例）。
        """
        for tool in (self.create, self.update, self.send):
            with self.subTest(tool=tool.name):
                self.assertFalse(tool.read_only)
                self.assertTrue(tool.system_serial)

    def test_read_tools_are_read_only_and_concurrent(self) -> None:
        """
        只读工具走并发桶，因此**不能**声明 `system_serial`，
        也不需要 `plan_safe`（只读工具在规划阶段本来就可用）。
        """
        for tool in (self.listing, self.get):
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.read_only)
                self.assertFalse(tool.system_serial)
                self.assertFalse(tool.plan_safe)

    def test_write_tools_declare_plan_safe(self) -> None:
        """拆任务、发消息在规划阶段仍要可用（spec F24）。"""
        for tool in (self.create, self.update, self.send):
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.plan_safe)

    def test_plan_safe_tools_accept_plan_stage_kwarg(self) -> None:
        """
        ⚠ `Tool.plan_safe` 的契约：声明它的工具**必须接受** `plan_stage`
        关键字参数（循环一定会传）。不接受会在运行期抛 TypeError，
        而那会被循环转成一条「工具执行异常」。
        """
        self.assertTrue(self.create.execute({"subject": "x", "description": "y"}, plan_stage=True).ok)
        self.assertTrue(self.update.execute({"task_id": "1", "status": "completed"}, plan_stage=True).ok)

    def test_no_tool_is_workspace_aware(self) -> None:
        """协作工具不碰路径、不起子进程，因此不需要工作目录。"""
        for tool in (self.create, self.listing, self.get, self.update, self.send):
            with self.subTest(tool=tool.name):
                self.assertFalse(tool.workspace_aware)

    def test_schemas_are_wellformed(self) -> None:
        for tool in (self.create, self.listing, self.get, self.update, self.send):
            with self.subTest(tool=tool.name):
                schema = tool.to_schema()
                self.assertEqual(schema["function"]["name"], tool.name)
                self.assertTrue(schema["function"]["description"])
                self.assertEqual(schema["function"]["parameters"]["type"], "object")


class BoardToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.create, self.listing, self.get, self.update, self.send = _build()
        bind_identity("worker-a")

    def test_create_reports_the_new_id(self) -> None:
        result = self.create.execute({"subject": "设计数据表", "description": "建表"})
        self.assertTrue(result.ok)
        self.assertIn("1", result.output)

    def test_create_requires_subject(self) -> None:
        self.assertFalse(self.create.execute({"subject": "  ", "description": "x"}).ok)

    def test_list_shows_all_tasks(self) -> None:
        self.create.execute({"subject": "甲", "description": ""})
        self.create.execute({"subject": "乙", "description": ""})
        output = self.listing.execute({}).output
        self.assertIn("甲", output)
        self.assertIn("乙", output)

    def test_list_on_empty_board_is_explicit(self) -> None:
        self.assertIn("空", self.listing.execute({}).output)

    def test_get_shows_full_detail(self) -> None:
        self.create.execute({"subject": "甲", "description": "详细说明"})
        output = self.get.execute({"task_id": "1"}).output
        self.assertIn("详细说明", output)
        self.assertIn("无人认领", output)

    def test_get_missing_lists_existing(self) -> None:
        self.create.execute({"subject": "甲", "description": ""})
        result = self.get.execute({"task_id": "99"})
        self.assertFalse(result.ok)
        self.assertIn("1", result.output)

    def test_claim_uses_the_atomic_path(self) -> None:
        """
        ⚠ 认领必须走 `board.claim`，不能走 `update(owner=...)`。

        后者不做「当前无人认领」的检查，两个队员会双双认领成功、
        各自以为任务归自己。这条用例从工具层验证走的是原子路径。
        """
        self.create.execute({"subject": "抢它", "description": ""})
        with identity("worker-a"):
            self.assertTrue(self.update.execute({"task_id": "1", "owner": "me"}).ok)
        with identity("worker-b"):
            result = self.update.execute({"task_id": "1", "owner": "me"})
        self.assertFalse(result.ok)
        self.assertIn("worker-a", result.output)

    def test_claim_blocked_task_is_refused_with_blockers(self) -> None:
        """spec F7 / AC8：被挡住的任务不能被认领，且要说清被谁挡着。"""
        self.create.execute({"subject": "前置", "description": ""})
        self.create.execute({"subject": "后续", "description": ""})
        self.update.execute({"task_id": "2", "add_blocked_by": ["1"]})
        result = self.update.execute({"task_id": "2", "owner": "me"})
        self.assertFalse(result.ok)
        self.assertIn("1", result.output)

    def test_completing_reports_newly_claimable(self) -> None:
        """
        做完一条就告诉它「现在可以认领哪几条」——省掉一轮 `task_list`，
        也少一次「它忘了看清单」的机会。
        """
        self.create.execute({"subject": "前置", "description": ""})
        self.create.execute({"subject": "后续", "description": ""})
        self.update.execute({"task_id": "2", "add_blocked_by": ["1"]})
        result = self.update.execute({"task_id": "1", "status": "completed"})
        self.assertIn("现在可以认领：2", result.output)

    def test_me_resolves_to_the_caller(self) -> None:
        """
        `me` 是一条刻意的容错：认领的常态就是「我来做」，而一个子 Agent
        未必总记得住自己叫什么（名字在几十轮之前的系统提示里）。
        """
        self.create.execute({"subject": "甲", "description": ""})
        with identity("worker-b"):
            self.update.execute({"task_id": "1", "owner": "me"})
        self.assertEqual(self.service.board.get("1").owner, "worker-b")

    def test_explicit_owner_is_passed_through(self) -> None:
        """代别人认领是允许的（对齐 Claude Code：owner 是任意 agent name）。"""
        self.create.execute({"subject": "甲", "description": ""})
        self.update.execute({"task_id": "1", "owner": "worker-b"})
        self.assertEqual(self.service.board.get("1").owner, "worker-b")

    def test_empty_owner_releases_the_task(self) -> None:
        self.create.execute({"subject": "甲", "description": ""})
        self.update.execute({"task_id": "1", "owner": "me"})
        self.update.execute({"task_id": "1", "owner": ""})
        self.assertEqual(self.service.board.get("1").owner, "")

    def test_deleted_status_removes_the_task(self) -> None:
        """`deleted` 是一次删除，不是一种状态（对齐 Claude Code）。"""
        self.create.execute({"subject": "甲", "description": ""})
        self.assertTrue(self.update.execute({"task_id": "1", "status": "deleted"}).ok)
        self.assertIsNone(self.service.board.get("1"))

    def test_invalid_status_lists_valid_ones(self) -> None:
        self.create.execute({"subject": "甲", "description": ""})
        result = self.update.execute({"task_id": "1", "status": "不存在的状态"})
        self.assertFalse(result.ok)
        self.assertIn("completed", result.output)

    def test_cyclic_dependency_is_refused(self) -> None:
        self.create.execute({"subject": "甲", "description": ""})
        self.create.execute({"subject": "乙", "description": ""})
        self.update.execute({"task_id": "2", "add_blocked_by": ["1"]})
        result = self.update.execute({"task_id": "1", "add_blocked_by": ["2"]})
        self.assertFalse(result.ok)
        self.assertIn("循环", result.output)

    def test_dependency_is_applied_before_claim(self) -> None:
        """
        ⚠ 一次调用里同时声明依赖与认领时，**先加依赖再认领**。

        反过来的话会先认领成功、再加上一条挡住自己的依赖，
        得到一个「已认领但被挡着」的怪状态。
        """
        self.create.execute({"subject": "前置", "description": ""})
        self.create.execute({"subject": "后续", "description": ""})
        result = self.update.execute(
            {"task_id": "2", "add_blocked_by": ["1"], "owner": "me"}
        )
        self.assertFalse(result.ok, "被自己刚声明的依赖挡住，认领应当失败")
        self.assertEqual(self.service.board.get("2").owner, "")

    def test_tools_never_raise(self) -> None:
        """`Tool` 契约：execute 不得向上抛异常。"""
        for tool in (self.create, self.listing, self.get, self.update):
            with self.subTest(tool=tool.name):
                result = tool.execute({"task_id": None, "subject": None})
                self.assertIsNotNone(result)


class SendMessageToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, *_rest, self.send = _build()
        bind_identity("worker-a")

    def test_message_reaches_recipient(self) -> None:
        self.assertTrue(self.send.execute({"to": "worker-b", "message": "喂"}).ok)
        self.assertTrue(self.service.has_unread("worker-b"))

    def test_sender_is_the_current_identity(self) -> None:
        """
        ⚠ 工具实例全进程共享，发件人只能从线程本地身份取。
        漏绑的表现是「worker-a 发的消息显示成 main 发的」，不报错。
        """
        with identity("worker-b"):
            self.send.execute({"to": "worker-a", "message": "喂"})
        received = self.service.take_unread("worker-a")
        self.assertEqual(received[0].sender, "worker-b")

    def test_unknown_recipient_lists_the_roster(self) -> None:
        result = self.send.execute({"to": "查无此人", "message": "喂"})
        self.assertFalse(result.ok)
        self.assertIn("worker-b", result.output)

    def test_success_tells_the_model_not_to_chase(self) -> None:
        """
        没有这句，模型拿到「已发送」之后最自然的下一步就是去确认对方收到没有
        ——而本章刻意不提供任何查收件箱的工具。
        """
        output = self.send.execute({"to": "worker-b", "message": "喂"}).output
        self.assertIn("不需要去确认", output)

    def test_plan_stage_blocks_writable_recipient(self) -> None:
        """
        spec F24：给一个能写文件的待命队员发消息会把它唤醒去动手，
        直接绕过 Plan Mode「批准前不动手」的承诺。
        """
        result = self.send.execute({"to": "worker-b", "message": "去改文件"}, plan_stage=True)
        self.assertFalse(result.ok)
        self.assertIn("规划阶段", result.output)

    def test_plan_stage_allows_main_and_read_only(self) -> None:
        self.assertTrue(self.send.execute({"to": MAIN_NAME, "message": "x"}, plan_stage=True).ok)
        with identity("worker-b"):
            self.assertTrue(
                self.send.execute({"to": "worker-a", "message": "x"}, plan_stage=True).ok
            )

    def test_plan_stage_refusal_delivers_nothing(self) -> None:
        """不通过时一个字节都不该进对方的信箱。"""
        self.send.execute({"to": "worker-b", "message": "去改文件"}, plan_stage=True)
        self.assertFalse(self.service.has_unread("worker-b"))

    def test_never_raises(self) -> None:
        self.assertIsNotNone(self.send.execute({"to": None, "message": None}))


class IdentityTest(unittest.TestCase):
    def test_default_identity_is_main(self) -> None:
        """未绑定时按主对话处理——测试与非工具模式下唯一合理的解释。"""
        result: list[str] = []
        thread = threading.Thread(target=lambda: result.append(current_identity()))
        thread.start()
        thread.join()
        self.assertEqual(result[0], MAIN_NAME)

    def test_identity_is_thread_local(self) -> None:
        """
        每条 Agent Loop 跑在自己的线程里，身份必须互不干扰——
        否则并发的两个队员会互相冒名。
        """
        bind_identity("main-thread")
        seen: list[str] = []

        def worker() -> None:
            bind_identity("worker-thread")
            seen.append(current_identity())

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(seen, ["worker-thread"])
        self.assertEqual(current_identity(), "main-thread")

    def test_identity_context_manager_restores(self) -> None:
        bind_identity("outer")
        with identity("inner"):
            self.assertEqual(current_identity(), "inner")
        self.assertEqual(current_identity(), "outer")


class SystemSerialPermissionTest(unittest.TestCase):
    """
    `system_serial=True` 的工具**照常过一次权限引擎**（perm-system-serial-bypass）。

    ## 这个类的来历

    它原名 `DenyRuleIneffectiveTest`，钉的是一个**已知不生效**的事实：
    C15 验收期实测发现，`system_serial` 工具在 `agent/loop.py` 的决策预扫里
    直接拿到一个 ALLOW 并 `continue`，根本不调 `engine.decide`，于是
    `permissions.yaml` 里的 `deny: send_message` 一条都不生效。那是个
    **从 C13 起就存在**的错误（`run_agent` / `load_skill` 同样如此），
    而当时四处注释都写着「仍可被 deny 规则整个禁掉」——
    **错误的安全承诺比没有承诺更危险**。

    现在缺陷已修，断言随之反过来。`system_serial` 现在精确地只意味着两件事：
    **强制串行** + **判 ASK 时按 ALLOW 处理（不弹面板）**。

    ## 四条判据缺一不可

    1. `deny` 规则**拦得住**（本次修复的全部内容）；
    2. 但**仍然不弹确认面板**——那是 `system_serial` 存在的理由之一，
       丢了它这次修复就从「让 deny 生效」变成「给七个工具全加人在回路」；
    3. Hook 的 `pre_tool_use` 依然拦得住（不因这次改动而失效）；
    4. 缺省档下**没有 deny 规则时照常执行**（反证：别把 ASK 也一起拒了）。
    """

    def _run_once(self, rules, hooks=None, mode=None):
        """
        跑一轮真实 Agent Loop。

        :returns: (工具是否成功, 消息是否送达, 确认面板弹了几次)
        """
        import threading

        from rhinecode.agent.loop import Agent, RunOptions
        from rhinecode.permission.engine import PermissionEngine
        from rhinecode.permission.models import PermissionMode
        from rhinecode.permission.rules import RuleSet
        from rhinecode.provider.base import StreamChunk, ToolCall
        from rhinecode.tools.registry import ToolRegistry

        service = TeamService()
        service.register_member("beta", "worker")
        registry = ToolRegistry()
        registry.register(SendMessageTool(service))

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
                            name="send_message",
                            arguments={"to": "beta", "message": "喂", "summary": "喂"},
                        ),
                    )
                else:
                    yield StreamChunk(type="text", content="完")

        ok = []
        # 确认面板的计数器。**这是本类第二重要的判据**——`system_serial` 的
        # 「不弹面板」这条性质丢了，界面上表现为一次普通的发消息突然要人点确认。
        asked: list[str] = []

        def _ask(tc, tool, decision):  # noqa: ARG001
            asked.append(tc.name)
            return True

        for event in Agent(_Provider(), registry, hooks=hooks).run(
            [], "off", False, "", lambda: "", "m", None,
            PermissionEngine(
                RuleSet(rules=rules), mode=mode or PermissionMode.DEFAULT
            ),
            _ask, None, None, threading.Event(), None, None,
            options=RunOptions(),
        ):
            if event.tool_result is not None:
                ok.append(event.tool_result.ok)
        return (ok[0] if ok else None), service.has_unread("beta"), len(asked)

    def test_deny_rule_blocks_a_system_serial_tool(self) -> None:
        """
        **本次修复的全部内容**：`deny: send_message` 现在拦得住了。

        ⚠ 规则的 `pattern` 必须是**空串**。这些工具不在 `_TOOL_MAP` 里、走
        `other` 分支，而那个分支要求 `rule.pattern == ""`（只有「整工具规则」
        命中得了无 specifier 的请求）。配置里 `deny: send_message`
        不带括号解析出来的正是空模式。
        """
        from rhinecode.permission.rules import Rule

        executed, delivered, asked = self._run_once(
            [Rule(effect="deny", tool="send_message", pattern="", source="test")]
        )
        self.assertFalse(executed, "deny 规则必须拦得住 system_serial 工具")
        self.assertFalse(delivered, "消息不该送达")
        self.assertEqual(asked, 0, "被 deny 拦下时不该弹面板")

    def test_deny_with_a_pattern_does_not_match_other_kind(self) -> None:
        """
        既有语义的护栏（**不是**本次引入的）：带模式的写法不命中 `other` 类请求。

        `deny: send_message(*)` 对它们无效——`other` 分支要求空模式。
        写这条是为了让下一个人不必再实测一遍「为什么我的规则没生效」。
        """
        from rhinecode.permission.rules import Rule

        executed, delivered, _asked = self._run_once(
            [Rule(effect="deny", tool="send_message", pattern="*", source="test")]
        )
        self.assertTrue(executed, "带模式的规则不命中 other 类请求（既有语义）")
        self.assertTrue(delivered)

    def test_no_rule_still_executes_without_a_panel(self) -> None:
        """
        **反证，且是本类里最容易被改坏的一条。**

        缺省档下③层未命中 → ④模式层判 ASK。`system_serial` 工具必须把它
        **按 ALLOW 处理**：既要执行，又**一次面板都不能弹**。

        丢了这条性质，这次修复就从「让 deny 生效」变成「给七个工具全加上
        人在回路」——而它们可能开一整条子对话，在预扫处停下来等面板
        会把整条交互链拧成死结。
        """
        executed, delivered, asked = self._run_once([])
        self.assertTrue(executed, "没有 deny 规则时照常执行")
        self.assertTrue(delivered)
        self.assertEqual(asked, 0, "system_serial 工具不弹确认面板")

    def test_strict_mode_denies_it(self) -> None:
        """
        严格档的④模式层判 DENY，而 DENY **不**降级——只有 ASK 降级。

        没有这条的话，把降级写成「非 DENY 一律放行」与「一律放行」
        看不出区别。
        """
        from rhinecode.permission.models import PermissionMode

        executed, delivered, asked = self._run_once([], mode=PermissionMode.STRICT)
        self.assertFalse(executed, "严格档下④层判 DENY，必须拦住")
        self.assertFalse(delivered)
        self.assertEqual(asked, 0)

    def test_hook_ask_still_pops_a_panel(self) -> None:
        """
        ⚠ **Hook 的 ASK 与④模式层的 ASK 刻意区别对待**，这条钉住那个不对称。

        ④是灰色地带的兜底，降级为放行；Hook 的 ASK 是用户针对这件事写下的
        一条规则，那是明确的意愿表达——照常弹面板（与改造前逐字一致）。
        """
        from rhinecode.hooks import HookEventType
        from rhinecode.hooks.models import HookDecision

        class _Verdict:
            decision = HookDecision.ASK
            reason = "hook 要求确认"

        class _Result:
            verdict = _Verdict()

        class _Hooks:
            def has_listeners(self, event) -> bool:
                return event == HookEventType.PRE_TOOL_USE

            def dispatch(self, event, factory, cwd=None):  # noqa: ARG002
                return _Result()

            def consume_injections(self):
                return []

        executed, delivered, asked = self._run_once([], hooks=_Hooks())
        self.assertEqual(asked, 1, "Hook 判 ASK 时必须弹面板")
        self.assertTrue(executed, "面板上点了同意，照常执行")
        self.assertTrue(delivered)

    def test_hook_pre_tool_use_does_block_it(self) -> None:
        """
        Hook 的 `pre_tool_use` 排在预扫更前面，**不因这次改动而失效**。

        它与 `deny` 规则是两条独立的收窄手段：Hook 那条连 `engine.decide`
        都不调（有反证测试钉着顺序），deny 那条在引擎内部。
        """
        from rhinecode.hooks import HookEventType
        from rhinecode.hooks.models import HookDecision

        class _Verdict:
            decision = HookDecision.DENY
            reason = "hook 拦下"

        class _Result:
            verdict = _Verdict()

        class _Hooks:
            def has_listeners(self, event) -> bool:
                return event == HookEventType.PRE_TOOL_USE

            def dispatch(self, event, factory, cwd=None):  # noqa: ARG002
                return _Result()

            def consume_injections(self):
                return []

        executed, delivered, _asked = self._run_once([], hooks=_Hooks())
        self.assertFalse(executed, "Hook 应当拦下它")
        self.assertFalse(delivered, "消息不该送达")


class SameVoiceTest(unittest.TestCase):
    """
    ⚠ **成对维护点的护栏**：发消息工具的描述与注入消息的标记块必须同口径。

    模型在**两个不同时刻**读到同一条约定——发消息前读工具描述、
    收消息时读标记块。一处写得强、另一处写得弱等于白改，
    模型会按弱的那份行事。

    这是本项目**第四次**面对同一个坑（C11 Skill 清单表头 ↔ load_skill、
    C13 角色清单 ↔ run_agent、C14 交付信息 ↔ 委派工具描述）。
    前三次都是真实模型实测才发现的。

    断言的是**几层意思**而不是逐字固化——措辞可以打磨，这几层不能丢。
    """

    def setUp(self) -> None:
        self.desc = SendMessageTool.description
        service = TeamService()
        service.register_member("worker-a", "explorer")
        service.send("worker-a", MAIN_NAME, "正文", "摘要")
        self.block = render_incoming(service.take_unread(MAIN_NAME)).content

    def test_both_say_plain_output_is_invisible_to_others(self) -> None:
        """
        ①「你的正文对方看不到」——最关键的一条。少了它，模型会把要说的话
        写在自己的正文里，而对方永远收不到。
        """
        self.assertIn("看不到", self.desc)
        self.assertIn("发消息", self.block)

    def test_both_imply_no_inbox_checking(self) -> None:
        """②「消息自动送达，不必查收」——本章刻意不提供查收件箱的工具。"""
        self.assertIn("自动送达", self.desc)
        self.assertIn("不需要", self.desc)

    def test_description_says_names_survive_completion(self) -> None:
        """
        ③「名字在它干完之后依然有效」——直接对应「待命队员被消息唤醒」。

        少了这句，模型想让某个队员再做一件事时会**重新委派一个新的**，
        于是背景要重新交代一遍，待命机制形同虚设。
        """
        self.assertIn("依然有效", self.desc)
        self.assertIn("不要重新委派", self.desc)

    def test_block_says_it_is_not_the_user_speaking(self) -> None:
        """
        ④ 标记块必须声明「这不是用户在说话」——混淆会让队员把队友的话
        当成用户指令，绕过「用户才是最终授权方」这个前提。
        """
        self.assertIn("不是用户在说话", self.block)
        self.assertIn("不构成授权", self.block)

    def test_both_refer_to_peers_by_name(self) -> None:
        """⑤ 按名字指代 ↔ 标记块的 from 属性。"""
        self.assertIn("名字", self.desc)
        self.assertIn('from="', self.block)


if __name__ == "__main__":
    unittest.main()
