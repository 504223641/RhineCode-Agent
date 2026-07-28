"""
单份 Skill 文本 → SkillSpec 的解析（c11 T6/T7）。

**职责**：把一个 Skill 文件的完整文本（YAML frontmatter + Markdown 正文）
解析成 `SkillSpec`，或给出一条可读的中文失败原因。

**纯函数，不碰文件系统**：读文件、遍历目录、决定去哪里找文件，全部由
`discovery.py` 负责；本模块只处理「已经拿到的一段文本」。这样解析规则可以
用字符串字面量直接测试，无需造临时目录。

对应 spec 条款：F1（frontmatter 六个字段）、F4（名字规则与保留子命令词）、
F5（单文件解析失败不阻断整体）、F22（共享模式声明 model 只警告不失败）。
"""

from typing import Optional

import yaml

from rhinecode.skills.models import (
    NAME_PATTERN,
    RESERVED_SUBCOMMANDS,
    SkillMode,
    SkillSource,
    SkillSpec,
)

# frontmatter 的分隔线。首行必须是它（允许之前有空白行），
# 向下找到第二条同样的线为止，中间是 YAML，之后是正文。
_FENCE = "---"


def parse_skill(
    text: str,
    path,
    source: SkillSource,
    resource_dir=None,
    resource_files: tuple[str, ...] = (),
) -> tuple[Optional[SkillSpec], Optional[str], list[str]]:
    """
    解析一份 Skill 文本。

    :param text: 文件完整内容（已按 UTF-8 解码）
    :param path: 该文本的来源路径，只用于填进 SkillSpec.entry_path，本函数不读它
    :param source: 来源层级
    :param resource_dir: 目录型 Skill 的目录；单文件型传 None
    :param resource_files: 目录型的随附文件相对路径清单
    :returns: `(spec, reason, warnings)` 三元组。
              **`spec` 与 `reason` 恰有一个非 None**——成功时 `reason is None`，
              失败时 `spec is None` 且 `reason` 是可读中文原因。
              `warnings` 是非致命提示列表，**成功时也可能非空**（如 F22 的
              共享模式声明 model），失败时恒为空列表。

    副作用：无。不读写文件、不改全局状态。
    """
    warnings: list[str] = []

    # ── 第一步：切出 frontmatter 与正文 ──
    #
    # 用 splitlines(keepends=True) 而不是 split("\n")：前者能正确处理 \r\n 与
    # 文件末尾有无换行的差异，重新 join 时能还原原文（正文里的缩进和空行要原样保留，
    # 它是要发给模型的 SOP，格式即语义）。
    lines = text.splitlines(keepends=True)

    # 允许 frontmatter 之前有空白行（有些编辑器会在文件开头留一行）。
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1

    if start >= len(lines) or lines[start].strip() != _FENCE:
        return None, "缺少 YAML frontmatter（文件须以 --- 开头）", []

    # 向下找第二条分隔线。
    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == _FENCE:
            end = i
            break

    if end is None:
        return None, "YAML frontmatter 未闭合（缺少第二条 --- 分隔线）", []

    front_text = "".join(lines[start + 1 : end])
    body = "".join(lines[end + 1 :])

    # ── 第二步：解析 YAML ──
    #
    # 用 safe_load 而非 load：Skill 文件可能来自团队仓库甚至第三方，
    # 绝不能允许 YAML 里的任意对象构造（safe_load 只产出基本类型）。
    try:
        front = yaml.safe_load(front_text)
    except yaml.YAMLError as exc:
        # 异常信息可能很长（含多行上下文），只取首行让报告保持可读。
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else "未知错误"
        return None, f"frontmatter 解析失败：{first_line}", []

    # 空 frontmatter（`---\n---\n`）时 safe_load 返回 None，也归入本分支。
    if not isinstance(front, dict):
        return None, "frontmatter 顶层必须是键值映射", []

    # ── 第三步：正文非空 ──
    #
    # 正文是要发给模型的 SOP 指令，空正文的 Skill 没有任何意义，
    # 加载了反而会占一个名字并让用户困惑「为什么激活了什么都没发生」。
    if not body.strip():
        return None, "SOP 正文为空", []

    # ── 第四步：逐字段校验 ──

    # name：必填，且必须能安全地拼成斜杠短命令。
    #
    # 「缺失」与「类型不对」要分开报，原因是 YAML 1.1 会把一批裸词解析成布尔值：
    # `name: off` 得到的是 False 而不是字符串 "off"。若两种情况都报「缺少 name」，
    # 用户看着自己明明写了 name 却被告知没写，根本无从下手。符合 NAME_PATTERN
    # 又会被 YAML 吃掉的词有 y / yes / n / no / true / false / on / off，
    # 这类名字必须加引号，提示里直接把办法说出来。
    if "name" not in front:
        return None, "缺少必填字段 name", []
    name = front.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, (
            "name 必须是非空字符串"
            "（注意 YAML 会把 on/off/yes/no/true/false 等裸词解析成布尔值，"
            "这类名字需要加引号写成 name: \"off\"）"
        ), []
    name = name.strip()
    if not NAME_PATTERN.match(name):
        return None, (
            f"名字 `{name}` 不合法"
            "（须小写字母开头、仅含小写字母数字连字符、不超过 32 字符）"
        ), []
    if name in RESERVED_SUBCOMMANDS:
        # 这些词是 `/skills` 的子命令。若允许同名 Skill，`/skills run xxx`
        # 与「名叫 run 的 Skill」就会产生解析二义。
        return None, f"名字使用了保留子命令词 `{name}`", []

    # description：必填。它是第一阶段清单里模型唯一能看到的信息，
    # 缺了它这个 Skill 就等于对模型不可见。
    description = front.get("description")
    if not isinstance(description, str) or not description.strip():
        return None, "缺少必填字段 description", []
    description = description.strip()

    # allowed_tools：可选。缺省 None = 不收窄工具集。
    raw_tools = front.get("allowed_tools")
    allowed_tools: Optional[tuple[str, ...]]
    if raw_tools is None:
        allowed_tools = None
    elif not isinstance(raw_tools, list) or not all(
        isinstance(t, str) for t in raw_tools
    ):
        # 常见笔误是写成一个逗号分隔的字符串。明确报错好过默默把整串当成一个工具名，
        # 后者会在启动校验时报出一个匪夷所思的「工具不存在」。
        return None, "allowed_tools 必须是字符串列表", []
    else:
        # 去重但**保序**：顺序对语义无影响，但保序能让 `/skills` 的展示与用户
        # 写在文件里的顺序一致，排查时不必来回对照。
        seen: set[str] = set()
        deduped: list[str] = []
        for tool in raw_tools:
            tool = tool.strip()
            if tool and tool not in seen:
                seen.add(tool)
                deduped.append(tool)
        allowed_tools = tuple(deduped)

    # mode：可选，缺省共享。
    raw_mode = front.get("mode", "shared")
    if raw_mode not in ("shared", "isolated"):
        return None, f"mode 只能是 shared 或 isolated，实际是 `{raw_mode}`", []
    mode = SkillMode.SHARED if raw_mode == "shared" else SkillMode.ISOLATED

    # history_messages：可选，缺省 0。
    #
    # 必须显式排除 bool：Python 里 `isinstance(True, int)` 为 True，
    # 写成 `history_messages: yes` 会被 YAML 解析成布尔 True，
    # 不排除的话它会当成 1 悄悄生效，用户完全看不出哪里错了。
    raw_history = front.get("history_messages", 0)
    if isinstance(raw_history, bool) or not isinstance(raw_history, int):
        return None, "history_messages 必须是非负整数", []
    if raw_history < 0:
        return None, "history_messages 必须是非负整数", []
    history_messages = raw_history

    # model：可选。
    raw_model = front.get("model")
    if raw_model is not None and not isinstance(raw_model, str):
        return None, "model 必须是字符串", []
    model = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else None

    # F22：共享模式声明 model **不是错误**。共享模式复用主对话的 Provider，
    # 中途换模型无从谈起，但用户可能只是从别的 Skill 复制了 frontmatter。
    # 直接失败太苛刻（整个 Skill 都用不了），静默忽略又会让用户以为换成功了，
    # 所以取中间路线：加载成功 + 一条明确的警告。
    if mode is SkillMode.SHARED and model is not None:
        warnings.append(
            f"Skill `{name}` 是共享模式，声明的 model `{model}` 将被忽略"
            "（仅独立模式支持指定模型）"
        )

    # 未知键一律忽略，不失败也不警告。这是刻意的向前兼容策略，与 c9 笔记的
    # frontmatter 口径一致：老版本 RhineCode 读到新版本写的 Skill 时，
    # 应当尽量把它用起来，而不是因为多了个不认识的键就整个拒绝。

    return (
        SkillSpec(
            name=name,
            description=description,
            body=body,
            mode=mode,
            allowed_tools=allowed_tools,
            history_messages=history_messages,
            model=model,
            source=source,
            entry_path=path,
            resource_dir=resource_dir,
            resource_files=resource_files,
        ),
        None,
        warnings,
    )
