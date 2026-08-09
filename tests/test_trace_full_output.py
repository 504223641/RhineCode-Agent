"""
护栏：**任何操作都要有能追溯的完整记录**。

## 这组用例在防什么

trace 层自己的截断阈值已经删干净了（见 `test_trace_models.FullTextTest`），
但那只解决了「记录器不丢内容」。还有一类更隐蔽的丢失：**内容在到达记录器之前
就已经被产品代码裁掉了**。

现在有两个这样的地方，它们的共同点是「裁剪有正当理由，但理由只对模型成立」：

| 位置 | 为什么裁 | 裁完谁受害 |
| --- | --- | --- |
| `run_command` | 命令输出可能几千行，全塞进上下文会爆 token | 排查「测试为什么失败」的人——被省掉的正是失败详情 |
| `hooks.actions` | 失败时 `detail` 会回灌给模型 | 排查「我的自动化跑出了什么」的人 |

两处的解法相同：**给模型的那份继续裁，另存一份完整原文只进 trace**
（`ToolResult.full_output` / `ActionOutcome.full_detail`）。

## 为什么这组用例必须存在

这是典型的「漏改不报错」：忘了填 `full_output` 的话，功能一切正常、
测试全绿、界面正常，只是那段输出**永久消失且无人察觉**——
因为被裁掉的地方连痕迹都没有（`_clip` 留下的「…（省略中间 k 行）…」
是给模型看的提示，它不告诉你被省掉的**内容**是什么）。
"""

import json
import tempfile
import unittest
from pathlib import Path

from rhinecode.hooks.actions import DETAIL_LIMIT, run_command_action
from rhinecode.hooks.models import CommandAction, HookEventType, HookPayload
from rhinecode.tools.run_command import RUN_HEAD, RUN_TAIL, RunCommandTool


class RunCommandFullOutputTest(unittest.TestCase):
    """`run_command`：模型拿裁剪版，trace 拿完整版。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.tool = RunCommandTool()

    def _run_many_lines(self, count: int):
        """跑一条产出 `count` 行、每行内容可辨识的命令。"""
        # 用 python 而不是 shell 循环：Windows 与 POSIX 上写法一致，
        # 不必为两个平台各写一套（本项目主力开发环境是 Windows）。
        #
        # ⚠ 必须写成**单行**列表推导。原先写的是带 `\n` 的多行脚本，
        # 经 `shell=True` 传下去时 `\n` 是字面两个字符而不是换行，
        # python 直接报语法错、命令产出 0 行——而断言「输出被裁剪了」
        # 于是失败在一个和真实原因毫不相干的地方。
        script = f"[print('L%04d' % i) for i in range({count})]"
        return self.tool.execute(
            {"command": f'python -c "{script}"'}, cwd=self.cwd
        )

    def test_short_output_leaves_full_output_unset(self) -> None:
        """
        没触发裁剪时 `full_output` 必须是 None。

        两份一模一样时多存一份纯属让记录文件白白翻倍——而记录文件已经
        因为去掉阈值变大了，能省的重复必须省。
        """
        res = self._run_many_lines(3)
        self.assertTrue(res.ok, res.output)
        self.assertIsNone(res.full_output)

    def test_long_output_is_clipped_for_model_but_kept_in_full(self) -> None:
        """裁剪版丢了中间行，完整版一行不少。"""
        total = RUN_HEAD + RUN_TAIL + 50
        res = self._run_many_lines(total)
        self.assertTrue(res.ok, res.output)

        # ① 给模型的那份**确实**被裁了（省 token 的行为一个字没变）
        self.assertIn("省略中间", res.output)
        self.assertNotIn("L0035", res.output)

        # ② 完整版存在，且被省掉的那些行真的在里面
        self.assertIsNotNone(res.full_output)
        assert res.full_output is not None  # 给类型检查看的
        self.assertNotIn("省略中间", res.full_output)
        for probe in ("L0000", "L0035", f"L{total - 1:04d}"):
            self.assertIn(probe, res.full_output, f"完整输出里应当有 {probe}")

        # ③ 行数对得上：完整版含全部 total 行
        body_lines = [ln for ln in res.full_output.splitlines() if ln.startswith("L")]
        self.assertEqual(len(body_lines), total)


class HookFullDetailTest(unittest.TestCase):
    """Hook 命令：`detail` 给模型（有上限），`full_detail` 给 trace（无上限）。"""

    def _payload(self) -> HookPayload:
        return HookPayload(event=HookEventType.SESSION_START, fields={"source": "test"})

    def test_short_output_leaves_full_detail_unset(self) -> None:
        action = CommandAction(command='python -c "print(\'ok\')"', timeout=30)
        outcome = run_command_action(action, self._payload())
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertIsNone(outcome.full_detail)

    def test_long_output_is_clipped_for_model_but_kept_in_full(self) -> None:
        # 造一段远超 DETAIL_LIMIT 的输出，尾部放一个哨兵。
        # 哨兵在末尾是刻意的——裁剪是「留头去尾」，哨兵能读到就证明尾部没丢。
        size = DETAIL_LIMIT * 3
        sentinel = "SENTINEL-TAIL-9f3a"
        script = f"print('z' * {size} + '{sentinel}')"
        action = CommandAction(command=f'python -c "{script}"', timeout=30)

        outcome = run_command_action(action, self._payload())
        self.assertTrue(outcome.ok, outcome.detail)

        # ① 给模型的那份被裁，哨兵读不到
        self.assertIn("已截断", outcome.detail)
        self.assertNotIn(sentinel, outcome.detail)

        # ② 完整版里哨兵在
        self.assertIsNotNone(outcome.full_detail)
        assert outcome.full_detail is not None
        self.assertIn(sentinel, outcome.full_detail)
        self.assertNotIn("已截断", outcome.full_detail)


class TraceRecordsFullOutputTest(unittest.TestCase):
    """
    端到端：完整原文真的落到了 `tool_execute` 事件里。

    上面两组只验数据结构填对了，这一组验**它真的被记下来了**——
    中间隔着 `_trace_tool` 一层，那里漏用 `full_output` 同样不报错。
    """

    def test_tool_execute_carries_full_output_and_model_view(self) -> None:
        from rhinecode.agent.loop import Agent
        from rhinecode.provider.base import ToolCall
        from rhinecode.tools.base import ToolResult
        from rhinecode.trace import TraceRecorder

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            rec = TraceRecorder(path)
            agent = Agent.__new__(Agent)  # 只用 _trace_tool，不装配整个 Agent
            agent._recorder = rec  # noqa: SLF001

            res = ToolResult(
                ok=True,
                output="裁剪版…（省略中间 160 行）…",
                summary="退出码 0",
                full_output="完整版第一行\n中间那 160 行\n最后一行",
            )
            agent._trace_tool(  # noqa: SLF001
                ToolCall(id="c1", name="run_command", arguments={"command": "pytest"}),
                res,
                outcome="executed",
                duration_ms=1.5,
                cwd=Path(d),
            )
            rec.close()

            record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        # 主字段 output 是**完整原文**——trace 的职责是完整
        self.assertIn("中间那 160 行", record["output"])
        # 模型看到的那份单独留痕，两个问题都能回答
        self.assertEqual(record["model_output"], "裁剪版…（省略中间 160 行）…")
        # c14：工作目录进记录（隔离故障的唯一线索）
        self.assertEqual(record["cwd"], str(Path(d)))

    def test_no_model_output_key_when_nothing_was_clipped(self) -> None:
        """没裁剪时不写 `model_output`——不制造一份完全重复的正文。"""
        from rhinecode.agent.loop import Agent
        from rhinecode.provider.base import ToolCall
        from rhinecode.tools.base import ToolResult
        from rhinecode.trace import TraceRecorder

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            rec = TraceRecorder(path)
            agent = Agent.__new__(Agent)
            agent._recorder = rec  # noqa: SLF001
            agent._trace_tool(  # noqa: SLF001
                ToolCall(id="c1", name="read_file", arguments={"file_path": "a.py"}),
                ToolResult(ok=True, output="文件内容", summary="读了 1 行"),
                outcome="executed",
            )
            rec.close()
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(record["output"], "文件内容")
        self.assertNotIn("model_output", record)


if __name__ == "__main__":
    unittest.main()
