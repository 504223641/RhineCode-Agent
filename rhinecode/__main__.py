"""
RhineCode 命令行入口模块。

执行流程：
1. 解析 --config 参数，获取配置文件路径
2. 加载并校验 YAML 配置文件
3. 根据配置创建对应的 Provider 实例
4. 创建 ConversationManager，绑定 Provider
5. 创建并启动 Textual TUI 应用

用法：
    python -m rhinecode --config config.yaml
"""

import argparse
import sys

from rhinecode.config import load
from rhinecode.provider.factory import create_provider
from rhinecode.conversation import ConversationManager
from rhinecode.tools.registry import ToolRegistry
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
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（默认：config.yaml）")
    args = parser.parse_args()

    # 加载配置，捕获文件缺失和字段缺失两类错误并友好提示
    try:
        cfg = load(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"配置错误：{e}", file=sys.stderr)
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

    # 依次构建各层组件，层间通过依赖注入解耦
    manager = ConversationManager(provider, cfg.protocol, registry)
    app = RhineApp(manager, cfg)
    app.run()


if __name__ == "__main__":
    main()
