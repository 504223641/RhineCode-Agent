"""MCP Server 运行时编排。

`MCPManager` 负责把配置里的 MCP Server 连接起来，并把远端工具注册到共享
`ToolRegistry`。它同时承担 TUI 状态汇总、进程/HTTP 客户端清理，以及自动添加 MCP 后
“只重载目标 server”的运行时更新能力。单个 server 的失败必须被隔离，不能影响内置工具
和 RhineCode 主流程。
"""

from dataclasses import dataclass, field
from typing import Optional

from rhinecode.mcp.client import MCPClient
from rhinecode.mcp.config import MCPServerConfig
from rhinecode.mcp.tool_adapter import MCPTool, sanitize_mcp_tool_name
from rhinecode.mcp.transport import HttpTransport, StdioTransport, Transport
from rhinecode.tools.registry import ToolRegistry


@dataclass
class ServerState:
    """单个 MCP Server 的运行时状态。

    `tool_names` 记录该 server 实际注册进 registry 的工具名，重载或关闭时依赖它做精确
    清理，避免误删其它 server 或内置工具。
    """

    name: str
    kind: str
    connected: bool
    tool_count: int = 0
    error: Optional[str] = None
    aliases: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)


class MCPManager:
    """管理 MCP 连接、工具注册和按 server 的运行时重载。"""

    def __init__(self) -> None:
        self.states: list[ServerState] = []
        self.config_errors: list[str] = []
        self._clients: list[MCPClient] = []
        # 下面两个索引按 server name 建立所有权关系，支持 reload 时只清理目标 server。
        self._clients_by_server: dict[str, MCPClient] = {}
        self._tools_by_server: dict[str, list[str]] = {}

    @staticmethod
    def _build_transport(cfg: MCPServerConfig) -> Transport:
        if cfg.kind == "stdio":
            return StdioTransport(cfg.command or "", cfg.args, cfg.env)
        return HttpTransport(cfg.url or "", cfg.headers)

    @staticmethod
    def _unique_tool_name(base_name: str, registry: ToolRegistry) -> str:
        if registry.get(base_name) is None:
            return base_name
        for i in range(2, 1000):
            suffix = f"_{i}"
            candidate = base_name[:64 - len(suffix)].rstrip("_") + suffix
            if registry.get(candidate) is None:
                return candidate
        raise ValueError(f"MCP 工具命名冲突过多：{base_name}")

    def connect_all(
        self,
        configs: list[MCPServerConfig],
        registry: ToolRegistry,
        extra_errors: Optional[list[str]] = None,
    ) -> None:
        """连接所有已配置 MCP Server，并隔离单个 server 的失败。

        启动阶段可能同时加载用户级和项目级配置；这里逐个 server 建立连接，某个配置坏掉时
        只记录状态，不阻断其它 server 和内置工具继续可用。
        """

        self.config_errors = list(extra_errors or [])
        for cfg in configs:
            self._drop_server(cfg.name, registry)
            self._connect_one(cfg, registry)

    def reload_server(self, cfg: MCPServerConfig, registry: ToolRegistry) -> ServerState:
        """在配置变化后只重载一个 MCP Server。

        自动添加 MCP 时不应重连全部 server，因为其它已连接 server 可能有长会话、昂贵初始化
        或临时故障。这里先精确清理目标 server，再按新配置连接并注册工具。
        """

        self._drop_server(cfg.name, registry)
        return self._connect_one(cfg, registry)

    def _drop_server(self, name: str, registry: ToolRegistry) -> None:
        """关闭并注销一个 server 拥有的资源。

        副作用：关闭旧 MCPClient、从 registry 移除该 server 注册的工具、删除状态记录。清理
        过程 best-effort，关闭异常不会阻止 registry 清理继续执行。
        """

        client = self._clients_by_server.pop(name, None)
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                pass
            self._clients = [c for c in self._clients if c is not client]

        for tool_name in self._tools_by_server.pop(name, []):
            registry.unregister(tool_name)
        self.states = [s for s in self.states if s.name != name]

    def _connect_one(self, cfg: MCPServerConfig, registry: ToolRegistry) -> ServerState:
        """连接一个 server、注册其远端工具，并记录状态。

        注册工具时会把 `<server>/<remote_name>` 归一化成全局唯一的工具名；若发生冲突则添加
        后缀并记录 alias，便于 `/mcp` 报告解释最终名字。任何异常都会回滚本次已注册工具。
        """

        client: Optional[MCPClient] = None
        tool_names: list[str] = []
        try:
            client = MCPClient(cfg.name, self._build_transport(cfg))
            client.initialize()
            tools = client.list_tools()
            aliases: list[str] = []

            for tool in tools:
                remote_name = tool.get("name")
                if not remote_name:
                    continue
                # original_name 用于判断是否发生重命名；safe_name 才是实际注册到 registry 的名字。
                original_name = f"mcp__{cfg.name}__{remote_name}"
                safe_name = self._unique_tool_name(
                    sanitize_mcp_tool_name(cfg.name, str(remote_name)),
                    registry,
                )
                registry.register(
                    MCPTool(
                        client=client,
                        server_name=cfg.name,
                        remote_name=str(remote_name),
                        description=str(tool.get("description") or ""),
                        parameters=tool.get("inputSchema"),
                        registered_name=safe_name,
                    )
                )
                tool_names.append(safe_name)
                if safe_name != original_name:
                    aliases.append(f"{safe_name} <- {cfg.name}/{remote_name}")

            self._clients.append(client)
            self._clients_by_server[cfg.name] = client
            self._tools_by_server[cfg.name] = tool_names
            state = ServerState(
                name=cfg.name,
                kind=cfg.kind,
                connected=True,
                tool_count=len(tool_names),
                aliases=aliases,
                tool_names=tool_names,
            )
            self.states.append(state)
            return state
        except Exception as exc:  # noqa: BLE001 - one server must not break startup/runtime
            # 连接或工具注册中途失败时，只回滚本 server 的部分成果，保留其它 server/内置工具。
            for tool_name in tool_names:
                registry.unregister(tool_name)
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            state = ServerState(cfg.name, cfg.kind, connected=False, error=str(exc))
            self.states.append(state)
            return state

    def status_line(self) -> Optional[str]:
        """返回状态栏使用的一行 MCP 摘要。"""

        total = len(self.states)
        if total == 0:
            return None
        ok = sum(1 for s in self.states if s.connected)
        tools = sum(s.tool_count for s in self.states)
        return f"MCP：已连接 {ok}/{total} · 工具 {tools}"

    def status_report(self) -> str:
        """返回 `/mcp` 命令展示的多行状态报告。"""

        lines: list[str] = []
        if not self.states and not self.config_errors:
            return "未配置任何 MCP Server（可在 ~/.rhinecode/mcp.yaml 或项目级 .rhinecode/mcp.yaml 声明）。"

        for s in self.states:
            if s.connected:
                lines.append(f"{s.name} ({s.kind}) 已连接 · {s.tool_count} 工具")
                for alias in s.aliases:
                    lines.append(f"  {alias}")
            else:
                lines.append(f"{s.name} ({s.kind}) 失败：{s.error}")

        for err in self.config_errors:
            lines.append(f"警告：配置：{err}")
        return "\n".join(lines)

    def close_all(self) -> None:
        """以 best-effort 方式关闭所有 MCP 客户端。

        该方法通常在 CLI/TUI 退出时调用；关闭失败不再上抛，避免退出流程因为子进程状态异常
        而卡住。
        """

        for client in list(self._clients):
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
        self._clients_by_server.clear()
        self._tools_by_server.clear()
