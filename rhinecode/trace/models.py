"""
Trace 层最底层：事件类型枚举、作用域常量、截断与脱敏纯函数。

本模块**只依赖标准库**，不感知任何上层（不 import provider / agent / tools / tui）。
这样做的目的是让 trace 成为一个「叶子包」——任何模块都可以安全地依赖它埋点，
而不会引入循环导入。唯一的分层例外是同包的 `tracing_provider.py`
（它必须继承 `provider.base.BaseProvider`），见该模块的 docstring。

术语说明（第一次接触本模块时先看这里）：
- **Trace（行为记录）**：把程序运行过程中「发生了什么」按时间顺序写成一条条结构化记录，
  类似飞机的黑匣子。它与 c9 的「会话存档」不同——存档记的是「给模型看的对话历史」，
  trace 记的是「程序内部实际做了什么」（发了什么请求、权限怎么判的、工具跑了多久）。
- **作用域（scope）**：一条事件属于哪一条对话。同一个进程里同时存在多条对话
  （主对话、Skill 独立模式子对话、上下文摘要、自动笔记），不区分的话时间线会串味。
"""

from __future__ import annotations

import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Union


class TraceEventType(str, Enum):
    """
    行为记录的事件种类，共十五类（spec F11–F16）。

    继承 `str` 是为了让枚举成员可以直接当字符串用（`json.dumps` 能原样序列化、
    与阅读器的 `--type` 过滤参数可直接比较），与项目里 `AgentEventType`、
    `SkillMode` 等既有枚举同一套做法。

    成员取值一律是成员名的小写形式，落盘后即 JSON 的 `type` 字段值。
    """

    SESSION_START = "session_start"              # F11：一次进程运行的起点快照
    SESSION_END = "session_end"                  # F11：一次进程运行的终点与总计
    USER_INPUT = "user_input"                    # F12：用户提交的原始输入
    COMMAND_DISPATCH = "command_dispatch"        # F12：斜杠命令的分发结果
    API_REQUEST = "api_request"                  # F13：发给模型的完整请求
    API_RESPONSE = "api_response"                # F13：模型返回的完整响应
    PERMISSION_DECISION = "permission_decision"  # F14：五层权限管线的判定结果
    INTERACTION = "interaction"                  # F14：人在回路面板的交互与结果
    TOOL_EXECUTE = "tool_execute"                # F14：工具执行的参数、结果与耗时
    UI_MESSAGE = "ui_message"                    # F15：界面上出现的消息文本
    STATUS_BAR = "status_bar"                    # F15：状态栏文本快照
    AGENT_EVENT = "agent_event"                  # F15：Agent Loop 事件流（轻量字段）
    CONTEXT_COMPACTION = "context_compaction"    # F16：两层上下文压缩动作
    SKILL_STATE = "skill_state"                  # F16：Skill 激活态变化
    HISTORY_RESTORED = "history_restored"        # F16：会话历史被恢复


# ---------------------------------------------------------------------------
# 作用域常量（spec F2）
# ---------------------------------------------------------------------------
# 为什么需要区分作用域：一个进程里可能同时跑四种对话，它们共用 Provider 实例。
#
# - SCOPE_MAIN：用户能看到的那条主对话。不隶属任何对话的事件（会话启停、命令分发、
#   状态栏刷新）也归它——「主对话」是缺省归属，而不是「必须由用户消息触发」。
# - SCOPE_SUMMARY：c8 第二层的 LLM 摘要调用。它与主对话**共用同一个 Provider 实例**，
#   若不区分，摘要请求会被算进主对话的轮次计数，读 trace 时会看到「用户只说了一句话，
#   却发了两轮请求」的假象。
# - SCOPE_NOTES：c9 的自动笔记调用。同样共用 Provider 实例，而且它跑在**独立的
#   daemon 线程**上，可能与用户的下一条消息并发——不区分就会两条对话的事件交错。
# - SCOPE_WEB_EXTRACT：web_fetch 扩展的抽取调用（把抓回的正文按提问压成答案）。
#   同样共用 Provider 实例。不区分的话，一次抓取会在时间线上显示成「模型自己多发了
#   一轮请求」，而读 trace 的人无从判断那一轮是谁发的。
#
# **刻意不为网络访问新增事件类型**：拒绝走既有的 permission_decision
# （layer 字段自然带出 network），抽取走既有的 api_request + 本作用域。
# 新增事件类型要同步改 reader.py 的 SUMMARIZERS，属于「漏改不报错」的成对维护点，
# 能不加就不加。
SCOPE_MAIN = "main"
SCOPE_SUMMARY = "summary"
SCOPE_NOTES = "notes"
SCOPE_WEB_EXTRACT = "web_extract"


def isolated_scope(name: str) -> str:
    """
    构造 Skill 独立模式子对话的作用域名。

    :param name: Skill 名（frontmatter 里的 `name` 字段）
    :returns: 形如 `isolated:review` 的作用域字符串

    取「前缀 + 名字」而不是单独一个 `isolated` 常量，是因为一次运行里可能先后
    跑多个不同的独立模式 Skill，各自的轮次计数需要分开。
    """
    return f"isolated:{name}"


# ---------------------------------------------------------------------------
# 截断与脱敏（spec F6 / F5）
# ---------------------------------------------------------------------------
# MAX_FIELD_CHARS：单个字段的字符上限。超过就截断并记原长——「全量记录 + 字段阀值截断」
# 策略的落点：既不丢事件，也不让一次 10MB 的文件读取把记录文件撑爆。
MAX_FIELD_CHARS = 4000
# MAX_MESSAGE_ITEMS：一次请求里最多记多少条历史消息。长会话的历史可能有上千条，
# 每条都记会让单行 JSON 极其庞大；超限只留头部并记原条数。
MAX_MESSAGE_ITEMS = 400
# REDACTED：脱敏占位符。固定字面量，便于阅读器与测试直接比对。
REDACTED = "***REDACTED***"


def clip(value: Any, limit: int = MAX_FIELD_CHARS) -> Union[str, dict]:
    """
    把任意值转成「可安全落盘的字段值」，超长则截断并保留原长信息。

    这是**全项目唯一的截断入口**。所有埋点里可能很长的字段（消息正文、工具输出、
    系统提示、状态栏文本）都必须经过它，好处是阀值只有一处、行为只有一种，
    阅读器也只需要认识一种截断表示。

    返回值的约定（重要，阅读器与测试都依赖它）：
    - **未截断 → 返回裸字符串**。绝大多数字段都不超限，保持裸串让落盘的 JSON 可读，
      不会到处是 `{"text": ..., "truncated": false}` 这种噪音。
    - **截断 → 返回一个三字段对象**：`text`（前 limit 个字符）、`truncated`（恒为 True）、
      `original_length`（截断前的字符数）。

    为什么必须记 `original_length`：spec F6 要求「能看出这里原本有多长」。只留前 4000 字
    而不记原长的话，读 trace 的人无法判断「模型看到的到底是 4KB 还是 4MB」，
    而这恰好是排查上下文相关问题时最关键的信息。

    :param value: 任意值；非字符串先经 `str()` 转换（工具参数里可能有 dict / int）
    :param limit: 字符上限，缺省 MAX_FIELD_CHARS
    :returns: 未截断时是原字符串；截断时是含三个键的字典

    副作用：无（纯函数）。

    注意截断按**字符**而不是字节切分，因此中文不会被切成半个字导致乱码
    （落盘时统一 UTF-8 编码，字符边界天然安全）。
    """
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return {
        "text": text[:limit],
        "truncated": True,
        "original_length": len(text),
    }


def redact_config(cfg: Any) -> dict:
    """
    把配置对象转成可落盘的字典，其中 API Key 换成固定掩码。

    为什么用 `getattr(cfg, name, None)` 逐字段取而不是 `dataclasses.asdict`：
    后者会把**所有**字段无差别倒出来，将来配置类新增一个含密字段（比如某个
    第三方服务的 token）时会静默泄漏进记录文件。逐字段白名单式取值，
    则新增字段默认不记录——「默认安全」。

    :param cfg: 配置对象（`rhinecode.config` 的配置数据类）；容忍字段增减
    :returns: 字段名 → 值的字典，`api_key` 恒为 REDACTED

    副作用：无（纯函数）。

    注意本函数只保证**配置快照**不含明文密钥。若模型在对话中用 `read_file`
    读过配置文件，密钥仍会出现在工具结果里——那不在本函数的职责范围内，
    由 `.gitignore` 兜底（spec N6）。
    """
    fields = (
        "protocol",
        "model",
        "base_url",
        "api_key",
        "debug_log",
        "context_window",
    )
    snapshot: dict = {}
    for name in fields:
        value = getattr(cfg, name, None)
        if name == "api_key":
            # 只要该字段存在（哪怕是空串）就记掩码，避免「没有这个键」被误读成「没配 key」
            snapshot[name] = REDACTED if value is not None else None
        else:
            snapshot[name] = value
    return snapshot


def agent_event_payload(event: Any) -> dict:
    """
    把一个 Agent Loop 事件压成**轻量字段白名单**，供 `agent_event` 类型落盘。

    为什么需要白名单（spec F17 / AC20）：Agent Loop 的事件流里，TOOL_RESULT 事件
    携带完整的工具输出、TOOL_START 携带完整的调用参数、TEXT 携带正文增量。
    这些内容**已经**由专属事件承载了——工具的参数与输出在 `tool_execute` 里，
    模型正文在 `api_response` 里。若 `agent_event` 再带一份，同一份数据会在
    记录文件里出现两次，而 TEXT 事件是逐块产出的，正文会被切成几百条重复记录。

    因此本函数**刻意排除**三样东西：
    - `event.tool_call.arguments` —— 由 `tool_execute` 承载
    - `event.tool_result.output` —— 由 `tool_execute` 承载
    - `event.text` 正文本身 —— 由 `api_response` 承载，这里只记长度

    :param event: Agent 事件对象（`agent.events.AgentEvent`）。**本函数不 import
                  agent 包**，全部字段用 `getattr` 取值，以保持 trace 的叶子包性质
    :returns: 只含白名单键的字典；取值为 None 的键**不写入**（让落盘的 JSON 保持稀疏）

    副作用：无（纯函数）。
    """
    raw_type = getattr(event, "type", None)
    # AgentEventType 继承 str，取 .value 得到干净的字符串；假事件可能直接给字符串
    event_type = getattr(raw_type, "value", raw_type)

    tool_call = getattr(event, "tool_call", None)
    tool_result = getattr(event, "tool_result", None)
    stop_reason = getattr(event, "stop_reason", None)
    text = getattr(event, "text", None)
    iteration = getattr(event, "iteration", None)
    message = getattr(event, "message", None)

    payload = {
        "event_type": event_type,
        # iteration 只在 PROGRESS 事件有意义，缺省 0 视同「无」，避免每条事件都带个 0
        "iteration": iteration or None,
        "stop_reason": getattr(stop_reason, "value", stop_reason),
        "message": message or None,
        "tool_call_id": getattr(tool_call, "id", None),
        "tool_name": getattr(tool_call, "name", None),
        "result_ok": getattr(tool_result, "ok", None),
        # 只记长度，不记正文——正文由 api_response 承载
        "text_length": len(text) if text else None,
    }
    return {k: v for k, v in payload.items() if v is not None}


def default_trace_path(project_root: Path) -> Path:
    """
    计算 `--trace` 不带参数时的缺省记录文件路径。

    :param project_root: 项目根目录（启动时的当前工作目录）
    :returns: `<项目根>/.rhinecode/traces/<时间戳>.jsonl`

    副作用：**不创建任何目录**。建目录是记录器构造时的事（它才知道自己要不要真的写），
    这里保持纯计算，便于测试直接断言路径形态而不留下垃圾目录。

    时间戳**必须含毫秒**，理由是 spec AC25 要求「连续两次运行产出两个独立文件」：
    秒级时间戳在同一秒内启动两次会算出同一个路径，而记录器以追加模式打开文件，
    结果是一个文件里塞了两段记录、时间线互相穿插，读的人根本分不开。
    """
    now = datetime.datetime.now()
    stamp = f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}"
    return project_root / ".rhinecode" / "traces" / f"{stamp}.jsonl"


__all__ = [
    "TraceEventType",
    "SCOPE_MAIN",
    "SCOPE_SUMMARY",
    "SCOPE_NOTES",
    "SCOPE_WEB_EXTRACT",
    "isolated_scope",
    "MAX_FIELD_CHARS",
    "MAX_MESSAGE_ITEMS",
    "REDACTED",
    "clip",
    "redact_config",
    "agent_event_payload",
    "default_trace_path",
]
