"""
主对话待办清单的存放与写入（todo-list 扩展 T3，spec F1/F3/F5/F6，N3）。

## 唯一的写入方式是「用一份完整清单替换当前清单」

模型每次传完整列表，不传增量指令。

### ⚠ C15 的共享清单是增量的，原因不是它更省 token，是并发

`team/board.py` 有 N 个并发写入方（主 Agent + 全部队员），覆写语义在那里
是**错的**：甲提交整表时会静默抹掉乙刚加的那条（典型 lost update），
而两边都不报错。那份清单为此付出的代价是一整套原子认领与 TOCTOU 防护。

主对话的待办只有**一个**写入方、串行跑在 Agent Loop 里，这个约束不存在。
于是可以选更省心的那个：模型不必维护标识、不必维护状态机、
不会出现「改了一条漏了另一条」的中间态，删除一条也变成了免费操作
（下次覆写时不带它即可）。

## ⚠ 加锁不变量（与 C11 `SkillManager`、C12 `HookManager`、C13 `TaskManager`、
## C15 `TaskBoard`、C16 分类器同一条）

**临界区只做纯内存读写**，一切埋点与跨线程调度在锁外。

本项目已因「加锁临界区内做跨线程调度」死锁**五次**，每次的表现都一样：
整个 TUI 冻结，而调用栈上没有任何线索。

本类**刻意不持有任何回调**，从结构上杜绝违反——没有可调的东西，
就不可能在持锁时调它。`tests/test_todo_store.py` 有一条结构护栏遍历
实例属性，断言不存在 callable 成员。

⚠ **`_recorder` 是唯一的例外，而它不是回调**：它是行为记录器，
方向是「本类 → 记录器」而不是「本类 → 外部逻辑」，且**只在锁外调用**。
结构护栏对它显式豁免——把这个区别写下来，免得后来的人把豁免当成
「这条不变量可以商量」。

## 对外一律返回结构化结果，不抛异常

`replace` 返回带 `ok` / `reason` 的结果对象。理由：直接调用方是**工具**，
而工具的契约是「不得向上抛异常，否则 Agent Loop 会把它变成一条
『工具执行异常』，丢掉这里已经组织好的可读原因」——而可读原因正是
模型自我纠正的唯一依据。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from rhinecode.todo.models import TodoItem, TodoState, parse_state
from rhinecode.trace import TraceEventType

# 一份清单最多容纳多少条。
#
# ⚠ **这是拒绝线，不是截断线**（spec F6）。截断看起来更「健壮」，实际是
# 本项目通篇最忌讳的静默降级：模型以为整份都写进去了，而清单上少了几条，
# 它下一轮据此做的判断全是错的——而界面上完全看不出异常。
#
# 30 这个数量级的依据：一份人能一眼扫完的计划表通常在 10 条以内，
# 20 条已经该拆任务了。设 30 是留足余量之后的硬顶，只为挡住
# 「模型把整个代码库的文件列成了待办」这种失控形态。
MAX_ITEMS = 30


@dataclass(frozen=True)
class ReplaceResult:
    """
    一次覆写的结果。

    :param ok: 是否成功
    :param reason: 失败原因（可读中文，**直接回灌给模型**）。
        成功时为空串。
    """

    ok: bool
    reason: str = ""


def _parse(raw: object) -> tuple[list[TodoItem], str]:
    """
    把模型给的原始参数解析成待办列表。**纯函数，在锁外跑。**

    :param raw: 工具参数里的 `todos` 值，形态完全不可信（模型生成的）
    :returns: `(条目列表, "")` 或 `([], 失败原因)`

    ## ⚠ 校验必须在写入之前**全部**跑完（spec F5）

    边解析边写的话，一份「前三条合法、第四条非法」的输入会在清单上留下
    半张表——而调用方拿到的是「失败」，于是它以为清单没变。
    两边对同一份数据的认知不一致，且都不报错。

    ## 失败文案的要求

    **必须指明是第几条**。模型据此能自我纠正（改那一条重试），
    只说「参数不合法」的话它只能整份重猜——这与 C13 `_unknown_agent_text`、
    C15 `_not_found_locked` 是同一条经验。

    副作用：无。
    """
    if raw is None:
        return [], "缺少 todos 参数。它应该是一个数组，每个元素形如 " '{"title": "要做的事", "state": "pending"}。'
    if not isinstance(raw, list):
        return [], (
            f"todos 应该是一个数组，收到的是 {type(raw).__name__}。"
            '每个元素形如 {"title": "要做的事", "state": "pending"}。'
        )
    if len(raw) > MAX_ITEMS:
        return [], (
            f"待办条数超出上限：收到 {len(raw)} 条，最多 {MAX_ITEMS} 条。"
            "清单**没有被截断、也没有被写入**——请把任务合并成更粗的几步再提交。"
        )

    items: list[TodoItem] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            return [], (
                f"第 {index} 条不是对象，收到的是 {type(entry).__name__}。"
                '每一条都应形如 {"title": "要做的事", "state": "pending"}。'
            )
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return [], (
                f"第 {index} 条的 title 为空或不是字符串。"
                "每一条待办都要有一句能看懂的标题（祈使句，比如「改 login 接口」）。"
            )
        state = parse_state(entry.get("state"))
        if state is None:
            return [], (
                f"第 {index} 条的 state 认不出：{entry.get('state')!r}。"
                "合法取值只有三个：pending（待办）、in_progress（进行中）、"
                "completed（已完成）。"
            )
        items.append(TodoItem(title=title.strip(), state=state))
    return items, ""


class TodoStore:
    """
    主对话的待办清单。一个会话一份，由装配层建、协调层持有。

    线程模型：内部一把 `threading.Lock`，全部公开方法自己加锁。
    调用方（工具、协调层、界面）**不需要**也**不应该**在外面再加锁。

    `snapshot()` 返回内部元组本身而**不复制**——`TodoItem` 是不可变的，
    元组也是，因此调用方在锁外拿到的东西不可能被别的线程改到
    （替换是整体换一个新元组，旧的那份纹丝不动）。
    这是 `frozen=True` 换来的直接好处，与 C15 `TaskBoard` 每次 `replace()`
    出副本的做法刻意不同。
    """

    def __init__(self, recorder=None) -> None:
        self._lock = threading.Lock()
        self._items: tuple[TodoItem, ...] = ()
        # 版本号：**界面刷新的唯一依据**。
        #
        # 每次成功覆写加一，失败不加。界面侧记住上次画的版本号，只有变了才重绘。
        # 这样做买到三件事：① 工作线程零成本判断「要不要通知界面」（读一个整数，
        # 不变就不发起任何跨线程调用）；② 界面层**不需要认识工具名**；
        # ③ 重绘天然幂等。
        self._version = 0
        # 行为记录器。`None` 时全部埋点退化成零成本空调用。
        # ⚠ 它不是回调（见类 docstring 里的说明），且只在锁外调用。
        self._recorder = recorder

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #

    def replace(self, raw: object) -> ReplaceResult:
        """
        用一份完整清单替换当前清单（spec F3）。**唯一的写入口。**

        :param raw: 工具参数里的 `todos` 值，形态完全不可信
        :returns: `ReplaceResult`

        执行流程：
        1. **锁外**跑 `_parse` 做全量校验。失败则**直接返回、根本不进锁**
           ——被拒时清单一个字节都不改（spec F5）。
        2. **锁内**整体替换元组并把版本号加一。纯内存读写，不调任何外部逻辑。
        3. **锁外**埋点，然后返回。

        副作用：成功时改内部状态；向行为记录器追加一条事件。
        """
        items, error = _parse(raw)
        if error:
            self._emit(ok=False, reason=error, items=())
            return ReplaceResult(ok=False, reason=error)

        with self._lock:
            self._items = tuple(items)
            self._version += 1
            # 埋点要用的数字在锁内取好，出锁之后不再碰内部状态——
            # 出锁再读会读到别人刚写进去的那一份，记下来的与本次覆写不是一回事。
            snapshot = self._items
            version = self._version

        self._emit(ok=True, reason="", items=snapshot, version=version)
        return ReplaceResult(ok=True)

    def clear(self) -> None:
        """
        清空整份清单（spec F17：`/clear` / `/resume`）。

        ⚠ **版本号刻意继续递增而不是归零。** 界面侧靠「版本号变了」判断要不要
        重绘，归零会让「清空」这件事在界面看来可能等于「什么都没发生」
        （旧值恰好是 0 时）。界面那边另有一次显式复位（`_reset_display_state`），
        两处配合才完整。

        副作用：改内部状态。
        """
        with self._lock:
            if not self._items:
                # 已经是空的：不动版本号，避免每次 `/clear` 都触发一次无谓重绘。
                return
            self._items = ()
            self._version += 1

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    def snapshot(self) -> tuple[TodoItem, ...]:
        """
        当前清单的只读快照，**保持模型给的原始顺序**。

        :returns: `TodoItem` 元组（不可变，无需复制）

        ⚠ **原序不可重排。** 那是模型表达的执行次序，重排会让清单读起来
        不像一份计划。显示时的优先级排序只发生在 `render.build_view` 里，
        且**只影响显示**。

        副作用：无。
        """
        with self._lock:
            return self._items

    def version(self) -> int:
        """当前版本号（界面靠它判断要不要重绘）。"""
        with self._lock:
            return self._version

    def counts(self) -> tuple[int, int]:
        """
        `(已完成条数, 总条数)`。展示与空态判断用。

        副作用：无。
        """
        with self._lock:
            items = self._items
        return sum(1 for i in items if i.state is TodoState.COMPLETED), len(items)

    def all_completed(self) -> bool:
        """
        清单是否**非空且全部完成**。

        ⚠ **空清单返回 `False`**：空不是「全做完了」。这条直接决定
        「全部完成」那行记录会不会在一个从来没列过待办的会话里凭空冒出来。

        副作用：无。
        """
        with self._lock:
            items = self._items
        return bool(items) and all(i.state is TodoState.COMPLETED for i in items)

    # ------------------------------------------------------------------ #
    # 埋点（**一律在锁外调用**）
    # ------------------------------------------------------------------ #

    def _emit(
        self,
        *,
        ok: bool,
        reason: str,
        items: tuple[TodoItem, ...],
        version: Optional[int] = None,
    ) -> None:
        """
        受保护的埋点漏斗。

        **吞掉一切异常**：记录失败最多是时间线上少一行，而异常逃逸会让一次
        正常的覆写变成「工具执行异常」——观测设施绝不能反过来阻断被观测的系统。
        与 `agent/loop.py` 的 `_safe_emit`、`team/service.py` 的 `_emit` 同一条口径。
        """
        if self._recorder is None:
            return
        try:
            self._recorder.emit(
                TraceEventType.TODO_UPDATE,
                ok=ok,
                reason=reason,
                total=len(items),
                completed=sum(1 for i in items if i.state is TodoState.COMPLETED),
                in_progress=sum(1 for i in items if i.state is TodoState.IN_PROGRESS),
                version=version if version is not None else 0,
                titles=[i.title for i in items],
            )
        except Exception:  # noqa: BLE001 —— 观测设施绝不能阻断被观测的系统
            pass


__all__ = ["MAX_ITEMS", "ReplaceResult", "TodoStore"]
