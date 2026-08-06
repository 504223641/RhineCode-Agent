"""
Trace 层：跨阶段的行为记录设施（**测试设施，不是产品功能**）。

它把程序运行过程中「实际发生了什么」按时间顺序写成结构化事件流，用于验收既有能力
与排查那类「界面上看不出、但行为确实不对」的问题。缺省关闭，开启方式见 `rhine --trace`。

本包是**叶子包**：`models` 与 `recorder` 只依赖标准库，因此任何模块都能安全依赖它埋点。

包级导出刻意**不含 `TracingProvider`**。它是 spec N4 允许的唯一分层例外
（必须继承 `provider.base.BaseProvider`，因此 trace 反向依赖了 provider 包）。
不从包级导出，是为了让这条依赖边在调用方代码里保持显式可见——调用方必须写
`from rhinecode.trace.tracing_provider import TracingProvider`，一眼就知道
「这里跨层了」，而不是被 `from rhinecode.trace import TracingProvider` 掩盖掉。
"""

from rhinecode.trace.models import (
    MAX_FIELD_CHARS,
    MAX_MESSAGE_ITEMS,
    REDACTED,
    SCOPE_MAIN,
    SCOPE_NOTES,
    SCOPE_WEB_EXTRACT,
    SCOPE_SUMMARY,
    TraceEventType,
    agent_event_payload,
    clip,
    default_trace_path,
    isolated_scope,
    redact_config,
    subagent_scope,
)
from rhinecode.trace.recorder import (
    NullRecorder,
    TraceRecorder,
    TraceRecorderProtocol,
    create_recorder,
)

__all__ = [
    "TraceEventType",
    "SCOPE_MAIN",
    "SCOPE_SUMMARY",
    "SCOPE_NOTES",
    "SCOPE_WEB_EXTRACT",
    "isolated_scope",
    "subagent_scope",
    "MAX_FIELD_CHARS",
    "MAX_MESSAGE_ITEMS",
    "REDACTED",
    "clip",
    "redact_config",
    "agent_event_payload",
    "default_trace_path",
    "TraceRecorder",
    "NullRecorder",
    "TraceRecorderProtocol",
    "create_recorder",
]
