"""
搜索类的权限判定单测（web_search 扩展 T16，spec F10/F11/F12/F14 · AC10–AC12/AC15）。

四组：

1. **前四层反证** —— ①②②′②″ 对搜索一律不生效，结论必须来自第④层
2. **④层三档模式** —— 放行档也判 ASK（例外），并有 write_file 对照组
3. **规则形状** —— 只认不带括号的整工具形式；带括号一律不命中**且无警告**
4. **`to_allow_rule`** —— 搜索类返回空模式，且写下的规则真的能命中

**本文件全程零 I/O**：判定层是纯逻辑（spec N4）。
"""

import unittest

from rhinecode.permission import config as perm_config
from rhinecode.permission.adapter import _TOOL_MAP, to_allow_rule
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import (
    Decision,
    Layer,
    PermissionMode,
    PermissionRequest,
    Rule,
)
from rhinecode.permission.rules import RuleSet
from rhinecode.tools.path_guard import main_project_root

_CWD = main_project_root()


def _request(query: str, mode: PermissionMode = PermissionMode.DEFAULT) -> PermissionRequest:
    """按 adapter 的真实映射构造一次搜索请求。"""
    rule_name, specifier, kind = _TOOL_MAP["web_search"]({"query": query})
    return PermissionRequest(
        tool_name="web_search",
        rule_name=rule_name,
        specifier=specifier,
        kind=kind,
        is_read_only=False,
        mode=mode,
        cwd=_CWD,
    )


def _write_request(mode: PermissionMode) -> PermissionRequest:
    """一次普通写文件请求，用作「④层例外只作用于搜索类」的对照组。"""
    return PermissionRequest(
        tool_name="write_file",
        rule_name="Write",
        specifier="out.txt",
        kind="write_path",
        is_read_only=False,
        mode=mode,
        cwd=_CWD,
    )


def _engine(rules=None, mode: PermissionMode = PermissionMode.DEFAULT) -> PermissionEngine:
    """
    ⚠ 引擎的第④层读的是 **`request.mode`**，不是 `engine.mode`——
    档位由 `adapter.to_request` 在规范化时写进请求里。因此本文件的每个用例
    都必须把同一个档位**同时**传给 `_engine` 与 `_request`，
    否则测的是「默认档」而不是自己以为的那一档。

    （实现期真的写错过一次：`_engine(mode=STRICT)` 而 `_request` 用缺省档，
    于是「严格档应当拒绝」那条拿到的是 ASK，看起来像实现漏了那一支。）
    """
    return PermissionEngine(RuleSet(list(rules or [])), mode=mode)


def _rule(effect: str, pattern: str = "", tool: str = "WebSearch") -> Rule:
    return Rule(effect=effect, tool=tool, pattern=pattern, source="project")


# =============================================================================
# 一、前四层反证（spec F11 / AC11）
# =============================================================================
class LayersDoNotApplyTests(unittest.TestCase):
    """
    ①危险命令黑名单 / ②路径沙箱 / ②′网络边界 / ②″保护路径**都不该碰搜索**。

    这是既有代码的结构性结果（四层各自按 `request.kind` 早退），而不是新增豁免。
    但「结构性成立」这句话本身需要被钉住——四层里任何一处把判据从
    `kind == "command"` 改成「或者也看看别的」，这四条会红。
    """

    def test_dangerous_looking_query_reaches_mode_layer(self) -> None:
        """查询词长得像危险命令，也只是一段要发出去的文字。"""
        verdict = _engine(mode=PermissionMode.PERMISSIVE).decide(
            _request("rm -rf / 是什么意思", PermissionMode.PERMISSIVE)
        )
        self.assertIs(verdict.layer, Layer.MODE)
        self.assertIsNot(verdict.layer, Layer.BLACKLIST)

    def test_path_traversal_looking_query_reaches_mode_layer(self) -> None:
        verdict = _engine(mode=PermissionMode.PERMISSIVE).decide(
            _request("../../etc/passwd 是什么", PermissionMode.PERMISSIVE)
        )
        self.assertIs(verdict.layer, Layer.MODE)
        self.assertIsNot(verdict.layer, Layer.SANDBOX)

    def test_url_looking_query_does_not_enter_network_layer(self) -> None:
        """
        ⚠ 最关键的一条：查询词里出现地址时**不能**被②′层当地址判。

        复用 `kind="url"` 的话，`check_hard` 会拿查询词去解析、判「地址畸形」，
        表现是**每次搜索都被硬拒**——而那看起来像「网络边界在正常工作」。
        """
        verdict = _engine(mode=PermissionMode.PERMISSIVE).decide(
            _request("http://127.0.0.1:8080 打不开怎么排查", PermissionMode.PERMISSIVE)
        )
        self.assertIs(verdict.layer, Layer.MODE)
        self.assertIsNot(verdict.layer, Layer.NETWORK)

    def test_config_path_query_not_upgraded_by_protected_layer(self) -> None:
        """②″保护路径按 `kind != "write_path"` 早退，搜索不受它影响。"""
        verdict = _engine(mode=PermissionMode.PERMISSIVE).decide(
            _request(".rhinecode/permissions.yaml 怎么写", PermissionMode.PERMISSIVE)
        )
        self.assertIs(verdict.layer, Layer.MODE)
        self.assertIsNot(verdict.layer, Layer.PROTECTED)


# =============================================================================
# 二、④层模式兜底（spec F12 / AC12）
# =============================================================================
class ModeFallbackTests(unittest.TestCase):
    def test_strict_denies(self) -> None:
        verdict = _engine(mode=PermissionMode.STRICT).decide(
            _request("q", PermissionMode.STRICT)
        )
        self.assertIs(verdict.decision, Decision.DENY)
        self.assertIs(verdict.layer, Layer.MODE)

    def test_default_asks(self) -> None:
        verdict = _engine(mode=PermissionMode.DEFAULT).decide(
            _request("q", PermissionMode.DEFAULT)
        )
        self.assertIs(verdict.decision, Decision.ASK)

    def test_permissive_still_asks(self) -> None:
        """
        ⚠ **本扩展最要紧的一格。**

        基线是 ASK 而不是 ALLOW，决定了分类器熔断之后退回**逐次弹面板**
        而不是**一律放行**——后者意味着「查询词外泄」这条防线在故障时
        完全消失，而用户只看到一条熔断提示。
        """
        verdict = _engine(mode=PermissionMode.PERMISSIVE).decide(
            _request("q", PermissionMode.PERMISSIVE)
        )
        self.assertIs(verdict.decision, Decision.ASK)
        self.assertIs(verdict.layer, Layer.MODE)

    def test_permissive_reason_does_not_mention_domain_whitelist(self) -> None:
        """
        ⚠ 反证：搜索类的文案**不能**复用网络类那句「未建立域名白名单时」。

        搜索根本没有白名单这回事（F10 只支持整工具规则），沿用那句话会把用户
        引去写一条不存在的规则，然后困惑于为什么不生效。
        """
        reason = (
            _engine(mode=PermissionMode.PERMISSIVE)
            .decide(_request("q", PermissionMode.PERMISSIVE))
            .reason
        )
        self.assertNotIn("白名单", reason)
        self.assertIn("第三方", reason)

    def test_permissive_write_file_unchanged(self) -> None:
        """对照组：④层的例外只作用于搜索类，其余工具行为逐字不变。"""
        verdict = _engine(mode=PermissionMode.PERMISSIVE).decide(
            _write_request(PermissionMode.PERMISSIVE)
        )
        self.assertIs(verdict.decision, Decision.ALLOW)


# =============================================================================
# 三、规则形状（spec F10 / AC10）
# =============================================================================
class RuleShapeTests(unittest.TestCase):
    def test_whole_tool_deny_hits(self) -> None:
        verdict = _engine([_rule("deny")], mode=PermissionMode.PERMISSIVE).decide(
            _request("q", PermissionMode.PERMISSIVE)
        )
        self.assertIs(verdict.decision, Decision.DENY)
        self.assertIs(verdict.layer, Layer.RULE)

    def test_whole_tool_allow_hits(self) -> None:
        verdict = _engine([_rule("allow")], mode=PermissionMode.DEFAULT).decide(
            _request("q", PermissionMode.DEFAULT)
        )
        self.assertIs(verdict.decision, Decision.ALLOW)
        self.assertIs(verdict.layer, Layer.RULE)

    def test_deny_beats_allow(self) -> None:
        engine = _engine([_rule("allow"), _rule("deny")], mode=PermissionMode.PERMISSIVE)
        self.assertIs(
            engine.decide(_request("q", PermissionMode.PERMISSIVE)).decision, Decision.DENY
        )

    def test_patterned_rules_never_hit(self) -> None:
        """
        ⚠ **已知边界的正面钉住**（spec F10 / plan 评审 2026-08-19 裁定不做容错）。

        带括号的写法一律不命中——**包括 deny**。于是一个想拦住搜索的用户写下
        `deny: WebSearch(*)` 之后什么都没拦住，而且没有任何提示。

        这是明知而接受的取舍，缓解手段是配置模板里的说明（F10a，下面一条测它）。
        **谁要日后补一个加载期警告，这条会红**——那正是它存在的目的：
        逼他连同 spec F10 的已知边界与安全边界第 8 条一起重新评审，
        而不是悄悄改掉一个被裁定过的取舍。
        """
        for pattern in ("*", "domain:x", "关键词", "query:*"):
            with self.subTest(pattern=pattern):
                allowed = _engine([_rule("allow", pattern)], mode=PermissionMode.DEFAULT)
                self.assertIs(
                    allowed.decide(_request("q", PermissionMode.DEFAULT)).decision,
                    Decision.ASK,
                )

                denied = _engine([_rule("deny", pattern)], mode=PermissionMode.PERMISSIVE)
                self.assertIs(
                    denied.decide(_request("q", PermissionMode.PERMISSIVE)).decision,
                    Decision.ASK,
                )

    def test_patterned_rules_produce_no_load_warning(self) -> None:
        """
        ⚠ 同上，从加载期这一侧再钉一次：**不产生任何警告**。

        `permission/config.py` 的语法校验只认 `WebFetch(...)`，对 `WebSearch(...)`
        一个字都不说——这是裁定的一部分，不是漏了。
        """
        for text, effect in (("WebSearch(*)", "allow"), ("WebSearch(domain:x)", "deny")):
            with self.subTest(text=text):
                warnings: list = []
                rule = perm_config.parse_rule_string(
                    text, effect, "project", warnings=warnings
                )
                # 规则照常被解析出来（只是永远不命中），且**一条警告都没有**。
                self.assertIsNotNone(rule)
                self.assertEqual(rule.tool, "WebSearch")
                self.assertEqual(warnings, [])

    def test_other_tool_rules_do_not_hit(self) -> None:
        """`WebFetch` 的规则管不到搜索——两者是不同的规则体系名。"""
        engine = _engine(
            [_rule("deny", "domain:*", tool="WebFetch")], mode=PermissionMode.PERMISSIVE
        )
        self.assertIs(
            engine.decide(_request("q", PermissionMode.PERMISSIVE)).decision, Decision.ASK
        )


# =============================================================================
# 四、to_allow_rule（spec F14 · 「本会话放行」的落点）
# =============================================================================
class AllowRuleTranslationTests(unittest.TestCase):
    def test_returns_empty_pattern(self) -> None:
        rule_name, pattern = to_allow_rule(_request("我的私密问题"))
        self.assertEqual(rule_name, "WebSearch")
        self.assertEqual(pattern, "")

    def test_query_not_written_into_rule(self) -> None:
        """查询词写进规则既不合法（带括号不命中）也是泄漏。"""
        _rule_name, pattern = to_allow_rule(_request("我的私密问题"))
        self.assertNotIn("私密", pattern)

    def test_session_rule_actually_hits(self) -> None:
        """
        「本会话放行」写下的规则必须真的能命中同类请求——
        否则用户点了之后下次还弹，与「永久放行」那个骗人的按钮是同一形态。
        """
        rule_name, pattern = to_allow_rule(_request("a"))
        engine = _engine(mode=PermissionMode.PERMISSIVE)
        engine.add_session_rule(
            Rule(effect="allow", tool=rule_name, pattern=pattern, source="session")
        )
        verdict = engine.decide(_request("完全不同的另一条查询", PermissionMode.PERMISSIVE))
        self.assertIs(verdict.decision, Decision.ALLOW)


# =============================================================================
# 五、「规则匹配层未改」的反证（checklist 第八节）
# =============================================================================
class RulesModuleUnchangedTests(unittest.TestCase):
    """
    搜索类走的是 `rules._rule_matches` 既有的「其它类」分支，本扩展**一行都没改**
    那个文件。这条断言把「不用改」这件事本身钉住——否则下一个人会以为漏了，
    去加一个 `if request.kind == "search"` 分支，而那只会引入 bug。
    """

    def test_other_branch_semantics(self) -> None:
        from rhinecode.permission.rules import _rule_matches

        request = _request("q")
        self.assertTrue(_rule_matches(_rule("allow", ""), request))
        self.assertFalse(_rule_matches(_rule("allow", "*"), request))
        self.assertFalse(_rule_matches(_rule("allow", "anything"), request))

    def test_tool_name_wildcard_still_works(self) -> None:
        """「其它类」分支的工具名用 fnmatch，`Web*` 这种写法照常命中。"""
        from rhinecode.permission.rules import _rule_matches

        self.assertTrue(_rule_matches(_rule("deny", "", tool="Web*"), _request("q")))


if __name__ == "__main__":
    unittest.main()
