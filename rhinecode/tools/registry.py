"""
工具注册中心。

集中登记所有工具、按名查找，并把已注册工具批量转换成 DeepSeek/OpenAI API
所需的工具描述列表。协调层持有一个 ToolRegistry 实例，据此向模型暴露工具、
并在模型发起调用后按名找回工具执行。

新增工具流程：实现 Tool 子类 → 在 default() 中 register 一行即可，无需改动上层。
"""

from rhinecode.tools.base import Tool
from rhinecode.tools.read_file import ReadFileTool
from rhinecode.tools.write_file import WriteFileTool
from rhinecode.tools.edit_file import EditFileTool
from rhinecode.tools.run_command import RunCommandTool
from rhinecode.tools.glob_files import GlobTool
from rhinecode.tools.grep_content import GrepTool


class ToolRegistry:
    """
    工具注册中心。

    内部用 name → Tool 的字典维护已注册工具，保证按名查找为 O(1)，
    且天然防止重名（重名后注册会覆盖前者）。
    """

    def __init__(self) -> None:
        # name → Tool 实例
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """
        登记一个工具。

        :param tool: 已实例化的 Tool 子类对象，其 name 作为查找键
        :raises ValueError: 工具未声明 name 时抛出（防止误注册无名工具）

        副作用：写入内部字典；若 name 已存在则覆盖。
        """
        if not tool.name:
            raise ValueError("工具必须声明非空的 name 才能注册")
        self._tools[tool.name] = tool

    def get(self, name: str) -> "Tool | None":
        """
        按名查找工具。

        :param name: 工具名（模型在 tool_call 中给出的函数名）
        :returns: 对应的 Tool 实例；不存在时返回 None（由调用方转结构化错误）
        """
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        """
        导出所有已注册工具的 API 描述列表。

        :returns: 每个元素为 OpenAI function 工具格式的字典，可直接作为
                  chat.completions 请求的 tools 参数

        副作用：无（仅读取已注册工具并转换）。
        """
        return [tool.to_schema() for tool in self._tools.values()]

    def readonly_schemas(self) -> list[dict]:
        """
        仅导出只读工具（read_only=True）的 API 描述列表。

        供 Agent 循环在 Plan Mode 规划阶段使用：此时只向模型开放只读工具，
        禁止其发起写文件 / 改文件 / 执行命令等有副作用的操作（spec F11）。

        :returns: 只读工具的 function 描述列表

        副作用：无（仅读取已注册工具并转换）。
        """
        return [tool.to_schema() for tool in self._tools.values() if tool.read_only]

    @classmethod
    def default(cls) -> "ToolRegistry":
        """
        构建并返回注册了 6 个核心工具的默认注册中心。

        注册顺序即为 schemas() 输出顺序：
        read_file / write_file / edit_file / run_command / glob_files / grep_content。

        :returns: 已登记 6 个工具的 ToolRegistry 实例
        """
        registry = cls()
        registry.register(ReadFileTool())
        registry.register(WriteFileTool())
        registry.register(EditFileTool())
        registry.register(RunCommandTool())
        registry.register(GlobTool())
        registry.register(GrepTool())
        return registry
