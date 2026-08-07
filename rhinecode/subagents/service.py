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
  └─ **立即返回**（永不阻塞）
```

## ⚠ 发起与等待是分离的（c13 修订）

初版在这里同步等结果（前台 60 秒），造成两个真实缺陷：

1. **多个委派串行**——委派工具是 `system_serial`，等待独占串行桶，
   三个各花 0.6 秒的子 Agent 要跑 1.86 秒（实测）；
2. **结论要等到下一条用户消息**——与之配套的异步交付点挂在协调层的
   `_run()` 上，主 Agent 在同一次运行里跑六轮也拿不到，任务被变相中断。

现在：**本层只负责起线程**，等待交给 Agent Loop 在「模型准备自然结束」时
统一处理（见 `agent/gate.py`）。于是并行天然成立，结论也能在同一次运行内回流。

`background` 参数的语义随之变成「**这次我要不要这个结果**」：
缺省要（循环会等），传 true 不要（循环不停留）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from rhinecode.subagents.models import (
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
from rhinecode.tools.path_guard import main_project_root
from rhinecode.worktree import ProvisionEntry, WorktreeError, create as create_worktree

# 隔离声明目前唯一的合法取值。与 `parser._ISOLATION_WORKTREE` 是同一个字符串，
# 但那边是「解析时认哪些写法」，这边是「判定时比什么」——两处职责不同，
# 各留一份比互相 import 更清楚（本模块不该反向依赖 parser）。
ISOLATION_WORKTREE = "worktree"

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


def resolve_isolation(
    spec_declared: Optional[str], call_requested: Optional[bool]
) -> bool:
    """
    算出这次委派要不要隔离工作区（c14 F14）。**纯函数。**

    :param spec_declared: 角色 frontmatter 里的 `isolation`（`"worktree"` 或 None）
    :param call_requested: 本次调用的 `isolation` 参数（True / False / None=未表态）
    :returns: 最终是否隔离

    **合并方向是单向加严**：

    | 角色声明 | 调用要求 | 结果 |
    | --- | --- | --- |
    | worktree | 任意（含 False） | **隔离** —— 调用方关不掉 |
    | 未声明 | True | 隔离 |
    | 未声明 | False / 未表态 | 不隔离 |

    「调用方关不掉」这条不是随手加的：写角色定义是**人在表达约束**，
    而调用方是模型。让模型能撤销用户设的隔离，等于把一道安全边界的开关
    交给了被约束的一方。

    这与 C13 已有的三处同形：Hook 只能收紧不能放宽、子 Agent 权限档取
    `min(主对话档, 角色声明档)`、工具集三层过滤只减不增。安全论证直接沿用。
    """
    if spec_declared == ISOLATION_WORKTREE:
        return True
    return bool(call_requested)


class SubAgentService:
    """
    子 Agent 的对外门面。

    :param catalog: 已扫描好的角色目录
    :param runtime: 运行子 Agent 所需的外部依赖
    :param tool_names_provider: 取「主对话当前可见工具名」的回调。
        **必须是回调**——MCP 工具是在装配期异步注册的，取值型会拿到一份
        不含 MCP 工具的陈旧快照。
    :param max_concurrent: 并发上限，测试可覆盖
    """

    def __init__(
        self,
        catalog: AgentCatalog,
        runtime: SubAgentRuntime,
        tool_names_provider,
        max_concurrent: int = MAX_CONCURRENT,
        provision_entries: tuple = (),
    ) -> None:
        self.catalog = catalog
        self.runtime = runtime
        self.tasks = TaskManager()
        self._tool_names_provider = tool_names_provider
        self._max_concurrent = max_concurrent
        # c14 F10：隔离工作区的环境初始化清单，来自 config.yaml。
        # 缺省为空 = 隔离工作区就是一次纯 checkout。
        self._provision_entries: tuple[ProvisionEntry, ...] = tuple(provision_entries)

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
        plan_stage: bool = False,
        isolation: Optional[bool] = None,
    ) -> DelegateOutcome:
        """
        发起一次委派（spec F6/F19/F20）。

        :param kind: `role` 或 `branch`
        :param agent_name: 角色名；`kind == "branch"` 时忽略
        :param task_text: 任务描述，必须非空
        :param background: 模型是否声明「这次不要这个结果」；`kind == "branch"` 时强制为真
        :param parent: 分支式的父快照，由协调层提供
        :param plan_stage: 是否处于 Plan Mode 的**规划阶段**（spec F19a）。
            为真时只允许委派给最终工具集**全只读**的角色——Plan Mode 的承诺是
            「批准前不动手」，一个能写文件的子 Agent 会直接绕过它
        :param isolation: 本次调用是否要求隔离工作区（c14 F14）。
            与角色声明**单向加严**合并，见 `resolve_isolation`
        :returns: `DelegateOutcome`

        失败时**不起线程、不发任何 API 请求**——这是 spec F14/F20 的全部价值。

        副作用：可能起一个 daemon 线程。**本方法永不阻塞。**
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
            # 分支式强制不等（spec F8）：它继承的历史可能很长、跑得久，
            # 而主对话在它运行期间还会继续追加消息——语义上它就是一条
            # 「另开一路去查，回头再说」的支线。
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

        # ── 规划阶段：只许委派全只读的角色（spec F19a）──
        #
        # 放在这里而不是更早：要先算出**最终**工具集才知道它会不会写。
        # 角色声明了 `write_file` 但被黑名单减掉时应当放行，只看声明会误判。
        if plan_stage:
            writable = self._writable_tools(toolset.allowed)
            if writable:
                return DelegateOutcome(
                    ok=False, text=self._plan_stage_text(kind, agent_name, writable)
                )

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

        # ── 隔离工作区（c14 F6-F12）──
        #
        # 位置在并发上限**之后**：上限没过就不该在磁盘上留下任何东西。
        # 在建任务记录**之前**：创建失败时连记录都不该产生，否则 `/agents` 里
        # 会多出一条永远不会开始的任务。
        want_isolation = resolve_isolation(
            spec.isolation if spec is not None else None, isolation
        )
        handle = None
        if want_isolation:
            try:
                handle, provision = self._create_worktree(
                    spec.name if spec is not None else BRANCH_AGENT_NAME, task_text
                )
            except WorktreeError as exc:
                # ⚠ **明确失败，绝不降级为「在主项目根里跑」**（spec F12）。
                # 降级是本项目通篇最忌讳的形态：用户配了隔离却没隔离，
                # 而界面上完全看不出来——子 Agent 与主 Agent 互相覆盖文件时，
                # 谁也想不到根因是隔离静默失效了。
                return DelegateOutcome(ok=False, text=self._isolation_failed_text(exc))

        record = self.tasks.create(
            kind,
            spec.name if spec is not None else BRANCH_AGENT_NAME,
            task_text,
        )
        if handle is not None:
            record.worktree_path = str(handle.path)
            record.worktree_branch = handle.branch
        # `background=true` = 模型明说「这次我不要这个结果」→ 循环不为它停留。
        record.awaited = not background
        start_subagent_thread(
            self.runtime, spec, task_text, record, self.tasks,
            toolset, all_names, parent, handle,
        )

        # ── 永远立即返回（c13 修订）──
        #
        # 初版在这里 `done_event.wait(60)` 同步等结果，造成两个缺陷：
        # ① 委派工具是 `system_serial`，等待独占串行桶 → **多个委派串行**
        #    （三个各 0.6 秒的子 Agent 要跑 1.86 秒，实测）；
        # ② 与之配套的「异步结论」只能等到下一条用户消息才交付 → 任务被变相中断。
        #
        # 修法是**发起与等待分离**：这里只管起线程，等待交给 Agent Loop
        # 在「准备自然结束」时统一处理（见 `agent/gate.py`）。于是并行天然成立。
        return DelegateOutcome(
            ok=True,
            text=self._started_text(record.task_id, record.agent_name, awaited=record.awaited),
            task_id=record.task_id,
            backgrounded=background,
        )

    def _create_worktree(self, agent_name: str, task_text: str):
        """
        为一次委派建隔离工作区（c14 F6-F10）。

        :returns: `(WorktreeHandle, ProvisionResult)`
        :raises WorktreeError: 任何创建失败（调用方转成明确的委派失败）

        名字由系统生成——**刻意不让模型给名字**。spec F7 允许委派方给名字，
        但那条通路目前没有产品需求（模型没有理由关心目录叫什么），而每多一个
        模型可控的字符串就多一份要校验的输入。将来真需要时，
        把名字透传进 `create` 即可，安全校验已经在 `naming` 里备好了。

        副作用：在磁盘上产生一份源码 checkout，在版本库中建一个分支。
        """
        return create_worktree(
            main_project_root(),
            name=None,
            agent_name=agent_name,
            # 用一次性随机短串区分并发委派。**刻意不用任务 ID**：
            # 任务记录要等工作区建成之后才创建（创建失败时不该留下记录），
            # 此刻还没有 ID 可用。名字只需唯一可读，不需要与任务 ID 对应——
            # 两者的关联由 `TaskRecord.worktree_path` 承担。
            task_id=uuid.uuid4().hex[:8],
            entries=self._provision_entries,
        )

    @staticmethod
    def _isolation_failed_text(exc: WorktreeError) -> str:
        """
        创建隔离工作区失败时回灌给模型的文案（spec F12）。

        要点是**说清楚为什么，并给出下一步**：模型拿到一句「失败了」只会重试，
        而这类失败重试一百次也一样（不是 Git 仓库就永远不是）。
        """
        return (
            f"委派失败：该角色要求隔离的 Git 工作目录，但创建不成功——{exc}\n\n"
            "这次委派**没有执行**，也没有降级成不隔离运行"
            "（那会让你和它同时改同一份文件而互相覆盖）。\n"
            "可选做法：改派一个不要求隔离的角色、自己动手做、"
            "或者告诉用户这个环境用不了工作区隔离。**不要重试**——"
            "这类失败重试多少次结果都一样。"
        )

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

    def _writable_tools(self, allowed: frozenset) -> list[str]:
        """
        挑出最终工具集里**有副作用**的那些。

        :param allowed: 已算好的最终工具集
        :returns: 非只读的工具名，按字典序；全只读时为空列表

        判据取注册中心里那个工具的 `read_only`，而不是名字白名单——
        新增工具时不需要回来改这里，MCP 远端工具（一律非只读）也自动被算进去。

        副作用：无（只读注册中心）。
        """
        registry = self.runtime.registry
        out: list[str] = []
        for name in sorted(allowed):
            tool = registry.get(name) if registry is not None else None
            # 查不到的名字按**有副作用**处理：宁可多挡一次，也不要因为
            # 一个查不到的工具把规划阶段的承诺放过去。
            if tool is None or not tool.read_only:
                out.append(name)
        return out

    def _plan_stage_text(self, kind: str, name: str, writable: list[str]) -> str:
        """
        规划阶段拒绝委派时的回灌文本。

        **必须列出「现在能用哪些角色」**，模型才可能自我纠正——
        只说「不许」的话它只能猜，或者干脆放弃委派、把调研全做在主对话里
        （那正是本功能要避免的）。
        """
        who = f"角色 {name!r}" if kind == KIND_ROLE and name else "分支式委派"
        readonly = self._read_only_agent_names()
        tail = (
            f"当前可用的只读角色：{'、'.join(readonly)}。"
            if readonly
            else "当前没有全只读的角色可用。"
        )
        return (
            "当前处于 Plan Mode 的**规划阶段**——批准计划之前不动手，"
            "因此只能委派给最终工具集**全只读**的角色。\n"
            f"{who} 的工具集里含有副作用工具：{'、'.join(writable)}，现在不可用。\n"
            f"{tail}\n"
            "如果这件事确实需要动手，请先用 present_plan 把计划提交给用户，"
            "获批之后再委派。"
        )

    def _read_only_agent_names(self) -> list[str]:
        """当前哪些角色的最终工具集是全只读的（规划阶段可用的那批）。"""
        all_names = frozenset(self._tool_names_provider())
        out: list[str] = []
        for agent_name, spec in self.catalog.specs.items():
            result = resolve_toolset(all_names, spec)
            if not result.is_empty and not self._writable_tools(result.allowed):
                out.append(agent_name)
        return out

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
    def _started_text(task_id: str, agent_name: str, awaited: bool) -> str:
        """
        委派成功时回灌给模型的文本。

        两种措辞差别很大，因为它要让模型建立**正确的预期**：

        - `awaited=True`（缺省）：结论会在你收工之前自动回到你手里，
          所以**现在就可以接着干别的**，不必守着它、更不必反复问进度；
        - `awaited=False`（模型自己传了 `background=true`）：这次不会为它停留，
          结论要等到下一条用户消息才送达。

        没有这段说明的话，模型拿到一个任务标识，最自然的下一步就是去查它
        ——而本章刻意不提供查询工具（结论是推过去的）。
        """
        head = f"已启动子 Agent {agent_name}，任务标识 {task_id}。"
        if awaited:
            return (
                f"{head}\n"
                "**它跑完之后结论会自动回到你手里**（在你结束本轮回答之前），"
                "你不需要去取、也不要反复询问进度。\n"
                "现在可以继续做别的事；如果接下来的工作依赖它的结果，直接往下推进即可，"
                "系统会在结论到位后再让你继续。"
            )
        return (
            f"{head}\n"
            "你声明了 `background=true`，因此**本轮不会为它停留**，"
            "结论会在它跑完之后的下一条用户消息时送达。\n"
            "现在请继续处理其它事情，不要等它、也不要反复询问进度。"
        )


__all__ = ["DelegateOutcome", "SubAgentService", "VALID_KINDS"]
