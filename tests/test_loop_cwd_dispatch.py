"""
c14 T21：Agent Loop 的四个 cwd 分发点（spec F1 / F2 / F3 / F25 / N3）。

**本文件存在的全部理由是一条极易漏的不对称：**

既有的 `plan_stage` 只在**串行**路径传递（它只对非只读工具有意义），
照抄那个写法就会漏掉**并发**路径——而 `read_file` / `glob_files` /
`grep_content` 全是只读工具、全走并发路径。

漏掉之后的表现：隔离子 Agent 的**写**是对的（串行路径），**读**却落到主项目根
（并发路径）。它既不报错也不越权，只是读到了另一份文件，界面上完全看不出来。
`test_readonly_tool_on_concurrent_path_receives_cwd` 就是那条反证。
"""

import threading
import unittest
from pathlib import Path

from rhinecode.agent.events import AgentEventType
from rhinecode.agent.loop import Agent, RunOptions
from rhinecode.permission import PermissionEngine, PermissionMode
from rhinecode.permission.rules import RuleSet
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall
from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.path_guard import main_project_root
from rhinecode.tools.registry import ToolRegistry


class _RecordingTool(Tool):
    """记录自己收到了什么 cwd 的假工具。"""

    parameters = {"type": "object", "properties": {}}

    def __init__(self, name: str, read_only: bool, workspace_aware: bool):
        self.name = name
        self.description = "测试用"
        self.read_only = read_only
        self.workspace_aware = workspace_aware
        self.seen: list = []
        self.saw_kwargs: list[dict] = []

    def execute(self, args: dict, **kwargs) -> ToolResult:
        # 用 **kwargs 接住一切，这样「有没有传 cwd」本身也是可断言的事实，
        # 而不是靠签名报错间接推断。
        self.saw_kwargs.append(dict(kwargs))
        self.seen.append(kwargs.get("cwd"))
        return ToolResult(ok=True, output="done")


class _ScriptedProvider(BaseProvider):
    """
    第一轮发一批工具调用，第二轮收工。

    只实现循环真正用到的那部分接口——本文件验的是分发，不是 Provider。
    """

    def __init__(self, tool_names: list[str]):
        self._tool_names = tool_names
        self._round = 0

    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        self._round += 1
        if self._round == 1:
            for i, name in enumerate(self._tool_names):
                yield StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(id=f"c{i}", name=name, arguments={}),
                )
            yield StreamChunk(type="done")
            return
        yield StreamChunk(type="text", content="完成")
        yield StreamChunk(type="done")

    def chat(self, messages, **kwargs):
        return Message(role="assistant", content="完成")


def _drain(agent, engine, options):
    """把一次 run 跑完，返回产出的事件类型序列。"""
    events = []
    for ev in agent.run(
        history=[Message(role="user", content="干活")],
        thinking_effort="off",
        plan_mode=False,
        stable="",
        dynamic=lambda: "",
        model="test-model",
        debug_log_path=None,
        engine=engine,
        ask=lambda *a, **k: True,
        clarify=None,
        approve_plan=None,
        cancel_event=threading.Event(),
        options=options,
    ):
        events.append(ev.type)
        if ev.type is AgentEventType.FINISHED:
            break
    return events


class DispatchTestBase(unittest.TestCase):
    def _run_with(self, tools: list[_RecordingTool], options: RunOptions):
        registry = ToolRegistry()
        for t in tools:
            registry.register(t)
        engine = PermissionEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        agent = Agent(_ScriptedProvider([t.name for t in tools]), registry)
        _drain(agent, engine, options)


class ConcurrentPathTest(DispatchTestBase):
    """并发桶（只读工具走这条）。"""

    def test_readonly_tool_on_concurrent_path_receives_cwd(self):
        """
        ⚠ **本文件最重要的一条反证。**

        只读工具走并发路径。若实现照抄 `plan_stage` 只在串行路径传 cwd，
        这里拿到的就是 `None`，隔离子 Agent 的一切读取都会落到主项目根。
        """
        target = Path.cwd().resolve()
        tool = _RecordingTool("ro", read_only=True, workspace_aware=True)

        self._run_with([tool], RunOptions(cwd=target))

        self.assertEqual(tool.seen, [target], "并发路径必须收到 cwd")

    def test_multiple_readonly_tools_all_receive_cwd(self):
        """并发桶里有多个工具时，每一个都要拿到——不是只有第一个。"""
        target = Path.cwd().resolve()
        tools = [
            _RecordingTool(f"ro{i}", read_only=True, workspace_aware=True)
            for i in range(3)
        ]

        self._run_with(tools, RunOptions(cwd=target))

        for t in tools:
            self.assertEqual(t.seen, [target], f"{t.name} 没收到 cwd")


class SerialPathTest(DispatchTestBase):
    """串行桶（有副作用的工具走这条）。"""

    def test_writable_tool_on_serial_path_receives_cwd(self):
        target = Path.cwd().resolve()
        tool = _RecordingTool("rw", read_only=False, workspace_aware=True)

        self._run_with([tool], RunOptions(cwd=target))

        self.assertEqual(tool.seen, [target], "串行路径必须收到 cwd")


class NotWorkspaceAwareTest(DispatchTestBase):
    """
    未声明 `workspace_aware` 的工具**签名不受影响**。

    这条保证了 c14 不需要一次性改掉全部工具：不碰路径的工具（如 web_fetch、
    委派工具）原样不动。
    """

    def test_readonly_without_flag_gets_no_cwd(self):
        tool = _RecordingTool("plain_ro", read_only=True, workspace_aware=False)
        self._run_with([tool], RunOptions(cwd=Path.cwd()))
        self.assertEqual(tool.saw_kwargs, [{}], "未声明的工具不该收到 cwd")

    def test_writable_without_flag_gets_no_cwd(self):
        tool = _RecordingTool("plain_rw", read_only=False, workspace_aware=False)
        self._run_with([tool], RunOptions(cwd=Path.cwd()))
        self.assertEqual(tool.saw_kwargs, [{}], "未声明的工具不该收到 cwd")


class DefaultCwdTest(DispatchTestBase):
    """
    N3：不传 `cwd` 时取主项目根——主对话与非隔离子 Agent 的行为
    与 c14 之前**逐字一致**。
    """

    def test_default_is_main_project_root(self):
        tool = _RecordingTool("ro", read_only=True, workspace_aware=True)
        self._run_with([tool], RunOptions())
        self.assertEqual(tool.seen, [main_project_root()])

    def test_default_serial_is_main_project_root(self):
        tool = _RecordingTool("rw", read_only=False, workspace_aware=True)
        self._run_with([tool], RunOptions())
        self.assertEqual(tool.seen, [main_project_root()])


class PermissionRequestCwdTest(unittest.TestCase):
    """
    第三个分发点：权限判定拿到的 cwd 必须与工具拿到的是同一个。

    两者不一致的后果最阴险——引擎按 A 批准，工具按 B 写入，
    **批准的和写的不是同一个文件**，两边都不报错。
    """

    def test_engine_receives_same_cwd(self):
        target = Path.cwd().resolve()
        captured: list = []

        class _SpyEngine(PermissionEngine):
            def decide(self, request):
                captured.append(request.cwd)
                return super().decide(request)

        tool = _RecordingTool("rw", read_only=False, workspace_aware=True)
        registry = ToolRegistry()
        registry.register(tool)
        engine = _SpyEngine(RuleSet([]), mode=PermissionMode.PERMISSIVE)
        agent = Agent(_ScriptedProvider(["rw"]), registry)

        _drain(agent, engine, RunOptions(cwd=target))

        self.assertEqual(captured, [target], "权限判定必须收到同一个 cwd")
        self.assertEqual(tool.seen, [target])
        self.assertEqual(captured[0], tool.seen[0], "两处必须是同一个目录")


if __name__ == "__main__":
    unittest.main()
