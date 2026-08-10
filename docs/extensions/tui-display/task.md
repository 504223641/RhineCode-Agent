# TUI 显示体系改造 Tasks

> 对应 `spec.md`（F1–F42 / N1–N8 / AC1–AC32）与 `plan.md`。
> 共 **10 个阶段 / 44 个任务**。每个任务自带验证方式，**验证不过不往下走**。

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 改名 | `rhinecode/memory/notes.py` → `memories.py` | G 组 |
| 改名 | `rhinecode/memory/note_updater.py` → `memory_updater.py` | G 组 |
| 修改 | `rhinecode/memory/manager.py` | G 组标识符与文案 |
| 修改 | `rhinecode/trace/models.py` `trace/__init__.py` | G 组作用域取值 |
| 修改 | `rhinecode/tui/widgets.py` | 新增活动区/报告/三个纯函数；改工具行、四个面板、CSS |
| 修改 | `rhinecode/tui/app.py` | 接线、键位、分级方法、数字键、展开开关 |
| 修改 | `rhinecode/commands/models.py` | 控制器协议加两个方法 |
| 修改 | `rhinecode/commands/builtins.py` | 9 处报告调用点改通道 |
| 修改 | `rhinecode/subagents/tasks.py` | `TaskRecord` 两字段、`ActivityRow`、两个方法 |
| 修改 | `rhinecode/subagents/runner.py` | `TOOL_RESULT` 计数 |
| 修改 | `rhinecode/subagents/service.py` `rhinecode/conversation.py` | 活动快照通路 |
| 修改 | `rhinecode/agent/events.py` | `AgentEvent` 加 `level` |
| 修改 | `rhinecode/tools/base.py` + 各工具 | `primary_arg` 声明 |
| 修改 | `tests/test_tui_keybindings.py` | 护栏改写 |
| 新建 | `tests/test_tui_activity.py` | 活动区 |
| 新建 | `tests/test_tui_report.py` | 报告分级纯函数 |
| 新建 | `tests/test_tui_tool_title.py` | 工具行标题解析纯函数 |
| 新建 | `tests/test_tui_quit.py` | 双击退出与复制分流 |
| 新建 | `tests/test_tui_layout.py` | 顶部对齐与跟随最新 |
| 修改 | `CLAUDE.md`、`docs/c2/checklist.md`、`docs/c9/`、`docs/c11/testing/p0-trace/` | 文档同步 |

---

## P0 · 开工前的阻塞确认

### T1: 确认 trace 判据是否依赖「第 N 轮」那条 ui_message

**文件：** `tests/`、`tests/e2e/assertions.py`
**依赖：** 无
**步骤：**
1. 搜索 `第 {` / `第 %d 轮` / `轮` 在 `tests/` 与 `tests/e2e/` 中的断言用法；
2. 搜索 `PROGRESS` 相关的 e2e 判据；
3. 若有判据依赖它，记录下来并在 T20 一并改；若无，记「无依赖」。

**验证：** 产出一份明确结论（有/无），写进本任务的执行记录。**结论未得出前不做 T20。**

> ⚠ 上一行的「T20」是笔误，阻塞的是 **T15**（删「第 N 轮」）——T20 是活动区的
> `ActivityRow`，与本任务无关。执行时按 T15 理解。

**执行结论（2026-08-11）：无依赖，T15 可做。** 逐项证据：
>
> - `tests/` 与 `tests/e2e/` 中全部「第 N 轮」字样，均为**注释**或**测试自造的
>   剧本文本**，无一条判据去读界面上那一行。
> - `test_trace_reader.py::NonUtf8ConsoleTest` 出现 `"🔄 第 2 轮"`，但它是该用例
>   **自己写进 JSONL 的夹具字符串**（验 GBK 控制台能否读完一份含 emoji 的记录），
>   与产品是否产出这行无关。⚠ 该用例的 docstring 说「`ui_message` 正文天然带
>   emoji」，本轮之后不再成立，T15 顺手改写那段说明、**保留用例**
>   （trace 正文仍可能从别处带进 emoji，例如被读过的文件内容）。
> - `test_e2e_control.py` 的三条 `ui_message` 护栏分别过滤
>   `source == "assistant"` 或匹配「已更新记忆」；轮次行是另一条
>   `source="system"`，删掉不影响。其中
>   `test_no_duplicate_ui_message_when_tool_runs` 断言 assistant 正文**恰好两条**
>   ——轮次行本就不在这个计数里。
> - `tests/e2e/assertions.py::check_ui_contains` 的全部调用点只查
>   `"review"` / `"seed.txt"` / `"三个函数"` 等具体内容，无一条查轮次。
> - `test_e2e_host.py:378` 只断言 `ui_message` **这个类型存在**
>   （user_echo 与 AI 正文都会产出），不看轮次。

---

## P1 · G 组：「笔记 → 记忆」改名（独立、机械，先做完以免后续任务在两套命名间摇摆）

### T2: 重命名两个模块文件

**文件：** `rhinecode/memory/notes.py` → `memories.py`、`note_updater.py` → `memory_updater.py`
**依赖：** 无
**步骤：**
1. `git mv` 两个文件；
2. 全仓搜索 `memory.notes` / `memory.note_updater` / `from .notes` / `from .note_updater`，逐处改导入。

**验证：** `python -m compileall rhinecode tests` 通过。

### T3: 改名九组标识符

**文件：** `rhinecode/memory/*.py`、`rhinecode/conversation.py`、`tests/`
**依赖：** T2
**步骤：** 按 `spec.md` G 组表逐项改：`Note`→`Memory`、`NoteAction`→`MemoryAction`、
`notes_enabled`→`memories_enabled`、`_last_note_result`→`_last_memory_result`、
`_note_watermark`/`_note_inflight`→`_memory_*`、`parse_note`/`render_note`→`parse_memory`/`render_memory`、
`build_note_request`/`parse_note_response`→`build_memory_*`、`_update_notes`/`_count_notes`→`_update_memories`/`_count_memories`。

⚠ **一律用带词边界的精确匹配**（`\bnote\b` 之类），**不要裸替换** ——
裸替换会打中 `notetaker`（C11 验收夹具）。

**验证：** `python -m unittest discover -s tests` 全绿；
且 `grep -rn "notetaker" tests/e2e/align_scenarios.py` 仍能命中原名。

### T4: trace 作用域取值改为 `memory`

**文件：** `rhinecode/trace/models.py`、`trace/__init__.py`、`CLAUDE.md`、`docs/c11/testing/p0-trace/plan.md` 与 `task.md`
**依赖：** T3
**步骤：**
1. `SCOPE_NOTES = "notes"` → `SCOPE_MEMORY = "memory"`（常量名与取值同改）；
2. 改 `trace/__init__.py` 的导入与 `__all__`；
3. 改 `memory/manager.py` 的使用处；
4. 改 `CLAUDE.md` 里 `--scope` 的命令示例；
5. 改 p0-trace 两份文档的对应描述。

**验证：** `python -m unittest tests.test_trace_reader tests.test_trace_hooks` 全绿；
`grep -rn "SCOPE_NOTES" rhinecode/ tests/` 无结果。

### T5: 界面与发给模型的文案改口

**文件：** `rhinecode/memory/manager.py`、`memory/memory_updater.py`、`rhinecode/commands/builtins.py`
**依赖：** T4
**步骤：** 改 8 处：记忆索引表头（去掉「笔记全文位于」里的「笔记」）、
`上一轮笔记更新仍在进行`、`已更新 N 条笔记`、`笔记流出错`、`自动笔记：`、`无笔记`、
抽取请求里的两处字段说明、`/memory` 命令描述。

⚠ 「已更新记忆（N 条笔记）」这条通知**同时被 D 组（降为提示级）、F 组（去 emoji）、
G 组（换词）命中**——本任务只改词，级别与 emoji 在 T30 一次改到位，**不要在这里改**。

**验证：** `python -m unittest discover -s tests` 全绿。

### T6: 文档措辞同步

**文件：** `CLAUDE.md`、`docs/c9/*.md`、其余引用处
**依赖：** T5
**步骤：** 把 194 处「笔记」改成「记忆」，逐处确认语义不别扭（如「自动笔记」→「自动记忆」）。

**验证：** `grep -rn "笔记" rhinecode/ docs/ CLAUDE.md` 仅剩本扩展 spec 里
引用旧文案的「改造前」示例；`grep -rn "notetaker"` 仍在。

---

## P2 · 纯函数地基（零接线、可单测）

### T7: `classify_report` 纯函数

**文件：** `rhinecode/tui/widgets.py`
**依赖：** 无
**步骤：** 加 `ReportLineKind` 枚举与 `classify_report(text)`，判定规则见 `plan.md`
（首行 TITLE / 空行 BLANK / `•`-`-`-`·` 开头 ITEM / 缩进≥4 DETAIL / 其余 SECTION）。

**验证：** 新建 `tests/test_tui_report.py`，用 `/agents` 真实报告文本断言各行级别；
`python -m unittest tests.test_tui_report` 通过。

### T8: `resolve_call_title` 纯函数

**文件：** `rhinecode/tui/widgets.py`
**依赖：** 无
**步骤：** 按 `plan.md` 三条分支实现，返回 `(标签, 括号内文本)`，两者都经本模块 `escape`。

**验证：** 新建 `tests/test_tui_tool_title.py`，覆盖三条分支
+ **一条含未闭合 `[` 的路径**（AC22）；`python -m unittest tests.test_tui_tool_title` 通过。

### T9: `numbered_prompt` 纯函数

**文件：** `rhinecode/tui/widgets.py`
**依赖：** 无
**步骤：** 实现 `numbered_prompt(index, text, selected)` → `> 1. 文本` / `  2. 文本`。

**验证：** 并入 `tests/test_tui_report.py` 或新增用例，断言选中/未选中两种前缀与对齐。

### T10: 扩充 `_TOOL_LABELS`

**文件：** `rhinecode/tui/widgets.py`
**依赖：** 无
**步骤：** 按 spec B 组表加 `run_command→Bash`、`web_fetch→WebFetch`、
`send_message→SendMessage`、`load_skill→Skill`、`task_*→TaskCreate/TaskList/TaskGet/TaskUpdate`。
**不加** `mcp_*` 与 `ask_user`/`present_plan`。

**验证：** `python -m unittest tests.test_tui_tool_title` 通过（含「未登记回退原名」用例）。

---

## P3 · H 组：布局与体量

### T11: 顶部对齐（一行 CSS）

**文件：** `rhinecode/tui/app.py` 的 `CSS`
**依赖：** 无
**步骤：** `HistoryView > Vertical` 追加 `min-height: 100%;`。**不动 `anchor()`。**

**验证：** 新建 `tests/test_tui_layout.py`，用 `run_test` 量：
两条消息时 `scroll_offset.y == 0` 且内容容器 `region.y` 贴顶（AC29）；
挂满 40 条后 `scroll_offset.y == max_scroll_y` 且最后一条在视口内（AC30）。

### T12: 工具结果分支行支持多行 + 折叠

**文件：** `rhinecode/tui/widgets.py` 的 `ToolCallWidget`
**依赖：** 无
**步骤：**
1. `finish()` 接收的摘要允许多行；
2. 超过 `BRANCH_LINE_LIMIT`（5）时只画前 5 行 + `… +N 行（Ctrl+O 展开）`；
3. 组件保留完整文本，新增 `set_expanded(bool)` 重画。

**验证：** 单测：给 20 行摘要，折叠态只出现 5 行且含「+15 行」；
`set_expanded(True)` 后 20 行齐全。

### T13: diff 块行数上限

**文件：** `rhinecode/tui/widgets.py` 的 `_DiffBlock`
**依赖：** T12
**步骤：** 渲染时按 `DIFF_ROW_LIMIT` 截断，末行提示剩余行数与展开方式；
展开态画全。

**验证：** 单测：构造超限 `DiffView`，断言折叠态行数受限且有剩余提示。

### T14: `_summarize_result` 改为保留完整文本

**文件：** `rhinecode/tui/app.py`
**依赖：** T12
**步骤：** 不再只取首行截断到 80 字符，改为返回完整输出（截断交给 T12 的组件）。
⚠ 工具自带的 `summary` 仍优先。

**验证：** `python -m unittest tests.test_tui_tool_pending` 通过。

### T15: 删除「第 N 轮」提示行

**文件：** `rhinecode/tui/app.py` 的 `_do_stream`
**依赖：** T1（结论为「无依赖」或已同步改判据）
**步骤：** `PROGRESS` 分支删掉 `_trace_ui_message` 与 `append_system` 两行，
**保留 `reset_text_widgets()`**。

**验证：** `python -m unittest discover -s tests` 全绿；
一次多轮脚本化 e2e 跑完后 `screen` 里搜不到「第 」（AC32）。

---

## P4 · B 组：工具行

### T16: `Tool.primary_arg` 与各工具声明

**文件：** `rhinecode/tools/base.py` 及各工具模块
**依赖：** 无
**步骤：** 基类加 `primary_arg: str = ""`；按 spec 表给 11 个工具声明。

**验证：** `python -m unittest discover -s tests` 全绿；
写一条用例遍历注册中心，断言声明过的工具其 `primary_arg` 确实在参数 schema 里。

### T17: 工具行接入标题解析

**文件：** `rhinecode/tui/widgets.py`（`ToolCallWidget`、`HistoryView`）、`rhinecode/tui/app.py`
**依赖：** T8 T10 T16
**步骤：**
1. `ToolCallWidget.__init__` 接收 `primary_args` 映射（缺省空字典）；
2. 标题改由 `resolve_call_title` 产出；
3. `HistoryView.add_tool_widget` 与回放路径 `_build_tool_record_widget` 一并透传；
4. `app.on_mount` 从注册表建一次映射交给 `HistoryView`。

**验证：** `python -m unittest tests.test_tui_tool_pending tests.test_tui_markup_escape` 全绿；
脚本化 e2e 跑一次含 `read_file` 的场景，`screen` 里出现 `Read(` 且**不出现** `path=`（AC10）。

### T18: 终态耗时低于一秒不显示

**文件：** `rhinecode/tui/widgets.py` 的 `ToolCallWidget.finish`
**依赖：** T17
**步骤：** `elapsed >= 1` 才拼 `(Ns)`。执行中的 `_render_running` 不动。

**验证：** 单测：立即 `finish` 的行不含 `(0s)`；
mock 时钟推进 3 秒后 `finish` 的行含 `(3s)`（AC11）。

---

## P5 · A 组：活动区

### T19: `TaskRecord` 两字段 + `note_tool`

**文件：** `rhinecode/subagents/tasks.py`
**依赖：** 无
**步骤：** 加 `tool_calls: int = 0` 与 `recent_tools: tuple[str, ...] = ()`；
加 `TaskManager.note_tool(task_id, rendered)`，尾部追加并裁到 `ACTIVITY_RECENT_LIMIT`。
⚠ 临界区只做纯内存读写，不得调任何回调。

**验证：** `python -m unittest tests.test_subagent_tasks` 全绿（含既有的「无 callable 成员」结构护栏）。

### T20: `ActivityRow` 与 `activity_rows()`

**文件：** `rhinecode/subagents/tasks.py`
**依赖：** T19
**步骤：** 加 frozen dataclass `ActivityRow`；
`activity_rows()` 取「运行中」+「终态且 `settled_seconds < ACTIVITY_LINGER_SECONDS`」，
在此处完成「队员名(角色名)」口径拼接。

**验证：** 单测：三个任务（运行中/刚结束/结束很久），断言只返回前两个，
且名字口径与 `/agents` 一致。

### T21: 运行器计数

**文件：** `rhinecode/subagents/runner.py`
**依赖：** T19 T8
**步骤：** 事件循环加 `elif event.type is AgentEventType.TOOL_RESULT:` →
用 `resolve_call_title` 渲染短文本 → `tasks.note_tool(...)`。

**验证：** `python -m unittest tests.test_subagent_runner` 全绿；
新增用例：喂 3 个 `TOOL_RESULT`，断言 `tool_calls == 3` 且 `recent_tools` 有 3 条（AC3）。

### T22: 领域层通路

**文件：** `rhinecode/subagents/service.py`、`rhinecode/conversation.py`
**依赖：** T20
**步骤：** 服务层转发 `activity_rows()`；`conversation.subagent_activity()`
在未启用时返回空元组。

**验证：** 单测：未启用协作/子 Agent 时返回 `()`（AC1 的零回归半边）。

### T23: `ActivityView` 组件

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T20
**步骤：** 新增 `ActivityView(Vertical)`，`update_rows(rows, expanded)` 整块重绘；
无行时 `display = False`；每行按 F2 格式；展开时画 `⎿` 子行。
⚠ 名字与工具文本一律 `escape`。

**验证：** 新建 `tests/test_tui_activity.py`：空 → 不显示；两行 → 两行文本正确；
展开 → 出现子行；**名字含未闭合 `[` 时不抛异常**（AC22）。

### T24: 活动区接线与 CSS

**文件：** `rhinecode/tui/app.py`
**依赖：** T22 T23
**步骤：**
1. `compose()` 在 `HistoryView` 之后、`CommandPanel` 之前 `yield ActivityView()`；
2. CSS 加 `ActivityView`（顶部分隔线、`display: none` 缺省、`height: auto`）；
3. `_poll_subagents()` 末尾调 `_refresh_activity()`。

**验证：** 脚本化 e2e：无委派时 `screen --selector ActivityView` 为空；
发起委派后出现活动行（AC1）。

### T25: 完成通知带成本（F6 历史留痕 = F7 通知，同一行）

**文件：** `rhinecode/tui/app.py` 的 `_poll_subagents`
**依赖：** T24
**步骤：** 把现有通知文案改为
`<名字> <状态> (<次数> 次调用 · <token> · <耗时>)` + 「结论将在下一轮…」。
⚠ **不要再另写一行历史留痕**。

**验证：** e2e：一次委派跑完后历史区**恰好一条**该任务的完成行且含三个数字（AC6/AC7）。

### T26: 会话切换清空活动区

**文件：** `rhinecode/tui/app.py`
**依赖：** T24
**步骤：** `clear_conversation()` 与恢复路径调用 `ActivityView.update_rows(())`。

**验证：** e2e：委派后 `/clear`，活动区为空（AC8）。

---

## P6 · C 组：报告

### T27: 控制器协议加 `show_report`

**文件：** `rhinecode/commands/models.py`
**依赖：** 无
**步骤：** `CommandController` 协议加 `show_report(text: str) -> None`。

**验证：** `python -m compileall rhinecode` 通过。

### T28: 9 处报告调用点改通道

**文件：** `rhinecode/commands/builtins.py`
**依赖：** T27
**步骤：** `/mcp` `/hooks` `/tasks` `/agents` `/context` `/memory` `/skills`
`/skills prompt` `/help` 共 9 处由 `show_message` 改为 `show_report`。
其余 `show_message` 一处不动。

**验证：** `python -m unittest tests.test_command_builtins` 全绿（需同步桩控制器）。

### T29: 报告渲染接线

**文件：** `rhinecode/tui/widgets.py`（`HistoryView.append_report`）、`rhinecode/tui/app.py`
**依赖：** T7 T28
**步骤：** `append_report` 用 `classify_report` 按级别拼 markup；
`app.show_report` 埋点 `source="system"` 后调它。

**验证：** e2e：跑 `/agents`，`screen --selector "#history-messages"` 的 markup 里
出现分级样式（AC13）；报告产出函数的既有单测全绿（证明「一字未改」）。

---

## P7 · D 组：分级

### T30: 新增 `show_event` 通道

**文件：** `rhinecode/tui/widgets.py`、`rhinecode/tui/app.py`、`rhinecode/commands/models.py`
**依赖：** 无
**步骤：** `HistoryView.append_event`（正常亮度、无前缀）；
`app.show_event`；协议加 `show_event`。
同时把 `append_warning`/`append_error` 的前缀改为「警告：」「错误：」，
去掉 `append_error` 的 `●`。

**验证：** 单测断言四条通道的 markup 各不相同，且警告/错误含文字前缀（AC15）。

### T31: `AgentEvent` 加 `level`

**文件：** `rhinecode/agent/events.py`、`rhinecode/trace/models.py`
**依赖：** T30
**步骤：** `AgentEvent` 加 `level: str = "notice"`；
`agent_event_payload` 白名单加 `level`。

**验证：** `python -m unittest tests.test_trace_reader` 全绿。

### T32: 逐个调用点指派级别

**文件：** `rhinecode/tui/app.py`、`rhinecode/conversation.py`、`rhinecode/agent/loop.py`
**依赖：** T31
**步骤：** 按下表逐处指派，**不留兜底**：

| 调用点 | 级别 |
| --- | --- |
| 记忆已更新（`_notify_memory`） | 提示 |
| 上下文已压缩（`conversation.py` 的 NOTICE） | 提示 |
| Skill 激活/卸载反馈 | 提示 |
| 子 Agent 完成通知（`_poll_subagents`） | **事件** |
| 子 Agent 结论送达（`conversation.py:1402` 的 NOTICE） | **事件** |
| 自动唤起（`_maybe_auto_wake`） | **事件** |
| 会话恢复成功 | **事件** |
| 协作降级通知（`team_drain_notices`） | **事件** |
| 启动提示 `startup_notice` | 提示 |
| 项目级 Hook 提示 | 警告（现状即此） |
| Esc 后仍有子 Agent 在跑 | **警告** |
| 达迭代上限 / 未知工具 / 流错误（`_finish_line`） | **警告** |
| 已取消 / 计划未执行 | **事件** |
| API 错误 | 错误（现状即此） |

**验证：** e2e：一次含子 Agent 与记忆更新的场景，两条消息的 markup 不同（AC14）；
全量测试全绿。

---

## P8 · E 组：面板

### T33: 三个面板的选项编号

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T9
**步骤：** `ConfirmPanel.show_for` / `show_prompt`、`ClarifyPanel.show_for`、
`SessionPanel.show_for` 对**可选项**递增编号；表头与详情行不占号。

**验证：** 单测：断言可选项文本以 `1.` `2.` 开头，disabled 行不带号。

### T34: 高亮指示符跟随

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T33
**步骤：** 三个面板实现 `watch_highlighted`，用 `replace_option_prompt_at_index`
只改 `> ` / `  ` 前缀。

**验证：** 单测：改 `highlighted` 后，新旧两行的前缀互换（AC17）。

### T35: 数字键选中

**文件：** `rhinecode/tui/app.py` 的 `on_key`
**依赖：** T33
**步骤：** 在既有「交互待决 → return」守卫**之前**加一段：面板挂起且键为 `1`–`9`
时映射到第 N 个可选项并走既有结算路径。

**验证：** e2e：确认面板弹出后 `keys 2`，结算为「本会话放行」（AC16）；
`keys escape`、上下键、回车行为不变（AC18）。

### T36: 面板去 emoji 与表头统一

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T33
**步骤：** 去掉四个面板里的 `✅ 🟢 💾 ❌ ❓ 📂 📋 🔒`；
`🔒` 改 `[锁定]`、`（当前）` 改 `[当前]`；表头结构统一。

**验证：** 单测：面板选项文本里搜不到表情；锁定项含 `[锁定]`（AC19）。

---

## P9 · F 组：去表情与退出键位

### T37: 其余位置去 emoji

**文件：** `rhinecode/tui/app.py`、`rhinecode/tui/widgets.py`、`rhinecode/memory/manager.py`、
`rhinecode/context/manager.py`、`rhinecode/context/offload.py`、`rhinecode/mcp/manager.py`、
`rhinecode/team/render.py`、`rhinecode/tools/team_tasks.py`
**依赖：** T36
**步骤：** 按 spec F 组表逐处替换（`💭`→`✻`、`⏹`/`⟳`/`🔄` 去掉、
`⛔` 去掉、`🧠 📦 🗜 📊 ✓ ✗` 改纯文字、`⚠`→「警告：」）。

**验证：** 全仓扫描脚本断言 `rhinecode/` 的**用户可见字符串**里无表情（AC19）；
全量测试全绿。

### T38: 键位绑定与双击退出

**文件：** `rhinecode/tui/app.py`
**依赖：** 无
**步骤：**
1. 加 `BINDINGS`：`ctrl+q → noop`（priority）、`ctrl+c → request_quit`（priority）、
   `ctrl+o → toggle_expand`；
2. `action_request_quit` 按 `plan.md` 三条分支实现；
   **选中文本判定同时查 `screen.get_selected_text()` 与焦点组件的 `selected_text`**，
   有选中则复制且**不计数**；
3. `QUIT_CONFIRM_SECONDS = 2.0`；
4. 占位符改为「…连按两次 Ctrl+C 退出」。

**验证：** 新建 `tests/test_tui_quit.py`：
单次 `ctrl+c` 后应用仍在运行（AC21，三种状态各验一次）；
两次连按退出（AC20）；超时后需重新按两次；
**有选中文本时按两次仍不退出**；`ctrl+q` 不退出。

### T39: 全局展开开关

**文件：** `rhinecode/tui/app.py`
**依赖：** T12 T23 T38
**步骤：** `_expanded: bool`；`action_toggle_expand` 切换后广播给
`ActivityView` 与当前挂着的工具行。

**验证：** e2e：`keys ctrl+o` 后活动区出现子行、超限工具行展开；
再按一次收回；**输入框焦点不丢**（AC5）。

### T40: 护栏改写

**文件：** `tests/test_tui_keybindings.py`、`docs/c2/checklist.md`
**依赖：** T38
**步骤：**
1. `test_ctrl_c_is_not_bound_to_quit` → `test_single_ctrl_c_never_quits`，
   断言**单次按下后应用仍在运行**（保留原意图，改判据形态）；
2. `test_placeholder_points_to_ctrl_q_for_quit` → 断言占位符写「连按两次 Ctrl+C 退出」；
3. 同步 `docs/c2/checklist.md` 的 AC9 文案，并注明由本扩展改写。

**验证：** `python -m unittest tests.test_tui_keybindings` 全绿。

---

## P10 · 收尾

### T41: 全量测试

**依赖：** T1–T40
**步骤：** `python -m compileall rhinecode tests` + `python -m unittest discover -s tests`。
**验证：** 全绿（skipped 仍为 4）。

### T42: 端到端跑一遍

**依赖：** T41
**步骤：** 起脚本化宿主，依次验：委派→活动区出现→Ctrl+O 展开→完成留痕→
`/agents` 报告分级→确认面板数字键→两次 Ctrl+C 退出。
**验证：** 逐条对照 `checklist.md`。

### T43: 文档同步

**文件：** `CLAUDE.md`、`docs/extensions/README.md`
**依赖：** T42
**步骤：** `CLAUDE.md` 的 TUI 层描述、成对维护点（新增「活动区 ↔ 完成通知同源」
「符号白名单」两条）、`docs/extensions/README.md` 的扩展表加一行。
**验证：** 人工通读。

### T44: 删除 todo

**文件：** `docs/todo/5-tui-display-overhaul.md`、`docs/todo/README.md`
**依赖：** T43
**步骤：** 删文件并从 README 索引摘除。
**验证：** `grep -rn "tui-display-overhaul" docs/todo/` 无结果。

---

## 执行顺序

```
T1（阻塞确认）
 └→ P1 改名  T2 → T3 → T4 → T5 → T6
     └→ P2 纯函数  T7、T8、T9、T10（彼此独立，可并行）
         ├→ P3 布局  T11、T12 → T13、T14、T15
         ├→ P4 工具行  T16 → T17 → T18
         ├→ P5 活动区  T19 → T20 → T21、T22 → T23 → T24 → T25、T26
         ├→ P6 报告  T27 → T28 → T29
         ├→ P7 分级  T30 → T31 → T32
         ├→ P8 面板  T33 → T34、T35、T36
         └→ P9 键位  T37、T38 → T39、T40
                                    └→ P10  T41 → T42 → T43 → T44
```

⚠ **T15 卡在 T1 的结论上**，T1 未出结论前不做。
⚠ **T25 与 T5 同碰一条文案**，顺序不能反（先改词、后改级别与格式）。
