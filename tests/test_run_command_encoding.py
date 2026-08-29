"""
`run_command` 的输出解码回归测试。

背景（一个静默且致命的真实故障）：原实现用 `subprocess.run(text=True)`，
Python 会按 `locale.getpreferredencoding()` 解码子进程输出——中文 Windows 上
是 cp936(GBK)。而 git / python / node 等现代工具链输出 UTF-8。两者一撞，
`UnicodeDecodeError` 抛在 subprocess 的**读取线程**里被吞掉，`proc.stdout`
最终是空串，而**退出码仍然是 0**。

对模型的表现是：「命令执行成功，但没有任何输出」。实测后果——`git log`
只要提交信息含中文就整段变空，模型据此断定「这个仓库没有提交历史」，
于是既学不到提交风格、也看不到 `git diff`，整条 SOP 全跑偏，且全程无报错。

因此这里断言的是行为而非实现：**非 ASCII 输出必须能被拿到**。
"""

import sys
import unittest

from rhinecode.tools.run_command import RunCommandTool
from rhinecode.tools.path_guard import main_project_root


def _cwd():
    """c14：用例会 chdir 到临时工作区，因此每次现取进程当前目录。"""
    return main_project_root()


class RunCommandEncodingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = RunCommandTool()

    def _run(self, command: str):
        return self.tool.execute({"command": command}, cwd=_cwd())

    def test_utf8_stdout_is_not_swallowed(self) -> None:
        """子命令输出 UTF-8 中文 → 内容出现在结果里，而不是变成空输出。"""
        # 用当前解释器输出一段中文，并显式按 UTF-8 写字节，
        # 模拟 git/node 这类「不管系统 locale、一律 UTF-8」的工具。
        code = (
            "import sys;"
            "sys.stdout.buffer.write('初始化购物车示例项目骨架\\n'.encode('utf-8'))"
        )
        result = self._run(f'{sys.executable} -c "{code}"')

        self.assertTrue(result.ok)
        self.assertIn("初始化购物车示例项目骨架", result.output)
        self.assertNotIn("输出 0 行", result.summary)

    def test_utf8_stderr_is_not_swallowed(self) -> None:
        """stderr 路同样按 UTF-8 解码（失败原因常带中文）。"""
        code = (
            "import sys;"
            "sys.stderr.buffer.write('入参非法：折扣率越界\\n'.encode('utf-8'));"
            "sys.exit(3)"
        )
        result = self._run(f'{sys.executable} -c "{code}"')

        self.assertFalse(result.ok)
        self.assertIn("入参非法：折扣率越界", result.output)

    def test_non_utf8_bytes_degrade_instead_of_vanishing(self) -> None:
        """
        输出不是合法 UTF-8（旧式 ANSI 命令）→ 退回本地编码并容错替换。

        判据是「拿得到东西且不抛异常」：乱码远好过静默丢失——用户至少
        看得出编码不对，而空输出会让模型得出完全错误的结论。
        """
        code = "import sys;sys.stdout.buffer.write(b'ok \\xff\\xfe\\n')"
        result = self._run(f'{sys.executable} -c "{code}"')

        self.assertTrue(result.ok)
        self.assertIn("ok", result.output)

    def test_exit_code_and_ascii_output_unchanged(self) -> None:
        """纯 ASCII 与退出码语义不受改动影响（零回归）。"""
        result = self._run(f'{sys.executable} -c "print(1+1)"')
        self.assertTrue(result.ok)
        self.assertIn("2", result.output)
        self.assertIn("退出码: 0", result.output)

    def test_timeout_path_still_works(self) -> None:
        """超时分支不碰 stdout，改动后仍返回超时提示而非崩溃。"""
        result = self.tool.execute(
            {"command": f'{sys.executable} -c "import time;time.sleep(5)"', "timeout": 1},
            cwd=_cwd(),
        )
        self.assertFalse(result.ok)
        self.assertIn("超时", result.output)


if __name__ == "__main__":
    unittest.main()
