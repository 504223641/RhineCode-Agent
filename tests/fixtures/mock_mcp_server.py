"""
最简 stdio MCP 模拟 Server（c7 测试夹具）。

作为子进程被拉起，从 stdin 逐行读 JSON-RPC 请求，按 method 回一行 JSON 响应。
只实现测试需要的三步会话 + 两个演示工具，不追求协议完整性；真实 Server 留到端到端手测。

支持的方法：
- initialize                → 回协议版本、能力、serverInfo
- notifications/initialized  → 通知（无回包）
- tools/list                → 回两个工具 echo / boom
- tools/call:
    echo → {content:[{type:text, text:<把 arguments 原样回显>}], isError:false}
    boom → {content:[{type:text, text:"failed"}], isError:true}

按请求 id 回带响应，验证传输层的 id 配对。
"""

import json
import sys

# MCP stdio 规范要求消息用 UTF-8；Windows 上 Python 子进程默认按本地编码（cp936）写 stdout，
# 这里显式把 stdin/stdout 切到 UTF-8，保证与客户端（按 UTF-8 解码）一致。
sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")


def _send(msg: dict) -> None:
    """把一条响应写成一行 JSON 到 stdout 并 flush（换行分帧）。"""
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(req: dict) -> None:
    """按 method 分派并回包；通知类（无 id）不回。"""
    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        _send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp-server", "version": "0.0.1"},
            },
        })
        return

    if method == "notifications/initialized":
        # 通知无 id、无回包
        return

    if method == "tools/list":
        _send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "回显传入的参数",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    },
                    {
                        "name": "boom",
                        "description": "总是返回错误，用于测试 isError",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            },
        })
        return

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(arguments, ensure_ascii=False)}],
                    "isError": False,
                },
            })
        elif name == "boom":
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "failed"}],
                    "isError": True,
                },
            })
        else:
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"未知工具：{name}"},
            })
        return

    # 其它方法：回方法不存在错误
    if req_id is not None:
        _send({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"方法不存在：{method}"},
        })


def main() -> None:
    """主循环：逐行读、逐行处理，直到 stdin EOF。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle(req)


if __name__ == "__main__":
    main()
