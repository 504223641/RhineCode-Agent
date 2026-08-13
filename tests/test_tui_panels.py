"""
四个面板的呈现（tui-display 扩展 E 组 T33/T34/T36，spec F23/F24/F25/F26 /
AC16/AC17/AC19）。

## 本组只改**呈现**，交互契约一字不动（F27/N8）

选项标识（`option.id`）、结算路径、Esc 语义、焦点转移规则、以及「会话面板期间
禁用输入框」全部保持——本文件因此有一整个 TestCase 专门钉住「id 没变」。
那些 id 是协调层解析结果的唯一依据，改了会让「点了本次放行却按拒绝处理」
这类事故发生，而界面上完全看不出来。

## 三条容易写错的地方

1. **序号只给可选项**。表头、澄清的详情行、会话面板里锁定/当前那两条都不占号
   ——占了的话按 `2` 会落到一行说明文字上，或者序号跳号、用户按下的数字对应
   的是另一条；
2. **高亮指示符要跟着 `highlighted` 走**，且换前缀的方式不能自激；
3. **`[锁定]` / `[当前]` 用文字**而不是 `🔒`：用户需要知道的是「为什么点不了」。
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from textual.app import App, ComposeResult

from rhinecode.agent.events import ClarifyOption
from rhinecode.memory.session import SessionInfo
from rhinecode.provider.base import ToolCall
from rhinecode.tui.widgets import ClarifyPanel, ConfirmPanel, SessionPanel


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield ConfirmPanel()
        yield ClarifyPanel()
        yield SessionPanel()


def _prompts(panel) -> list[str]:
    """取面板里每一行当前的展示文本（含前缀与序号）。"""
    return [str(option.prompt) for option in panel._options]


def _decision(reason: str = "默认模式下无规则命中", layer: str = "mode"):
    """
    造一个决策结果。

    ⚠ `layer` 缺省是 `"mode"`（第④层兜底）——**那才是真实世界里绝大多数
    确认面板的来源**，也是「原因恒定、没有分辨力」那条规则适用的场合。
    要验「别的层的原因仍然显示」，显式传 `layer="hook"` 之类。
    """
    return SimpleNamespace(
        reason=reason, kind="path", host="", layer=SimpleNamespace(value=layer)
    )


CLARIFY_OPTIONS = [
    ClarifyOption(summary="历史区下方按需出现", detail="不占空间，位置贴近输入框视线焦点"),
    ClarifyOption(summary="历史区上方常驻", detail="位置稳定不跳动，但空转时白占两行"),
]


def _sessions():
    return [
        SessionInfo(
            session_id="20260811-a3f1c9",
            path=Path("20260811-a3f1c9.jsonl"),
            title="调研权限层",
            message_count=42,
            last_time=datetime(2026, 8, 11, 14, 2),
            locked=False,
        ),
        SessionInfo(
            session_id="20260810-7b2e10",
            path=Path("20260810-7b2e10.jsonl"),
            title="修 hook",
            message_count=17,
            last_time=datetime(2026, 8, 10, 9, 11),
            locked=True,
        ),
    ]


class NumberingTest(unittest.IsolatedAsyncioTestCase):
    """AC16a / AC16c：可选项带序号，不可选行不占号。"""

    async def test_confirm_options_are_numbered_one_to_four(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.show_for(
                ToolCall(id="c1", name="write_file", arguments={"path": "docs/notes.md"}),
                None,
                _decision(),
            )
            await pilot.pause()

            lines = _prompts(panel)
            self.assertEqual(len(lines), 5, "一行表头 + 四个选项")
            for number, index in enumerate(range(1, 5), start=1):
                self.assertIn(f"{number}. ", lines[index])

    async def test_confirm_header_takes_no_number(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.show_for(ToolCall(id="c1", name="write_file", arguments={}), None, _decision())
            await pilot.pause()
            self.assertNotIn("1. ", _prompts(panel)[0])

    async def test_clarify_detail_rows_take_no_number(self) -> None:
        """
        澄清面板每个候选下面那行详情不可选，**不占号**。

        占了的话按 `2` 会落到一行说明文字上——那正是 spec 里点名的坑。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ClarifyPanel)
            panel.show_for("活动区应该放在界面的哪个位置？", CLARIFY_OPTIONS)
            await pilot.pause()

            lines = _prompts(panel)
            self.assertIn("1. 历史区下方按需出现", lines[1])
            self.assertNotIn("2. ", lines[2], "详情行不该占号")
            self.assertIn("2. 历史区上方常驻", lines[3])

    async def test_session_numbers_skip_unselectable_rows(self) -> None:
        """
        会话面板里锁定 / 当前那两条不可选，**不占号**。

        占了的话序号会跳号，而用户按下的那个数字对应的是另一条。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(SessionPanel)
            panel.show_for(_sessions(), current_id="")
            await pilot.pause()

            lines = "\n".join(_prompts(panel))
            self.assertIn("1. 20260811-a3f1c9", lines)
            self.assertNotIn("2. ", lines, "唯一可选的那条之外不该再有序号")

    async def test_choice_index_maps_numbers_to_rows(self) -> None:
        """数字键选中所依赖的映射：序号（从 1 起）→ 行下标。"""
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.show_for(ToolCall(id="c1", name="write_file", arguments={}), None, _decision())
            await pilot.pause()

            self.assertEqual(panel.choice_index(1), 1)
            self.assertEqual(panel.choice_index(4), 4)
            self.assertIsNone(panel.choice_index(5), "越界必须返回 None，不能环绕")
            self.assertIsNone(panel.choice_index(0))


class HighlightIndicatorTest(unittest.IsolatedAsyncioTestCase):
    """AC17：当前高亮项有独立于颜色的指示符 `>`。"""

    async def test_indicator_follows_the_highlight(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.show_for(ToolCall(id="c1", name="write_file", arguments={}), None, _decision())
            await pilot.pause()

            before = _prompts(panel)
            self.assertTrue(before[1].startswith("> "), f"默认高亮项要带指示符：{before[1]}")
            self.assertFalse(before[2].startswith("> "))

            panel.highlighted = 2
            await pilot.pause()

            after = _prompts(panel)
            self.assertFalse(after[1].startswith("> "), "旧行的指示符要摘掉")
            self.assertTrue(after[2].startswith("> "), "新行要带上")

    async def test_watch_does_not_self_trigger(self) -> None:
        """
        **反证**：换前缀不得改动 `highlighted` 本身。

        `replace_option_prompt_at_index` 不碰高亮，所以不会触发第二次 watch。
        换成「清空重建选项」的写法就会自激（重建重置高亮 → 再次触发）——
        那是一个安静的无限循环。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.show_for(ToolCall(id="c1", name="write_file", arguments={}), None, _decision())
            await pilot.pause()

            panel.highlighted = 3
            await pilot.pause()
            self.assertEqual(panel.highlighted, 3, "watch 不该把高亮改回去")

    async def test_prefix_width_is_stable(self) -> None:
        """选中与未选中等宽，高亮移动时整列文字不左右抖。"""
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.show_for(ToolCall(id="c1", name="write_file", arguments={}), None, _decision())
            await pilot.pause()

            lines = _prompts(panel)
            self.assertEqual(lines[1].index("1."), lines[2].index("2."))


class NoEmojiTest(unittest.IsolatedAsyncioTestCase):
    """AC19：四个面板里搜不到表情，且被替换处的语义仍可辨。"""

    async def _all_text(self) -> str:
        app = _Harness()
        async with app.run_test() as pilot:
            confirm = app.query_one(ConfirmPanel)
            confirm.show_for(
                ToolCall(id="c1", name="write_file", arguments={"path": "docs/notes.md"}),
                None,
                _decision(),
            )
            clarify = app.query_one(ClarifyPanel)
            clarify.show_for("放哪个位置？", CLARIFY_OPTIONS)
            session = app.query_one(SessionPanel)
            session.show_for(_sessions(), current_id="20260811-a3f1c9")
            await pilot.pause()
            return "\n".join(
                _prompts(confirm) + _prompts(clarify) + _prompts(session)
            )

    async def test_no_emoji_anywhere(self) -> None:
        text = await self._all_text()
        for glyph in ("✅", "🟢", "💾", "❌", "❓", "📂", "🔒", "📋", "⚠", "⭐"):
            with self.subTest(glyph=glyph):
                self.assertNotIn(glyph, text)

    async def test_locked_and_current_are_readable_as_words(self) -> None:
        """
        F30：被替换掉的语义必须由**文字**显式承担，不能只靠颜色。

        用户需要知道的是「为什么这条点不了」，`🔒` 说不清是锁定还是别的。
        """
        text = await self._all_text()
        self.assertIn("[锁定]", text)
        self.assertIn("[当前]", text)

    async def test_confirm_header_uses_the_primary_arg_form(self) -> None:
        """
        E 组做完的样子：确认面板的工具名也走 B 组口径。

        用户就是靠这一行决定放不放行的，`path=` 这类键名在这里同样只占地方。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.set_primary_args({"write_file": "path"})
            panel.show_for(
                ToolCall(id="c1", name="write_file", arguments={"path": "docs/notes.md"}),
                None,
                _decision(),
            )
            await pilot.pause()

            header = _prompts(panel)[0]
            self.assertIn("Write(docs/notes.md)", header)
            self.assertNotIn("path=", header)

    async def test_confirm_header_does_not_show_the_decision_reason(self) -> None:
        """
        **判定原因不进表头**（真机反馈）。

        它原本拼在工具名后面，想说明「为什么停下来问」。但绝大多数确认走的是
        第④层兜底，那句话恒为「默认模式：无规则命中」——每次都一样、对
        「放不放行」没有任何帮助，纯粹把真正要读的 `工具名(参数)` 挤到一边。

        ⚠ 有分辨力的原因仍然看得见：URL 类请求下面单独有「判定来自：…」
        那几行（`_url_detail_lines`），完整判定链在 `--trace` 里。
        本条只钉住「不要把那句恒定的兜底话铺在表头上」。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.set_primary_args({"write_file": "path"})
            panel.show_for(
                ToolCall(id="c1", name="write_file", arguments={"path": "a.md"}),
                None,
                _decision("默认模式下无规则命中"),
            )
            await pilot.pause()
            self.assertNotIn("无规则命中", _prompts(panel)[0])

    async def test_confirm_header_keeps_reasons_from_other_layers(self) -> None:
        """
        **反证**：只压第④层那句恒定的兜底话，**别的层的原因必须留着**。

        Hook / 规则 / 沙箱 / 网络边界给出的原因是**这一次特有**的，而且往往是
        用户唯一能看到它的地方——比如「Hook 规则「x」（来源：y）要求这次调用
        由你确认」。一刀砍掉的后果实测过：`test_e2e_hooks` 场景 2 当场红，
        **用户再也看不出这次面板是哪条 Hook 规则要求弹的**。

        没有这条反证，把上一条实现成「无条件不显示」照样全绿。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.set_primary_args({"read_file": "path"})
            panel.show_for(
                ToolCall(id="c1", name="read_file", arguments={"path": "a.md"}),
                None,
                _decision("Hook 规则「审计」要求这次调用由你确认。涉及敏感文件", layer="hook"),
            )
            await pilot.pause()
            header = _prompts(panel)[0]
            self.assertIn("Hook 规则", header)
            self.assertIn("敏感文件", header)

    async def test_option_hints_line_up(self) -> None:
        """
        四个选项右边的**说明必须起于同一列**（真机反馈）。

        主文本宽度不一（「拒绝」4 格 / 「本会话放行」10 格），
        直接拼两个空格会让说明参差不齐，一眼扫过去像四段互不相干的话。

        ⚠ 判据必须按**显示宽度**（`cell_len`）算：中文一个字占两格，
        按字符数量出来的「对齐」在屏幕上照样是歪的——而这正是最容易
        写错、且测试还会绿的地方。
        """
        from rich.cells import cell_len
        from rich.text import Text

        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.set_primary_args({"write_file": "path"})
            panel.show_for(
                ToolCall(id="c1", name="write_file", arguments={"path": "a.md"}),
                None,
                _decision(),
            )
            await pilot.pause()

            # 四个可选项：取「说明」起始处的显示宽度。
            # ⚠ 不能按「第一处两个空格」切——补齐用的空格本身就是变长的，
            # 那样切出来的是主文本末尾而不是说明开头（写的时候正是这么错的）。
            # 说明整段包在 `[dim]…[/dim]` 里，按标记切才对。
            starts = set()
            for _index, markup in panel._choices:
                head, sep, _tail = markup.partition("[dim]")
                self.assertTrue(sep, f"这一项没有说明列：{markup!r}")
                starts.add(cell_len(Text.from_markup(head).plain))

            self.assertEqual(
                len(starts), 1, f"说明列没有对齐，起始列有 {sorted(starts)}"
            )


class ContractUnchangedTest(unittest.IsolatedAsyncioTestCase):
    """
    AC18a / F27：**交互契约一字不动**——本组只改呈现。

    选项 id 是协调层解析结果的唯一依据。改了会让「点了本次放行却按拒绝处理」
    这类事故发生，而界面上完全看不出来。
    """

    async def test_confirm_option_ids(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.show_for(ToolCall(id="c1", name="write_file", arguments={}), None, _decision())
            await pilot.pause()

            ids = [o.id for o in panel._options]
            self.assertEqual(ids, [None, "yes", "yes_session", "yes_permanent", "no"])

    async def test_plan_prompt_ids(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.show_prompt("计划已就绪，是否开始执行？", "开始执行", "暂不执行")
            await pilot.pause()

            self.assertEqual([o.id for o in panel._options], [None, "yes", "no"])

    async def test_clarify_ids_are_option_indexes(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(ClarifyPanel)
            panel.show_for("问题？", CLARIFY_OPTIONS)
            await pilot.pause()

            ids = [o.id for o in panel._options if o.id is not None]
            self.assertEqual(ids, ["0", "1"])

    async def test_session_ids_are_full_session_ids(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(SessionPanel)
            panel.show_for(_sessions(), current_id="")
            await pilot.pause()

            ids = [o.id for o in panel._options if o.id is not None]
            self.assertEqual(ids, ["20260811-a3f1c9"])

    async def test_locked_and_current_stay_disabled(self) -> None:
        """
        锁定项与当前会话仍然不可选——这是既有的 UX 优化（导航自动跳过，
        从源头避免「选中后才报错」的挫败感），不能因为改了呈现就丢掉。
        """
        app = _Harness()
        async with app.run_test() as pilot:
            panel = app.query_one(SessionPanel)
            panel.show_for(_sessions(), current_id="20260811-a3f1c9")
            await pilot.pause()

            self.assertEqual([o.id for o in panel._options if o.id is not None], [])


class DigitKeyTest(unittest.IsolatedAsyncioTestCase):
    """
    AC16b / AC18c：面板挂起时数字键直选；没有面板时数字照常进输入框。

    ⚠ 用**真实的 App**（`test_command_tui` 那套桩件装出来的）而不是裸面板：
    数字键的全部逻辑在 `RhineApp.on_key` 里，脱开它验的就只是 `choice_index`
    ——那个已经由上面的 `NumberingTest` 钉过了。
    """

    def _app(self):
        from tests.test_command_tui import _make_app

        app, _manager = _make_app()
        return app

    @staticmethod
    def _pending(kind: str = "confirm") -> dict:
        """造一个形态与 `_interact` 一致的待决盒（结算方会读它的 kind）。"""
        return {
            "event": threading.Event(),
            "result": None,
            "kind": kind,
            "source": "human",
        }

    async def test_digit_selects_the_matching_option(self) -> None:
        app = self._app()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            box = self._pending()
            app._pending_interaction = box
            panel.show_for(
                ToolCall(id="c1", name="write_file", arguments={"path": "a.txt"}),
                None,
                _decision(),
            )
            # 与真实路径一致：面板弹出时 App 会把焦点移过去（见 `_interact`）。
            # 不移的话按键被输入框吃掉，压根到不了 `App.on_key`——那验的是
            # 另一件事（而且恰好是下面那条「无面板时数字进输入框」的机制）。
            panel.focus()
            await pilot.pause()

            await pilot.press("2")
            await pilot.pause()

            from rhinecode.agent.events import ConfirmDecision

            self.assertIsNone(app._pending_interaction, "结算之后待决盒应已清空")
            self.assertIs(
                box["result"],
                ConfirmDecision.ALLOW_SESSION,
                "按 `2` 必须结算成第二项「本会话放行」",
            )

    async def test_out_of_range_digit_does_nothing(self) -> None:
        """
        面板只有四项时按 `7` 什么都不该发生——**尤其不能环绕到第 1 项**，
        那会让人误选。
        """
        app = self._app()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            app._pending_interaction = self._pending()
            panel.show_for(ToolCall(id="c1", name="write_file", arguments={}), None, _decision())
            panel.focus()
            await pilot.pause()
            highlighted = panel.highlighted

            await pilot.press("7")
            await pilot.pause()

            self.assertIsNotNone(app._pending_interaction, "越界不该结算")
            self.assertEqual(panel.highlighted, highlighted, "越界不该移动高亮")

    async def test_digit_goes_to_the_input_when_no_panel(self) -> None:
        """
        **最要紧的一条反证**：没有面板时数字必须照常落进输入框。

        拦错了的话用户再也打不出带数字的消息，而这在有面板的用例里完全测不出来。
        """
        from rhinecode.tui.widgets import InputBar

        app = self._app()
        async with app.run_test() as pilot:
            bar = app.query_one(InputBar)
            bar.focus()
            await pilot.press("2")
            await pilot.pause()

            self.assertEqual(bar.value, "2")

    async def test_zero_is_never_a_choice(self) -> None:
        """`0` 不是任何一项的序号，它该照常进输入框。"""
        from rhinecode.tui.widgets import InputBar

        app = self._app()
        async with app.run_test() as pilot:
            panel = app.query_one(ConfirmPanel)
            panel.show_for(ToolCall(id="c1", name="write_file", arguments={}), None, _decision())
            bar = app.query_one(InputBar)
            bar.focus()
            await pilot.pause()

            await pilot.press("0")
            await pilot.pause()
            self.assertEqual(bar.value, "0")


if __name__ == "__main__":
    unittest.main()
