"""
工具规范化层（spec F1 的输入端）：把一次具体的工具调用翻译成引擎认识的 PermissionRequest。

这是「引擎」与「具体工具」之间唯一知道工具细节的地方——不同工具参数名不同
（run_command 用 command、文件工具用 path、glob/grep 用 pattern），本模块把它们统一
映射到 (rule_name, specifier, kind)，使 engine.decide 对具体工具完全无感（spec N4/N5）。

新增工具若要纳入权限控制：在 _TOOL_MAP 加一行映射即可；未映射工具自动落到 "other"
分支（引擎只按工具名匹配整工具规则、并走模式兜底），不会漏过权限检查。

⚠ **成对维护点**：新增一种 `kind` 时，除了 `_TOOL_MAP`，**同文件的 `to_allow_rule`
也要跟着加分支**。漏改后者不报错——本次调用照常放行，要到下次启动才发现那条
「永久放行」写下的规则是废的。
"""

from pathlib import Path
from typing import Callable, Optional

from rhinecode.tools.base import Tool
from rhinecode.permission import network
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
    # web_fetch：完整 URL 进②′网络边界 + ③域名规则匹配。
    # specifier 刻意用**完整 URL 原文**而非主机名——确认面板与行为记录里要留下
    # 模型实际请求的那个地址；主机名另放在 PermissionRequest.host（见 to_request）。
    "web_fetch": lambda a: ("WebFetch", str(a.get("url") or ""), "url"),
    # ⚠ **c15 的五个协作工具刻意不在这张表里**，与 `run_agent` / `load_skill`
    # 同先例：它们既不读文件也不执行命令，没有可映射的 Bash / Read / Edit /
    # Write 语义，副作用限于改本进程内存里的清单与信箱。
    #
    # 别为了「看起来完整」给它们硬编一个类别——那会让 `deny: Write(...)`
    # 之类的路径规则意外命中一个根本不碰文件系统的工具。
    #
    # 不登记的实际后果：它们落 `other` 分支（`rule_name` 取工具自身的 name、
    # `specifier` 为空），因此只有**整工具规则**（不带括号的 `deny: send_message`）
    # 命中得了它们。带模式的写法（`deny: send_message(*)`）不命中——`other` 分支
    # 要求 `rule.pattern == ""`，这是既有语义，不是本次引入的。
    #
    # ⚠ 这里一度写着「登不登记对它们都不生效」——因为 `system_serial=True` 的工具
    # 在 `agent/loop.py` 的预扫里**直接拿 ALLOW、根本不调 `engine.decide`**。
    # 那个绕过已于 perm-system-serial-bypass 修掉：它们现在照常过一次引擎，
    # `deny` 规则**确实生效**（只是判 ASK 时按 ALLOW 处理，仍不弹确认面板）。
}


def to_request(
    tool: Tool,
    args: Optional[dict],
    mode: PermissionMode,
    cwd: Path,
) -> PermissionRequest:
    """
    把一次工具调用规范化为 PermissionRequest，作为 engine.decide 的输入。

    :param tool: 被调用的工具实例（提供 name 与 read_only）
    :param args: 模型给出的、已解析的参数字典（None 时按空字典处理）
    :param mode: 当前权限模式，原样写入请求供④层兜底
    :param cwd: **本次调用的工作目录**（c14 F2）。第②层路径沙箱据它判定边界。
                主对话与非隔离子 Agent 传主项目根，隔离子 Agent 传它的隔离工作区。
                **必填**——理由见 `PermissionRequest.cwd` 的说明
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

    # url 类额外填主机名，一次算好供三处消费：规则匹配、确认面板展示、行为记录。
    # 解析失败时留空串——后续 check_hard 会在②′层把这次请求拒掉，不必在这里报错。
    host = ""
    if kind == "url":
        try:
            _scheme, host, _port = network.split_url(specifier)
        except ValueError:
            host = ""

    return PermissionRequest(
        tool_name=tool.name,
        rule_name=rule_name,
        specifier=specifier,
        kind=kind,
        is_read_only=tool.read_only,
        mode=mode,
        cwd=cwd,
        host=host,
    )


def to_allow_rule(request: PermissionRequest) -> tuple[str, str]:
    """
    把一次权限请求翻译成「本会话放行 / 永久放行」要登记的规则 (工具名, 模式)。

    :param request: 规范化后的权限请求
    :returns: (rule_name, pattern)；pattern 为空串表示「匹配该工具全部调用」

    ## 这个函数为什么存在

    确认面板选「本会话」或「永久」时，需要把本次调用翻译成一条等价的 allow 规则。
    原先的写法是直接 `f"{rule_name}({specifier})"`，对命令类与路径类是对的，
    但对 url 类会写出：

        allow:
          - "WebFetch(https://example.com/a?token=abc)"

    三个问题一次凑齐：① 它不是合法的域名规则，下次启动会被加载期的校验处理掉——
    用户点过的「永久放行」**重启后凭空失效**；② 就算不被处理掉也匹配不上任何东西
    （`match_domain` 收到的模式是一整个 URL）；③ **查询参数被原样写进了配置文件**，
    而 URL 里可能带令牌。

    最难受的是**当场看不出来**：本次调用因为选了「永久」照常放行了，
    问题要到下次启动才显形。

    ## 各 kind 的翻译

    - `url`：取**主机名**，返回 `domain:<host>`。**不带路径、查询参数与端口**
      （spec F9）——域名规则本来就只匹配主机名，带上其余部分既不合法也会泄漏令牌。
    - 其余：返回 specifier 原文，**逐字等于本函数存在之前的行为**。

    ⚠ 与 `_TOOL_MAP` 是**成对维护点**：新增一种 kind 时两处都要加。

    副作用：无（纯数据转换）。
    """
    if request.kind == "url":
        # host 已由 to_request 归一化（小写、去末尾点）。取不到时退回空模式，
        # 那等价于「放行该工具全部调用」——比写一条废规则安全性更差，
        # 所以只在 host 确实非空时才生成 domain: 模式。
        if request.host:
            return request.rule_name, f"domain:{request.host}"
        return request.rule_name, ""
    return request.rule_name, request.specifier
