"""
工具规范化层（spec F1 的输入端）：把一次具体的工具调用翻译成引擎认识的 PermissionRequest。

这是「引擎」与「具体工具」之间唯一知道工具细节的地方——不同工具参数名不同
（run_command 用 command、文件工具用 path、glob/grep 用 pattern），本模块把它们统一
映射到 (rule_name, specifier, kind)，使 engine.decide 对具体工具完全无感（spec N4/N5）。

新增工具若要纳入权限控制：在 _TOOL_MAP 加一行映射即可；未映射工具自动落到 "other"
分支（引擎只按工具名匹配整工具规则、并走模式兜底），不会漏过权限检查。
"""

from typing import Callable, Optional

from rhinecode.tools.base import Tool
from rhinecode.permission.models import PermissionMode, PermissionRequest

# 单个工具的映射规则：给定参数字典，返回 (rule_name, specifier, kind)。
_Mapper = Callable[[dict], tuple[str, str, str]]

# 工具名 → 映射函数。集中登记 6 个核心工具的「规则名 / 取哪个参数作 specifier / 种类」。
_TOOL_MAP: dict[str, _Mapper] = {
    # run_command：整条命令进①黑名单 + ③Bash 命令匹配。
    "run_command": lambda a: ("Bash", str(a.get("command") or ""), "command"),
    # read_file：读路径进②沙箱 + ③Read 路径匹配。
    "read_file": lambda a: ("Read", str(a.get("path") or ""), "read_path"),
    # grep_content：path 可选，缺省为当前目录 "."；归为只读 Read。
    "grep_content": lambda a: ("Read", str(a.get("path") or "."), "read_path"),
    # glob_files：pattern 作为路径模式，归为只读 Read，种类 glob。
    "glob_files": lambda a: ("Read", str(a.get("pattern") or ""), "glob"),
    # write_file：写路径进②沙箱 + ③Write 路径匹配。
    "write_file": lambda a: ("Write", str(a.get("path") or ""), "write_path"),
    # edit_file：改文件归为 Edit。
    "edit_file": lambda a: ("Edit", str(a.get("path") or ""), "write_path"),
}


def to_request(tool: Tool, args: Optional[dict], mode: PermissionMode) -> PermissionRequest:
    """
    把一次工具调用规范化为 PermissionRequest，作为 engine.decide 的输入。

    :param tool: 被调用的工具实例（提供 name 与 read_only）
    :param args: 模型给出的、已解析的参数字典（None 时按空字典处理）
    :param mode: 当前权限模式，原样写入请求供④层兜底
    :returns: 规范化后的 PermissionRequest

    映射规则见 _TOOL_MAP；未登记的工具落到 "other" 分支：rule_name 用工具自身的 name，
    specifier 为空，kind="other"（引擎只按工具名匹配整工具规则 + 走模式兜底）。

    副作用：无（纯数据转换）。
    """
    a = args if isinstance(args, dict) else {}
    mapper = _TOOL_MAP.get(tool.name)
    if mapper is not None:
        rule_name, specifier, kind = mapper(a)
    else:
        rule_name, specifier, kind = tool.name, "", "other"
    return PermissionRequest(
        tool_name=tool.name,
        rule_name=rule_name,
        specifier=specifier,
        kind=kind,
        is_read_only=tool.read_only,
        mode=mode,
    )
