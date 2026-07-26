"""
Skill 系统的核心模型（c11 T4/T5）：枚举、不可变数据类与模块级常量。

本模块处于 skills 包依赖链的**最底层**，只依赖标准库与 `tools/policy.py`：

    models.py      ← 本模块（数据结构与常量）
       ↑
    parser.py      单份 Skill 文本 → SkillSpec / 失败原因（纯函数）
       ↑
    discovery.py   三层目录扫描、层内去重、跨层覆盖
       ↑
    render.py      清单 / 激活正文 / 参数替换 / 自包含文本（纯函数）
       ↑
    validation.py  白名单两段校验与空集降级（纯函数）
       ↑
    manager.py     SkillManager：唯一持有可变状态与副作用编排

严格单向，下层不感知上层。本模块不导入 Textual、Provider SDK、commands 包，
可在无终端无网络的测试进程中独立使用（spec N1）。

对应 spec 条款：F1（frontmatter 字段）、F2（目录型入口名）、F3（三级存放）、
F4（名字规则与保留词）、F6/F9（注入上限）、F13（资源清单上限）。
"""

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


# ────────────────────────────── 枚举 ──────────────────────────────


class SkillMode(Enum):
    """
    执行模式（spec F18/F19）。

    - SHARED：共享当前对话。SOP 正文注入主对话上下文，模型在主历史里直接执行，
      过程中的工具调用与结果全部留在主历史。
    - ISOLATED：开一条独立子对话跑完，只把最终结论作为一条 assistant 消息回流主历史，
      子对话的中间过程不污染主历史。
    """

    SHARED = "shared"
    ISOLATED = "isolated"


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

    - ACTIVATED：共享模式激活成功（含幂等的重复激活）。
    - NOT_FOUND：名字不存在，需把可用名字列表回灌给模型。
    - ISOLATED：目标是独立模式 Skill。模型**不能**自行发起独立模式
      （它会开一条新的子对话，必须由用户显式触发），因此这里视为一种失败，
      并给出用户实际可执行的入口提示。
    """

    ACTIVATED = "activated"
    NOT_FOUND = "not_found"
    ISOLATED = "isolated"


# ────────────────────────────── 常量 ──────────────────────────────

# Skill 名字规则（spec F4）：小写字母开头，其后可跟小写字母/数字/连字符，总长 1–32。
# 之所以卡这么死：名字要直接拼成斜杠短命令 `/<name>`，必须与 C10 的命令字段口径兼容
# （不含空白、大小写不敏感解析下无歧义）。
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# `/skills` 的子命令词（spec F4）。Skill 不得叫这些名字，否则 `/skills run` 之类的
# 子命令解析会与「名叫 run 的 Skill」产生二义。注意这与「短命令 `/run` 是否冲突」无关，
# 冲突检查是另一条路径（F25），这里挡的是 `/skills <子命令>` 的解析歧义。
RESERVED_SUBCOMMANDS = frozenset({"reload", "off", "prompt", "run"})

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

    :param name: frontmatter 的 `name`，已通过 F4 的格式与保留词校验
    :param description: 一句话说明。它是第一阶段清单里模型唯一能看到的信息，
                        决定模型会不会想到去加载这个 Skill
    :param body: SOP 正文原文（**未做参数替换**，替换发生在渲染时）
    :param mode: 共享 / 独立
    :param allowed_tools: 可见工具白名单。
                          **None = 未声明白名单（不收窄工具集）**；
                          **空 tuple = 声明了但被剔空**（F17 降级的输入）。
                          两者语义完全不同，**不可合并**——前者是用户没打算限制，
                          后者是用户想限制但白名单项全部失效，需要产出警告并降级。
    :param history_messages: 独立模式带入子对话的主历史条数，>= 0；共享模式下无意义
    :param model: 指定模型，**仅独立模式生效**（共享模式共用主对话的 Provider，
                  换模型无从谈起）。共享模式声明它不算错误，只产出一条警告（F22）
    :param source: 来源层级，决定同名覆盖的胜负，并在 `/skills` 中展示
    :param entry_path: 单文件型 = 该 `.md` 文件；目录型 = 其中的 `SKILL.md`。
                       用于错误提示定位与 `/skills` 展示
    :param resource_dir: **仅目录型非空**，指向 Skill 目录本身（F13）
    :param resource_files: 目录型的随附文件相对路径，按字典序、上限 RESOURCE_LIST_MAX
    """

    name: str
    description: str
    body: str
    mode: SkillMode
    allowed_tools: Optional[tuple[str, ...]]
    history_messages: int
    model: Optional[str]
    source: SkillSource
    entry_path: Path
    resource_dir: Optional[Path]
    resource_files: tuple[str, ...]


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
    """

    skills: tuple[SkillSpec, ...]
    errors: tuple[SkillLoadError, ...]
    warnings: tuple[str, ...]


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

    :param name: 不带斜杠的 Skill 名
    :param description: 一句话说明（进 `/help` 与补全菜单）
    :param mode: 供命令层在需要时区分两种执行路径
    """

    name: str
    description: str
    mode: SkillMode


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
    :param dropped_fatal: 白名单含不存在的内置工具名、被本次热更新丢弃的。
                          注意与启动时不同：启动时这是**致命错误直接退出**，
                          热更新时只丢弃并警告（详见 manager.reload 的注释）
    :param warnings: 全部警告，含「下次启动会失败」这条关键提示
    :param errors: 本次扫描的加载失败记录
    """

    added: tuple[str, ...]
    removed: tuple[str, ...]
    auto_deactivated: tuple[str, ...]
    dropped_fatal: tuple[str, ...]
    warnings: tuple[str, ...]
    errors: tuple[SkillLoadError, ...]
