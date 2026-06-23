"""
执行命令工具。

在项目工作目录下执行一条 shell 命令，捕获标准输出、标准错误和退出码。
属于有副作用工具（read_only=False），执行前需用户确认，且串行执行。

安全说明：本章不做沙箱/黑名单，命令直接以 shell 方式执行，危险操作由
协调层的用户确认弹窗兜底（spec N6）。命令带超时上限，避免无限期挂起（N1）。
"""

import subprocess

from rhinecode.tools.base import Tool, ToolResult

# 命令执行的默认超时（秒）。超过则终止子进程并返回超时错误。
# 定义为模块常量，便于后续统一调整；本章不暴露为 YAML 配置项。
DEFAULT_TIMEOUT = 30


class RunCommandTool(Tool):
    """在项目工作目录下执行 shell 命令并返回输出。"""

    name = "run_command"
    description = (
        "在项目工作目录下执行一条 shell 命令，返回标准输出、标准错误与退出码。"
        "用于运行测试、构建、查看环境等。命令有超时限制，请避免执行交互式或长时间挂起的命令。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令。",
            },
            "timeout": {
                "type": "integer",
                "description": f"超时秒数（可选，默认 {DEFAULT_TIMEOUT} 秒）。",
            },
        },
        "required": ["command"],
    }
    read_only = False

    def execute(self, args: dict) -> ToolResult:
        """
        执行 shell 命令。

        执行步骤：
        1. 取出 command 与可选 timeout
        2. 以 shell 方式在当前工作目录运行，捕获 stdout/stderr，带超时
        3. 拼接退出码、stdout、stderr 为可读文本返回
        4. 超时 → 结构化超时错误；其他异常 → 结构化错误

        :param args: 含 "command"（必填）与 "timeout"（可选）键
        :returns: output 含退出码与输出；退出码非 0 时 ok 仍可为 True（命令本身失败由输出体现），
                  但执行层面的异常（超时、无法启动）返回 ok=False

        副作用：在工作目录执行外部命令，可能修改文件系统、网络等任意状态。
        """
        try:
            command = args.get("command")
            if not command:
                return ToolResult(ok=False, output="缺少必填参数 command")

            timeout = args.get("timeout") or DEFAULT_TIMEOUT

            # cwd 不显式指定，默认即为当前进程工作目录（项目根），与文件类工具基准一致
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            # 把退出码与两路输出拼成对模型可读的结构化文本
            parts = [f"退出码: {proc.returncode}"]
            if proc.stdout:
                parts.append(f"stdout:\n{proc.stdout}")
            if proc.stderr:
                parts.append(f"stderr:\n{proc.stderr}")
            output = "\n".join(parts)

            # 退出码非 0 视为命令失败（ok=False），但仍把完整输出回灌供模型判断
            return ToolResult(ok=(proc.returncode == 0), output=output)

        except subprocess.TimeoutExpired:
            return ToolResult(
                ok=False,
                output=f"命令执行超时（超过 {args.get('timeout') or DEFAULT_TIMEOUT} 秒）已被终止。",
            )
        except Exception as e:
            return ToolResult(ok=False, output=f"命令执行失败: {e}")
