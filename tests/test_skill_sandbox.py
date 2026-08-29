"""
Skill 的沙箱边界与会话恢复语义（c11 T43b）。

覆盖两条验收：
- **AC37（N5/N9）——本章唯一的安全边界验收**：Skill 目录进只读白名单后，
  读面扩大、写面与搜索面完全不动；Skill 不能放宽权限管线的任何一层。
- **AC36（N4）**：`/resume` 恢复历史后激活列表为空。

参照 c9 的同构先例 `tests/test_memory_sandbox.py`。
"""

import os
import tempfile
import unittest
from pathlib import Path

from rhinecode.config import Config
from rhinecode.conversation import ConversationManager
from rhinecode.agent.events import AgentEventType
from rhinecode.permission.adapter import to_request
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import (
    Decision,
    PermissionMode,
    PermissionRequest,
)
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import StreamChunk
from rhinecode.skills.manager import SkillManager
from rhinecode.tools.glob_files import GlobTool
from rhinecode.tools.path_guard import (
    PathGuardError,
    clear_read_roots,
    is_readable_path,
    register_read_root,
    resolve_readable,
)
from rhinecode.tools.read_file import ReadFileTool
from rhinecode.tools.run_command import RunCommandTool
from rhinecode.tools.write_file import WriteFileTool
from rhinecode.tools.path_guard import main_project_root


def _cwd():
    """
    c14：这些用例会 chdir 到临时工作区再断言，因此**每次现取**进程当前目录，
    而不是在模块加载时算一次——加载时的目录是仓库根，不是用例的工作区。
    """
    return main_project_root()


def _read_request(path: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name="read_file", rule_name="Read", specifier=path,
        kind="read_path", is_read_only=True, mode=PermissionMode.DEFAULT, cwd=_cwd(),
    )


def _write_request(path: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name="write_file", rule_name="Write", specifier=path,
        kind="write_path", is_read_only=False, mode=PermissionMode.DEFAULT, cwd=_cwd(),
    )


class SkillSandboxTest(unittest.TestCase):
    """
    AC37：Skill 目录只读白名单——读面扩大、写面与搜索面不动。

    目录型 Skill 的随附资源（模板、示例、参考文档）位于用户级或内置目录，
    **在项目工作区之外**。模型必须能读它们，但绝不能因此获得写权限。
    """

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace = root / "ws"
        self.workspace.mkdir()
        os.chdir(self.workspace)

        # 工作区**之外**的 Skill 目录
        self.skills_dir = root / "home" / ".rhinecode" / "skills"
        self.skills_dir.mkdir(parents=True)
        self.resource = self.skills_dir / "pack" / "template.md"
        self.resource.parent.mkdir()
        self.resource.write_text("模板内容", encoding="utf-8")

        clear_read_roots()
        register_read_root(self.skills_dir)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        # 复位白名单，避免污染后续用例。
        clear_read_roots()
        self._tmp.cleanup()

    def test_read_face_widened(self) -> None:
        """read_file 能读到白名单目录内的文件（绝对路径）。"""
        self.assertTrue(is_readable_path(str(self.resource), _cwd()))
        result = ReadFileTool().execute({"path": str(self.resource)}, cwd=_cwd())
        self.assertTrue(result.ok, result.output)
        self.assertIn("模板内容", result.output)

    def test_write_face_untouched(self) -> None:
        """
        **写面完全不动**：同一路径写入被拒。

        白名单只对 read 类判定生效——若它顺带放开了写，Skill 目录就成了
        一个模型可以随意改写的工作区外后门。
        """
        target = self.skills_dir / "pack" / "injected.md"
        result = WriteFileTool().execute(
            {"path": str(target), "content": "恶意内容"}
        )
        self.assertFalse(result.ok)
        self.assertFalse(target.exists())

    def test_search_face_untouched(self) -> None:
        """
        **搜索面完全不动**：白名单目录里的文件不会出现在 glob 结果中。

        `glob_files` 始终以工作区根为起点（它没有 `path` 参数），
        白名单对它一点作用都没有——这正是「只对 read 类判定生效」的含义。
        这里用一个独特文件名做探针：只要它没出现，就说明搜索面没被撑开。
        """
        probe = self.skills_dir / "pack" / "UNIQUEPROBE.md"
        probe.write_text("探针", encoding="utf-8")
        result = GlobTool().execute({"pattern": "**/*.md"}, cwd=_cwd())
        self.assertNotIn("UNIQUEPROBE", result.output)

    def test_grep_face_untouched(self) -> None:
        """同理，grep 也搜不到白名单目录里的内容（否则等于内容泄露）。"""
        from rhinecode.tools.grep_content import GrepTool

        # 用**文件名**做探针而不是内容关键词：grep 的「无匹配」输出会回显 pattern
        # 本身，拿关键词做断言会把回显误判成泄露。
        (self.skills_dir / "pack" / "LEAKPROBE.md").write_text(
            "SECRETNEEDLE", encoding="utf-8"
        )
        result = GrepTool().execute({"pattern": "SECRETNEEDLE"}, cwd=_cwd())
        self.assertNotIn("LEAKPROBE", result.output)
        self.assertIn("无匹配", result.output)

    def test_parent_traversal_still_rejected(self) -> None:
        """含 `..` 的路径仍被拒——白名单不是「随便去哪都行」。"""
        with self.assertRaises(PathGuardError):
            resolve_readable("../home/.rhinecode/skills/pack/template.md", _cwd())

    def test_unregistered_outside_path_still_rejected(self) -> None:
        """未注册的工作区外目录仍被拒（白名单是精确的，不是「工作区外全放开」）。"""
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        secret = other / "secret.txt"
        secret.write_text("机密", encoding="utf-8")
        self.assertFalse(is_readable_path(str(secret), _cwd()))
        self.assertFalse(ReadFileTool().execute({"path": str(secret)}, cwd=_cwd()).ok)

    def test_workspace_semantics_unchanged(self) -> None:
        """原工作区语义不变：区内文件读写都照常。"""
        inside = self.workspace / "a.txt"
        inside.write_text("原有内容", encoding="utf-8")
        self.assertTrue(ReadFileTool().execute({"path": "a.txt"}, cwd=_cwd()).ok)
        self.assertTrue(
            WriteFileTool().execute({"path": "b.txt", "content": "新"}, cwd=_cwd()).ok
        )

    def test_permission_engine_read_allow_write_deny(self) -> None:
        """
        权限引擎口径一致：同一路径 read 类 ALLOW、write 类 DENY。

        验证 c9 定下的「只对 read 类判定生效」在本章沿用无误。
        """
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)
        path = str(self.resource)
        self.assertIs(engine.decide(_read_request(path)).decision, Decision.ALLOW)
        self.assertIs(engine.decide(_write_request(path)).decision, Decision.DENY)


class SkillHasNoPermissionExemptionTest(unittest.TestCase):
    """
    AC37 / N9：Skill 拿不到任何权限豁免。

    Skill 的 SOP 正文是**发给模型的文本**，不是配置。它可以要求模型做任何事，
    但模型的每个工具调用照样要过五层管线——正文里写「请直接执行 rm -rf /」
    也只会在第①层黑名单被拦下。
    """

    def test_dangerous_command_still_blocked_by_blacklist(self) -> None:
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        tool = RunCommandTool()
        request = to_request(
            tool, {"command": "rm -rf /"}, PermissionMode.PERMISSIVE, _cwd()
        )
        result = engine.decide(request)
        self.assertIs(result.decision, Decision.DENY)
        # 即使权限模式是「放行」，黑名单也翻不过去——它是第①层硬防线。
        self.assertEqual(result.layer.value, "blacklist")


class ScriptedProvider:
    """按脚本响应的假 Provider。"""

    def __init__(self) -> None:
        self.calls = []

    def stream_chat(self, messages, thinking_effort, tools=None, system=None):
        self.calls.append(list(messages))
        yield StreamChunk(type="text", content="ok")
        yield StreamChunk(type="done")


class ResumeClearsActivationTest(unittest.TestCase):
    """
    AC36 / N4：`/resume` 恢复历史后激活列表为空。

    **必须走真实的 resume 路径**，不能直接调 `clear_active()` 冒充——
    后者验的只是「这个方法本身能清空状态」这一孤立行为，而 AC36 要的是
    「`/resume` 之后确实清空了」，两者之间隔着一条接线。
    """

    def setUp(self) -> None:
        self._old_cwd = os.getcwd()
        self._ws = tempfile.TemporaryDirectory()
        os.chdir(self._ws.name)
        self._home = tempfile.TemporaryDirectory()
        self.user_dir = Path(self._home.name)
        self.skills_dir = self.user_dir / "skills"
        self.skills_dir.mkdir(parents=True)
        (self.skills_dir / "s.md").write_text(
            "---\nname: s\ndescription: 说明\n---\nSOP正文MARKER\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        clear_read_roots()
        self._ws.cleanup()
        self._home.cleanup()

    def _manager(self) -> ConversationManager:
        sm = SkillManager(
            project_root=None,
            user_dir=self.user_dir,
            builtin_dir=None,
            has_short_command=lambda _n: False,
        )
        sm.startup()
        config = Config(
            protocol="deepseek", model="m", base_url="http://t",
            api_key="k", debug_log=False,
        )
        return ConversationManager(
            ScriptedProvider(), config, None, skill_manager=sm
        )

    def test_successful_resume_clears_activation(self) -> None:
        """
        激活 Skill → 走真实 resume 载入另一个会话 → 激活列表为空。

        激活态是**进程内存**状态，而 /resume 换的是历史。不清空的话，
        会话 A 激活的 Skill 会跟着进入会话 B——SOP 继续注入、白名单继续收窄，
        而会话 B 的历史里没有任何激活过它的痕迹。
        """
        mgr = self._manager()
        mgr.skill_manager.activate("s", "参数")
        self.assertEqual(mgr.skill_manager.status_segment(), "Skill:1")

        # 造一个可恢复的历史会话：先写几条消息，再 /clear 开新档。
        mgr.submit_user_message("第一条消息")
        mgr.clear()
        # clear 本身也会清激活态，这里重新激活以确保测的是 resume 那条路径。
        mgr.skill_manager.activate("s", "参数")
        self.assertEqual(mgr.skill_manager.status_segment(), "Skill:1")

        sessions = mgr.memory_manager.list_resume_sessions()
        target = next(
            (s for s in sessions if s.session_id != mgr.memory_manager.session_id),
            None,
        )
        self.assertIsNotNone(target, "没有可供恢复的历史会话")

        events = list(mgr.resume(target.session_id))
        history_events = [e for e in events if e.type == AgentEventType.HISTORY]
        self.assertTrue(history_events, "resume 未成功载入")

        self.assertIsNone(mgr.skill_manager.status_segment())
        self.assertEqual(mgr.skill_manager.active_text(), "")
        self.assertNotIn("SOP正文MARKER", mgr.skill_manager.prompt_report(frozenset()))

    def test_failed_resume_keeps_activation(self) -> None:
        """
        载入失败（目标不存在）→ 激活态**保持不变**。

        此时仍停留在原会话，清空激活态反而是错的。
        """
        mgr = self._manager()
        mgr.skill_manager.activate("s", "参数")

        events = list(mgr.resume("不存在的会话ID"))
        self.assertFalse([e for e in events if e.type == AgentEventType.HISTORY])
        self.assertEqual(mgr.skill_manager.status_segment(), "Skill:1")

    def test_clear_also_clears_activation(self) -> None:
        """F11：/clear 一并卸载已激活 Skill。"""
        mgr = self._manager()
        mgr.skill_manager.activate("s", "")
        mgr.clear()
        self.assertIsNone(mgr.skill_manager.status_segment())


if __name__ == "__main__":
    unittest.main()
