"""
Skill 短命令工厂（c11 T40）：`SkillCommandInfo` → `CommandSpec`。

**职责**：把 skills 层产出的中立描述转成命令层的 `CommandSpec`，
使每个 Skill 自动获得一条 `/<name>` 斜杠短命令（spec F25）。

**依赖方向**：`commands → skills.models`，**单向**。
skills 包不认识 `CommandSpec`、不认识 commands 包，它只产出
`SkillCommandInfo` 这个中立结构。转换这件事发生在 commands 这一侧，
于是 skills 仍是一个不被任何层反向依赖的叶子包。
"""

from rhinecode.commands.models import (
    CommandController,
    CommandInvocation,
    CommandSpec,
    CommandType,
)
from rhinecode.skills.models import SkillCommandInfo


def _make_handler(info: SkillCommandInfo):
    """
    为一条 Skill 造一个处理函数，用**工厂函数**捕获 info。

    :param info: 该 Skill 的中立描述
    :returns: 符合 `CommandHandler` 协议的处理函数

    **为什么必须用工厂函数而不是在循环里直接 def**：
    Python 的闭包捕获的是**变量**（cell）而不是值。写成

        for info in infos:
            def handler(inv, ctrl):
                ctrl.run_skill(info.name, ...)   # ← 捕获的是循环变量本身
            specs.append(CommandSpec(name=f"/{info.name}", handler=handler, ...))

    所有 handler 捕获的是**同一个 cell**，循环结束后它指向最后一条 info；
    而 `CommandSpec(name=f"/{info.name}")` 是即时求值的、名字是对的。

    失败形态因此格外恶心：补全菜单和 `/help` 显示 `/commit`，用户敲下去
    执行的却是 `review`——不崩溃、不报错，只是静默跑错 Skill。

    也不用默认参数写法（`def h(inv, ctrl, info=info)`）：它虽然与
    `CommandHandler = Callable[[CommandInvocation, CommandController], None]`
    兼容（调用方只传两个位置参数），但把第三个参数暴露在签名上，
    读代码的人会困惑它是不是协议的一部分。
    """

    def handler(invocation: CommandInvocation, controller: CommandController) -> None:
        # display 传用户敲的原始输入：界面回显与会话回放显示它，
        # 而模型看到的是 conversation 层生成的自包含文本（C10 双内容模型）。
        controller.run_skill(
            info.name, invocation.arguments, invocation.raw_text.strip()
        )

    return handler


def build_skill_command_specs(
    infos: "list[SkillCommandInfo] | tuple[SkillCommandInfo, ...]",
) -> list[CommandSpec]:
    """
    把一批 Skill 描述转成命令定义（spec F25）。

    :param infos: skills 层产出的中立描述，顺序即命令注册顺序
    :returns: `CommandSpec` 列表，交给 `CommandRegistry.replace_skill_commands`

    两处刻意的设计：

    - **`command_type` 复用 `PROMPT` 而不新增枚举值**：语义与 `/init` 同类
      （把预设内容作为用户请求交给正常 Agent 路径），复用可以避免触发
      「新增 `CommandType` 值 → 三处同步」这个成对维护点。
    - **`aliases=()` 是显式不变量**，不是「暂时没做」。
      `replace_skill_commands` 的 probe 逻辑依赖「每个 skill spec 只有一个标识」
      这一点才在当前实现下不出问题（见其 docstring）；将来若要放开别名，
      必须同步复查那段逻辑。

    副作用：无（纯转换）。
    """
    specs: list[CommandSpec] = []
    for info in infos:
        specs.append(
            CommandSpec(
                name=f"/{info.name}",
                aliases=(),
                description=info.description,
                usage=f"/{info.name} [参数]",
                command_type=CommandType.PROMPT,
                handler=_make_handler(info),
                argument_hint="[参数]",
            )
        )
    return specs
