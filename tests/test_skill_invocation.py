"""
「在哪执行」与「谁能触发」两个正交维度（对齐改造 F8）。

## C11 把两件事捆在一起了

C11 的 `mode: isolated` 同时表达了「开子对话」与「只能由用户触发」——
模型调加载工具去发起一个 isolated Skill 会被硬编码地拒绝。

标准把它们拆成两个独立字段：`context: fork` 管在哪执行，
`disable-model-invocation` 管谁能触发。一份 CC 的 fork Skill 只要没写
`disable-model-invocation: true`，模型就能自行发起——搬到 RhineCode 若仍被
硬编码拒绝，行为与作者预期不符。

## 本模块验四件事

1. 缺省下模型可自行发起 fork Skill，结论作为工具结果回灌；
2. `disable-model-invocation: true` 挡下模型，但用户仍可触发；
3. `user-invocable: false` 不进命令菜单，但模型仍可发起；
4. 子对话里加载工具不可见，硬造调用会被拒（防嵌套）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rhinecode.skills.manager import SkillManager
from rhinecode.skills.models import ActivationStatus
from rhinecode.tools.load_skill import LoadSkillTool


def _write(root: Path, name: str, frontmatter: str, body: str = "SOP 正文。") -> None:
    """在项目级目录写一个单文件型 Skill，命令名即文件名。"""
    d = root / ".rhinecode" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _manager(self) -> SkillManager:
        m = SkillManager(
            project_root=self.root,
            user_dir=self.root / "user",
            builtin_dir=None,
            has_short_command=lambda n: True,
        )
        m.startup()
        return m


class ModelInvokesForkTest(_Base):
    def test_fork_is_model_invocable_by_default(self) -> None:
        """
        缺省下 `context: fork` **不**隐含「只能由用户触发」。

        这是与 C11 的行为差异：那时同一份定义会被拒绝。
        """
        _write(self.root, "review", "context: fork")
        m = self._manager()
        result = m.activate("review", "")
        self.assertIs(result.status, ActivationStatus.FORKED)

    def test_tool_returns_conclusion_as_result(self) -> None:
        """
        模型发起 fork 时，工具结果是**子对话跑出的结论正文**，
        而不是「已激活」那一句确认。

        理由：子对话的过程完全不进主历史，结论就是这次动作的唯一产出，
        不回灌等于白跑。
        """
        _write(self.root, "review", "context: fork")
        tool = LoadSkillTool(self._manager())
        tool.run_fork = lambda name, args: f"结论：{name} 看完了，没问题。"

        res = tool.execute({"name": "review", "arguments": "看看改动"})
        self.assertTrue(res.ok)
        self.assertEqual(res.output, "结论：review 看完了，没问题。")

    def test_missing_callback_says_so_instead_of_pretending(self) -> None:
        """
        没注入回调时明确说明，**不假装成功**。

        静默降级会让模型以为 Skill 跑过了，然后基于一个不存在的结论继续往下做。
        """
        _write(self.root, "review", "context: fork")
        tool = LoadSkillTool(self._manager())  # run_fork 保持 None
        res = tool.execute({"name": "review"})
        self.assertFalse(res.ok)
        self.assertIn("不支持", res.output)


class DisableModelInvocationTest(_Base):
    def test_model_is_refused_with_a_usable_entry(self) -> None:
        """挡下模型时必须给出用户**实际可执行**的入口，而不是只说一句不行。"""
        _write(self.root, "deploy", "disable-model-invocation: true")
        tool = LoadSkillTool(self._manager())
        res = tool.execute({"name": "deploy"})
        self.assertFalse(res.ok)
        self.assertIn("deploy", res.output)
        self.assertIn("/", res.output, "应包含一条可执行的命令")

    def test_user_path_still_works(self) -> None:
        """
        `disable-model-invocation` 只挡模型，**挡不到用户**。

        两个维度正交：它管「谁能触发」，与「用户能不能用斜杠命令」无关。
        """
        _write(self.root, "deploy", "disable-model-invocation: true")
        m = self._manager()
        # 用户路径不经 activate 的 model_invocable 判定——短命令仍然注册
        self.assertIn("deploy", [i.name for i in m.command_infos()])

    def test_hyphen_and_underscore_both_recognised(self) -> None:
        _write(self.root, "a", "disable-model-invocation: true")
        _write(self.root, "b", "disable_model_invocation: true")
        m = self._manager()
        self.assertFalse(m.get("a").model_invocable)
        self.assertFalse(m.get("b").model_invocable)


class UserInvocableTest(_Base):
    def test_hidden_from_command_menu_but_model_can_still_invoke(self) -> None:
        """
        `user-invocable: false` 只影响斜杠命令菜单。

        它仍然出现在 `/skills` 列表与第一阶段清单里——**不进菜单不等于不存在**，
        这类 Skill 的用途正是「背景知识，由模型按需加载」。
        """
        _write(self.root, "houserules", "user-invocable: false")
        m = self._manager()
        self.assertEqual([i.name for i in m.command_infos()], [])
        self.assertIsNotNone(m.get("houserules"), "仍应能按名取到")
        self.assertIn("houserules", m.index_text(), "仍应出现在第一阶段清单里")

        result = m.activate("houserules", "")
        self.assertIs(result.status, ActivationStatus.ACTIVATED)


class ForkNestingGuardTest(_Base):
    def test_excluded_set_contains_the_loader(self) -> None:
        """
        子对话的排除集含加载工具——这是防「Skill 里再激活 Skill」的第一道。

        第二道在循环里（调用了被排除的工具就拒绝），两者缺一防线都不成立：
        只排除不拒绝的话，模型凭训练先验硬造出的调用会照常执行。
        """
        m = self._manager()
        self.assertIn("load_skill", m.fork_excluded_tools())


if __name__ == "__main__":
    unittest.main()
