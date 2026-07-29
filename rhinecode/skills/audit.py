"""
Skill 体检——把「已加载的定义 + 覆盖事实」算成「一组可操作的建议」。

## 这一层解决什么

Skill 系统对齐开放标准之后**可导入**了，但仍然**不可创作**：系统只会读
Skill 目录，不会检查用户写得对不对。写错时的反馈也极弱——要么静默失效
（域名规则漏写 `domain:` 前缀，那条预授权无声消失），要么给出一句只陈述现象、
不给改法的警告。

既有三类反馈各司其职，但都不回答「那我该怎么改」：

| 类别 | 它说的是 |
|---|---|
| `SkillLoadError` | 这份文件我看见了但没能用上，原因是…… |
| catalog 的 warnings | 这次运行过程中发生了什么 |
| `SkillSpec.notices` | 你的某个声明与实际行为有出入 |
| **本模块产出的 `SkillAdvice`** | **这份 Skill 能用，但有更好的写法，建议改成……** |

「可操作」是这一类反馈存在的**全部理由**：一条只说「预授权过宽」却不给出
带模式写法的建议，与既有的警告没有区别。所以 `SkillAdvice` 强制三段式，
每条都必须填 `suggestion`。

## 为什么坐在 `validation` 与 `manager` 之间

位置是**被依赖关系逼出来的**，不是随手挑的：

- 往下：「预授权全军覆没」要知道声明**翻译成了几条规则** → 依赖 `validation`；
  「逼近注入上限」要量**渲染后的注入段** → 依赖 `render`。
  所以必须在这两者**之上**。
- 往上：`manager` 是本包唯一持有可变状态与做 IO 的地方，体检一旦挪进去就不再是
  纯函数（spec N1 要求零 IO、可用字符串字面量直接单测）。所以必须在它**之下**。

夹在中间只剩这一个位置。另外 `render` 与 `validation` **互不 import**
（前者只依赖 `models`，后者只依赖 `permission` + `models`），
所以本模块同时依赖两者**不会成环**。

## 为什么不叫 `lint_*`

`tests/test_skill_validation.py::ObsoleteApiRemovedTest` 钉着 `validation` 模块里
不得再出现 `lint_skill` / `lint_skills`——那是上一轮作者期为**已废止的
「白名单收窄」语义**写的，随对齐改造一起删除了。沿用旧名字会让人以为
那套东西回来了。用 `audit` 明确区分。

## 纯函数、零 IO

输入是已解析的定义与覆盖事实，输出是建议元组。不读文件、不访问网络、
不依赖工具注册中心的状态。因此可以用字符串字面量构造定义直接单测，
无需创建任何临时目录。

结果**每次现算，调用方不要缓存**：缓存就要考虑何时失效，多一处
「热更新之后忘了更新」的机会，而体检的成本只是几十个 Skill 的字符串判断。

对应文档：`docs/extensions/skill-authoring/spec.md`（F1–F6、F2a）。
"""

from typing import Iterable, Sequence

from rhinecode.permission.config import DOMAIN_PREFIX
from rhinecode.permission.models import Rule
from rhinecode.skills.models import (
    BODY_MAX_BYTES,
    BODY_MAX_LINES,
    DESCRIPTION_MAX_CHARS,
    MCP_PREFIX,
    NEAR_LIMIT_RATIO,
    PLACEHOLDER,
    READ_ONLY_GRANT_TOOLS,
    AdviceKind,
    SkillAdvice,
    SkillSource,
    SkillSpec,
)
from rhinecode.skills.render import render_active_body
from rhinecode.skills.validation import grants_for

# 判定「模式是不是纯通配」时认的字符。fnmatch 的元字符里只有这三个能构成
# 「匹配一切」，`[` 单独出现不成立，但保守起见一并算上（多报一条建议无害）。
_WILDCARD_CHARS = "*?["


def audit_skills(
    specs: Iterable[SkillSpec],
    shadowed: Sequence[tuple[str, SkillSource]] = (),
) -> tuple[SkillAdvice, ...]:
    """
    对一批 Skill 定义跑一遍体检。

    :param specs: 已跨层覆盖、最终生效的那批定义（即 `SkillCatalog.skills`）
    :param shadowed: 被覆盖掉的定义，每个元素是 `(命令名, 被覆盖那份所在的层)`
                     （即 `SkillCatalog.shadowed`）。**缺省为空**是为了让调用方
                     在只关心前几项检查时可以不传——那时「覆盖了内置」恒不触发，
                     这是正确的而非漏检
    :returns: 建议元组，**按「Skill 名 → 检查项定义顺序」排序**

    ## 排序契约

    输出顺序固定，使报告稳定、测试可逐条断言。同一个 Skill 的多条建议
    按检查项在本模块中的实现顺序排列（与 `AdviceKind` 的成员定义顺序一致）。

    副作用：无（纯函数）。
    """
    # 建议按 Skill 名分组产出，天然满足「Skill 名 → 检查项顺序」的排序契约：
    # 外层按名字排序遍历，内层按检查项的调用顺序追加。
    advices: list[SkillAdvice] = []
    for spec in sorted(specs, key=lambda s: s.command_name):
        advices.extend(_audit_one(spec, shadowed))
    return tuple(advices)


def _audit_one(
    spec: SkillSpec,
    shadowed: Sequence[tuple[str, SkillSource]],
) -> list[SkillAdvice]:
    """
    对单个定义跑全部检查项。

    :param spec: 一份已加载的定义
    :param shadowed: 全局的覆盖事实（判「覆盖了内置」时用）
    :returns: 该定义的建议列表，**按检查项定义顺序**

    副作用：无。
    """
    out: list[SkillAdvice] = []
    out.extend(_check_description_length(spec))
    out.extend(_check_missing_description(spec))
    out.extend(_check_placeholder(spec))
    out.extend(_check_grants(spec))
    out.extend(_check_injection_size(spec))
    out.extend(_check_overrides_builtin(spec, shadowed))
    return out


# ─────────────────────── 检查 1：说明字段过长 ───────────────────────


def _check_description_length(spec: SkillSpec) -> list[SkillAdvice]:
    """
    说明字段超过 `DESCRIPTION_MAX_CHARS` 时给建议（F2-1）。

    ⚠️ **建议里必须说明「挪进 when_to_use 省不下清单预算」**。
    直觉上会以为把长说明拆一半到 `when_to_use` 能减轻清单负担，
    实际上 `render_index` 把两者拼成**同一行**、共享同一份预算
    （见 `render.render_index`），挪动只省下四个字符的分隔符。

    真正受益的是**状态报告的列表**与 `/help` 菜单——那两处只显示说明字段。
    不写清这一点，用户按建议改完会发现「预算没变」，然后不再信任后面的建议。
    """
    length = len(spec.description)
    if length <= DESCRIPTION_MAX_CHARS:
        return []
    return [
        SkillAdvice(
            kind=AdviceKind.DESCRIPTION_TOO_LONG,
            skill=spec.command_name,
            finding=f"description 有 {length} 个字符，超过建议上限 {DESCRIPTION_MAX_CHARS}",
            suggestion=(
                f"压到 {DESCRIPTION_MAX_CHARS} 字符以内，只说「这个 Skill 做什么」。"
                f"若超出部分是在描述「什么时候该用」，挪进 when_to_use。"
                f"注意这**不会省下第一阶段清单的预算**（两个字段在清单里拼成同一行、"
                f"共享同一份预算），但能让 /skills 列表与 /help 菜单恢复单行。"
            ),
        )
    ]


# ────────────────── 检查 2：有触发说明却没写说明字段 ──────────────────


def _check_missing_description(spec: SkillSpec) -> list[SkillAdvice]:
    """
    声明了 `when_to_use` 却没显式写 `description` 时给建议（F2-2）。

    ⚠️ 判的是 **`description_explicit`（作者写没写）**，不是「description 是不是空的」
    ——它经解析后**永不为空**（未写时由正文第一段回填），按空判永远命不中。

    ⚠️ **刻意不查「说明字段是自动提取的」这件事本身**：一份只有正文、
    没有 frontmatter 的 Markdown 也是合法 Skill，那是对齐改造刻意支持的用法。
    对它一直报建议等于用体检反对系统自己支持的写法，且几乎每个简易 Skill
    都会命中，会把真正要紧的几条淹掉。

    所以本项只在「作者显然知道有 frontmatter（写了 when_to_use）却漏了最关键
    那一项」时才报——那是**笔误**，不是用法选择。
    """
    if not spec.when_to_use or spec.description_explicit:
        return []
    return [
        SkillAdvice(
            kind=AdviceKind.MISSING_DESCRIPTION,
            skill=spec.command_name,
            finding=(
                "写了 when_to_use 却没写 description"
                "（当前这句说明是从正文第一段自动提取的）"
            ),
            suggestion=(
                "在 frontmatter 里补一行 description，一句话说清「这个 Skill 做什么」。"
                "模型在第一阶段清单里只看得到 description 与 when_to_use 这两句，"
                "而正文第一段通常是流程的开场白，不足以让它判断要不要加载。"
            ),
        )
    ]


# ─────────────────────── 检查 3：正文没有占位符 ───────────────────────


def _check_placeholder(spec: SkillSpec) -> list[SkillAdvice]:
    """
    正文不含参数占位符时给建议（F2-3）。

    ⚠️ **建议里必须写明「参数不会丢」**。没有占位符时 `render.substitute` 会把
    参数追加到正文末尾的「用户补充参数」段——它**没有被丢弃**，只是位置固定在最后。

    不写这一点的话，用户会把「位置不对」误读成「功能失效」，
    然后去排查一个根本不存在的 bug。实测中作者常因此以为「参数没生效」。
    """
    if PLACEHOLDER in spec.body:
        return []
    return [
        SkillAdvice(
            kind=AdviceKind.NO_PLACEHOLDER,
            skill=spec.command_name,
            finding=f"正文里没有 {PLACEHOLDER} 占位符",
            suggestion=(
                f"若这个 Skill 要接收参数，就在正文中你希望参数出现的那一步写上 "
                f"{PLACEHOLDER}。**参数不会丢**——没有占位符时它会被追加到正文末尾的"
                f"「用户补充参数」段，只是位置固定在最后，模型可能读到得太晚。"
                f"不需要参数的 Skill 可以忽略本条。"
            ),
        )
    ]


# ──────────────── 检查 4 与 5：预授权（共用一次翻译） ────────────────


def _is_pure_wildcard(pattern: str) -> bool:
    """
    判断一个参数模式是不是「匹配一切」。

    :param pattern: 规则里的括号内容
    :returns: 该模式等价于不写模式时为真

    认两种形态：裸的 `*`，以及域名规则的 `domain:*`。

    为什么要单独判它：`Bash(*)` 与裸写 `Bash` **效果逐字相同**
    （`permission/matching.py` 实测），但前者「带了模式」，
    朴素的「无模式即过宽」判定会把它整个放过。

    而这类**更该报**——裸写是「我知道我全放开了」，
    纯通配是「我以为我收窄了」。
    """
    text = pattern.strip()
    if text.startswith(DOMAIN_PREFIX):
        text = text[len(DOMAIN_PREFIX) :].strip()
    return text == "*"


def _is_broad(rule: Rule) -> bool:
    """
    判断一条预授权规则是不是「过宽」。

    :param rule: `grants_for` 翻译出的规则
    :returns: 过宽为真

    ## 为什么判翻译后的规则而不是原始声明串

    原始串的写法太多——YAML 列表、空格/逗号分隔串、大小写差异、
    本系统的内部工具名别名（`read_file` → `Read`）。逐一处理迟早漏一种；
    翻译后的规则只有一种形态。

    ## ⚠️ 远端（MCP）工具必须另判

    权限引擎对 MCP 走 `other` 分支，命中条件是
    **`rule.pattern == ""`** 再对工具名做 fnmatch（见 `permission/rules.py`）。
    也就是说 MCP 规则**必须无参数模式才可能生效**。

    若套用下面那支的判定，会有两个后果：

    1. **每一条** MCP 预授权都被判成过宽（它们按设计就必须无模式）；
    2. 更糟——建议给出的改法「加个参数模式」会让那条规则**永远匹配不到任何请求**，
       预授权无声消失，而「全军覆没」那条检查也不会报（规则本身仍解析成功）。

    第 2 条是**建议本身把用户的配置改坏**，比误报严重得多，且坏得很安静：
    用户只会发现「怎么又开始弹确认了」。

    而且方向是反的——`mcp__github__search` 是能写的**最精确**的声明，
    却会被判成最宽的。所以 MCP 改按「工具名里含不含通配符」判：
    `mcp__github__*` 才是真正的整台服务器放行。
    """
    if rule.tool.startswith(MCP_PREFIX):
        return any(ch in rule.tool for ch in _WILDCARD_CHARS)
    if rule.tool in READ_ONLY_GRANT_TOOLS:
        # 只读类别没有副作用，全放开也无所谓。
        return False
    return rule.pattern == "" or _is_pure_wildcard(rule.pattern)


def _check_grants(spec: SkillSpec) -> list[SkillAdvice]:
    """
    预授权的两项检查（F2-4 过宽、F2-5 全军覆没），**共用一次翻译**。

    两项都建立在「声明翻译成了什么规则」之上，各调一次 `grants_for`
    只会做两遍同样的解析。
    """
    if not spec.granted_tools:
        return []

    rules, _warnings = grants_for([spec])
    out: list[SkillAdvice] = []

    # ── 检查 5：一条都没翻译出来 ──
    #
    # 单条认不出已经由 grants_for 产出警告了，这里报的是**整体误解**：
    # 一条都没生效，说明作者对这个字段的写法有根本性的误会，
    # 而他看到的现象是「我明明写了预授权，还是每次弹确认」。
    if not rules:
        return [
            SkillAdvice(
                kind=AdviceKind.GRANTS_ALL_DROPPED,
                skill=spec.command_name,
                finding=(
                    f"allowed-tools 声明了 {len(spec.granted_tools)} 项，"
                    f"但**一条都没能生效**"
                ),
                suggestion=(
                    "检查工具名与写法。可用类别：Read / Write / Edit / Bash / WebFetch，"
                    "或 mcp__ 开头的远端工具。带模式的写法形如 `Bash(git status *)`；"
                    "域名规则**必须**带前缀，写成 `WebFetch(domain:github.com)`——"
                    "漏掉 `domain:` 的那条会被静默丢弃。"
                ),
            )
        ]

    # ── 检查 4：过宽 ──
    for rule in rules:
        if not _is_broad(rule):
            continue
        shown = f"{rule.tool}({rule.pattern})" if rule.pattern else rule.tool
        if rule.tool.startswith(MCP_PREFIX):
            suggestion = (
                "写成具体的工具名（如 `mcp__server__某个工具`）而不是通配。"
                "⚠️ 远端工具**不要加参数模式**——权限引擎要求这类规则的括号必须为空，"
                "加了模式反而会让这条预授权永远匹配不上。"
            )
        else:
            # ⚠️ 最后那句「换成别的类别不算收窄」是**实测补上的**：
            # 真实模型读到「收窄」二字后，把裸写的 `Write` 改成了裸写的 `Edit`，
            # 并在汇总里自评「✅ 改已有文件才免确认」——它以为换了个更窄的类别
            # 就算收窄了，而裸写 `Edit` 仍然是「全部编辑免确认」，
            # 于是同一条建议换个工具名又冒出来。
            #
            # 收窄的**唯一**手段是括号里的参数模式，这一点必须写在建议里，
            # 不能指望读的人自己想到。
            suggestion = (
                f"加一个**具体**的参数模式收窄，形如 `{rule.tool}(具体前缀 *)`。"
                f"只预授权这个 Skill 必然要做的那几件事，其余照常弹确认面板。"
                f"⚠️ **换成另一个工具类别不算收窄**——裸写 `Edit` 或裸写 `Write` "
                f"同样是「该类别全部操作免确认」；真正收窄的是括号里的模式。"
            )
        out.append(
            SkillAdvice(
                kind=AdviceKind.BROAD_GRANT,
                skill=spec.command_name,
                finding=(
                    f"预授权 `{shown}` 等于该类别的**全部**操作在本次执行内免确认"
                ),
                suggestion=suggestion,
            )
        )
    return out


# ─────────────────── 检查 6：逼近/超过注入上限 ───────────────────


def _check_injection_size(spec: SkillSpec) -> list[SkillAdvice]:
    """
    渲染后的注入段逼近或超过单个 Skill 的注入上限时给建议（F2-6）。

    ⚠️ **量的是渲染后的整段**（含边界标识、说明行、随附资源清单），
    不是正文原文——截断作用在前者（见 `render.render_active_body`），
    按正文原文判会漏报：一份正文刚好卡在阈值下、但带了一长串随附资源清单的
    目录型 Skill，实际注入时照样被截断。

    ⚠️ **传空参数**是刻意的偏松选择。含占位符的定义在真实激活时会被替换成
    实际参数，量出来会比这里略大。阈值本身留了 20% 余量，宁可少报一条，
    也不要为一个正常大小的 Skill 报警。

    传空串还有个好处：`render.substitute` 在参数为空时**不会**追加
    「用户补充参数」段，量到的就是正文本身的体积。
    """
    text, degrade = render_active_body(spec, "")
    lines = len(text.splitlines())
    size = len(text.encode("utf-8"))

    if degrade is not None:
        # 已经被截断了——比「逼近」更该报，措辞也要区分开。
        finding = (
            f"注入段已超过上限（{lines} 行 / {size} 字节，"
            f"上限 {BODY_MAX_LINES} 行 / {BODY_MAX_BYTES} 字节），"
            f"**激活时后半段会被切掉**"
        )
    elif lines >= BODY_MAX_LINES * NEAR_LIMIT_RATIO or size >= BODY_MAX_BYTES * NEAR_LIMIT_RATIO:
        finding = (
            f"注入段已达 {lines} 行 / {size} 字节，"
            f"逼近上限（{BODY_MAX_LINES} 行 / {BODY_MAX_BYTES} 字节）"
        )
    else:
        return []

    return [
        SkillAdvice(
            kind=AdviceKind.NEAR_INJECTION_LIMIT,
            skill=spec.command_name,
            finding=finding,
            suggestion=(
                "把正文拆成几个更聚焦的 Skill，或者改成目录型、"
                "把参考资料与模板挪进随附资源——那些不占注入预算，"
                "模型需要时会自己去读。"
                "超过上限时后半段会被静默切掉，而模型不知道自己只拿到了一半，"
                "会当成完整流程执行。"
            ),
        )
    ]


# ─────────────────── 检查 7：覆盖了内置样板 ───────────────────


def _check_overrides_builtin(
    spec: SkillSpec,
    shadowed: Sequence[tuple[str, SkillSource]],
) -> list[SkillAdvice]:
    """
    非内置层的 Skill 盖掉了同名内置样板时给建议（F2-7）。

    ⚠️ **措辞必须中性**：跨层覆盖是三层设计的**正常用法**（项目定制版盖掉内置
    样板），这条命中的多数情况没有任何问题。所以建议里明确写「若是有意定制请忽略」。

    报它是为了另一半情况——**无意撞名**。用户随手建了个同名文件，
    内置样板凭空消失、对应命令行为完全变了，而现场唯一的线索只是状态列表里
    一个来源标签。用户几乎不可能把「这命令怎么不好使了」联想到几天前建的那个文件。

    ⚠️ **收窄到只报覆盖内置层**：用户级的 Skill 是用户自己放进去的，
    撞掉了他心里有数；内置样板是他从没主动装过、却确实存在的东西，
    最容易被无意撞掉。
    """
    if (spec.command_name, SkillSource.BUILTIN) not in shadowed:
        return []
    return [
        SkillAdvice(
            kind=AdviceKind.OVERRIDES_BUILTIN,
            skill=spec.command_name,
            finding=(
                f"这个 Skill 盖掉了同名的**内置样板**——内置那份的每个字段都不再生效"
                f"（覆盖是整份替换，不做字段合并）"
            ),
            suggestion=(
                "若这是有意定制，忽略本条。"
                "若并非有意，把这个文件改个名——否则内置样板对应的命令行为已经"
                "完全变成你这份了，而界面上唯一的线索只是来源标签。"
            ),
        )
    ]
