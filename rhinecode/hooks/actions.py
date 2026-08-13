"""
四种动作的执行器（spec F4）：把一个 `HookAction` 跑起来，产出统一的 `ActionOutcome`。

四个执行器签名一致 `(action, payload, ...) -> ActionOutcome`，由 `run_action` 按类型分派。

## ⚠ `ok` 与 `verdict` 是两回事

- `ok`      —— **动作自身**跑完了没有。脚本崩了、超时了、HTTP 连不上 → False
- `verdict` —— 动作给出的**拦截结论**。DENY / ASK / NONE

一个成功跑完并明确判 DENY 的 Hook，`ok` 是 True 而 `verdict` 是 DENY。
两者混成一个字段会让「Hook 成功运行并放行」与「Hook 崩了」无法区分——
前者该继续、后者该拦截（spec F7.1 的 fail-closed 判据是 `ok` 为假，不是 `verdict` 为 DENY）。

## 本层不做的两件事

1. **不判断该不该 fail-closed** —— 那要看事件类型，是 `manager` 的职责。
   本层只如实报告 `ok` 与 `verdict`。
2. **不合并多条结论** —— 同上。

## 安全边界

- `command` 动作**过①危险命令黑名单**。①层的既有性质是「不可被任何配置或权限模式
  放开」，而 `hooks.yaml` 就是配置——尤其项目级的那份可能来自别人的仓库。
  ②③④⑤不过：Hook 是用户配置而非模型行为，过完整管线等于每次自动化都弹确认。
- `http` 动作**过②′网络边界层的结构性硬校验**（禁 `file://`、禁内嵌凭据、
  禁回环与非公网地址），复用 `permission/network.py` 的同一份实现，
  **不在本模块另写一份**。
- **配置里不做任何字符串插值**（spec N3）。上下文只经标准输入的 JSON 抵达命令，
  因此模型生成的工具参数不可能被拼进 shell 命令行。
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from rhinecode.hooks.models import (
    ActionOutcome,
    AgentAction,
    CommandAction,
    HookAction,
    HookDecision,
    HookEventType,
    HookPayload,
    HttpAction,
    PromptAction,
)
from rhinecode.permission import blacklist
from rhinecode.permission.network import check_hard
from rhinecode.tools.path_guard import main_project_root
from rhinecode.tools.run_command import decode_subprocess_output, run_shell_captured

# stdout / stderr 进 `detail` 前的截断长度。
#
# ⚠️ **它管的是「给模型看的那一份」，不是 trace。** 原注释写着「防止 trace 爆掉」
# ——那个理由已被推翻：观测设施不该替读它的人决定哪些内容不重要。
# 完整原文现在走 `ActionOutcome.full_detail` 无损进记录。
#
# 这个上限**仍然必要**，但理由变了：失败路径上 `detail` 会经
# `HOOK_FAILED_REASON` 回灌给模型（见 `manager.py`），一条刷屏的 Hook
# 没有上限就能把上下文撑爆。
DETAIL_LIMIT = 2000

# 退出码 2 = 拦截（对齐 Claude Code 的 hooks 约定）。
EXIT_BLOCK = 2

# `agent` 占位动作的说明文案。
AGENT_PLACEHOLDER = "子 Agent 动作尚未支持（等待 SubAgent 章节接入），本次未执行。"

# 非 pre_tool_use 事件上 Hook 却给出了决策时的说明（spec F8 第 8 项的运行期落点）。
#
# 为什么在这里而不是加载期：唯一「会产出决策」的动作是 `command`，而
# `post_tool_use` + `command` 正是最常见的正当用法（自动格式化）。加载期无从区分
# 「这个脚本会不会返回决策」，照字面校验会给每一条格式化 Hook 都挂一条警告，
# 警告区随即失去可读性。运行期则有确切信息：它**真的**返回了决策，那才值得说一句。
DECISION_IGNORED = "（该结论已忽略：拦截只在 pre_tool_use 事件上生效）"


def _clip(text: str) -> str:
    """把一段输出截断到 `DETAIL_LIMIT`，超长时标注原长。"""
    if len(text) <= DETAIL_LIMIT:
        return text
    return text[:DETAIL_LIMIT] + f"…（共 {len(text)} 字符，已截断）"


def _payload_json(payload: HookPayload) -> str:
    """
    把事件负载序列化成喂给 Hook 的 JSON。

    :param payload: 事件负载
    :returns: JSON 文本；含无法序列化的值时退回一个尽力而为的字符串化版本

    `default=str` 兜住任何非 JSON 原生类型（如枚举、Path）——**序列化失败绝不能
    让一次事件分发抛异常**，那会把观测/自动化设施变成故障源。
    """
    return json.dumps(payload.fields, ensure_ascii=False, default=str)


def parse_decision(stdout: str) -> tuple[HookDecision, str]:
    """
    把 Hook 的标准输出解析成拦截结论（spec F6.1）。

    :param stdout: 命令的标准输出原文
    :returns: `(结论, 原因)`

    识别的形态：
    - 空、非 JSON、非对象 → `(NONE, "")`，即「不表态」。**这不是失败**——
      绝大多数 Hook 根本不输出任何东西，把它当错误会让 fail-closed 拦下一切。
    - `{"decision": "deny", "reason": "..."}` → DENY
    - `{"decision": "ask",  "reason": "..."}` → ASK

    ## ⚠ `allow` 与未知取值一律按「不表态」处理

    **这是决策 1A 的最后一道闸。** spec 与配置模板都写明了「Hook 不能放行」，
    但真正拦住它的是这个分支：一个照着 Claude Code 文档写出来的 Hook 会输出
    `{"decision":"allow"}`，若这里不显式忽略，随手写个 `dict.get` 映射就可能
    让它变成放行语义——而那意味着 Hook 能翻过①黑名单与②沙箱。

    未知取值同样按不表态：偏严（继续走五层）且不崩溃。

    副作用：无。
    """
    text = (stdout or "").strip()
    if not text:
        return HookDecision.NONE, ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return HookDecision.NONE, ""
    if not isinstance(data, dict):
        return HookDecision.NONE, ""

    raw = data.get("decision")
    reason = data.get("reason")
    reason_text = reason if isinstance(reason, str) else ""

    # 白名单式匹配：只认这两个。`allow` 与任何未知取值都落到最后的 NONE。
    if raw == HookDecision.DENY.value:
        return HookDecision.DENY, reason_text
    if raw == HookDecision.ASK.value:
        return HookDecision.ASK, reason_text
    return HookDecision.NONE, ""


def _apply_event_scope(
    payload: HookPayload, verdict: HookDecision, detail: str
) -> tuple[HookDecision, str]:
    """
    非 `pre_tool_use` 事件上的决策一律作废，并在 detail 里说明（见 DECISION_IGNORED）。

    :returns: `(生效后的结论, 补充说明后的 detail)`

    副作用：无。
    """
    if verdict == HookDecision.NONE or payload.event == HookEventType.PRE_TOOL_USE:
        return verdict, detail
    suffix = DECISION_IGNORED if not detail else f"{detail}\n{DECISION_IGNORED}"
    return HookDecision.NONE, suffix


def run_prompt_action(action: PromptAction, payload: HookPayload) -> ActionOutcome:
    """
    注入提示词（spec F4.2）。无 I/O，不可能失败。

    :returns: `ok=True`，`injected_text` 为待注入文本

    真正的「一次性」语义由 `HookManager.consume_injections` 保证，本函数只产出文本。

    副作用：无。
    """
    return ActionOutcome(ok=True, injected_text=action.text, detail="已排入注入队列")


def run_agent_action(action: AgentAction, payload: HookPayload) -> ActionOutcome:
    """
    启动子 Agent —— **本章占位，不执行**（spec F4.4）。

    :returns: `ok=True`、`verdict=NONE`

    ## ⚠ `ok` 必须为 True，不可改成 False

    占位不是故障。若按「失败」处理，`pre_tool_use` 上的 fail-closed（spec F7.1）
    会把**每一次工具调用**都拦下——一条用户明知还没接通的占位规则，
    会让整个 Agent 干不了任何事。

    副作用：无。
    """
    return ActionOutcome(ok=True, detail=AGENT_PLACEHOLDER)


def run_command_action(
    action: CommandAction,
    payload: HookPayload,
    cwd: Optional[Path] = None,
) -> ActionOutcome:
    """
    执行 shell 命令（spec F4.1）。

    :param action: 命令动作
    :param payload: 事件负载，序列化成 JSON 从**标准输入**喂给命令
    :param cwd: 触发本次事件的 Agent 的工作目录（c14 F25）。`None` 取主项目根
    :returns: 执行结果

    执行步骤：
    1. **过①危险命令黑名单**。命中即返回 `ok=False`，**子进程不启动**。
    2. 负载序列化成 JSON，以 UTF-8 字节写入子进程标准输入。
    3. 以 shell 方式在项目根运行，捕获 stdout/stderr 字节，带超时——
       **超时会杀掉整棵进程树**，`timeout` 是真的等待上限而不只是一句文案
       （复用 `tools/run_command.py` 的 `run_shell_captured`）。
    4. **自己解码**（复用 `tools/run_command.py` 的 `decode_subprocess_output`）。
    5. 按退出码分支：0 → 试解析决策 JSON；2 → DENY（stderr 作原因）；其它 → 失败。

    :raises: 不抛异常。超时、启动失败、任何 OS 错误都转成 `ok=False`。

    副作用：**起一个子进程**，它可以读写文件、访问网络——Hook 命令不过②沙箱。
    """
    started = time.monotonic()

    def _finish(**kwargs) -> ActionOutcome:
        """统一填耗时，避免每条 return 手写一次（漏填不报错，只是统计恒为 0）。"""
        kwargs.setdefault("ok", False)
        kwargs["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        return ActionOutcome(**kwargs)

    # ── ① 危险命令黑名单 ──
    # hooks.yaml 是配置，而①层的既有性质是「不可被任何配置或权限模式放开」。
    # 尤其项目级的那份可能来自别人的仓库，一条挂在 session_start 上的
    # `rm -rf ~` 会在启动那一刻就跑起来。
    hit = blacklist.check_command(action.command)
    if hit:
        return _finish(detail=f"命中危险命令黑名单：{hit}，未执行。")

    try:
        # ⚠ 走 `run_shell_captured` 而不是 `subprocess.run`：后者超时后只杀
        # shell 壳层，孙子进程还握着输出管道，于是**要一直等到命令自己跑完**
        # ——`timeout` 只改变返回的文案。而这里的等待发生在 Hook 分发路径上，
        # 一条挂了的 `pre_tool_use` 命令能把整次工具调用拖住任意久
        # （与 `hooks/manager.py` 那条「锁里不做动作执行」是同一隐患的两半）。
        proc = run_shell_captured(
            action.command,
            # c14 F25：命令跑在**触发它的那个 Agent 的工作目录**里。
            # 主对话触发 → 主项目根；隔离子 Agent 触发 → 它的隔离工作区。
            # 一个检查文件的 pre_tool_use 钩子应当看到子 Agent 正在动的那个文件，
            # 而不是主项目根里的同名文件。
            cwd=str(cwd if cwd is not None else main_project_root()),
            stdin_bytes=_payload_json(payload).encode("utf-8"),
            timeout=action.timeout,
        )
    except subprocess.TimeoutExpired:
        return _finish(detail=f"Hook 命令超时（{action.timeout} 秒）。")
    except Exception as exc:  # noqa: BLE001 —— OSError / 权限 / 找不到 shell 等
        return _finish(detail=f"Hook 命令启动失败：{exc}")

    stdout = decode_subprocess_output(proc.stdout)
    stderr = decode_subprocess_output(proc.stderr)
    code = proc.returncode

    def _detail(extra: str = "") -> str:
        """给模型/报告看的那一份：两路输出各自裁到 `DETAIL_LIMIT`。"""
        parts = [f"退出码 {code}"]
        if extra:
            parts.append(extra)
        if stdout.strip():
            parts.append(f"stdout: {_clip(stdout.strip())}")
        if stderr.strip():
            parts.append(f"stderr: {_clip(stderr.strip())}")
        return "\n".join(parts)

    def _full_detail(extra: str = "") -> Optional[str]:
        """
        同一份内容的完整原文，只进 trace（见 `ActionOutcome.full_detail`）。

        :returns: 与 `_detail` 结果相同时返回 None——两份一模一样时多存一份
                  纯属让记录文件白白翻倍
        """
        parts = [f"退出码 {code}"]
        if extra:
            parts.append(extra)
        if stdout.strip():
            parts.append(f"stdout: {stdout.strip()}")
        if stderr.strip():
            parts.append(f"stderr: {stderr.strip()}")
        full = "\n".join(parts)
        return full if full != _detail(extra) else None

    # ── 退出码 2：拦截 ──
    if code == EXIT_BLOCK:
        reason = stderr.strip() or stdout.strip() or "Hook 以退出码 2 拦截了这次调用。"
        verdict, detail = _apply_event_scope(payload, HookDecision.DENY, _detail())
        return _finish(ok=True, verdict=verdict, reason=_clip(reason), detail=detail,
                       full_detail=_full_detail())

    # ── 退出码 0：通过，可能带决策 JSON ──
    if code == 0:
        parsed, reason = parse_decision(stdout)
        verdict, detail = _apply_event_scope(payload, parsed, _detail())
        if verdict != HookDecision.NONE and not reason:
            reason = f"Hook 规则要求{'拦截' if verdict == HookDecision.DENY else '人工确认'}。"
        return _finish(ok=True, verdict=verdict, reason=_clip(reason), detail=detail,
                       full_detail=_full_detail())

    # ── 其它非 0：失败 ──
    return _finish(detail=_detail("Hook 命令以非预期退出码结束"),
                   full_detail=_full_detail("Hook 命令以非预期退出码结束"))


def run_http_action(
    action: HttpAction,
    payload: HookPayload,
    client_factory: Optional[Callable[[], Any]] = None,
) -> ActionOutcome:
    """
    发一个 HTTP 请求（spec F4.3）。

    :param action: HTTP 动作
    :param payload: 事件负载；`action.body` 为空时作为请求体发出
    :param client_factory: 造 HTTP 客户端的工厂，缺省用真 `httpx.Client`。
                           **可注入是硬要求**——测试与端到端场景必须能离线跑
                           （形态与 `web/manager.py`、`bootstrap` 的注入点一致）
    :returns: 执行结果，**`verdict` 恒为 NONE**

    执行步骤：
    1. **过②′结构性硬校验**（`permission.network.check_hard`）。命中即返回，
       **请求不发出**。
    2. 发请求，超时按 `action.timeout`。
    3. 非 2xx / 任何异常 → `ok=False`。

    ## ⚠ 响应不参与任何决策

    这是刻意的：让一个外部服务能决定本地工具跑不跑，可用性与安全性都不成立——
    服务挂了整个 Agent 就动不了，服务被攻陷则攻击者获得了本地工具的开关。

    ## ⚠ 这是本项目第二条主动外发链路

    与 `web_fetch`「只取不发」不同，本动作明确要发 body 与 header。
    硬校验必须复用 `permission/network.py` 的同一份实现，**不要在这里另写一份**——
    那是典型的「改一处漏一处」，且漏改不报错，只是某个地址悄悄能访问了。

    副作用：**向外部地址发送数据**。
    """
    started = time.monotonic()

    def _finish(**kwargs) -> ActionOutcome:
        kwargs.setdefault("ok", False)
        kwargs["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        return ActionOutcome(**kwargs)

    hard = check_hard(action.url)
    if hard is not None:
        return _finish(detail=f"网络边界拒绝：{hard}，请求未发出。")

    headers = dict(action.headers)
    if action.body is None:
        body = _payload_json(payload)
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
    else:
        body = action.body

    try:
        if client_factory is not None:
            client = client_factory()
        else:
            import httpx  # 延迟导入：没有 http 动作时不必付这个代价

            client = httpx.Client(timeout=action.timeout, follow_redirects=False)
        with client:
            response = client.request(
                action.method,
                action.url,
                headers=headers,
                content=body.encode("utf-8"),
                timeout=action.timeout,
            )
    except Exception as exc:  # noqa: BLE001 —— 连接失败 / 超时 / 协议错误
        return _finish(detail=f"HTTP 请求失败：{type(exc).__name__}: {exc}")

    status = getattr(response, "status_code", 0)
    ok = 200 <= status < 300
    detail = f"HTTP {status}"
    return _finish(ok=ok, detail=detail if ok else f"{detail}（非 2xx，按失败处理）")


def run_action(
    action: HookAction,
    payload: HookPayload,
    client_factory: Optional[Callable[[], Any]] = None,
    cwd: Optional[Path] = None,
) -> ActionOutcome:
    """
    按类型分派到对应执行器。

    :param action: 四种动作之一
    :param payload: 事件负载
    :param client_factory: 透传给 HTTP 执行器
    :param cwd: 触发本次事件的 Agent 的工作目录（c14 F25）。只有 shell 动作用得上
                ——注入型动作没有工作目录的概念，HTTP 动作发的是网络请求
    :returns: 执行结果；未知类型（理论上不可能，加载期已校验）→ `ok=False`

    副作用：取决于具体动作，见各执行器。
    """
    if isinstance(action, CommandAction):
        return run_command_action(action, payload, cwd)
    if isinstance(action, PromptAction):
        return run_prompt_action(action, payload)
    if isinstance(action, HttpAction):
        return run_http_action(action, payload, client_factory)
    if isinstance(action, AgentAction):
        return run_agent_action(action, payload)
    # 兜底：加载期已保证类型合法，走到这里说明有人绕过了解析层直接构造规则。
    return ActionOutcome(ok=False, detail=f"未知的动作类型：{type(action).__name__}")


__all__ = [
    "AGENT_PLACEHOLDER",
    "DECISION_IGNORED",
    "EXIT_BLOCK",
    "parse_decision",
    "run_action",
    "run_agent_action",
    "run_command_action",
    "run_http_action",
    "run_prompt_action",
]
