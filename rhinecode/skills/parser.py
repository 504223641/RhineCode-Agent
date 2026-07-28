"""
单份 Skill 文本 → SkillSpec 的解析。

**职责**：把一个 Skill 文件的完整文本（可选的 YAML frontmatter + Markdown 正文）
解析成 `SkillSpec`，或给出一条可读的中文失败原因。

**纯函数，不碰文件系统**：读文件、遍历目录、决定去哪里找文件、**推导命令名**，
全部由 `discovery.py` 负责；本模块只处理「已经拿到的一段文本」。这样解析规则可以
用字符串字面量直接测试，无需造临时目录。命令名由调用方算好后传进来。

## 本轮改造要点（对齐 Agent Skills 开放标准）

1. **全部字段可选**——一份没有 frontmatter、只有正文的 Markdown 也是合法 Skill，
   说明从正文第一个非空段落提取。标准如此，外部 Skill 才能原样搬进来。
2. **键名连字符与下划线都认**（标准用连字符，YAML 使用者习惯下划线）。
3. **`allowed_tools`（下划线）触发一条明确的语义变更警告**——它在旧版本里是
   「收窄可见工具集」，现在是「免确认」，两者**相反**。静默沿用会让同一份文件
   在新旧版本下行为相反而用户毫不知情，这是本次改造唯一「静默会造成实际损害」
   的迁移点，故单独处理、措辞不可淡化。
4. **名字的字符集与长度校验整段删除**——命令名来自文件系统，不来自这里。
"""

from typing import Optional

import yaml

from rhinecode.skills.models import (
    UNSUPPORTED_FIELDS,
    SkillSource,
    SkillSpec,
)

# frontmatter 的分隔线。首行必须是它（允许之前有空白行），
# 向下找到第二条同样的线为止，中间是 YAML，之后是正文。
_FENCE = "---"

# YAML 1.1 之外，标准还允许这些布尔字面量（大小写不敏感）。
# `yaml.safe_load` 已经把 true/false/yes/no/on/off 转成 bool，
# 这里兜的是被引号包住、或写成 1/0 字符串的情形。
_TRUE_LITERALS = frozenset({"true", "yes", "on", "1"})
_FALSE_LITERALS = frozenset({"false", "no", "off", "0"})


def _normalize_keys(front: dict) -> tuple[dict, list[str]]:
    """
    把 frontmatter 的键名归一到下划线形态。

    :param front: 原始 frontmatter 映射
    :returns: `(归一后的映射, 提示列表)`

    同一逻辑键的两种写法同时出现时**以连字符版为准**（那是标准写法），
    并产出一条提示——两种写法并存多半是复制粘贴时的疏漏，
    静默取其一会让用户以为另一个生效了。

    副作用：无（返回新字典）。
    """
    notices: list[str] = []
    out: dict = {}
    # 先放下划线版，再让连字符版覆盖它，从而实现「连字符优先」
    for key, value in front.items():
        if not isinstance(key, str) or "-" in key:
            continue
        out[key] = value
    for key, value in front.items():
        if not isinstance(key, str) or "-" not in key:
            continue
        normalized = key.replace("-", "_")
        if normalized in out and out[normalized] != value:
            notices.append(
                f"同时出现 `{key}` 与 `{normalized}` 两种写法且取值不同，"
                f"按标准写法 `{key}` 为准"
            )
        out[normalized] = value
    return out, notices


def _as_bool(value, default: bool) -> bool:
    """
    宽松地把 frontmatter 取值读成布尔。

    :param value: 原始取值（可能已被 YAML 转成 bool，也可能是字符串）
    :param default: 取值缺失或无法识别时的缺省
    :returns: 布尔值

    无法识别时**返回缺省而不是报错**：一个布尔字段写错不该让整个 Skill 用不了。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in _TRUE_LITERALS:
        return True
    if text in _FALSE_LITERALS:
        return False
    return default


def _split_outside_parens(text: str) -> list[str]:
    """
    按空白与逗号切分，但**括号内的分隔符不算数**。

    :param text: 形如 `Bash(git add *) Bash(git commit *), Read` 的声明串
    :returns: 切分后的条目列表

    ⚠️ 不能直接 `text.split()`：标准里最常见的写法恰恰是
    `allowed-tools: Bash(git add *) Bash(git commit *)`——括号内**必然含空格**，
    朴素切分会把一条声明劈成 `Bash(git` 与 `add` 与 `*)` 三段，
    然后三段都因为「不认识的工具类别」被丢掉，而用户只会看到「预授权没生效」。

    副作用：无。
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and (ch.isspace() or ch == ","):
            if buf:
                parts.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _as_tool_list(value) -> tuple[str, ...]:
    """
    把 `allowed-tools` 的取值读成字符串元组。

    :param value: YAML 列表，或空格/逗号分隔的字符串
    :returns: 去重且**保序**的元组

    标准明确允许两种写法（列表与分隔串），两种都要认——外部 Skill 里两种都常见。
    保序是为了让 `/skills` 的展示与用户写在文件里的顺序一致，排查时不必来回对照。

    副作用：无。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw = _split_outside_parens(value)
    elif isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        return ()

    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _first_paragraph(body: str) -> str:
    """
    从正文提取第一个非空段落，作为 `description` 的缺省值。

    :param body: SOP 正文
    :returns: 第一段文本（单行化）；提取不到时返回空串

    跳过 Markdown 标题行（`#` 开头）——标题通常只是 Skill 名字的重复，
    拿它当说明对模型判断「什么时候该用这个 Skill」毫无帮助。

    副作用：无。
    """
    paragraph: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith("#"):
            continue
        paragraph.append(stripped)
    return " ".join(paragraph)


def parse_skill(
    text: str,
    path,
    source: SkillSource,
    command_name: str,
    resource_dir=None,
    resource_files: tuple[str, ...] = (),
) -> tuple[Optional[SkillSpec], Optional[str], list[str]]:
    """
    解析一份 Skill 文本。

    :param text: 文件完整内容（已按 UTF-8 解码）
    :param path: 该文本的来源路径，只用于填进 `entry_path`，本函数不读它
    :param source: 来源层级
    :param command_name: **由发现层从路径推导好的命令名**，本函数原样填入
    :param resource_dir: 目录型 Skill 的目录；单文件型传 None
    :param resource_files: 目录型的随附文件相对路径清单
    :returns: `(spec, reason, warnings)` 三元组。
              **`spec` 与 `reason` 恰有一个非 None**。
              `warnings` 与 `spec.notices` 内容相同——前者供发现层做「被覆盖
              就不发出」的过滤，后者随 spec 一路带到状态报告。

    副作用：无。不读写文件、不改全局状态。
    """
    notices: list[str] = []

    # ── 第一步：切出 frontmatter 与正文 ──
    #
    # 用 splitlines(keepends=True) 而不是 split("\n")：前者能正确处理 \r\n 与
    # 文件末尾有无换行的差异，重新 join 时能还原原文（正文里的缩进和空行要原样保留，
    # 它是要发给模型的 SOP，格式即语义）。
    lines = text.splitlines(keepends=True)

    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1

    front: dict = {}
    if start < len(lines) and lines[start].strip() == _FENCE:
        end = None
        for i in range(start + 1, len(lines)):
            if lines[i].strip() == _FENCE:
                end = i
                break
        if end is None:
            return None, "YAML frontmatter 未闭合（缺少第二条 --- 分隔线）", []

        try:
            # safe_load 而非 load：Skill 文件可能来自团队仓库甚至第三方，
            # 绝不能允许 YAML 里的任意对象构造。
            parsed = yaml.safe_load("".join(lines[start + 1 : end]))
        except yaml.YAMLError as exc:
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else "未知错误"
            return None, f"frontmatter 解析失败：{first}", []

        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            return None, "frontmatter 顶层必须是键值映射", []
        front = parsed
        body = "".join(lines[end + 1 :])
    else:
        # **没有 frontmatter 也是合法 Skill**（标准如此）：整份文件都是正文。
        body = "".join(lines[start:])

    # ── 第二步：正文非空 ──
    #
    # 正文是要发给模型的 SOP 指令，空正文的 Skill 没有任何意义，
    # 加载了反而会占一个命令名并让用户困惑「为什么执行了什么都没发生」。
    if not body.strip():
        return None, "SOP 正文为空", []

    # ── 第三步：键名归一 ──
    #
    # ⚠️ 归一**之前**先留一份原始键名：归一后连字符形态全部变成下划线，
    # 就再也分不出「用户写的是标准的 allowed-tools」还是「旧的 allowed_tools」了，
    # 而下面那条语义变更告知恰恰依赖这个区分。
    original_keys = {k for k in front if isinstance(k, str)}
    front, key_notices = _normalize_keys(front)
    notices.extend(key_notices)

    # ── 第四步：逐字段读取 ──

    # display_name：仅展示标签，缺省回填命令名。
    raw_name = front.get("name")
    display_name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else command_name

    # description：缺省从正文第一段提取。**不再是必填**。
    raw_desc = front.get("description")
    if isinstance(raw_desc, str) and raw_desc.strip():
        description = raw_desc.strip()
    else:
        description = _first_paragraph(body) or f"（无说明）{command_name}"

    # when_to_use：可选，拼在 description 之后进清单。
    raw_when = front.get("when_to_use")
    when_to_use = raw_when.strip() if isinstance(raw_when, str) and raw_when.strip() else None

    # allowed-tools：预授权声明。
    granted_tools = _as_tool_list(front.get("allowed_tools"))

    # **语义变更的显式告知**：只有用户写的是下划线形态才提示——
    # 连字符形态是标准写法，作者本来就是按预授权语义写的，无需提醒。
    if "allowed_tools" in original_keys and "allowed-tools" not in original_keys and granted_tools:
        notices.append(
            "检测到 `allowed_tools`（下划线写法）。**该字段的语义已变更**："
            "旧版本中它表示「收窄模型可见的工具集」，现在表示「列出的操作在本次执行内"
            "免于人工确认」，两者作用相反。当前按新语义（免确认）处理，"
            "请确认这符合你的本意；若想限制模型能做什么，请用 permissions.yaml 的 deny 规则。"
        )

    # context：取值 fork 时开子对话。
    raw_context = front.get("context")
    forked = False
    if raw_context is not None:
        text_context = str(raw_context).strip().casefold()
        if text_context == "fork":
            forked = True
        else:
            notices.append(f"`context` 取值 `{raw_context}` 不认识，按留在主对话处理")

    # 两个可调用性开关。
    model_invocable = not _as_bool(front.get("disable_model_invocation"), False)
    user_invocable = _as_bool(front.get("user_invocable"), True)

    # model：仅 fork 生效。
    raw_model = front.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else None
    if model is not None and not forked:
        notices.append(
            f"声明的 model `{model}` 将被忽略（只有 `context: fork` 的 Skill 才独立开子对话，"
            "留在主对话时共用主对话的模型）"
        )

    # ── 第五步：无对应能力的标准字段，逐条告知 ──
    #
    # **不能静默忽略**：作者写 `background: true` 的预期是「后台跑、不阻塞」，
    # 实际却同步阻塞跑完，这个差异用户不知道就会误判 Skill 的行为。
    for field, behaviour in UNSUPPORTED_FIELDS.items():
        if field not in front:
            continue
        # background 只有取真值时才与本版本行为不同；取假正是本版本的行为。
        if field == "background" and not _as_bool(front.get(field), True):
            continue
        notices.append(f"`{field}`：{behaviour}")

    # 其余未知键一律忽略，不失败也不提示。这是刻意的向前兼容策略：
    # 读到更新版本写的 Skill 时应当尽量把它用起来，而不是因为多了个键就整个拒绝。

    return (
        SkillSpec(
            command_name=command_name,
            display_name=display_name,
            description=description,
            when_to_use=when_to_use,
            body=body,
            granted_tools=granted_tools,
            forked=forked,
            model_invocable=model_invocable,
            user_invocable=user_invocable,
            model=model,
            source=source,
            entry_path=path,
            resource_dir=resource_dir,
            resource_files=resource_files,
            notices=tuple(notices),
        ),
        None,
        # **同一批 notices 也作为 warnings 返回**：发现层已经有一套「只发出生效
        # 那份的警告」的机制（低优先层被覆盖时它的警告不该发出去，否则用户会被
        # 指去改一个根本没生效的文件）。复用它，而不是另建一条 notices 通路。
        list(notices),
    )
