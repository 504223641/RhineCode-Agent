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
  （主对话、Skill 独立模式子对话、上下文摘要、自动记忆），不区分的话时间线会串味。
"""

from __future__ import annotations

import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class TraceEventType(str, Enum):
    """
    行为记录的事件种类，共**二十七类**。

    构成（每一批都对应一个章节，新增时请一并更新这个计数与 CLAUDE.md 的能力表）：
    spec F11–F16 十五类 + c12 Hook 两类 + c13 子 Agent 两类 + c14 worktree 四类
    + c15 协作四类。

    ⚠️ 这个数字长期是错的（曾停在「十九类」，CLAUDE.md 停在「二十三类」），
    因为它是**纯注释、漏改不报错**。`tests/test_trace_models.py` 现在有一条
    用例把「枚举成员数」与这段文字里的数字钉在一起，漏改当场红。

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
    HOOK_DISPATCH = "hook_dispatch"              # c12：生命周期事件的分发（**零命中也记**）
    HOOK_EXECUTE = "hook_execute"                # c12：单条 Hook 规则的执行结果
    SUBAGENT_START = "subagent_start"            # c13：一次委派的发起（角色、任务、工具集）
    SUBAGENT_END = "subagent_end"                # c13：子 Agent 的结束（原因、轮次、用量）
    WORKTREE_CREATE = "worktree_create"          # c14：隔离工作区的创建或快速恢复
    WORKTREE_PROVISION = "worktree_provision"    # c14：环境初始化（复制/软链的结果与警告）
    WORKTREE_SETTLE = "worktree_settle"          # c14：结束时的保留/删除决定
    WORKTREE_CLEANUP = "worktree_cleanup"        # c14：启动清理的结果
    TEAM_MESSAGE = "team_message"                # c15：一条队友消息的发出与送达
    TEAM_TASK = "team_task"                      # c15：共享任务清单的一次变更
    TEAM_MEMBER = "team_member"                  # c15：队员状态流转（注册/待命/唤醒/退场）
    AUTO_WAKE = "auto_wake"                      # c15：主对话的一次自动唤起
    UI_TOOL_BATCH = "ui_tool_batch"              # tui-activity-fold：一批工具调用归并成一行
    UI_DETAIL_LEVEL = "ui_detail_level"          # tui-activity-fold：展开档位的切换


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
# - SCOPE_MEMORY：c9 的自动记忆调用。同样共用 Provider 实例，而且它跑在**独立的
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
SCOPE_MEMORY = "memory"
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


def subagent_scope(name: str) -> str:
    """
    构造 c13 子 Agent 的作用域名。

    :param name: 角色名；分支式子 Agent 传 `"branch"`
    :returns: 形如 `subagent:explorer` 的作用域字符串

    **为什么不复用 `isolated_scope`**：两者都是「一条独立的子对话」，但读 trace 的人
    需要把它们分开——Skill 子对话由一个写好的 Skill 承载、同步阻塞主对话；
    子 Agent 由模型临时委派、可能在后台跑、有独立的角色与工具集。
    排查「这轮请求是谁发的」时，混成一个前缀等于把两类问题揉在一起。

    多个子 Agent 可以**并发**，各自的作用域靠角色名区分；作用域本身存在
    `threading.local` 里，因此并发的多个后台线程天然互不干扰。
    """
    return f"subagent:{name}"


# ---------------------------------------------------------------------------
# 字段值的落盘形态与脱敏（spec F6 / F5）
# ---------------------------------------------------------------------------
# ⚠️ **本层不做任何截断。**
#
# 这里原先有两个阈值：`MAX_FIELD_CHARS = 4000`（单字段字符上限）与
# `MAX_MESSAGE_ITEMS = 400`（一次请求最多记多少条历史消息）。两者都已删除，
# 理由是它们与 trace 的立项目的直接冲突：
#
# **观测设施一旦自作主张地丢内容，它就不再是可信证据。** 截断的坏处不是「少了一段」，
# 而是「读的人无法判断自己看到的够不够」——排查一个行为问题时，你永远不知道
# 被切掉的那 4001 字里有没有答案，于是每一条从 trace 得出的结论都要打个折扣。
#
# 实测过的具体代价（这就是删掉它们的直接动因）：结构化系统提示在**最小配置**下
# （无 RHINE.md、无记忆索引、无 Skill 清单，只有三个内置角色 + 组队说明）
# 已经有 3886 字符，贴着 4000 线；而稳定通道按「越稳定越靠前」排序，
# 尾部依次是 134 组队协作 / 135 角色清单 / 140 Skill 清单。也就是说
# **任何一份真实的 RHINE.md 一进来，被切掉的正好是 C15 / C13 / C11 那三段清单**
# ——恰恰是「模型到底看没看到这个能力」这类问题唯一的证据。
#
# 代价是记录文件更大（`api_request` 每条都含完整历史，长会话下可达数十 MB）。
# 这是**刻意付的**：磁盘便宜，而一次查不出根因的排查很贵。文件体积与敏感性
# 见 CLAUDE.md「安全边界」里 trace 那条——产物勿提交、勿外传。
#
# REDACTED：脱敏占位符。固定字面量，便于阅读器与测试直接比对。
# **脱敏与截断是两回事**：前者是安全要求（不记密钥），后者是容量妥协（已删除）。
REDACTED = "***REDACTED***"


def full_text(value: Any) -> str:
    """
    把任意值转成可落盘的字段值——**完整，不截断**。

    这是全项目埋点的统一入口。它现在只剩一件事：把非字符串安全地转成字符串
    （工具参数里可能是 dict、用量里可能是 int）。

    ## 为什么保留这个函数而不是让调用点直接写 `str(x)`

    ① 它是一个**语义标记**：出现 `full_text(...)` 的地方就是「这里是一段可能很长的
    自由文本」，读代码的人一眼能看出哪些字段是重量级的；
    ② 将来若要对某类字段做统一处理（比如把二进制安全地转义），只有一处要改；
    ③ 它取代了旧的 `clip()`，**改名是刻意的**——留着旧名字做别名的话，
    漏改的调用点会静默继续按旧语义工作；改名让每一个调用点在导入时就报 `ImportError`，
    强制被访问一遍。这与本项目「路径判定不给默认值、让遗漏在开发期变成 TypeError」
    是同一套思路。

    :param value: 任意值；非字符串经 `str()` 转换
    :returns: 完整字符串，**任何长度都原样返回**

    副作用：无（纯函数）。
    """
    return value if isinstance(value, str) else str(value)


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
    # tui-display 扩展 F19：NOTICE 的展示档位。缺省的 `"notice"` 不写进负载
    # （下面的稀疏过滤会滤掉），只有被明确抬到 `"event"` 时才留下一条痕迹
    # ——记录里因此能看出「这条提示当时是按要紧的那档显示的」。
    level = getattr(event, "level", None)

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
        "level": level if level and level != "notice" else None,
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
    "SCOPE_MEMORY",
    "SCOPE_WEB_EXTRACT",
    "isolated_scope",
    "subagent_scope",
    "REDACTED",
    "full_text",
    "redact_config",
    "agent_event_payload",
    "default_trace_path",
]
