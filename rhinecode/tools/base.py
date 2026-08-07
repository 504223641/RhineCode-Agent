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
                    为空时 TUI 回退到取 output 首行（见 RhineApp._summarize_result）。
    :param diff: 可选的结构化差异。改文件类工具（edit_file/write_file）填充它，
                 TUI 据此在状态行下方渲染彩色 diff 块；其它工具留空（None）。
                 与 output/summary 解耦：diff 是「结构化数据」，渲染样式由 TUI 决定。
    """
    ok: bool
    output: str
    summary: str = ""
    diff: Optional[DiffView] = None


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

    - system_serial：**系统级串行工具**。为 True 时循环直接放行并强制串行执行，
                 既不进权限管线也不进只读并发桶。

                 目前唯一的使用者是加载 Skill 的工具：它可能开一整条子对话
                 （`context: fork` 的 Skill 由模型自行发起时），而在只读并发桶里
                 跑子对话意味着子对话自己的确认面板会从线程池的工作线程里弹出来。

                 之所以做成工具自己声明的标志、而不是在循环里按名字判断：
                 循环不该认识任何具体工具的名字，那会让 agent 层反向依赖 skills 层。
    """

    name: str = ""
    description: str = ""
    parameters: dict = {}
    read_only: bool = True
    system_serial: bool = False
    plan_safe: bool = False

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
