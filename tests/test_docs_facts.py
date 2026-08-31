"""
文档里的计数与代码现算值的一致性护栏（R7 方案 P1）。

## 为什么要为几个数字写测试

同一个整数活在 6 个地方（trace 事件类数实测），**没有一处是「源」**。写它的人
每次只改手上那一份——不是不细心，是没有任何东西告诉他还有五份。

R7 用同一批方法扫出 12 条漂移，其中最能说明问题的一条：`docs/guide/skills.md`
是 2026-08-30 **新建**的文件，建的当天就已经继承了一处旧的错误数字。
**新写的文档不会自动是对的**——它是从旧文档复制来的。

## 与 `tests/run_parallel.py` 同构

期望值**当场从代码算**，绝不硬编码：断言的左边永远是 `len(list(SomeEnum))`，
右边才是文档。硬编码期望值的护栏只能发现「代码变了」，发现不了「文档没跟上」
——而后者才是这里要治的。

## ⚠ 成对维护点

- 新增一处对这些数字的表述 → 加进对应 `Fact` 的 `sites`；否则那一处**不受保护**
  （漏改不报错）。那份清单本身就是「这个事实住在哪几处」的可执行版文档。
- 改这些数字的**措辞** → 断言②会红（登记了却没匹配上），那是**刻意的**：
  文案改写会让正则静默失效，而**静默失效的护栏比没有护栏更糟**。

## ⚠ 三条明确不做的

1. **不做全仓扫描。** R7 原型实测会大量误报在「子集说法」上——
   `docs/c14/checklist.md` 的「四类（worktree 新增的那批）」与总数说法在文本层面
   长得一模一样，只能靠「登记文件 + 上下文词」区分。一个会误报的门禁的结局是
   被整体关掉。
2. **不给 `docs/c*/` 与 `docs/extensions/*/` 接护栏。** 它们是**验收当时的快照**，
   数字随实现演进而与现状不符是**正确的**，不是漂移。改它们等于篡改记录。
3. **不接测试条数。** 它满足全部判据却不该接——**每次加用例都会变**，接了等于每个
   加测试的 PR 都要顺手改一次文档数字，而人对噪声门禁的标准反应是放宽断言。
   判断一个数字要不要接护栏看的是 **变更频率 ÷ 拷贝数**，不是「能不能算出来」。
   那几处已改成「不写具体数」。
"""

import pathlib
import re
import unittest

from rhinecode.hooks.models import HookEventType
from rhinecode.skills.models import AdviceKind
from rhinecode.trace.models import TraceEventType

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_DIGITS = "零一二三四五六七八九"


def to_chinese(n: int) -> str:
    """
    把 0–99 的整数写成中文。

    :param n: 0–99
    :returns: 中文数字

    文档里一律用中文数字，故只需这一个方向。
    """
    if n < 10:
        return _DIGITS[n]
    if n < 20:
        return "十" + (_DIGITS[n % 10] if n % 10 else "")
    return _DIGITS[n // 10] + "十" + (_DIGITS[n % 10] if n % 10 else "")


class Fact:
    """
    一个「代码里算得出、文档里写了 N 遍」的事实。

    :param calc: 现算真值的函数（**唯一的期望值来源**）
    :param pattern: 从文档行里抓中文数字的正则，**必须带上下文词**——不带的话会把
                    「某一章新增四类」这种子集说法一起抓进来（R7 实测踩过）
    :param sites: 这个事实住在哪几份文档里，相对仓库根
    """

    def __init__(self, calc, pattern: str, sites: list[str]) -> None:
        self.calc = calc
        self.pattern = re.compile(pattern)
        self.sites = sites


FACTS: dict[str, Fact] = {
    "trace 事件类数": Fact(
        calc=lambda: len(list(TraceEventType)),
        pattern=r"([零一二三四五六七八九十]+)类(?:\*\*)?(?:结构化)?事件",
        sites=[
            "CLAUDE.md",
            "README.md",
            "docs/guide/features.md",
            "docs/guide/project-structure.md",
            "docs/internals/architecture.md",
        ],
    ),
    "Hook 事件数": Fact(
        calc=lambda: len(list(HookEventType)),
        pattern=r"([零一二三四五六七八九十]+)个事件",
        sites=[
            "CLAUDE.md",
            "docs/guide/features.md",
            "docs/guide/hooks.md",
            "docs/internals/architecture.md",
            "docs/internals/capabilities.md",
        ],
    ),
    # ⚠ **口径已裁定：数 `AdviceKind` 成员（8），不是数 `_audit_one` 里的函数调用（7）。**
    #
    # 这不是笔误，两个数**都对**——`_check_grants` 一个函数产出两类建议
    # （`GRANTS_ALL_DROPPED` 与 `BROAD_GRANT`），于是文档里那 20 多处「七项 / 八项」
    # 是**两派各自自洽**。R7 把这条单独列出来要求人先裁定，理由是「自动生成治不了
    # 口径未定义」。
    #
    # 选 8 的理由：**文档里的计数是写给读者的，应该数读者能观察到的东西**——用户在
    # `/skills` 报告里看到的是 8 种不同的建议，不是 7 个函数。而且事实源唯一且稳定
    # （一个 Enum），另一个口径的事实源是「AST 里数函数调用」，脆弱得多。
    "Skill 体检项数": Fact(
        calc=lambda: len(list(AdviceKind)),
        pattern=r"([零一二三四五六七八九十]+)项检查",
        sites=["CLAUDE.md", "docs/guide/skills.md", "docs/internals/capabilities.md"],
    ),
}


class DocsFactsTest(unittest.TestCase):
    """三个断言：值对、登记有效、名字清单也对。"""

    def test_documented_counts_match_code(self) -> None:
        """
        断言①：登记文件里每一处该计数，都等于代码现算值。

        ⚠ **必须先收集完再断言一次，不能在循环里 assert。** 循环里断言会在第一处
        不一致时中止，于是修的人只看得见 1 处、改完再跑又冒出第 2 处——既看不到
        规模，也没法一次改完。R7 实测：循环内断言只报出 `CLAUDE.md` 一处，
        收集式报出 4 处。
        """
        wrong = []
        for name, fact in FACTS.items():
            want = to_chinese(fact.calc())
            for rel in fact.sites:
                lines = (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
                for lineno, line in enumerate(lines, 1):
                    for got in fact.pattern.findall(line):
                        if got != want:
                            wrong.append(
                                f"{rel}:{lineno} 写「{got}」，应为「{want}」"
                                f"（{name} 现算 {fact.calc()}）"
                            )
        self.assertEqual(wrong, [], "文档计数与代码不一致：\n" + "\n".join(wrong))

    def test_every_registered_site_still_matches(self) -> None:
        """
        断言②：登记的每个文件都至少匹配上一处。

        ⚠ **这条比断言①更容易被忽略，但缺了它整套护栏会静默失效。** 文案改写
        （哪怕只是加一对加粗星号）会让正则不再匹配，于是断言①在那个文件上
        「零处需要检查」——**全绿，而文件是错的**。

        R7 原型实测撞过：`**二十七类**事件枚举` 里的星号让正则漏了整个文件，
        而如果没有这条断言，那个文件会被当成「检查过且通过」。
        """
        for name, fact in FACTS.items():
            for rel in fact.sites:
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
                self.assertTrue(
                    fact.pattern.search(text),
                    f"{rel} 登记了「{name}」却一处都没匹配上——"
                    f"要么措辞改了（同步改 pattern），要么该处已删（从 sites 移除）",
                )

    def test_trace_scope_names_are_documented(self) -> None:
        """
        断言③：**名字清单**也要对得上，不只是个数。

        只比个数挡不住两种真实发生过的漂移：
        ① 加一个、删一个 → 计数纹丝不动而内容全错；
        ② 名字本身写错 → 架构文档曾写作用域叫 `notes`，而代码里那个常量叫
           `memory`，且漏了 c16 新增的 `classifier`。
        """
        from rhinecode.trace import models

        actual = {
            v
            for k, v in vars(models).items()
            if k.startswith("SCOPE_") and isinstance(v, str)
        }
        text = (REPO_ROOT / "docs/internals/architecture.md").read_text(encoding="utf-8")
        missing = sorted(s for s in actual if f"`{s}`" not in text)
        self.assertEqual(missing, [], f"架构文档没提到这些作用域：{missing}")


class LeafPackageWordRetiredTest(unittest.TestCase):
    """
    ⚠ **「叶子包」这个词已从架构文档里退役（D4，口径已裁定）。**

    R7 用 AST 与运行期两种方式各量了一遍，结论是**这个词有三种量法，三种答案
    不一样**，而同一个包在两种量法下结论可以完全相反：

    | 包 | 量法 A（`import <包>` 的运行期闭包） | 量法 B（包内任一模块的模块级 import） |
    | --- | --- | --- |
    | `web` | ✅ **无**（`__init__.py` 不 re-export） | ❌ `permission` / `provider` / `tools` / `trace` |
    | `trace` | ✅ 无 | ❌ `provider` |
    | `skills` | ❌ `permission` / `tools` / `trace` | ❌ `permission` / `trace` |

    两个人各拿一种量法会得出完全相反的结论，而**谁都没写错**。

    修法不是「把这个词的定义写清楚」，是**别用这个词**——`todo` 与 `classifier`
    的现有说法（「只依赖标准库与 `trace`」/「只依赖 `provider.base` 与 `trace`」）
    **在两种量法下都成立**，因为它们写的是**依赖清单**而不是一个形容词。

    ⚠ R6 在另一个数字上撞过同一堵墙：强连通分量是 10 还是 11 取决于两个口径，
    而 `context` 之所以进环，恰恰因为 `agent → context` 那条边**只存在于
    `TYPE_CHECKING` 里**——**作者为了不进环而做的事被扫描器算成了一条真边**。
    那是第三种量法。
    """

    #: 这两份「描述当前状态」的文档不该再出现这个形容词。
    #: ⚠ 刻意**不含** `docs/c*/` 与 `docs/extensions/*/`——历史 spec 是当时的快照。
    #: 也不含 `docs/review/`（审查台账要逐字引用当时的说法作为证据）。
    _LIVE_DOCS = ("CLAUDE.md", "docs/internals/architecture.md")

    def test_the_word_is_gone_from_live_docs(self) -> None:
        for rel in self._LIVE_DOCS:
            with self.subTest(doc=rel):
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
                # ⚠ 判据要区分**断言**与**提及**：
                #   断言 = 「`skills` 是叶子包」「（c15，叶子包）」——这是要禁的；
                #   提及 = 「⚠ **「叶子包」这个词已退役**……」——这是**必须留着**的
                #          那段说明，删了它下一个人会把这个词原样加回来。
                # 用中文引号 `「叶子包」` 把提及标出来，判据只查引号之外的出现。
                bad = [
                    line.strip()[:60]
                    for line in text.splitlines()
                    if "叶子包" in line.replace("「叶子包」", "")
                ]
                self.assertEqual(
                    bad,
                    [],
                    f"{rel} 里还有断言式的「叶子包」用法：{bad}——"
                    f"这个词有三种量法、结论会反转，请改写成实际依赖清单",
                )

    def test_replacement_wording_actually_lists_dependencies(self) -> None:
        """
        反证：光删掉那个词不够，**得换成依赖清单**。

        删了不换等于把信息扔掉——读的人本来能从「叶子包」推出「不依赖兄弟包」，
        现在什么都推不出来了。判据取架构表里那几个包旁边确实写了「只依赖 …」。
        """
        text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("只依赖标准库", text)


if __name__ == "__main__":
    unittest.main()
