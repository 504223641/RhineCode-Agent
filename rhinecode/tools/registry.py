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
from rhinecode.tools.mcp_config import MCPResolveServerTool


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

    def unregister(self, name: str) -> bool:
        """
        按名称注销一个工具。

        MCP 重载时需要移除旧 server 暴露的远端工具；返回值用于测试和诊断，调用方无需先
        判断工具是否存在。

        :returns: 实际移除了工具时返回 True；原本不存在时返回 False。
        """
        return self._tools.pop(name, None) is not None

    def names(self) -> frozenset[str]:
        """
        返回当前已注册的全部工具名。

        用途（c11 T3）：
        1. **启动校验**——Skill 的 `allowed_tools` 白名单里若出现不存在的内置工具名
           （通常是笔误），启动时立刻 fail-fast（spec F16）；
        2. **每轮运行期自愈**——已激活 Skill 的白名单并集要与「注册中心当前工具名」
           取交集，这样 MCP 运行时重载导致某个远端工具消失后，它自动不再出现在
           可见工具集里，无需任何额外同步（spec F14）。

        返回 frozenset 而非 list/set：调用方只做成员判断与集合运算，
        不可变的返回值能防止调用方误改注册中心内部状态。

        注意：`ToolRegistry` 没有实现 `__iter__`，调用方不得写 `for t in registry`，
        取名字请用本方法、取工具对象请用 `get(name)`。

        副作用：无（只读快照，此后注册/注销不影响已返回的集合）。
        """
        return frozenset(self._tools)

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

    def planning_schemas(self) -> list[dict]:
        """
        导出 **Plan Mode 规划阶段**可用的工具描述（c13）。

        = 只读工具 **+** 声明了 `plan_safe` 的工具。

        :returns: function 描述列表

        与 `readonly_schemas()` 分成两个方法而不是加参数：前者的语义是
        「哪些工具没有副作用」，是一个关于**工具本身**的事实；本方法的语义是
        「规划阶段能发什么」，是一条**策略**。把策略混进事实查询里，
        将来任何一方变化都会牵动另一方。

        用 `or` 而不是两次查询再拼接：一个工具可能同时是只读与 plan_safe
        （虽然那样声明没有意义），拼接会让它在 schema 列表里出现两次。

        副作用：无。
        """
        return [
            tool.to_schema()
            for tool in self._tools.values()
            if tool.read_only or tool.plan_safe
        ]

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
        # 解析 MCP 只访问 registry/NPM 元数据，不依赖运行时 MCPManager，因此可作为默认工具注册。
        registry.register(MCPResolveServerTool())
        return registry
