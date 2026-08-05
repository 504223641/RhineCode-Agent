"""
条件求值层（spec F3）：把 `if` 里写的「字段 = 模式」翻译成可执行的匹配项，并对一次事件负载求值。

本模块**零 I/O**（spec N5）：给定「条件 + 负载字典」即可判定，不碰文件系统、不发网络请求。
这既是可测性要求，也是性能要求——`pre_tool_use` 的条件会在**每一次工具调用**上求值。

## 两个公开函数，分属两个时机

- `parse_matcher`  —— **加载期**调用一次。识别匹配形态、编译正则、产出 `Matcher`。
- `evaluate`       —— **求值期**每次事件分发调用。只做分支执行，不再解析、不再编译。

这个切分是刻意的：把「判形态」和「编译正则」留到求值期，意味着重复做成千上万次
纯粹的解析工作；放在加载期还顺带完成了 spec F8 第 5 项校验（正则编译不过就是加载期
错误，用户立刻知道，而不是等到某次事件触发时才失败）。

## 四种匹配形态（spec F3.3）

| 形态 | 写法 | 语义 |
| --- | --- | --- |
| 精确 | `tool: run_command` | 字符串完全相等（大小写敏感） |
| glob | `command: "git push *"` | 按字段类型选算法，见 `FIELD_MATCH_KIND` |
| 正则 | `command: "/^git (push\\|reset)/"` | 一对 `/` 包裹，**非锚定**（要整串匹配自己写 `^...$`） |
| 反向 | `tool: "!run_command"` | 值以 `!` 开头，可与上面三种叠加 |
"""

import fnmatch
import re
from typing import Any, Optional

from rhinecode.hooks.models import (
    COMBINE_ALL,
    COMBINE_ANY,
    FIELD_MATCH_KIND,
    MATCH_KIND_PLAIN,
    MATCHER_EXACT,
    MATCHER_GLOB,
    MATCHER_REGEX,
    Condition,
    Matcher,
)
from rhinecode.permission.matching import match_command, match_path, split_commands


def stringify(value: Any) -> str:
    """
    把任意负载字段值或 YAML 模式值归一化成用于匹配的字符串。

    :param value: 原始值（字符串 / 布尔 / 数字 / 其它）
    :returns: 归一化后的字符串

    **布尔单独处理**：Python 的 `str(True)` 是 `"True"`（首字母大写），而用户在 YAML 里
    写的是 `true`。不特判的话，一条 `is_read_only: true` 的条件永远匹配不上——
    而且它**不报错**，只是那条规则悄悄永不命中。

    **本函数被两处共用**（模式侧与字段值侧），这是刻意的：两边归一化口径必须一致，
    否则 `is_read_only: true` 会在模式侧变成 `"true"`、在字段值侧变成 `"True"`，
    永远对不上。各写一份是典型的「改一处漏一处」。

    副作用：无。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return str(value)


def parse_matcher(field: str, raw: Any) -> tuple[Optional[Matcher], Optional[str]]:
    """
    把一条「字段: 模式」翻译成 `Matcher`（加载期调用）。

    :param field: 字段名（条件左侧）
    :param raw: 模式原文（条件右侧，可能是 YAML 解析出的非字符串值）
    :returns: `(Matcher, None)` 或 `(None, 中文错误说明)`——**两者恰有一个非 None**

    执行步骤：
    1. 归一化成字符串（`stringify`，与字段值侧同口径）。
    2. 剥一个 `!` 前缀置 `negated`。**只剥一个**——`!!x` 表示「反向匹配字面量 `!x`」，
       这样用户仍有办法匹配以 `!` 开头的值。
    3. 判 `/.../` 包裹 → 正则形态，剥掉首尾 `/` 后 `re.compile`。
    4. 否则含 `*` → glob 形态；不含 → 精确形态。
    5. 剥完为空串 → 错误。

    :raises: 不抛异常。正则编译失败走返回值的错误分支——**加载期的错误必须是可收集的
             警告，不能是异常**，否则一条写坏的规则会阻断整份配置的加载（spec F8：
             丢弃该条、其余照常）。

    副作用：无。
    """
    text = stringify(raw).strip()
    if not text:
        return None, f"条件字段 `{field}` 的模式为空。"

    # ① 反向前缀。只剥一个，保留「匹配字面 `!` 开头的值」的写法。
    negated = text.startswith("!")
    if negated:
        text = text[1:]
        if not text:
            return None, f"条件字段 `{field}` 只有一个 `!`，缺少模式内容。"

    # ② 正则形态：一对 `/` 包裹。长度须 >= 2，否则单个 `/` 会被误判成空正则。
    if len(text) >= 2 and text.startswith("/") and text.endswith("/"):
        body = text[1:-1]
        if not body:
            return None, f"条件字段 `{field}` 的正则为空（`//`）。"
        try:
            compiled = re.compile(body)
        except re.error as exc:
            return None, f"条件字段 `{field}` 的正则无法编译（{body}）：{exc}"
        return Matcher(field, negated, MATCHER_REGEX, body, compiled), None

    # ③ glob 与精确：只看有没有 `*`。
    kind = MATCHER_GLOB if "*" in text else MATCHER_EXACT
    return Matcher(field, negated, kind, text), None


def _match_command_field(text: str, predicate) -> bool:
    """
    命令类字段的匹配：**整条 + 逐段双重检查**（与①危险命令黑名单同口径）。

    :param text: 字段值（可能是一条复合命令）
    :param predicate: 对单条命令做判定的函数
    :returns: 整条命中、或任一子命令命中即为 True

    ## ⚠ 这一半不能省，真实模型自己就会撞上

    `match_command` 是**整串匹配**，不拆复合命令（它的 docstring 明写「调用方按需
    先用 `split_commands` 拆段」）。只调它的话，一条

        if: {all: [{tool: run_command}, {command: "git push *"}]}

    的拦截规则会被

        git add x && git commit -m "..." && git push origin main

    整个绕过——而**这不是攻击者构造的**，是真实模型在一次普通「改完提交推上去」
    的请求里自然产出的形态（C12 验收期实测，deepseek-v4-flash）。

    后果比「少拦一次」更糟：`/hooks` 报告里那条规则显示「触发：0 次」，
    用户会据此认定「模型压根没试过 push」，而它其实推了。**规则静默失效**。

    ①黑名单早就是「逐段 + 整条」双重检查的（防 `safe && rm -rf`），
    这里只是把同一口径补齐。方向偏严——对一个只能拦截/升级、不能放行的系统而言，
    偏严永远是安全的那一侧。

    副作用：无。
    """
    if predicate(text):
        return True
    segments = split_commands(text)
    # 单段时 `split_commands` 返回的就是它自己（可能去了空白），上面已判过。
    if len(segments) <= 1:
        return False
    return any(predicate(seg) for seg in segments)


def _match_glob(matcher: Matcher, text: str) -> bool:
    """
    glob 形态的匹配，按字段类型选算法（spec F3.3）。

    :param matcher: 已解析的匹配项
    :param text: 已归一化的字段值
    :returns: 命中返回 True

    三种算法：
    - `"command"` → `permission.matching.match_command`：前缀 + **词边界**，
      因此 `git *` 命中 `git status` 但**不**命中 `github-cli`
    - `"path"`    → `permission.matching.match_path`：gitignore 风格，
      `**/*.py` 跨任意层，大小写不敏感（跨平台）
    - 其余        → `fnmatch.fnmatchcase`

    **通用分支用 `fnmatchcase` 而不是 `fnmatch`** ——后者会先过 `os.path.normcase`，
    在 Windows 上退化成大小写不敏感、在 POSIX 上保持敏感。同一份 `hooks.yaml`
    在两个平台上行为不同，而配置和界面上都看不出任何异常。命令类与路径类的
    大小写语义已由各自算法定死（前者敏感、后者不敏感），这里只需保证通用分支
    **确定**即可。

    副作用：无。
    """
    kind = FIELD_MATCH_KIND.get(matcher.field, MATCH_KIND_PLAIN)
    if kind == "command":
        return _match_command_field(
            text, lambda one: match_command(matcher.pattern, one)
        )
    if kind == "path":
        return match_path(matcher.pattern, text)
    return fnmatch.fnmatchcase(text, matcher.pattern)


def _match_one(matcher: Matcher, fields: dict[str, Any]) -> bool:
    """
    对单个匹配项求值。

    :param matcher: 已解析的匹配项
    :param fields: 事件负载的字段字典
    :returns: 该匹配项是否成立

    ## ⚠ 缺失字段的语义：不成立，**反向匹配也不成立**

    字段不在负载里、或值为 `None` 时**直接返回 False**，`negated` 完全不参与。

    这条容易写反。直觉上「`!x` 是 `x` 的否定，`x` 不成立那 `!x` 就该成立」——
    但那样一条 `if: {all: [{command: "!git *"}]}` 的规则会在**所有非命令类事件**上
    突然成立（那些事件压根没有 `command` 字段），于是一条本意为「除了 git 命令之外的命令」
    的规则变成了「几乎所有东西」。

    正确的心智模型是：「字段不存在」表示**这个条件与本次事件无关**，无关就是不成立，
    与匹配结果的正反无关。实现上必须写成**在取反之前提前返回**，不能让 `negated`
    有机会翻转缺失分支。

    副作用：无。
    """
    # 缺失分支必须在取反之前提前返回。
    if matcher.field not in fields:
        return False
    value = fields[matcher.field]
    if value is None:
        return False

    text = stringify(value)

    if matcher.kind == MATCHER_EXACT:
        # 命令类字段同样走「整条 + 逐段」——一条写成 `command: "git push"` 的
        # 精确规则，同样不该被 `git status && git push` 绕过。
        if FIELD_MATCH_KIND.get(matcher.field) == "command":
            result = _match_command_field(text, lambda one: one == matcher.pattern)
        else:
            result = text == matcher.pattern
    elif matcher.kind == MATCHER_REGEX:
        # `search` 而非 `fullmatch`：spec F3.3 明确正则是**非锚定**的，
        # 要整串匹配由用户自己写 `^...$`（与 Claude Code 的 matcher 同口径）。
        result = matcher.regex is not None and matcher.regex.search(text) is not None
    else:
        result = _match_glob(matcher, text)

    return (not result) if matcher.negated else result


def evaluate(condition: Optional[Condition], fields: dict[str, Any]) -> bool:
    """
    对一条规则的条件表达式求值（求值期调用）。

    :param condition: 条件；`None` 表示规则省略了 `if`
    :param fields: 事件负载的字段字典
    :returns: 条件是否满足（满足则该规则本次命中）

    - `condition is None` → 恒真（spec F3.4「省略 if = 无条件触发」）
    - `all` → 全部匹配项成立（短路）
    - `any` → 任一匹配项成立（短路）

    空匹配列表的行为是**防御性定义**：`all` 恒真、`any` 恒假（Python 内建语义）。
    加载期已保证 `matchers` 非空，正常路径走不到这里；写明只是为了让读代码的人
    不必去推断，也不必担心它悄悄变成另一种行为。

    副作用：无。
    """
    if condition is None:
        return True
    if condition.combine == COMBINE_ANY:
        return any(_match_one(m, fields) for m in condition.matchers)
    # COMBINE_ALL 是缺省分支：加载期已校验 combine 只可能是两个取值之一，
    # 这里不为未知取值单开分支——真出现未知值时按更严的 all 处理，偏严安全。
    return all(_match_one(m, fields) for m in condition.matchers)


__all__ = ["stringify", "parse_matcher", "evaluate"]
