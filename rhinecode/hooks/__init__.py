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

## 三条不可违反的性质

1. **Hook 只能收紧，不能放宽**（spec 决策 1A）。`HookDecision` 里没有 ALLOW，
   `pre_tool_use` 的结论只能是 DENY / ASK / 不表态。这是本章安全论证的全部依据：
   Hook 加进来之后「能通过的调用集合」只会变小，C6 的五层不变量原样成立。
2. **加锁临界区只做纯内存读写**（`manager.py`）。动作执行会起子进程、发网络请求，
   持锁执行会让一个超时的 Hook 锁死整个 manager，表现为界面假死而调用栈上无线索。
3. **拦截类 fail-closed，其余 fail-open**（spec F7）。`pre_tool_use` 上 Hook 自身
   跑失败即按拦截处理——写坏的安全 Hook 必须**可见地**坏掉，而不是静默失效。

## 缺省零回归

两层 `hooks.yaml` 都不存在时，装配层给出的是 `NullHookManager`：
全部分发都是零成本空操作，负载构造器一次都不会被调用，运行时行为与 C11 逐字一致。
"""

from typing import Any, Callable, Optional

from rhinecode.hooks.config import (
    SOURCE_PROJECT,
    SOURCE_USER,
    load_all,
    project_config_path,
    scaffold_user_config,
    user_config_path,
)
from rhinecode.hooks.manager import HookManager
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


class NullHookManager:
    """
    「没有任何 Hook」的空实现，与 `trace.NullRecorder` 同形态、同理由。

    ## 为什么用空对象而不是 `Optional[HookManager]` + 判空

    分发点有五处（Agent Loop / 协调层 / 上下文管理器 / TUI / 装配层），其中
    Agent Loop 那处还分散在三个方法里。让每个调用点各写一次
    `if self._hooks is not None:` 是典型的「漏一处不报错」形态——漏掉的那处
    在没有 Hook 时会崩，而**恰恰是没有 Hook 才是绝大多数用户的常态**，
    于是这个崩溃只有少数装了 Hook 的人不会遇到。

    空对象让全部分发点写成无条件调用，漏不了。

    接口与 `HookManager` **必须逐个对齐**——新增公开方法时两边都要加。
    好在漏改会当场 `AttributeError`，不是静默错误。
    """

    enabled = False
    rules: list = []
    warnings: list = []

    def bind_context(self, session_id: str = "", cwd: str = "") -> None:
        """什么都不做（没有负载要填）。"""

    def has_listeners(self, event: HookEventType) -> bool:
        """恒为 False——调用方据此跳过负载构造。"""
        return False

    def dispatch(
        self,
        event: HookEventType,
        payload_factory: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> DispatchResult:
        """
        什么都不做，返回共享的空结果。

        **刻意不调用 `payload_factory`**：这正是 spec N7「零命中低开销」的落点。
        """
        return EMPTY_DISPATCH

    def consume_injections(self) -> str:
        """恒为空串。"""
        return ""

    def report(self) -> str:
        """`/hooks` 在未启用时的报告。"""
        return (
            "当前没有加载任何 Hook 规则。\n"
            "\n"
            "在下面任一位置写 hooks.yaml 即可启用：\n"
            f"  用户级：{user_config_path()}\n"
            f"  项目级：<项目根>/.rhinecode/hooks.yaml"
        )

    def project_notice(self) -> Optional[str]:
        """恒为 None（没有项目级规则可提示）。"""
        return None


__all__ = [
    # 编排
    "HookManager",
    "NullHookManager",
    # 加载
    "load_all",
    "scaffold_user_config",
    "user_config_path",
    "project_config_path",
    "SOURCE_USER",
    "SOURCE_PROJECT",
    # 数据类型
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
