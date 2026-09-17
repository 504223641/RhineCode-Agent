"""第一层存盘单测（c8 T14 / AC4/AC5/AC6/AC7）：单结果/聚合存盘、user 不动、幂等。"""

import tempfile
import unittest
from pathlib import Path

from rhinecode.provider.base import Message, ToolCall
from rhinecode.context.estimate import CHARS_PER_TOKEN
from rhinecode.context.offload import (
    Offloader,
    PREVIEW_CHARS,
    SINGLE_RESULT_TOKENS,
    SOURCE_ARGS_MAX_CHARS,
    COMBINED_RESULT_TOKENS,
)
from rhinecode.tools.path_guard import is_offload_store_path
from rhinecode.tools.read_file import ReadFileTool


def _chars_for_tokens(tokens: int) -> int:
    """构造「估算约为 tokens」所需的字符数（略放大，确保跨过阈值）。"""
    return int(tokens * CHARS_PER_TOKEN) + 50


class OffloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)
        self.off = Offloader(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tool_msg(self, tid: str, tokens: int) -> Message:
        return Message(role="tool", content="A" * _chars_for_tokens(tokens), tool_call_id=tid)

    def test_single_result_offloaded_and_file_written(self) -> None:
        big = self._tool_msg("c1", SINGLE_RESULT_TOKENS + 500)
        original = big.content
        history = [Message(role="user", content="keep me"), big]
        notices = self.off.run(history)

        self.assertEqual([n.kind for n in notices], ["offload"])
        # 历史里该条变为占位（含路径提示），user 原文不变（AC6）
        self.assertTrue(history[1].content.startswith("[大型工具结果已存盘"))
        self.assertEqual(history[0].content, "keep me")
        # 磁盘落了完整原文（AC4）
        f = self.store / "c1.txt"
        self.assertTrue(f.exists())
        self.assertEqual(f.read_text(encoding="utf-8"), original)

    def test_small_result_untouched(self) -> None:
        small = self._tool_msg("c1", 10)
        history = [small]
        self.assertEqual(self.off.run(history), [])
        self.assertTrue(history[0].content.startswith("A"))  # 原文未改

    def test_idempotent(self) -> None:
        big = self._tool_msg("c1", SINGLE_RESULT_TOKENS + 500)
        history = [big]
        self.off.run(history)
        placeholder = history[0].content
        # 二次运行：不再处理、不产生重复文件、count 不增
        self.assertEqual(self.off.run(history), [])
        self.assertEqual(history[0].content, placeholder)
        self.assertEqual(self.off.count, 1)
        self.assertEqual(len(list(self.store.glob("*.txt"))), 1)

    def test_aggregate_offloads_largest_first(self) -> None:
        # 每条各自不超单阈值（<4000），但足够多条使合计超聚合阈值（>16000）；
        # 6 条 × ~3500 ≈ 21000 > 16000，且 3500 < SINGLE_RESULT_TOKENS。
        each = SINGLE_RESULT_TOKENS - 500
        self.assertLess(each, SINGLE_RESULT_TOKENS)
        history = [self._tool_msg(f"c{i}", each - i * 20) for i in range(6)]
        total_before = each * 6
        self.assertGreater(total_before, COMBINED_RESULT_TOKENS)

        notices = self.off.run(history)
        self.assertEqual([n.kind for n in notices], ["offload"])
        # c0 体积最大，应最先被存盘（挑大的先存）
        self.assertTrue(history[0].content.startswith("[大型工具结果已存盘"))
        # 至少存了 1 条，且未把所有条都存（存到合计达标即停）
        self.assertGreaterEqual(self.off.count, 1)
        remaining = [m for m in history if m.content.startswith("A")]
        self.assertTrue(remaining, "聚合存盘应在合计达标后停止，保留部分较小结果原文")

    def test_user_message_never_offloaded(self) -> None:
        huge_user = Message(role="user", content="U" * _chars_for_tokens(SINGLE_RESULT_TOKENS + 5000))
        history = [huge_user]
        self.assertEqual(self.off.run(history), [])
        self.assertTrue(history[0].content.startswith("U"))

    def test_write_failure_keeps_original(self) -> None:
        # store_dir 指向一个「已被占用为文件」的路径，mkdir 会失败 → 保留原文（N2）
        bad_file = Path(self._tmp.name) / "afile"
        bad_file.write_text("x", encoding="utf-8")
        off = Offloader(bad_file / "sub")  # 父级是文件，mkdir 必失败
        big = self._tool_msg("c1", SINGLE_RESULT_TOKENS + 500)
        original = big.content
        history = [big]
        notices = off.run(history)
        self.assertEqual(notices, [])
        self.assertEqual(history[0].content, original)  # 原文保留，不崩溃


class PlaceholderPointsAtSourceTest(unittest.TestCase):
    """
    占位符指向**原始来源**，绝不指向存盘文件（2026-09-17 修掉的死循环）。

    ## 这条护栏钉的是什么

    原文案写着「完整内容见文件：<存盘路径>（需要完整内容时，请用 read_file 读取
    该文件路径）」。模型照做，而 `read_file` 会给每行加行号前缀、再套一层文件头，
    于是**读回来的结果比存盘原文更大**（真实 trace 实测 22.2K → 25.2K）→ 必然
    再次超阈值 → 再次存盘 → 新路径 → 占位又叫它去读。内容每轮单调增大，
    **结构上不可能收敛**。那份 trace 里 14 次读存盘文件 100% 触发二次存盘，
    一条 25 轮的任务有 9 轮烧在三条这样的链上，模型一次都没拿到它要的内容。

    ⚠ **「有来源」与「没路径」两条缺一不可**：只断言有来源的话，把那行路径原样
    加回去照样全绿，而死循环也就原样回来了。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)
        self.off = Offloader(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _history(tool_name: str = "read_file", args: "dict | None" = None) -> list[Message]:
        """一条最小历史：user → assistant(发起调用) → tool(超大结果)。"""
        return [
            Message(role="user", content="看下那个文档"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name=tool_name,
                        arguments=args if args is not None else {"path": "docs/agent-issues.md"},
                    )
                ],
            ),
            Message(
                role="tool",
                content="Z" * _chars_for_tokens(SINGLE_RESULT_TOKENS + 500),
                tool_call_id="c1",
            ),
        ]

    def test_placeholder_never_leaks_the_store_path(self) -> None:
        """占位里不得出现存盘目录、文件名或任何 .txt 路径。"""
        history = self._history()
        self.off.run(history)
        placeholder = history[2].content

        self.assertNotIn(str(self.store), placeholder, "占位里出现了存盘目录")
        self.assertNotIn("c1.txt", placeholder, "占位里出现了存盘文件名")
        self.assertNotIn(".rhinecode", placeholder, "占位里出现了 .rhinecode 路径")
        # 顺带钉住旧文案的两个特征串，防「顺手把那句加回去」。
        self.assertNotIn("完整内容见文件", placeholder)
        self.assertNotIn("读取该文件路径", placeholder)

    def test_placeholder_names_the_original_call(self) -> None:
        """占位写清「重调哪一次调用」——这是它替代路径之后唯一的出路。"""
        history = self._history()
        self.off.run(history)
        placeholder = history[2].content

        self.assertIn("来源：read_file(", placeholder)
        self.assertIn("docs/agent-issues.md", placeholder, "来源里没带上关键参数")
        self.assertIn("start_line", placeholder, "没告诉模型怎么缩小范围")

    def test_source_args_are_clipped_not_dumped(self) -> None:
        """
        来源那一行会截断，**不会**把整份参数倒进上下文。

        反证形态：`write_file` 的 content 参数可以是整份文件。把它原样拼进占位，
        等于「为了省 token 而存盘」的同时又把 token 加了回去。
        """
        history = self._history("write_file", {"path": "a.py", "content": "Q" * 50_000})
        self.off.run(history)
        placeholder = history[2].content

        self.assertIn("来源：write_file(", placeholder)
        self.assertLess(
            len(placeholder),
            PREVIEW_CHARS + SOURCE_ARGS_MAX_CHARS + 500,
            "来源那一行没有被截断",
        )

    def test_unknown_source_falls_back_without_naming_a_tool(self) -> None:
        """
        历史里查不到发起方时，退回不点名的通用提示。

        ⚠ 这里**刻意不硬编一个工具名**（比如假定是 read_file）：说错了比不说更糟，
        模型会去调一次它根本没调过的工具，然后带着一个更离谱的结果回来。
        """
        # 只有 tool 消息、没有对应的 assistant.tool_calls（第二层摘要压掉之后的形态）
        history = [
            Message(
                role="tool",
                content="Z" * _chars_for_tokens(SINGLE_RESULT_TOKENS + 500),
                tool_call_id="orphan",
            )
        ]
        self.off.run(history)
        placeholder = history[0].content

        self.assertNotIn("来源：", placeholder)
        self.assertIn("重新执行产生这条结果的那次工具调用", placeholder)
        self.assertNotIn(".rhinecode", placeholder)

    def test_the_reread_chain_no_longer_diverges(self) -> None:
        """
        **端到端反证**：重演那条发散链，确认它现在断在第一步。

        链条（修复前）：占位给出路径 → 模型 read_file 那个路径 → 结果更大 →
        再次存盘 → 新路径 → 再读。这里用真的 `ReadFileTool` 走一遍，
        确认「模型就算拿到了路径，也读不出一份可以再次膨胀的结果」。

        ⚠ 用真工具而不是桩：这条链的关键一环是 `read_file` 的行号前缀让内容变大，
        桩掉它就把被测的那件事一起桩掉了。
        """
        workdir = Path(self._tmp.name) / "proj"
        store = workdir / ".rhinecode" / "context"
        store.mkdir(parents=True)
        spilled = store / "call_00_x.txt"
        spilled.write_text("Y" * 30_000, encoding="utf-8")

        result = ReadFileTool().execute(
            {"path": ".rhinecode/context/call_00_x.txt"}, cwd=str(workdir)
        )

        self.assertFalse(result.ok, "存盘副本仍然读得出来——链条没断")
        self.assertLess(len(result.output), 1000, "拒绝文案本身就够大到会被再次存盘")
        # 拒绝要给出路，不是一句「不允许」——只说不允许会让模型去找绕过的办法。
        self.assertIn("重新调用产生那条结果的工具", result.output)

    def test_sessions_and_traces_stay_readable(self) -> None:
        """
        ⚠ **只拦存盘目录，不拦 sessions / traces。**

        `path_guard._RUNTIME_ARTIFACT_RELATIVE`（搜索时跳过的那张表）里还有
        `sessions/` 与 `traces/`，但「让模型帮我看一份 trace / 翻一下上次的会话」
        是完全合理的请求。拿那张表来拦读取是**功能损失，不是修 bug**——
        这条反证钉住两张表不被顺手合一。
        """
        workdir = Path(self._tmp.name) / "proj2"
        for sub, name in ((("sessions",), "a.jsonl"), (("traces",), "t.jsonl")):
            d = workdir.joinpath(".rhinecode", *sub)
            d.mkdir(parents=True)
            (d / name).write_text("{}", encoding="utf-8")

        tool = ReadFileTool()
        for rel in (".rhinecode/sessions/a.jsonl", ".rhinecode/traces/t.jsonl"):
            with self.subTest(rel=rel):
                self.assertTrue(tool.execute({"path": rel}, cwd=str(workdir)).ok)

    def test_store_path_predicate_boundaries(self) -> None:
        """
        `is_offload_store_path` 的边界：**只有存盘目录及其内部**为真。

        ⚠ 它刻意不复用 `is_inside`（那个函数对两个入参各做一次 `Path.resolve()`，
        实测单次 1.0 ms、占 `read_file` 整体耗时 38%，而两个入参进来时都已解析过）。
        改成纯内存比较之后，**边界必须另行钉住**——这几条就是钉它的：
        少写一个 `store in target.parents` 会让深一层漏判，
        多写一层 `.parent` 会把整个 `.rhinecode/` 连坐拦掉。
        """
        root = Path(self._tmp.name).resolve()
        store = root / ".rhinecode" / "context"
        cases = [
            (store / "c.txt", True, "存盘文件"),
            (store, True, "存盘目录本身"),
            (store / "sub" / "d.txt", True, "深一层"),
            (root / ".rhinecode" / "sessions" / "a.jsonl", False, "会话存档"),
            (root / ".rhinecode", False, ".rhinecode 本身不该连坐"),
            (root / "a.md", False, "普通文件"),
        ]
        for target, expected, label in cases:
            with self.subTest(label=label):
                self.assertIs(is_offload_store_path(target, root), expected)

        # 判定按**调用者的工作目录**算（与权限管线第②层同口径）：
        # 换一个 root，同一个文件就不再是「它的」存盘副本。c14 隔离子 Agent 靠这条。
        self.assertFalse(is_offload_store_path(store / "c.txt", root / "other"))


if __name__ == "__main__":
    unittest.main()
