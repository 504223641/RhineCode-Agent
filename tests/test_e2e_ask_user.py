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

    async def test_free_text_carries_the_checked_ones_in_multi_select(self):
        """
        ⚠ **多选题里选「其它…」，已勾的项要一起交上去**（F15 修订的相邻空白）。

        原写法无条件只回传打的那句话：用户勾了「项目级」、又想补一条自己的，
        那个勾选就凭空没了——而进自由输入态之后勾选**不在屏幕上**，
        他不会发现。「其它…」在多选题里是「补一条」，不是「换一批」。

        步骤与真人一致：回车勾第一项（光标自动前进）→ 下移到「其它…」→
        回车进自由输入 → 打字 → 回车。
        """
        app, provider = self.assemble(
            self._script(_question("要哪几样？", "项目级", "用户级", multi=True))
        )
        async with self.driving(app) as (_pilot, core):
            await asyncio.to_thread(core.send, "问问我")
            await asyncio.to_thread(core.wait, 30.0)

            # 回车勾上第一项（光标自动前进到第二项），再下移一格到「其它…」
            await asyncio.to_thread(core.keys, ["enter", "down", "enter"])
            self.assertTrue(
                app.query_one(ClarifyPanel).display, "自由输入态下面板不该收起"
            )
            await asyncio.to_thread(core.send, "还要一份 README")
            await asyncio.to_thread(core.wait, 30.0)

            events = self._clarify_events()
            self.assertEqual(len(events), 1)
            settled = str(events[0]["result"])
            self.assertIn("还要一份 README", settled, "用户打的字没交上去")
            self.assertIn(
                "项目级", settled,
                "已勾选的项被自由输入吞掉了——用户会以为它还在",
            )
        self.assertGreaterEqual(len(provider.calls), 2)

    async def test_clarify_dispatches_the_notification_hook(self):
        """
        ⚠ **AC6a 第三条：面板弹出时确实分发 `notification` 事件。**

        这条必须有护栏，因为 `CLAUDE.md` 把 `notification` 列为
        「`ask_user` 现在真实可用的三样手段」之一（另两样是可见性判据与跳过熔断）。
        它要是没真的分发，那就是一条**错误的能力承诺**——已知项 #18 的教训
        正是这个形态：文档说得通、实际不生效，而用户照着配了还以为是自己写错了。

        ⚠ 真机验收里这条测不出来：那个工作区没有任何 hook 规则，
        于是记录里一条 hook 事件都没有，**看起来像是没分发**。
        这里直接盯分发动作本身。
        """
        app, _provider = self.assemble(
            self._script(_question("放哪一层？", "项目级", "用户级"))
        )
        seen = []
        original = app._dispatch_hook

        def _spy(event_type, **kw):
            seen.append((getattr(event_type, "value", event_type), kw.get("kind")))
            return original(event_type, **kw)

        app._dispatch_hook = _spy
        async with self.driving(app) as (_pilot, core):
            await asyncio.to_thread(core.send, "问问我")
            await asyncio.to_thread(core.wait, 30.0)
            await asyncio.to_thread(core.answer, "1")
            await asyncio.to_thread(core.wait, 30.0)

        kinds = [k for name, k in seen if name == "notification"]
        self.assertIn(
            "awaiting_clarify", kinds,
            "澄清面板弹出时没有分发 notification——CLAUDE.md 把它列为"
            "`ask_user` 唯三可用手段之一，不分发就是一条错误的能力承诺",
        )

    async def test_the_input_box_is_only_unlocked_in_the_free_text_state(self):
        """
        ⚠ **AC17：自由输入那条岔路必须是窄的，而挡住其余情形的是输入框本身。**

        澄清面板一弹出就 `InputBar.disabled = True`（C4 起的既有行为），
        因此选项列表态下用户**根本打不了字**——守卫那句「请先在面板上做出选择」
        是第二道，不是第一道。本扩展做的是：进自由输入态时**解禁**它，
        退回选项列表时**再锁上**。

        这一条盯的就是那对开关。漏了「退回时锁上」的话，用户按 Esc 从
        自由输入退回选项列表，输入框还开着，他打的字会被守卫的例外分支
        当成一次结算送进回调——而他以为自己只是在聊天。

        ⚠ **本条刻意不走 `core.send`**：驱动器自己在那一层就会拒绝非空闲态的提交，
        于是判据变成「驱动器挡住了」而不是「产品挡住了」，产品侧改坏了也照样绿
        （实测确认过——把产品的例外条件整个去掉，走 `send` 的写法仍然全绿）。
        """
        app, _provider = self.assemble(
            self._script(_question("放哪一层？", "项目级", "用户级"))
        )
        async with self.driving(app) as (_pilot, core):
            await asyncio.to_thread(core.send, "问问我")
            await asyncio.to_thread(core.wait, 30.0)

            bar = app.query_one(InputBar)
            self.assertTrue(bar.disabled, "选项列表态下输入框必须是锁着的")

            # 移到「其它…」并回车 → 解禁
            await asyncio.to_thread(core.keys, ["down", "down", "enter"])
            self.assertFalse(bar.disabled, "自由输入态下输入框必须解禁")

            # Esc 退回选项列表 → **重新锁上**
            await asyncio.to_thread(core.keys, ["escape"])
            self.assertFalse(
                getattr(app, "_clarify_free_text", True), "Esc 应当退回选项列表态"
            )
            self.assertTrue(
                bar.disabled,
                "从自由输入退回之后输入框还开着——用户打的字会被当成一次结算",
            )

            await asyncio.to_thread(core.answer, "1")
            await asyncio.to_thread(core.wait, 30.0)
            self.assertEqual(len(self._clarify_events()), 1)

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
