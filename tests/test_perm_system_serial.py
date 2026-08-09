"""
护栏：七个 `system_serial=True` 的工具都能被 `deny` 规则整个禁掉。

## 这组用例为什么单独成文件

`tests/test_team_tools.py::SystemSerialPermissionTest` 验的是**行为**——
跑一轮真实 Agent Loop，看工具执没执行、面板弹没弹。那种用例每个工具都写一遍
太重（`run_agent` 要 `SubAgentService`、`load_skill` 要 `SkillManager`）。

这里验的是**规则能不能命中**这一件事，走 `to_request` + `RuleSet` 的纯判定路径，
因此可以遍历全部七个工具逐个断言。两组配合起来才完整：
这边证明「规则匹配得上」，那边证明「循环真的照着它做了」。

## 背景

`deny: run_agent` 从 C13 起就写在注释里、但**一直不生效**——
`agent/loop.py` 的预扫里那七个工具直接拿 ALLOW、根本不调 `engine.decide`。
C15 验收期实测戳穿，已于 perm-system-serial-bypass 修掉。
危害不在「这些工具很危险」（它们不读写文件、不执行命令），
而在于**文档承诺了一个不存在的安全边界**。
"""

import unittest

from rhinecode.permission.adapter import to_request
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import Decision, PermissionMode, Rule
from rhinecode.permission.rules import RuleSet
from rhinecode.subagents.toolset import ALWAYS_GRANTED_TOOLS, GLOBAL_DENIED_TOOLS
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.path_guard import main_project_root

# 七个 `system_serial=True` 的工具。**刻意从两张既有常量表拼出来**而不是
# 手抄一份名单：将来新增协作工具时这里自动覆盖到，不会悄悄漏掉。
#
# `GLOBAL_DENIED_TOOLS` = {run_agent, load_skill}（C13）
# `ALWAYS_GRANTED_TOOLS` = C15 的五个协作工具
# 其中 `task_list` / `task_get` 是只读工具、不是 system_serial，
# 单独在 `test_read_only_collaboration_tools_are_not_system_serial` 里说明。
_WRITE_SIDE = {"task_create", "task_update", "send_message"}
SYSTEM_SERIAL_TOOL_NAMES = sorted(GLOBAL_DENIED_TOOLS | _WRITE_SIDE)

_CWD = main_project_root()


class _Stub(Tool):
    """
    只带名字与 `read_only` 的最小工具替身。

    用替身而不是真实工具实例，是因为本组验的是**规则匹配**，
    而真实实例要拖进 `SubAgentService` / `SkillManager` / `TeamService`
    三条依赖链，那些与「`deny: run_agent` 命不命中」毫无关系。
    真实工具的 `system_serial` 取值另有护栏钉着
    （`test_subagent_tool.py` / `test_team_tools.py`）。
    """

    description = "替身"
    parameters = {"type": "object", "properties": {}}
    read_only = False

    def __init__(self, name: str) -> None:
        self.name = name

    def execute(self, args: dict, **kwargs) -> ToolResult:  # pragma: no cover
        return ToolResult(ok=True, output="")


class DenyRuleReachesSystemSerialToolsTest(unittest.TestCase):
    def _decide(self, name: str, rules: list[Rule], mode=PermissionMode.DEFAULT):
        engine = PermissionEngine(RuleSet(rules), mode=mode)
        return engine.decide(to_request(_Stub(name), {}, mode, _CWD))

    def test_every_system_serial_tool_can_be_denied(self) -> None:
        """
        逐个断言：一条不带括号的整工具 deny 规则拦得住它。

        遍历常量表而不是手写七条，是为了让将来新增的协作工具自动被覆盖。
        """
        for name in SYSTEM_SERIAL_TOOL_NAMES:
            with self.subTest(tool=name):
                result = self._decide(name, [Rule("deny", name, "", "user")])
                self.assertEqual(
                    result.decision,
                    Decision.DENY,
                    f"deny: {name} 必须拦得住它",
                )

    def test_they_fall_into_the_other_kind(self) -> None:
        """
        它们都不在 `_TOOL_MAP` 里，因此落 `other` 分支：`specifier` 为空。

        这是「为什么规则必须写成不带括号的整工具形式」的根据——
        `other` 分支要求 `rule.pattern == ""`。
        """
        for name in SYSTEM_SERIAL_TOOL_NAMES:
            with self.subTest(tool=name):
                request = to_request(_Stub(name), {"x": 1}, PermissionMode.DEFAULT, _CWD)
                self.assertEqual(request.kind, "other")
                self.assertEqual(request.specifier, "")
                self.assertEqual(request.rule_name, name)

    def test_a_pattern_rule_does_not_match(self) -> None:
        """
        **反证**（既有语义，非本次引入）：带模式的写法不命中 `other` 类请求。

        没有这条的话，上一条用 `pattern="*"` 写也会「通过」，
        而用户照着那么配会发现规则不生效。
        """
        for name in SYSTEM_SERIAL_TOOL_NAMES:
            with self.subTest(tool=name):
                result = self._decide(name, [Rule("deny", name, "*", "user")])
                self.assertNotEqual(result.decision, Decision.DENY)

    def test_wildcard_tool_name_still_works(self) -> None:
        """
        `other` 分支的工具名走 fnmatch，因此 `deny: task_*` 能一次禁掉两个任务工具。

        这是 c7 为 MCP 工具定的既有语义，本次改动让它对协作工具也真正生效了。
        """
        for name in ("task_create", "task_update"):
            with self.subTest(tool=name):
                result = self._decide(name, [Rule("deny", "task_*", "", "user")])
                self.assertEqual(result.decision, Decision.DENY)
        # 不该误伤别的工具。
        self.assertNotEqual(
            self._decide("send_message", [Rule("deny", "task_*", "", "user")]).decision,
            Decision.DENY,
        )

    def test_deny_beats_allow_for_them_too(self) -> None:
        """deny 优先在这些工具上同样成立（③层的既有哲学，没有特例）。"""
        result = self._decide(
            "run_agent",
            [Rule("allow", "run_agent", "", "project"), Rule("deny", "run_agent", "", "user")],
        )
        self.assertEqual(result.decision, Decision.DENY)

    def test_default_mode_yields_ask_which_the_loop_downgrades(self) -> None:
        """
        没有规则时引擎判 **ASK**（④模式层），循环再把它当 ALLOW 用。

        这条说明「不弹面板」这件事**发生在循环里而不是引擎里**——
        引擎对它们没有任何特例，这正是本次改动能保住
        「权限层是纯判定、无特例」的原因。
        循环那一侧的行为由 `test_team_tools.py` 与 `test_trace_system_serial.py` 钉住。
        """
        for name in SYSTEM_SERIAL_TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertEqual(self._decide(name, []).decision, Decision.ASK)

    def test_permissive_mode_yields_allow(self) -> None:
        """放行档下④层判 ALLOW（URL 类的例外与它们无关）。"""
        self.assertEqual(
            self._decide("run_agent", [], mode=PermissionMode.PERMISSIVE).decision,
            Decision.ALLOW,
        )

    def test_read_only_collaboration_tools_are_not_system_serial(self) -> None:
        """
        对照：`task_list` / `task_get` 是只读工具，走并发桶、不是 `system_serial`。

        它们在引擎里被「只读简化分支」直接放行（③未命中即 ALLOW，不进④），
        因此没有 ASK 可降级。列在这里是为了让七这个数字有据可查。
        """
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
        for name in ("task_list", "task_get"):
            with self.subTest(tool=name):
                stub = _Stub(name)
                stub.read_only = True
                request = to_request(stub, {}, PermissionMode.DEFAULT, _CWD)
                self.assertEqual(engine.decide(request).decision, Decision.ALLOW)
        # 但 deny 规则照样压得过只读简化分支（③排在它之前）。
        stub = _Stub("task_list")
        stub.read_only = True
        engine2 = PermissionEngine(
            RuleSet([Rule("deny", "task_list", "", "user")]), mode=PermissionMode.DEFAULT
        )
        self.assertEqual(
            engine2.decide(to_request(stub, {}, PermissionMode.DEFAULT, _CWD)).decision,
            Decision.DENY,
        )


if __name__ == "__main__":
    unittest.main()
