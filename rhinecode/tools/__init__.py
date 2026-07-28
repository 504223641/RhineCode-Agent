"""
工具层包。

本包实现 RhineCode 的工具系统：统一的 Tool 抽象（base）、六个核心工具
（read_file / write_file / edit_file / run_command / glob_files / grep_content）
以及集中登记与查找的 ToolRegistry（registry）。

设计原则：
- 工具层完全独立，不依赖 Provider / 协调层 / TUI，任何一层都可单独调用工具
- 新增工具只需继承 Tool 并在 ToolRegistry 中登记，无需改动上层代码

──────────────────────────────────────────────────────────────────────
本 __init__.py 刻意不导入任何子模块，这不是疏忽，而是一条必须守住的前提
──────────────────────────────────────────────────────────────────────

`tools` 与其它包存在**单向但交叉**的依赖：

    tools.load_skill   → skills.manager       （tools 依赖 skills）
    tools.base         ← mcp.tool_adapter     （mcp 依赖 tools）
    tools.mcp_config   → mcp.auto_config      （tools 依赖 mcp）

`tools ↔ mcp` 这一组是货真价实的互依，之所以不成环，唯一依靠的就是本文件是空的
（只有 docstring）：Python 导入 `rhinecode.tools.mcp_config` 时会先执行
`rhinecode/tools/__init__.py`，若它是空的，`tools` 包立刻初始化完成，
随后加载子模块即可，整条链路上不会回头去碰 `mcp`。

**警告：不要在此处 re-export 任何子模块。**
比如图方便写一行 `from rhinecode.tools.registry import ToolRegistry`，
后果是：`mcp.tool_adapter` 导入 `tools.base` → 触发 `tools/__init__.py` →
它去导入 `tools.registry`（或任何最终会牵扯到 `tools.mcp_config` 的模块）→
后者又要导入 `mcp.auto_config` → 而 `mcp` 此刻正处在半初始化状态 →
`ImportError: cannot import name ...`。这类错误的报错位置离真正的原因很远，
排查成本极高，所以在这里写死这条约束。

> 历史注记：C11 时 `skills.models` 反向依赖 `tools.policy`，
> 于是 `tools ↔ skills` 也是一组互依。对齐改造把 `ToolPolicy` 整个删掉之后，
> 这一组变成了单向（只剩 `tools.load_skill → skills.manager`）。
> 约束本身不变——`tools ↔ mcp` 仍然需要它。
"""
