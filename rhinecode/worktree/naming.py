"""
隔离工作区名字的安全校验与生成（c14 T2，spec F7）。

**纯函数、零 IO。** 本模块不碰文件系统、不调 git，因此可以被单测穷举——
而这正是它该有的样子：它是**唯一**挡在「模型给的字符串」与「拼进文件系统路径」
之间的那道闸门。

⚠ **校验必须先于任何路径拼接。**

先拼后校验的写法（`target = root / raw` 然后再检查 `target`）看起来等价，实际不是：
拼接那一步就已经产生了一个越界路径对象，后续任何一处忘记检查返回值、或者把它
传给了别的函数，都会直接落到目标目录上。把校验放在最前面，越界的字符串
**根本没有机会**变成 Path。

**威胁模型**：名字可以来自模型（委派工具的参数），因此要按不可信输入对待。
真实的攻击/误用形态是 `../../etc/passwd`、`..\\..\\Windows`、`/etc/x`、`C:/x`
这类——它们的共同点是**都能通过一个只检查「有没有奇怪字符」的粗校验**，
所以下面逐条列出的规则一条都不能省。
"""

from __future__ import annotations

import re

from rhinecode.worktree.models import MAX_NAME_LENGTH, WorktreeNameError

# 单段允许的字符集：字母、数字、点、下划线、短横。
#
# 与 Claude Code 的 worktree 名字规则**刻意一致**（它的原文是
# "Each '/'-separated segment may contain only letters, digits, dots,
# underscores, and dashes; max 64 chars total"）——从那个生态复制过来的名字
# 应当在这里同样合法，否则用户会以为是我们的 bug。
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# 生成名字时用来清洗角色名的替换表：非法字符一律换成短横。
_ILLEGAL_RUN_RE = re.compile(r"[^A-Za-z0-9._-]+")

# 生成的名字里，角色名部分的长度上限。留出空间给任务 ID 前缀与连接符，
# 保证拼出来的结果必然远小于 MAX_NAME_LENGTH。
_AGENT_PART_LIMIT = 32

# 任务 ID 取前几位。够区分并发的几个任务即可，太长会让目录名难读。
_TASK_PART_LIMIT = 8


def validate_name(raw: str) -> str:
    """
    校验并归一化一个隔离工作区名字。

    :param raw: 待校验的原始字符串（可能来自模型，按不可信输入对待）
    :returns: 归一化后的名字（仅去除首尾空白，其余原样）
    :raises WorktreeNameError: 任一规则不满足。**消息写明具体违反了哪一条**——
        笼统的「名字非法」会让用户（和模型）不知道该怎么改

    校验规则（按此顺序，先廉价后昂贵）：

    1. 去首尾空白后**非空**
    2. **不含反斜杠**。Windows 上 `\\` 是路径分隔符，放行等于开了第二条遍历通道；
       而合法的嵌套需求用 `/` 已经满足，没有任何理由需要它
    3. **不含冒号**。挡住 `C:/x` 这种盘符形式的绝对路径
    4. **不以 `/` 开头或结尾**。开头是绝对路径（POSIX），结尾会拼出空的末段
    5. 总长 **≤ MAX_NAME_LENGTH**
    6. 按 `/` 分段后，每段：非空、只含 `[A-Za-z0-9._-]`、**且不等于 `.` 或 `..`**

    第 6 条里那个 `.`/`..` 的排除是全模块最关键的一句：`.` 和 `..` **完全由
    合法字符组成**，字符集检查放不出任何异常，必须单独判。

    副作用：无。
    """
    if not isinstance(raw, str):
        raise WorktreeNameError(f"名字必须是字符串，收到 {type(raw).__name__}")

    name = raw.strip()

    if not name:
        raise WorktreeNameError("名字不能为空")

    if "\\" in name:
        raise WorktreeNameError(
            f"名字不能包含反斜杠（Windows 上它是路径分隔符）：{raw!r}"
        )

    if ":" in name:
        raise WorktreeNameError(f"名字不能包含冒号（会构成盘符形式的绝对路径）：{raw!r}")

    if name.startswith("/"):
        raise WorktreeNameError(f"名字不能以 '/' 开头（那是绝对路径）：{raw!r}")

    if name.endswith("/"):
        raise WorktreeNameError(f"名字不能以 '/' 结尾：{raw!r}")

    if len(name) > MAX_NAME_LENGTH:
        raise WorktreeNameError(
            f"名字过长：{len(name)} 字符，上限 {MAX_NAME_LENGTH}"
        )

    for segment in name.split("/"):
        if not segment:
            raise WorktreeNameError(f"名字中不能有空的路径段（连续的 '/'）：{raw!r}")
        # ⚠ 这一条必须排在字符集检查**之前或独立存在**：'.' 与 '..' 完全由
        # 合法字符组成，靠字符集是挡不住的。
        if segment in (".", ".."):
            raise WorktreeNameError(f"名字中不能有 '.' 或 '..' 路径段：{raw!r}")
        if not _SEGMENT_RE.match(segment):
            raise WorktreeNameError(
                f"名字的路径段 {segment!r} 含非法字符，"
                f"只允许字母、数字、'.'、'_'、'-'：{raw!r}"
            )

    return name


def generate_name(agent_name: str, task_id: str) -> str:
    """
    在委派方未给出名字时生成一个（spec F7）。

    :param agent_name: 角色名，用于让目录名可读（一眼看出是谁的工作区）
    :param task_id: 任务标识，用于区分同一角色的多次并发委派
    :returns: 一个**必然通过 `validate_name`** 的名字

    形如 `reviewer-a1b2c3d4`。两部分都会被清洗：非法字符成段替换为单个 `-`，
    再截断到长度上限。

    ⚠ **产出前自己过一遍 `validate_name`**，不是多余的：清洗规则和校验规则是
    两段独立的代码，将来任何一边改动都可能让它们错位（比如校验加了一条新规则
    而清洗没跟上）。让生成路径也走一遍校验，这种错位会在开发期立刻暴露，
    而不是等到某个角色名恰好触发时才出现一次莫名其妙的创建失败。

    副作用：无。
    """
    agent_part = _ILLEGAL_RUN_RE.sub("-", str(agent_name or "")).strip("-.")
    agent_part = agent_part[:_AGENT_PART_LIMIT].strip("-.")
    if not agent_part:
        agent_part = "agent"

    task_part = _ILLEGAL_RUN_RE.sub("-", str(task_id or "")).strip("-.")
    task_part = task_part[:_TASK_PART_LIMIT].strip("-.")

    candidate = f"{agent_part}-{task_part}" if task_part else agent_part

    # 清洗有可能产出 '.' / '..' / 空串这类形态（例如 agent_name 全是非法字符
    # 且 task_id 为空）。上面的 strip 已经挡住绝大多数，这里让校验做最终裁决。
    return validate_name(candidate)


__all__ = ["validate_name", "generate_name"]
