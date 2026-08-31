"""
MCP stdio 子进程的环境变量过滤护栏——审查报告 B4 的附带修复。

## 钉的是什么

`StdioTransport.start` 此前是 `merged_env = {**os.environ, **self._env}`，
把**完整的进程环境**（含 `DEEPSEEK_API_KEY`）交给一个外部程序。

项目自己把 MCP Server 定性为「外部程序、不可信」，而 `mcp.yaml` 可能来自
`git clone` 来的项目级配置。同一条理由在 `run_command` 那边早就落实了
（auto-plan 扩展 F17：「命令全放行之后，一句打印环境的命令就能拿到 API Key」），
MCP 这条路径当时漏了——**而它比命令更隐蔽**：命令串会出现在确认面板与行为记录里，
一个 MCP Server 自己去读环境变量不留任何痕迹。

## 违反会发生什么

把那行改回 `{**os.environ, ...}`，**任何地方都不会报错**：Server 照常连上、
工具照常注册、其余全部用例照常绿。唯一的差别是密钥出现在一个第三方进程里，
而那件事从界面上、从行为记录里、从任何一条日志里都看不出来。

## 为什么这几条不能简化

- **必须真起子进程去读它拿到的环境**。只断言「`start` 里调了 `filtered_environ`」
  的话，一个「调了但没用返回值」的实现照样绿——那正是最容易写出来的错法。
- **`PATH` 必须还在**（反证一）：一个把环境清空的实现会让上面那条断言通过，
  而绝大多数 MCP Server（`npx` / `uvx` 起的那些）当场就起不来。
- **配置里的 `env` 必须仍然覆盖得上**（反证二）：过滤只针对「本进程环境的无差别
  继承」，用户在 `mcp.yaml` 里显式写下的 `${GITHUB_TOKEN}` 是**他自己的授权决定**。
  把它一起过滤掉会让「配了却不生效」成为常态，而那是产品缺陷不是安全改进。
  ⚠ 这条尤其容易在「顺手加严一点」时被改坏，且改坏之后**只在真装了这类
  Server 的用户那里复现**。
- **黑名单复用 `run_command` 那一份**（反证三）：抄第二份的话两处会各自漂移，
  而漏改不报错——新增一个敏感前缀只在命令那侧生效，MCP 这侧照样漏出去。
"""

import json
import os
import sys
import unittest
from unittest import mock

from rhinecode.mcp.client import MCPClient
from rhinecode.mcp.transport import StdioTransport
from rhinecode.tools.run_command import _SENSITIVE_ENV_MARKERS

# 一个只做一件事的模拟 Server：把**它自己看到的环境变量**原样回给调用方。
#
# 用真子进程而不是打桩 `Popen`，是因为本护栏要回答的问题就是「那个外部程序
# 实际拿到了什么」——打桩只能验到我们传了什么，验不到 Python 有没有按我们
# 想的那样把 env 交下去。
_ENV_DUMP_SERVER = r"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "envdump", "version": "0.0.1"},
            },
        })
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": []}})
    elif method == "tools/call":
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(dict(os.environ), ensure_ascii=False)}
                ]
            },
        })
"""

# 装进「本进程环境」的样本：一个真实的密钥名 + 一个 PATH 对照。
# ⚠ `mock.patch.dict` 不带 `clear=True`——清空整个环境会让 Windows 上的
# `subprocess.Popen` 因为缺 `SystemRoot` 直接起不来子进程。
_FAKE_ENV = {
    "DEEPSEEK_API_KEY": "sk-should-never-reach-the-child",
    "MY_SERVICE_TOKEN": "also-secret",
    "RHINE_HARMLESS_MARKER": "keep-me",
}


def _child_environ(config_env: dict = None) -> dict:
    """
    真起一个 stdio 子进程，返回**它自己看到的**环境变量字典。

    :param config_env: 传给 `StdioTransport` 的配置级 env（模拟 `mcp.yaml` 里的
                       `env:` 段），默认不给
    :returns: 子进程的 `os.environ` 快照

    副作用：起一个 Python 子进程并在返回前关掉它。
    """
    transport = StdioTransport(sys.executable, ["-c", _ENV_DUMP_SERVER], config_env or {})
    client = MCPClient("envdump", transport, call_timeout=15.0)
    try:
        client.initialize()
        result = client.call_tool("dump", {})
        return json.loads(result["content"][0]["text"])
    finally:
        client.close()


class ChildProcessEnvironmentTest(unittest.TestCase):
    """MCP stdio 子进程拿到的环境里不能有密钥。"""

    def test_secrets_do_not_reach_the_child(self):
        """
        ⚠ **本文件的核心断言。**

        密钥名按 `run_command` 那份黑名单判定（`API_KEY` / `TOKEN` / …），
        断言它们**一个都没进到子进程里**。
        """
        with mock.patch.dict(os.environ, _FAKE_ENV):
            env = _child_environ()
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("MY_SERVICE_TOKEN", env)

    def test_every_marker_is_covered(self):
        """
        遍历 `_SENSITIVE_ENV_MARKERS` 逐个构造变量名再验一遍。

        遍历常量表而不是硬编码几个名字：**将来往那份黑名单里加片段，
        这条自动覆盖到**；而如果有人在 MCP 这侧另抄了一份黑名单、
        两份漂了，这条会红。
        """
        fake = {f"RHINE_{marker}_PROBE": "secret" for marker in _SENSITIVE_ENV_MARKERS}
        with mock.patch.dict(os.environ, fake):
            env = _child_environ()
        for name in fake:
            with self.subTest(name=name):
                self.assertNotIn(name, env)

    def test_ordinary_variables_still_reach_the_child(self):
        """
        **反证一：不是把环境清空。**

        少了它，一个 `merged_env = dict(self._env)` 的实现会让上面两条全绿——
        而 `npx` / `uvx` 起的 MCP Server 会因为没有 `PATH` 当场起不来，
        表现是「配好的 Server 突然连不上」，根因完全看不出来。
        """
        with mock.patch.dict(os.environ, _FAKE_ENV):
            env = _child_environ()
        self.assertEqual(env.get("RHINE_HARMLESS_MARKER"), "keep-me")
        self.assertIn("PATH", {key.upper() for key in env})

    def test_config_env_still_wins(self):
        """
        **反证二：`mcp.yaml` 里显式写下的 `env` 仍然送得到，即便名字像密钥。**

        过滤只针对「本进程环境的无差别继承」——那部分不是任何人的决定；
        而用户在配置里写下的 `${GITHUB_TOKEN}` 是他自己的授权决定。
        把它一起过滤掉会让「配了却不生效」成为常态，那是产品缺陷不是安全改进，
        且只在真装了这类 Server 的用户那里复现。
        """
        with mock.patch.dict(os.environ, _FAKE_ENV):
            env = _child_environ({"GITHUB_TOKEN": "from-mcp-yaml"})
        self.assertEqual(env.get("GITHUB_TOKEN"), "from-mcp-yaml")
        # 继承那一侧仍然被过滤：两件事互不影响。
        self.assertNotIn("DEEPSEEK_API_KEY", env)


class SharedBlacklistTest(unittest.TestCase):
    """黑名单只有一份——**结构护栏**，防「另抄一份然后两处漂移」。"""

    def test_transport_reuses_the_command_tool_filter(self):
        """
        `mcp/transport.py` 必须 import `tools/run_command.py` 的 `filtered_environ`。

        ⚠ 这条断言的是**来源**而不是行为，与上面那组真子进程用例是两个角度：
        行为用例回答「这次过滤对不对」，这条回答「下次有人加片段时会不会漏」。
        两条都在，才既钉住当下也钉住将来。
        """
        from rhinecode.mcp import transport as mcp_transport
        from rhinecode.tools import run_command

        self.assertIs(mcp_transport.filtered_environ, run_command.filtered_environ)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
