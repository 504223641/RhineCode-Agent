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

from typing import Callable, Iterable, Optional

from rhinecode.skills.models import (
    BODY_MAX_BYTES,
    BODY_MAX_LINES,
    DegradeKind,
    INDEX_MAX_BYTES,
    INDEX_MAX_LINES,
    PLACEHOLDER,
    SkillSource,
    SkillSpec,
    TOTAL_MAX_BYTES,
    TOTAL_MAX_LINES,
)

# ────────────────────── 第一阶段清单的表头 ──────────────────────
#
# ## 为什么这段措辞是「指令」而不是「公告」
#
# 原文只有一句「以下 Skill 可用，用 `load_skill` 工具加载其完整指令。」——
# 它**陈述可用性**，却从不要求模型去用。实测后果：用户说「帮我做个前端页面」、
# 清单里明明有前端设计 Skill，模型照自己的默认做法做完，一次都没加载。
#
# 这不是本项目独有的偏差。Anthropic 官方 skill-creator 的指导原话是：
# 描述要写得**「有点 pushy」**，因为「Claude 有**可测量的欠触发倾向**
# （a measured tendency to under-trigger skills）」。
#
# 所以这里照 Claude Code 给 agent 的 Skill 工具描述的口径重写，三个要件：
#
# 1. **动手前先查清单**（而不是「清单在这儿」）；
# 2. **命中则用它替代默认做法**——Claude Code 的原话是
#    「call this tool first … to follow **in place of your default approach**」。
#    没有这半句，模型会把 Skill 当成"另一种可选做法"而不是"该走的那条路"；
# 3. **点明代价不对称**，直接纠偏：漏加载会产出不合项目约定的结果，
#    多加载只是多读几百字。这条针对的正是上面那个已知偏差。
#
# 另外显式覆盖「用户没明说要用 Skill」这一情形——那是实际使用中最常见的
# 漏触发场景，也是用户报上来的原始现象。
_INDEX_HEADER = (
    "以下是已为本项目/本用户配置好的 Skill。每一个都是**针对某一类任务的成套做法**，"
    "记录着这个项目在这类任务上的既有约定。",
    "",
    "**动手做任何事之前先扫一遍这份清单。** 如果手上的任务属于其中某个 Skill 覆盖的类型，"
    "**先用 `load_skill` 加载它，然后按它的流程做**——而不是按你自己的默认做法做。",
    "",
    "⚠️ **用户不必明确说「用某个 Skill」。** 判断依据是**任务类型是否匹配**，"
    "不是用户有没有点名。他说「帮我做个前端页面」而清单里有前端相关的 Skill，那就是命中。",
    "",
    "⚠️ **拿不准要不要加载时，倾向加载。** 两边的代价不对称：漏加载 = 用你自己的默认做法"
    "做出一个不符合本项目约定的结果，用户往往要到很后面才发现；多加载一次 = 多读几百字。",
    "",
    "标注「子对话」的会另开一条对话跑完并只回流结论（你同样可以自行发起）；"
    "标注「仅用户可发起」的你不能加载，只能建议用户执行对应命令。",
)

# 清单预算不够时的**降级顺序权重**：数字越大越先被削成「只有名字」。
#
# 内置样板最先削——它们随程序分发，用户在 `/help` 与文档里都能看到；
# 项目级最后削——那是用户为这个仓库刻意添加的东西，最不该在模型眼里变模糊。
_SOURCE_DEGRADE_RANK = {
    SkillSource.BUILTIN: 2,
    SkillSource.USER: 1,
    SkillSource.PROJECT: 0,
}


def _truncate(text: str, max_lines: int, max_bytes: int) -> tuple[str, bool]:
    """
    按行数与 UTF-8 字节数双重截断，先到者为准。

    :param text: 待截断文本
    :param max_lines: 行数上限
    :param max_bytes: UTF-8 字节数上限
    :returns: `(截断后文本, 是否发生了截断)`

    **字节截断按整行回退，绝不切在字符中间**：直接 `text.encode()[:N].decode()`
    会把一个多字节汉字切成两半产生非法 UTF-8，序列化成 JSON 发给 API 时会炸。
    这里逐行累加字节数，放不下的行整行丢弃——沿用 c9 记忆索引的既有做法
    （`memory/memories.py` 的 `truncate_index`）。

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


def render_index(
    skills: Iterable[SkillSpec],
    entry_hint: Optional[Callable[[SkillSpec], str]] = None,
) -> str:
    """
    渲染第一阶段清单——模型在**启动时**看到的全部 Skill 信息（spec F6）。

    :param skills: 已按名字排序的 Skill 列表
    :param entry_hint: 给出某个 Skill 的**真实用户入口命令**（如 `/deploy` 或
                       `/skills run deploy`）。只对 `disable-model-invocation`
                       的 Skill 有用——见下方「为什么需要它」。缺省 None 时
                       退化为不带命令的旧措辞（纯函数测试与 Null 场景走这条）。
    :returns: 清单文本；**列表为空时返回空串**

    **`entry_hint` 为什么需要**（真实模型端到端场景 5 抓到的）：清单原本只说
    「你不能自行加载，只能建议用户执行对应命令」，却从不说明那条命令是什么。
    实测模型于是**编了一条不存在的命令**（`rhine skill deploy`）告诉用户，
    用户照着敲只会得到「未知命令」。短命令是否注册成功要问 `CommandRegistry`
    （重名会跳过），渲染层自己算不出来，所以由调用方注入。

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

    lines = [*_INDEX_HEADER, ""]

    def _entry(spec: SkillSpec, *, with_text: bool) -> str:
        """渲染一行。`with_text=False` 时只留名字与标记（预算不够时的降级形态）。"""
        flags = []
        if spec.forked:
            flags.append("子对话")
        if not spec.model_invocable:
            # 把真实入口命令直接写进标注。不带 hint 时退回旧措辞——
            # 模糊总比给一条错命令强。
            hint = entry_hint(spec) if entry_hint is not None else ""
            flags.append(f"仅用户可发起，请建议用户执行 {hint}" if hint else "仅用户可发起")
        suffix = f"（{'、'.join(flags)}）" if flags else ""
        if not with_text:
            return f"- {spec.command_name}{suffix}"
        text = spec.description
        if spec.when_to_use:
            text = f"{text} —— {spec.when_to_use}"
        return f"- {spec.command_name}{suffix}：{text}"

    # ── 预算不够时：**降级成只有名字，而不是整条丢掉** ──
    #
    # 原实现直接按行截断，尾部那些 Skill 连名字都不出现——**对模型等于不存在**，
    # 它既不会加载也不会向用户提起，而用户完全看不出发生了什么。
    #
    # 名字是模型唯一的入口：只要名字在，模型至少能判断「这里像是有个相关的东西」
    # 并去加载看看；描述没了只是命中率下降。所以宁可牺牲描述，不牺牲名字
    # （与 Claude Code 的口径一致：清单**永远包含每个 Skill 的名字**）。
    #
    # 降级顺序按**来源层级从低到高**：内置 → 用户级 → 项目级。理由是项目级
    # 是用户为这个仓库刻意添加的，最不该被削；内置样板即使只剩名字，
    # 用户也能从 `/help` 与文档里知道它们是什么。
    #
    # ⚠️ 显示顺序仍按名字排（调用方已排好），**只有「谁被降级」按层级挑**——
    # 否则每次超预算时列表顺序都会跳，用户没法在两次输出间对照。
    degrade_order = sorted(
        range(len(items)),
        key=lambda i: (-_SOURCE_DEGRADE_RANK.get(items[i].source, 0), items[i].command_name),
    )
    with_text = [True] * len(items)

    def _render(flags: list[bool]) -> str:
        return "\n".join(
            lines + [_entry(spec, with_text=flag) for spec, flag in zip(items, flags)]
        )

    text = _render(with_text)
    degraded = 0
    for idx in degrade_order:
        fitted, did = _truncate(text, INDEX_MAX_LINES, INDEX_MAX_BYTES)
        if not did:
            break
        with_text[idx] = False
        degraded += 1
        text = _render(with_text)

    fitted, did = _truncate(text, INDEX_MAX_LINES, INDEX_MAX_BYTES)
    if did:
        # 连「全部只有名字」都装不下——只能真丢了。这时才用条数兜底，
        # 并如实说明「未列出」而不是假装完整。
        listed = sum(1 for line in fitted.splitlines() if line.startswith("- "))
        fitted += (
            f"\n（另有 {len(items) - listed} 个 Skill 因清单预算未列出；"
            f"用 `/skills` 可看到全部）"
        )
        return fitted

    if degraded:
        text += (
            f"\n（其中 {degraded} 个因清单预算只列了名字，未附说明；"
            f"名字看着可能相关就直接 `load_skill` 试，加载后才能看到它到底做什么）"
        )
    return text


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
        "### 本 Skill 的随附资源",
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
