"""
规则解析与集中校验（spec F8）：把一层 YAML 结构翻译成 `HookRule` 列表 + 警告列表。

本模块**零 I/O**：输入是已经 `yaml.safe_load` 出来的 Python 对象，不碰文件系统。
文件定位与读取在 `config.py`，这样解析逻辑可以脱离磁盘被完整单测覆盖（spec N5）。

## 配置格式

    hooks:
      - name: 格式化改动的 Python 文件      # 可选，缺省生成 "<来源层>#<序号>"
        event: post_tool_use               # 必填
        if:                                # 可选，省略 = 无条件触发
          all:                             # all / any 二选一，不混用不嵌套
            - tool: edit_file
            - file_path: "**/*.py"
        action:                            # 必填
          type: command
          command: "black ..."
          timeout: 30
        once: false                        # 可选
        async: false                       # 可选

## 校验哲学：丢弃 + 可见

任一硬校验项不通过 → **丢弃该条规则**，往警告列表追加一条中文说明，继续解析其余规则。

**这个方向偏松，是刻意接受的**（spec F8 已登记）。丢弃一条 `pre_tool_use` 拦截规则
等于「少拦一次」——与 C6 里「deny 规则写坏就降级为整工具拒绝」那种偏严处理不同。
之所以只能这样：Hook 规则**没有可降级的偏严形态**。一条条件写坏的拦截规则，
唯一「偏严」的处理是让它恒成立（即拦掉一切），那会让一个笔误瘫痪整个 Agent。
补偿手段是让它**可见**：警告在启动提示与 `/hooks` 报告里都出现。

## 加载期校验七项（spec F8 的 1–7）

1. `event` 是十二个之一
2. `action.type` 是四个之一，且必填字段齐全、类型正确
3. `if` 下 `all` / `any` 恰好一个，不嵌套
4. 每个字段名属于该事件的字段集（三个工具级事件放行，见 `OPEN_INPUT_EVENTS`）
5. 正则能被编译（由 `conditions.parse_matcher` 顺带完成）
6. `pre_tool_use` 未声明 `async: true`
7. `timeout` 是正数

**spec F8 的第 8 项不在这里**——它要求「非 pre_tool_use 事件挂了会产出决策的动作时
给提示级警告」，但唯一「会产出决策」的动作是 `command`，而 `post_tool_use` + `command`
正是最常见的正当用法（自动格式化）。照字面实现会给每一条格式化 Hook 都挂一条警告，
警告区随即失去可读性。该项已移到**运行期**：只有当一条非 `pre_tool_use` 的 Hook
真的返回了决策，才在执行结果的 `detail` 里注明「该结论已忽略」（见 `actions.py`）。
信息更准、零噪音，spec 那句「说明用户可能误解了拦截只在一个事件上生效」的意图不变。
"""

from typing import Any, Optional

from rhinecode.hooks.conditions import parse_matcher
from rhinecode.hooks.models import (
    ACTION_AGENT,
    ACTION_COMMAND,
    ACTION_HTTP,
    ACTION_PROMPT,
    ACTION_TYPES,
    COMBINE_ALL,
    COMBINE_ANY,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_HTTP_TIMEOUT,
    EVENT_FIELDS,
    OPEN_INPUT_EVENTS,
    AgentAction,
    CommandAction,
    Condition,
    HookAction,
    HookEventType,
    HookRule,
    HttpAction,
    Matcher,
    PromptAction,
)

# 顶层键：整份配置的规则列表挂在它下面。
ROOT_KEY = "hooks"


def _fmt_event_values() -> str:
    """把十二个合法事件名拼成一行，供错误说明使用。"""
    return "、".join(e.value for e in HookEventType)


def _parse_timeout(raw: Any, default: int, label: str) -> tuple[Optional[int], Optional[str]]:
    """
    解析并校验 `timeout`（spec F8 第 7 项）。

    :param raw: 原始值；None 表示未声明，取 default
    :param default: 该动作类型的缺省超时
    :param label: 出错时展示用的动作类型名
    :returns: `(秒数, None)` 或 `(None, 中文错误说明)`

    **布尔要单独挡掉**：Python 里 `isinstance(True, int)` 是 True，
    不挡的话 `timeout: true` 会被当成 1 秒——一个几乎必然超时的 Hook，
    而用户完全看不出哪里错了。

    副作用：无。
    """
    if raw is None:
        return default, None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None, f"{label} 动作的 timeout 必须是正整数（秒），当前是 {raw!r}。"
    if raw <= 0:
        return None, f"{label} 动作的 timeout 必须大于 0，当前是 {raw}。"
    return raw, None


def _require_text(raw: dict, key: str, label: str) -> tuple[Optional[str], Optional[str]]:
    """
    取一个必填的非空字符串字段。

    :returns: `(值, None)` 或 `(None, 中文错误说明)`
    """
    value = raw.get(key)
    if value is None:
        return None, f"{label} 动作缺少必填字段 `{key}`。"
    if not isinstance(value, str) or not value.strip():
        return None, f"{label} 动作的 `{key}` 必须是非空字符串，当前是 {value!r}。"
    return value, None


def parse_action(raw: Any) -> tuple[Optional[HookAction], Optional[str]]:
    """
    把 YAML 里的 `action` 映射翻译成动作对象（spec F8 第 2、7 项）。

    :param raw: `action` 的原始值
    :returns: `(动作, None)` 或 `(None, 中文错误说明)`——**两者恰有一个非 None**

    四种类型各自的必填字段：`command` 要 `command`、`prompt` 要 `text`、
    `http` 要 `url`、`agent` 要 `prompt`。可选字段填缺省值。

    副作用：无。
    """
    if not isinstance(raw, dict):
        return None, f"`action` 必须是映射，当前是 {type(raw).__name__}。"

    kind = raw.get("type")
    if kind not in ACTION_TYPES:
        return None, (
            f"`action.type` 必须是 {'、'.join(ACTION_TYPES)} 之一，当前是 {kind!r}。"
        )

    if kind == ACTION_COMMAND:
        command, err = _require_text(raw, "command", "command")
        if err:
            return None, err
        timeout, err = _parse_timeout(raw.get("timeout"), DEFAULT_COMMAND_TIMEOUT, "command")
        if err:
            return None, err
        return CommandAction(command, timeout), None

    if kind == ACTION_PROMPT:
        text, err = _require_text(raw, "text", "prompt")
        if err:
            return None, err
        return PromptAction(text), None

    if kind == ACTION_AGENT:
        prompt, err = _require_text(raw, "prompt", "agent")
        if err:
            return None, err
        return AgentAction(prompt), None

    # ACTION_HTTP
    url, err = _require_text(raw, "url", "http")
    if err:
        return None, err
    timeout, err = _parse_timeout(raw.get("timeout"), DEFAULT_HTTP_TIMEOUT, "http")
    if err:
        return None, err

    method = raw.get("method", "POST")
    if not isinstance(method, str) or not method.strip():
        return None, f"http 动作的 `method` 必须是非空字符串，当前是 {method!r}。"

    headers_raw = raw.get("headers")
    if headers_raw is None:
        headers: tuple[tuple[str, str], ...] = ()
    elif isinstance(headers_raw, dict):
        # 转成有序元组对：HttpAction 是 frozen dataclass，要可哈希，而 dict 不可哈希。
        headers = tuple((str(k), str(v)) for k, v in headers_raw.items())
    else:
        return None, f"http 动作的 `headers` 必须是映射，当前是 {type(headers_raw).__name__}。"

    body = raw.get("body")
    if body is not None and not isinstance(body, str):
        return None, f"http 动作的 `body` 必须是字符串，当前是 {type(body).__name__}。"

    return HttpAction(url, method.strip().upper(), headers, body, timeout), None


def parse_condition(
    raw: Any, event: HookEventType
) -> tuple[Optional[Condition], Optional[str]]:
    """
    把 YAML 里的 `if` 映射翻译成 `Condition`（spec F8 第 3、4、5 项）。

    :param raw: `if` 的原始值
    :param event: 该规则的事件，用于字段名校验
    :returns: `(条件, None)` 或 `(None, 中文错误说明)`

    校验：
    - 必须是映射，且**恰好**含 `all` / `any` 之一（spec F3.1：二选一，不混用）
    - 组合词的值必须是非空列表
    - 每个元素是「字段: 模式」的映射；一个元素里写多个键即多个匹配项（自然的 YAML 写法）
    - 字段名须属于该事件的字段集——**三个工具级事件除外**，它们的字段集是开放的
      （`tool_input` 的键取决于是哪个工具，加载期不可能枚举，见 `OPEN_INPUT_EVENTS`）

    副作用：无。
    """
    if not isinstance(raw, dict):
        return None, f"`if` 必须是映射，当前是 {type(raw).__name__}。"

    present = [k for k in (COMBINE_ALL, COMBINE_ANY) if k in raw]
    if len(present) != 1:
        return None, (
            f"`if` 下必须恰好写一个 `{COMBINE_ALL}` 或 `{COMBINE_ANY}`"
            f"（二选一，不混用、不嵌套），当前写了 {len(present)} 个。"
        )
    # 除组合词外不允许其它键——多写的键是笔误，静默忽略会让用户以为它生效了。
    extra = sorted(set(raw) - {present[0]})
    if extra:
        return None, f"`if` 下出现了无法识别的键：{'、'.join(extra)}。"

    combine = present[0]
    items = raw[combine]
    if not isinstance(items, list) or not items:
        return None, f"`if.{combine}` 必须是非空列表。"

    allowed = EVENT_FIELDS[event]
    open_fields = event in OPEN_INPUT_EVENTS

    matchers: list[Matcher] = []
    for item in items:
        if not isinstance(item, dict) or not item:
            return None, f"`if.{combine}` 的每一项必须是「字段: 模式」的映射。"
        for field_name, pattern in item.items():
            name = str(field_name)
            if not open_fields and name not in allowed:
                return None, (
                    f"事件 `{event.value}` 没有字段 `{name}`。"
                    f"可用字段：{'、'.join(sorted(allowed))}。"
                )
            matcher, err = parse_matcher(name, pattern)
            if err:
                return None, err
            matchers.append(matcher)  # type: ignore[arg-type]

    return Condition(combine, tuple(matchers)), None


def _parse_rule(
    raw: Any, source: str, index: int
) -> tuple[Optional[HookRule], Optional[str]]:
    """
    解析单条规则（spec F8 的 1–7 项在此汇合）。

    :param raw: 规则的原始映射
    :param source: 来源层（"user" / "project"）
    :param index: 层内声明序（从 0 开始）
    :returns: `(规则, None)` 或 `(None, 中文警告)`

    警告文案里**必须带规则标识**——一份配置里可能有十几条规则，只说「event 非法」
    用户根本不知道是哪一条。标识用显式 `name`，没写时用生成的 `<来源层>#<序号>`。

    副作用：无。
    """
    label = f"{source}#{index + 1}"
    if not isinstance(raw, dict):
        return None, f"Hook 规则 {label}（来源：{source}）不是映射，已丢弃。"

    # 规则标识优先取显式 name，供后续所有错误文案使用。
    name_raw = raw.get("name")
    name = str(name_raw).strip() if isinstance(name_raw, str) and name_raw.strip() else label
    prefix = f"Hook 规则 `{name}`（来源：{source}）"

    # ── 第 1 项：event ──
    event_raw = raw.get("event")
    if event_raw is None:
        return None, f"{prefix} 缺少必填字段 `event`，已丢弃。"
    try:
        event = HookEventType(str(event_raw))
    except ValueError:
        return None, (
            f"{prefix} 的 `event` 取值 {event_raw!r} 不是合法事件，已丢弃。"
            f"合法取值：{_fmt_event_values()}。"
        )

    # ── 第 2、7 项：action ──
    if "action" not in raw:
        return None, f"{prefix} 缺少必填字段 `action`，已丢弃。"
    action, err = parse_action(raw["action"])
    if err:
        return None, f"{prefix} {err} 已丢弃。"

    # ── 第 3、4、5 项：if（可省）──
    condition: Optional[Condition] = None
    if raw.get("if") is not None:
        condition, err = parse_condition(raw["if"], event)
        if err:
            return None, f"{prefix} {err} 已丢弃。"

    # ── 执行控制 ──
    once = bool(raw.get("once", False))
    run_async = bool(raw.get("async", False))

    # ── 第 6 项：pre_tool_use 禁止 async ──
    #
    # 异步意味着「不等结果」，而拦截的全部意义就是等结果。允许这个组合的话，
    # 一条本意为「拦住危险命令」的规则会变成「一边放行一边在后台检查」——
    # 检查出问题时命令早就跑完了。
    if event == HookEventType.PRE_TOOL_USE and run_async:
        return None, (
            f"{prefix} 在 `pre_tool_use` 上声明了 `async: true`，已丢弃。"
            f"拦截类事件必须等待结果，异步执行会让拦截在命令跑完之后才得出结论。"
        )

    return (
        HookRule(
            name=name,
            source=source,
            index=index,
            event=event,
            condition=condition,
            action=action,  # type: ignore[arg-type]
            once=once,
            run_async=run_async,
        ),
        None,
    )


def parse_rules(data: Any, source: str) -> tuple[list[HookRule], list[str]]:
    """
    解析一层配置的全部规则。

    :param data: 已 `yaml.safe_load` 的顶层对象
    :param source: 来源层标记（"user" / "project"），写入每条规则的 `source`
    :returns: `(规则列表, 警告列表)`

    ## ⚠ 两处「整层降级」的行为一个字都不许动

    照搬 `permission/config.py` 的既有结构，理由完全相同：

    | 位置 | 性质 | 行为 |
    |---|---|---|
    | 顶层非映射 | 整文件级 | **整层降级为空并 return** |
    | `hooks` 存在但非列表 | 整字段级 | **整层降级为空并 return** |
    | 单条规则解析失败 | 单条级 | 跳过该条、继续解析其余 |

    **第二行尤其危险**：若把它改成「记下警告后继续」，一个 `hooks:` 写成映射
    （少写了列表的 `-`）的文件会变成「零条规则生效但用户以为全都生效了」。
    整层降级至少让规则数明确为 0，配合警告能立刻看出问题。

    `data` 为 None（空文件或全注释文件）时返回空规则集且**无警告**——
    这使「有模板」与「无文件」完全等价（模板全部是注释）。

    副作用：无。
    """
    if data is None:
        return [], []
    if not isinstance(data, dict):
        return [], [f"Hook 配置（来源：{source}）顶层应为映射，整层已忽略。"]

    items = data.get(ROOT_KEY)
    if items is None:
        return [], []
    if not isinstance(items, list):
        # 整字段级失败 → 整层降级为空。**不要改成 continue**（见上方表格）。
        return [], [f"Hook 配置（来源：{source}）的 `{ROOT_KEY}` 应为列表，整层已忽略。"]

    rules: list[HookRule] = []
    warnings: list[str] = []
    for index, raw in enumerate(items):
        rule, warning = _parse_rule(raw, source, index)
        if warning:
            warnings.append(warning)
        if rule is not None:
            rules.append(rule)
    return rules, warnings


__all__ = ["ROOT_KEY", "parse_action", "parse_condition", "parse_rules"]
