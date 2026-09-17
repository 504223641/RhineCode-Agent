"""
「模型知不知道命令交给哪个 shell」的护栏（2026-09-17）。

## 这组护栏钉的是什么

两份真实 trace 实录：模型不知道 `run_command` 用的是哪个解释器，于是把三种都
猜了一遍——POSIX（`| tail -40`、`VAR=x cmd`、`cmd &`）、PowerShell
（`| Select-Object`）、cmd（`set X=0 && cmd`）。一次任务里 **4 轮迭代、
197K 输入 token** 白烧在语法试错上，占那条用户消息的 23%。

修法是两处各说一次：环境信息里报**解释器名**（动态、按轮次计费，故只给名字），
工具描述里给**语法细节**（进可缓存前缀、只付一次费）。

**最要紧的一条是「说的和真跑的必须是同一个」**——说错了比什么都不说更糟：
模型会自信地写一种根本不生效的语法，而且不会去怀疑这条环境信息。
"""

import os
import subprocess
import unittest

from rhinecode.agent.prompt.environment import EnvironmentInfo, _detect_shell, collect_environment
from rhinecode.config import Config
from rhinecode.tools.run_command import RunCommandTool


def _config() -> Config:
    return Config(
        api_key="k", base_url="https://example.invalid", model="m", protocol="deepseek"
    )


class DetectedShellMatchesRealityTest(unittest.TestCase):
    """
    ⚠ **本类是这组护栏的核心：报出去的解释器必须就是真正执行命令的那个。**

    其余几条都只是「有没有写这句话」，只有这一条验的是「这句话是不是真的」。
    """

    def test_reported_shell_is_the_one_that_actually_runs_the_command(self) -> None:
        """
        真起一个 `shell=True` 子进程，让它自报家门，与 `_detect_shell()` 比对。

        ⚠ **刻意真起进程而不是断言 `os.name == "nt" → "cmd.exe"`**：后者是把
        `_detect_shell` 的实现抄一遍当判据，`run_shell_captured` 哪天改成显式
        `executable=`（比如换 Git Bash），那种写法**照样全绿**，而我们已经在
        对模型撒谎了。
        """
        detected = _detect_shell()

        if os.name == "nt":
            # cmd.exe 认 %COMSPEC%，POSIX shell 不认——用它区分「真的是 cmd」。
            probe = subprocess.run(
                "echo %COMSPEC%", shell=True, capture_output=True, text=True
            )
            real = probe.stdout.strip()
            self.assertTrue(real, "子进程没回显 COMSPEC，可能根本不是 cmd.exe")
            self.assertNotIn("%COMSPEC%", real, "变量没被展开——这个 shell 不是 cmd.exe")
            self.assertTrue(
                real.lower().endswith(detected.lower()),
                f"环境信息报的是 {detected!r}，真正执行命令的却是 {real!r}",
            )
        else:
            probe = subprocess.run(
                "echo $0", shell=True, capture_output=True, text=True
            )
            # POSIX 下 `sh -c` 的 $0 通常就是 shell 路径；个别实现回 "sh"。
            real = probe.stdout.strip()
            self.assertTrue(
                real.endswith("sh"), f"$0 回的是 {real!r}，与 /bin/sh 的预期不符"
            )
            self.assertEqual(detected, "/bin/sh")

    def test_detected_shell_is_a_bare_name_not_a_full_path(self) -> None:
        """
        只给名字不给完整路径——环境信息按轮次重复计费，路径没有额外信息量。
        """
        detected = _detect_shell()
        self.assertNotIn("\\", detected)
        if os.name == "nt":
            self.assertNotIn("/", detected)
            self.assertEqual(detected, os.path.basename(detected))


class EnvironmentLineTest(unittest.TestCase):
    """环境信息里那一行本身。"""

    def test_render_names_the_interpreter(self) -> None:
        text = collect_environment(_config(), os.getcwd()).render()
        self.assertIn("命令解释器：", text)
        self.assertIn(_detect_shell(), text)
        self.assertIn("run_command", text, "没说清这行说的是哪个工具用的 shell")

    def test_shell_is_a_required_field(self) -> None:
        """
        ⚠ **`shell` 刻意不给默认值。** 给了默认值，将来新增一个构造点忘了传
        就会静默报出一个错的解释器——而那正是本组护栏要防的形态。
        """
        with self.assertRaises(TypeError):
            EnvironmentInfo(  # type: ignore[call-arg]
                working_dir="/x",
                platform="p",
                date="2026-01-01",
                git_branch="main",
                model="m",
                protocol="deepseek",
            )


class ToolDescriptionTest(unittest.TestCase):
    """
    工具描述那一半。

    ⚠ **两处必须指同一个 shell**，这是成对维护点的可断言形态。
    """

    def test_description_and_environment_agree_on_the_shell(self) -> None:
        env_text = collect_environment(_config(), os.getcwd()).render()
        desc = RunCommandTool.description
        shell = _detect_shell()
        self.assertIn(shell, env_text)
        self.assertIn(shell, desc, "工具描述没提解释器，或与环境信息说的不是同一个")

    def test_description_matches_this_platform(self) -> None:
        """
        描述随平台切换，**两个平台各断言各的**。

        ⚠ **刻意写成分支而不是 `skipUnless`**：跳过的话另一个平台上这段描述
        一个字都没人验，而它是随 `os.name` 生成的、两条分支都可能被改坏。
        顺带避免在非 Windows 上凭空多出一条 skip——`CLAUDE.md` 那句
        「默认跳过 N 项」会因此在不同平台上对不上。

        Windows 那一支里 ⚠ **只说「是 cmd.exe」不够，两个陷阱必须逐条点名**：
        实测模型写的 `set X=0 && python` 是**教科书式正确的 cmd 语法**，
        败在 cmd 把 `&` 前的空格算进变量值；它还把这误诊成引号问题、又白烧一轮。
        一条只说 shell 名字的提示救不了这种。
        """
        desc = RunCommandTool.description
        if os.name == "nt":
            self.assertIn("cmd.exe", desc)
            self.assertIn("PowerShell", desc, "没排除掉 PowerShell 这个猜测")
            self.assertIn("tail", desc, "没说 cmd 里没有 tail 这类命令")
            self.assertIn("set X=1&&", desc, "没给出设环境变量的正确写法")
            self.assertIn("空格", desc, "没点名尾随空格这个陷阱")
            self.assertIn("VAR=", desc, "没说前缀式环境变量在 cmd 里不成立")
        else:
            self.assertIn("/bin/sh", desc)
            self.assertIn("POSIX", desc)
            # POSIX 上不该冒出 cmd 的那套说法（描述是按平台生成的，别串了）。
            self.assertNotIn("cmd.exe", desc)
            self.assertNotIn("set X=1&&", desc)

    def test_trailing_space_trap_is_real(self) -> None:
        """
        反证：那个陷阱确实存在，描述里那句话不是凭空写的。

        ⚠ 这条同时防「有人觉得这句话啰嗦想删掉」——删之前先看它是真的。
        """
        if os.name != "nt":
            self.skipTest("cmd 陷阱只在 Windows 上存在")
        read = 'python -c "import os;print(repr(os.environ.get(\'ZZ_TRAP\')))"'
        with_space = subprocess.run(
            f"set ZZ_TRAP=1 && {read}", shell=True, capture_output=True, text=True
        )
        without = subprocess.run(
            f"set ZZ_TRAP=1&& {read}", shell=True, capture_output=True, text=True
        )
        self.assertEqual(with_space.stdout.strip(), "'1 '", "尾随空格陷阱没复现")
        self.assertEqual(without.stdout.strip(), "'1'", "不留空格的写法反而不对")


class LanguageConstraintTest(unittest.TestCase):
    """
    「短更新也要用中文」的两处。

    ⚠ **两处缺一不可。** 语言约束本体在 `system_constraints`，但真正产出那些
    英文播报的是 `tone` 里「工作中给用户短更新」那一条——模型写那句话时读的
    是后者，不是几千 token 之前的前者。2026-09-17 真实 trace：主对话 12 段正文
    **8 段是英文**，全部是这类播报，而实质回答一段不落全是中文。
    """

    def test_constraint_covers_more_than_the_final_answer(self) -> None:
        from rhinecode.agent.prompt.texts.system_constraints import SYSTEM_CONSTRAINTS

        self.assertIn("每一个字都用中文", SYSTEM_CONSTRAINTS)
        self.assertIn("进度播报", SYSTEM_CONSTRAINTS, "没点名播报，模型会认为那不算「回答」")
        # 例外条款要留着，否则模型会把代码和命令也翻译了。
        self.assertIn("代码", SYSTEM_CONSTRAINTS)

    def test_old_wording_is_gone(self) -> None:
        """
        ⚠ **反证：旧措辞不得残留。**

        「始终用中文回答用户」字面上完全像句好话，最可能的退化就是有人觉得
        新写法啰嗦、顺手改回去——而那正是 8/12 段英文的成因。
        """
        from rhinecode.agent.prompt.texts.system_constraints import SYSTEM_CONSTRAINTS

        self.assertNotIn("始终用中文回答用户", SYSTEM_CONSTRAINTS)

    def test_tone_module_repeats_it_next_to_the_short_update_rule(self) -> None:
        from rhinecode.agent.prompt.texts.tone import TONE

        self.assertIn("短更新", TONE)
        idx = TONE.index("短更新")
        # 「用中文」必须紧挨着那一条，而不是飘在模块别处。
        self.assertIn("同样用中文写", TONE[idx : idx + 120])


if __name__ == "__main__":
    unittest.main()
