"""
「启动外部程序」类（`kind == "launch"`）的权限判定护栏——审查报告 B4 / S1 的修复。

## 这一组用例钉的是什么

`mcp_add_server` 会往 `mcp.yaml` 写一条 `mcpServers` 配置并**立刻把它拉起来**，
而 `command` 字段是**任意本地命令**。修复之前它落在 adapter 的 `other` 兜底分支上，
实测缺省预设（`auto` = 放行档）下的判定是 `allow @ mode`——**六层防御一层都不生效**：

| 层 | 为什么碰不到它 |
| --- | --- |
| ①危险命令黑名单 | 只对 `kind == "command"` 生效 |
| ②路径沙箱 | 只对 `read_path` / `write_path` / `glob` 生效 |
| ②′网络边界 | 只对 `kind == "url"` 生效 |
| ②″保护路径 | 第一行就是 `if request.kind != "write_path": return result` |
| ③可配置规则 | 只有用户**主动写下** `deny: mcp_add_server` 才拦得住 |
| ④权限档兜底 | 缺省预设 `auto` → `PERMISSIVE` → ALLOW |

修法是把 `mcp_add_server` 映射成一种新的 `kind`（`launch`），并在第④层放行档下
对它判 ASK——与 `url`（web_fetch F7）、`search`（web_search F12）两个既有例外同格。
**这不是新增约束，是把一条从 C7 就写在 `tools/mcp_config.py` docstring 里的老承诺
还给它原来的落点**（那时缺省档是 `DEFAULT`，④层判 ASK、面板照弹）。

## 违反会发生什么

把 `_TOOL_MAP` 里那行映射删掉、或把④层那一支去掉，**任何地方都不会报错**：
工具照常执行、界面照常显示、全量测试（这个文件之外）照常绿。唯一的差别是
缺省配置下模型可以**不弹任何面板**地拉起一个任意本地进程，而那个进程
（在环境变量过滤之前）还继承着完整的 `os.environ`。

## 为什么这几条不能简化

- **`test_permissive_is_not_a_blanket_allow` 断的是「不是 ALLOW」而不是「是 ASK」**
  ——留给将来改判 DENY 的余地；同时另有一条精确断言 ASK + MODE，
  两条一起才既钉住性质又钉住当前形态。
- **对照组（`write_file`）不可省**：只断言 launch 判 ASK 的话，一个「放行档整个
  失效」的实现也会绿，而那会让缺省体验退回每次弹面板。
- **`deny` / `allow` 两条规则形状的用例不可省**：`launch` 落规则匹配的「其它类」
  分支，只认**不带括号**的整工具规则。带括号的写法一律不命中且**没有任何警告**
  ——与 `deny: WebSearch(*)` 完全同形，那是已知项里明知而接受的取舍，
  写在这里是为了让下一个人一眼看到，而不是去猜。
- **`test_allow_rule_uses_the_empty_pattern` 不可省**：确认面板的「本会话 / 永久
  放行」经 `to_allow_rule` 登记规则，返回非空模式会写出一条**永远不会命中**的
  废规则——用户点完下次还弹，而问题要到下次才显形。

判定那几组用例**全程零 I/O**（判定层是纯逻辑，spec N4）；末尾一组要真跑一个
Textual app，因为它验的是「用户在面板上到底看得到什么」——那件事只有渲染出来才算数。
"""

import unittest
from types import SimpleNamespace

from rhinecode.permission.adapter import _TOOL_MAP, to_allow_rule, to_request
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import (
    Decision,
    Layer,
    PermissionMode,
    PermissionRequest,
    Rule,
)
from rhinecode.permission.rules import RuleSet
from rhinecode.presets import DEFAULT_PRESET, PRESET_AXES
from rhinecode.tools.mcp_config import MCPAddServerTool
from rhinecode.tools.path_guard import main_project_root

_CWD = main_project_root()

# 一次「拉起任意本地命令」的真实形态：`scope: user` 还会写到工作区之外。
_ADD_ARGS = {
    "scope": "user",
    "server_name": "helper",
    "config": {"command": "python", "args": ["-c", "import os;print(os.environ)"]},
}


def _tool() -> MCPAddServerTool:
    """
    造一个不带运行时依赖的工具实例。

    `MCPAddServerTool.__init__` 要注入 MCPManager 与 ToolRegistry，而本文件
    只用到它的 `name` / `read_only` 两个**类属性**——`__new__` 绕开构造即可，
    也顺带保证这一组用例不会意外碰到 MCP 运行时。
    """
    return MCPAddServerTool.__new__(MCPAddServerTool)


def _request(mode: PermissionMode, args: dict = None) -> PermissionRequest:
    """按 adapter 的**真实**映射构造一次请求（不手写 kind，免得与产品口径分叉）。"""
    return to_request(_tool(), _ADD_ARGS if args is None else args, mode, _CWD)


def _engine(mode: PermissionMode, rules: list = None) -> PermissionEngine:
    """造一个只带文件规则集的引擎。"""
    return PermissionEngine(RuleSet(list(rules or [])), mode=mode)


def _write_request(mode: PermissionMode) -> PermissionRequest:
    """一次普通写文件请求，用作「④层例外只作用于 launch 类」的对照组。"""
    return PermissionRequest(
        tool_name="write_file",
        rule_name="Write",
        specifier="notes.md",
        kind="write_path",
        is_read_only=False,
        mode=mode,
        cwd=_CWD,
    )


class MappingTest(unittest.TestCase):
    """adapter 必须把 `mcp_add_server` 认成 `launch` 类。"""

    def test_tool_is_mapped_at_all(self):
        """
        它必须在 `_TOOL_MAP` 里有一行。

        ⚠ 落回 `other` 兜底分支正是 B4 的成因——而那个分支**不报错**，
        它只是安静地把这次调用交给④层的「无规则命中，默认允许」。
        """
        self.assertIn("mcp_add_server", _TOOL_MAP)

    def test_kind_is_launch(self):
        """kind 必须是 `launch`：④层那一支、面板的补充展示行都按它分流。"""
        request = _request(PermissionMode.PERMISSIVE)
        self.assertEqual(request.kind, "launch")

    def test_specifier_records_what_will_be_started(self):
        """
        specifier 要能回答「那次到底启动了什么」。

        它不参与任何规则匹配（`launch` 落「其它类」分支），唯一的消费方是
        行为记录与原因展示——留空串的话事后排查只剩一个工具名。
        """
        request = _request(PermissionMode.PERMISSIVE)
        self.assertIn("helper", request.specifier)
        self.assertIn("python", request.specifier)
        # **完整参数在内、不截断**：被截断意味着只要把危险部分放在可见范围之后，
        # 记录与面板就都看不到它。
        self.assertIn("import os;print(os.environ)", request.specifier)

    def test_specifier_survives_garbage_arguments(self):
        """
        参数是模型产出的任意 JSON，映射函数**绝不能抛异常**。

        它跑在权限判定的入口上：抛出去会让整轮工具执行炸掉，而这里只是在拼
        一句给人看的话。三种畸形形态各来一次。
        """
        for args in ({}, {"config": "not-a-dict"}, {"server_name": None, "config": {"args": 3}}):
            with self.subTest(args=args):
                request = _request(PermissionMode.PERMISSIVE, args)
                self.assertEqual(request.kind, "launch")
                self.assertIsInstance(request.specifier, str)


class ModeLayerTest(unittest.TestCase):
    """第④层三档模式：放行档也判 ASK（例外），且例外只作用于本类。"""

    def test_permissive_is_not_a_blanket_allow(self):
        """
        ⚠ **本文件的核心断言。** 缺省预设下它绝不能是无条件放行。

        断「不是 ALLOW」而不是「是 ASK」：将来若判定改严成 DENY，
        这条仍然成立——它钉的是性质，不是当下的形态。
        """
        result = _engine(PermissionMode.PERMISSIVE).decide(_request(PermissionMode.PERMISSIVE))
        self.assertIsNot(result.decision, Decision.ALLOW)

    def test_permissive_asks_at_the_mode_layer(self):
        """当下的精确形态：ASK，且来自第④层。"""
        result = _engine(PermissionMode.PERMISSIVE).decide(_request(PermissionMode.PERMISSIVE))
        self.assertIs(result.decision, Decision.ASK)
        self.assertIs(result.layer, Layer.MODE)

    def test_reason_says_it_starts_an_external_process(self):
        """
        理由必须说破「会启动外部程序」。

        面板对④层的结论**不显示 reason**（那句话对绝大多数确认恒为「无规则命中」），
        所以这句话的读者是行为记录与模型；它要能把「为什么这一类不吃放行档」
        说清楚，否则下一个人只会以为是放行档坏了。
        """
        result = _engine(PermissionMode.PERMISSIVE).decide(_request(PermissionMode.PERMISSIVE))
        self.assertIn("外部", result.reason)

    def test_default_mode_still_asks(self):
        """默认档下与修复前逐字一致（那本来就是承诺兑现的地方）。"""
        result = _engine(PermissionMode.DEFAULT).decide(_request(PermissionMode.DEFAULT))
        self.assertIs(result.decision, Decision.ASK)

    def test_strict_mode_still_denies(self):
        """严格档不受影响：新增的例外排在 STRICT 判定之后，不可能把 DENY 放宽。"""
        result = _engine(PermissionMode.STRICT).decide(_request(PermissionMode.STRICT))
        self.assertIs(result.decision, Decision.DENY)

    def test_other_tools_keep_the_permissive_behavior(self):
        """
        **反证：例外只作用于 launch 类。**

        少了它，一个「把放行档整个改成 ASK」的实现也会让上面几条全绿——
        而那会让缺省体验退回「每次写文件都弹面板」，与 auto 预设的全部意义相反。
        """
        result = _engine(PermissionMode.PERMISSIVE).decide(_write_request(PermissionMode.PERMISSIVE))
        self.assertIs(result.decision, Decision.ALLOW)
        self.assertIs(result.layer, Layer.MODE)


class DefaultPresetTest(unittest.TestCase):
    """把「缺省预设」这条链一起钉住，而不是只钉一个手写的 PERMISSIVE。"""

    def test_the_shipped_default_preset_does_not_allow_it(self):
        """
        ⚠ 取值**从 `presets.py` 现算**，不硬编码 `PERMISSIVE`。

        B4 的成因不是某一层写错了，是**缺省档被换掉之后没人回来看这条承诺**。
        这条用例把「缺省预设是什么」与「它对本工具的判定」绑在一起：
        将来再换缺省档，这里会当场告诉你这条承诺还成不成立。
        """
        mode, _plan_stage = PRESET_AXES[DEFAULT_PRESET]
        result = _engine(mode).decide(_request(mode))
        self.assertIsNot(result.decision, Decision.ALLOW)


class RuleShapeTest(unittest.TestCase):
    """③层规则形状：只认不带括号的整工具规则（`other` 分支的既有语义）。"""

    def test_plain_deny_rule_blocks_it(self):
        """`deny: mcp_add_server`（不带括号）拦得住，且结论来自③层。"""
        rules = [Rule(effect="deny", tool="mcp_add_server", pattern="", source="project")]
        result = _engine(PermissionMode.PERMISSIVE, rules).decide(_request(PermissionMode.PERMISSIVE))
        self.assertIs(result.decision, Decision.DENY)
        self.assertIs(result.layer, Layer.RULE)

    def test_parenthesised_deny_rule_does_not_match(self):
        """
        ⚠ **带括号的写法一律不命中，且没有任何警告。**

        这不是本次引入的，是 `other` 分支从 c7 起的语义（`deny: WebSearch(*)`
        完全同形，已登记为已知边界）。钉在这里是为了让下一个人一眼看到，
        而不是配了一条 `deny: mcp_add_server(*)` 之后以为自己已经关掉了它。
        """
        rules = [Rule(effect="deny", tool="mcp_add_server", pattern="*", source="project")]
        result = _engine(PermissionMode.PERMISSIVE, rules).decide(_request(PermissionMode.PERMISSIVE))
        self.assertIsNot(result.decision, Decision.DENY)

    def test_plain_allow_rule_short_circuits_the_mode_layer(self):
        """
        **用户写下的 allow 规则短路④层**——这正是「放在④层而不是做成出口收紧器」
        的全部理由：那个按钮必须是诚实的。

        安全性由另一件事保证：模型改不了 `permissions.yaml`（②″保护路径），
        所以这条规则只可能出自人手。
        """
        rules = [Rule(effect="allow", tool="mcp_add_server", pattern="", source="local")]
        result = _engine(PermissionMode.PERMISSIVE, rules).decide(_request(PermissionMode.PERMISSIVE))
        self.assertIs(result.decision, Decision.ALLOW)
        self.assertIs(result.layer, Layer.RULE)


class AllowRuleTest(unittest.TestCase):
    """`to_allow_rule`：面板上的「本会话 / 永久放行」必须写出一条真的会命中的规则。"""

    def test_allow_rule_uses_the_empty_pattern(self):
        """
        必须返回空模式（整工具形式）。

        返回 `("mcp_add_server", "helper · python …")` 会写出一条**永远不会命中**
        的规则——`launch` 落「其它类」分支，那个分支只认 `rule.pattern == ""`。
        当场看不出来：本次调用因为选了「放行」照常执行了，问题要到下次才显形。
        """
        rule_name, pattern = to_allow_rule(_request(PermissionMode.PERMISSIVE))
        self.assertEqual(rule_name, "mcp_add_server")
        self.assertEqual(pattern, "")

    def test_the_generated_rule_really_matches_next_time(self):
        """
        **端到端反证**：把 `to_allow_rule` 的产出真的登记成规则，再判一次。

        只断言「返回空模式」的话，一个把匹配分支也改坏的实现照样绿。
        """
        rule_name, pattern = to_allow_rule(_request(PermissionMode.PERMISSIVE))
        rules = [Rule(effect="allow", tool=rule_name, pattern=pattern, source="session")]
        result = _engine(PermissionMode.PERMISSIVE, rules).decide(_request(PermissionMode.PERMISSIVE))
        self.assertIs(result.decision, Decision.ALLOW)


class ConfirmPanelTest(unittest.IsolatedAsyncioTestCase):
    """
    面板上必须看得到**将要执行的完整命令**——否则这次确认是装饰品。

    形态照抄 `test_web_search_bootstrap.py::ConfirmPanelTests`（真跑一个 Textual app
    把面板挂出来，断言实际渲染出的文本），那一组验的是同一件事的另一半。
    """

    async def _show(self):
        """挂出面板并返回它（含一条参数很长的 `mcp_add_server` 调用）。"""
        from textual.app import App, ComposeResult

        from rhinecode.provider.base import ToolCall
        from rhinecode.tui.widgets import ConfirmPanel

        class _Harness(App):
            def compose(self) -> ComposeResult:
                yield ConfirmPanel()

        app = _Harness()
        async with app.run_test():
            panel = app.query_one(ConfirmPanel)
            panel.show_for(
                ToolCall(id="c1", name="mcp_add_server", arguments=dict(_ADD_ARGS)),
                None,
                SimpleNamespace(
                    reason="交由用户确认",
                    kind="launch",
                    host="",
                    layer=SimpleNamespace(value="mode"),
                ),
            )
            return panel

    def _texts(self, panel) -> str:
        """面板上的全部文本（选项 prompt 逐个取纯文本再拼起来）。"""
        chunks = []
        for option in panel._options:
            prompt = option.prompt
            chunks.append(prompt if isinstance(prompt, str) else str(prompt))
        return "\n".join(chunks)

    async def test_the_full_command_is_visible(self):
        """
        ⚠ **完整命令必须原样出现在面板上，一个字都不能少。**

        表头走 `summarize_args`，**每个参数值只留 30 个字符**——
        `config={'command': 'python', 'args': […` 会在 `command` 刚露头的地方断掉，
        而用户要放行的正是「跑这条命令」。被截断意味着只要把危险部分放在可见
        范围之后，人在回路这一层就形同虚设（与 url / search 两类同一条理由）。
        """
        panel = await self._show()
        text = self._texts(panel)
        self.assertIn("python -c import os;print(os.environ)", text)

    async def test_the_headline_alone_really_does_truncate(self):
        """
        **反证：补充展示行不是多余的。**

        断言表头那一行**确实**把参数截断了（出现省略号）。少了这条，
        「把补充展示行删掉、反正表头也显示参数」看起来毫无代价。
        """
        panel = await self._show()
        headline = self._texts(panel).splitlines()[0]
        self.assertIn("…", headline)
        self.assertNotIn("print(os.environ)", headline)

    async def test_scope_user_is_called_out_as_outside_the_workspace(self):
        """
        `scope: user` 写到工作区之外，面板必须说破。

        用户对「添加一个 MCP」的心智是「往这个项目里加点东西」，
        而它可能写的是主目录下那份、对**以后每个项目**都生效。
        """
        panel = await self._show()
        self.assertIn("在工作区之外", self._texts(panel))

    async def test_four_choices_including_permanent(self):
        """
        **`launch` 类照常给四个选项，别顺手把它归进 `no_permanent`。**

        它与搜索类都是「④层放行档下的例外」，看起来该归一档；但搜索类砍掉
        「永久放行」的推导链走不通到这里——F16 的宽泛规则丢弃只处理 `Bash` 与
        `WebSearch` 两类，`allow: mcp_add_server` 下次启动照常在③层命中并短路④层。
        砍掉它是拿走一个真的有用的选项。
        """
        panel = await self._show()
        ids = [option.id for option in panel._options if option.id is not None]
        self.assertEqual(ids, ["yes", "yes_session", "yes_permanent", "no"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
