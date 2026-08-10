"""
C15 协作场景的脚本化端到端验证。

## 为什么这组用例在这里

C15 此前**没有任何可复跑的端到端场景**：`tests/e2e/` 的预置只有
`p0_` / `c11_` / `align_` / `c14_` / `sweep_` 五份，剧本里也没有一条
`run_agent` / `send_message` / `task_*`。它的验收（`docs/c15/acceptance/`）
全靠真实模型的自然行为——而那份记录本身写着最重要的结论：
**模型不会主动组队**（已知项 #17）。于是「机制本身对不对」这件事，
验收里只在用户明说「组一个队」时碰到过一次。

脚本化剧本不问模型愿不愿意组队，**直接让它组**，于是能稳定地验到那些
只在边界上才出现的行为——而那些恰恰是自然场景最难碰到的。

## 这组用例先验的是「设施本身可信」

`ScopedScriptedProvider` 是本轮新写的分派器。**它自己不对，上面所有判据都是假的**
（一个走兜底的剧本会让每个 Agent 都说 `[e2e-fallback]`，而流程照样跑完、
测试照样绿）。所以第一个 TestCase 专门验分派，第二个才验协作行为。
"""

import os
import threading
import unittest
from pathlib import Path

from rhinecode.provider.base import Message
from rhinecode.trace.recorder import bind_scope, current_scope
from tests.e2e.scripted import FALLBACK_MARKER, ScopedScriptedProvider, done, text


class ScopeDispatchTest(unittest.TestCase):
    """
    `ScopedScriptedProvider` 的分派语义。

    ⚠ 这组是**设施的自证**。它不验产品，验的是「上面那些协作判据所依赖的
    分派器本身靠不靠谱」——分派错了的话，剧本会静默走兜底，
    而流程仍然跑得通、测试仍然绿。
    """

    def _drain(self, provider: ScopedScriptedProvider) -> str:
        """跑一次 stream_chat，把正文块拼起来。"""
        chunks = list(provider.stream_chat([Message(role="user", content="go")]))
        return "".join(c.content for c in chunks if c.type == "text")

    def test_each_scope_has_its_own_cursor(self) -> None:
        """
        核心语义：**每个作用域各走各的轮次**。

        这正是 `ScriptedProvider` 做不到的——它一个全局计数器，
        主对话跑一轮就会把队员的第一轮「吃掉」。
        """
        provider = ScopedScriptedProvider(
            {
                "main": [[text("主-1"), done()], [text("主-2"), done()]],
                "subagent:w": [[text("员-1"), done()], [text("员-2"), done()]],
            }
        )
        # 交错调用：主、员、主、员。若共用一个游标，第二次「主」会拿到「主-2」
        # 之外的东西（或直接错位到员的剧本上）。
        bind_scope("main")
        self.assertEqual(self._drain(provider), "主-1")
        bind_scope("subagent:w")
        self.assertEqual(self._drain(provider), "员-1")
        bind_scope("main")
        self.assertEqual(self._drain(provider), "主-2")
        bind_scope("subagent:w")
        self.assertEqual(self._drain(provider), "员-2")
        bind_scope("main")

    def test_unknown_scope_falls_back_to_star_then_marker(self) -> None:
        """没写剧本的作用域：先找 `*`，再退兜底。"""
        with_star = ScopedScriptedProvider(
            {"main": [[text("主"), done()]], "*": [[text("其它"), done()]]}
        )
        bind_scope("subagent:nobody")
        self.assertEqual(self._drain(with_star), "其它")

        without_star = ScopedScriptedProvider({"main": [[text("主"), done()]]})
        self.assertIn(FALLBACK_MARKER, self._drain(without_star))
        bind_scope("main")

    def test_scope_is_thread_local_so_concurrent_agents_do_not_interleave(self) -> None:
        """
        ⚠ **并发下不串味**——这是整个分派器存在的理由。

        两条线程各绑一个作用域、各跑两轮，断言各自拿到的**都是自己剧本里的
        那两轮、且顺序正确**。用 `ScriptedProvider` 跑同样的场景，
        四次调用会按线程调度的先后瓜分同一个剧本列表。
        """
        provider = ScopedScriptedProvider(
            {
                "subagent:a": [[text("a1"), done()], [text("a2"), done()]],
                "subagent:b": [[text("b1"), done()], [text("b2"), done()]],
            }
        )
        got: dict[str, list[str]] = {"subagent:a": [], "subagent:b": []}
        # 用栅栏把两条线程卡在同一时刻放行，最大化交错概率——
        # 不这样做的话第一条线程往往已经跑完了，验不到并发。
        gate = threading.Barrier(2)

        def run(scope: str) -> None:
            bind_scope(scope)
            gate.wait()
            for _ in range(2):
                got[scope].append(self._drain(provider))

        threads = [threading.Thread(target=run, args=(s,)) for s in got]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(got["subagent:a"], ["a1", "a2"])
        self.assertEqual(got["subagent:b"], ["b1", "b2"])

    def test_recorded_calls_carry_their_scope(self) -> None:
        """
        每条留存调用都带作用域——断言「这一轮是谁跑的」全靠它。

        没有这个字段的话，多 Agent 场景里 `calls[3]` 到底是主对话第 2 轮
        还是队员第 1 轮，只能靠数数猜。
        """
        provider = ScopedScriptedProvider(
            {"main": [[text("主"), done()]], "subagent:w": [[text("员"), done()]]}
        )
        bind_scope("main")
        self._drain(provider)
        bind_scope("subagent:w")
        self._drain(provider)
        bind_scope("main")

        self.assertEqual([c.scope for c in provider.calls], ["main", "subagent:w"])
        self.assertEqual(len(provider.calls_in("subagent:w")), 1)


class TeamScenarioShapeTest(unittest.TestCase):
    """
    三份协作剧本的**形态**校验。

    不起真实应用（那是 `TeamRunTest` 的事），只检查剧本本身写得对不对。
    这类错误在真跑时的现象都很误导：作用域键写错 → 队员走兜底、
    什么都不做，看起来像「唤醒机制坏了」。
    """

    def _scripts(self):
        from tests.e2e import scripts

        return {
            "TEAM_PARALLEL": scripts.TEAM_PARALLEL,
            "TEAM_WAKE": scripts.TEAM_WAKE,
            "TEAM_AUTO_WAKE": scripts.TEAM_AUTO_WAKE,
        }

    def test_all_team_scripts_are_dicts_keyed_by_scope(self) -> None:
        """
        必须是 dict 且键形如 `main` / `subagent:<名字>`。

        写成 list 的话宿主会分派给 `ScriptedProvider`（按全局序号），
        剧本每次跑对应到不同 Agent——**不确定的判据比没有判据更坏**。
        """
        for name, script in self._scripts().items():
            with self.subTest(script=name):
                self.assertIsInstance(script, dict, f"{name} 必须是 dict")
                self.assertIn("main", script, f"{name} 缺主对话剧本")
                for key in script:
                    self.assertTrue(
                        key == "main" or key == "*" or key.startswith("subagent:"),
                        f"{name} 的作用域键 {key!r} 不合法",
                    )

    def test_member_names_match_run_agent_calls(self) -> None:
        """
        ⚠ **作用域里的名字必须是 `run_agent` 的 `name`，不是角色名。**

        同一个角色可以派出多个队员（`TEAM_PARALLEL` 里两个 impl 都是 `worker`），
        所以作用域取的是队员名。把它写成角色名的话，两个队员会挤在同一个
        作用域里、共用一份剧本——而现象是「第二个队员莫名其妙重复了第一个的动作」。

        这条用例把「剧本里声明的队员」与「主对话真的派出的队员」对齐。
        """
        for name, script in self._scripts().items():
            with self.subTest(script=name):
                spawned = set()
                for turn in script["main"]:
                    for chunk in turn:
                        call = getattr(chunk, "tool_call", None)
                        if call is not None and call.name == "run_agent":
                            member = call.arguments.get("name")
                            self.assertTrue(
                                member,
                                f"{name}：run_agent 必须显式给 name，"
                                "否则队员名自动生成、作用域对不上",
                            )
                            spawned.add(f"subagent:{member}")

                declared = {k for k in script if k.startswith("subagent:")}
                self.assertEqual(
                    declared,
                    spawned,
                    f"{name}：剧本声明的队员与主对话派出的队员对不上"
                    f"（多余 {sorted(declared - spawned)}；缺失 {sorted(spawned - declared)}）",
                )

    def test_scenarios_module_exposes_both_seeds(self) -> None:
        """
        两个预置函数都在，且签名是 `(workspace, user_dir)`。

        宿主的 `--seed MOD:FUNC` 就按这个签名调，写错了要到真跑时才炸。
        """
        import inspect

        from tests.e2e import c15_scenarios

        for fn_name in ("seed_team", "seed_team_readonly"):
            fn = getattr(c15_scenarios, fn_name, None)
            self.assertIsNotNone(fn, f"缺预置函数 {fn_name}")
            params = list(inspect.signature(fn).parameters)
            self.assertEqual(params, ["workspace", "user_dir"], f"{fn_name} 签名不对")

    def test_roles_do_not_declare_isolation(self) -> None:
        """
        ⚠ **协作角色不得声明 `isolation: worktree`。**

        C15 实现期定下的边界是「隔离委派与待命互斥」：声明了隔离的队员
        跑完即退场、不进入待命。给协作角色加隔离的话，唤醒续跑那几条判据
        会**永远验不到**，而失败现象是「队员叫不醒」——离根因很远。

        这条直接读预置写出来的角色文件，而不是读源码字符串：
        要验的是「落到盘上的那份定义」。
        """
        import tempfile

        from tests.e2e import c15_scenarios

        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            c15_scenarios.seed_team(ws, ws / "user")
            agents = sorted((ws / ".rhinecode" / "agents").glob("*.md"))
            self.assertTrue(agents, "预置没有写出任何角色定义")
            for path in agents:
                body = path.read_text(encoding="utf-8")
                self.assertNotIn(
                    "isolation",
                    body,
                    f"{path.name} 声明了隔离——待命/唤醒的判据会永远验不到",
                )


if __name__ == "__main__":
    unittest.main()


class TeamRunTest(unittest.IsolatedAsyncioTestCase):
    """
    **真跑一遍 `TEAM_PARALLEL`**：装配真实应用，用协作剧本驱动完整闭环。

    上面两组分别验了「分派器可信」与「剧本形态对」，这一组验的是最要紧的
    那件事——**这套剧本真的能驱动产品跑完一次协作**，而不是看起来能跑。

    判据分三层，缺一不可：
    ① 两个队员都真的跑起来了（各自的作用域下有模型调用）；
    ② 文件真的被改了（说明工具真的执行、权限真的放行）；
    ③ trace 里有 team_* 事件（说明走的是协作路径，不是各干各的）。

    只跑 `TEAM_PARALLEL` 一条：它同时覆盖共享清单、并行委派与点对点消息，
    是三份剧本里信息量最大的。另外两份（唤醒续跑、自动唤起）依赖 TUI 的
    0.5 秒轮询与真实时序，更适合用宿主 + 客户端手工驱动，
    预置与剧本已经备好，`docs/c15/acceptance/` 可以直接引用。
    """

    def setUp(self) -> None:
        from tests.e2e import sandbox
        from rhinecode.tools import path_guard

        self._cwd = Path.cwd()
        self.ws = sandbox.create_workspace()
        self.user_dir = sandbox.create_user_dir()
        os.chdir(self.ws)
        path_guard.clear_read_roots()
        self.trace_path = self.ws / ".rhinecode" / "traces" / "team.jsonl"
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.result = None

    def tearDown(self) -> None:
        from tests.e2e import sandbox
        from rhinecode.tools import path_guard

        if self.result is not None:
            self.result.cleanup("test_teardown")
        os.chdir(self._cwd)
        path_guard.clear_read_roots()
        for path in (self.ws, self.user_dir):
            sandbox.force_rmtree(path)

    async def test_parallel_team_runs_end_to_end(self) -> None:
        from rhinecode.bootstrap import build_app
        from rhinecode.config import Config
        from rhinecode.trace import TraceRecorder
        from tests.e2e import c15_scenarios
        from tests.e2e.assertions import TraceView
        from tests.e2e.scripts import TEAM_PARALLEL

        c15_scenarios.seed_team(self.ws, self.user_dir)

        provider = ScopedScriptedProvider(TEAM_PARALLEL)
        recorder = TraceRecorder(self.trace_path)
        self.result = build_app(
            Config(
                protocol="deepseek",
                model="deepseek-chat",
                base_url="https://api.deepseek.com",
                api_key="fake-key-for-test",
                debug_log=False,
                context_window=65536,
            ),
            user_dir=self.user_dir,
            recorder=recorder,
            provider_factory=lambda cfg: provider,
            exclude_tools=frozenset({"mcp_add_server", "mcp_resolve_server"}),
        )
        # 自动笔记是不确定性来源（它另起一条对话、另调一次模型），关掉
        self.result.manager.memory_manager.memories_enabled = False

        app = self.result.app
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # 走**真人提交入口**（聚焦输入框 → 设值 → 按回车），
            # 不调任何内部方法：内部方法会绕过命令层与协调层的接线，
            # 那样验到的就不是用户实际走的那条路（驱动设施的同一条纪律）。
            from rhinecode.tui.widgets import InputBar

            bar = app.query_one(InputBar)
            bar.focus()
            bar.value = "把这两处小改动分给队员做"
            await pilot.press("enter")
            # 等到界面空闲**且后台也没活了**——`idle` 单独不够：
            # 队员在后台跑的时候界面照样空闲（这正是 quiescent 存在的理由）
            await self._wait_quiescent(app, pilot, timeout=30.0)

        # ① 两个队员都真的跑起来了
        for scope in ("subagent:impl-a", "subagent:impl-b"):
            self.assertTrue(
                provider.calls_in(scope),
                f"{scope} 一次模型调用都没有——剧本的作用域键可能写错了",
            )
        # 兜底文本一次都不该出现：出现了就说明有哪条对话走到了剧本之外
        for call in provider.calls:
            self.assertIsNotNone(call.scope)

        # ② 文件真的被改了（工具执行 + 权限放行都成立）
        app_py = (self.ws / "src" / "app.py").read_text(encoding="utf-8")
        util_py = (self.ws / "src" / "util.py").read_text(encoding="utf-8")
        self.assertIn("你好，", app_py, "impl-a 的修改没有落盘")
        self.assertIn("shout", util_py)

        # ③ 走的是协作路径
        view = TraceView.load(self.trace_path)
        self.assertTrue(view.of_type("team_task"), "共享任务清单没有任何变更记录")
        self.assertTrue(view.of_type("team_member"), "花名册没有任何状态流转记录")
        self.assertTrue(view.of_type("team_message"), "没有任何队友消息送达")
        # 两次委派都记下来了，且带上了本轮新增的字段
        starts = view.of_type("subagent_start")
        self.assertEqual(len(starts), 2, "应当有两次委派")
        for s in starts:
            self.assertIn(s.get("member"), ("impl-a", "impl-b"))
            self.assertIs(s.get("isolated"), False)
            self.assertTrue(s.get("permission_mode"), "实际生效的权限档没记下来")

    async def _wait_quiescent(self, app, pilot, timeout: float) -> None:
        """
        等到「界面空闲 + 后台没活」。

        这里手写一小段而不是复用 `DriverCore.wait`：那个要一整套控制通道
        （responder / build_result / trace_path），而本用例只需要它的判据。
        判据本身与 `control._is_quiescent` 保持同一口径——**待命队员不算**。
        """
        import asyncio

        deadline = asyncio.get_running_loop().time() + timeout
        manager = app._manager
        while asyncio.get_running_loop().time() < deadline:
            await pilot.pause()
            busy = getattr(app, "_stream_active", False)
            running = manager.running_subagent_count()
            unread = manager.team_has_unread_for_main()
            if not busy and not running and not unread:
                # 再多等一拍：0.5 秒的轮询可能刚好要起一条自动唤起的流
                await asyncio.sleep(0.05)
                await pilot.pause()
                if not getattr(app, "_stream_active", False):
                    return
            await asyncio.sleep(0.05)
        self.fail(
            f"等待 {timeout} 秒仍未静止："
            f"busy={getattr(app, '_stream_active', None)} "
            f"running={manager.running_subagent_count()} "
            f"unread={manager.team_has_unread_for_main()}"
        )
