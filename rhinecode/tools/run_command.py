"""
执行命令工具。

在项目工作目录下执行一条 shell 命令，捕获标准输出、标准错误和退出码。
属于有副作用工具（read_only=False），执行前需用户确认，且串行执行。

安全说明：本章不做沙箱/黑名单，命令直接以 shell 方式执行，危险操作由
协调层的用户确认弹窗兜底（spec N6）。命令带超时上限，避免无限期挂起（N1）。
"""

import locale
import subprocess

from rhinecode.tools.base import Tool, ToolResult
from rhinecode.tools.path_guard import workspace_root

# 命令执行的默认超时（秒）。超过则终止子进程并返回超时错误。
# 定义为模块常量，便于后续统一调整；本章不暴露为 YAML 配置项。
DEFAULT_TIMEOUT = 30

# 输出截断保护：单路输出（stdout/stderr）行数超过 RUN_HEAD+RUN_TAIL 时，
# 只保留前 RUN_HEAD 行与后 RUN_TAIL 行，中间以省略提示替代，避免长输出爆 token。
RUN_HEAD = 30
RUN_TAIL = 10


def _decode(raw: bytes) -> str:
    """
    把子进程的原始字节输出解码成文本。

    :param raw: 子进程某一路输出的原始字节（可能为空）
    :returns: 解码后的文本；解不出来时用替换字符兜底，**绝不抛异常**

    为什么必须自己解码，而不是让 subprocess 用 `text=True` 代劳：
    `text=True` 会按 `locale.getpreferredencoding()` 解码，在中文 Windows 上
    是 cp936(GBK)。而 git / python / node 这些现代工具链一律输出 UTF-8。
    两者一撞，subprocess 的**读取线程**里抛出 UnicodeDecodeError——那个异常
    死在后台线程里被吞掉，`proc.stdout` 最终是空字符串，**退出码仍是 0**。
    表现就是「命令明明成功了，输出却凭空消失」：`git log` / `git diff` 只要
    提交信息或代码注释里有中文就整段变空，模型据此得出「这仓库没有提交历史」
    之类的错误结论，且没有任何报错可循。

    因此这里固定按 UTF-8 优先解码，失败再退回本地编码（照顾 `dir`、`chcp`
    这类仍按 ANSI 输出的旧 Windows 命令），最后一道 errors="replace" 保证
    任何字节序列都能出结果——乱码远好过静默丢失。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _clip(text: str) -> str:
    """
    对多行文本做「头+尾」截断保护。

    行数不超过 RUN_HEAD+RUN_TAIL 时原样返回；否则取前 RUN_HEAD 行 +
    省略中间行数的提示 + 后 RUN_TAIL 行。

    :param text: 原始文本（如命令的 stdout）
    :returns: 截断后的文本（必要时含「…（省略中间 k 行）…」提示）
    """
    lines = text.splitlines()
    if len(lines) <= RUN_HEAD + RUN_TAIL:
        return text
    omitted = len(lines) - RUN_HEAD - RUN_TAIL
    head = lines[:RUN_HEAD]
    tail = lines[-RUN_TAIL:]
    return "\n".join(head + [f"…（省略中间 {omitted} 行）…"] + tail)


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
                return ToolResult(ok=False, output="缺少必填参数 command", summary="缺少参数 command")

            timeout = args.get("timeout") or DEFAULT_TIMEOUT

            # 显式固定 cwd，避免调用方未来改变进程目录后命令跑到工作区外。
            # 刻意不传 text=True：由 _decode 自己按 UTF-8 优先解码，
            # 否则中文输出会在 subprocess 的读取线程里解码失败并被静默吞成空串。
            proc = subprocess.run(
                command,
                shell=True,
                cwd=workspace_root(),
                capture_output=True,
                timeout=timeout,
            )

            stdout = _decode(proc.stdout)
            stderr = _decode(proc.stderr)
            out_lines = len(stdout.splitlines())
            err_lines = len(stderr.splitlines())

            # 回显命令 + 退出码 + 截断保护后的两路输出，便于模型阅读且不爆 token
            parts = [f"$ {command}", f"退出码: {proc.returncode}"]
            if stdout:
                parts.append(f"stdout（{out_lines} 行）:\n{_clip(stdout)}")
            if stderr:
                parts.append(f"stderr（{err_lines} 行）:\n{_clip(stderr)}")
            output = "\n".join(parts)

            ok = proc.returncode == 0
            if ok:
                summary = f"退出码 0 · 输出 {out_lines} 行"
            else:
                # 失败时摘要带上首行 stderr，方便用户一眼看到原因
                first_err = stderr.splitlines()[0] if stderr else ""
                summary = f"退出码 {proc.returncode}" + (f" · {first_err}" if first_err else "")

            # 退出码非 0 视为命令失败（ok=False），但仍把完整输出回灌供模型判断
            return ToolResult(ok=ok, output=output, summary=summary)

        except subprocess.TimeoutExpired:
            t = args.get("timeout") or DEFAULT_TIMEOUT
            return ToolResult(
                ok=False,
                output=f"命令执行超时（超过 {t} 秒）已被终止。",
                summary=f"超时（{t}s）",
            )
        except Exception as e:
            return ToolResult(ok=False, output=f"命令执行失败: {e}", summary="执行失败")
