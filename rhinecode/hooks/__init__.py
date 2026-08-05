"""
Hook 系统（c12）：在 Agent 生命周期的固定节点上挂用户声明的自动化动作。

一条规则由三要素描述——**事件**（何时触发）+ **条件**（可省，省略即无条件）
+ **动作**（做什么），从两层 YAML（用户级 / 项目级）声明式加载。

## 包内分层

    models      纯数据（零依赖）
      ↑
    conditions  条件求值（零 I/O）
      ↑
    parser      YAML → 规则 + 集中校验（零 I/O）
      ↑
    config      两层文件定位与加载
    actions     四种动作的执行器
    report      报告渲染（零 I/O）
      ↑
    manager     编排：分发、once 状态、结论合并、注入队列、统计、trace 埋点

## 依赖方向

本包**不是叶子包**：它依赖 `permission`（复用匹配算法、①危险命令黑名单、
②′网络硬校验）与 `trace`（埋点）。因此 `permission` / `trace` / `skills`
**绝不可反向依赖本包**——那会成环。

## 两条不可违反的性质

1. **Hook 只能收紧，不能放宽**（spec 决策 1A）。`HookDecision` 里没有 ALLOW，
   `pre_tool_use` 的结论只能是 DENY / ASK / 不表态。这是本章安全论证的全部依据：
   Hook 加进来之后「能通过的调用集合」只会变小，C6 的五层不变量原样成立。
2. **加锁临界区只做纯内存读写**（`manager.py`）。动作执行会起子进程、发网络请求，
   持锁执行会让一个超时的 Hook 锁死整个 manager，表现为界面假死而调用栈上无线索。
"""

from rhinecode.hooks.models import (
    ActionOutcome,
    AgentAction,
    CommandAction,
    Condition,
    DispatchResult,
    EMPTY_DISPATCH,
    HookAction,
    HookDecision,
    HookEventType,
    HookPayload,
    HookRule,
    HookVerdict,
    HttpAction,
    Matcher,
    NO_VERDICT,
    PromptAction,
)

__all__ = [
    "ActionOutcome",
    "AgentAction",
    "CommandAction",
    "Condition",
    "DispatchResult",
    "EMPTY_DISPATCH",
    "HookAction",
    "HookDecision",
    "HookEventType",
    "HookPayload",
    "HookRule",
    "HookVerdict",
    "HttpAction",
    "Matcher",
    "NO_VERDICT",
    "PromptAction",
]
