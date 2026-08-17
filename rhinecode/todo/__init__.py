"""
主对话的待办清单（todo-list 扩展）。

## 它是什么

主 Agent 的**私有进度笔记**。一个多步任务开工前它列几条，做的过程中逐条更新，
用户在历史区底部一直看得到「还剩什么」。

起点是一个具体症状：一个十几步的任务跑起来，用户只能从工具行反推进度——
而 tui-activity-fold 把连续的只读检索调用归并成一行之后，这件事更看不出来了。
**归并压掉的是「做了什么」，本清单回答的是「还剩什么」**，两者互补。

## ⚠ 这个包是叶子包

只依赖标准库与 `trace`（后者本身也只依赖标准库，是叶子→叶子的依赖，
与 `classifier` 依赖 `provider.base` + `trace` 同一先例）。

**不依赖** `team` / `subagents` / `tools` / `permission` / `conversation` / `tui`。
因此三件最容易做错的事——限高取哪几条、什么时候该隐藏、覆写合法不合法——
全部可以**脱离 Textual 与整个应用**做单元测试。

## ⚠ 它与 C15 的共享任务清单（`team/`）是两个东西

那份是为**多 Agent 抢活**设计的（认领人、阻塞依赖、原子认领），
本清单里那些字段恒空、那些方法一个也用不到。详见 `models.py` 的模块 docstring。

## 三个不变量

1. **`MAX_ITEMS` 是拒绝线不是截断线** —— 截断会让模型以为整份写进去了。
2. **加锁临界区只做纯内存读写** —— 埋点一律在锁外；本包刻意不持有回调。
3. **排序只影响显示** —— `TodoStore.snapshot()` 永远返回模型给的原序。
"""

from rhinecode.todo.models import (
    TODO_STATE_LABELS,
    TodoItem,
    TodoState,
    parse_state,
)
from rhinecode.todo.render import (
    DISPLAY_LIMIT,
    TodoRow,
    TodoView,
    build_view,
    render_all_done_text,
    render_todo_brief,
)
from rhinecode.todo.store import MAX_ITEMS, ReplaceResult, TodoStore

__all__ = [
    "DISPLAY_LIMIT",
    "MAX_ITEMS",
    "TODO_STATE_LABELS",
    "ReplaceResult",
    "TodoItem",
    "TodoRow",
    "TodoState",
    "TodoStore",
    "TodoView",
    "build_view",
    "parse_state",
    "render_all_done_text",
    "render_todo_brief",
]
