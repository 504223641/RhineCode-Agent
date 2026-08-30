# 扩展新 Provider 与新工具

> RhineCode 用户手册 · [返回手册目录](README.md) · [返回项目 README](../../README.md)

## 扩展新 Provider

1. 在 `rhinecode/provider/` 下新建实现文件，继承 `BaseProvider` 并实现 `stream_chat`。
2. 在 `rhinecode/provider/factory.py` 的 `create_provider` 中添加对应分支。
1. 在 `config.yaml` 中将 `protocol` 改为新值。

如果新 Provider 要支持工具调用，需要参考 `deepseek.py`：

- 把 `tools` schema 传给模型 API。
- 从流式响应中拼接工具调用参数。
- 产出 `StreamChunk(type="tool_call")`。
- 能序列化历史中的 `assistant(tool_calls)` 与 `role="tool"` 消息。

## 扩展新工具

1. 在 `rhinecode/tools/` 下新建工具实现，继承 `Tool`。
2. 声明 `name`、`description`、`parameters`、`read_only`。
1. 在 `ToolRegistry.default()` 中注册工具。
2. 文件类工具必须复用 `path_guard.py` 的路径边界校验。
3. 若要纳入细粒度权限控制，在 `permission/adapter.py` 的 `_TOOL_MAP` 加一行映射（映射到 Bash/Read/Edit/Write 规则名与对应 specifier）；未映射的工具自动落到 `other` 分支（仅按工具名匹配整工具规则 + 走权限模式兜底），不会漏过权限检查。

`read_only=True` 的工具经权限引擎放行后可并发执行；`read_only=False` 的工具串行执行，并按权限系统决策决定是否在执行前请求用户确认。
