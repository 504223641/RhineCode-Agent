"""
断言层：把「这次运行到底发生了什么」变成可断言的十一项词汇（spec F24–F26）。

## 事实来源的裁决（为什么不是所有东西都从记录里读）

| # | 词汇 | 事实来源 | 为什么是它 |
| --- | --- | --- | --- |
| ① | 某轮发出的工具名集合 | 假模型的 `RecordedCall` | 记录里的 `tool_names` 会被截断 |
| ② | 某工具是否执行过及结局 | 记录 `tool_execute` | |
| ③ | 界面消息含某文本 | 记录 `ui_message` | |
| ④ | 状态栏含 / 不含某段 | 记录 `status_bar` | |
| ⑤ | 权限决策的层与结果 | 记录 `permission_decision` | |
| ⑥ | 事件的作用域归属 | 任意事件的 `scope` | |
| ⑦ | 两事件先后 | `seq` 比较 | |
| ⑧ | 某类事件条数 | 记录计数 | |
| ⑨ | 主历史消息条数 | **会话存档 JSONL** | 十五类事件没有一类承载它 |
| ⑩ | 稳定系统提示含某文本 | 假模型的 `RecordedCall.system` | 记录里 system 受截断 |
| ⑪ | 动态提醒含某文本 | 假模型的 `RecordedCall.dynamic_reminder` | 同上，且它与 system 分处两个位置 |

①⑩⑪ 取自假模型而非记录，理由是**记录会截断**：`clip` 把长字段压到 4000 字并换成
`{text, truncated, original_length}` 结构。要断言「第 3 轮到底发了哪些工具」或
「SOP 正文进没进动态提醒」，截断过的副本不够用；假模型手里的是**原始参数对象**。

⑨ 取自会话存档，是因为十五类事件里**没有任何一类**承载「当前主历史有多少条消息」，
而记录里的请求消息列表同时受条数与长度双重截断，数它等于数一个不完整的样本。

## F24 硬要求：读记录一律复用 `rhinecode.trace.reader`

`TraceView` 内部调 `reader.load_records` / `reader.filter_records`，**绝不自行
`json.loads` 记录文件**。两边各写一份解析逻辑必然漂移——比如 reader 后来改了坏行
的判定口径，这里不跟着改，于是断言层报告的 `skipped` 与阅读器给出的对不上，
而这种偏差几乎不可能被发现。

`test_e2e_assertions.py` 用源码扫描把这条钉死：本文件里除 `check_history_len`
（它读的是**会话存档**不是记录）之外**不允许出现 `json.loads`**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from rhinecode.trace import reader


PathLike = Union[str, Path]

# 失败诊断时，每个证据序号前后各展开几条事件
NEIGHBOR_RADIUS = 3


# ---------------------------------------------------------------------------
# 记录视图
# ---------------------------------------------------------------------------
class TraceView:
    """
    对一份记录产物的**只读**查询视图。

    加载与过滤一律走 `rhinecode.trace.reader`（见模块 docstring 的 F24 说明）。
    """

    def __init__(self, records: list[dict], skipped: int, path: Optional[Path] = None):
        self.records = records
        self.skipped = skipped
        self.path = path

    @classmethod
    def load(cls, path: PathLike) -> "TraceView":
        """
        读入一份记录文件。

        :raises FileNotFoundError: 文件不存在（**不吞**：记录没产出本身就是重要结论，
            静默返回空视图会让所有断言以「没有这条事件」的形式失败，掩盖真正的根因）
        """
        p = Path(path)
        records, skipped = reader.load_records(p)
        return cls(records, skipped, p)

    def of_type(self, *types: str) -> list[dict]:
        """按事件类型过滤（多个取并集）。"""
        return list(reader.filter_records(self.records, types=set(types) if types else None))

    def in_scope(self, *scopes: str) -> "TraceView":
        """
        按作用域收窄出一个**新视图**（可与 `of_type` 链式组合，两者取交集）。

        返回新视图而不是列表，是为了让 `view.in_scope("main").of_type("api_request")`
        这样连写成立。
        """
        selected = list(
            reader.filter_records(self.records, scopes=set(scopes) if scopes else None)
        )
        return TraceView(selected, self.skipped, self.path)

    def by_seq(self, seq: int) -> dict:
        """取指定序号的那一条。"""
        for r in self.records:
            if r.get("seq") == seq:
                return r
        raise LookupError(f"记录里没有 seq={seq} 的事件（共 {len(self.records)} 条）")

    def nth(self, type_: str, n: int) -> dict:
        """
        取某类事件里的第 n 条（从 0 起）。

        :raises LookupError: 该类事件不足 n+1 条；消息里带上实际条数，
            省掉「为什么取不到」的第二轮排查。
        """
        matched = self.of_type(type_)
        if n >= len(matched):
            raise LookupError(
                f"{type_} 类事件只有 {len(matched)} 条，取不到第 {n} 条（从 0 起）"
            )
        return matched[n]

    def timeline(self) -> list[str]:
        """整份记录的时间线整行（复用 `reader.render_timeline`）。"""
        return reader.render_timeline(iter(self.records))


# ---------------------------------------------------------------------------
# 检查结果与失败诊断
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CheckResult:
    """
    一次检查的结果。

    :param ok: 是否通过
    :param message: 不通过时的说明（要写清「期望什么、实际是什么」）
    :param evidence_seqs: 相关事件的序号。失败时 `assert_check` 会把这些序号
        **前后各 3 条**事件的整行附在错误消息后面——落实 spec F26「失败可诊断」。
        空列表也允许（词汇 ①⑩⑪ 的来源不是记录，没有序号可指）。
    """

    ok: bool
    message: str = ""
    evidence_seqs: tuple[int, ...] = field(default_factory=tuple)


def _neighborhood(view: TraceView, seqs: Iterable[int]) -> list[str]:
    """
    取若干证据序号的邻域整行。

    ⚠️ **必须用 `reader.render_timeline` 而不是 `reader.summarize`**：
    后者只返回一句话摘要，而诊断需要的 `<seq> <ts> <scope> <type>` 前缀是
    `render_timeline` 产出的。看不到 seq 与 scope 的摘要行，在诊断里几乎没用
    （最常见的两个问题恰恰是「这条是第几号」和「它属于哪个作用域」）。

    邻域会**去重并按 seq 排序**：多个证据挨得近时邻域会重叠，不去重就刷屏。
    """
    wanted: set[int] = set()
    index_by_seq = {r.get("seq"): i for i, r in enumerate(view.records)}
    for seq in seqs:
        center = index_by_seq.get(seq)
        if center is None:
            continue
        lo = max(0, center - NEIGHBOR_RADIUS)
        hi = min(len(view.records), center + NEIGHBOR_RADIUS + 1)
        wanted.update(range(lo, hi))
    selected = [view.records[i] for i in sorted(wanted)]
    return reader.render_timeline(iter(selected))


def assert_check(result: CheckResult, view: Optional[TraceView] = None) -> None:
    """
    把一次检查结果转成断言。

    :raises AssertionError: 检查未通过。消息 = 检查说明 + 证据序号邻域的时间线整行。

    ⚠️ **不做任何降级**（spec N7）：不通过就是不通过，不重试、不放宽。
    """
    if result.ok:
        return
    parts = [result.message]
    if view is not None and result.evidence_seqs:
        parts.append(f"\n证据序号：{list(result.evidence_seqs)}，上下文时间线：")
        parts.extend(_neighborhood(view, result.evidence_seqs))
    raise AssertionError("\n".join(parts))


def _field_text(value: Any) -> str:
    """
    把一个可能被 `clip` 截断过的字段值取成可搜索的文本。

    截断后的形态是 `{"text": …, "truncated": True, "original_length": N}`，
    直接 `str()` 会得到一个带花括号的 dict 字面量，`in` 判断必然失败。
    """
    if isinstance(value, dict) and "text" in value:
        return str(value.get("text", ""))
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# 词汇 ②–⑧：取自行为记录
# ---------------------------------------------------------------------------
def check_tool_outcome(view: TraceView, tool_name: str, expected: str) -> CheckResult:
    """
    ② 某工具是否执行过、结局是什么。

    :param expected: 六个 outcome 之一——`executed` / `out_of_scope` /
        `unknown_tool` / `invalid_arguments` / `denied_by_permission` / `denied_by_user`
    """
    events = [e for e in view.of_type("tool_execute") if e.get("tool") == tool_name]
    if not events:
        seen = sorted({str(e.get("tool")) for e in view.of_type("tool_execute")})
        return CheckResult(
            False,
            f"记录里没有工具 {tool_name!r} 的执行事件；出现过的工具：{seen or '（一个也没有）'}",
            tuple(e.get("seq") for e in view.of_type("tool_execute")[:3]),
        )
    outcomes = [str(e.get("outcome")) for e in events]
    if expected not in outcomes:
        return CheckResult(
            False,
            f"工具 {tool_name!r} 的结局期望含 {expected!r}，实际是 {outcomes}",
            tuple(e.get("seq") for e in events),
        )
    return CheckResult(True, evidence_seqs=tuple(e.get("seq") for e in events))


def check_ui_contains(view: TraceView, text: str, *, source: Optional[str] = None) -> CheckResult:
    """
    ③ 界面上出现过含某文本的消息。

    :param source: 可选，限定消息来源（`assistant` / `user_echo` / `system` 等）
    """
    events = view.of_type("ui_message")
    if source is not None:
        events = [e for e in events if e.get("source") == source]
    for e in events:
        if text in _field_text(e.get("text")):
            return CheckResult(True, evidence_seqs=(e.get("seq"),))
    sample = [_field_text(e.get("text"))[:40] for e in events[-5:]]
    return CheckResult(
        False,
        f"界面消息里找不到 {text!r}"
        + (f"（限定 source={source}）" if source else "")
        + f"；共 {len(events)} 条界面消息，最后几条是：{sample}",
        tuple(e.get("seq") for e in events[-3:]),
    )


def check_status_bar(view: TraceView, text: str, *, present: bool = True) -> CheckResult:
    """
    ④ 状态栏**含 / 不含**某段文本。

    :param present: True 断言出现过；False 断言从未出现过（如「卸载 Skill 后
        状态栏不再有 Skill 段」这类否定断言）
    """
    events = view.of_type("status_bar")
    hits = [e for e in events if text in _field_text(e.get("text"))]
    if present and not hits:
        sample = [_field_text(e.get("text"))[:60] for e in events[-3:]]
        return CheckResult(
            False,
            f"状态栏从未出现过 {text!r}；共 {len(events)} 次刷新，最后几次是：{sample}",
            tuple(e.get("seq") for e in events[-3:]),
        )
    if not present and hits:
        return CheckResult(
            False,
            f"状态栏不应出现 {text!r}，但在 {len(hits)} 次刷新里出现了",
            tuple(e.get("seq") for e in hits[:3]),
        )
    return CheckResult(True, evidence_seqs=tuple(e.get("seq") for e in hits[:3]))


def check_permission(
    view: TraceView,
    tool_name: str,
    layer: Optional[str] = None,
    decision: Optional[str] = None,
) -> CheckResult:
    """
    ⑤ 某工具的权限决策命中了哪一层、判成了什么。

    :param layer: `blacklist` / `sandbox` / `rule` / `mode` 之一
    :param decision: `allow` / `deny` / `ask` 之一

    这是「不扩大权限面」那组护栏的读取入口：驱动器替人应答等于换了第⑤层的执行者，
    必须能证明第①层黑名单仍然拦得住。
    """
    events = [e for e in view.of_type("permission_decision") if e.get("tool") == tool_name]
    if not events:
        return CheckResult(False, f"记录里没有工具 {tool_name!r} 的权限决策事件")
    for e in events:
        if layer is not None and str(e.get("layer")) != layer:
            continue
        if decision is not None and str(e.get("decision")) != decision:
            continue
        return CheckResult(True, evidence_seqs=(e.get("seq"),))
    actual = [(str(e.get("layer")), str(e.get("decision"))) for e in events]
    return CheckResult(
        False,
        f"工具 {tool_name!r} 的权限决策期望 layer={layer} decision={decision}，实际是 {actual}",
        tuple(e.get("seq") for e in events),
    )


def check_scope(view: TraceView, seq: int, expected_scope: str) -> CheckResult:
    """⑥ 指定序号的事件属于哪个作用域（`main` / `isolated:<skill>` / `summary` / `notes`）。"""
    try:
        record = view.by_seq(seq)
    except LookupError as e:
        return CheckResult(False, str(e))
    actual = str(record.get("scope"))
    if actual != expected_scope:
        return CheckResult(
            False,
            f"seq={seq}（{record.get('type')}）的作用域期望 {expected_scope!r}，实际 {actual!r}",
            (seq,),
        )
    return CheckResult(True, evidence_seqs=(seq,))


def check_order(view: TraceView, seq_before: int, seq_after: int) -> CheckResult:
    """⑦ 两个事件的先后（序号严格递增即先后确定，见 P0「成功落盘才推进序号」）。"""
    if seq_before >= seq_after:
        return CheckResult(
            False,
            f"期望 seq={seq_before} 早于 seq={seq_after}，但序号并非严格递增",
            (seq_before, seq_after),
        )
    return CheckResult(True, evidence_seqs=(seq_before, seq_after))


def check_count(view: TraceView, type_: str, expected: int) -> CheckResult:
    """⑧ 某类事件的条数**精确等于**期望值。"""
    events = view.of_type(type_)
    if len(events) != expected:
        return CheckResult(
            False,
            f"{type_} 类事件期望 {expected} 条，实际 {len(events)} 条",
            tuple(e.get("seq") for e in events[:5]),
        )
    return CheckResult(True, evidence_seqs=tuple(e.get("seq") for e in events[:5]))


# ---------------------------------------------------------------------------
# 词汇 ⑨：取自会话存档
# ---------------------------------------------------------------------------
def check_history_len(
    sessions_dir: PathLike, expected: int, *, role: Optional[str] = None
) -> CheckResult:
    """
    ⑨ 主历史的消息条数。

    :param sessions_dir: `<项目根>/.rhinecode/sessions/`
    :param expected: 期望条数
    :param role: 可选，只数某个角色的消息（如 `user`）

    **为什么取会话存档而不是记录**：十五类事件中没有任何一类承载「当前主历史多少条」；
    记录里的 `api_request.messages` 同时受条数（400）与长度（4000 字）双重截断，
    数它等于数一个不完整的样本。会话存档是逐条追加写的**完整**消息流，
    正是这个问题的权威来源。

    ⚠️ **本函数是本模块里唯一允许出现 `json.loads` 的地方**——它读的是会话存档，
    不是记录文件。读记录一律走 `trace.reader`（见模块 docstring 的 F24 说明），
    `test_e2e_assertions.py` 的源码护栏会把本函数排除在外。

    坏行跳过，口径与会话存档自身的容错一致（JSON 解析失败 / 非对象 / 缺 role 均跳过）。
    """
    directory = Path(sessions_dir)
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if directory.is_dir() else []
    if not files:
        return CheckResult(False, f"{directory} 下没有任何会话存档（.jsonl）")

    latest = files[-1]
    count = 0
    for line in latest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or not obj.get("role"):
            continue
        if role is not None and obj.get("role") != role:
            continue
        count += 1

    if count != expected:
        return CheckResult(
            False,
            f"会话存档 {latest.name} 里"
            + (f"角色为 {role!r} 的" if role else "")
            + f"消息期望 {expected} 条，实际 {count} 条",
        )
    return CheckResult(True)


# ---------------------------------------------------------------------------
# 词汇 ①⑩⑪：取自假模型留存的原始参数
# ---------------------------------------------------------------------------
def check_tools_offered(provider: Any, turn: int, expected: set[str]) -> CheckResult:
    """
    ① 第 `turn` 轮（从 0 起）实际发给模型的工具名集合**精确等于**期望。

    这是「Skill 白名单确实收窄了工具集」那类断言的入口——也是 trace 立项动因之一
    「模型调用了本轮没发给它的工具」的正面判据。
    """
    calls = provider.calls
    if turn >= len(calls):
        return CheckResult(False, f"模型只被调用了 {len(calls)} 次，没有第 {turn} 轮（从 0 起）")
    actual = calls[turn].tool_names
    if actual != expected:
        return CheckResult(
            False,
            f"第 {turn} 轮发出的工具集期望 {sorted(expected)}，实际 {sorted(actual)}"
            f"（多出：{sorted(actual - expected)}；缺少：{sorted(expected - actual)}）",
        )
    return CheckResult(True)


def check_stable_prompt(provider: Any, turn: int, text: str, *, present: bool = True) -> CheckResult:
    """
    ⑩ 第 `turn` 轮的**稳定系统提示**含 / 不含某文本。

    ⚠️ 稳定段（`system` 参数）与动态段（消息列表末条 system 消息）是两条不同的通道。
    已激活 Skill 的 SOP 正文在**动态段**，用本函数去找它必然失败——那种情形请用
    `check_dynamic_reminder`。
    """
    calls = provider.calls
    if turn >= len(calls):
        return CheckResult(False, f"模型只被调用了 {len(calls)} 次，没有第 {turn} 轮（从 0 起）")
    system = calls[turn].system or ""
    hit = text in system
    if present and not hit:
        return CheckResult(
            False,
            f"第 {turn} 轮的稳定系统提示里找不到 {text!r}（全长 {len(system)} 字）",
        )
    if not present and hit:
        idx = system.find(text)
        return CheckResult(
            False,
            f"第 {turn} 轮的稳定系统提示不应含 {text!r}，"
            f"但在偏移 {idx} 处找到了：…{system[max(0, idx - 40):idx + 60]}…",
        )
    return CheckResult(True)


def check_dynamic_reminder(
    provider: Any, turn: int, text: str, *, present: bool = True
) -> CheckResult:
    """
    ⑪ 第 `turn` 轮的**动态提醒**含 / 不含某文本。

    动态提醒 = 消息列表末条 system 消息（见 `RecordedCall.dynamic_reminder`）。
    「第 N 轮激活 Skill、第 N+1 轮其 SOP 才出现在提醒里」这条判据就靠本函数的
    两次调用（present=False + present=True）钉死。
    """
    calls = provider.calls
    if turn >= len(calls):
        return CheckResult(False, f"模型只被调用了 {len(calls)} 次，没有第 {turn} 轮（从 0 起）")
    reminder = calls[turn].dynamic_reminder
    hit = text in reminder
    if present and not hit:
        return CheckResult(
            False,
            f"第 {turn} 轮的动态提醒里找不到 {text!r}（全长 {len(reminder)} 字）；"
            f"提示：SOP 正文在动态提醒里而不在稳定系统提示里，别弄反了",
        )
    if not present and hit:
        return CheckResult(
            False, f"第 {turn} 轮的动态提醒不应含 {text!r}，但它出现了"
        )
    return CheckResult(True)
