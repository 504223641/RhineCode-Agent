"""
Textual App 主类模块。

RhineApp 是 TUI 层的核心，负责：
1. 组合各面板（HistoryView / CommandPanel / ConfirmPanel / ClarifyPanel / InputBar / StatusBar）
2. 监听用户输入事件，交给 CommandDispatcher 统一分流（c10：普通消息与斜杠命令）
3. 将 Agent 循环产出的 AgentEvent 通过 Worker + call_from_thread 安全地渲染到 UI
4. 实现命令层的 CommandController 协议（显示、发送、模式切换、报告、状态刷新、
   清空、压缩、恢复、退出），命令处理函数经该窄接口驱动界面而不感知 Textual
5. 提供三类用户交互回调（有副作用工具确认 / Plan Mode 需求澄清 / 计划执行审批）
6. 提供运行中取消（按 Esc）

线程模型：
  Textual 的事件循环运行在主线程，UI 操作必须在主线程执行。
  Agent 循环通过 run_worker(thread=True) 在独立线程中运行（消费 AgentEvent 生成器），
  Worker 通过 call_from_thread() 把每个事件的渲染操作调度回主线程，保证线程安全。
  需要用户决定的交互（确认/澄清/审批）由循环在 Worker 线程调用回调，回调内部用
  call_from_thread 在主线程弹面板、用 threading.Event 阻塞 Worker 等待用户选择（不死锁，N2）。
"""

import asyncio
import logging
import signal
import threading
import traceback
from time import monotonic
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, Input

from rhinecode import logsetup
from rhinecode.config import Config
from rhinecode.subagents.tasks import STATUS_LABELS, TaskManager
from rhinecode.commands import (
    CommandDispatcher,
    CommandRegistry,
    ModeTarget,
    ReportTarget,
)
from rhinecode.commands.skill_commands import build_skill_command_specs
from rhinecode.conversation import (
    PRESET_SWITCH_UNAVAILABLE,
    ConversationManager,
    SessionListRequest,
)
from rhinecode.agent.events import (
    AgentEventType,
    ClarifyReply,
    ConfirmDecision,
    StopReason,
)
from rhinecode.commands.parser import InputKind, parse_input
from rhinecode.hooks import HookEventType

# `_interact` 的交互种类 → Hook `notification` 事件的 `kind` 取值（c12 spec F2）。
#
# 两套词汇刻意不合一：`_interact` 的 kind 是**内部结算标识**（还要映射面板选项、
# 进 trace 的 interaction 事件），而 Hook 的 kind 是**写进用户配置里的稳定契约**。
# 合并会让「改一个内部标识」变成「破坏用户的 hooks.yaml」。
#
# ⚠ 新增交互种类时要在这里加一行；漏改不报错，只是那种面板弹出时
# `kind` 会退回内部标识，用户按文档写的条件匹配不上。
# 系统行的四个档位（tui-display 扩展 F19）。
#
# 用字符串常量而不是枚举，与 `AgentEvent.level` 同一条理由：这个值要跨过
# agent 层原样进 trace 负载，而 trace 是只依赖标准库的叶子包。
#
# ⚠ **不留「默认丢进提示级」的兜底**（F20）：本组的全部价值就在于把「重要的」
# 从「可忽略的」里分出来，留兜底等于没分。每个调用点都必须明确挑一档——
# `_LEVEL_CHANNELS` 里查不到的取值会当场 KeyError，而不是静默降级。
LEVEL_NOTICE = "notice"
LEVEL_EVENT = "event"
LEVEL_WARNING = "warning"

_NOTIFY_KINDS = {
    "confirm": "awaiting_confirm",
    "clarify": "awaiting_clarify",
    "approve": "awaiting_plan",
}
from rhinecode.trace import (
    NullRecorder,
    SCOPE_MAIN,
    TraceEventType,
    TraceRecorderProtocol,
    agent_event_payload,
    full_text,
)
from rhinecode.tui.clipboard import copy_text
from rhinecode.tui.widgets import (
    ActivityView,
    HistoryView, InputBar, StatusBar, StatusHint, CommandPanel, ConfirmPanel,
    TodoPane,
    ClarifyPanel, SessionPanel, StatusLine, OverlayPanel, compose_status_text,
    # 详细度档位（tui-activity-fold）：三档循环取代改造前的布尔开关
    DETAIL_CYCLE, DETAIL_FOLDED, DetailLevelClicked,
    # ⚠️ **必须用 widgets 的 escape，不能 `from rich.markup import escape`**。
    # 这里唯一的用途是转义**流式累积中的思考文本**，而它是最不该用 rich 那版的地方：
    # 「流式累积」意味着任何一帧都是在**任意位置**被截断的模型自由文本，
    # 而 rich 的 escape 只转义「看起来像完整标签」的 `[...]`，认不出被截断的括号。
    # 一旦漏过去，Textual 会在渲染时抛 MarkupError 并拆掉整个 app（详见
    # widgets.escape 的注释与 tests/test_tui_markup_escape.py 的现场重演）。
    #
    # 说明边界：本处**未实测复现**过崩溃（触发形态较窄，见测试里那条
    # 「两个未闭合括号」的用例）；换成安全版是因为输入性质相同——
    # 任意模型文本 × 任意截断点，没有理由赌它撞不上。
    escape,
    format_activity_cost,
    THINKING_MARK,
)


def _subagent_finish_text(record) -> str:
    """
    子 Agent 结束时写进历史区的那**一条**记录（tui-display 扩展 F6/F7）。

    :param record: `subagents.tasks.TaskRecord`
    :returns: 两行文本——首行是「谁 · 什么结局 · 花了多少」，次行说明结论的去向

    ## ⚠ F6 的「历史永久痕」与 F7 的「完成通知」是**同一行**

    写成两行不报错，只是每个子 Agent 在历史区留下**重复的两条**——用户会
    以为它跑了两次。所以这里只有一个产出点，活动区那条终态行是它的短期镜像，
    数秒后消失。

    ## 成本数字为什么绕道 `TaskManager.row_of`

    spec F6 要求活动区终态行与这条留痕的成本数字**同源同口径**。两边各自
    从 `TaskRecord` 上取字段拼一遍的话，一次口径改动只改一处不报错，
    而两个数字都「看起来对」、只是不相等——那种不一致最难解释。

    ## 次行为什么对失败也说「交给 AI」

    因为那是**事实**：失败与取消的任务同样会被 `take_deliverables` 取走，
    它们的「结论」是一段可读的失败说明。不交给模型的话，模型发起的委派会
    石沉大海，它永远等不到回音、也无从判断该不该重试。

    副作用：无（纯函数）。
    """
    row = TaskManager.row_of(record)
    return (
        f"{row.display_name} "
        f"{STATUS_LABELS.get(record.status, record.status.value)} "
        f"({format_activity_cost(row)})\n"
        f"  结论将在下一轮对话中自动交给 AI。"
    )


# C6：崩溃处理要写日志，见 `RhineApp._handle_exception`。
_logger = logging.getLogger(__name__)


class RhineApp(App):
    """
    RhineCode 的 Textual 应用主类。

    布局（从上到下）：
    - HistoryView：占据除底部区域外的全部高度（height: 1fr），可滚动
    - CommandPanel：斜杠命令提示面板，默认隐藏，输入 "/" 时弹出
    - ConfirmPanel：有副作用工具执行前确认 / 计划执行审批的内联面板，默认隐藏
    - ClarifyPanel：Plan Mode 需求澄清的内联面板，默认隐藏
    - SessionPanel：/resume 会话选择的内联面板，默认隐藏（c9 交互化）
    - InputBar：固定 3 行高（含边框），用户在此输入
    - StatusBar：固定 1 行，展示 Provider / 模型 / 思考模式 / 计划模式
    """

    # ---------------------------------------------------------------- #
    # 键位（tui-display 扩展 F31/F32/F5）
    # ---------------------------------------------------------------- #
    # ⚠ **两条 `priority=True` 是实测确认必需的，别去掉。**
    #
    # - `ctrl+q`：退出行为**来自 Textual 自己**（`App` 内置一条
    #   `ctrl+q → quit` 的 priority 绑定），不是本项目的代码。要取消它，
    #   覆盖的那条也必须是 priority，否则内置那条先赢。
    # - `ctrl+c`：`Input` 自带 `ctrl+c → copy`，不用 priority 的话输入框聚焦时
    #   我们的动作根本不触发——而输入框聚焦正是绝大多数时间的状态。
    #
    # - `shift+tab`（auto-plan 扩展 F5）：Textual 的 `Screen` 自带一条
    #   `shift+tab → app.focus_previous`（`priority=False`）。不用 priority 的话
    #   按下去只会把焦点挪到上一个组件，**模式一动不动**——而这个失败形态很难
    #   自查：切换命令 `/mode` 照常工作，只有真人按键那条路径是坏的。
    #
    # `ctrl+o` 不需要 priority：没有任何组件占用它。
    BINDINGS = [
        Binding("ctrl+q", "noop", "", show=False, priority=True),
        Binding("ctrl+c", "request_quit", "", show=False, priority=True),
        Binding("ctrl+o", "toggle_expand", "", show=False),
        Binding("shift+tab", "cycle_preset", "", show=False, priority=True),
    ]

    # 两次 `Ctrl+C` 之间的最长间隔（秒）。超过就当成「第一次」重新计数。
    #
    # 取 2 秒：短到不会让「几分钟前误按过一次」在此刻突然生效，
    # 长到够人看清提示再按第二下。
    QUIT_CONFIRM_SECONDS = 2.0

    CSS = """
    Screen {
        layout: vertical;
    }
    /*
     * 历史区与四个交互面板共处的「舞台」（tui-activity-fold 验收期修订）。
     *
     * ⚠ **`layers` 必须声明在这里**，因为 dock 与图层都是相对**父容器**的。
     * 面板走 `panels` 层 + `dock: bottom`，于是它们浮在历史区底部、
     * **不占常规流的高度**——这正是「面板弹出不再挤压历史区」的全部实现。
     *
     * 实测（80×20 终端）：面板出现前后 `HistoryView` 高度都是 14、
     * 输入框位置都是 17。改造前这两个数会各变一次，用户看到的就是整块在跳。
     */
    #stage {
        height: 1fr;
        layers: base panels;
    }
    /*
     * 四个交互面板的浮层容器（验收期修订两次）。
     *
     * **浮层与框线都落在这一层**，面板自身回到普通子组件：
     * - `layer` + `dock`：整块浮在历史区底部，不占常规流高度（不挤压历史区）
     * - `border`（左 / 右 / 下）：**接着历史区那圈框继续画**
     *
     * ## ⚠ 为什么是 border 而不是 padding（真机反馈后的第二次修订）
     *
     * 上一版用 `padding: 0 1` 把面板缩进到历史区框线内侧，理由是「透明背景
     * 不会盖掉下面的框线」。**那个理由是错的**：Textual 里
     * `background: transparent` 只让**颜色**透下来，字符照画不误——padding
     * 那两列画的是**空格**，于是历史区的 `│` 在面板那几行被逐个擦成空白。
     * 真机反馈：「弹出面板左右没有边框，对应历史记录左右也没有边框」。
     *
     * 现在改成让容器**自己画那两条竖线**（颜色与 `HistoryView` 同一个），
     * 于是框线在视觉上是连续的一整圈，面板嵌在里面。
     *
     * `border-bottom` 同理：容器贴着 stage 底部，正好接管历史区的下边框那一行
     * ——上一版靠 `margin-bottom: 1` 给那条线让位，让出来的那行在面板与框线
     * 之间留了一道缝（「面板下面会有一点历史记录面板的空白」）。
     *
     * `border-top` **刻意留空**：面板自己带 `border-top: tall <各自的颜色>`，
     * 那条线兼作「面板与历史内容的分界」，颜色还随面板类型变（确认橘、命令青）。
     * 在这里再画一条会变成两条平行线。
     *
     * `height: auto` + 内部面板缺省 `display: none` ⇒ 空闲时这块高度为 0。
     */
    #panel-dock {
        layer: panels;
        dock: bottom;
        height: auto;
        /* ⚠ **缺省隐藏。** 容器自带边框，而边框在 `height: auto` 下
           **照样算进高度**——空面板的容器仍占 1 行，dock 在底部时正好压住
           历史区的下边框，那条框线变成一行空白。
           可见性由 `_reserve_space_for_panels` 按「有没有可见面板」切换。 */
        display: none;
        border: solid #7AEEFF 60%;
        border-top: none;
        background: transparent;
    }
    HistoryView {
        height: 1fr;
        border: solid #7AEEFF 60%;
        padding: 0 1;
    }
    /*
     * 内容容器随消息增长，超出 HistoryView 高度时触发父容器滚动。
     *
     * `min-height: 100%` 是 tui-display 扩展 F39/F40 的**全部实现**——
     * 一行 CSS 同时兑现「内容短时贴顶」与「内容长时跟随最新」两件事。
     *
     * ## 它解决的是什么
     *
     * `HistoryView.on_mount` 里的 `anchor()` 让视口粘在底部。内容比视口长时
     * 这正是要的；但内容**短于**视口时，锚点会产生一个**负的滚动偏移**，
     * 把两条消息整个推到视口底部，上方留一大片空白——用户看到的是
     * 「对话从下往上长」。实测（视口 13 行、内容 2 行）：
     *
     *     HistoryView       region=(y=0, height=13)   scroll_y=-9
     *     #history-messages region=(y=10, height=2)   ← 落在第 10、11 行
     *
     * 让内容容器**至少和视口一样高**之后，"内容短于视口" 这个前提就不成立了：
     * 容器被撑到满高，锚点把它按到底 == 按在顶（`scroll_y` 由 -9 变 0），
     * 消息自然从第一行开始排。内容超过视口时容器高度由 `height: auto` 接管，
     * 跟随最新的行为逐字不变（实测 `scroll_y == max_scroll_y`）。
     *
     * ⚠ **不要改成删掉 `anchor()`。** 那个调用是带实测证据加进去的
     * （理由见 `HistoryView.on_mount` 的 docstring：不用它时 `scroll_end()`
     * 取到的是**加新组件之前**的 `max_scroll_y`，每次都停在「差最后一条消息」
     * 的位置）。两个需求必须同时满足，而这一行 CSS 让它们不再互相冲突。
     */
    /*
     * 历史区各条内容之间留一行（真机反馈后加）。
     *
     * 改造前所有消息紧挨着排，一屏里「用户说的」「AI 说的」「工具跑了什么」
     * 糊成一整块，要靠前缀符号去分辨每一段从哪开始。加一行行距之后，
     * **段落边界由留白承担**，前缀符号退回它本来的角色（标明这一段是什么）。
     * 与 Claude Code 的观感一致。
     *
     * ⚠ 选 `margin-bottom` 而不是 `margin-top`：后者会在历史区**最顶上**
     * 留一行空白，而那一行紧挨着框线、看起来像排版错位。
     * 落在末尾的那一行反而有用——它把最后一条内容与待办块/输入框分开。
     *
     * ⚠ 通配符 `*` 是刻意的：历史区里挂的东西有六七种
     * （用户消息 / AI 正文 / 思考块 / 系统行 / 工具行 / 批次聚合行 / 命令报告），
     * 逐个列类型必然漏，而漏掉的那种就会**只有它前后没有行距**——
     * 那种不一致比完全没有行距更刺眼。它们全部经 `_mount_widget` 挂进来，
     * 所以「直接子节点」这个范围是准确的。
     *
     * 代价：每条内容多占一行。长对话里可见内容大约减半——这是明码标价的
     * 取舍，用户明确要的就是这个距离。
     */
    #history-messages > * {
        margin-bottom: 1;
    }
    HistoryView > Vertical {
        height: auto;
        min-height: 100%;
    }
    /*
     * 待办清单块（todo-list 扩展 F10–F14）。
     *
     * 它是 `HistoryView` 的**第二个子节点**，`dock: bottom` 把它钉在历史区
     * 底部：历史内容在它上面滚动，它自己不动，同时占掉可滚动区的相应高度。
     *
     * ## 实测数据（80×24，见该扩展 plan 的「T1 实测结论」）
     *
     * `dock` 在 `ScrollableContainer` 内部本项目此前无先例，故先量后写：
     * 滚到顶与滚到底，本块 `region.y` **都是 17**（钉住）；块高由 3 变 5 时
     * `max_scroll_y` 24 → 26、实际可见内容 16 → 14（**占位，精确 -2**）；
     * 内容画在 `y 1..14`、本块在 `y 15..19`（**不重叠，不遮挡**）。
     *
     * ⚠ 断言「占位」时**不能用 `scrollable_content_region.height`**——
     * 实测它恒为 19、**不扣 dock 子节点的高度**，用它会得出「没占地方」
     * 的错误结论。正确的量是 `max_scroll_y`。
     *
     * ## 三条样式决定
     *
     * - **顶部分隔线用灰不用主题青**：它是**观测区**，而青色在本项目里
     *   专属于「等着你决定」的四个交互面板。与活动区同一条理由、同一个取值。
     * - **不加左右 padding**：`HistoryView` 自带 `padding: 0 1`，这里再加一层
     *   会把框线内侧擦出空白——`background: transparent` 只让**颜色**透下来，
     *   padding 那两列画的是**空格**，会把 `│` 逐个擦掉（验收第 19 条踩过）。
     * - **`display: none` 是缺省态**，可见性由 `TodoPane.update_view` 按
     *   「该不该显示」切换（判断在 `todo.render.build_view` 里，不在界面层）。
     */
    TodoPane {
        dock: bottom;
        height: auto;
        display: none;
        /* 自己画左右与下边框，**接着历史区那圈框继续画**（手法与 `#panel-dock`
           完全相同）。
         *
         * ⚠ **顶边框必须是 `none`**（真机反馈后改，别改回来）。
         *
         * 初版用 `border-top: tall #808080` 想给一条灰色分隔线。问题出在
         * **端帽**：`tall` 那一行的两端画的是方块 `▊` / `▎`，而它上下每一行
         * 的两端都是 `│`——方块贴在格子一侧、竖线在格子中间，于是那一行
         * 看起来跟上下**差一条线的距离**（用户原话）。
         *
         * 实测过四种（80×24，逐字符 dump 屏幕）：
         *   tall  → `▊ ▔▔▔ ▎`   端帽是方块，错位（初版，就是这个 bug）
         *   hkey  → `▔ ▔▔▔ ▔`   线齐了，但两端仍不是 `│`
         *   solid → `┌ ─── ┐`   画成新开一个框，比错位更糟
         *   none  → `│     │`   **整圈框线完全连续**
         *
         * Textual 的边框样式**给不出 `├───┤`**，所以「既要分隔线又要接上竖线」
         * 做不到。取舍是保框线连续——待办块靠 `● 待办 (n/m)` 表头自己区分，
         * Claude Code 的待办同样没有分隔线。
         *
         * ⚠ 四个交互面板也写着 `border-top: tall`，但**不是同一个坑**：
         * 它们嵌在 `#panel-dock` 里、被那圈边框内缩一格，端帽落在框线
         * **内侧**、不与 `│` 同列，因此不会错位。别顺手一起改。
         */
        border: solid #7AEEFF 60%;
        border-top: none;
        /* 上下各留一行（真机反馈后加）。
         *
         * 去掉顶边框之后框线是连续了，但历史区的最后一条消息与
         * `● 待办 (n/m)` 直接贴在一起，读起来像同一段内容。留白是这里
         * **唯一可用的分隔手段**——分隔线的路已经走不通（Textual 给不出
         * 能接上 `│` 的端帽，见上方那段），色块又与「全项目只用框线划分
         * 区域、不用色块」的既定口径冲突。
         *
         * ⚠ 代价是**待办块高 2 行**，历史区相应少 2 行。这是明码标价的
         * 取舍：待办块是占位的（spec F12），它每高一行，历史内容就少一行。
         */
        padding: 1 1;
        background: transparent;
    }
    /*
     * 待办块可见时，历史区收起它自己的下边框（否则两者之间多一条横线，
     * 看起来是**两个框**而不是一个）。类由 `_refresh_todo` 统一切。
     *
     * ⚠ 与 `#panel-dock` 的做法不同：那个是**浮层**，直接盖住历史区的下边框
     * 那一行，所以历史区不必改。本块是**占位**的，两者上下相邻、谁也不盖谁，
     * 因此必须有一方让出那条线。
     */
    HistoryView.-todo-open {
        border-bottom: none;
    }
    /*
     * 子 Agent 活动区（tui-display 扩展 F1）。
     *
     * `display: none` 是缺省态——不使用子 Agent 的用户永远看不到它，
     * 布局与改造前逐字一致（F9 零回归）。可见性由 `ActivityView.update_rows`
     * 按「有没有行」切换。
     *
     * 顶部分隔线用灰色而不是主题青：它是**观测区**不是交互区，
     * 与下面那几个等着人应答的面板必须在视觉上分得开。
     */
    ActivityView {
        height: auto;
        max-height: 12;
        display: none;
        border: none;
        border-top: tall #808080 60%;
        padding: 0 1;
    }
    /*
     * 四个面板的共同外观（真机反馈后统一）。
     *
     * **底色一律透明**：原本用 `$boost` 给面板垫一层浅灰，想把它与历史内容
     * 分开。但浮层容器现在自带左右框线、面板自带顶部分隔线，边界已经说清楚了；
     * 那层灰反而在框线内侧又切出一条深浅边，看起来像面板没有贴住框
     * （用户原话：「弹出面板的左右和下方还是有一点边距」）。
     *
     * **分隔线一律用主题青**：确认面板原本是橘色（想警示「需要你决定」）。
     * 但整块面板本来就是为「需要你决定」才弹出来的，颜色不承担额外信息，
     * 却与输入框、历史区那圈青色框线打架。橘色在本项目里已有确定含义
     * （工具执行中 / 「情况变了」），面板顶线用它属于一符两义。
     * 「这是有副作用的操作」由表头那行橘色文字承担，位置更贴近它说的那件事。
     */
    CommandPanel {
        height: auto;
        max-height: 6;
        display: none;
        /* 清除 OptionList 自带全方向边框，统一用顶部分隔线与主题色对齐 */
        border: none;
        border-top: tall #7AEEFF 60%;
        padding: 0 1;
        background: transparent;
    }
    ConfirmPanel {
        height: auto;
        max-height: 8;
        display: none;
        border: none;
        border-top: tall #7AEEFF 60%;
        padding: 0 1;
        background: transparent;
    }
    ClarifyPanel {
        height: auto;
        max-height: 12;
        display: none;
        border: none;
        border-top: tall #7AEEFF 60%;
        padding: 0 1;
        background: transparent;
    }
    SessionPanel {
        height: auto;
        max-height: 15;
        display: none;
        border: none;
        border-top: tall #7AEEFF 60%;
        padding: 0 1;
        background: transparent;
    }
    /*
     * 回合状态行（tui-activity-fold F14）。
     *
     * ⚠ **固定占一行，永不隐藏**（验收期修订）。
     *
     * 初版照搬了四个交互面板的做法（`display: none`，按需出现）。那是错的：
     * 面板是**打断性**的（弹出时用户注意力本就在面板上），状态行是**常伴**的
     * ——每次运行开始都要出现、结束都要收回，而每一次出现/收回都让
     * `HistoryView` 的 `1fr` 高度变一次，历史区内容随之重排。
     * 用户的原话是「每次出现时历史记录的窗口会抖动」。
     *
     * 现在它永远占这一行：**布局恒定，零抖动**。空闲时内容为空串，
     * 屏幕上就是输入框上方的一行空白——这是刻意付的代价，
     * 换掉的是每轮两次的整区重排。
     *
     * 因此这里**不能写 `display: none`**，组件那边也不再改 `display`。
     */
    StatusLine {
        height: 1;
        padding: 0 1;
    }
    /*
     * 输入框：**边框在外层容器上，整块不上底色**（改了三轮，这是终态）。
     *
     * ## 为什么最后是「不上底色」
     *
     * 边框线画在**一整个格子**里：`─` 只占那个格子的垂直中段，其余部分露出的是
     * **该格子的背景色**。于是只要底色与屏幕底色不同，「底色的边界」与「框线」
     * 就永远差半个格子，差在哪一侧取决于边框那一圈用谁的背景——
     *
     * - 边框那圈用灰底 ⇒ 灰色比框线**往外多半格**（「灰色溢出了框」）；
     * - 边框那圈用屏幕底色 ⇒ 框线与灰块之间**露出一圈黑**（「还是有一圈黑边」）。
     *
     * 两者在格子这个粒度上**不可能同时消除**。把底色整个去掉，这个矛盾就不存在了
     * ——而且与历史区、四个面板的观感统一：全项目只用**框线**划分区域，
     * 不用色块。
     *
     * ⚠ **三处必须一起透明**：容器、`InputBar` 自身（`Input` 自带
     * `background: $surface`）、以及聚焦态的 `background-tint`
     * （自带 `$foreground 5%`，只作用在输入行上，不关掉的话一聚焦就又多出
     * 一层比框线亮的色块）。漏掉任何一处，那圈边就以另一种颜色回来。
     *
     * ⚠ **边框留在外层容器上，别挪回 `InputBar`。** 那样 `Input` 的底色与
     * tint 会连框线那一圈一起铺，上面两条就都失效了。
     *
     * 三行的总高度不变（容器 1 + 1 + 1），布局与改造前逐字一致。
     */
    #input-frame {
        height: 3;
        border: solid #7AEEFF 60%;
        background: transparent;
    }
    InputBar {
        height: 1;
        border: none;
        padding: 0 1;
        background: transparent;
    }
    InputBar:focus {
        background: transparent;
        background-tint: $foreground 0%;
    }
    /* 底部状态栏那一行拆成左右两个区（tui-display 扩展 F31）。
       背景色挂在**行容器**上而不是任一子组件上——挂在子组件上的话，
       左区 `width: auto` 在没内容时宽度为 0，那一小段底色会跟着消失，
       表现为状态栏左边缘缺一块。 */
    #status-row {
        height: 1;
        background: #7AEEFF 20%;
    }
    /* 左区：贴着左边缘的瞬时提示（「再按一次 Ctrl+C 退出」）。
       `width: auto` 让它在没提示时不占位，右区照常铺满整行。 */
    StatusHint {
        width: auto;
        height: 1;
        color: $text;
        text-align: left;
    }
    /* 右区：常驻状态。`width: 1fr` 吃掉剩余宽度，再右对齐——
       这样右区那串的位置**不随左区有没有提示而移动**（会移动的话，
       每次误按 Ctrl+C 整条状态栏都会抖一下）。 */
    StatusBar {
        width: 1fr;
        height: 1;
        color: $text;
        text-align: right;
    }
    """

    def __init__(
        self,
        manager: ConversationManager,
        config: Config,
        command_registry: CommandRegistry,
        recorder: "Optional[TraceRecorderProtocol]" = None,
    ):
        """
        :param manager: 已初始化的对话管理器，持有 Provider / Agent 和对话历史
        :param config: 配置对象，用于在状态栏展示 Provider 和模型信息
        :param command_registry: 启动早期构建的命令注册表（c10）。App 不自建注册表——
                                 同一实例同时注入分发器、命令面板与输入高亮器（spec F3），
                                 保证执行、补全与帮助共享同一份事实来源
        :param recorder: 行为记录器（trace 设施）。缺省用 `NullRecorder()`，
                         **不传等于零回归**，界面层的全部埋点变成空调用
        """
        super().__init__()
        self._manager = manager
        self._config = config
        # 行为记录器：Null Object 兜底，界面各埋点无需判空（trace spec N1）
        self._recorder: TraceRecorderProtocol = recorder or NullRecorder()
        # 命令层接线（c10）：单个分发器实例，提交入口的唯一分流点。
        self._command_registry = command_registry
        self._dispatcher = CommandDispatcher(command_registry, recorder=self._recorder)
        # 待决的用户交互（确认/澄清/审批）：None 表示当前无交互在进行；
        # 进行中时为 {"event": threading.Event, "result": Any, "kind": str}，
        # 由回调在 Worker 线程创建并阻塞、由主线程的选择/取消处理写入结果并唤醒。
        self._pending_interaction: dict | None = None
        # 当前澄清面板的候选项列表（用于把所选下标还原为概述文本）
        # 当前正在问的那个问题（ask-user 扩展）。驱动设施也读它来算结算值
        # ——**刻意读私有属性**，为的是与产品侧走同一份计算，
        # 照抄一份「等价实现」反而会在产品改了算法时静默分叉。
        self._clarify_question = None
        # 是否处于自由输入态（用户选了「其它…」，正在主输入框里打字，F16）。
        # 它是**输入框提交守卫**与 **Esc 分支**共同的判据，两处成对。
        self._clarify_free_text: bool = False
        # 单轮运行锁：避免多个 Worker 同时修改同一份 conversation history。
        self._stream_active = False
        # c15 F20：「自动唤起已达上限」只提示一次的标志。
        # 轮询每 0.5 秒跑一次，不设它会刷满整屏。用户下次提交消息时复位。
        self._auto_wake_limit_notified = False
        # 「当前不能提交」提示是否已在本次忙碌期显示过（c11 T55）：
        # 进入流式 / 每次新面板弹出时复位，避免用户连按回车刷屏。
        self._busy_hint_shown = False
        # /resume 会话选择面板是否正在展示（c9 交互化）：
        # 展示期间输入框被禁用，此标志作为各输入路径的一致性兜底守卫。
        self._session_panel_active = False
        # 上一次轮询看到的运行中子 Agent 数（c13）。
        # 只在它**变化**时刷状态栏——每 0.5 秒无条件刷一次是白干活，
        # 而状态栏刷新还会产出一条 trace 埋点，空转会把时间线淹掉。
        self._last_subagent_count = 0
        # todo-list 扩展：待办块的刷新状态。
        #
        # `_todo_version` 是**上一次画过的版本号**——工作线程每轮读一次协调层的
        # 当前版本号，不同才发起跨线程重绘。这样空闲时零成本，也不必新增定时器。
        # `_todo_shown` 记「上一次画出来了没有」，用于判定「全部完成」那一次
        # 由显示转隐藏的**跃迁**（只在那一刻留一行记录，见 `_refresh_todo`）。
        self._todo_version = 0
        self._todo_shown = False
        # 缺省空集合 = 「一个都不静默」，历史区行为与本扩展之前逐字一致。
        # 真值在 `on_mount` 里从工具中心取（见那里）。
        self._silent_tools = frozenset()
        # 全局详细度档位（`Ctrl+O`）。**一个键同时管活动区与历史区**——
        # 对齐 Claude Code 的全局 verbose 语义，两个键会让用户记两套。
        #
        # tui-activity-fold 起从布尔改成**三档循环**（折叠 → 逐条 → 全文）：
        # 批次归并之后「展开」有了两层含义（把批次摊成逐条 / 把单条摊成原文），
        # 一个布尔表达不了。⚠ 用整数而不是两个布尔——后者能表达一种非法状态
        # （「不展开批次却展开单条」），而非法状态迟早会被某条路径构造出来。
        self._detail_level = DETAIL_FOLDED
        # 上一次按下 `Ctrl+C` 的时刻（`time.monotonic`）。
        # 初值取一个足够久远的负数，保证第一次按下必然走「提示」那一支。
        self._last_quit_request = -1e9
        # SIGINT 守卫的两个字段（见 `_install_sigint_guard`）。
        # `_sigint_previous` 存原处理器用于卸载时还原；`_sigint_loop` 存事件循环，
        # 信号处理器靠它把动作**排队**回事件循环而不是就地执行。
        self._sigint_previous = None
        self._sigint_loop: Optional[asyncio.AbstractEventLoop] = None
        # 退出提示的显示态与它的到期定时器（F31）。
        # ⚠ **判定的依据始终是上面那个时间戳，不是这个标志**——它只负责「显示」。
        # 反过来（拿标志当判据）会让定时器的调度抖动变成退出行为的抖动。
        self._quit_hint_active = False
        self._quit_hint_timer = None

    def compose(self) -> ComposeResult:
        """
        按从上到下的顺序挂载各面板（命令面板与输入框共享同一注册表，c10）。

        ## ⚠ `#stage` 这一层容器是为「面板不再挤压历史区」而加的（验收期修订）

        改造前四个交互面板是常规流里的兄弟节点，一弹出就把 `HistoryView` 的
        `1fr` 高度挤小，历史区整块重排——用户的原话是「确认面板也会导致历史
        窗口抖动」。

        现在历史区与四个面板一起放进 `#stage`，面板走**独立图层**并
        `dock: bottom`：它们浮在历史区底部，**不占常规流的高度**。
        实测（80×20 终端）历史区高度在面板出现前后都是 14、输入框位置都是 17。

        ⚠ **`ActivityView` 刻意留在 `#stage` 外面**：它是持续显示的观测区，
        不是打断性的，浮起来会长期遮住历史区内容。
        """
        with Vertical(id="stage"):
            yield HistoryView()
            # 四个交互面板装进一个浮层容器（见上方说明与 CSS）。
            # ⚠ **缩进由容器的 padding 做，不能写在面板自己的 margin 上**：
            # dock 组件的宽度默认是 `1fr`，而 `1fr` 会让 `margin-right` 失效
            # ——实测面板 x 从 0 变成 1（左边距生效了）、宽度仍是父容器全宽，
            # 于是右边框被顶出屏幕。左边缩进了、右边没缩，比不缩更难看。
            with Vertical(id="panel-dock"):
                yield CommandPanel(self._command_registry)
                yield ConfirmPanel()
                yield ClarifyPanel()
                yield SessionPanel()
            # 待办清单块（todo-list 扩展 F10）。**base 层 + dock: bottom**，
            # 因此它真的占地方：`HistoryView` 的 `1fr` 少分到相应的行数。
            #
            # ⚠ 它必须排在 `#panel-dock` **之后**声明，但那与叠放无关——
            # 叠放由图层决定（面板在 `panels` 层、本块在 `base` 层），
            # 面板弹出时盖住它是预期行为。
            yield TodoPane()
        # 活动区（tui-display 扩展 F1）：历史区**之下**、各面板与输入框**之上**。
        #
        # 位置是刻意的：它贴着输入框，也就是用户视线本来就在的地方；
        # 放历史区上方的话，它空转时会白占两行，而且位置会随历史区滚动跳动。
        yield ActivityView()
        # 回合状态行（tui-activity-fold F14）：各面板**下方**、输入框**上方**。
        #
        # 位置贴着输入框——用户视线本来就在那里，而它回答的正是「现在还在跑吗」。
        # ⚠ 它与 `#status-row` 刻意分开：那一行装的是**配置态**（provider /
        # 模型 / 权限档，常驻），这一行是**本回合活体态**（只在运行中存在）。
        # 合并会让「常驻状态」与「瞬时状态」争同一块地方。
        yield StatusLine()
        # ⚠ 外层容器只负责画框（见 `#input-frame` 那段 CSS）：`Input` 自带的
        # 灰底会填满含边框在内的整个区域，边框留在它自己身上时，青色框线就画在
        # 一片灰底上、看起来灰色溢出了框外。
        with Vertical(id="input-frame"):
            yield InputBar(
                self._command_registry,
                # ⚠ `Ctrl+O` 是 tui-activity-fold 验收期补进来的：行内的
                # 「（Ctrl+O 展开）」提示被撤掉之后，这里成了它**唯一**的发现渠道。
                # 撤那句话的理由是它一屏出现四五次、全在说同一个全局快捷键；
                # 说一次、说在用户找快捷键时会看的地方，才是它该待的位置。
                placeholder=(
                    "输入消息，/ 查看命令，Tab 补全，Ctrl+O 展开详情，"
                    "运行中按 Esc 取消，连按两次 Ctrl+C 退出"
                ),
            )
        # 状态栏是**一行两个区**：左区贴左边缘放瞬时提示，右区右对齐放常驻状态
        # （F31，对齐 Claude Code 底部那一行）。合成一个组件做不到「贴左」——
        # 右对齐块里的最左边会随其余各段长度在屏幕中间浮动。
        yield Horizontal(StatusHint(), StatusBar(), id="status-row")

    def on_mount(self) -> None:
        """
        应用挂载完成后的初始化操作。

        在此时机执行而非 __init__，是因为此时 DOM 已完全构建，query_one() 可安全查找子组件。
        把三类交互回调注入协调层：协调层在循环中需要用户决定时调用它们，而不感知 Textual 细节。
        """
        self._refresh_status()
        self._manager.confirm_callback = self._confirm_tool
        self._manager.clarify_callback = self._clarify
        self._manager.approve_plan_callback = self._approve_plan

        # 工具行标题的主参数映射（tui-display 扩展 F12）。
        #
        # **只建一次**：工具集在启动装配完成之后不再变化（MCP 运行期重载只增删
        # 远端工具，而远端工具一律没有 `primary_arg`，映射里本来就没有它们）。
        # 必须排在 `render_history` **之前**——`--continue` 恢复出来的历史里
        # 有工具行，晚一步的话首屏那批会用空映射画成键值对形态，与其后新产生的
        # 行长得不一样，而这在界面上只表现为「上下两截风格不同」。
        primary_args = self._manager.primary_arg_map()
        self.query_one(HistoryView).set_primary_args(primary_args)
        # 确认面板也要（E 组做完的样子里那句「工具名也走 B 组的主参数口径」）：
        # 用户就是靠面板上那一行决定放不放行的，键名在那里同样只占地方。
        self.query_one(ConfirmPanel).set_primary_args(primary_args)
        # 归并分组表（tui-activity-fold F2）：哪些工具的调用会被收进一行聚合语。
        # ⚠ 位置与上面那两行同理，**必须排在 `render_history` 之前**——
        # `--continue` 恢复出来的历史里有工具行，晚一步的话首屏那批会用空表
        # 画成独立行，与其后新产生的形态不一致（界面上表现为「上下两截风格不同」）。
        self.query_one(HistoryView).set_fold_groups(self._manager.fold_group_map())
        # 历史区静默工具（todo-list 扩展，真机反馈后加）：这些工具的调用
        # **不产生工具行**——它们的结果已由界面上另一块常驻区域完整呈现。
        # 工具集在启动之后不再变化，故与折叠分组表同样只取一次。
        self._silent_tools = self._manager.silent_tool_names()
        # 批次封闭的行为记录（tui-activity-fold N7/AC26）。走回调注入而不是让
        # 历史区直接持有记录器——它是纯展示层，认识 trace 会让依赖方向倒过来。
        self.query_one(HistoryView).set_batch_closed_hook(self._trace_tool_batch)

        # 记忆系统接线（c9）：
        # 1. 启动提示（--continue 恢复结果等）作为系统提示行显示；
        # 2. 记忆通知回调：记忆线程（非主线程）触发，必须经 call_from_thread 调回主线程渲染；
        # 3. 会话锁心跳：每 2 分钟 touch 一次，保证「进程活着锁就新鲜」（过期阈值 10 分钟）。
        # --continue 启动恢复对齐（c9 交互化）：若启动时已恢复出历史（history 非空），
        # 先把整段历史回放到聊天区，再显示启动提示——与 /resume 面板载入后的体验一致。
        if self._manager.history:
            self.query_one(HistoryView).render_history(self._manager.history)
        # 项目级 Hook 的逐条展示（c12 F9.1）**单独走醒目通道、排在最前**。
        #
        # 它与下面那条 `startup_notice` 分开，是因为两者的性质完全不同：
        # 后者是记忆系统提示、权限/Hook 加载警告这类**信息**，dim 正合适；
        # 而这一段是「这些命令会在你机器上直接执行」的**警告**，用同一条 dim 通道
        # 渲染会让它比普通提示还不显眼——方向正好反了（人眼评审时发现）。
        hook_notice = self._manager.hooks_project_notice()
        if hook_notice:
            self.show_warning(hook_notice)
        if self._manager.startup_notice:
            # ⚠️ 必须走 `show_message` 而不是直接 `append_system`。
            #
            # 两者在**界面上**一模一样，差别只在前者会顺带产出一条 `ui_message`
            # 埋点。直接调 `append_system` 的话，这段提示在界面上显示得好好的，
            # 却**一个字都没被记下来**——与 P1a 实测到的「最后一段 AI 正文不进
            # 记录」是同型缺口，同样在界面上完全看不出来。
            #
            # c12 起这条不再只是「记录完整性」问题：项目级 Hook 的逐条展示是
            # spec F9.1 的**全部安全价值**，而「它到底有没有出现在首屏」只能靠
            # 这条埋点来判定（护栏见 `tests/test_e2e_hooks.py` 场景 7）。
            self.show_message(self._manager.startup_notice)
        self._manager.memory_manager.notify = self._notify_memory
        self.set_interval(120, self._manager.memory_manager.touch_session_lock)

        # 子 Agent 完成轮询（c13）。0.5 秒是「人眼感觉是即时的」与「不白跑」的折中：
        # 更快没有可感收益，更慢会让「跑完了却半天不出通知」变得明显。
        # 只在服务启用时注册——没启用时每 0.5 秒调一次空方法纯属浪费。
        if self._manager.subagent_service is not None:
            self.set_interval(0.5, self._poll_subagents)

        # Skill 激活通知（c11）：模型调 load_skill 成功后立刻刷新状态栏的
        # Skill 段，让用户当下就看到激活数变化，而不用等本轮流式结束。
        self._manager.skill_manager.notify_activation = self._notify_skill_activation

        # 预设变化通知（auto-plan 扩展 F12）：计划获批后预设当场变回 auto，
        # 而那件事发生在**工作线程**（审批回调的返回路径上），状态栏必须跟着变。
        # 与上面那条 Skill 激活通知同一个先例、同一套理由。
        self._manager.notify_preset_change = self._notify_preset_change

        # 启动后将焦点置于输入框，用户可以直接开始输入
        self.query_one(InputBar).focus()

        # SIGINT 守卫必须在这里装（见下面那个方法的说明）。
        self._install_sigint_guard()

    # ------------------------------------------------------------------ #
    # SIGINT 守卫（tui-display 扩展 F31 的兜底）
    # ------------------------------------------------------------------ #

    def _install_sigint_guard(self) -> None:
        """
        接管 `SIGINT`，让它走与 `Ctrl+C` **同一条**连按两次的判定。

        ## 为什么需要这层兜底：`Ctrl+C` 有两条完全不同的抵达路径

        终端把 `Ctrl+C` 交给程序的方式取决于**控制台输入模式**：

        - 关掉 `ENABLE_PROCESSED_INPUT` 时（Textual 启动时正是这么设的），
          它只是一个**普通按键**——`0x03` 字节进输入流，走 BINDINGS 那条
          `ctrl+c → request_quit`，连按两次的判定生效；
        - 开着 `ENABLE_PROCESSED_INPUT` 时，控制台改为向进程组发
          `CTRL_C_EVENT`，Python 的**默认处理器**在主线程抛 `KeyboardInterrupt`
          ——它会直接把 `app.run()` 掀翻，**一次按下就退出**，而且是带回溯的
          难看退出，`request_quit` 里的计数一个字都读不到。

        第二条路径在本项目里是**可达**的：控制台输入模式是**整个控制台共享**的
        属性，不是每个进程各一份。`run_command` 用 `shell=True` 起
        `cmd.exe`，子进程完全可能改掉这个模式且不还原干净；再加上终端复用、
        SSH、以及外部程序直接投递 `CTRL_C_EVENT` 等情形，都会让后续的
        `Ctrl+C` 突然从「按键」变成「信号」。用户看到的现象就是
        **平时要按两下，偶尔一下就退了**。

        所以这里不去猜是哪种情形，直接把两条路径**收敛到同一个判定**上：
        信号来了也当成一次 `Ctrl+C` 按下，该提示提示、该退出退出。

        ## 实现上的两个要点

        1. **信号处理器里不做任何实际工作**，只 `call_soon_threadsafe` 把
           `action_request_quit` 排进事件循环。Python 的信号处理器是在主线程的
           字节码边界上被插进来执行的，可能打断任意一段代码——在那里直接碰
           Textual 的组件树等于在不确定的时机重入 UI；
        2. **失败一律静默**（`signal.signal` 只能在主线程调，某些嵌入场景会抛
           `ValueError`）。守卫装不上时行为退回今天的样子，不该反过来让程序起不来。

        副作用：安装进程级 SIGINT 处理器，在 `on_unmount` 还原。
        """
        try:
            loop = asyncio.get_running_loop()
            self._sigint_previous = signal.signal(signal.SIGINT, self._on_sigint)
            self._sigint_loop = loop
        except (ValueError, OSError, RuntimeError):  # noqa: BLE001 —— 见上：装不上就退回原行为
            self._sigint_previous = None
            self._sigint_loop = None

    def _on_sigint(self, signum, frame) -> None:
        """
        `SIGINT` 处理器：把它转成一次「按了 `Ctrl+C`」。

        :param signum: 信号编号（未使用，签名由 `signal.signal` 规定）
        :param frame: 被打断的栈帧（未使用，同上）

        ⚠ **只排队、不执行**。理由见 `_install_sigint_guard` 的要点 1。
        """
        loop = self._sigint_loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self.action_request_quit)
        except RuntimeError:
            # 循环已经关了（退出竞态）。此时程序本来就在收尾，忽略即可。
            pass

    # ── 崩溃处理（C6 缺口二）───────────────────────────────────────────
    #
    # ⚠ **先说清楚 C6 原报告错在哪，免得下一个人照着它改。** 原报告说「异常会
    # 崩到终端、用户可能得敲 reset」，R3 实跑推翻了：`App.run_async` 的
    # `finally: await asyncio.shield(app._shutdown())` **无条件执行**，备用屏幕
    # 缓冲与 raw mode 两种形态下都复位了。终端不会坏。而且在 `app.run()` 外面
    # 包 catch-all **兜不住主要形态**——绝大多数崩溃发生在消息处理器与事件回调
    # 里（`on_mount`、`on_key`、Worker 的 done callback），Textual 的
    # `_handle_exception` 会把它接住、把 app 关掉，`app.run()` 正常返回，
    # 外面那个 `except Exception` 一个字都收不到。
    #
    # 真缺口是另外两条，下面这两个覆写治的是第二条（第一条是退出码，在
    # `__main__.py` 里）：**崩溃时把带 locals 的完整回溯甩给用户，且没有日志**。
    # `App._fatal_error()` 用的是 `rich.traceback.Traceback(show_locals=True, …)`，
    # 它渲染**每一帧**的局部变量——崩溃时栈上若有 `DeepSeekProvider.__init__`
    # （局部变量 `config` 含明文 `api_key`）、或任何持有文件内容的帧，那些内容
    # 会原样打进终端。本项目对 trace 的配置快照做了逐字段掩码（`redact_config`），
    # **而这条路径上一道掩码都没有**。

    def _handle_exception(self, error: Exception) -> None:
        """
        Textual 留给应用的未捕获异常钩子；**这是形态①真正经过的那个点**。

        :param error: 逃出来的异常

        本覆写只多做一件事：把完整堆栈写进日志。随后**原样交给基类**，
        由它去做 `_return_code = 1`、记 `_exception`、置 `_exception_event`
        这些簿记——那几件事 `run_test` 要靠着重新抛出异常，测试框架与 e2e
        宿主都依赖它，**自己复制一份等于给未来的 textual 升级埋雷**。

        ⚠ 日志写在 `super()` **之前**：基类那一步会关掉整个 app，万一它自己
        再抛一次，至少崩溃证据已经落盘了。

        副作用：往日志文件写一条 ERROR（未开 `--log-file` 时什么都不写）。
        """
        # 完整堆栈只进**日志文件**，不进终端——终端上那份见 `_fatal_error`。
        # ⚠ 这里刻意用 `traceback.format_exception` 而不是 rich 的 Traceback：
        # 我们要的正是**不带 locals** 的那一份（locals 是泄漏面，见上面那段）。
        _logger.error(
            "未捕获异常，应用即将退出：\n%s",
            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        )
        super()._handle_exception(error)

    def _fatal_error(self) -> None:
        """
        覆写基类的崩溃展示：终端上只留一句话，完整堆栈去日志里看。

        基类版本会把 `Traceback(show_locals=True, …)` 追加进退出渲染列表，
        于是每一帧的局部变量都打在用户屏幕上（可能含明文 api_key 与文件内容）。
        本版本换成一行说明 + 日志路径。

        ⚠ **只用公开 API `panic()`**：它做的事与基类 `_fatal_error` 的后半段
        逐字相同（渲染 → 追加进 `_exit_renderables` → `_close_messages_no_wait`），
        但不碰任何私有字段。基类那两个私有名字哪天改了，这里也不会跟着碎。

        ⚠ **反过来说，覆写这件事本身依赖 `_fatal_error` 这个名字还在。**
        它要是被上游改名，本覆写会变成一段谁也不调的死代码，而**掩码会静默
        消失**——那正是本项目最忌讳的形态。`tests/test_tui_crash.py` 里有一条
        用例专门断言这个方法在基类上存在，textual 升级掉它时当场红。

        副作用：响铃、追加退出渲染、关闭消息泵（= 退出应用）。
        """
        self.bell()
        path = logsetup.log_path()
        where = f"完整堆栈已写入 {path}" if path else "用 `rhine --log-file` 重跑可把完整堆栈写进文件"
        # 不用图形符号：错误靠「错误：」文字前缀辨认（tui-display F29 的符号白名单）。
        self.panic(f"错误：RhineCode 遇到未预期的错误已退出。{where}。")

    def on_unmount(self) -> None:
        """还原 SIGINT 处理器，避免把进程级状态留给退出之后的代码。"""
        if self._sigint_previous is not None:
            try:
                signal.signal(signal.SIGINT, self._sigint_previous)
            except (ValueError, OSError, RuntimeError):  # noqa: BLE001 —— 还原失败不该阻断退出
                pass
            self._sigint_previous = None
        self._sigint_loop = None

    def _show_busy_hint(self, text: str) -> None:
        """
        显示一次「当前不能提交」的提示（c11 T55）。

        :param text: 提示文本

        **同一次忙碌期内只提示一次**。用户在等待时连按几次回车是很自然的，
        每次都刷一行会把聊天区淹掉，反而看不见真正的内容。

        复位规则写死三条：
        1. 进入流式时复位一次；
        2. 每次**新**面板弹出时复位一次；
        3. 面板关闭时**不**复位——关闭后就不再拦截提交了，没有复位的必要，
           而在关闭时复位反而会让「面板刚关、流式仍在跑」的窗口里多刷一行。
        """
        if self._busy_hint_shown:
            return
        self._busy_hint_shown = True
        self.query_one(HistoryView).append_system(text)

    def _notify_memory(self, text: str) -> None:
        """
        记忆更新的低打扰通知（c9 F20）。运行在记忆 daemon 线程，
        用 call_from_thread 把渲染调度回主线程（Textual 线程安全要求）。

        **埋点位置刻意排在 call_from_thread 之后**（全阶段复测观察 O5）。

        原先这里直接调 `append_system`、绕过了 `_trace_ui_message`，后果是
        c9 的 AC19「记忆变更时界面出现低打扰提示」在**任何**基于 trace 的验收里
        都是盲区——记录里没有这条 `ui_message`，而「显示了没记」与「压根没显示」
        （notify 为 None，或下面这个 except 把异常吞了）在 trace 上完全无法区分。

        补埋点时把它放在成功调用之后，是为了让「记录里有 ⟺ 界面上真的出现过」
        成立：`call_from_thread` 是**阻塞式**的，它正常返回就意味着主线程确实
        执行完了 `append_system`。反过来若照 `show_message` 那样先记后显示，
        退出竞态下 `call_from_thread` 抛出、异常被下面吞掉，就会留下一条
        **界面上从未出现过的记录**——观测设施撒谎，而且不报错。

        这与 CLAUDE.md 记的 `_do_stream` 那条「埋点放在 call_from_thread 之前」
        **不矛盾**：那里记的是**已经流式显示过**的正文，埋点只是补记录，
        放后面等于「出错时不记录」；这里的提示则是**还没显示**，先记就是撒谎。
        判据是同一条——记录必须与用户真实看到的一致。
        """
        try:
            self.call_from_thread(self.query_one(HistoryView).append_system, text)
            self._trace_ui_message("system", text)
        except Exception:
            # 应用正在退出等边缘情况：通知丢弃即可，不影响任何状态。
            pass

    def _refresh_status(self) -> None:
        """刷新状态栏，反映当前 Provider、模型、思考模式、运行预设、上下文用量。"""
        # 上下文用量（c8）：随对话增长实时变化，故每次刷新都重新取值；
        # 返回 (文本, 是否高亮) 或 None（工具不可用的 Provider）。
        ctx = self._manager.context_status_line()
        # 各值先组成**一份**参数组，再同时喂给 update_status 与纯函数
        # compose_status_text（trace T44）。
        #
        # ⚠️ 不要在下面手抄第二份参数清单：`update_status` 与 `compose_status_text`
        # 是同名同签名，抄一遍会让「新增状态栏字段」这个维护点从两处涨到三处，
        # 而漏改的后果是记录里的状态栏文本与用户实际看到的不一致——最坏的一种
        # 观测设施失效（它撒谎但不报错）。
        status_args = dict(
            provider=self._config.protocol,
            model=self._config.model,
            thinking_effort=self._manager.thinking_effort,
            # auto-plan 扩展：取**推导好的预设**，不再分别取 plan_mode 与权限档。
            # 在这里拼一次「哪个预设」等于把 spec N5 的唯一推导点复制成两处，
            # 而两处迟早分叉——表现为状态栏说 auto、实际在规划阶段。
            preset=self._manager.preset_value,
            # MCP 连接状态（c7）：启动后不变，随每次刷新一并带上即可。
            mcp_status=self._manager.mcp_status_line(),
            context_status=ctx[0] if ctx else None,
            context_warn=bool(ctx and ctx[1]),
            # 已激活 Skill 数（c11）：无激活时为 None，状态栏隐藏该段。
            skill_status=self._manager.skill_status_segment(),
            # 运行中的子 Agent 数（c13）：为 0 时给 None，状态栏隐藏该段——
            # 没用委派的用户看到的状态栏与 c12 逐字一致。
            subagent_status=self._subagent_status_segment(),
        )
        self.query_one(StatusBar).update_status(**status_args)
        # 左区（F31）在这里一并刷新，而不是只在按下 Ctrl+C 时改一次。
        # 好处是「显示态的唯一真相是 `_quit_hint_active`」——任何一条刷新路径
        # 都会把左区拉回与它一致，不会出现某条路径重画了右区却漏掉左区。
        self.query_one(StatusHint).set_quit_hint(self._quit_hint_active)
        # 为什么记「组装后的文本」而不是九个散字段（trace F15）：用户真正看到的
        # 那行文本是 compose_status_text 在 update_status 内部拼出来的，只记字段
        # 的话「文本快照」这个承诺不成立（比如某个字段的渲染分支写错了，
        # 记录里看不出来）。compose_status_text 是纯函数、零副作用，多调一次没成本。
        # `quit_hint` 与 `text` 并列而不是拼进 `text`：左区是**独立组件**，
        # 拼进去的话记录里那行会与用户看到的右区文本对不上。
        # ⚠ 漏记它的后果是「按下 Ctrl+C 之后界面到底有没有给反馈」在记录上
        # 完全无法判断——而这条提示本来就只活两秒，事后没有第二处可查。
        self._recorder.emit_lazy(
            TraceEventType.STATUS_BAR,
            lambda: {
                "text": full_text(compose_status_text(**status_args)),
                "quit_hint": self._quit_hint_active,
            },
        )

    # ------------------------------------------------------------------ #
    # CommandController 协议实现（c10 T43/T45）：命令处理函数经此驱动界面
    # ------------------------------------------------------------------ #
    @property
    def tools_enabled(self) -> bool:
        """当前 Provider 是否具备工具能力（委托 Manager，供 /init 等命令判断）。"""
        return self._manager.tools_enabled

    def _trace_ui_message(self, source: str, text: str) -> None:
        """
        记一条 `ui_message` 事件。

        :param source: 来源（user_echo / system / error / assistant）
        :param text: **markup 转义之前的原始文本**（trace F15）

        为什么记转义前：界面为了不让字面 `[` 被 Textual 当标签吞掉，会做 `\[` 转义
        （见状态栏与历史区的既有做法）。记转义后的文本，读 trace 的人看到的是
        `\[provider]` 这种带反斜杠的怪东西，而那不是用户看到的内容也不是程序
        产生的内容，两头都对不上。
        """
        self._recorder.emit_lazy(
            TraceEventType.UI_MESSAGE,
            lambda: {"source": source, "text": full_text(text)},
        )

    def _trace_tool_batch(self, summary: str, calls: int) -> None:
        """
        记一条 `ui_tool_batch`：一批工具调用归并成了一行（tui-activity-fold N7）。

        由 `HistoryView` 在批次封闭时回调。**记的是聚合语原文与调用数**——
        前者是用户真正看到的那句话（与界面同源，见 `ToolBatchWidget.summary_text`），
        后者是「归并有没有生效」的直接依据：排查「怎么还是一行一行地铺」时，
        看到 `1 次调用` 就知道批次根本没攒起来。

        :param summary: 聚合语纯文本（未转义）
        :param calls: 本批次纳入的调用次数
        """
        self._recorder.emit_lazy(
            TraceEventType.UI_TOOL_BATCH,
            lambda: {"summary": full_text(summary), "calls": calls},
        )

    def show_user_input(self, text: str) -> None:
        """聊天区回显一次用户输入（仅显示，不写入模型历史；由分发器统一调用）。"""
        self._trace_ui_message("user_echo", text)
        self.query_one(HistoryView).append_user(text)

    def show_message(self, text: str) -> None:
        """显示本地命令结果或错误（系统行）。"""
        self._trace_ui_message("system", text)
        self.query_one(HistoryView).append_system(text)

    @staticmethod
    def _history_channel(history_view: HistoryView, level: str):
        """
        级别 → 历史区的对应渲染方法（tui-display 扩展 F19/F20）。

        :param history_view: 历史区组件
        :param level: `LEVEL_NOTICE` / `LEVEL_EVENT` / `LEVEL_WARNING` 之一
        :returns: 可直接交给 `call_from_thread` 的绑定方法

        ⚠ **未知取值当场 `KeyError`，刻意不给兜底**（F20）。留一个
        「认不出就按提示级」的默认分支，等于让任何一处拼错的级别静默退回最暗的
        那一档——而本组的全部价值就在于把「重要的」从「可忽略的」里分出来。
        宁可在开发期炸掉，也不要在生产里悄悄降级。

        错误级不在这张表里：它有独立的 `append_error`，且只由 `ERROR` 事件产出，
        不经级别分发。
        """
        return {
            LEVEL_NOTICE: history_view.append_system,
            LEVEL_EVENT: history_view.append_event,
            LEVEL_WARNING: history_view.append_warning,
        }[level]

    def show_event(self, text: str) -> None:
        """
        显示一条**事件级**系统行（tui-display 扩展 F19）。

        与 `show_message` 的唯一差别是亮度：那条 `[dim]`，这条正常亮度。
        用于「真的发生了一件事」的消息（子 Agent 完成、自动唤起、会话已恢复），
        与「记忆已更新」这类可忽略的提示分开。

        埋点仍记 `source="system"`——**刻意不新增 source 取值**，
        与 `show_warning` / `show_report` 同口径。

        副作用：产出一条 `ui_message` 埋点；往历史区挂一个组件。
        """
        self._trace_ui_message("system", text)
        self.query_one(HistoryView).append_event(text)

    def show_report(self, text: str) -> None:
        """
        显示一段**分级渲染**的命令报告（tui-display 扩展 F15）。

        埋点仍记 `source="system"`——**刻意不新增一种 source 取值**，
        与 `show_warning` 同口径：trace 那边的 source 词汇是断言与阅读器共用的
        契约，为一个样式差异扩充它不划算，而「界面上出现过这段文本」才是这条
        埋点的价值所在。

        副作用：产出一条 `ui_message` 埋点；往历史区挂一个组件。
        """
        self._trace_ui_message("system", text)
        self.query_one(HistoryView).append_report(text)

    def show_warning(self, text: str) -> None:
        """
        显示一条**醒目**的警告（橙色粗体，c12）。

        与 `show_message` 的唯一差别是渲染样式：那条走 `[dim]`（比正文更暗），
        这条走 `[bold #FFA500]`。埋点仍记 `source="system"`——**刻意不新增一种
        source 取值**：trace 那边的 source 词汇是断言与阅读器共用的契约，
        为一个样式差异扩充它不划算，而「界面上出现过这段文本」才是这条埋点的价值。

        今天唯一的用户是项目级 Hook 的启动提示（spec F9.1）。
        """
        self._trace_ui_message("system", text)
        self.query_one(HistoryView).append_warning(text)

    def send_user_message(self, content: str, display_content: Optional[str] = None) -> None:
        """
        把用户消息交给现有对话路径：Manager 追加历史/存档后返回事件流，
        统一经 _consume_manager_result 启动后台 Worker 流式消费。
        """
        self._consume_manager_result(
            self._manager.submit_user_message(content, display_content)
        )

    def switch_mode(self, target: ModeTarget) -> str:
        """按目标模式调用对应领域方法，返回供界面显示的结果文本。"""
        if target == ModeTarget.THINKING:
            return self._manager.cycle_thinking()
        if target == ModeTarget.PRESET:
            return self._manager.cycle_preset()
        # 未知枚举值明确报错（不静默选默认分支）：新增 ModeTarget 时必须同步这里
        raise ValueError(f"未知的模式目标：{target!r}")

    def query_report(self, target: ReportTarget) -> str:
        """按目标报告调用对应只读领域方法。"""
        if target == ReportTarget.MCP:
            return self._manager.mcp_report()
        if target == ReportTarget.CONTEXT:
            return self._manager.context_report()
        if target == ReportTarget.MEMORY:
            return self._manager.memory_report()
        if target == ReportTarget.SKILLS:
            return self._manager.skills_report()
        if target == ReportTarget.SKILLS_PROMPT:
            return self._manager.skills_prompt_report()
        if target == ReportTarget.HOOKS:
            return self._manager.hooks_report()
        if target == ReportTarget.AGENTS:
            return self._manager.agents_report()
        if target == ReportTarget.TASKS:
            return self._manager.team_board_text()
        raise ValueError(f"未知的报告目标：{target!r}")

    def cancel_subagents(self, target: "Optional[str]") -> str:
        """取消子 Agent 任务（`/agents cancel`，c13 F22/F24）。"""
        return self._manager.cancel_subagents(target)

    # ------------------------------------------------------------------ #
    # 子 Agent（c13）
    # ------------------------------------------------------------------ #

    def _subagent_status_segment(self) -> "Optional[str]":
        """
        状态栏的子 Agent 段。

        :returns: 形如 `子Agent:2`；**没有任务在跑时返回 None**（该段隐藏）

        隐藏而不是显示 `子Agent:0`，与 MCP / Skill 两段同构：
        没用委派的用户看到的状态栏与 c12 逐字一致，不平白多一段噪音。

        文本刻意不含方括号（与 `SkillManager.status_segment` 同口径）——
        状态栏走 Content markup，字面 `[` 要转义，能不引入就不引入。
        """
        count = self._manager.running_subagent_count()
        return f"子Agent:{count}" if count else None

    def _poll_subagents(self) -> None:
        """
        定时轮询子 Agent 的完成情况（c13 F21 第 1 步 / F23）。

        由 `on_mount` 注册的 `set_interval` 在**主线程**每 0.5 秒调一次。

        ## 为什么用轮询而不是让后台线程推送

        后台线程要更新界面只能走 `call_from_thread`，而它是**阻塞式**的：
        调用线程会一直等到主线程处理完。子 Agent 线程在持有任务表锁、
        或恰好在 Hook 分发的临界区里调它，就与主线程组成确定性死锁——
        C11 的 `SkillManager` 已经踩过一次，整个 TUI 冻结、调用栈上没有线索。

        轮询把**全部 widget 写入留在主线程**，从结构上消掉这一整类问题。
        代价是完成通知最多晚 0.5 秒出现，对人眼完全无感。

        整段包 try/except：**观测与通知设施绝不能反过来打断界面**。
        一条通知渲染失败最多是少看见一行，而异常逃逸出定时器会让整个
        轮询停摆，后续所有任务的通知一并消失。

        副作用：向聊天区追加通知行；可能刷新状态栏。
        """
        try:
            finished = self._manager.drain_subagent_notifications()
            for record in finished:
                # 事件级：真的发生了一件事，而且它带着这次委派的全部成本。
                # 与「记忆已更新」共用一条 dim 通道正是改造前最刺眼的问题。
                self.show_event(_subagent_finish_text(record))
            running = self._manager.running_subagent_count()
            if finished or running != self._last_subagent_count:
                self._last_subagent_count = running
                self._refresh_status()
            # c15：协作通知（目前只有 N3 降级）与自动唤起判定复用同一次轮询。
            # **不新增 set_interval** —— 空闲会话的 CPU 占用与 C13/C14 完全相同
            # （spec N6 的落实方式，见 spec 里那段措辞修订）。
            for notice in self._manager.team_drain_notices():
                # 事件级：协作侧的降级/状态变化，用户需要知道但无需动作
                self.show_event(notice)
            # tui-display 扩展 F4：活动区的数据也搭这趟车。
            # **不新增定时器**——空闲会话的开销必须与改造前一致。
            self._refresh_activity()
            self._maybe_auto_wake()
        except Exception:
            # 应用退出竞态、渲染异常等：丢弃即可，绝不让它打断定时器。
            pass

    def _refresh_todo(self) -> None:
        """
        把待办块刷成清单当前的样子（todo-list 扩展 F10/F11/F15）。

        **必须在主线程调用。** 唯一的调用来源是 `_do_stream` 里那次
        `call_from_thread`，以及 `_reset_display_state`。

        ## 版本号驱动，不是定时器驱动

        清单每改一次内部版本号加一。这里先比对版本号，没变直接返回——
        于是重绘天然幂等，反复调用零成本。

        ⚠ **刻意不搭 `_poll_subagents` 那个 0.5 秒定时器**：它**只在子 Agent
        服务启用时才注册**（见 `on_mount` 里那个 `if ... is not None`），
        搭它会让待办块在关掉子 Agent 的配置下**整个不刷新**——
        而配置和界面上都看不出异常。
        ⚠ 也**不新增定时器**：空闲会话的开销必须与改造前一致（与活动区同一约束）。

        ## 「全部完成」那一行只在**跃迁**的那一刻留（F15）

        判据是「上一次显示过 **且** 这一次不该显示了 **且** 清单确实是全完成」。
        三个条件缺一不可：
        - 少了第一条，一个从来没显示过的会话也会冒出这行；
        - 少了第三条，`/clear` 造成的「不该显示」会被误当成「全做完了」。

        整段包 `try/except`：**观测与通知设施绝不能反过来打断界面**。

        副作用：可能向聊天区追加一行事件级记录；改待办块的内容与可见性。
        """
        try:
            version = self._manager.todo_version()
            if version == self._todo_version:
                return
            self._todo_version = version
            view = self._manager.todo_view()

            if self._todo_shown and view is None and self._manager.todo_all_done():
                # 事件级：真的完成了一件事，该看得见。提示级（dim）那档是留给
                # 「记忆已更新」这类误读代价为零的消息的。
                self.show_event(self._manager.todo_all_done_text())

            self.query_one(TodoPane).update_view(view)
            # 待办块可见时历史区收起自己的下边框，由本块接着画——
            # 不切的话两者之间会多一条横线，看起来是两个框（见那段 CSS 的说明）。
            self.query_one(HistoryView).set_class(view is not None, "-todo-open")
            self._todo_shown = view is not None
        except Exception:
            # 应用退出竞态、渲染异常等：丢弃即可，绝不让它打断调用方
            # （调用方是 Agent Loop 的事件消费循环）。
            pass

    def _refresh_activity(self) -> None:
        """
        把子 Agent 活动区刷成任务表当前的样子（tui-display 扩展 F4/N7）。

        由 `_poll_subagents` 在**主线程**调用，与完成通知共用同一个 0.5 秒节拍。

        ⚠ **本方法只做纯内存读取与组件更新**（N7）：不做 IO、不发请求、
        不调 `call_from_thread`。它跑在每 0.5 秒都会执行的路径上，
        往里加任何一件慢事都会让整个界面卡顿。

        副作用：重绘活动区组件。
        """
        # 活动区只有折叠 / 展开两态（tui-activity-fold F13：**不为它造第三档**）。
        # 「逐条」与「全文」对它而言表现一致——它展开后列的是最近的工具调用，
        # 那些本来就没有「更详细」的第二层可展。
        self.query_one(ActivityView).update_rows(
            self._manager.subagent_activity(), self._detail_level != DETAIL_FOLDED
        )

    def _maybe_auto_wake(self) -> None:
        """
        主对话空闲且有队友消息时，自动跑一轮处理它（c15 F17/F20/F21）。

        由 `_poll_subagents` 每 0.5 秒调一次。三个条件全满足才触发：

        1. **主对话空闲** —— 没有流式 Worker 在跑，也没有面板等着人应答；
        2. 有发给 `main` 的未读消息；
        3. 自动唤起的连锁次数未达上限。

        ⚠ **第 1 条不可省，且必须排在最前。** 流式 Worker 是 `exclusive=True`
        的：不判空闲就起新 Worker，会把**正在跑的那个挤掉**——用户正在等的
        回答凭空消失，而界面上只表现为「AI 说到一半不说了」。

        ⚠ `_pending_interaction` 也要判：确认面板挂着时用户显然在场，
        这时自动跑一轮会让两个运行争同一个面板。

        副作用：可能启动一条流式 Worker（一次真实的模型调用）；
        向聊天区追加提示行。
        """
        if self._stream_active or self._pending_interaction is not None:
            return
        if self._session_panel_active:
            return
        if not self._manager.team_has_unread_for_main():
            return

        if not self._manager.team_can_auto_wake():
            # ⚠ 只提示一次：本方法每 0.5 秒被调一次，不设标志会刷满整屏。
            if not self._auto_wake_limit_notified:
                self._auto_wake_limit_notified = True
                self.show_event(
                    f"队友还在发消息，但自动唤起已达上限"
                    f"（连续 {self._manager.team_auto_wake_limit()} 次）——"
                    f"先停下来等你回来。说句话就能继续。"
                )
            return

        count = self._manager.team_bump_auto_wake()
        limit = self._manager.team_auto_wake_limit()
        # F21：用户回来时要能一眼看出「这段是我不在的时候程序自己跑的」。
        # 事件级：用户回来时要能一眼看出「这段是我不在的时候程序自己跑的」。
        # `⟳` 去掉（F28）——「自动唤起」四个字本身已经说清了，符号不添信息。
        self.show_event(
            f"自动唤起（第 {count}/{limit} 次）——队友发来了消息，"
            f"主对话在你不在场时自行处理。"
        )
        self._start_stream_worker(self._manager.run_auto_wake())

    def refresh_status(self) -> None:
        """刷新状态栏（命令处理函数显式调用，取代旧的命令字符串白名单）。"""
        self._refresh_status()

    def clear_conversation(self) -> None:
        """
        清空对话：领域侧清历史/开新档 + 界面侧清聊天区（确认文本由命令层显示）。

        活动区一并清空（tui-display 扩展 F8）。⚠ 这不是「顺手也清一下」：
        `/clear` 会取消还在跑的子 Agent 并开新的会话代，那些任务的行留在活动区里
        就是**在展示一段已经不存在的对话的状态**。领域侧下一轮轮询也会把它们
        滤掉（它们随即转终态、再过几秒淡出），但那中间有半秒到几秒的窗口，
        用户会在一个刚清空的界面上看到上一段对话的残影。
        """
        self._manager.clear()
        self.query_one(HistoryView).clear_all()
        self.query_one(ActivityView).update_rows(())
        self._reset_display_state()

    def _reset_display_state(self) -> None:
        """
        会话切换后的界面复位（tui-activity-fold F20）：撤下状态行、档位回默认。

        ⚠ **`/clear` 与 `/resume` 两条路径共用这一处。** 各写一遍的话，
        必然出现「清空能复位、恢复不能」这种一半对的状态，而那在界面上
        表现为「上一段对话的展开档位莫名其妙地留着」。

        状态行本应已由 `_set_streaming(False)` 收掉，这里再兜一次——
        会话切换可能发生在一次运行的异常路径上。

        todo-list 扩展 F17：待办块一并收起。**这是界面那一半**，
        数据那一半由协调层的 `_clear_todo` 负责（两处配合才完整）。
        """
        self.query_one(StatusLine).stop()
        self._detail_level = DETAIL_FOLDED
        self.query_one(HistoryView).set_detail_level(DETAIL_FOLDED)
        # 待办块收起 + 刷新状态复位（todo-list 扩展 F17）。
        #
        # ⚠ **版本号必须一起复位。** 新会话的清单从 0 重新计数，不复位的话
        # 第一次覆写（版本号 1）在旧值恰好是 1 时会被判成「没变」，
        # 于是**那一次刷新被整个跳过**——用户看到的是「换了会话之后
        # 第一次列待办不显示」，看起来像功能坏了。
        # 旧值不是 1 时它碰巧能工作，而「碰巧对」正是这类 bug 难查的原因。
        #
        # ⚠ `_todo_shown` 也要复位，否则下一段对话第一次收起待办时会误留
        # 一行「全部完成」——那条记录的判据里有「上一次显示过」。
        self.query_one(TodoPane).update_view(None)
        self.query_one(HistoryView).set_class(False, "-todo-open")
        self._todo_version = 0
        self._todo_shown = False

    def compact_context(self) -> None:
        """手动压缩：Manager 返回事件流（阻塞的摘要 LLM 调用）走后台 Worker。"""
        self._consume_manager_result(self._manager.manual_compact())

    def resume_session(self, key: Optional[str]) -> None:
        """
        恢复会话：key 为 None 弹选择面板、非 None 直接载入。
        面板选中路径与命令路径复用本方法（c10 起不再伪造 "/resume <id>" 文本）。
        """
        self._consume_manager_result(self._manager.resume(key))

    def exit_application(self) -> None:
        """退出应用（/exit 经此退出，不再依赖 SystemExit 穿透，c10）。"""
        self.exit()

    # ---- Skill 控制器方法（c11）----

    def run_skill(self, name: str, arguments: str, display: str) -> None:
        """
        执行一个 Skill（c11 F24）。

        两种模式的返回值形态不同（共享模式返回主对话事件流、独立模式返回子对话
        事件流、找不到时返回文本），全部交给 `_consume_manager_result` 统一消费。
        """
        self._consume_manager_result(
            self._manager.run_skill(name, arguments, display)
        )

    def reload_skills(self) -> str:
        """
        热更新 Skill 定义**并重新注册斜杠短命令**（c11 F26），返回报告文本。

        分两步，顺序不能反：
        1. 领域侧 `manager.reload_skills()` 重新扫盘、重新解析 `allowed-tools` 声明、
           同步激活列表，产出可读报告；
        2. 界面侧用**新的** `command_infos()` 整体替换注册表里的 Skill 短命令。

        **为什么接在这里而不是 `conversation.py`**：短命令刷新需要
        `CommandRegistry`，而协调层刻意不依赖 commands 包（依赖方向是
        `commands ← tui/app ← __main__`）。而 `RhineApp` 本来就同时持有
        注册表与 SkillManager，且已经导入 commands——这正是「领域能力放
        conversation、接线放控制器方法」的既定模式，不需要新增任何回调或协议方法。

        补全菜单与输入高亮**无需刷新**：`CommandPanel` 与 `CommandHighlighter`
        持有的是注册表引用，每次按键现调 `complete()` / `resolve()`，
        替换后下一次按键就是新结果。

        副作用：重新扫盘；替换注册表的 Skill 短命令集合。
        """
        report = self._manager.reload_skills()

        skipped = self._command_registry.replace_skill_commands(
            build_skill_command_specs(self._manager.skill_manager.command_infos())
        )
        if skipped:
            # 与启动时同一口径：短命令没注册不等于 Skill 不可用，
            # 必须给出 /skills run 这个替代入口，否则用户会以为 Skill 坏了。
            names = "、".join(s.name for s in skipped)
            report += (
                f"\n\n以下短命令与已有命令冲突、未注册：{names}"
                f"\n请改用 /skills run <名字> 执行它们。"
            )
        return report

    def deactivate_skill(self, name: Optional[str]) -> str:
        """卸载已激活的 Skill；name 为 None 表示全部。返回结果文本。"""
        return self._manager.deactivate_skill(name)

    def _notify_skill_activation(self) -> None:
        """
        模型激活 Skill 后立刻刷新状态栏（c11 T53）。

        本方法由 `SkillManager` 在**工作线程**里回调，因此必须 `call_from_thread`
        跨回主线程更新界面。

        **不能用裸 lambda 而要包 try/except**：`activate()` 跑在
        `LoadSkillTool.execute()` 里，而它又在只读并发桶的 `ThreadPoolExecutor` 里；
        `future.result()` 外层的 `except Exception` 会把这里抛出的任何异常
        转成「工具执行异常」——于是应用退出竞态下一次本已成功的激活，
        会被报告成工具失败回灌给模型，模型可能因此重试或放弃。

        本回调的价值只在「激活当下立刻刷新」：`_do_stream` 的 finally 里已经
        无条件刷一次状态栏，所以即使这里丢掉一次刷新也不会留下错误状态。

        副作用：跨线程调度一次界面刷新。
        """
        try:
            self.call_from_thread(self._refresh_status)
        except Exception:
            # 应用正在退出等边缘情况：刷新丢弃即可。
            pass

    def _notify_preset_change(self) -> None:
        """
        预设在工作线程里变化后立刻刷新状态栏（auto-plan 扩展 F12）。

        目前唯一的触发点是「计划获批 → 预设当场变回 `auto`」，它发生在
        `ConversationManager._approve_plan_then_exit` 里、跑在 Worker 线程上。
        不刷的话，整个执行阶段状态栏都还写着 `\\[PLAN]`——而那时模型已经在
        动手改文件了，标记与实际情况正好相反。

        ⚠ **本方法只能从工作线程调用。** Textual 的 `call_from_thread` 在主线程上
        调会直接报错，所以主线程发起的切换（`Shift+Tab` 与 `/mode`）**不走这里**
        ——那两条路径在动作/命令处理函数里同步调 `_refresh_status()` 即可。

        与 `_notify_skill_activation` 同型：**必须包 try/except**，且丢一次刷新
        不会留下错误状态——`_do_stream` 的 finally 里无条件再刷一次。
        这里若把异常放出去，它会顺着审批回调的返回路径爬回 Agent 循环，
        被当成一次工具执行异常回灌给模型。

        副作用：跨线程调度一次界面刷新。
        """
        try:
            self.call_from_thread(self._refresh_status)
        except Exception:
            # 应用正在退出等边缘情况：刷新丢弃即可。
            pass

    def _consume_manager_result(self, result) -> None:
        """
        统一消费 Manager 领域方法的三类返回值（c10 T44）：

        - str：本地反馈文本，直接显示；
        - SessionListRequest：打开会话选择面板；
        - 事件迭代器：设置 streaming 状态并在下一帧启动现有 Worker 流式消费
          （同一时间只允许一个流式 Worker，由 exclusive=True 与提交守卫共同保证）。
        """
        if isinstance(result, str):
            self.show_message(result)
        elif isinstance(result, SessionListRequest):
            self._show_session_panel(result)
        else:
            self._set_streaming(True)
            # 先让本帧渲染（用户输入回显等）完成，再启动后台 Worker——
            # 避免用户消息与首块回复合并在同一帧绘制（观感上像输入被延迟显示）。
            self.call_after_refresh(self._start_stream_worker, result)

    # ------------------------------------------------------------------ #
    # 输入与命令面板
    # ------------------------------------------------------------------ #
    def on_input_changed(self, event: Input.Changed) -> None:
        """
        监听输入框内容变化，控制命令提示面板的显示与过滤。

        仅当输入以 "/" 开头且仍处于命令字段（尚无空白分隔符，即尚未进入参数区）
        时显示候选面板；参数区输入、普通文本、零候选均隐藏（c10 plan 10.2）。

        ## ⚠ 流式运行期间**照常显示**（真机反馈后改，别改回来）

        这里原本还有一条 `or self._stream_active`。它造成的现象是
        **「请求发出去之后敲 `/` 就没反应了」**——而输入框在忙碌期间
        **是不禁用的**（见 `_set_streaming` 的说明：那样 Esc 才能可靠路由），
        于是用户能打字、字也进去了，**唯独提示面板不出来**。
        「能打字但没有任何反应」比「压根不让打」更让人困惑。

        那条守卫对另外两个是必要的——确认面板与会话面板**占着同一个
        浮层容器**，这时候抢过来会把交互链打断。而「正在跑模型」与这个
        容器毫无关系：命令面板只是一张**只读的提示列表**，弹出来不改变
        任何状态，也不会让用户多做成任何事（真正的闸门是
        `on_input_bar_input_submitted` 里那条 `_stream_active` 守卫，
        它拦的是**提交**，那一条原样保留）。
        """
        if (
            self._pending_interaction is not None
            or self._session_panel_active
        ):
            return
        panel = self.query_one(CommandPanel)
        value = event.value
        if value.startswith("/") and not any(ch.isspace() for ch in value):
            panel.show_for(value)
        else:
            panel.hide()

    def on_input_bar_command_completion_requested(
        self, event: InputBar.CommandCompletionRequested
    ) -> None:
        """
        处理命令字段的 Tab 补全请求（c10 T47，spec F22）。

        - 单候选：直接替换输入框命令字段；候选规范命令有参数提示时末尾保留一个空格；
        - 多候选：显示稳定排序的候选菜单（焦点保持在 InputBar，方向键经 on_key
          转发给面板移动高亮，Enter 执行当前高亮项）；
        - 零候选：隐藏面板，不做任何改动。

        ⚠ **流式运行期间照常补全**，理由与 `on_input_changed` 那条完全相同
        （见那里的说明）：输入框忙碌时不禁用，只关补全会造成
        「能打字、Tab 却没反应」。两处必须同口径——只改一处的话，
        运行中 `/mo` 弹得出面板却按不了 Tab，比两处都关更莫名其妙。
        """
        if (
            self._pending_interaction is not None
            or self._session_panel_active
        ):
            return
        items = self._command_registry.complete(event.prefix)
        panel = self.query_one(CommandPanel)
        if not items:
            panel.hide()
            return
        if len(items) == 1:
            item = items[0]
            spec = self._command_registry.resolve(item.canonical_name)
            trailing = bool(spec and spec.argument_hint)
            self.query_one(InputBar).apply_completion(item.value, trailing_space=trailing)
            panel.hide()
            return
        panel.show_for(event.prefix)

    # ------------------------------------------------------------------ #
    # 键位动作（tui-display 扩展 F31/F32/F5）
    # ------------------------------------------------------------------ #

    def action_noop(self) -> None:
        """
        什么都不做——用来**吃掉** `Ctrl+Q`（F31）。

        退出行为来自 Textual 自带的 priority 绑定，不覆盖是去不掉的。
        绑到一个空动作上，按下去就真的什么都不发生。
        """

    def action_cycle_preset(self) -> None:
        """
        在 `auto` 与 `plan` 两个预设间循环（`Shift+Tab`，auto-plan 扩展 F5/F6）。

        与 `/mode` 命令走**同一个**领域方法 `cycle_preset()`，两条入口的行为
        逐字相同——各自实现一遍的话，迟早出现「命令切得动、按键切不动」这类
        只在一条路径上现形的分叉。

        ⚠ **本动作不得碰焦点**（spec F5）。`Shift+Tab` 在 Textual 里原本是
        「焦点移到上一个组件」，我们抢占了它；抢占之后再自己去动焦点，
        等于把被抢掉的行为又还回去一半，用户会看到「模式变了、光标也跑了」。
        与 `Ctrl+O` 的既有做法一致（那条同样只改状态、不动焦点）。

        ## ⚠ 切换成功时**不往历史区写东西**

        按键的反馈就是**状态栏那一格变了**（`[AUTO]` ⇄ `[PLAN]`，右区常驻）。
        再往聊天区写一条「模式：plan」是同一件事说两遍，而聊天区是对话内容、
        不是状态显示——这与 tui-display F31 给 `Ctrl+C` 提示定的口径一致
        （那条同样只活在状态栏左区，**刻意不进聊天区**）。

        **但切不动的时候必须写。** 非 DeepSeek Provider 上两条轴都无可控对象，
        此时若也保持安静，用户按下去会毫无反应——分不清是「没生效」还是
        「这个键压根没被接住」。判据走具名常量 `PRESET_SWITCH_UNAVAILABLE`，
        不比字面量（有人改文案时字面量比较会静默失配，表现为
        「切不动时也不再提示」，而那正是这条分支存在的全部理由）。

        ⚠ **`/mode` 那条入口仍然照常回显**，这不是分叉：用户**敲了一条命令**，
        一条命令不给任何回应看起来就是没执行；而按键有状态栏当回执。
        两条入口共用的是**领域行为**（`cycle_preset` 写哪两条轴），
        本来就不包括「界面上怎么回执」。

        副作用：改写权限引擎档位与 `plan_mode`；刷新状态栏；
                仅在切换不可用时向历史区写一条提示。
        """
        result = self.switch_mode(ModeTarget.PRESET)
        if result == PRESET_SWITCH_UNAVAILABLE:
            self.show_message(result)
        self._refresh_status()

    def action_toggle_expand(self) -> None:
        """
        全局详细度档位，**三档循环**（`Ctrl+O`）：折叠 → 逐条 → 全文 → 折叠。

        **一个键同时管活动区与历史区**——对齐 Claude Code 的全局 verbose 语义。
        两个键会让用户记两套，而这两处展开的是同一类东西（「刚才具体做了什么」）。

        ## 三档各是什么

        | 档 | 历史区 | 活动区 |
        | --- | --- | --- |
        | 折叠 | 批次只显示一行聚合语 | 每个子 Agent 一行 |
        | 逐条 | 批次摊成逐次调用，单条结果仍受行数上限 | 列出最近的工具调用 |
        | 全文 | 单条显示完整参数与输出原文 | **与逐条一致**（F13：活动区没有第三档） |

        ⚠ 三态循环本身难以预期（用户记不住按第三下会怎样），因此**不靠记忆**
        ——批次的聚合行末尾常驻一句提示，写的是**按下去会到哪一档**。

        ⚠ **不引入焦点切换**：本动作只重绘，不 `focus()` 任何组件。
        活动区任何时候都不抢焦点，输入框与四个面板的键位体系一字不动（F5）。

        副作用：重绘活动区与历史区的批次与工具行。
        """
        index = DETAIL_CYCLE.index(self._detail_level) if self._detail_level in DETAIL_CYCLE else 0
        self._detail_level = DETAIL_CYCLE[(index + 1) % len(DETAIL_CYCLE)]
        self._recorder.emit_lazy(
            TraceEventType.UI_DETAIL_LEVEL,
            lambda level=self._detail_level: {"level": level},
        )
        self._refresh_activity()
        # 历史区自己记下档位并广播给已挂载的批次与工具行——**不要在这里直接
        # 遍历组件**：那样只覆盖「此刻挂着的」，切档之后新产生的又会是折叠的。
        self.query_one(HistoryView).set_detail_level(self._detail_level)

    def on_detail_level_clicked(self, event: "DetailLevelClicked") -> None:
        """
        某一行被鼠标点开/收起了：**把全局档位同步到它切到的那一档**。

        不同步的话，点击与 `Ctrl+O` 会各记各的档位——用户点开一行之后再按
        `Ctrl+O`，第一下只是把全局从折叠推到逐条（那一行看不出任何变化），
        得按第二下才到全文。用户原话：「先点击展开后得按两下 ctrl+o
        才能切换到 3 档」。

        ⚠ **只同步数字，不重新广播**（不调 `HistoryView.set_detail_level`）：
        广播会把满屏的批次一起摊开，而「点一下只开这一个」正是鼠标存在的理由。
        下一次 `Ctrl+O` 才是广播的时机。

        副作用：改 `self._detail_level`。
        """
        self._detail_level = event.level

    def on_overlay_panel_visibility_changed(
        self, _event: "OverlayPanel.VisibilityChanged"
    ) -> None:
        """
        某个浮层面板显示或隐藏了：重算历史区要让出多少底部空间。

        面板改成浮层之后不再挤压历史区（那是抖动的根源），但它们会**盖住
        历史区最后几行**——而那几行往往正是用户要看的（「我在批准哪一次写入」
        的那条工具行就在最后）。这里给历史区补一个等于面板高度的底部内边距，
        内容随之上移，被盖住的部分重新露出来。

        ## 为什么走消息而不是在调用点同步

        Textual 的 `Show` / `Hide` 事件**在 app 层收不到**（实测 `on_show` /
        `on_hide` 一次都不触发），而面板的显示/隐藏散落在四个组件的
        `show_for` / `hide` 里。让面板自己广播，就不必在 app 里枚举所有调用点
        ——枚举那种写法漏一处不报错，只表现为「某个面板弹出时内容少了几行」。

        副作用：改 `HistoryView` 的 `padding` 样式。
        """
        # 面板高度要等布局算完才知道，故推到下一帧再读
        self.call_after_refresh(self._reserve_space_for_panels)

    def _reserve_space_for_panels(self) -> None:
        """
        按当前可见面板的高度，设置历史区的底部内边距。

        面板互斥（同时最多一个可见），但仍按总和算——多一个面板同时弹出时
        这里不会算错，而写死「取第一个」会在那种情况下少让一块地方。
        """
        try:
            # ⚠ **不能写 `self.query(OverlayPanel)`。** Textual 的类型查询按
            # **CSS 类型名**匹配，而那套名字只收 Widget 子类——`OverlayPanel`
            # 是个纯 mixin，不在其中，查出来恒为空（实测：面板明明可见、
            # 查询结果 0 个，padding 永远算成 0，而且不报任何错）。
            any_visible = any(
                isinstance(widget, OverlayPanel) and widget.display
                for widget in self.query("*")
            )
            dock = self.query_one("#panel-dock")
            # ⚠ **没有面板时容器必须整个隐藏。**
            #
            # 它带一圈缩进（把面板收进历史区框内），而缩进在 `height: auto` 下
            # **照样算进高度**——于是空面板的容器仍占 1 行，dock 在 stage 底部时
            # 正好**压住历史区的下边框**，那条框线变成一行空白。
            # 真机反馈：「原本的历史记录下边框的边框没了」。
            dock.display = any_visible
            # ⚠ **高度取自面板自身，不能读容器的 `outer_size`。**
            # 容器刚从隐藏切到显示，此刻它的尺寸还是上一次布局的值（0），
            # 当场读会把内边距算成 0、内容仍被盖住；而等下一帧再读又要多绕一次
            # 异步，实测在并发跑测试时帧数不稳。
            # 面板本身此刻**已经布局完**（它先于本方法被显示），读它是可靠的。
            panel_h = max(
                (
                    widget.outer_size.height
                    for widget in self.query("*")
                    if isinstance(widget, OverlayPanel) and widget.display
                ),
                default=0,
            )
            # 容器自身的缩进从**样式声明**取，同样不依赖布局是否算完。
            # ⚠ 与 `#panel-dock` 的 CSS 成对：那边改了缩进/边框，这里自动跟上
            # ——`gutter` 已经把 padding 与 border 一起算了，改用哪一种都不必动这行。
            spacing = dock.styles.gutter.height + dock.styles.margin.height
            reserved = (panel_h + spacing) if any_visible else 0
            # todo-list 扩展：待办块**已经在历史区下面占着地方了**，而面板浮在
            # 它上面。面板盖住的是待办块，不是历史内容——那部分不必再让一次。
            #
            # ⚠ 不减掉的话，每次弹面板历史内容都会**白抖一下**：往上跳了
            # 待办块那么多行，而被盖住的压根不是它们。这正是 tui-activity-fold
            # 花一整轮消掉的那类抖动，别让它从另一个入口回来。
            #
            # 夹到 0：面板比待办块高时，超出的部分仍要让。
            if reserved:
                todo = self.query_one(TodoPane)
                if todo.display:
                    reserved = max(0, reserved - todo.outer_size.height)
            # 只改下边距，左右沿用原样式（padding: 0 1）
            self.query_one(HistoryView).styles.padding = (0, 1, reserved, 1)
        except Exception:  # noqa: BLE001
            # 布局相关的兜底：这只是「让内容别被盖住」的锦上添花，
            # 出错时宁可少让一块地方，也不能把界面拆了。
            pass


    def action_request_quit(self) -> None:
        """
        `Ctrl+C`：**连按两次**才退出（F31/F32）。

        ⚠ **本方法是两条路径共同的落点**：按键（BINDINGS）与 `SIGINT`
        （`_on_sigint`）。判定只有这一份，两条路径因此不可能给出不同的结果
        ——写成两套的话，「按键要两下、信号一下就退」这种偏差在界面上完全看不出来。

        ## 三条分支，顺序固定

        1. **屏幕上有选中文本 → 复制，且不计数**；
        2. 距上次按下 ≤ `QUIT_CONFIRM_SECONDS` → 退出；
        3. 否则记下时间戳，并在**状态栏最左侧**挂出「再按一次 Ctrl+C 退出」
           （见 `_arm_quit_hint`）。

        ## 为什么复制这一支必须存在

        C2 那条护栏（`Ctrl+C` 不得绑定退出）的理由是「**`Ctrl+C` 用于复制场景**」。
        而 Textual 里 `Screen` 与 `Input` **各有一条** `ctrl+c → copy` 绑定，
        两条都是 `priority=False`——**都会被我们上面那条 priority 绑定盖掉**。
        不做分流的话，「Ctrl+C 复制」这个今天真实可用的功能会整个消失，
        而那正是当年写下那条护栏时指的东西。

        两处选中来源**缺一不可**（实测确认是两套独立机制）：
        鼠标在历史区拖选走 `screen.get_selected_text()`，
        输入框内 Shift+方向键选中走焦点组件的 `selected_text`。

        ## 「不计数」是这条设计的要害

        连续复制五次，一次都不会靠近退出。反过来（复制也计入双击）会让
        「连按两次复制」意外退出程序——那个方向更糟。

        **已知代价（接受）**：屏幕上有选中内容时按两次得到的是「复制两次」，
        不会退出；想退出需先清掉选中。相比「复制两次就退出」，这个方向更安全。

        副作用：可能复制到剪贴板、可能改状态栏并起一个定时器、可能退出应用。
        """
        if self._copy_selection_if_any():
            return

        now = monotonic()
        if now - self._last_quit_request <= self.QUIT_CONFIRM_SECONDS:
            self._settle_pending_before_quit()
            self.exit()
            return

        self._last_quit_request = now
        self._arm_quit_hint()

    def _settle_pending_before_quit(self) -> None:
        """
        退出前把还挂着的交互结算掉（C10-a）。

        ## 这条缺口的实测后果比原报告严重一档

        原报告说后果是「线程停到天亮」。R3 实跑出来的是**整个进程退不掉**：

            ⚠ app.run() 在 25 秒后仍未返回 —— 退出被那个 wait() 挡住了
            ThreadPoolExecutor 线程 daemon = False
            3.11 的 shutdown_default_executor 签名: (self)

        链条是这样的：`run_worker(thread=True)` 最终走
        `loop.run_in_executor(None, …)`，用的是 **asyncio 的默认线程池**；
        `ThreadPoolExecutor` 的线程**不是 daemon**；`asyncio.run()` 收尾时调
        `loop.shutdown_default_executor()`，而 **Python 3.11 的这个方法没有
        timeout 参数**（3.12 才加），于是它**无限期等待**那个停在
        `Event.wait()` 上的线程。

        结果：`app.run()` 永不返回 → `__main__.py` 的 `finally: result.cleanup()`
        **永不执行** → MCP 子进程不回收、会话锁不释放、trace 文件句柄不关闭。
        用户看到的是「按了退出，程序卡死了」。

        ## 触发路径

        `ctrl+c` 那条绑定是 `priority=True`，面板持有焦点时照样能触发；
        而退出分支此前直接 `self.exit()`，**不检查 `_pending_interaction`**。
        所以「面板挂着 → 连按两次 Ctrl+C」这条路径在真实入口下会踩中。

        ⚠ **e2e 宿主复现不了这一条**（它走 `app.run_test()`，收尾语义不同，
        R3 实测宿主干净退出了）。证据来自真实 `app.run(headless=True)`。

        ## 为什么是「结算」而不是「给 wait 加超时」

        `None` 在三类面板上的语义都已经是「取消 / 拒绝 / 跳过」，语义现成，
        且它必须走 `_resolve_interaction`——那是**唯一**会 `box["event"].set()`
        的地方。

        ⚠ **绝不能写成「`wait(timeout=N)` 然后按超时返回一个默认结果」**：
        那等于「用户没答，我替他答了」，在确认面板上就是**替用户点了放行或
        拒绝**。超时只能用来**跳出等待**，不能用来**编造结果**。这里用的是
        用户按 Ctrl+C 这个明确动作，不是超时。

        ## 两条路都要走

        会话选择面板走的是**另一条**结算路径（`_settle_session`），它同样在
        退出时不被结算。只修一条是半个修复。

        副作用：唤醒被阻塞的 Worker 线程；可能隐藏面板、还焦输入框。
        """
        # 确认 / 澄清 / 审批三类面板：None = 取消 / 拒绝 / 跳过
        if self._pending_interaction is not None:
            self._resolve_interaction(None, source="shutdown")
        # 会话选择面板：它有自己的结算入口（`_settle_session` 是唯一入口，
        # 走别处会丢埋点与幂等守卫）
        if self._session_panel_active:
            self._settle_session(None, source="shutdown")

    def _arm_quit_hint(self) -> None:
        """
        在状态栏最左侧挂出退出提示，并安排它在有效期结束时自己消失（F31）。

        ## 为什么是状态栏而不是聊天区

        这条提示是一个**只活两秒的瞬时状态**，不是对话内容。写进聊天区的话，
        每一次误按都会在历史里留下一条永久噪音，而它在两秒后就已经**不再成立**
        ——历史区里躺着一句「再按一次就退出」，可那时按一次根本不会退，
        提示本身变成了错的。状态栏是「当前是什么状态」该待的地方：窗口一过
        自己消失，什么痕迹都不留。

        ## 到期与判定的关系

        定时器与判定共用同一个 `QUIT_CONFIRM_SECONDS`，所以提示在屏幕上的存续期
        **就是**连按有效期：看得见提示 = 现在按第二下能退出，提示没了 = 得重新按。

        ⚠ 但**判定的依据始终是 `_last_quit_request` 这个时间戳，不是显示标志**。
        定时器的调度有抖动（事件循环忙的时候会晚几毫秒），拿标志当判据等于把
        这点抖动变成退出行为的抖动；而反过来（时间戳判定 + 标志只管显示）
        最坏也只是提示多挂了几毫秒，没有任何行为后果。

        ## 重复按下的处理

        每次都先**取消**上一个定时器再起新的。不取消的话，第一次按下起的那个
        定时器会在第二次按下之后的某个时刻把提示清掉——用户明明刚按过一下，
        提示却提前消失了，看上去像窗口缩短了。

        副作用：改状态栏显示态，起一个 `QUIT_CONFIRM_SECONDS` 后触发的定时器。
        """
        if self._quit_hint_timer is not None:
            self._quit_hint_timer.stop()
        self._quit_hint_active = True
        self._refresh_status()
        self._quit_hint_timer = self.set_timer(
            self.QUIT_CONFIRM_SECONDS, self._expire_quit_hint
        )

    def _expire_quit_hint(self) -> None:
        """有效期结束：撤下状态栏上的退出提示（F31）。"""
        self._quit_hint_timer = None
        if not self._quit_hint_active:
            return
        self._quit_hint_active = False
        self._refresh_status()

    def _copy_selection_if_any(self) -> bool:
        """
        屏幕上有选中文本就复制它，返回是否复制过。

        :returns: True 表示本次 `Ctrl+C` 是一次复制，**不该计入双击**

        整段 try/except：剪贴板在某些终端里不可用，而复制失败不该妨碍
        「再按一次就退出」这条主路径。
        """
        try:
            focused = self.focused
            selected = getattr(focused, "selected_text", "") if focused else ""
            if not selected:
                selected = self.screen.get_selected_text() or ""
            if not selected:
                return False
            # **两条路一起走，只要有一条成了就行。**
            #
            # `copy_to_clipboard` 走 OSC 52 转义序列（由终端代为写剪贴板），
            # 好处是天然支持 SSH，代价是**很多终端出于安全默认关闭它**——
            # 而应用这一端只是往标准输出写了几个字节，**成没成功它根本不知道**。
            # 用户侧的表现就是「选中了、按了 Ctrl+C、什么也没发生」，且无任何报错。
            #
            # 因此再直接调一次操作系统的剪贴板（见 `tui/clipboard.py`）。
            self.copy_to_clipboard(selected)
            copy_text(selected)
            return True
        except Exception:  # noqa: BLE001 —— 见上：复制失败不阻断退出路径
            return False

    def _handle_digit_choice(self, event: Key) -> bool:
        """
        面板挂起时，把 `1`–`9` 当成「选中第 N 项」（tui-display 扩展 F23）。

        :param event: 按键事件
        :returns: 是否已消化本次按键。False 表示按原有路径继续处理

        ## 三条不生效的情形，每一条都不能少

        1. **没有面板挂起** —— 数字照常落进输入框（AC18c）。这是最要紧的一条：
           拦错了的话用户再也打不出带数字的消息；
        2. **可见的面板不是那三个之一** —— 命令补全面板从不取得焦点，
           它的候选也不该被数字键选中；
        3. **序号越界** —— 面板只有四项时按 `7` 什么都不该发生，
           尤其不能环绕到第 1 项（那会让人误选）。

        ⚠ **不新增结算路径**：命中后把高亮移过去，再调 OptionList 原生的
        `action_select()`，走回车那条既有路径。另造一条的话，「按 2」与
        「移过去按回车」会慢慢分叉，而分叉出来的那条没有护栏。

        副作用：命中时移动面板高亮并触发一次选择结算。
        """
        if not (len(event.key) == 1 and event.key.isdigit() and event.key != "0"):
            return False
        panel = self._active_choice_panel()
        if panel is None:
            return False
        index = panel.choice_index(int(event.key))
        if index is None:
            return False
        event.stop()
        # 走面板自己的覆写点（ask-user 扩展 F15）：缺省实现就是
        # 「移过去 + action_select()」，与改造前逐字相同；澄清面板在**多选**下
        # 覆写成「切换勾选」——切换不是结算，因此上面那条
        # 「不新增结算路径」的约束仍然成立。
        panel.activate_choice(index)
        return True

    def _active_choice_panel(self):
        """
        当前挂着的可选面板（确认 / 澄清 / 会话），没有则 None。

        ⚠ 判据用 `display` 而不是「有没有待决交互」：会话面板走的是另一条路径
        （主线程发起、无 Worker 阻塞等待，见 `_settle_session`），
        用 `_pending_interaction` 判会把它整个漏掉。
        """
        for panel_type in (ConfirmPanel, ClarifyPanel, SessionPanel):
            panel = self.query_one(panel_type)
            if panel.display:
                return panel
        return None

    def on_key(self, event: Key) -> None:
        """
        处理特殊按键：面板数字键直选、运行中取消、命令面板导航。

        优先级：
        1. 有交互待决（确认/澄清/审批）或会话选择面板展示中 → 交给被聚焦的面板自身的
           Esc 绑定与 OptionList 原生导航处理，这里不拦截。
        2. 流式运行中 → Esc 触发取消当前 Agent 循环（spec F9）。
        3. 命令面板可见 → Up/Down 移动高亮、Esc 隐藏（焦点始终保持在 InputBar）。
        """
        # -1. 自由输入态的 Esc：**退回选项列表，而不是取消整次提问**
        #     （ask-user 扩展 F16）。
        #
        # ⚠ **必须排在最前面**，两条理由都不能少：
        # ① 排在数字键之后不行——那时用户正在打字，数字必须落进输入框；
        #    （目前提示态里一个可选项都没有，数字键天然不会命中，但那是
        #    「刚好如此」，把顺序钉死才是结构上的保证）
        # ② 排在下面那条「有待决交互就 return」之后更不行——自由输入态下
        #    焦点在**输入框**上，`ClarifyPanel` 自己的 Esc 绑定根本收不到，
        #    于是 Esc 会变成一个什么都不做的键。
        if self._clarify_free_text and event.key == "escape":
            event.stop()
            self._leave_clarify_free_text()
            return

        # 0. 数字键直选（tui-display 扩展 F23）。
        #
        # ⚠ **必须排在下面那条「交互待决 → return」守卫之前**，否则永远走不到
        # ——面板挂起正是它唯一该生效的时候。
        #
        # 它**不新增任何结算路径**：把高亮移过去，再调 OptionList 原生的
        # `action_select()`，走的仍是回车那条既有的
        # `on_option_list_option_selected`。另造一条结算路径的话，
        # 「按 2」与「移过去按回车」会慢慢分叉，而分叉出来的那条没有护栏。
        #
        # 无面板时不拦截（AC18c）：数字照常落进输入框。
        if self._handle_digit_choice(event):
            return

        # 1. 交互待决 / 会话选择面板展示中：让面板自己处理（它们各有 escape 绑定，
        #    上下键与回车由获得焦点的 OptionList 原生消化），不在此拦截
        if self._pending_interaction is not None or self._session_panel_active:
            return

        # 2. 运行中按 Esc 取消循环
        #
        # c13 注记：这里曾有一个 `Ctrl+B`「把前台等待中的子 Agent 切到后台」。
        # 随「发起与等待分离」的改造一并删除——前台阻塞等待已经不存在了，
        # 而且那个语义空间本来就被占满了：「这次要不要这个结果」由**模型**用
        # `background` 参数表达，「不干了」用 Esc，「掐掉某个子 Agent」用
        # `/agents cancel <标识>`。用户中途推翻模型的声明、逼它拿不完整的信息
        # 回答，既罕用产出又差。
        #
        # 更根本的一条：**等待不是卡顿，是进度**——跑子 Agent 就是在执行任务，
        # 与主 Agent 自己跑一遍测试套件性质相同，没人会为后者设计「别等了」的键。
        if self._stream_active:
            if event.key == "escape":
                event.stop()
                # 返回值是「这次没被停下来的」子 Agent 条数。
                #
                # `Esc` 的语义是「我不等了」而不是「全停」（C13 契约：委派永不阻塞），
                # 子 Agent 线程会照常跑到底——真实验收里它在 Esc 之后又跑了 7 轮、
                # 写文件、提交、留下一个工作区。语义保持不变，但**必须把话说清**，
                # 否则用户以为已经停了，而后台还在烧 token、还在往项目里写。
                remaining = self._manager.request_cancel()
                if remaining:
                    # 警告级：后台还在烧 token、非隔离的那些还在往主项目根写。
                    # 这是四级里最需要脱离颜色也认得出的一条，故走文字前缀通道。
                    # 句中的 `⚠` 去掉——widget 已在行首加「警告：」（F21/F28）。
                    self.show_warning(
                        f"已请求取消当前回合。仍有 {remaining} 个子 Agent 在后台运行"
                        f"——Esc 只停主对话，不会停它们。"
                        f"要一并停止请用 /agents cancel all。"
                    )
            return

        # 3. 命令面板导航
        panel = self.query_one(CommandPanel)
        if not panel.display:
            return
        if event.key == "up":
            event.stop()
            panel.action_cursor_up()
        elif event.key == "down":
            event.stop()
            panel.action_cursor_down()
        elif event.key == "escape":
            event.stop()
            panel.hide()

    def on_input_bar_input_submitted(self, event: InputBar.InputSubmitted) -> None:
        """
        处理用户提交输入（c10：唯一入口是 CommandDispatcher）。

        步骤：
        1. 保留交互待决、流式运行、会话面板展示时的提交守卫；
        2. 若命令面板有高亮候选，用候选文本替换待提交文本（Enter 执行当前高亮项，
           即使用户只输入了部分前缀，spec F23）；
        3. 隐藏命令面板后只调用 dispatcher.dispatch(text, self)——回显、命令执行、
           未知命令提示与错误边界全部由分发器统一负责，App 不再区分具体命令名，
           也不再维护状态刷新白名单。
        """
        # ── 自由输入态：这一条提交**就是答案**（ask-user 扩展 F16/F18）──
        #
        # ⚠ **必须排在下面那条「有待决交互就拦下」之前**：自由输入态本来就
        # 处在一次待决交互当中，走到那条守卫会被回一句「请先在面板上做出选择」
        # ——而用户此刻正在做的恰恰就是那件事。
        #
        # ⚠ 这段文本**不是一条新消息**：不进对话历史、不触发消息类 Hook、
        # 不开新回合、也不做斜杠命令解析（用户答案里的 `/` 是答案的一部分）。
        # 因此这里直接结算并返回，压根不往下走到 `parse_input` / `dispatch`。
        if self._clarify_free_text and self._pending_interaction is not None:
            text = (event.text or "").strip()
            if not text:
                # 空回车不结算，停在自由输入态——与主输入框「空提交零副作用」同口径。
                # 结算成空串的话，模型会拿到一个「用户输入了空」的答案，
                # 那比什么都不做更糟。
                return
            # ⚠ **多选题里打完字不结算**（F15 二次修订）。
            #
            # 「其它…」在多选题里是**第 N 个勾选项**，勾上它的方式恰好是打一段字。
            # 因此打完回车 = 那一项勾上了，回到勾选界面接着挑，最后仍在「提交」
            # 行交卷——与其余勾选项走同一个出口。
            #
            # 原写法在这里直接结算，于是用户一打完字整道题就交了：他想「先补一条
            # 自己的，再回去把剩下几项勾上」这个再自然不过的意图**做不到**，
            # 而且交出去之后才发现。
            #
            # 单选题不受影响：那里没有「提交」行，打完就是答完。
            question = self._clarify_question
            if question is not None and question.multi_select:
                panel = self.query_one(ClarifyPanel)
                panel.set_custom_text(text)
                self._leave_clarify_free_text()
                panel.move_to_submit()
                return

            self._resolve_interaction(ClarifyReply(kind="free_text", text=text))
            return

        # 交互进行中 / 流式运行中 / 会话选择面板展示中：拦下提交。
        #
        # c11 起前两种情形给出**可见提示**而不是静默 return——静默会让用户
        # 以为界面卡死了（尤其在确认面板期间，输入框并没有被禁用）。
        if self._pending_interaction is not None:
            # 本分支**可达且最常见**：只有澄清面板会禁用 InputBar，
            # 确认面板与计划审批面板期间用户点回输入框敲回车就会命中这里。
            self._show_busy_hint("正在等待你的确认，请先在面板上做出选择。")
            return
        if self._stream_active:
            self._show_busy_hint("正在运行中，可按 Esc 取消后再执行命令。")
            return
        if self._session_panel_active:
            # 本分支实际不可达：`_show_session_panel` 已经把 InputBar 设为
            # disabled，提交事件根本发不出来。保留裸 return 作为一致性兜底。
            return
        panel = self.query_one(CommandPanel)

        if panel.display and panel.highlighted is not None:
            text = panel.get_option_at_index(panel.highlighted).id
        else:
            text = event.text

        panel.hide()
        # c12 `user_message`：在**命令分发之前**分发。
        #
        # 它与 `turn_start` 不是重复事件而是来源不同：`/help` 这类命令触发
        # 前者而不触发后者（命令不进 AI），模型自行发起的 fork 子对话则相反。
        # 空输入不算（与 trace 的 `user_input` 同口径：空提交是零副作用的）。
        parsed = parse_input(text)
        # c15 F20：用户回来了 → 自动唤起的连锁计数清零，
        # 「已达上限」的提示标志也一并复位，下次达到上限时会重新提示一遍。
        #
        # ⚠ 放在**空输入判断之外**：用户敲一个回车也是「人在场」的证据，
        # 而计数复位是零副作用的。放进 if 里会让「回车 → 发现没恢复」
        # 成为一种谁都想不到的现象。
        self._manager.team_reset_auto_wake()
        self._auto_wake_limit_notified = False
        if parsed.kind != InputKind.EMPTY:
            self._dispatch_hook(
                HookEventType.USER_MESSAGE,
                text=parsed.raw_text.strip(),
                is_command=(parsed.kind == InputKind.SLASH),
            )
        self._dispatcher.dispatch(text, self)

    def _dispatch_hook(self, event: "HookEventType", **fields) -> None:
        """
        分发一个界面侧的 Hook 事件（c12）。

        :param event: 事件类型
        :param fields: 事件专有字段

        无人监听时不构造负载（spec N7）；异常一律吞掉——本方法有一个调用点在
        `_do_stream` 的收尾路径上，抛出会把一次正常结束变成崩溃。

        副作用：可能起子进程 / 发 HTTP 请求。
        """
        hooks = getattr(self._manager, "_hooks", None)
        if hooks is None or not hooks.has_listeners(event):
            return
        try:
            hooks.dispatch(event, lambda: dict(fields))
        except Exception:
            pass

    def _start_stream_worker(self, gen) -> None:
        """
        在本帧渲染完成后，启动后台线程 Worker 消费 Agent 事件流。

        由 _consume_manager_result 通过 call_after_refresh 调度，运行在主线程消息循环中，
        因此可安全调用 run_worker。exclusive=True 保证同一时间只有一个流式 Worker。

        :param gen: Manager 领域方法返回的 AgentEvent 生成器
        """
        self.run_worker(
            lambda: self._do_stream(gen),
            thread=True,
            exclusive=True,
        )

    def _set_streaming(self, active: bool) -> None:
        """
        切换运行忙碌状态。

        与 c3 不同：忙碌期间不禁用输入框（保持焦点，使运行中 Esc 取消可靠路由到 on_key），
        新一轮的并发提交由 on_input_bar_input_submitted 的 _stream_active 守卫拦截。

        ⚠ **回合状态行挂在这一处**（tui-activity-fold F14）：它是「一次运行的
        开始与结束」在本文件里唯一的判定点，异常路径也必经此处（`_do_stream`
        的 `finally` 里那次复位）。另立一处判定必然与它漂移，
        而漂移的表现是「跑完了状态行还赖着不走」或「跑着跑着它自己没了」。
        """
        self._stream_active = active
        if active:
            # 进入流式：复位提示标志，本轮可以再提示一次。
            self._busy_hint_shown = False
        status_line = self.query_one(StatusLine)
        if active:
            status_line.start()
        else:
            status_line.stop()
        if not active:
            self.query_one(InputBar).focus()

    # ------------------------------------------------------------------ #
    # /resume 会话选择面板（c9 交互化）
    # ------------------------------------------------------------------ #
    def _show_session_panel(self, request: SessionListRequest) -> None:
        """
        在主线程展示会话选择面板并移焦。

        与澄清面板同款处理：禁用输入框阻止用户点回输入框打字；焦点移到面板后，
        上下键/回车由 OptionList 原生消化，Esc 由面板自身绑定发 Cancelled。
        本交互由主线程发起（用户输入命令的直接结果），没有 Worker 在阻塞等待，
        因此**不需要** _interact 的 threading.Event 机制。

        :param request: Manager.resume(None) 返回的会话列表信号
        """
        self.query_one(CommandPanel).hide()
        self.query_one(InputBar).disabled = True
        self._session_panel_active = True
        panel = self.query_one(SessionPanel)
        panel.show_for(request.sessions, request.current_id)
        panel.focus()

    def _close_session_panel(self) -> None:
        """关闭会话选择面板：隐藏、恢复输入框可用并还焦。"""
        self._session_panel_active = False
        self.query_one(SessionPanel).hide()
        self.query_one(InputBar).disabled = False
        self.query_one(InputBar).focus()

    def _settle_session(self, session_id: Optional[str], source: str = "human") -> None:
        """
        **会话选择面板结算的唯一入口**（选中与取消两条路径都走它）。

        :param session_id: 选中的会话标识；`None` 表示取消（关闭面板不载入）
        :param source: 结算路径来源，取值集合见 `_resolve_interaction`

        执行步骤：埋一条 INTERACTION 事件 → 关闭面板 → 有 id 则载入该会话。

        ⚠️ **第一行的幂等守卫不可省**：没有面板挂着时静默返回。
        缺了它，驱动设施退出时的「强制结算」会在没有面板的情况下凭空多埋一条交互
        事件，破坏 trace「四类面板各产出恰好一条」的口径（trace AC16）。

        ⚠️ 会话选择面板走的不是 `_interact` 那条路（它由主线程发起，没有 Worker 在
        阻塞等待），所以埋点只能在结算处做，而**两条结算路径必须合并到这里**——
        分散在两处时，将来任何第三条路径都会漏掉埋点与守卫。

        副作用：产出记录事件、隐藏面板并还焦输入框、可能触发一次会话载入
        （载入会走后台 Worker，因为它可能触发阻塞的 C8 摘要调用）。
        """
        if not self._session_panel_active:
            return
        self._recorder.emit(
            TraceEventType.INTERACTION,
            kind="session",
            display=str(session_id) if session_id is not None else "",
            source=source,
            result="selected" if session_id is not None else "cancelled",
        )
        self._close_session_panel()
        if session_id is not None:
            # 直接复用恢复控制器（c10 T48）：与 /resume <id> 命令同一领域入口，
            # 不再伪造用户没有手输的 "/resume <session_id>" 文本（也不回显它）。
            # option.id 携带完整 session_id，_resolve_key 按精确 ID 匹配必中。
            self.resume_session(session_id)

    def on_session_panel_cancelled(self, event: SessionPanel.Cancelled) -> None:
        """会话选择面板按 Esc 退出：关闭面板，不做任何载入，界面原样保留。"""
        self._settle_session(None)

    # ------------------------------------------------------------------ #
    # Agent 事件流消费
    # ------------------------------------------------------------------ #
    def _do_stream(self, gen) -> None:
        """
        在 Worker 线程中消费 AgentEvent 生成器，把每个事件渲染到 UI。

        本方法运行在独立线程，所有 UI 操作通过 call_from_thread() 调度到主线程。

        渲染策略：
        - PROGRESS：进入新一轮——重置正文/思考占位组件，使新一轮文本另起新块
        - THINKING / TEXT：增量更新对应占位组件（思考灰色斜体、正文 Markdown）
        - TOOL_PENDING：模型刚开始生成该调用的参数（可能持续几十秒）——立刻建一行
          橘色「参数生成中… Ns」，这是那段时间里界面上唯一的活体信号
        - TOOL_START：进入执行态。若 TOOL_PENDING 已建过行则**原地复用**（补参数摘要、
          重新起算耗时），否则新建；同时重置正文/思考占位（工具后的文本另起块）
        - TOOL_RESULT：工具行定色（绿/红）+ 摘要；同时把它从待定表里摘掉
        - 收尾：待定表里剩下的（取消/流出错导致没等到结果）统一标记为「未执行」，
          不留永远转圈的橘色行
        - FINISHED：按结束原因追加系统行（自然完成不打扰）
        - ERROR：红色错误行
        - HISTORY：会话恢复成功（c9）——清空聊天区并整体回放携带的历史快照

        :param gen: ConversationManager._run() 返回的 AgentEvent 生成器
        """
        history_view = self.query_one(HistoryView)

        thinking_widget: Static | None = None
        thinking_chunks: list[str] = []
        response_widget: Static | None = None
        response_chunks: list[str] = []
        tool_widgets: dict = {}

        def reset_text_widgets() -> None:
            """重置正文/思考占位，使后续文本另起新组件（轮次切换或工具执行后调用）。"""
            nonlocal thinking_widget, thinking_chunks, response_widget, response_chunks
            # 界面消息埋点（trace F15）：本轮 AI 正文在这里收尾——重置占位之前
            # 把已累积的内容记一条。记的是**已在界面上呈现的完整一段**，
            # 而不是逐块增量（那会产出几百条碎片事件）。
            if response_chunks:
                text = "".join(response_chunks)
                self._recorder.emit_lazy(
                    TraceEventType.UI_MESSAGE,
                    lambda text=text: {"source": "assistant", "text": full_text(text)},
                )
                # c12 `assistant_message`：与 `ui_message` 埋点**同位置、同产出条件**。
                # 搭它的车是刻意的——「一段 AI 正文产出完毕」这件事在本文件里只有
                # 这一个判定点，另立一处判定必然与它漂移。
                self._dispatch_hook(
                    HookEventType.ASSISTANT_MESSAGE,
                    scope=self._recorder.current_scope(),
                    text=text,
                )
            thinking_widget = None
            thinking_chunks = []
            response_widget = None
            response_chunks = []

        try:
            for event in gen:
                etype = event.type
                # 循环事件埋点（trace F15 + F17 字段白名单）。
                #
                # ⚠️ **TEXT / THINKING 两类刻意不记录**，这是 F17「同一份数据不重复
                # 携带」裁决的必然延伸。它们是**逐块**产出的流式增量：一次几百字的
                # 回答会切成好几百个块、每块一条事件，而按字段白名单剥掉正文之后，
                # 每条剩下的全部信息只有 `text_length: 2`——信息量为零，却把整条
                # 时间线淹掉。
                #
                # 实测（手测场景 3）：2096 条记录里 1944 条是这种噪音（93%），
                # 一次 `api_request` 与它的 `api_response` 之间夹着 322 条，人没法读。
                #
                # 丢掉的那点信息由更有用的聚合形态承载：完整正文与思考在
                # `api_response`（各一条）、界面上呈现的完整段落在 `ui_message`、
                # 块数与首块延迟也在 `api_response`（见 tracing_provider）。
                #
                # ⚠️ 必须用**默认参数绑定** `e=event`：循环内直接写
                # `lambda: agent_event_payload(event)` 捕获的是变量而不是当轮的值，
                # 全部闭包最终都指向最后一个事件（Python 闭包按引用捕获）。
                if etype not in (AgentEventType.TEXT, AgentEventType.THINKING):
                    self._recorder.emit_lazy(
                        TraceEventType.AGENT_EVENT,
                        lambda e=event: agent_event_payload(e),
                    )

                if etype == AgentEventType.PROGRESS:
                    # ⚠ `reset_text_widgets()` **必须保留**：它负责让新一轮的正文
                    # 另起一块。删掉会让相邻两轮的正文粘在一起，看起来像一段话。
                    #
                    # 这里原本还追加一行「🔄 第 N 轮」（tui-display 扩展 F42 已删除）。
                    # 那是 Agent Loop 的**内部结构**，对用户没有任何可操作信息，
                    # 而一次十几轮的运行会因此多出十几行，把真正有内容的工具行挤下去。
                    # Claude Code 没有对应物。「循环仍在推进」这件事由工具行本身
                    # 与子 Agent 活动区表达，都比一个轮次序号具体。
                    #
                    # 删之前查过 trace 与 e2e 判据有无依赖它产出的那条 `ui_message`
                    # （task.md 的 T1）：结论是**无依赖**——全部「第 N 轮」字样要么是
                    # 注释，要么是测试自造的剧本文本或 JSONL 夹具。
                    reset_text_widgets()
                    # 每轮迭代刷一次状态栏，让**上下文用量在回合进行中就跟着涨**。
                    #
                    # 改这里之前，本方法整个运行期只在 `finally` 刷一次，于是那一段
                    # 用量数字是**冻结**的。auto 模式下一个回合往往几十秒、看不太出来；
                    # Plan Mode 下整段规划（多轮 ask_user 面板，用户可能停留好几分钟）
                    # 是**同一次运行**，状态栏就一直停在进入规划那一刻的值——而规划
                    # 期间每一轮问答都在实实在在地吃上下文。用户实测记录：规划历时
                    # 92 秒、5 轮请求，状态栏全程 `0/976.6K`，直到计划获批才跳到 8.7K。
                    # 那次跳变也不是「切回 auto 顺带更新了用量」，而是审批回调触发的
                    # `_notify_preset_change()` 顺手刷了一次状态栏——**用量本身从来
                    # 没有自己的刷新点**，这才是根因。
                    #
                    # 为什么挂在 PROGRESS 上：它在每轮迭代开头产出，此时上一轮的
                    # assistant 消息与全部工具结果都已追加进 history，估算读到的正是
                    # 「刚刚长了多少」。挂在 USAGE 上也能更新，但那只覆盖「模型答完」
                    # 这一种增长，覆盖不到工具结果——而后者才是大头。
                    #
                    # 线程安全：`context_status_line()` 要遍历 history 做增量估算，
                    # 而 history 由 Agent Loop 在**本线程**（生成器就在这个 Worker 里
                    # 求值）追加。`call_from_thread` 是**阻塞**调用，主线程估算期间
                    # 本线程停在这一句上，因此不存在「一边遍历一边追加」。
                    #
                    # 成本：一次锚点+增量估算（只算锚点之后新增的那几条），
                    # 每轮一次可忽略；且它本来就是 `finally` 里已经在做的同一件事。
                    self.call_from_thread(self._refresh_status)

                elif etype == AgentEventType.THINKING:
                    if thinking_widget is None:
                        thinking_widget = self.call_from_thread(history_view.begin_thinking_turn)
                    thinking_chunks.append(event.text)
                    self.call_from_thread(
                        history_view.update_widget,
                        thinking_widget,
                        f"[dim italic]{THINKING_MARK} {escape(''.join(thinking_chunks))}[/dim italic]",
                    )

                elif etype == AgentEventType.TEXT:
                    if response_widget is None:
                        response_widget = self.call_from_thread(history_view.begin_assistant_turn)
                    response_chunks.append(event.text)
                    self.call_from_thread(
                        history_view.update_ai_widget,
                        response_widget,
                        ''.join(response_chunks),
                    )

                elif etype == AgentEventType.TOOL_PENDING:
                    # 模型刚开始吐这个调用，参数还在流里（可能要几十秒）。
                    # 先建一行「参数生成中… Ns」，让界面立刻有活体信号；这一行随后
                    # 由 TOOL_START 原地转成执行态，**不会**再多建一行。
                    #
                    # ⚠ 静默工具三个分支都要跳过（见 `_silent_tools`）。
                    # 只跳过其中一两个会留下「建了行却永远收不了尾」的半成品，
                    # 而 `finally` 的收尾会把它涂成「失败 · 未执行」。
                    if event.tool_call.name in self._silent_tools:
                        reset_text_widgets()
                        continue
                    reset_text_widgets()
                    tc = event.tool_call
                    if tc.id not in tool_widgets:
                        tool_widgets[tc.id] = self.call_from_thread(
                            history_view.add_tool_widget, tc, True
                        )

                elif (
                    etype == AgentEventType.TOOL_START
                    and event.tool_call.name in self._silent_tools
                ):
                    # 静默工具：不建行、不复用行。正文照常收尾（否则下一段
                    # 正文会与上一段粘在一起）。
                    reset_text_widgets()

                elif etype == AgentEventType.TOOL_START:
                    # 工具开始：重置文本占位（工具后的文本另起块）。
                    # 若 TOOL_PENDING 已经建过行，复用它并补上参数摘要；否则新建
                    # （非 DeepSeek Provider、脚本化 Provider 都不产 TOOL_PENDING）。
                    reset_text_widgets()
                    tc = event.tool_call
                    widget = tool_widgets.get(tc.id)
                    if widget is None:
                        tool_widgets[tc.id] = self.call_from_thread(
                            history_view.add_tool_widget, tc
                        )
                    else:
                        self.call_from_thread(widget.begin_running, tc)

                elif (
                    etype == AgentEventType.TOOL_RESULT
                    and event.tool_call.name in self._silent_tools
                ):
                    # 静默工具：不定色、不留行。**但界面刷新照做**——
                    # 待办块正是靠这里更新的，跳过它整块就永远不动了。
                    if self._manager.todo_version() != self._todo_version:
                        self.call_from_thread(self._refresh_todo)

                elif etype == AgentEventType.TOOL_RESULT:
                    tc = event.tool_call
                    res = event.tool_result
                    # pop 而不是 get：留在字典里的都是「还没定色」的行，
                    # finally 里据此把它们收尾（见下方 _settle_unfinished_tools）。
                    widget = tool_widgets.pop(tc.id, None)
                    if widget is None:
                        widget = self.call_from_thread(history_view.add_tool_widget, tc)
                    elif widget.pending:
                        # 有结果却从没进过执行态：权限拒绝 / 用户拒绝 / 规划阶段拦下
                        # 这些路径**只产 TOOL_RESULT、不产 TOOL_START**。此时参数已经
                        # 完整（就在 event.tool_call 里），补上再定色——否则标题只剩
                        # 工具名，用户看不出被拒的到底是哪一次写入。
                        self.call_from_thread(widget.begin_running, tc)
                    # 改文件类工具会在 res.diff 带上结构化差异，传给工具行渲染彩色 diff 块
                    self.call_from_thread(
                        widget.finish,
                        res.ok,
                        self._result_summary(res),
                        getattr(res, "diff", None),
                        self._result_detail(res),
                    )
                    # todo-list 扩展：待办清单变了就重绘那一块。
                    #
                    # ⚠ **先在工作线程读一个整数，不同才跨线程。** 版本号没变时
                    # 一次 `call_from_thread` 都不发起——待办块的刷新因此不给
                    # 每一次工具调用增加任何跨线程往返。
                    #
                    # ⚠ **刻意不按工具名判断**（不写 `if tc.name == "todo_write"`）：
                    # 界面层不该认识任何具体工具的名字，而版本号这个判据对将来
                    # 任何写路径都成立。
                    if self._manager.todo_version() != self._todo_version:
                        self.call_from_thread(self._refresh_todo)

                elif etype == AgentEventType.USAGE:
                    # 本轮 token 用量送进状态行（tui-activity-fold F15/F17）。
                    #
                    # ⚠ **这个事件每轮只在流末尾到达一次**（Provider 协议限制：
                    # OpenAI 兼容协议的 `include_usage` 在流的最后额外发一块
                    # usage）。因此状态行上的 token 是**跳变式**更新，
                    # 而不是像耗时那样持续滚动——这是**已知且如实记录**的行为，
                    # 验收时别误判成「数字不动 = 坏了」（AC16）。
                    #
                    # ⚠ **这里传的是输入与输出两个分量，不是 `total_tokens`。**
                    # 「哪个累加、哪个覆写」的判断收在 `StatusLine.set_usage` 里
                    # （输入重发所以取最近一轮、输出新增所以累加），本处只负责
                    # 如实转交——在这里先算个和再传过去，等于把那条口径拆成两半。
                    prompt = getattr(event.usage, "prompt_tokens", 0) or 0
                    completion = getattr(event.usage, "completion_tokens", 0) or 0
                    if prompt or completion:
                        self.call_from_thread(
                            self.query_one(StatusLine).set_usage, prompt, completion
                        )

                elif etype == AgentEventType.FINISHED:
                    level, line = self._finish_line(event.stop_reason, event.message)
                    if line:
                        self._trace_ui_message("system", line)
                        self.call_from_thread(
                            self._history_channel(history_view, level), line
                        )

                elif etype == AgentEventType.ERROR:
                    self._trace_ui_message("error", event.message)
                    self.call_from_thread(history_view.append_error, event.message)

                elif etype == AgentEventType.NOTICE:
                    # 系统级提示，按**事件自带的档位**分发（tui-display 扩展 F19/F22）。
                    #
                    # 档位由产出方声明（见 `AgentEvent.level`）：上下文压缩这类
                    # 走提示级，子 Agent 结论送达这类走事件级。界面无法从文本
                    # 本身判断哪条要紧——两者都只是一句陈述句。
                    self._trace_ui_message("system", event.message)
                    self.call_from_thread(
                        self._history_channel(history_view, event.level), event.message
                    )

                elif etype == AgentEventType.HISTORY:
                    # 会话恢复成功（c9 /resume 交互化）：清屏并整体回放历史快照。
                    # 只发起一次 call_from_thread——清空与重画在主线程一次调用内原子完成；
                    # 同时重置本 Worker 的文本/工具占位引用（旧引用指向已被移除的组件）。
                    reset_text_widgets()
                    tool_widgets.clear()
                    self.call_from_thread(history_view.render_history, event.messages)
                    # 活动区同样属于「上一段对话的状态」（tui-display 扩展 F8）。
                    # `/resume` 与 `/clear` 是同一类切换：`cancel_all_for_session_switch`
                    # 会取消在跑的子 Agent 并开新会话代，那些行不该跨到新对话里。
                    self.call_from_thread(
                        self.query_one(ActivityView).update_rows, ()
                    )
                    # 展开档位同样属于「上一段对话的状态」（tui-activity-fold
                    # F20）。与 `/clear` 共用同一处复位——各写一遍必然出现
                    # 「清空能复位、恢复不能」这种一半对的状态。
                    self.call_from_thread(self._reset_display_state)
        finally:
            # **作用域泄漏的唯一可靠防护**，必须是 finally 的第一行（trace T42）。
            #
            # 为什么必需：Textual 的 thread worker 用**默认线程池**
            # （`Worker._run_threaded` 末行是 `run_in_executor(None, ...)`），
            # 线程会被复用。而本方法的循环体几乎全是 `call_from_thread`，
            # 应用退出竞态下它会抛 `RuntimeError`（`_notify_memory` 与
            # `_notify_skill_activation` 两处既有代码为此包了 try/except，
            # 说明这不是理论风险）；此时生成器被放弃，
            # `_run_isolated_skill` 里 `with recorder.scope(...)` 的 `__exit__`
            # 可能压根不跑。一次泄漏的 `isolated:<name>` 会污染后续复用该线程的
            # 主对话运行——概率性、极难复现。
            #
            # 为什么放在**首行**：它后面的 `call_from_thread` 自己也可能抛，
            # 放在后面就等于「出错时不复位」，防护形同虚设。
            self._recorder.bind_scope(SCOPE_MAIN)
            # **本轮最后一段 AI 正文在这里收尾**（trace F15 的补齐）。
            #
            # 为什么必须补这一次：`reset_text_widgets` 原本只在
            # 「下一轮开始 / 工具开始 / 历史回放」三个时机被调用，
            # 也就是说它**总是靠下一个动作来给上一段正文收尾**。
            # 于是一轮运行里的**最后**一段正文永远等不到那个动作，
            # 一条 `ui_message` 都不会产出——而那恰恰是用户最终看到的结论。
            #
            # 实测（P1a 端到端驱动）：跑完两轮对话，记录里 `ui_message` 只有两条
            # `user_echo`，两段 AI 正文一条都没有。后果是断言词汇「界面消息含某文本」
            # 在「最后一句话」上完全不可用，而这个缺口在界面上看不出来
            # （界面显示得好好的，只是没被记下来）。
            #
            # 位置有两条约束：
            # ① 必须在 `bind_scope(SCOPE_MAIN)` **之后**——独立模式子对话结束时
            #    线程作用域可能还是 `isolated:<name>`，而这段正文是呈现在主界面上的，
            #    该记成 `main`；
            # ② 必须在下面两个 `call_from_thread` **之前**——它们在应用退出竞态下
            #    会抛 `RuntimeError`（见上方注释），放在后面等于「出错时不记录」。
            #
            # 本调用只做内存写与一次 `emit`，不碰界面，故在 finally 里是安全的。
            reset_text_widgets()
            # c12 `notification`：一次 Agent 运行结束。
            #
            # 位置与上面那次 `reset_text_widgets()` 同理——必须在两个
            # `call_from_thread` **之前**：它们在应用退出竞态下会抛 RuntimeError，
            # 放后面等于「出错时不通知」，而「跑完了叫我一声」正是出错时也想要的。
            self._dispatch_hook(
                HookEventType.NOTIFICATION,
                kind="agent_finished",
                message="本次运行已结束。",
            )
            self.call_from_thread(self._set_streaming, False)
            # **没等到结果的工具行必须在这里收尾**，否则留在界面上一直橘着、
            # 计时器每秒还在跳，看起来程序卡在某个工具上了。
            #
            # 什么时候会有这种行：`TOOL_PENDING` 一旦播报就建了行，而它之后的
            # `TOOL_RESULT` 并不保证到达——用户按 Esc 取消、底层流出错、或本轮
            # 因取消而 break 掉剩下的调用，这几条路径都会让后面的调用一个事件都不再产。
            #
            # 放在 `_set_streaming(False)` **之后**：那是必须生效的状态复位
            # （否则输入框一直处于忙碌态），而本清理只是视觉收尾，退出竞态下
            # `call_from_thread` 抛异常时宁可丢清理也不能丢复位。
            self._settle_unfinished_tools(tool_widgets)
            # 工具调用可能在本轮流式执行中通过 mcp_add_server 改变 MCP 连接状态；
            # 收尾时刷新状态栏，让新工具数量或失败信息立即反映到界面上。
            self.call_from_thread(self._refresh_status)

    def _settle_unfinished_tools(self, tool_widgets: dict) -> None:
        """
        把本轮结束时仍未定色的工具行统一收尾为灰白的「未执行」。

        `tool_widgets` 里只会剩「建了行但没等到 TOOL_RESULT」的调用——正常拿到结果的
        在 TOOL_RESULT 分支就被 pop 掉了。因此这里的每一项都对应一次**真的没有跑**
        的调用（取消 / 流出错 / 本轮提前 break）。

        :param tool_widgets: 调用 id → ToolCallWidget，处理后被清空

        副作用：更新界面组件、清空传入的字典。异常一律吞掉——本方法在 `finally` 里
        被调用，而它只是视觉收尾，不能反过来把一次正常结束变成异常退出。
        """
        for widget in list(tool_widgets.values()):
            try:
                self.call_from_thread(widget.finish, False, "未执行（本轮已结束）")
            except Exception:
                # 应用退出竞态下 call_from_thread 会抛 RuntimeError；此时界面正在
                # 拆除，收不收尾都无意义，继续处理剩下的即可。
                pass
        tool_widgets.clear()

    @staticmethod
    def _finish_line(stop_reason, message: str) -> "tuple[str, str]":
        """
        把循环结束原因转成一行系统提示（自然完成返回空串，不打扰用户）。

        :param stop_reason: StopReason
        :param message: 循环附带的补充说明（如有则优先使用）
        :returns: `(级别, 文本)`；文本为空串表示不展示

        ## 为什么级别在这里定，而不是让调用方猜（tui-display 扩展 F20）

        六种结束原因分成两档，判据是「用户看到之后要不要做点什么」：

        - **事件级**——「已取消」「计划未执行」是**用户自己刚做的决定**的回执，
          他知道发生了什么，不需要被醒目提示；
        - **警告级**——迭代上限、未知工具、流错误都是**任务没做完就停了**，
          用户多半要重试或改写请求。漏看这三条会让人以为任务成功了。

        改造前六种全走同一条 `[dim]` 通道，最要紧的三条与最平常的三条长得
        一模一样。emoji（`⏹` `⚠`）一并去掉：警告级由 widget 统一加「警告：」
        文字前缀（F21/F28），留着会变成「警告：⚠ …」。
        """
        if stop_reason == StopReason.COMPLETED:
            return LEVEL_NOTICE, ""
        if stop_reason == StopReason.USER_CANCELLED:
            return LEVEL_EVENT, "已取消"
        if stop_reason == StopReason.PLAN_REJECTED:
            return LEVEL_EVENT, "计划未执行"
        if stop_reason == StopReason.MAX_ITERATIONS:
            return LEVEL_WARNING, message or "已达迭代上限，自动停止"
        if stop_reason == StopReason.UNKNOWN_TOOL:
            return LEVEL_WARNING, message or "连续调用未知工具，已停止"
        if stop_reason == StopReason.STREAM_ERROR:
            return LEVEL_WARNING, "因流错误已停止"
        return LEVEL_NOTICE, ""

    @staticmethod
    def _result_summary(res) -> str:
        """
        取工具结果的**规模描述**，供折叠档与逐条档展示。

        优先使用工具自带的 `summary`（那是工具作者亲手写的一句话概括，
        比机器截出来的首行准确得多）；没有时回退到 `output` **全文**。

        ## 为什么不再截断（tui-display 扩展 F41）

        改造前这里取首个非空行、截到 80 字符。于是一次 `grep` 命中 23 处，
        用户只看得到第一处，**而且没有任何迹象表明还有别的**——既不知道被省了
        什么，也没法展开。

        现在把「省略」整个交给展示层：`ToolCallWidget` 收全文、按
        `BRANCH_LINE_LIMIT` 折叠、并在末行如实写出「… +N 行」。
        职责因此清楚了一层——**这里负责取内容，那里负责决定画多少**。

        ⚠ 不截断**不等于**无界（N5）：组件那边有行数上限，且 `output` 本身在
        工具侧已受各自的上限约束（如 `run_command` 的前 30 + 后 10 行）。

        :param res: tools.base.ToolResult
        :returns: 展示文本，可能是多行
        """
        if getattr(res, "summary", ""):
            return res.summary
        text = (res.output or "").strip()
        if not text:
            return "（无输出）" if res.ok else "（无错误信息）"
        return text

    @staticmethod
    def _result_detail(res) -> str:
        """
        取工具结果的**输出原文**，只供最详细一档展示（tui-activity-fold F12）。

        ## 为什么必须与 `_result_summary` 分成两个函数

        改造前只有一个取值函数，且**优先返回 `summary`**——于是展开到最详细
        一档时，用户看到的仍是那句「读取 1902 行 · 78.4 KB」。
        「展开」等于没展开，因为 `output` 原文压根没传到组件手里。

        ## ⚠ 取 `output` 而不是 `full_output`

        有些工具会主动裁剪输出（`run_command` 保留前 30 + 后 10 行，
        完整原文另存 `full_output`）。这里**刻意取裁剪后的那份**：
        展开成完整原文会让一次测试套件输出撑爆历史区，而那正是 spec F12
        明确否掉的。

        代价是「展开了也看不到全部」，因此**被裁剪时就地补一句说明**——
        判定与措辞都收在这一个函数里，组件侧不必多一个参数。
        多一个布尔参数就多一处「传了但没用」或「用了但没传」的可能。

        :param res: tools.base.ToolResult
        :returns: 输出原文；被裁剪过时末尾附一行说明。无输出时返回空串
                  （组件据此回退显示规模描述）
        """
        text = (res.output or "").strip()
        if not text:
            return ""
        # `full_output` 非空即说明工具主动裁剪过（见 `tools/base.py` 的成对维护点）
        if getattr(res, "full_output", ""):
            text += "\n（输出已由工具裁剪，完整原文见行为记录）"
        return text

    # ------------------------------------------------------------------ #
    # 三类用户交互回调（均在 Worker 线程被调用，阻塞等待主线程选择）
    # ------------------------------------------------------------------ #
    def _interact(
        self,
        kind: str,
        show_fn,
        default,
        display: str = "",
        keep_panel: bool = False,
        extra: "Optional[dict]" = None,
    ):
        """
        统一的「阻塞式询问主线程」机制（确认/澄清/审批共用）。

        在 Worker 线程：登记一个待决交互盒（含 Event 与默认结果），用 call_from_thread 在主线程
        弹出对应面板，然后阻塞等待，直到主线程的选择/取消处理写入结果并 set() 唤醒。
        主线程事件循环不被阻塞，UI（含其它工具行计时）照常刷新（N2 不死锁）。

        :param kind: 交互种类标识（"confirm"/"clarify"/"approve"），用于结算时映射结果
        :param show_fn: 在主线程展示面板的无参函数
        :param default: 未明确选择（如异常路径）时的默认结果
        :param display: 面板展示内容的摘要，仅用于 trace 埋点。**必须由调用方传入**——
                        展示内容全被闭进 `show_fn` 里，本方法拿不到（trace T45）
        :param keep_panel: 结算后**不收面板、不还焦输入框**（ask-user 扩展 F14）。
            一次 `ask_user` 可能带好几个问题，逐个弹的时候如果每答完一题都收一次面板，
            用户会看到焦点在输入框与面板之间来回跳。⚠ 它只在**真的拿到了作答**时
            才生效，见 `_resolve_interaction`
        :param extra: 并进行为记录负载的附加字段（ask-user 扩展 F22）。
            ⚠ **新增字段要同步 `trace/reader.py` 的摘要函数**（成对维护点）——
            漏改不报错，只是那些字段读时间线时看不见，等于白记
        :returns: 用户选择的结果
        """
        # `source` 记录**这次结算走的是哪条路径**，缺省 human（面板按键路径）。
        # 结算方（`_resolve_interaction`）可以覆写它，见该方法的说明。
        box = {
            "event": threading.Event(),
            "result": default,
            "kind": kind,
            "source": "human",
            "keep_panel": keep_panel,
        }

        def _arm() -> None:
            """
            **在主线程上一次做完三件事**：登记待决盒 → 改状态行 → 弹面板。

            ## ⚠ 为什么必须是一次，而且必须包含登记（真机 + 全量测试实测）

            改造前 `self._pending_interaction = box` 写在**工作线程**上、
            `call_from_thread(show_fn)` 之前。于是存在一个真实的窗口：
            **程序已经认为「正在等你应答」，而面板还没画出来。**
            那一瞬间用户（或端到端驱动）看到的是「三态是 pending，但屏幕上
            什么都没有」。

            tui-activity-fold 把这个窗口拉宽了——F18 要在弹面板前先把状态行
            切到「等待确认」，那是**第二次**跨线程往返。全量测试因此开始偶发红：
            `wait` 返回 pending 之后立刻取快照，`panel_visible` 是 `False`、
            `focused` 还是 `InputBar`。单跑必过、全量偶发，正是竞态的典型形态。

            合成一次之后窗口整个消失：登记与显示在同一个主线程回合内完成，
            外部**不可能**观察到「pending 但没有面板」这个中间态。
            顺带省掉一次跨线程往返，面板出得更快。

            ⚠ 别把登记挪回工作线程去「省事」——那正是窗口的来源。
            """
            self._pending_interaction = box
            # 新面板弹出：复位提示标志，这一次面板期间可以再提示一次。
            self._busy_hint_shown = False
            # 状态行切到等待语义并**撤下中断提示**（tui-activity-fold F18）：
            # 面板有自己的取消方式，两套提示同屏会误导用户去按 Esc。
            # ⚠ 耗时**继续累计**——那段等待确实在这次回合内，用户等了多久就是多久。
            # 三类交互（确认 / 澄清 / 审批）共用本入口，改这一处即可。
            self.query_one(StatusLine).set_phase("等待确认", False)
            show_fn()

        self.call_from_thread(_arm)
        # c12 `notification`：面板**已经弹出之后**才分发。
        #
        # 放在 `show_fn` 之后而不是之前，是因为 Hook 动作可能跑上几十秒；
        # 放前面会让「面板迟迟不出现」，而这个事件的用途恰恰是「我切走了，
        # 有事叫我」——面板出得越早越好。放在 `wait()` 之前也无害：
        # 用户在此期间的选择会被存进盒子并 set()，稍后 wait() 立即返回。
        self._dispatch_hook(
            HookEventType.NOTIFICATION,
            kind=_NOTIFY_KINDS.get(kind, kind),
            message=display or f"等待用户{kind}",
        )
        box["event"].wait()
        # 面板结算：状态行回到运行语义、恢复中断提示（F18）。
        # 放在 `wait()` 之后、埋点之前——此刻用户已经做完决定，Agent Loop
        # 即将继续跑，界面上就该重新显示「按 Esc 可以中断」。
        self.call_from_thread(self.query_one(StatusLine).set_phase, "处理中…", True)
        result = box["result"]
        # 交互埋点（trace F14）：埋在**阻塞等待返回之后**（即结算时刻），
        # 一次交互恰好一条。埋在弹出时会记不到 result，而「用户选了什么」
        # 正是这条事件的全部价值。
        # source 从盒子里读而不是写死 "human"：结算可能来自面板按键，也可能来自
        # 端到端驱动设施的控制通道，二者在记录里必须能分辨。
        source = box.get("source", "human")
        self._recorder.emit_lazy(
            TraceEventType.INTERACTION,
            lambda: {
                "kind": kind,
                "display": full_text(display),
                "source": source,
                "result": getattr(result, "value", result),
                **(extra or {}),
            },
        )
        return result

    def _confirm_tool(self, tool_call, tool, decision) -> ConfirmDecision:
        """
        人在回路确认（c6 spec F6），返回四态决定。

        :param tool_call: 待确认的工具调用
        :param tool: 工具实例
        :param decision: 决策管线给出的 DecisionResult，面板用其 reason 告知用户为何需要确认
        """
        return self._interact(
            "confirm",
            lambda: self._show_confirm_panel(tool_call, tool, decision),
            ConfirmDecision.DENY,
            display=f"{tool_call.name} {tool_call.arguments}｜{decision.reason}",
        )

    def _clarify(self, question, index: int = 0, total: int = 1):
        """
        澄清提问（c4 spec F12 / ask-user 扩展）：弹一次面板问**一个**问题。

        :param question: `ClarifyQuestion`
        :param index: 这是第几题（从 0 起）
        :param total: 本次一共几题
        :returns: `ClarifyReply`；用户跳过（按 Esc）返回 None

        ⚠ **一题一次调用**，串行由 Agent 循环侧驱动（plan.md D2）：
        本方法底下那条阻塞回调链一次只能等一个信号，而主线程不能阻塞——
        把串行搬到界面侧就得改跨线程机制，那是本扩展明令不动的东西。

        副作用：阻塞当前 Worker 线程直到用户作答；写 `_clarify_question`
        （驱动设施读它算结算值，见 `tests/e2e/control.py` 的 `settlement_for`）。
        """
        self._clarify_question = question
        self._clarify_free_text = False
        return self._interact(
            "clarify",
            lambda: self._show_clarify_panel(question, index, total),
            None,
            display=f"{question.question}｜候选：{[o.label for o in question.options]}",
            # 不是最后一题就别收面板（F14）——否则每答完一题焦点都要
            # 在输入框与面板之间跳一次。
            keep_panel=(index < total - 1),
            extra={
                "question_index": index,
                "question_total": total,
                "multi_select": question.multi_select,
            },
        )

    def _approve_plan(self, plan: str) -> bool:
        """Plan Mode 计划执行审批（spec F13），返回是否批准开始执行。"""
        return self._interact(
            "approve",
            lambda: self._show_approve_panel(plan),
            False,
            display=plan,
        )

    def _show_confirm_panel(self, tool_call, tool, decision) -> None:
        """在主线程展示工具确认面板并移焦（c6：传入 decision 以展示拒绝/询问原因）。"""
        self.query_one(CommandPanel).hide()
        panel = self.query_one(ConfirmPanel)
        panel.show_for(tool_call, tool, decision)
        panel.focus()

    def _show_clarify_panel(self, question, index: int = 0, total: int = 1) -> None:
        """
        在主线程展示澄清面板并移焦（选项列表态）。

        与确认/审批不同：选项列表态要求用户「只能在候选项间选择，不能输入文本」，
        因此这里禁用输入框（disabled=True），阻止用户点击输入框继续打字；
        结算交互时（`_resolve_interaction`）再恢复。其它交互（confirm/approve）不做此限制。

        ⚠ **自由输入态会把这个禁用解除**（`_enter_clarify_free_text`）——
        那时打字正是我们要的行为。两处成对，见 ask-user 扩展 F16。
        """
        self.query_one(CommandPanel).hide()
        self.query_one(InputBar).disabled = True
        panel = self.query_one(ClarifyPanel)
        panel.show_question(question, index, total)
        panel.focus()

    def _enter_clarify_free_text(self) -> None:
        """
        进入自由输入态：面板换成提示态，光标回到主输入框（ask-user 扩展 F16）。

        ⚠ **面板不收起**——「现在到底在干什么」必须一直看得见。收起来的话
        用户会以为提问已经结束，而澄清回调其实还阻塞在 Worker 线程上，
        那正是本项目反复吃亏的「静默中间态」。

        副作用：改面板内容、解禁并聚焦输入框、置 `_clarify_free_text`。
        """
        self._clarify_free_text = True
        self.query_one(ClarifyPanel).show_free_text()
        bar = self.query_one(InputBar)
        bar.disabled = False
        bar.focus()

    def _leave_clarify_free_text(self) -> None:
        """
        从自由输入态退回选项列表态（F16 的 Esc 分支）。**不结算**。

        用 `restore_options()` 而不是 `show_question()`：前者保留已勾选的项。
        多选题里用户勾了两项、又去看了看「其它…」、然后按 Esc 退回来——
        勾选还在才是对的。

        副作用：改面板内容、禁用输入框、把焦点移回面板。
        """
        self._clarify_free_text = False
        panel = self.query_one(ClarifyPanel)
        panel.restore_options()
        self.query_one(InputBar).disabled = True
        panel.focus()

    def _show_approve_panel(self, plan: str) -> None:
        """在主线程展示计划审批面板（复用 ConfirmPanel 的通用是/否）并移焦。"""
        self.query_one(CommandPanel).hide()
        panel = self.query_one(ConfirmPanel)
        # 计划全文可能很长，已作为聊天记录中的普通助手消息展示；这里仅询问是否进入执行阶段。
        # 三个 emoji 全部去掉（F28）：橘色分隔线已经表达「这是要你决定的事」，
        # 两个选项的语义由序号 + 动词承担（面板自己加序号，见 NumberedPanel）。
        #
        # ⚠ **这句说明必须与「获批之后实际会发生什么」逐字对应**，它是本面板上
        # 唯一影响用户决策的信息。改这里之前它写的是「写文件/改文件/运行命令仍会
        # 逐个确认」——那是 auto-plan 扩展**之前**的行为。现在计划获批即回到 `auto`
        # 预设（放行档），那三类操作**一次面板都不弹**。用户实测记录：获批后
        # `write_file` 与两次 `run_command` 全部 `allow（④模式）`，零确认。
        #
        # 一句过期的安全承诺比没有承诺更危险：用户是**据此**点下「开始执行」的，
        # 他以为后面还有一道人工闸门，实际上这就是最后一道。
        #
        # 仍然会弹面板的只剩四类（都不是这句话在说的那三类），所以措辞是
        # 「不再逐个确认」而不是「不会再有任何确认」：②″保护路径的写入、
        # 网络访问（未建域名白名单时）、用户自己写的 Hook `ask` 规则、
        # C16 分类器拦下的命令。
        #
        # ⚠ **成对维护点**：这句文案跟着 `presets.PRESET_AXES[Preset.AUTO]` 的档位走。
        # 将来缺省档若改回 `default`，这里必须同步改回「仍会逐个确认」——
        # 漏改不报错，只是面板又开始撒谎。
        panel.show_prompt(
            "计划已就绪，是否开始执行？",
            "开始执行  [dim]文件写入与命令将直接执行，不再逐个确认[/dim]",
            "暂不执行  [dim]停止本次执行[/dim]",
        )
        panel.focus()

    def _resolve_interaction(self, result, source: str = "human") -> None:
        """
        在主线程结算一次交互：隐藏所有交互面板、还焦输入框、唤醒被阻塞的 Worker。

        幂等：无待决交互时直接返回，避免重复结算（如选择后又收到取消消息）。

        :param result: 结算值（四态确认枚举 / 布尔 / 澄清摘要文本 / None）
        :param source: **这次结算走的是哪条路径**，写进待决盒供 `_interact` 埋点时读取。
            取值集合：
            - `human`        面板按键路径（真人敲键，也包括测试用 Pilot 模拟的按键）
            - `driver`       端到端驱动设施经控制通道直接结算
            - `driver_forced` 驱动设施退出时的强制结算（仍属外部驱动者，单列以便审计）
            - `shutdown`     **产品自己**退出前的强制结算（C10-a，见
              `_settle_pending_before_quit`）。⚠ 与 `driver_forced` 刻意分开：
              那条是外部驱动者掐掉的，这条是用户按了两次 Ctrl+C——
              合并之后，一次真人退出会在审计记录里显示成「测试设施干的」
            - `policy`       P1b 的固定策略应答者预留

            ⚠️ 它标注的是**结算走的哪条路径**，不是对操作者身份的断言——
            用 Pilot 模拟按键时走的是面板自身的按键路径，来源就该是 `human`。

            ⚠️ **本方法有四个调用方**，不是一个：`on_option_list_option_selected`
            的 confirm/approve/clarify 三个分支、`on_confirm_panel_cancelled`、
            `on_clarify_panel_cancelled`，以及驱动设施。前几个靠默认值 `"human"`
            兜住——这是对的，但改动本方法的人很容易以为只有一处调用方。
        """
        box = self._pending_interaction
        if box is None:
            return
        self._pending_interaction = None
        box["source"] = source
        # 自由输入态一定随本次结算结束（ask-user 扩展 F16）
        self._clarify_free_text = False

        # ── 收尾是**有条件**的（ask-user 扩展 F14）──
        #
        # 一次 `ask_user` 可能带好几个问题，循环侧逐题调过来。不是最后一题时
        # 保留面板与焦点，否则用户每答完一题都会看到焦点在输入框与面板之间跳一次。
        #
        # ⚠ **`and result is not None` 不可省。** 用户在第 2 题（共 3 题）
        # 按 Esc 时 `keep_panel` 是真，但循环马上就要 break——不加这个条件，
        # **面板会永远挂在屏幕上，而输入框还是禁用的**，也就是界面假死。
        keep_panel = bool(box.get("keep_panel")) and result is not None
        if not keep_panel:
            self.query_one(ConfirmPanel).hide()
            self.query_one(ClarifyPanel).hide()
            # 恢复输入框：澄清面板期间被禁用（见 _show_clarify_panel），
            # 结算后统一解禁并还焦
            self.query_one(InputBar).disabled = False
            self.query_one(InputBar).focus()

        box["result"] = result
        box["event"].set()

    def on_option_list_option_selected(self, event) -> None:
        """
        处理确认/审批/澄清/会话选择面板的选择（回车/点击）。

        按「当前待决交互的种类」+「事件来源面板」分别把 option.id 解析为对应结果。
        命令面板从不取得焦点、不会触发此消息，故无需额外区分。

        注意：SessionPanel 分支必须放在 `box is None` 守卫**之前**——会话选择不走
        _pending_interaction 机制（主线程发起、无 Worker 阻塞等待），守卫会把它拦掉。
        """
        if isinstance(event.option_list, SessionPanel):
            event.stop()
            # 埋点、关面板、载入三件事全在 `_settle_session` 里（它是会话面板结算的
            # 唯一入口，与 Esc 取消分支共用同一份实现）。
            self._settle_session(event.option.id)
            return

        box = self._pending_interaction
        if box is None:
            return
        ol = event.option_list
        kind = box["kind"]

        if isinstance(ol, ConfirmPanel) and kind == "confirm":
            event.stop()
            mapping = {
                "yes": ConfirmDecision.ALLOW,
                "yes_session": ConfirmDecision.ALLOW_SESSION,
                "yes_permanent": ConfirmDecision.ALLOW_PERMANENT,
                "no": ConfirmDecision.DENY,
            }
            self._resolve_interaction(mapping.get(event.option.id, ConfirmDecision.DENY))

        elif isinstance(ol, ConfirmPanel) and kind == "approve":
            event.stop()
            self._resolve_interaction(event.option.id == "yes")

        elif isinstance(ol, ClarifyPanel) and kind == "clarify":
            event.stop()
            self._settle_clarify(ol, event.option.id)

    def _settle_clarify(self, panel, option_id) -> None:
        """
        澄清面板上选中了一项：三条分支，**顺序即需求**（ask-user 扩展 F16/F11）。

        :param panel: 发出选择消息的 `ClarifyPanel`
        :param option_id: 被选中那一项的 id（候选项下标的字符串、`OTHER_ID`
            或多选题的 `SUBMIT_ID`）

        1. **「其它…」** → 进自由输入态，**不结算**（回调继续阻塞着）。
        2. **多选的「提交」** → 结算为已勾选的那些（可能一项都没有——那是「都不要」，
           与「跳过」是两回事，见 `ClarifyReply` 的说明）。
        3. **多选的候选项** → 勾选并前进，**不结算**。
        4. **单选** → 结算为该项。

        ⚠ **顺序不能反**：多选态下高亮停在「其它…」上按回车，
        要进自由输入而不是提交勾选结果。

        ⚠ **分支 3 在 Textual 8.2.7 上正常不可达，留着是兜底**（F15 修订）。
        实测确认过上游实现：`OptionList._on_click` 就是
        `self.highlighted = clicked_option; self.action_select()`，因此
        **鼠标点击与回车、数字键走的是同一条路**，三者都被
        `ClarifyPanel.action_select` 接住，到不了这里。

        那为什么不删：真到达时的**替代行为是错的**。删掉之后多选态下的数字 id
        会落到分支 4（单选结算），把「用户勾了一项」当成「用户提交了一项」，
        **静默提交一个他没打算交的答案**。留一支与键盘同语义的兜底，
        代价是几行代码；删掉的代价是一次看不见的错答。
        护栏见 `test_ask_user_panel.py::TextualClickRoutingTest`——它钉住的是
        **上游那个事实**，换 Textual 大版本时会红，那正是需要重新判断的时刻。

        副作用：进入自由输入态、切换勾选，或结算一次交互。
        """
        # ── 分支 1：其它…（不结算）──
        if option_id == ClarifyPanel.OTHER_ID:
            self._enter_clarify_free_text()
            return

        question = self._clarify_question
        multi = question is not None and question.multi_select

        # ── 分支 2：多选的「提交」──
        if multi and option_id == ClarifyPanel.SUBMIT_ID:
            custom = panel.custom_text()
            # 勾了「其它…」的话，`kind` 是 `free_text`——它带着 `labels` 与
            # `text` 两截，回灌会把两截分开说（见 `clarify._render_reply`）。
            # 没勾就是纯多选，措辞与从前逐字一致。
            self._resolve_interaction(
                ClarifyReply(kind="free_text", labels=panel.checked_labels(), text=custom)
                if custom
                else ClarifyReply(kind="multi", labels=panel.checked_labels())
            )
            return

        # ── 分支 3：多选的候选项（鼠标点击才到得了，见上面的 ⚠）──
        if multi:
            try:
                position = int(option_id)
            except (TypeError, ValueError):
                return  # 认不出来就什么都不做，绝不拿一个猜的答案去结算
            panel.toggle_and_advance(position)
            return

        # ── 分支 4：单选 ──
        try:
            label = question.options[int(option_id)].label
        except (AttributeError, IndexError, TypeError, ValueError):
            # 取不出来就按「跳过」处理：宁可让模型自己拿主意，
            # 也不要回灌一个我们自己都不确定的答案。
            self._resolve_interaction(None)
            return
        self._resolve_interaction(ClarifyReply(kind="option", labels=(label,)))

    def on_confirm_panel_cancelled(self, event: ConfirmPanel.Cancelled) -> None:
        """确认/审批面板按 Esc 取消：确认视为 DENY、审批视为不批准。"""
        box = self._pending_interaction
        if box is None:
            return
        if box["kind"] == "confirm":
            self._resolve_interaction(ConfirmDecision.DENY)
        elif box["kind"] == "approve":
            self._resolve_interaction(False)

    def on_clarify_panel_cancelled(self, event: ClarifyPanel.Cancelled) -> None:
        """
        澄清面板按 Esc：结算为 None，即「用户跳过」（ask-user 扩展 F17）。

        ⚠ **循环那边据此分两种行为，界面这边只有一种**：规划阶段是「不想规划了、
        整轮停止」（C4 以来的语义），其余任何时候是「我不选，你自己定、循环继续」。
        分岔在 `agent/loop.py` 的 `_run_ask_user` 里做——界面不知道也不该知道
        当前是不是规划阶段。

        ⚠ 自由输入态下的 Esc **走不到这里**：那时焦点在输入框上，面板的绑定
        收不到按键，由 `on_key` 最前面那条分支接住并退回选项列表（F16）。
        """
        if self._pending_interaction is not None:
            self._resolve_interaction(None)
