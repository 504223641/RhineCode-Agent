"""
RHINE.md 项目指令文件的三层加载与 @include 展开（c9 F1/F2/F3，纯函数、零全局状态）。

三层来源与拼接顺序（F1）：
    用户级 ~/.rhinecode/RHINE.md  →  项目级 <项目根>/.rhinecode/RHINE.md  →  项目根 <项目根>/RHINE.md
越具体的层越靠后——LLM 对靠近末尾的内容注意力更强（近因效应），项目级指令在
与用户级冲突时更占优（同 Claude Code 官方实测方案，见 docs/c9/spec.md 澄清记录）。
三层是**拼接**不是覆盖：每层内容都完整进入上下文，各带来源路径标注。

@include 引用（F2）：文件内的 `@相对路径` token 会被目标文件内容原地展开。规则：
- 相对路径相对于**引用文件所在目录**解析（不是项目根，便于子文件再引用兄弟文件）；
- 嵌套展开上限 4 层（对齐 Claude Code），超深不展开；
- visited 集合防循环（A 引 B、B 引 A 时第二次遇到即停）；
- 展开目标 resolve 后必须仍在宿主层的边界目录内（项目层限项目根内、用户层限
  ~/.rhinecode 内），越界不展开——防止指令文件把任意系统文件带进上下文；
- 围栏代码块（``` 包围）内的 `@` 不视为引用，保证示例代码原样保留。

全程 fail-safe（F3）：单文件读取失败、include 越界/超深/循环都只跳过出问题的部分、
把可读提示记入该层的 errors（供 /memory 展示），绝不抛异常阻断启动。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

# include 嵌套展开的最大深度（层）：第 0 层是 RHINE.md 本身引的文件，
# 达到该深度后不再继续展开（保留 @ 原文并记录提示）。
MAX_INCLUDE_DEPTH = 4

# 匹配行内 `@路径` 引用：@ 后跟不含空白与 @ 的连续字符（相对路径）。
# 只接受相对路径形态；绝对路径（盘符/根开头）大概率越界，交给边界校验统一拦截。
_INCLUDE_RE = re.compile(r"@([^\s@`]+)")

# 路径 token 的尾随标点集合：`@docs/x.md。` `(@b.md)` 这类写法里，句尾/括号标点
# 会被上面的贪婪匹配吞进路径，需要剥离出来作为普通文本保留（中英文标点都覆盖）。
_TRAILING_PUNCT = ".,;:!?)]}>\"'，。、；：！？）】》"

# 围栏代码块的起止行（``` 或 ~~~ 开头），块内不解析 include。
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass
class InstructionLayer:
    """
    一层 RHINE.md 的加载结果（供 /memory 报告展示）。

    :param label: 层名（"用户级" / "项目级 .rhinecode" / "项目根"）
    :param path: 该层文件的路径（无论是否存在）
    :param loaded: 文件存在且读取成功
    :param size: include 展开后的字符数（loaded=False 时为 0）
    :param errors: include 越界 / 超深度 / 循环 / 读取失败等可读提示
    """

    label: str
    path: Path
    loaded: bool = False
    size: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class LoadedInstructions:
    """
    三层加载的汇总结果。

    :param text: 拼接好的全部内容（各层带来源标注，空行分隔）；空串 = 三层全缺
    :param layers: 每层的加载明细（含缺失层，loaded=False）
    """

    text: str
    layers: list[InstructionLayer]


def load_instructions(user_dir: Path, project_root: Path) -> LoadedInstructions:
    """
    加载三层 RHINE.md 并拼接。

    执行流程：按固定顺序枚举三层（用户级 → 项目 .rhinecode → 项目根），对每层：
    1. 文件不存在 → 记 loaded=False 跳过（F1 缺层跳过）；
    2. 读取失败 → loaded=False + errors 记录（F3）；
    3. 读取成功 → 以宿主层边界做 include 展开，拼上 `# 来源：<路径>` 标注头。

    :param user_dir: 用户级目录（通常 ~/.rhinecode）；其边界即该目录本身
    :param project_root: 项目根；项目级两层的边界都是项目根
    :returns: LoadedInstructions（text 为空串表示三层全缺，注入时整体跳过）

    副作用：只读文件系统，不写任何内容。
    """
    # (层名, 文件路径, include 边界目录)；边界按「宿主文件所属层」划定（F2）。
    layer_specs = [
        ("用户级", user_dir / "RHINE.md", user_dir),
        ("项目级 .rhinecode", project_root / ".rhinecode" / "RHINE.md", project_root),
        ("项目根", project_root / "RHINE.md", project_root),
    ]

    layers: list[InstructionLayer] = []
    parts: list[str] = []
    for label, path, boundary in layer_specs:
        layer = InstructionLayer(label=label, path=path)
        layers.append(layer)
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            layer.errors.append(f"读取失败：{e}")
            continue
        # include 展开：visited 以「本层的 RHINE.md 自身」起步，防止子文件反引宿主成环。
        expanded = _expand_includes(
            raw,
            base_dir=path.parent,
            boundary_root=_safe_resolve(boundary),
            depth=0,
            visited={_safe_resolve(path)},
            errors=layer.errors,
        )
        layer.loaded = True
        layer.size = len(expanded)
        # 来源标注让模型（和排查问题的人）知道每段指令来自哪一层（F1）。
        parts.append(f"# 来源：{path}\n\n{expanded.strip()}")

    return LoadedInstructions(text="\n\n".join(parts), layers=layers)


def _safe_resolve(path: Path) -> Path:
    """resolve 的 fail-safe 包装：解析失败时退回原路径（后续比较自然不会通过）。"""
    try:
        return path.resolve()
    except OSError:
        return path


def _expand_includes(
    text: str,
    base_dir: Path,
    boundary_root: Path,
    depth: int,
    visited: set[Path],
    errors: list[str],
) -> str:
    """
    递归展开文本中的 @include 引用（单层的核心逻辑）。

    执行流程：逐行处理——
    1. 维护「是否在围栏代码块内」状态（行首 ``` / ~~~ 翻转），块内行原样保留；
    2. 块外行用正则找 `@路径` token，逐个尝试展开：
       - depth 已达 MAX_INCLUDE_DEPTH → 保留原文 + errors 记录；
       - 路径 resolve 后不在 boundary_root 内 → 保留原文 + errors 记录（越界拦截）；
       - 已在 visited → 保留原文 + errors 记录（循环拦截）；
       - 目标不存在 / 读取失败 → 保留原文 + errors 记录；
       - 否则读取目标内容，先递归展开其内部 include（base_dir 换成目标所在目录、
         depth+1、visited 加入目标），再替换进当前行。

    :param text: 待展开的原始文本
    :param base_dir: 相对路径的解析基准（引用文件所在目录）
    :param boundary_root: 边界目录（resolve 过）；展开目标必须位于其内
    :param depth: 当前已经历的展开层数（0 = 顶层文件里的引用）
    :param visited: 已进入展开链的文件集合（resolve 过），防循环
    :param errors: 输出参数，收集可读提示
    :returns: 展开后的文本；任何问题都退化为「该处保留原文」，不抛异常

    副作用：只读文件系统；向 errors 追加提示。
    """
    lines_out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            # 围栏起止行本身原样保留，并翻转「块内」状态。
            in_fence = not in_fence
            lines_out.append(line)
            continue
        if in_fence or "@" not in line:
            lines_out.append(line)
            continue

        def _replace(match: "re.Match[str]") -> str:
            # 剥离尾随标点：`(@b.md)` 匹配到的是 `b.md)`，右括号不属于路径，
            # 剥下来拼回替换结果末尾作为普通文本。
            raw_ref = match.group(1)
            suffix = ""
            while raw_ref and raw_ref[-1] in _TRAILING_PUNCT:
                suffix = raw_ref[-1] + suffix
                raw_ref = raw_ref[:-1]
            if not raw_ref:
                return match.group(0)
            candidate = Path(raw_ref)
            target = candidate if candidate.is_absolute() else base_dir / candidate
            resolved = _safe_resolve(target)
            # 边界校验：resolve 后必须仍在宿主层边界内（防越界读任意文件）。
            try:
                resolved.relative_to(boundary_root)
            except ValueError:
                errors.append(f"@include 越界，未展开：{raw_ref}")
                return match.group(0)
            if depth >= MAX_INCLUDE_DEPTH:
                errors.append(f"@include 超过 {MAX_INCLUDE_DEPTH} 层深度，未展开：{raw_ref}")
                return match.group(0)
            if resolved in visited:
                errors.append(f"@include 循环引用，未展开：{raw_ref}")
                return match.group(0)
            if not resolved.is_file():
                errors.append(f"@include 目标不存在，未展开：{raw_ref}")
                return match.group(0)
            try:
                content = resolved.read_text(encoding="utf-8")
            except OSError as e:
                errors.append(f"@include 读取失败，未展开：{raw_ref}（{e}）")
                return match.group(0)
            # 递归展开子文件自身的 include：基准目录换成子文件所在目录，
            # visited 传入含子文件的新集合（分支间不互相污染，只防链上成环）。
            expanded = _expand_includes(
                content,
                base_dir=resolved.parent,
                boundary_root=boundary_root,
                depth=depth + 1,
                visited=visited | {resolved},
                errors=errors,
            )
            # 剥下来的尾随标点拼回展开内容之后，保持原文语序。
            return expanded + suffix

        lines_out.append(_INCLUDE_RE.sub(_replace, line))
    return "\n".join(lines_out)
