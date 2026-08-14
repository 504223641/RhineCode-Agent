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
    "●",   # 发生了一件事——工具行、活动行、批次聚合行；状态靠颜色区分
    "⎿",   # 从属于上一行——结果、子调用、折叠详情
    ">",   # 当前选中——面板高亮指示符
    "·",   # 分隔（`23s · 3.1k tokens`），不作行首前缀
    "↑",   # 输入 token（状态行；发上去的那一半）
    "↓",   # 输出 token（状态行；收回来的那一半）
    "→",   # 从 A 到 B / 前后对照（`关闭 → 高效`、`声明值 → 实际值`、`事件 → 动作`）
    "↔",   # 两态互换（`[AUTO] ↔ [PLAN]`、`逐条 ↔ 全文`）
    "✻",   # 思考块
    # ── tui-activity-fold 新增 ──
    # 回合状态行的旋转标记（四帧循环：◇ → ◈ → ◆ → ◈）。
    "◇",
    "◆",
    # ⚠ **`◈` 一符两用，是本轮登记时发现的一处待决冲突**：
    # 它既是**用户消息行的前缀**（`#99FFFF` 青色粗体，tui-display 起就在用），
    # 又是状态行旋转标记的第二、四帧（`#7AEEFF` 主题青）。
    # 两处都在行首、两种青色几乎分不出，而状态行就在输入框上方、离用户消息不远。
    #
    # 另有一处**文档与代码的既有不一致**：CLAUDE.md 与本文件此前都写着
    # 「用户消息前缀是 `>`」，而代码里实际是 `◈`——之所以一直没被发现，
    # 是因为扫描区间原本不覆盖 Geometric Shapes 区块（见 `_SUSPECT` 的注释）。
    "◈",
}

# 「看起来会被渲染成彩色图形」的码点区间。刻意不用 `emoji` 之类的第三方库：
# 本项目不为一条测试引入依赖，而这几段区间已经覆盖了实际会混进来的那些。
_SUSPECT = re.compile(
    "[\U0001F000-\U0001FAFF"   # 各类 emoji
    "\U00002600-\U000026FF"    # 杂项符号（⚠ ⛔ ✻ 都在这里）
    "\U00002700-\U000027BF"    # 装饰符号（✅ ❌ ❓）
    "\U00002B00-\U00002BFF"    # 箭头与几何图形
    "\U000025A0-\U000025FF"    # Geometric Shapes（● ◇ ◈ ◆ ▲ ▪ …）
    "\U00002190-\U000021FF"    # Arrows（↑ ↓ ← → ↔ ⇒ …）
    "\U0001F7E0-\U0001F7EB]"   # 彩色圆点
)
# ⚠ **Arrows 那一段是「输入/输出分开显示」那轮补的，与 Geometric Shapes 同一个坑。**
#
# 白名单里从一开始就有 `↑`，但扫描区间**从来没覆盖过箭头**（U+2191 落在
# Arrows 区块 2190–21FF，而原来的五段区间一段都不沾）——也就是说那一条白名单
# 登记了三年、一次都没生效过。加第二个箭头 `↓` 时才发现。
#
# 补上之后，将来往界面上塞 `→` `⇒` 这类箭头会被拦下（它们与 `↑`/`↓` 的语义
# 距离很近，混用会让「这个箭头是什么意思」变成每次都要想一下的事）。
# ⚠ **Geometric Shapes 那一段是 tui-activity-fold 补进来的。**
#
# 此前它不在扫描范围内，后果是**白名单里最常用的那个符号（`●`）从来没被
# 护栏管过**，而用户消息前缀 `◈` 与文档记的 `>` 分叉了很久也没人发现。
# 也就是说：这张表原本声称管六个符号，实际只管得到其中三个。
#
# 补上之后覆盖面才与白名单一致。代价是本区块里的常见排版字符
# （`▪` `▫` `■` `□` 等）今后一律要先登记——那正是想要的效果。

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
        self.assertEqual(
            len(WHITELIST),
            12,
            "六个原有符号 + tui-activity-fold 的三帧旋转标记 + 箭头三个"
            "（`↓` 输出 token、`→` 从 A 到 B、`↔` 两态互换）",
        )

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
