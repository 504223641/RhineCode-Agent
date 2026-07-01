"""
JSON-RPC 2.0 层（c7）：消息构造/分类、请求 id 生成、协议错误类型。

本模块是**纯数据**——不做任何 I/O，只负责把「方法名 + 参数」拼成符合 JSON-RPC 2.0
规范的字典，以及把收到的字典分类为「响应 / 非响应」。真正的收发由 transport 层负责。

MCP 的一次会话在 JSON-RPC 语义下由三类消息组成：
- 请求（request）：带唯一 id、带 method，期望对端回一个同 id 的响应。
- 通知（notification）：带 method 但**不带 id**，对端不回包（如 notifications/initialized）。
- 响应（response）：带请求的 id，且含 result（成功）或 error（失败）。

id 的作用：传输层可能同时有多个在途请求（尤其 stdio 单管道复用），响应回来时
必须靠 id 与「发出的哪个请求」配对。故 id 必须全局唯一，这里用线程安全的自增计数器。
"""

import threading
from typing import Any, Optional

# 全局自增 id 计数器 + 锁。所有传输实例共享同一序列即可保证唯一（值本身无业务含义）。
_id_lock = threading.Lock()
_id_counter = 0


def next_id() -> int:
    """
    生成下一个全局唯一的请求 id（线程安全）。

    :returns: 严格递增的正整数 id

    副作用：递增模块级计数器（受锁保护）。
    """
    global _id_counter
    with _id_lock:
        _id_counter += 1
        return _id_counter


def build_request(id: int, method: str, params: Optional[dict]) -> dict:
    """
    构造一条 JSON-RPC 2.0 请求。

    :param id: 请求 id（由 next_id 生成），响应按此 id 配对
    :param method: 方法名，如 "initialize" / "tools/list" / "tools/call"
    :param params: 参数字典；为 None 时省略 params 键（部分方法无参数）
    :returns: 形如 {"jsonrpc":"2.0","id":id,"method":method,"params":params} 的字典

    副作用：无。
    """
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def build_notification(method: str, params: Optional[dict]) -> dict:
    """
    构造一条 JSON-RPC 2.0 通知（无 id、对端不回包）。

    :param method: 方法名，如 "notifications/initialized"
    :param params: 参数字典；为 None 时省略 params 键
    :returns: 形如 {"jsonrpc":"2.0","method":method} 的字典

    副作用：无。
    """
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def is_response(msg: Any) -> bool:
    """
    判断一个已解析的消息是否是「响应」（带 id 且含 result 或 error）。

    传输层的 reader 收到消息后据此区分：是响应就按 id 找回等待中的请求并唤醒；
    否则是服务端发来的通知/请求（本章不处理，忽略）。

    :param msg: 已 json.loads 的对象
    :returns: 是响应返回 True
    """
    return (
        isinstance(msg, dict)
        and "id" in msg
        and msg.get("id") is not None
        and ("result" in msg or "error" in msg)
    )


class JsonRpcError(Exception):
    """
    对端返回了 JSON-RPC error 对象（协议级错误，如方法不存在、参数非法）。

    注意：它与「工具执行出错」不同——工具执行出错是一次**成功的** tools/call 响应里
    isError=true（属于 result，不是这里的 error）。本异常表示 JSON-RPC 层面的失败。

    :ivar code: JSON-RPC 错误码（如 -32601 方法不存在）
    :ivar message: 错误描述
    :ivar data: 可选的附加数据
    """

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class TransportError(Exception):
    """
    传输层错误：子进程退出、管道断开、请求超时、HTTP 非 2xx、连接失败等。

    与 JsonRpcError 区分开，便于上层针对「协议错误」与「传输故障」分别处理；
    但在适配层最终都会被兜底为对模型可读的失败结果（spec N2）。
    """
