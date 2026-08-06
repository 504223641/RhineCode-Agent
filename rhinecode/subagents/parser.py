"""
角色定义文件的解析（c13 T2，spec F1）。

**职责**：一段文本 → 一个 `AgentSpec`。切 frontmatter、归一键名、逐字段校验与夹取。

**不做的事**：不读文件（调用方给文本）、不扫目录（那是 `discovery` 的事）、
不判断优先级。这样本模块是纯函数，能被单测直接喂字符串验证。

## 两类失败的分界

- **抛 `AgentParseError`**：这份定义根本不能用（缺 `description`、YAML 坏了、
  名字非法）。调用方转成一条 `AgentLoadError`，该角色不进目录。
- **记 warning**：定义能用，但有些地方不如用户预期（写了本项目不支持的字段、
  轮次上限越界被夹、权限档位写了不认识的值）。角色照常可用，提示在 `/agents` 里可见。

分界的判据是**「照着用户写的跑下去，结果会不会与他的意图相反」**：
缺 `description` 的角色主 Agent 永远选不中它，等于没加载；而写了 `memory: user`
的角色只是少一个本来就没有的能力，其余照常。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from rhinecode.permission.models import PermissionMode
from rhinecode.subagents.models import (
    DEFAULT_MAX_TURNS,
    HARD_MAX_TURNS,
    UNSUPPORTED_FIELDS,
    AgentSource,
    AgentSpec,
)

_FENCE = "---"

# 角色名里不允许出现的字符。
#
# - 路径分隔符：角色名会出现在 trace 作用域（`subagent:<name>`）与报告里，
#   放进路径分隔符会让作用域串看起来像个路径，过滤时极易误判；
# - 空白：模型在委派时按名字指定角色，含空白的名字会在各种切分处走样；
# - 冒号：与 Claude Code 一致——它保留给插件作用域标识（`my-plugin:reviewer`）。
_ILLEGAL_NAME_CHARS = ("/", "\\", ":")

# `model: inherit` 与「不写」等价，统一归一为 None。
_INHERIT = "inherit"


class AgentParseError(Exception):
    """一份角色定义无法使用。消息是中文，直接展示给用户。"""


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """
    切出 YAML frontmatter 与正文。

    :param text: 文件全文
    :returns: `(frontmatter 映射, 正文)`
    :raises AgentParseError: 没有 frontmatter / 未闭合 / YAML 语法错 / 顶层不是映射

    与 C11 的 Skill 解析**有一处刻意不同**：Skill 允许「没有 frontmatter，
    整份文件都是正文」，角色**不允许**——`description` 是必填的，
    而它只能来自 frontmatter。没有 frontmatter 的文件必然缺 `description`，
    早一步报错能给出更准确的提示（「缺少 frontmatter」比「缺少 description」
    更接近用户实际写错的地方）。

    用 `splitlines(keepends=True)` 而非 `split("\\n")`：前者能正确处理 `\\r\\n`
    与文件末尾有无换行的差异，重新 join 能还原原文——正文是要发给模型的系统提示，
    缩进和空行即语义。

    副作用：无。
    """
    lines = text.splitlines(keepends=True)

    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1

    if start >= len(lines) or lines[start].strip() != _FENCE:
        raise AgentParseError(
            "缺少 YAML frontmatter（文件应以一行 --- 开头）"
        )

    end = None
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == _FENCE:
            end = i
            break
    if end is None:
        raise AgentParseError("YAML frontmatter 未闭合（缺少第二条 --- 分隔线）")

    try:
        # safe_load 而非 load：角色定义可能来自团队仓库甚至第三方，
        # 绝不能允许 YAML 里的任意对象构造。
        parsed = yaml.safe_load("".join(lines[start + 1 : end]))
    except yaml.YAMLError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else "未知错误"
        raise AgentParseError(f"frontmatter 解析失败：{first}") from exc

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise AgentParseError("frontmatter 顶层必须是键值映射")

    return parsed, "".join(lines[end + 1 :])


def _normalize_keys(front: dict) -> dict:
    """
    把 frontmatter 的键名归一到「小写 + 下划线」形态。

    :param front: 原始映射
    :returns: 归一后的新映射

    认两种写法（`disallowed-tools` 与 `disallowed_tools`）的理由与 C11 相同：
    用户是从别处复制来的定义，不该因为一个字符的写法而加载失败。
    同一逻辑键两种写法并存时**以连字符版为准**——那是 Claude Code 的标准写法。

    副作用：无。
    """
    out: dict = {}
    # 先放下划线版，再让连字符版覆盖，从而实现「连字符优先」
    for key, value in front.items():
        if isinstance(key, str) and "-" not in key:
            out[key.strip().lower()] = value
    for key, value in front.items():
        if isinstance(key, str) and "-" in key:
            out[key.strip().lower().replace("-", "_")] = value
    return out


def _as_name_list(value: Any) -> tuple[str, ...]:
    """
    把 `tools` / `disallowed_tools` 的取值转成名字元组。

    :param value: YAML 列表、逗号分隔字符串，或其它
    :returns: 逐项 strip 后的非空名字元组

    两种写法都要认：Claude Code 文档里的例子是逗号分隔字符串
    （`tools: Read, Grep, Glob`），而 YAML 列表是更自然的写法。
    只认一种会让另一种**静默变成一个名字叫「Read, Grep, Glob」的工具**——
    它匹配不到任何东西，最终工具集为空，用户看到的是「委派怎么直接失败了」。

    副作用：无。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        parts = [str(value)]
    return tuple(p.strip() for p in parts if str(p).strip())


def _parse_max_turns(value: Any, warnings: list[str]) -> int:
    """
    读取并夹取轮次上限。

    :param value: frontmatter 取值
    :param warnings: 输出参数，越界或非法时追加一条中文提示
    :returns: 夹在 `[1, HARD_MAX_TURNS]` 内的整数

    **越界只夹不报错**：用户写 `max_turns: 999` 的意图很清楚（「让它多跑一会儿」），
    为此拒绝整份定义不成比例。但必须告知夹到了多少——否则他会以为真的能跑 999 轮，
    而实际在第 25 轮被截断时看到的是一句「触达迭代上限」，无从联系到这里。

    副作用：可能向 `warnings` 追加。
    """
    if value is None:
        return DEFAULT_MAX_TURNS

    try:
        # 先转 int：YAML 可能给出 "15" 这种带引号的写法。bool 是 int 的子类，
        # 单独挡掉——`max_turns: true` 会被 int() 转成 1，静默变成「只跑一轮」。
        if isinstance(value, bool):
            raise ValueError
        turns = int(value)
    except (TypeError, ValueError):
        warnings.append(
            f"max_turns 的取值 {value!r} 不是整数，已按缺省值 {DEFAULT_MAX_TURNS} 处理"
        )
        return DEFAULT_MAX_TURNS

    if turns < 1:
        warnings.append(f"max_turns 至少为 1，{turns} 已提升为 1")
        return 1
    if turns > HARD_MAX_TURNS:
        warnings.append(
            f"max_turns 上限为 {HARD_MAX_TURNS}，{turns} 已夹到 {HARD_MAX_TURNS}"
        )
        return HARD_MAX_TURNS
    return turns


def _parse_permission_mode(
    value: Any, warnings: list[str]
) -> Optional[PermissionMode]:
    """
    读取权限档位声明。

    :param value: frontmatter 取值
    :param warnings: 输出参数
    :returns: 对应的 `PermissionMode`；未声明或不认识时 `None`（= 继承）

    **认不出来时按「继承」处理而不是按「最严」**：按最严会让一个拼写错误
    （`permission_mode: strick`）静默把角色降级成只读，用户看到的是
    「我的角色怎么什么都干不了」，而定义文件里明明写着别的。继承 + 一条警告，
    现象与提示才对得上。

    注意声明**放行档不产生提权效果**（spec F16），实际生效档位由
    `narrower_mode(主对话档, 本值)` 算出——本函数只负责读。

    副作用：可能向 `warnings` 追加。
    """
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw == _INHERIT:
        return None
    for mode in PermissionMode:
        if mode.value == raw:
            return mode
    warnings.append(
        f"permission_mode 的取值 {value!r} 不认识"
        f"（可选：{' / '.join(m.value for m in PermissionMode)}），已按「继承」处理"
    )
    return None


def parse_agent(
    text: str,
    path: Path,
    source: AgentSource,
    fallback_name: str,
) -> AgentSpec:
    """
    把一份角色定义文本解析成 `AgentSpec`（spec F1）。

    :param text: 文件全文
    :param path: 文件路径，写进产出的 spec（报告与排错用）
    :param source: 该文件所在的层
    :param fallback_name: frontmatter 没写 `name` 时用的名字，通常是去扩展名的文件名
    :returns: 解析结果
    :raises AgentParseError: 缺 frontmatter / YAML 坏 / 缺 description / 名字非法

    执行步骤：

    1. 切 frontmatter 与正文；
    2. 键名归一（连字符 → 下划线、大小写不敏感）；
    3. `description` 必填校验；
    4. `name` 取值与合法性校验（缺省回落到 `fallback_name`）；
    5. `tools` / `disallowed_tools` / `model` / `max_turns` / `permission_mode` 读取；
    6. 未支持字段逐个产出具名警告。

    **正文可以为空**（与 C11 的 Skill 不同）：一个只有 `description` 和
    `tools` 白名单的角色是有意义的——它靠工具集收窄来定义行为，
    系统提示交给缺省环境信息即可。硬性要求正文会挡掉这种合法用法。

    副作用：无。不读写文件、不改全局状态。
    """
    warnings: list[str] = []

    front, body = _split_frontmatter(text)
    front = _normalize_keys(front)

    # ── description：唯一的必填字段 ──
    description = str(front.get("description") or "").strip()
    if not description:
        raise AgentParseError(
            "缺少 description 字段（它是主 Agent 判断「什么时候该委派给这个角色」的唯一依据）"
        )

    # ── name：缺省回落文件名 ──
    raw_name = front.get("name")
    name = str(raw_name).strip() if raw_name is not None else fallback_name.strip()
    if not name:
        raise AgentParseError("name 为空")
    for ch in _ILLEGAL_NAME_CHARS:
        if ch in name:
            raise AgentParseError(f"name 不能包含 {ch!r}：{name!r}")
    if any(c.isspace() for c in name):
        raise AgentParseError(f"name 不能包含空白字符：{name!r}")

    # ── tools：None（未声明，继承）与 ()（声明了但为空）语义不同 ──
    #
    # 这个区分不能省：未声明表示「给我主对话有的全部工具」，
    # 而声明了空列表表示「一个都不给」——后者会让最终工具集为空、
    # 委派立即失败（spec F14），那是用户写错了该被告知的情形。
    tools: Optional[tuple[str, ...]] = None
    if "tools" in front and front["tools"] is not None:
        tools = _as_name_list(front["tools"])

    disallowed = _as_name_list(front.get("disallowed_tools"))

    # ── model：inherit 与不写等价 ──
    raw_model = front.get("model")
    model = str(raw_model).strip() if raw_model is not None else ""
    if not model or model.lower() == _INHERIT:
        model = ""

    max_turns = _parse_max_turns(front.get("max_turns"), warnings)
    permission_mode = _parse_permission_mode(front.get("permission_mode"), warnings)

    # ── 未支持字段：逐个具名告知 ──
    for key, reason in UNSUPPORTED_FIELDS.items():
        if key in front:
            warnings.append(f"本项目不支持 {key} 字段，已忽略——{reason}")

    return AgentSpec(
        name=name,
        description=description,
        body=body.strip(),
        source=source,
        path=path,
        tools=tools,
        disallowed_tools=disallowed,
        model=model or None,
        max_turns=max_turns,
        permission_mode=permission_mode,
        warnings=tuple(warnings),
    )


__all__ = ["AgentParseError", "parse_agent"]
