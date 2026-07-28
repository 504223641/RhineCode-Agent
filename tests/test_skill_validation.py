"""
预授权声明 → 权限规则的翻译（对齐改造后本模块职责整个换掉）。

C11 时它验的是「白名单两段校验与空集降级」——那套语义已移除。
现在的判据全在 `tests/test_perm_turn_grant.py::GrantTranslationTest`，
本文件只留一条「模块仍可导入且不再暴露旧接口」的结构护栏，
避免有人日后又把白名单校验加回来而无人察觉。
"""

from __future__ import annotations

import unittest

import rhinecode.skills.validation as validation


class ObsoleteApiRemovedTest(unittest.TestCase):
    def test_whitelist_checkers_are_gone(self) -> None:
        """
        白名单校验的四个函数必须不存在。

        它们随收窄能力一起移除；留着会让人以为白名单还在起作用。
        """
        for name in (
            "check_builtin_tool_names",
            "collect_exempt_notices",
            "prune_mcp_tool_names",
            "format_fatal_message",
            "lint_skill",
            "lint_skills",
        ):
            with self.subTest(name=name):
                self.assertFalse(
                    hasattr(validation, name), f"{name} 应已随收窄能力一并移除"
                )

    def test_grants_for_is_the_only_entry(self) -> None:
        self.assertTrue(callable(validation.grants_for))


if __name__ == "__main__":
    unittest.main()
