"""
应用装配层：把「构造一个可运行的 RhineCode 应用」这件事从命令行入口里抽出来。

## 为什么要抽出来

原先整套装配（十二步：注册表 → Provider → 工具中心 → MCP → Skill → 协调层 → App）
写在 `__main__.main()` 的函数体里，与 argparse、模板生成、`sys.exit` 混在一起。
后果是**测试无法装配一个真实应用**——想验证「装配顺序对不对」「白名单笔误确实
能拦住启动」只能起子进程，而子进程里什么都断言不了。

抽成一个函数后：
- 测试可以在**进程内**调 `build_app(cfg, user_dir=<临时目录>)`，拿到中间组件逐个断言；
- 致命错误从 `print + sys.exit(1)` 变成抛 `BootstrapError`，测试能 `assertRaises`；
- `user_dir` 可参数化，测试不会去读、也不会污染开发者真实的 `~/.rhinecode/`。

## 术语：什么是「工厂函数」

一个「负责把一堆零件按正确顺序组装成成品并交还给你」的函数。它自己不做业务，
只负责创建对象、按依赖关系接线、返回结果。好处是「怎么组装」这件知识收在一处，
调用方（命令行入口、测试、将来可能的其它前端）都复用同一份组装逻辑，
不会出现「测试里的装配顺序和真实启动不一样」这种最坑的偏差。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from rhinecode.agent.prompt.builder import build_default_prompt
from rhinecode.agent.prompt.environment import collect_environment
from rhinecode.agent.prompt.texts import UNTRUSTED_CONTENT
from rhinecode.commands import CommandRegistrationError, build_builtin_registry
from rhinecode.commands.skill_commands import build_skill_command_specs
from rhinecode.config import Config
from rhinecode.conversation import ConversationManager
from rhinecode.hooks import HookEventType, HookManager, NullHookManager
from rhinecode.hooks import load_all as load_hooks
from rhinecode.mcp import config as mcp_config
from rhinecode.mcp.manager import MCPManager
from rhinecode.provider.factory import create_provider
from rhinecode.skills.manager import SkillManager
from rhinecode.skills.models import builtin_skills_dir
from rhinecode.subagents.discovery import discover_agents
from rhinecode.subagents.models import builtin_agents_dir
from rhinecode.subagents.runner import SubAgentRuntime
from rhinecode.subagents.service import SubAgentService
from rhinecode.tools.run_agent import RunAgentTool
from rhinecode.team import TeamService
from rhinecode.tools.load_skill import LoadSkillTool
from rhinecode.tools.send_message import SendMessageTool
from rhinecode.tools.team_tasks import build_board_tools
from rhinecode.todo import TodoStore
from rhinecode.tools.todo_write import TodoWriteTool
from rhinecode.tools.mcp_config import MCPAddServerTool
from rhinecode.tools.web_fetch import WebFetchTool
from rhinecode.tools.web_search import WebSearchTool
from rhinecode.web.manager import WebFetchManager
from rhinecode.web.search import PROVIDERS, check_endpoint
from rhinecode.web.search_manager import WebSearchManager
# c14：装配期定位项目级配置与目录，与「调用者站在哪个工作目录」无关，故取主项目根。
from rhinecode.tools.path_guard import clear_read_roots, main_project_root
from rhinecode.worktree import ProvisionEntry, render_cleanup_notice, scan_and_clean
from rhinecode.tools.registry import ToolRegistry
from rhinecode.trace import (
    NullRecorder,
    TraceEventType,
    TraceRecorderProtocol,
    full_text,
    redact_config,
)
from rhinecode.trace.tracing_provider import TracingProvider
# c16：分类器审查。`is_broad_allow` / `why_broad` 收的是两个字符串，
# 由本层从 `Rule` 上取字段——那是刻意的，见 `classifier/broad.py` 的模块 docstring
# （让分类器包 import `permission` 会连带把引擎与 `rhinecode.tools` 拉起来，
#  它就不再是叶子包了）。
from rhinecode.classifier import (
    ClassifierConfig,
    ClassifierService,
    is_broad_allow,
    is_broad_domain_allow,
    why_broad,
)
from rhinecode.classifier.render import (
    render_broad_domain_warning,
    render_dropped_rules,
)
from rhinecode.permission.rules import RuleSet
from rhinecode.tui.app import RhineApp


class BootstrapError(Exception):
    """
    装配阶段的致命错误：调用方应打印后以退出码 1 终止。

    ⚠️ **`args[0]` 是已经成文的完整 stderr 文案**，调用方原样 `print(e)` 即可，
    **不要再拼任何前缀**。这样设计是因为三段文案是既有启动测试逐字断言的对象
    （见 `tests/test_command_startup.py` / `tests/test_skill_startup.py`），
    把前缀留在异常里能保证「文案的唯一来源」只有一处。
    """


# 三段文案登记为**不可变契约**（改动会让既有启动测试变红，且用户看到的提示会变）：
#   1. f"命令注册冲突：{e}"          —— build_builtin_registry 抛 CommandRegistrationError
#   2. f"Provider 初始化错误：{e}"   —— create_provider 抛 ValueError
#
# （C11 的第三段「Skill 白名单笔误」已随对齐改造删除：`allowed-tools` 现在是
#  预授权声明，无法识别的项只警告不致命，启动不会再因它失败。）


@dataclass(frozen=True)
class BuildResult:
    """
    一次装配的产物。

    :param app: 可 `run()` 的 Textual 应用
    :param cleanup: 幂等的清理闭包，签名 `cleanup(reason: str = "normal_exit")`
    :param manager: 协调层实例
    :param tool_registry: 工具注册中心
    :param command_registry: 命令注册表
    :param skill_manager: Skill 编排者
    :param mcp_manager: MCP 连接管理器
    :param recorder: 本次装配使用的行为记录器

    后六个字段是**为测试断言而暴露**的中间组件：验证「关闭记录时链路上没有中间层」
    需要拿到 manager 手里的 provider、验证「白名单笔误时 MCP 未被连接」需要拿到
    mcp_manager、验证「装配期产出了 Skill 状态事件」需要拿到 recorder。
    真实启动只用 `app` 与 `cleanup` 两个。
    """

    app: Any
    cleanup: Callable[..., None]
    manager: ConversationManager
    tool_registry: ToolRegistry
    command_registry: Any
    skill_manager: SkillManager
    mcp_manager: MCPManager
    recorder: TraceRecorderProtocol


def build_app(
    cfg: Config,
    *,
    user_dir: Optional[Path] = None,
    resume_latest: bool = False,
    recorder: Optional[TraceRecorderProtocol] = None,
    provider_factory: Optional[Callable[[Config], Any]] = None,
    exclude_tools: frozenset = frozenset(),
    web_client_factory: Optional[Callable[[], Any]] = None,
    web_resolver: Optional[Callable[[str], list]] = None,
    hook_client_factory: Optional[Callable[[], Any]] = None,
    search_client_factory: Optional[Callable[[], Any]] = None,
) -> BuildResult:
    """
    按固定顺序装配一个完整的 RhineCode 应用。

    :param cfg: 已加载校验过的配置
    :param user_dir: 用户级目录；缺省 `Path.home() / ".rhinecode"`（等于现状）。
                     给定时用户级项目指令 / 记忆 / Skill / 权限规则 / MCP 声明
                     五类内容一并改从该目录读取
    :param resume_latest: 对应 `rhine --continue`：启动时恢复最近的未锁定会话。
                          **不可漏传**——漏了 `--continue` 会静默失效
    :param recorder: 行为记录器；缺省 `NullRecorder()`（关闭记录）
    :param provider_factory: 「怎么造一个模型客户端」的可替换实现，签名
                     `(Config) -> BaseProvider`；缺省 `create_provider`（等于现状）。
                     这是**依赖注入**：端到端驱动设施靠它把整条链路换成假模型，
                     而产品代码里一个 `if 测试模式` 都不必写。
                     **本参数会一路透传给 `ConversationManager`**——协调层的
                     `_provider_for`（Skill 指定 `model:` 时的换模型旁路）自己会再造
                     一次 Provider，不透传的话那条旁路会绕过假模型静默连上真实网络。
    :param exclude_tools: 装配完成后要从工具注册中心摘掉的工具名集合；
                     缺省空集（等于现状）。用于把「会突破测试隔离」的工具拿掉，
                     摘除位置见下方那段窄窗口注释。
    :param web_client_factory: 造 HTTP 客户端的工厂，透传给 `web_fetch` 工具。
                     缺省 None（用真 `httpx.Client`）。**可注入是 spec N5 的硬要求**——
                     端到端场景也要能离线跑，形态与 `provider_factory` 完全一致。
    :param web_resolver: 主机名解析函数，同上（缺省用 `socket.getaddrinfo`）。
    :param hook_client_factory: 造 HTTP 客户端的工厂，透传给 Hook 的 `http` 动作。
                     缺省 None（用真 `httpx.Client`）。形态同 `web_client_factory`。
    :param search_client_factory: 造 HTTP 客户端的工厂，透传给 `web_search` 工具。
                     缺省 None（用真 `httpx.Client`）。形态同 `web_client_factory`
                     ——**可注入同样是 spec N5 的硬要求**，而且这里的安全含义更重：
                     测试套件不该因为跑测试就把查询词发给第三方服务商。
    :returns: BuildResult

    :raises BootstrapError: 三类致命配置错误（命令注册冲突 / Provider 初始化失败 /
                            Skill 白名单笔误）。**本函数内不出现 `sys.exit`**，
                            以便测试能捕获（trace spec F22）

    副作用（不少，按发生顺序）：
    - 读三层权限配置、两层 MCP 配置、三级 Skill 目录、三层 RHINE.md
    - 连接外部 MCP Server（可能拉起 stdio 子进程、发起网络请求）
    - 在项目根建 `.rhinecode/sessions/` 并取会话锁
    - 往 path_guard 的进程级只读白名单注册三个目录
    """
    user_dir = user_dir if user_dir is not None else Path.home() / ".rhinecode"
    recorder = recorder if recorder is not None else NullRecorder()

    # ① 命令注册表：必须在 Provider / 工具注册中心 / MCP 连接等昂贵资源之前完成——
    # 命令名/别名冲突属于代码级配置错误，fail-fast 在此拦下，此时尚未创建网络连接、
    # MCP 子进程或会话锁，启动失败干净利落（c10 spec F2/N4）。
    try:
        command_registry = build_builtin_registry()
    except CommandRegistrationError as e:
        raise BootstrapError(f"命令注册冲突：{e}") from e

    # ② 模型 Provider。
    # 工厂可被调用方替换（依赖注入）；不传时逐字等于原来的 `create_provider(cfg)`。
    # 注意 ValueError 的捕获与 BootstrapError 的文案**一字不改**——那是既有启动测试
    # 逐字断言的不可变契约（见本文件顶部的三段文案登记）。
    factory = provider_factory or create_provider
    try:
        provider = factory(cfg)
    except ValueError as e:
        raise BootstrapError(f"Provider 初始化错误：{e}") from e

    # ②' 开启记录时，给 Provider 套一层装饰器壳（装饰器模式，见 tracing_provider.py）。
    # **仅在 recorder.enabled 时包装**：关闭记录时链路上不得有任何中间层
    # （trace spec AC3——「零侵入」不是「开销很小」，而是「结构上根本不存在」）。
    if recorder.enabled:
        provider = TracingProvider(provider, recorder, cfg.model)

    # ③ 工具注册中心（含核心工具）。命名为 tool_registry 与 command_registry
    # 明确区分（两者互不相干）。
    tool_registry = ToolRegistry.default()

    # ④ MCP 配置加载 + 管理器创建。connect_all 留到 Skill 第一阶段之后。
    mcp_configs, mcp_errors = mcp_config.load_all(user_dir=user_dir)
    mcp_manager = MCPManager()
    # mcp_add_server 需要同时写配置、重载目标 server、更新 registry，因此必须在
    # MCPManager 创建后注入运行时依赖；只读的 mcp_resolve_server 已在
    # ToolRegistry.default() 中注册。
    tool_registry.register(MCPAddServerTool(mcp_manager, tool_registry))

    # ④' 网络访问工具（web_fetch 扩展 F4）。位置卡在一个窄窗口里，两头都不能挪：
    #
    # **必须在第②步 Provider 之后**——manager 要持它，而且要持的是**已经被
    # `TracingProvider` 包过的那一个**（第②'步）。拿到未包装的那个，抽取请求
    # 就不会进行为记录，`--scope web_extract` 永远是空的。
    #
    # **必须在 `exclude_tools` 摘除与 `session_start` 快照之前**——摘除要能摘到它；
    # 快照里的 `tool_names` 要与实际工具集一致（理由同下方那段窄窗口注释）。
    #
    # 它不能进 `ToolRegistry.default()`：那里造不出 provider（同 MCPAddServerTool）。
    if cfg.web_fetch_enabled:
        web_manager = WebFetchManager(
            provider,
            cfg.context_window,
            recorder=recorder,
            client_factory=web_client_factory,
            resolver=web_resolver,
        )
        tool_registry.register(WebFetchTool(web_manager))

    # ④'' 网络搜索工具（web_search 扩展 F4/F17/F27）。
    #
    # **位置约束与 ④' 完全相同**：必须在 `exclude_tools` 摘除与 `session_start`
    # 快照之前（摘除要能摘到它；快照里的 `tool_names` 要与实际工具集一致）。
    #
    # ⚠ 但它**没有**「必须在第②步 Provider 之后」那条约束——搜索结果不经过
    # 二次抽取（spec F18），`WebSearchManager` 完全不碰 LLM。
    #
    # 两条启动提示（端点非 https / 未配置密钥）在这里**只是攒起来**，
    # 到第 ⑤ 步协调层建好之后再统一发——`add_startup_notice` 是协调层的方法，
    # 而协调层此刻还不存在。
    search_notices: list[str] = []
    if cfg.search_enabled:
        # ⚠ **用 `.get` 而不是 `[]`**：`config.load()` 已经校验过服务商名，
        # 但那不是唯一的构造路径——直接 `Config(search_provider="x")` 会绕过它
        # （测试与嵌入式调用都这么干）。硬索引的后果是一个 `KeyError` 从装配层
        # 冒出来，而 `BootstrapError` 才是本函数的「致命配置错误」通道。
        provider_spec = PROVIDERS.get(str(cfg.search_provider or ""))
        if provider_spec is None:
            raise BootstrapError(
                f"search.provider 不认识的搜索服务商：{cfg.search_provider}。"
                f"目前支持：{', '.join(sorted(PROVIDERS))}"
            )
        endpoint = cfg.search_endpoint or provider_spec.endpoint

        # 端点协议校验（spec F17）。**这里抛错而不是降级**：一个 `file://` 端点
        # 会让搜索工具变成文件读取工具、绕过第②层路径沙箱，与②′的协议限制同性质。
        # 校验放装配层而不是 `config.py`，是为了保持配置层只做类型解析，
        # 且 `BootstrapError` 本来就是既有的「致命配置错误」通道。
        bad = check_endpoint(endpoint)
        if bad is not None:
            raise BootstrapError(f"搜索端点配置错误：{bad}")
        if not endpoint.lower().startswith("https://"):
            # 非 https 是**合法**的（内网搜索代理常常是 http），但值得说一句：
            # 查询词是明文外发的数据。
            search_notices.append(
                f"提示：搜索端点 {endpoint} 不是 https，查询词将以明文发送。"
                "若这是内网搜索代理，可以忽略本条。"
            )
        if not cfg.search_api_key:
            # spec F27：工具**照常注册**（用户明确选择了「调用时返回可读错误」
            # 而不是「工具凭空消失」），但启动时要说清楚。
            # 两条出口服务不同的人：这条给用户，工具的失败文案给模型。
            search_notices.append(
                "提示：网络搜索已启用但未配置密钥，模型调用它只会拿到一条说明。"
                "请在 config.yaml 的 search.api_key 里填入密钥，"
                "或设 search.enabled: false 关掉这个能力。"
            )

        search_manager = WebSearchManager(
            provider_spec,
            cfg.search_api_key,
            cfg.search_endpoint,
            cfg.search_max_results,
            cfg.search_session_quota,
            cfg.search_timeout,
            client_factory=search_client_factory,
        )
        tool_registry.register(WebSearchTool(search_manager))
    else:
        search_manager = None

    # ── Skill 系统第一阶段（c11 T57）：扫盘 + 白名单严格校验 ──
    #
    # **位置为什么卡在这个窄窗口里**（`MCPAddServerTool` 注册之后、
    # `connect_all` 之前），两头都不能挪：
    #
    # 往前挪不行：`mcp_add_server` 是内置工具，但名字是**单**下划线 `mcp_`，
    # 不匹配 `mcp__` 判别式，所以它会落进第一段的**严格**校验。而它比其它内置
    # 工具晚注册（依赖 MCPManager 实例）。若把校验放在「核心工具注册完成」这个
    # 看似自然的位置，一个白名单写了 `mcp_add_server` 的合法 Skill 会被误判成
    # 笔误并硬终止启动。
    #
    # 往后挪不行：`connect_all` 会拉起 stdio 子进程、建立网络连接。此刻退出
    # 干净利落，一个子进程都还没起，不会留下孤儿进程。
    #
    # （这段注释随代码从 `__main__.py` 迁来。它是 C11 留下的唯一记载——
    #  迁走代码却把理由留在原地，等于把知识丢了。）
    skill_manager = SkillManager(
        main_project_root(),
        user_dir,
        builtin_skills_dir(),
        has_short_command=command_registry.has_skill_command,
        recorder=recorder,
    )
    # load_skill 在这里注册而不是在 ToolRegistry.default() 里：它依赖
    # SkillManager 实例，且 tools/registry.py 若导入 tools/load_skill.py
    # 会把 tools ↔ skills 的包级互依变成真环（见 tools/__init__.py 的说明）。
    #
    # 注册它的位置不再有额外约束——白名单 fail-fast 已随对齐改造删除
    # （`allowed-tools` 现在是预授权声明，无法识别的项只警告不致命）。
    load_skill_tool = LoadSkillTool(skill_manager)
    tool_registry.register(load_skill_tool)

    skill_manager.startup()

    # ── exclude_tools 的摘除：**必须在 `session_start` 快照之前** ──
    #
    # 快照里的 `tool_names` 是断言「工具确实被摘掉了」的依据，摘除若发生在快照
    # 之后，快照就会与实际工具集不符——观测设施撒谎，且不报错。
    #
    # 历史注记：C11 时这里还有另一半约束——「不能挪到 known_tools 计算之前，
    # 否则白名单里写了 `mcp_add_server` 的 Skill 会被判成笔误并 fail-fast」。
    # 对齐改造把白名单 fail-fast 整个删除后，那半段不再成立，已一并删去。
    for name in sorted(exclude_tools):
        tool_registry.unregister(name)

    mcp_manager.connect_all(mcp_configs, tool_registry, extra_errors=mcp_errors)

    # ── Skill 系统第二阶段：工具集快照 + 短命令注册 ──
    # 此刻远端工具已经注册进 tool_registry，快照才是完整的。
    skill_manager.bind_tools(registered=tool_registry.names())
    command_registry.replace_skill_commands(
        build_skill_command_specs(skill_manager.command_infos())
    )
    # 注意：这里**刻意不打印**「短命令冲突 / 字段提示 / 发现项目级 Skill」这三类
    # 状态信息。启动阶段的 print 发生在 Textual 接管屏幕之前，会被 alternate screen
    # 整个盖住，用户要等到退出程序才在终端里看见——那时早已失去意义。
    # 三类信息全部改由 `/skills` 报告承载（见 SkillManager.report）。

    # ④'' Hook 系统（c12）。
    #
    # **位置在协调层之前、MCP 连接之后**，两条理由：
    # - 必须早于第 ⑤ 步：协调层要把它一路透传给 Agent 与 ContextManager；
    # - 不必更早：Hook 与工具注册、Skill 扫盘、MCP 连接**完全无关**，
    #   它只读自己的两层 YAML。放在这里让「加载配置 → 构造 → 注入」三步挨着，
    #   读代码的人不必在四百行里来回找。
    #
    # 加载失败不阻断启动（fail-safe）：坏配置降级为空规则集 + 一条警告，
    # 警告经协调层的 `_compose_startup_notice` 展示在首屏。
    hook_rules, hook_warnings = load_hooks(user_dir=user_dir)
    hook_manager: Any = (
        HookManager(
            hook_rules,
            hook_warnings,
            recorder=recorder,
            client_factory=hook_client_factory,
            user_path=str(user_dir / "hooks.yaml"),
            project_path=str(main_project_root() / ".rhinecode" / "hooks.yaml"),
        )
        if (hook_rules or hook_warnings)
        # 两层配置都没有内容时用空对象：全部分发变成零成本空操作，
        # 且负载构造器一次都不会被执行（spec F13 缺省零行为）。
        else NullHookManager()
    )

    # ⑤ 协调层。resume_latest 透传 --continue：构造时经 MemoryManager 恢复最近会话（c9）。
    manager = ConversationManager(
        provider,
        cfg,
        tool_registry,
        mcp_manager=mcp_manager,
        resume_latest=resume_latest,
        skill_manager=skill_manager,
        user_dir=user_dir,
        recorder=recorder,
        # **必须透传**：协调层的 `_provider_for` 会在 Skill 声明 `model:` 时自己再造
        # 一个 Provider。那条旁路在协调层内部，本函数第②步包住的那一层管不到它——
        # 不透传的话，一个指定了模型的 Skill 会绕过假模型、静默连上真实网络。
        provider_factory=provider_factory,
        hook_manager=hook_manager,
    )

    # 把「跑一个 fork Skill」的能力回注给加载工具（对齐改造 F8）。
    #
    # **必须在这里、不能提前**：工具要在第 ④ 步之前注册（`bind_tools` 才数得到
    # 它），而能跑子对话的协调层要到第 ⑤ 步才存在。属性注入让两者的构造顺序解耦，
    # 与 `memory_manager.notify` / `skill_manager.notify_activation` 是同一形态。
    load_skill_tool.run_fork = manager.run_forked_for_model
    load_skill_tool.on_activated = manager.on_skill_activated

    # ⑤' 网络搜索的两处回填（web_search 扩展 F13/F27）。
    #
    # **必须在协调层建好之后**：`add_startup_notice` 与配额复位的挂点都在它身上。
    # 第 ④'' 步只是把两条提示攒进 `search_notices`，真正发出来在这里。
    for notice in search_notices:
        manager.add_startup_notice(notice)
    # 属性注入，与 `manager.classifier` / `load_skill_tool.run_fork` 同一形态：
    # `/clear` 要把会话级搜索配额清零（spec F13——那正是「新一次会话」的语义所在）。
    manager.web_search_manager = search_manager

    # ⑤″ 安全审查分类器（c16）。
    #
    # **位置卡在协调层之后、子 Agent 装配之前**，两头都有理由：
    # - 不能提前：本段要读 `manager.permission_engine` 的规则集（丢弃过宽的
    #   命令放行规则），而引擎是协调层构造的；
    # - 不能推后到子 Agent 之后：`SubAgentRuntime` 要拿到 service 才能让
    #   子 Agent 也走分类器（spec F23——否则主 Agent 只要把「跑 git push」
    #   委派出去就绕过了整层）。
    #
    # 与 `tool_registry is not None` 同生共死：分类器只审查三类工具动作，
    # 非工具模式下那三个工具压根不注册，整段没有意义。
    classifier_service = None
    if tool_registry is not None and cfg.classifier_enabled:
        # 分类器用**独立的 Provider 副本**：模型可能不同（`classifier.model`），
        # 且它必须带超时——只在调用方计时是假超时（见 `provider/deepseek.py`）。
        #
        # ⚠ 走 `factory` 而不是 `create_provider`：第 ② 步那层依赖注入的全部
        # 意义就是「测试能换掉真实网络」，这里绕过去的话，一次端到端测试会
        # **静默连上真实网络**——`_provider_for` 那条旁路踩过同一个坑。
        # ⚠ **显式构造而不是 `dataclasses.replace(cfg, ...)`**，两条理由：
        #
        # ① `replace` 要求真正的 dataclass 实例，而本函数的 `cfg` 在若干启动
        #    测试里是 `MagicMock`——那会当场 `TypeError` 把整个启动炸掉。
        # ② 更要紧的是**说清楚分类器继承了什么**。它只需要「连得上哪个模型」
        #    这四样加一个超时；`debug_log` / `context_window` / `web_fetch_enabled`
        #    这些对一次性判定毫无意义，复制过去只会让人以为它们有用。
        classifier_cfg = Config(
            protocol=cfg.protocol,
            # 留空时跟主对话同一个模型（spec F25）。
            model=cfg.classifier_model or cfg.model,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            # ⚠ 超时必须落到 SDK 客户端上——只在调用方计时是假超时，
            # 第一个数据块永远不到达时外面的计时器一点用都没有。
            request_timeout=cfg.classifier_timeout,
        )
        try:
            classifier_provider = factory(classifier_cfg)
        except Exception as exc:  # noqa: BLE001
            # ⚠ **整段 fail-safe**：分类器建不起来绝不能阻断启动。
            # 但**必须说出来**——静默不启用等于静默关掉一层安全机制，
            # 用户会以为它在保护自己（与「熔断必须可见」是同一条原则）。
            manager.add_startup_notice(
                f"警告：安全审查分类器未能启动（{exc}），本次运行不做分类器审查。"
            )
        else:
            if recorder.enabled:
                classifier_provider = TracingProvider(
                    classifier_provider, recorder, classifier_cfg.model
                )
            classifier_service = ClassifierService(
                classifier_provider,
                ClassifierConfig(
                    enabled=True,
                    model=classifier_cfg.model,
                    timeout=cfg.classifier_timeout,
                ),
                recorder=recorder,
            )
            manager.classifier = classifier_service

            # F20/F21：丢弃过宽的命令放行规则并逐条告知。
            #
            # ⚠ **只动 `file_ruleset`**。会话级与本次执行级规则不受影响——
            # 确认面板生成的规则用的是**完整命令串原文**
            # （`permission/adapter.py` 的 `to_allow_rule` 对命令类返回
            # `request.specifier`），天然是窄的；而 `policy_ruleset` 是域名
            # 白名单，spec F22 明确不动它（丢弃会连带改变②′层的行为）。
            engine = manager.permission_engine
            kept, dropped = [], []
            # F22：全域名放行规则**只提醒、不丢弃**（见下方 `broad_domains` 的说明）。
            broad_domains: list[str] = []
            for rule in engine.file_ruleset.rules:
                # ⚠ 用**伞函数** `is_broad_allow` 而不是 `A(...) or B(...)`：
                # 这里是本模块唯一的调用点，写成两个调用的话，将来加第三类时
                # **漏加一个 `or` 不会报错**——只表现为某一类规则悄悄不再被
                # 丢弃，而那等于对那一类静默关掉整层审查。
                #
                # ⚠ `include_search` 由 `cfg.search_enabled` 把门（spec F4）：
                # **关掉的能力不该影响用户的规则文件**——搜索关着的时候
                # 丢掉一条 `allow: WebSearch` 只会产生一条让人困惑的启动提示。
                if rule.effect == "allow" and is_broad_allow(
                    rule.tool, rule.pattern, include_search=cfg.search_enabled
                ):
                    text = f"{rule.tool}({rule.pattern})" if rule.pattern else rule.tool
                    dropped.append((text, rule.source, why_broad(rule.tool, rule.pattern)))
                else:
                    kept.append(rule)
                    # F22：全域名放行规则（`WebFetch(domain:*)` 与整工具
                    # `WebFetch`）同样会让结论停在③层，于是**分类器对网络访问
                    # 零次调用**——那一整类审查被静默关掉。
                    #
                    # ⚠ **只提醒，不丢弃**，与命令类刻意不同：域名规则同时承担
                    # 「建立域名白名单」的语义（②′层用 `policy_ruleset
                    # .has_allow_for` 判断用户有没有声明过白名单），丢掉它会让
                    # 「白名单未建立」重新成立，未列出的域名从 DENY 退回 ASK
                    # ——**那是放宽**。所以它走 `is_broad_domain_allow` 而**不进**
                    # `is_broad_allow` 伞函数，且这一支排在 `kept.append` 旁边、
                    # 不碰 kept/dropped 的分流。
                    #
                    # ⚠ 由 `cfg.web_fetch_enabled` 把门，与上面 `include_search`
                    # 同一条理由（F4）：**关掉的能力不该影响用户的规则文件**
                    # ——网络访问关着的时候，为一条 `WebFetch(domain:*)` 提醒
                    # 「分类器对网络访问不生效」只会让人困惑。
                    if (
                        cfg.web_fetch_enabled
                        and rule.effect == "allow"
                        and is_broad_domain_allow(rule.tool, rule.pattern)
                    ):
                        broad_domains.append(
                            f"{rule.tool}({rule.pattern})" if rule.pattern else rule.tool
                        )
            if dropped:
                engine.file_ruleset = RuleSet(kept)
                manager.add_startup_notice(render_dropped_rules(dropped))
            if broad_domains:
                # 与丢弃说明同一个出口（B2 的要求）：两者都是「你写的规则和你
                # 以为的不一样」，分两个通道说会让其中一条显得不那么要紧。
                manager.add_startup_notice(render_broad_domain_warning(broad_domains))

    # ⑤' 子 Agent 系统（c13）。
    #
    # **位置卡在协调层之后、`session_start` 快照之前**，两头都不能挪：
    # - 不能提前：`SubAgentRuntime` 要拿协调层的 `_provider_for`（角色可指定
    #   `model:`）、`_engine`、以及「造一个新 ContextManager」的工厂，
    #   这些都要到第 ⑤ 步才存在；
    # - 不能推后：`run_agent` 必须在 `session_start` **之前**注册进工具中心，
    #   否则快照里的 `tool_names` 与实际工具集不符——观测设施撒谎，且不报错。
    #   （与上面 `exclude_tools` 摘除是同一条理由。）
    #
    # 与 `load_skill` 同样采用属性注入解耦构造顺序：服务在协调层之后建好，
    # 再回填给协调层。
    # c14 F19：隔离工作区的启动清理。
    #
    # **位置**：在配置加载之后（要读 cleanup_days）、任何子 Agent 可能启动之前。
    # 后者不是理论顾虑——清理会真的删目录，而一个正在写文件的子 Agent
    # 撞上它就是数据丢失。放在启动期则**竞态从根上不存在**：那一刻不可能有
    # 子 Agent 在跑（服务还没建出来）。
    #
    # ⚠ **整段 fail-safe**：清理是空间回收的增强项，失败绝不能阻断启动。
    # `scan_and_clean` 内部已经对每个条目单独兜底，这里再包一层是纵深防御——
    # 它连「根目录本身不可读」这种情形也要吞掉。
    worktree_cleanup = None
    try:
        worktree_cleanup = scan_and_clean(
            main_project_root(), cfg.worktree_cleanup_days, recorder
        )
    except Exception:  # noqa: BLE001
        worktree_cleanup = None
    if worktree_cleanup is not None and not worktree_cleanup.is_empty:
        manager.add_startup_notice(render_cleanup_notice(worktree_cleanup))

    if tool_registry is not None:
        # ── c15：协作服务 ──
        #
        # 位置在子 Agent 装配**之前**：`SubAgentService` 要拿它做队员命名，
        # `SubAgentRuntime` 要拿它给运行器（待命与消息注入都靠它）。
        #
        # 它与 `tool_registry is not None` 同生共死——协作能力只在
        # DeepSeek 工具模式下有意义（消息与清单都是给工具用的），
        # 非工具模式下整段不执行，五个工具也就不会被注册。
        team_service = TeamService(recorder=recorder)
        manager.team_service = team_service
        for tool in build_board_tools(team_service):
            tool_registry.register(tool)
        tool_registry.register(SendMessageTool(team_service))

        # todo-list 扩展：主对话的待办清单。
        #
        # 与协作服务同一条件、同一形态——它同样只在 DeepSeek 工具模式下
        # 有意义（清单是给工具用的）。
        #
        # ⚠ **不注册时 `manager.todo_store` 保持 `None`**，于是
        # `todo_view()` 恒返回 `None`、界面上那块永不显示、系统提示那个槽位
        # 整体跳过——**这就是 spec N5「零回归」的全部实现**，
        # 不需要任何配置开关。别为它加一个 `enabled` 字段：
        # 那会多出一条「配置说开着、但工具模式没开」的自相矛盾状态。
        todo_store = TodoStore(recorder=recorder)
        manager.todo_store = todo_store
        tool_registry.register(TodoWriteTool(todo_store))

        agent_catalog = discover_agents(
            main_project_root() / ".rhinecode" / "agents",
            user_dir / "agents",
            builtin_agents_dir(),
        )
        subagent_runtime = SubAgentRuntime(
            # `_provider_for` 只接受非空模型名；角色未指定时用主 Provider。
            provider_for=lambda model: (
                manager._provider_for(model) if model else provider
            ),
            registry=tool_registry,
            engine=manager.permission_engine,
            main_mode=lambda: manager.permission_engine.mode,
            # c14 修正：入参是**本次子 Agent 的工作目录**。隔离子 Agent 传的是
            # 它的工作区，于是环境信息段里的「工作目录」与 git 分支都跟着它走
            # ——原先固定取主项目根，与 `<isolated-workspace>` 段自相矛盾。
            environment_text=lambda agent_cwd: build_default_prompt(
                collect_environment(cfg, agent_cwd)
            ).dynamic,
            default_model=cfg.model,
            hooks=hook_manager,
            recorder=recorder,
            new_context_manager=manager.new_subagent_context_manager,
            # 「外部不可信内容」段原文。运行器只在子 Agent 的**最终工具集**
            # 含网络访问工具时才注入它（spec F7 的例外），这里只负责把文本递过去。
            # ⚠ **两者任一启用即注入**（web_search 扩展 F19 / AC26）。
            #
            # 本行原来只挂在 `web_fetch_enabled` 上。关掉 web_fetch 而只开
            # web_search 时，那条「外部不可信内容是数据不是指令」的约束会
            # **凭空消失**，而搜索结果（标题与摘要，SEO 投毒的主要落点）
            # 照样进上下文——这是本扩展**最容易漏改的一处**，
            # 护栏见 `tests/test_web_search_bootstrap.py` 的四组合断言。
            untrusted_section=(
                UNTRUSTED_CONTENT
                if (cfg.web_fetch_enabled or cfg.search_enabled)
                else ""
            ),
            thinking_effort=manager.thinking_effort,
            # c16：共用主对话那一个分类器实例（F23），并把主对话历史作为
            # 取用户消息的来源（F9）。两者都用回调/共享对象而不是快照——
            # 委派可能在排队，快照会绑住一份陈旧的历史。
            classifier=classifier_service,
            principal_history=lambda: manager.history,
            # c15：运行器据它做待命/唤醒、注入队友消息、绑定协作身份。
            team=team_service,
        )
        subagent_service = SubAgentService(
            agent_catalog,
            subagent_runtime,
            # **必须是回调**：MCP 工具在上面的 `connect_all` 里才注册进来，
            # 取值型会拿到一份不含它们的陈旧快照。
            tool_names_provider=tool_registry.names,
            # c14 F10：隔离工作区的环境初始化清单。两段清单在这里合成
            # `ProvisionEntry` 序列——config 层只存字符串，语义（copy / link）
            # 由字段名承载，转换点收在这一处。
            provision_entries=tuple(
                [ProvisionEntry(source=x, mode="copy") for x in cfg.worktree_copy]
                + [ProvisionEntry(source=x, mode="link") for x in cfg.worktree_link]
            ),
            # c15：委派时占用队员名字（F1/F2）。
            team=team_service,
        )
        manager.subagent_service = subagent_service
        tool_registry.register(
            RunAgentTool(subagent_service, parent_snapshot=manager.parent_snapshot)
        )

    # ⑥ 界面层。
    app = RhineApp(manager, cfg, command_registry, recorder=recorder)

    # ⑦ 装配期事件：必须在 connect_all + bind_tools **之后**产出，否则工具清单与
    # MCP 状态都还是半空的快照，读 trace 的人会以为「启动时就没连上」。
    #
    # 由此带来一个要知道的事实：`session_start` **不是记录文件里的第一条事件**——
    # 上面 `bind_tools` 的 `skill_state` 会排在它前面。这是「快照必须完整」的
    # 必然代价，不是 bug。读 trace 时把 `session_start` 当作「装配完成」的标记，
    # 而不是「进程起点」。
    recorder.emit_lazy(
        TraceEventType.SESSION_START,
        lambda: {
            "project_root": str(main_project_root()),
            "user_dir": str(user_dir),
            "config": redact_config(cfg),
            "permission_mode": manager.permission_mode_value,
            "plan_mode": manager.plan_mode,
            "tools_enabled": manager.tools_enabled,
            "tool_names": sorted(tool_registry.names()),
            # MCP 连接结果**不单独产事件**——由本快照承载。新增一个事件类型只为
            # 记一次性的连接结果，会让「十五类事件」这个契约白白多一类。
            "mcp_status": [
                {
                    "name": s.name,
                    "kind": s.kind,
                    "connected": s.connected,
                    "tool_count": s.tool_count,
                    "error": s.error,
                }
                for s in mcp_manager.states
            ],
            # Skill 清单快照直接取「实际注入模型的那段清单文本」，而不是另造一份
            # 结构化摘要：读 trace 时最想确认的正是「模型到底看到了哪些 Skill」。
            "skills_index": full_text(skill_manager.index_text()),
            "resume_latest": resume_latest,
        },
    )

    # ⑦' 会话级 Hook：`session_start`（c12 spec F2）。
    #
    # 排在 trace 的 `session_start` **之后**，理由相同——此刻工具清单、MCP 状态、
    # Skill 都已就位，Hook 命令看到的是一个装配完成的系统。
    if hook_manager.has_listeners(HookEventType.SESSION_START):
        try:
            hook_manager.dispatch(
                HookEventType.SESSION_START, lambda: {"source": "startup"}
            )
        except Exception:  # noqa: BLE001 —— 自动化设施绝不能阻断启动
            pass

    # ⑧ 启动恢复的历史事件。**启动恢复不经任何事件流**——它在 ConversationManager
    # 构造期间由 MemoryManager.startup 原地改写 history，一条事件都不产生。
    # 若不在这里单独补一条，trace 里会凭空出现一段历史而没有任何事件解释它的来源。
    if resume_latest and manager.history:
        recorder.emit(
            TraceEventType.HISTORY_RESTORED,
            origin="startup",
            message_count=len(manager.history),
            session_id=manager.memory_manager.session_id,
        )

    # 幂等标志用单元素列表而不是 nonlocal 布尔：两者都行，列表更直观地表明
    # 「这是个被闭包共享的可变容器」。
    done = [False]

    def cleanup(reason: str = "normal_exit") -> None:
        """
        统一回收资源，**五步固定顺序**且**幂等**。

        :param reason: 结束原因，写进 session_end 事件

        顺序理由：
        ① session_end 必须在 recorder.close() 之前——句柄关了就写不进去了；
        ② memory_manager.close() 释放会话锁（不释放会短暂挡住其它实例接管，
           直到锁过期自愈），单独 try 住以免它的异常挡住后面几步；
        ③ mcp_manager.close_all() 回收 MCP 连接与 stdio 子进程；
        ④ clear_read_roots() 复位进程级只读白名单——同一进程内连续装配时，
           不清理会让第二次继承第一次注册的目录（trace spec F24）；
        ⑤ recorder.close() 关闭记录文件句柄。

        为什么需要幂等守卫：其余四步本身天然幂等（重复关闭都有守卫），
        但 `session_end` 重复产出会破坏「一次运行一份记录」的可读性——
        读的人会以为程序结束了两次。
        """
        if done[0]:
            return
        done[0] = True

        recorder.emit(
            TraceEventType.SESSION_END,
            reason=reason,
            turn_total=recorder.turn_total(),
            elapsed_seconds=round(recorder.elapsed(), 3),
        )
        # 会话级 Hook：`session_end`（c12）。位置在 trace 的 session_end 之后、
        # `recorder.close()` 之前——它自己的 `hook_execute` 事件还要写进同一份记录。
        try:
            if hook_manager.has_listeners(HookEventType.SESSION_END):
                hook_manager.dispatch(
                    HookEventType.SESSION_END, lambda: {"reason": reason}
                )
        except Exception:  # noqa: BLE001 —— 清理路径绝不能因它中断
            pass
        try:
            manager.memory_manager.close()
        except Exception:
            pass
        mcp_manager.close_all()
        clear_read_roots()
        recorder.close()

    return BuildResult(
        app=app,
        cleanup=cleanup,
        manager=manager,
        tool_registry=tool_registry,
        command_registry=command_registry,
        skill_manager=skill_manager,
        mcp_manager=mcp_manager,
        recorder=recorder,
    )


__all__ = ["build_app", "BuildResult", "BootstrapError"]
