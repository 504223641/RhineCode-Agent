"""
子 Agent 系统（c13）：把子任务委派给独立上下文的 Agent。

## 这个包做什么

主对话是唯一的执行上下文，一次「找出项目里所有用到 XX 的地方」会读二十个文件，
这些内容全部沉淀在主历史里、此后每轮都要重发。本包让主 Agent 把这类子任务
委派出去：子任务在**独立的对话上下文**里跑完，只有结论回到主历史。

两条路径：

- **定义式（role）**：从空白对话起步，加载一个预定义角色（Markdown + frontmatter）。
- **分支式（branch）**：继承父对话历史与工具集，不需要角色定义，强制走后台。

## 模块分工

| 模块 | 职责 |
| --- | --- |
| `models` | 数据结构与常量表（无 IO，不依赖包内其它模块） |
| `parser` | frontmatter 解析与字段归一 |
| `discovery` | 三层目录扫描与同名覆盖 |
| `toolset` | 分层工具过滤（纯函数） |
| `tasks` | 后台任务表（线程安全） |
| `runner` | 在独立线程里跑完一个子 Agent |
| `service` | 对外门面：委派主流程、三种进后台方式 |
| `render` | 角色清单注入文本（给主对话的系统提示） |
| `report` | `/agents` 报告 |

## 依赖方向

本包依赖 `agent` / `provider` / `permission` / `context` / `hooks` / `trace` / `tools`；
`tools/run_agent.py` 反过来依赖本包。

⚠ 这形成 **`tools ↔ subagents` 的包级互相依赖**，是本项目的**第四组**
（另三组：`tools ↔ skills`、`tools ↔ mcp`、`tools ↔ web`）。
**不成环的唯一依靠是 `rhinecode/tools/__init__.py` 保持为空**——
那个文件里加任何 re-export 都会让这四组同时变成真环。

本包**不依赖** `conversation` / `tui` / `commands`：运行子 Agent 所需的外部依赖
由协调层打包成 `SubAgentRuntime` 注入（见 `runner.py`）。
"""

from rhinecode.subagents.models import (
    DEFAULT_MAX_TURNS,
    ENTRY_SUFFIX,
    FOREGROUND_TIMEOUT,
    HARD_MAX_TURNS,
    MAX_CONCURRENT,
    SOURCE_LABELS,
    UNSUPPORTED_FIELDS,
    AgentCatalog,
    AgentLoadError,
    AgentSource,
    AgentSpec,
    ShadowedAgent,
    builtin_agents_dir,
)

__all__ = [
    "AgentSpec",
    "AgentCatalog",
    "AgentSource",
    "AgentLoadError",
    "ShadowedAgent",
    "SOURCE_LABELS",
    "UNSUPPORTED_FIELDS",
    "ENTRY_SUFFIX",
    "DEFAULT_MAX_TURNS",
    "HARD_MAX_TURNS",
    "MAX_CONCURRENT",
    "FOREGROUND_TIMEOUT",
    "builtin_agents_dir",
]
