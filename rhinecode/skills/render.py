"""
Skill 系统的全部文本产出（c11 T12–T15）。

**职责**：所有「给模型看的文本」集中在这一个模块——第一阶段清单、
已激活 Skill 的正文段、参数替换、资源清单、自包含调用文本。

**为什么要集中**：这些文本是 Skill 系统实际的「接口」，模型的行为完全取决于
它们的措辞。散落在 manager / tools / conversation 里的话，改一句话要翻三个文件，
而且没法单独测试。全部做成纯函数放这里，措辞调整只动一处，且能用字符串断言直接验证。

**纯函数、无状态**：不读文件、不碰锁、不依赖 manager。

对应 spec 条款：F6（清单上限）、F9（正文上限与两种降级）、F10（多 Skill 边界标识）、
F12（$ARGUMENTS 替换）、F13（资源清单）、F24（自包含调用文本）。
"""

from typing import Iterable, Optional

from rhinecode.skills.models import (
    BODY_MAX_BYTES,
    BODY_MAX_LINES,
    DegradeKind,
    INDEX_MAX_BYTES,
    INDEX_MAX_LINES,
    PLACEHOLDER,
    SkillSpec,
    TOTAL_MAX_BYTES,
    TOTAL_MAX_LINES,
)

# 模式的中文标签，清单与报告共用。


def _truncate(text: str, max_lines: int, max_bytes: int) -> tuple[str, bool]:
    """
    按行数与 UTF-8 字节数双重截断，先到者为准。

    :param text: 待截断文本
    :param max_lines: 行数上限
    :param max_bytes: UTF-8 字节数上限
    :returns: `(截断后文本, 是否发生了截断)`

    **字节截断按整行回退，绝不切在字符中间**：直接 `text.encode()[:N].decode()`
    会把一个多字节汉字切成两半产生非法 UTF-8，序列化成 JSON 发给 API 时会炸。
    这里逐行累加字节数，放不下的行整行丢弃——沿用 c9 笔记索引的既有做法
    （`memory/notes.py` 的 `truncate_index`）。

    副作用：无。
    """
    lines = text.splitlines()
    truncated = len(lines) > max_lines
    lines = lines[:max_lines]

    out: list[str] = []
    total = 0
    for line in lines:
        size = len(line.encode("utf-8")) + 1  # +1 计换行符
        if total + size > max_bytes:
            truncated = True
            break
        out.append(line)
        total += size

    return "\n".join(out), truncated


def render_index(skills: Iterable[SkillSpec]) -> str:
    """
    渲染第一阶段清单——模型在**启动时**看到的全部 Skill 信息（spec F6）。

    :param skills: 已按名字排序的 Skill 列表
    :returns: 清单文本；**列表为空时返回空串**

    只含命令名与两段说明，**不含任何 SOP 正文**——这正是「两阶段加载」
    第一阶段的定义：让模型知道有什么可用，但不为此付出上下文代价。
    模型想用时再调 `load_skill` 把完整指令拉进来。

    **`description` 与 `when_to_use` 拼接后共享同一字符预算**（对齐改造 F7）。
    拆成两个字段的意义在于让作者能分别写「这个 Skill 做什么」与「什么时候该用它」，
    而不是把两件事挤进一个字段——实测中那会让 description 被写成两百多字符。

    空列表返回空串是刻意的：`build_default_prompt` 对空内容的槽位会整体跳过，
    于是没装任何 Skill 的用户，其系统提示与改造前逐字节相同（零回归）。

    副作用：无。
    """
    items = list(skills)
    if not items:
        return ""

    lines = [
        "以下 Skill 可用，用 `load_skill` 工具加载其完整指令。",
        "标注「子对话」的会另开一条对话跑完并只回流结论；"
        "标注「仅用户可发起」的你不能自行加载，只能建议用户执行对应命令。",
        "",
    ]
    for spec in items:
        flags = []
        if spec.forked:
            flags.append("子对话")
        if not spec.model_invocable:
            flags.append("仅用户可发起")
        suffix = f"（{'、'.join(flags)}）" if flags else ""
        text = spec.description
        if spec.when_to_use:
            text = f"{text} —— {spec.when_to_use}"
        lines.append(f"- {spec.command_name}{suffix}：{text}")

    text = "\n".join(lines)
    truncated, did = _truncate(text, INDEX_MAX_LINES, INDEX_MAX_BYTES)
    if did:
        # 算出实际列出了几条，好让「另有 N 个」的数字是准的。
        listed = sum(1 for line in truncated.splitlines() if line.startswith("- "))
        remaining = len(items) - listed
        truncated += f"\n（另有 {remaining} 个 Skill 未列出）"
    return truncated


def substitute(body: str, arguments: str) -> str:
    """
    把用户传入的参数放进 SOP 正文（spec F12）。

    :param body: SOP 正文原文
    :param arguments: 用户参数，**原样传入**（未做 shell 分词、未去内部空白）
    :returns: 替换后的正文

    两条路径：
    - 正文含 `$ARGUMENTS` → 替换**全部**出现处（一份 SOP 可能在多个步骤里引用参数）；
    - 正文不含占位符但用户给了参数 → 在末尾追加一段「用户补充参数」。
      不能直接丢掉：用户敲了 `/commit 修复登录超时`，那句话就是他这次的意图，
      Skill 作者没写占位符不代表用户的输入该被吞掉。
    - 正文不含占位符且参数为空 → 原样返回。

    **参数原样保留**：不做 shell 分词、不做引号处理、不做任何模板求值。
    参数里的空格、引号、管道符、反斜杠全部按字面量进入正文——它最终是给模型读的
    自然语言，不是给 shell 执行的命令行。

    副作用：无。
    """
    if PLACEHOLDER in body:
        return body.replace(PLACEHOLDER, arguments)
    if arguments.strip():
        return f"{body.rstrip()}\n\n## 用户补充参数\n\n{arguments}\n"
    return body


def render_resources(spec: SkillSpec) -> str:
    """
    渲染目录型 Skill 的随附资源清单（spec F13）。

    :param spec: Skill 定义
    :returns: 资源说明段；**单文件型返回空串**

    为什么要给**绝对路径**：Skill 目录可能在用户级 `~/.rhinecode/skills/` 或
    随包分发的 builtin 目录里，这两处都在**项目工作区之外**。工作区外的路径
    没法用 `glob_files` / `grep_content` 枚举（路径沙箱会拒），模型只能靠这份
    清单知道有哪些文件、再用绝对路径 `read_file` 逐个读取。所以清单里明确写出
    这一点，免得模型反复尝试 glob 然后反复被拒。

    （读取本身是可行的：用户级 skills 目录与 builtin 目录已注册进 path_guard
    的**只读白名单**，见 conversation 层的 `register_read_root`。）

    副作用：无。
    """
    if spec.resource_dir is None:
        return ""

    lines = [
        "",
        f"### 本 Skill 的随附资源",
        "",
        f"资源目录：`{spec.resource_dir}`",
    ]
    if spec.resource_files:
        lines.append("")
        lines.append("可用文件（相对资源目录）：")
        lines.extend(f"- {rel}" for rel in spec.resource_files)
    lines.append("")
    lines.append(
        "注意：该目录在项目工作区之外，**不支持 glob/grep 枚举**，"
        "请按上述清单用绝对路径直接 read_file 读取。"
    )
    return "\n".join(lines)


def render_active_body(
    spec: SkillSpec, arguments: str
) -> tuple[str, Optional[DegradeKind]]:
    """
    渲染**单个**已激活 Skill 的注入段（spec F9/F10）。

    :param spec: Skill 定义
    :param arguments: 激活时传入的参数
    :returns: `(注入文本, 降级形态)`。未降级时第二项为 None，
              单体超上限时为 `DegradeKind.TRUNCATED`

    带明确的边界标识（含 Skill 名）：多个 Skill 同时激活时，若没有边界，
    模型会把两份 SOP 的步骤混在一起执行。标识里带名字还能让模型在回答时
    说清「我在按 commit 这个 Skill 的第 3 步做」。

    副作用：无。
    """
    header = f"===== Skill 指令开始：{spec.command_name} ====="
    footer = f"===== Skill 指令结束：{spec.command_name} ====="
    parts = [
        header,
        f"（{spec.description}）",
        "",
        substitute(spec.body, arguments).rstrip(),
    ]
    resources = render_resources(spec)
    if resources:
        parts.append(resources.rstrip())
    parts.append(footer)

    text = "\n".join(parts)
    truncated, did = _truncate(text, BODY_MAX_LINES, BODY_MAX_BYTES)
    if not did:
        return text, None

    # 截断后 footer 大概率被切掉了，补一个明确的收尾，
    # 否则模型会看到一个没有结束标识的段落，与后一个 Skill 的内容黏在一起。
    truncated += f"\n（正文已截断，未显示完整）\n{footer}"
    return truncated, DegradeKind.TRUNCATED


def render_active_section(
    items: Iterable[tuple[SkillSpec, str]],
) -> tuple[str, dict[str, DegradeKind]]:
    """
    渲染**全部**已激活 Skill 的注入段（spec F9/F10）。

    :param items: `(spec, arguments)` 序列，顺序即激活顺序
    :returns: `(完整注入文本, {skill 名: 降级形态})`。
              未降级的 Skill 不出现在字典里。空输入返回 `("", {})`

    **总量超限时整段丢弃，不切半**（plan 决策 10）：
    一个被切掉后半段的 SOP 会让模型执行一个残缺流程——它不知道自己只拿到一半，
    会当成完整流程去执行，产出一个「做了前三步就宣告完成」的结果。
    整段丢弃至少是可判定的状态：模型完全不知道这个 Skill，不会假装执行它，
    而用户会通过 `/skills` 与激活警告看到 DROPPED 标记。

    **一旦超限，其后所有段一并 DROPPED**，不再尝试「跳过大的、塞进小的」：
    那样会打乱激活顺序的语义（F10 规定顺序即拼接顺序），且结果依赖于各 Skill
    的体积，用户完全无法预测哪个会生效。

    副作用：无。
    """
    pairs = list(items)
    if not pairs:
        return "", {}

    degrades: dict[str, DegradeKind] = {}
    chunks: list[str] = []
    total_lines = 0
    total_bytes = 0
    overflowed = False

    for spec, arguments in pairs:
        if overflowed:
            # 前面已经溢出，其后一律丢弃，保持顺序语义。
            degrades[spec.command_name] = DegradeKind.DROPPED
            continue

        body, degrade = render_active_body(spec, arguments)
        body_lines = len(body.splitlines())
        body_bytes = len(body.encode("utf-8"))

        if (
            total_lines + body_lines > TOTAL_MAX_LINES
            or total_bytes + body_bytes > TOTAL_MAX_BYTES
        ):
            overflowed = True
            degrades[spec.command_name] = DegradeKind.DROPPED
            continue

        chunks.append(body)
        total_lines += body_lines
        total_bytes += body_bytes
        if degrade is not None:
            degrades[spec.command_name] = degrade

    return "\n\n".join(chunks), degrades


def render_invocation_text(spec: SkillSpec, arguments: str) -> str:
    """
    渲染**自包含**的调用文本（spec F24）。

    :param spec: Skill 定义
    :param arguments: 用户传入的参数
    :returns: 一段同时含 Skill 名、一句话说明、原样参数的文本

    **三项缺一不可**，因为这段文本有三个消费者，且它们要求逐字一致：

    1. 共享模式下作为主历史里那条 user 消息的 `content`；
    2. 独立模式下作为子对话首条 user 消息（同源复用，F20）；
    3. 会话存档里记录的就是它。

    「自包含」的含义：几个月后 `/resume` 恢复这条会话时，Skill 文件可能已被
    修改甚至删除。如果历史里只存了一句 `/commit 修复超时`，那时候谁也不知道
    当初 `/commit` 到底让模型做了什么。带上说明与参数，至少这条记录本身
    还能读懂——不需要回头去查一个可能已经不存在的文件。

    （界面与回放显示的是用户敲的原始输入，走 `Message.display_content`，
    与本文本是两条通道，见 C10 的双内容模型。）

    副作用：无。
    """
    shown = arguments.strip() if arguments.strip() else "（无）"
    return (
        f"执行 Skill /{spec.command_name}（{spec.description}）\n"
        f"参数：{shown}"
    )
