"""
「MCP 远端工具」类（`kind == "remote"`）的权限判定护栏——审查报告 S2 的那一半。

## 这一组用例钉的是什么

项目对 MCP 的定性从 C7 起就是「远端 Server 是**外部程序、不可信**」，而当时
**唯一写明的缓解手段**就是 spec F11 那句「一律 read_only=False——默认每次调用
都经人在回路确认」。

那句承诺后来失去了兑现它的那一层，且过程中没有任何东西报错：MCP 工具落在
adapter 的 `other` 兜底分支上，auto-plan 扩展把缺省档从 `DEFAULT` 换成
`PERMISSIVE` 之后，第④层对 `other` 类从判 ASK 变成判 ALLOW。实测
（审查报告 S2）：`mcp__everything__printEnv → allow @ mode`，**零提示、零面板**。

逐层核验它为什么一层都碰不到（与 `launch` 那一组同构）：

| 层 | 为什么碰不到它 |
| --- | --- |
| ①危险命令黑名单 | 只对 `kind == "command"` 生效 |
| ②路径沙箱 | 只对 `read_path` / `write_path` / `glob` 生效 |
| ②′网络边界 | 只对 `kind == "url"` 生效 |
| ②″保护路径 | 第一行就是 `if request.kind != "write_path": return result` |
| ③可配置规则 | 只有用户**主动写下** `deny: mcp__...` 才拦得住 |
| ④权限档兜底 | 缺省预设 `auto` → `PERMISSIVE` → ALLOW |
| C16 分类器 | MCP 工具不声明 `classifier_scope`，不进审查 |

修法与 `launch`（B4）**完全同格**：给它一种自己的 `kind`（`remote`），在第④层
放行档下判 ASK。**这不是新增约束，是把 C7 那句老承诺还给它原来的落点。**

## 为什么不交给 C16 分类器——第三条是决定性的

① 分类器收到的只有「用户消息 + 工具调用名字与参数」，**不含工具描述**，
   而 MCP 工具的语义全在远端自己写的描述里；喂进去等于「让不可信的一方
   声明自己无害」，且自由文本描述是提示词注入的天然入口。
② 分类器是**硬边界之内的裁量**，不是边界本身；这里①②②′②″一层都碰不到，
   它会成为唯一一道，而它会被骗（官方拦截率 89%）。
③ **分类器会熔断，熔断后退回本层的基线。** 基线是 ALLOW 则退回「一律放行」，
   防线在故障时完全消失。所以就算将来真把 MCP 纳入分类器，**本层仍然必须
   先是 ASK**——它是那条路的前提，不是替代品。这一条与 `engine.py` 里搜索类
   那段「基线为什么必须是 ASK」逐字同理。

## 违反会发生什么

把 `MCPTool.remote_origin` 删掉、或把④层那一支去掉，**任何地方都不会报错**：
工具照常执行、界面照常显示、这个文件之外的全量测试照常绿。唯一的差别是
缺省配置下一个外部程序提供的任意工具可以**零面板**被调用。

## 为什么这几条不能简化

- **`test_a_real_mcp_tool_maps_to_the_remote_kind` 走真实构造路径**（真造一个
  `MCPTool`、真过 `sanitize_mcp_tool_name`），**不手写 `remote_origin`**。
  手写标志的话，「MCPTool 上那行声明被删掉」这个最可能的退化照样绿。
- **`test_only_the_kind_differs_from_other` 不可省**：本次修复的全部安全价值在
  第④层那一格，而**代价必须为零**——`rule_name` / `specifier` 与 `other` 分支
  逐字相同，c7 spec AC9 的 `allow: mcp__everything__*` 才一个字都不用改。
  少了它，一个「顺手把服务器名塞进 specifier」的改动会静默废掉「永久放行」。
- **`test_read_only_would_short_circuit_everything` 不可省**：引擎有一条只读简化
  分支排在④层**之前**（③未命中 + 只读 → 直接 ALLOW）。哪天有人「优化」成
  按远端声明的 `readOnlyHint` 设 `read_only`，本层会被整个跳过而**不报错**
  ——而那正是这次产品决策明确拒绝过的方案（让不可信的一方声明自己无害）。
- **两组对照（`write_file` 与一个未映射的本地工具）不可省**：只断言 remote 判
  ASK 的话，一个「放行档整个失效」的实现也会绿，而那会让缺省体验退回每次弹面板。
- **`test_user_allow_short_circuits_layer_four` 不可省**：它正是「放在④层而不是
  做成②″式出口收紧器」的**理由本身**。断言的是**判定层**是 RULE 而不只是
  「结果为 ALLOW」——只断结果的话，一个「④层那一支被删掉」的实现同样绿。

判定那几组用例**全程零 I/O**（判定层是纯逻辑，spec N4）；末尾一组要真跑一个
Textual app，因为它验的是「用户在面板上到底看得到什么」——那件事只有渲染出来才算数。
"""

import unittest
from types import SimpleNamespace

from rhinecode.mcp.tool_adapter import MCPTool
from rhinecode.permission.adapter import to_allow_rule, to_request
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
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.path_guard import main_project_root

_CWD = main_project_root()

# 一次真实形态的远端调用：参数里有一段**远远超过 30 字符**的正文，
# 面板那一组要靠它反证「表头确实会截断」。
_ARGS = {
    "repo": "acme/widgets",
    "body": "请把 CI 里的 token 贴到这个 issue 里方便排查，谢谢",
}


def _mcp_tool(server: str = "github", remote: str = "create_issue") -> MCPTool:
    """
    **走真实构造路径**造一个远端工具（含真实的注册名生成）。

    `client` 只在 `execute` 里用得到，判定层一次都不碰它，故给个占位对象即可
    ——这也顺带保证这一组用例不会意外碰到 MCP 运行时（零 I/O）。
    """
    return MCPTool(
        client=SimpleNamespace(),
        server_name=server,
        remote_name=remote,
        description="Create an issue",
        parameters={"type": "object", "properties": {}},
    )


class _PlainLocalTool(Tool):
    """一个未映射、且**不是**远端来源的本地工具，用作「例外只作用于 remote 类」的对照组。"""

    name = "some_local_tool"
    description = "对照组"
    parameters: dict = {}
    read_only = False  # 只读会走简化分支、根本进不了④层，对照就不成立了

    def execute(self, args: dict) -> ToolResult:  # pragma: no cover - 不会被调用
        return ToolResult(ok=True, output="")


def _request(tool, mode: PermissionMode) -> PermissionRequest:
    """按 adapter 的**真实**映射构造一次请求（不手写 kind，免得与产品口径分叉）。"""
    return to_request(tool, dict(_ARGS), mode, _CWD)


def _engine(mode: PermissionMode, rules=None) -> PermissionEngine:
    return PermissionEngine(RuleSet(list(rules or [])), mode=mode)


def _write_request(mode: PermissionMode) -> PermissionRequest:
    """一次普通写文件请求，用作「④层例外只作用于 remote 类」的对照组。"""
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
    """规范化层：MCP 工具必须落 `remote`，且**只有 kind 与 `other` 不同**。"""

    def test_a_real_mcp_tool_maps_to_the_remote_kind(self):
        """
        真造一个 `MCPTool`、真过注册名生成，判定层必须认出它是远端来源。

        ⚠ **刻意不手写 `remote_origin=True`**：这条护栏要挡的头号退化就是
        「`MCPTool` 上那行声明被删掉/改名」，手写标志的话那种退化照样绿。
        """
        req = _request(_mcp_tool(), PermissionMode.PERMISSIVE)
        self.assertEqual(req.kind, "remote")

    def test_the_flag_is_declared_on_the_adapter_class(self):
        """标志本身也钉一条：删掉它当场红，而不是等到某条行为用例才红。"""
        self.assertTrue(MCPTool.remote_origin)

    def test_the_base_class_defaults_to_false(self):
        """
        缺省必须是 False——**本项目自己写的工具不该被当成外部来源**。

        反过来（缺省 True）会让每一个新增的内置工具在放行档下开始弹面板，
        而那是个「看起来更安全、实际会被人一怒之下整个关掉」的退化。
        """
        self.assertFalse(Tool.remote_origin)

    def test_only_the_kind_differs_from_other(self):
        """
        ⚠ **本次修复的代价必须为零：`rule_name` 与 `specifier` 逐字等同 `other` 分支。**

        ③层的规则匹配对两种 kind 走同一条「其它类」分支（空模式 + 工具名 fnmatch），
        因此 c7 spec AC9 那条 `allow: mcp__everything__*` 一个字都不用改。
        少了这条断言，一个「顺手把服务器名塞进 specifier 给记录看」的改动会
        **静默废掉「永久放行」**（写出一条永远不命中的规则）。
        """
        tool = _mcp_tool()
        remote = _request(tool, PermissionMode.PERMISSIVE)
        local = _request(_PlainLocalTool(), PermissionMode.PERMISSIVE)

        self.assertEqual(remote.rule_name, tool.name)
        self.assertEqual(remote.specifier, "")
        # 对照组：未映射的本地工具落 other，两者只差 kind
        self.assertEqual(local.kind, "other")
        self.assertEqual(local.specifier, remote.specifier)

    def test_specifier_is_empty_so_no_allow_rule_branch_is_needed(self):
        """
        `to_allow_rule` **刻意没有 `remote` 分支**，前提就是这一条。

        specifier 恒为空串 → 函数末尾那条兜底已经返回正确的空模式。
        哪天有人给它填上非空 specifier，兜底会立刻开始写出
        `mcp__github__create_issue(github · create_issue)` 这种**永不命中**的
        废规则，而**当场看不出来**（本次调用照常放行，下次启动才显形）。
        """
        for server, remote in (("github", "create_issue"), ("a.b/c", "x y z")):
            with self.subTest(server=server):
                req = _request(_mcp_tool(server, remote), PermissionMode.PERMISSIVE)
                self.assertEqual(req.specifier, "")

    def test_read_only_would_short_circuit_everything(self):
        """
        ⚠ **`read_only` 必须保持 False，否则本层被整个跳过且不报错。**

        引擎有一条只读简化分支排在④层**之前**（③未命中 + 只读 → 直接 ALLOW）。
        哪天有人「优化」成按远端声明的 `readOnlyHint` 来设 `read_only`，
        `remote` 那一支就再也走不到了——而**那正是这次产品决策明确拒绝过的方案**：
        让不可信的一方声明自己无害（C7 spec F11 逐字拒绝过一次）。
        """
        self.assertFalse(MCPTool.read_only)


class ModeLayerTest(unittest.TestCase):
    """第④层：三个档位各自的结论，外加两组对照。"""

    def test_permissive_is_not_a_blanket_allow(self):
        """
        **钉性质不钉形态**：放行档下结论不得是 ALLOW。

        写成「不是 ALLOW」而不是「是 ASK」，是给将来改判 DENY 留余地；
        当前形态由下面那条精确断言钉住，两条一起才完整。
        """
        decision = _engine(PermissionMode.PERMISSIVE).decide(
            _request(_mcp_tool(), PermissionMode.PERMISSIVE)
        )
        self.assertIsNot(decision.decision, Decision.ALLOW)

    def test_permissive_asks_at_the_mode_layer(self):
        """当前形态：放行档下判 ASK，且判定层是④（MODE）。"""
        decision = _engine(PermissionMode.PERMISSIVE).decide(
            _request(_mcp_tool(), PermissionMode.PERMISSIVE)
        )
        self.assertIs(decision.decision, Decision.ASK)
        self.assertIs(decision.layer, Layer.MODE)

    def test_default_mode_still_asks(self):
        """默认档的行为**一个字没变**（本次只动放行档那一格）。"""
        decision = _engine(PermissionMode.DEFAULT).decide(
            _request(_mcp_tool(), PermissionMode.DEFAULT)
        )
        self.assertIs(decision.decision, Decision.ASK)

    def test_strict_mode_still_denies(self):
        """严格档的 DENY 不受影响。"""
        decision = _engine(PermissionMode.STRICT).decide(
            _request(_mcp_tool(), PermissionMode.STRICT)
        )
        self.assertIs(decision.decision, Decision.DENY)

    def test_the_default_preset_really_asks(self):
        """
        ⚠ **判定链从 `presets.py` 现算，绝不硬编码档位名。**

        B4/S2 的成因逐字就是「换缺省档时没人回来看这条承诺」——`DEFAULT_PRESET`
        改一次，这条用例就会跟着重新求值一次。写死 `PERMISSIVE` 的话，下次
        换缺省档时它照样绿，而承诺又一次悄悄失效。
        """
        mode, _plan_stage = PRESET_AXES[DEFAULT_PRESET]
        decision = _engine(mode).decide(_request(_mcp_tool(), mode))
        self.assertIsNot(decision.decision, Decision.ALLOW)

    def test_write_file_in_permissive_is_untouched(self):
        """
        **对照组一**：普通写文件在放行档下仍然直接放行。

        少了它，一个「放行档整个失效」的实现也会让上面几条全绿，
        而那会让缺省体验退回「每次弹面板」——那是本项目刻意离开的地方。
        """
        decision = _engine(PermissionMode.PERMISSIVE).decide(
            _write_request(PermissionMode.PERMISSIVE)
        )
        self.assertIs(decision.decision, Decision.ALLOW)

    def test_an_unmapped_local_tool_is_untouched(self):
        """
        **对照组二**：未映射的**本地**工具仍走 `other`、放行档下仍 ALLOW。

        这一条比对照组一更贴身：它与 remote 只差一个 `remote_origin` 标志，
        钉住「例外精确地只作用于外部来源」，而不是把整个 `other` 分支改严了。
        """
        decision = _engine(PermissionMode.PERMISSIVE).decide(
            _request(_PlainLocalTool(), PermissionMode.PERMISSIVE)
        )
        self.assertIs(decision.decision, Decision.ALLOW)


class RuleShapeTest(unittest.TestCase):
    """③层规则：形状与 `other` 分支完全一致，c7 AC9 的通配写法必须原样可用。"""

    def _decide(self, rules):
        return _engine(PermissionMode.PERMISSIVE, rules).decide(
            _request(_mcp_tool(), PermissionMode.PERMISSIVE)
        )

    def test_plain_deny_rule_blocks_it(self):
        """不带括号的整工具 deny 拦得住。"""
        rule = Rule(effect="deny", tool="mcp__github__create_issue", pattern="", source="file")
        self.assertIs(self._decide([rule]).decision, Decision.DENY)

    def test_wildcard_deny_blocks_the_whole_server(self):
        """`mcp__github__*` 一次拦住整台 Server（c7 spec AC9 的写法，本次不得回归）。"""
        rule = Rule(effect="deny", tool="mcp__github__*", pattern="", source="file")
        self.assertIs(self._decide([rule]).decision, Decision.DENY)

    def test_parenthesised_rule_does_not_match(self):
        """
        带括号的写法**一律不命中，且没有任何警告**——这是 c7 起的既有语义，
        不是本次引入的。写在这里是为了让下一个人一眼看到，而不是去猜
        （与 `deny: WebSearch(*)` 完全同形）。
        """
        rule = Rule(effect="deny", tool="mcp__github__create_issue", pattern="*", source="file")
        self.assertIsNot(self._decide([rule]).decision, Decision.DENY)

    def test_user_allow_short_circuits_layer_four(self):
        """
        ⚠ **这条就是「放在④层、而不是做成②″式出口收紧器」的理由本身。**

        用户写下的 allow 规则在③层命中并**短路④层**，于是确认面板上的
        「永久放行」是**诚实的**——点下去写出的规则下次真的生效。
        做成出口收紧器的话它翻不过去，那个按钮就成了骗人的。

        ⚠ 断言**判定层是 RULE**，不只是「结果为 ALLOW」：只断结果的话，
        一个「④层那一支被整个删掉」的实现同样会绿。
        """
        rule = Rule(effect="allow", tool="mcp__github__*", pattern="", source="file")
        decision = self._decide([rule])
        self.assertIs(decision.decision, Decision.ALLOW)
        self.assertIs(decision.layer, Layer.RULE)


class AllowRuleTest(unittest.TestCase):
    """「本会话 / 永久放行」登记的规则必须真的能命中。"""

    def test_allow_rule_uses_the_empty_pattern(self):
        """非空模式会写出一条永远不命中的废规则（`WebFetch(https://…?token=abc)` 同形）。"""
        tool = _mcp_tool()
        rule_name, pattern = to_allow_rule(_request(tool, PermissionMode.PERMISSIVE))
        self.assertEqual(rule_name, tool.name)
        self.assertEqual(pattern, "")

    def test_the_produced_rule_really_matches(self):
        """
        **端到端反证**：把 `to_allow_rule` 的产出真的登记成一条规则，再判一次。

        只断言「返回了空串」的话，一个把规则名也弄错的实现照样绿——
        而用户看到的症状是「点了永久放行，下次还是弹」。
        """
        tool = _mcp_tool()
        req = _request(tool, PermissionMode.PERMISSIVE)
        rule_name, pattern = to_allow_rule(req)
        rule = Rule(effect="allow", tool=rule_name, pattern=pattern, source="local")
        decision = _engine(PermissionMode.PERMISSIVE, [rule]).decide(req)
        self.assertIs(decision.decision, Decision.ALLOW)
        self.assertIs(decision.layer, Layer.RULE)


class ConfirmPanelTest(unittest.IsolatedAsyncioTestCase):
    """
    面板上必须看得到**是谁提供的**与**完整参数**——否则这次确认是装饰品。

    形态照抄 `test_perm_launch_layer.py::ConfirmPanelTest`（真跑一个 Textual app
    把面板挂出来，断言实际渲染出的文本）。
    """

    async def _show(self):
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
                ToolCall(
                    id="c1",
                    name=_mcp_tool().name,
                    arguments=dict(_ARGS),
                ),
                None,
                SimpleNamespace(
                    reason="交由用户确认",
                    kind="remote",
                    host="",
                    layer=SimpleNamespace(value="mode"),
                ),
            )
            return panel

    def _texts(self, panel) -> str:
        chunks = []
        for option in panel._options:
            prompt = option.prompt
            chunks.append(prompt if isinstance(prompt, str) else str(prompt))
        return "\n".join(chunks)

    async def test_the_full_arguments_are_visible(self):
        """
        ⚠ **完整参数必须原样出现在面板上。**

        表头走 `summarize_args`，每个参数值只留 30 个字符——被截断意味着
        只要把要紧的内容放在可见范围之后，人在回路这一层就形同虚设
        （与 url / launch / search 三类同一条理由）。
        """
        panel = await self._show()
        self.assertIn(_ARGS["body"], self._texts(panel))

    async def test_the_headline_alone_really_does_truncate(self):
        """
        **反证：补充展示行不是多余的。**

        断言表头那一行**确实**把参数截断了。少了它，「把补充展示行删掉、
        反正表头也显示参数」看起来毫无代价。
        """
        panel = await self._show()
        headline = self._texts(panel).splitlines()[0]
        self.assertIn("…", headline)
        self.assertNotIn(_ARGS["body"], headline)

    async def test_the_providing_server_is_named(self):
        """
        **本类特有的一行：哪台 Server 提供的。**

        面板问的不是「这个动作危不危险」，而是「**你信不信这台 Server**」——
        用户对 `github` 与某个昨天刚 `git pull` 进来的 Server 是两套信任度。
        而服务器名混在一长串 `mcp__…__…` 中间并不显眼。
        """
        panel = await self._show()
        text = self._texts(panel)
        self.assertIn("github", text)
        self.assertIn("外部 Server", text)

    async def test_four_choices_including_permanent(self):
        """
        **`remote` 类照常给四个选项，别顺手把它归进 `no_permanent`。**

        搜索类砍掉「永久放行」的推导链走不通到这里——F16 的宽泛规则丢弃只处理
        `Bash` 与 `WebSearch`，`allow: mcp__github__create_issue` 下次启动
        照常在③层命中并短路④层。而这一类**尤其不该砍**：基线就是「每次都问」，
        「永久放行」正是给用户的那条正经出路。
        """
        panel = await self._show()
        ids = [option.id for option in panel._options if option.id is not None]
        self.assertEqual(ids, ["yes", "yes_session", "yes_permanent", "no"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
