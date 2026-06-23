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


@dataclass
class ToolResult:
    """
    工具执行的统一结果。

    无论成功或失败，工具都返回本结构，便于协调层统一回灌模型、TUI 统一着色。

    :param ok: 执行是否成功。True → TUI 以绿色展示；False → 红色展示
    :param output: 回灌给模型的文本。成功时为工具产出内容（如文件内容、命令输出）；
                   失败时为对模型可读的错误描述，模型可据此调整重试
    """
    ok: bool
    output: str


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
    """

    name: str = ""
    description: str = ""
    parameters: dict = {}
    read_only: bool = True

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
