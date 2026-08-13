"""
②″保护路径的端到端场景（protected-paths 扩展 checklist 第八节）。

用 `tests/e2e/` 的驱动设施起**真宿主子进程**跑完整交互闭环：真实 `build_app`、
真实 TUI、真实权限管线、真实确认面板，只有模型是假的。

## 这一层验的是什么（与单测的分工）

单测能验「收紧器的判定对不对」，验不到这三件：

1. **面板真的弹出来了**，而且上面**真的只有三个选项**——`test_protected_wiring`
   验的是 `ConfirmPanel.show_for` 造出了什么，验不到「协调层真的把这次判定
   送到了那个面板」；
2. **本地配置文件真的没被写过**——单测断言的是 `persist_local_rule` 的调用次数，
   而真机上要看的是磁盘；
3. **隔离子 Agent 真的还能干活**——坑 1 的行为症状只在「工作区确实坐落在主项目根的
   `.rhinecode/` 之下」时才出现，而单测用的是系统临时目录，**那个症状在单测里
   物理上复现不出来**（实现期变异测试实测确认）。这条只能在真机上验。

## ⚠ 全部在放行档下跑

每条用例先送两次 `/perm`（默认 → 严格 → 放行）。缺省档下普通写入本来就弹面板，
那样「保护路径弹面板」这件事**没有任何分辨力**——两种实现都会弹。

每条用例起一次宿主（本机约 8–12 秒），因此只挑真正需要「真跑」的场景。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.test_e2e_host import HostFixture


class ProtectedPathE2ETest(HostFixture):
    """②″保护路径在真实宿主里的行为面。"""

    # ------------------------------------------------------------------ #
    def _permissive(self) -> None:
        """切到放行档：`/perm` 三档循环是 默认 → 严格 → 放行，所以送两次。"""
        for _ in range(2):
            self.send("/perm")
            res = self.wait(timeout=30)
            self.assertTrue(res["ok"], res)

    def _panel(self, res: dict) -> dict:
        return (res["data"].get("state") or {}).get("panel") or {}

    def _option_ids(self, res: dict) -> list[str]:
        return [o.get("id") for o in self._panel(res).get("options", [])]

    def _wait_quiescent(self, timeout: float = 120.0) -> dict:
        """
        等到「界面空闲**且**后台也没活了」。

        ⚠ 写多 Agent 场景必须用它：后台委派在跑时界面照样 `idle`，
        用缺省的 `terminal` 会让判据变成竞态的——而**竞态判据比没有判据更坏，
        它偶尔通过**（CLAUDE.md 成对维护点已登记）。
        """
        return self.cmd({"cmd": "wait", "timeout": timeout, "until": "quiescent"})

    def _drain(self, rounds: int = 6, answer: str | None = None) -> None:
        """跑到空闲；遇到面板时按 `answer` 应答（None 表示不该有面板）。"""
        for _ in range(rounds):
            res = self.wait(timeout=60)
            self.assertTrue(res["ok"], res)
            if res["data"]["terminal"] != "pending":
                return
            if answer is None:
                self.fail(f"不该弹出面板：{self._panel(res)}")
            self.answer(answer)
        self.fail("交互没有在预期轮数内结束")

    def _workspace(self) -> Path:
        """宿主的临时工作区路径（用于直接查磁盘）。"""
        self.assertIsNotNone(self.info)
        return Path(self.info.workspace)

    # ------------------------------------------------------------------ #
    def test_scenario_1_permissive_write_to_config_pops_the_panel(self):
        """
        场景 1：**放行档下写 `.rhinecode/hooks.yaml` 仍然弹面板，且只有三个选项。**

        放行档的语义是「灰色地带别再烦我」，④层对这次写入本会直接 ALLOW。
        面板弹出本身就是「②″收紧器生效了」的证据。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:PROTECTED_WRITE_CONFIG",
            "--seed", "tests.e2e.scripts:seed_protected_plain",
            "--idle-timeout", "300",
        )
        self._permissive()
        self.send("往 .rhinecode/hooks.yaml 加一条规则")

        res = self.wait(timeout=60)
        self.assertEqual(
            res["data"]["terminal"], "pending",
            "放行档下写配置文件必须仍然弹面板（②″收紧器）",
        )
        display = str(self._panel(res).get("display", ""))
        self.assertIn("保护路径", display, f"面板要说清这是保护路径判定：{display}")
        self.assertIn(
            "Hook", display,
            "还要说清**这个文件为什么特殊**——那是用户决定放不放行的唯一依据",
        )
        self.assertEqual(
            self._option_ids(res), ["yes", "yes_session", "no"],
            "保护路径场景不提供「永久放行」：那个选项写的是③层规则，"
            "而③层压不过本层，留着它就是一个点了没用的按钮",
        )

        self.answer("once")
        self._drain()

        view = self.view()
        decisions = [
            e for e in view.of_type("permission_decision")
            if e.get("tool") == "write_file"
        ]
        self.assertTrue(decisions, "write_file 应当有判定记录")
        self.assertEqual(decisions[-1].get("decision"), "ask")
        self.assertEqual(decisions[-1].get("layer"), "protected")

    def test_scenario_2_a_wide_allow_rule_cannot_dissolve_it(self):
        """
        场景 2（**顺序论证的真机落点**）：项目级 `allow: Write(.rhinecode/**)`
        存在时，写 `hooks.yaml` **仍然**弹面板。

        ⚠ 这条与场景 1 的差别只有那一条规则，但它是整条顺序论证唯一的真机证据：
        若②″被写成「②之后③之前的短路站」，那条 allow 会在③层先行放行、
        本层根本轮不到说话（其实反了——短路站会更早说话，但它同时会吞掉③的 deny
        与④严格档的 DENY，那两条由单测的 `NoDowngradeTest` 钉着）。
        真正要在真机上确认的是：**用户配置里的宽 allow 消解不掉它**。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:PROTECTED_WRITE_CONFIG",
            "--seed", "tests.e2e.scripts:seed_protected_wide_allow",
            "--idle-timeout", "300",
        )
        self._permissive()
        self.send("往 .rhinecode/hooks.yaml 加一条规则")

        res = self.wait(timeout=60)
        self.assertEqual(
            res["data"]["terminal"], "pending",
            "一条 allow: Write(.rhinecode/**) 也盖不过②″保护路径",
        )
        self.assertEqual(self._option_ids(res), ["yes", "yes_session", "no"])

        self.answer("once")
        self._drain()

        view = self.view()
        decisions = [
            e for e in view.of_type("permission_decision")
            if e.get("tool") == "write_file"
        ]
        self.assertEqual(decisions[-1].get("layer"), "protected")

    def test_scenario_4_ordinary_write_gets_no_extra_panel(self):
        """
        场景 4（**对照组**）：放行档下改普通业务文件，一次面板都不多。

        ⚠ 没有这条就分不清「②″生效」与「这个档位本来就什么都要问」——
        场景 1、2 在一个「无条件弹面板」的错误实现下同样全绿。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:PROTECTED_ORDINARY_WRITE",
            "--seed", "tests.e2e.scripts:seed_protected_plain",
            "--idle-timeout", "300",
        )
        self._permissive()
        self.send("把 src/app.py 改成 x = 1")

        # answer=None：出现任何面板都当场失败
        self._drain(answer=None)

        view = self.view()
        decisions = [
            e for e in view.of_type("permission_decision")
            if e.get("tool") == "write_file"
        ]
        self.assertTrue(decisions)
        self.assertEqual(decisions[-1].get("decision"), "allow")
        self.assertEqual(decisions[-1].get("layer"), "mode")
        self.assertIs(decisions[-1].get("protected_exempt"), False)
        self.assertTrue(
            (self._workspace() / "src" / "app.py").read_text(encoding="utf-8").strip()
            == "x = 1",
            "文件应当真的被改了",
        )

    def test_scenario_5_session_allow_works_and_writes_nothing(self):
        """
        场景 5：选「本会话放行」之后，**同一个文件**的第二次写入不再弹面板，
        且**本地配置文件里没有新增任何东西**。

        后半句是选项 C（豁免只在内存）在真机上的落点。单测断言的是
        `persist_local_rule` 的调用次数，这里看的是磁盘——两者都要，
        因为「换一条路径写了盘」在调用计数上看不出来。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:PROTECTED_WRITE_TWICE",
            "--seed", "tests.e2e.scripts:seed_protected_plain",
            "--idle-timeout", "300",
        )
        self._permissive()
        self.send("往 .rhinecode/hooks.yaml 加两条规则")

        first = self.wait(timeout=60)
        self.assertEqual(first["data"]["terminal"], "pending", "第一次必须问")
        self.answer("session")

        # 第二次写同一个文件：豁免已登记，不该再弹
        self._drain(answer=None)

        view = self.view()
        decisions = [
            e for e in view.of_type("permission_decision")
            if e.get("tool") == "write_file"
        ]
        self.assertEqual(len(decisions), 2, f"应当有两次写入判定：{decisions}")
        self.assertEqual(decisions[0].get("layer"), "protected")
        self.assertIs(
            decisions[1].get("protected_exempt"), True,
            "第二次必须标记为「保护路径已豁免」——不标的话时间线上只剩一条 "
            "`allow（④模式）`，读的人会以为用户切到了放行档",
        )
        self.assertEqual(decisions[1].get("decision"), "allow")

        # ⚠ 豁免绝不落盘
        local = self._workspace() / ".rhinecode" / "permissions.local.yaml"
        if local.exists():
            self.assertNotIn(
                "Write", local.read_text(encoding="utf-8"),
                "「本会话放行」不该往本地配置里写任何 Write 规则",
            )

    def test_scenario_6_non_isolated_subagent_is_auto_denied(self):
        """
        场景 6：**非隔离子 Agent 写配置时被自动拒绝。**

        ⚠ **这是期望行为不是误伤**——子 Agent 不该改配置。它非交互，
        判 ASK 即自动拒绝（C13 F15）。记在这里是为了免得后来的人当成缺陷「修」掉。

        第 2 条 todo（auto 成为缺省档）落地后这条的分量会显著上升：那时子 Agent
        的生效档位也是 auto，能写文件、后台、并行、用户不在场，而②″是唯一挡住
        它写配置的东西。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:PROTECTED_SUBAGENT_WRITES_CONFIG",
            "--seed", "tests.e2e.scripts:seed_protected_subagent",
            "--idle-timeout", "300",
        )
        self._permissive()
        self.send("让 helper 去加那条 hook 规则")

        # 委派不弹面板；子 Agent 的 ASK 自动拒绝，全程无人工介入
        for _ in range(8):
            res = self._wait_quiescent(timeout=90)
            self.assertTrue(res["ok"], res)
            if res["data"]["terminal"] != "pending":
                break
            self.fail(f"全程不该弹面板：{self._panel(res)}")

        view = self.view()
        decisions = [
            e for e in view.of_type("permission_decision")
            if e.get("tool") == "write_file"
        ]
        self.assertTrue(decisions, "子 Agent 的写入应当有判定记录")
        self.assertEqual(decisions[-1].get("decision"), "ask")
        self.assertEqual(decisions[-1].get("layer"), "protected")

        self.assertFalse(
            (self._workspace() / ".rhinecode" / "hooks.yaml").exists(),
            "判 ASK 自动拒绝之后，配置文件绝不该被写出来",
        )

    def test_scenario_3_isolated_subagent_can_still_write(self):
        """
        场景 3（**坑 1 的真机反证，不可省**）：隔离子 Agent 照常能在自己的
        工作区里写文件。

        ⚠ **这条只能在真机上验。** 隔离工作区是
        `<主项目根>/.rhinecode/worktrees/<名字>`——**从主项目根看，它整个人都在
        保护目录里**。判定基准若退回主项目根，它的每一次写入都会命中②″，
        而它非交互、判 ASK 即自动拒绝，于是隔离委派**全部静默失败**，
        界面上只看到「子 Agent 什么都没做出来」。

        单测里这个症状**物理上复现不出来**：那里用的是系统临时目录，
        不在主项目根之下（实现期变异测试实测确认，因此单测那一侧改用了结构判据
        `IsolatedAgentEngineTest.test_the_layer_is_asked_about_the_callers_cwd`）。
        """
        self.start_host(
            "--mode", "scripted",
            "--script", "tests.e2e.scripts:PROTECTED_ISOLATED_SUBAGENT",
            "--seed", "tests.e2e.scripts:seed_protected_isolated",
            "--idle-timeout", "300",
        )
        self._permissive()
        self.send("让 builder 在隔离工作区里新建 feature.py")

        for _ in range(8):
            res = self._wait_quiescent(timeout=120)
            self.assertTrue(res["ok"], res)
            if res["data"]["terminal"] != "pending":
                break
            self.fail(f"全程不该弹面板：{self._panel(res)}")

        view = self.view()
        decisions = [
            e for e in view.of_type("permission_decision")
            if e.get("tool") == "write_file"
        ]
        self.assertTrue(decisions, "隔离子 Agent 的写入应当有判定记录")
        self.assertNotEqual(
            decisions[-1].get("layer"), "protected",
            "隔离子 Agent 写自己工作区里的业务文件绝不该命中②″保护路径"
            "——判定基准必须是它自己的工作目录",
        )
        self.assertEqual(decisions[-1].get("decision"), "allow")

        executed = [
            e for e in view.of_type("tool_execute")
            if e.get("tool") == "write_file" and e.get("outcome") == "executed"
        ]
        self.assertTrue(executed, "写入应当真的执行了")

        # 文件真的落在隔离工作区里（而不是主项目根）。
        # ⚠ 目录名带随机后缀（`builder-0abae7de`），只能 glob，不能拼死名字。
        worktrees = self._workspace() / ".rhinecode" / "worktrees"
        candidates = sorted(worktrees.glob("builder*")) if worktrees.exists() else []
        self.assertTrue(candidates, f"隔离工作区应当还在：{worktrees}")
        self.assertTrue(
            any((d / "feature.py").exists() for d in candidates),
            f"feature.py 应当落在隔离工作区里：{candidates}",
        )
        self.assertFalse(
            (self._workspace() / "feature.py").exists(),
            "它绝不该落到主项目根——那说明隔离没生效",
        )


if __name__ == "__main__":
    unittest.main()
