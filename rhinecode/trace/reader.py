"""
行为记录阅读器：把一份 JSONL 记录读成人能扫的时间线，或展开单条事件的完整负载。

调用方式（**刻意不注册控制台入口**，避免给用户的 PATH 里多一个命令）：

    python -m rhinecode.trace.reader <记录文件>
    python -m rhinecode.trace.reader <记录文件> --type api_request,tool_execute
    python -m rhinecode.trace.reader <记录文件> --scope main
    python -m rhinecode.trace.reader <记录文件> --seq 42

设计约束：
- **只读**。全程以读模式打开文件，绝不写回——记录是证据，读的动作不该改变它。
- **坏行跳过并计数**（与 c9 会话存档的容错口径一致）。进程崩溃时最后一行可能是
  半截 JSON，为这个直接报错退出等于让最需要看的那份记录读不出来。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from rhinecode.trace.models import TraceEventType


# ---------------------------------------------------------------------------
# 一句话摘要：type → 摘要函数
# ---------------------------------------------------------------------------
# ⚠️ **成对维护点**：新增 trace 事件类型时，除了 models.py 的枚举，还必须在这张表
# 里登记一条摘要函数。漏了不会报错——只会让新事件在时间线里显示成
# 「（未登记类型）」。之所以显式输出那句话而不是留空白，就是为了让这个遗漏
# 在第一次读记录时就暴露出来。


def _text_of(value: Any, limit: int = 90) -> str:
    """
    把一个字段值渲染成单行短文本。

    落盘时被截断的字段是 `{"text":…, "truncated": True, "original_length": N}`
    这种对象（见 `models.clip` 的约定），这里统一还原成「文本 +（原长 N 字）」。
    """
    if isinstance(value, dict) and value.get("truncated"):
        body = str(value.get("text", ""))
        original = value.get("original_length")
        suffix = f"（原长 {original} 字）"
    else:
        body = "" if value is None else str(value)
        suffix = ""
    body = body.replace("\n", "⏎ ")
    if len(body) > limit:
        body = body[:limit] + "…"
    return body + suffix


def _s_session_start(r: dict) -> str:
    cfg = r.get("config") or {}
    tools = r.get("tool_names") or []
    return f"{cfg.get('protocol')}/{cfg.get('model')} · 工具 {len(tools)} 个 · 根 {r.get('project_root')}"


def _s_session_end(r: dict) -> str:
    return f"{r.get('reason')} · 轮次合计 {r.get('turn_total')} · 用时 {r.get('elapsed_seconds')}s"


def _s_user_input(r: dict) -> str:
    return f"[{r.get('kind')}] {_text_of(r.get('text'))}"


def _s_command_dispatch(r: dict) -> str:
    if r.get("is_unknown"):
        return f"未知命令 {r.get('command_token')}"
    return f"{r.get('command')}（{r.get('command_type')}）{_text_of(r.get('arguments'), 40)}"


def _s_api_request(r: dict) -> str:
    msgs = r.get("messages")
    count = len(msgs) if isinstance(msgs, list) else msgs.get("original_length", "?") if isinstance(msgs, dict) else "?"
    return (
        f"turn {r.get('turn')} · {r.get('model')} · 消息 {count} 条 · "
        f"工具 {len(r.get('tool_names') or [])} 个 · thinking={r.get('thinking_effort')}"
    )


def _s_api_response(r: dict) -> str:
    calls = r.get("tool_calls") or []
    err = f" · 流错误 {r['stream_error']}" if r.get("stream_error") else ""
    # 首字延迟单独显示：它是流式体验最关键的指标（用户等了多久才看到第一个字），
    # 而总耗时里混着「出字很慢」与「首字就慢」两种完全不同的问题。
    first = r.get("first_chunk_ms")
    ttfb = f" · 首字 {first}ms" if first is not None else ""
    chunks = r.get("text_chunks")
    density = f" · {chunks} 块" if chunks else ""
    return (
        f"turn {r.get('turn')} · {r.get('duration_ms')}ms{ttfb}{density}"
        f" · 工具调用 {len(calls)} 个{err} · {_text_of(r.get('text'), 60)}"
    )


# 权限管线各可命中层的中文名，与「五层防御」的心智模型对齐（第⑤层是人在回路，
# 它不由 decide 返回、而是 ASK 判定后由界面承载，故不在此表）。
# 读 trace 时最常问的问题就是「这次是被哪一层拦的」，直接显示序号与名字最省事。
#
# ⚠ **本表与 `permission.models.Layer` 是成对维护点，但刻意不合一。**
# 让本模块 import Layer 会连带拉起整个 permission 包 + rhinecode.tools + yaml
# （因为 permission/__init__.py re-export 了 PermissionEngine），
# 破坏「trace 是只依赖标准库的叶子包」这条架构不变量。
# 两处一致由 tests/test_trace_reader.py 里一条遍历 Layer 的断言钉住，漏改当场红。
_LAYER_NAMES = {
    "blacklist": "①黑名单",
    "sandbox": "②沙箱",
    "network": "②′网络边界",
    "rule": "③规则",
    "mode": "④模式",
}


def _s_permission_decision(r: dict) -> str:
    layer = str(r.get("layer"))
    return (
        f"{r.get('tool')} → {r.get('decision')}（{_LAYER_NAMES.get(layer, layer)}）"
        f" · {_text_of(r.get('reason'), 50)}"
    )


def _s_interaction(r: dict) -> str:
    return f"{r.get('kind')} → {r.get('result')} · {_text_of(r.get('display'), 50)}"


def _s_tool_execute(r: dict) -> str:
    flag = "并发" if r.get("is_concurrent") else "串行"
    return (
        f"{r.get('tool')} · {r.get('outcome')} · ok={r.get('ok')} · "
        f"{r.get('duration_ms')}ms · {flag} · {_text_of(r.get('summary') or r.get('output'), 50)}"
    )


def _s_ui_message(r: dict) -> str:
    return f"[{r.get('source')}] {_text_of(r.get('text'))}"


def _s_status_bar(r: dict) -> str:
    return _text_of(r.get("text"), 110)


def _s_agent_event(r: dict) -> str:
    bits = [str(r.get("event_type"))]
    for key in ("iteration", "stop_reason", "tool_name", "result_ok", "text_length"):
        if key in r:
            bits.append(f"{key}={r[key]}")
    if r.get("message"):
        bits.append(_text_of(r.get("message"), 40))
    return " · ".join(bits)


def _s_context_compaction(r: dict) -> str:
    if r.get("layer") == "offload":
        return f"第一层存盘 {r.get('count')} 个 · {(r.get('tool_call_ids') or [])[:3]}"
    ok = "成功" if r.get("ok") else "未压缩"
    tail = f" · {_text_of(r.get('reason') or r.get('skipped'), 40)}" if not r.get("ok") else ""
    return (
        f"第二层摘要 {ok} · 边界 {r.get('retain_index')} · "
        f"{r.get('before_count')}→{r.get('after_count')} 条{tail}"
    )


def _s_skill_state(r: dict) -> str:
    action = r.get("action")
    if action == "activate":
        return f"激活 {r.get('skill')} · 当前 {r.get('active')}"
    if action == "deactivate":
        return f"卸载 {r.get('skill')}（移除 {r.get('removed_count')}）· 当前 {r.get('active')}"
    if action == "clear_active":
        return f"清空激活态（移除 {r.get('removed_count')}）"
    if action == "reload":
        return (
            f"热更新 · 新增 {r.get('added')} · 移除 {r.get('removed')} · "
            f"自动卸载 {r.get('auto_deactivated')}"
        )
    if action == "bind_tools":
        return f"白名单绑定 · Skill {len(r.get('skills') or [])} 个"
    return str(action)


def _s_history_restored(r: dict) -> str:
    return f"{r.get('origin')} · {r.get('message_count')} 条 · session {r.get('session_id')}"


SUMMARIZERS: dict[str, Callable[[dict], str]] = {
    TraceEventType.SESSION_START.value: _s_session_start,
    TraceEventType.SESSION_END.value: _s_session_end,
    TraceEventType.USER_INPUT.value: _s_user_input,
    TraceEventType.COMMAND_DISPATCH.value: _s_command_dispatch,
    TraceEventType.API_REQUEST.value: _s_api_request,
    TraceEventType.API_RESPONSE.value: _s_api_response,
    TraceEventType.PERMISSION_DECISION.value: _s_permission_decision,
    TraceEventType.INTERACTION.value: _s_interaction,
    TraceEventType.TOOL_EXECUTE.value: _s_tool_execute,
    TraceEventType.UI_MESSAGE.value: _s_ui_message,
    TraceEventType.STATUS_BAR.value: _s_status_bar,
    TraceEventType.AGENT_EVENT.value: _s_agent_event,
    TraceEventType.CONTEXT_COMPACTION.value: _s_context_compaction,
    TraceEventType.SKILL_STATE.value: _s_skill_state,
    TraceEventType.HISTORY_RESTORED.value: _s_history_restored,
}

# 未登记类型的显式标记。**不要改成空串**——它是「新增事件类型时忘了登记摘要函数」
# 的自检信号，空白会让这个遗漏永远不被发现。
UNREGISTERED = "（未登记类型）"


def summarize(record: dict) -> str:
    """把一条记录压成一句话关键信息。未登记的类型返回显式标记。"""
    fn = SUMMARIZERS.get(str(record.get("type")))
    if fn is None:
        return UNREGISTERED
    try:
        return fn(record)
    except Exception as e:  # noqa: BLE001 —— 阅读器不该因一条畸形记录整体失败
        return f"（摘要失败：{e}）"


# ---------------------------------------------------------------------------
# 读取与过滤
# ---------------------------------------------------------------------------
def load_records(path: Path) -> tuple[list[dict], int]:
    """
    逐行解析记录文件。

    :param path: 记录文件路径
    :returns: (记录列表, 被跳过的坏行数)

    副作用：只读打开文件。**绝不写回。**
    """
    records: list[dict] = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # 进程崩溃时最后一行可能是半截 JSON；跳过并计数，不整体失败
                skipped += 1
                continue
            if isinstance(obj, dict):
                records.append(obj)
            else:
                skipped += 1
    return records, skipped


def _csv(value: Optional[str]) -> Optional[set[str]]:
    """把逗号分隔的过滤参数解析成集合；None / 空串返回 None（表示不过滤）。"""
    if not value:
        return None
    return {v.strip() for v in value.split(",") if v.strip()}


def filter_records(
    records: list[dict],
    types: Optional[set[str]] = None,
    scopes: Optional[set[str]] = None,
) -> Iterator[dict]:
    """按类型与作用域过滤（两者同时给出时取**交集**）。"""
    for r in records:
        if types is not None and str(r.get("type")) not in types:
            continue
        if scopes is not None and str(r.get("scope")) not in scopes:
            continue
        yield r


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def render_timeline(records: Iterator[dict]) -> list[str]:
    """摘要模式：每事件一行 `seq / ts / scope / type / 一句话关键信息`。"""
    lines = []
    for r in records:
        ts = str(r.get("ts", ""))
        # 只留时分秒毫秒，日期在文件名里已有，占宽度不划算
        short_ts = ts.split("T")[-1] if "T" in ts else ts
        lines.append(
            f"{r.get('seq'):>5}  {short_ts:<12} {str(r.get('scope')):<18} "
            f"{str(r.get('type')):<20} {summarize(r)}"
        )
    return lines


def render_detail(record: dict) -> list[str]:
    """
    展开模式：人可读地打印单条记录的完整负载。

    被截断的字段**显式标出原长**——只看到 4000 字的正文而不知道它原本有 40 万字，
    结论会完全不同。
    """
    lines = [
        f"seq   : {record.get('seq')}",
        f"ts    : {record.get('ts')}",
        f"type  : {record.get('type')}",
        f"scope : {record.get('scope')}",
        "-" * 60,
    ]
    for key, value in record.items():
        if key in ("seq", "ts", "type", "scope"):
            continue
        if isinstance(value, dict) and value.get("truncated"):
            lines.append(f"{key}（已截断，原长 {value.get('original_length')} 字）:")
            lines.append(str(value.get("text", "")))
        elif isinstance(value, (dict, list)):
            lines.append(f"{key}:")
            lines.append(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        else:
            lines.append(f"{key}: {value}")
        lines.append("")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    """
    命令行入口。

    :param argv: 参数列表（缺省取 sys.argv[1:]，便于测试直接调用）
    :returns: 退出码（0 正常，1 文件不存在或指定序号找不到）
    """
    parser = argparse.ArgumentParser(
        prog="python -m rhinecode.trace.reader",
        description="RhineCode 行为记录阅读器（只读）",
    )
    parser.add_argument("path", help="记录文件路径（.jsonl）")
    parser.add_argument(
        "--type",
        dest="types",
        default=None,
        help="只看这些事件类型，逗号分隔（如 api_request,tool_execute）",
    )
    parser.add_argument(
        "--scope",
        dest="scopes",
        default=None,
        help="只看这些作用域，逗号分隔（如 main,summary,isolated:review）",
    )
    parser.add_argument(
        "--seq",
        type=int,
        default=None,
        help="展开指定序号那一条的完整负载",
    )
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.is_file():
        print(f"找不到记录文件：{path}", file=sys.stderr)
        return 1

    records, skipped = load_records(path)

    if args.seq is not None:
        for r in records:
            if r.get("seq") == args.seq:
                print("\n".join(render_detail(r)))
                return 0
        print(f"记录里没有 seq={args.seq} 的事件。", file=sys.stderr)
        return 1

    selected = filter_records(records, _csv(args.types), _csv(args.scopes))
    lines = render_timeline(selected)
    print("\n".join(lines))
    print(f"\n共 {len(lines)} 条事件（文件内 {len(records)} 条）", end="")
    if skipped:
        print(f"，跳过 {skipped} 个坏行", end="")
    print("。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
