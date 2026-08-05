"""
对话管理模块。

ConversationManager 是 TUI 层与下层之间的唯一协调者，职责包括：
- 维护多轮对话的完整消息历史（history）
- 管理思考模式三档强度（off / high / max）
- 管理 Plan Mode 开关与权限模式、当前运行的取消信号
- 把每条普通消息委托给 Agent（ReAct 循环引擎）执行，并把其事件流交给 TUI

c4 变化：c3 的工具编排（写死的单轮往返 _stream/_execute 等）已整体迁入 agent.loop.Agent。
本类不再亲自跑循环，只负责「协调 + 状态」：每条普通消息构造一次 Agent 运行，把回调与策略
以参数/闭包注入 Agent，返回 Agent 产出的 AgentEvent 事件流。

c10 变化：斜杠命令的解析与分发整体迁出到 rhinecode/commands/ 命令层——
本类**不再解析任何斜杠文本**（旧 handle_input 已删除），改为暴露一组不依赖
命令字符串的领域方法（submit_user_message / cycle_thinking / toggle_plan /
cycle_permission / mcp_report / context_report / memory_report /
manual_compact / resume / clear），由 RhineApp（CommandController 实现）调用。
依赖方向：commands → tui/app → 本类；本类不导入 commands 包（plan 依赖固定）。

领域方法返回值类型决定 TUI 后续行为：
- str：本地反馈文本，直接作为系统提示显示
- Iterator[AgentEvent]：Agent 循环的事件流，由 TUI 的 Worker 消费并逐个渲染
- SessionListRequest：请求 TUI 弹出会话选择面板（仅 resume 无参时）
"""

import dataclasses
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider, Message, ToolCall
from rhinecode.provider.factory import create_provider
from rhinecode.tools.base import Tool
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tools.path_guard import workspace_root, register_read_root
from rhinecode.mcp.manager import MCPManager
from rhinecode.context import ContextManager
from rhinecode.memory import MemoryManager
from rhinecode.memory.session import SessionInfo
from rhinecode.skills.manager import SkillManager
from rhinecode.hooks import HookEventType, NullHookManager
from rhinecode.trace import (
    SCOPE_MAIN,
    NullRecorder,
    TraceEventType,
    TraceRecorderProtocol,
    isolated_scope,
)
from rhinecode.trace.tracing_provider import TracingProvider
from rhinecode.skills.models import (
    ActivationStatus,
    SKILL_MAX_ITERATIONS,
    SkillSpec,
    builtin_skills_dir,
)
from rhinecode.skills.render import render_active_body, render_invocation_text
from rhinecode.agent.loop import Agent, RunOptions
from rhinecode.agent.prompt import build_default_prompt, collect_environment
from rhinecode.agent.events import (
    AgentEvent,
    AgentEventType,
    ClarifyOption,
    ConfirmDecision,
    StopReason,
)
from rhinecode.permission import (
    Decision,
    DecisionResult,
    PermissionEngine,
    PermissionMode,
    PermissionRequest,
    Rule,
    to_allow_rule,
    to_request,
)

# 人在回路（HITL）确认回调类型：给定工具调用、工具实例与决策结果（含拒绝原因），
# 返回四态决定（本次/本会话/永久/拒绝）。由 TUI 层实现（弹确认面板），协调层只调用。
ConfirmCallback = Callable[[ToolCall, Tool, DecisionResult], ConfirmDecision]
# 需求澄清回调：给定问题与候选项，返回用户所选概述；返回 None 表示用户取消。
ClarifyCallback = Callable[[str, list[ClarifyOption]], Optional[str]]
# 计划审批回调：给定计划文本，返回用户是否批准开始执行。
ApprovePlanCallback = Callable[[str], bool]


# 独立模式子对话「未产出结果」时，按停止原因给出的回流文案（c11）。
# 每种原因都要能让用户一眼看出「为什么没结果」，而不是一句笼统的失败。
_ISOLATED_FAILURE_TEXT = {
    StopReason.USER_CANCELLED: "已取消，本次 Skill 未产出结果。",
    StopReason.MAX_ITERATIONS: "达到子任务迭代上限，本次 Skill 未产出结果。",
    StopReason.STREAM_ERROR: "模型请求出错（含上下文超限），本次 Skill 未产出结果。",
    StopReason.UNKNOWN_TOOL: "连续调用未知工具已停止，本次 Skill 未产出结果。",
    StopReason.PLAN_REJECTED: "计划未获批准，本次 Skill 未执行。",
}


def _degrade_notice(name: str, degrade) -> str:
    """
    激活时的降级警告文案（c11 F9 的「对用户可见」那一半）。

    两种形态措辞必须不同——一个是「可能只做了一半」，一个是「完全没生效」，
    用户的应对也不同（前者复查结果，后者先卸载别的 Skill 再重试）。
    """
    from rhinecode.skills.models import DegradeKind

    if degrade is DegradeKind.TRUNCATED:
        return (
            f"Skill `{name}` 的正文被截断（超单体上限），"
            f"可能只执行前半部分流程，请复查结果。"
        )
    return (
        f"Skill `{name}` 未注入（已激活 Skill 正文合计超总量上限），本次不会生效。"
        f"请先用 /skills off <名字> 卸载部分 Skill 后重试。"
    )


@dataclass
class SessionListRequest:
    """
    「请 TUI 弹出会话选择面板」的分发信号（c9 /resume 交互化）。

    resume(None)（/resume 无参）时返回本类型（而非文本列表），TUI 据此显示
    SessionPanel 让用户上下键选择。放在 conversation 层而非 agent/events.py：
    它是「TUI ↔ 协调层」的领域结果，不是 Agent 循环产出的事件。

    :param sessions: 全部会话信息（含锁定项与当前会话，是否 disabled 由面板决定）
    :param current_id: 当前会话 ID（面板据此标注「（当前）」并禁用该项）
    """

    sessions: list[SessionInfo]
    current_id: str


class ConversationManager:
    """
    多轮对话管理器。

    持有对话历史、思考模式、Plan Mode 与会话级免确认等状态，以及三类交互回调，
    是 TUI 与下层之间的唯一协调点。TUI 层不直接调用 Provider / Agent，全部通过此类中转。
    """

    # /think 命令的三态循环顺序：关闭 → 高效 → 最强 → 关闭
    _EFFORT_CYCLE = {"off": "high", "high": "max", "max": "off"}
    # 各档位对应的中文显示名称
    _EFFORT_LABEL = {"off": "关闭", "high": "高效（high）", "max": "最强（max）"}

    # /perm 命令的三档循环顺序：默认 → 严格 → 放行 → 默认（c6）
    _PERM_CYCLE = {
        PermissionMode.DEFAULT: PermissionMode.STRICT,
        PermissionMode.STRICT: PermissionMode.PERMISSIVE,
        PermissionMode.PERMISSIVE: PermissionMode.DEFAULT,
    }
    # 各权限模式对应的中文显示名称
    _PERM_LABEL = {
        PermissionMode.STRICT: "严格（strict）",
        PermissionMode.DEFAULT: "默认（default）",
        PermissionMode.PERMISSIVE: "放行（permissive）",
    }

    def __init__(
        self,
        provider: BaseProvider,
        config: Config,
        registry: Optional[ToolRegistry] = None,
        mcp_manager: "Optional[MCPManager]" = None,
        resume_latest: bool = False,
        skill_manager: "Optional[SkillManager]" = None,
        user_dir: Optional[Path] = None,
        recorder: "Optional[TraceRecorderProtocol]" = None,
        provider_factory: Optional[Callable[[Config], BaseProvider]] = None,
        hook_manager=None,
    ):
        """
        初始化对话管理器。

        :param provider: 已实例化的 Provider，负责实际的 API 调用
        :param config: 运行配置；提供 protocol（判断思考/工具能力）、model 与 debug_log（c5）。
                       构造结构化系统提示与环境信息、决定缓存调试日志是否开启时都要用到
        :param registry: 工具注册中心；为 None 时不启用工具能力
        :param mcp_manager: MCP 连接管理器（c7）；为 None 时 /mcp 命令与状态栏 MCP 段不展示。
                            仅用于状态查询，工具已在启动时注册进 registry，不经此引用调用。
        :param resume_latest: True = `rhine --continue`：启动时恢复最近的未锁定会话（c9 F10）
        :param skill_manager: Skill 编排者（c11）；由 `__main__` 在启动早期构造并注入。

            **为 None 时用 `SkillManager.empty()` 兜底，协调层绝不自行扫盘造一个**，
            三条理由：
            1. 它拿不到 `has_short_command`——那需要 `CommandRegistry` 实例，
               而本构造函数没有；
            2. 自行构造会**扫盘**，等于给既有一整套测试引入读用户主目录的隐式 IO，
               违背 spec N1「可在无终端无网络环境测试」；
            3. 与 `mcp_manager` 的形态不一致（那个也是外部注入、None 时降级）。

            用 Null Object 而不是到处 `if self.skill_manager is not None`：
            各使用点无需散落判空，空实例的每个方法都返回「什么都没有」的安全值。
        :param user_dir: 用户级目录（`~/.rhinecode` 的替身）。**必须可选、缺省等于现状**——
                         为 None 时取 `Path.home() / ".rhinecode"`，行为与参数化之前
                         逐字一致。改成必选会把回归面从零推到五个测试文件
                         （`test_review_fixes` / `test_resume_replay` / `test_skill_isolated` /
                         `test_skill_sandbox` / `test_memory_*` 都直接构造本类且都不传它）。
                         给定时，用户级项目指令 / 笔记索引 / Skill 目录 / 权限规则四类内容
                         一并改从该目录读取，使装配层能在临时目录里跑一次完整装配而不读
                         真实主目录（trace spec F23）。
        :param recorder: 行为记录器（trace 设施）。缺省用 `NullRecorder()`——
                         **不传等于零回归**，全部埋点变成空调用。
        :param provider_factory: 「怎么造一个模型客户端」的可替换实现，签名
                         `(Config) -> BaseProvider`；缺省 `create_provider`（逐字等于现状）。

                         **这是「换模型旁路」的注入点**：`_provider_for` 会在 Skill
                         声明了 `model:` 时自己再造一个 Provider，那条路径完全绕开
                         构造函数收到的 `provider` 参数。端到端驱动设施若只替换了
                         `provider` 而没透传本参数，那条旁路会**静默连上真实网络**
                         ——现象是「测试莫名其妙很慢、偶尔失败」，极难定位。
                         （与 trace P0 的记录旁路是同一处：那次也是 `_provider_for`
                         漏包 `TracingProvider` 导致换模型的请求整段不进记录。）
        :param hook_manager: Hook 编排者（c12）。缺省用 `NullHookManager()`——
                         **不传等于零回归**，全部分发变成空调用且不构造任何负载。
                         与 `skill_manager` 同形态：由装配层构造并注入，本类绝不
                         自行加载配置（那会给一整套既有测试引入读用户主目录的隐式 IO）。
        """
        self._provider = provider
        # 换模型旁路的工厂。**存 None 而不是在这里就 `or create_provider`**：
        # 那样会把模块级的 `create_provider` 在**构造时**固化下来，而既有测试
        # （`test_skill_isolated.py`）是在构造之后猴补 `conversation.create_provider`
        # 模块属性来验证换模型行为的——固化会让那些猴补静默失效。
        # 存 None、到 `_provider_for` 里再解析，两种用法就都成立。
        self._provider_factory: Optional[Callable[[Config], BaseProvider]] = provider_factory
        # 行为记录器：Null Object 兜底，各使用点无需判空（trace spec N1）
        self._recorder: TraceRecorderProtocol = recorder or NullRecorder()
        # 用户级目录：**必须在这里解析**，因为下面的 PermissionEngine.load 就要用它。
        # （历史上这行赋值位于 PermissionEngine.load 之后、只服务 memory/skills，
        #  参数化时若只改那一处，权限层拿不到 user_dir，「用户级权限规则不参与求值」
        #  这条承诺会静默落空。）
        self._user_dir: Path = user_dir if user_dir is not None else Path.home() / ".rhinecode"
        self._config = config
        # MCP 连接管理器：仅供 /mcp 命令与状态栏读取连接状态；工具走 registry，与此解耦。
        self._mcp_manager = mcp_manager
        # 协议名沿用配置里的 protocol，逻辑与此前一致（仅入参由字符串换成整份 config）。
        self._protocol = config.protocol
        self._registry = registry
        self.history: list[Message] = []
        # 思考模式强度：off（关闭）/ high（高效）/ max（最强）
        self.thinking_effort: str = "off"
        # Plan Mode 开关：开启时循环只放只读工具并注入引导提示（仅工具可用 Provider 生效）
        self.plan_mode: bool = False
        # 权限决策引擎（c6）：启动时加载三层 YAML 规则，持有权限模式与会话级规则。
        # 取代 c5 的「会话级一刀切免确认」标志——本会话放行改为按规则登记（见 _run 的 ask 闭包）。
        # web_fetch 扩展 F4 链路①：总开关要穿过这里才到得了 config.load_all
        # ——`bootstrap.py` 对权限 load_all 是零调用，在装配层透传是做不到的。
        self._engine: PermissionEngine = PermissionEngine.load(
            user_dir=self._user_dir,
            web_fetch_enabled=config.web_fetch_enabled,
        )
        # 当前运行的取消信号；每次运行重建，置位即让循环尽快停止
        self._cancel_event: threading.Event = threading.Event()

        # 三类交互回调，由 TUI 层在挂载后注入；为 None 时各自走 fail-closed/降级处理
        self.confirm_callback: Optional[ConfirmCallback] = None
        self.clarify_callback: Optional[ClarifyCallback] = None
        self.approve_plan_callback: Optional[ApprovePlanCallback] = None

        # 工具/循环能力仅在 DeepSeek 协议且提供了注册中心时启用（本章范围）
        self._tools_enabled = (self._protocol == "deepseek" and registry is not None)
        if registry is not None:
            self._install_path_filters(registry)
        # Hook 编排者（c12）。Null Object 兜底，各分发点无需判空。
        self._hooks = hook_manager if hook_manager is not None else NullHookManager()
        # ReAct 循环引擎：持有长期依赖，每条普通消息调用一次 run()
        self._agent = Agent(provider, registry, recorder=self._recorder, hooks=self._hooks)

        # 上下文压缩器（c8）：仅在工具可用模式构造——压缩的主要对象是工具结果，
        # 且循环/工具只在该模式存在。长期持有，跨消息累积估算锚点与熔断状态。
        # 存盘目录锁定在项目根 .rhinecode/context/（与其它 .rhinecode 配置同处，可 gitignore）。
        self._context_manager: Optional[ContextManager] = None
        if self._tools_enabled:
            self._context_manager = ContextManager(
                provider,
                config.model,
                config.context_window,
                workspace_root() / ".rhinecode" / "context",
                recorder=self._recorder,
                hook_manager=self._hooks,
            )

        # 记忆系统编排者（c9）：所有 Provider 都构造——RHINE.md 注入与会话存档不依赖
        # 工具能力；自动笔记由 notes_enabled 门控（仅工具模式，F21）。
        # user_dir 已在构造函数开头解析成 self._user_dir（权限层要先用），此处直接复用
        user_dir = self._user_dir
        self.memory_manager = MemoryManager(
            provider,
            config.model,
            workspace_root(),
            user_dir,
            notes_enabled=self._tools_enabled,
            recorder=self._recorder,
        )
        # 用户级记忆目录加入只读白名单（F18）：模型可按索引 read_file 用户级笔记全文。
        # 注册本身无副作用（写类判定不受影响），无条件执行即可。
        register_read_root(user_dir / "memory")

        # Skill 编排者（c11）。缺省用 Null Object，见构造参数说明。
        self.skill_manager: SkillManager = skill_manager or SkillManager.empty()
        # 用户级与内置 Skill 目录加入**只读**白名单（N5）：目录型 Skill 的随附资源
        # （模板、示例、参考文档）位于项目工作区之外，模型需要按资源清单读取它们。
        # 与 c9 的 memory 目录同理——只对 read 类判定生效，写/搜索面完全不动。
        register_read_root(user_dir / "skills")
        register_read_root(builtin_skills_dir())

        # 按模型名缓存的临时 Provider（c11 F22：独立模式 Skill 可指定模型）。
        self._provider_cache: dict[str, BaseProvider] = {}

        # 启动编排：加载 RHINE.md、清理过期会话、开新档或 --continue 恢复。
        # 返回的提示由 TUI 挂载时展示（无提示为 None）。
        memory_notice = self.memory_manager.startup(resume_latest, self.history)
        # 权限规则的加载警告并入启动提示（web_fetch 扩展 F25）。
        #
        # `PermissionEngine.load_errors` 在此之前**全仓零消费者**——警告收集完就死在
        # 那里。之所以并进 startup_notice 而不是新做一个 /perm 报告：前者是既有字段
        # （TUI 挂载时写进聊天区）、零新增维护点，且**不需要用户主动敲命令就能看见**。
        # Hook 的公共字段（session_id / cwd）绑定一次；会话切换时在 clear/_resume_stream
        # 里重新绑定——不重绑的话，`/clear` 之后所有 Hook 负载里的 session_id 仍是旧档，
        # 而那是排查「这条 hook 是哪次会话触发的」时唯一能对上的字段。
        self._hooks.bind_context(
            session_id=self.memory_manager.session_id, cwd=str(workspace_root())
        )
        self.startup_notice: Optional[str] = self._compose_startup_notice(memory_notice)

    def _dispatch_session(self, event: "HookEventType", **fields) -> None:
        """
        分发一个会话级事件（c12 spec F2）。

        :param event: SESSION_START 或 SESSION_END
        :param fields: 事件专有字段（`source` 或 `reason`）

        三个会话切换点共用它：`/clear`、`/resume`、装配层的启动与退出。
        写成一个方法而不是各处直接 `dispatch`，是为了让「先绑定再分发」这个顺序
        只有一处——漏了重绑定不报错，只是负载里的 session_id 是旧的。

        副作用：可能起子进程 / 发 HTTP 请求；异常一律吞掉。
        """
        if not self._hooks.has_listeners(event):
            return
        try:
            self._hooks.dispatch(event, lambda: dict(fields))
        except Exception:
            pass

    def hooks_report(self) -> str:
        """`/hooks` 的报告文本（c12 F11）。纯只读，不改任何状态。"""
        return self._hooks.report()

    def hooks_project_notice(self) -> Optional[str]:
        """项目级 Hook 规则的启动提示（c12 F9.1）；无项目级规则时返回 None。"""
        return self._hooks.project_notice()

    def _compose_startup_notice(self, memory_notice: Optional[str]) -> Optional[str]:
        """
        把记忆系统的启动提示与权限规则的加载警告拼成一条启动提示。

        :param memory_notice: `MemoryManager.startup()` 的返回（可能为 None）
        :returns: 拼接后的提示；两者都为空时返回 None

        由 `tui/app.py` 在界面挂载时写进聊天区。两者都可能为空，
        拼接时不产生多余空行——空提示与「有提示但只有一行空白」在界面上
        是两种观感，后者会让人以为出了什么事。

        副作用：无。
        """
        parts: list[str] = []
        if memory_notice:
            parts.append(memory_notice)
        errors = getattr(self._engine, "load_errors", None) or []
        if errors:
            parts.append("权限规则加载提示：\n" + "\n".join(f"- {e}" for e in errors))
        if not parts:
            return None
        return "\n\n".join(parts)

    def _install_path_filters(self, registry: ToolRegistry) -> None:
        """
        给会递归发现文件的只读工具注入文件级权限过滤器。

        read_file 的单文件路径在执行前已由权限管线判断；grep/glob 这类目录级工具还会在
        execute 内部发现更多文件，因此需要在真正读取或返回每个文件前再次用 Read 规则判定。
        """
        def allow_read_path(rel_path: str) -> bool:
            req = PermissionRequest(
                tool_name="read_file",
                rule_name="Read",
                specifier=rel_path,
                kind="read_path",
                is_read_only=True,
                mode=self._engine.mode,
            )
            return self._engine.decide(req).decision != Decision.DENY

        for name in ("grep_content", "glob_files"):
            tool = registry.get(name)
            setter = getattr(tool, "set_path_filter", None)
            if callable(setter):
                setter(allow_read_path)

    @property
    def permission_mode_value(self) -> Optional[str]:
        """
        当前权限模式的取值字符串（"strict"/"default"/"permissive"），供状态栏展示（c6）。

        仅在工具可用（DeepSeek 工具模式）时有意义——其它 Provider 没有受控工具，
        权限模式不参与任何判断，故返回 None，让状态栏不展示这一段，避免误导。

        :returns: 模式值字符串；工具不可用时返回 None
        """
        if not self._tools_enabled:
            return None
        return self._engine.mode.value

    @property
    def tools_enabled(self) -> bool:
        """当前 Provider 是否具备工具能力（DeepSeek 工具模式，c10 供控制器只读）。"""
        return self._tools_enabled

    def clear(self) -> str:
        """
        清空对话历史，返回兼容确认文本（c10 起由命令层展示，文案保持不变）。

        副作用：self.history 被重置为空列表，下次请求将不携带任何上下文；
        同时重置上下文压缩器的会话级状态（估算锚点、熔断计数、已存盘幂等集合，c8 F15）——
        历史清空后旧锚点与熔断态都不再适用，必须一并归零。
        c9：会话存档随之「开新档」——旧存档保留不动、后续消息写入新文件（F8），
        笔记高水位一并归零。
        c11：已激活的 Skill 一并卸载（F11）——激活态属于「当前这段对话」，
        历史都清空了，还留着 SOP 注入与工具集收窄会让下一句话的行为莫名其妙。

        :returns: 确认文本「对话历史已清空」
        """
        # c12：先送 SESSION_END（此刻 session_id 还是旧档），清空并开新档后再送
        # SESSION_START。顺序不可颠倒——颠倒会让两条事件都带着新档 ID，
        # 「哪次会话结束了」这个信息就丢了。
        self._dispatch_session(HookEventType.SESSION_END, reason="clear")
        self.history = []
        if self._context_manager is not None:
            self._context_manager.reset()
        self.memory_manager.on_clear()
        self.skill_manager.clear_active()
        self._hooks.bind_context(session_id=self.memory_manager.session_id)
        self._dispatch_session(HookEventType.SESSION_START, source="clear")
        return "对话历史已清空"

    def request_cancel(self) -> None:
        """
        请求取消当前正在运行的 Agent 循环。

        由 TUI 在用户按取消键时调用：置位当前运行的取消信号，循环会在安全点检测并尽快停止
        （spec F9）。无运行时调用也安全（仅置位一个会在下次运行被重建的 Event）。
        """
        self._cancel_event.set()

    # ------------------------------------------------------------------ #
    # 领域方法（c10：命令解析已迁出到 commands 层，这里只做领域能力）
    # ------------------------------------------------------------------ #
    def submit_user_message(
        self,
        content: str,
        display_content: Optional[str] = None,
        grant_skill: "Optional[SkillSpec]" = None,
    ) -> Iterator[AgentEvent]:
        """
        提交一条用户消息并启动 Agent 循环（普通对话与提示词命令共用的唯一入口）。

        :param content: 模型实际接收的完整内容（语义历史）
        :param display_content: 界面/回放优先显示的原始输入（c10 双内容，
                                仅提示词命令如 /init 需要设置；普通消息为 None）
        :param grant_skill: 本次由哪个 Skill 触发（短命令路径传入）。它的
                            `allowed-tools` 会在**本次执行内**生效，结束即撤销。
                            **必须由 `_wrap_events` 在取过令牌之后授予**——
                            在这之前授予的话，令牌把它一起圈了进去，撤销时保留下来，
                            于是授权跟着常驻态一直活下去（实测踩过）。
        :returns: Agent 事件流（由 TUI Worker 消费）

        副作用：向 self.history 追加用户消息并写入会话存档（c9 F6）；
        不解析斜杠、不做界面回显（回显由命令分发器统一负责）。
        """
        user_msg = Message(role="user", content=content, display_content=display_content)
        self.history.append(user_msg)
        self.memory_manager.record_message(user_msg)
        return self._run(grant_skill=grant_skill)

    def cycle_thinking(self) -> str:
        """
        三态循环切换思考模式（off → high → max → off）。

        :returns: 供界面显示的结果文本；不支持的 Provider 返回原能力限制提示
        """
        # Anthropic 和 DeepSeek 均支持思考模式，OpenAI 原生协议不支持
        if self._protocol not in ("anthropic", "deepseek"):
            return "当前 Provider 不支持思考模式"
        self.thinking_effort = self._EFFORT_CYCLE[self.thinking_effort]
        label = self._EFFORT_LABEL[self.thinking_effort]
        return f"思考模式：{label}"

    def toggle_plan(self) -> str:
        """
        切换 Plan Mode 开关（仅工具可用 Provider 生效）。

        :returns: 供界面显示的结果文本；不支持时保持关闭并返回原提示
        """
        # Plan Mode 依赖工具能力，仅在工具可用的 Provider（DeepSeek + 注册中心）下生效
        if not self._tools_enabled:
            return "当前 Provider 不支持计划模式"
        self.plan_mode = not self.plan_mode
        return "计划模式：开启" if self.plan_mode else "计划模式：关闭"

    def cycle_permission(self) -> str:
        """
        三档循环切换权限模式（默认 → 严格 → 放行 → 默认，c6）。

        :returns: 供界面显示的结果文本；工具不可用的 Provider 返回原提示
        """
        # 仅在工具可用的 Provider 下有意义（无工具则无可控对象）
        if not self._tools_enabled:
            return "当前 Provider 不支持权限模式"
        self._engine.set_mode(self._PERM_CYCLE[self._engine.mode])
        label = self._PERM_LABEL[self._engine.mode]
        return f"权限模式：{label}"

    def mcp_report(self) -> str:
        """MCP 连接状态只读报告（c7 F16）：纯读不改状态。"""
        if self._mcp_manager is None:
            return "未启用 MCP（未配置任何 MCP Server）"
        return self._mcp_manager.status_report()

    def context_report(self) -> str:
        """上下文用量只读报告（c8 F16）：纯读不改状态，不涉及阻塞的 LLM 调用。"""
        if not self._tools_enabled or self._context_manager is None:
            return "当前 Provider 不支持上下文管理"
        return self._context_manager.usage_report(self.history)

    def memory_report(self) -> str:
        """记忆系统只读报告（c9 F19）：纯读不改状态。"""
        return self.memory_manager.memory_report()

    def manual_compact(self) -> "Iterator[AgentEvent] | str":
        """
        手动触发第二层上下文压缩（c8 F14）。

        摘要是阻塞的 LLM 调用，若同步执行会卡死 UI 线程，故返回事件流生成器
        交给 TUI 的后台 Worker 消费（与普通消息同一执行路径，spec F18）。

        :returns: 事件流；工具不可用的 Provider 返回原能力限制提示文本
        """
        if not self._tools_enabled or self._context_manager is None:
            return "当前 Provider 不支持上下文管理"
        return self._manual_compact()

    def resume(self, key: Optional[str] = None) -> "SessionListRequest | Iterator[AgentEvent] | str":
        """
        会话恢复（c9 F9）。

        - key 为 None（/resume 无参）：返回 SessionListRequest（TUI 弹交互式选择面板）；
          无存档或全部不可选时返回原提示文本。
        - key 非 None（编号或 ID）：返回载入事件流走 Worker——载入后可能触发 C8 压缩
          （阻塞的摘要 LLM 调用），不能卡 UI 主线程。面板选中后 TUI 直接以
          session_id 调本方法（c10 起不再伪造 "/resume <id>" 文本），两条路径共用
          _resume_stream。

        :param key: 会话编号或完整 ID；None 表示请求列表
        """
        if key is None:
            infos = self.memory_manager.list_resume_sessions()
            if not infos:
                return "没有可恢复的会话存档。"
            current = self.memory_manager.session_id
            # 全部条目都不可选（只有当前会话 / 均被其它实例锁定）时不弹面板，
            # 直接如实反馈——弹一个没有可选项的面板只会让用户困惑。
            if all(i.session_id == current or i.locked for i in infos):
                return "没有可恢复的其它会话（仅有当前会话或均被其它实例占用）。"
            return SessionListRequest(sessions=infos, current_id=current)
        return self._resume_stream(key)

    def _manual_compact(self) -> Iterator[AgentEvent]:
        """
        /compact 的事件流：在后台 Worker 里执行一次手动压缩并反馈结果（c8 F14/F17）。

        与普通消息不同，这里**不**向 history 追加任何 user 消息——/compact 是命令而非对话；
        只调 ContextManager.manual_compact（可能原地重构 history），把结果作为 NOTICE 展示，
        再产出一个 FINISHED(COMPLETED) 让 TUI 收尾（自然完成，不额外打扰）。

        :returns: 仅含一条 NOTICE 与一条 FINISHED 的事件流

        副作用：可能发起摘要 LLM 调用并原地重构 self.history。
        """
        notice = self._context_manager.manual_compact(self.history)
        yield AgentEvent(type=AgentEventType.NOTICE, message=notice.message)
        yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED)

    def _resume_stream(self, key: str) -> Iterator[AgentEvent]:
        """
        /resume <编号或ID> 的事件流：在后台 Worker 里接管并载入目标会话（c9 F9/F11）。

        步骤：
        1. memory_manager.resume_into：锁检查 → 接管 → 容错载入 → 原地替换 history；
        2. 成功时先产出 HISTORY 事件携带**压缩前**的历史快照——TUI 收到后清屏整体回放。
           快照必须取在下一步 before_request 之前：第二层压缩会把早段消息替换为摘要占位，
           而用户要回看的是原始对话（浅拷贝即可，压缩用 history[:] 整体重构、不改旧元素）；
        3. 成功且有上下文压缩器时：先 reset()（旧估算锚点对应旧历史，换历史不复位会
           严重低估用量，c9 plan 技术决策），再跑一次 before_request——载入的历史若已
           逼近窗口上限就地压一次（F11③），不逼近则无动作；
        4. 结果以 NOTICE 反馈（回放后追加在底部，充当成功反馈），FINISHED(COMPLETED) 收尾。

        失败路径（找不到 / 当前会话 / 被锁）不产 HISTORY 事件，TUI 不清屏、只显示 NOTICE。

        :returns: 事件流（成功：HISTORY + NOTICE 若干 + FINISHED；失败：NOTICE + FINISHED）

        副作用：可能切换会话锁、改写 self.history、发起摘要 LLM 调用。
        """
        # c12：SESSION_END 必须在 resume_into 之前——它一成功，session_id 就已经
        # 换成目标会话了，那时再送就带不出「离开的是哪一个」。
        self._dispatch_session(HookEventType.SESSION_END, reason="resume")
        ok, message = self.memory_manager.resume_into(key, self.history)
        if ok:
            # c11 N4：切换会话必须清空 Skill 激活态，且要在产出 HISTORY 之前。
            #
            # 激活态是**进程内存**状态，而 /resume 换的是历史、不是进程。
            # 不主动清空的话，用户在会话 A 激活的 Skill 会跟着进入会话 B 的上下文——
            # 它的 SOP 正文继续每轮注入、白名单继续收窄工具集，而会话 B 的历史里
            # 根本没有任何激活过它的痕迹。用户看着一段陌生的历史，模型却在按
            # 另一段对话里定的规矩行事，这种状态几乎无法自查。
            #
            # 载入**失败**时不清空：此时仍停留在原会话，激活态应当原样保持。
            self.skill_manager.clear_active()
            # 历史恢复埋点（trace F16）。`origin` 区分两条来源：
            # 这里是运行中的 `/resume`；另一条是启动时的 `--continue`
            # （它在 ConversationManager 构造期间原地改写 history、一条事件都不产，
            #  故由装配层单独补一条 origin="startup"，见 bootstrap.build_app）。
            self._recorder.emit(
                TraceEventType.HISTORY_RESTORED,
                origin="resume_command",
                message_count=len(self.history),
                session_id=self.memory_manager.session_id,
            )
            self._hooks.bind_context(session_id=self.memory_manager.session_id)
            self._dispatch_session(HookEventType.SESSION_START, source="resume")
            # 浅拷贝快照：防止后续 before_request 对 history 的原地重构影响回放内容
            yield AgentEvent(type=AgentEventType.HISTORY, messages=list(self.history))
        if ok and self._context_manager is not None:
            self._context_manager.reset()
            for notice in self._context_manager.before_request(self.history):
                yield AgentEvent(type=AgentEventType.NOTICE, message=notice.message)
        yield AgentEvent(type=AgentEventType.NOTICE, message=message)
        yield AgentEvent(type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED)

    def mcp_status_line(self) -> "str | None":
        """
        返回底部状态栏用的 MCP 一行摘要（c7 F15）。

        :returns: 形如「MCP：已连接 2/3 · 工具 11」；未启用 MCP 或无 Server 时返回 None
                  （None 时状态栏不展示 MCP 段，避免误导）。

        副作用：无（仅读取 manager 状态）。
        """
        if self._mcp_manager is None:
            return None
        return self._mcp_manager.status_line()

    def context_status_line(self) -> "tuple[str, bool] | None":
        """
        返回底部状态栏用的上下文用量摘要（c8）。

        :returns: (文本, 是否高亮) 如 ("上下文：19% · 12.3K/64K", False)；
                  工具不可用的 Provider（无 ContextManager）返回 None，此时状态栏
                  不展示该段，语义与 mcp_status_line 的「None 即隐藏」一致。

        副作用：无（仅只读估算当前历史用量）。
        """
        if self._context_manager is None:
            return None
        return self._context_manager.status_line(self.history)

    # ------------------------------------------------------------------ #
    # Skill 领域方法（c11）
    # ------------------------------------------------------------------ #
    def skills_report(self) -> str:
        """
        `/skills` 的只读状态报告（含体检建议段）。

        **不需要传注册中心的工具名**：体检只看 Skill 定义本身，不依赖当前注册了
        哪些工具。曾经这里传过一份，那是上一轮作者期为「白名单是否已等于全集」
        那套检查留的——收窄能力随对齐改造删除后，参数就成了死的。
        """
        return self.skill_manager.report()

    def skills_prompt_report(self) -> str:
        """`/skills prompt` 的只读注入内容报告。"""
        registered = self._registry.names() if self._registry else frozenset()
        return self.skill_manager.prompt_report(registered)

    def skill_status_segment(self) -> Optional[str]:
        """状态栏的 Skill 段（形如 `Skill:2`）；无激活时 None，状态栏随之隐藏该段。"""
        return self.skill_manager.status_segment()

    def deactivate_skill(self, name: Optional[str]) -> str:
        """卸载已激活的 Skill；name 为 None 表示全部卸载。返回结果文本。"""
        return self.skill_manager.deactivate(name)

    def reload_skills(self) -> str:
        """
        热更新 Skill 定义（`/skills reload`，spec F26/F27）。

        :returns: 可读的变更报告

        `known` 的口径与 `__main__` 启动时完全一致（注册中心当前名字 ∪ 两个
        Plan Mode 特殊工具），否则热更新会用一套不同的标准去判定笔误。

        副作用：重新扫盘、替换 catalog、可能自动卸载已消失的 Skill。

        **本方法只管领域侧**，不刷新斜杠短命令注册表——那需要 `CommandRegistry`，
        而本类刻意不依赖 commands 包（依赖方向 `commands ← tui/app ← __main__`）。
        短命令的重新注册由控制器方法 `RhineApp.reload_skills()` 在调完本方法后
        接上，它本来就同时持有注册表与 SkillManager。
        """
        outcome = self.skill_manager.reload()

        lines = ["Skill 定义已重新加载。"]
        if outcome.added:
            lines.append(f"新增：{'、'.join(outcome.added)}")
        if outcome.removed:
            lines.append(f"移除：{'、'.join(outcome.removed)}")
        if outcome.auto_deactivated:
            lines.append(
                f"已自动卸载（定义已消失）：{'、'.join(outcome.auto_deactivated)}"
            )
        if not (outcome.added or outcome.removed or outcome.auto_deactivated):
            lines.append("（Skill 列表无变化；已激活 Skill 的正文按最新定义生效）")
        if outcome.errors:
            lines.append("")
            lines.append("加载失败：")
            lines.extend(f"- {e.path}：{e.reason}" for e in outcome.errors)
        if outcome.warnings:
            lines.append("")
            lines.append("警告：")
            lines.extend(f"- {w}" for w in outcome.warnings)
        return "\n".join(lines)

    def run_skill(
        self, name: str, arguments: str, display: str
    ) -> "Iterator[AgentEvent] | str":
        """
        执行一个 Skill（c11 F18/F19/F24），两种模式走两条完全不同的路径。

        :param name: Skill 名（不带斜杠）
        :param arguments: 用户参数，原样保留
        :param display: 用户敲的原始输入，作为 `Message.display_content`
        :returns: 事件流（成功执行）或提示文本（找不到）

        - **留在主对话**（不写 `context`）：激活它，然后把一段自包含调用文本作为
          普通用户消息提交，走完全正常的 Agent 路径。SOP 正文由动态槽位注入。
        - **`context: fork`**：开一条子对话跑完，只回流结论（见 `_run_forked_skill`）。

        副作用：修改激活列表；向主历史追加消息；发起 API 请求。
        """
        # `by_model=False`：用户敲斜杠命令是显式动作，`disable-model-invocation`
        # 那道闸门只挡模型（对齐改造 F8，两个维度正交）。不传的话
        # `/deploy` 会被自己的闸门挡下，模型转头让用户去敲 `/deploy`——死循环。
        result = self.skill_manager.activate(name, arguments, by_model=False)

        if result.status is ActivationStatus.NOT_FOUND:
            available = (
                "、".join(result.available_names)
                if result.available_names
                else "（当前没有任何可用 Skill）"
            )
            return f"没有名为 `{name}` 的 Skill。\n可用的 Skill：{available}"

        spec = self.skill_manager.get(name)

        if result.status is ActivationStatus.FORKED:
            # 用户经斜杠命令触发是显式动作，`disable-model-invocation` 只挡模型，
            # 挡不到这里（对齐改造 F8：两个维度正交）。
            return self._wrap_events(
                self._run_forked_skill(spec, arguments, display),
                extra_skill=spec,
                trigger="skill",
                # 作用域必须显式传：子对话的 trace 作用域在**内层生成器**里才进入，
                # `turn_start` 发生在那之前，现取只会拿到 `main`。
                scope=isolated_scope(spec.command_name),
            )

        # 留在主对话：激活成功，把自包含文本作为一条普通用户消息提交，
        # 并把「本次由谁触发」透传下去——授予与撤销都由 `_wrap_events` 负责，
        # 顺序才对得上（先取令牌、再授予、finally 回滚到令牌）。
        events = self.submit_user_message(
            render_invocation_text(spec, arguments),
            display_content=display,
            grant_skill=spec,
        )
        if result.degrade is not None:
            # 降级必须让用户看见（F9）。在事件流最前面插一条 NOTICE——
            # 这是「激活时给出警告」那一半（另一半是 /skills 报告里的标记）。
            return self._prepend_notice(_degrade_notice(name, result.degrade), events)
        return events

    @staticmethod
    def _prepend_notice(
        message: str, events: Iterator[AgentEvent]
    ) -> Iterator[AgentEvent]:
        """在一个事件流最前面插一条 NOTICE，其余原样透传。"""
        yield AgentEvent(type=AgentEventType.NOTICE, message=message)
        yield from events

    def _provider_for(self, model: str) -> BaseProvider:
        """
        取一个使用指定模型的 Provider（c11 F22，仅独立模式）。

        :param model: 模型名
        :returns: 该模型对应的 Provider 实例（同名复用同一个）

        **这样实现的关键收益是不必改动 `BaseProvider.stream_chat` 的接口**——
        若走「给 stream_chat 加一个 model 参数」的路子，三个 Provider 实现都要改。
        而 `Config` 是 dataclass，`dataclasses.replace` 换掉 model 再走
        `create_provider` 就能得到一个新 Provider，Provider 层一行不动。

        缓存**有界**（上界 = 全部 Skill 声明的不同模型数，通常是个位数）、
        **不关闭**——`BaseProvider` 本来就没有 `close()`，`__main__` 的 finally
        只回收 MCP 连接与会话锁，这里与主 Provider 的处理同口径。

        副作用：首次调用某模型时构造一个新 Provider（可能建立 HTTP 连接池）。
        """
        cached = self._provider_cache.get(model)
        if cached is not None:
            return cached
        # 走可注入的工厂而不是直接 `create_provider`：这样端到端驱动设施注入假模型时，
        # 这条换模型旁路也一并被换掉（见构造函数 provider_factory 的说明）。
        # **每次现取**（而不是构造时固化）：没注入时它就是当下的模块级
        # `create_provider`，既有测试的猴补照常生效。
        factory = self._provider_factory or create_provider
        provider = factory(dataclasses.replace(self._config, model=model))
        # 旁路覆盖（trace spec F18）：这条 Provider 不经装配层，若不在这里包一层，
        # 「Skill 指定了别的模型」的那些请求就完全不会出现在记录里——而那正是
        # 最需要看清楚的场景之一（换了模型之后行为为什么变了）。
        # 只在开启记录时包装，关闭时链路上不得有中间层（AC3）。
        # 包装后再存进缓存，保证同名模型复用的是同一个包装实例（轮次计数才连续）。
        if self._recorder.enabled:
            provider = TracingProvider(provider, self._recorder, model)
        self._provider_cache[model] = provider
        return provider

    def _run_forked_skill(
        self, spec: SkillSpec, arguments: str, display: str, record: bool = True
    ) -> Iterator[AgentEvent]:
        """
        独立模式：开一条子对话跑完，只把结论回流主历史（c11 F19/F20/F21/F23）。

        :param spec: 要执行的 Skill
        :param arguments: 用户参数
        :param display: 用户敲的原始输入
        :param record: 是否把那两条配对消息写进主历史与会话存档。
            用户经斜杠命令触发时为真；**模型自行发起时为假**——模型这次动作
            已经在主历史里留下 `assistant(tool_calls)` 与 `tool` 结果一对，
            再追加一对 user/assistant 会凭空多出一轮不存在的对话。
        :returns: 事件流；`record` 为真时主历史**恰好**新增两条配对消息

        为什么值得开一条子对话：一次代码审查可能读二十个文件、跑几条命令，
        这些中间过程对主对话毫无价值，却会占掉大量上下文。子对话跑完只回流
        一段结论，主历史干净、上下文预算也省下来了。

        **子对话触达上下文上限时的兜底链路**（兑现 F21 的说明义务）：
        子对话只跑 C8 第一层（工具结果存盘），不跑第二层摘要——它是短任务，
        为它调一次摘要 LLM 不划算，且摘要用的锚点属于主历史、对它无意义。
        万一仍然超窗：API 报错 → `chunk.type == "error"` → 循环产出 ERROR 事件
        （TUI 渲染红色错误行）→ `FINISHED(STREAM_ERROR)` → 下面判为「未产出」
        → 追加一条 NOTICE + 在主历史留一条说明。用户有两处可见反馈，
        历史里也留下可追溯记录，不会「什么都没发生」。

        副作用：向主历史追加两条消息并写会话存档；发起 API 请求；
        重建 `self._cancel_event`；可能弹权限确认面板。
        """
        # ── 1. 自包含调用文本：主历史与子对话首条 user 消息**同源复用** ──
        # 逐字一致很重要：几个月后回看主历史那条 user 消息，它描述的就是
        # 子对话当时实际收到的任务陈述。
        invocation = render_invocation_text(spec, arguments)

        # ── 2. 主历史先记 user 消息 ──
        user_msg = Message(
            role="user", content=invocation, display_content=display
        )
        if record:
            self.history.append(user_msg)
            self.memory_manager.record_message(user_msg)

        # ── 3. 子历史 = 只有那条自包含消息 ──
        # 标准里的 fork 不提供「从主历史带入尾部若干条」，C11 的 `history_messages`
        # 随之删除。子对话拿到的就是这一条——它是自包含的（含 Skill 名、说明与参数），
        # 模型据此就能知道要做什么。
        sub_history: list[Message] = [Message(role="user", content=invocation)]

        # ── 4. 子系统提示：stable 复用主对话，dynamic 只带这一个 Skill 的正文 ──
        project_root = str(workspace_root())
        env = collect_environment(self._config, project_root)
        assembled = build_default_prompt(
            env,
            custom_instructions=self.memory_manager.custom_instructions(),
            memory_index=self.memory_manager.memory_index(),
            skill_index="",   # 子对话不给清单——它不许再激活别的 Skill（F23）
            active_skills="",
            # ⚠ web_fetch 扩展 F4 链路②的**第二个**调用点。漏传这里的表现是
            # 「主对话有不可信约束、fork 子对话没有」——界面上完全看不出来，
            # 只有被注入的页面恰好走进 fork 子对话时才显形。
            untrusted_enabled=self._config.web_fetch_enabled,
        )
        sub_body, _degrade = render_active_body(spec, arguments)
        env_text = assembled.dynamic

        # Plan Mode 继承时的衔接语。理由：子循环每轮会把 plan_toggle_instruction
        # 与 dynamic() 合并进**同一条** <system-reminder>，于是「SOP 让你按步骤做」
        # 与「Plan 让你先别动手」两段指令会互相拉扯。显式把它们串成一条流程，
        # 模型才知道先规划后执行而不是二选一。
        plan_bridge = (
            "当前处于计划模式：请先依据上述 Skill 指令拟出执行计划并提交审批，"
            "获批后再按该指令执行。"
            if self.plan_mode
            else ""
        )

        def sub_dynamic() -> str:
            parts = [p for p in (env_text, sub_body, plan_bridge) if p]
            # c12 注入通道（子对话侧）。**两处都要接**——只接主对话的话，
            # 一条挂在 `turn_start` 上的注入型 Hook 在 Skill 子对话里会静默失效。
            injected = self._hooks.consume_injections()
            if injected:
                parts.append(injected)
            return "\n\n".join(parts)

        # ── 5. 驱动子 Agent ──
        #
        # 整段（构造子 Agent、跑完子循环）都包在独立作用域里（trace spec F2），
        # 让子对话的模型请求、权限判定、工具执行、循环事件全部标成
        # `isolated:<name>`，读 trace 时一眼就能把它和主对话分开。
        #
        # ① 正常路径为什么有效：生成器的函数体要到 Worker 线程首次 `next()` 时才
        #    开始执行，`with` 的 `__enter__` 就发生在那时、在同一个线程上；
        #    下面驱动子事件流的 for 循环也在生成器体内，所以整段都在作用域内；
        #    生成器耗尽时 `__exit__` 复位。
        # ② **为什么还需要 `_do_stream` 的兜底复位**：异常与「生成器被放弃」的路径下
        #    `__exit__` 可能压根不跑（比如 TUI 退出竞态里 `call_from_thread` 抛
        #    RuntimeError），而 Textual 复用池化线程，泄漏出去的 `isolated:<name>`
        #    会污染后续复用该线程的主对话运行。见 `tui/app.py` 的 `_do_stream`。
        with self._recorder.scope(isolated_scope(spec.command_name)):
            sub_provider = (
                self._provider_for(spec.model) if spec.model else self._provider
            )
            sub_agent = Agent(sub_provider, self._registry, recorder=self._recorder)

            # 取消信号必须**重建**：`request_cancel()` 置的就是 `self._cancel_event`，
            # 而它原本只在 `_run()` 里重建。上一次运行残留的置位会让子对话开局即被取消。
            self._cancel_event = threading.Event()

            registry = self._registry
            stop_reason = StopReason.COMPLETED
            events = sub_agent.run(
                sub_history,
                self.thinking_effort,
                self.plan_mode,          # 继承主对话的 Plan Mode
                assembled.stable,
                sub_dynamic,
                spec.model or self._config.model,
                None,                    # 子对话不写缓存调试日志
                self._engine,
                self._build_ask(),       # 复用同一份确认实现，避免两套规则登记逻辑
                self.clarify_callback,
                self.approve_plan_callback,
                self._cancel_event,
                self._context_manager,
                None,                    # recorder=None：子对话过程不写会话存档
                options=RunOptions(
                    max_iterations=SKILL_MAX_ITERATIONS,
                    record_usage=False,   # 别拿子对话的 usage 污染主历史锚点
                    allow_summary=False,  # 只跑 C8 第一层（F21）
                    # 防嵌套：子对话里看不到加载工具，也调不动它。
                    excluded_tools=self.skill_manager.fork_excluded_tools(),
                ),
            )

            # ── 6. 转发子循环事件，但**拦下 FINISHED** ──
            # 外层要自己收尾（先回流结论再产出 FINISHED），不能让子循环的 FINISHED
            # 提前把 TUI 的流式状态收掉。
            for event in events:
                if event.type == AgentEventType.FINISHED:
                    stop_reason = event.stop_reason
                    continue
                yield event

        # ── 7. 提取结论 ──
        conclusion: Optional[str] = None
        for msg in reversed(sub_history):
            if msg.role == "assistant" and (msg.content or "").strip():
                conclusion = msg.content.strip()
                break

        # `stop_reason != COMPLETED` 这个条件不可省：计划被拒时子历史的最后一条
        # assistant 可能带着非空的前言正文（「我打算这样做：……」），只按
        # 「找最后一条非空 assistant」会把那段前言当成结论回流，用户会以为
        # Skill 执行完了。
        failed = conclusion is None or stop_reason != StopReason.COMPLETED
        if failed:
            conclusion = _ISOLATED_FAILURE_TEXT.get(
                stop_reason, "本次 Skill 未产出结果"
            )

        # ── 8. 结论回流主历史。至此主历史新增恰好两条配对消息 ──
        assistant_msg = Message(role="assistant", content=conclusion)
        if record:
            self.history.append(assistant_msg)
            self.memory_manager.record_message(assistant_msg)
        # 模型路径靠它把结论交回给 `run_forked_for_model`（生成器没有返回值可用）。
        self._last_fork_conclusion = conclusion

        # ── 9. 「未产出」时必须补一条 NOTICE ──
        # 第 6 步拦下了全部 FINISHED，而 TUI 对 COMPLETED 的收尾行**不渲染任何东西**。
        # 不补这条，用户按 Esc 取消后界面会完全没有反应，像是卡住了。
        if failed:
            yield AgentEvent(type=AgentEventType.NOTICE, message=conclusion)

        # ── 10. 自然完成收尾：让 _wrap_events 触发 c9 的笔记钩子（F21）──
        yield AgentEvent(
            type=AgentEventType.FINISHED, stop_reason=StopReason.COMPLETED
        )

    def run_forked_for_model(self, name: str, arguments: str) -> str:
        """
        **模型自行发起**一个 `context: fork` 的 Skill（对齐改造 F8）。

        :param name: Skill 命令名
        :param arguments: 模型传入的参数
        :returns: 子对话跑出的结论文本，由加载工具作为工具结果回灌给模型

        由 `LoadSkillTool.run_fork` 回调进来，运行在 Agent 循环的**串行段**
        （`system_serial` 保证它不在只读并发桶里，见 `tools/base.py` 的说明）。

        ## 与用户触发那条路径的三处差别

        1. **不向主历史追加那两条配对消息**——模型这次动作本身已经在主历史里
           留下了 `assistant(tool_calls)` 与 `tool` 结果一对；再追加一对
           user/assistant 会凭空多出一轮不存在的对话；
        2. 因此也不写会话存档（那两条不存在）；
        3. 结论以**返回值**交回，而不是以事件流回流。

        共用的是：同一份 `_run_forked_skill` 的子对话实现、同一套权限管线、
        同一个 `ask` 确认闭包。

        副作用：跑一整条子对话（可能读写文件、执行命令、弹确认面板）。
        """
        spec = self.skill_manager.get(name)
        if spec is None:
            return f"没有名为 `{name}` 的 Skill。"

        # 复用子对话实现（`record=False`：不碰主历史、不写存档），
        # 就地耗尽它产出的事件流——模型路径不需要事件，只需要最后那段结论。
        self._last_fork_conclusion = ""
        for _ in self._wrap_events(
            self._run_forked_skill(
                spec, arguments, f"/{name} {arguments}".strip(), record=False
            ),
            extra_skill=spec,
            trigger="skill",
            scope=isolated_scope(spec.command_name),
        ):
            pass
        return self._last_fork_conclusion or "本次 Skill 未产出结果。"

    def _build_ask(self) -> "AskFn":
        """
        构造人在回路确认闭包（c11 T44 从 _run 内联提取，逻辑逐字不变）。

        :returns: 循环所需的 `(ToolCall, Tool, DecisionResult) -> bool` 回调

        **抽出来是为了让独立模式子对话复用同一份实现**。子对话同样要走完整
        权限管线、同样可能弹四态确认面板；若各写一份，「本会话放行」的规则
        登记与「永久放行」的落盘就会出现两套逻辑，用户在子对话里选的
        「本会话放行」很可能对主对话不生效——那是极难察觉的不一致。

        副作用：返回的闭包被调用时可能向引擎登记会话规则或写本地配置文件。
        """
        def ask(tool_call: ToolCall, tool: Tool, decision: DecisionResult) -> bool:
            """
            人在回路确认（仅当决策为 ASK 时由循环调用，返回是否执行）。

            - 无确认回调（理论上不该发生）→ fail-closed 拒绝，绝不擅自执行有副作用工具。
            - 否则调用 TUI 四态确认回调，按所选处理：
              本次（ALLOW）→ 放行本次；
              本会话（ALLOW_SESSION）→ 为「该工具 + 本次目标」登记一条会话级 allow 规则后放行，
                  本会话内后续相同调用经引擎直接 ALLOW，不再弹面板；
              永久（ALLOW_PERMANENT）→ 把同样的 allow 规则写入本地级配置（重启仍生效）后放行；
              拒绝（DENY）→ 不执行。

            规则的工具名与匹配模式来自 adapter 的规范化结果（与引擎判断口径一致），
            specifier 直接作为模式（精确匹配本次目标，偏保守、最小授权）。
            """
            if self.confirm_callback is None:
                return False
            choice = self.confirm_callback(tool_call, tool, decision)
            if choice == ConfirmDecision.DENY:
                return False
            if choice == ConfirmDecision.ALLOW:
                return True
            # 本会话 / 永久：构造与本次调用同口径的 allow 规则。
            req = to_request(tool, tool_call.arguments, self._engine.mode)
            # ⚠ 规则的**单一来源**是 adapter.to_allow_rule（web_fetch 扩展 T23）。
            #
            # 原先这里直接 f"{rule_name}({specifier})"，对命令类与路径类是对的，
            # 但对 url 类会写出 `WebFetch(https://example.com/a?token=abc)`：
            # ① 不是合法域名规则，下次启动被加载期处理掉——用户点过的「永久放行」
            # **重启后凭空失效**；② 就算不被处理掉也匹配不上任何东西；
            # ③ **查询参数里的令牌被原样写进配置文件**。
            # 而本次调用因为选了「永久」照常放行了，问题要到下次启动才显形。
            #
            # 下面**三处**（会话级、永久级、永久写入失败的回退）都用它，别只改前两处。
            grant_tool, grant_pattern = to_allow_rule(req)
            rule_string = f"{grant_tool}({grant_pattern})" if grant_pattern else grant_tool
            if choice == ConfirmDecision.ALLOW_SESSION:
                self._engine.add_session_rule(
                    Rule(effect="allow", tool=grant_tool, pattern=grant_pattern, source="session")
                )
                return True
            if choice == ConfirmDecision.ALLOW_PERMANENT:
                if not self._engine.persist_local_rule(rule_string):
                    self._engine.add_session_rule(
                        Rule(effect="allow", tool=grant_tool, pattern=grant_pattern, source="session")
                    )
                return True
            return False

        return ask

    def _run(self, grant_skill: "Optional[SkillSpec]" = None) -> Iterator[AgentEvent]:
        """
        构造一次 Agent 运行并返回其事件流。

        步骤：
        1. 重建取消信号（每次运行独立，避免上次的取消影响本次）。
        2. 采集环境信息并拼装结构化系统提示，分出 stable（可缓存）与 dynamic（动态）两段（c5）。
        3. 计算缓存调试日志路径（debug_log 关闭时为 None）。
        4. 构造 ask 闭包：把 TUI 的四态确认回调封装成循环只需的 bool 接口，并在用户选
           「本会话/永久放行」时把对应 allow 规则登记进引擎（会话级 / 写本地配置，c6 F6）。
        5. 调用 Agent.run，注入历史、思考模式、Plan Mode、系统提示两段、日志路径、权限引擎、
           四类回调与取消信号。

        :returns: Agent 产出的 AgentEvent 事件流

        副作用：重建 self._cancel_event；ask 闭包可能向引擎登记会话规则或写本地配置文件。
        """
        self._cancel_event = threading.Event()

        # 结构化系统提示（c5）：以项目根为工作目录采集环境信息，拼装出稳定/动态两段。
        # stable 逐轮不变 → 走 system 参数命中缓存；dynamic（环境信息）由循环注入 <system-reminder>。
        # Plan Mode 的引导不再在此构造，改由循环按轮节奏注入（见 loop.run / reminders）。
        project_root = str(workspace_root())
        env = collect_environment(self._config, project_root)
        # c9：把 RHINE.md 拼接结果与记忆索引填进 110/130 槽位（两者都可能为空串，
        # 为空时槽位整体跳过，输出与 c8 一致）。索引现读现截断，笔记线程会话中途
        # 更新后，下一条消息就能看到新索引。
        # c11：第一阶段 Skill 清单填进 140 稳定槽位（进前缀缓存）；
        # 已激活正文不在这里填——它必须每轮重算，走下面 dynamic_provider 的动态通道。
        assembled = build_default_prompt(
            env,
            custom_instructions=self.memory_manager.custom_instructions(),
            memory_index=self.memory_manager.memory_index(),
            skill_index=self.skill_manager.index_text(),
            active_skills="",
            # web_fetch 扩展 F4 链路②的第一个调用点（另一个在 _run_forked_skill）。
            untrusted_enabled=self._config.web_fetch_enabled,
        )
        # 一次性动态提醒（c9：恢复会话的时间跨度提醒）：并入本次 dynamic，取走即清，
        # 不进持久历史、不被存档（它是「此刻的环境事实」）。
        base_dynamic = assembled.dynamic
        pending = self.memory_manager.consume_pending_notice()

        def dynamic_provider() -> str:
            """
            产出本轮 <system-reminder> 的动态内容（c11 起循环每轮调一次）。

            **`pending` 在闭包外取一次是刻意的**，不要「优化」成闭包内消费：
            c9 的既有行为就是「取一次拼进字符串，循环每轮重新包一层」，
            也就是同一次运行的每一轮都带着这条提醒。「取走即清」指的是
            「本次运行消费掉、不带到下一条用户消息」，不是「只在第一轮出现」。
            若改成闭包内 `consume_pending_notice()`，第 2 轮起它就变空了——
            那是改变现状而不是保持现状。

            段落顺序：环境信息 → 已激活 Skill → 一次性提醒（spec F9 要求
            Skill 正文排在环境信息之后）。

            已激活 Skill 的正文**每轮现取**（c11 改造点 1）：模型可能在上一轮
            才调 `load_skill`，这一轮就该看见它的 SOP。
            """
            parts = [p for p in (base_dynamic,) if p]
            active = self.skill_manager.active_text()
            if active:
                parts.append(active)
            if pending:
                parts.append(pending)
            # c12 注入通道：Hook 的 `prompt` 动作产物排在最后一段。
            # **必须在这个每轮求值的闭包里取**，不能在闭包外取一次——模型可能在
            # 第 N 轮触发一条注入型 Hook，它要从第 N+1 轮起可见（与 Skill 正文同理）。
            # 「一次性」由 `consume_injections` 的取走即清保证。
            injected = self._hooks.consume_injections()
            if injected:
                parts.append(injected)
            return "\n\n".join(parts)
        # debug_log 开启时把缓存日志写到项目根下的固定文件，否则传 None 关闭日志。
        debug_log_path = (
            str(workspace_root() / ".rhinecode_debug.log") if self._config.debug_log else None
        )

        ask = self._build_ask()

        events = self._agent.run(
            self.history,
            self.thinking_effort,
            self.plan_mode,
            assembled.stable,
            dynamic_provider,
            self._config.model,
            debug_log_path,
            self._engine,
            ask,
            self.clarify_callback,
            self.approve_plan_callback,
            self._cancel_event,
            self._context_manager,
            self.memory_manager.record_message,
            options=RunOptions(),   # 主对话不排除任何工具
        )
        return self._wrap_events(events, extra_skill=grant_skill)

    def _grant_for_skill(self, spec: "SkillSpec") -> None:
        """
        为一个**刚被触发**的 Skill 授予预授权（F11/F12）。

        :param spec: 被触发的 Skill

        由三条触发路径共用：用户敲短命令（`run_skill`）、模型调加载工具
        （经 `on_skill_activated` 回调）、以及 fork 子对话（`_wrap_events`）。
        撤销统一由 `_wrap_events` 的 `finally` 负责。

        副作用：向权限引擎追加本次执行级规则。
        """
        rules, _ = self.skill_manager.grants_for_spec(spec)
        self._engine.grant_turn_rules(rules)

    def on_skill_activated(self, name: str) -> None:
        """
        模型经加载工具激活一个共享模式 Skill 后的回调（由 `LoadSkillTool` 注入调用）。

        必须在这里授权而不是在 `_wrap_events` 起点：模型可能在第 N 轮才激活，
        那时外层的 `try` 早已进入，起点授权错过了它。

        副作用：同 `_grant_for_skill`。
        """
        spec = self.skill_manager.get(name)
        if spec is not None:
            self._grant_for_skill(spec)

    def _wrap_events(
        self,
        events: Iterator[AgentEvent],
        extra_skill: "Optional[SkillSpec]" = None,
        trigger: str = "user",
        scope: Optional[str] = None,
    ) -> Iterator[AgentEvent]:
        """
        Agent 事件流的包装生成器：**预授权的成对授予/撤销** + 自然停止钩子
        + **回合级 Hook 两事件**（c12）。

        :param events: Agent 产出的原始事件流
        :param extra_skill: 本次额外触发的 Skill（`context: fork` 走子对话时它不进
                            激活列表，但它的 `allowed-tools` 同样该生效）
        :param trigger: 本次回合由谁触发，进 `turn_start` 负载：
                        `"user"`（用户消息 / 斜杠命令）或 `"skill"`（Skill 子对话）
        :param scope: 本次回合的作用域，进两个事件的负载。**fork 子对话必须显式传**——
                      子对话的 trace 作用域是在**内层生成器**里进入的，而本方法的
                      `turn_start` 发生在那之前，现取只会拿到 `main`。
                      为 None 时取记录器当前作用域（主对话即 `main`）。
        :returns: 语义完全相同的事件流（仅多了几处副作用）

        ## 为什么授予与撤销放在这里

        本方法是**每一次 Agent 执行的唯一包装点**——主对话、用户触发的子对话、
        模型自行发起的子对话，三条路径都经过它。把 `try/finally` 放在这里，
        spec N3 要求的「取消/出错/迭代上限三种终止都要撤销」就由生成器的
        `finally` 语义自动保证，不需要在每条停止路径上各写一次。

        ⚠️ **生成器的 `finally` 只在生成器被耗尽或关闭时执行。** 消费方提前
        `break` 时靠的是垃圾回收触发 `close()`——这在 CPython 上即时发生，
        但不是语言保证。所有实际消费方（TUI 的 Worker）都会跑到 FINISHED 事件，
        故这里可接受；若将来出现「消费一半就丢弃」的调用方，需要显式 `closing()`。

        ## 自然停止钩子（c9）

        看到 FINISHED 且停止原因为 COMPLETED（自然完成）时，先触发 MemoryManager
        的异步笔记更新再透传——钩子只是「起一个 daemon 线程」，本身不阻塞事件流。
        其它停止原因（取消/出错/迭代上限）不触发笔记：非自然结束的对话大概率
        不完整，不值得沉淀。
        """
        # **只记录起点、不在这里授予**（F12）。授予发生在 Skill 被**触发**的那一刻
        # （`run_skill` 或模型调 `load_skill`），因为共享模式 Skill 是常驻的——
        # 若在这里按激活列表授予，用户跑过一次带写权限的 Skill 之后，
        # 此后每一轮都会重新拿到授权，写操作从此静默免确认而用户毫不知情。
        # **顺序不可调**：先取令牌，再授予。反过来的话令牌把本次授予也圈了进去，
        # `finally` 的回滚就留着它不动——授权于是跟着 Skill 的常驻态一直活下去，
        # 用户跑过一次带写权限的 Skill 之后每一轮都免确认而毫不知情（实测踩过）。
        token = len(self._engine.turn_rules)
        if extra_skill is not None:
            self._grant_for_skill(extra_skill)

        # ── 回合级 Hook（c12）──
        #
        # 搭本方法的车是刻意的：它已经是「每一次 Agent 执行的唯一包装点」，
        # 主对话 / 用户触发的子对话 / 模型自行发起的子对话三条路径都经过。
        # 于是一处接线就覆盖全部，且生成器 `finally` 的语义顺带保证
        # **取消、出错、迭代上限三种非正常终止下 `turn_end` 照样产出**。
        turn_scope = scope or self._current_scope()
        self._dispatch_turn(HookEventType.TURN_START, scope=turn_scope, trigger=trigger)
        # 只有真的有人监听 `turn_end` 才累积正文（spec N7）——否则一整回合的
        # AI 正文会白攒一遍内存。
        track_text = self._hooks.has_listeners(HookEventType.TURN_END)
        text_buffer: list[str] = []
        stop_reason = ""

        try:
            for event in events:
                if track_text:
                    # PROGRESS 表示进入新一轮迭代，正文段落随之翻篇；
                    # 因此缓冲区里留下的始终是**最后一段**正文。
                    if event.type == AgentEventType.PROGRESS:
                        text_buffer.clear()
                    elif event.type == AgentEventType.TEXT:
                        text_buffer.append(event.text)
                if event.type == AgentEventType.FINISHED:
                    stop_reason = (
                        event.stop_reason.value if event.stop_reason is not None else ""
                    )
                    if event.stop_reason == StopReason.COMPLETED:
                        self.memory_manager.on_natural_stop(self.history)
                yield event
        finally:
            # 回滚而非清空：模型在主对话里自行发起子对话时，这里是内层，
            # 清空会连外层那次执行的授权一并抹掉（见 grant_turn_rules 的说明）。
            #
            # **撤销必须排在 Hook 分发之前**：`turn_end` 的动作可能跑上几十秒，
            # 把回滚压在它后面等于让预授权多活那么久。
            self._engine.restore_turn_rules(token)
            self._dispatch_turn(
                HookEventType.TURN_END,
                scope=turn_scope,
                stop_reason=stop_reason,
                last_text="".join(text_buffer),
            )

    def _current_scope(self) -> str:
        """读记录器当前作用域，失败时退回主作用域（观测不该把主流程带下水）。"""
        try:
            return self._recorder.current_scope()
        except Exception:
            return SCOPE_MAIN

    def _dispatch_turn(self, event: "HookEventType", **fields) -> None:
        """
        分发一个回合级事件（c12 spec F2）。

        副作用：可能起子进程 / 发 HTTP 请求；异常一律吞掉——本方法在 `finally`
        里也会被调用，抛出会覆盖掉正在传播的真实异常。
        """
        if not self._hooks.has_listeners(event):
            return
        try:
            self._hooks.dispatch(event, lambda: dict(fields))
        except Exception:
            pass
