"""
本次执行级预授权（Skill 的 `allowed-tools`）。

## 这套机制是什么

Agent Skills 标准里的 `allowed-tools` 是**预授权**——「列出的操作在本次执行内
免于人工确认」，它**不限制**模型能调用什么。本系统把它实现成权限引擎第③层的
又一级规则（`turn_rules`），优先级高于会话级与文件级。

## 为什么要有这组测试

预授权是本轮改造中**唯一扩大模型自由度**的改动：它把原本需要逐次确认的操作
变成免确认。因此两件事必须钉死：

1. **它翻不过前三层**——一个声明「放行全部命令」的 Skill 也不能执行危险命令，
   也不能读写工作区外的路径。这是 spec N2；
2. **它必须成对撤销**——任何一条泄漏到下一次执行，就意味着用户在完全不知情的
   情况下失去了一次确认机会。这是 spec N3。

第 1 条尤其重要：它是「预授权是否安全」的**全部依据**。合并点一旦被挪到
黑名单或沙箱之前，这两条会立刻变红。
"""

from __future__ import annotations

import unittest

from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import Decision, Layer, PermissionMode, PermissionRequest
from rhinecode.permission.rules import RuleSet
from rhinecode.skills.models import SkillSource, SkillSpec
from rhinecode.skills.validation import GRANT_SOURCE, grants_for
from pathlib import Path


def _engine() -> PermissionEngine:
    """默认模式的干净引擎——此时「无规则命中」的副作用工具一律判 ASK。"""
    return PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)


def _bash(command: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name="run_command",
        rule_name="Bash",
        specifier=command,
        kind="command",
        is_read_only=False,
        mode=PermissionMode.DEFAULT,
    )


def _write(path: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name="write_file",
        rule_name="Write",
        specifier=path,
        kind="write_path",
        is_read_only=False,
        mode=PermissionMode.DEFAULT,
    )


def _spec(*granted: str) -> SkillSpec:
    return SkillSpec(
        command_name="demo",
        display_name="demo",
        description="d",
        when_to_use=None,
        body="正文",
        granted_tools=tuple(granted),
        forked=False,
        model_invocable=True,
        user_invocable=True,
        model=None,
        source=SkillSource.PROJECT,
        entry_path=Path("/x/demo.md"),
        resource_dir=None,
        resource_files=(),
    )


class GrantTranslationTest(unittest.TestCase):
    """声明 → 规则的翻译。"""

    def test_standard_vocabulary_maps_identically(self) -> None:
        """
        标准词汇与本系统的规则名**逐字相同**，恒等映射即可。

        这正是「不需要翻译层」的依据——`allowed-tools: Bash(git add *)` 可以
        原封不动当成一条 allow 规则，连括号里的 glob 语法都一致。
        """
        rules, warnings = grants_for([_spec("Bash(git add *)", "Read")])
        self.assertEqual(warnings, [])
        self.assertEqual([(r.tool, r.pattern, r.effect) for r in rules],
                         [("Bash", "git add *", "allow"), ("Read", "", "allow")])
        self.assertTrue(all(r.source == GRANT_SOURCE for r in rules))

    def test_glob_and_grep_fold_into_read(self) -> None:
        """标准把只读检索拆成两个工具名，本系统都归在 Read 下。"""
        rules, _ = grants_for([_spec("Glob", "Grep")])
        self.assertEqual({r.tool for r in rules}, {"Read"})

    def test_duplicate_rules_deduped(self) -> None:
        """
        映射到同一条规则的多个声明只产出一条。

        求值上重复无害（命中哪条都一样），但 `/skills prompt` 会把同一行列两遍，
        用户会以为自己写重了——实测三个内置样板里的 `Read` + `Grep` 就是这样。
        """
        rules, _ = grants_for([_spec("Read", "Grep", "Glob")])
        self.assertEqual(len(rules), 1)

    def test_internal_tool_names_also_accepted(self) -> None:
        """
        本系统的内部工具名也收下。

        用户可能照着 `/skills` 里看到的工具名来写，那时报「不认识」纯属自找麻烦。
        """
        rules, warnings = grants_for([_spec("run_command(npm *)", "read_file")])
        self.assertEqual(warnings, [])
        self.assertEqual([(r.tool, r.pattern) for r in rules],
                         [("Bash", "npm *"), ("Read", "")])

    def test_mcp_names_pass_through(self) -> None:
        """MCP 工具名原样放行——权限引擎的 other 分支按工具名做 fnmatch。"""
        rules, warnings = grants_for([_spec("mcp__github__*")])
        self.assertEqual(warnings, [])
        self.assertEqual(rules[0].tool, "mcp__github__*")

    def test_unknown_name_warns_but_does_not_fail(self) -> None:
        """
        无法识别的项**跳过 + 警告，不 fail-fast**。

        这与 C11「内置工具名笔误就 fail-fast」的取舍相反，理由是来源不同：
        外部 Skill 里出现 `Task` / `TodoWrite` 是正常现象，不该让程序起不来。

        ⚠ **样例工具名换过一次**：本用例原先用的是 `WebFetch`，
        而 web_fetch 扩展让它变成了**真工具**（`skills/validation.py` 的
        `_TOOL_ALIASES` 里有它），于是它会被正常翻译成一条规则、不再产生警告。
        现改用 `TodoWrite`——挑样例时请确认它确实不在那张别名表里。
        """
        rules, warnings = grants_for([_spec("TodoWrite", "Bash")])
        self.assertEqual(len(rules), 1, "认识的那条仍要照常解析")
        self.assertEqual(rules[0].tool, "Bash")
        self.assertEqual(len(warnings), 1)
        self.assertIn("TodoWrite", warnings[0])

    def test_never_produces_deny(self) -> None:
        """
        预授权只能放宽、不能收紧。

        这条是语义护栏：若将来有人想「顺便支持 disallowed-tools」而在这里产出
        deny 规则，会破坏「allowed-tools 不限制模型能做什么」这条标准语义。
        """
        rules, _ = grants_for([_spec("Bash", "Read", "Write", "Edit")])
        self.assertTrue(all(r.effect == "allow" for r in rules))


class GrantLifecycleTest(unittest.TestCase):
    """授予与撤销。"""

    def test_grant_turns_ask_into_allow(self) -> None:
        engine = _engine()
        self.assertIs(engine.decide(_bash("git status")).decision, Decision.ASK)

        rules, _ = grants_for([_spec("Bash(git *)")])
        engine.grant_turn_rules(rules)
        result = engine.decide(_bash("git status"))
        self.assertIs(result.decision, Decision.ALLOW)
        self.assertIs(result.layer, Layer.RULE)

    def test_revoke_restores_ask(self) -> None:
        engine = _engine()
        rules, _ = grants_for([_spec("Bash(git *)")])
        engine.grant_turn_rules(rules)
        engine.revoke_turn_rules()
        self.assertIs(engine.decide(_bash("git status")).decision, Decision.ASK)

    def test_revoke_is_idempotent(self) -> None:
        """
        撤销要能重复调用。

        它整体清空而非按条移除，正是为了让「撤销」不依赖任何前置状态——
        异常路径上「记住了授予什么但没走到移除」是最容易发生的情形。
        """
        engine = _engine()
        engine.grant_turn_rules(grants_for([_spec("Bash")])[0])
        engine.revoke_turn_rules()
        engine.revoke_turn_rules()
        self.assertEqual(engine.turn_rules, [])

    def test_grants_accumulate(self) -> None:
        """一次执行中可能有多个 Skill 先后触发，授权应当叠加而非替换。"""
        engine = _engine()
        engine.grant_turn_rules(grants_for([_spec("Bash(git *)")])[0])
        engine.grant_turn_rules(grants_for([_spec("Write")])[0])
        self.assertIs(engine.decide(_bash("git status")).decision, Decision.ALLOW)
        self.assertIs(engine.decide(_write("a.txt")).decision, Decision.ALLOW)

    def test_session_rules_untouched_by_revoke(self) -> None:
        """撤销本次执行级授权，不能顺手清掉「本会话放行」。"""
        from rhinecode.permission.models import Rule

        engine = _engine()
        engine.add_session_rule(Rule("allow", "Bash", "npm *", "session"))
        engine.grant_turn_rules(grants_for([_spec("Write")])[0])
        engine.revoke_turn_rules()
        self.assertIs(engine.decide(_bash("npm test")).decision, Decision.ALLOW)


class GrantIsPerTriggerNotPerActivationTest(unittest.TestCase):
    """
    **授权跟「触发」走，不跟「激活态」走**（F12）。

    ## 这条护栏钉的是一个真实缺陷

    初版的 `turn_grants()` 从**激活列表**取规则。共享模式 Skill 是常驻的，
    于是实测发现：用户跑一次 `/notetaker`（声明了 Write 授权）之后，
    **此后整个会话的每一轮都会重新拿到那份授权**——写操作从此静默免确认，
    而用户完全不知情。那正是 spec N3 要防的「在不知情的情况下失去确认机会」。

    改成「谁触发就为谁授权」之后，授权的生命周期由调用方的 `try/finally` 界定，
    与「Skill 正文是否常驻」彻底解耦。
    """

    def test_grants_come_from_the_triggered_spec_only(self) -> None:
        """接口层面：取规则要传入具体的 spec，不再有「读激活列表」那条路。"""
        from rhinecode.skills.manager import SkillManager

        self.assertFalse(
            hasattr(SkillManager, "turn_grants"),
            "按激活列表取授权的旧入口必须已移除——它会让授权跟着常驻态一起长命",
        )
        self.assertTrue(callable(SkillManager.grants_for_spec))

    def test_grants_for_spec_ignores_activation_state(self) -> None:
        """
        同一个 spec 无论激活与否，产出的规则都一样——它是**静态**函数。

        这正是「授权与常驻态解耦」的形式化表达。
        """
        from rhinecode.skills.manager import SkillManager

        spec = _spec("Write")
        a, _ = SkillManager.grants_for_spec(spec)
        b, _ = SkillManager.grants_for_spec(spec)
        self.assertEqual(a, b)
        self.assertEqual([r.tool for r in a], ["Write"])


class GrantCannotBypassEarlierLayersTest(unittest.TestCase):
    """
    **本模块最重要的两条**：预授权翻不过前三层。

    合并点位于①黑名单与②沙箱**之后**，这个顺序是预授权安全性的全部依据。
    """

    def test_blacklist_still_wins(self) -> None:
        """声明「放行全部命令」也执行不了危险命令——第①层先判。"""
        engine = _engine()
        engine.grant_turn_rules(grants_for([_spec("Bash")])[0])
        result = engine.decide(_bash("rm -rf /"))
        self.assertIs(result.decision, Decision.DENY)
        self.assertIs(result.layer, Layer.BLACKLIST)

    def test_sandbox_still_wins(self) -> None:
        """声明「放行全部写入」也写不到工作区外——第②层先判。"""
        engine = _engine()
        engine.grant_turn_rules(grants_for([_spec("Write")])[0])
        result = engine.decide(_write("../../etc/passwd"))
        self.assertIs(result.decision, Decision.DENY)
        self.assertIs(result.layer, Layer.SANDBOX)

    def test_explicit_deny_rule_still_wins(self) -> None:
        """
        配置里的 deny 规则压过预授权——同层内 deny 优先求值。

        预授权排在合并列表最前只影响「同为 allow 时谁先命中」，
        改变不了 deny 优先这条基本语义。
        """
        from rhinecode.permission.models import Rule

        engine = PermissionEngine(
            RuleSet([Rule("deny", "Bash", "git push *", "project")]),
            mode=PermissionMode.DEFAULT,
        )
        engine.grant_turn_rules(grants_for([_spec("Bash")])[0])
        result = engine.decide(_bash("git push --force"))
        self.assertIs(result.decision, Decision.DENY)


if __name__ == "__main__":
    unittest.main()
