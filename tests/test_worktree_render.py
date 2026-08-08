"""
隔离工作区交付信息段的渲染（c14 F17）。

## 为什么这一段值得单独一组用例

它是**系统**追加的，不是模型自述的——主 Agent 后续的 `git merge` / `git diff`
全靠这几行。模型写错的分支名会被它纠正（F17 的全部用意），所以它的措辞既是
给人看的，也是给下一轮的模型看的**指令**。

本文件里「一个提交都没有」那组用例来自真实模型实测，见 `NoCommitWarningTest`。
"""

import unittest
from pathlib import Path

from rhinecode.worktree.models import ChangeStatus, WorktreeHandle
from rhinecode.worktree.render import render_delivery


def _handle(name="worker-a1b2") -> WorktreeHandle:
    return WorktreeHandle(
        name=name,
        path=Path("/proj/.rhinecode/worktrees") / name,
        branch=f"agent/{name}",
        base_commit="9c6df51",
    )


def _status(*, dirty=False, commits=0, files=()) -> ChangeStatus:
    """`untouched` 是派生属性，不传——它由 dirty/commits 算出来。"""
    return ChangeStatus(dirty=dirty, commits=commits, files=tuple(files))


class RemovedFormTest(unittest.TestCase):
    """已回收时**不给分支名**——给了会让主 Agent 去 merge 一个不存在的分支。"""

    def test_no_branch_name_when_removed(self):
        text = render_delivery(_handle(), _status(), Path("/proj"), removed=True)
        self.assertNotIn("agent/worker-a1b2", text)
        self.assertIn("已自动清理", text)


class KeptFormTest(unittest.TestCase):
    """保留时给出 merge 所需的一切。"""

    def test_gives_branch_base_and_counts(self):
        text = render_delivery(
            _handle(), _status(commits=2, files=("a.py",)), Path("/proj")
        )
        self.assertIn("agent/worker-a1b2", text)
        self.assertIn("9c6df51", text)
        self.assertIn("提交：2 个", text)
        self.assertIn("a.py", text)


class NoCommitWarningTest(unittest.TestCase):
    """
    **「改了但一个提交都没有」必须说重话。** 这组来自真实模型实测。

    实测两次撞到同一条路：主 Agent 在任务描述里写下「不要执行 git commit」
    （从用户那句「我这边的改动先不提交」推断而来，很自然），子 Agent 照办，
    成果全部搁浅在工作区目录里、分支上 0 个提交。

    原先这一段只**陈述事实**（「未提交改动：有」），主 Agent 的反应是把它当成
    正常结局报给用户「已完成，未提交，符合要求」；第二次它自己钻进工作区目录
    逐个读 diff、再在主目录重打一遍——隔离本该省下的上下文全吃回来了。

    所以判据不是「有没有提到未提交」，而是**有没有给出结论与下一步**。
    """

    def _text(self):
        return render_delivery(
            _handle(), _status(dirty=True, commits=0, files=("a.py",)), Path("/proj")
        )

    def test_says_the_result_did_not_come_back(self):
        self.assertIn("没有交回来", self._text())

    def test_tells_it_not_to_copy_files_out(self):
        """自己进目录抄文件是**最贵**的那条错路：一次委派的价值当场归零。"""
        text = self._text()
        self.assertIn("不要自己进那个目录", text)

    def test_tells_it_not_to_forbid_commit_next_time(self):
        """根因在任务描述上，所以纠正也要落在任务描述上。"""
        self.assertIn("不要写「不要提交」", self._text())

    def test_silent_when_there_are_commits(self):
        """
        反证：有提交时**不能**冒出这段警告。

        「改了、也提交了、还剩一点未提交」是完全正常的结局（实测里
        `__pycache__` 就会让它恒为 dirty）。在那种情况下喊「成果没交回来」
        会把主 Agent 推去做多余的补救。
        """
        text = render_delivery(
            _handle(), _status(dirty=True, commits=3, files=("a.py",)), Path("/proj")
        )
        self.assertNotIn("没有交回来", text)

    def test_silent_when_clean_and_no_commits(self):
        """反证：什么都没改时也不该喊——那条路走的是 removed 形态。"""
        text = render_delivery(_handle(), _status(), Path("/proj"))
        self.assertNotIn("没有交回来", text)


class ToolDescriptionSameVoiceTest(unittest.TestCase):
    """
    ⚠ 成对维护点：交付信息段的这条纠正，与委派工具描述里的必须同口径。

    它们是主 Agent 在**两个不同时刻**读到的同一条约束——委派前读工具描述、
    委派后读交付信息。一处强一处弱等于白改：模型会按弱的那份行事。
    这与 C11 的「Skill 清单表头 ↔ load_skill.description」、C13 的
    「角色清单表头 ↔ run_agent.description」是同一个坑的第三次。
    """

    def test_tool_description_forbids_no_commit_instruction(self):
        from rhinecode.tools.run_agent import RunAgentTool

        self.assertIn("不要写「不要提交」", RunAgentTool.description)

    def test_tool_description_forbids_copying_out_of_the_worktree(self):
        from rhinecode.tools.run_agent import RunAgentTool

        self.assertIn("不要自己钻进那个目录", RunAgentTool.description)

    def test_tool_description_states_uncommitted_changes_are_invisible(self):
        """
        「基点是当前 HEAD、你未提交的改动它看不到」必须写在**决策期**读到的文本里。

        ## 这条也是实测补的，且代价可量化

        隔离宣传的场景是「你手上有未提交改动时用它」，而基点规则是「取当前 HEAD、
        未提交改动不带入」——两句话组合到**同一个文件**上时必然冲突，而工具描述
        原先只讲前半句。实测：主 Agent 自己把后半句推了出来，但为此在一轮回复里
        把同一个两难重述了七遍、最后停下来让用户拍板。它推对了，只是很贵。

        断言落在 `parameters` 与 `description` 两处：模型不一定两处都读，
        写在一处等于赌它读的是那一处。
        """
        from rhinecode.tools.run_agent import RunAgentTool

        self.assertIn("看不到", RunAgentTool.description)
        self.assertIn(
            "看不到你未提交的改动",
            RunAgentTool.parameters["properties"]["isolation"]["description"],
        )
