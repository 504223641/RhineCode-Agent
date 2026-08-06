"""
子 Agent 服务门面（c13 T17，spec F6/F19/F20/F22）。

**职责**：把目录、任务表、运行器组合起来，对外只暴露几个方法。
委派工具、协调层、TUI、`/agents` 命令都只跟本类打交道。

## 委派主流程

```
delegate()
  ├─ 参数校验（类型、任务描述非空）
  ├─ 查角色（找不到 → 失败并列出全部可用角色）
  ├─ 算最终工具集（空集 → 失败，不起线程不发 API）
  ├─ 并发上限（超限 → 失败并列出在跑的是谁）
  ├─ 建任务 + 起 daemon 线程
  └─ 分流
       ├─ 后台（显式 / 分支式）→ 立即返回「已转入后台」
       └─ 前台 → wait(60s)
            ├─ 真做完了 → 返回结论
            └─ 超时 / 被切后台 → 返回「已转入后台」
```

## 三种进入后台的方式，同一套机制

它们全都退化成「主 Worker 要不要继续等」：子 Agent **一律**在独立线程跑，
「前台」只是调用方选择阻塞等待它。因此「转后台」不需要在运行途中移交任何
执行状态——这是三种方式能统一实现的关键。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rhinecode.subagents.models import (
    FOREGROUND_TIMEOUT,
    MAX_CONCURRENT,
    AgentCatalog,
    AgentSpec,
)
from rhinecode.subagents.runner import (
    ParentSnapshot,
    SubAgentRuntime,
    start_subagent_thread,
)
from rhinecode.subagents.tasks import (
    BRANCH_AGENT_NAME,
    KIND_BRANCH,
    KIND_ROLE,
    TaskManager,
    TaskStatus,
)
from rhinecode.subagents.toolset import resolve_toolset

VALID_KINDS = (KIND_ROLE, KIND_BRANCH)


@dataclass(frozen=True)
class DelegateOutcome:
    """
    一次委派的结果，由委派工具转成 `ToolResult`。

    :param ok: 是否成功发起（**不是**「子 Agent 是否成功」——转后台也算成功发起）
    :param text: 回灌给模型的文本
    :param task_id: 任务标识；失败时为 `None`
    :param backgrounded: 是否走了后台（决定工具行显示什么）
    """

    ok: bool
    text: str
    task_id: Optional[str] = None
    backgrounded: bool = False


class SubAgentService:
    """
    子 Agent 的对外门面。

    :param catalog: 已扫描好的角色目录
    :param runtime: 运行子 Agent 所需的外部依赖
    :param tool_names_provider: 取「主对话当前可见工具名」的回调。
        **必须是回调**——MCP 工具是在装配期异步注册的，取值型会拿到一份
        不含 MCP 工具的陈旧快照。
    :param foreground_timeout: 前台等待秒数，测试可覆盖
    :param max_concurrent: 并发上限，测试可覆盖
    """

    def __init__(
        self,
        catalog: AgentCatalog,
        runtime: SubAgentRuntime,
        tool_names_provider,
        foreground_timeout: float = FOREGROUND_TIMEOUT,
        max_concurrent: int = MAX_CONCURRENT,
    ) -> None:
        self.catalog = catalog
        self.runtime = runtime
        self.tasks = TaskManager()
        self._tool_names_provider = tool_names_provider
        self._foreground_timeout = foreground_timeout
        self._max_concurrent = max_concurrent
        # 当前正被前台等待的任务标识。`Ctrl+B` 靠它知道该切哪个。
        # 只由 `delegate` 在自己的线程里读写，不需要锁——同一时刻只可能有
        # 一个前台等待（Agent Loop 的串行段是串行的）。
        self._foreground_task_id: Optional[str] = None

    # ------------------------------------------------------------------ #
    # 委派
    # ------------------------------------------------------------------ #

    def delegate(
        self,
        kind: str,
        agent_name: str,
        task_text: str,
        background: bool = False,
        parent: Optional[ParentSnapshot] = None,
    ) -> DelegateOutcome:
        """
        发起一次委派（spec F6/F19/F20）。

        :param kind: `role` 或 `branch`
        :param agent_name: 角色名；`kind == "branch"` 时忽略
        :param task_text: 任务描述，必须非空
        :param background: 是否直接走后台；`kind == "branch"` 时强制为真
        :param parent: 分支式的父快照，由协调层提供
        :returns: `DelegateOutcome`

        失败时**不起线程、不发任何 API 请求**——这是 spec F14/F20 的全部价值。

        副作用：可能起一个 daemon 线程；前台路径会阻塞至多 `foreground_timeout` 秒。
        """
        kind = (kind or "").strip().lower()
        if kind not in VALID_KINDS:
            return DelegateOutcome(
                ok=False,
                text=f"type 必须是 {' 或 '.join(VALID_KINDS)}，收到的是 {kind!r}。",
            )

        task_text = (task_text or "").strip()
        if not task_text:
            return DelegateOutcome(
                ok=False,
                text="task 不能为空——子 Agent 看不到你和用户的对话，"
                "任务描述必须自包含，把背景、目标和期望的产出都写进去。",
            )

        spec: Optional[AgentSpec] = None
        if kind == KIND_ROLE:
            spec = self.catalog.specs.get((agent_name or "").strip())
            if spec is None:
                return DelegateOutcome(ok=False, text=self._unknown_agent_text(agent_name))

        if kind == KIND_BRANCH:
            # 分支式强制后台（spec F8）：它继承的历史可能很长，同步等待会把
            # 主对话卡住很久；而主对话在它运行期间还会继续追加消息。
            background = True
            if parent is None:
                return DelegateOutcome(
                    ok=False,
                    text="分支式委派需要父对话快照，但当前环境没有提供——"
                    "请改用 type=role 委派给一个预定义角色。",
                )

        # ── 工具集：空集直接失败，不起线程不发 API ──
        all_names = frozenset(self._tool_names_provider())
        toolset = resolve_toolset(all_names, spec, is_background=background)
        if toolset.is_empty:
            return DelegateOutcome(ok=False, text=toolset.reason)

        # ── 并发上限 ──
        if self.tasks.running_count() >= self._max_concurrent:
            return DelegateOutcome(
                ok=False,
                text=(
                    f"同时运行的子 Agent 已达上限 {self._max_concurrent} 个，"
                    f"当前在跑：{self.tasks.running_brief()}。"
                    "请等其中一个完成后再委派，或用 /agents 查看进度。"
                ),
            )

        record = self.tasks.create(
            kind,
            spec.name if spec is not None else BRANCH_AGENT_NAME,
            task_text,
        )
        start_subagent_thread(
            self.runtime, spec, task_text, record, self.tasks,
            toolset, all_names, parent,
        )

        if background:
            return DelegateOutcome(
                ok=True,
                text=self._backgrounded_text(record.task_id, record.agent_name),
                task_id=record.task_id,
                backgrounded=True,
            )

        # ── 前台：阻塞等待，超时或被切后台则放手 ──
        self._foreground_task_id = record.task_id
        try:
            record.done_event.wait(self._foreground_timeout)
        finally:
            self._foreground_task_id = None

        if record.status.is_terminal and not record.backgrounded:
            return DelegateOutcome(
                ok=record.status is TaskStatus.COMPLETED,
                text=record.conclusion,
                task_id=record.task_id,
            )

        # 超时了、或用户按了 Ctrl+B。任务照跑，结论稍后自动送达。
        self.tasks.mark_backgrounded(record.task_id)
        return DelegateOutcome(
            ok=True,
            text=self._backgrounded_text(record.task_id, record.agent_name, timed_out=True),
            task_id=record.task_id,
            backgrounded=True,
        )

    # ------------------------------------------------------------------ #
    # 后台控制
    # ------------------------------------------------------------------ #

    def foreground_task_id(self) -> Optional[str]:
        """当前正被前台等待的任务标识；没有则 `None`（`Ctrl+B` 用）。"""
        return self._foreground_task_id

    def request_background(self) -> Optional[str]:
        """
        把当前前台等待中的任务切到后台（spec F19 第三种方式）。

        :returns: 被切的任务标识；当前没有前台任务时 `None`

        由 TUI 的 `Ctrl+B` 在**主线程**调用，而等待方在 Worker 线程里阻塞。
        置 `done_event` 让它立刻醒来。

        副作用：改任务的 `backgrounded` 标志并置 `done_event`。
        """
        task_id = self._foreground_task_id
        if task_id is None:
            return None
        return task_id if self.tasks.mark_backgrounded(task_id) else None

    def cancel(self, target: Optional[str]) -> str:
        """
        取消任务（`/agents cancel`，spec F22/F24）。

        :param target: 任务标识；`None` 表示全部
        :returns: 给用户看的结果文本
        """
        if target is None:
            count = self.tasks.cancel_all()
            if not count:
                return "当前没有正在运行的子 Agent。"
            return f"已请求取消 {count} 个子 Agent 任务。"

        if self.tasks.cancel(target):
            return f"已请求取消任务 {target}。"

        record = self.tasks.get(target)
        if record is None:
            return f"没有标识为 {target} 的任务。用 /agents 查看当前任务列表。"
        return f"任务 {target} 已经结束（{record.status.value}），无需取消。"

    def cancel_all_for_session_switch(self) -> int:
        """
        会话切换（`/clear` / `/resume` / 退出）时取消全部任务（spec F22）。

        :returns: 被取消的数量，供调用方并进提示文案

        与 `cancel(None)` 的区别只是返回值类型——这里给数字，让协调层
        自己组织文案（它要把这句拼进「对话历史已清空」那条消息里）。
        """
        return self.tasks.cancel_all()

    # ------------------------------------------------------------------ #
    # 文本
    # ------------------------------------------------------------------ #

    def _unknown_agent_text(self, name: str) -> str:
        """
        角色不存在时的回灌文本。

        **必须列出全部可用角色名**：模型据此能自我纠正（改用正确的名字重试），
        只说「角色不存在」的话它只能猜。
        """
        available = "、".join(self.catalog.specs) or "（当前一个角色都没有加载）"
        return (
            f"没有名为 {name!r} 的角色。当前可用：{available}。\n"
            "请从中选一个重新委派；都不合适的话，就自己动手做，"
            "或者用 type=branch 开一条继承当前对话的分支。"
        )

    @staticmethod
    def _backgrounded_text(
        task_id: str, agent_name: str, timed_out: bool = False
    ) -> str:
        """转入后台时的回灌文本。"""
        head = (
            f"子 Agent {agent_name} 前台等待超时，已转入后台继续运行。"
            if timed_out
            else f"已在后台启动子 Agent {agent_name}。"
        )
        return (
            f"{head}任务标识 {task_id}。\n"
            "**它跑完之后结论会自动送达，你不需要去取、也不要反复询问进度。**"
            "现在请继续处理其它事情；如果接下来的工作依赖它的结果，"
            "就先告诉用户你在等它。"
        )


__all__ = ["DelegateOutcome", "SubAgentService", "VALID_KINDS"]
