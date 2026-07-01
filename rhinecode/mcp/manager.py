"""
编排层（c7，spec F12/F13/F14/F15/F16）：MCPManager 管理多 Server 的连接。

职责：
- 启动发现（F14）：对每个配置的 Server 依次连接 → 握手 → 列出工具 → 注册进 ToolRegistry。
- 单点隔离（F13）：每个 Server 用独立 try/except 包住，任一失败只跳过该 Server、记录原因，
  不影响其它 Server、内置工具与 RhineCode 启动。
- 生命周期（F12）：持有所有成功连接的 client，程序退出时统一 close_all 回收（含 stdio 子进程）。
- 状态汇总（F15/F16）：status_line 供底部状态栏，status_report 供 /mcp 命令。
"""

from dataclasses import dataclass
from typing import Optional

from rhinecode.mcp.client import MCPClient
from rhinecode.mcp.config import MCPServerConfig
from rhinecode.mcp.tool_adapter import MCPTool
from rhinecode.mcp.transport import HttpTransport, StdioTransport, Transport
from rhinecode.tools.registry import ToolRegistry


@dataclass
class ServerState:
    """
    一个 Server 在本次运行中的连接结果，供状态栏与 /mcp 展示。

    :ivar name: Server 名字
    :ivar kind: 传输类型（stdio/http）
    :ivar connected: 是否成功连上并列出工具
    :ivar tool_count: 成功注册的工具数
    :ivar error: 失败原因（连接/握手/列表任一步失败）；成功为 None
    """
    name: str
    kind: str
    connected: bool
    tool_count: int = 0
    error: Optional[str] = None


class MCPManager:
    """
    多 MCP Server 的连接管理器。一个进程持有一个实例。

    :ivar states: 每个 Server 的连接结果（含成功与失败），按配置顺序
    :ivar config_errors: 配置解析阶段收集的错误（来自 config.load_all）
    """

    def __init__(self) -> None:
        self.states: list[ServerState] = []
        self.config_errors: list[str] = []
        # 仅保存成功连接的 client，供退出时统一关闭
        self._clients: list[MCPClient] = []

    @staticmethod
    def _build_transport(cfg: MCPServerConfig) -> Transport:
        """按配置类型构造对应传输实例（未 start）。"""
        if cfg.kind == "stdio":
            return StdioTransport(cfg.command or "", cfg.args, cfg.env)
        return HttpTransport(cfg.url or "", cfg.headers)

    def connect_all(
        self,
        configs: list[MCPServerConfig],
        registry: ToolRegistry,
        extra_errors: Optional[list[str]] = None,
    ) -> None:
        """
        连接所有配置的 Server 并把其工具注册进 registry（启动阶段调用）。

        每个 Server 独立 try/except 隔离：成功则注册工具并记 connected 状态；
        失败则记录 error 并继续下一个（spec F13）。

        :param configs: 规范化后的 Server 配置列表
        :param registry: 目标工具注册中心（就地写入 MCPTool）
        :param extra_errors: 配置解析阶段的错误（一并纳入 config_errors 展示）

        副作用：建立网络/子进程连接；向 registry 注册工具；填充 self.states/_clients。
        """
        self.config_errors = list(extra_errors or [])
        for cfg in configs:
            client: Optional[MCPClient] = None
            try:
                client = MCPClient(cfg.name, self._build_transport(cfg))
                client.initialize()
                tools = client.list_tools()
                count = 0
                for tool in tools:
                    remote_name = tool.get("name")
                    if not remote_name:
                        continue
                    registry.register(
                        MCPTool(
                            client=client,
                            server_name=cfg.name,
                            remote_name=str(remote_name),
                            description=str(tool.get("description") or ""),
                            parameters=tool.get("inputSchema"),
                        )
                    )
                    count += 1
                self._clients.append(client)
                self.states.append(ServerState(cfg.name, cfg.kind, connected=True, tool_count=count))
            except Exception as exc:  # noqa: BLE001 —— 单 Server 失败隔离，不影响其它
                # 失败的 client 尽力关闭，回收可能已拉起的子进程
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.states.append(
                    ServerState(cfg.name, cfg.kind, connected=False, error=str(exc))
                )

    def status_line(self) -> Optional[str]:
        """
        状态栏用的一行摘要。

        :returns: 形如「MCP：已连接 2/3 · 工具 11」；未配置任何 Server 时返回 None（不占状态栏）
        """
        total = len(self.states)
        if total == 0:
            return None
        ok = sum(1 for s in self.states if s.connected)
        tools = sum(s.tool_count for s in self.states)
        return f"MCP：已连接 {ok}/{total} · 工具 {tools}"

    def status_report(self) -> str:
        """
        /mcp 命令用的多行明细。

        :returns: 每个 Server 一行（含 ✓/✗ 与工具数或失败原因），附配置错误；无 Server 时给提示
        """
        lines: list[str] = []
        if not self.states and not self.config_errors:
            return "未配置任何 MCP Server（可在 ~/.rhinecode/mcp.yaml 或项目级 .rhinecode/mcp.yaml 声明）。"

        for s in self.states:
            if s.connected:
                lines.append(f"{s.name} ({s.kind}) ✓ 已连接 · {s.tool_count} 工具")
            else:
                lines.append(f"{s.name} ({s.kind}) ✗ 失败：{s.error}")

        for err in self.config_errors:
            lines.append(f"⚠ 配置：{err}")
        return "\n".join(lines)

    def close_all(self) -> None:
        """关闭所有成功连接的 client（best-effort，逐个吞异常），退出时调用（spec F12）。"""
        for client in self._clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001 —— 清理阶段不因单个失败中断
                pass
        self._clients.clear()
