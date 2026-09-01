"""供 Agent 自动解析和添加 MCP Server 的内置工具。

工具层负责把底层 resolver/writer 暴露给模型，并把所有异常转换成 `ToolResult`。其中
`mcp_resolve_server` 是只读工具；`mcp_add_server` 会写入配置并启动外部 MCP，因此标记为
非只读，交给现有权限确认流程拦截。

⚠ **「交给现有权限确认流程拦截」这句话一度是假的**，记在这里免得下一个人
再照着它推理（审查报告 B4 / `docs/review/04-security.md` 的 S1）。

它写于 C7，当时缺省权限档是 `DEFAULT`、第④层对未映射工具判 ASK，面板照弹。
auto-plan 扩展把缺省档换成 `PERMISSIVE`（`presets.py` 的 `DEFAULT_PRESET`）之后，
本工具落在 `other` 兜底分支上，实测判定是 `allow @ mode`——**六层防御一层都碰不到
它**（①黑名单只认命令类、②沙箱只认路径类、②′只认 url、②″保护路径第一行就是
`if request.kind != "write_path"`、③层要用户主动写 `deny` 才拦得住）。
于是模型可以在一次调用里、不弹任何面板地写一条 `mcpServers` 配置并立刻拉起来，
而 `command` 是任意本地命令。

**现在这句话由 `permission/adapter.py` 的 `launch` 类映射兑现**：本工具的请求
被规范化为 `kind == "launch"`，第④层在放行档下对它判 ASK（与 url / search
两个既有例外同格）。改动那处映射等于把这条承诺再拿掉一次，
护栏见 `tests/test_perm_launch_layer.py`。
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

    ⚠ `read_only=False` 是**必要条件、不是充分条件**：它只保证请求不走「只读简化
    分支」、会进第④层，而第④层在放行档下对未映射工具给的是 ALLOW。真正让它过人眼
    的是 `permission/adapter.py` 把它映射成 `kind == "launch"`（见模块 docstring）。
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
    # c16 第五类：把一个外部程序拉起来，要经分类器审查。
    #
    # ⚠ **这不是给 B4 补漏，是给它接第二道。** B4（`permission/adapter.py` 的
    # `launch` 类映射）已经让本工具在缺省预设下必弹面板；本行加的是**面板之前
    # 的那一眼**——面板看得到「要跑什么命令」，看不到「用户到底有没有要求过
    # 引入这个 Server」，而后者恰恰是完整对话上下文才回答得了的问题。
    #
    # ⚠ **它会让面板在日常消失**：分类器判放行时把④层的 ASK 覆写成 ALLOW
    # （`agent/loop.py::_apply_classifier`），与网络类 / 搜索类同形；熔断时
    # 退回逐次弹面板（④层基线是 ASK，见 `permission/engine.py` 的 launch 分支）。
    # 换言之**判放行的那条路上分类器是唯一的一道**——①②②′②″一层都碰不到
    # 这类动作。这是评审时明知并接受的取舍，登记在 CLAUDE.md 的安全边界一节。
    #
    # ⚠ 这里写**字面量**而不是 import `classifier.models.SCOPE_LAUNCH`，
    # 与 `web_fetch` / `web_search` 同一先例：`import` 那个模块会连带执行
    # `classifier/__init__.py`，把服务、熔断、缓存与 `provider.base` 一起拉进
    # 工具层的导入图，而工具层只需要一个字符串。
    # 两者相等由 `tests/test_mcp_launch_classifier.py` 的一条断言钉住。
    classifier_scope = "launch"

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
