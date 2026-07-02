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
from rhinecode.provider.factory import create_provider
from rhinecode.conversation import ConversationManager
from rhinecode.tools.registry import ToolRegistry
from rhinecode.tools.mcp_config import MCPAddServerTool
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

    # 根据配置创建模型 Provider。
    try:
        provider = create_provider(cfg)
    except ValueError as e:
        print(f"Provider 初始化错误：{e}", file=sys.stderr)
        sys.exit(1)

    # 构建工具注册中心（含 6 个核心工具），注入协调层以启用工具能力。
    # 工具仅在 DeepSeek 协议下实际生效，其余协议下协调层会自动忽略（见 ConversationManager）。
    registry = ToolRegistry.default()

    # 加载 MCP 配置并连接外部 Server，把发现到的远端工具注册进同一个 registry（c7）。
    # connect_all 逐 Server 隔离：单个失败只跳过、不影响内置工具与启动（spec F13）；
    # 无 mcp.yaml 时 configs 为空、无任何 MCP 工具，行为与 c6 完全一致。
    mcp_configs, mcp_errors = mcp_config.load_all()
    mcp_manager = MCPManager()
    # mcp_add_server 需要同时写配置、重载目标 server、更新 registry，因此必须在 MCPManager
    # 创建后注入运行时依赖；只读的 mcp_resolve_server 已在 ToolRegistry.default() 中注册。
    registry.register(MCPAddServerTool(mcp_manager, registry))
    mcp_manager.connect_all(mcp_configs, registry, extra_errors=mcp_errors)

    # 依次构建各层组件，层间通过依赖注入解耦
    manager = ConversationManager(provider, cfg, registry, mcp_manager=mcp_manager)
    app = RhineApp(manager, cfg)
    # try/finally 保证无论正常退出还是异常，都统一回收 MCP 连接与 stdio 子进程（spec F12/AC11）。
    try:
        app.run()
    finally:
        mcp_manager.close_all()


if __name__ == "__main__":
    main()
