"""
`load_skill` 工具——两阶段加载的第二阶段入口（c11 T28）。

**两阶段加载在解决什么**（spec F7）：Skill 的完整 SOP 正文可能有几百行。
如果启动时把所有 Skill 的正文都注入系统提示，装十个 Skill 就吃掉几万 token，
而一次对话通常只用得上其中一个。

于是拆成两阶段：
- **第一阶段**（启动，稳定通道）：只注入名字 + 一句话说明，模型知道「有什么可用」；
- **第二阶段**（按需，本工具）：模型判断该用某个 Skill 时调本工具，
  完整 SOP 才被拉进上下文。

**本工具是系统级的**（spec F8）：它不受任何 Skill 的 `allowed_tools` 白名单约束
（在 `ToolPolicy.exempt` 里恒常存在）。否则模型激活第一个 Skill 之后，
若那个 Skill 的白名单没写 `load_skill`，它就再也加载不了第二个 Skill 了。

依赖方向：`tools/load_skill.py → skills/manager.py`。本模块**不由
`tools/registry.py` 导入**（`ToolRegistry.default()` 里没有它），而是由
`__main__` 在构造出 `SkillManager` 之后单独注册——这样 `tools` 与 `skills`
的包级互依不会成环（详见 `tools/__init__.py` 的说明）。
"""

from rhinecode.skills.manager import SkillManager
from rhinecode.skills.models import ActivationStatus, DegradeKind
from rhinecode.tools.base import Tool, ToolResult

# 两种降级形态给模型的说明。措辞面向模型的下一步决策：
# 被截断时它该意识到自己可能只拿到半份流程；整段没注入时它压根不该假装执行。
_DEGRADE_NOTE = {
    DegradeKind.TRUNCATED: (
        "注意：该 Skill 的正文超出单体上限已被截断，你可能只看到前半部分流程。"
        "执行到看不见后续步骤时，请如实告知用户而不要自行编造剩余步骤。"
    ),
    DegradeKind.DROPPED: (
        "注意：当前已激活的 Skill 正文合计超出总量上限，该 Skill 的指令"
        "**本次没有被注入**，你不会看到它的内容。请告知用户先卸载部分 Skill"
        "（/skills off <名字>）后重试。"
    ),
}


class LoadSkillTool(Tool):
    """
    按名加载一个共享模式 Skill 的完整指令。

    **`read_only = True` 的两个后果，改动此标志前必须同时评估**：

    1. **免确认**——这是它在默认权限模式下不弹确认面板的唯一前提。
       `permission/engine.py` 的只读简化分支会在规则未命中时对只读工具直接 ALLOW，
       根本不进权限模式层。若改成 False，模型每加载一个 Skill 用户都要按一次确认，
       两阶段加载的体验优势就没了。
    2. **进只读并发桶**——Agent 循环会把只读工具放进 `ThreadPoolExecutor` 并发执行。
       模型同一轮发两次 `load_skill` 时，两个线程会并发跑 `SkillManager.activate()`
       里的读-改-写序列。**这就是 `SkillManager` 内部必须加锁的原因。**

    另：本工具**有意不在 `permission/adapter.py` 的 `_TOOL_MAP` 中登记**。
    未登记的工具落进 `other` 分支，仍然要走规则层（可用 `deny: load_skill` 禁掉）
    与权限模式兜底，不存在漏检。而它没有任何可映射的 Bash/Read/Edit/Write 语义——
    它既不读文件也不执行命令，只是改一个内存状态。强行映射成 `Read(...)` 之类
    反而会让权限规则的语义变得莫名其妙。
    """

    name = "load_skill"
    read_only = True
    description = (
        "加载一个 Skill 的完整操作指令。当用户的请求与某个已列出的 Skill 匹配时调用它，"
        "该 Skill 的完整 SOP 会被注入你的上下文，此后你按其步骤执行。\n"
        "只能加载**共享模式**的 Skill；独立模式的 Skill 需要由用户主动触发，"
        "你调用本工具会得到失败提示与应当建议用户执行的命令。\n"
        "可用的 Skill 名字见系统提示中的 Skill 清单。重复加载同一个 Skill 是安全的"
        "（只会更新其参数）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "要加载的 Skill 名字（不带斜杠），须来自系统提示的 Skill 清单",
            },
            "arguments": {
                "type": "string",
                "description": (
                    "传给该 Skill 的参数，原样代入其 $ARGUMENTS 占位符。"
                    "通常是用户本次请求里与该 Skill 相关的具体内容。可省略。"
                ),
            },
        },
        "required": ["name"],
    }

    def __init__(self, manager: SkillManager) -> None:
        """
        :param manager: Skill 编排者。本工具不持有任何自己的状态，
                        全部委托给它——激活列表的唯一真相在 manager 里。
        """
        self._manager = manager

    def execute(self, args: dict) -> ToolResult:
        """
        激活指定的 Skill。

        :param args: 含 `name`（必填）与 `arguments`（可选）
        :returns: 三态结果——
                  ACTIVATED → ok=True，**只回一句简短确认**；
                  NOT_FOUND → ok=False，附可用名字列表；
                  ISOLATED → ok=False，附用户实际可执行的入口

        **成功时为什么不把正文回灌给模型**：正文已经通过系统提示的
        「已激活 Skill」槽位注入了，且**每轮都在**。在工具结果里再回一份，
        等于同一段文本在上下文里存两份，纯浪费——而且工具结果是一次性的，
        随着对话变长会被上下文压缩挪走，系统提示槽位才是持久的那份。

        副作用：修改 `SkillManager` 的激活列表；可能触发状态栏刷新回调。
        本方法自行兜底全部异常（Tool 契约要求绝不向上抛）。
        """
        try:
            name = args.get("name")
            if not isinstance(name, str) or not name.strip():
                return ToolResult(
                    ok=False,
                    output="参数 name 缺失或为空，请给出要加载的 Skill 名字。",
                    summary="缺少 name",
                )
            name = name.strip()

            raw_args = args.get("arguments")
            arguments = raw_args if isinstance(raw_args, str) else ""

            result = self._manager.activate(name, arguments)

            if result.status is ActivationStatus.ACTIVATED:
                output = f"已激活 Skill `{name}`，其完整指令已注入你的上下文，请按其步骤执行。"
                if result.degrade is not None:
                    output += "\n" + _DEGRADE_NOTE[result.degrade]
                return ToolResult(ok=True, output=output, summary=f"激活 {name}")

            if result.status is ActivationStatus.NOT_FOUND:
                available = (
                    "、".join(result.available_names)
                    if result.available_names
                    else "（当前没有任何可用 Skill）"
                )
                return ToolResult(
                    ok=False,
                    output=(
                        f"没有名为 `{name}` 的 Skill。\n可用的 Skill：{available}"
                    ),
                    summary=f"未找到 {name}",
                )

            # ISOLATED：模型不能自行发起独立模式（它会开一条新的子对话，
            # 必须由用户显式触发）。给出用户实际可执行的入口——
            # entry_hint 由 manager 保证指向真实存在的命令。
            return ToolResult(
                ok=False,
                output=(
                    f"Skill `{name}` 是独立模式，需要由用户主动触发，你无法加载它。\n"
                    f"请建议用户执行：{result.entry_hint}"
                ),
                summary=f"{name} 需用户触发",
            )
        except Exception as exc:  # noqa: BLE001 —— Tool 契约：绝不向上抛
            return ToolResult(
                ok=False, output=f"加载 Skill 失败：{exc}", summary="加载失败"
            )
