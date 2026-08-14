"""
脚本化假模型（spec F22）：把「模型这一轮会说什么」变成写死的剧本。

## 为什么需要它

端到端驱动的确定性形态（`--mode scripted`）不能连真实模型：真实模型每次说的话都不同，
断言就无从写起；而且它要花钱、要网络、还慢。`ScriptedProvider` 顶替真实 Provider，
按「第几次被调用」返回预先写好的数据块序列。

除了顶替，它还承担一个**记录**职责：把每次调用收到的完整参数（消息列表、稳定系统
提示、本轮实际发出的工具 schema、思考强度）原样留存下来。断言词汇里的三项
（①某轮发出的工具名集合、⑩稳定系统提示、⑪动态提醒）就取自这里——
**不取行为记录**，因为记录里的这些字段受长度与条数双重截断。

## 脚本耗尽的兜底（F22）

脚本写了两轮，模型却被调了第三次，怎么办？——返回 `fallback`，默认是一条含
`[e2e-fallback]` 的文本。**不抛错、不挂起**：上下文摘要与自动记忆都会额外调模型，
为它们抛错等于把一次正常的系统行为变成测试失败。取这个字面量是为了**可识别**：
读记录时一眼能认出「这条不是脚本里写的」。

## 线程安全

`calls` 会被 Agent 的 Worker 线程追加、被断言层在测试线程读取。用一把**只保护追加**
的独立锁，临界区内只做 `append`、不做任何调度（spec N6，沿用 C11 SkillManager 的
加锁教训：临界区里一旦出现跨线程调用就有死锁风险）。
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from rhinecode.classifier.prompt import CLASSIFIER_MARKER
from rhinecode.provider.base import BaseProvider, Message, StreamChunk, ToolCall


# 脚本耗尽时兜底文本里的标记。读记录时用它一眼分辨「这条响应不是剧本写的」。
FALLBACK_MARKER = "[e2e-fallback]"

# tool() 不显式给 call_id 时的自增计数器（跨脚本全局唯一即可，不必连续）
_call_id_counter = itertools.count(1)


# ---------------------------------------------------------------------------
# 数据块构造器：让剧本读起来像剧本
# ---------------------------------------------------------------------------
def text(content: str) -> StreamChunk:
    """一块 AI 正文。"""
    return StreamChunk(type="text", content=content)


def thinking(content: str) -> StreamChunk:
    """一块思考内容（界面以灰色斜体渲染）。"""
    return StreamChunk(type="thinking", content=content)


def tool(name: str, args: Optional[dict] = None, call_id: Optional[str] = None) -> StreamChunk:
    """
    一次工具调用。

    :param name: 工具名，必须是注册中心里真实存在的名字（否则循环会走「未知工具」路径）
    :param args: 参数字典
    :param call_id: 调用标识；缺省自增生成。**同一轮里发多个工具调用时它必须各不相同**
        ——结果回灌靠它配对，重复会让配对错乱。
    """
    cid = call_id if call_id is not None else f"e2e_call_{next(_call_id_counter)}"
    return StreamChunk(type="tool_call", tool_call=ToolCall(id=cid, name=name, arguments=args or {}))


def tool_pending(name: str, call_id: str) -> StreamChunk:
    """
    「模型开始吐一个工具调用」的播报块（参数还没生成完）。

    真实 Provider 在拿到「id + 工具名」的第一时间产出它，界面据此立刻挂一行
    「参数生成中… Ns」。剧本里要手工写出来——脚本化 Provider 只是原样吐出给定的块，
    不会自己模拟碎片拼接。

    ⚠️ **`call_id` 必须与随后那条 `tool()` 的 `call_id` 一致**，否则界面认不出这是
    同一次调用，会先留下一行永远转不完的「参数生成中」，再另起一行执行——正是这个
    机制要避免的形态。因此本参数**不给缺省值**（自增计数器会算出两个不同的 id）。

    :param name: 工具名
    :param call_id: 调用标识，与配对的 `tool()` 逐字相同
    """
    return StreamChunk(
        type="tool_pending", tool_call=ToolCall(id=call_id, name=name, arguments=None)
    )


def stream_error(message: str) -> StreamChunk:
    """一块流错误（循环据此以「流错误」停止）。"""
    return StreamChunk(type="error", content=message)


def usage(prompt: int, completion: int) -> StreamChunk:
    """
    一块 token 用量。

    C8 的上下文估算把它当作**锚点**（精确值），后续增量才靠字符数估。
    要验「压缩在什么时候触发」的场景必须给它，否则估算永远从零开始。
    """
    return StreamChunk(
        type="usage",
        usage={
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    )


def done() -> StreamChunk:
    """流正常结束。**每一轮剧本的最后一块都应该是它**（或 `stream_error`）。"""
    return StreamChunk(type="done")


# ---------------------------------------------------------------------------
# 调用留存
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecordedCall:
    """
    一次 `stream_chat` 调用收到的**原始**参数。

    :param index: 第几次调用（从 0 起）
    :param messages: 完整消息列表，**原样引用不截断**（与记录里的截断版本区别正在于此）
    :param system: 稳定系统提示全文
    :param tools: 本轮实际发出的工具 schema 列表（None 表示本轮禁用了工具，
                  如上下文摘要与自动记忆的调用）
    :param thinking_effort: 思考强度
    :param scope: 发出这次调用的**对话**（trace 作用域：`main` / `subagent:worker-a` /
                  `summary` …）。`ScriptedProvider` 不填（恒 `main`），
                  `ScopedScriptedProvider` 按线程本地作用域填真值。
                  多 Agent 场景里「这一轮是谁跑的」全靠它区分
    """

    index: int
    messages: list[Message]
    system: Optional[str]
    tools: Optional[list[dict]]
    thinking_effort: str
    scope: str = "main"

    @property
    def tool_names(self) -> set[str]:
        """本轮实际发给模型的工具名集合（断言词汇①）。`tools` 为 None 时是空集。"""
        if not self.tools:
            return set()
        names = set()
        for schema in self.tools:
            fn = schema.get("function") if isinstance(schema, dict) else None
            if isinstance(fn, dict) and fn.get("name"):
                names.add(str(fn["name"]))
        return names

    @property
    def dynamic_reminder(self) -> str:
        """
        本轮的动态提醒全文（断言词汇⑪）。

        ⚠️ **已激活的 Skill 正文在这里，不在 `system` 参数里。**
        `loop.py` 每轮把环境信息、Plan Mode 提醒、已激活 Skill 的 SOP 正文等
        拼成**一条 system 角色的消息追加到历史末尾**，与走「稳定可缓存通道」的
        `system` 参数是两条完全不同的通道。

        把两者混作一谈会写出**永远失败的断言**——去 `system` 里找 SOP 正文是找不到的。
        这也正是「第 N 轮激活、第 N+1 轮生效」这条判据要读的地方。

        :returns: 末条消息若是 system 角色则返回其 content，否则空串
        """
        if not self.messages:
            return ""
        last = self.messages[-1]
        if getattr(last, "role", None) == "system":
            return getattr(last, "content", "") or ""
        return ""


# ---------------------------------------------------------------------------
# 假 Provider
# ---------------------------------------------------------------------------
@dataclass
class _CallLog:
    """`calls` 的可变容器 + 它的专用锁（只保护 append，见模块 docstring）。"""

    items: list[RecordedCall] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def is_classifier_request(system: Optional[str]) -> bool:
    """
    判断一次 provider 调用是不是 c16 分类器发出的。

    :param system: 本次调用的系统提示
    :returns: 是分类器请求返回 True

    ## 为什么需要这个判断

    分类器与主对话**共用同一个 `provider_factory`**。那是刻意的：绕过注入的
    工厂会让一次端到端测试**静默连上真实网络**（`_provider_for` 那条旁路
    踩过同一个坑，`bootstrap.py` 里写着）。

    代价是剧本 Provider 必须自己分辨两种请求——它按**调用次序**取轮次，
    分类器每判定一次就会吃掉一轮主对话的剧本。真实撞到过：C15 的并行组队
    剧本在分类器接进来之后整个错位，表现是「一条队友消息都没发出去」，
    看起来像协作功能坏了。

    副作用：无（纯函数）。
    """
    return bool(system) and CLASSIFIER_MARKER in system


class ScriptedProvider(BaseProvider):
    """
    按剧本作答的假 Provider。

    用法::

        provider = ScriptedProvider([
            [text("我先看看这个文件。"), tool("read_file", {"path": "a.py"}), done()],
            [text("看完了，结论是……"), done()],
        ])

    第 1 次调用产出第一组块，第 2 次产出第二组，第 3 次及以后产出 `fallback`。
    """

    def __init__(
        self,
        turns: Optional[list[list[StreamChunk]]] = None,
        fallback: Optional[list[StreamChunk]] = None,
    ):
        """
        :param turns: 每一轮要产出的数据块序列；缺省空剧本（每次调用都走兜底）
        :param fallback: 剧本耗尽后的兜底块序列；缺省一条含 `FALLBACK_MARKER` 的文本 + done
        """
        self._turns = list(turns or [])
        self._fallback = list(fallback) if fallback is not None else [text(FALLBACK_MARKER), done()]
        self._log = _CallLog()
        # c16：分类器请求的应答。缺省一律放行（`pass`），使**既有的每一份剧本
        # 一字不用改**——分类器接进来之前它们的行为就是「命令照跑」。
        #
        # 要验「分类器拦下了什么」的剧本可以把它换成 `"block"`，
        # 或直接覆写 `classifier_reply`。
        self.classifier_verdict = "pass"
        # 分类器被调了几次。**这是 spec AC3/AC12 那几条反证的唯一依据**——
        # 「allow 规则短路时零次调用」与「第一阶段通过时第二阶段零次调用」
        # 都只能靠计数分辨，断言结果的话两种情形看不出区别。
        self.classifier_calls = 0

    @property
    def calls(self) -> list[RecordedCall]:
        """
        留存的全部调用，**返回列表副本**——调用方拿到的是快照，
        误改它不会污染真实记录（也避免遍历时被 Worker 线程追加打断）。
        """
        with self._log.lock:
            return list(self._log.items)

    @property
    def call_count(self) -> int:
        """被调用的次数。比 `len(calls)` 省一次拷贝，用于「假模型是唯一被调实现」的计数断言。"""
        with self._log.lock:
            return len(self._log.items)

    def stream_chat(
        self,
        messages: list[Message],
        thinking_effort: str = "off",
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
    ) -> Iterator[StreamChunk]:
        """
        按调用次序产出剧本里的数据块。

        :returns: StreamChunk 迭代器
        副作用：往 `calls` 追加一条 `RecordedCall`。**不发起任何网络请求**。
        """
        # ⓪ 分类器请求走独立通道，**不消耗剧本轮次**（c16）。见 `classifier_reply`。
        if is_classifier_request(system):
            yield from self.classifier_reply(messages, system)
            return
        # ① 临界区：只做 append 与取序号，不做任何调度（spec N6）
        with self._log.lock:
            index = len(self._log.items)
            self._log.items.append(
                RecordedCall(
                    index=index,
                    messages=messages,
                    system=system,
                    tools=tools,
                    thinking_effort=thinking_effort,
                )
            )
        # ② 出锁后再取剧本与产出。取剧本本身是纯读，但产出是个生成器、
        #    消费方会边消费边跑 Agent 循环——那绝不能在锁里发生。
        chunks = self._turns[index] if index < len(self._turns) else self._fallback
        for chunk in chunks:
            yield chunk

    def classifier_reply(
        self, messages: list[Message], system: Optional[str]
    ) -> Iterator[StreamChunk]:
        """
        应答一次分类器请求（c16）。**不消耗剧本轮次、不进 `calls`。**

        :param messages: 分类器的请求消息
        :param system: 分类器的系统提示（据它分辨是第一阶段还是第二阶段）
        :returns: StreamChunk 迭代器

        副作用：`classifier_calls` 加一。

        缺省一律回 `pass`，理由见 `__init__` 里 `classifier_verdict` 的说明：
        既有剧本在分类器接进来之前的行为就是「命令照跑」，缺省放行使它们
        **一字不用改**。

        第二阶段要求「结论 + 理由」两行格式，所以这里按系统提示里有没有
        「复核」二字分别作答——回错格式会走进 `parse_stage2` 的
        「看不懂」分支，那条路通往拒绝，会让剧本莫名其妙地失败。
        """
        with self._log.lock:
            self.classifier_calls += 1
        verdict = self.classifier_verdict
        if system and "复核" in system:
            yield text(f"结论: {verdict}\n理由: 剧本固定应答")
        else:
            yield text(verdict)
        yield done()


class CountingProviderFactory:
    """
    包一层计数的工厂，用于断言「假模型是**唯一**被调用的模型实现」（AC27 / AC30）。

    每次被 `build_app` 或 `_provider_for` 调用都会记一笔，并返回同一个（或按模型名
    区分的）`ScriptedProvider`。

    用法::

        factory = CountingProviderFactory(provider)
        build_app(cfg, provider_factory=factory, ...)
        ...
        assert factory.created == ["deepseek-chat", "other-model"]
    """

    def __init__(self, provider: ScriptedProvider, per_model: Optional[dict] = None):
        """
        :param provider: 缺省返回的假 Provider
        :param per_model: 可选的「模型名 → 专属假 Provider」映射，
            用于验证换模型旁路确实走了另一个实例
        """
        self._provider = provider
        self._per_model = dict(per_model or {})
        self.created: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, cfg: Any) -> BaseProvider:
        model = getattr(cfg, "model", "")
        with self._lock:
            self.created.append(model)
        return self._per_model.get(model, self._provider)


class ScopedScriptedProvider(BaseProvider):
    """
    **按作用域分派**的假 Provider——多 Agent 协作剧本（C13/C15）的必需品。

    ## 为什么 `ScriptedProvider` 不够用

    它按「第几次被调用」取轮次，而那个计数是**全进程共享**的。
    单条对话时这没问题；一旦有子 Agent 并发跑，主对话的第 2 轮与
    worker 的第 1 轮谁先调模型完全取决于线程调度——同一份剧本每次跑
    都可能对应到不同的 Agent 身上。**剧本一旦不确定，判据就不可信**，
    而这正是 C15 至今没有脚本化场景的直接原因。

    ## 分派依据：trace 的线程本地作用域

    `subagents/runner.py` 在子 Agent 线程启动时会
    `recorder.bind_scope(subagent_scope(队员名或角色名))`，主对话则是 `main`。
    它存在 `threading.local` 里，因此**在 `stream_chat` 里读到的一定是
    当前这条对话自己的作用域**——天然的分派键，不必自己再传一遍上下文。

    用法::

        ScopedScriptedProvider({
            "main": [
                [text("我派两个人去。"), tool("run_agent", {...}), done()],
                [text("都回来了。"), done()],
            ],
            "subagent:worker-a": [
                [text("我做完了 A。"), tool("send_message", {...}), done()],
            ],
        })

    键写 `"*"` 表示「其余全部作用域」，用于不关心是谁的兜底剧本。

    ## 与 `ScriptedProvider` 的关系

    刻意**不做成子类**：两者的取轮语义完全相反（全局序 vs 每作用域序），
    继承会让「我用的是哪种」在调用点看不出来。断言用的 `calls` 与
    `call_count` 两个属性保持同名同义，场景代码切换成本很低。
    """

    def __init__(
        self,
        scripts: dict[str, list[list[StreamChunk]]],
        fallback: Optional[list[StreamChunk]] = None,
    ):
        """
        :param scripts: 作用域名 → 该作用域的逐轮剧本。键 `"*"` 是兜底剧本
        :param fallback: 剧本耗尽后的块序列；缺省一条含 `FALLBACK_MARKER` 的文本 + done
        """
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self._fallback = list(fallback) if fallback is not None else [text(FALLBACK_MARKER), done()]
        self._log = _CallLog()
        # 每个作用域各自的轮次游标。**必须与 `_log.lock` 同一把锁保护**——
        # 取游标与记调用是同一件事的两半，分两把锁会让两者错位。
        self._cursor: dict[str, int] = {}
        # c16：分类器应答与计数，语义与 `ScriptedProvider` 上那两个字段完全一致。
        # **刻意不抽公共基类**：两个类的其余部分（游标是全局的还是按作用域的）
        # 差别足够大，为两个字段造一层继承会让「这两个类到底哪不一样」更难看清。
        self.classifier_verdict = "pass"
        self.classifier_calls = 0

    # 分类器应答复用 `ScriptedProvider` 的实现（两个类的这部分逐字相同）。
    classifier_reply = ScriptedProvider.classifier_reply

    @property
    def calls(self) -> list[RecordedCall]:
        """留存的全部调用（副本）。与 `ScriptedProvider` 同义。"""
        with self._log.lock:
            return list(self._log.items)

    @property
    def call_count(self) -> int:
        with self._log.lock:
            return len(self._log.items)

    def calls_in(self, scope: str) -> list[RecordedCall]:
        """
        某个作用域下的调用（按发生顺序）。

        断言「worker-a 一共只跑了 2 轮」这类判据要用它——`calls` 是全部
        作用域混在一起的，数出来的轮次没有意义。
        """
        with self._log.lock:
            return [c for c in self._log.items if c.scope == scope]

    def stream_chat(
        self,
        messages: list[Message],
        thinking_effort: str = "off",
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
    ) -> Iterator[StreamChunk]:
        """
        按**本线程所属作用域**的轮次游标产出数据块。

        :returns: StreamChunk 迭代器
        副作用：往 `calls` 追加一条 `RecordedCall`；推进该作用域的游标。
        """
        # ⓪ 分类器请求走独立通道，**不推进任何作用域的游标**（c16）。
        #
        # ⚠ 这一条在本类上比在 `ScriptedProvider` 上更要紧：这里的游标是**按作用域**
        # 分开的，而分类器请求跑在**发起它的那个 Agent 的线程**上——于是它会去推进
        # 那个队员的游标。真实撞到过：C15 的并行组队剧本因此整个错位，
        # 表现是「一条队友消息都没发出去」，看起来像协作功能坏了。
        if is_classifier_request(system):
            yield from self.classifier_reply(messages, system)
            return

        scope = _current_scope()

        # ① 临界区：取游标 + 记调用，只做纯内存操作，不做任何调度
        with self._log.lock:
            index = len(self._log.items)
            turn_index = self._cursor.get(scope, 0)
            self._cursor[scope] = turn_index + 1
            self._log.items.append(
                RecordedCall(
                    index=index,
                    messages=messages,
                    system=system,
                    tools=tools,
                    thinking_effort=thinking_effort,
                    scope=scope,
                )
            )

        # ② 出锁之后再产出（生成器的消费方会跑很久，绝不能在锁内）
        script = self._scripts.get(scope)
        if script is None:
            script = self._scripts.get("*")
        chunks = (
            script[turn_index] if script is not None and turn_index < len(script) else self._fallback
        )
        for chunk in chunks:
            yield chunk


def _current_scope() -> str:
    """
    当前线程的 trace 作用域；取不到就当主对话。

    延迟导入是刻意的：`scripted.py` 的其余部分只依赖 `provider.base`
    （一个零副作用的纯抽象模块），把 trace 的导入放在函数里，
    可以让不使用 `ScopedScriptedProvider` 的场景保持原有的依赖面。
    """
    try:
        from rhinecode.trace.recorder import current_scope

        return current_scope()
    except Exception:  # noqa: BLE001 —— 测试设施，取不到就退回主对话
        return "main"
