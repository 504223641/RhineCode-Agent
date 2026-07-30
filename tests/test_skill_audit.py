"""
Skill 体检（作者期扩展）。

覆盖七项检查各自的命中与不命中、边界、以及若干条**反向用例**——
后者钉住的是「这种情况**不该**报」，比正向用例更容易在日后被人「顺手补全」掉。

## 全程不创建任何文件

`audit_skills` 是纯函数、零 IO（spec N1/AC16），定义全部用字符串字面量构造。
这不只是图快：一旦体检需要读文件，它就没法在报告渲染时被随意调用，
「每次现算、不缓存」（F4）也就无从谈起。

**所以本文件里出现 `tempfile` / `mkdir` / `write_text` 都属于走错了方向。**
"""

from __future__ import annotations

import unittest
from pathlib import Path

from rhinecode.skills.audit import audit_skills
from rhinecode.skills.models import (
    BODY_MAX_LINES,
    DESCRIPTION_MAX_CHARS,
    PLACEHOLDER,
    AdviceKind,
    SkillSource,
    SkillSpec,
)


def S(
    name: str = "demo",
    *,
    description: str = "一句话说明这个 Skill 做什么",
    description_explicit: bool = True,
    when_to_use: str | None = "用户说要做某件事时",
    body: str = "按以下步骤做。\n\n## 本次的补充说明\n\n$ARGUMENTS\n",
    granted_tools: tuple[str, ...] = (),
    source: SkillSource = SkillSource.PROJECT,
    resource_dir: Path | None = None,
    resource_files: tuple[str, ...] = (),
) -> SkillSpec:
    """
    构造一份**各方面都合规**的定义，用关键字覆盖单个字段来制造问题。

    缺省值刻意选成「零建议」的形态，这样每条用例只需要说明
    「我改坏了哪一个字段」，读起来就是它要验的那件事。
    """
    return SkillSpec(
        command_name=name,
        display_name=name,
        description=description,
        when_to_use=when_to_use,
        body=body,
        granted_tools=granted_tools,
        forked=False,
        model_invocable=True,
        user_invocable=True,
        model=None,
        source=source,
        entry_path=Path("/x") / f"{name}.md",
        resource_dir=resource_dir,
        resource_files=resource_files,
        description_explicit=description_explicit,
    )


def kinds(advices) -> list[AdviceKind]:
    """把建议列表压成判定类别列表——断言「命中了哪几条」时用。"""
    return [a.kind for a in advices]


class CleanSkillTest(unittest.TestCase):
    def test_compliant_skill_yields_nothing(self) -> None:
        """
        各方面都合规 → 零建议。

        这条是全组的基线：它一旦红了，说明某条检查的缺省判定太松，
        后面所有「不命中」用例的结论都不可信。
        """
        self.assertEqual(audit_skills([S()]), ())

    def test_empty_input(self) -> None:
        self.assertEqual(audit_skills([]), ())


class OrderingTest(unittest.TestCase):
    def test_sorted_by_skill_name(self) -> None:
        """
        建议按 Skill 名排序——报告要稳定，否则每次刷新顺序都在跳。
        """
        advices = audit_skills(
            [
                S("zebra", body="没有占位符的正文\n"),
                S("alpha", body="没有占位符的正文\n"),
            ]
        )
        self.assertEqual([a.skill for a in advices], ["alpha", "zebra"])


class AdviceShapeTest(unittest.TestCase):
    def test_every_advice_is_actionable(self) -> None:
        """
        **每一条建议都必须给出改法**（spec F1）。

        「可操作」是这一类反馈存在的全部理由——一条只陈述问题、不给改法的建议，
        与既有的警告没有区别，那本扩展就白做了。这里对一份**处处有问题**的定义
        逐条检查 `suggestion` 非空。
        """
        broken = S(
            description="说明" * 80,
            description_explicit=False,
            body="正文里没有占位符\n",
            granted_tools=("Bash", "什么都不是的名字"),
        )
        advices = audit_skills([broken], shadowed=[("demo", SkillSource.BUILTIN)])
        self.assertTrue(advices, "这份定义应当触发多条建议")
        for advice in advices:
            with self.subTest(kind=advice.kind):
                self.assertTrue(advice.finding.strip(), "finding 不能为空")
                self.assertTrue(advice.suggestion.strip(), "suggestion 不能为空")
                self.assertEqual(advice.skill, "demo")


class DescriptionLengthTest(unittest.TestCase):
    """检查 1：说明字段过长。"""

    def test_too_long_hits(self) -> None:
        advices = audit_skills([S(description="说" * (DESCRIPTION_MAX_CHARS + 1))])
        self.assertEqual(kinds(advices), [AdviceKind.DESCRIPTION_TOO_LONG])

    def test_exactly_at_limit_does_not_hit(self) -> None:
        """边界：恰好等于阈值不报。阈值是「超过才报」，不是「达到就报」。"""
        self.assertEqual(audit_skills([S(description="说" * DESCRIPTION_MAX_CHARS)]), ())

    def test_advice_says_moving_does_not_save_index_budget(self) -> None:
        """
        ⚠️ 建议必须说明「挪进 when_to_use 省不下清单预算」。

        直觉会以为拆一半到 when_to_use 能减轻清单负担，实际上两者在第一阶段
        清单里拼成**同一行**、共享同一份预算，挪动只省下四个字符的分隔符。
        不写清这点，用户按建议改完发现预算没变，就不会再信任后面的建议。
        """
        advice = audit_skills([S(description="说" * 200)])[0]
        self.assertIn("when_to_use", advice.suggestion)
        self.assertIn("不会省下", advice.suggestion)

    def test_counts_characters_not_bytes(self) -> None:
        """
        按**字符数**计，不按字节数。

        中文一个字符占三字节——按字节算的话，一句正常长度的中文说明会比
        同样信息量的英文早三倍命中，而这个阈值是为了「别把列表撑成多行」，
        那按显示宽度算，与编码无关。
        """
        # 100 个中文字符 = 300 字节，若按字节判必然命中；按字符判恰好不命中。
        self.assertEqual(audit_skills([S(description="中" * 100)]), ())


class MissingDescriptionTest(unittest.TestCase):
    """检查 2：声明了触发说明却没写说明字段。"""

    def test_when_to_use_without_explicit_description_hits(self) -> None:
        advices = audit_skills(
            [S(when_to_use="用户说要提交时", description_explicit=False)]
        )
        self.assertEqual(kinds(advices), [AdviceKind.MISSING_DESCRIPTION])

    def test_explicit_description_does_not_hit(self) -> None:
        self.assertEqual(
            audit_skills([S(when_to_use="用户说要提交时", description_explicit=True)]),
            (),
        )

    def test_no_frontmatter_style_skill_yields_nothing(self) -> None:
        """
        ⚠️ **反向用例，钉住「刻意不查说明是自动提取的」**（spec AC2b）。

        一份只有正文、没有 frontmatter 的 Markdown 也是合法 Skill——
        那是对齐改造**刻意支持**的用法。它的说明必然是自动提取的、也没有
        when_to_use，此时**不该有任何建议**。

        没有这条，将来有人会觉得「说明是自动提取的也该提醒一句」而把它加回来，
        然后几乎每个简易 Skill 都开始报建议，把真正要紧的几条淹掉。
        """
        self.assertEqual(
            audit_skills([S(when_to_use=None, description_explicit=False)]),
            (),
        )


class TriggerHintTest(unittest.TestCase):
    """
    检查 3：`description` 缺触发线索（**弱提示**，R5）。

    这条来自用户报的真实场景：他有个前端设计 Skill，请求只说
    「帮我创建个前端页面」、没点名 Skill，`load_skill` 就很难触发。
    根因之一是 description 只写了「做什么」，模型据此想不到该加载。

    Anthropic 官方 skill-creator 的指导是描述要写得「有点 pushy」，
    因为「Claude 有可测量的欠触发倾向」——所以这条值得报。
    """

    def test_pure_what_it_does_hits(self) -> None:
        """只说「做什么」、且没声明 when_to_use → 命中。"""
        advices = audit_skills([S(description="生成一个前端页面", when_to_use=None)])
        self.assertEqual(kinds(advices), [AdviceKind.DESCRIPTION_LACKS_TRIGGER])

    def test_trigger_words_in_description_does_not_hit(self) -> None:
        """description 自带时机线索 → 不命中（这正是我们希望作者写的形态）。"""
        self.assertEqual(
            audit_skills(
                [S(description="做前端页面。用户说「写个页面」「做个前端」时用", when_to_use=None)]
            ),
            (),
        )

    def test_declared_when_to_use_suppresses_it(self) -> None:
        """
        ⚠️ **命中条件刻意收窄**：声明了 `when_to_use` 就不报，
        哪怕 description 本身一个线索词都没有。

        作者写了那个字段说明他已经想过触发问题，再唠叨一句只是噪音——
        而这条本身是启发式、有误报，噪音的代价比漏报更高。
        """
        self.assertEqual(
            audit_skills([S(description="生成一个前端页面", when_to_use="用户要做页面时")]),
            (),
        )

    def test_auto_extracted_description_does_not_hit(self) -> None:
        """
        ⚠️ **本条是设计冲突留下的疤，不要「顺手」放宽它。**

        作者压根没写 description（由正文第一段回填）时**不报**。

        本检查一上线就与 AC2b 撞了：无 frontmatter 的 Skill 既没有 when_to_use、
        其自动回填的说明也几乎不含时机词，于是**必然命中**——等于把早先因为
        「几乎每个简易 Skill 都会中」而砍掉的那条检查换个名字放回来。

        收窄之后本检查只针对「作者认真写了 description 却写成纯『做什么』式」，
        那才是会照建议改的人。
        """
        self.assertEqual(
            audit_skills(
                [S(description="这是正文第一段", when_to_use=None, description_explicit=False)]
            ),
            (),
        )

    def test_english_trigger_phrasing_does_not_hit(self) -> None:
        """外部 Skill 常见的英文写法也要认，否则一导入就一堆误报。"""
        self.assertEqual(
            audit_skills(
                [S(description="Build a frontend page. Use when the user asks for a page.",
                   when_to_use=None)]
            ),
            (),
        )

    def test_advice_declares_itself_weak(self) -> None:
        """
        ⚠️ 措辞必须**自称弱提示并明说可忽略**。

        一条不确定的建议若语气跟确定的一样硬，用户会开始不信**全部**建议——
        那比不给这条建议损失更大。
        """
        advice = audit_skills([S(description="生成一个前端页面", when_to_use=None)])[0]
        self.assertIn("弱提示", advice.suggestion)
        self.assertIn("忽略本条", advice.suggestion)

    def test_advice_says_put_triggers_in_description(self) -> None:
        """
        建议要说清「触发词放 description 而不是只放 when_to_use」——
        后者不是开放标准字段，别的 Agent 工具只读 description。
        """
        advice = audit_skills([S(description="生成一个前端页面", when_to_use=None)])[0]
        self.assertIn("只读 description", advice.suggestion)


class PlaceholderTest(unittest.TestCase):
    """检查 3：正文不含参数占位符。"""

    def test_missing_placeholder_hits(self) -> None:
        advices = audit_skills([S(body="第一步做这个。\n第二步做那个。\n")])
        self.assertEqual(kinds(advices), [AdviceKind.NO_PLACEHOLDER])

    def test_present_placeholder_does_not_hit(self) -> None:
        self.assertEqual(audit_skills([S(body=f"做事。\n{PLACEHOLDER}\n")]), ())

    def test_advice_says_arguments_are_not_dropped(self) -> None:
        """
        ⚠️ 建议必须写明「参数不会丢」。

        没有占位符时参数会被追加到正文末尾的「用户补充参数」段——它**没有被
        丢弃**，只是位置固定在最后。不写这点的话，用户会把「位置不对」误读成
        「功能失效」，然后去排查一个根本不存在的 bug。
        """
        advice = audit_skills([S(body="没有占位符\n")])[0]
        self.assertIn("不会丢", advice.suggestion)


class BroadGrantTest(unittest.TestCase):
    """检查 4：预授权过宽。"""

    def test_bare_side_effect_tool_hits(self) -> None:
        advices = audit_skills([S(granted_tools=("Bash",))])
        self.assertEqual(kinds(advices), [AdviceKind.BROAD_GRANT])

    def test_specific_pattern_does_not_hit(self) -> None:
        self.assertEqual(audit_skills([S(granted_tools=("Bash(git status *)",))]), ())

    def test_bare_read_only_tool_does_not_hit(self) -> None:
        """
        ⚠️ **反向用例**：只读类别不带模式**不报**（spec AC4）。

        它没有副作用，全放开也无所谓——而三个既有内置样板都裸写了
        `Read` / `Grep` / `Glob`（后两者经别名表都映射到 `Read`），
        这条一旦反了，内置样板立刻集体触发建议。
        """
        self.assertEqual(audit_skills([S(granted_tools=("Read", "Grep", "Glob"))]), ())

    def test_pure_wildcard_pattern_hits(self) -> None:
        """
        ⚠️ **纯通配必须算过宽**。

        `Bash(*)` 与裸写 `Bash` 效果逐字相同，但它「带了模式」，
        朴素的「无模式即过宽」判定会整个放过它。

        而这类**更该报**：裸写是「我知道我全放开了」，
        纯通配是「我以为我收窄了」。
        """
        advices = audit_skills([S(granted_tools=("Bash(*)",))])
        self.assertEqual(kinds(advices), [AdviceKind.BROAD_GRANT])

    def test_pure_wildcard_domain_hits(self) -> None:
        """`WebFetch(domain:*)` 同理——它对任何域名都匹配。"""
        advices = audit_skills([S(granted_tools=("WebFetch(domain:*)",))])
        self.assertEqual(kinds(advices), [AdviceKind.BROAD_GRANT])

    def test_specific_domain_does_not_hit(self) -> None:
        self.assertEqual(
            audit_skills([S(granted_tools=("WebFetch(domain:github.com)",))]), ()
        )

    def test_advice_warns_that_swapping_category_is_not_narrowing(self) -> None:
        """
        ⚠️ **本条来自真实模型实测。**

        建议原本只说「加一个具体的参数模式收窄」。真实模型读到「收窄」二字后，
        把裸写的 `Write` 改成了裸写的 `Edit`，并在汇总里自评
        「✅ 改已有文件才免确认」——它以为换了个更窄的类别就算收窄了。
        而裸写 `Edit` 仍然是「全部编辑免确认」，于是同一条建议换个工具名又冒出来。

        收窄的**唯一**手段是括号里的参数模式，这一点必须写进建议正文，
        不能指望读的人自己想到。
        """
        advice = audit_skills([S(granted_tools=("Write",))])[0]
        self.assertIn("换成另一个工具类别不算收窄", advice.suggestion)

    def test_bare_edit_is_also_broad(self) -> None:
        """把 `Write` 换成 `Edit` 之后仍然命中——这是上面那条实测的直接形态。"""
        self.assertEqual(
            kinds(audit_skills([S(granted_tools=("Edit",))])),
            [AdviceKind.BROAD_GRANT],
        )

    def test_bare_webfetch_hits(self) -> None:
        """
        裸写 `WebFetch` 是最宽的一种——它连「放行档对网络不生效」那道降级
        都绕过了（预授权在第③层，那道降级在第④层，够不到）。
        """
        advices = audit_skills([S(granted_tools=("WebFetch",))])
        self.assertEqual(kinds(advices), [AdviceKind.BROAD_GRANT])


class McpGrantTest(unittest.TestCase):
    """
    检查 4 的**远端工具例外**。

    ## 为什么远端工具必须另判

    权限引擎对 MCP 走 `other` 分支，命中条件是 `rule.pattern == ""`
    再对工具名做 fnmatch。也就是说 MCP 规则**必须无参数模式才可能生效**。

    若套用非 MCP 那支的判定，两个后果：

    1. **每一条** MCP 预授权都被判成过宽（它们按设计就必须无模式）；
    2. 更糟——建议给的改法「加个参数模式」会让那条规则**永远匹配不到任何请求**，
       预授权无声消失，而「全军覆没」也不会报（规则本身仍解析成功）。

    第 2 条是**建议本身把用户配置改坏**，比误报严重得多，且坏得很安静。
    """

    def test_specific_mcp_tool_does_not_hit(self) -> None:
        """具体的远端工具名是能写的**最精确**的声明——朴素判定会把它判成最宽的。"""
        self.assertEqual(audit_skills([S(granted_tools=("mcp__github__search",))]), ())

    def test_wildcard_mcp_tool_hits(self) -> None:
        """带通配符才是真正的整台服务器放行。"""
        advices = audit_skills([S(granted_tools=("mcp__github__*",))])
        self.assertEqual(kinds(advices), [AdviceKind.BROAD_GRANT])

    def test_mcp_advice_warns_against_adding_pattern(self) -> None:
        """
        ⚠️ 远端工具的建议**不能**说「加个参数模式」——那会让预授权失效。
        它必须反过来提醒用户别加。
        """
        advice = audit_skills([S(granted_tools=("mcp__github__*",))])[0]
        self.assertIn("不要加参数模式", advice.suggestion)


class GrantsAllDroppedTest(unittest.TestCase):
    """检查 5：预授权一条都没生效。"""

    def test_all_unrecognized_hits(self) -> None:
        advices = audit_skills([S(granted_tools=("Task", "TodoWrite"))])
        self.assertEqual(kinds(advices), [AdviceKind.GRANTS_ALL_DROPPED])

    def test_partially_recognized_does_not_hit(self) -> None:
        """
        ⚠️ **反向用例**：部分认得、部分认不出时**不报本条**（spec AC5）。

        单条认不出已经由预授权翻译层产出警告了。本条报的是**整体误解**——
        一条都没生效，说明作者对这个字段的写法有根本性的误会。
        部分生效时再报一次只是噪音。
        """
        advices = audit_skills([S(granted_tools=("Read", "Task"))])
        self.assertNotIn(AdviceKind.GRANTS_ALL_DROPPED, kinds(advices))

    def test_no_declaration_does_not_hit(self) -> None:
        """压根没声明 ≠ 声明了但没生效。"""
        self.assertEqual(audit_skills([S(granted_tools=())]), ())

    def test_broken_domain_rule_hits(self) -> None:
        """
        漏写 `domain:` 前缀的域名规则会被静默丢弃——这正是本扩展立项
        要消灭的那类「无声失效」，体检必须报出来。
        """
        advices = audit_skills([S(granted_tools=("WebFetch(github.com)",))])
        self.assertEqual(kinds(advices), [AdviceKind.GRANTS_ALL_DROPPED])
        self.assertIn("domain:", advices[0].suggestion)


class InjectionSizeTest(unittest.TestCase):
    """检查 6：逼近/超过注入上限。"""

    def test_small_body_does_not_hit(self) -> None:
        self.assertEqual(audit_skills([S()]), ())

    def test_near_limit_hits(self) -> None:
        body = ("一行正文\n" * int(BODY_MAX_LINES * 0.85)) + PLACEHOLDER
        advices = audit_skills([S(body=body)])
        self.assertIn(AdviceKind.NEAR_INJECTION_LIMIT, kinds(advices))

    def test_over_limit_says_already_truncated(self) -> None:
        """已经超限与仅仅逼近，措辞必须区分——前者是「现在就在丢内容」。"""
        body = ("一行正文\n" * (BODY_MAX_LINES * 2)) + PLACEHOLDER
        advice = [
            a
            for a in audit_skills([S(body=body)])
            if a.kind is AdviceKind.NEAR_INJECTION_LIMIT
        ][0]
        self.assertIn("已超过上限", advice.finding)

    def test_resource_list_counts_toward_the_limit(self) -> None:
        """
        ⚠️ **量的是渲染后的整段，不是正文原文**（spec AC6）。

        一份正文本身未达阈值、但带了一长串随附资源清单的目录型 Skill，
        实际注入时照样会被截断。按正文原文判会漏报这一整类。
        """
        # 正文体积选在阈值之下、但离得不远：单独看不命中，
        # 加上一份满额的随附资源清单就越线。这正是要暴露的那种情形。
        body = ("一行正文\n" * int(BODY_MAX_LINES * 0.7)) + PLACEHOLDER
        plain = S(body=body)
        self.assertEqual(audit_skills([plain]), (), "正文本身不该命中")

        with_resources = S(
            body=body,
            resource_dir=Path("/x/demo"),
            resource_files=tuple(f"参考资料/第{i}份文档.md" for i in range(50)),
        )
        self.assertIn(
            AdviceKind.NEAR_INJECTION_LIMIT,
            kinds(audit_skills([with_resources])),
            "加上资源清单后应当命中",
        )


class OverridesBuiltinTest(unittest.TestCase):
    """检查 7：覆盖了内置样板。"""

    def test_shadowing_builtin_hits(self) -> None:
        advices = audit_skills(
            [S("commit", source=SkillSource.PROJECT)],
            shadowed=[("commit", SkillSource.BUILTIN)],
        )
        self.assertEqual(kinds(advices), [AdviceKind.OVERRIDES_BUILTIN])

    def test_no_shadowing_does_not_hit(self) -> None:
        self.assertEqual(audit_skills([S("commit")], shadowed=[]), ())

    def test_project_over_user_does_not_hit(self) -> None:
        """
        ⚠️ **反向用例**：项目级盖用户级**不报**（spec AC6a）。

        用户级的 Skill 是用户自己放进去的，撞掉了他心里有数；
        内置样板才是他从没主动装过、却确实存在的东西，最容易被无意撞掉。
        """
        self.assertEqual(
            audit_skills([S("s")], shadowed=[("s", SkillSource.USER)]),
            (),
        )

    def test_advice_is_neutral(self) -> None:
        """
        措辞必须中性——覆盖是三层设计的**正常用法**，这条命中的多数情况
        没有任何问题。不写「若是有意定制请忽略」的话，它读起来就像在指责用户。
        """
        advice = audit_skills(
            [S("commit")], shadowed=[("commit", SkillSource.BUILTIN)]
        )[0]
        self.assertIn("有意定制", advice.suggestion)

    def test_shadowed_defaults_to_empty(self) -> None:
        """不传覆盖事实时本项恒不触发——这是正确的，不是漏检。"""
        self.assertEqual(audit_skills([S("commit")]), ())


class PurityTest(unittest.TestCase):
    """spec N1/AC16：纯函数、零 IO。"""

    def test_entry_path_need_not_exist(self) -> None:
        """
        定义里的路径**指向一个不存在的文件**，体检照样跑得通——
        这是「零 IO」最直接的证据：它一次都没去碰文件系统。
        """
        spec = S(body="没有占位符\n")
        self.assertFalse(spec.entry_path.exists())
        self.assertEqual(kinds(audit_skills([spec])), [AdviceKind.NO_PLACEHOLDER])

    def test_repeated_calls_are_identical(self) -> None:
        """同样输入永远同样输出——没有任何隐藏状态。"""
        spec = S(granted_tools=("Bash",), body="没有占位符\n")
        self.assertEqual(audit_skills([spec]), audit_skills([spec]))


if __name__ == "__main__":
    unittest.main()
