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

from rhinecode.commands import CommandRegistrationError, build_builtin_registry
from rhinecode.commands.skill_commands import build_skill_command_specs
from rhinecode.config import Config
from rhinecode.conversation import ConversationManager
from rhinecode.mcp import config as mcp_config
from rhinecode.mcp.manager import MCPManager
from rhinecode.provider.factory import create_provider
from rhinecode.skills.manager import SkillManager
from rhinecode.skills.models import builtin_skills_dir
from rhinecode.tools.load_skill import LoadSkillTool
from rhinecode.tools.mcp_config import MCPAddServerTool
from rhinecode.tools.web_fetch import WebFetchTool
from rhinecode.web.manager import WebFetchManager
from rhinecode.tools.path_guard import clear_read_roots, workspace_root
from rhinecode.tools.registry import ToolRegistry
from rhinecode.trace import (
    NullRecorder,
    TraceEventType,
    TraceRecorderProtocol,
    clip,
    redact_config,
)
from rhinecode.trace.tracing_provider import TracingProvider
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
) -> BuildResult:
    """
    按固定顺序装配一个完整的 RhineCode 应用。

    :param cfg: 已加载校验过的配置
    :param user_dir: 用户级目录；缺省 `Path.home() / ".rhinecode"`（等于现状）。
                     给定时用户级项目指令 / 笔记 / Skill / 权限规则 / MCP 声明
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
        workspace_root(),
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
    )

    # 把「跑一个 fork Skill」的能力回注给加载工具（对齐改造 F8）。
    #
    # **必须在这里、不能提前**：工具要在第 ④ 步之前注册（`bind_tools` 才数得到
    # 它），而能跑子对话的协调层要到第 ⑤ 步才存在。属性注入让两者的构造顺序解耦，
    # 与 `memory_manager.notify` / `skill_manager.notify_activation` 是同一形态。
    load_skill_tool.run_fork = manager.run_forked_for_model
    load_skill_tool.on_activated = manager.on_skill_activated

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
            "project_root": str(workspace_root()),
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
            "skills_index": clip(skill_manager.index_text()),
            "resume_latest": resume_latest,
        },
    )

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
