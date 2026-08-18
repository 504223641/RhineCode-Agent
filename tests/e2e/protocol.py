"""
控制通道的**线上格式**：编解码、错误码、面板决策取值表、`via` 取值。

本模块是**纯数据 + 纯函数，零 IO、零产品依赖**——它同时被宿主（`host.py`）与瘦客户端
（`client.py`）导入，而客户端刻意不 import 任何 `rhinecode` 模块，所以这里也一个都不能有。

## 线上格式

一条指令一次往返，**行分隔的 JSON**：每条消息是一行 JSON、以 `\\n` 结尾。
选这个格式是因为它天然自带消息边界（读到换行就是一条完整消息），
不必再设计长度前缀之类的分帧协议。

    # 请求
    {"cmd": "send",    "text": "/skills"}
    {"cmd": "status"}
    {"cmd": "wait",    "timeout": 180.0}
    {"cmd": "answer",  "choice": "once", "via": "channel"}
    {"cmd": "cancel"}
    {"cmd": "observe", "since": 42, "types": ["tool_execute"]}
    {"cmd": "keys",    "sequence": ["ctrl+q"]}
    {"cmd": "screen",  "selector": "#history-messages"}
    {"cmd": "quit"}

    # 成功响应
    {"ok": true,  "data": {...}}
    # 失败响应
    {"ok": false, "error": {"code": "not_pending", "message": "...", "data": {...}}}

## 为什么编解码要显式写 UTF-8（spec N9）

`json.dumps` 产出的是 `str`，写进 socket 前必须编码成 `bytes`。如果依赖
`str.encode()` 的默认值或平台默认编码，Windows 上就可能按 GBK 之类的代码页处理，
中文面板文本一往返就烂掉。这里**每一处编解码都显式写 `"utf-8"`**，与系统默认编码无关。

实测补充：中文经通道往返是正确的，只有终端打印时会因控制台代码页显示为乱码——
那是显示层问题，不是通道问题，不要为它去改协议。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------
# 客户端会按 code 分支处理（比如 `starting` 要提示「宿主还在装配，稍后重试」，
# 而 `fatal` 要把 message 原样打出来）。**码字拼错是静默失效**——客户端的分支
# 永远命不中，只会走到「未知错误」的兜底。所以 `err()` 会对不在本集合里的 code
# 直接抛 ValueError，把拼写错误在构造响应的那一刻就打出来。
ERROR_CODES = frozenset(
    {
        "bad_request",  # 指令名或参数不合法
        "starting",  # 装配尚未完成（socket 先于装配就绪，见 plan §3.8）
        "not_pending",  # answer 时没有待决面板
        "busy",  # send 时会话不处于空闲态
        "timeout",  # wait 超时（error.data 里带诊断）
        "turn_budget",  # 触及会话轮次预算（spec N5）
        "fatal",  # 宿主已进入致命错误态（error.message 为成文文案）
        "shutting_down",  # 宿主正在退出
        "internal",  # 宿主内部异常（handler 兜底，不该出现但不能让连接静默断掉）
    }
)


# ---------------------------------------------------------------------------
# 面板决策取值表
# ---------------------------------------------------------------------------
# ⚠️ **这里的 `choice` 是「线上取值」，不是产品侧的 `option.id`，也不是结算值。**
# 三套标识必须分清，映射关系写在 `control.settlement_for` 的表里。举例：
# 线上 choice `session` → 产品 option.id `yes_session` → 结算值 ConfirmDecision.ALLOW_SESSION。
CHOICE_TABLE: dict[str, frozenset[str]] = {
    "confirm": frozenset({"once", "session", "permanent", "deny"}),
    "approve": frozenset({"yes", "no"}),
    # clarify 的取值是「选项序号」，是开放取值，用正则判定而不是枚举
    "clarify": frozenset(),
    # session 的取值是会话标识（编号或 ID）或字面 cancel，同样是开放取值
    "session": frozenset(),
}

# 取值为开放集合、需要另行判定的面板类型
_OPEN_KINDS = frozenset({"clarify", "session"})

PANEL_KINDS = frozenset(CHOICE_TABLE.keys())

# clarify 的四种应答形态（ask-user 扩展 F21）：
#
#   2                    单选，选第 2 项（下标从 0 起）
#   0,2                  多选，勾第 0 与第 2 项
#   other:放到 docs 下面   自由输入，冒号之后整段原文即答案
#   skip                 等价于在面板上按 Esc（我不选，你自己定）
#
# ⚠ 改造前只认「纯数字」。多选与自由输入之后那条式子不成立了——
# 不改这里的结果是**新面板没法无头验收**，而本项目所有 TUI 行为的判据
# 都建立在驱动设施上。
_CLARIFY_INDEX = re.compile(r"^\d+(,\d+)*$")
_CLARIFY_FREE_TEXT_PREFIX = "other:"
_CLARIFY_SKIP = "skip"


# `wait` 的 `until`：等到什么算数。
#
# - terminal ：idle 或 pending。**缺省，与 C13 之前逐字同义**。
# - quiescent：pending，或「界面空闲**且**后台也没活了」。
#
# 加这个取值的原因是「idle 不等于系统静止」——后台委派在跑、或队友消息
# 躺在信箱里等主对话自动唤起时，界面照样是 idle，一秒后却又忙起来。
# 判据细节见 `control._is_quiescent`（尤其「待命队员为什么不算」那段）。
UNTIL_VALUES = frozenset({"terminal", "quiescent"})

# `via` 决定应答走哪条路径：
# - channel：驱动器直接调产品的结算方法，来源记为应答者的 source（本轮是 `driver`）
# - keys   ：模拟按键走面板自身的按键路径，来源保持产品默认的 `human`
# AC15 要求同一次运行内出现两种不同来源，`via` 就是它的驱动手段。
VIA_VALUES = frozenset({"channel", "keys"})

# `keys` 支持哪几类面板。
#
# ⚠ **clarify 曾被排除，ask-user 扩展重新量过之后放开了。**
# 原注释写的理由是「候选项之间夹着 disabled 详情行，按键次数推不出来」——
# 而 `_answer_by_keys` 的实现**从来就不是按行数算的**：它在
# `extract_panel` **已经过滤掉 disabled 之后**的可选项序列里算下标，
# 详情行本来就不参与计数。`ClarifyPanel` 的初始高亮也正是第一个可选项，
# 与 `ConfirmPanel` 完全同构。
#
# 放开它是必须的：多选的核心交互就是**按空格勾选**，不走按键路径根本验不到。
KEYS_SUPPORTED_KINDS = frozenset({"confirm", "approve", "session", "clarify"})


# ---------------------------------------------------------------------------
# 编解码
# ---------------------------------------------------------------------------
def encode(obj: Any) -> bytes:
    """
    把一个可 JSON 序列化的对象编成一行待发送的字节。

    :param obj: 请求或响应字典
    :returns: 以 `\\n` 结尾的 UTF-8 字节串（可直接 `sock.sendall`）

    `ensure_ascii=False` 是刻意的：让中文在线上就是中文，抓包与日志都能直接读；
    随后**显式** `.encode("utf-8")`，不依赖任何平台默认编码（spec N9）。
    """
    return (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def decode(line: bytes) -> dict:
    """
    把收到的一行字节解成字典。

    :param line: 一行原始字节（结尾的 `\\n` 可有可无）
    :returns: 解析出的字典
    :raises json.JSONDecodeError: 内容不是合法 JSON——**刻意不吞**。
        通道两端都是本设施自己写的，出现非法 JSON 说明分帧或编码出了错，
        吞掉只会让问题以「指令莫名其妙没生效」的形式出现在更远的地方（spec N7）。
    :raises UnicodeDecodeError: 字节不是合法 UTF-8，同样不吞。
    """
    return json.loads(line.decode("utf-8"))


def ok(data: Any = None) -> dict:
    """构造成功响应。`data` 允许为 None（如 `cancel` 这类没有返回值的指令）。"""
    return {"ok": True, "data": data}


def err(code: str, message: str, data: Any = None) -> dict:
    """
    构造失败响应。

    :param code: 必须是 `ERROR_CODES` 里的值
    :param message: 给人看的说明（客户端会原样打印）
    :param data: 结构化诊断（如 `wait` 超时时的三个判据分量）
    :raises ValueError: `code` 不在 `ERROR_CODES` 内。见该常量处的注释——
        拼错码字会让客户端的分支静默失效，必须在构造的那一刻就炸出来。
    """
    if code not in ERROR_CODES:
        raise ValueError(
            f"未知错误码 {code!r}；合法取值：{sorted(ERROR_CODES)}"
        )
    return {"ok": False, "error": {"code": code, "message": message, "data": data}}


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------
def validate_choice(kind: str, choice: Any) -> Optional[str]:
    """
    校验某类面板收到的 `choice` 是否合法。

    :param kind: 面板类型（confirm / clarify / approve / session）
    :param choice: 线上送来的决策取值
    :returns: 不合法时返回**给人看的错误消息**；合法返回 None

    设计成「返回消息而不是抛异常」，是因为调用方（`DriverCore.answer`）拿到消息后
    要包成 `bad_request` 响应发回客户端，抛异常反而要再 try 一层。
    """
    if kind not in PANEL_KINDS:
        return f"未知面板类型 {kind!r}；合法取值：{sorted(PANEL_KINDS)}"
    if not isinstance(choice, str) or not choice:
        return f"choice 必须是非空字符串，收到 {choice!r}"

    if kind == "clarify":
        # 四种形态见 `_CLARIFY_INDEX` 上方那段说明。越界与否要等拿到实际选项
        # 列表才知道，这里只管格式。
        if choice == _CLARIFY_SKIP:
            return None
        if choice.startswith(_CLARIFY_FREE_TEXT_PREFIX):
            if not choice[len(_CLARIFY_FREE_TEXT_PREFIX):].strip():
                return "clarify 的 other: 后面要跟非空文本（那段原文就是答案）"
            return None
        if not _CLARIFY_INDEX.match(choice):
            return (
                "clarify 的 choice 必须是选项序号（如 2）、逗号分隔的多个序号"
                f"（如 0,2）、other:<文本> 或 skip，收到 {choice!r}"
            )
        return None

    if kind == "session":
        # 会话面板的取值是会话编号或 ID，任意非空串都可能是合法的；
        # 唯一的特殊值是字面 `cancel`（关闭面板不载入）。真伪由产品侧判定。
        return None

    allowed = CHOICE_TABLE[kind]
    if choice not in allowed:
        return f"{kind} 的 choice 必须是 {sorted(allowed)} 之一，收到 {choice!r}"
    return None


def validate_until(until: Any) -> Optional[str]:
    """
    校验 `wait` 的 `until` 取值。

    :returns: 不合法时返回给人看的错误消息；合法返回 None

    与 `validate_choice` 同样「返回消息而不是抛异常」——调用方要把它包成
    `bad_request` 响应发回客户端。

    ⚠ 拼错必须报错，不能默默按缺省处理：一个写了 `--until quiescnet` 的场景
    会静默退化成旧行为，于是它想验的那个竞态问题**照样存在，而判据看起来是绿的**。
    """
    if until not in UNTIL_VALUES:
        return f"until 必须是 {sorted(UNTIL_VALUES)} 之一，收到 {until!r}"
    return None


def validate_via(kind: str, via: Any, choice: str = "") -> Optional[str]:
    """
    校验 `via` 取值，以及它与面板类型（和取值）的组合是否被支持。

    :param choice: 本次的应答取值。**只有 clarify 的自由输入用得上**
        （ask-user 扩展 F21），缺省空串即「不看取值」，既有调用点零改动
    :returns: 不合法时返回给人看的错误消息；合法返回 None

    唯一被拒绝的组合是 `clarify` + `other:`（自由输入）走 `keys`：
    逐字模拟打字反而绕开了要验的东西，见下面那条错误消息给出的替代办法。
    """
    if via not in VIA_VALUES:
        return f"via 必须是 {sorted(VIA_VALUES)} 之一，收到 {via!r}"
    if via == "keys" and kind not in KEYS_SUPPORTED_KINDS:
        return f"via=keys 不支持 {kind} 面板，请改用 via=channel。"
    if (
        via == "keys"
        and kind == "clarify"
        and isinstance(choice, str)
        and choice.startswith(_CLARIFY_FREE_TEXT_PREFIX)
    ):
        # 自由输入的键盘全链路**有更贴近真实的验法**：用 `keys` 把光标移到
        # 「其它…」按回车，再用 `send` 打字——后者走的是真人提交入口，
        # 正好压在产品那条新岔路上。让驱动器逐字模拟按键反而绕开了它。
        return (
            "via=keys 不支持 clarify 的 other:<文本>。"
            "改用 via=channel，或者走「keys 选中「其它…」+ send 打字」——"
            "后者更接近真人路径。"
        )
    return None
