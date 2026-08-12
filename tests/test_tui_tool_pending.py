"""
`tool_pending` 的护栏：**工具调用参数生成期，界面必须有活体状态。**

## 这条测试在防什么（一个真实观测到的缺口）

现象（用户报的原话）：「模型准备写一个文件，感觉他是直接准备好写的内容了，但是
没有一个状态，等弹出写文件确认我点击确认，直接就显示出写好的文件和写文件的状态」。

用端到端驱动设施实测到的时间线（trace 原文，`write_file` 场景）：

    09:11:55.464  ui_message   [user_echo] 写个文件
    09:12:01.317  agent_event  tool_start · write_file      ← 5.85 秒后才第一次出现
    09:12:01.319  tool_execute write_file · ok=True · 0.0ms
    09:12:01.319  agent_event  tool_result · write_file

`tool_start` 到 `tool_result` 只隔 **2 毫秒**——那行橘色的「执行中… 0s」客观存在，
但只活了 2 毫秒，人眼看不见。写盘本身不花时间，花时间的是**模型生成那段
`content`**，而那段时间里三个环节叠出一个完全静音的窗口：

1. `provider/deepseek.py` 把 `tool_call` 块压到**整条流结束后**才产出——工具名其实
   第一个碎片就到了；
2. `agent/collector.py` 对 `tool_call` 块**刻意不产展示事件**；
3. `agent/loop.py` 的 `TOOL_START` 排在权限判定与确认面板**之后**。

加上 TUI 里没有任何 spinner，写一个几百行的文件时，从模型开始吐参数到确认面板弹出
的几十秒里，聊天窗口**一个事件都收不到**。

## 为什么护栏要写成这个形状

分四层各自钉住，因为这四层**任何一层退回原状都会让整条链重新静音**，而且退回后
其余三层的测试照样全绿：

- Provider 层：`tool_pending` 必须在**流还没结束时**就发出来（用「流中途」断言，
  不是「发过」——只断言发过的话，把它挪回流末尾也能通过）；
- Collector 层：`tool_pending` **不得**累积进 `tool_calls`（累积进去会让同一次调用
  被执行两遍，第二遍参数为 None）；
- Widget 层：pending 行转执行态必须**复用同一行**且**重新起算耗时**；
- App 层：没等到结果的行必须收尾，不留永远转圈的橘色行。
"""

from __future__ import annotations

import re
import unittest

from textual.app import App, ComposeResult

from rhinecode.agent.collector import StreamCollector
from rhinecode.agent.events import AgentEvent, AgentEventType, StopReason
from rhinecode.config import Config
from rhinecode.provider.base import StreamChunk, ToolCall
from rhinecode.tui.widgets import HistoryView, ToolCallWidget


# --------------------------------------------------------------------------- #
# SDK 流式对象的替身：只实现 Provider 真正读到的那几个属性
# --------------------------------------------------------------------------- #


class _FakeFunction:
    def __init__(self, name: str = "", arguments: str = "") -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCallDelta:
    def __init__(self, index: int, id: str = "", function=None) -> None:
        self.index = index
        self.id = id
        self.function = function


class _FakeDelta:
    def __init__(self, tool_calls=None, content: str = "") -> None:
        self.tool_calls = tool_calls
        self.content = content
        self.reasoning_content = None


class _FakeChoice:
    def __init__(self, delta) -> None:
        self.delta = delta


class _FakeChunk:
    def __init__(self, delta) -> None:
        self.choices = [_FakeChoice(delta)]
        self.usage = None


def _fragment(index: int, id: str = "", name: str = "", arguments: str = "") -> _FakeChunk:
    """造一个「工具调用碎片」块，形态与 OpenAI 兼容协议的 delta.tool_calls 一致。"""
    return _FakeChunk(
        _FakeDelta(
            tool_calls=[
                _FakeToolCallDelta(
                    index=index, id=id, function=_FakeFunction(name, arguments)
                )
            ]
        )
    )


def _deepseek_with_stream(chunks: list, pulled: "list | None" = None) -> "object":
    """
    构造一个 DeepSeekProvider，其 SDK 客户端产出给定的流式块序列。

    :param chunks: 要产出的块序列
    :param pulled: 传入一个列表时，SDK 替身会**惰性**产出并把每个块的下标记进去。
                   这是「Provider 到底在流的哪个位置播报」的唯一硬证据——见
                   `test_pending_arrives_before_stream_ends`
    """
    from rhinecode.provider.deepseek import DeepSeekProvider

    provider = DeepSeekProvider(
        Config(
            protocol="deepseek",
            model="test-model",
            base_url="http://test",
            api_key="test-key",
            debug_log=False,
        )
    )

    def _lazy():
        for index, chunk in enumerate(chunks):
            if pulled is not None:
                pulled.append(index)
            yield chunk

    class FakeCompletions:
        def create(self, **kwargs):
            return _lazy()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    provider._client = FakeClient()
    return provider


class ProviderAnnouncesPendingMidStreamTest(unittest.TestCase):
    """Provider 必须在流**进行中**播报 tool_pending，而不是等流结束。"""

    def test_pending_arrives_before_stream_ends(self) -> None:
        """
        **本文件最核心的一条**：播报必须发生在流**还没读完**的时候。

        写文件的典型碎片序列：首片给 id + 名字，随后若干片才慢慢拼出 content
        （真实场景下这几十上百片要花几十秒）。

        判据用的是「拿到 pending 时，上游被拉取了几个块」——**不是**事件的先后顺序。
        顺序断言在这里是无效护栏：把 `yield` 挪回 `for chunk in stream` 循环外面
        （也就是缺陷原样复现），产出序列**照样**是 pending → tool_call → done，
        用例会绿。而惰性拉取的计数骗不过去：真正在流中途播报，此刻只该拉过第 0 块；
        挪到流末尾则四块全被拉完。
        """
        chunks = [
            _fragment(0, id="call_1", name="write_file"),
            _fragment(0, arguments='{"path": "a.txt", '),
            _fragment(0, arguments='"content": "第一行\\n'),
            _fragment(0, arguments='第二行"}'),
        ]
        pulled: list = []
        provider = _deepseek_with_stream(chunks, pulled)

        for chunk in provider.stream_chat([], tools=[{"x": 1}]):
            if chunk.type == "tool_pending":
                break
        else:  # pragma: no cover - 播报丢失时才走到
            self.fail("整条流跑完都没有 tool_pending")

        self.assertEqual(
            pulled, [0],
            "拿到 tool_pending 时上游只该被拉过第一个碎片；"
            f"实际拉了 {len(pulled)} 个，说明播报被推迟到了流的更后面",
        )

    def test_pending_announced_once_per_call(self) -> None:
        """
        参数碎片会到达几十上百次，**每次调用只播报一次**。

        没有这条，一次写文件会往事件流里灌进上百条 pending——量级与 text 增量相同，
        时间线会被淹掉（这正是 trace 的 F17 裁决刻意不记 TEXT 的理由）。
        """
        chunks = [_fragment(0, id="call_1", name="write_file")]
        chunks += [_fragment(0, arguments="x") for _ in range(40)]
        provider = _deepseek_with_stream(chunks)

        pendings = [
            c for c in provider.stream_chat([], tools=[{"x": 1}]) if c.type == "tool_pending"
        ]

        self.assertEqual(len(pendings), 1)

    def test_pending_carries_name_and_id_but_no_arguments(self) -> None:
        """播报的载荷只有 id 与工具名；arguments 必须是 None（那时还没拼完）。"""
        chunks = [
            _fragment(0, id="call_1", name="write_file"),
            _fragment(0, arguments='{"path": "a.txt"}'),
        ]
        provider = _deepseek_with_stream(chunks)

        pending = next(
            c for c in _deepseek_stream(provider) if c.type == "tool_pending"
        )

        self.assertEqual(pending.tool_call.id, "call_1")
        self.assertEqual(pending.tool_call.name, "write_file")
        self.assertIsNone(pending.tool_call.arguments)

    def test_no_pending_without_id(self) -> None:
        """
        协议没给 id 时**安静地不播报**，退回旧行为。

        id 是界面把这行状态与后续 tool_start / tool_result 对上的唯一键。缺了它就
        猜不出该更新哪一行，宁可不显示，也不能造出一行永远转圈的孤儿状态。
        """
        chunks = [
            _fragment(0, name="write_file"),
            _fragment(0, arguments='{"path": "a.txt"}'),
        ]
        provider = _deepseek_with_stream(chunks)

        types = [c.type for c in provider.stream_chat([], tools=[{"x": 1}])]

        self.assertNotIn("tool_pending", types)
        # 但完整调用照旧产出——不播报不等于把这次调用弄丢了
        self.assertIn("tool_call", types)

    def test_parallel_calls_each_announced(self) -> None:
        """并行的多个调用各自播报一次，按 index 区分。"""
        chunks = [
            _fragment(0, id="call_1", name="read_file"),
            _fragment(1, id="call_2", name="grep_content"),
            _fragment(0, arguments='{"path": "a"}'),
            _fragment(1, arguments='{"pattern": "b"}'),
        ]
        provider = _deepseek_with_stream(chunks)

        names = [
            c.tool_call.name
            for c in provider.stream_chat([], tools=[{"x": 1}])
            if c.type == "tool_pending"
        ]

        self.assertEqual(names, ["read_file", "grep_content"])


def _deepseek_stream(provider):
    """跑一次带工具的流并返回全部块（把 tools 非空这个前提收在一处）。"""
    return list(provider.stream_chat([], tools=[{"x": 1}]))


class CollectorMapsPendingTest(unittest.TestCase):
    """收集器把 tool_pending 翻成展示事件，且**绝不**累积进 tool_calls。"""

    def test_pending_becomes_display_event(self) -> None:
        collector = StreamCollector()
        tc = ToolCall(id="c1", name="write_file", arguments=None)

        event = collector.feed(StreamChunk(type="tool_pending", tool_call=tc))

        self.assertIsNotNone(event)
        self.assertEqual(event.type, AgentEventType.TOOL_PENDING)
        self.assertIs(event.tool_call, tc)

    def test_pending_not_accumulated(self) -> None:
        """
        **这条是安全护栏，不只是整洁性要求。**

        `tool_calls` 是循环用来决定「要执行哪些调用」的清单。把 pending 也累积进去，
        同一次调用会出现两遍：第二遍的 arguments 为 None，于是模型莫名收到一条
        「参数解析失败」，而写盘已经真的发生过一次了。
        """
        collector = StreamCollector()
        collector.feed(
            StreamChunk(
                type="tool_pending",
                tool_call=ToolCall(id="c1", name="write_file", arguments=None),
            )
        )
        collector.feed(
            StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="c1", name="write_file", arguments={"path": "a"}),
            )
        )

        self.assertEqual(len(collector.tool_calls), 1)
        self.assertEqual(collector.tool_calls[0].arguments, {"path": "a"})

    def test_pending_without_payload_is_ignored(self) -> None:
        """载荷缺失时返回 None，不产出一个 tool_call 为 None 的残废事件。"""
        collector = StreamCollector()
        self.assertIsNone(collector.feed(StreamChunk(type="tool_pending")))


class _Harness(App):
    """只挂一个历史区的最小应用，用于让工具行真的经历挂载与渲染。"""

    def compose(self) -> ComposeResult:
        yield HistoryView()


def _text_of(widget) -> str:
    """
    取出工具行当前展示的文本，屏蔽 `Static.update` 收到的三种形态差异。

    进行中态传的是 markup 字符串（含 `[#FFA500]` 这类标签，断言时无妨）；
    定色态传的是 `RichGroup`——直接 `str()` 只会得到对象表示，必须逐个取子元素的
    `.plain`。测试关心的是「用户看到了什么字」，所以在这里统一拍平成纯文本。
    """
    content = widget.content
    if isinstance(content, str):
        return content
    renderables = getattr(content, "renderables", None) or [content]
    parts = []
    for item in renderables:
        plain = getattr(item, "plain", None)
        parts.append(plain if plain is not None else str(item))
    return "\n".join(parts)


class PendingWidgetTest(unittest.IsolatedAsyncioTestCase):
    """工具行的两阶段：参数生成中 → 执行中，全程同一行。"""

    async def test_pending_row_shows_generating_state(self) -> None:
        """参数没到时显示工具名 + 「参数生成中」，且**不带空括号**。"""
        tc = ToolCall(id="c1", name="write_file", arguments=None)
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(tc, pending=True)
            await pilot.pause()

            text = _text_of(widget)
            self.assertIn("参数生成中", text)
            # 展示标签而不是内部工具名（与既有行为一致）
            self.assertIn("Write", text)
            # "Write()" 会被读成「无参调用」，必须避免
            self.assertNotIn("()", text)
            self.assertTrue(widget.pending)

    async def test_begin_running_reuses_same_row(self) -> None:
        """
        转执行态是**原地更新**，不新建第二行。

        没有这条，界面上会先出现一行「参数生成中」再冒出一行「执行中」，同一次
        写文件占两行，用户根本分不清是一次调用还是两次。
        """
        tc_pending = ToolCall(id="c1", name="write_file", arguments=None)
        tc_full = ToolCall(id="c1", name="write_file", arguments={"path": "a.txt"})
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(tc_pending, pending=True)
            await pilot.pause()
            before = len(view.query(ToolCallWidget))

            widget.begin_running(tc_full)
            await pilot.pause()

            self.assertEqual(len(view.query(ToolCallWidget)), before)
            text = _text_of(widget)
            self.assertIn("执行中", text)
            self.assertNotIn("参数生成中", text)
            # 参数摘要这时才补上
            self.assertIn("a.txt", text)
            self.assertFalse(widget.pending)

    async def test_begin_running_restarts_the_clock(self) -> None:
        """
        转执行态必须**重新起算耗时**。

        否则最终定色显示的 "(Ns)" 会把「模型生成参数」乃至「用户盯着确认面板发呆」
        的时间一并算进去——一次 2 毫秒的写盘可能显示成 "(600s)"，而那个数字的语义
        一直是工具执行耗时。

        ⚠ **判据形态在 tui-display 扩展 F13 之后变了，意图没变。** 那一版起
        「终态耗时不足一秒就整个不写括号」，所以这里的正向判据由「显示 (0s)」
        改成「一个耗时括号都不出现」——两者说的是同一件事：那 600 秒没有被
        算进执行耗时。反证（`assertNotIn("600")`）原样保留，它才是这条的分辨力所在。
        """
        tc = ToolCall(id="c1", name="write_file", arguments={"path": "a.txt"})
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(tc, pending=True)
            await pilot.pause()
            # 假装参数生成了很久。直接拨起点、手动重绘一次——真实场景下这一步由
            # 主线程的每秒定时器完成，测试里不能干等 600 秒。
            widget._start -= 600
            widget._render_running()

            self.assertIn("600s", _text_of(widget))

            widget.begin_running(tc)
            widget.finish(True, "新建 · 1 行 · 2 B")
            await pilot.pause()

            text = _text_of(widget)
            self.assertNotIn("600", text)
            self.assertIsNone(
                re.search(r"\(\d+s\)", text),
                f"耗时不足一秒的终态不该出现任何耗时括号，实际：{text!r}",
            )

    async def test_finish_while_pending_omits_empty_parens(self) -> None:
        """
        参数没生成完就被取消/拒绝时，定色标题不写括号。

        写成 "Write() 失败" 会被读成「调用没有参数」，而真相是「参数没来得及生成」。
        """
        tc = ToolCall(id="c1", name="write_file", arguments=None)
        app = _Harness()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            widget = view.add_tool_widget(tc, pending=True)
            await pilot.pause()

            widget.finish(False, "未执行（本轮已结束）")
            await pilot.pause()

            text = _text_of(widget)
            self.assertNotIn("()", text)
            self.assertIn("失败", text)


class SettleUnfinishedToolsTest(unittest.TestCase):
    """本轮结束时没等到结果的工具行必须收尾，不留永远转圈的橘色行。"""

    @staticmethod
    def _settle(widgets: dict, boom: bool = False) -> list:
        """
        直接调 `RhineApp._settle_unfinished_tools` 的函数体，self 用替身。

        不构造真的 `RhineApp`——它要 manager / provider / 权限引擎一整套装配，
        而本方法只用到 `call_from_thread` 一个成员。替身把跨线程调度拍平成直接调用。
        """
        from rhinecode.tui.app import RhineApp

        calls: list = []

        class _Stub:
            @staticmethod
            def call_from_thread(fn, *args, **kwargs):
                if boom:
                    # 复现应用退出竞态：Textual 此时会抛 RuntimeError
                    raise RuntimeError("app is not running")
                calls.append((fn, args))
                return fn(*args, **kwargs)

        RhineApp._settle_unfinished_tools(_Stub(), widgets)
        return calls

    def test_leftover_rows_are_marked_not_executed(self) -> None:
        """
        取消 / 流出错时，`TOOL_PENDING` 建过的行等不到 `TOOL_RESULT`。

        这几条路径都是真的：用户按 Esc、底层流出错、本轮因取消 break 掉剩下的调用
        ——后面的调用一个事件都不再产。
        """
        finished: list = []

        class _FakeWidget:
            def finish(self, ok, summary, diff=None):
                finished.append((ok, summary))

        widgets = {"c1": _FakeWidget(), "c2": _FakeWidget()}

        self._settle(widgets)

        self.assertEqual(len(finished), 2)
        for ok, summary in finished:
            self.assertFalse(ok)
            self.assertIn("未执行", summary)
        # 清空，避免同一行被下一轮重复收尾
        self.assertEqual(widgets, {})

    def test_exit_race_is_swallowed(self) -> None:
        """
        `call_from_thread` 抛异常必须被吞掉。

        本方法在 `_do_stream` 的 `finally` 里被调用，它只是视觉收尾；让它把一次
        正常结束变成异常退出是本末倒置。退出竞态下界面正在拆除，收不收尾都无意义。
        """
        class _FakeWidget:
            def finish(self, ok, summary, diff=None):  # pragma: no cover - 不会被调到
                raise AssertionError("不应该走到这里")

        widgets = {"c1": _FakeWidget()}

        self._settle(widgets, boom=True)  # 不抛即通过

        self.assertEqual(widgets, {})


class DoStreamWiringTest(unittest.IsolatedAsyncioTestCase):
    """
    `_do_stream` 三个分支的接线：**同一份 `tool_widgets` 表**，
    pending 建行 → start 复用 → result 摘除。

    走真实的 Worker + `call_from_thread` + Textual 挂载，不做源码断言——
    要钉的是「界面上最后剩下几行、写着什么」，而那是源码断言够不着的。
    `FakeManager` 借用 `test_command_tui` 的那一份（鸭子替身，不碰 Provider/网络）。
    """

    async def _drive(self, events: list):
        """把给定事件序列喂进 `_do_stream`，跑完后返回工具行列表与 app。"""
        from tests.test_command_tui import _make_app

        app, _ = _make_app()

        def gen():
            yield from events

        async with app.run_test() as pilot:
            app._start_stream_worker(gen())
            await app.workers.wait_for_complete()
            await pilot.pause()
            return list(app.query(ToolCallWidget)), _text_of

    async def test_pending_row_is_reused_not_duplicated(self) -> None:
        """
        pending → start → result 走完，界面上**只剩一行**，且是绿色的完成态。

        两件事一起钉住：
        ① start 分支复用了 pending 建的行（否则会是两行）；
        ② result 分支用 `pop` 把它摘出了待定表——若改回 `get`，收尾逻辑会在
           `finally` 里把这行**再收一次**，绿色的「完成」被覆写成「未执行」。
           这一步在界面上看不出是哪来的，只会觉得「明明写成功了却显示没执行」。
        """
        from rhinecode.tools.base import ToolResult

        tc_pending = ToolCall(id="c1", name="write_file", arguments=None)
        tc_full = ToolCall(
            id="c1", name="write_file", arguments={"path": "x.txt", "content": "hi"}
        )
        rows, text_of = await self._drive([
            AgentEvent(type=AgentEventType.TOOL_PENDING, tool_call=tc_pending),
            AgentEvent(type=AgentEventType.TOOL_START, tool_call=tc_full),
            AgentEvent(
                type=AgentEventType.TOOL_RESULT,
                tool_call=tc_full,
                tool_result=ToolResult(ok=True, output="已写入 x.txt", summary="新建 · 1 行 · 2 B"),
            ),
            AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED),
        ])

        self.assertEqual(len(rows), 1, "pending 与 start 必须共用一行")
        text = text_of(rows[0])
        # tui-activity-fold F7 起**成功态不再写「完成」二字**（绿色已经把状态
        # 说完了），原来的 `assertIn("完成")` 因此失效。
        #
        # 换成两条合起来等价的判据：**结果摘要出现了**（说明 `finish` 正常走完、
        # 拿到了 ToolResult），且**没有任何失败字样**。这一行真正要证明的是
        # 「它没有被 finally 里的收尾逻辑覆写成失败·未执行」，两条都指着它。
        self.assertIn("新建", text)
        self.assertNotIn("失败", text)
        self.assertNotIn("未执行", text)
        self.assertIn("x.txt", text)

    async def test_cancelled_pending_row_is_settled(self) -> None:
        """
        只来了 pending 就被取消：那一行必须被收尾，不能一直橘着转圈。

        真实路径：用户按 Esc、底层流出错、或本轮因取消 break 掉剩下的调用——
        这几种情况下后面的调用**一个事件都不再产**，没有这道收尾，界面上会留下
        一行计时器还在跳的「参数生成中」，看起来程序卡在某个工具上了。
        """
        rows, text_of = await self._drive([
            AgentEvent(
                type=AgentEventType.TOOL_PENDING,
                tool_call=ToolCall(id="c1", name="write_file", arguments=None),
            ),
            AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.USER_CANCELLED),
        ])

        self.assertEqual(len(rows), 1)
        text = text_of(rows[0])
        self.assertIn("未执行", text)
        self.assertNotIn("参数生成中", text)

    async def test_denied_row_still_shows_arguments(self) -> None:
        """
        权限拒绝 / 用户拒绝只产 `TOOL_RESULT`、不产 `TOOL_START`。

        这条路径下参数必须补上——否则标题只剩 "Write 失败"，用户看不出被拒的
        到底是哪一次写入。（改前是新建一行、天然带参数；改后复用 pending 行，
        不补就会丢信息，这是**引入两阶段之后才出现的**回退风险。）
        """
        from rhinecode.tools.base import ToolResult

        tc = ToolCall(id="c1", name="write_file", arguments={"path": "secret.env"})
        rows, text_of = await self._drive([
            AgentEvent(
                type=AgentEventType.TOOL_PENDING,
                tool_call=ToolCall(id="c1", name="write_file", arguments=None),
            ),
            AgentEvent(
                type=AgentEventType.TOOL_RESULT,
                tool_call=tc,
                tool_result=ToolResult(ok=False, output="用户拒绝", summary="用户拒绝"),
            ),
            AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED),
        ])

        self.assertEqual(len(rows), 1)
        text = text_of(rows[0])
        self.assertIn("secret.env", text)
        self.assertIn("失败", text)


if __name__ == "__main__":
    unittest.main()
