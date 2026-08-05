"""
缺省零行为的护栏（c12 T41，对应 checklist 第十节 / spec AC24、AC26、AC27）。

要钉住的性质：**两层 `hooks.yaml` 都不存在时，Hook 系统在结构上等于不存在**——
不读文件、不构造负载、不产任何 trace 事件、不改变任何既有行为。

这一条比它看起来重要：绝大多数用户不会写 hooks.yaml，所以「无配置」才是常态。
如果只有装了 Hook 的人不踩坑，那这个特性就是净负债。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from rhinecode.bootstrap import build_app
from rhinecode.config import Config
from rhinecode.hooks import NullHookManager
from rhinecode.hooks.models import HookEventType
from rhinecode.provider.base import BaseProvider, StreamChunk
from rhinecode.tools import path_guard
from rhinecode.trace.recorder import TraceRecorder


class QuietProvider(BaseProvider):
    def stream_chat(self, messages, thinking_effort="off", tools=None, system=None):
        yield StreamChunk(type="text", content="好的")
        yield StreamChunk(type="done")


def _cfg(**over) -> Config:
    base = dict(
        protocol="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="fake-key-for-test",
        debug_log=False,
        context_window=65536,
    )
    base.update(over)
    return Config(**base)


class NoConfigFixture(unittest.TestCase):
    """临时工作区 + 临时 user_dir，**两处都不放 hooks.yaml**。"""

    def setUp(self) -> None:
        self._work = tempfile.TemporaryDirectory()
        self._user = tempfile.TemporaryDirectory()
        self.work = Path(self._work.name).resolve()
        self.user_dir = Path(self._user.name).resolve()
        self._cwd = os.getcwd()
        os.chdir(self.work)
        path_guard.clear_read_roots()

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        path_guard.clear_read_roots()
        for d in (self._work, self._user):
            try:
                d.cleanup()
            except OSError:
                pass

    def build(self, **kw):
        result = build_app(
            _cfg(), user_dir=self.user_dir,
            provider_factory=lambda cfg: QuietProvider(), **kw
        )
        self.addCleanup(result.cleanup)
        return result


class ZeroBehaviourTest(NoConfigFixture):
    """AC24：无配置时的结构性零行为。"""

    def test_manager_is_the_null_implementation(self):
        result = self.build()
        self.assertIsInstance(result.manager._hooks, NullHookManager)
        self.assertFalse(result.manager._hooks.enabled)

    def test_every_event_has_no_listeners(self):
        result = self.build()
        hooks = result.manager._hooks
        for event in HookEventType:
            with self.subTest(event=event.value):
                self.assertFalse(hooks.has_listeners(event))

    def test_startup_notice_says_nothing_about_hooks(self):
        result = self.build()
        self.assertNotIn("Hook", result.manager.startup_notice or "")

    def test_hooks_report_is_the_empty_form(self):
        result = self.build()
        self.assertIn("没有加载任何 Hook 规则", result.manager.hooks_report())

    def test_no_hook_trace_events_across_a_full_run(self):
        """
        跑完一整轮交互，记录里**零条** hook 事件。

        这是比「既有测试仍通过」更精确的一条：既有断言只能证明它们仍成立，
        证明不了「没有多出任何东西」。
        """
        trace_path = self.work / "trace.jsonl"
        recorder = TraceRecorder(trace_path)
        result = self.build(recorder=recorder)

        list(result.manager.submit_user_message("你好"))
        result.cleanup()

        types = set()
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                types.add(json.loads(line).get("type"))
        self.assertNotIn("hook_dispatch", types)
        self.assertNotIn("hook_execute", types)
        self.assertIn("session_start", types, "其余事件照常产出（证明记录器确实在工作）")


class NullManagerContractTest(unittest.TestCase):
    """AC24 / AC26：空实现自身的行为。"""

    def test_payload_factory_is_never_called(self):
        """
        spec N7 的落点：`pre_tool_use` 的负载要展开整个 `tool_input`
        （一次 `write_file` 就是整份文件内容），零命中时白构造一遍不可接受。
        """
        calls = []
        m = NullHookManager()
        for event in HookEventType:
            m.dispatch(event, lambda: calls.append(1) or {})
        self.assertEqual(calls, [])

    def test_dispatch_returns_a_neutral_result(self):
        m = NullHookManager()
        result = m.dispatch(HookEventType.PRE_TOOL_USE)
        self.assertEqual(result.matched, 0)
        self.assertEqual(result.executed, 0)
        self.assertEqual(result.verdict.decision.value, "none")

    def test_consume_injections_is_empty(self):
        self.assertEqual(NullHookManager().consume_injections(), "")

    def test_project_notice_is_none(self):
        self.assertIsNone(NullHookManager().project_notice())

    def test_bind_context_is_a_no_op(self):
        NullHookManager().bind_context(session_id="x", cwd="/y")  # 不抛即通过


class NoIOTest(unittest.TestCase):
    """
    AC26：条件求值与结论合并可在**不触碰文件系统与网络**的前提下被完整覆盖。

    这里做的是一个结构性检查——`conditions` / `parser` / `report` / `models`
    四个模块不得导入任何 I/O 模块。它比「测试跑得过」更强：跑得过只说明
    这一次没碰，导入检查说明它**没有能力**去碰。
    """

    IO_MODULES = {"subprocess", "httpx", "socket", "requests", "urllib"}

    def test_pure_modules_import_no_io(self):
        import ast

        root = Path(__file__).resolve().parent.parent / "rhinecode" / "hooks"
        for name in ("models.py", "conditions.py", "parser.py", "report.py"):
            with self.subTest(module=name):
                tree = ast.parse((root / name).read_text(encoding="utf-8"))
                imported = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module.split(".")[0])
                offenders = imported & self.IO_MODULES
                self.assertEqual(offenders, set(), f"{name} 不该导入 {offenders}")

    def test_parser_does_not_import_pathlib_or_yaml(self):
        """
        解析与文件读取**必须分开**：`parser` 只吃已经 `yaml.safe_load` 出来的
        Python 对象，文件定位与读取在 `config.py`。合并会让解析逻辑再也无法
        脱离磁盘被单测覆盖。
        """
        import ast

        root = Path(__file__).resolve().parent.parent / "rhinecode" / "hooks"
        tree = ast.parse((root / "parser.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("yaml", imported)
        self.assertNotIn("pathlib", imported)


if __name__ == "__main__":
    unittest.main()
