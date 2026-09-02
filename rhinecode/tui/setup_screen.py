"""
首次启动配置向导的四屏界面（first-run-setup 扩展 T11–T13）。

## 为什么是 ModalScreen，而不是既有的内联覆盖层

本项目已有三个交互面板（`ConfirmPanel` / `ClarifyPanel` / `SessionPanel`），
它们都是 `NumberedPanel(OverlayPanel, OptionList)`——贴在输入框上方的**内联
覆盖层**，靠数字键选项工作，自由文本要绕到主输入框去打。

本向导套不进那个模子，三条理由：

1. 它有**两个必填文本框**（key 与地址），绕到主输入框意味着一次一个、
   来回切换，而这两项在同一屏上要能互相对照；
2. 它有**四个步骤加两个等待态**，内联覆盖层没有「屏」的概念；
3. 启动期那一次**根本没有主输入框**——那时 `RhineApp` 还不存在。

`ModalScreen` 是 Textual 为「占据整屏、有自己的按键绑定、结束时回一个值」
提供的原语，两个入口（启动期宿主 / `/setup`）能原样复用同一个类。
⚠ 这是本项目第一个 `ModalScreen`，是刻意的偏离，不改变既有三个面板的任何行为。

## ⚠ 三条必须守住的东西

**① 所有外部文本渲染前必须过 `tui/widgets.py` 的 `escape`。**
服务端返回的模型名、SDK 的错误消息、Windows 路径都是外部文本。落单的 `[`
会在**布局阶段的主线程**抛 `MarkupError`，**没有任何 try/except 兜得住，
整个 app 退出**（`CLAUDE.md` 架构表 TUI 层第一条致命不变量）。
错误消息里带方括号是常见形态，Windows 路径更是天天见。

**② 后台线程只 `post_message`，绝不 `call_from_thread`。**
两次网络请求是阻塞的，跑在 worker 线程里。回主线程的方式**只用
`post_message`**（非阻塞投递），刻意不用 `call_from_thread`（阻塞式跨线程
调度）——本项目已经因为后一种形态死锁过五次。这里虽然没有持锁，
但形态一旦立住就会被抄，所以从第一版就用对的那个。

**③ 本文件一个 trace 埋点都不加。**
这里没有值得观测的东西，而它经手的恰恰是最敏感的字段（api_key）。
`/setup` 那次是跑在记录器已经存在之后的，加了就会写进产物。
"""

from pathlib import Path
from typing import Callable, Optional

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from rhinecode.setup import catalog, probe, writer
from rhinecode.setup.models import (
    ModelListResult,
    ProbeFailure,
    ProbeResult,
    SetupAction,
    SetupDraft,
    SetupMode,
    SetupOutcome,
)
from rhinecode.tui.widgets import escape

# 官方默认接口地址。第二屏的预填值。
DEFAULT_BASE_URL = "https://api.deepseek.com"

# 四屏的编号。用普通整数而不是枚举：它只在本文件内部流转，
# 而 `_step` 要参与 `1 <= step <= 4` 这类比较，枚举反而绕。
_STEP_INTRO = 1
_STEP_CREDENTIALS = 2
_STEP_MODEL = 3
_STEP_VERIFY = 4


class ModelsLoaded(Message):
    """worker 线程拉完模型清单后投递给主线程的消息。"""

    def __init__(self, result: ModelListResult) -> None:
        super().__init__()
        self.result = result


class VerifyFinished(Message):
    """worker 线程跑完终验后投递给主线程的消息。"""

    def __init__(self, result: ProbeResult) -> None:
        super().__init__()
        self.result = result


class SetupScreen(ModalScreen[SetupOutcome]):
    """
    四屏配置向导。

    构造参数：

    - `config_path`：要写到哪个文件（第一屏会把它显示出来）
    - `prefill`：预填草稿；`/setup` 重跑时由 `writer.read_current` 提供，
      首次启动时是 None
    - `mode`：`FIRST_RUN` / `RERUN`。⚠ **只影响文案与预填，不影响流程**——
      四屏的顺序、能不能放弃、校验失败有几个出口，两种模式逐字相同。
      做成「重跑时少几屏」会让两条路分家，而分家的那一半迟早只有一条被维护到。
    - `list_models_fn` / `verify_fn`：仅供测试注入，缺省是 `probe` 的真函数

    结束方式：`dismiss(SetupOutcome)`。
    """

    BINDINGS = [
        # ⚠ `escape` 在任何一屏都可用（spec F3）。向导是新增的一条路，
        # 不是把「生成模板、让用户自己填」那条老路拆了。
        Binding("escape", "abandon", "放弃", show=False),
    ]

    DEFAULT_CSS = """
    /*
     * ⚠ **颜色一律取项目自己的调色板，不用 Textual 的 `$accent` / `$warning`。**
     * 那两个在默认深色主题下是**橘色**，而橘色在本项目里有专门的语义
     * （「你没做什么，但情况变了」——警告、确认面板、运行中）。真机反馈：
     * 「橘色的边框和字体换成 Rhine 主题的颜色」。
     *
     * 取值全部与 `tui/widgets.py` 同源：
     *   #7AEEFF  主题青（= `widgets.THEME_COLOR`，历史区框线用的就是它）
     *   #5FD75F  成功绿（= `_COLOR_OK`）
     *   #FF5F5F  失败红（= `_COLOR_FAIL`）
     *   #FFA500  警告橘（**兜底提示刻意保留它**——那正是「情况变了」）
     *   #808080  次级灰（= `widgets.SECONDARY_COLOR`）
     */
    SetupScreen {
        align: center middle;
    }
    SetupScreen #setup-root {
        width: 74;
        height: auto;
        max-height: 90%;
        /* 与 HistoryView / #panel-dock 同一条边框，视觉上是一家的 */
        border: solid #7AEEFF 60%;
        background: $surface;
        padding: 1 2;
    }
    SetupScreen #setup-header {
        height: 1;
        margin-bottom: 1;
    }
    SetupScreen #setup-title {
        width: 1fr;
        color: #7AEEFF;
        text-style: bold;
    }
    SetupScreen #setup-step {
        width: auto;
        color: #808080;
        text-align: right;
    }
    SetupScreen .setup-warn {
        color: #FFA500;
    }
    SetupScreen .setup-error {
        color: #FF5F5F;
    }
    SetupScreen .setup-ok {
        color: #5FD75F;
    }
    SetupScreen .setup-dim {
        color: #808080;
    }
    SetupScreen .setup-field-label {
        margin-top: 1;
        color: #808080;
    }
    SetupScreen #setup-models {
        height: auto;
        max-height: 10;
        margin-top: 1;
        border: none;
        background: transparent;
    }
    SetupScreen #setup-models > .option-list--option-highlighted {
        background: #7AEEFF 20%;
        color: #7AEEFF;
        text-style: bold;
    }
    /*
     * 动作按钮区。
     *
     * ⚠ **只有一个按钮时居中，多个时右对齐**（真机反馈）。靠 `.single`
     * 这个类切换，由 `_set_buttons` 按「本屏可见几个」加减——写死一种的话，
     * 四屏里三屏都是单按钮，那一屏三个按钮会挤在中间很别扭。
     */
    SetupScreen #setup-actions {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    SetupScreen #setup-actions.single {
        align-horizontal: center;
    }
    /*
     * ⚠ **按钮扁平化，三个长得一模一样**（真机反馈：「按钮不统一」）。
     * Textual 的 `Button` 缺省带边框、高 3 行，且 `variant="primary"` 会换一套
     * 主题色——于是「开始」是蓝底、「我自己去改文件」是灰底，看起来像两种东西。
     * 这里全部去掉 variant 与边框，只留一行青字，主次靠**位置**区分（主动作在最右）。
     */
    SetupScreen Button {
        height: 1;
        min-width: 0;
        width: auto;
        padding: 0 2;
        margin-left: 2;
        border: none;
        background: transparent;
        color: #7AEEFF;
        text-style: none;
    }
    SetupScreen Button:focus,
    SetupScreen Button:hover {
        background: #7AEEFF 20%;
        color: #7AEEFF;
        text-style: bold;
    }
    """

    def __init__(
        self,
        config_path: Path,
        prefill: Optional[SetupDraft] = None,
        mode: SetupMode = SetupMode.FIRST_RUN,
        *,
        list_models_fn: Optional[Callable[..., ModelListResult]] = None,
        verify_fn: Optional[Callable[..., ProbeResult]] = None,
    ) -> None:
        super().__init__()
        self._config_path = config_path
        self._mode = mode
        self._list_models = list_models_fn or probe.list_models
        self._verify = verify_fn or probe.verify

        # 草稿的初值。⚠ `api_key` 无论如何都从空串起步——
        # `read_current` 本来就不回真实密钥，这里再确认一次。
        self._draft = SetupDraft(
            api_key="",
            base_url=(prefill.base_url if prefill else DEFAULT_BASE_URL),
            model=(prefill.model if prefill else catalog.FALLBACK_OPTIONS[0].model_id),
            context_window=(
                prefill.context_window
                if prefill
                else catalog.window_for(catalog.FALLBACK_OPTIONS[0].model_id)
            ),
        )
        self._step = _STEP_INTRO
        self._list_result: Optional[ModelListResult] = None
        self._probe_result: Optional[ProbeResult] = None
        self._written: tuple[Path, ...] = ()

    # ---- 组装 ----

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-root"):
            # 标题行：左边标题、右上角一个灰色角标（`Esc 退出  1/4`）。
            # ⚠ 角标是**四个字的角标**，不是那句独占一行的说明——真机反馈说
            # 后者「有点多余，不太像一个产品」，已删；但完全没有退出提示的话，
            # 不知道 Esc 的人就只能杀进程了。
            with Horizontal(id="setup-header"):
                yield Static("", id="setup-title")
                yield Static("", id="setup-step")
            yield Static("", id="setup-body")

            # 第二屏：标签 + 输入框。**说明全部放进占位符**（真机选定的极简版式）。
            # ⚠ 已知代价：占位符一打字就消失，而地址那个框预填了值、占位符
            # 根本不会显示。这是选版式时明知并接受的——地址本身自解释。
            yield Static("API Key", id="setup-key-label", classes="setup-field-label")
            yield Input(id="setup-key", password=False)
            yield Static("接口地址", id="setup-url-label", classes="setup-field-label")
            yield Input(id="setup-url")

            # 第三屏：列表 + 兜底提示 + 手输框。
            # ⚠ **正常时不说「列表来自服务端」**（真机选定）——那句话对用户没有
            # 决策价值。兜底提示排在**列表之后**，于是它出现时列表位置不动。
            yield OptionList(id="setup-models")
            yield Static("", id="setup-fallback", classes="setup-warn")
            yield Static("", id="setup-manual-label", classes="setup-field-label")
            yield Input(id="setup-manual", placeholder="直接输入模型名")

            # 第四屏
            yield Static("", id="setup-status")

            # ⚠ 三个按钮**都不带 variant**——带了就会各自换一套主题色，
            # 那正是「按钮不统一」的成因。主次靠位置：主动作永远在最右。
            with Horizontal(id="setup-actions"):
                yield Button("", id="btn-tertiary")
                yield Button("", id="btn-secondary")
                yield Button("", id="btn-primary")

    def on_mount(self) -> None:
        # ⚠ 输入框的初值在挂载后设，不在 compose 里——compose 阶段部件尚未
        # 进入 DOM，对它设值在某些 Textual 版本上会被随后的挂载流程覆盖掉。
        self.query_one("#setup-url", Input).value = self._draft.base_url
        self._repaint()

    # ---- 屏与屏之间 ----

    def _repaint(self) -> None:
        """
        按当前 `_step` 决定每个部件显示与否、内容是什么。

        ⚠ **这个方法一度叫 `_render`，那会让整个 app 起不来。**
        `Widget._render` 是 Textual 自己的方法（渲染内容），覆盖掉它之后
        Screen 渲染自身时拿到的 visual 是 None，抛
        `AttributeError: NoneType has no attribute render_strips`——
        而且抛在**布局阶段**，堆栈里全是 Textual 内部帧，看不出跟本文件有关。

        这正是 `CLAUDE.md` 架构表 TUI 层第三条不变量点名的形态
        （「撞上 `MessagePump` 内部字段一律不报错」——这次运气好，报错了）。
        ⚠ 那条不变量说的是「新增组件的**字段名**先 hasattr 查一遍」，
        实测教训是**方法名也要查**：本次字段名全部查过了，恰恰漏了方法名。
        """
        step = self._step
        self.query_one("#setup-title", Static).update(escape("RhineCode 配置向导"))
        # 右上角标：`Esc 退出  N/4`。
        # ⚠ **成功写盘之后不再提示 Esc**——那时 Esc 的语义已经变成「完成」
        # （见 `action_abandon`），再挂一个「退出」会让人以为按下去东西没存。
        saved = bool(self._written)
        corner = f"{step}/4" if saved else f"Esc 退出  {step}/4"
        self.query_one("#setup-step", Static).update(escape(corner))

        # 各屏专属部件的显隐
        creds = step == _STEP_CREDENTIALS
        model = step == _STEP_MODEL
        verify = step == _STEP_VERIFY
        for widget_id in ("#setup-key-label", "#setup-key", "#setup-url-label", "#setup-url"):
            self.query_one(widget_id).display = creds
        self.query_one("#setup-models").display = model
        self.query_one("#setup-manual-label").display = model
        self.query_one("#setup-manual").display = model
        self.query_one("#setup-fallback").display = model
        self.query_one("#setup-status").display = verify

        renderer = {
            _STEP_INTRO: self._render_intro,
            _STEP_CREDENTIALS: self._render_credentials,
            _STEP_MODEL: self._render_model,
            _STEP_VERIFY: self._render_verify,
        }[step]
        renderer()

    def _set_buttons(
        self,
        primary: Optional[str],
        secondary: Optional[str] = None,
        tertiary: Optional[str] = None,
    ) -> None:
        """
        设置三个动作按钮的文案；传 None 表示这一屏不需要它。

        三个按钮是**固定存在**的部件，靠显隐与改文案复用——每屏各建一批
        按钮的话，`query_one` 会在切屏的瞬间撞上「旧的还没卸、新的已经挂」。
        """
        for widget_id, label in (
            ("#btn-primary", primary),
            ("#btn-secondary", secondary),
            ("#btn-tertiary", tertiary),
        ):
            button = self.query_one(widget_id, Button)
            button.display = label is not None
            if label is not None:
                button.label = label

        # ⚠ **一个按钮时居中，多个时右对齐**（真机反馈）。四屏里三屏是单按钮，
        # 写死右对齐会让它们孤零零地贴在右下角。
        visible = sum(1 for x in (primary, secondary, tertiary) if x is not None)
        self.query_one("#setup-actions").set_class(visible == 1, "single")

    def _focus_primary(self) -> None:
        """
        把焦点放到主按钮上。

        ⚠ **每一屏都必须有一个明确的焦点落点**，因为「按 Enter 等于点主按钮」
        这条约定是靠焦点实现的：`Input` 与 `OptionList` 自己会把 Enter 变成
        提交/选中事件，而没有输入部件的那两屏（第一屏、第四屏）只能靠
        **焦点停在主按钮上**。不显式聚焦的话，Textual 会挑 DOM 里第一个
        可聚焦部件——第四屏失败态那是「改接口地址」，于是 Enter 按下去
        跑到了一个完全不相干的动作上。
        """
        button = self.query_one("#btn-primary", Button)
        if button.display:
            button.focus()

    # ---- 第一屏：说明 ----

    def _render_intro(self) -> None:
        """
        第一屏：一句话说清这是什么、要几步、大概多久。

        ⚠ **刻意不显示配置文件路径，也不解释 Esc 能退出。** 真机反馈原话：
        「这些内容感觉有点多余，不太像一个产品」。退出提示压缩成右上角四个字
        （见 `_repaint`），路径则彻底不出现——需要手改的人有 `/setup`，
        README 里也写着位置。**这是对 spec F6/AC10 的一次修订**，
        `spec.md` 与 `checklist.md` 里都挂了勘误块。
        """
        if self._mode is SetupMode.RERUN:
            body = "修改模型接入配置。保存后于下次启动生效。"
            primary = "开始"
        else:
            body = "首次使用需要配置模型接入信息。共 4 步，约 1 分钟。"
            primary = "开始"
        self.query_one("#setup-body", Static).update(escape(body))
        self._set_buttons(primary)
        self._focus_primary()

    # ---- 第二屏：凭据与地址 ----

    def _render_credentials(self) -> None:
        """第二屏：两个字段，说明放在各自的占位符里。"""
        self.query_one("#setup-body", Static).update(
            escape("填写 DeepSeek 的访问凭据。")
        )
        key_input = self.query_one("#setup-key", Input)
        key_input.placeholder = (
            "留空则不修改"
            if self._mode is SetupMode.RERUN
            else "在 platform.deepseek.com 获取"
        )
        self.query_one("#setup-url", Input).placeholder = "使用代理或私有部署时才需要修改"
        self._set_buttons("下一步")
        key_input.focus()

    def _credentials_ready(self) -> bool:
        """
        第二屏能不能往下走。

        首次配置必须填 key（spec F7）；重跑时留空表示不改，因此允许为空。
        地址两种模式下都必须非空——空地址没有任何合理解释。
        """
        key = self.query_one("#setup-key", Input).value.strip()
        url = self.query_one("#setup-url", Input).value.strip()
        if not url:
            return False
        if self._mode is SetupMode.FIRST_RUN and not key:
            return False
        return True

    # ---- 第三屏：选模型 ----

    def _render_model(self) -> None:
        """
        第三屏：模型列表。

        ⚠ **正常时不显示「列表来自服务端」**（真机选定的版式）——那句话对用户
        没有决策价值。兜底提示排在**列表之后**，因此它出现与否不改变列表的位置。
        """
        self.query_one("#setup-manual-label", Static).update(escape("或直接输入模型名"))
        fallback = self.query_one("#setup-fallback", Static)

        if self._list_result is None:
            self.query_one("#setup-body", Static).update(escape("正在获取可用模型列表…"))
            fallback.update("")
            self._set_buttons(None)
            return

        self.query_one("#setup-body", Static).update(escape("选择模型。"))
        if self._list_result.from_fallback:
            # ⚠ 兜底必须**说出来**（spec F9）。静默退回内置列表等于把
            # 「列表会过期」这个问题原样搬回来了，还多骗用户一次。
            # ⚠ 这里**保留橘色**（真机确认）：橘色在本项目里的语义正是
            # 「你没做什么，但情况变了」，而这恰好就是那种情况。
            reason = self._list_result.error or ""
            lines = ["● 未能获取服务端列表，以下为内置列表，可能已过期"]
            if reason:
                lines.append("  " + reason)
            fallback.update(escape("\n".join(lines)))
        else:
            fallback.update("")

        option_list = self.query_one("#setup-models", OptionList)
        option_list.clear_options()
        for item in self._list_result.options:
            # ⚠ `model_id` 与 `blurb` 都可能来自服务端，一律过 escape
            label = escape(item.model_id)
            if item.blurb:
                label += "  " + escape(item.blurb)
            if item.recommended:
                label += "  · 推荐"
            option_list.add_option(Option(label, id=item.model_id))
        self._set_buttons("下一步")
        # ⚠ **进来就聚焦列表**（真机要求「上下键可以选择」）。不聚焦的话
        # 焦点会落在手输框上，上下键什么都不做，而列表看起来是可选的。
        option_list.focus()

    # ---- 第四屏：终验与写盘 ----

    def _render_verify(self) -> None:
        """
        第四屏：终验结果。

        ⚠ **成功页刻意只有一行结果**。原先还列了写入的文件路径与
        「同时生成的三份模板」两段，真机反馈说多余；而且那两段**自相矛盾**
        ——清单里只列 1 个文件，紧接着又说还有 3 个。**这是对 spec F10/AC15
        的一次修订**，`spec.md` 与 `checklist.md` 都挂了勘误块。
        """
        status = self.query_one("#setup-status", Static)
        if self._probe_result is None:
            self.query_one("#setup-body", Static).update(
                escape(f"正在连接 {self._draft.model}…")
            )
            status.update("")
            self._set_buttons(None)
            return

        self.query_one("#setup-body", Static).update("")

        if self._probe_result.ok:
            # 毫秒换成秒：`1264 毫秒` 要在脑子里换算一次，`1.3 秒` 不用。
            seconds = self._probe_result.elapsed_ms / 1000
            status.update(
                escape(f"● 连接成功 · {self._draft.model} · {seconds:.1f} 秒")
            )
            status.set_classes("setup-ok")
            self._set_buttons("开始用" if self._mode is SetupMode.FIRST_RUN else "完成")
            self._focus_primary()
            return

        # 失败：三个出口（spec F4）。
        # ⚠ 服务端原话**另起一行、灰色**，不再挤在括号里。
        detail = self._probe_result.detail
        head, _, tail = detail.partition("（服务端说：")
        lines = ["● " + head.strip()]
        if tail:
            lines.append("  服务端说：" + tail.rstrip("）"))
        status.update(escape("\n".join(lines)))
        status.set_classes("setup-error")
        self._set_buttons("重填 Key", "跳过验证，直接保存", "改接口地址")
        self._focus_primary()

    # ---- 后台工作 ----

    @work(thread=True, exclusive=True, group="setup-net")
    def _load_models(self, api_key: str, base_url: str) -> None:
        """
        在 worker 线程里拉模型清单，结果经 `post_message` 回主线程。

        ⚠ **只 `post_message`，绝不 `call_from_thread`**——后者是阻塞式跨线程
        调度，本项目已经因为那个形态死锁过五次。这里虽然没有持锁，
        但形态一旦立住就会被抄。

        副作用：一次 HTTP 请求。
        """
        self.post_message(ModelsLoaded(self._list_models(api_key, base_url)))

    @work(thread=True, exclusive=True, group="setup-net")
    def _run_verify(self, api_key: str, base_url: str, model: str) -> None:
        """
        在 worker 线程里跑终验，结果经 `post_message` 回主线程。

        副作用：一次 HTTP 请求，消耗极少量 token。
        """
        self.post_message(VerifyFinished(self._verify(api_key, base_url, model)))

    def on_models_loaded(self, message: ModelsLoaded) -> None:
        if self._step != _STEP_MODEL:
            # 用户在等待期间按 Esc 或退回上一屏了——结果直接丢弃。
            return
        self._list_result = message.result
        self._repaint()
        # 预选：优先选中草稿里那个（重跑时是用户当前在用的），
        # 否则选推荐项，再否则选第一个。
        option_list = self.query_one("#setup-models", OptionList)
        ids = [o.model_id for o in message.result.options]
        if self._draft.model in ids:
            option_list.highlighted = ids.index(self._draft.model)
        else:
            preferred = next(
                (i for i, o in enumerate(message.result.options) if o.recommended), 0
            )
            option_list.highlighted = preferred

    def on_verify_finished(self, message: VerifyFinished) -> None:
        if self._step != _STEP_VERIFY:
            return
        self._probe_result = message.result
        if message.result.ok:
            self._save()
        self._repaint()

    # ---- 写盘与结束 ----

    def _save(self) -> None:
        """
        把草稿写进配置文件。

        写盘失败不抛给 Textual——那会掀掉整个 app，而用户此刻只是在填配置。
        失败时把结果改写成一条可读的错误，仍然留在第四屏上。

        副作用：写文件。
        """
        try:
            self._written = writer.apply(self._config_path, self._draft)
        except OSError as exc:
            self._probe_result = ProbeResult(
                ok=False,
                kind=ProbeFailure.OTHER,
                detail=f"配置写不进去：{exc}",
                elapsed_ms=0,
            )
            self._written = ()

    def action_abandon(self) -> None:
        """
        `Esc`：放弃（spec F3/F18）——**不动任何文件**，交回 ABANDONED。

        ⚠ 这一条在**任何一屏**都成立，包括两个等待态——网络卡住时
        `Esc` 必须能出来（spec N4）。worker 是 `exclusive` 的，
        Screen 关闭后它投递的消息会被上面两个 handler 的 `_step` 检查丢掉。

        ⚠ **但配置已经写盘之后，`Esc` 的语义翻转成「完成」。** 这是真机复核
        时发现的一个真 bug：第四屏成功页里配置**已经存好了**，此时按 Esc
        却走放弃分支——启动路径据此打印「请在 config.yaml 填入真实 api_key
        后重新运行」然后退出，而那句话此刻是**假的**（key 就在文件里）。
        用户看到的是「明明配好了，它还让我去填」。判据取 `self._written`
        非空，即「本次真的落过盘」。
        """
        if self._written:
            self._finish_saved()
            return
        self.dismiss(SetupOutcome(action=SetupAction.ABANDONED, written=()))

    def _finish_saved(self) -> None:
        self.dismiss(SetupOutcome(action=SetupAction.SAVED, written=self._written))

    # ---- 事件 ----

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button_id = event.button.id
        step = self._step

        # 主按钮一律走 `_advance`——它与 Enter 共用同一条路径，
        # 两处各写一遍的话「按钮做了 A、回车做了 B」这种分叉不会报错。
        if button_id == "btn-primary":
            self._advance()
            return

        if step == _STEP_VERIFY and self._probe_result is not None:
            if button_id == "btn-secondary":
                # 「跳过验证，直接保存」（spec F4）：校验没过也让人进去。
                # 断网、代理抽风、服务端 5xx 都不该把人锁死在向导里。
                self._save()
                self._finish_saved()
            elif button_id == "btn-tertiary":
                self._back_to_credentials(focus_url=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """
        在输入框里按回车 = 点主按钮（真机要求：**每一屏 Enter 都等于下一步**）。

        另外两屏（第一屏、第四屏）没有输入部件，靠 `_focus_primary` 把焦点
        停在主按钮上，Textual 自己就会把 Enter 变成一次 `Button.Pressed`。
        两条路合起来才是完整的「Enter 一律等于主按钮」。
        """
        event.stop()
        self._advance()

    def _advance(self) -> None:
        """按当前屏执行「下一步」。Enter 与主按钮共用它，避免两处分叉。"""
        if self._step == _STEP_INTRO:
            self._goto_credentials()
        elif self._step == _STEP_CREDENTIALS:
            self._submit_credentials()
        elif self._step == _STEP_MODEL:
            self._submit_model()
        elif self._probe_result is not None and self._probe_result.ok:
            self._finish_saved()
        elif self._probe_result is not None:
            self._back_to_credentials(focus_url=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """在清单里按回车选中一个模型 = 选好了，直接往下走。"""
        event.stop()
        if self._step != _STEP_MODEL:
            return
        self._submit_model()

    # ---- 步骤迁移 ----

    def _goto_credentials(self) -> None:
        self._step = _STEP_CREDENTIALS
        self._repaint()

    def _back_to_credentials(self, *, focus_url: bool) -> None:
        self._step = _STEP_CREDENTIALS
        self._probe_result = None
        self._repaint()
        self.query_one("#setup-url" if focus_url else "#setup-key", Input).focus()

    def _submit_credentials(self) -> None:
        if not self._credentials_ready():
            return
        self._draft = SetupDraft(
            api_key=self.query_one("#setup-key", Input).value.strip(),
            base_url=self.query_one("#setup-url", Input).value.strip(),
            model=self._draft.model,
            context_window=self._draft.context_window,
        )
        self._step = _STEP_MODEL
        self._list_result = None
        self._repaint()
        self._load_models(self._effective_key(), self._draft.base_url)

    def _selected_model(self) -> str:
        """
        当前选定的模型：手输框非空时以它为准，否则取清单里高亮那一项。

        手输优先是刻意的——用户特意打了字，那就是他的意思。
        """
        manual = self.query_one("#setup-manual", Input).value.strip()
        if manual:
            return manual
        option_list = self.query_one("#setup-models", OptionList)
        index = option_list.highlighted
        if index is None or self._list_result is None:
            return self._draft.model
        try:
            return self._list_result.options[index].model_id
        except IndexError:
            return self._draft.model

    def _submit_model(self) -> None:
        model = self._selected_model()
        self._draft = SetupDraft(
            api_key=self._draft.api_key,
            base_url=self._draft.base_url,
            model=model,
            # ⚠ 窗口跟着模型走（spec F12）。显式写进配置而不是依赖缺省值，
            # 是为了让用户看得见这个数字，将来换模型时知道有这么一项要跟着改。
            context_window=catalog.window_for(model),
        )
        self._step = _STEP_VERIFY
        self._probe_result = None
        self._repaint()
        self._run_verify(self._effective_key(), self._draft.base_url, self._draft.model)

    def _effective_key(self) -> str:
        """
        发网络请求时用哪个 key。

        草稿里为空（`/setup` 重跑、用户没重填）时，用**文件里现有的那个**——
        不然重跑一次向导必然验证失败，而用户什么都没做错。

        ⚠ 这个值只用于发请求，**绝不回写进草稿**：草稿里的空串是
        「不要改这一项」的信号，污染它就等于把 spec F16 撤销了。
        """
        if self._draft.api_key:
            return self._draft.api_key
        return _read_existing_key(self._config_path)


def _read_existing_key(path: Path) -> str:
    """
    从现有配置里取出 api_key，仅供 `/setup` 重跑时发验证请求用。

    :param path: 配置文件路径
    :returns: 现有密钥；读不出来时返回空串

    ⚠ 刻意**不放进 `writer.read_current`**：那个函数的契约是「绝不把真实密钥
    读进草稿」，而本函数干的正是相反的事。混在一起会让那条契约变得含糊，
    下一个人很容易顺手让 `read_current` 也回真实密钥。

    副作用：读一次文件。
    """
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get("api_key")
    return value if isinstance(value, str) else ""
