"""
RhineCode 命令行入口模块。

执行流程：
1. 解析 --config 参数，获取配置文件路径
2. 加载并校验 YAML 配置文件
3. 根据配置创建对应的 Provider 实例
4. 创建 ConversationManager，绑定 Provider
5. 创建并启动 Textual TUI 应用

用法：
    rhine                          # 安装后直接运行，读 ~/.rhinecode/config.yaml
    rhine --config config.yaml     # 显式指定配置文件覆盖全局配置
    python -m rhinecode            # 未安装或开发调试时的等价入口
"""

import argparse
import sys
from pathlib import Path

from rhinecode.config import load, user_config_path, scaffold_user_config, PLACEHOLDER_API_KEY
from rhinecode.commands import CommandRegistrationError, build_builtin_registry
from rhinecode.commands.skill_commands import build_skill_command_specs
from rhinecode.provider.factory import create_provider
from rhinecode.conversation import ConversationManager
from rhinecode.skills.manager import SkillManager
from rhinecode.skills.models import builtin_skills_dir
from rhinecode.skills.validation import format_fatal_message
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tools.load_skill import LoadSkillTool
from rhinecode.tools.mcp_config import MCPAddServerTool
from rhinecode.tools.path_guard import workspace_root
from rhinecode.permission import config as perm_config
from rhinecode.mcp import config as mcp_config
from rhinecode.mcp.manager import MCPManager
from rhinecode.tui.app import RhineApp


def main() -> None:
    """
    程序主入口，负责初始化所有组件并启动 TUI。

    执行步骤：
    1. 解析命令行参数（--config）
    2. 加载配置文件，失败时打印错误并以非零状态码退出
    3. 按配置创建 Provider → ConversationManager → RhineApp
    4. 启动 Textual 事件循环（阻塞直到用户退出）

    异常处理：
    - FileNotFoundError：配置文件路径不存在
    - ValueError：配置文件缺少必填字段，或 Provider 配置无效
    两种情况均打印可读错误信息后 sys.exit(1)，不向用户暴露堆栈。
    """
    parser = argparse.ArgumentParser(description="RhineCode - 终端 AI 编程助手")
    # --config 缺省为 None，表示「用用户级全局配置 ~/.rhinecode/config.yaml」；
    # 显式传入时按给定路径加载，供临时覆盖或多配置切换。
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（缺省使用 ~/.rhinecode/config.yaml）",
    )
    # --continue（c9 F10）：启动时恢复最近的未锁定会话。dest 必须显式指定——
    # `continue` 是 Python 关键字，argparse 默认派生的 args.continue 会语法错误。
    parser.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="启动时恢复最近一次会话，接着上次继续",
    )
    args = parser.parse_args()

    # 决定实际配置路径：显式 --config 优先，否则用用户级全局配置。
    # explicit 用于区分「用户点名的文件」和「缺省全局文件」——只有缺省文件缺失时才自动生成模板，
    # 显式指到一个不存在的文件仍按错误处理（不擅自造文件）。
    explicit = args.config is not None
    config_path = Path(args.config) if explicit else user_config_path()

    # 首次运行引导：仅在缺省流程（未显式 --config）里为用户级 ~/.rhinecode 生成三类模板。
    # 三类语义不同：
    # - config.yaml 必需（含 api_key）→ 本次才生成时，引导填 key 后退出。
    # - permissions.yaml / mcp.yaml 可选（fail-safe，缺省即空）→ 模板全注释、等价于空，
    #   静默生成、不因它们退出；已有 config.yaml 的老用户下次运行会顺带补上这两份。
    if not explicit:
        try:
            config_created = scaffold_user_config(config_path)
            perm_config.scaffold_user_config(perm_config.user_config_path())
            mcp_config.scaffold_user_config(mcp_config.user_config_path())
        except OSError as e:
            print(f"无法生成配置模板：{e}", file=sys.stderr)
            sys.exit(1)
        if config_created:
            print(
                f"已在 {config_path.parent} 生成配置模板"
                "（config.yaml / permissions.yaml / mcp.yaml），"
                "请在 config.yaml 填入真实 api_key 后重新运行 rhine。"
            )
            sys.exit(0)

    # 加载配置，捕获文件缺失和字段缺失两类错误并友好提示
    try:
        cfg = load(str(config_path))
    except (FileNotFoundError, ValueError) as e:
        print(f"配置错误：{e}", file=sys.stderr)
        sys.exit(1)

    # 占位符未替换引导：模板里的 YOUR_API_KEY 是非空字符串，能通过 load() 的非空校验，
    # 这里单独拦下，避免带着假 key 启动、到调用模型 API 时才报难懂的错。
    if cfg.api_key == PLACEHOLDER_API_KEY:
        print(
            f"检测到 {config_path} 的 api_key 仍是占位符，请填入真实 api_key 后重新运行 rhine。",
            file=sys.stderr,
        )
        sys.exit(1)

    # 构建命令注册表（c10 T53）：必须在 Provider / 工具注册中心 / MCP 连接等昂贵资源
    # 之前完成——命令名/别名冲突属于代码级配置错误，fail-fast 在此拦下（退出码 1），
    # 此时尚未创建网络连接、MCP 子进程或会话锁，启动失败干净利落（spec F2/N4）。
    try:
        command_registry = build_builtin_registry()
    except CommandRegistrationError as e:
        print(f"命令注册冲突：{e}", file=sys.stderr)
        sys.exit(1)

    # 根据配置创建模型 Provider。
    try:
        provider = create_provider(cfg)
    except ValueError as e:
        print(f"Provider 初始化错误：{e}", file=sys.stderr)
        sys.exit(1)

    # 构建工具注册中心（含 6 个核心工具），注入协调层以启用工具能力。
    # 工具仅在 DeepSeek 协议下实际生效，其余协议下协调层会自动忽略（见 ConversationManager）。
    # 命名为 tool_registry 与上面的 command_registry 明确区分（两者互不相干）。
    tool_registry = ToolRegistry.default()

    # 加载 MCP 配置并连接外部 Server，把发现到的远端工具注册进同一个 tool_registry（c7）。
    # connect_all 逐 Server 隔离：单个失败只跳过、不影响内置工具与启动（spec F13）；
    # 无 mcp.yaml 时 configs 为空、无任何 MCP 工具，行为与 c6 完全一致。
    mcp_configs, mcp_errors = mcp_config.load_all()
    mcp_manager = MCPManager()
    # mcp_add_server 需要同时写配置、重载目标 server、更新 registry，因此必须在 MCPManager
    # 创建后注入运行时依赖；只读的 mcp_resolve_server 已在 ToolRegistry.default() 中注册。
    tool_registry.register(MCPAddServerTool(mcp_manager, tool_registry))

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
    skill_manager = SkillManager(
        workspace_root(),
        Path.home() / ".rhinecode",
        builtin_skills_dir(),
        has_short_command=command_registry.has_skill_command,
    )
    # load_skill 在这里注册而不是在 ToolRegistry.default() 里：它依赖
    # SkillManager 实例，且 tools/registry.py 若导入 tools/load_skill.py
    # 会把 tools ↔ skills 的包级互依变成真环（见 tools/__init__.py 的说明）。
    #
    # **必须在算 known_tools 之前注册**：否则白名单里写了 `load_skill` 的
    # Skill 会被第一段严格校验判成笔误，启动直接挂掉——而那是个完全合法的
    # 声明（虽然没有效果，只会得到一条「可以删除」的提示）。
    tool_registry.register(LoadSkillTool(skill_manager))

    # known 的组成：注册中心当前全部工具名 ∪ Plan Mode 的两个特殊工具。
    # 后两个不在注册中心里但确实可被模型调用，白名单写它们不算笔误。
    known_tools = tool_registry.names() | {"ask_user", "present_plan"}
    fatal_tool_names = skill_manager.startup(known_tools)
    if fatal_tool_names:
        print(format_fatal_message(fatal_tool_names), file=sys.stderr)
        sys.exit(1)

    mcp_manager.connect_all(mcp_configs, tool_registry, extra_errors=mcp_errors)

    # ── Skill 系统第二阶段（c11 T58）：MCP 剪枝 + 短命令注册 ──
    # 此刻远端工具已经注册进 tool_registry，可以判断哪些 mcp__ 白名单项有效了。
    skill_manager.bind_tools(registered=tool_registry.names())
    skipped_skill_commands = command_registry.replace_skill_commands(
        build_skill_command_specs(skill_manager.command_infos())
    )
    for spec in skipped_skill_commands:
        # 与内置命令重名 → 短命令没注册，但 Skill 本身仍可用（走 /skills run）。
        # 必须明说，否则用户会以为 Skill 坏了。
        print(
            f"提示：Skill 的短命令 {spec.name} 与已有命令冲突，未注册；"
            f"请用 /skills run {spec.name.lstrip('/')} 执行它。",
            file=sys.stderr,
        )
    for warning in skill_manager.runtime_warnings():
        print(f"提示：{warning}", file=sys.stderr)
    project_notice = skill_manager.project_skill_notice()
    if project_notice:
        print(project_notice, file=sys.stderr)

    # 依次构建各层组件，层间通过依赖注入解耦。
    # resume_latest 透传 --continue：协调层构造时经 MemoryManager 恢复最近会话（c9）。
    manager = ConversationManager(
        provider, cfg, tool_registry, mcp_manager=mcp_manager,
        resume_latest=args.continue_session,
        skill_manager=skill_manager,
    )
    app = RhineApp(manager, cfg, command_registry)
    # try/finally 保证无论正常退出还是异常，都统一回收资源：
    # MCP 连接与 stdio 子进程（c7）、会话锁（c9，不释放会短暂挡住其它实例接管，
    # 直到锁过期自愈）。两者各自 try 住，互不影响。
    try:
        app.run()
    finally:
        try:
            manager.memory_manager.close()
        except Exception:
            pass
        mcp_manager.close_all()


if __name__ == "__main__":
    main()
