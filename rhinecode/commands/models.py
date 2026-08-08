"""
命令层核心模型（c10 T1/T2）：枚举、不可变数据类与框架无关的控制接口。

本模块是 commands 包依赖方向的最底层：
- 只定义数据结构与 Protocol，不导入 Textual、ConversationManager、Provider SDK；
- parser / registry / dispatcher / builtins 都向上依赖本模块；
- TUI 层（RhineApp）实现 CommandController 协议，命令处理函数只面向该协议编程，
  从而让命令层可以在无终端、无网络的测试进程中独立验证（spec N8）。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Protocol


class CommandType(Enum):
    """
    命令执行类型（spec F15）：

    - LOCAL：纯本地查询/本地编排，绕过 Agent Loop（/help、/mcp、/context、/compact、/memory）。
      注意「本地」指不作为普通用户消息进入 Agent，不代表绝对不访问模型——
      /compact 仍保留既有的专用摘要 LLM 调用（spec 明确例外）。
    - UI：改变会话或界面状态，绕过 Agent（/think、/plan、/perm、/resume、/clear、/exit）。
    - PROMPT：把内置预设提示词作为用户请求交给正常 Agent 路径（/init）。
    """

    LOCAL = "local"
    UI = "ui"
    PROMPT = "prompt"


class InputKind(Enum):
    """输入分类（spec F4/F5/F7）：空输入 / 普通消息 / 斜杠命令。"""

    EMPTY = "empty"
    MESSAGE = "message"
    SLASH = "slash"


class DispatchKind(Enum):
    """
    分发结果类型（plan 4.4）：供测试、日志与提交回调做轻量判断，不承载渲染对象。

    - EMPTY：空输入，无任何副作用
    - MESSAGE：普通消息，已交给 Agent 对话路径
    - COMMAND：已知命令执行成功
    - UNKNOWN：未知斜杠命令，仅本地提示（绝不进入 AI，spec F8）
    - ERROR：命令执行失败或缺少必需参数（本地错误，不降级为普通消息，spec N5）
    """

    EMPTY = "empty"
    MESSAGE = "message"
    COMMAND = "command"
    UNKNOWN = "unknown"
    ERROR = "error"


class ModeTarget(Enum):
    """switch_mode 的目标模式：思考模式 / Plan Mode / 权限模式。"""

    THINKING = "thinking"
    PLAN = "plan"
    PERMISSION = "permission"


class ReportTarget(Enum):
    """
    query_report 的目标报告：MCP 连接状态 / 上下文用量 / 记忆系统状态 /
    Skill 状态 / Skill 实际注入内容 / Hook 规则 / 子 Agent 角色与任务 /
    共享任务清单（c15）。

    **成对维护点**：新增枚举值须同步三处——本枚举、`tui/app.py` 的
    `query_report` 分支（未知值明确抛错）、`conversation.py` 的对应领域方法。
    """

    MCP = "mcp"
    CONTEXT = "context"
    MEMORY = "memory"
    SKILLS = "skills"
    SKILLS_PROMPT = "skills_prompt"
    HOOKS = "hooks"
    AGENTS = "agents"
    TASKS = "tasks"


@dataclass(frozen=True)
class ParsedInput:
    """
    输入解析结果（parser 产物，plan 4.3）。

    :param kind: 输入分类（EMPTY / MESSAGE / SLASH）
    :param raw_text: 用户原始输入（未做任何改写）
    :param command_token: 仅 SLASH 时非 None——第一个空白字符前的命令字段，
                          保留用户输入的原始大小写（如 "/PLAN"）
    :param arguments: 命令参数——命令字段之后去除两端空白的剩余内容；
                      内部空白与大小写原样保留（spec F6/F11）
    """

    kind: InputKind
    raw_text: str
    command_token: Optional[str] = None
    arguments: str = ""


@dataclass(frozen=True)
class CommandSpec:
    """
    单条命令的完整登记信息（spec F1，单一事实来源）。

    :param name: 规范名，带 "/" 的完整标识（如 "/context"）；索引比较用 casefold()
    :param aliases: 别名元组，同样带 "/"（如 ("/ctx",)）；与规范名共用同一处理行为
    :param description: 简短描述（帮助与补全展示共用同一份文案，spec F3）
    :param usage: 用法示例（如 "/resume [编号或ID]"）
    :param command_type: 执行类型（LOCAL / UI / PROMPT）
    :param handler: 处理函数，签名 (CommandInvocation, CommandController) -> None
    :param argument_hint: 可选参数提示，仅用于补全菜单与帮助，不参与解析
    :param hidden: True 时规范名与全部别名不出现在帮助/补全/候选菜单，但仍可直接执行（spec F20）
    :param requires_argument: True 且参数为空时由分发器统一显示用法（spec F14）；
                              C10 全部内置命令为 False，为后续命令预留统一校验点
    """

    name: str
    aliases: tuple[str, ...]
    description: str
    usage: str
    command_type: CommandType
    handler: "CommandHandler"
    argument_hint: Optional[str] = None
    hidden: bool = False
    requires_argument: bool = False


@dataclass(frozen=True)
class CommandInvocation:
    """
    一次已命中命令的调用上下文（dispatcher 构造后交给处理函数）。

    :param raw_text: 用户原始输入（如 "/CTX  now"）
    :param typed_name: 用户实际输入的命令字段（保留大小写，如 "/CTX"）
    :param matched_name: 实际命中的规范名或别名（注册时的小写形式，如 "/ctx"）
    :param arguments: 参数字符串（两端空白已去、内部原样）
    :param spec: 命中的命令定义（spec.name 始终是规范名，如 "/context"）
    """

    raw_text: str
    typed_name: str
    matched_name: str
    arguments: str
    spec: CommandSpec


@dataclass(frozen=True)
class DispatchResult:
    """
    一次分发的轻量结果（plan 4.4）。

    :param kind: 分发结果类型
    :param command_name: 命中命令时的规范名（如 "/context"）；其它情况为 None
    :param message: 本地提示文本（未知命令引导、错误信息等）；无则为 None
    """

    kind: DispatchKind
    command_name: Optional[str] = None
    message: Optional[str] = None


@dataclass(frozen=True)
class CompletionItem:
    """
    一个补全候选：只有可见命令的规范名参与补全，别名不出现在候选中
    （别名仍可解析执行、完整命中仍高亮、/help 仍展示）。

    :param value: 候选文本（规范名，如 "/context"）
    :param canonical_name: 所属命令的规范名；当前恒等于 value，保留该字段是
        因为 Tab 单候选直补时 App 层用它 resolve 出 spec 判断是否补尾随空格
    :param description: 展示描述（命令描述）
    """

    value: str
    canonical_name: str
    description: str


class CommandController(Protocol):
    """
    框架无关的界面控制接口（spec F16，plan 第 5 节）。

    命令处理函数只依赖本协议，不导入 Textual / RhineApp / ConversationManager；
    生产环境由 RhineApp 实现，测试用 Fake Controller 替身（spec N8）。
    """

    @property
    def tools_enabled(self) -> bool:
        """当前 Provider 是否具备工具能力（DeepSeek 工具模式）。"""
        ...

    def show_user_input(self, text: str) -> None:
        """在聊天区回显一次用户输入（仅显示，不写入模型历史）。"""
        ...

    def show_message(self, text: str) -> None:
        """显示本地命令结果或错误提示（系统行）。"""
        ...

    def send_user_message(
        self,
        content: str,
        display_content: Optional[str] = None,
    ) -> None:
        """
        把一条用户消息交给现有对话路径（存档 → Agent → Worker 流式渲染）。

        :param content: 模型实际接收的完整内容（语义历史）
        :param display_content: 界面/回放优先显示的原始输入；仅提示词命令需要设置
        """
        ...

    def switch_mode(self, target: "ModeTarget") -> str:
        """切换目标模式（思考/Plan/权限），返回供界面显示的结果文本。"""
        ...

    def query_report(self, target: "ReportTarget") -> str:
        """查询只读报告（MCP / 上下文 / 记忆），返回报告文本。"""
        ...

    def refresh_status(self) -> None:
        """刷新底部状态栏（影响模式或状态的命令执行后显式调用）。"""
        ...

    def clear_conversation(self) -> None:
        """清空对话历史与聊天区（/clear 的领域与界面副作用）。"""
        ...

    def compact_context(self) -> None:
        """触发手动上下文压缩；耗时工作走后台 Worker，不阻塞界面（spec F18）。"""
        ...

    def resume_session(self, key: Optional[str]) -> None:
        """恢复会话：key 为 None 时打开选择面板，非 None 时直接载入（spec F13）。"""
        ...

    def exit_application(self) -> None:
        """退出应用。"""
        ...

    def cancel_subagents(self, target: Optional[str]) -> str:
        """
        取消子 Agent 任务（c13 F22/F24）。

        :param target: 任务标识；`None` 或 `"all"` 表示取消**全部**未完成任务
        :returns: 给用户看的结果文本（取消了几个 / 标识不存在 / 无任务可取消）

        **为什么 `/agents` 不是纯只读命令**（与 `/mcp` / `/hooks` 不同）：
        一个跑偏的后台子 Agent 若没有取消入口，用户只能退出整个程序。
        取消是本章唯一必须的写操作，其余形态仍是只读。
        """
        ...

    def run_skill(self, name: str, arguments: str, display: str) -> None:
        """
        执行一个 Skill（c11 F24）。

        :param name: Skill 名（不带斜杠）
        :param arguments: 用户参数，原样保留（不做 shell 分词）
        :param display: 用户敲的**原始输入**（如 `/commit 修复登录超时`）。

            **这个参数不可省**：spec F24/AC24 要求界面与会话回放显示用户的原始
            输入，而进入模型历史的是一段自包含文本（含 Skill 名、说明、参数）。
            两者靠 C10 的双内容模型分流——`Message.content` 给模型、
            `Message.display_content` 给界面与回放。没有本参数就无从设置后者，
            `/resume` 回放时用户会看到一段机器生成的文本而不是自己当初敲的命令。
        """
        ...

    def reload_skills(self) -> str:
        """热更新 Skill 定义（c11 F26），返回供界面显示的报告文本。"""
        ...

    def deactivate_skill(self, name: Optional[str]) -> str:
        """卸载已激活的 Skill；name 为 None 表示全部卸载。返回结果文本。"""
        ...


# 命令处理函数类型：接收调用上下文与控制器，无返回值（plan 4.2）。
# 耗时工作由控制器复用现有 Worker 消费路径，处理函数本身不阻塞。
CommandHandler = Callable[[CommandInvocation, CommandController], None]
