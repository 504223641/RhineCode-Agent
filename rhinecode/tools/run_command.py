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
from rhinecode.tools.path_guard import PathGuardError, require_cwd as _require_cwd

# 命令执行的默认超时（秒）。超过则终止子进程并返回超时错误。
# 定义为模块常量，便于后续统一调整；本章不暴露为 YAML 配置项。
DEFAULT_TIMEOUT = 30

# 输出截断保护：单路输出（stdout/stderr）行数超过 RUN_HEAD+RUN_TAIL 时，
# 只保留前 RUN_HEAD 行与后 RUN_TAIL 行，中间以省略提示替代，避免长输出爆 token。
RUN_HEAD = 30
RUN_TAIL = 10


def decode_subprocess_output(raw: bytes) -> str:
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

    **本函数是公开的，因为 c12 的 Hook 命令动作复用它**（`hooks/actions.py`）。
    那里同样要起子进程读输出，同样会撞上这个坑。各写一份是典型的
    「改一处漏一处」——而漏改的表现正是上面描述的「输出凭空消失且不报错」。
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
    # ⚠ **「别拿它替代专用工具」这条必须写在这里，不能只写在系统提示里。**
    #
    # 系统提示的「工具使用」模块早就写了「读文件用 read_file、找文件用 glob_files」，
    # 但实测模型仍然会用 `cat` / `head` / `find` / `grep` 读东西——因为它**选工具那一刻
    # 读的是工具描述**，不是几千 token 之前的那段规则。
    #
    # 这与 CLAUDE.md 里记的那一串「同一条约束要在两处同口径」是同一个坑
    # （Skill 清单 ↔ load_skill.description、角色清单 ↔ run_agent.description、
    # 交付信息 ↔ run_agent.description）——前几次也都是真实模型实测才发现的。
    #
    # 代价：绕开专用工具意味着绕开路径沙箱的逐次判定、绕开结果的结构化摘要，
    # 而且那些输出还会被当成命令输出原样铺在界面上。
    description = (
        "在项目工作目录下执行一条 shell 命令，返回标准输出、标准错误与退出码。"
        "用于运行测试、构建、启动脚本、查看环境等。\n"
        "**不要用它替代专用工具**：读文件用 `read_file`，按文件名找文件用 `glob_files`，"
        "按内容搜索用 `grep_content`。除非用户明确要求，否则不要用它跑 "
        "`cat` / `head` / `tail` / `find` / `grep` / `ls` / `echo` 这类命令——"
        "专用工具更快、结果更结构化，也不会把大段原文铺进对话。\n"
        "**能一条命令做完的不要拆成多条**：无依赖的命令用 `&&` 连起来一次发完；"
        "只有当后一条真的依赖前一条的输出时才分开。\n"
        "命令有超时限制，请避免执行交互式或长时间挂起的命令。"
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
    # c14：本工具碰路径/起子进程，必须知道调用者的工作目录。
    workspace_aware = True
    primary_arg = "command"

    def execute(self, args: dict, cwd=None) -> ToolResult:
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
            #
            # ⚠ `stdin=DEVNULL` 不可省，两条独立理由：
            #
            # 1. **不让子进程抢用户的键盘**。不给 stdin 的话子进程会**继承**我们的
            #    终端输入句柄，一个等输入的命令（`git commit` 开编辑器、
            #    `npm init`……）会和 Textual 的输入读取器抢同一批按键，
            #    而且要一直卡到 timeout；
            # 2. **不让子进程改掉控制台输入模式**。Windows 上控制台输入模式是
            #    **整个控制台共享**的属性，`shell=True` 起的 `cmd.exe` 拿到的若是
            #    真控制台句柄，它对模式的改动会留给我们——一旦
            #    `ENABLE_PROCESSED_INPUT` 被重新打开，此后的 `Ctrl+C` 就从「按键」
            #    变成 `CTRL_C_EVENT`/`SIGINT`，连按两次退出的判定被整个跳过。
            #    指到 NUL 之后子进程的标准输入不再是控制台，也就碰不到那个模式。
            #    （`tui/app.py` 的 `_install_sigint_guard` 是同一问题的另一半兜底。）
            #
            # 刻意不传 text=True：由 decode_subprocess_output 自己按 UTF-8 优先解码，
            # 否则中文输出会在 subprocess 的读取线程里解码失败并被静默吞成空串。
            proc = subprocess.run(
                command,
                shell=True,
                cwd=_require_cwd(cwd),
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )

            stdout = decode_subprocess_output(proc.stdout)
            stderr = decode_subprocess_output(proc.stderr)
            out_lines = len(stdout.splitlines())
            err_lines = len(stderr.splitlines())

            # 回显命令 + 退出码 + 截断保护后的两路输出，便于模型阅读且不爆 token
            parts = [f"$ {command}", f"退出码: {proc.returncode}"]
            if stdout:
                parts.append(f"stdout（{out_lines} 行）:\n{_clip(stdout)}")
            if stderr:
                parts.append(f"stderr（{err_lines} 行）:\n{_clip(stderr)}")
            output = "\n".join(parts)

            # 同一份内容的**未裁剪**版本，只供行为记录（见 ToolResult.full_output）。
            # 模型仍然只拿到上面那份裁剪版——token 预算的考量一个字没变；
            # 变的是「被省掉的中间那些行不再凭空消失」。
            #
            # 只在**真的裁剪了**的时候才构造：没超行数时两份完全相同，
            # 多存一份纯属让记录文件白白翻倍。
            full_parts = [f"$ {command}", f"退出码: {proc.returncode}"]
            if stdout:
                full_parts.append(f"stdout（{out_lines} 行）:\n{stdout}")
            if stderr:
                full_parts.append(f"stderr（{err_lines} 行）:\n{stderr}")
            full = "\n".join(full_parts)
            full_output = full if full != output else None

            ok = proc.returncode == 0
            if ok:
                summary = f"退出码 0 · 输出 {out_lines} 行"
            else:
                # 失败时摘要带上首行 stderr，方便用户一眼看到原因
                first_err = stderr.splitlines()[0] if stderr else ""
                summary = f"退出码 {proc.returncode}" + (f" · {first_err}" if first_err else "")

            # 退出码非 0 视为命令失败（ok=False），但仍把输出回灌供模型判断
            return ToolResult(ok=ok, output=output, summary=summary, full_output=full_output)

        except subprocess.TimeoutExpired:
            t = args.get("timeout") or DEFAULT_TIMEOUT
            return ToolResult(
                ok=False,
                output=f"命令执行超时（超过 {t} 秒）已被终止。",
                summary=f"超时（{t}s）",
            )
        except Exception as e:
            return ToolResult(ok=False, output=f"命令执行失败: {e}", summary="执行失败")
