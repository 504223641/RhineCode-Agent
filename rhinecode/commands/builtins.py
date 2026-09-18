"""
内置命令登记（c10 T15–T19）：全部规范命令、别名与静态 /init 提示词。

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
    controller.show_report(controller.query_report(ReportTarget.MCP))


def _handle_hooks(invocation: CommandInvocation, controller: CommandController) -> None:
    """/hooks：查看已加载的 Hook 规则、加载警告与本次运行的触发统计（纯只读，c12）。"""
    controller.show_report(controller.query_report(ReportTarget.HOOKS))


# `/agents` 的用法串，多个分支要用，抽出来避免各处写得不一致（照 `_SKILLS_USAGE` 先例）。
_AGENTS_USAGE = "用法：/agents [cancel <任务标识|all>]"


def _handle_tasks(
    invocation: CommandInvocation, controller: CommandController
) -> None:
    """
    /tasks：查看共享任务清单（c15 F26）。

    纯只读，无子命令——**任务的增删改由模型通过工具做，不由用户敲命令做**。
    给用户一个改清单的入口会造成两条并行的写路径，而其中一条（命令层）
    绕开了「谁改的」这个记录，清单上会出现无从追溯的变更。

    未启用协作能力时给出明确说明而不是空白（空白会让用户以为命令坏了）。

    副作用：向聊天区输出一段文本。
    """
    controller.show_report(controller.query_report(ReportTarget.TASKS))


def _handle_agents(invocation: CommandInvocation, controller: CommandController) -> None:
    """
    /agents：查看子 Agent 角色与本次运行的任务，或取消任务（c13 F24）。

    三种形态：

    | 输入 | 行为 |
    |---|---|
    | `/agents` | 只读报告：全部角色（来源层、工具集、模型、轮次、权限档位声明值与生效值）、加载错误、被覆盖的定义、本次运行的任务列表 |
    | `/agents cancel <标识>` | 取消指定任务 |
    | `/agents cancel all` | 取消全部未完成任务 |

    子命令按**首个空白**切分（沿用 C10 的 `split(maxsplit=1)` 口径）。

    **本章不做 `reload`**：与 c12 同口径，改了角色定义要重启。
    """
    raw = invocation.arguments.strip()

    if not raw:
        controller.show_report(controller.query_report(ReportTarget.AGENTS))
        return

    parts = raw.split(maxsplit=1)
    sub = parts[0].casefold()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if sub == "cancel":
        # 无参与 `all` 都表示「全部」——`None` 交给下游统一处理，
        # 这里不把 `all` 翻译成别的东西，免得两处对「全部」的表示不一致。
        target = None if rest.casefold() in ("", "all") else rest
        controller.show_message(controller.cancel_subagents(target))
        # 取消会改变运行中的任务数，状态栏要跟着刷新。
        controller.refresh_status()
        return

    controller.show_message(f"未知子命令：{parts[0]}\n{_AGENTS_USAGE}")


def _handle_context(invocation: CommandInvocation, controller: CommandController) -> None:
    """/context：查询上下文用量报告并展示（纯只读）。"""
    controller.show_report(controller.query_report(ReportTarget.CONTEXT))


def _handle_memory(invocation: CommandInvocation, controller: CommandController) -> None:
    """/memory：查询记忆系统状态报告并展示（纯只读）。"""
    controller.show_report(controller.query_report(ReportTarget.MEMORY))


# `/skills` 的用法串，多个分支要用，抽出来避免各处写得不一致。
_SKILLS_USAGE = "用法：/skills [reload | off [名字] | run <名字> [参数] | prompt]"


def _handle_skills(invocation: CommandInvocation, controller: CommandController) -> None:
    """
    /skills：Skill 的列表 / 热更新 / 卸载 / 执行 / 查看注入（c11 F26/F31）。

    五种形态：

    | 输入 | 行为 |
    |---|---|
    | `/skills` | 只读报告：全部 Skill 及其来源、模式、激活状态、加载错误 |
    | `/skills prompt` | 只读报告：当前**实际注入**了什么（清单 / 正文 / 可见工具集） |
    | `/skills reload` | 热更新定义，刷新状态栏 |
    | `/skills off [名字]` | 卸载指定或全部激活的 Skill，刷新状态栏 |
    | `/skills run <名字> [参数]` | 执行指定 Skill（独立模式 Skill 的通用入口） |

    子命令按**首个空白**切分（沿用 C10 的 `split(maxsplit=1)` 口径），
    其后内容原样保留、不做 shell 分词。
    """
    raw = invocation.arguments.strip()

    if not raw:
        controller.show_report(controller.query_report(ReportTarget.SKILLS))
        return

    parts = raw.split(maxsplit=1)
    sub = parts[0].casefold()
    rest = parts[1] if len(parts) > 1 else ""

    if sub == "prompt":
        controller.show_report(controller.query_report(ReportTarget.SKILLS_PROMPT))
        return

    if sub == "reload":
        controller.show_message(controller.reload_skills())
        # 热更新可能自动卸载消失的 Skill，激活数会变，状态栏要跟着刷新。
        controller.refresh_status()
        return

    if sub == "off":
        # `off` 之后 strip 非空即视为名字，内部不再切分——Skill 名按 F4 不含空白，
        # 多词输入必然落到「未激活」分支，给出的提示也是对的。
        name = rest.strip() or None
        controller.show_message(controller.deactivate_skill(name))
        controller.refresh_status()
        return

    if sub == "run":
        if not rest.strip():
            controller.show_message(f"请指定要执行的 Skill 名字。{_SKILLS_USAGE}")
            return
        run_parts = rest.strip().split(maxsplit=1)
        name = run_parts[0]
        # 参数原样保留（含内部多余空白），与 C10 的命令参数口径一致。
        args = run_parts[1] if len(run_parts) > 1 else ""
        controller.run_skill(name, args, invocation.raw_text.strip())
        return

    controller.show_message(f"未知子命令 `{parts[0]}`。{_SKILLS_USAGE}")


# ---------------------------------------------------------------------- #
# 模式处理函数（T16）
# ---------------------------------------------------------------------- #
def _handle_think(invocation: CommandInvocation, controller: CommandController) -> None:
    """/think：循环切换思考模式。能力判断（Provider 是否支持）在领域层。"""
    controller.show_message(controller.switch_mode(ModeTarget.THINKING))
    controller.refresh_status()


def _handle_mode(invocation: CommandInvocation, controller: CommandController) -> None:
    """
    /mode：在 `auto` 与 `plan` 两个预设间循环，并立即刷新状态栏（[AUTO] ↔ [PLAN]）。

    与 `Shift+Tab` 走**同一个**领域方法，两条入口行为逐字相同（auto-plan F5/F6）。
    `/plan` 是它的别名——老用户的肌肉记忆不断。
    """
    controller.show_message(controller.switch_mode(ModeTarget.PRESET))
    controller.refresh_status()


# auto-plan 扩展：`_handle_perm` 与 `/perm` 的 CommandSpec 已整体删除。
# 权限档不再有运行期切换入口，改由 `permissions.yaml` 的规则与角色定义的
# `permission_mode` 字段决定（手法对齐 Claude Code 的 `dontAsk`：档位仍然存在、
# 可被显式指定，只是永不进用户的切换循环）。


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


def _handle_setup(invocation: CommandInvocation, controller: CommandController) -> None:
    """
    /setup：重新走一遍配置向导（first-run-setup 扩展 F15）。

    向导自己负责一切（预填、拉模型清单、终验、写盘），本处只负责把它打开。
    多余参数忽略——它没有子命令，而报一个「参数错误」只会挡住用户。

    ⚠ **成对维护点**：本处理函数与注册项 ↔ `CommandController.open_setup`
    ↔ `tui/app.py` 的实现，三处缺一不可，漏了不报错。
    """
    controller.open_setup()


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

    登记内容以 c10 plan 第 7 节的批准表格为起点，其后各章陆续增删
    （c12 `/hooks`、c13 `/agents`、c15 `/tasks`、auto-plan 用 `/mode` 取代
    `/plan` 与 `/perm`）。**这里刻意不写条数**——原文写着「12 条 + 8 个别名」，
    而实测早已不是那个数：一个会静默漂移的计数比没有计数更容易误导人。
    仅 /resume 有参数提示；全部内置命令 requires_argument=False。
    register_many 一次性原子注册：任一冲突则整批不生效（理论上内置表不冲突，
    这里主要为测试注入与未来命令来源保留同一失败语义）。

    :returns: 已完成注册的 CommandRegistry
    :raises CommandRegistrationError: 内置表出现名称/别名冲突（fail-fast，spec N4）
    """
    registry = CommandRegistry()

    def handle_help(invocation: CommandInvocation, controller: CommandController) -> None:
        """/help：展示可见命令的帮助（闭包捕获本注册表实例，plan 第 7 节）。"""
        controller.show_report(registry.render_help())

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
                name="/mode",
                aliases=("/plan",),
                # 「互换」一律用 `↔`（符号白名单 F29）：此处原为 `⇄`，是全项目唯一一处
        # ——`[AUTO] ↔ [PLAN]`、「逐条 ↔ 全文」、「折叠 ↔ 逐条」用的都是 `↔`。
        # 两个符号一个意思，正是白名单「语义互不重叠」那条判据要挡的形态。
        description="切换运行模式：auto（放手干活）↔ plan（先规划再执行）；等价于 Shift+Tab",
                usage="/mode",
                command_type=CommandType.UI,
                handler=_handle_mode,
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
                name="/hooks",
                aliases=(),
                description="查看已加载的 Hook 规则、加载警告与本次触发统计",
                usage="/hooks",
                command_type=CommandType.LOCAL,
                handler=_handle_hooks,
            ),
            CommandSpec(
                name="/agents",
                aliases=(),
                description="查看子 Agent 角色与本次运行的任务，或取消任务",
                usage="/agents [cancel <任务标识|all>]",
                command_type=CommandType.LOCAL,
                handler=_handle_agents,
            ),
            CommandSpec(
                name="/tasks",
                aliases=("/board",),
                description="查看队员共用的共享任务清单（编号/状态/认领人/阻塞来源）",
                usage="/tasks",
                command_type=CommandType.LOCAL,
                handler=_handle_tasks,
            ),
            CommandSpec(
                name="/context",
                aliases=("/ctx",),
                description="查看当前上下文用量（估算 token / 距上限余量）",
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
                description="查看记忆系统状态（RHINE.md / 记忆 / 会话存档 / 锁）",
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
                name="/skills",
                aliases=(),
                description="管理 Skill：列表 / 热更新 / 卸载 / 执行 / 查看注入",
                usage="/skills [reload|off [名字]|run <名字> [参数]|prompt]",
                command_type=CommandType.LOCAL,
                handler=_handle_skills,
                argument_hint="[子命令]",
            ),
            CommandSpec(
                name="/setup",
                aliases=(),
                description="重新配置 API Key、接口地址与模型（下次启动生效）",
                usage="/setup",
                command_type=CommandType.UI,
                handler=_handle_setup,
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
