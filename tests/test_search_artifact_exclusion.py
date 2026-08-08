"""
搜索类工具对「RhineCode 自己写下的运行期产物」的排除。

## 这组用例的来历：真实模型端到端实测

一次普通的 `grep_content('USAGE')` 实测返回「68 处匹配 · 5 个文件」，其中
真正的源码命中**只有一个**，其余全来自 `.rhinecode/` 下的机器产物：
会话存档（`sessions/*.jsonl`）、上下文存盘（`context/*.txt`）、记忆笔记。

三重危害，前两条在那次实跑里都真的发生了：

1. **结果被自己的历史淹没**——用户说过的每一句话都逐字躺在会话存档里，
   搜任何他打过的字符串都会命中「他打过这句话」这条噪声。
2. **自放大**——搜索结果过大 → c8 第一层把它存进 `.rhinecode/context/` →
   下一次搜索命中这个存盘 → 结果更大 → 再存盘。实测滚到 1.8 MB，
   模型分段读它、读出来的却是上一次搜索结果的副本，白烧五次工具调用。
3. **它是 deny 规则的间接绕过**——`deny: Read(config.yaml)` 挡得住直接读，
   但那份内容一旦被读过就逐字躺在会话存档里，一次 grep 就能捞回来。

⚠ `.rhinecode/memory/` **刻意不排除**，本文件有一条用例钉住这个「刻意」，
免得后来的人看到「排除了三个、漏了一个」以为是漏改。
"""

import unittest
from pathlib import Path

from rhinecode.tools.glob_files import GlobTool
from rhinecode.tools.grep_content import GrepTool
from rhinecode.tools.path_guard import runtime_artifact_dirs_of
from tests.worktree_support import cleanup, make_plain_dir


NEEDLE = "MAGIC_TOKEN_XYZ"


class SearchExclusionBase(unittest.TestCase):
    """
    一个像样的项目：一处真实源码命中，外加 `.rhinecode/` 下的各类产物。

    每类产物都放同一个 `NEEDLE`，这样「有没有被排除」可以直接数命中文件数，
    不必去猜是哪一条规则起的作用。
    """

    def setUp(self):
        self.root = make_plain_dir(prefix="c14-search-")
        self.addCleanup(cleanup, self.root)

        def write(rel: str, text: str) -> None:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        # 唯一应当被搜到的那份
        write("src/app.py", f"{NEEDLE} = 1\n")

        # 机器产物三类
        write(".rhinecode/sessions/s1.jsonl", f'{{"role":"user","content":"{NEEDLE}"}}\n')
        write(".rhinecode/context/call_1.txt", f"工具结果原文里也有 {NEEDLE}\n")
        write(".rhinecode/traces/run.jsonl", f'{{"text":"{NEEDLE}"}}\n')

        # 刻意**不**排除的两类：记忆笔记与用户自己写的配置/角色
        write(".rhinecode/memory/notes.md", f"项目知识：{NEEDLE}\n")
        write(".rhinecode/agents/worker.md", f"---\ndescription: {NEEDLE}\n---\n")

        # 隔离工作区（既有的 F18 排除，一并回归）
        write(".rhinecode/worktrees/w1/src/app.py", f"{NEEDLE} = 1\n")

        self.grep = GrepTool()
        self.glob = GlobTool()

    def grep_files(self) -> set:
        """跑一次 grep，返回命中的文件相对路径集合（POSIX 分隔符，跨平台稳定）。"""
        result = self.grep.execute({"pattern": NEEDLE}, cwd=self.root)
        self.assertTrue(result.ok, result.output)
        hits = set()
        for line in result.output.splitlines()[1:]:
            if not line.startswith("  "):
                hits.add(line.replace("\\", "/"))
        return hits


class GrepExclusionTest(SearchExclusionBase):
    """grep_content 的排除。"""

    def test_runtime_artifacts_are_excluded(self):
        hits = self.grep_files()
        for noise in (
            ".rhinecode/sessions/s1.jsonl",
            ".rhinecode/context/call_1.txt",
            ".rhinecode/traces/run.jsonl",
        ):
            self.assertNotIn(noise, hits, f"运行期产物不该进搜索结果：{noise}")

    def test_real_source_hit_survives(self):
        """反证：排除不能把真正的源码命中一起干掉。"""
        self.assertIn("src/app.py", self.grep_files())

    def test_memory_and_agents_stay_searchable(self):
        """
        `.rhinecode/memory/` 与 `.rhinecode/agents/` **刻意不排除**。

        前者是刻意写下的项目知识摘要（体量小、语义明确，用户搜它是合理需求），
        后者是用户自己写的角色定义。排除的只是「机器逐字复刻对话与工具输出」
        的那三类。这条断言存在的意义是把「刻意」钉下来——否则下一个人看到
        「排了三个、漏了两个」会当成漏改顺手补上。
        """
        hits = self.grep_files()
        self.assertIn(".rhinecode/memory/notes.md", hits)
        self.assertIn(".rhinecode/agents/worker.md", hits)

    def test_worktrees_still_excluded(self):
        """F18 的既有排除一并回归（新加的判断排在它后面，别把它挤掉了）。"""
        hits = self.grep_files()
        self.assertFalse(
            any(h.startswith(".rhinecode/worktrees/") for h in hits),
            f"隔离工作区不该进搜索结果：{hits}",
        )

    def test_self_amplification_is_broken(self):
        """
        **现场重演**：上下文存盘里装着上一次搜索的结果，它绝不能再被搜到。

        这是那条自放大循环的闭环处——只要存盘目录还能被搜，
        「搜索 → 结果过大 → 存盘 → 下次搜到存盘 → 结果更大」就成立。
        故这条用例刻意把存盘内容写成「一份搜索结果的样子」。
        """
        spill = self.root / ".rhinecode" / "context" / "call_2.txt"
        spill.write_text(
            f"68 处匹配 · 5 个文件\nsrc/app.py\n  1│ {NEEDLE} = 1\n",
            encoding="utf-8",
        )
        self.assertNotIn(".rhinecode/context/call_2.txt", self.grep_files())


class GlobExclusionTest(SearchExclusionBase):
    """glob_files 的排除必须与 grep 同口径——两个工具分头写，最容易只改一个。"""

    def _glob(self, pattern: str) -> set:
        result = self.glob.execute({"pattern": pattern}, cwd=self.root)
        self.assertTrue(result.ok, result.output)
        return {
            line.replace("\\", "/")
            for line in result.output.splitlines()[1:]
            if line.strip()
        }

    def test_runtime_artifacts_are_excluded(self):
        hits = self._glob("**/*.jsonl")
        self.assertEqual(set(), hits, f"三类产物全是 .jsonl 或 .txt，不该有命中：{hits}")

    def test_real_files_survive(self):
        self.assertIn("src/app.py", self._glob("**/*.py"))

    def test_memory_stays_globbable(self):
        self.assertIn(".rhinecode/memory/notes.md", self._glob("**/*.md"))


class HelperTest(unittest.TestCase):
    """`runtime_artifact_dirs_of` 本身。"""

    def test_returns_the_three_artifact_dirs(self):
        root = Path("/tmp/proj")
        self.assertEqual(
            runtime_artifact_dirs_of(root),
            (
                Path("/tmp/proj/.rhinecode/sessions"),
                Path("/tmp/proj/.rhinecode/context"),
                Path("/tmp/proj/.rhinecode/traces"),
            ),
        )

    def test_is_per_root(self):
        """与 `worktrees_dir_of` 同口径：跟着调用者的工作目录走，不是全进程一份。"""
        self.assertNotEqual(
            runtime_artifact_dirs_of("/a"), runtime_artifact_dirs_of("/b")
        )
