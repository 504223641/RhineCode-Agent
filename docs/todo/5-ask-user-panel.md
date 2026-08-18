# 澄清提问面板：从「Plan Mode 专属」变成「随时可用」

> 状态：待开工 · **建议走完整 `/spec`** · 预计 2–3 天
>
> 建议分支：`ask-user-panel`（从 `main` 起）
>
> ⚠️ **这不是从零做一个新面板——面板与工具都已经在了，只是被关在 Plan Mode 里。**
> 真正的工作量分两半：**放开可用范围**（小）与**把面板补齐到 Claude Code 的形态**
> （多选 / 自由输入 / 多问题，大）。开工前必须先量一遍现状，别照着「新建一个功能」
> 的思路开工。

## 用户要的是什么

> 我希望当模型需要向用户确认细节时**总是**通过这个面板来获取用户的选择。目的是方便
> 用户，对于选择不用手动打一大堆文字，而是通过预定义的选项来进行选择，提高用户体验。
> 面板的实现可以参考 Claude Code 的澄清提问面板。

拆成两条可验收的诉求：

1. **覆盖面**：不再只有 Plan Mode 规划阶段能用。任何时候模型遇到「有好几种都说得通
   的做法、猜错了要返工」时，都该弹面板，而不是在正文里写一段问句然后停下来等用户打字。
2. **表达力**：面板要能承载 Claude Code 那套结构（选项带说明、可多选、可自己输入），
   否则模型会因为「面板表达不了我要问的东西」而退回用正文提问——那样第 1 条等于没做。

## 现状盘点（**开工前必读，它决定了工作量的形状**）

本项目**已经有**这套东西的一半，是 C4 Plan Mode 时做的：

| 件 | 位置 | 现在的样子 |
| --- | --- | --- |
| 工具 | `agent/plan_tools.py` 的 `AskUserTool`（`ask_user`） | 单问题 + 若干选项，每项 `summary` / `detail`，约定第一个是最推荐项 |
| 面板 | `tui/widgets.py:3262` 的 `ClarifyPanel` | 单选。表头是问题，每个选项一行可选的 `summary` + 一行 disabled 的 `detail`（导航自动跳过、不占序号） |
| 回调链 | `conversation.py` 的 `clarify_callback` → `tui/app.py` 的 `_clarify` | 阻塞式，与确认面板同款交互（方向键 / 数字键直选 / Esc 取消） |
| 驱动设施 | `tests/e2e/control.py` 的 `answer <序号>` | 已支持 `clarify` 类面板，但**明确拒绝 `--via keys`**（详情行让「按几次方向键」的推导不稳） |

**三条已经量过的事实，能省掉一整轮试错**：

- ⚠ **循环里的拦截路由与阶段无关。** `agent/loop.py:1331` 是
  `if tc.name in (ASK_USER, PRESENT_PLAN): special.append(tc)`，**无条件分流**。
  也就是说「非 Plan 模式下模型调 `ask_user` 会走不通」这件事**不成立**——
  唯一的门槛在 `loop.py:909`：`plan_schemas()` 只在 `planning` 为真时拼接，
  于是**模型平时压根看不到这个工具的 schema**。放开覆盖面的核心改动可能只有这一处。
- ⚠ **子 Agent 天然拿不到它。** `subagents/runner.py:771` 传的是 `clarify=None`
  （注释写着「问不了人」），`_run_special` 里有 `clarify is None` 的兜底。这与
  Claude Code 官方登记的限制（*`AskUserQuestion` is not currently available in
  subagents*）恰好一致，**不是巧合而是同一个理由**：子 Agent 全程非交互、用户不在场。
  这条要在 spec 里写成不变量并加护栏，别让将来有人「顺手补上」。
- ⚠ **它不进 `ToolRegistry`，因此不过权限管线。** 现在是合理的（它无副作用，且
  「向用户提问」本身就是人在回路）。但覆盖面放开之后要重新论证一次，并写进
  `CLAUDE.md` 的安全边界——理由不再是「只在规划阶段可用」了。

## Claude Code 的形态（官方文档口径，2026-08-15 查证）

来源：[Handle approvals and user input](https://code.claude.com/docs/en/agent-sdk/user-input)

| 字段 | 约束 | 我们现在有没有 |
| --- | --- | --- |
| 每次调用的问题数 | **1–4 个** | ❌ 只有 1 个 |
| 每题选项数 | **2–4 个** | ⚠ 无约束（模型爱给几个给几个） |
| `header` | 短标签，**最多 12 字符**，渲染成 chip | ❌ 没有 |
| `options[].label` / `description` | 选项名 + 一句「选了会怎样」 | ✅ 对应 `summary` / `detail` |
| `multiSelect` | 为真时可多选 | ❌ 只能单选 |
| 「Other」自由输入 | **客户端补的**，不是模型给的；用户打的原文直接作为答案 | ❌ 没有 |
| 答案回传 | `{问题原文: 选中的 label}`，多选传数组或 `", "` 拼接 | ⚠ 只回传单个 `summary` |
| 子 agent 中可用 | **否**（官方 Limitations） | ✅ 已满足 |

另有一条机制上的对照，对理解本项目的现有实现有帮助：Claude Code 那边
`AskUserQuestion` 与权限确认**走同一条 `canUseTool` 回调**，官方原话是
「both trigger your `canUseTool` callback, **which pauses execution until you
return a response**」。本项目的 `clarify_callback` 与 `ask_callback` 正是同构的
两条阻塞回调——**这个结构不用改**。

⚠ **官方的 `preview` / `previewFormat`（给选项配 ASCII 或 HTML 小样）建议不做**，
理由见「不做的事」。

## 要决的事（**都得在 spec 阶段定死，别留到实现期**）

### 1. ⚠ 最要紧的一条：怎么让模型「总是」用它，而**不掉进过触发**

这是整件事里唯一有真实前车之鉴的坑，而且这个坑本项目**已经踩过一次**。

`CLAUDE.md` 已知项 #17 记着：C13 委派的触发口径先是欠触发（模型全程自己做、
0 次委派），于是补了四条推力（「命中就委派 / 拿不准就委派 / 不要先看一眼再决定」），
结果**同一个模型**被推到了另一个极端——用户实测反馈「一个非常简单的任务都要让子
Agent 去做」。根因写在那条 todo 里：那些推力是**单向**的，只写了该做的理由，
一句「什么时候不该」都没有。

**本条会正面撞上同一个形态**：写「需要用户拍板时一律用面板」，模型就会开始为
「用哪个变量名」这种事弹面板，而那比让它自己决定更烦人。

因此 spec 里必须同时写下**两侧**，而且下限要**可数**（已知项 #17 留下的线索：
模型对有具体可匹配项的指令遵循得好，对抽象判断系统性偷懒）：

- **该用**：有 2–4 种都说得通的做法、选错要返工、且用户的偏好无法从代码或
  `RHINE.md` 推出来。
- **不该用**：答案能从代码里查出来（那就去查）；只有一种合理做法（那就做）；
  用户已经说过了（翻对话）；纯风格问题（跟着周围代码走）。

⚠ **别只改工具 description**。它至少有两个出口要同口径——这是 `CLAUDE.md`
「成对维护点」里那个坑的第五次（C11 Skill 清单 ↔ `load_skill.description`、
C13 角色清单 ↔ `run_agent.description`、C14 交付信息 ↔ 委派工具描述、
C15 发消息工具 ↔ 注入标记块，前四次全是真实模型实测才发现的）：

- `agent/plan_tools.py` 的 `AskUserTool.description`（模型决定要不要调它时读）
- 系统提示里的相应段落（`agent/prompt/texts/task_mode.py` 现在有一条
  「区分两种提问」，与本条直接相关，**必须一起改，否则两处打架**）

护栏形态照抄 `test_subagent_tool.py::SameVoiceTest`：断言两处都包含那几层意思，
**并带反证**。

### 2. 一次一问，还是一次多问（1–4）

| | 一次一问（现状） | 一次多问 |
| --- | --- | --- |
| 面板 | 现成 | 要么串行弹 N 次，要么重做成多段面板 |
| 模型侧 | 要连问三件事得占三轮迭代（三次 API 往返） | 一轮问完 |
| 用户侧 | 一次只看一个决定，认知负担小 | 一屏看完全貌，但也更容易乱点 |

⚠ **倾向：schema 按数组定义（对齐官方 1–4），但实现先做「串行弹 N 次」。**
理由是面板复用现有的、改动小，而**接口一旦定成单问题，将来扩成多问题就是破坏性
变更**（模型侧的调用格式变了）。这个取舍要在 spec 里写明，别让后来的人以为是没做完。

### 3. 多选（`multiSelect`）怎么做

现有 `ClarifyPanel` 继承 `NumberedPanel`（底层是 Textual 的 `OptionList`），
天生单选：高亮 + 回车 = 选中并结算。

多选要加一套状态：空格切换勾选、回车提交、至少选一项。**这是本条里最容易做出
交互不一致的一块**——同一个面板在两种模式下按键含义不同（单选时回车 = 选这一项，
多选时回车 = 提交全部勾选），用户分不清自己在哪种模式里。

⚠ 建议：多选时在表头明写「空格勾选 · 回车提交」，且勾选状态用**行首标记**表达。
新符号要先进 `CLAUDE.md` 的符号白名单（九个符号那张表），并同步
`tests/test_tui_symbols.py` 的 `WHITELIST`——**漏了会当场红**，那是刻意的。

### 4. ⚠「Other」自由输入怎么落地（**最麻烦的一块**）

官方明确说这一项**是客户端补的**，不是模型生成的：

> Display an additional "Other" choice after Claude's options that accepts text
> input. Use the user's custom text as the answer value (not the word "Other").

也就是说这是**我们这边的责任**，而本项目的面板里没有输入框。两条路：

- **方案 A：选「其它」→ 收面板 → 焦点回主输入框，用户打的下一条消息作为答案。**
  改动小，复用现有输入框（含历史、粘贴、补全）。代价是那条消息要**特殊路由**
  ——它不能走正常的「用户发新消息」路径，否则会被当成新一轮对话（而模型这一轮
  还阻塞在回调里等答案）。
- **方案 B：面板内嵌一个 `Input`。** 交互连贯，但要给面板加子组件。
  ⚠ **动手前先读 `CLAUDE.md` 的那条**：新增 TUI 组件字段名必须先在 `Static`
  实例上 `hasattr` 查一遍（tui-activity-fold 一轮撞了三个 Textual 内部字段，
  `_render` / `_closed` / `_running`，**一律不报错**，只表现为「界面上东西凭空少了」）。

⚠ **倾向 A，但 A 的那条特殊路由必须在 spec 里画出状态图**——「面板收了、回调还
阻塞着、用户正在打字」这个中间态是本项目此前没有过的形态，`Esc` 按下去该发生什么、
这时候按 `Ctrl+C` 两下退出会不会留下一个永远等不到答案的 Worker，都要有答案。

### 5. 取消（`Esc`）的语义要重新定

现在：`loop.py:1229` 把「澄清面板被用户取消」当作**循环的停止条件**——整轮结束。
那在 Plan Mode 里说得通（用户不想规划了）。

覆盖面放开之后，一次普通任务跑到一半弹面板、用户按 Esc，整轮直接结束**多半不是
用户的本意**（他可能只是想说「这个你自己定」）。

⚠ 倾向：**非规划阶段的取消回灌一句「用户没有选择，请按你的最佳判断继续，
或直接说明你需要什么」并让循环继续**；规划阶段维持现状。两种行为要分开写、
分开加护栏，否则一次改动会静默改掉 Plan Mode 的既有语义。

### 6. 这算「扩展」还是「章节」

判据只有一条（`docs/extensions/README.md`）：`CLAUDE.md` 的能力表要不要多一行。

⚠ 倾向 **扩展**（`docs/extensions/ask-user/`）：没有新增能力层级，改的是一个既有
工具的可用范围与一个既有面板的表达力。但如果 spec 阶段发现方案 B 或多问题面板
把交互层撑成了新结构，改判为章节也合理——**在 spec 第一节就写下判定与理由**，
别做到一半再挪目录。

## 接线清单（**这些漏了都不报错**）

- **`tests/e2e/control.py`**：`_settle` 的 clarify 分支现在算的是
  `app._clarify_options[idx].summary`（**刻意读私有属性**，为的是与产品侧走同一份
  计算）。多选与自由输入会让「值 = 单个 summary」这条式子不成立，
  `tests/e2e/protocol.py` 的取值判定（`clarify` 走正则「纯数字」）也要跟着改。
  ⚠ **不改这里的后果是「新面板没法无头验收」**，而本项目所有 TUI 行为的判据都建立
  在驱动设施上。
- **`tui/app.py` 的 `_NOTIFY_KINDS`**：`_interact` 新增交互种类要登记（成对维护点，
  漏改不报错，只是 Hook 的 `notification` 条件匹配不上用户按文档写的值）。
- **新增确认/交互态的四处**：`agent/events.py` 枚举 + `tui/widgets.py` 面板选项 id
  + `tui/app.py` 的 id→枚举映射 + `conversation.py` 的回调闭包。
- **trace**：新事件类型要同步 `trace/models.py` 枚举与 `trace/reader.py` 的
  `SUMMARIZERS`（漏后者只显示成「（未登记类型）」）。
- **符号白名单**：见上文第 3 条。
- **`CLAUDE.md`**：安全边界要补一句——`ask_user` 不进权限管线的理由变了（见现状
  盘点第三条）。

## 不做的事（建议写进 spec）

- **选项预览**（官方的 `preview` / `previewFormat`，给选项配 ASCII 或 HTML 小样）。
  终端里 HTML 无意义，ASCII 小样又会把面板撑得很高，而本项目底部已经四层了
  （活动区 / 状态行 / 输入框 / 状态栏）。
- **用户主动召唤面板**（「我想让你问我几个问题」）。那是第二条入口，与 C15
  共享清单「命令层不提供写路径」同理。
- **把权限确认面板也统一成这套结构**。两者只是长得像：确认面板的选项是**固定四个**
  且带安全语义（会写规则、会落盘），澄清面板的选项是模型现编的。合并会让
  「本会话放行 / 永久放行」这类带副作用的选项与普通选项混在同一套渲染里。
- **跨会话记住用户的选择**（「上次你选了 A，这次还选 A 吗」）。
- **子 Agent 里可用**（见现状盘点第二条，这是不变量不是缺口）。

## 开工 Prompt

```
先看 `git branch --show-current`，如果在 main 上就 `git checkout -b ask-user-panel`。

读 `docs/todo/6-ask-user-panel.md`，然后走 `/spec` 做「澄清提问面板：从 Plan Mode
专属变成随时可用」。

⚠ 开工前先自己量一遍现状，别照着「新建一个功能」的思路做——面板与工具都已经在了
（`agent/plan_tools.py` 的 AskUserTool、`tui/widgets.py:3262` 的 ClarifyPanel），
而且 `agent/loop.py:1331` 的拦截路由是无条件的，唯一的门槛在 loop.py:909
（`plan_schemas()` 只在规划阶段拼接）。

⚠ 六件事必须在 spec 阶段定下来：

1. **怎么让模型「总是」用它而不过触发。** 这是本条唯一有前车之鉴的坑：
   先读 `CLAUDE.md` 已知项 #17（C13 委派口径从欠触发被推到过触发的全过程），
   本条会撞上同一个形态。两侧都要写，且下限要可数。改动至少涉及两处文本
   （工具 description + `agent/prompt/texts/task_mode.py` 里「区分两种提问」那条），
   必须同口径，护栏照抄 `test_subagent_tool.py::SameVoiceTest`。

2. **一次一问还是一次多问（官方是 1–4）。** 倾向「schema 按数组定义、实现先串行
   弹 N 次」——接口定成单问题的话，将来扩成多问题是破坏性变更。

3. **多选怎么做。** 现有面板是 OptionList 天生单选。同一面板两种模式下回车含义
   不同，是最容易做出交互不一致的地方。新符号要先进 `CLAUDE.md` 的符号白名单
   与 `tests/test_tui_symbols.py` 的 WHITELIST。

4. **「Other」自由输入怎么落地。** 官方明确这一项是客户端补的。两条路（收面板回
   主输入框 / 面板内嵌 Input）都要评估，并把「面板收了、回调还阻塞着、用户正在
   打字」这个中间态画成状态图——Esc 与两下 Ctrl+C 在那个态下的行为要有答案。

5. **Esc 的语义。** 现在取消 = 整轮循环停止（`loop.py:1229`），那只在 Plan Mode
   里说得通。倾向非规划阶段改成「回灌一句话并继续」，两种行为分开加护栏。

6. **算扩展还是章节。** 判据是 `CLAUDE.md` 能力表要不要多一行，倾向扩展
   （`docs/extensions/ask-user/`），在 spec 第一节写下判定与理由。

⚠ 别忘了驱动设施：`tests/e2e/control.py` 的 `_settle` clarify 分支现在算的是
`app._clarify_options[idx].summary`，多选与自由输入会让这条式子不成立，
`protocol.py` 的取值判定也要跟着改。不改的话新面板没法无头验收。

做完 spec 先给我确认再往下走。
```

## 相关

- `rhinecode/agent/plan_tools.py` —— 现有 `ask_user` 工具（本条的主要改造对象）
- `rhinecode/tui/widgets.py:3262` 的 `ClarifyPanel` —— 现有面板
- `rhinecode/agent/loop.py:909` / `:1331` / `:1229` —— schema 拼接点 / 拦截路由 / 取消即停止
- `rhinecode/agent/prompt/texts/task_mode.py` —— 「区分两种提问」那条，必须同口径改
- `rhinecode/subagents/runner.py:771` —— 子 Agent 传 `clarify=None`（不变量的落点）
- `tests/e2e/control.py` / `protocol.py` —— 驱动设施里的 clarify 应答路径
- `CLAUDE.md` 已知项 #17 —— 触发口径「欠触发 → 过触发」的完整前车之鉴
- [Claude Code 官方：Handle approvals and user input](https://code.claude.com/docs/en/agent-sdk/user-input)
- [Claude Code 官方：permission modes（plan 模式与审批流）](https://code.claude.com/docs/en/permission-modes)
