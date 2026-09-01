"""
分类器的熔断状态机（c16 F15/F16/F16a）。

## 两种熔断，成因不同、给用户的说法也不同

| 熔断 | 触发条件 | 语义 |
| --- | --- | --- |
| **拦截熔断** | 连续拦 3 次，或本段对话累计拦 20 次 | 分类器多半不了解你的环境、在反复误伤，此时该把决定权交回人 |
| **失败熔断** | 连续调用失败 3 次 | 接口多半不通。防「你的 API 地址挂了，于是所有命令都跑不了而你查不出根因」 |

⚠ **失败不计入拦截计数**（F16），对齐 Claude Code 官方口径：
*Claude Code doesn't count a denial toward either threshold when a safety check
separate from auto mode refuses the classifier's own request.*
混在一起的话，一次网络故障会被当成「分类器在误伤」，给用户的说法就错了。

## ⚠ **全部**被审查的动作共用同一套计数器

不按 `scope` 分桶。分桶看似更精细，实际会让「分类器整体不可用」这件事被拆成
好几份、各自不到阈值，于是**永远不熔断**——而那正是熔断要防的状态。
护栏见 `tests/test_classifier_breaker.py`（三类各拦 1 次即触发）。
⚠ **新增一类动作时这里一行都不该改**：这句话本身就是判据。哪天有人
「顺手」给新类别加一个自己的计数器，上面那段推理当场失效，而它不会报错。

## ⚠ 加锁不变量：临界区只做纯内存读写

`record_*` 只改计数、只返回不可变快照；界面通知、行为记录、任何回调一律由
调用方在锁外做。

这是本项目**第五次**面对同一类死锁——前四次分别在
`SkillManager`、`subagents/TaskManager`、`hooks/HookManager`、`team/` 里，
每一处的 docstring 都写着同一条。共同的失败形态是：持锁期间做跨线程调度
（典型是 Textual 的阻塞式 `call_from_thread`），组成确定性死锁、整个界面冻结。

本类连回调都不持有，从结构上杜绝违反——与 `TaskManager` 的做法相同。
"""

from __future__ import annotations

import threading
from typing import Optional

from rhinecode.classifier.models import BreakerReason, BreakerState

# ── 三个阈值 ─────────────────────────────────────────────────────────────
#
# ⚠ **刻意不可配置**（spec「不做的事」，对齐官方——官方原话是
# *These thresholds are not configurable*）。
#
# 可配置化会让「分类器被静默调松」成为可能：一个项目级配置把阈值设成 9999，
# 熔断就等于不存在，而界面上看不出任何异常。而熔断本身是**安全机制的一部分**
# ——拦截熔断把决定权交回人，失败熔断让根因可见。
CONSECUTIVE_BLOCKS = 3
TOTAL_BLOCKS = 20
CONSECUTIVE_FAILURES = 3


class CircuitBreaker:
    """
    熔断计数与状态。**线程安全**：主对话与全部子 Agent 共用同一个实例。

    :ivar _consecutive_blocks: 连续拦截次数，任意一次放行即清零
    :ivar _total_blocks: 本段对话累计拦截次数，触发一次熔断后清零
    :ivar _consecutive_failures: 连续调用失败次数，任意一次成功判定即清零
    :ivar _state: 当前熔断状态快照

    ## 为什么作用域是「整个会话共享」而不是每次运行一个

    F15 说的是「本段对话累计 20 次」。而且分开计数会让「分类器整体不可用」
    被并发的子 Agent 拆成好几份、每份都不到阈值——与上面「不按 scope 分桶」
    是同一个道理的另一个维度。

    ## 恢复

    只有两条路：`note_manual_approval`（用户在面板上批准一次，F15 的恢复条件）
    与 `reset`（会话切换）。**刻意没有「过一会儿自动恢复」**——
    时间不解决任何一种成因：接口还是不通，分类器还是不了解你的环境。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_blocks = 0
        self._total_blocks = 0
        self._consecutive_failures = 0
        self._state = BreakerState(False, BreakerReason.NONE, "")

    # ------------------------------------------------------------------ #
    # 记录（三个入口，都只改内存并返回快照）
    # ------------------------------------------------------------------ #
    def record_allow(self) -> None:
        """
        记一次放行。

        重置**两个**连续计数（拦截与失败）——一次成功的放行同时证明了
        「分类器能连上」与「它不是在无差别拦截」。

        ⚠ **不重置累计拦截数**：那个计数的语义是「这段对话里它一共拦了多少次」，
        一次放行不该抹掉之前的 19 次。对齐官方：
        *Any allowed action resets the consecutive counter, while the total
        counter persists for the session.*

        副作用：修改内部计数。
        """
        with self._lock:
            self._consecutive_blocks = 0
            self._consecutive_failures = 0

    def record_block(self) -> Optional[BreakerState]:
        """
        记一次拦截。

        :returns: **本次触发了熔断**时返回新状态快照；否则返回 None

        返回值设计成「只在触发的那一次非空」，是为了让调用方的写法只有一种：
        非空就出一条界面提示。返回当前状态的话，调用方要自己比对前后差异，
        而漏比对的后果是熔断提示每次都出、或者一次都不出。

        副作用：修改内部计数；可能置位熔断状态。
        """
        with self._lock:
            self._consecutive_blocks += 1
            self._total_blocks += 1
            if self._consecutive_blocks >= CONSECUTIVE_BLOCKS:
                detail = f"连续拦下 {self._consecutive_blocks} 次"
            elif self._total_blocks >= TOTAL_BLOCKS:
                detail = f"本段对话累计拦下 {self._total_blocks} 次"
            else:
                return None
            # 触发即清零：让熔断恢复之后重新开始计数，而不是一恢复就再次撞线。
            self._consecutive_blocks = 0
            self._total_blocks = 0
            self._state = BreakerState(True, BreakerReason.BLOCKS, detail)
            return self._state

    def record_failure(self, error: str = "") -> Optional[BreakerState]:
        """
        记一次调用失败（异常 / 超时 / 第二阶段输出解析不出来）。

        :param error: 本次失败的错误描述。**会被带进快照的 `detail`**——
            spec F16 的全部价值就在于让用户看见「根因是分类器连不上」，
            只说「已熔断」而不说为什么，用户仍然查不出来
        :returns: 本次触发了熔断时返回新状态快照；否则返回 None

        副作用：修改内部计数；可能置位熔断状态。
        """
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures < CONSECUTIVE_FAILURES:
                return None
            detail = f"连续 {self._consecutive_failures} 次调用失败"
            if error:
                detail += f"；最后一次：{error}"
            self._consecutive_failures = 0
            self._state = BreakerState(True, BreakerReason.FAILURES, detail)
            return self._state

    # ------------------------------------------------------------------ #
    # 查询与恢复
    # ------------------------------------------------------------------ #
    def is_tripped(self) -> bool:
        """当前是否处于熔断。"""
        with self._lock:
            return self._state.tripped

    def state(self) -> BreakerState:
        """取状态快照（不可变，可在锁外随便渲染与埋点）。"""
        with self._lock:
            return self._state

    def reset(self) -> None:
        """
        解除熔断并清空全部计数。

        两个调用方：用户在确认面板上批准一次（F15 的恢复条件），
        以及会话切换（`/clear` / `/resume`）。

        副作用：清空全部计数与熔断状态。
        """
        with self._lock:
            self._consecutive_blocks = 0
            self._total_blocks = 0
            self._consecutive_failures = 0
            self._state = BreakerState(False, BreakerReason.NONE, "")
