"""
适配层（c7，spec F8/F9/F10/F11）：把一个远端 MCP 工具包装成 RhineCode 的 Tool。

这是 MCP 世界与 RhineCode 工具体系的接缝：
- 命名：注册名统一加 mcp__<server>__<tool> 前缀，与内置工具、其它 Server 天然隔离（F9）。
- 参数：直接透传远端 inputSchema 给模型（F10）。
- 只读性：一律 read_only=False——外部 Server 不可信（F11）。
- 来源标注：一律 remote_origin=True——权限层据它判 kind="remote"，第④层在放行档下
  仍判 ASK。⚠ F11 那句「默认每次经人在回路确认」在 auto-plan 把缺省档换成放行档
  之后曾整整失效一段时间（审查报告 S2），**兑现它的现在是这个标志**，不是 read_only。
- 结果转换：把 MCP 的 CallToolResult（content 块数组 + isError）翻译成统一的 ToolResult（F8）。
- 健壮性：execute 捕获一切异常转 ok=False，绝不外抛（spec N2），保证 Agent Loop 不崩。
"""

import hashlib
import re
from typing import Optional

from rhinecode.mcp.client import MCPClient
from rhinecode.tools.base import Tool, ToolResult

# 远端工具未提供 inputSchema 时的兜底：一个合法的空 object schema，避免把 None 发给模型 API。
_EMPTY_SCHEMA = {"type": "object", "properties": {}}
_MAX_FUNCTION_NAME = 64
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_]+")


def _safe_name_part(value: str) -> str:
    part = _UNSAFE_NAME_CHARS.sub("_", str(value)).strip("_")
    return part or "unnamed"


def sanitize_mcp_tool_name(server_name: str, remote_name: str) -> str:
    """
    生成 OpenAI/DeepSeek function name 兼容的 MCP 注册名。

    远端 MCP 名字可能包含点号、斜杠、空格等 API function name 不接受的字符；这里把
    server/tool 两段压成字母数字下划线，并在过长时加稳定 hash，保证名字可发送给模型。
    """
    server = _safe_name_part(server_name)
    remote = _safe_name_part(remote_name)
    candidate = f"mcp__{server}__{remote}"
    if len(candidate) <= _MAX_FUNCTION_NAME:
        return candidate

    digest = hashlib.sha1(f"{server_name}\0{remote_name}".encode("utf-8")).hexdigest()[:8]
    fixed = len("mcp__") + len("__") + len("_") + len(digest)
    budget = max(12, _MAX_FUNCTION_NAME - fixed)
    server_budget = max(4, min(len(server), budget // 3))
    remote_budget = max(4, budget - server_budget)
    server = server[:server_budget].rstrip("_") or "srv"
    remote = remote[:remote_budget].rstrip("_") or "tool"
    return f"mcp__{server}__{remote}_{digest}"


class MCPTool(Tool):
    """
    把一个远端 MCP 工具适配成 RhineCode Tool。

    :ivar _client: 所属 Server 的会话客户端（execute 时用它发 tools/call）
    :ivar _remote_name: 远端工具原名（不带前缀），调用时用它
    :ivar _server_name: Server 名字（用于 summary 展示）
    """

    read_only = False  # 外部 Server 不可信：默认走确认（spec F11）
    # 「本工具由外部程序提供」——权限层据它把请求映射成 kind="remote"，
    # 于是第④层在放行档下判 ASK 而不是 ALLOW（审查报告 S2 那一半）。
    #
    # ⚠ **它与上面的 read_only 是两件事，别合并也别互相推导。**
    # `read_only=False` 说的是「这次调用可能有副作用」，管的是要不要走④层；
    # 本标志说的是「实现这个工具的代码不是我们写的」，管的是④层怎么判。
    # 少了它，MCP 工具会落回 `kind="other"`，而缺省预设（放行档）下那一类
    # 的结论是**直接放行、零面板**——C7 那句「默认每次调用都经人在回路确认」
    # 就是在 auto-plan 把缺省档换成放行档之后，从这里悄悄失效的。
    remote_origin = True

    def __init__(
        self,
        client: MCPClient,
        server_name: str,
        remote_name: str,
        description: str,
        parameters: Optional[dict],
        registered_name: Optional[str] = None,
    ):
        """
        :param client: 所属 MCPClient
        :param server_name: Server 名字，用于命名前缀与展示
        :param remote_name: 远端工具原名
        :param description: 远端工具描述（缺失时回退到工具名）
        :param parameters: 远端 inputSchema（缺失/非法时回退空 object schema）
        """
        self._client = client
        self._server_name = server_name
        self._remote_name = remote_name
        # 实例属性覆盖类属性：注册中心与 API schema 都读 self.name/description/parameters
        self.name = registered_name or sanitize_mcp_tool_name(server_name, remote_name)
        self.description = description or remote_name
        self.parameters = parameters if isinstance(parameters, dict) and parameters else _EMPTY_SCHEMA
        self.original_name = f"{server_name}/{remote_name}"

    def execute(self, args: dict) -> ToolResult:
        """
        调用远端工具并把结果转成 ToolResult。

        执行步骤：
        1. 用所属 client 发 tools/call（远端原名 + 参数）。
        2. 遍历返回的 content 块：text 块累加文本；非 text 块以 [非文本内容: <type>] 占位。
        3. isError 为真 → ok=False；否则 ok=True。

        :param args: 模型给出的参数字典
        :returns: 统一 ToolResult；任何异常都兜底为 ok=False（绝不外抛，spec N2）

        副作用：发起一次远端 tools/call 请求（网络 / 子进程 IO）。
        """
        try:
            result = self._client.call_tool(self._remote_name, args)
        except Exception as exc:  # noqa: BLE001 —— 传输/协议/超时/其它一律兜底为可读失败
            return ToolResult(
                ok=False,
                output=f"MCP 工具调用失败：{exc}",
                summary="MCP 调用失败",
            )

        # 拼接 content：text 块取文本，其它类型给占位说明。
        content = result.get("content")
        parts: list[str] = []
        block_count = 0
        if isinstance(content, list):
            block_count = len(content)
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(f"[非文本内容: {btype}]")
        output = "\n".join(parts)

        is_error = bool(result.get("isError"))
        summary = f"MCP {self._server_name}/{self._remote_name} · {block_count} 块"
        if is_error:
            # 工具执行报错：output 为对端给出的错误文本（供模型据此调整）
            return ToolResult(ok=False, output=output or "MCP 工具返回错误", summary=summary)
        return ToolResult(ok=True, output=output, summary=summary)
