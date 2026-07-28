"""
Skill 作者期体检（lint）。

## 这组测试在防什么

C11 原本只把 Skill 当成「已经写好的东西」来消费，从不告诉作者写得对不对。
而四类写法问题**一个都不报错**，全是静默降级——把一份外部 Skill 原样搬进来
跑真实模型，四条全中：

1. description 218 字符 → `/skills` 列表被撑成三行（其余三条各一行），
   且它每次执行都被塞进调用文本；
2. 正文无 `$ARGUMENTS` → 参数被追加到末尾，作者以为没生效；
3. `allowed_tools` 被一路加到覆盖全部工具 → 等于没收窄；
4. 未声明白名单 → 不收窄（可能有意，故只告知）。

## 判据取向

**每条警告都必须带可操作的建议**，所以断言的不只是「报了」，还有「说清了怎么改」。
只说「description 太长」而不说「它会被塞进每次调用文本」，作者不会知道为什么该改。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from rhinecode.skills.models import PLACEHOLDER, SkillMode, SkillSource, SkillSpec
from rhinecode.skills.validation import (
    DESCRIPTION_MAX_CHARS,
    lint_skill,
    lint_skills,
)


ALL_TOOLS = frozenset(
    {"read_file", "write_file", "edit_file", "glob_files", "grep_content", "run_command"}
)


def _spec(
    name: str = "demo",
    description: str = "一句话说明",
    body: str = f"做点什么。\n\n{PLACEHOLDER}",
    allowed_tools=("read_file",),
) -> SkillSpec:
    """造一份**默认全部合格**的 Skill，各用例只改自己关心的那一项。"""
    return SkillSpec(
        name=name,
        description=description,
        body=body,
        mode=SkillMode.SHARED,
        allowed_tools=tuple(allowed_tools) if allowed_tools is not None else None,
        history_messages=0,
        model=None,
        source=SkillSource.PROJECT,
        entry_path=Path(f"/x/{name}.md"),
        resource_dir=None,
        resource_files=(),
    )


class CleanSkillTest(unittest.TestCase):
    def test_well_formed_skill_produces_nothing(self) -> None:
        """
        合格的 Skill 一条建议都不该有。

        这条是**噪音护栏**：体检只要对正常 Skill 也唠叨，用户三天就会开始无视整段。
        """
        self.assertEqual(lint_skill(_spec(), ALL_TOOLS), [])


class DescriptionLengthTest(unittest.TestCase):
    def test_long_description_is_flagged_with_reason(self) -> None:
        long = "x" * (DESCRIPTION_MAX_CHARS + 1)
        (msg,) = lint_skill(_spec(description=long), ALL_TOOLS)
        self.assertIn("demo", msg)
        self.assertIn(str(len(long)), msg)          # 说清实际多长
        self.assertIn("调用文本", msg)               # 说清为什么该改
        self.assertIn("一句话", msg)                 # 说清怎么改

    def test_boundary_is_not_flagged(self) -> None:
        """恰好等于上限不算超——边界宽一格，避免为一个字符唠叨。"""
        self.assertEqual(lint_skill(_spec(description="x" * DESCRIPTION_MAX_CHARS), ALL_TOOLS), [])


class PlaceholderTest(unittest.TestCase):
    def test_missing_placeholder_is_flagged(self) -> None:
        (msg,) = lint_skill(_spec(body="没有占位符的正文"), ALL_TOOLS)
        self.assertIn(PLACEHOLDER, msg)
        self.assertIn("追加到正文末尾", msg)   # 说清实际会发生什么
        self.assertNotIn("失败", msg)          # 这不是错误，措辞不能像错误

    def test_placeholder_anywhere_counts(self) -> None:
        """占位符在正文中间也算数，不要求它在末尾。"""
        self.assertEqual(lint_skill(_spec(body=f"前言\n{PLACEHOLDER}\n后记"), ALL_TOOLS), [])


class WhitelistTest(unittest.TestCase):
    def test_missing_whitelist_is_informational(self) -> None:
        """未声明白名单只是告知——很多 Skill 确实不需要收窄。"""
        (msg,) = lint_skill(_spec(allowed_tools=None), ALL_TOOLS)
        self.assertIn("不收窄", msg)
        self.assertIn("有意为之可忽略", msg)

    def test_whitelist_covering_everything_is_flagged(self) -> None:
        """
        白名单覆盖全部已注册工具 → 等于没写。

        这正是那份外部 Skill 的实际形态（被一路加到 6 个 = 全部非系统工具）。
        """
        (msg,) = lint_skill(_spec(allowed_tools=sorted(ALL_TOOLS)), ALL_TOOLS)
        self.assertIn("等于没有收窄", msg)
        self.assertIn("删掉", msg)

    def test_superset_also_flagged(self) -> None:
        """
        白名单是全集的**超集**（含尚未连接的 mcp__ 项）同样算覆盖。

        判据用 `>=` 而不是 `==` 就是为了这个：那些 mcp__ 项此刻不在 registered 里，
        但白名单确实已经覆盖了全部**可用**工具。
        """
        tools = sorted(ALL_TOOLS) + ["mcp__x__y"]
        msgs = lint_skill(_spec(allowed_tools=tools), ALL_TOOLS)
        self.assertTrue(any("等于没有收窄" in m for m in msgs))

    def test_narrow_whitelist_is_clean(self) -> None:
        self.assertEqual(lint_skill(_spec(allowed_tools=("read_file", "glob_files")), ALL_TOOLS), [])

    def test_empty_registered_skips_the_check(self) -> None:
        """
        `registered` 为空（调用方没传）时跳过这条检查，**不能误报**。

        `report()` 的 registered 有缺省空值，误报会让一堆既有调用点凭空多出警告。
        """
        msgs = lint_skill(_spec(allowed_tools=sorted(ALL_TOOLS)), frozenset())
        self.assertEqual(msgs, [])


class RealWorldSampleTest(unittest.TestCase):
    """用**那份真实的外部 Skill** 的形态跑一遍——四条里应当命中三条。"""

    def test_foreign_skill_shape(self) -> None:
        foreign = _spec(
            name="frontend-design",
            description=(
                "Guidance for distinctive, intentional visual design when building new UI "
                "or reshaping an existing one. Helps with aesthetic direction, typography, "
                "and making choices that don't read as templated defaults."
            ),
            body="# Frontend Design\n\n（一大段英文 SOP，没有占位符）",
            allowed_tools=sorted(ALL_TOOLS),
        )
        msgs = lint_skill(foreign, ALL_TOOLS)
        self.assertEqual(len(msgs), 3, f"预期命中三条，实际：{msgs}")
        joined = "\n".join(msgs)
        self.assertIn("description", joined)
        self.assertIn(PLACEHOLDER, joined)
        self.assertIn("等于没有收窄", joined)


class BatchTest(unittest.TestCase):
    def test_lint_skills_preserves_order(self) -> None:
        """批量体检按 Skill 顺序拼平，便于用户对照列表逐条改。"""
        a = _spec(name="a", description="x" * 200)
        b = _spec(name="b", body="无占位符")
        msgs = lint_skills([a, b], ALL_TOOLS)
        self.assertEqual(len(msgs), 2)
        self.assertIn("`a`", msgs[0])
        self.assertIn("`b`", msgs[1])

    def test_empty_input(self) -> None:
        self.assertEqual(lint_skills([], ALL_TOOLS), [])


if __name__ == "__main__":
    unittest.main()
