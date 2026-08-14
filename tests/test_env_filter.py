"""
子进程环境变量过滤的护栏（auto-plan 扩展 T10）。

这一组用例的分辨力全在**两条反证**上：只验「密钥被剔掉了」的话，
一个把整个环境清空的实现也会全绿，而那会让几乎所有命令坏掉。
"""

import os
import unittest
from unittest import mock

from rhinecode.tools.run_command import _SENSITIVE_ENV_MARKERS, filtered_environ


class SensitiveNamesDroppedTest(unittest.TestCase):
    """命中黑名单的变量必须被剔除（spec F17）。"""

    def test_every_marker_is_actually_filtered(self):
        """
        遍历 `_SENSITIVE_ENV_MARKERS` 逐个构造变量名并断言被剔除。

        遍历常量表而不是硬编码几个名字：**新增片段自动被覆盖**，
        加了片段却没接进判定时这条会红。
        """
        fake = {f"MY_{marker}_VALUE": "secret" for marker in _SENSITIVE_ENV_MARKERS}
        fake["PATH"] = "/usr/bin"
        with mock.patch.dict(os.environ, fake, clear=True):
            env, dropped = filtered_environ()
            self.assertEqual(dropped, len(_SENSITIVE_ENV_MARKERS))
            for name in fake:
                if name == "PATH":
                    continue
                with self.subTest(name=name):
                    self.assertNotIn(name, env)

    def test_matching_is_case_insensitive(self):
        """
        变量名大小写不影响判定。

        Windows 上环境变量名的大小写并不稳定（`Path` / `PATH` 都见过），
        只按大写比较的实现会在 POSIX 上漏掉小写命名的密钥变量。
        """
        with mock.patch.dict(os.environ, {"my_api_key": "x", "PATH": "/usr/bin"}, clear=True):
            env, dropped = filtered_environ()
            self.assertEqual(dropped, 1)
            self.assertNotIn("my_api_key", env)

    def test_real_world_secret_names(self):
        """几个真实存在的密钥变量名，确保常见形态都盖得到。"""
        names = [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "GITHUB_TOKEN",
            "HF_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "DB_PASSWORD",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ]
        with mock.patch.dict(os.environ, dict.fromkeys(names, "x"), clear=True):
            env, dropped = filtered_environ()
            self.assertEqual(dropped, len(names))
            self.assertEqual(env, {})


class SurvivorsTest(unittest.TestCase):
    """
    **反证**：不该被剔的必须还在。

    没有这一组的话，一个 `return {}, len(os.environ)` 的实现也能让上一组全绿。
    """

    def test_ssh_auth_sock_survives(self):
        """
        ⚠ **`SSH_AUTH_SOCK` 必须活着** —— 本文件里分辨力最高的一条。

        它是 ssh-agent 的 **socket 路径**，名字里带 AUTH 但**本身不是密钥**，
        却是 ssh 方式 `git push` / `git clone` 能工作的唯一依靠。

        把它剔掉之后，命令会报 `Permission denied (publickey)`，而**根因完全
        看不出来**：用户会去查 `~/.ssh/`，那里一切正常；查 ssh-agent，也在跑。
        没人会想到是 rhine 把一个环境变量吞了。

        这条同时钉住实现里「用 `AUTHORIZATION` 而不是裸 `AUTH`」这个选择——
        谁把片段改回 `AUTH` 图省事，这里当场红。
        """
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/agent.sock"}, clear=True):
            env, dropped = filtered_environ()
            self.assertEqual(dropped, 0)
            self.assertIn("SSH_AUTH_SOCK", env)

    def test_path_like_names_survive(self):
        """
        名字里带 KEY 但其实是**路径**的变量必须活着。

        钉住「不用裸 `KEY` 作片段」这个选择，与上一条同型。
        """
        keep = {"SSH_KEY_PATH": "/home/u/.ssh/id_rsa", "KEYBOARD_LAYOUT": "us"}
        with mock.patch.dict(os.environ, keep, clear=True):
            env, dropped = filtered_environ()
            self.assertEqual(dropped, 0)
            self.assertEqual(env, keep)

    def test_ordinary_vars_survive(self):
        """
        常规变量原样传给子进程。

        这条是「别把环境删干净」的底线：`PATH` 没了的话连 `ls` 都跑不起来。
        """
        keep = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/u",
            "LANG": "en_US.UTF-8",
            "HTTP_PROXY": "http://127.0.0.1:8080",
        }
        with mock.patch.dict(os.environ, keep, clear=True):
            env, dropped = filtered_environ()
            self.assertEqual(dropped, 0)
            self.assertEqual(env, keep)


class NameOnlyTest(unittest.TestCase):
    """**反证**：判定只看变量名，绝不看取值（spec N6）。"""

    def test_values_are_never_inspected(self):
        """
        名字普通、取值长得像密钥的变量**不得**被剔除。

        按取值猜「这看起来像密钥」会误伤正常配置，而且结果不可预测：
        同一个变量今天传得过去、明天换个值就传不过去，没人查得出来。
        """
        looks_secret = {
            "MY_NOTE": "sk-ant-api03-xxxxxxxxxxxxxxxxxxxx",
            "EXAMPLE_LINE": "ghp_0123456789abcdefghijklmnopqrstuvwx",
        }
        with mock.patch.dict(os.environ, looks_secret, clear=True):
            env, dropped = filtered_environ()
            self.assertEqual(dropped, 0)
            self.assertEqual(env, looks_secret)


class SingleChokepointTest(unittest.TestCase):
    """
    结构护栏：过滤必须发生在**起子进程的唯一落点**上（spec F17b）。

    两个调用方（`run_command` 工具、Hook 的命令动作）都经过 `run_shell_captured`，
    在那里过滤一次即全覆盖。谁将来绕开它自己 `Popen`，下面第二条会红。
    """

    def _source(self, module) -> str:
        import inspect

        return inspect.getsource(module)

    def test_run_shell_captured_applies_the_filter(self):
        """`run_shell_captured` 内部要真的调过滤并把结果交给 `Popen`。"""
        from rhinecode.tools import run_command

        src = self._source(run_command)
        self.assertIn("filtered_environ()", src)
        # 光调用不够——不把 env 传给 Popen 的话，子进程照样继承完整环境。
        self.assertIn("env=env", src)

    def test_no_caller_starts_its_own_subprocess(self):
        """
        除 `run_shell_captured` 外，没有第二处直接起 shell 子进程。

        `hooks/actions.py` 复用它正是为了不出现第二份实现——各写一份的话，
        漏改的那一处不会报错，只是密钥照旧可见。
        """
        from rhinecode.hooks import actions

        self.assertNotIn("subprocess.Popen", self._source(actions))
        self.assertIn("run_shell_captured", self._source(actions))


if __name__ == "__main__":
    unittest.main()
