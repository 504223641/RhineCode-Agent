"""
澄清提问的无头端到端验收（ask-user 扩展，spec AC21、AC26、AC27）。

三条应答路径各一条用例，外加两条边界：

| 用例 | 走哪条路 | 验的是 |
| --- | --- | --- |
| 单选 | `answer 1`（channel） | 最基本的一问一答 |
| 多选 | `answer "0,2" --via keys` | **逐项回车勾选 + 提交行回车**——只有按键路径验得到 |
| 自由输入 | `keys` 选中「其它…」+ `send` 打字 | 产品那条**真人提交入口**的新岔路 |
| 中途跳过 | `answer skip` | 面板确实收起、输入框确实恢复（不是界面假死） |
| 多问题 | 连续两次 `answer` | 中间那次**面板不收** |

⚠ 自由输入刻意**不走** `answer other:…`：`send` 走的是真人提交入口
（`on_input_bar_input_submitted`），正好压在产品新增的那条岔路上；
让驱动器直接结算反而绕开了要验的东西。
"""

from __future__ import annotations

import asyncio

from rhinecode.tui.widgets import ClarifyPanel, InputBar
from tests.e2e.control import SessionState
from tests.e2e.scripted import done, text, tool
from tests.test_e2e_control import DriverFixture, _history_text


def _user_messages(manager) -> int:
    """模型侧历史里 user 角色的消息条数——「有没有开新回合」的实质判据。"""
    return sum(1 for m in manager.history if getattr(m, "role", "") == "user")


def _ask(*questions):
    """造一次 `ask_user` 调用。"""
    return tool("ask_user", {"questions": list(questions)})


def _question(text_, *labels, header="", multi=False):
    payload = {
        "question": text_,
        "options": [{"label": x, "description": f"{x}的说明"} for x in labels],
    }
    if header:
        payload["header"] = header
    if multi:
        payload["multiSelect"] = True
    return payload


class AskUserE2ETest(DriverFixture):
    """三条应答路径 + 两条边界。"""

    def _script(self, *questions, reply="好，按你说的来。"):
        return [
            [text("有几个地方要你定。"), _ask(*questions), done()],
            [text(reply), done()],
        ]

    def _clarify_events(self):
        return [e for e in self.interactions() if e["kind"] == "clarify"]

    # ------------------------------------------------------------------ #
    async def test_single_choice_over_the_channel(self):
        """AC21 第一条：单选走控制通道。"""
        app, provider = self.assemble(
            self._script(_question("先改哪一块？", "先改登录", "先改上传", header="优先级"))
        )
        async with self.driving(app) as (_pilot, core):
            await asyncio.to_thread(core.send, "问问我")
            waited = await asyncio.to_thread(core.wait, 30.0)
            self.assertEqual(waited["data"]["terminal"], SessionState.PENDING.value)

            await asyncio.to_thread(core.answer, "1")
            await asyncio.to_thread(core.wait, 30.0)

            events = self._clarify_events()
            self.assertEqual(len(events), 1)
            self.assertIn("先改上传", str(events[0]["result"]))
            self.assertGreaterEqual(len(provider.calls), 2, "作答后循环必须继续")

    async def test_multi_select_over_the_key_path(self):
        """
        AC21 第二条：多选**走按键路径**。

        ⚠ 这一条是放开 `clarify` + `keys` 的全部理由：多选的核心交互是
        「逐项回车勾选、最后在提交行回车」，走控制通道直接结算的话，那些按键根本没被按过。
        """
        app, provider = self.assemble(
            self._script(
                _question("要包含哪几节？", "概述", "数据", "结论", multi=True)
            )
        )
        async with self.driving(app) as (_pilot, core):
            await asyncio.to_thread(core.send, "问问我")
            await asyncio.to_thread(core.wait, 30.0)

            answered = await asyncio.to_thread(core.answer, "0,2", "keys")
            self.assertTrue(answered["ok"], answered)
            self.assertEqual(answered["data"]["source"], "human", "按键路径来源是真人")
            await asyncio.to_thread(core.wait, 30.0)

            events = self._clarify_events()
            self.assertEqual(len(events), 1)
            result = str(events[0]["result"])
            self.assertIn("概述", result)
            self.assertIn("结论", result)
            self.assertNotIn("数据", result, "没勾的那项不该混进答案")
            self.assertGreaterEqual(len(provider.calls), 2)

    async def test_free_text_over_the_real_input_bar(self):
        """
        AC21 第三条 / AC14b：自由输入走**真人提交入口**。

        步骤刻意与真人一致：按方向键移到「其它…」→ 回车 → 打字 → 回车。
        `send` 走的正是产品新增的那条岔路（`on_input_bar_input_submitted`
        顶部的自由输入分支）。
        """
        app, provider = self.assemble(
            self._script(_question("放哪一层？", "项目级", "用户级"))
        )
        async with self.driving(app) as (_pilot, core):
            await asyncio.to_thread(core.send, "问问我")
            await asyncio.to_thread(core.wait, 30.0)
            user_msgs_before = _user_messages(self.result.manager)

            # 移到「其它…」（第 3 个可选项）并回车 → 进自由输入态
            await asyncio.to_thread(core.keys, ["down", "down", "enter"])
            snap = await asyncio.to_thread(core.snapshot)
            self.assertEqual(
                snap["state"], SessionState.PENDING.value, "还没作答，仍然待决"
            )
            self.assertTrue(
                app.query_one(ClarifyPanel).display, "自由输入态下面板不该收起"
            )
            self.assertFalse(
                app.query_one(InputBar).disabled, "自由输入态下输入框必须可用"
            )

            await asyncio.to_thread(core.send, "放到 docs 下面")
            await asyncio.to_thread(core.wait, 30.0)

            events = self._clarify_events()
            self.assertEqual(len(events), 1)
            self.assertIn("放到 docs 下面", str(events[0]["result"]))

            # ⚠ AC14b：那段文本**不是一条新消息**。
            #
            # 判据分两条，缺一不可：
            # ① 模型侧历史里**没有多出一条 user 消息**——这是「不开新回合」的实质；
            # ② 界面上**没有多出一条用户回显行**（那种行以 `◈` 打头）。
            #
            # ⚠ 不能简单地断言「历史区里搜不到这段文字」：它会出现在**工具行的
            # 规模描述**里（`用户输入：放到 docs 下面`），那是对的、也是有用的。
            self.assertEqual(
                _user_messages(self.result.manager),
                user_msgs_before,
                "自由输入的答案不该被当成一条新的用户消息进历史",
            )
            for line in _history_text(app).splitlines():
                if line.lstrip().startswith("◈"):
                    self.assertNotIn(
                        "放到 docs 下面", line, "它不该被回显成一条用户消息"
                    )
            self.assertGreaterEqual(len(provider.calls), 2)

    async def test_skip_closes_the_panel_and_frees_the_input(self):
        """
        AC27b：中途跳过时面板确实收起、输入框确实恢复。

        ⚠ 这条专盯 `_resolve_interaction` 里那个
        `keep_panel and result is not None`——漏掉后半句时，用户在第 2 题
        （共 3 题）按 Esc 会让**面板永远挂着而输入框永远禁用**，即界面假死。
        """
        app, provider = self.assemble(
            self._script(
                _question("第一题？", "甲", "乙"),
                _question("第二题？", "丙", "丁"),
                _question("第三题？", "戊", "己"),
            )
        )
        async with self.driving(app) as (_pilot, core):
            await asyncio.to_thread(core.send, "问问我")
            await asyncio.to_thread(core.wait, 30.0)

            # 第一题正常作答 —— 面板应当**留着**换成第二题
            await asyncio.to_thread(core.answer, "0")
            await asyncio.to_thread(core.wait, 30.0)
            snap = await asyncio.to_thread(core.snapshot)
            self.assertEqual(snap["state"], SessionState.PENDING.value)
            self.assertIn("第二题", snap["panel"]["display"], "面板要换成第二题")

            # 第二题跳过 —— 剩下的不再问，面板收起，输入框恢复
            await asyncio.to_thread(core.answer, "skip")
            await asyncio.to_thread(core.wait, 30.0)

            self.assertFalse(app.query_one(ClarifyPanel).display, "面板必须收起")
            self.assertFalse(app.query_one(InputBar).disabled, "输入框必须恢复可用")
            events = self._clarify_events()
            self.assertEqual(len(events), 2, "第三题不该再问")
            self.assertGreaterEqual(len(provider.calls), 2, "跳过之后循环必须继续")

    async def test_panel_stays_between_questions(self):
        """
        AC7 / F14：多问题之间**面板不收**。

        断言的是「第二题弹出来时面板一直是可见的」——收了再开在快照上
        看不出区别，所以顺带断言输入框**从未**被解禁（收面板会连带解禁它）。
        """
        app, _provider = self.assemble(
            self._script(
                _question("第一题？", "甲", "乙"),
                _question("第二题？", "丙", "丁"),
            )
        )
        async with self.driving(app) as (_pilot, core):
            await asyncio.to_thread(core.send, "问问我")
            await asyncio.to_thread(core.wait, 30.0)

            await asyncio.to_thread(core.answer, "0")
            await asyncio.to_thread(core.wait, 30.0)

            self.assertTrue(app.query_one(ClarifyPanel).display)
            self.assertTrue(
                app.query_one(InputBar).disabled,
                "两题之间输入框不该被解禁——解禁说明面板收过一次",
            )

            await asyncio.to_thread(core.answer, "1")
            await asyncio.to_thread(core.wait, 30.0)

            events = self._clarify_events()
            self.assertEqual(len(events), 2)
            self.assertEqual([e["question_index"] for e in events], [0, 1])
            self.assertEqual([e["question_total"] for e in events], [2, 2])
            self.assertFalse(app.query_one(ClarifyPanel).display, "最后一题答完才收")


if __name__ == "__main__":
    import unittest

    unittest.main()
