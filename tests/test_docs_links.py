"""
文档链接与 `docs/todo/` 序号引用的完整性护栏（R7 方案 P2）。

## 为什么不用 `docs/todo/README.md` 里那条 grep

那份 README 自己规定了「重排后要跑一条 grep 自检」，而同一份文件里记着某处
**「此处四次成为悬空引用」**。题面据此问「是自检命令不够，还是没人记得跑？」
——R7 实测给出了第三个答案：**跑了也查不出来**。

在一棵有 2 处真悬空引用的树上，那条官方命令输出 6 行、逐行核对全部正确、
退出码 0。它的正则只认 `docs/todo/N` 这种带前缀的形态，而真实的悬空写成相对
链接 `(3-skill-recall-eval.md)` 与反引号裸文件名 `` `4-p1b-unattended.md` ``。

**跑了会拿到一份看起来很干净的报告**——所以「没人记得跑」这个诊断是错的，
而把一条查不出问题的命令接进 CI 只会得到一个永远绿的门禁。

## 判定规则：查「序号过期」，不查「文件存不存在」

`docs/todo/` 的文件名是 `<序号>-<slug>.md`，而**重排只改序号、不改 slug**
（那个目录的既定约定，用 `git mv` 保住历史）。于是有一条零误报的判定：

> 全仓任何一处形如 `N-<slug>.md` 的引用，若 `<slug>` 对应的文件**当前存在**
> 且序号 ≠ `N` → 序号过期。

三个性质：**不需要任何登记表**（事实源就是 `ls docs/todo/`）；**对已删除的待办
天然免疫**（`docs/todo/README.md` 那一大段重排历史提到十来个已删文件，它们的
slug 不在当前集合里，规则直接跳过——而一个朴素的「链接必须能解析」检查会把
它们全部误报）；**覆盖全部书写形态**（它只认 `N-slug.md` 这个字符串本身）。
"""

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TODO_DIR = REPO_ROOT / "docs" / "todo"

# `N-slug.md`：覆盖 markdown 链接、反引号裸名、正文直写三种形态
_TODO_REF = re.compile(r"([0-9]+)-([0-9A-Za-z][-0-9A-Za-z_]*\.md)")

# ⚠ `docs/review/` 整个排除：审查台账的职责就是**逐字引用**过期的引用作为证据。
# 不排除的话，一份写着「这里引用了 3-skill-recall-eval.md（应为 1-）」的报告
# **自己就会红**——而它写的是对的。
# R7 实测：不排除时本轮的报告一份文件被误报 16 次。
#
# ⚠ 这条排除**不是「那里可以随便写错」**：`05-maintainability.md` 里确实有 5 处
# 真坏链，它们被这条挡在管辖之外，只能靠人改（本批已改）。护栏不管 ≠ 没错。
_EXCLUDE_DIRS = ("docs/review/",)


def _iter_docs():
    """全部纳入检查的 markdown（含仓库根的两份），跳过审查台账。"""
    for path in sorted(REPO_ROOT.glob("docs/**/*.md")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not any(rel.startswith(d) for d in _EXCLUDE_DIRS):
            yield path
    for name in ("CLAUDE.md", "README.md"):
        path = REPO_ROOT / name
        if path.exists():
            yield path


class TodoNumberingTest(unittest.TestCase):
    def test_no_stale_todo_numbers(self) -> None:
        """
        全仓不得引用过期的待办序号。

        重排 `docs/todo/` 之后**不需要记得跑任何命令**——这条用例会红，
        并逐处给出「引用了什么 / 当前应为什么」。

        ⚠ R7 实测：真实规模是 **10 处**而不是题面说的 2 处，**8 处在
        `docs/todo/` 之外**。那个目录 README 里「重排时要改的都在本目录」这个
        判断方向反了，自检命令的搜索路径也因此写死成了 `docs/todo/`——
        而漏掉的那些恰恰最要紧：好几处在「一键开工 Prompt」段落里，
        那是要被原样粘进新 session 的文本，粘过去照着读文件会直接读不到。
        """
        current = {}
        for f in sorted(TODO_DIR.glob("[0-9]*-*.md")):
            num, slug = f.name.split("-", 1)
            current[slug] = num

        stale = []
        for f in _iter_docs():
            lines = f.read_text(encoding="utf-8").splitlines()
            for lineno, line in enumerate(lines, 1):
                for num, slug in _TODO_REF.findall(line):
                    # slug 不在当前集合里 = 该待办已完成删除，
                    # 历史记录提到它是正常的，不报
                    if slug in current and current[slug] != num:
                        rel = f.relative_to(REPO_ROOT).as_posix()
                        stale.append(
                            f"{rel}:{lineno} 引用 {num}-{slug}，"
                            f"当前应为 {current[slug]}-{slug}"
                        )
        self.assertEqual(stale, [], "待办序号过期：\n" + "\n".join(stale))

    def test_the_rule_ignores_deleted_todos(self) -> None:
        """
        ⚠ **反证：提到「已删除的待办」不算错。**

        `docs/todo/README.md` 那一大段重排历史**必须**提到十来个已经完成删除的
        文件名，那是它作为历史记录的职责。一个朴素的「链接必须能解析」检查会把
        它们全部误报，然后这个门禁就会被整体关掉。

        判据：造一个当前集合里没有的 slug，规则必须放过它。
        """
        current = {"skill-recall-eval.md": "1"}
        line = "当年那份 `9-some-long-deleted-todo.md` 已经做完删了"

        stale = [
            (num, slug)
            for num, slug in _TODO_REF.findall(line)
            if slug in current and current[slug] != num
        ]
        self.assertEqual(stale, [], "对已删除待办的引用被误报了")


class MarkdownLinkTest(unittest.TestCase):
    def test_relative_links_resolve(self) -> None:
        """
        文档里的相对链接必须能解析到真实文件。

        ⚠ 只查相对链接，**不查反引号裸文件名**——后者常常是「用户会创建的文件」
        （`RHINE.md`）或「计划中的文件」（`CONTRIBUTING.md`），R7 的原型实测那条
        规则误报 100+ 处，是典型的会被整体关掉的噪声门禁。

        ⚠ **围栏代码块整块跳过，这一条是本批实测补上的。** R7 记着
        `05-maintainability.md` 有「5 处真坏链」，诊断是「在子目录里写了从仓库根
        出发的路径」。本批逐处核对发现**那 5 处全在围栏代码块里**——它们是 R6 产出
        的 `CONTRIBUTING.md` 草案正文，而那份文件将来住在**仓库根**，从根出发的
        路径对它而言完全正确。改成 `../docs/…` 反而会让草案装上去之后是错的。

        所以这不是坏链，是**草案里的路径按目的地解析**。不跳过代码块的话，任何
        一份「在文档里贴一段将来住别处的文件」都会误报——而那正是本项目 spec 驱动
        开发的常规写法（`05-maintainability.md` 里还贴着 ci.yml 与 pyproject 片段）。
        """
        link = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")
        broken = []
        for f in _iter_docs():
            lines = f.read_text(encoding="utf-8").splitlines()
            in_fence = False
            for lineno, line in enumerate(lines, 1):
                if line.lstrip().startswith("```"):
                    in_fence = not in_fence
                    continue
                if in_fence:
                    continue
                for target in link.findall(line):
                    if target.startswith(("http://", "https://", "mailto:")):
                        continue
                    # ⚠ 代码块里的 JSON/字典字面量会被链接正则误抓（R7 实测撞到
                    # `{'query':'abc'}`），只放行长得像路径的目标
                    if not re.fullmatch(r"[-\w./]+", target):
                        continue
                    if not (f.parent / target).resolve().exists():
                        rel = f.relative_to(REPO_ROOT).as_posix()
                        broken.append(f"{rel}:{lineno} -> {target}")
        self.assertEqual(broken, [], "坏链：\n" + "\n".join(broken))


class ExtensionIndexTest(unittest.TestCase):
    def test_every_extension_is_indexed(self) -> None:
        """
        `docs/extensions/README.md` 的索引表必须覆盖目录下每一个扩展。

        R7 实测漏了两个（`auto-plan/` 与 `ask-user/`，全文 0 次提及），
        而它们各有完整的 spec/plan/task/checklist。

        ⚠ 比对的是**集合**不是**个数**：个数相等而内容不同的情况真实存在过，
        这与 P1 那边「只比个数挡不住『加一个删一个』」是同一条理由。
        """
        ext_root = REPO_ROOT / "docs" / "extensions"
        dirs = {p.name for p in ext_root.iterdir() if p.is_dir()}
        index = (ext_root / "README.md").read_text(encoding="utf-8")
        missing = sorted(d for d in dirs if f"{d}/" not in index)
        self.assertEqual(missing, [], f"扩展目录未进索引：{missing}")


if __name__ == "__main__":
    unittest.main()
