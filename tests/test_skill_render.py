"""
Skill 文本渲染单测（c11 T16）。

覆盖 spec AC6（第一阶段清单只含名字与说明）、AC9 的渲染侧、
AC12（$ARGUMENTS 替换与参数原样保留）、AC13（两种降级可区分）。
"""

import unittest
from pathlib import Path

from rhinecode.skills.models import (
    BODY_MAX_LINES,
    DegradeKind,
    SkillMode,
    SkillSource,
    SkillSpec,
    TOTAL_MAX_LINES,
)
from rhinecode.skills.render import (
    render_active_body,
    render_active_section,
    render_index,
    render_invocation_text,
    render_resources,
    substitute,
)


def _spec(
    name: str = "s",
    description: str = "说明",
    body: str = "SOP 正文",
    mode: SkillMode = SkillMode.SHARED,
    resource_dir: Path = None,
    resource_files: tuple = (),
) -> SkillSpec:
    return SkillSpec(
        name=name,
        description=description,
        body=body,
        mode=mode,
        allowed_tools=None,
        history_messages=0,
        model=None,
        source=SkillSource.USER,
        entry_path=Path("/tmp") / f"{name}.md",
        resource_dir=resource_dir,
        resource_files=resource_files,
    )


class IndexTest(unittest.TestCase):
    """第一阶段清单（AC6）。"""

    def test_contains_name_mode_description_but_no_body(self) -> None:
        """清单含名字/模式/说明，**不含任何 SOP 正文**——这是两阶段加载的定义。"""
        text = render_index(
            [
                _spec("alpha", "第一个", body="绝密正文AAA"),
                _spec("beta", "第二个", body="绝密正文BBB", mode=SkillMode.ISOLATED),
            ]
        )
        self.assertIn("alpha", text)
        self.assertIn("第一个", text)
        self.assertIn("共享", text)
        self.assertIn("beta", text)
        self.assertIn("独立", text)
        self.assertNotIn("绝密正文", text)

    def test_empty_list_returns_empty_string(self) -> None:
        """空列表返回空串，使系统提示槽位整体跳过（N3 零回归的实现基础）。"""
        self.assertEqual(render_index([]), "")

    def test_truncated_with_remaining_count(self) -> None:
        """超上限被截断并标注还剩几个。"""
        many = [_spec(f"s{i:04d}", f"说明{i}") for i in range(400)]
        text = render_index(many)
        self.assertLessEqual(len(text.splitlines()), 210)
        self.assertIn("另有", text)
        self.assertIn("未列出", text)


class SubstituteTest(unittest.TestCase):
    """参数替换（AC12）。"""

    def test_placeholder_replaced(self) -> None:
        self.assertEqual(substitute("请处理 $ARGUMENTS 这件事", "登录超时"),
                         "请处理 登录超时 这件事")

    def test_all_occurrences_replaced(self) -> None:
        """一份 SOP 可能在多个步骤里引用参数，必须全部替换。"""
        out = substitute("先看 $ARGUMENTS，再改 $ARGUMENTS", "X")
        self.assertEqual(out, "先看 X，再改 X")
        self.assertNotIn("$ARGUMENTS", out)

    def test_arguments_preserved_verbatim(self) -> None:
        """参数含空格/引号/管道/反斜杠时原样保留，不做 shell 分词。"""
        raw = '  "a b" | c \\d  '
        out = substitute("参数：$ARGUMENTS", raw)
        self.assertIn(raw, out)

    def test_no_placeholder_with_arguments_appends_section(self) -> None:
        """作者没写占位符不代表用户的输入该被吞掉。"""
        out = substitute("固定流程", "补充说明")
        self.assertIn("固定流程", out)
        self.assertIn("用户补充参数", out)
        self.assertIn("补充说明", out)

    def test_no_placeholder_no_arguments_unchanged(self) -> None:
        self.assertEqual(substitute("固定流程", "   "), "固定流程")


class ResourceTest(unittest.TestCase):
    """目录型资源清单（F13）。"""

    def test_single_file_skill_returns_empty(self) -> None:
        self.assertEqual(render_resources(_spec()), "")

    def test_directory_skill_lists_absolute_path_and_files(self) -> None:
        d = Path("/abs/skills/pack")
        text = render_resources(_spec(resource_dir=d, resource_files=("tpl/a.md", "b.txt")))
        self.assertIn(str(d), text)
        self.assertIn("tpl/a.md", text)
        self.assertIn("b.txt", text)
        # 明确告诉模型别去 glob，否则它会反复尝试然后反复被沙箱拒绝。
        self.assertIn("glob", text)


class ActiveBodyTest(unittest.TestCase):
    """单个激活段与 TRUNCATED 降级（AC13）。"""

    def test_boundary_markers_carry_name(self) -> None:
        text, degrade = render_active_body(_spec("commit", "提交"), "")
        self.assertIsNone(degrade)
        self.assertIn("commit", text)
        self.assertIn("开始", text)
        self.assertIn("结束", text)

    def test_oversized_body_truncated(self) -> None:
        big = "\n".join(f"第 {i} 行" for i in range(BODY_MAX_LINES + 200))
        text, degrade = render_active_body(_spec("big", body=big), "")
        self.assertIs(degrade, DegradeKind.TRUNCATED)
        self.assertIn("已截断", text)
        # 截断后仍要有结束标识，否则会与下一个 Skill 的内容黏在一起。
        self.assertIn("Skill 指令结束：big", text)

    def test_byte_truncation_yields_valid_utf8(self) -> None:
        """字节截断按整行回退，绝不切在多字节字符中间。"""
        big = "\n".join("中文内容测试" * 40 for _ in range(BODY_MAX_LINES + 100))
        text, degrade = render_active_body(_spec("cn", body=big), "")
        self.assertIs(degrade, DegradeKind.TRUNCATED)
        # 能往返编解码即证明没有产生非法 UTF-8。
        self.assertEqual(text.encode("utf-8").decode("utf-8"), text)


class ActiveSectionTest(unittest.TestCase):
    """多个激活段与 DROPPED 降级（AC13）。"""

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(render_active_section([]), ("", {}))

    def test_two_skills_have_distinguishable_boundaries(self) -> None:
        text, degrades = render_active_section(
            [(_spec("aa", "第一"), ""), (_spec("bb", "第二"), "")]
        )
        self.assertEqual(degrades, {})
        self.assertIn("Skill 指令开始：aa", text)
        self.assertIn("Skill 指令结束：aa", text)
        self.assertIn("Skill 指令开始：bb", text)
        # 顺序即激活顺序（F10）。
        self.assertLess(text.index("aa"), text.index("bb"))

    def test_total_overflow_drops_current_and_all_after(self) -> None:
        """
        总量超限 → 当前段整段丢弃，其后一并 DROPPED。

        不「跳过大的塞进小的」：那样结果依赖各 Skill 体积，用户无法预测。
        """
        chunk = "\n".join(f"行{i}" for i in range(TOTAL_MAX_LINES // 2 - 20))
        text, degrades = render_active_section(
            [
                (_spec("one", body=chunk), ""),
                (_spec("two", body=chunk), ""),
                (_spec("three", body=chunk), ""),
                (_spec("four", body="很短"), ""),
            ]
        )
        self.assertNotIn("one", degrades)
        self.assertIs(degrades["three"], DegradeKind.DROPPED)
        # 第四个虽然很短，也不塞进来——顺序语义优先。
        self.assertIs(degrades["four"], DegradeKind.DROPPED)
        self.assertNotIn("Skill 指令开始：four", text)

    def test_two_degrade_kinds_are_distinguishable(self) -> None:
        """
        TRUNCATED 与 DROPPED 必须能分开。

        前者「做了一半」、后者「完全没生效」，后果差一个量级，
        用一个 bool 表示的话 F9 要求的「截断对用户可见」就形同虚设。
        """
        oversized = "\n".join(f"x{i}" for i in range(BODY_MAX_LINES + 50))
        filler = "\n".join(f"y{i}" for i in range(TOTAL_MAX_LINES))
        _, degrades = render_active_section(
            [(_spec("trunc", body=oversized), ""), (_spec("drop", body=filler), "")]
        )
        self.assertIs(degrades["trunc"], DegradeKind.TRUNCATED)
        self.assertIs(degrades["drop"], DegradeKind.DROPPED)


class InvocationTextTest(unittest.TestCase):
    """自包含调用文本（AC24 的文本侧 / F24）。"""

    def test_contains_name_description_and_arguments(self) -> None:
        text = render_invocation_text(_spec("commit", "按项目约定提交"), "修复登录超时")
        self.assertIn("commit", text)
        self.assertIn("按项目约定提交", text)
        self.assertIn("修复登录超时", text)

    def test_no_arguments_still_self_contained(self) -> None:
        """无参时仍含名字与说明——几个月后回看这条历史要能读懂。"""
        text = render_invocation_text(_spec("review", "审查改动"), "")
        self.assertIn("review", text)
        self.assertIn("审查改动", text)
        self.assertIn("无", text)


if __name__ == "__main__":
    unittest.main()
