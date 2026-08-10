"""
驱动内核：读界面状态、投递输入与按键、应答面板、导出界面文本、取消、观察记录、编排退出。

宿主进程把控制通道收到的每条指令翻译成本模块 `DriverCore` 的一次方法调用。
本模块**不认识 socket、不认识 JSON**（那是 `host.py` 与 `protocol.py` 的事），
它只认识「一个跑着的 RhineApp」和「一份行为记录」。

===============================================================================
四条不变量 —— 违反的后果分别是**确定性死锁、静默失败、随机红**
===============================================================================

**不变量① 加锁四段式：临界区内只做纯内存读写，一切跨线程调度必须在锁外。**

    正确形态：
        ① 锁内：读状态、校验、记 `_last_action`     （全是内存操作，微秒级）
        ② 出锁
        ③ 锁外：`run_on_main(...)` 投递到主线程     （可能要等几百毫秒甚至更久）
        ④ 锁外：组装返回值

    反例（**不可写**）：持锁 → `run_on_main` → 主线程恰好被一个慢回调堵住 →
    锁被无限期占住 → 同样要锁的 `cancel` 排不进去 →
    **恰恰在最需要取消的时候失去了取消能力**。

    这与 C11 `SkillManager` 的加锁教训完全同型（先在锁内决定，出锁后再驱动界面）。

**不变量② `wait` 不持驱动锁。**
    它只轮询只读快照。持锁的话，`wait` 挂着的整段时间里 `status` 都答不了，
    而 spec N4 要求「任何查询一秒内返回」。

**不变量③ 应答前必须复核「面板已展示**且**已获得焦点」。**
    判据写死为：目标面板控件 `.display is True` **且**
    `type(app.focused) is 该面板控件类`。

    ⚠️ 面板类型**必须取自待决盒的 `kind`，不能从控件类反推**——
    `ConfirmPanel` 被 `confirm`（工具确认，四个选项）与 `approve`（计划审批，两个选项）
    **两种交互复用**，看到 `ConfirmPanel` 并不能断定是哪一种。

    实测抢跑窗口占比 8869/8870：待决盒置位远早于面板真正挂载完成。
    不复核几乎必然应答在空处——而那是「驱动器返回 ok 但什么也没发生」这种
    最不该有的失败形态。

**不变量④ 跨线程一律只走 `run_on_main`，且一律带超时。**
    禁止直接用 `app.call_from_thread`：它没有超时参数、会一直阻塞到工作跑完、
    还拒绝从主线程调用。两条都会以最难排查的方式发作（见 `run_on_main` 的 docstring）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from rhinecode.agent.events import ConfirmDecision
from rhinecode.trace import reader
from rhinecode.tui.widgets import ClarifyPanel, ConfirmPanel, InputBar, SessionPanel
from tests.e2e import protocol


# 跨线程投递的缺省超时（秒）。取值依据：实测忙碌期一次快照 0.21–0.36 ms，
# 提交与结算也都是毫秒级；给到 15 秒是为了容忍主线程偶发被长回调占住，
# 同时保证「卡死」不会变成「无限期挂起」。
DEFAULT_DISPATCH_TIMEOUT = 15.0

# `wait` 的轮询间隔：起步 30 ms（够灵敏），退避到 100 ms 封顶
# （长等待时不至于打出几万次跨线程调度）。
POLL_MIN = 0.03
POLL_MAX = 0.10

# 一次 `keys` 指令最多投递多少个按键。
#
# 上限的作用不是「防滥用」——驱动者是自己人。它防的是**手滑**：
# 一个写错的循环把上万个按键投进去，Textual 会在主线程上逐个处理，
# 界面卡死几分钟而调用方只看到一次超时，排查方向完全被带偏。
MAX_KEY_SEQUENCE = 200

# `screen` 逐行读一个控件时最多读多少行。控件高度理论上可以很大（一个长滚动区），
# 而导出是给人看的，读满几千行既慢又没人看。
MAX_PAINTED_LINES = 500

# 不变量③ 的复核重试上限
SETTLE_RECHECK_TRIES = 40
SETTLE_RECHECK_INTERVAL = 0.03

# 退出编排的整体上限（秒）
SHUTDOWN_LIMIT = 20.0


class SessionState(str, Enum):
    """会话的六个可观测状态。前四个由界面推导，后两个由宿主置位。"""

    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    PENDING = "pending"
    FATAL = "fatal"
    SHUTTING_DOWN = "shutting_down"


# ---------------------------------------------------------------------------
# 跨线程投递原语（不变量④）
# ---------------------------------------------------------------------------
def run_on_main(loop: asyncio.AbstractEventLoop, coro: Any, timeout: float = DEFAULT_DISPATCH_TIMEOUT) -> Any:
    """
    在 Textual 的事件循环（主线程）上执行一个协程，并带超时取回结果。

    :param loop: 应用所在的事件循环
    :param coro: **一个协程对象**（见下方使用规则）
    :param timeout: 调用方愿意等多久
    :returns: 协程的返回值
    :raises TimeoutError: 超时（`concurrent.futures.TimeoutError`，
        在 Python 3.11+ 它就是内置的 `TimeoutError`）

    ## 为什么不用 `app.call_from_thread`（实测结论，不是推断）

    - **它没有 timeout 参数，会一直阻塞到工作跑完**。实测：主线程被一个 8 秒的回调
      堵住时，调用方整整等了 7.8 秒才返回。所以「先调用、再 `queue.get(timeout=1)`」
      这种写法**毫无作用**——`get` 是它返回之后才执行的语句。
    - **它拒绝从主线程自己调用**，会抛
      `RuntimeError: The 'call_from_thread' method must run in a different thread from the app`。

    ## ⚠️ 使用规则：同步工作一律**先包成 `async def` 再走本原语**

    反例（会写出 bug）::

        run_on_main(loop, app.some_sync_method(x), t)   # ← 错

    这行会**在调用方线程上先执行** `some_sync_method`（违反「界面状态只能在主线程
    读写」），然后把它返回的 `None` 喂给 `run_coroutine_threadsafe`，抛 `TypeError`。

    正确写法::

        async def _work() -> None:
            app.some_sync_method(x)
        run_on_main(loop, _work(), t)

    ## 超时语义

    **超时只让调用方脱身，不取消已排队的工作。** 这正是 spec N7「失败即失败」要的
    语义——超时就是超时，不能假装成功，也不能靠重试掩盖。
    """
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


# ---------------------------------------------------------------------------
# 应答者接缝（spec F6）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PanelSnapshot:
    """
    一个面板的只读快照。

    :param kind: 面板类型（confirm / clarify / approve / session），取自待决盒
    :param display: 面板**展示原文**——注意与 Textual 的 `widget.display` 同名不同义，
        见 `extract_panel` 的注释
    :param options: 可选项列表，每项 `{"id": ..., "label": ...}`，**已跳过 disabled 项**
    """

    kind: str
    display: str
    options: list[dict]


class Responder(Protocol):
    """
    「这个面板该怎么答」的决策者。

    本轮只实现外部通道一种（`ExternalResponder`）。P1b 的固定策略应答者与
    脚本预设应答者**只需换一个实现**（`source = "policy"`），`DriverCore` 一行不动
    ——这正是把接缝本轮就留出来的意义。
    """

    source: str

    def decide(self, panel: PanelSnapshot) -> str:
        """返回 `protocol.CHOICE_TABLE` 认可的决策取值。"""
        ...


class ExternalResponder:
    """
    本轮唯一的应答者实现：阻塞等待控制通道送来的 `answer` 指令。

    结算来源记为 `driver`——它标注的是「这次结算走的是控制通道」，
    不是对操作者身份的断言。
    """

    source = "driver"

    def __init__(self) -> None:
        self._event = threading.Event()
        self._slot: Optional[str] = None

    def submit(self, choice: str) -> None:
        """由控制通道的 handler 线程调用，把决策塞进槽位并唤醒等待者。"""
        self._slot = choice
        self._event.set()

    def decide(self, panel: PanelSnapshot) -> str:
        """阻塞直到外部送入决策。"""
        self._event.wait()
        self._event.clear()
        choice = self._slot
        self._slot = None
        return choice or ""


# ---------------------------------------------------------------------------
# 面板提取
# ---------------------------------------------------------------------------
# 待决盒的 kind → 承载它的面板控件类。
# ⚠️ 这张表是**单向**的：ConfirmPanel 同时服务 confirm 与 approve，
# 反过来从控件类推 kind 会得到二义结果（见不变量③）。
PANEL_WIDGET = {
    "confirm": ConfirmPanel,
    "approve": ConfirmPanel,
    "clarify": ClarifyPanel,
    "session": SessionPanel,
}


async def extract_panel(app: Any, kind: str) -> Optional[PanelSnapshot]:
    """
    从界面上提取某类面板的展示原文与可选项（**协程，只能在主线程执行**）。

    :param kind: 面板类型（取自待决盒，不要从控件类反推）
    :returns: 面板未展示时返回 None

    ## ⚠️ 命名陷阱（必读）

    Textual 的 `widget.display` 是**可见性布尔值**，与 `PanelSnapshot.display`
    （面板展示原文）**同名不同义**。

    **面板原文不在 `widget.display` 里**，而在 `OptionList` 的 **0 号 disabled
    表头**的 `prompt` 中——`ConfirmPanel.show_for` / `show_prompt` /
    `ClarifyPanel.show_for` / `SessionPanel.show_for` 四者都是先
    `add_option(Option(表头, disabled=True))`、再加可选项。

    ## 为什么跳过所有 disabled 项

    `ClarifyPanel` 在候选项之间**夹着 disabled 的详情行**（`[dim]…[/dim]` 那种）。
    不跳过的话选项下标会错位，而 `SessionPanel` 里锁定与当前会话也是 disabled 的。

    ## 为什么一律返回原始字符串

    返回控件持有的**原始字符串**（含 `[dim]` 之类的 markup 标记），
    **不做渲染、不去标记**。渲染态的对照由记录里的 `ui_message` / `status_bar`
    承载——P0 已经确立了「转义前原文」的口径，这里不另起一套。
    """
    widget_cls = PANEL_WIDGET.get(kind)
    if widget_cls is None:
        return None
    try:
        panel = app.query_one(widget_cls)
    except Exception:  # noqa: BLE001 —— 控件不存在（应用尚未挂载完）
        return None
    if not panel.display:  # ← 这个 display 是可见性布尔值
        return None

    display_text = ""
    options: list[dict] = []
    for index in range(panel.option_count):
        option = panel.get_option_at_index(index)
        prompt = str(option.prompt)
        if index == 0:
            # 0 号是 disabled 表头，它承载面板的展示原文
            display_text = prompt
            continue
        if getattr(option, "disabled", False):
            continue  # 详情行 / 锁定会话行，不是可选项
        options.append({"id": option.id, "label": prompt})

    return PanelSnapshot(kind=kind, display=display_text, options=options)


# ---------------------------------------------------------------------------
# choice → 结算值
# ---------------------------------------------------------------------------
# 线上 choice → 产品 option.id → 结算值，**三套标识必须分清**：
#
#   | kind    | 线上 choice | 产品 option.id  | via=channel 的结算值           |
#   |---------|-------------|-----------------|--------------------------------|
#   | confirm | once        | yes             | ConfirmDecision.ALLOW          |
#   | confirm | session     | yes_session     | ConfirmDecision.ALLOW_SESSION  |
#   | confirm | permanent   | yes_permanent   | ConfirmDecision.ALLOW_PERMANENT|
#   | confirm | deny        | no              | ConfirmDecision.DENY           |
#   | approve | yes / no    | yes / no        | True / False                   |
#   | clarify | 数字串 idx  | （走 App 私有列表） | app._clarify_options[idx].summary |
#   | session | 会话标识/cancel | 会话标识    | 交给 app._settle_session(...)  |
_CONFIRM_SETTLEMENT = {
    "once": ConfirmDecision.ALLOW,
    "session": ConfirmDecision.ALLOW_SESSION,
    "permanent": ConfirmDecision.ALLOW_PERMANENT,
    "deny": ConfirmDecision.DENY,
}

# choice → 产品 option.id，供 via=keys 路径推算光标要移动几格
CHOICE_TO_OPTION_ID = {
    "confirm": {"once": "yes", "session": "yes_session", "permanent": "yes_permanent", "deny": "no"},
    "approve": {"yes": "yes", "no": "no"},
}


def settlement_for(kind: str, choice: str, app: Any) -> Any:
    """
    把线上的 `choice` 翻译成产品侧的结算值。

    :raises ValueError: 未知 choice 或越界序号；由 `answer` 转成 `bad_request` 响应。

    ⚠️ `clarify` 的结算值要读 App 的 `_clarify_options`——**这是刻意读私有属性**：
    产品侧的结算路径本身就是这么算的（见 `on_option_list_option_selected` 的
    clarify 分支：`self._clarify_options[idx].summary`）。走同一份计算是有意的，
    照抄一份「等价实现」反而会在产品改了算法时静默分叉。
    **代价是：产品若改了该属性名，本处会一起失效**——已登记为成对维护点。
    """
    if kind == "confirm":
        value = _CONFIRM_SETTLEMENT.get(choice)
        if value is None:
            raise ValueError(f"confirm 面板不认识 choice={choice!r}")
        return value

    if kind == "approve":
        if choice not in ("yes", "no"):
            raise ValueError(f"approve 面板不认识 choice={choice!r}")
        return choice == "yes"

    if kind == "clarify":
        try:
            idx = int(choice)
        except ValueError:
            raise ValueError(f"clarify 面板的 choice 必须是序号，收到 {choice!r}") from None
        options = getattr(app, "_clarify_options", None) or []
        if idx < 0 or idx >= len(options):
            raise ValueError(f"clarify 序号 {idx} 越界：当前只有 {len(options)} 个选项")
        return options[idx].summary

    raise ValueError(f"未知面板类型 {kind!r}")


# ---------------------------------------------------------------------------
# 驱动内核
# ---------------------------------------------------------------------------
class DriverCore:
    """
    一个跑着的 RhineApp 的驱动器。方法与控制通道的指令一一对应。

    线程模型：本类的方法**几乎全部在 socket 的 handler 线程上被调用**，
    只有 `shutdown_on_main` 是例外（它只能在主线程调用，见其 docstring）。
    """

    def __init__(
        self,
        app: Any,
        pilot: Any,
        loop: asyncio.AbstractEventLoop,
        build_result: Any,
        responder: Responder,
        *,
        turn_budget: int,
        trace_path: Path,
    ):
        """
        :param app: 真实的 `RhineApp` 实例
        :param pilot: `app.run_test()` 给出的 Pilot（模拟按键用）
        :param loop: 应用所在的事件循环
        :param build_result: `build_app` 的产物（要用到 manager 与 recorder）
        :param responder: 应答者（本轮是 `ExternalResponder`）
        :param turn_budget: 轮次预算，见 `send` 的说明
        :param trace_path: 行为记录文件路径
        """
        self.app = app
        self.pilot = pilot
        self.loop = loop
        self.build_result = build_result
        self.responder = responder
        self.turn_budget = turn_budget
        self.trace_path = Path(trace_path)

        # 驱动锁：只保护 `_last_action` 与「状态校验 → 记动作」这段纯内存判断。
        # **绝不允许在持有它时做跨线程调度**（不变量①）。
        self._lock = threading.Lock()
        self._last_action: str = ""

    # ------------------------------------------------------------------ #
    # 只读：随时可答，不持驱动锁（不变量②）
    # ------------------------------------------------------------------ #
    async def _read_ui_state(self) -> dict:
        """
        **一次**读齐全部界面状态（协程，在主线程执行）。

        为什么要「一次读齐」：每次跨线程投递都有调度开销，而这些字段必须是**同一时刻**
        的快照——分几次读会拿到互相矛盾的状态（比如面板刚好在两次读之间关掉了）。

        ⚠️ **这里不读记录文件**，理由见 `snapshot`。
        """
        box = getattr(self.app, "_pending_interaction", None)
        kind = box.get("kind") if isinstance(box, dict) else None
        session_active = bool(getattr(self.app, "_session_panel_active", False))
        stream_active = bool(getattr(self.app, "_stream_active", False))

        # 会话面板不走待决盒机制（它由主线程发起，没有 Worker 阻塞等待），
        # 所以 kind 要单独补
        effective_kind = kind or ("session" if session_active else None)

        # ── 后台活动量（C13/C15）──────────────────────────────────────
        #
        # ⚠ **这三个量是「`idle` 不等于系统静止」的解药。**
        #
        # 三态只描述**界面**：没有流式 Worker、没有面板挂着就是 `idle`。
        # 但 C13 起，界面空闲时进程里仍可能有活：后台委派在跑、队员待命、
        # 队友消息躺在信箱里等主对话自动唤起（`_maybe_auto_wake` 每 0.5 秒
        # 在空闲时检查一次，符合条件就**自己起一条流**）。
        #
        # 于是一个 `wait` 返回 `idle` 之后，会话随时可能又忙起来——
        # 断言因此是竞态的，而竞态的判据比没有判据更坏（它偶尔通过）。
        #
        # 读的都是内存里的整数与布尔，放在主线程这一次调度里一并取，
        # 与其它界面字段同一时刻、互相自洽。
        manager = getattr(self.app, "_manager", None)
        background = {"subagents": 0, "idle_members": 0, "unread_for_main": False}
        if manager is not None:
            for key, fn in (
                ("subagents", "running_subagent_count"),
                ("idle_members", "team_idle_member_count"),
                ("unread_for_main", "team_has_unread_for_main"),
            ):
                # 逐个 getattr + try：驱动设施要能驱动**旧版本**的产品代码
                # （宿主与产品是分开演进的），少一个方法不该让 status 整个挂掉。
                try:
                    method = getattr(manager, fn, None)
                    if method is not None:
                        background[key] = method()
                except Exception:  # noqa: BLE001 —— 只读快照绝不阻断
                    pass

        panel: Optional[PanelSnapshot] = None
        panel_visible = False
        focused_type = type(self.app.focused).__name__ if self.app.focused is not None else None
        if effective_kind:
            panel = await extract_panel(self.app, effective_kind)
            widget_cls = PANEL_WIDGET.get(effective_kind)
            if widget_cls is not None:
                try:
                    panel_visible = bool(self.app.query_one(widget_cls).display)
                except Exception:  # noqa: BLE001
                    panel_visible = False

        return {
            "stream_active": stream_active,
            "pending": box is not None,
            "session_panel": session_active,
            "kind": effective_kind,
            "panel_visible": panel_visible,
            "focused": focused_type,
            "panel": panel,
            "background": background,
        }

    @staticmethod
    def _quiescent(background: dict) -> bool:
        """实例侧的薄封装，便于子类或测试覆盖。见模块级 `_is_quiescent`。"""
        return _is_quiescent(background)

    def _trace_seq(self) -> int:
        """
        记录里最后一条**可解析**事件的序号。

        取末条 seq 而不是问记录器要内部计数，是为了**不新增产品接口**——
        在「成功落盘才推进序号」的语义下（P0 F1），两者本就等价。
        """
        try:
            records, _ = reader.load_records(self.trace_path)
        except OSError:
            return 0
        return int(records[-1].get("seq", 0)) if records else 0

    def snapshot(self) -> dict:
        """
        取一次完整状态快照（界面部分 + 记录游标）。

        :returns: `status` 响应的界面部分（宿主级字段由 `host.py` 补齐）

        ## ⚠️ `trace_seq` 绝不能放进 `run_on_main`

        `wait` 每 30–100 ms 调一次本方法。把「全量重读记录文件」放到 Textual 主线程上
        等于**每秒十几次文件 IO 打在 UI 线程**，长会话下界面会明显卡顿，
        还可能顶穿 spec N4 的一秒预算。它是纯文件 IO、与界面状态无关，
        就该留在调用方线程上跑。

        ## 三态推导的顺序不能颠倒

        待决盒非空或会话面板展示中 → `PENDING`；忙碌态为真 → `BUSY`；否则 `IDLE`。
        **实测：面板挂着的时候忙碌态仍然为真**（Worker 线程正阻塞等待结算，
        流还没结束）。先判 BUSY 会让所有 `PENDING` 都被误报成 `BUSY`，
        于是 `wait` 永远等不到「需要你应答」这个终态。
        """
        ui = run_on_main(self.loop, self._read_ui_state())
        # 出了跨线程调度之后再读文件（见上）
        trace_seq = self._trace_seq()

        if ui["pending"] or ui["session_panel"]:
            state = SessionState.PENDING
        elif ui["stream_active"]:
            state = SessionState.BUSY
        else:
            state = SessionState.IDLE

        panel: Optional[PanelSnapshot] = ui["panel"]
        background = ui.get("background") or {}
        return {
            "state": state.value,
            "panel": (
                {"kind": panel.kind, "display": panel.display, "options": panel.options}
                if panel is not None
                else None
            ),
            "trace_seq": trace_seq,
            "stream_active": ui["stream_active"],
            "focused": ui["focused"],
            "panel_visible": ui["panel_visible"],
            "background": background,
            # 「系统真的停下来了」——见 `_is_quiescent`
            "quiescent": state is SessionState.IDLE and _is_quiescent(background),
        }

    def observe(self, since: int = 0, types: Optional[Iterable[str]] = None) -> dict:
        """
        读记录的增量。

        :param since: 只要 `seq > since` 的事件
        :param types: 只要这些类型（None 表示全部）
        :returns: `{next_since, skipped, timeline, events}`

        `events` 给的是**完整记录**（含 `ui_message` 与 `tool_execute` 的 payload）
        而不只是摘要行——那正是遍历界面控件取不到的那部分（spec AC34）。

        ## 末行半截不算损坏

        宿主正在写、外部正在读，最后一行经常是半截 JSON。**把它当作正常并丢弃、
        不计入 `skipped`**：否则观察指令会周期性地报告一条根本不存在的损坏，
        用不了几次就没人再信这个数字了。
        """
        try:
            records, skipped = reader.load_records(self.trace_path)
        except OSError as e:
            return {"next_since": since, "skipped": 0, "timeline": [], "events": [], "error": str(e)}

        # 末行半截：reader 会把它计进 skipped，这里扣回来一条。
        # 判据是「文件末尾没有换行符」——那说明最后一行还没写完。
        if skipped > 0:
            try:
                with open(self.trace_path, "rb") as fh:
                    fh.seek(0, 2)
                    size = fh.tell()
                    if size > 0:
                        fh.seek(size - 1)
                        if fh.read(1) != b"\n":
                            skipped -= 1
            except OSError:
                pass

        wanted = set(types) if types else None
        selected = [
            r
            for r in reader.filter_records(records, types=wanted)
            if int(r.get("seq", 0)) > since
        ]
        next_since = int(records[-1].get("seq", since)) if records else since
        return {
            "next_since": next_since,
            "skipped": max(skipped, 0),
            "timeline": reader.render_timeline(iter(selected)),
            "events": selected,
        }

    # ------------------------------------------------------------------ #
    # 改变状态：四段式加锁（不变量①）
    # ------------------------------------------------------------------ #
    def send(self, text: str) -> dict:
        """
        像真人一样提交一次输入。

        :returns: 成功响应或 `busy` / `turn_budget` 失败响应

        ## 必须走真人提交入口（spec F2）

        提交协程**必须**写成下面这样，三行一个都不能省::

            async def _submit() -> None:
                bar = app.query_one(InputBar)
                bar.focus()                 # 不可省：上一次交互结束后焦点未必在输入框
                bar.value = text            # InputBar 没有 set_value，value 是 reactive
                await pilot.press("enter")  # 必须 await

        实测教训（两条都是「返回 ok 而什么都没发生」这种最坏形态）：
        - 只设 `value` 不按回车 → 文本躺在输入框里，`stream_chat` 调用数保持 0；
        - 把 `pilot.press` 塞进 lambda 元组 → 只造出一个**从未被 await 的协程对象**，
          驱动器返回 ok，模型一次都没被调用。

        ## 与 via=keys 的互斥

        确认/审批面板挂起期间焦点在面板上，`bar.focus()` 会把焦点抢走。
        所以 `send` 与 `via=keys` 的应答**不可交错使用**。
        实际上 `send` 在非 IDLE 时本就被 `busy` 拦住，这里只是把理由写明。

        ## 轮次预算的口径

        预算**跨 `send` 累计**，计数取自 `recorder.turn_total()`（含 `main` /
        `isolated:*` / `summary` / `notes` 四种作用域——子对话和摘要也花钱）。
        它在**前置检查**处生效，**挡不住单次 send 内的循环**：单次上界由产品既有的
        `MAX_ITERATIONS = 25` 兜底，故最坏烧掉 `budget + 25` 轮；
        独立模式子对话另有独立预算，实际上界更高。这是刻意接受的口径。
        """
        # ⓪ **锁外**取快照与轮次：两者都要跨线程调度或读文件，
        #    放进临界区会直接违反不变量①（主线程被堵住时锁被无限期占住，
        #    连 `cancel` 都排不进去）。
        state = self.snapshot()["state"]
        turns = self.build_result.recorder.turn_total()

        # ① 锁内：只做纯内存判断与记账（微秒级）
        with self._lock:
            if state != SessionState.IDLE.value:
                return protocol.err(
                    "busy", f"会话当前处于 {state} 态，只有 idle 时才能提交", {"state": state}
                )
            if turns >= self.turn_budget:
                return protocol.err(
                    "turn_budget",
                    f"已用 {turns} 轮，触及预算 {self.turn_budget}。"
                    f"重启宿主或用 --max-turns 调大。",
                    {"turns": turns, "turn_budget": self.turn_budget},
                )
            self._last_action = f"send:{text[:80]}"

        # ②③ 出锁后投递（不变量①）
        app, pilot = self.app, self.pilot

        async def _submit() -> None:
            bar = app.query_one(InputBar)
            bar.focus()
            bar.value = text
            await pilot.press("enter")

        run_on_main(self.loop, _submit())
        return protocol.ok({"submitted": text})

    def wait(self, timeout: float = 180.0, until: str = "terminal") -> dict:
        """
        等到会话到达指定的终态。

        :param until: 等什么
            - `"terminal"`（缺省，**行为与改造前逐字相同**）：
              `idle`（这一轮跑完了）或 `pending`（需要你应答）。
            - `"quiescent"`：`pending` **或**「界面空闲**且**后台也没活了」。
        :returns: 成功时 `{"terminal": "idle"|"pending", "state": {...}}`；
            超时走失败响应，`error.data` 是诊断

        ## 为什么需要 `quiescent`

        三态只看界面。C13 起，界面空闲时进程里仍可能有活——后台委派在跑，
        或者队友消息躺在信箱里等主对话**自动唤起**（那会自己起一条流）。
        于是 `wait` 返回 `idle` 之后会话随时可能又忙起来，
        **在此之上做的断言是竞态的**，而竞态的判据比没有判据更坏：它偶尔通过。

        C13 之前不存在这个问题，所以缺省值保持 `terminal`——既有场景一个字不用改。
        写 C13/C15 的场景时请显式用 `quiescent`。

        判据细节（尤其「待命队员为什么不算」）见模块级 `_is_quiescent`。

        **不持驱动锁**（不变量②）：它只轮询只读快照。

        轮询间隔从 30 ms 起、退避到 100 ms 封顶：够灵敏，长等待时也不至于
        打出几万次跨线程调度。
        """
        want_quiescent = until == "quiescent"
        deadline = time.monotonic() + timeout
        interval = POLL_MIN
        state: dict = {}
        while time.monotonic() < deadline:
            state = self.snapshot()
            # `pending` 在两种模式下都是终态：它意味着**要人来应答**，
            # 而人不应答的话后台那点活也推进不下去，继续等只会白等到超时。
            if state["state"] == SessionState.PENDING.value:
                return protocol.ok({"terminal": state["state"], "state": state})
            if state["state"] == SessionState.IDLE.value:
                if not want_quiescent or state.get("quiescent"):
                    return protocol.ok({"terminal": state["state"], "state": state})
            time.sleep(interval)
            interval = min(interval * 1.5, POLL_MAX)

        # 超时诊断：把判据分量都摊开，让人一眼看出「卡在哪一步」。
        # `background` 必须在里面——`quiescent` 模式下超时时，
        # 「是子 Agent 还在跑，还是有条消息没人处理」是完全不同的两件事。
        final = state or self.snapshot()
        target = "idle（且后台静止）或 pending" if want_quiescent else "idle 或 pending"
        return protocol.err(
            "timeout",
            f"等待 {timeout} 秒后会话仍未进入 {target}",
            {
                "stream_active": final.get("stream_active"),
                "pending": final.get("state") == SessionState.PENDING.value,
                "session_panel": final.get("panel") is not None,
                "until": until,
                "quiescent": final.get("quiescent"),
                "background": final.get("background"),
                "last_action": self._last_action,
                "waited": timeout,
                "state": final.get("state"),
            },
        )

    def answer(self, choice: str, via: str = "channel") -> dict:
        """
        应答当前挂起的面板。

        :param choice: 线上决策取值，见 `protocol.CHOICE_TABLE`
        :param via: `channel`（直接结算，来源记 `driver`）或
            `keys`（模拟按键走面板自身的按键路径，来源保持 `human`）
        :returns: 成功响应或 `not_pending` / `bad_request` 失败响应

        执行步骤：① 取当前面板类型 → ② 校验参数 → ③ **不变量③ 复核**
        （面板已展示且已获得焦点，不满足则短暂重试）→ ④ 按 `via` 分派结算。
        """
        snap = self.snapshot()
        kind = (snap.get("panel") or {}).get("kind")
        if kind is None:
            return protocol.err(
                "not_pending",
                f"当前没有待决面板（会话状态 {snap['state']}）",
                {"state": snap["state"]},
            )

        # ② 参数校验（非法组合在协议层就被挡下，不进产品）
        for message in (protocol.validate_choice(kind, choice), protocol.validate_via(kind, via)):
            if message:
                return protocol.err("bad_request", message, {"kind": kind})

        # ③ 不变量③ 复核：面板已展示 **且** 已获得焦点。
        #    实测抢跑窗口占比 8869/8870，不复核几乎必然应答在空处。
        widget_name = PANEL_WIDGET[kind].__name__
        for _ in range(SETTLE_RECHECK_TRIES):
            if snap.get("panel_visible") and snap.get("focused") == widget_name:
                break
            time.sleep(SETTLE_RECHECK_INTERVAL)
            snap = self.snapshot()
        else:
            return protocol.err(
                "not_pending",
                f"{kind} 面板未在上限内就绪（展示={snap.get('panel_visible')}，"
                f"焦点={snap.get('focused')}，期望焦点={widget_name}）",
                {
                    "panel_visible": snap.get("panel_visible"),
                    "focused": snap.get("focused"),
                    "expected_focus": widget_name,
                },
            )

        with self._lock:
            self._last_action = f"answer:{kind}:{choice}:{via}"

        if via == "keys":
            return self._answer_by_keys(kind, choice, snap)
        return self._answer_by_channel(kind, choice)

    def _answer_by_channel(self, kind: str, choice: str) -> dict:
        """
        直接调产品的结算方法（来源记为应答者的 `source`，本轮是 `driver`）。

        注意 `run_on_main` 的使用规则：**同步工作必须先包成协程**，
        否则会在调用方线程上先把界面方法执行掉。
        """
        app = self.app
        source = self.responder.source

        if kind == "session":
            session_id = None if choice == "cancel" else choice

            async def _settle() -> None:
                app._settle_session(session_id, source)

        else:
            try:
                value = settlement_for(kind, choice, app)
            except ValueError as e:
                return protocol.err("bad_request", str(e), {"kind": kind})

            async def _settle() -> None:
                app._resolve_interaction(value, source)

        run_on_main(self.loop, _settle())
        return protocol.ok({"kind": kind, "choice": choice, "via": "channel", "source": source})

    def _answer_by_keys(self, kind: str, choice: str, snap: dict) -> dict:
        """
        走面板自身的按键路径（上下键移动 + 回车），来源保持产品默认的 `human`。

        ## ⚠️ 移动次数不是「选项下标」

        面板的首项是 disabled 表头；`ConfirmPanel` 的初始高亮在 `_YES_INDEX`
        （即**第一个可选项**）；`ClarifyPanel` 还在候选项之间夹着 disabled 详情行，
        而 `OptionList` 的上下导航**会自动跳过 disabled 项**。

        **正确做法**：在「可选项序列」（`extract_panel` 已过滤掉 disabled）里算出
        目标的下标，减去初始高亮在该序列中的下标，得到要移动几格；负数就按 `up`。

        `clarify` + `keys` 在协议层已被拒（详情行使推导不稳），这里不必再处理。
        """
        options = (snap.get("panel") or {}).get("options") or []
        if kind == "session":
            target_id = choice
        else:
            target_id = CHOICE_TO_OPTION_ID.get(kind, {}).get(choice)

        if choice == "cancel" and kind == "session":
            # 会话面板的取消走 Esc，与真人按键一致
            pilot = self.pilot

            async def _cancel() -> None:
                await pilot.press("escape")

            run_on_main(self.loop, _cancel())
            return protocol.ok({"kind": kind, "choice": choice, "via": "keys", "source": "human"})

        ids = [o.get("id") for o in options]
        if target_id not in ids:
            return protocol.err(
                "bad_request",
                f"{kind} 面板里没有 id={target_id!r} 的可选项，当前可选项：{ids}",
                {"options": ids},
            )
        target_index = ids.index(target_id)

        # 初始高亮所在的**可选项序列下标**：ConfirmPanel 停在第一个可选项（即 0），
        # SessionPanel 停在第一个可恢复会话（同样是可选项序列的 0）。
        steps = target_index - 0
        key = "down" if steps >= 0 else "up"
        pilot = self.pilot

        async def _press() -> None:
            for _ in range(abs(steps)):
                await pilot.press(key)
            await pilot.press("enter")

        run_on_main(self.loop, _press())
        return protocol.ok({"kind": kind, "choice": choice, "via": "keys", "source": "human"})

    def keys(self, sequence: list) -> dict:
        """
        向界面投递**任意按键序列**（P1b 缺口①）。

        :param sequence: 按键名列表，用 Textual 的键名
            （`ctrl+q` / `escape` / `tab` / `down` / `a` …）
        :returns: `{"pressed": [...]}`
        :raises: 不抛；非法输入走 `bad_request`

        ## 为什么需要一条通用指令，而不是继续加专用指令

        `answer --via keys` 只能应答**面板**，它把「目标选项」换算成「按几次方向键」。
        但有一整类判据根本不在面板上：
        - C2 AC9 的 `Ctrl+Q` / `Ctrl+C` 退出；
        - C4 场景 11 的澄清面板键盘导航（`answer` 明确拒绝 `clarify`+`keys`，
          因为候选项之间夹着 disabled 详情行、按键次数推不出来——
          但**逐键投递**没有这个问题，调用方自己决定按几次）；
        - C10 的 Tab 补全（要先按 `/`、再按 `tab`，看候选菜单怎么变）。

        `docs/e2e-sweep/summary.md` 把这 4 条列为「P1a 设施限制」，
        指向的就是这一个缺口。

        ## 与 `answer --via keys` 的分工

        本指令是**低层原语**：它不知道面板是什么，只管把键按下去。
        `answer --via keys` 是**语义化封装**，替调用方算移动次数。
        两者都保留——用原语写面板应答要自己数按键次数，
        那正是 `answer` 当初存在的理由。

        副作用：真实地在界面上按键，可能触发任何绑定（包括退出应用）。
        """
        if not isinstance(sequence, list) or not sequence:
            return protocol.err("bad_request", "keys 需要非空的按键名列表")
        if len(sequence) > MAX_KEY_SEQUENCE:
            return protocol.err(
                "bad_request",
                f"一次最多投递 {MAX_KEY_SEQUENCE} 个按键，收到 {len(sequence)} 个",
            )
        for item in sequence:
            if not isinstance(item, str) or not item:
                return protocol.err("bad_request", f"按键名必须是非空字符串，收到 {item!r}")

        with self._lock:
            self._last_action = f"keys:{','.join(sequence)}"

        pilot = self.pilot

        async def _press() -> None:
            for key in sequence:
                await pilot.press(key)

        # ⚠ 这里**刻意不复核任何前置状态**（不像 answer 要先确认面板就绪）。
        # 按键是最低层的输入，「现在能不能按」本身常常就是判据的一部分——
        # 加了前置校验，「面板没弹出来时按回车会怎样」这类场景就没法验了。
        run_on_main(self.loop, _press())
        return protocol.ok({"pressed": list(sequence)})

    def screen(self, selector: str = "") -> dict:
        """
        导出界面上**可见的文本**（P1b 缺口②）。

        :param selector: Textual 选择器，缺省 `""` 表示整屏
        :returns: `{"text", "content", "lines", "widgets"}`

        ## ⚠ `text` 与 `content` 是两份，别用错

        - `text` —— **屏幕上真的画出来的**（逐行读 `render_line`）。
          长句会按控件宽度折行；菜单候选、输入框内容、布局判据用它。
        - `content` —— 逻辑内容（`render()`），不折行。
          `assertIn("一句很长的话")` 这类**内容断言用它**，
          否则会失败在折行位置这种与判据无关的地方。

        每个 widget 条目里也各有一份（`text` / `painted` / `content`）。

        ## 为什么控制通道需要它

        既有的观察面只有两个：`status`（面板与三态）与 `observe`（trace 事件）。
        两者都读不到**聊天区正文与补全菜单**，于是这些判据一条都验不了：
        - C10 E03 补全与高亮（候选菜单里有哪些项、命令字段有没有高亮）；
        - 「界面上真的出现了那句话」这类内容判据——
          `ui_message` 事件只能证明**产品打算显示它**，
          证明不了它真的渲染进了组件树（两者不同：markup 异常会让渲染失败
          而事件照常落盘，那正是 CLAUDE.md 里 `MarkupError` 那条坑的形态）。

        ## 样式怎么带出来

        每个控件除了纯文本，还给出它的**类名**与（若有）markup 原文。
        样式判据（如「命令字段以青色加粗高亮」）靠 markup 原文断言——
        导出渲染后的 ANSI 序列既难读又依赖终端能力，
        而 markup 原文就是产品自己写下的那份意图。

        副作用：无（纯读组件树）。
        """
        async def _read() -> dict:
            widgets: list[dict] = []
            try:
                nodes = self.app.query(selector) if selector else self.app.query("*")
            except Exception as e:  # noqa: BLE001 —— 选择器语法错误
                return {"error": f"选择器无效：{e}"}

            for node in nodes:
                if not getattr(node, "display", True):
                    continue
                entry = {
                    "class": type(node).__name__,
                    "text": "",       # 屏幕上真的画出来的（可能折行）
                    "content": "",    # 逻辑内容（不折行），做内容断言用
                    "markup": None,
                }
                try:
                    entry["content"] = _plain_text_of(node.render())
                except Exception:  # noqa: BLE001 —— 容器没有 render / 渲染抛异常
                    entry["content"] = ""
                entry["painted"] = _painted_text_of(node)
                # **画出来的那份优先**——`screen` 承诺的是「界面上可见的文本」。
                # 见 `_painted_text_of` 的 docstring：`Input` / `OptionList` 的
                # `render()` 返回的是 Panel 外壳（只有边框），内容只在逐行绘制里。
                entry["text"] = entry["painted"] or entry["content"]
                # markup 原文：Textual 8.x 的 `Content` 有个 `.markup` 属性，
                # 给出的正是产品自己写下的那串带标记的文本（如 `[dim]…[/dim]`）。
                # 取不到就留 None——样式判据自行判断能不能用。
                try:
                    markup = getattr(node.render(), "markup", None)
                    if isinstance(markup, str):
                        entry["markup"] = markup
                except Exception:  # noqa: BLE001
                    pass
                if entry["text"] or entry["markup"]:
                    widgets.append(entry)

            lines = [w["text"] for w in widgets if w["text"]]
            return {
                "text": "\n".join(lines),
                "lines": lines,
                # 逻辑内容单独给一份：`text` 是屏幕上画出来的，长句会**按控件宽度折行**，
                # 而 `assertIn("一句很长的话")` 会因此失败在一个与判据无关的地方。
                # 内容断言用这份，布局 / 菜单 / 输入框判据用 `text`。
                "content": "\n".join(w["content"] for w in widgets if w["content"]),
                "widgets": widgets,
            }

        data = run_on_main(self.loop, _read())
        if "error" in data:
            return protocol.err("bad_request", data["error"])
        return protocol.ok(data)

    def cancel(self) -> dict:
        """
        请求取消当前的 Agent 循环（等价于真人按 Esc）。

        ⚠ **「等价」指的是领域效果，不含界面反应**：本方法直调
        `manager.request_cancel()`，绕过 `tui/app.py` 的按键处理，因此
        **不会**产生真人按 Esc 时那条「仍有 N 个子 Agent 在后台运行」的提示。
        要验那条提示，走 `keys escape`（模拟按键的完整路径）。

        这个差别本身是**刻意**的（`cancel` 要在忙碌态下也能可靠送达，
        而按键要经过焦点与面板优先级），但它必须写在这里——
        观测设施与产品行为的每一处分叉都得可见，否则分叉处恰恰会被当成验收依据。
        """
        with self._lock:
            self._last_action = "cancel"
        manager = self.build_result.manager

        async def _cancel() -> None:
            manager.request_cancel()

        run_on_main(self.loop, _cancel())
        return protocol.ok({"cancelled": True})

    # ------------------------------------------------------------------ #
    # 退出编排：**只能在主线程调用**
    # ------------------------------------------------------------------ #
    async def shutdown_on_main(self, reason: str, *, live_mode: bool = False) -> None:
        """
        编排一次干净的退出（**协程，只能在主线程调用**）。

        :param reason: 结束原因（写进记录的 session_end）
        :param live_mode: 真实模式下额外等待自动笔记线程收敛

        ## ⚠️ 内部绝不使用 `run_on_main` 或 `call_from_thread`

        本方法已经在主线程上了：
        - `call_from_thread` 会直接抛 `RuntimeError`（它拒绝主线程调用）；
        - `run_coroutine_threadsafe(...).result()` 是**自己等自己**，永久挂死。

        直接同步调用界面方法即可。

        ## 为什么必须做「强制结算」，以及为什么两步必须交织

        **实测**：`quit` 时若挂着确认面板而不强制结算，进程**永远退不出去**——
        `HOST_EXITED_CLEAN` 日志已打出、进程仍然活着，40 秒后被外部超时杀掉。
        挂死点在 `asyncio.run()` 收尾的 `shutdown_default_executor`：它去 join 那个
        阻塞在 `box["event"].wait()` 的工作线程，而 **Python 3.11 的该方法没有超时
        参数、永不返回**（3.12 才加了 5 分钟默认值）。加上强制结算后，
        同一场景 0.1 秒干净退出。

        **为什么要交织在同一个循环里**：结算掉一个面板之后，循环可能**立刻弹出下一个**
        （模型一轮发多个工具调用就是这样）。写成「先结算 N 次，再等忙碌态转假」两段式，
        会在第二段再次挂住。
        """
        deadline = time.monotonic() + SHUTDOWN_LIMIT
        app = self.app
        while time.monotonic() < deadline:
            box = getattr(app, "_pending_interaction", None)
            if isinstance(box, dict):
                app._resolve_interaction(_safe_default(box.get("kind")), "driver_forced")
            elif getattr(app, "_session_panel_active", False):
                app._settle_session(None, "driver_forced")
            elif not getattr(app, "_stream_active", False):
                break
            await asyncio.sleep(0.03)

        if live_mode:
            # 自动笔记线程是 daemon，不 join 就会被进程退出截断。
            # 按**线程名**找而不是改产品去暴露句柄——零产品改动。
            # 顺序天然安全：笔记钩子在产出结束事件之前触发，而忙碌态在事件流耗尽后
            # 才转假，故「先等忙碌态转假、再 join」不存在「线程还没起就以为收敛了」的竞态。
            for thread in threading.enumerate():
                if thread.name == "rhine-memory":
                    thread.join(timeout=30.0)

        app.exit()


def _plain_text_of(rendered: Any) -> str:
    """
    把一个控件的渲染产物压成纯文本。

    :param rendered: `widget.render()` 的返回值
    :returns: 可见文本；实在取不出来时返回空串（**绝不返回 `repr`**）

    ## 为什么不能只写 `getattr(rendered, "plain", str(rendered))`

    最初就是那么写的，然后第一次真跑就撞上了：聊天区的 AI 正文控件渲染出的是一个
    包在 `RichVisual` 里的 `rich.console.Group`，它没有 `.plain`，于是
    `str()` 兜底给出的是 `RichVisual(Static(), <rich.console.Group object at 0x…>)`
    ——**一个看起来像内容的字符串**。断言「界面上出现了那句话」于是失败在
    「导出实现不完整」上，而报错完全不指向这个原因。

    比失败更坏的是它的另一面：如果判据恰好是 `assertNotIn`，
    这个 repr 会让它**通过**。观测设施返回 repr 比返回空串危险得多。

    三级取值：
    1. `.plain`（`rich.text.Text` 这类，最常见）；
    2. Rich console 捕获（`Group` / 表格 / 任何组合渲染）；
    3. 都不行就空串——**宁可少一行，也不要把 repr 冒充成内容**。
    """
    # ① `textual.content.Content`（Textual 8.x 里 Static 之类的渲染产物）直接有 .plain
    plain = getattr(rendered, "plain", None)
    if isinstance(plain, str):
        return plain

    # ② 先拆包再捕获。**顺序不能反**——`RichVisual` 是 Textual 的 Visual，
    #    **不是** Rich 可渲染对象，把它丢给 `console.print` 不会报错，
    #    Rich 会用 `Pretty` 打出它的 repr，于是「捕获成功」而内容是
    #    `RichVisual(Static(), <rich.console.Group object at 0x…>)`。
    #    先捕获后拆包的话，第一步就「成功」了，拆包分支永远走不到（实测踩过）。
    inner = getattr(rendered, "_renderable", None)
    if inner is not None and inner is not rendered:
        nested = _plain_text_of(inner)
        if nested:
            return nested

    # ③ 真正的 Rich 可渲染对象才走捕获。宽度取一个足够宽的固定值：
    #    太窄会折行、把一句话拆成两行，内容断言就得考虑折行位置。
    if hasattr(rendered, "__rich_console__") or hasattr(rendered, "__rich__") or isinstance(rendered, str):
        try:
            import io

            from rich.console import Console

            console = Console(file=io.StringIO(), width=400, no_color=True, legacy_windows=False)
            with console.capture() as capture:
                console.print(rendered, end="")
            return capture.get()
        except Exception:  # noqa: BLE001
            pass

    # ④ 取不出来就空串。**宁可少一行，也不要把 repr 冒充成内容**
    return ""


def _painted_text_of(widget: Any) -> str:
    """
    逐行读一个控件**实际画在屏幕上**的字符。

    :param widget: 任意 Textual 控件
    :returns: 逐行拼接的可见文本；读不出来返回空串

    ## 为什么必须有这条路径（真实模型验收当场发现的）

    `render()` 只覆盖「一次性产出整块可渲染对象」的控件（`Static` 那一类）。
    另一类控件**按行绘制**——`Input` 与 `OptionList` 都是，它们实现的是
    `render_line(y)` 而不是 `render()`。对这类控件调 `render()` 拿到的是空白，
    于是导出结果里只有边框，内容一个字都没有。

    实测现场：`keys / t a s k s` 之后补全菜单确实弹出来了（`CommandPanel` 可见
    本身就是证据——只有 `/` 开头才弹），但 `screen` 导出的 `CommandPanel`
    只有一串 `╭───────`。**而 C10 E03「补全菜单里有哪些候选」正是这个缺口
    要解决的判据之一**——补不上的话，`screen` 对那条判据依然无能为力。

    `Strip.text` 给的是这一行所有 segment 的文本拼接，也就是**真的被画出来的
    那些字符**——比任何「去问控件要内容」的写法都更接近「用户看到了什么」。

    副作用：无（只读；`render_line` 在 Textual 里是纯函数式的绘制）。
    """
    try:
        height = widget.size.height
    except Exception:  # noqa: BLE001 —— 尚未布局的控件没有 size
        return ""
    if not height:
        return ""

    lines: list[str] = []
    for y in range(min(height, MAX_PAINTED_LINES)):
        try:
            strip = widget.render_line(y)
        except Exception:  # noqa: BLE001 —— 不支持逐行绘制 / 越界
            break
        text = getattr(strip, "text", None)
        if isinstance(text, str):
            lines.append(text.rstrip())
    # 全是空白就当作「没内容」返回空串，避免把一堆空行塞进导出结果
    return "\n".join(lines) if any(l.strip() for l in lines) else ""


def _is_quiescent(background: dict) -> bool:
    """
    这份后台活动量说不说明「系统真的停下来了」。

    :param background: `snapshot()["background"]`，三个键见 `_read_ui_state`
    :returns: 没有任何还会自己动起来的东西 → True

    ## 判据只有两条，第三条**刻意不算**

    - `subagents > 0`   → 不静止。后台委派还在跑，它随时可能改文件、发消息。
    - `unread_for_main` → 不静止。主对话的自动唤起（`_maybe_auto_wake`）
      每 0.5 秒在空闲时检查一次，有未读就**自己起一条流**。
      也就是说「现在 idle」与「一秒后又忙起来」完全兼容。
    - `idle_members`    → **不算**。⚠ 这一条不能加。

    ## 为什么 `idle_members` 不能算进去

    C15 有一条明写的不变量：**主对话可以在队员待命时正常收工**
    （`TeamGate.has_awaited` 恒为假，护栏
    `test_team_wake.py::test_main_agent_can_finish_while_a_member_idles`）。
    待命队员就是在那儿等消息，没有任何机制让它自己醒过来——
    把它算作「系统还在动」，`wait --until quiescent` 会**永远等不到静止**，
    然后超时，然后下一个人把这个判据整条删掉。

    这与 CLAUDE.md 里那条「绝不要给 TaskStatus 加一个 is_terminal 为假的 IDLE」
    是**同一个坑**：待命是一种稳定状态，不是一段未完成的工作。

    ## 三条同时成立时为什么可以断定静止

    主对话空闲（调用方已判）+ 0 个在跑的子 Agent + 信箱里没有给 main 的消息
    → 没有任何正在执行的 Agent，因而没有人能发出新消息；
    待命队员只能被消息唤醒，而消息只能由正在执行的 Agent 发出。闭环成立。
    """
    if background.get("subagents"):
        return False
    if background.get("unread_for_main"):
        return False
    return True


def _safe_default(kind: Optional[str]) -> Any:
    """强制结算时用的安全默认值：一律选择「最保守」的那一档。"""
    if kind == "confirm":
        return ConfirmDecision.DENY
    if kind == "approve":
        return False
    return None
