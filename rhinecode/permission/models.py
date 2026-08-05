"""
权限系统的纯数据层：只定义枚举与 dataclass，不含任何行为逻辑。

设计目的（与 agent/events.py 同思路）：
- 让决策管线（engine）、规则求值（rules）、工具规范化（adapter）共享同一套轻量类型，
  彼此通过这些数据结构通信，避免互相依赖具体实现而形成耦合或循环引用。
- 所有结构都是「值对象」：构造后即可被纯函数读取、断言，便于单元测试（spec N5）。

这些类型贯穿五层决策管线（spec F1）：
    adapter 把一次工具调用规范化成 PermissionRequest →
    engine.decide(request) 跑四层 → 返回 DecisionResult（含 Decision 与命中 Layer）。
"""

from dataclasses import dataclass, field
from enum import Enum


class Decision(str, Enum):
    """
    单次权限判断的三种结局（spec F1）。

    继承 str 便于直接和字符串比较、打印调试（与 AgentEventType 同风格）。

    - ALLOW：放行，工具可执行
    - DENY：拒绝，不执行并把结构化原因回灌模型（不终止 Agent Loop，spec F8）
    - ASK：交人工确认，弹 HITL 面板由用户定夺（spec F6）
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionMode(str, Enum):
    """
    第④层「权限模式」三档（spec F5）。

    模式只决定「③规则未命中」时的兜底行为，翻不了①黑名单/②沙箱/③的 deny。

    - STRICT（严格）：灰色地带一律拒绝
    - DEFAULT（默认）：灰色地带交人工确认（启动默认档）
    - PERMISSIVE（放行）：灰色地带一律放行
    """

    STRICT = "strict"
    DEFAULT = "default"
    PERMISSIVE = "permissive"


class Layer(str, Enum):
    """
    决定是由决策管线的哪一层做出的，用于构造结构化原因与调试展示（spec F8）。

    - HOOK：⓪Hook 前置层（c12）。**排在①之前**，且只能收紧不能放宽——
      它只在「Hook 把 ALLOW 升级为 ASK」时作为层标出现（拦截走另一条路径，
      不产生 DecisionResult）
    - BLACKLIST：①危险命令黑名单
    - SANDBOX：②路径沙箱
    - NETWORK：②′网络边界（仅 URL 类；硬校验 + 域名策略，web_fetch 扩展 spec F5/F6）
    - RULE：③可配置规则（也含只读简化分支的放行）
    - MODE：④权限模式兜底

    ⚠ 新增枚举值时，`trace/reader.py` 的 `_LAYER_NAMES` 要跟着加一行——
    那两份表**刻意不合一**（合一要让只依赖标准库的 trace 叶子包反向依赖本包），
    一致性由 `tests/test_trace_reader.py` 里一条遍历本枚举的断言钉住。
    """

    HOOK = "hook"
    BLACKLIST = "blacklist"
    SANDBOX = "sandbox"
    NETWORK = "network"
    RULE = "rule"
    MODE = "mode"


@dataclass
class DecisionResult:
    """
    一次决策的完整结果，是 engine.decide() 的返回值。

    :param decision: 三选一结局（ALLOW/DENY/ASK）
    :param layer: 在第几层定的论（用于原因展示与调试）
    :param reason: 面向「模型/用户」的中文可读原因，例如
                   「命中危险命令黑名单：递归强删」或「命中 deny 规则 Bash(git push *)（来源：user）」
    :param kind: 本次请求的种类（透传自 PermissionRequest.kind）。**带默认值**，
                 既有构造点不必改也能编译。唯一消费者是确认面板——它据此决定要不要走
                 URL 类的专用展示（完整地址不截断 + 主机名 + 命中层）。
    :param host: 本次请求的主机名（仅 url 类非空，透传自 PermissionRequest.host）。同上。

    ⚠ **`engine.decide()` 的每一条 return 路径都必须显式填 `kind`（url 类另填 `host`）。**
    「带默认值所以既有构造点不动」只保证编译过、不保证功能对：确认面板只在判定为
    ASK 时弹，而对 url 请求 ASK 只可能来自④模式兜底——漏填的后果是 `kind` 恒为空串、
    面板的 URL 专用分支**永不进入**，而这在真机弹面板之前完全看不出来。
    """

    decision: Decision
    layer: Layer
    reason: str
    kind: str = ""
    host: str = ""


@dataclass(frozen=True)
class Rule:
    """
    一条解析后的权限规则（spec F4）。

    frozen=True 使其不可变且可哈希：规则一旦从配置解析出来就不应被改写，
    不可变也让它能安全地在会话级/文件级规则列表间传递、去重。

    :param effect: 规则效果，只有 "allow" 或 "deny" 两种
    :param tool: 规则体系内的工具名（Bash / Read / Edit / Write），由 adapter 的映射决定
    :param pattern: 括号内的匹配模式，如 "git *"；空串 "" 表示匹配该工具的所有调用
                    （对应配置里写 "Bash" 而非 "Bash(...)"）
    :param source: 规则来源层，便于在拒绝原因里告诉用户「这条限制来自哪层」并辅助调试。
                   取值：user / project / local / session
    """

    effect: str
    tool: str
    pattern: str
    source: str


@dataclass
class PermissionRequest:
    """
    规范化后的一次权限请求，是 engine.decide() 的输入。

    它是「引擎」与「具体工具」之间的解耦中间层：adapter 负责把五花八门的工具调用
    （不同工具参数名不同）统一翻译成本结构，引擎只认本结构、对具体工具一无所知，
    从而能脱离 TUI、脱离真实文件/命令被纯单测覆盖（spec N4/N5）。

    :param tool_name: 真实工具名（rhinecode 内部），如 "run_command"。仅用于原因展示
    :param rule_name: 规则体系工具名，如 "Bash"/"Read"/"Edit"/"Write"。规则匹配按它进行
    :param specifier: 待匹配的字符串：命令类是整条命令，路径/glob 类是路径或模式
    :param kind: 请求种类，决定走哪些层与用哪种匹配：
                 "command"（命令，进①黑名单 + ③命令匹配）、
                 "read_path"/"write_path"（文件路径，进②沙箱 + ③路径匹配）、
                 "glob"（glob 模式，进②沙箱 + ③路径匹配）、
                 "url"（网络地址，进②′网络边界；specifier 是**完整 URL 原文**，
                        主机名单独放在 host 字段）、
                 "other"（未映射工具，只按工具名进③ + ④兜底）
    :param is_read_only: 是否只读工具。True 走只读简化分支（spec F7）：③未命中即放行，不进④
    :param mode: 当前权限模式，供④层兜底使用
    :param host: **仅 url 类填充**的主机名，已归一化（小写、去末尾点），由 adapter 一次填好。
                 带默认值，其余种类留空串。

                 之所以单独立一个字段而不是让各处自己从 specifier 里解析：三处都要它——
                 规则匹配（`rules._rule_matches` 的 url 分支）、确认面板展示（spec F9）、
                 行为记录（spec F23）。若让规则层去调 `network.py` 的解析函数，
                 而 `network.py` 又要 import `rules.py` 拿 RuleSet，就成了真的循环导入。
    """

    tool_name: str
    rule_name: str
    specifier: str
    kind: str
    is_read_only: bool
    mode: PermissionMode
    host: str = ""
