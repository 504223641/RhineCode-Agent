"""供 Agent 自动解析和添加 MCP Server 的内置工具。

工具层负责把底层 resolver/writer 暴露给模型，并把所有异常转换成 `ToolResult`。其中
`mcp_resolve_server` 是只读工具；`mcp_add_server` 会写入配置并启动外部 MCP，因此标记为
非只读，交给现有权限确认流程拦截。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rhinecode.mcp.auto_config import (
    resolve_mcp_query,
    server_config_from_entry,
    write_server_config,
)
from rhinecode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    # 仅用于类型标注，避免工具模块导入时反向初始化 MCPManager/ToolRegistry。
    from rhinecode.mcp.manager import MCPManager
    from rhinecode.tools.registry import ToolRegistry


class MCPResolveServerTool(Tool):
    """把用户口语化的 MCP 名称解析成候选配置。

    该工具不写文件、不启动外部进程，只返回候选来源、置信度和风险提示。Agent 必须先调用
    它，再根据 `resolved/ambiguous/error` 决定是否继续向用户说明并调用添加工具。
    """

    name = "mcp_resolve_server"
    description = (
        "Resolve an MCP name, NPM package name, or HTTP URL into candidate mcp.yaml "
        "server configuration. Use this before adding or enabling an MCP server."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "MCP name, NPM package name, or HTTP MCP URL to resolve.",
            }
        },
        "required": ["query"],
    }
    read_only = True

    def execute(self, args: dict) -> ToolResult:
        """执行只读解析，并把结构化结果序列化给模型消费。"""

        query = str(args.get("query") or "").strip()
        result = resolve_mcp_query(query)
        payload = result.to_dict()
        output = json.dumps(payload, ensure_ascii=False, indent=2)
        if result.status == "error":
            return ToolResult(ok=False, output=output, summary="MCP resolve failed")
        if result.status == "ambiguous":
            return ToolResult(ok=True, output=output, summary="MCP candidates need confirmation")
        return ToolResult(ok=True, output=output, summary="MCP server resolved")


class MCPAddServerTool(Tool):
    """写入一个 MCP Server 配置，并在当前会话中只重载该 server。

    副作用包含两部分：更新用户级或项目级 `mcp.yaml`，以及尝试连接新 MCP 并注册其工具。
    因为这可能运行第三方命令或访问远端 URL，本工具必须保持 `read_only=False`。
    """

    name = "mcp_add_server"
    description = (
        "Add one MCP server to user or project mcp.yaml, then reload that server "
        "immediately. Use only after mcp_resolve_server produced a config and the "
        "user has enough context to approve the write/start action."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["auto", "project", "user"],
                "description": "Where to write config. auto defaults to project.",
            },
            "server_name": {
                "type": "string",
                "description": "MCP server key to write under mcpServers.",
            },
            "config": {
                "type": "object",
                "description": "Server config object with either command/args/env or url/headers.",
            },
            "replace": {
                "type": "boolean",
                "description": "Whether to replace an existing same-name server config.",
            },
        },
        "required": ["server_name", "config"],
    }
    read_only = False

    def __init__(self, mcp_manager: "MCPManager", registry: "ToolRegistry"):
        """注入运行时依赖，使添加后可以立即连接并注册 MCP 工具。"""

        self._mcp_manager = mcp_manager
        self._registry = registry

    def execute(self, args: dict) -> ToolResult:
        """写入配置并触发单 server 重载。

        所有参数校验、YAML 写入错误和连接失败都被转成 `ToolResult`，避免工具异常直接打断
        Agent Loop；连接失败时配置可能已经写入，结果里会明确返回 `connected=false` 和错误。
        """

        try:
            server_name = str(args.get("server_name") or "").strip()
            config = args.get("config")
            if not server_name:
                return ToolResult(ok=False, output="Missing required parameter server_name", summary="Missing server_name")
            if not isinstance(config, dict):
                return ToolResult(ok=False, output="Missing or invalid required parameter config", summary="Invalid config")

            scope = str(args.get("scope") or "auto")
            replace = bool(args.get("replace") or False)
            write_result = write_server_config(server_name, config, scope=scope, replace=replace)
            # 运行时重载使用已落盘的归一化配置，避免“写入内容”和“本次启动内容”出现分叉。
            cfg = server_config_from_entry(write_result.server_name, write_result.config)
            state = self._mcp_manager.reload_server(cfg, self._registry)

            payload: dict[str, Any] = {
                "scope": write_result.scope,
                "path": str(write_result.path),
                "server_name": write_result.server_name,
                "action": write_result.action,
                "changed": write_result.changed,
                "connected": state.connected,
                "tool_count": state.tool_count,
                "error": state.error,
            }
            output = json.dumps(payload, ensure_ascii=False, indent=2)
            if state.connected:
                return ToolResult(ok=True, output=output, summary=f"MCP {state.name} connected · {state.tool_count} tools")
            return ToolResult(ok=False, output=output, summary=f"MCP {state.name} reload failed")
        except Exception as exc:  # noqa: BLE001 - tools return structured failures, never raise
            return ToolResult(ok=False, output=f"Failed to add MCP server: {exc}", summary="MCP add failed")
