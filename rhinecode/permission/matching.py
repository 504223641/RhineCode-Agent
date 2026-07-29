"""
匹配算法层：被①黑名单、②′网络边界与③规则共用的模式匹配算法。

本模块是纯字符串/正则运算，无任何外部依赖与副作用，可被单测充分覆盖（spec N5）。
四个公开函数：
- split_commands：把复合命令拆成子命令（防 `safe && rm -rf` 整条蒙混过关，c6 spec F2）。
- match_command：命令模式匹配（前缀 + glob + 词边界，对应 Bash 规则，c6 spec F4）。
- match_path：文件路径模式匹配（gitignore 风格，对应 Read/Edit/Write 规则，c6 spec F4）。
- match_domain：域名模式匹配（对应 WebFetch(domain:...) 规则，web_fetch 扩展 spec F11）。

跨平台（c6 spec N8）：match_path 先把反斜杠归一化为正斜杠，并以大小写不敏感匹配，
以适配 Windows 路径语义。
"""

import re

# 复合命令分隔符正则：多字符操作符必须排在单字符之前，否则 `&&` 会被 `&` 抢先切坏。
# 覆盖 spec 列出的 &&、||、|&、;、|、&、换行（\n/\r）。
_SEPARATOR_RE = re.compile(r"&&|\|\||\|&|;|\||&|\n|\r")


def split_commands(command: str) -> list[str]:
    """
    把一条可能的复合命令按 shell 分隔符拆成多个子命令。

    用途：黑名单/规则匹配前先拆段，保证 `git status && rm -rf /` 这种「前半安全、
    后半危险」的命令里每一段都被独立检查，任一段命中危险即可整条拦截（spec F2/AC2）。

    实现说明：这里做的是「朴素拆分」，不解析引号。这对安全是偏保守（fail-safe）的——
    宁可多拆几段、多检查几次，也不放过藏在分隔符后的危险子命令。

    :param command: 原始命令字符串（可能含多个子命令）
    :returns: 去空白后的非空子命令列表；输入为空时返回空列表
    """
    if not command:
        return []
    parts = _SEPARATOR_RE.split(command)
    return [seg.strip() for seg in parts if seg.strip()]


def _command_pattern_to_regex(pattern: str) -> "re.Pattern[str]":
    """
    把一条命令模式（Bash 规则的括号内容）编译成正则。

    匹配语义（对齐 Claude Code 的 Bash 规则）：
    - 末尾 ` *`（空格星号）或等价的 `:*` 后缀：带「词边界」的前缀匹配——
      `ls *` / `ls:*` 匹配 `ls` 本身或 `ls <任意>`，但不匹配 `lsof`（不会粘连成新词）。
    - 其它位置的 `*`：普通通配，匹配任意字符序列（含空格），可跨多个参数，
      如 `git * main` 匹配 `git checkout main`。
    - 无 `*`：精确匹配整条命令。

    :param pattern: 命令模式，如 "git *"、"git * main"、"npm run build"
    :returns: 已编译的正则，供 fullmatch 使用
    """
    # 末尾 `:*` 归一化为 ` *`（两者等价，且冒号只在末尾作通配语义）。
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + " *"

    # 识别末尾 ` *` 的「词边界前缀」语义：核心 = 去掉末尾 ` *` 的部分。
    trailing_prefix = pattern.endswith(" *")
    core = pattern[:-2] if trailing_prefix else pattern

    # 转义核心里的正则元字符，再把其中的 `*`（转义后是 \*）还原成通配 .*。
    core_regex = re.escape(core).replace(r"\*", ".*")

    if trailing_prefix:
        # 词边界：核心之后要么结束，要么跟一个空格再接任意内容 → 不会粘连成 `lsof`。
        body = core_regex + r"(?: .*)?"
    else:
        body = core_regex
    return re.compile(r"^" + body + r"$")


def match_command(pattern: str, command: str) -> bool:
    """
    判断一条命令是否匹配给定的命令模式（Bash 规则）。

    :param pattern: 规则模式（括号内容）；空串表示「匹配该工具所有命令」
    :param command: 待匹配的命令字符串
    :returns: 命中返回 True

    注意：本函数对单条命令做整体匹配，不负责拆分复合命令；调用方（规则层）按需
    先用 split_commands 拆段后逐段调用本函数。
    """
    if pattern == "":
        return True
    return _command_pattern_to_regex(pattern).fullmatch(command.strip()) is not None


def _normalize_path(p: str) -> str:
    """把路径归一化为匹配用形式：反斜杠转正斜杠、去掉开头的 `./`（spec N8 跨平台）。"""
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _path_body_to_regex(pattern: str) -> str:
    """
    把一条含 `/` 的路径模式翻译成正则主体（不含 ^ $ 锚点）。

    gitignore 风格语义：
    - `**/`：跨任意层目录（含零层），故 `**/.env` 同时匹配 `.env` 与 `a/b/.env`
    - `**`（结尾）：匹配其后任意深度内容，如 `src/**` 匹配 `src/a/b.py`
    - `*`：段内通配，不跨越 `/`
    - 其余字符按字面（正则转义）

    :param pattern: 已归一化、已去前导锚点的路径模式
    :returns: 正则主体字符串
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        if pattern[i] == "*":
            if pattern[i : i + 2] == "**":
                if pattern[i : i + 3] == "**/":
                    # `**/` → 可选的「任意层目录」，使零层时也能匹配根下文件
                    out.append(r"(?:.*/)?")
                    i += 3
                else:
                    # 结尾或独立的 `**` → 任意内容（可跨目录）
                    out.append(r".*")
                    i += 2
            else:
                # 单 `*` → 段内通配（不跨 `/`）
                out.append(r"[^/]*")
                i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def match_path(pattern: str, path: str) -> bool:
    """
    判断一个文件路径是否匹配给定的路径模式（Read/Edit/Write 规则，gitignore 风格）。

    规则：
    - 裸文件名（模式不含 `/`，可带 `*`）：匹配路径的「basename」，等价任意深度命中。
      例：`.env` 命中 `src/.env`；`*.env` 命中 `a/b/c.env`。
    - 含 `/` 的模式：按路径整体匹配。模式以 `/` 开头表示锚定到根（去掉该 `/`），
      否则同样从头锚定（gitignore 中含斜杠模式默认锚定）。`**`/`*` 语义见 _path_body_to_regex。

    跨平台：路径与模式都先归一化（反斜杠→正斜杠），并大小写不敏感匹配（适配 Windows，spec N8）。

    :param pattern: 规则模式（括号内容）；空串表示「匹配该工具所有路径」
    :param path: 待匹配的文件路径或 glob 模式串
    :returns: 命中返回 True
    """
    if pattern == "":
        return True

    norm_pattern = _normalize_path(pattern)
    norm_path = _normalize_path(path)

    if "/" not in norm_pattern:
        # 裸文件名：匹配 basename，任意深度。
        basename = norm_path.rsplit("/", 1)[-1]
        # 转义除 * 外的元字符：先按 * 切，逐段 escape 再用 [^/]* 连接。
        parts = [re.escape(seg) for seg in norm_pattern.split("*")]
        seg_regex = "[^/]*".join(parts)
        return re.fullmatch(seg_regex, basename, re.IGNORECASE) is not None

    # 含斜杠：锚定匹配整条路径。
    anchored = norm_pattern.lstrip("/")
    body = _path_body_to_regex(anchored)
    return re.fullmatch(body, norm_path, re.IGNORECASE) is not None


def _normalize_domain(text: str) -> str:
    """
    把域名模式或主机名归一化为匹配用形式：去两侧空白、转小写、去掉末尾的 `.`。

    末尾点要去掉，是因为 `example.com.` 与 `example.com` 在 DNS 里指同一个域
    （前者是「完全限定域名」的书写形式）。不归一化的话，一条 `allow` 规则会被
    多写/少写一个点绕过。

    :param text: 域名模式或主机名
    :returns: 归一化后的字符串
    """
    return text.strip().lower().rstrip(".")


def match_domain(pattern: str, host: str) -> bool:
    """
    判断一个主机名是否匹配给定的域名模式（WebFetch 规则，spec F11）。

    :param pattern: 规则模式，即 `WebFetch(domain:<模式>)` 里去掉 `domain:` 前缀后的部分；
                    空串表示「匹配该工具的所有调用」（与 match_command / match_path 同口径）
    :param host: 待匹配的主机名（不含协议、端口、路径与查询参数）
    :returns: 命中返回 True

    匹配语义（逐条对齐 Claude Code，便于用户迁移既有配置）：

    | 模式             | 匹配                                   | 不匹配                          |
    |------------------|----------------------------------------|---------------------------------|
    | `example.com`    | `example.com`                          | `api.example.com`               |
    | `*.example.com`  | `api.example.com`、`a.b.example.com`   | 裸域 `example.com` 本身         |
    | `*`              | 一切主机名                             | —                               |
    | `example.*`      | `example.org`（`*` 取到 `org`）        | `example.evil.com`（需跨越一点）|

    **最后一行是安全要求而非风格选择。** 若非前导位置的 `*` 允许跨点匹配，
    一条本意为「放行 example 各国域名」的 `example.*` 会连带放行攻击者可以自行注册的
    `example.evil.com`。因此除**前导 `*.`** 与**单独一个 `*`** 这两种写法外，
    `*` 一律只匹配「两个点之间」的一段文本（正则用 `[^.]*`，与 match_path 里
    单个 `*` 不跨 `/` 是同一手法）。

    大小写不敏感，模式与主机名末尾的 `.` 都先行去除（见 _normalize_domain）。

    副作用：无。
    """
    if pattern == "":
        return True

    norm_pattern = _normalize_domain(pattern)
    norm_host = _normalize_domain(host)

    # 归一化后模式为空（例如原文只有一个 "."）视为匹配全部，与空模式同口径。
    if norm_pattern == "" or norm_pattern == "*":
        return True

    # 前导 `*.`：匹配任意深度子域，但**不匹配裸域本身**。
    # 用 endswith("." + 基域) 而不是正则，是因为这条语义与「段数」无关，
    # 只要求「以 .<基域> 结尾」，写成字符串判断比正则更难写错。
    if norm_pattern.startswith("*."):
        base = norm_pattern[2:]
        if base == "":
            return True
        return norm_host.endswith("." + base)

    # 其余：把模式按 `*` 切段，逐段转义后用 `[^.]*` 连接（不跨点），整体锚定匹配。
    parts = [re.escape(seg) for seg in norm_pattern.split("*")]
    body = "[^.]*".join(parts)
    return re.fullmatch(body, norm_host) is not None
