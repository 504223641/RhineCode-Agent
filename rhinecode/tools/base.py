"""
工具抽象层，定义所有工具必须遵守的统一接口与统一的结果类型。

设计原则：
- 协调层只依赖 Tool 抽象与 ToolResult，不感知具体工具实现
- 每个工具自描述（name / description / parameters），注册中心据此批量导出 API 工具列表
- read_only 标志一处声明，同时决定两件事：是否需要执行前确认、能否与其他工具并发执行
- execute 必须自行兜底所有异常，统一转成 ToolResult(ok=False)，绝不向上抛出，
  以保证协调层的工具执行流程不崩溃（对应 spec 的 F13/N2）
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from rhinecode.tools.diff import DiffView


def human_size(n_bytes: int) -> str:
    """
    把字节数格式化为便于人读的体量字符串。

    规则：<1024 用 B；<1MiB 用 K（保留一位小数）；其余用 M（保留一位小数）。
    供 read_file / write_file 等工具构造 output 头部与 summary 复用。

    :param n_bytes: 字节数（非负）
    :returns: 如 "340 B" / "4.2K" / "1.3M"
    """
    if n_bytes < 1024:
        return f"{n_bytes} B"
    if n_bytes < 1024 * 1024:
        return f"{n_bytes / 1024:.1f}K"
    return f"{n_bytes / (1024 * 1024):.1f}M"


@dataclass
class ToolResult:
    """
    工具执行的统一结果。

    无论成功或失败，工具都返回本结构，便于协调层统一回灌模型、TUI 统一着色。

    :param ok: 执行是否成功。True → TUI 以绿色展示；False → 红色展示
    :param output: 回灌给模型的文本。成功时为工具产出内容（如文件内容、命令输出）；
                   失败时为对模型可读的错误描述，模型可据此调整重试。面向「模型」。
    :param summary: 面向「TUI 单行展示」的简短摘要（如「读取 152 行 · 4.2K」）。
                    与 output 解耦：output 给模型看完整内容，summary 给人看量级与状态。
                    为空时 TUI 回退到取 output 首行（见 RhineApp._result_summary）。
    :param diff: 可选的结构化差异。改文件类工具（edit_file/write_file）填充它，
                 TUI 据此在状态行下方渲染彩色 diff 块；其它工具留空（None）。
                 与 output/summary 解耦：diff 是「结构化数据」，渲染样式由 TUI 决定。
    :param full_output: **完整原文，只进行为记录（trace），不进模型上下文、不进界面。**
                 缺省 None 表示「`output` 就是全部」——绝大多数工具都是这样，不必填。

                 只有**主动裁剪过 `output`** 的工具才填它。目前唯一的填写方
                 是 `run_command`：它的 `_clip` 只保留前 30 行 + 后 10 行，
                 一次 200 行的测试输出有 160 行**在任何地方都不存在**——
                 模型看不到是对的（省 token），但连 trace 里也没有就不对了，
                 那正是排查「测试到底为什么失败」时唯一有用的部分。

                 ⚠️ **裁剪 `output` 与填 `full_output` 必须成对。** 漏填不报错，
                 只是那段内容永久丢失且无人察觉——记录看起来是完整的，
                 因为被裁掉的地方连痕迹都没有（`_clip` 的省略提示是给模型看的，
                 它不告诉你被省掉的**内容**是什么）。
                 护栏见 `tests/test_trace_full_output.py`。
    """
    ok: bool
    output: str
    summary: str = ""
    diff: Optional[DiffView] = None
    full_output: Optional[str] = None


class Tool(ABC):
    """
    工具抽象基类。

    所有具体工具均须继承本类，声明四项类属性元信息并实现 execute 方法。

    类属性约定：
    - name：工具名，作为 API function 名称，须在注册中心内唯一
    - description：面向模型的用途描述，决定模型何时选择该工具，应清晰具体
    - parameters：参数的 JSON Schema（顶层 type 固定为 "object"），随工具描述发给模型
    - read_only：是否只读。True 表示无副作用（如读文件、搜索），执行前不需确认且可并发；
                 False 表示有副作用（如写文件、执行命令），执行前需用户确认且须串行执行
    - plan_safe：**规划阶段仍可用**。缺省 False。

                 Plan Mode 的规划阶段只向模型开放只读工具（承诺是「批准前不动手」）。
                 声明本标志的工具即使 `read_only=False` 也照常出现在那一阶段，
                 且不被规划阶段守卫拦下。

                 ⚠️ **声明它等于做出一个承诺：本工具在规划阶段不产生任何副作用。**
                 循环会在调用时多传一个 `plan_stage: bool` 关键字参数告知当前阶段，
                 因此**声明本标志的工具必须接受它**（签名写成
                 `execute(self, args, plan_stage=False)`），并据此自我约束。

                 目前唯一的使用者是委派工具：Plan Mode 下最需要把调研赶出主上下文
                 （规划要读很多东西，而那些内容要一路背到执行阶段），因此它在规划阶段
                 仍然开放，但**只允许委派给最终工具集全只读的角色**——这个约束由
                 它自己在 `plan_stage=True` 时执行。

                 只对**非只读**工具有意义：只读工具本来就在规划阶段可用，
                 而非只读工具一律走串行桶，所以 `plan_stage` 只在串行路径传递。

    - workspace_aware：**需要知道本次调用的工作目录**。缺省 False（c14）。

                 声明本标志的工具，其 `execute` **必须接受一个 `cwd` 关键字参数**
                 （签名写成 `execute(self, args, cwd)`），循环会把本次运行的
                 工作目录传进来：主对话与非隔离子 Agent 拿到主项目根，
                 隔离子 Agent 拿到它自己的隔离工作区。

                 一切碰路径或起子进程的工具都该声明它：文件读写编辑、
                 模式匹配、内容检索、命令执行。声明后**必须真的用那个 cwd**
                 去做路径解析（`path_guard` 的四个判定函数都要求显式传根），
                 否则隔离形同虚设。

                 ⚠ **与 `plan_stage` 的传递方式刻意不同：`cwd` 两条执行路径都要传。**
                 `plan_stage` 只在串行路径传递（它只对非只读工具有意义），
                 而只读工具（读文件、glob、grep）走的是**并发**路径，
                 它们同样需要 cwd。照抄 `plan_stage` 的写法会漏掉并发路径，
                 后果是隔离子 Agent 的**读**落到主项目根、**写**却是对的——
                 界面上完全看不出来。护栏见 `tests/test_loop_cwd_dispatch.py`。

    - system_serial：**系统级串行工具**。它精确地意味着两件事，一件都不多：

                 ① **强制串行执行**，不进只读并发桶；
                 ② 权限引擎判 **ASK 时按 ALLOW 处理**——即**不弹确认面板**。

                 ⚠ 它**不**意味着「不进权限管线」。这类工具照常过一次
                 `engine.decide`，DENY 照常生效（`deny: run_agent` 这种整工具规则
                 拦得住它们）。这里一度写着「直接放行、既不进权限管线」，那描述
                 对应的是一个从 C13 起就存在的缺陷，已于 perm-system-serial-bypass
                 修掉——**错误的安全承诺比没有承诺更危险**。

                 两条性质同源：这类工具可能开一整条子对话（`context: fork` 的
                 Skill 由模型自行发起、或 `run_agent` 委派）。在只读并发桶里跑
                 子对话意味着子对话自己的确认面板会从线程池的工作线程里弹出来；
                 而让它在预扫处停下来等一个面板会把整条交互链拧成死结。

                 使用者共七个：`load_skill`、`run_agent`，以及 C15 的五个协作工具。

                 之所以做成工具自己声明的标志、而不是在循环里按名字判断：
                 循环不该认识任何具体工具的名字，那会让 agent 层反向依赖 skills 层。

    - primary_arg：**界面上要显示哪一个参数的值**。缺省空串 = 未声明
                 （tui-display 扩展 F12）。

                 取值必须是 `parameters` 里真实存在的属性名。工具行的标题会写成
                 `标签(该参数的值)`——例如 `read_file` 声明 `"path"` 之后显示成
                 `Read(rhinecode/tui/app.py)`，而不是改造前那种
                 `Read(path=rhinecode/tui/app.py, offset=10)`。

                 **怎么选**：挑「用户扫一眼就知道这次调用在动什么」的那一个。
                 通常是路径、模式、命令、地址、收件人——**不是**内容、不是选项。
                 一个反例：`write_file` 选 `path` 而不是 `content`，
                 后者可能是整份文件。

                 **不声明是安全的**：界面会回退到既有的键值对摘要
                 （`tui/widgets.py` 的 `summarize_args`），形态与改造前一致。
                 之所以留这条兜底，是因为「新增工具忘了声明」必然会发生，
                 而它的后果应该是「显示得没那么好看」，不该是
                 `Read()` 这种看起来像无参调用的异常形态。

                 与 `_TOOL_LABELS`（内部名 → 展示标签）的分工：那张表在展示层，
                 因为它是纯粹的用词选择；这个字段在工具本体，因为**只有工具自己
                 知道哪个参数最重要**，而且改参数时它就在眼前、不容易漏。
    """

    name: str = ""
    description: str = ""
    parameters: dict = {}
    read_only: bool = True
    system_serial: bool = False
    plan_safe: bool = False
    workspace_aware: bool = False
    primary_arg: str = ""

    @abstractmethod
    def execute(self, args: dict) -> ToolResult:
        """
        执行工具逻辑。

        :param args: 模型生成并解析后的参数字典，键对应 parameters 中声明的属性
        :returns: 统一的 ToolResult；实现必须捕获自身所有异常并转为 ok=False 结果，
                  不得向上抛出（保证协调层流程不崩溃）

        副作用：因工具而异，read_only=False 的工具会修改文件系统或执行外部命令。
        """
        ...

    def to_schema(self) -> dict:
        """
        把工具元信息转换为 DeepSeek/OpenAI API 接受的 function 工具描述。

        :returns: 形如 {"type": "function", "function": {name, description, parameters}}
                  的字典，供注册中心聚合后随 chat.completions 请求发送
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
