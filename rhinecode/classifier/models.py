"""
分类器审查的纯数据层（c16）：只定义枚举、dataclass 与协议，不含任何行为逻辑。

设计目的与 `permission/models.py` 同思路——让本包各模块（提示词构造、输出解析、
熔断、缓存、门面）共享同一套轻量类型，彼此通过这些结构通信，从而各自都能被
脱离网络的纯单测覆盖（spec N3）。

## 这些类型在一次判定里的流向

    Agent Loop 拿到权限结论（来自第④层）
        → 构造 ReviewAction（待判动作）
        → ReviewSession 用两段历史构造 Transcript（转录）
        → ClassifierService.review(action, transcript) → Verdict
        → Agent Loop 据 Verdict 覆写权限结论

## ⚠ 本模块只依赖标准库

这是 spec N5「叶子模块」的落点。特别地，**不要 import `rhinecode.permission`**：
它的 `__init__.py` 会连带把引擎、`rhinecode.tools.path_guard` 一起拉起来，
本包就不再是叶子了。需要判断规则宽泛与否时，`broad.py` 收的是两个字符串
而不是 `Rule` 对象，正是为此。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


# ── 四类被审查的动作 ──────────────────────────────────────────────────────
#
# 取值与 `Tool.classifier_scope` 一一对应。空串表示「该工具不进分类器」，
# 那是绝大多数工具的情况（文件读写、委派、Skill 加载、任务清单……）。
#
# ⚠ 新增取值是**成对维护点**：这里加一个之后，`agent/loop.py` 的两条判定分支、
# `classifier/prompt.py` 的待判动作段落、`classifier/render.py` 的范围名表
# 都要认得它，否则新类别会静默地「声明了但从不被审查」
# ——而配置和界面上都看不出异常。
SCOPE_COMMAND = "command"
SCOPE_URL = "url"
SCOPE_MESSAGE = "message"
# web_search 扩展 F9。待判内容是**完整查询词**，不是地址——
# 复用 SCOPE_URL 那一支会让分类器拿到空内容然后放行，而且完全无声
# （`_review_action` 对 SCOPE_URL 取的是 `args["url"]`）。
SCOPE_SEARCH = "search"


@dataclass(frozen=True)
class ReviewAction:
    """
    一次待判动作，是 `ClassifierService.review` 的输入之一。

    `frozen=True` 与 `PermissionRequest` 同理由：它是值对象，构造后只被纯函数
    读取、断言，不该被中途改写。

    :param scope: 三类之一（见上方常量）。决定走哪种缓存、渲染成什么措辞
    :param tool_name: 真实工具名（如 "run_command"），仅用于文案展示
    :param specifier: 待判的主体内容——
                      命令类是**完整命令串**、网络类是**完整地址**、
                      消息类是**完整正文**、搜索类是**完整查询词**
    :param recipient: 仅消息类填充：收件人的名字
    :param host: 仅网络类填充：已归一化的主机名（小写、去末尾点）
    :param port: 仅网络类填充：端口。与 host 一起构成缓存键（spec F18）
    :param cwd: 本次调用的工作目录。隔离子 Agent 传的是它自己的隔离工作区，
                因此分类器看到的「在哪儿干活」与权限管线判定时用的是同一个值

    ⚠ **`specifier` 是不可信输入**：它由模型生成，可能含伪装成指令的文本。
    渲染进提示词之前必须过 `prompt.neutralize`。
    """

    scope: str
    tool_name: str
    specifier: str
    recipient: str = ""
    host: str = ""
    port: int = 0
    cwd: str = ""


class VerdictKind(str, Enum):
    """
    一次判定的三种结局（spec F12）。

    继承 str 便于直接和字符串比较、打印调试（与本项目其它枚举同风格）。

    - ALLOW：分类器认为可以执行
    - BLOCK：分类器认为不该执行
    - FAILED：**没能得到结论**——调用抛异常、超时、或第二阶段输出解析不出来。
      调用方按拒绝处理（未熔断时），这是刻意的 fail-closed
    """

    ALLOW = "allow"
    BLOCK = "block"
    FAILED = "failed"


@dataclass(frozen=True)
class Verdict:
    """
    一次判定的完整结果。

    :param kind: 三选一结局
    :param reason: **给用户看的**完整理由；FAILED 时是错误描述
    :param staged: 本次是否跑到了第二阶段（第一阶段判不可疑时为 False）
    :param cached: 本次是否直接命中缓存（命中时未发生任何模型调用）
    :param elapsed_ms: 本次判定耗时（毫秒），命中缓存时为 0

    ⚠ **`reason` 永远不进模型历史。** 回灌给模型的是 `render.py` 里的固定文案
    （`DENIED_BY_CLASSIFIER` / `MESSAGE_NOT_DELIVERED`，spec F13）。

    理由：分类器写的理由对模型而言是一份**绕过指南**——「原来是因为域名不在
    白名单里」会直接导致它换个域名重试。完整理由只走两条出口：界面上的系统行
    （给人看，人才是能据此做决定的那一方）与行为记录（事后排查）。
    """

    kind: VerdictKind
    reason: str
    staged: bool = False
    cached: bool = False
    elapsed_ms: int = 0


@dataclass(frozen=True)
class ClassifierConfig:
    """
    分类器的三个配置项（spec F25），整段在 `config.yaml` 里可缺省。

    :param enabled: 总开关，**缺省开**。关掉之后三类动作回到本章之前的行为
                    （命令一律放行、网络仍然每次弹面板、队友消息一律投递），
                    且宽泛放行规则不再被丢弃
    :param model: 分类器用哪个模型。**空串 = 跟主对话同一个模型**。
                  留这个口子是因为跑命令是高频操作，每次多一次往返有感，
                  将来可以换更便宜的模型
    :param timeout: 单次调用超时（秒）。超时按调用失败处理（spec F12），
                    即未熔断时拒绝、并计入失败熔断的计数
    """

    enabled: bool = True
    model: str = ""
    timeout: float = 10.0


class BreakerReason(str, Enum):
    """
    熔断的两种成因（spec F15/F16）。**必须分开**，因为它们要给用户的说法不同。

    - NONE：未熔断
    - BLOCKS：拦得太多（连续 3 次或累计 20 次）。通常意味着分类器不了解你的
      环境、在反复误伤，此时该把决定权交回人
    - FAILURES：连续调用失败 3 次。通常意味着接口地址不通或凭据有问题，
      此时用户需要知道**根因是分类器连不上**，而不是「我的命令怎么都被拒了」
    """

    NONE = "none"
    BLOCKS = "blocks"
    FAILURES = "failures"


@dataclass(frozen=True)
class BreakerState:
    """
    熔断状态的对外快照。

    :param tripped: 当前是否处于熔断
    :param reason: 成因
    :param detail: 给用户看的补充——BLOCKS 时是触发时的计数说明，
                   FAILURES 时是**最后一次失败的错误原文**

    ⚠ `detail` 在 FAILURES 时不可省：spec F16 的全部价值就在于让用户看见
    「根因是分类器连不上」。只说「已熔断」而不说为什么，用户仍然查不出来。

    做成不可变快照而不是直接暴露熔断器内部字段，是为了让
    「临界区只做纯内存读写」这条不变量成立——调用方拿到的是一份拷贝，
    可以在锁外随便渲染、埋点。
    """

    tripped: bool
    reason: BreakerReason
    detail: str


@dataclass(frozen=True)
class RecordedCall:
    """
    转录里的一条「模型发起过的工具调用」。

    :param name: 工具名
    :param arguments: 参数的 JSON 序列化原文。**不截断**（spec F6）

    为什么参数必须带上：它是「模型三轮前把一段密钥写进了某个文件」这件事的
    唯一物证。分类器看不到工具的**输出**，但看得到工具的**输入**——
    于是后续那条 `git push` 上它才有判断依据。
    """

    name: str
    arguments: str


@dataclass(frozen=True)
class Transcript:
    """
    喂给分类器的转录（spec F4/F5）。

    :param user_messages: 用户说过的全部话，按时间顺序
    :param tool_calls: 模型发起过的全部工具调用，按时间顺序

    **这两样就是全部。** 刻意不含的两样各有理由，见 `prompt.build_transcript`：
    模型自己写的正文（防模型说服分类器）、任何工具的输出（主要的注入防御）。

    ⚠ **不做任何截断**（spec F6）。截断会静默削弱防御且不可见——被截掉的地方
    连痕迹都没有，记录上只会显示「判定通过」。体积问题用两阶段过滤、
    前缀缓存友好的排布、以及可单独配一个便宜模型来摊，不用丢内容解决。
    """

    user_messages: tuple[str, ...]
    tool_calls: tuple[RecordedCall, ...]


@runtime_checkable
class ClassifierProtocol(Protocol):
    """
    Agent Loop 对分类器的全部要求，只有四个方法。

    ## ⚠ 为什么协议定义在**本包**，而不是像 `agent/gate.py` 那样放在消费方

    `gate.py` 把协议放在 `agent/` 是因为 `subagents` 反过来 import `agent.loop`
    （运行器要跑一个 Agent），协议若放在 `subagents/` 会直接撞循环导入——
    实测报错是 `cannot import name 'Agent' from partially initialized module`。

    **本包没有那个约束**：分类器只调 provider，一行 `agent` 的代码都不碰。
    照抄 `gate.py` 的形状会让下一个人以为这里也有循环导入风险，而那是假的——
    他会据此推理出一堆并不存在的限制。

    ## 为什么仍然要有协议

    两条：① `RunOptions.classifier` 缺省 None，**不传等于零回归**；
    ② 测试要断言「分类器被调用了几次」（spec AC3/AC12 那几条反证的全部依据是
    调用次数而非结果），有协议就能塞一个纯计数的假实现进去。
    """

    def review(self, action: ReviewAction, transcript: Transcript) -> Verdict:
        """对一次待判动作给出结论。**绝不抛异常**——一切失败收敛成 FAILED。"""
        ...

    def is_tripped(self) -> bool:
        """当前是否处于熔断。调用方据此决定 FAILED 走哪条退路（spec F16a）。"""
        ...

    def note_manual_approval(self) -> None:
        """用户在确认面板上批准了一次 → 解除熔断（spec F15 的恢复条件）。"""
        ...

    def breaker_state(self) -> BreakerState:
        """取熔断状态快照，供界面渲染与行为记录使用。"""
        ...
