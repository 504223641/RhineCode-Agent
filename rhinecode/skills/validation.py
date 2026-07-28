"""
把 Skill 的 `allowed-tools` 声明翻译成权限规则（对齐改造 F11）。

## 本模块的职责在本轮改造中整个换掉了

C11 时它做的是「白名单两段校验与空集降级」——那套语义（收窄模型可见的工具集）
已被移除。现在它只做一件事：**把预授权声明解析成 `Rule`**，交给权限引擎的
第③层，效果是「列出的操作在本次执行内免于人工确认」。

## 为什么能直接复用既有的规则体系

本系统的权限规则名恰好就是 `Bash` / `Read` / `Write` / `Edit`
（见 `permission/adapter.py` 的 `_TOOL_MAP`），与 Agent Skills 标准里的工具名
**逐字相同**。所以 `allowed-tools: Bash(git add *)` 可以原封不动地当成一条
`allow` 规则，连括号里的 glob 模式语法都一致，不需要任何翻译层。

只有两处需要映射：标准把只读检索拆成了 `Glob` 与 `Grep` 两个工具名，
而本系统的 `glob_files` / `grep_content` 都归到 `Read` 类别下。

## 无法识别的项：跳过 + 警告，**不 fail-fast**

这与 C11「内置工具名笔误就 fail-fast」的取舍**正好相反**，理由是来源不同：
那时白名单是自家格式，写错就是笔误；现在声明可能来自 Claude Code 或 Codex，
里面出现本系统没有的工具名（`Task`、`WebFetch`、`TodoWrite`……）是**正常现象**，
不该让程序起不来。
"""

from typing import Iterable

from rhinecode.permission.config import parse_rule_string
from rhinecode.permission.models import Rule
from rhinecode.skills.models import MCP_PREFIX, SkillSpec

# 预授权规则的来源标记。出现在拒绝/放行原因里，让用户能分辨
# 「这条放行是某个 Skill 给的」而不是自己配的。
GRANT_SOURCE = "skill"

# 标准工具名 → 本系统的权限规则名。
#
# 绝大多数是**恒等映射**——两边用的就是同一批名字。表里真正做事的只有
# Glob / Grep 两行：标准把只读检索拆成两个工具，本系统的 glob_files 与
# grep_content 都归在 Read 类别下（见 permission/adapter.py 的 _TOOL_MAP）。
#
# 同时收下本系统的内部工具名（read_file 等）：用户可能照着 /skills 里看到的
# 工具名来写，那时报「不认识」纯属自找麻烦。
_TOOL_ALIASES: dict[str, str] = {
    # 标准词汇
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "bash": "Bash",
    "glob": "Read",
    "grep": "Read",
    # 本系统的内部工具名
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "run_command": "Bash",
    "glob_files": "Read",
    "grep_content": "Read",
}


def _split_declaration(item: str) -> tuple[str, str]:
    """
    把一条声明拆成「工具名」与「括号内的模式」。

    :param item: 形如 `Bash(git add *)` 或 `Read` 的声明
    :returns: `(工具名, 模式)`；无括号时模式为空串

    副作用：无。
    """
    item = item.strip()
    if item.endswith(")") and "(" in item:
        idx = item.index("(")
        return item[:idx].strip(), item[idx + 1 : -1].strip()
    return item, ""


def grants_for(specs: Iterable[SkillSpec]) -> tuple[list[Rule], list[str]]:
    """
    把若干 Skill 的 `allowed-tools` 声明翻译成一批 allow 规则。

    :param specs: 本次执行涉及的 Skill（通常是刚被触发的那一个，
                  但一次执行中可能有多个先后触发，故接受序列）
    :returns: `(规则列表, 警告列表)`

    副作用：无（纯函数）。

    ⚠️ 产出的规则一律 `effect="allow"`。**这里不产生 deny**——预授权只能放宽，
    不能收紧；想限制模型能做什么请用 `permissions.yaml` 的 deny 规则，
    那才是安全边界。
    """
    rules: list[Rule] = []
    warnings: list[str] = []

    for spec in specs:
        for item in spec.granted_tools:
            tool, pattern = _split_declaration(item)
            if not tool:
                continue

            # MCP 工具名原样放行：它们在权限引擎里走 `other` 分支，
            # 按工具名做 fnmatch 通配匹配，`mcp__server__*` 这种写法直接可用。
            if tool.startswith(MCP_PREFIX):
                mapped = tool
            else:
                mapped = _TOOL_ALIASES.get(tool.casefold(), "")

            if not mapped:
                warnings.append(
                    f"Skill `{spec.command_name}` 的 allowed-tools 声明了 `{item}`，"
                    f"本系统没有对应的工具类别，该项被忽略"
                    f"（可用的类别：Read / Write / Edit / Bash，或 mcp__ 开头的远端工具）"
                )
                continue

            rule = parse_rule_string(
                f"{mapped}({pattern})" if pattern else mapped,
                effect="allow",
                source=GRANT_SOURCE,
            )
            if rule is not None:
                rules.append(rule)

    return rules, warnings
