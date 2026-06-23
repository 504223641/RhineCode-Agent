"""
工具层包。

本包实现 RhineCode 的工具系统：统一的 Tool 抽象（base）、六个核心工具
（read_file / write_file / edit_file / run_command / glob_files / grep_content）
以及集中登记与查找的 ToolRegistry（registry）。

设计原则：
- 工具层完全独立，不依赖 Provider / 协调层 / TUI，任何一层都可单独调用工具
- 新增工具只需继承 Tool 并在 ToolRegistry 中登记，无需改动上层代码
"""
