"""
②″保护路径层（protected-paths 扩展）—— 决策管线里专管「写入哪些文件必须过人眼」的一层。

## 它解决什么

`.rhinecode/` 下有一批文件，**它们的内容决定「以后会发生什么」**：`permissions.yaml`
决定模型能做什么、`hooks.yaml` 里的动作**直接执行且不过五层管线**、`mcp.yaml` 会启动
外部程序、`agents/` 与 `skills/` 是会被自动加载并指挥后续行为的文本。

本扩展之前，这些文件的写入**没有任何特殊待遇**：①黑名单只管命令类；②沙箱判它们
就在项目根内、合法；③规则在用户没写 deny 时不表态；④模式在放行档下直接放行。
于是模型可以改写自己的权限配置——**持久化提权、下次启动生效**。

这条路径绕过的不是某一层，是 C11–C15 全部安全论证共同的前提：**配置由人写下，
模型只在配置划定的范围内行动**。

## ⚠ 它的真实形态：出口处的收紧器，不是管线里的一站

    ⓪Hook → ①黑名单 → ②沙箱 → ②′网络 → ③规则 → 只读短路 → ④模式
                                                                  │
                                                                  ▼
                                                      ②″保护路径（本层）
                                          只把**非 DENY** 的结论升级为 ASK

**为什么不做成「②之后③之前」的短路站**（spec 分歧一）——按那个字面实现会**静默
放宽**两种情况：

| 场景 | 既有结论 | 短路站写法 | 差别 |
| --- | --- | --- | --- |
| 用户写下 `deny: Write(.rhinecode/hooks.yaml)` | ③层 DENY | 先短路 → ASK | 用户明确写下的禁止被降级成「问一下」 |
| `/perm` 切**严格档**写 `hooks.yaml` | ④层 DENY | 先短路 → ASK | 严格档的「灰色地带一律拒绝」被降级 |

两处都是放宽，与本层目标正相反。正确语义与 **C12 的 Hook ASK 完全同型**：
**只把 ALLOW/ASK 升级为 ASK，绝不把任何 DENY 降级。**

「必须排在③之前」这句话的**实质**因此是：**本层的升级效力不被③层的 allow 规则
消解**——一条 `allow: Write(.rhinecode/**)` 盖不过它。位置论证原样成立，
而 deny 与严格档不再被误伤。护栏见 `tests/test_perm_protected.py`。

## ⚠ 判定基准是**本次调用的工作目录**，不是主项目根

这是本层最容易写错、而且**写错了不报错**的一条。

C14 的隔离子 Agent 的工作目录是 `<主项目根>/.rhinecode/worktrees/<名字>`——
**从主项目根看，它整个人都在保护目录里**。若按主项目根做前缀判断，隔离子 Agent 的
**每一次写入**都会命中本层，而它是非交互的（判 ASK 自动拒绝），结果是隔离委派
全部静默失败、界面上只看到「子 Agent 什么都没做出来」。

正确口径与②层完全一致：**以 `request.cwd` 为根去拼保护路径**。隔离工作区里没有
`.rhinecode/`（被忽略规则排除、checkout 不出来），所以隔离子 Agent 天然一条都
命中不了——这正是想要的。

## 本层不管什么

- **只管写入类请求**。读配置不改变「以后会发生什么」。
- **`run_command` 起的命令不受约束**（`echo >> .rhinecode/hooks.yaml`）。命令串里
  无法区分读写，做了会让 `cat .rhinecode/hooks.yaml` 也弹面板，而变量拼接、heredoc、
  命令替换一律绕得过。已登记为已知边界，与 OS 级沙箱同源。
- **MCP 工具不受约束**（落 `other` 分支、没有路径判定）。
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from rhinecode.tools.path_guard import require_cwd, resolve_in_workspace

# ---------------------------------------------------------------------------
# 保护范围。**黑名单式**：默认保护整个目录，排除项必须给出理由。
#
# 方向是刻意选的偏严侧——漏了排除项只是「多弹一次面板」（看得见、有人会问），
# 反过来漏了保护项是**静默的洞**。将来往 `.rhinecode/` 下新增配置文件时，
# 它自动就被保护了，不需要有人记得回来改这张表。
# ---------------------------------------------------------------------------
PROTECTED_RELATIVE: tuple[tuple[str, ...], ...] = (
    (".rhinecode",),
    (".git",),
)

# 排除项：**纯机器副本，改了不影响「以后会发生什么」**。三项共用这一条理由。
#
# ⚠ **与 `tools/path_guard.py` 的 `_RUNTIME_ARTIFACT_RELATIVE` 取值恰好相同，
# 但两张表刻意不合一。** 那张表的语义是「搜索时跳过」，本表是「写入不必过人眼」，
# 语义不同。合一的后果：将来出现一个「不该进搜索结果、但改了会变天」的目录时
# （比如某种缓存下来的角色定义），把它加进那张表会**静默地把它从保护里摘掉**。
# 两处都写了注释互相指认。
#
# ⚠ 另外两个曾被 todo 列进排除、本 spec 判定**不该排除**的目录，理由记在这里
# 免得后来的人当成漏改顺手补上：
#   - `worktrees/`：判定基准是 `request.cwd`，隔离子 Agent 的 cwd **就是**那个
#     工作区目录，它拼出的保护根跟主项目根的 `.rhinecode/` 毫无关系。排除它唯一的
#     实际效果是让**主对话**可以随手改写别人的隔离工作区——而 C14 明说「不要自己
#     进工作区目录抄文件，成果经分支交付」。（另：C14 自己建工作区、做环境初始化
#     走的是 `worktree/` 包里的 `shutil` / `mkdir`，**不经工具管线**，本层碰不到它。）
#   - `memory/`：自动沉淀确实是内部可信写盘、不走管线（本层影响不到它），但模型
#     **主动用 `write_file` 写 memory 目录**是走管线的，而记忆会经索引注入系统提示
#     ——那正是「给自己写持久指令」，与 `skills/` 同性质。
EXCLUDED_RELATIVE: tuple[tuple[str, ...], ...] = (
    (".rhinecode", "sessions"),   # c9 会话存档
    (".rhinecode", "context"),    # c8 存盘的工具结果原文
    (".rhinecode", "traces"),     # 行为记录
)

# 「为什么这个文件特殊」——面板上那一行的信息量全靠它。
#
# **按最长前缀匹配**：表内自上而下由细到粗，取第一个命中项即可。
#
# `permissions.yaml` / `permissions.local.yaml` **刻意不单列**，由 `.rhinecode`
# 那条兜底——「配置目录，下次启动时加载」对它们成立，单列一条要多一份维护而说法
# 没有实质区别。
#
# ⚠ 成对维护点：往 `PROTECTED_RELATIVE` 加保护范围时，这张表也要加一条。
# 漏了不报错，只是面板上那行退回泛泛的兜底说法，用户看不出这个文件为什么特殊
# ——而「为什么特殊」正是他决定放不放行的唯一依据。
_WHY: tuple[tuple[tuple[str, ...], str], ...] = (
    ((".rhinecode", "hooks.yaml"), "Hook 动作会直接执行，不经权限管线"),
    ((".rhinecode", "mcp.yaml"), "会启动外部程序并把它的工具注册进工具中心"),
    ((".rhinecode", "agents"), "子 Agent 角色定义（工具白名单与权限档）"),
    ((".rhinecode", "skills"), "会被自动加载、指挥后续行为的指令文本"),
    ((".rhinecode", "memory"), "会经索引注入系统提示的项目知识"),
    ((".rhinecode", "worktrees"), "子 Agent 的隔离工作区，成果应经分支交付"),
    ((".rhinecode",), "RhineCode 的配置目录，下次启动时加载"),
    ((".git",), "版本库内部（写 .git/hooks/ 等于让下次提交执行任意代码）"),
)

# 兜底说法。正常不可达（上表的两条粗粒度项已经覆盖了全部保护范围），
# 留着是为了「新增保护范围但忘了加 `_WHY`」时有个不至于崩的落点。
_WHY_FALLBACK = "它决定 RhineCode 以后的行为"

REASON = "保护路径：写入 {shown} 会改变 RhineCode 以后的行为（{why}），需要你过目"
REASON_UNRESOLVED = "保护路径：路径 {raw} 无法解析，按保护路径处理（判定失败一律偏严）"


@dataclass(frozen=True)
class ProtectedHit:
    """
    一次命中的结果。

    :param path: **解析后的绝对路径**。它同时是会话级豁免集合的键——
                 一次解析、两处用（升级判定 + 豁免查表），避免同一个路径解析两遍
                 而两处口径悄悄漂移。
    :param reason: 面向用户与模型的中文原因，已经含「为什么这个文件特殊」。
    """

    path: Path
    reason: str


def _normcased(path: Path) -> Path:
    """
    把路径按**当前平台的大小写敏感性**归一化，供包含判断使用。

    :param path: 已解析的绝对路径
    :returns: 归一化后的路径（Windows 上转小写并统一分隔符，POSIX 上原样）

    ⚠ **为什么不直接复用 `path_guard.is_inside`。**

    那个函数用 `relative_to` 做包含判断，而 `PurePath` 的比较是**逐字符**的。
    在 Windows 上 `.RHINECODE\\hooks.yaml` 与 `.rhinecode\\hooks.yaml` 是**同一个
    文件**，但两个字符串不相等——于是一个大小写变体就能**静默跳过整层保护**，
    而它写到的还是那份真正的配置。

    `os.path.normcase` 恰好只在需要的平台上做这件事（Windows 转小写 + 分隔符归一，
    POSIX 上是恒等函数），因此这个写法在 Linux 上仍然把 `.RHINECODE` 当成一个
    真正不同的目录——那是对的，那里它确实是。
    """
    return Path(os.path.normcase(str(path)))


def _is_within(target: Path, container: Path) -> bool:
    """
    判断 `target` 是否落在 `container` 之内（含相等）。

    :param target: 已解析的绝对路径
    :param container: 已解析的绝对路径
    :returns: 落在其内返回 True

    用**路径相等/前缀**判断，不按目录名——目录名匹配会误伤用户自己叫同名的业务目录
    （与 `path_guard.runtime_artifact_dirs_of` 同一条理由）。
    """
    t = _normcased(target)
    c = _normcased(container)
    if t == c:
        return True
    try:
        t.relative_to(c)
        return True
    except ValueError:
        return False


def protected_roots_of(root: Union[str, Path]) -> tuple[Path, ...]:
    """
    给定工作目录下的保护根。

    :param root: 本次判定的工作目录（**必须是调用者的 cwd，不是主项目根**）
    :returns: 绝对路径元组（**每一项都可能不存在**——不存在不影响判定，
              模型完全可以往一个还不存在的 `.rhinecode/hooks.yaml` 写第一笔）
    """
    base = Path(root)
    return tuple(base.joinpath(*parts) for parts in PROTECTED_RELATIVE)


def excluded_roots_of(root: Union[str, Path]) -> tuple[Path, ...]:
    """
    给定工作目录下的排除根（保护范围内部、但不必过人眼的那几个）。

    :param root: 本次判定的工作目录
    :returns: 绝对路径元组（每一项都可能不存在）
    """
    base = Path(root)
    return tuple(base.joinpath(*parts) for parts in EXCLUDED_RELATIVE)


def _why_for(relative_parts: tuple[str, ...]) -> str:
    """
    按最长前缀在 `_WHY` 里查「这个文件为什么特殊」。

    :param relative_parts: 相对工作目录的路径分段（已按平台归一化大小写）
    :returns: 中文说明；查不到时返回兜底说法
    """
    for prefix, why in _WHY:
        normalized = tuple(os.path.normcase(part) for part in prefix)
        if relative_parts[: len(normalized)] == normalized:
            return why
    return _WHY_FALLBACK


def inspect(specifier: str, cwd: Union[str, Path, None]) -> Optional[ProtectedHit]:
    """
    判断一次写入是否落在保护路径内。

    :param specifier: 模型给出的路径（相对或绝对，原样传入）
    :param cwd: **本次调用的工作目录**。主对话与非隔离子 Agent 传主项目根，
                隔离子 Agent 传它自己的隔离工作区
    :returns: 命中返回 `ProtectedHit`；未命中返回 None

    执行流程：

    1. 用 `resolve_in_workspace` 解析。**任何异常都按命中处理**（偏严，spec N3）。
       ⚠ 这里必须用它而不是自己拼 `Path`——它同时做了「拒绝 `..`」「绝对路径按
       真实位置判断」「解析已存在父目录中的符号链接」三件事。自己拼的话，
       「在工作区内建一个指向 `.rhinecode/hooks.yaml` 的软链再写它」会静默绕过整层。
    2. 落在任一**排除根**内 → 返回 None。
       ⚠ **排除必须查在保护之前**：排除项是保护项的真子集，顺序反了排除永远不生效
       （`.rhinecode/traces/x` 会先命中 `.rhinecode` 那条保护根）。
       护栏见 `tests/test_perm_protected.py::ExcludeBeatsProtectTest`。
    3. 落在任一**保护根**内（含相等）→ 查 `_WHY` 组装原因返回。
    4. 否则 None。

    副作用：无（纯判定）。路径解析会读文件系统（`resolve` 要跟符号链接），
    这与②层沙箱是同一份既有开销，本层不新增 I/O 形态。
    """
    raw = str(specifier)
    try:
        root = require_cwd(cwd)
        resolved = resolve_in_workspace(raw, cwd)
    except Exception:  # noqa: BLE001 —— 判定失败一律偏严，见下
        # ⚠ **失败按命中处理**，而不是「解析不了就放过」。
        #
        # 这一支经引擎调用时其实不可达：路径解析失败意味着②层沙箱已经给出 DENY，
        # 而收紧器见到 DENY 就原样返回、根本不会走到这里。留着它是给直接调用方
        # （测试、将来的第二个调用点）的兜底——本层是安全边界，不确定就偏严。
        #
        # `path` 这里填的不是绝对路径，因此它**永远匹配不上豁免集合里的任何一项**
        # （那里存的都是解析后的绝对路径）。这同样是安全的方向：解析不了的路径
        # 豁免不掉。
        return ProtectedHit(path=Path(raw), reason=REASON_UNRESOLVED.format(raw=raw))

    for excluded in excluded_roots_of(root):
        if _is_within(resolved, excluded):
            return None

    for protected in protected_roots_of(root):
        if _is_within(resolved, protected):
            # 展示用的相对路径：面板上写绝对路径又长又没有额外信息。
            # `resolve_in_workspace` 已经保证 resolved 落在 root 内，
            # 这里的 relative_to 不会失败；仍留一个兜底，绝不让展示逻辑影响判定。
            try:
                relative = resolved.relative_to(root)
                shown = relative.as_posix()
                parts = tuple(os.path.normcase(part) for part in relative.parts)
            except ValueError:  # pragma: no cover —— 见上，理论不可达
                shown = raw
                parts = ()
            return ProtectedHit(
                path=resolved,
                reason=REASON.format(shown=shown, why=_why_for(parts)),
            )

    return None


__all__ = [
    "PROTECTED_RELATIVE",
    "EXCLUDED_RELATIVE",
    "ProtectedHit",
    "protected_roots_of",
    "excluded_roots_of",
    "inspect",
]
