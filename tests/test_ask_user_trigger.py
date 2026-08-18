"""
澄清提问的触发口径护栏（ask-user 扩展 F19 / F20，spec AC18–AC20）。

## 这是同一个坑的第六次

模型要读**好几处文本**才决定「要不要用这个能力」，而那几处分散在不同文件里。
前五次都是真实模型实测才发现两处口径打架：

| 次 | 两（三）处 |
| --- | --- |
| C11 | Skill 清单表头 ↔ `load_skill.description` |
| C13 | 角色清单表头 ↔ `run_agent.description` |
| C14 | 交付信息 ↔ 委派工具描述 |
| C15 | 发消息工具描述 ↔ 注入消息的标记块 |
| todo-list | 待办工具描述 ↔ 系统提示那段 |
| **本次** | **工具描述 ↔ 任务模式 ↔ 计划模式（三处，不是两处）** |

## 本次比前五次多一处

`plan.py` 的 `PLAN_FULL` 也在教模型怎么用 `ask_user`，而且它此前写着
**「一次一个问题」**——F7 支持 1–4 个问题之后那句话当场过期，
会与工具描述**直接打架**。下面 `test_plan_text_no_longer_says_one_question`
就是钉这一条的。
"""

import unittest

from rhinecode.agent.plan_tools import AskUserTool
from rhinecode.agent.prompt.texts.plan import PLAN_FULL
from rhinecode.agent.prompt.texts.task_mode import TASK_MODE


class SameVoiceTest(unittest.TestCase):
    """三处文本必须在同样五层意思上一致。"""

    # ⚠ **五个锚点词的选法有讲究：它们要能自然地出现在三处文本里。**
    #
    # 「一两次」（可数下限）与「2 到 4 个」（用法）都带数字，这是刻意的——
    # 已知项 #17 留下的线索：**模型对有具体可匹配项的指令遵循得好，
    # 对抽象判断系统性偷懒。** 把下限写成「能自行确认的」那种抽象说法，
    # 等于把判断权交回给一个已知会在这上面偷懒的东西。
    _LAYERS = ("拍板", "一两次", "默认做法", "已经说过", "2 到 4 个")

    def setUp(self) -> None:
        self.texts = {
            "工具描述": AskUserTool.description,
            "任务模式": TASK_MODE,
            "计划模式": PLAN_FULL,
        }

    def test_every_layer_appears_in_all_three(self) -> None:
        """五层意思逐个在三处都要出现——这就是「同口径」的完整定义。"""
        for layer in self._LAYERS:
            for name, text in self.texts.items():
                with self.subTest(layer=layer, text=name):
                    self.assertIn(layer, text, f"{name}缺了「{layer}」这一层")

    def test_the_guard_covers_five_layers(self) -> None:
        """
        ⚠ **护栏自身的护栏。**

        上面那条遍历 `_LAYERS`，因此**把某一层从表里删掉**会让它静默变弱：
        测试照样全绿，而三处文本从此可以在那一层上自由分叉。
        **这正是前五次共同的失效方式**——护栏在，但它管的东西被悄悄缩小了。

        钉住数量之后，缩表就必须在这里改，改的时候人会读到这段说明。
        """
        self.assertEqual(len(self._LAYERS), 5)
        self.assertEqual(len(set(self._LAYERS)), 5, "五层意思不能有重复")

    def test_the_guard_covers_three_texts(self) -> None:
        """
        同上，钉住「三处」这个数量。

        ⚠ 本次比前五次多一处（`PLAN_FULL`），而多出来的那处恰恰是最容易
        被漏掉的——它在另一个文件里，且此前的说法与新行为**直接矛盾**。
        """
        self.assertEqual(len(self.texts), 3)

    def test_the_three_texts_are_not_the_same_string(self) -> None:
        """
        ⚠ **反向约束：同口径不等于同一份文本。**

        合成一份共享常量看起来更「不会漏改」，实际会毁掉这条护栏的用途：
        三处的**受众时刻不同**——工具描述是模型「决定要不要调它」时查的说明书，
        任务模式那段**每一轮都发**（必须能被扫读），计划模式那段只在规划时读。
        篇幅、语气、举例都该不一样。

        真正要一致的是那**五层意思**，不是字面。
        """
        values = list(self.texts.values())
        for i, a in enumerate(values):
            for b in values[i + 1:]:
                self.assertNotEqual(a, b)

    def test_plan_text_no_longer_says_one_question(self) -> None:
        """
        ⚠ **反证：计划模式那段不能再写「一次一个问题」。**

        F7 落地后一次可以问 1–4 个，那句话与工具描述**直接矛盾**。
        两处打架时模型听谁的没有定论，而这种矛盾在界面上完全看不出来。
        """
        self.assertNotIn("一次一个问题", PLAN_FULL)
        self.assertNotIn("逐一提问", PLAN_FULL)

    def test_no_text_still_uses_the_old_field_names(self) -> None:
        """
        ⚠ 反证：三处都不能再提旧字段名。

        字段名已按 F7 对齐官方（`label` / `description`）。提示词里留着
        `summary` / `detail` 会让模型按旧名字写参数——解析层虽然认，
        但那是兜底不是正路，而且它会**永远认不出自己写错了**。
        """
        for name, text in self.texts.items():
            with self.subTest(text=name):
                self.assertNotIn("summary（", text)
                self.assertNotIn("detail（", text)


class ManyShotExamplesTest(unittest.TestCase):
    """
    ⚠ **描述里那八个示例是本工具唯一有效的触发手段，护栏钉住它们还在。**

    背景（`docs/extensions/todo-list/acceptance-live.md`）：静态规则那条路
    走到头了——能力描述 → 带可匹配条件的指令 → 每轮 `<system-reminder>`，
    三个杠杆逐个加上，两个模型在一个明确五步的任务上仍然**各 0 次**调用；
    差距查到是 Claude Code 那份描述有**八个带 reasoning 的正反示例**，
    占了三分之二篇幅，而我们此前**全是规则**。

    因此「顺手删几个例子省 token」是这段文本最可能的退化方式。
    """

    def setUp(self) -> None:
        self.description = AskUserTool.description

    def test_four_positive_and_four_negative(self) -> None:
        """正反各四个，一个都不能少。"""
        self.assertEqual(self.description.count("**该问**"), 4)
        self.assertEqual(self.description.count("**不要问**"), 4)

    def test_every_example_carries_a_reason(self) -> None:
        """
        每个例子都要带判据。

        ⚠ **没有 reasoning 的例子退化成一张清单**，模型只能照着字面匹配场景，
        遇到清单外的情形一律不触发。带上「为什么」它才能外推。
        """
        blocks = self.description.count("<例子>")
        self.assertEqual(blocks, 8)
        self.assertEqual(self.description.count("判断："), 8)

    def test_a_negative_example_sits_right_on_the_lower_bound(self) -> None:
        """
        ⚠ **负例里必须有一个贴着下限的临界情形。**

        「pytest 还是 unittest」形式上**完全符合**「有几种都说得通的做法」
        ——这正是它有价值的原因：它教模型区分「形式上像选择题」与
        「真的没有答案」。

        而「用户刚说过的」那类模型本来就不会误判，全放那种等于白占篇幅。
        """
        self.assertIn("pytest 还是 unittest", self.description)
        self.assertIn("形式上像选择题", self.description)

    def test_no_unbounded_push(self) -> None:
        """
        ⚠ **反证：描述里不许出现没有下限的推力词。**

        「拿不准就问」「宁可多问」这类单向推力正是已知项 #17 翻车的形态
        （C13 委派为治欠触发写下四条推力，结果用户实测「一个非常简单的
        任务都要让子 Agent 去做」）。
        """
        for push in ("拿不准就问", "宁可多问", "不确定就问", "尽量多问", "有疑问就问"):
            with self.subTest(push=push):
                self.assertNotIn(push, self.description)

    def test_examples_take_most_of_the_description(self) -> None:
        """
        ⚠ 示例应当占大头，规则只占开头。

        这条对齐 Claude Code 那份 TodoWrite 描述的结构（三分之二是示例）。
        它不是审美要求——规则要模型做抽象判断，示例把它降级成模式匹配，
        而后者才是实测有效的那个。
        """
        head, _, examples = self.description.partition("<例子>")
        self.assertGreater(
            len(examples), len(head),
            "示例部分比规则部分还短了——这段文本正在退回「全是规则」的形态",
        )


if __name__ == "__main__":
    unittest.main()
