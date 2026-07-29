"""
Skill 文本解析（对齐 Agent Skills 开放标准后重写）。

C11 的解析规则是自研的：`name` 必填且必须匹配严格字符集、`description` 必填、
`mode` 只能是 shared/isolated。这些在对齐改造中全部放宽或改写——本文件随之重写，
不保留任何「已废弃但仍断言」的用例（spec N5）。

判据集中在三件事：
1. **全部字段可选**（标准如此，外部 Skill 才能原样搬进来）；
2. **两种键名写法等价**，但只有旧的下划线 `allowed_tools` 触发语义变更告知；
3. **无对应能力的标准字段逐条告知**，而不是静默忽略。

全部用字符串字面量驱动，不造临时目录——`parse_skill` 是纯函数，
这正是把「读文件」与「推导命令名」都留给 discovery 的收益。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from rhinecode.skills.models import UNSUPPORTED_FIELDS, SkillSource
from rhinecode.skills.parser import parse_skill


def P(text: str, command_name: str = "demo"):
    """解析一份文本；命令名由调用方给（真实链路里它来自发现层的路径推导）。"""
    return parse_skill(text, Path("/x/demo.md"), SkillSource.PROJECT, command_name)


class OptionalFieldsTest(unittest.TestCase):
    """**全部字段可选**——这是「外部 Skill 原样可用」的地基。"""

    def test_no_frontmatter_at_all(self) -> None:
        """
        一份只有正文的 Markdown 就是一个合法 Skill。

        说明从正文第一个非空段落提取，**跳过 Markdown 标题行**——标题通常只是
        名字的重复，拿它当说明对模型判断「什么时候该用这个 Skill」毫无帮助。
        """
        spec, reason, _ = P("# 部署流程\n\n把当前分支部署到预发环境。\n\n步骤一……\n")
        self.assertIsNone(reason)
        self.assertEqual(spec.command_name, "demo")
        self.assertEqual(spec.display_name, "demo")
        self.assertEqual(spec.description, "把当前分支部署到预发环境。")
        self.assertEqual(spec.granted_tools, ())
        self.assertFalse(spec.forked)
        self.assertTrue(spec.model_invocable)
        self.assertTrue(spec.user_invocable)

    def test_empty_frontmatter(self) -> None:
        spec, reason, _ = P("---\n---\n\n正文说明。\n")
        self.assertIsNone(reason)
        self.assertEqual(spec.description, "正文说明。")

    def test_name_is_display_only(self) -> None:
        """
        `name` 只是显示标签，命令名来自路径。

        含大写、空格、超长——C11 会判它非法，现在一律接受。
        """
        spec, reason, _ = P("---\nname: My Very Long Display Name\n---\n正文\n", "frontend-design")
        self.assertIsNone(reason)
        self.assertEqual(spec.command_name, "frontend-design")
        self.assertEqual(spec.display_name, "My Very Long Display Name")

    def test_explicit_description_wins_over_body(self) -> None:
        spec, _, _ = P("---\ndescription: 明确的说明\n---\n正文第一段\n")
        self.assertEqual(spec.description, "明确的说明")


class KeyNormalisationTest(unittest.TestCase):
    """标准用连字符，YAML 使用者习惯下划线，两种都要认。"""

    def test_hyphen_and_underscore_equivalent(self) -> None:
        a, _, _ = P("---\nallowed-tools: Read Grep\nwhen-to-use: 当用户说提交\n"
                    "disable-model-invocation: true\nuser-invocable: false\n---\n正文\n")
        b, _, _ = P("---\nallowed_tools: [Read, Grep]\nwhen_to_use: 当用户说提交\n"
                    "disable_model_invocation: true\nuser_invocable: false\n---\n正文\n")
        self.assertEqual(a.granted_tools, b.granted_tools)
        self.assertEqual(a.when_to_use, b.when_to_use)
        self.assertEqual(a.model_invocable, b.model_invocable)
        self.assertEqual(a.user_invocable, b.user_invocable)

    def test_both_spellings_present_prefers_standard(self) -> None:
        """并存且取值不同时以标准写法为准并提示——静默取其一会让用户以为另一个生效了。"""
        spec, _, _ = P("---\nallowed-tools: Read\nallowed_tools: [Bash]\n---\n正文\n")
        self.assertEqual(spec.granted_tools, ("Read",))
        self.assertTrue(any("两种写法" in n for n in spec.notices))

    def test_tool_list_accepts_both_shapes(self) -> None:
        """标准明确允许 YAML 列表与空格/逗号分隔串两种写法。"""
        a, _, _ = P("---\nallowed-tools: Read, Bash(git *)\n---\n正文\n")
        b, _, _ = P("---\nallowed-tools: [Read, Bash(git *)]\n---\n正文\n")
        self.assertEqual(a.granted_tools, b.granted_tools)

    def test_tool_list_deduped_preserving_order(self) -> None:
        """去重但保序——保序让 `/skills` 的展示与文件里的顺序一致，排查时不必来回对照。"""
        spec, _, _ = P("---\nallowed-tools: [Read, Bash, Read]\n---\n正文\n")
        self.assertEqual(spec.granted_tools, ("Read", "Bash"))


class SemanticChangeNoticeTest(unittest.TestCase):
    """
    **本次改造唯一「静默会造成实际损害」的迁移点**。

    `allowed_tools` 在旧版本是「收窄可见工具集」，现在是「免确认」，两者相反。
    同一份文件在新旧版本下行为相反而用户毫不知情，是不可接受的。
    """

    def test_underscore_spelling_triggers_notice(self) -> None:
        spec, _, _ = P("---\nallowed_tools: [Bash]\n---\n正文\n")
        notice = next(n for n in spec.notices if "语义已变更" in n)
        self.assertIn("免于人工确认", notice)
        self.assertIn("permissions.yaml", notice, "要指出真正该用的限制手段")

    def test_standard_spelling_does_not_trigger_notice(self) -> None:
        """
        连字符是标准写法，作者本来就按预授权语义写的，**不该被打扰**。

        这条与上一条成对：只在写法确实是旧的时才提醒。
        """
        spec, _, _ = P("---\nallowed-tools: [Bash]\n---\n正文\n")
        self.assertFalse(any("语义已变更" in n for n in spec.notices))

    def test_empty_declaration_does_not_trigger_notice(self) -> None:
        """声明为空时没有行为差异可言，不必提醒。"""
        spec, _, _ = P("---\nallowed_tools: []\n---\n正文\n")
        self.assertFalse(any("语义已变更" in n for n in spec.notices))


class ContextAndSwitchesTest(unittest.TestCase):
    def test_context_fork(self) -> None:
        spec, _, _ = P("---\ncontext: fork\n---\n正文\n")
        self.assertTrue(spec.forked)

    def test_unknown_context_value_notices_and_stays_in_main(self) -> None:
        spec, _, _ = P("---\ncontext: background\n---\n正文\n")
        self.assertFalse(spec.forked)
        self.assertTrue(any("context" in n for n in spec.notices))

    def test_boolean_literals(self) -> None:
        """标准允许多种布尔字面量，大小写不敏感。"""
        for literal in ("true", "yes", "on", "1", "TRUE", "Yes"):
            with self.subTest(literal=literal):
                spec, _, _ = P(f'---\ndisable-model-invocation: "{literal}"\n---\n正文\n')
                self.assertFalse(spec.model_invocable)
        for literal in ("false", "no", "off", "0"):
            with self.subTest(literal=literal):
                spec, _, _ = P(f'---\ndisable-model-invocation: "{literal}"\n---\n正文\n')
                self.assertTrue(spec.model_invocable)

    def test_unrecognised_boolean_falls_back_to_default(self) -> None:
        """一个布尔字段写错不该让整个 Skill 用不了。"""
        spec, reason, _ = P("---\nuser-invocable: maybe\n---\n正文\n")
        self.assertIsNone(reason)
        self.assertTrue(spec.user_invocable)

    def test_model_outside_fork_notices(self) -> None:
        spec, _, _ = P("---\nmodel: gpt-x\n---\n正文\n")
        self.assertTrue(any("model" in n for n in spec.notices))

    def test_model_inside_fork_is_silent(self) -> None:
        spec, _, _ = P("---\ncontext: fork\nmodel: gpt-x\n---\n正文\n")
        self.assertEqual(spec.model, "gpt-x")
        self.assertFalse(any("model" in n for n in spec.notices))


class UnsupportedFieldsTest(unittest.TestCase):
    """
    无对应能力的标准字段**必须逐条告知**。

    静默忽略不可接受：作者写 `background: true` 的预期是「后台跑、不阻塞」，
    实际却同步阻塞跑完，这个差异用户不知道就会误判 Skill 的行为。
    """

    def test_each_field_produces_one_notice(self) -> None:
        text = (
            "---\nbackground: true\nagent: explorer\neffort: high\n"
            "hooks: {}\npaths: src/**\nshell: powershell\n---\n正文\n"
        )
        spec, reason, _ = P(text)
        self.assertIsNone(reason, "有这些字段仍应正常加载")
        for field in UNSUPPORTED_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(
                    any(n.startswith(f"`{field}`") for n in spec.notices),
                    f"{field} 应有一条告知",
                )

    def test_notice_states_actual_behaviour(self) -> None:
        """只说「不支持」没用，必须说清「本版本实际会怎么做」。"""
        spec, _, _ = P("---\nbackground: true\n---\n正文\n")
        notice = next(n for n in spec.notices if n.startswith("`background`"))
        self.assertIn("同步等待", notice)

    def test_background_false_is_silent(self) -> None:
        """`background: false` 正是本版本的行为，没有差异可言，不必提醒。"""
        spec, _, _ = P("---\nbackground: false\n---\n正文\n")
        self.assertFalse(any(n.startswith("`background`") for n in spec.notices))

    def test_truly_unknown_keys_stay_silent(self) -> None:
        """
        未登记的未知键**静默忽略**，这是刻意的向前兼容策略。

        读到更新版本写的 Skill 时应当尽量把它用起来，而不是因为多了个键就整个拒绝。
        """
        spec, reason, _ = P("---\nfuture_field: 42\n---\n正文\n")
        self.assertIsNone(reason)
        self.assertEqual(spec.notices, ())


class FailureTest(unittest.TestCase):
    """仍然算失败的只剩这几类——它们让 Skill 真的没法用。"""

    def test_unclosed_frontmatter(self) -> None:
        _, reason, _ = P("---\nname: x\n没有第二条分隔线\n")
        self.assertIn("未闭合", reason)

    def test_bad_yaml(self) -> None:
        _, reason, _ = P("---\nname: [unclosed\n---\n正文\n")
        self.assertIn("解析失败", reason)

    def test_frontmatter_not_mapping(self) -> None:
        _, reason, _ = P("---\n- a\n- b\n---\n正文\n")
        self.assertIn("键值映射", reason)

    def test_empty_body(self) -> None:
        """空正文的 Skill 没有意义——激活了什么都不会发生，只会让用户困惑。"""
        _, reason, _ = P("---\nname: x\n---\n   \n")
        self.assertIn("正文为空", reason)


class BodyPreservationTest(unittest.TestCase):
    def test_body_preserved_verbatim(self) -> None:
        """正文是要发给模型的 SOP，**格式即语义**，缩进与空行必须原样保留。"""
        body = "第一行\n\n    缩进行\n\t制表行\n"
        spec, _, _ = P(f"---\nname: x\n---\n{body}")
        self.assertEqual(spec.body, body)

    def test_leading_blank_lines_before_frontmatter(self) -> None:
        """有些编辑器会在文件开头留空行。"""
        spec, reason, _ = P("\n\n---\nname: x\n---\n正文\n")
        self.assertIsNone(reason)
        self.assertEqual(spec.display_name, "x")


class DescriptionExplicitTest(unittest.TestCase):
    """
    `description_explicit`——解析期派生事实（作者期扩展 F2a）。

    ## 这三条为什么必须存在

    `description` 经解析后**永不为空**（未写时由正文第一段回填，
    再兜底成「（无说明）名字」）。所以体检没法靠「是不是空的」判出
    「作者漏写了说明字段」，只能靠本标记。

    标记只在解析层产生，**出了 parse_skill 就再也算不出来**——
    这三条用例钉住的正是「它有没有被正确赋值」，
    因为漏赋值的后果是**静默漏报**（默认值 True，体检什么都不说）。
    """

    def test_explicit_description_marked_true(self) -> None:
        """作者在 frontmatter 里写了说明 → 真。"""
        spec, _, _ = P("---\ndescription: 我写的说明\n---\n正文第一段\n")
        self.assertEqual(spec.description, "我写的说明")
        self.assertTrue(spec.description_explicit)

    def test_backfilled_description_marked_false(self) -> None:
        """未写说明、由正文第一段回填 → 假。"""
        spec, _, _ = P("---\nname: x\n---\n这是正文第一段\n")
        self.assertEqual(spec.description, "这是正文第一段")
        self.assertFalse(spec.description_explicit)

    def test_no_frontmatter_marked_false(self) -> None:
        """连 frontmatter 都没有 → 同样是回填 → 假。"""
        spec, _, _ = P("只有正文\n")
        self.assertFalse(spec.description_explicit)

    def test_explicit_but_identical_to_first_paragraph_still_true(self) -> None:
        """
        ⚠️ **本条是整组里最重要的一条。**

        作者显式写下的说明**恰好与正文第一段逐字相同**时，标记仍须为真。

        它钉住的是「**不许**用『重新提取一遍正文第一段再比对』来代替这个标记」：
        那种启发式在本场景下会把一份写得完全正确的 Skill 误判成「作者没写说明」，
        然后给出一条毫无意义的建议。

        没有这条用例，将来有人图省事把标记删掉改成比对，**测试会全绿**。
        """
        same = "一模一样的一句话"
        spec, _, _ = P(f"---\ndescription: {same}\n---\n{same}\n")
        self.assertEqual(spec.description, same)
        self.assertTrue(spec.description_explicit)

    def test_blank_description_falls_back_and_marked_false(self) -> None:
        """写了但只有空白 → 视同没写（既有回填逻辑），标记为假。"""
        spec, _, _ = P("---\ndescription: '   '\n---\n正文第一段\n")
        self.assertEqual(spec.description, "正文第一段")
        self.assertFalse(spec.description_explicit)


if __name__ == "__main__":
    unittest.main()
