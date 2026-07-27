"""
真实模式端到端用例（P1a T67，AC2 / AC41）。

**默认跳过。** 要跑必须同时满足两个条件：

    set RHINE_E2E_LIVE=1              # 显式开启（AC41）
    ~/.rhinecode/config.yaml 里有有效的 api_key

为什么默认跳过：这些用例会**真的调用模型 API**——花钱、要网络、慢，而且模型
每次说的话都不一样。让它们进全量测试等于让 CI 变得又贵又不稳定。

## 断言只针对「模式与存在性」，不针对措辞

真实模型说什么是不可预测的。所以这里断言的是「有没有发生模型请求」「有没有工具
被执行」「记录能不能完整解析」这类**结构性事实**，绝不断言它说了哪句话——
那种断言今天绿明天红，最后只会被人加上 `skip` 了事。
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path

from tests.e2e.assertions import TraceView
from tests.test_e2e_host import HostFixture


LIVE_ENABLED = os.environ.get("RHINE_E2E_LIVE") == "1"


def has_real_credentials() -> bool:
    """配置文件存在且 `api_key` 不是占位符。"""
    path = Path.home() / ".rhinecode" / "config.yaml"
    if not path.is_file():
        return False
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return False
    key = str(data.get("api_key") or "")
    return bool(key) and key not in ("YOUR_API_KEY", "fake", "fake-key-for-test")


@unittest.skipUnless(LIVE_ENABLED, "真实模式需显式开启：设 RHINE_E2E_LIVE=1")
class LiveModeTest(HostFixture):
    """AC2：真实模式下走完 AC1 的完整序列。"""

    def setUp(self) -> None:
        super().setUp()
        if not has_real_credentials():
            self.skipTest("~/.rhinecode/config.yaml 里没有有效的 api_key")

    def test_live_closed_loop(self):
        self.start_host("--mode", "live", "--idle-timeout", "600")

        self.assertEqual(self.status()["state"], "idle")
        # 让模型做一件必然要用工具的事，好验证「至少一条工具执行成功」
        self.send("在当前目录新建一个 hello.txt，内容写一行 hello，然后告诉我做完了")

        # 真实模型可能分多轮、可能中途要确认，逐轮推进
        for _ in range(12):
            res = self.wait(timeout=180)
            self.assertTrue(res["ok"], f"等待超时：{res}")
            if res["data"]["terminal"] == "idle":
                break
            # 停在面板上：批准它
            panel = self.status()["panel"]
            choice = {"confirm": "once", "approve": "yes", "clarify": "0"}.get(panel["kind"])
            self.assertIsNotNone(choice, f"没料到的面板类型：{panel['kind']}")
            self.answer(choice)
        else:
            self.fail("十二轮之后仍未回到空闲")

        view = self.view()
        # 真实模型的请求与响应都进了记录
        self.assertTrue(view.of_type("api_request"), "必须有真实模型请求")
        responses = view.of_type("api_response")
        self.assertTrue(responses, "必须有真实模型响应")
        # 至少一条工具执行成功（**不断言是哪个工具**——模型有自己的做法）
        executed = [e for e in view.of_type("tool_execute") if e.get("outcome") == "executed"]
        self.assertTrue(executed, "至少应有一条工具执行成功")

        self.quit_host(timeout=120)

    def test_notes_thread_converges_before_exit(self):
        """
        AC24 的 live 一半：真实模式保留自动笔记，退出前必须等它收敛。

        它是 daemon 线程，不 join 就会被进程退出截断——那样笔记会写到一半。
        """
        self.start_host("--mode", "live", "--idle-timeout", "600")
        self.send("用一句话介绍你自己")
        self.wait(timeout=180)
        self.quit_host(timeout=120)

        # 进程已退出，那个线程自然不在本进程里；这里验的是「宿主没有因为等它而卡死」
        # 以及「记录完整收尾」——两者合起来说明 join 生效且没有超时。
        view = self.view() if self.info else None
        if view is not None:
            self.assertEqual(view.records[-1]["type"], "session_end")


@unittest.skipUnless(LIVE_ENABLED, "真实模式需显式开启：设 RHINE_E2E_LIVE=1")
class LiveCredentialGuardTest(HostFixture):
    """F23：真实模式无凭据时明确报错退出，不静默降级成假模型。"""

    def test_missing_credentials_fails_loudly(self):
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(
                "protocol: deepseek\nmodel: deepseek-chat\n"
                "base_url: https://api.deepseek.com\napi_key: YOUR_API_KEY\n"
            )
            path = fh.name
        self.addCleanup(os.unlink, path)

        import subprocess
        import sys

        from tests.test_e2e_host import REPO_ROOT

        out = subprocess.run(
            [sys.executable, "-m", "tests.e2e.host", "--mode", "live", "--config", path],
            cwd=str(REPO_ROOT), capture_output=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        self.assertNotEqual(out.returncode, 0, "无凭据必须以非零退出码终止")
        self.assertIn("api_key", out.stderr)
        self.assertIn("占位符", out.stderr)


if __name__ == "__main__":
    unittest.main()
