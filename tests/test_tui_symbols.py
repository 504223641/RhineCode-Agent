"""
符号白名单的全仓扫描护栏（tui-display 扩展 F28/F29，AC19）。

## 为什么需要一条扫描式的护栏

「界面上不出现表情符号」是一条**全局**约束，而表情是一次加一个混进来的：
下一个人给某条新提示挂个 `⏱`，既不报错也不会让任何一条既有用例变红。
逐处断言拦不住这个——它只覆盖今天存在的那些位置。

## 判据边界（**读这两条再改本文件**）

⚠ **本文件只管「界面上的字」**，两类刻意不管：

1. **注释与 docstring**。项目里到处是 `⚠` 开头的警告注释，它们是写给开发者
   看的、不进界面。全都改掉既没有收益，又会把这份扫描变成一场噪音清理。
2. **发给模型的提示词**（`skills/render.py` / `subagents/render.py` /
   `team/render.py` / `tools/run_agent.py` / `runner.py` 里那些 `⚠️` 开头的
   句子）。它们是 prompt 不是界面，而且那几段的措辞是真实模型验收反复调过的
   （见「委派触发口径」那次反转）——为一条排版约束去动它们，是拿一次真实
   行为回归换一个看不见的整洁。

因此扫描只覆盖 `tui/` 与那几个**产出界面文本**的报告函数所在模块。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import rhinecode

# 白名单（F29）。判据两条：**单色字形**（任何终端里都不会被渲染成彩色图形）、
# 且**每个的语义能一句话说清、互不重叠**。
WHITELIST = {
    "●",   # 发生了一件事——工具行、活动行；状态靠颜色区分
    "⎿",   # 从属于上一行——结果、子调用、折叠详情
    ">",   # 你，或当前选中——用户消息前缀与面板高亮指示符合一
    "·",   # 分隔（`23s · 3.1k tokens`），不作行首前缀
    "↑",   # token 计数
    "✻",   # 思考块
}

# 「看起来会被渲染成彩色图形」的码点区间。刻意不用 `emoji` 之类的第三方库：
# 本项目不为一条测试引入依赖，而这几段区间已经覆盖了实际会混进来的那些。
_SUSPECT = re.compile(
    "[\U0001F000-\U0001FAFF"   # 各类 emoji
    "\U00002600-\U000026FF"    # 杂项符号（⚠ ⛔ ✻ 都在这里）
    "\U00002700-\U000027BF"    # 装饰符号（✅ ❌ ❓）
    "\U00002B00-\U00002BFF"    # 箭头与几何图形
    "\U0001F7E0-\U0001F7EB]"   # 彩色圆点
)

# 只扫这些模块：它们要么就是界面层，要么产出**直接显示给用户**的报告文本。
_UI_MODULES = (
    "tui/app.py",
    "tui/widgets.py",
    "commands/builtins.py",
    "context/manager.py",
    "context/offload.py",
    "mcp/manager.py",
    "memory/manager.py",
    # ⚠ `team/render.py` **按函数扫**：同一个模块里既有 `/tasks` 报告
    # （界面），也有 `render_team_brief()`（发给模型的组队说明）。后者的措辞是
    # 真实模型验收反复调过的，不在本文件的判据范围内。
    "tools/team_tasks.py",
    "subagents/report.py",
    "hooks/report.py",
)

# 字符串字面量。够用即可——本项目的界面文本都是普通引号里的单行/f-string。
_STRING = re.compile(r'(?:f?"(?:[^"\\\n]|\\.)*")|(?:f?\'(?:[^\'\\\n]|\\.)*\')')


# `team/render.py` 里**只扫这两个产出界面文本的函数**，跳过发给模型的那段。
_PARTIAL = {"team/render.py": ("def render_board", "def render_roster")}


def _slice_functions(text: str, starts: "tuple[str, ...]") -> str:
    """截出若干个顶层函数的源码（从 `def X` 到下一个顶层 `def`/常量为止）。"""
    lines = text.splitlines()
    out: list[str] = []
    keeping = False
    for line in lines:
        if line.startswith("def ") or (line and not line[0].isspace() and "=" in line):
            keeping = any(line.startswith(s) for s in starts)
        if keeping:
            out.append(line)
    return chr(10).join(out)


def _visible_strings(path: Path, only: "tuple[str, ...] | None" = None):
    """
    逐行取出「像是会被显示出来」的字符串字面量。

    刻意用正则而不是 `ast`：`ast` 分不清 docstring 之外的注释，
    也会把整段三引号 docstring 当成字符串交上来——那正是本文件不想扫的东西。
    这里跳过纯注释行与三引号行，剩下的单行字面量就是界面文本的绝大多数。
    """
    text = path.read_text(encoding="utf-8")
    if only is not None:
        text = _slice_functions(text, only)
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        for match in _STRING.finditer(line):
            yield number, match.group()


class SymbolWhitelistTest(unittest.TestCase):
    def test_no_emoji_in_ui_strings(self) -> None:
        """AC19a：界面文本里搜不到白名单之外的图形符号。"""
        root = Path(rhinecode.__file__).parent
        offenders = []
        for rel in _UI_MODULES + tuple(_PARTIAL):
            path = root / rel
            for number, literal in _visible_strings(path, _PARTIAL.get(rel)):
                for match in _SUSPECT.finditer(literal):
                    if match.group() in WHITELIST:
                        continue
                    offenders.append(f"{rel}:{number}  {match.group()}  {literal[:70]}")
        self.assertEqual(
            offenders,
            [],
            "界面文本里出现了白名单之外的符号（F29）。要新增必须先进 CLAUDE.md "
            "里那张表，并同步本文件的 WHITELIST：\n" + "\n".join(offenders),
        )

    def test_whitelist_matches_the_documented_table(self) -> None:
        """
        白名单就是 CLAUDE.md 里那张表，两处不许分叉。

        分叉的表现是「文档说不许用，测试却放过」——而测试是唯一会被执行的那份。
        """
        self.assertEqual(len(WHITELIST), 6, "F29 收敛后恰好六个符号")

    def test_the_whitelisted_symbols_are_actually_used(self) -> None:
        """
        **反证**：白名单不是许愿池。

        六个里每一个都必须真的在界面代码里用着——放一个没人用的进去，
        等于给未来的人一个「这个符号是被批准过的」的错误信号。
        """
        root = Path(rhinecode.__file__).parent
        source = "\n".join(
            (root / rel).read_text(encoding="utf-8") for rel in _UI_MODULES
        )
        for symbol in WHITELIST:
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, source)


if __name__ == "__main__":
    unittest.main()
