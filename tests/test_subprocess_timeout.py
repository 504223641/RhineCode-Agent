"""
超时必须是**真的等待上限**，而不只是返回文案里的一句话。

## 修的是什么

`run_command` 与 Hook 的 `command` 动作原本都是这么写的：

    subprocess.run(cmd, shell=True, capture_output=True, timeout=T)

`shell=True` 起来的是 `cmd.exe`（POSIX 下是 `/bin/sh`），真正干活的是它的
**子进程**。超时后 Python 只杀壳层，孙子进程照样活着，而且它**继承了
stdout/stderr 管道的写端**——管道要等最后一个写端关闭才会 EOF，于是
`communicate()` 仍要一直阻塞到孙子进程自己跑完。

净效果：`timeout=1` 的调用在命令跑 30 秒时**真的等 30 秒**，然后返回
一句「超时」。三组对照（子命令 `sleep 8`、`timeout=1`）：

| 形态 | 实际返回耗时 |
| --- | --- |
| `shell=True` + 捕获输出 | **8.05s** |
| `shell=True` + 不捕获输出 | 1.01s |
| 不用 shell + 捕获输出 | 1.01s |

## ⚠ 这里为什么可以用挂钟时间做判据

`docs/internals/testing.md` 有一条「不要用挂钟时间做判据」，那条针对的是
**拿耗时去判顺序**（「主对话先返回」之类）——机器一忙判据就翻。

本文件不一样：**被测的性质之一本身就是「多久之内返回」**，除了钟没有别的
东西可以问。为此判据留了 2 秒以上的余量，且钉的是「快慢差一个数量级」
而不是某个精确值。

## ⚠ 为什么必须有反证

改回 `subprocess.run` 之后，所有既有用例**照样全绿**——它们断言的是
「返回了超时文案」，而那一条改坏了也成立。
因此本文件里那条跑朴素写法的用例不是冗余，它是这层防护**唯一**的报警器。

## ⚠ 反证的判据不能只看钟（2026-08-30，CI 首次在 Linux 上跑出来的）

反证原先写的是「朴素写法必须**慢**」（耗时 > THRESHOLD）。那句话
**只在 Windows 上成立**，装了 CI 之后三个 Linux 格子全红在这一条上：

| | 朴素写法耗时 | 孙子进程泄漏了吗 |
| --- | --- | --- |
| Windows | **6.05s**（被拖住） | 是 |
| Linux | **1.00s**（没被拖住） | 是 |

同一个缺陷在两个平台上的**症状不一样**：Windows 上是「又慢又漏」，
Linux 上是「快但漏」。而原判据量的是耗时——**那只是 Windows 那一侧的症状**。

于是判据换成两条性质的组合，这才是这层防护真正想守住的东西
（不是「快」，是「**别把命令留在后台继续跑**」）：

- **正面**：`run_shell_captured` 必须**同时**满足「立刻返回」与「没有泄漏」
- **反证**：朴素写法**至少违反其中一条**

这个形式是平台无关的，且比原来**更严**——它顺带把「修好的那一侧不许泄漏」
也钉进了同一条对照里。⚠ 若哪天在某个平台上朴素写法两条都满足，这条会红，
而那时**该做的正是重新核对本文件的整套论证**，不是把它调松。

⚠ **`CHILD_SLEEP` 仍然不能缩小**：它要给孙子进程一个「本该还活着」的窗口，
标记文件的判定完全靠它。`THRESHOLD` 现在只是「立刻返回」那一半的判据，
不再独自承担反证。
"""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from rhinecode.hooks.actions import run_action
from rhinecode.hooks.models import CommandAction, HookEventType, HookPayload
from rhinecode.tools.path_guard import main_project_root
from rhinecode.tools.run_command import RunCommandTool, run_shell_captured

# 子命令要睡多久。取 6 秒是在两头之间折中：
# 太短则「有没有真的停下来」与噪声分不开，太长则本文件自己变成慢用例。
CHILD_SLEEP = 6
# 「立刻返回」那一半的判据阈值。修好之后是 ~1s + 杀进程树的开销，
# 放在 4 秒上留了 2 秒以上余量。
# ⚠ 它**不再是反证的判据**——朴素写法在 Linux 上同样 ~1s（见模块 docstring
# 那张表），反证改看「泄漏没泄漏」。
THRESHOLD = 4.0
# 「进程真的死了没有」那条用例单独用一个更短的睡眠。
# 它要**等过**这个时刻才能判，因此它直接决定那条用例的耗时；
# 而它不像上面那个常数那样需要与 THRESHOLD 拉开距离，短一点即可。
MARKER_SLEEP = 3


class ScriptMixin:
    """把一段 Python 源码落成临时脚本，返回可直接交给 shell 的命令串。

    ⚠ **不在命令串里嵌 Python 代码**——cmd.exe 与 POSIX shell 的引号转义规则
    不同，内联会让用例只在一个平台上过（与 `test_hook_actions.py` 同一写法）。
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._n = 0

    def script(self, source: str) -> str:
        self._n += 1
        path = Path(self._tmp.name) / f"probe_{self._n}.py"
        path.write_text(source, encoding="utf-8")
        return f'"{sys.executable}" "{path}"'

    def tmp_path(self, name: str) -> Path:
        return Path(self._tmp.name) / name


def _elapsed(fn) -> float:
    """跑一次 `fn` 并返回耗时（秒）；`fn` 抛的异常一律吞掉——本文件只关心时间。"""
    started = time.monotonic()
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass
    return time.monotonic() - started


class TimeoutIsARealBoundTest(ScriptMixin, unittest.TestCase):
    """超时后必须立刻返回，且**整棵进程树真的死了**。"""

    def _leaky_probe(self, marker: Path) -> str:
        """一条「先睡 CHILD_SLEEP 秒、再落一个标记文件」的命令串。

        标记文件是「这条命令有没有被留在后台继续跑」的物证：超时返回之后
        **等过它本该落盘的时刻**再看，文件在 = 泄漏了。

        :param marker: 该次探测专用的标记文件路径
        """
        return self.script(
            "import time\n"
            f"time.sleep({CHILD_SLEEP})\n"
            f"open(r'{marker}', 'w').write('still alive')\n"
        )

    def test_helper_returns_promptly_while_the_naive_form_does_not(self) -> None:
        """
        正反两跑：`run_shell_captured` 两条性质都满足，朴素写法至少破一条。

        两种写法跑**同一条命令**，因此判据不依赖机器有多快——它钉的是两者之差。
        哪天有人把 `run_shell_captured` 改回 `subprocess.run`，这条会红，
        而别的用例一条都不会（既有用例断言的是「返回了超时文案」，那句话改坏了
        照样成立）。

        ⚠ **两条性质缺一不可，而且不能只留耗时那条**——那正是 2026-08-30
        装 CI 之后三个 Linux 格子红掉的原因：同一个缺陷在 Windows 上表现为
        「又慢又漏」，在 Linux 上表现为「快但漏」，只看钟的话反证在 POSIX 上
        量不到任何东西。详见模块 docstring 末节那张表。

        执行步骤：

        1. 两种写法**各用各的标记文件**跑一次（共用一个会互相污染——R5 的第一版
           探针就是这么得出「Linux 上没杀干净」这个错误结论的）
        2. 等过两者本该落盘的时刻（取两次启动里更晚的那个算）
        3. 正面：修好的那一侧要**同时**满足「立刻返回」与「没泄漏」
        4. 反证：朴素写法**至少违反其中一条**

        副作用：起两个 shell 子进程，本条用例耗时约 8 秒（Windows 上约 14 秒，
        朴素写法在那里真的会被拖满 CHILD_SLEEP 秒）。
        """
        naive_marker = self.tmp_path("naive_survived.txt")
        fixed_marker = self.tmp_path("fixed_survived.txt")

        naive_started = time.monotonic()
        naive = _elapsed(lambda: subprocess.run(
            self._leaky_probe(naive_marker),
            shell=True, capture_output=True, timeout=1))

        fixed_started = time.monotonic()
        fixed = _elapsed(lambda: run_shell_captured(
            self._leaky_probe(fixed_marker),
            cwd=str(main_project_root()), timeout=1))

        # 等过**较晚**那一次本该落盘的时刻再判，否则「还没写」与「不会写」分不开。
        # 从各自**启动**那一刻算起，不是从调用返回算起——后者会把超时那 1 秒重复计一遍。
        deadline = max(naive_started, fixed_started) + CHILD_SLEEP + 1.5
        time.sleep(max(0.0, deadline - time.monotonic()))
        naive_leaked = naive_marker.exists()
        fixed_leaked = fixed_marker.exists()

        # ── 正面：修好的那一侧两条都要满足 ──────────────────────────────
        self.assertLess(
            fixed, THRESHOLD,
            f"超时后仍等了 {fixed:.1f}s：进程树没杀干净，`timeout` 又变回一句文案",
        )
        self.assertFalse(
            fixed_leaked,
            "超时返回后子进程仍活着并写了文件——进程树没杀干净",
        )

        # ── 反证：朴素写法至少违反一条 ────────────────────────────────
        # ⚠ 这里刻意写成析取而不是分别断言：两个平台上被违反的**不是同一条**
        #   （Windows 又慢又漏 / Linux 快但漏），钉死任何单独一条都会在另一个
        #   平台上红，而那时红的是判据不是产品。
        self.assertTrue(
            naive > THRESHOLD or naive_leaked,
            f"朴素写法这次既没被拖住（{naive:.1f}s）也没泄漏进程——"
            "两条性质上它都与修好的写法无异，"
            "若平台行为已变，本文件的整套论证需要重新核对",
        )

    def test_the_child_is_really_dead_afterwards(self) -> None:
        """
        「返回得快」不等于「停下来了」——分开验后一半。

        脚本先睡再落一个标记文件。我们 1 秒就超时返回，然后**等过它本该
        落盘的时刻**再看：文件不存在，说明它是真被杀了而不是还在后台跑。

        这一条才是安全意义所在：一个没被杀掉的命令仍在读写文件、占着端口，
        而调用方已经按「超时终止」继续往下走了。
        """
        marker = self.tmp_path("survived.txt")
        cmd = self.script(
            "import time\n"
            f"time.sleep({MARKER_SLEEP})\n"
            f"open(r'{marker}', 'w').write('still alive')\n"
        )

        started = time.monotonic()
        _elapsed(lambda: run_shell_captured(
            cmd, cwd=str(main_project_root()), timeout=1))

        # 等过它原定的落盘时刻再判，否则「还没写」与「不会写」分不开。
        # 从**启动那一刻**算起，不是从调用返回算起——后者会把超时那 1 秒重复计一遍。
        time.sleep(max(0.0, MARKER_SLEEP + 1.5 - (time.monotonic() - started)))
        self.assertFalse(
            marker.exists(),
            "超时返回后子进程仍活着并写了文件——进程树没杀干净",
        )


class CallSitesTest(ScriptMixin, unittest.TestCase):
    """两个调用点都要拿到这条性质（漏接一个不报错，只是那一处照旧挂着）。"""

    def test_run_command_tool(self) -> None:
        tool = RunCommandTool()
        cmd = self.script(f"import time; time.sleep({CHILD_SLEEP})")

        started = time.monotonic()
        result = tool.execute({"command": cmd, "timeout": 1}, cwd=main_project_root())
        elapsed = time.monotonic() - started

        self.assertFalse(result.ok)
        self.assertIn("超时", result.output)
        self.assertLess(elapsed, THRESHOLD, f"实测等了 {elapsed:.1f}s")

    def test_hook_command_action(self) -> None:
        """
        Hook 这一处比工具那处更要紧：它挂在 `pre_tool_use` 上时，等待发生在
        **每一次工具调用之前**——一条挂了的钩子能把整个 Agent 拖住任意久。
        """
        cmd = self.script(f"import time; time.sleep({CHILD_SLEEP})")
        payload = HookPayload(
            HookEventType.PRE_TOOL_USE,
            {"event": "pre_tool_use", "session_id": "sess-1", "cwd": "/tmp/x"},
        )

        started = time.monotonic()
        outcome = run_action(CommandAction(cmd, 1), payload)
        elapsed = time.monotonic() - started

        self.assertFalse(outcome.ok)
        self.assertIn("超时", outcome.detail)
        self.assertLess(elapsed, THRESHOLD, f"实测等了 {elapsed:.1f}s")


if __name__ == "__main__":
    unittest.main()
