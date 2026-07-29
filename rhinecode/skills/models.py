"""
Skill 系统的核心模型：枚举、不可变数据类与模块级常量。

本模块处于 skills 包依赖链的**最底层**，只依赖标准库：

    models.py      ← 本模块（数据结构与常量）
       ↑
    parser.py      单份 Skill 文本 → SkillSpec / 失败原因（纯函数）
       ↑
    discovery.py   三层目录扫描、命令名推导、跨层覆盖
       ↑
    render.py      清单 / 激活正文 / 参数替换 / 自包含文本（纯函数）
       ↑
    validation.py  预授权声明 → 权限规则（纯函数）
       ↑
    manager.py     SkillManager：唯一持有可变状态与副作用编排

严格单向，下层不感知上层。本模块不导入 Textual、Provider SDK、commands 包，
可在无终端无网络的测试进程中独立使用。

## 本轮改造（对齐 Agent Skills 开放标准）

字段表按标准重定义，三处**语义变更**需要格外留意：

1. **`allowed-tools` 从「收窄可见工具集」改为「本次执行内免确认」**——语义相反。
   这是本次立项的直接动因：一份外部 Skill 写 `allowed-tools: Read Grep`，
   作者本意是「这两个别烦我确认」，旧实现却当成「只有这两个能用」。
2. **命令名来自文件系统路径，不再来自 `name` 字段**（见 `SkillSpec.command_name`）。
3. **「在哪执行」与「谁能触发」拆成正交两维**——旧的 `mode: isolated` 同时表达了
   两件事，现在分别是 `forked` 与 `model_invocable`。

对应 spec 条款见 `docs/c11/align/spec.md`。
"""


from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


# ────────────────────────────── 枚举 ──────────────────────────────


class SkillSource(Enum):
    """
    Skill 的来源层级（spec F3）。

    **成员定义顺序即优先级顺序**（PROJECT 最高、BUILTIN 最低）：
    `discovery.discover()` 直接按本枚举的迭代顺序逐层扫描，先扫到的同名 Skill 生效，
    低优先层的同名条目被静默丢弃。这样就不必另外维护一张优先级表——
    **调整优先级只需调整这里的成员顺序，但改动前务必确认 discovery 的注释仍成立**。
    """

    PROJECT = "project"   # <项目根>/.rhinecode/skills/
    USER = "user"         # ~/.rhinecode/skills/
    BUILTIN = "builtin"   # 随包分发


class DegradeKind(Enum):
    """
    注入降级的两种形态（spec F9）。

    两者严重程度差一个量级，**必须能被用户区分**，否则 F9 要求的
    「截断对用户可见」形同虚设：

    - TRUNCATED：单个 Skill 正文超过 BODY_MAX_*，后半段被切掉。
      模型仍会执行前半段流程，结果是「做了一半」。
    - DROPPED：累加超过 TOTAL_MAX_*，整段**完全没有注入**。
      模型根本不知道这个 Skill 存在，结果是「完全没生效」。

    所以不能用一个 bool 表示，`/skills` 报告与激活警告要按此分别措辞。
    """

    TRUNCATED = "truncated"
    DROPPED = "dropped"


class ActivationStatus(Enum):
    """
    一次激活尝试的结果状态（spec F7）。

    - ACTIVATED：正文已注入动态槽位（含幂等的重复激活）。
    - NOT_FOUND：名字不存在，需把可用名字列表回灌给模型。
    - FORKED：目标声明了 `context: fork`，**不能**用注入正文的方式处理——
      它要开一条子对话完整跑完。调用方据此改走子对话路径。
      注意这**不是失败**（对齐改造 F8：模型可以自行发起 fork Skill）。
    - NOT_MODEL_INVOCABLE：目标声明了 `disable-model-invocation`，
      模型不得自行发起。这才是失败，需给出用户实际可执行的入口提示。
    """

    ACTIVATED = "activated"
    NOT_FOUND = "not_found"
    FORKED = "forked"
    NOT_MODEL_INVOCABLE = "not_model_invocable"


class AdviceKind(Enum):
    """
    体检的七项检查各一个成员（作者期扩展 F2）。

    ## 为什么要枚举，而不是只留一段建议文本

    测试要能断言「这次命中的是哪一条」。只有文本的话，用例只能写
    `assertIn("过长", report)`——而**措辞恰恰是本扩展要反复打磨的东西**
    （F1 要求建议必须可操作，措辞不好就得改）。改一次措辞碎一批测试，
    人的第一反应会是把断言放宽成谁都能过，护栏于是名存实亡。

    有了枚举，措辞怎么改都不影响判定类的断言，两件事各归各的。

    ## ⚠️ 成对维护点

    新增一项检查 → **本枚举** + `audit.py` 的判定与措辞。漏了枚举不报错，
    只是那条新检查在测试里没法精确断言。
    """

    # 说明字段超过 DESCRIPTION_MAX_CHARS
    DESCRIPTION_TOO_LONG = "description_too_long"
    # 声明了 when_to_use 却没显式写 description
    MISSING_DESCRIPTION = "missing_description"
    # 正文里没有 $ARGUMENTS
    NO_PLACEHOLDER = "no_placeholder"
    # 预授权给得过宽（有副作用类别无模式/纯通配；MCP 名字带通配符）
    BROAD_GRANT = "broad_grant"
    # allowed-tools 有声明，但一条都没能翻译成规则
    GRANTS_ALL_DROPPED = "grants_all_dropped"
    # 渲染后的注入段逼近或超过单个 Skill 的注入上限
    NEAR_INJECTION_LIMIT = "near_injection_limit"
    # 非内置层的 Skill 覆盖掉了同名内置样板
    OVERRIDES_BUILTIN = "overrides_builtin"


# ────────────────────────────── 常量 ──────────────────────────────

# `/skills` 的子命令词。命令名不得取这些值，否则 `/skills run` 之类的子命令解析
# 会与「命令名叫 run 的 Skill」产生二义。注意这与「短命令 `/run` 是否与内置命令冲突」
# 无关，那是另一条路径，这里挡的是 `/skills <子命令>` 的解析歧义。
#
# ⚠️ 名字的**字符集与长度校验已整段删除**（对齐改造 F4）：命令名现在来自文件系统，
# 文件系统已经保证它是合法文件名；再叠一层自定义规则，只会让本可直接使用的外部
# Skill 目录（含大写、下划线、超长名）被无理由拒绝，而那正是本次改造要消灭的摩擦。
RESERVED_SUBCOMMANDS = frozenset({"reload", "off", "prompt", "run"})

# 键名归一（对齐改造 F1）：标准用连字符，而 YAML 使用者习惯下划线，两种都要认。
# 归一方向是「连字符 → 下划线」，因为下划线形态才能做 Python 标识符与字段名。
HYPHEN_KEYS = ("allowed-tools", "when-to-use", "disable-model-invocation", "user-invocable")

# 无对应能力的标准字段（对齐改造 F10）：名字 → 本版本的实际行为。
# **必须逐条告知而不是静默忽略**——作者写 `background: true` 的预期是「后台跑、
# 不阻塞」，实际却同步阻塞跑完；这个差异用户不知道就会误判 Skill 的行为。
UNSUPPORTED_FIELDS: dict[str, str] = {
    "background": "本版本不支持后台执行，将同步等待子对话跑完",
    "agent": "本版本没有子代理类型的概念，该声明被忽略",
    "effort": "本版本的思考强度由 /think 全局控制，该声明被忽略",
    "hooks": "本版本不支持 Skill 级钩子，该声明被忽略",
    "paths": "本版本不支持按路径自动激活，该声明被忽略",
    "shell": "本版本的命令执行走系统默认 shell，该声明被忽略",
}

# 目录型 Skill 的入口文件名（spec F2）。一个目录含此文件即视为目录型 Skill，
# 目录内其余文件是随附资源（模板/示例/脚本/参考文档）。
ENTRY_FILENAME = "SKILL.md"

# 第二阶段加载工具的名字（spec F7/F8）。它是系统级工具，不受任何 Skill 白名单约束。
LOAD_SKILL_TOOL = "load_skill"

# SOP 正文中的参数占位符（spec F12）。用户传入的参数原样替换到这里，
# 不做 shell 分词、不做任何模板求值。
PLACEHOLDER = "$ARGUMENTS"

# 第一阶段清单的注入上限（spec F6）。清单进的是**稳定通道**（会被前缀缓存），
# 只含名字与一句话说明，200 行 / 25KB 足够容纳上百个 Skill。
INDEX_MAX_LINES, INDEX_MAX_BYTES = 200, 25 * 1024

# 单个已激活 Skill 正文的注入上限（spec F9）。超出则切掉后半段并标记 TRUNCATED。
BODY_MAX_LINES, BODY_MAX_BYTES = 300, 12 * 1024

# 全部已激活 Skill 正文合计的注入上限（spec F9）。超出则当前段整段丢弃并标记 DROPPED。
TOTAL_MAX_LINES, TOTAL_MAX_BYTES = 600, 24 * 1024

# 目录型 Skill 随附资源清单的条数上限（spec F13）。资源目录可能很大，
# 清单只是给模型一个「有哪些文件可读」的索引，不必也不该全量列出。
RESOURCE_LIST_MAX = 50

# MCP 工具名的判别前缀（spec F16），**双下划线**。
# 白名单校验用它区分两类名字：`mcp__` 开头的走「运行期剪枝」（Server 可能没连上，
# 不算致命），其余一律走「启动 fail-fast」（内置工具名写错就是笔误）。
# 注意内置工具 `mcp_add_server` / `mcp_resolve_server` 是**单**下划线，不匹配本前缀，
# 因此会被当作普通内置工具做严格校验——这是有意为之。
MCP_PREFIX = "mcp__"

# 独立模式子对话的迭代上限（spec N6）。子任务应当聚焦，给它一份独立且更小的预算，
# 避免一个跑偏的 Skill 把主对话的 25 轮额度也一并耗光。
SKILL_MAX_ITERATIONS = 15

# ─────────────────── 体检阈值（作者期扩展 F2） ───────────────────

# 说明字段的建议上限（F2-1）。**按字符数而不是字节数**——按字节算的话，
# 一句中文说明会比同样信息量的英文早三倍命中，而这个阈值的目的是
# 「别把状态列表和命令菜单撑成多行」，那是按显示宽度算的，与编码无关。
#
# 注意它**不是**硬限制：超了照常加载、照常可用，只是给一条建议。
DESCRIPTION_MAX_CHARS = 100

# 「逼近注入上限」的比例（F2-6）。达到 BODY_MAX_* 的这个比例就提醒，
# 留出的余量让作者有机会在真被截断之前拆分。
NEAR_LIMIT_RATIO = 0.8

# 判定「预授权是否过宽」时的**只读白名单**（F2-4）。
#
# ⚠️ 方向是刻意选的：**维护「只读」清单，不在其中的一律视为有副作用**。
# 反过来维护一份「有副作用清单」的话，将来新增一个有副作用的工具、
# 忘了往里登记，就会**静默漏报**——没人会发现。
# 现在这个方向下，忘了登记只会**多报一条建议**：用户看得见、会来问，
# 且不造成任何实际损害。**让遗漏偏向可见的一侧。**
#
# 现在只有一个成员，是因为 `validation._TOOL_ALIASES` 把标准里的
# `Glob` / `Grep` 也都映射到了 `Read`——本系统的只读检索就这一个类别。
#
# ⚠️ 成对维护点：新增一个**只读**工具类别 → `validation.py` 的 `_TOOL_ALIASES`
# + 本集合。漏改的后果是「多报一条预授权过宽」（见上，是刻意选的偏严方向）。
READ_ONLY_GRANT_TOOLS = frozenset({"Read"})


def builtin_skills_dir() -> Path:
    """
    返回随包分发的内置 Skill 目录（spec F3 的第三层）。

    :returns: `<本模块所在目录>/builtin` 的绝对路径。目录**可能不存在**
              （例如打包遗漏），调用方 `discovery._scan_layer` 会把
              「目录不存在」当作空层处理，不报错。

    为什么用 `Path(__file__).parent` 而不是 `importlib.resources`：
    本项目以常规目录形式安装（不是 zip import），该写法在开发模式
    （`pip install -e .`）与真实安装下都成立，且返回真实文件系统路径，
    可以直接被 `iterdir()` / `read_text()` 使用，也可以注册进 path_guard
    的只读白名单。用 `importlib.resources` 反而要处理 Traversable 抽象。

    **前提**：`pyproject.toml` 的 package-data 必须把 `skills/builtin/*.md`
    打进分发包，否则开发模式能跑、真实安装后三个内置样板会凭空消失。

    副作用：无（不创建目录、不读文件）。
    """
    return Path(__file__).resolve().parent / "builtin"


# ──────────────────────────── 数据类 ────────────────────────────


@dataclass(frozen=True)
class SkillSpec:
    """
    一个已成功加载的 Skill（spec F1）。

    由 `parser.parse_skill` 产出，此后在整个系统里只读传递。

    :param command_name: **由发现层从文件系统路径推导**（目录型取目录名，单文件型取
                         去扩展名的文件名），不来自 frontmatter。它是斜杠命令名、
                         跨层覆盖的判定键、以及模型加载时使用的标识。

                         这条是「外部 Skill 原样可用」的基础：从任何来源拉一个目录
                         丢进去，命令名就是目录名，不必检查也不必修改 frontmatter。
    :param display_name: frontmatter 的 `name`，**仅作展示标签**；缺省回填为 `command_name`
    :param description: 一句话说明。它与 `when_to_use` 一起构成第一阶段清单里模型
                        唯一能看到的信息，决定模型会不会想到去加载这个 Skill。
                        frontmatter 未声明时由解析层从正文第一个非空段落提取
    :param when_to_use: 触发说明，拼接在 `description` 之后进清单。把「这个 Skill 做什么」
                        与「什么时候该用它」拆开写，避免 description 一个字段扛两个职责
                        （实测中它会因此被写成两百多字符）
    :param body: SOP 正文原文（**未做参数替换**，替换发生在渲染时）
    :param granted_tools: `allowed-tools` 的原始声明串，**预授权语义**——
                          列出的操作在本次执行内免于人工确认，**不限制**模型能调用什么。

                          ⚠️ 与 C11 的 `allowed_tools`（收窄可见工具集）**语义相反**。
                          用空元组而非 None 表示「未声明」：预授权没有「未声明 = 不收窄」
                          那种三态，空就是不授权，一种含义一个取值。
    :param forked: `context: fork` —— 开子对话执行、只回流结论。
                   **它不再隐含「只能由用户触发」**，那由 `model_invocable` 单独表达
    :param model_invocable: 模型是否可自行发起（`disable-model-invocation` 的反面），缺省真
    :param user_invocable: 是否注册斜杠短命令、进补全菜单，缺省真
    :param model: 指定模型，**仅 `forked` 时生效**（留在主对话时共用主对话的 Provider，
                  换模型无从谈起）。非 fork 声明它不算错误，只产出一条 notice
    :param source: 来源层级，决定同名覆盖的胜负，并在 `/skills` 中展示
    :param entry_path: 单文件型 = 该 `.md` 文件；目录型 = 其中的 `SKILL.md`。
                       用于错误提示定位与 `/skills` 展示
    :param resource_dir: **仅目录型非空**，指向 Skill 目录本身
    :param resource_files: 目录型的随附文件相对路径，按字典序、上限 RESOURCE_LIST_MAX
    :param notices: 解析期产出的告知性提示（旧字段语义变更、无对应能力的标准字段）。
                    **不是加载失败**——这些 Skill 照常可用，只是有些声明的效果与作者
                    预期不同，必须让用户看见。随 spec 一路带到状态报告
    :param description_explicit: `description` 是**作者自己写的**（真），
                    还是由正文第一段自动回填的（假）。

                    **这不是 frontmatter 字段，是解析期的派生事实**，
                    存在的唯一理由是让体检判得出「声明了 when_to_use 却漏了
                    description」（作者期扩展 F2a）——`description` 经解析后
                    **永不为空**（未写时回填），按「是不是空的」判永远命不中。

                    体检也**不许**自己重新提取一遍正文第一段再比对：那既是启发式
                    （作者手写的说明恰好等于第一段时会误报），又等于把解析规则
                    复制成两份，日后必然分叉。

                    ⚠️ 默认取 `True` 而非 `False`：该默认只对**手工构造**的定义生效
                    （解析器两条路径都显式赋值）。取 `False` 会让任何一个带
                    `when_to_use`、却没设本字段的手工定义**误报**一条
                    「你忘了写说明」；取 `True` 最坏是漏报。
                    **建议系统里误报比漏报贵**——一条错的建议会让用户跑去改一个
                    本来没问题的地方。代价是「解析器忘了赋值」会变成静默漏报，
                    由 `tests/test_skill_parser.py` 钉住两条路径各自的取值。
    """

    command_name: str
    display_name: str
    description: str
    when_to_use: Optional[str]
    body: str
    granted_tools: tuple[str, ...]
    forked: bool
    model_invocable: bool
    user_invocable: bool
    model: Optional[str]
    source: SkillSource
    entry_path: Path
    resource_dir: Optional[Path]
    resource_files: tuple[str, ...]
    notices: tuple[str, ...] = ()
    description_explicit: bool = True


@dataclass(frozen=True)
class SkillAdvice:
    """
    一条体检建议（作者期扩展 F1）——**第四类反馈**。

    既有三类各司其职，但都不回答「那我该怎么改」：

    | 类别 | 它说的是 |
    |---|---|
    | `SkillLoadError` | 这份文件我看见了但没能用上，原因是…… |
    | catalog 的 warnings | 这次运行过程中发生了什么 |
    | `SkillSpec.notices` | 你的某个声明与实际行为有出入 |
    | **本类** | **这份 Skill 能用，但有更好的写法，建议改成……** |

    ## 为什么拆成三段而不是一个 text 字段

    F1 要求每条建议必须同时说清「哪个 Skill / 发现了什么 / 建议怎么改」。
    拆成字段让这三样在**类型层面**被强制——写一条新建议时漏掉「怎么改」
    根本构造不出对象；合成一个字符串则全靠自觉，而「可操作」正是这一类反馈
    存在的**全部理由**：一条只说「预授权过宽」却不给出带模式写法的建议，
    与既有的警告没有区别，那本扩展就白做了。

    :param kind: 判定类别。供测试精确断言「命中的是哪一条」，
                 使措辞的调整不会碎掉一批用例
    :param skill: 命令名
    :param finding: 发现了什么（陈述现状）
    :param suggestion: 建议怎么改（**必须可操作**，能给出替代写法时必须给）
    """

    kind: AdviceKind
    skill: str
    finding: str
    suggestion: str


@dataclass(frozen=True)
class SkillLoadError:
    """
    单个 Skill 的加载失败记录（spec F5）。

    单文件解析失败**不阻断整体**——其余 Skill 照常可用，失败者记一条本结构，
    在 `/skills` 报告里原样展示，让用户知道「这个文件我看见了但没能用上，原因是……」。

    :param path: 出问题的文件路径（目录型缺入口时指向该目录），供用户直接定位
    :param source: 所属层级
    :param reason: 可读中文原因，直接展示给用户，不做二次加工
    """

    path: Path
    source: SkillSource
    reason: str


@dataclass(frozen=True)
class SkillCatalog:
    """
    一次完整扫描的产物：**不可变快照**。

    热更新（spec F26）的实现就是「造一个新 Catalog 整体替换旧的」——
    不可变快照使替换天然原子，不存在「新的一半旧的一半」的中间状态，
    也就不需要为读取方加读锁（读引用即可，引用本身的赋值是原子的）。

    :param skills: 已按 name 排序、跨层覆盖后每个名字唯一
    :param errors: 加载失败的记录，按发现顺序
    :param warnings: 非致命提示（如共享模式声明了 model，F22），按发现顺序
    :param shadowed: **被跨层覆盖掉的那些定义**，每个元素是
                     `(命令名, 被覆盖那份所在的层)`（作者期扩展 F2a）。

                     ⚠️ **必须在扫描时当场记**，因为那是这个信息**唯一还存在**
                     的时刻：`discover` 一旦把低优先层那份丢弃，就再没有任何引用，
                     成品清单 `skills` 里连它存在过的痕迹都没有——覆盖这件事
                     **无从推断**，只能在丢弃之前记下来。

                     用途是让体检报出「你覆盖了一个内置样板」。跨层覆盖本身是
                     **整份替换且完全静默**的正常用法（项目定制版盖掉内置样板），
                     所以它不记错误、不发警告；但**无意撞名**时后果很重——
                     内置样板凭空消失、对应命令行为完全变了，而现场唯一的线索
                     只是状态列表里一个来源标签。

                     **记全部层而不只记内置**：多记两个元组不花什么代价，
                     将来若要放宽到「项目级盖用户级也提示」，数据已经在了。
    """

    skills: tuple[SkillSpec, ...]
    errors: tuple[SkillLoadError, ...]
    warnings: tuple[str, ...]
    shadowed: tuple[tuple[str, SkillSource], ...] = ()


@dataclass(frozen=True)
class ActiveSkill:
    """
    一条激活记录（共享模式专用）。

    **只有两个字段，这是刻意的**：

    1. **正文不存在这里**。每轮渲染时按 name 从当前 catalog 现取，
       这样热更新改了 SOP 正文后，下一轮自动用新正文，无需同步两份状态（F27）。
    2. **降级标记不作为字段**。本类是 frozen，`degrade` 若是字段就无法在每轮
       `active_text()` 时写回（会抛 `FrozenInstanceError`），只能整列表
       `dataclasses.replace` 重建；而这个写操作又必须进锁，会与
       「临界区只做内存读写」的加锁纪律纠缠。因此降级信息由 `SkillManager`
       单独持有一个**纯派生**字典 `_degrades: dict[str, DegradeKind]`。

    :param name: Skill 名
    :param arguments: 最近一次激活传入的参数。重复激活同一个 Skill 时，
                      列表位置不动、只更新本字段（F30 幂等）
    """

    name: str
    arguments: str


@dataclass(frozen=True)
class SkillCommandInfo:
    """
    交给命令层构造斜杠短命令的**中立描述**。

    存在的唯一意义是**切断 `skills → commands` 的依赖**：
    skills 包只产出这个不认识 `CommandSpec` 的中立结构，
    由 `commands/skill_commands.py` 单向地转成真正的 `CommandSpec`。
    这样 skills 仍然是一个不被任何层反向依赖的叶子包。

    :param name: 不带斜杠的命令名（即 `SkillSpec.command_name`）
    :param description: 一句话说明（进 `/help` 与补全菜单）
    :param forked: 供命令层在需要时区分两种执行路径

    注意本结构**只承载 `user_invocable` 为真的 Skill**——为假的那些根本不该走到
    命令层（对齐改造 F8），过滤发生在管理器产出这批描述时，而不是命令层再判一次。
    """

    name: str
    description: str
    forked: bool


@dataclass(frozen=True)
class ActivationResult:
    """
    一次激活尝试的完整结果，服务三个消费者：
    `LoadSkillTool.execute` 的三态返回文案（F7）、短命令路径的失败提示、
    以及激活成功时的降级警告（F9）。

    :param status: 三态状态
    :param name: 尝试激活的名字（原样回显，便于文案拼接）
    :param available_names: 仅 NOT_FOUND 时非空——回灌给模型的可用名字列表，
                            让它下一轮能自我纠正而不是反复猜
    :param entry_hint: 仅 ISOLATED 时非空——用户**实际可执行**的入口。
                       短命令注册成功时是 `/<name>`，因重名未注册时是
                       `/skills run <name>`。**绝不能指向一个不存在的命令**
    :param degrade: 仅 ACTIVATED 时可能非空——本次注入是否降级及其形态
    """

    status: ActivationStatus
    name: str
    available_names: tuple[str, ...] = ()
    entry_hint: Optional[str] = None
    degrade: Optional[DegradeKind] = None


@dataclass(frozen=True)
class ReloadOutcome:
    """
    一次热更新（`/skills reload`）的结果，由 conversation 层渲染成可读报告。

    :param added: 新出现的 Skill 名
    :param removed: 消失的 Skill 名
    :param auto_deactivated: 原本已激活、但在新 catalog 中消失，因而被自动卸载的（F27）
    :param warnings: 全部警告
    :param errors: 本次扫描的加载失败记录
    """

    added: tuple[str, ...]
    removed: tuple[str, ...]
    auto_deactivated: tuple[str, ...]
    warnings: tuple[str, ...]
    errors: tuple[SkillLoadError, ...]
