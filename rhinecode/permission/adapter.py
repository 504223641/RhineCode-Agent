"""
工具规范化层（spec F1 的输入端）：把一次具体的工具调用翻译成引擎认识的 PermissionRequest。

这是「引擎」与「具体工具」之间唯一知道工具细节的地方——不同工具参数名不同
（run_command 用 command、文件工具用 path、glob/grep 用 pattern），本模块把它们统一
映射到 (rule_name, specifier, kind)，使 engine.decide 对具体工具完全无感（spec N4/N5）。

新增工具若要纳入权限控制：在 _TOOL_MAP 加一行映射即可；未映射工具自动落到 "other"
分支（引擎只按工具名匹配整工具规则、并走模式兜底），不会漏过权限检查。

⚠ **成对维护点**：新增一种 `kind` 时，除了 `_TOOL_MAP`，**同文件的 `to_allow_rule`
也要跟着加分支**。漏改后者不报错——本次调用照常放行，要到下次启动才发现那条
「永久放行」写下的规则是废的。目前有三种 kind 需要在两处同步：`url`（写
`domain:` 模式）、`search`（写空模式）与 `launch`（写空模式）。

⚠ 新增 `kind` 时还要看第三处：`permission/engine.py` 第④层的模式兜底。
`url` / `search` / `launch` 在放行档下都判 ASK（各有各的理由文案），而其余种类
判 ALLOW——漏改那里的表现是「配了放行档之后搜索就再也不问了」，也不报错。
"""

from pathlib import Path
from typing import Callable, Optional

from rhinecode.tools.base import Tool
from rhinecode.permission import network
from rhinecode.permission.models import PermissionMode, PermissionRequest

# 单个工具的映射规则：给定参数字典，返回 (rule_name, specifier, kind)。
_Mapper = Callable[[dict], tuple[str, str, str]]

def _launch_specifier(args: dict) -> str:
    """
    把一次 `mcp_add_server` 调用压成一行「要启动什么」的人类可读描述。

    :param args: 模型给出的参数字典（`server_name` / `config`）
    :returns: 形如 `context7 · npx -y @upstash/context7-mcp`；取不到时尽量降级

    ## 为什么要有它

    `launch` 类的 specifier **不参与任何规则匹配**（它落 `rules._rule_matches`
    的「其它类」分支，那个分支只认 `rule.pattern == ""` 的整工具规则）。
    它唯一的消费方是**行为记录与原因展示**——而「那次委派到底把什么程序拉起来了」
    正是事后排查时唯一想知道的事。留空串的话记录里只剩一个工具名。

    ⚠ **刻意不截断。** 与 url / search 两类同一条理由：这段文字是用户判断
    放不放行的依据，截断意味着只要把危险部分放在可见范围之后，人在回路这一层
    就形同虚设。（确认面板另有专用展示行，见 `tui/widgets.ConfirmPanel`。）

    ⚠ **绝不抛异常。** 它跑在权限判定的入口上，参数是模型产出的任意 JSON——
    `config` 完全可能不是字典、`args` 完全可能不是列表。判定层抛异常会让
    整轮工具执行炸掉，而这里只是在拼一句给人看的话。

    副作用：无（纯数据转换）。
    """
    name = str(args.get("server_name") or "").strip()
    config = args.get("config")
    target = ""
    if isinstance(config, dict):
        command = config.get("command")
        url = config.get("url")
        if command:
            parts = [str(command)]
            extra = config.get("args")
            if isinstance(extra, (list, tuple)):
                parts.extend(str(one) for one in extra)
            target = " ".join(parts)
        elif url:
            target = str(url)
    if name and target:
        return f"{name} · {target}"
    return name or target


# 工具名 → 映射函数。集中登记各内置工具的「规则名 / 取哪个参数作 specifier / 种类」。
# ⚠ 这里刻意不写「共 N 个」——加一行映射就得回来改一次数字，而漏改不报错。
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
    # mcp_add_server：**启动外部程序类**（B4 修复）。
    #
    # ⚠ 它此前落在未映射的 `other` 兜底分支上，而那个分支在缺省预设（auto =
    # 放行档）下的结论是 `allow @ mode`——**六层防御一层都碰不到它**：
    # ①黑名单只认 `command`、②沙箱只认路径类、②′网络只认 `url`、
    # ②″保护路径第一行就是 `if request.kind != "write_path": return result`，
    # ③层要用户主动写下 `deny: mcp_add_server` 才拦得住。
    # 于是模型可以在一次调用里、不弹任何面板地写一条 `mcpServers` 配置
    # 并立刻把它拉起来，而 `command` 是**任意本地命令**。
    #
    # 这条洞不是新引入的，是**一条老承诺失去了兑现它的那一层**：
    # `tools/mcp_config.py` 的 docstring 写着「交给现有权限确认流程拦截」，
    # 那句话写于 C7——当时缺省档是 `DEFAULT`，④层对 `other` 类判 ASK，面板照弹。
    # auto-plan 扩展把缺省档换成 `PERMISSIVE` 之后，那一层就不再说话了。
    #
    # 修法因此是**把承诺还给④层**：新增一种 kind `launch`，在④层放行档下判 ASK
    # （与 `url` / `search` 两个既有例外同格，见 `engine._decide_core`）。
    # 选它而不是「出口收紧器」的理由见 `engine` 里那一支的注释。
    #
    # specifier 取「服务器名 · 将要执行的命令（或远端地址）」——它是行为记录里
    # 唯一能回答「那次到底启动了什么」的字段。规则匹配用不到它（`launch` 落
    # 「其它类」分支，只认不带括号的整工具规则），因此可以放人看的文本。
    "mcp_add_server": lambda a: ("mcp_add_server", _launch_specifier(a), "launch"),
    # web_search：完整查询词进③整工具规则匹配 + ④模式兜底（web_search 扩展 F10）。
    # specifier 用**完整查询词原文**——确认面板与行为记录里要留下模型实际搜了什么，
    # 那是用户放不放行的唯一依据（spec F7）。
    #
    # ⚠ 种类是新的 "search" 而不是复用 "url"：复用会让②′网络边界层拿查询词
    # 当地址去解析，`check_hard` 一律判「地址畸形」→ **每次搜索都被硬拒**。
    "web_search": lambda a: ("WebSearch", str(a.get("query") or ""), "search"),
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
    if request.kind == "launch":
        # ⚠ **必须返回空模式（整工具形式）**，理由与搜索类逐字相同：
        # `launch` 落规则匹配的「其它类」分支，那个分支只认 `rule.pattern == ""`。
        # 返回 `("mcp_add_server", "context7 · npx …")` 会写出一条
        # **永远不会命中任何东西的废规则**——用户点了「永久放行」，
        # 下次添加 MCP 还是弹面板，而配置文件里明明躺着一条他亲手点出来的规则。
        #
        # ⚠ 与搜索类不同的是：`launch` 的「永久放行」是**诚实的**。
        # 写下的 `allow: mcp_add_server` 下次启动属于文件规则集，在③层命中并
        # **短路④层**（本层的 ASK 就是④层给的），所以那个按钮真的会生效。
        # 分类器的宽泛规则丢弃（F16）只处理 `Bash` 与 `WebSearch` 两类，
        # 不碰这条。因此确认面板对 `launch` 照常给四个选项——
        # 别顺手把它加进 `no_permanent`。
        return request.rule_name, ""
    if request.kind == "search":
        # ⚠ **必须返回空模式（整工具形式）。**
        #
        # 搜索类落规则匹配的「其它类」分支，而那个分支只认 `rule.pattern == ""`
        # （见 `rules._rule_matches` 最后一行）。返回
        # `("WebSearch", "<查询词>")` 会写出一条**永远不会命中任何东西的废规则**
        # ——与 `WebFetch(https://…?token=abc)` 完全同形，而那正是本函数
        # 被造出来的原因。
        #
        # 顺带一条：查询词写进配置文件本身也是泄漏（它可能含用户的私密问题），
        # 与 url 类不写查询参数是同一条理由。
        #
        # ⚠ 注意本函数只服务「本会话放行」——搜索类的确认面板**不提供
        # 「永久放行」**（web_search 扩展 F14）：那个选项写的是文件级规则，
        # 而启用分类器时 `allow: WebSearch` 会被 F16 丢弃，用户会看到
        # 「点了永久放行，下次还是弹」。
        return request.rule_name, ""
    return request.rule_name, request.specifier
