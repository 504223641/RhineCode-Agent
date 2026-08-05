"""
Hook 系统的端到端场景（c12 checklist 第十三节，场景 1/2/3/5/6/7）。

用 `tests/e2e/` 的驱动设施起**真宿主子进程**跑完整交互闭环：真实 `build_app`、
真实 TUI、真实权限管线、真实 Hook 分发，只有模型是假的。

## 这一层验的是什么（与单测的分工）

单测能验「分发点在不在」「结论合不合并对」，验不到**「配置文件真的被读到了、
命令真的在这台机器上跑起来了、界面真的弹出了面板」**。这批用例就是为那一段存在的。

每条用例起一次宿主（本机约 8–12 秒），因此只挑真正需要「真跑」的场景：
- 场景 4（上下文注入）由 `test_hook_dispatch_points.py::InjectionChannelTest` 覆盖，
  它直接断言注入文本进了**发给 Provider 的 system 消息**且只出现一次——
  比经宿主观察更精确。
- 场景 8（零配置）由 `test_hook_zero_regression.py` 覆盖，那里连 trace 里
  「零条 hook 事件」都断言了。
"""

from __future__ import annotations

import unittest

from tests.e2e.assertions import TraceView
from tests.test_e2e_host import HostFixture


class HookSurfaceTest(HostFixture):
    """Hook 在真实宿主里的行为面。"""

    def _drain(self, rounds: int = 6, answer: str | None = None) -> None:
        """
        把一次交互跑到空闲：遇到面板时按 `answer` 应答（None 表示不该有面板）。
        """
        for _ in range(rounds):
            res = self.wait(timeout=60)
            self.assertTrue(res["ok"], res)
            if res["data"]["terminal"] != "pending":
                return
            if answer is None:
                self.fail(f"不该弹出面板：{res['data']}")
            self.answer(answer)
        self.fail("交互没有在预期轮数内结束")

    def _hook_events(self, view: TraceView, event: str) -> list[dict]:
        return [e for e in view.of_type("hook_dispatch") if e.get("event") == event]

    # ------------------------------------------------------------------ #
    def test_scenario_1_pre_tool_use_blocks_the_command(self):
        """
        场景 1：一条 `pre_tool_use` Hook 拦住 `git push`。

        判据三条：命令**没有执行**、模型收到了拦截原因、**对话继续进行**
        （拦截不终止 Agent Loop——这是 spec F6.1 与 C6 权限拒绝一致的性质）。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:HOOK_BLOCKED_PUSH",
            "--seed", "tests.e2e.scripts:seed_hook_block_push",
            "--idle-timeout", "300",
        )
        self.send("把改动推上去")
        self._drain()

        view = self.view()

        # ① Hook 确实被分发并命中
        dispatched = self._hook_events(view, "pre_tool_use")
        self.assertTrue(dispatched, "pre_tool_use 一次都没分发")
        self.assertTrue(
            any(e.get("verdict") == "deny" for e in dispatched),
            f"没有任何一次分发给出 deny：{dispatched}",
        )

        # ② 命令没有执行成功事件，且结局是「被 Hook 拦下」
        runs = [e for e in view.of_type("tool_execute") if e.get("tool") == "run_command"]
        self.assertTrue(runs, "run_command 一条记录都没有")
        self.assertEqual(
            [e for e in runs if e.get("outcome") == "executed"], [],
            "被拦下的命令绝不能有执行成功事件",
        )
        self.assertTrue(
            any(e.get("outcome") == "blocked_by_hook" for e in runs),
            f"结局应为 blocked_by_hook：{[e.get('outcome') for e in runs]}",
        )

        # ③ 拦截原因确实回灌给了模型（含规则名与「不要绕」的措辞）
        blocked = next(e for e in runs if e.get("outcome") == "blocked_by_hook")
        output = str(blocked.get("output", ""))
        self.assertIn("禁止直接 push", output, "原因里要点名是哪条规则")
        self.assertIn("请走 PR", output, "要带上 Hook 自己给的原因")
        self.assertIn("绕过", output)

        # ④ 循环没有异常终止：模型跑到了第二轮并说了话
        self.assertGreaterEqual(
            len(view.of_type("api_request")), 2, "拦截之后对话应当继续"
        )

    def test_scenario_2_ask_upgrade_pops_the_panel(self):
        """
        场景 2：Hook 把一次**只读**调用升级为「问用户」。

        只读工具在默认权限档下本该直接放行、不弹面板；面板弹出本身就是
        「升级生效了」的证据。再看判定层是不是 `hook`。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:HOOK_ASK_READ",
            "--seed", "tests.e2e.scripts:seed_hook_ask",
            "--idle-timeout", "300",
        )
        self.send("读一下 seed.txt")

        res = self.wait(timeout=60)
        self.assertEqual(
            res["data"]["terminal"], "pending",
            "只读调用被 Hook 升级为 ask 之后必须弹面板",
        )
        # `wait` 的成功响应把快照挂在 `state` 下，面板原文在 `state.panel.display`。
        panel = (res["data"].get("state") or {}).get("panel") or {}
        display = str(panel.get("display", ""))
        self.assertIn("敏感", display, f"面板上要显示 Hook 给的原因：{panel}")
        self.assertIn("Hook 规则", display, "要点名是哪条规则要求确认的")

        self.answer("once")
        self._drain()

        view = self.view()
        decisions = [
            e for e in view.of_type("permission_decision") if e.get("tool") == "read_file"
        ]
        self.assertTrue(decisions, "read_file 应当有权限判定记录")
        self.assertEqual(decisions[-1].get("decision"), "ask")
        self.assertEqual(decisions[-1].get("layer"), "hook", "判定层应标为 Hook")

        # 用户同意后照常执行
        executed = [
            e for e in view.of_type("tool_execute")
            if e.get("tool") == "read_file" and e.get("outcome") == "executed"
        ]
        self.assertTrue(executed, "用户同意后工具应当照常执行")

    def test_scenario_3_post_tool_use_automation_runs(self):
        """
        场景 3（自动化正路）：写完文件后 `post_tool_use` 的命令真的跑起来了。

        判据是**副作用文件真的出现在工作区里**，而且内容是从 stdin 的 JSON
        里取出来的——这同时验证了「负载真的经标准输入传过去了」。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:HOOK_POST_WRITE",
            "--seed", "tests.e2e.scripts:seed_hook_post_action",
            "--idle-timeout", "300",
        )
        self.send("写个 out.txt")
        self._drain(answer="once")

        marker = self.workspace() / "hook_ran.txt"
        self.assertTrue(marker.is_file(), "post_tool_use 的命令没有跑起来")
        self.assertEqual(
            marker.read_text(encoding="utf-8"), "out.txt",
            "内容应来自 stdin 里那份负载的 path 字段",
        )

        view = self.view()
        executes = view.of_type("hook_execute")
        self.assertTrue(executes, "应当有 hook_execute 记录")
        self.assertTrue(all(e.get("ok") for e in executes), f"Hook 应当成功：{executes}")

    def test_scenario_5_fail_closed_blocks_the_tool(self):
        """
        场景 5：一条**跑不起来**的 `pre_tool_use` Hook 必须把调用拦下（fail-closed）。

        这是决策 3A 的正面：写坏的安全 Hook 要**可见地**坏掉，
        而不是静默失效让用户以为防线还在。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:HOOK_POST_WRITE",
            "--seed", "tests.e2e.scripts:seed_hook_broken_pre",
            "--idle-timeout", "300",
        )
        self.send("写个 out.txt")
        self._drain()

        self.assertFalse(
            (self.workspace() / "out.txt").is_file(),
            "Hook 自身失败时工具不该执行",
        )
        view = self.view()
        blocked = [
            e for e in view.of_type("tool_execute")
            if e.get("outcome") == "blocked_by_hook"
        ]
        self.assertTrue(blocked, "应当有 blocked_by_hook 结局")
        output = str(blocked[0].get("output", ""))
        self.assertIn("Hook 自身执行失败", output)
        self.assertIn("fail-closed", output)
        self.assertNotIn(
            "工具执行异常", output,
            "文案必须与「工具执行异常」可区分——两者的排查方向完全不同",
        )

    def test_scenario_6_fail_open_lets_the_tool_through(self):
        """
        场景 6：**同一条坏 Hook** 改挂 `post_tool_use`，工具必须照常跑完。

        与场景 5 成对：它俩的差别就是决策 3A 的全部内容。分开验才看得出
        实现有没有真的按事件类型分流，而不是全走同一条路径。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:HOOK_POST_WRITE",
            "--seed", "tests.e2e.scripts:seed_hook_broken_post",
            "--idle-timeout", "300",
        )
        self.send("写个 out.txt")
        self._drain(answer="once")

        self.assertTrue(
            (self.workspace() / "out.txt").is_file(),
            "非拦截类事件上的 Hook 失败不该影响工具执行",
        )
        view = self.view()
        executed = [
            e for e in view.of_type("tool_execute")
            if e.get("tool") == "write_file" and e.get("outcome") == "executed"
        ]
        self.assertTrue(executed, "write_file 应当正常执行")
        failures = [e for e in view.of_type("hook_execute") if not e.get("ok")]
        self.assertTrue(failures, "Hook 的失败仍要被记录，不能静默吞掉")

    def test_scenario_7_project_notice_on_the_first_screen(self):
        """
        场景 7：项目级 `hooks.yaml` 存在时，**首屏**逐条列出事件与完整命令串。

        判据取自 `ui_message`——那是「界面上真的出现过什么」的记录。
        用 print 的话这段会被 Textual 的 alternate screen 整个盖住，
        既看不见也不会进 `ui_message`，所以这条用例同时钉住了「不能用 print」。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:SAY_HELLO",
            "--seed", "tests.e2e.scripts:seed_hook_block_push",
            "--idle-timeout", "300",
        )
        self.send("你好")
        self._drain()

        view = self.view()
        texts = "\n".join(str(e.get("text", "")) for e in view.of_type("ui_message"))
        self.assertIn("项目级 Hook 规则", texts, "首屏应当出现项目级提示")
        self.assertIn("直接执行", texts, "要说明它们不经模型、不经确认面板")
        self.assertIn("pre_tool_use", texts, "要列出事件")
        self.assertIn("hook_block.py", texts, "命令串要完整可见")


if __name__ == "__main__":
    unittest.main()
