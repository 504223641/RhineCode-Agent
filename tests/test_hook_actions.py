"""
四种动作执行器的单元测试（c12 T16，对应 checklist 第四节与第八节 / spec AC7–AC11、AC15）。

含三条**安全性反证**：
- 危险命令被①黑名单拦下且子进程从未启动
- `{"decision":"allow"}` 不产生放行语义
- HTTP 硬校验命中时请求从未发出

跨平台写法：需要真跑子进程的用例一律把逻辑写进临时 `.py` 文件再用
`sys.executable` 执行，**不在命令串里嵌 Python 代码**——那样在 cmd.exe 与
POSIX shell 上的引号转义规则不同，会让用例只在一个平台上过。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rhinecode.hooks import actions as hook_actions
from rhinecode.hooks.actions import (
    AGENT_PLACEHOLDER,
    DECISION_IGNORED,
    parse_decision,
    run_action,
)
from rhinecode.hooks.models import (
    AgentAction,
    CommandAction,
    HookDecision,
    HookEventType,
    HookPayload,
    HttpAction,
    PromptAction,
)


def _payload(event=HookEventType.PRE_TOOL_USE, **fields) -> HookPayload:
    base = {"event": event.value, "session_id": "sess-1", "cwd": "/tmp/x"}
    base.update(fields)
    return HookPayload(event, base)


class ScriptMixin:
    """把一段 Python 源码落成临时脚本，返回可直接交给 shell 的命令串。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._n = 0

    def script(self, source: str) -> str:
        self._n += 1
        path = Path(self._tmp.name) / f"hook_{self._n}.py"
        path.write_text(source, encoding="utf-8")
        return f'"{sys.executable}" "{path}"'


class CommandActionTest(ScriptMixin, unittest.TestCase):
    """`command` 动作（AC7、AC8、AC17）。"""

    def test_payload_arrives_on_stdin(self):
        """事件负载以完整 JSON 从标准输入抵达（决策 3B 的核心）。"""
        cmd = self.script(
            "import sys\n"
            "data = sys.stdin.read()\n"
            "sys.stderr.write(data)\n"
        )
        payload = _payload(tool="edit_file", file_path="a/b.py")
        outcome = run_action(CommandAction(cmd, 30), payload)
        self.assertTrue(outcome.ok, outcome.detail)
        # stderr 被原样带进 detail，从中还原出负载
        self.assertIn('"tool": "edit_file"', outcome.detail)
        self.assertIn('"file_path": "a/b.py"', outcome.detail)
        self.assertIn('"session_id": "sess-1"', outcome.detail)

    def test_payload_is_valid_json(self):
        cmd = self.script(
            "import json, sys\n"
            "d = json.load(sys.stdin)\n"
            "sys.stdout.write(json.dumps({'seen': sorted(d)}))\n"
        )
        outcome = run_action(CommandAction(cmd, 30), _payload(tool="read_file"))
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertIn("cwd", outcome.detail)
        self.assertIn("tool", outcome.detail)

    def test_command_string_is_executed_verbatim(self):
        """
        配置里写的命令串**逐字**执行，不做任何插值（AC7 / spec N3）。

        命令里写字面 `${file_path}`，它必须原样保留而不是被负载里的值替换掉——
        插值一旦存在，模型生成的工具参数就能拼进 shell 命令行（命令注入）。
        """
        # 脚本把自己收到的参数原样打回来。
        cmd = self.script(
            "import sys\n"
            "sys.stdout.write('ARGV=' + '|'.join(sys.argv[1:]))\n"
        )
        # 在命令串里写两种常见的插值语法。它们必须**不**变成负载里的值：
        # POSIX shell 下 ${file_path} 展开为空串、Windows cmd 下原样保留，
        # 两种结果都可以接受——不可接受的只有一种，就是变成 SHOULD_NOT_APPEAR。
        outcome = run_action(
            CommandAction(cmd + ' "${file_path}" "$file_path"', 30),
            _payload(file_path="SHOULD_NOT_APPEAR"),
        )
        self.assertTrue(outcome.ok, outcome.detail)
        self.assertIn("ARGV=", outcome.detail, "脚本应当真的跑起来了")
        self.assertNotIn(
            "SHOULD_NOT_APPEAR",
            outcome.detail,
            "负载里的值绝不能被插进命令串——那就是命令注入面",
        )

    def test_exit_2_is_deny(self):
        cmd = self.script(
            "import sys\n"
            "sys.stderr.write('请走 PR，不要直接 push')\n"
            "sys.exit(2)\n"
        )
        outcome = run_action(CommandAction(cmd, 30), _payload())
        self.assertTrue(outcome.ok, "跑完了就是 ok，拦不拦是 verdict 的事")
        self.assertEqual(outcome.verdict, HookDecision.DENY)
        self.assertIn("请走 PR", outcome.reason)

    def test_exit_1_is_failure_not_deny(self):
        cmd = self.script("import sys; sys.exit(1)")
        outcome = run_action(CommandAction(cmd, 30), _payload())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.verdict, HookDecision.NONE)

    def test_exit_0_silent_is_no_verdict(self):
        """绝大多数 Hook 什么都不输出——那必须是「不表态」而不是失败。"""
        cmd = self.script("pass")
        outcome = run_action(CommandAction(cmd, 30), _payload())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.verdict, HookDecision.NONE)

    def test_decision_json_on_stdout(self):
        cmd = self.script(
            "import sys\n"
            "sys.stdout.write('{\"decision\": \"ask\", \"reason\": \"需要你确认\"}')\n"
        )
        outcome = run_action(CommandAction(cmd, 30), _payload())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.verdict, HookDecision.ASK)
        self.assertIn("需要你确认", outcome.reason)

    def test_timeout(self):
        cmd = self.script("import time; time.sleep(30)")
        outcome = run_action(CommandAction(cmd, 1), _payload())
        self.assertFalse(outcome.ok)
        self.assertIn("超时", outcome.detail)

    def test_launch_failure_is_not_an_exception(self):
        """启动失败必须转成 ok=False，绝不向上抛——否则会污染 Agent 主流程。"""
        with mock.patch.object(
            hook_actions.subprocess, "run", side_effect=OSError("boom")
        ):
            outcome = run_action(CommandAction("whatever", 5), _payload())
        self.assertFalse(outcome.ok)
        self.assertIn("启动失败", outcome.detail)

    def test_duration_recorded(self):
        outcome = run_action(CommandAction(self.script("pass"), 30), _payload())
        self.assertGreater(outcome.duration_ms, 0)


class BlacklistTest(unittest.TestCase):
    """①危险命令黑名单对 Hook 命令同样生效（AC8）。"""

    def test_dangerous_command_blocked_and_subprocess_never_started(self):
        """
        ①层的既有性质是「不可被任何配置或权限模式放开」，而 hooks.yaml 就是配置。
        项目级那份可能来自别人的仓库——一条挂在 session_start 上的 `rm -rf ~`
        会在启动那一刻就跑起来。
        """
        with mock.patch.object(hook_actions.subprocess, "run") as fake_run:
            outcome = run_action(CommandAction("rm -rf ~", 30), _payload())
        fake_run.assert_not_called()
        self.assertFalse(outcome.ok)
        self.assertIn("黑名单", outcome.detail)

    def test_force_push_blocked(self):
        with mock.patch.object(hook_actions.subprocess, "run") as fake_run:
            outcome = run_action(
                CommandAction("git push --force origin main", 30), _payload()
            )
        fake_run.assert_not_called()
        self.assertFalse(outcome.ok)


class DecisionParsingTest(unittest.TestCase):
    """结论解析——含决策 1A 的最后一道闸（AC15）。"""

    def test_allow_is_ignored(self):
        """
        ⚠ 一个照着 Claude Code 文档写出来的 Hook 会输出 `{"decision":"allow"}`。
        它必须被当成「不表态」——若这里放行，Hook 就能翻过①黑名单与②沙箱。
        """
        decision, reason = parse_decision('{"decision": "allow"}')
        self.assertEqual(decision, HookDecision.NONE)
        self.assertEqual(reason, "")

    def test_unknown_decision_is_ignored(self):
        decision, _ = parse_decision('{"decision": "whatever"}')
        self.assertEqual(decision, HookDecision.NONE)

    def test_garbage_stdout_is_not_a_failure(self):
        for text in ("", "   ", "hello world", "[1,2,3]", "{broken"):
            with self.subTest(text=text):
                decision, _ = parse_decision(text)
                self.assertEqual(decision, HookDecision.NONE)

    def test_deny_and_ask(self):
        self.assertEqual(parse_decision('{"decision":"deny"}')[0], HookDecision.DENY)
        self.assertEqual(parse_decision('{"decision":"ask"}')[0], HookDecision.ASK)


class EventScopeTest(ScriptMixin, unittest.TestCase):
    """非 pre_tool_use 事件上的决策一律作废（spec F8 第 8 项的运行期落点）。"""

    def test_decision_ignored_on_post_tool_use(self):
        cmd = self.script("import sys; sys.exit(2)")
        outcome = run_action(
            CommandAction(cmd, 30), _payload(event=HookEventType.POST_TOOL_USE)
        )
        self.assertEqual(outcome.verdict, HookDecision.NONE, "拦截只在 pre_tool_use 生效")
        self.assertIn(DECISION_IGNORED, outcome.detail, "要说明为什么它没生效")

    def test_decision_kept_on_pre_tool_use(self):
        cmd = self.script("import sys; sys.exit(2)")
        outcome = run_action(
            CommandAction(cmd, 30), _payload(event=HookEventType.PRE_TOOL_USE)
        )
        self.assertEqual(outcome.verdict, HookDecision.DENY)
        self.assertNotIn(DECISION_IGNORED, outcome.detail)


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    """记录调用的假 HTTP 客户端，用于离线验证（形态同 web/manager.py 的注入点）。"""

    def __init__(self, status=200):
        self.status = status
        self.calls: list[tuple] = []

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _FakeResponse(self.status)


class HttpActionTest(unittest.TestCase):
    """`http` 动作（AC10）。"""

    FORBIDDEN = [
        "http://127.0.0.1/x",
        "http://localhost:8080/x",
        "file:///etc/passwd",
        "https://user:pw@example.com/x",
        "http://[::1]/x",
        "http://10.0.0.5/x",
    ]

    def test_forbidden_urls_never_send_a_request(self):
        for url in self.FORBIDDEN:
            with self.subTest(url=url):
                client = _FakeClient()
                outcome = run_action(HttpAction(url), _payload(), client_factory=client)
                self.assertFalse(outcome.ok, f"{url} 应被拒绝")
                self.assertEqual(client.calls, [], "请求绝不能发出")
                self.assertIn("网络边界拒绝", outcome.detail)

    def test_public_url_is_sent(self):
        client = _FakeClient()
        outcome = run_action(
            HttpAction("https://example.com/webhook", "POST"),
            _payload(tool="read_file"),
            client_factory=client,
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(client.calls), 1)
        method, url, kwargs = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://example.com/webhook")
        body = json.loads(kwargs["content"].decode("utf-8"))
        self.assertEqual(body["tool"], "read_file")

    def test_custom_body_and_headers(self):
        client = _FakeClient()
        run_action(
            HttpAction("https://example.com/x", "PUT", (("X-T", "1"),), "hello"),
            _payload(),
            client_factory=client,
        )
        method, _, kwargs = client.calls[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(kwargs["headers"]["X-T"], "1")
        self.assertEqual(kwargs["content"], b"hello")

    def test_non_2xx_is_failure_but_still_no_verdict(self):
        """响应不参与任何决策——服务挂了不该让本地工具动不了。"""
        client = _FakeClient(status=500)
        outcome = run_action(
            HttpAction("https://example.com/x"), _payload(), client_factory=client
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.verdict, HookDecision.NONE)
        self.assertIn("500", outcome.detail)

    def test_connection_error_is_not_an_exception(self):
        class _Boom:
            def __call__(self):
                raise RuntimeError("no network")

        outcome = run_action(
            HttpAction("https://example.com/x"), _payload(), client_factory=_Boom()
        )
        self.assertFalse(outcome.ok)
        self.assertIn("HTTP 请求失败", outcome.detail)


class PromptAndAgentTest(unittest.TestCase):
    """`prompt` 与 `agent` 动作（AC11）。"""

    def test_prompt_produces_injection(self):
        outcome = run_action(PromptAction("记得跑测试"), _payload())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.injected_text, "记得跑测试")
        self.assertEqual(outcome.verdict, HookDecision.NONE)

    def test_agent_placeholder_is_ok_true(self):
        """
        ⚠ `ok` 必须是 True。占位不是故障——若按失败处理，pre_tool_use 上的
        fail-closed 会让一条用户明知还没接通的规则拦死**每一次**工具调用。
        """
        outcome = run_action(AgentAction("去查一下"), _payload())
        self.assertTrue(outcome.ok, "占位动作不是失败")
        self.assertEqual(outcome.verdict, HookDecision.NONE)
        self.assertEqual(outcome.detail, AGENT_PLACEHOLDER)


if __name__ == "__main__":
    unittest.main()
