"""
子 Agent 协作（c15）。

C13 给了主 Agent 委派能力，C14 给了子 Agent 文件隔离。本包让那些子 Agent
**能互相协作**：共享一块任务看板、按名字直接发消息、干完之后留在场上待命，
一条消息就能唤醒继续干。

## 包外只用得着门面

```python
from rhinecode.team import TeamService
```

`TeamService` 把花名册、共享清单、信箱三者组合起来，协作工具、协调层、
TUI 与命令层**都只跟它打交道**。包内其余模块（`board` / `roster` /
`mailbox` / `render` / `gate`）不在这里导出——它们是实现细节，
直接依赖它们会让将来的重构变成破坏性变更。

`TeamGate` 是唯一的例外候选，但它由协调层与运行器按需从
`rhinecode.team.gate` 显式导入，同样不在这里 re-export：
本包被 `tools` 依赖，而 `gate` 会 import `provider.base`，
放进这里会让每次 `import rhinecode.team` 都连带拉起 provider。

## 架构位置

**叶子包**：只依赖标准库，以及在渲染注入消息时局部 import 的
`provider.base.Message`。`subagents` / `tools` / `conversation` / `tui`
单向依赖本包，反向依赖一律不允许——那会让本包背上整个 Agent Loop。

文档见 `docs/c15/`（spec / plan / task / checklist 四份）。
"""

from rhinecode.team.service import TeamService

__all__ = ["TeamService"]
