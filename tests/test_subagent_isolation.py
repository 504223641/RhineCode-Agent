"""
c14 T30：子 Agent 工作区隔离的集成验收（spec F12–F17 / AC16–AC23 / AC35）。

验的是**跨模块接线**：角色定义 → 单向加严 → 建工作区 → 运行器接 cwd →
结束结算 → 交付信息。单模块的行为在各自的测试文件里已经验过，这里只验
「串起来之后还对不对」。

用真实的临时 Git 仓库跑，**不打桩 git**：本章的全部价值就在于「隔离是物理的」，
用假的 git 验它等于什么都没验。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from rhinecode.subagents.models import AgentCatalog, AgentSource, AgentSpec
from rhinecode.subagents.runner import SubAgentRuntime, run_subagent
from rhinecode.subagents.service import SubAgentService, resolve_isolation
from rhinecode.subagents.tasks import KIND_ROLE, TaskStatus
from rhinecode.subagents.toolset import resolve_toolset
from rhinecode.permission.engine import PermissionEngine
from rhinecode.permission.models import PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import BaseProvider, StreamChunk
from rhinecode.tools.registry import ToolRegistry
from rhinecode.worktree import lifecycle
from tests.worktree_support import cleanup, git, make_plain_dir, make_repo


# ---------------------------------------------------------------------- #
# 脚手架
# ---------------------------------------------------------------------- #


class _QuietProvider(BaseProvider):
    """只说一句话就结束，不调任何工具。"""

    def __init__(self, reply: str = "查完了。") -> None:
        self.reply = reply
        self.systems: list = []
        self.messages: list = []

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self.systems.append(system)
        # 隔离说明走的是**动态通道**（`<system-reminder>` 注入消息里），
        # 不是 `system` 参数——后者只承载稳定的角色正文，为的是命中前缀缓存。
        self.messages.append([getattr(m, "content", "") or "" for m in messages])
        yield StreamChunk(type="text", content=self.reply)
        yield StreamChunk(type="done")


def _spec(name="worker", isolation=None) -> AgentSpec:
    return AgentSpec(
        name=name,
        description="干活的",
        body="你是干活的。",
        source=AgentSource.BUILTIN,
        path=Path(f"{name}.md"),
        isolation=isolation,
    )


class IsolationBase(unittest.TestCase):
    """
    每个用例一个临时 Git 仓库，并把进程工作目录切进去。

    ⚠ 必须 chdir：`service._create_worktree` 用 `main_project_root()`
    定位主项目根，而那正是进程的当前工作目录。测试要模拟「rhine 在这个仓库里
    启动」，就得真的站在那里。
    """

    def setUp(self):
        self.repo = make_repo(prefix="c14-iso-")
        self.addCleanup(cleanup, self.repo)
        self._old_cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(lambda: os.chdir(self._old_cwd))

        self.provider = _QuietProvider()
        self.registry = ToolRegistry.default()
        self.engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)

    def _service(self, spec: AgentSpec, provision_entries=()) -> SubAgentService:
        runtime = SubAgentRuntime(
            provider_for=lambda model: self.provider,
            registry=self.registry,
            engine=self.engine,
            main_mode=lambda: self.engine.mode,
            environment_text=lambda cwd: f"工作目录：{cwd}",
            default_model="m",
        )
        return SubAgentService(
            AgentCatalog(specs={spec.name: spec}),
            runtime,
            tool_names_provider=self.registry.names,
            provision_entries=provision_entries,
        )

    def _delegate(self, service, **kw):
        """发起一次委派并等它跑完（子 Agent 在独立线程里）。"""
        outcome = service.delegate(KIND_ROLE, "worker", "去干活", **kw)
        if outcome.ok:
            record = service.tasks.get(outcome.task_id)
            record.done_event.wait(30)
        return outcome


# ---------------------------------------------------------------------- #
# F14：单向加严
# ---------------------------------------------------------------------- #


class OneWayTighteningTest(unittest.TestCase):
    """
    AC19：合并方向是单向加严。**纯函数，不需要 git。**

    「调用方关不掉」这条是本章的一条安全边界：写角色定义是人在表达约束，
    而调用方是模型。让模型撤销用户设的隔离，等于把开关交给被约束的一方。
    """

    def test_role_declared_cannot_be_turned_off(self):
        self.assertTrue(resolve_isolation("worktree", False))

    def test_role_declared_wins_over_silence(self):
        self.assertTrue(resolve_isolation("worktree", None))

    def test_caller_can_opt_in_when_role_is_silent(self):
        self.assertTrue(resolve_isolation(None, True))

    def test_no_isolation_when_neither_asks(self):
        self.assertFalse(resolve_isolation(None, False))
        self.assertFalse(resolve_isolation(None, None))


class OneWayTighteningIntegrationTest(IsolationBase):
    """AC19 的接线版：真的建出工作区来。"""

    def test_declared_role_isolates_even_when_call_says_false(self):
        service = self._service(_spec(isolation="worktree"))
        outcome = self._delegate(service, isolation=False)

        self.assertTrue(outcome.ok, outcome.text)
        record = service.tasks.get(outcome.task_id)
        self.assertTrue(record.worktree_path, "角色声明了隔离，调用方关不掉")

    def test_silent_role_isolates_when_call_asks(self):
        service = self._service(_spec())
        outcome = self._delegate(service, isolation=True)

        self.assertTrue(outcome.ok, outcome.text)
        self.assertTrue(service.tasks.get(outcome.task_id).worktree_path)

    def test_silent_role_does_not_isolate_by_default(self):
        service = self._service(_spec())
        outcome = self._delegate(service)

        self.assertTrue(outcome.ok, outcome.text)
        record = service.tasks.get(outcome.task_id)
        self.assertEqual(record.worktree_path, "")
        self.assertEqual(record.worktree_branch, "")


# ---------------------------------------------------------------------- #
# F12：失败不降级
# ---------------------------------------------------------------------- #


class NoDegradationTest(unittest.TestCase):
    """
    ⚠ **AC16/AC17：非 Git 环境下必须明确失败，绝不降级。**

    降级是本项目通篇最忌讳的形态：用户配了隔离却没隔离，而界面上完全看不出来。
    """

    def setUp(self):
        self.plain = make_plain_dir(prefix="c14-nogit-")
        self.addCleanup(cleanup, self.plain)
        self._old = Path.cwd()
        os.chdir(self.plain)
        self.addCleanup(lambda: os.chdir(self._old))

        self.provider = _QuietProvider()
        self.registry = ToolRegistry.default()
        self.engine = PermissionEngine(RuleSet([]), mode=PermissionMode.DEFAULT)

    def _service(self, spec):
        runtime = SubAgentRuntime(
            provider_for=lambda model: self.provider,
            registry=self.registry,
            engine=self.engine,
            main_mode=lambda: self.engine.mode,
            environment_text=lambda cwd: f"工作目录：{cwd}",
            default_model="m",
        )
        return SubAgentService(
            AgentCatalog(specs={spec.name: spec}),
            runtime,
            tool_names_provider=self.registry.names,
        )

    def test_isolated_delegation_fails_with_a_readable_reason(self):
        service = self._service(_spec(isolation="worktree"))
        outcome = service.delegate(KIND_ROLE, "worker", "去干活")

        self.assertFalse(outcome.ok)
        self.assertIn("Git 仓库", outcome.text)
        self.assertIn("不要重试", outcome.text, "这类失败重试多少次都一样，要说清楚")

    def test_nothing_is_written_to_disk(self):
        service = self._service(_spec(isolation="worktree"))
        before = sorted(p.name for p in self.plain.iterdir())

        service.delegate(KIND_ROLE, "worker", "去干活")

        self.assertEqual(sorted(p.name for p in self.plain.iterdir()), before)

    def test_no_task_record_is_created(self):
        """
        创建失败连任务记录都不该产生——否则 `/agents` 里会多出一条
        永远不会开始的任务。
        """
        service = self._service(_spec(isolation="worktree"))
        service.delegate(KIND_ROLE, "worker", "去干活")
        self.assertEqual(service.tasks.running_count(), 0)

    def test_non_isolated_role_still_works_in_the_same_environment(self):
        """AC16 的另一半：同一环境下不要求隔离的角色照常成功。"""
        service = self._service(_spec())
        outcome = service.delegate(KIND_ROLE, "worker", "去干活")

        self.assertTrue(outcome.ok, outcome.text)
        record = service.tasks.get(outcome.task_id)
        record.done_event.wait(30)
        self.assertEqual(record.worktree_path, "")


# ---------------------------------------------------------------------- #
# F15 / F16 / F17：提示、结算、交付
# ---------------------------------------------------------------------- #


class IsolationNoticeTest(IsolationBase):
    """AC20：子 Agent 知道自己在哪。"""

    def test_prompt_carries_path_and_branch(self):
        service = self._service(_spec(isolation="worktree"))
        outcome = self._delegate(service)
        record = service.tasks.get(outcome.task_id)

        # ⚠ 观察点是**消息**不是 `system` 参数：隔离说明走动态通道
        # （每轮求值的 `<system-reminder>`），而 `system` 只承载稳定的角色正文
        # ——那是为命中前缀缓存刻意分开的两条通道。
        joined = "\n".join(
            "\n".join(batch) for batch in self.provider.messages
        )
        self.assertIn("isolated-workspace", joined)
        self.assertIn(record.worktree_branch, joined)
        self.assertIn("git commit", joined, "要告诉它成果靠提交交回去")

    def test_environment_section_reports_the_worktree_not_the_main_root(self):
        """
        环境信息段里的「工作目录」必须是**隔离工作区**，不是主项目根。

        ## 这条是真实模型实测补的（不是设计推演）

        原先 `environment_text` 固定按主项目根取，于是同一份系统提示里出现两个
        互相矛盾的工作目录：环境信息说主项目根，`<isolated-workspace>` 段说工作区。
        真实模型的反应是把两者「调和」成一个相对路径——连着去读
        `.rhinecode/worktrees/<名字>/calc/x.py`，全部落空，白烧两轮。

        **它不报错**：权限管线仍按工作区判定，工具照常工作，只是模型一直在猜路。
        故这条断言直接对着注入文本查，不看行为。
        """
        service = self._service(_spec(isolation="worktree"))
        outcome = self._delegate(service)
        record = service.tasks.get(outcome.task_id)

        joined = "\n".join("\n".join(batch) for batch in self.provider.messages)
        self.assertIn(f"工作目录：{record.worktree_path}", joined)
        # 反证：主项目根**不能**作为一条独立的「工作目录：」行出现。
        # 只断言「不含主项目根」是不成立的——工作区路径本身就以它开头。
        self.assertNotIn(f"工作目录：{self.repo}\n", joined)

    def test_non_isolated_agent_still_reports_the_main_root(self):
        """反证：不隔离的子 Agent 仍按主项目根报，改造没有殃及既有行为。"""
        service = self._service(_spec(isolation=None))
        self._delegate(service)

        joined = "\n".join("\n".join(batch) for batch in self.provider.messages)
        self.assertIn(f"工作目录：{self.repo}", joined)


class SettlementTest(IsolationBase):
    """AC21 / AC22 / AC23：结束时按变更决定去留，交付信息由系统给。"""

    def _run_with_provider(self, provider, spec=None):
        """直接驱动运行器，绕开线程——本组要精确控制子 Agent 干了什么。"""
        spec = spec or _spec(isolation="worktree")
        service = self._service(spec)
        service.runtime = SubAgentRuntime(
            provider_for=lambda model: provider,
            registry=self.registry,
            engine=self.engine,
            main_mode=lambda: self.engine.mode,
            environment_text=lambda cwd: f"工作目录：{cwd}",
            default_model="m",
        )
        handle, _ = lifecycle.create(self.repo, "task1")
        git(["config", "user.name", "c14 test"], handle.path)
        git(["config", "user.email", "c14@example.invalid"], handle.path)

        record = service.tasks.create(KIND_ROLE, "worker", "去干活")
        toolset = resolve_toolset(frozenset(self.registry.names()), spec)
        run_subagent(
            service.runtime, spec, "去干活", record, service.tasks,
            toolset, frozenset(self.registry.names()), None, handle,
        )
        return handle, record

    def test_untouched_worktree_is_removed(self):
        """AC21：什么都没改 → 目录与分支一并回收。"""
        handle, record = self._run_with_provider(_QuietProvider())

        self.assertFalse(handle.path.exists(), "无变更的工作区应被回收")
        self.assertIn("已自动清理", record.conclusion)

    def test_changed_worktree_is_kept(self):
        """AC22：改了东西 → 目录保留。"""
        handle, _ = self._run_with_provider(_QuietProvider())
        # 上一步已经把它删了，重开一个并制造变更。
        handle2, record = self._make_dirty_and_run()

        self.assertTrue(handle2.path.is_dir(), "有变更的工作区必须保留")
        self.assertIn(handle2.branch, record.conclusion)

    def _make_dirty_and_run(self):
        spec = _spec(isolation="worktree")
        service = self._service(spec)
        handle, _ = lifecycle.create(self.repo, "task2")
        git(["config", "user.name", "c14 test"], handle.path)
        git(["config", "user.email", "c14@example.invalid"], handle.path)
        (handle.path / "made.py").write_text("x = 1\n", encoding="utf-8")

        record = service.tasks.create(KIND_ROLE, "worker", "去干活")
        toolset = resolve_toolset(frozenset(self.registry.names()), spec)
        run_subagent(
            service.runtime, spec, "去干活", record, service.tasks,
            toolset, frozenset(self.registry.names()), None, handle,
        )
        return handle, record

    def test_delivery_info_comes_from_git_not_the_model(self):
        """
        ⚠ **AC23：模型在正文里写了错的分支名，交付信息段里仍是实际值。**

        这是「代码保证 > 约定保证」的具体形态。分支名靠模型自述的话，
        主 Agent 会拿着一个不存在的名字去 merge，而排查时谁也想不到根因在这。
        """
        liar = _QuietProvider("我把工作提交到了 agent/completely-wrong 分支上。")
        handle2, record = self._make_dirty_and_run_with(liar)

        self.assertIn("agent/completely-wrong", record.conclusion, "模型的原话应保留")
        # 交付信息段（分隔线之后）里必须是实际分支名。
        tail = record.conclusion.split("── 隔离工作区")[-1]
        self.assertIn(handle2.branch, tail)
        self.assertNotIn("completely-wrong", tail)

    def _make_dirty_and_run_with(self, provider):
        spec = _spec(isolation="worktree")
        service = self._service(spec)
        service.runtime = SubAgentRuntime(
            provider_for=lambda model: provider,
            registry=self.registry,
            engine=self.engine,
            main_mode=lambda: self.engine.mode,
            environment_text=lambda cwd: f"工作目录：{cwd}",
            default_model="m",
        )
        handle, _ = lifecycle.create(self.repo, "task3")
        git(["config", "user.name", "c14 test"], handle.path)
        git(["config", "user.email", "c14@example.invalid"], handle.path)
        (handle.path / "made.py").write_text("x = 1\n", encoding="utf-8")

        record = service.tasks.create(KIND_ROLE, "worker", "去干活")
        toolset = resolve_toolset(frozenset(self.registry.names()), spec)
        run_subagent(
            service.runtime, spec, "去干活", record, service.tasks,
            toolset, frozenset(self.registry.names()), None, handle,
        )
        return handle, record


# ---------------------------------------------------------------------- #
# AC35：并发
# ---------------------------------------------------------------------- #


class ConcurrencyTest(IsolationBase):
    """三个隔离子 Agent 同时委派，互不干扰。"""

    def test_three_isolated_delegations_get_distinct_worktrees(self):
        service = self._service(_spec(isolation="worktree"))

        outcomes = [
            service.delegate(KIND_ROLE, "worker", f"任务 {i}") for i in range(3)
        ]
        for o in outcomes:
            self.assertTrue(o.ok, o.text)
            service.tasks.get(o.task_id).done_event.wait(30)

        branches = {
            service.tasks.get(o.task_id).worktree_branch for o in outcomes
        }
        self.assertEqual(len(branches), 3, f"三个分支必须互不相同：{branches}")


if __name__ == "__main__":
    unittest.main()
