"""
内置命令登记（c10 T15–T19）：12 条规范命令、批准的全部别名与静态 /init 提示词。

设计要点（plan 12.5）：
- 处理函数只做「参数解释 + 控制器调用」，不导入 Textual、ConversationManager
  或具体 Manager 类型——能力判断（如 Provider 是否支持某模式）留在领域层，
  处理函数不复制这些判断（T16）；
- /help 处理函数通过闭包捕获同一个注册表实例，避免给 CommandController
  增加只为帮助服务的接口（plan 第 7 节）；
- build_builtin_registry() 显式构造并返回注册表，不在模块导入时隐式注册
  （便于测试与启动失败控制，plan 12.6）；register_many 一次性原子注册。
"""

from rhinecode.commands.models import (
    CommandController,
    CommandInvocation,
    CommandSpec,
    CommandType,
    ModeTarget,
    ReportTarget,
)
from rhinecode.commands.registry import CommandRegistry


# /init 命令的内置静态提示词（c9 F25 → c10 T18 迁入命令层）。
# /init 不是新造的分析引擎——它把这条指令作为一条普通 user 消息交给现有的
# Agent Loop：模型用 glob/grep/read 探索项目、用 write_file 落盘 RHINE.md，
# 写文件照常经过五层权限管线与人在回路确认（用户能先看 diff 再放行）。
# spec F28：仅支持代码内置的静态预设提示词，不做运行时动态生成或模板替换。
INIT_PROMPT = """\
请分析当前项目并生成（或改进）项目指令文件 RHINE.md。

RHINE.md 是每次会话启动时自动注入模型上下文的项目说明书，目标读者是「下一次\
进入本项目、对它一无所知的 AI 助手」。请按以下步骤执行：

1. 先检查项目根是否已存在 RHINE.md：
   - **已存在**：读取它，再探索项目对照现状，只输出一份具体的改进建议\
（缺了什么、过时了什么、哪些写法不够明确），**不要覆盖或修改现有文件**。
   - **不存在**：继续第 2 步。
2. 探索项目：用 glob/grep/read 了解目录结构、技术栈与依赖（如 pyproject.toml / \
package.json）、构建与测试命令、代码风格约定、关键架构分层。README 和已有文档优先参考。
3. 用 write_file 在**项目根**生成 RHINE.md，内容要求：
   - 用 Markdown 结构化组织：项目简介、技术栈、常用命令（构建/测试/运行）、\
架构概览、代码规范、注意事项；
   - 只写「代码里看不出来或很难看出来」的信息，不要复述显而易见的内容；
   - 具体可执行（写 "运行 python -m unittest discover -s tests" 而不是 "跑测试"）；
   - 控制在 200 行以内，宁缺毋滥。
"""


# ---------------------------------------------------------------------- #
# 只读报告处理函数（T15）
# ---------------------------------------------------------------------- #
def _handle_mcp(invocation: CommandInvocation, controller: CommandController) -> None:
    """/mcp：查询 MCP 连接状态报告并展示（纯只读，不改状态）。"""
    controller.show_message(controller.query_report(ReportTarget.MCP))


def _handle_context(invocation: CommandInvocation, controller: CommandController) -> None:
    """/context：查询上下文用量报告并展示（纯只读）。"""
    controller.show_message(controller.query_report(ReportTarget.CONTEXT))


def _handle_memory(invocation: CommandInvocation, controller: CommandController) -> None:
    """/memory：查询记忆系统状态报告并展示（纯只读）。"""
    controller.show_message(controller.query_report(ReportTarget.MEMORY))


# ---------------------------------------------------------------------- #
# 模式处理函数（T16）
# ---------------------------------------------------------------------- #
def _handle_think(invocation: CommandInvocation, controller: CommandController) -> None:
    """/think：循环切换思考模式。能力判断（Provider 是否支持）在领域层。"""
    controller.show_message(controller.switch_mode(ModeTarget.THINKING))
    controller.refresh_status()


def _handle_plan(invocation: CommandInvocation, controller: CommandController) -> None:
    """/plan：切换 Plan Mode 并立即刷新状态栏（[DEFAULT] ↔ [PLAN]，spec F31）。"""
    controller.show_message(controller.switch_mode(ModeTarget.PLAN))
    controller.refresh_status()


def _handle_perm(invocation: CommandInvocation, controller: CommandController) -> None:
    """/perm：循环切换权限模式并刷新状态栏。"""
    controller.show_message(controller.switch_mode(ModeTarget.PERMISSION))
    controller.refresh_status()


# ---------------------------------------------------------------------- #
# 压缩、恢复、清空与退出（T17）
# ---------------------------------------------------------------------- #
def _handle_compact(invocation: CommandInvocation, controller: CommandController) -> None:
    """
    /compact：触发手动上下文压缩。不创建用户消息（spec F17/N2 例外条款）；
    摘要 LLM 调用阻塞，由控制器交给后台 Worker（spec F18）。多余参数忽略（spec F12）。
    """
    controller.compact_context()


def _handle_resume(invocation: CommandInvocation, controller: CommandController) -> None:
    """
    /resume：空参数转换为 None（打开选择面板），非空参数原样传入（编号或 ID 直达恢复，
    spec F13）。参数内容不做任何解释——编号/ID 的解析在领域层。
    """
    controller.resume_session(invocation.arguments or None)


def _handle_clear(invocation: CommandInvocation, controller: CommandController) -> None:
    """
    /clear：清空对话（历史 + 聊天区 + 新会话档），显示兼容确认文本并刷新状态栏
    （/clear 会复位上下文估算，状态栏用量段需要立即归零）。多余参数忽略（spec F12）。
    """
    controller.clear_conversation()
    controller.show_message("对话历史已清空")
    controller.refresh_status()


def _handle_exit(invocation: CommandInvocation, controller: CommandController) -> None:
    """/exit：退出应用。经控制器接口退出，不抛 SystemExit 穿过命令层（T17）。"""
    controller.exit_application()


# ---------------------------------------------------------------------- #
# 提示词命令（T18）
# ---------------------------------------------------------------------- #
def _handle_init(invocation: CommandInvocation, controller: CommandController) -> None:
    """
    /init：把静态内置提示词作为用户请求交给正常 Agent 路径（spec F15 PROMPT 类）。

    双内容模型（spec F25–F27）：content 为完整展开提示词（模型语义历史），
    display_content 为用户实际输入（界面与恢复回放只显示原命令）。
    回显已由分发器统一完成，这里不再调用 show_user_input（plan 8.4）。
    """
    if not controller.tools_enabled:
        # 与 c9 行为兼容的能力限制提示；不发送任何消息。
        controller.show_message("当前 Provider 不支持 /init（需要 DeepSeek 工具模式）")
        return
    controller.send_user_message(INIT_PROMPT, display_content=invocation.raw_text.strip())


# ---------------------------------------------------------------------- #
# 注册表构建（T19）
# ---------------------------------------------------------------------- #
def build_builtin_registry() -> CommandRegistry:
    """
    构建并返回登记了全部内置命令的注册表（无导入副作用，调用时才注册）。

    登记内容与 plan 第 7 节的批准表格一致：12 条规范命令 + 8 个别名；
    仅 /resume 有参数提示；C10 全部内置命令 requires_argument=False。
    register_many 一次性原子注册：任一冲突则整批不生效（理论上内置表不冲突，
    这里主要为测试注入与未来命令来源保留同一失败语义）。

    :returns: 已完成注册的 CommandRegistry
    :raises CommandRegistrationError: 内置表出现名称/别名冲突（fail-fast，spec N4）
    """
    registry = CommandRegistry()

    def handle_help(invocation: CommandInvocation, controller: CommandController) -> None:
        """/help：展示可见命令的帮助（闭包捕获本注册表实例，plan 第 7 节）。"""
        controller.show_message(registry.render_help())

    registry.register_many(
        [
            CommandSpec(
                name="/help",
                aliases=("/h",),
                description="显示所有可用命令、别名与用法",
                usage="/help",
                command_type=CommandType.LOCAL,
                handler=handle_help,
            ),
            CommandSpec(
                name="/think",
                aliases=(),
                description="循环切换思考模式：关闭 → 高效 → 最强（Anthropic/DeepSeek 支持）",
                usage="/think",
                command_type=CommandType.UI,
                handler=_handle_think,
            ),
            CommandSpec(
                name="/plan",
                aliases=(),
                description="切换计划模式：先规划/澄清需求，审批后再执行（DeepSeek）",
                usage="/plan",
                command_type=CommandType.UI,
                handler=_handle_plan,
            ),
            CommandSpec(
                name="/perm",
                aliases=("/permissions", "/allowed-tools"),
                description="循环切换权限模式：默认 → 严格 → 放行（DeepSeek 工具模式）",
                usage="/perm",
                command_type=CommandType.UI,
                handler=_handle_perm,
            ),
            CommandSpec(
                name="/mcp",
                aliases=(),
                description="查看 MCP 服务连接状态（Server / 工具 / 失败原因）",
                usage="/mcp",
                command_type=CommandType.LOCAL,
                handler=_handle_mcp,
            ),
            CommandSpec(
                name="/context",
                aliases=("/ctx",),
                description="查看当前上下文用量（估算 token / 余量 / 已存盘数）",
                usage="/context",
                command_type=CommandType.LOCAL,
                handler=_handle_context,
            ),
            CommandSpec(
                name="/compact",
                aliases=(),
                description="压缩上下文：LLM 摘要早前对话，保留近期原文",
                usage="/compact",
                command_type=CommandType.LOCAL,
                handler=_handle_compact,
            ),
            CommandSpec(
                name="/memory",
                aliases=(),
                description="查看记忆系统状态（RHINE.md / 笔记 / 会话存档 / 锁）",
                usage="/memory",
                command_type=CommandType.LOCAL,
                handler=_handle_memory,
            ),
            CommandSpec(
                name="/resume",
                aliases=("/continue",),
                description="恢复历史会话：无参打开选择面板，带编号/ID 直接载入",
                usage="/resume [编号或ID]",
                command_type=CommandType.UI,
                handler=_handle_resume,
                argument_hint="[编号或ID]",
            ),
            CommandSpec(
                name="/init",
                aliases=(),
                description="分析项目并生成 RHINE.md 项目指令文件（DeepSeek 工具模式）",
                usage="/init",
                command_type=CommandType.PROMPT,
                handler=_handle_init,
            ),
            CommandSpec(
                name="/clear",
                aliases=("/reset", "/new"),
                description="清空当前对话历史并开启新会话存档",
                usage="/clear",
                command_type=CommandType.UI,
                handler=_handle_clear,
            ),
            CommandSpec(
                name="/exit",
                aliases=("/quit",),
                description="退出 RhineCode",
                usage="/exit",
                command_type=CommandType.UI,
                handler=_handle_exit,
            ),
        ]
    )
    return registry
