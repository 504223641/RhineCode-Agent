# RhineCode Agent Loop Tasks

> 自底向上：数据类型 → Provider 基础 → 收集器/提示/特殊工具 → 循环引擎 → 协调层 → TUI。
> 每个任务 2–5 分钟，完成即跑「验证」。验证里的 `python -c "..."` 在项目根目录执行（已 `pip install -e .`）。

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `rhinecode/agent/__init__.py` | 标记 agent 包 |
| 新建 | `rhinecode/agent/events.py` | AgentEvent / AgentEventType / StopReason / Usage / ClarifyOption / ConfirmDecision |
| 新建 | `rhinecode/agent/collector.py` | StreamCollector（双路收集） |
| 新建 | `rhinecode/agent/prompt.py` | build_plan_prompt（Plan Mode 引导提示） |
| 新建 | `rhinecode/agent/plan_tools.py` | AskUserTool / PresentPlanTool / 名称常量 / plan_schemas |
| 新建 | `rhinecode/agent/loop.py` | Agent（ReAct 循环引擎） |
| 修改 | `rhinecode/provider/base.py` | StreamChunk 增 usage 字段 + "usage" 类型 |
| 修改 | `rhinecode/provider/deepseek.py` | stream_options 取 usage、产出 usage chunk |
| 修改 | `rhinecode/conversation.py` | 委托 Agent；plan_mode/always_allow/cancel 状态；/plan；回调封装 |
| 修改 | `rhinecode/tui/widgets.py` | StatusBar(plan+usage)；ConfirmPanel 三项；新增 ClarifyPanel |
| 修改 | `rhinecode/tui/app.py` | 消费 AgentEvent；确认三态/澄清/审批回调；取消键；/plan；状态栏 |
| 修改 | `rhinecode/__main__.py` | 注释/装配核对（如需） |

---

## T1: 建立 agent 包与事件数据类型

**文件：** `rhinecode/agent/__init__.py`、`rhinecode/agent/events.py`
**依赖：** 无
**步骤：**
1. 新建空 `agent/__init__.py`（含一行包说明注释）。
2. 在 `events.py` 定义枚举 `AgentEventType`（TEXT/THINKING/TOOL_START/TOOL_RESULT/USAGE/PROGRESS/FINISHED/ERROR，继承 `str, Enum`）。
3. 定义枚举 `StopReason`（COMPLETED/MAX_ITERATIONS/USER_CANCELLED/PLAN_REJECTED/UNKNOWN_TOOL/STREAM_ERROR）。
4. 定义枚举 `ConfirmDecision`（ALLOW/ALLOW_ALWAYS/DENY）。
5. 定义 `@dataclass Usage`（prompt_tokens/completion_tokens/total_tokens: int）。
6. 定义 `@dataclass ClarifyOption`（summary: str、detail: str）。
7. 定义 `@dataclass AgentEvent`，字段按 plan：`type` + `text`/`tool_call`/`tool_result`/`usage`/`iteration`/`stop_reason`/`message`，均带默认值；`tool_call` 引用 `provider.base.ToolCall`，`tool_result`/`usage` 用 `Any`/`Optional`。
8. 全部成员写中文注释，说明各字段在何种事件类型下有效。

**验证：**
```
python -c "from rhinecode.agent.events import AgentEvent, AgentEventType, StopReason, Usage, ClarifyOption, ConfirmDecision; print(AgentEventType.TEXT, StopReason.COMPLETED, ConfirmDecision.ALLOW_ALWAYS)"
```
期望：打印三个枚举值，无导入错误。

---

## T2: StreamChunk 增加 usage 支持

**文件：** `rhinecode/provider/base.py`
**依赖：** 无
**步骤：**
1. 给 `StreamChunk` dataclass 新增字段 `usage: Any = None`（复用已 import 的 `Any`）。
2. 在 `StreamChunk` 文档字符串的 type 说明里补充 `"usage"`：流中携带 token 用量，载荷在 `usage` 字段。
3. 不改其他字段，保持向后兼容。

**验证：**
```
python -c "from rhinecode.provider.base import StreamChunk; c=StreamChunk(type='usage', usage={'total_tokens':10}); print(c.type, c.usage)"
```
期望：`usage {'total_tokens': 10}`。

---

## T3: DeepSeek Provider 产出 token 用量

**文件：** `rhinecode/provider/deepseek.py`
**依赖：** T2
**步骤：**
1. 在 `stream_chat` 的 `create_kwargs` 中加入 `"stream_options": {"include_usage": True}`，使流末尾返回 usage。
2. 在流式 `for chunk in stream` 循环里，新增：`usage = getattr(chunk, "usage", None); if usage: yield StreamChunk(type="usage", usage=usage)`（放在读取 delta 之前或之后均可，但需在 `done` 之前产出）。注意带 usage 的尾块通常 `choices` 为空，已有的 `if not delta: continue` 不能吞掉 usage——确保先处理 usage 再 continue。
3. 补充中文注释说明 include_usage 的作用与尾块特征。

**验证：**
```
python -c "import rhinecode.provider.deepseek"
```
期望：导入无错误（真实用量在端到端阶段验证）。

---

## T4: 双路流式收集器 StreamCollector

**文件：** `rhinecode/agent/collector.py`
**依赖：** T1、T2
**步骤：**
1. 定义 `StreamCollector`：`__init__` 初始化 `self.text=""`、`self.tool_calls=[]`、`self.usage=None`。
2. 实现 `feed(chunk) -> Optional[AgentEvent]`：
   - `type=="text"`：累积到 `self.text`，返回 `AgentEvent(TEXT, text=chunk.content)`。
   - `type=="thinking"`：返回 `AgentEvent(THINKING, text=chunk.content)`（不累积进 text）。
   - `type=="tool_call"`：`self.tool_calls.append(chunk.tool_call)`，返回 `None`。
   - `type=="usage"`：把 `chunk.usage` 转 `Usage`（按 SDK 字段 prompt_tokens/completion_tokens/total_tokens，缺失给 0），存 `self.usage`，返回 `AgentEvent(USAGE, usage=...)`。
   - 其他（done/error）：返回 `None`。
3. 写中文注释解释「双路」：feed 返回值供实时展示、属性供循环判断。

**验证：**
```
python -c "from rhinecode.agent.collector import StreamCollector; from rhinecode.provider.base import StreamChunk, ToolCall; c=StreamCollector(); print(c.feed(StreamChunk(type='text',content='hi')).text); c.feed(StreamChunk(type='tool_call', tool_call=ToolCall(id='1',name='read_file',arguments={}))); print(c.text, len(c.tool_calls))"
```
期望：`hi` 与 `hi 1`。

---

## T5: Plan Mode 引导提示

**文件：** `rhinecode/agent/prompt.py`
**依赖：** 无
**步骤：**
1. 实现 `build_plan_prompt() -> str`，返回一段中文 system 提示，要求模型：先用只读工具调研，不执行任何修改类操作；对不清楚的需求细节用 `ask_user` 工具逐一向用户提问（options 第一个为最推荐项）；调研与澄清完成后用 `present_plan` 工具提交计划等待用户审批；获批前不得修改文件或执行命令。
2. 写中文注释说明该提示仅在 Plan Mode 注入。

**验证：**
```
python -c "from rhinecode.agent.prompt import build_plan_prompt; s=build_plan_prompt(); print(len(s)>0, 'ask_user' in s, 'present_plan' in s)"
```
期望：`True True True`。

---

## T6: Plan Mode 特殊工具 schema

**文件：** `rhinecode/agent/plan_tools.py`
**依赖：** 无（用 `tools.base.Tool`）
**步骤：**
1. 定义名称常量 `ASK_USER = "ask_user"`、`PRESENT_PLAN = "present_plan"`。
2. 定义 `AskUserTool(Tool)`：`name=ASK_USER`、`read_only=True`、`description`（说明用于向用户澄清、options 第一个为推荐项）、`parameters` 为 `{type:object, properties:{question:string, options:array[{summary,detail}]}, required:[question,options]}`；`execute` 返回兜底 `ToolResult(ok=False, "该工具由循环拦截处理")`（正常不会被调用）。
3. 定义 `PresentPlanTool(Tool)`：`name=PRESENT_PLAN`、`read_only=True`、`description`（提交计划等待审批）、`parameters` 为 `{type:object, properties:{plan:string}, required:[plan]}`；`execute` 同样兜底。
4. 实现 `plan_schemas() -> list[dict]`：返回 `[AskUserTool().to_schema(), PresentPlanTool().to_schema()]`。
5. 写中文注释说明这两个工具「只暴露 schema、由循环路由到回调」。

**验证：**
```
python -c "from rhinecode.agent.plan_tools import plan_schemas, ASK_USER, PRESENT_PLAN; s=plan_schemas(); print([t['function']['name'] for t in s])"
```
期望：`['ask_user', 'present_plan']`。

---

## T7: Agent 循环引擎

**文件：** `rhinecode/agent/loop.py`
**依赖：** T1、T3、T4、T6
**步骤：**
1. 定义模块常量 `MAX_ITERATIONS = 25`、`MAX_CONSECUTIVE_UNKNOWN = 3`。
2. 定义 `Agent`：`__init__(self, provider, registry)` 保存依赖。
3. 实现私有 `_schema_for(plan_mode, execution_phase) -> Optional[list[dict]]`：Plan Mode 且未获批 → 只读工具 schema + `plan_schemas()`；否则全部工具 schema（无 registry 时 None）。只读工具 schema 可通过遍历 `registry` 内工具按 `read_only` 过滤生成（必要时给 registry 加一个只读筛选 helper，或在 loop 内过滤 `tool.to_schema()`）。
4. 实现 `run(history, thinking_effort, plan_mode, system_prompt, confirm, clarify, approve_plan, cancel_event) -> Iterator[AgentEvent]`，按 plan 的主循环逻辑：迭代上限 for 循环；每轮先查 cancel、产出 PROGRESS、算 tools、组装 `[system?]+history` 请求、用 `StreamCollector` 双路消费并转发事件、处理流错误、判断有无 tool_calls（无则 COMPLETED）、追加 assistant、调 `_execute`、回灌 tool 结果、更新 consecutive_unknown 与 execution_phase、检查停止条件；走完上限产出 FINISHED(MAX_ITERATIONS)。
5. 实现 `_execute(tool_calls, results, ctx, confirm, clarify, approve_plan, cancel_event) -> Iterator[AgentEvent]`：
   - 分流特殊工具 / 只读 / 副作用（含未知）。
   - `ask_user`：解析 options 为 `list[ClarifyOption]`，调 `clarify`；返回 None → `ctx["cancelled"]=True` 且结果文本「用户取消」；否则结果文本为所选概述。
   - `present_plan`：先产出 `AgentEvent(TEXT, text=plan)`，让计划全文进入聊天记录；再调 `approve_plan(plan)`；批准 → `ctx["approved"]=True`，结果文本「用户已批准，开始执行」；拒绝 → `ctx["plan_rejected"]=True`，结果文本「用户暂未批准计划，已停止本次执行」。
   - 只读工具并发执行（迁移 c3 `_run_readonly_concurrent` 逻辑，产出 AgentEvent.TOOL_START/RESULT）。
   - 副作用/未知工具串行执行（迁移 c3 `_run_one_serial`）：未知工具记 `ctx["unknown"]+=1` 且结构化错误；副作用工具始终调 `confirm` 决定执行/拒绝。计划审批只切换到执行阶段，不跳过逐工具确认；只有会话级 `ALLOW_ALWAYS` 会让 `confirm` 自动放行。
   - 已知工具被调用时 `ctx["had_known"]=True`（供主循环清零连续未知计数）。
6. 全程 try/except 兜底为 `ToolResult(ok=False)`，事件类型用 AgentEvent。
7. 充分中文注释（循环不变式、停止条件、安全点、特殊工具路由）。

**验证：**
```
python -c "
from rhinecode.agent.loop import Agent
from rhinecode.provider.base import StreamChunk
from rhinecode.agent.events import AgentEventType, StopReason
import threading
class FakeP:
    def stream_chat(self, msgs, effort='off', tools=None):
        yield StreamChunk(type='text', content='done')
        yield StreamChunk(type='done')
a=Agent(FakeP(), None)
evs=list(a.run([], 'off', False, None, lambda *x:True, lambda *x:None, lambda *x:True, threading.Event()))
types=[e.type for e in evs]
print(AgentEventType.PROGRESS in types, AgentEventType.TEXT in types, evs[-1].type==AgentEventType.FINISHED, evs[-1].stop_reason==StopReason.COMPLETED)
"
```
期望：`True True True True`（无工具调用 → 自然完成）。

---

## T8: ConversationManager 委托 Agent

**文件：** `rhinecode/conversation.py`
**依赖：** T1、T5、T7
**步骤：**
1. 顶部导入 `Agent`、`build_plan_prompt`、`ConfirmDecision`、`ClarifyOption`、`AgentEvent`。
2. `__init__`：构造 `self._agent = Agent(provider, registry)`；新增 `self.plan_mode=False`、`self._always_allow=False`、`self._cancel_event`（`threading.Event()`）；回调字段 `self.confirm_callback`（返回 `ConfirmDecision`）、`self.clarify_callback`、`self.approve_plan_callback`，初值 None。
3. 删除 c3 的 `_stream` / `_execute` / `_run_readonly_concurrent` / `_run_one_serial`（逻辑已迁入 loop）。
4. `handle_input`：在 `/think` 之后新增 `/plan` 分支——`self._tools_enabled` 为真才切换 `self.plan_mode` 并返回「计划模式：开启/关闭」，否则返回「当前 Provider 不支持计划模式」；普通消息追加 history 后 `return self._run()`。
5. 实现 `_run(self) -> Iterator[AgentEvent]`：重建 `self._cancel_event`；`system_prompt = build_plan_prompt() if self.plan_mode else None`；定义内部 `confirm(tc, tool) -> bool`：若 `self._always_allow` 直接 True；否则调 `self.confirm_callback(tc, tool)` 得 `ConfirmDecision`，`ALLOW_ALWAYS` 时置 `self._always_allow=True` 返回 True，`ALLOW` 返回 True，`DENY` 返回 False；回调为 None 时 fail-closed 返回 False；`return self._agent.run(self.history, self.thinking_effort, self.plan_mode, system_prompt, confirm, self.clarify_callback, self.approve_plan_callback, self._cancel_event)`。
6. 新增 `request_cancel(self)`：`self._cancel_event.set()`。
7. 更新类/方法注释，移除「单轮往返」描述，改为「委托 Agent 跑 ReAct 循环」。

**验证：**
```
python -c "
from rhinecode.conversation import ConversationManager
class P: 
    def stream_chat(self,*a,**k): yield None
m=ConversationManager(P(), 'deepseek', object())
print(m.plan_mode); print(m.handle_input('/plan')); print(m.plan_mode)
m2=ConversationManager(P(), 'openai', None)
print(m2.handle_input('/plan'))
"
```
期望：`False` → `计划模式：开启` → `True`；openai 下返回「当前 Provider 不支持计划模式」。

---

## T9: 状态栏与面板组件

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T1
**步骤：**
1. `StatusBar.update_status`：增参 `plan_mode: bool = False`；显示串追加 `| 计划模式：开/关`。
2. `CommandPanel.COMMANDS`：追加 `("/plan", "切换计划模式：先规划/澄清需求，审批后再执行（DeepSeek）")`。
3. `ConfirmPanel.show_for`：在「执行」「取消」之间或之后加入第三项「执行且本会话不再询问」，三项 `option.id` 为 `yes`/`yes_always`/`no`；调整默认高亮仍为「执行」。
4. 新增 `ClarifyPanel(OptionList)`：`Cancelled` 内部消息类；`show_for(self, question, options: list[ClarifyOption])`——清空后加一个 disabled 表头（展示 question），再对每个 option 依次加「可选概述行（id=索引字符串）」+「disabled 详情行」，首个概述前缀「⭐ 推荐 」；`display=True`；默认高亮首个可选项。Esc 处理：参考 ConfirmPanel 发 `Cancelled`（具体键绑定在 app 层或本类 on_key）。
5. 充分中文注释，尤其「概述可选 + 详情 disabled 实现只在概述间导航」。

**验证：**
```
python -c "from rhinecode.tui.widgets import StatusBar, ClarifyPanel, ConfirmPanel; from rhinecode.agent.events import ClarifyOption; print(ClarifyPanel, ConfirmPanel, ClarifyOption('a','b'))"
```
期望：导入并实例化数据类无错误。

---

## T10: App 消费 AgentEvent 与三类交互回调

**文件：** `rhinecode/tui/app.py`
**依赖：** T8、T9
**步骤：**
1. 导入 `AgentEvent, AgentEventType, StopReason, ConfirmDecision, ClarifyOption` 与 `ClarifyPanel`。
2. `compose`：在 ConfirmPanel 之后挂载 `ClarifyPanel`。
3. `on_mount`：注入 `self._manager.confirm_callback=self._confirm_tool`、`clarify_callback=self._clarify`、`approve_plan_callback=self._approve_plan`。
4. 改造 `_do_stream(gen)`：按 `AgentEvent.type` 分发——TEXT/THINKING 沿用 c3 渲染（注意现在是 `event.text`）；TOOL_START/TOOL_RESULT 沿用 c3 工具行（`event.tool_call`/`event.tool_result`）；PROGRESS 第 2 轮起追加「第 N 轮」系统行；FINISHED 依 `stop_reason` 追加系统行（已达上限/已取消/计划未执行/连续未知工具停止/流错误等）；ERROR 红色行。`USAGE` 事件当前由 Agent 产出但 TUI 暂未展示。
5. `_confirm_tool` 返回 `ConfirmDecision`：复用 c3 阻塞模式，但读三选项 id 映射为枚举（yes→ALLOW, yes_always→ALLOW_ALWAYS, no→DENY）。
6. 新增 `_clarify(question, options) -> Optional[str]`：阻塞模式弹 `ClarifyPanel`，返回所选概述；Esc/取消返回 None。
7. 新增 `_approve_plan(plan) -> bool`：阻塞模式弹一个 Yes/No 面板（可复用 ConfirmPanel 或一个简单提示面板），展示计划摘要，返回是否批准。
8. 统一交互结算：把 `_pending_confirm` 泛化为 `_pending_interaction`（保存 event + 结果），三类回调共用 `_resolve_*`；`on_option_list_option_selected` 按来源面板（ConfirmPanel / ClarifyPanel）分别结算。
9. 取消键：`on_key` 中 `if self._stream_active and event.key=="escape": event.stop(); self._manager.request_cancel()`（不在交互面板等待时）。
10. `/plan`：`on_input_bar_input_submitted` 的 str 分支中，`text=="/plan"` 时调用 `_refresh_status()`。
11. `_refresh_status`：传入 `self._manager.plan_mode`。
12. 充分中文注释。

**验证：**
```
python -c "import rhinecode.tui.app"
```
期望：导入无错误（交互行为在端到端阶段验证）。

---

## T11: 入口装配核对与整体导入

**文件：** `rhinecode/__main__.py`
**依赖：** T10
**步骤：**
1. 核对 `__main__.py` 装配链（Agent 由 ConversationManager 内部构造，通常无需改动）；如注释提到「单轮往返」等过时描述则更新。
2. 整体导入自检。

**验证：**
```
python -c "import rhinecode.__main__, rhinecode.tui.app, rhinecode.conversation, rhinecode.agent.loop; print('ok')"
```
期望：`ok`。

---

## T12: 端到端冒烟（tmux 手动）

**文件：** 无（运行验证）
**依赖：** T11
**步骤：**
1. tmux 启动 `python -m rhinecode --config config.yaml`（DeepSeek 配置）。
2. 提一个需多步工具的任务（如「读取 README 并统计行数后告诉我」），观察：进度推进、工具行、最终自动回答。
3. 输入 `/plan` 开启，提一个含模糊点的需求，观察：只读调研、`ask_user` 澄清面板（概述+详情、推荐首位）、`present_plan` 完整计划入聊天记录并弹审批；批准后进入执行阶段，副作用工具仍逐个确认；拒绝后提示计划未执行并停止本轮。
4. 运行中按 Esc 测试取消。
5. 对照 `checklist.md` 逐项验收。

**验证：** 见 `checklist.md`。

---

## 执行顺序

```
T1 ─┬─ T2 ─ T3 ─┐
    │           ├─ T4 ─┐
    ├─ T5 ──────┤      │
    ├─ T6 ──────┘      ├─ T7 ─ T8 ─┐
    │                   │           ├─ T10 ─ T11 ─ T12
    └─ T9 ──────────────────────────┘
```
（T1 是公共基础；T2→T3 Provider 链；T4/T5/T6 可在 T1 后并行；T7 汇聚 T3/T4/T6；T8 接 T7；T9 接 T1；T10 汇聚 T8/T9；T11/T12 收尾。）
