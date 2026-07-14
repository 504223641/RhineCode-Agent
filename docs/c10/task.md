# C10 斜杠命令注册与分发 Tasks

> 状态：已批准（2026-07-14，含修订）
> 输入：已批准的 `docs/c10/spec.md` 与 `docs/c10/plan.md`
> 粒度：每项约 2–5 分钟；每项完成后先执行其验证，再进入依赖它的任务

## 1. 文件清单

### 1.1 新建

| 文件 | 职责 |
|---|---|
| `rhinecode/commands/__init__.py` | 导出稳定命令 API，不产生导入副作用 |
| `rhinecode/commands/models.py` | 枚举、不可变数据类、处理函数类型和 `CommandController` 协议 |
| `rhinecode/commands/parser.py` | 空输入、普通消息、斜杠输入分类与首空白切分 |
| `rhinecode/commands/registry.py` | 原子注册、冲突校验、名称解析、补全和帮助文本 |
| `rhinecode/commands/dispatcher.py` | 输入分流、单次回显、命令执行和本地错误边界 |
| `rhinecode/commands/builtins.py` | 12 条规范内置命令、确认的别名和静态 `/init` 提示词处理 |
| `tests/test_command_parser.py` | 解析器边界与大小写测试 |
| `tests/test_command_registry.py` | 冲突、原子性、隐藏项、帮助和补全测试 |
| `tests/test_command_dispatcher.py` | 普通消息、命令、未知命令和异常分发测试 |
| `tests/test_command_builtins.py` | 内置命令元数据与 Fake Controller 行为测试 |
| `tests/test_command_startup.py` | 启动早期注册、冲突退出和资源未创建测试 |
| `tests/test_command_tui.py` | 高亮、Tab、候选菜单、Enter 和模式状态测试 |

### 1.2 修改

| 文件 | 改动 |
|---|---|
| `rhinecode/provider/base.py` | 给 `Message` 增加可选 `display_content` |
| `rhinecode/memory/session.py` | JSONL 兼容读写显示内容，会话标题优先使用显示内容 |
| `rhinecode/conversation.py` | 把 `handle_input` 拆成普通消息、模式、报告、压缩和恢复领域方法 |
| `rhinecode/tui/widgets.py` | 动态命令面板、命令高亮、Tab 请求、双内容回放和状态模式标签 |
| `rhinecode/tui/app.py` | 实现控制器、接入分发器、统一消费 Manager 结果与恢复面板路径 |
| `rhinecode/__main__.py` | Provider/MCP 前构建命令注册表并注入 App |
| `tests/test_memory_session.py` | `display_content` JSONL、旧档和标题回归 |
| `tests/test_resume_replay.py` | 回放显示内容与新恢复领域入口 |
| `tests/test_review_fixes.py` | 普通消息测试迁移，并验证显示元数据不进入三个 Provider 的请求负载 |
| `tests/test_tui_keybindings.py` | 更新旧静态命令表与提交路径的结构断言 |
| `README.md` | 更新命令、别名、补全和模式标记说明 |
| `CLAUDE.md` | 更新架构与新增命令维护方式 |
| `AGENTS.md` | 更新 C10 能力和命令单一注册规则，保留用户已有无关改动 |

### 1.3 明确不改

- `rhinecode/provider/deepseek.py`
- `rhinecode/provider/openai.py`
- `rhinecode/provider/anthropic.py`
- `rhinecode/agent/loop.py`
- Permission、Context、MCP、Memory 的核心编排实现
- Tool 注册与权限判断机制

## 2. 命令层基础

## T1：定义命令核心模型

**文件：** `rhinecode/commands/models.py`
**依赖：** 无

**步骤：**

1. 定义 `CommandType`、`InputKind`、`DispatchKind`、`ModeTarget`、`ReportTarget`。
2. 定义冻结的 `CommandSpec`、`ParsedInput`、`CommandInvocation`、`CompletionItem`、`DispatchResult`。
3. 按 Plan 固定字段顺序和默认值，名称与别名保存完整斜杠。
4. 用前向引用定义 `CommandHandler`，避免导入 TUI 或 Conversation。

**验证：** 运行 `python -m py_compile rhinecode/commands/models.py`，期望退出码 0。

## T2：定义框架无关控制接口

**文件：** `rhinecode/commands/models.py`
**依赖：** T1

**步骤：**

1. 定义 `CommandController(Protocol)`。
2. 加入 `tools_enabled`、显示用户输入、显示本地消息、发送用户消息、切换模式、查询报告、刷新状态、清空、压缩、恢复和退出接口。
3. 将 `send_user_message` 的 `display_content` 设为可选参数。
4. 确保协议签名不引用 Textual widget、App 或 Provider SDK 类型。

**验证：** 运行 `python -m py_compile rhinecode/commands/models.py`，并期望 `rg -n "textual|RhineApp|ConversationManager" rhinecode/commands/models.py` 无匹配。

## T3：实现纯输入解析器

**文件：** `rhinecode/commands/parser.py`
**依赖：** T1

**步骤：**

1. 实现 `parse_input(text) -> ParsedInput`。
2. 空串和纯空白返回 `InputKind.EMPTY`。
3. 两端去空白后首字符不是 `/` 时返回 `InputKind.MESSAGE`。
4. 斜杠输入只按第一个空白切分命令字段和参数。
5. 保留 `raw_text` 与命令字段原始大小写；只去掉参数两端空白，不改变参数内部内容。

**验证：** 在 `python -m py_compile rhinecode/commands/parser.py` 后运行一个最小导入，期望 `parse_input("/PLAN  Keep Case").arguments == "Keep Case"`。

## T4：覆盖解析器基本分类

**文件：** `tests/test_command_parser.py`
**依赖：** T3

**步骤：**

1. 添加空串、纯空白、普通消息和首位斜杠用例。
2. 验证正文中的 `/plan` 不被识别为命令。
3. 验证 `/PLAN` 的 `command_token` 保留用户大小写。
4. 验证 `/resume ABC-123` 正确切出参数。

**验证：** 运行 `python -m unittest tests.test_command_parser`，期望全部通过。

## T5：覆盖解析器空白与参数边界

**文件：** `tests/test_command_parser.py`
**依赖：** T4

**步骤：**

1. 添加空格、Tab 和换行作为首次分隔符的用例。
2. 验证参数外层空白被去除。
3. 验证参数内部连续空白、引号、斜杠和大小写保持原样。
4. 验证只有 `/` 的输入仍是斜杠输入，由注册表决定是否未知。

**验证：** 运行 `python -m unittest tests.test_command_parser`，期望全部通过。

## T6：实现注册与冲突校验

**文件：** `rhinecode/commands/registry.py`
**依赖：** T1、T2

**步骤：**

1. 定义 `CommandRegistrationError` 和 `CommandRegistry`。
2. 校验规范名与别名都以 `/` 开头且不含空白。
3. 对索引键统一调用 `casefold()`。
4. 实现 `register` 和原子的 `register_many`。
5. 覆盖名称/名称、名称/别名、别名/别名、同一命令重复别名及大小写冲突。
6. 冲突信息包含冲突标识与双方规范命令。

**验证：** 运行 `python -m py_compile rhinecode/commands/registry.py`，期望退出码 0。

## T7：实现名称解析与可见命令查询

**文件：** `rhinecode/commands/registry.py`
**依赖：** T6

**步骤：**

1. 实现 `resolve(name_or_alias)`，规范名与别名大小写不敏感。
2. 保留注册顺序。
3. 实现 `visible_commands()`，过滤 `hidden=True` 的命令。
4. 保留隐藏命令的直接解析能力。

**验证：** 运行最小导入脚本注册 `/context` 与 `/ctx`，期望 `resolve("/CTX").name == "/context"`。

## T8：实现补全与帮助文本

**文件：** `rhinecode/commands/registry.py`
**依赖：** T7

**步骤：**

1. 实现 `complete(prefix)`，同时匹配可见规范名和别名。
2. 候选使用注册顺序，每条命令先规范名、后声明顺序中的别名。
3. 为别名候选设置 `is_alias=True` 与 `canonical_name`。
4. 实现 `render_help()`，展示命令、别名、简短描述、用法和可选参数提示。
5. 确保隐藏命令及其全部别名不出现在补全和帮助中。

**验证：** 运行 `python -m py_compile rhinecode/commands/registry.py`，期望退出码 0。

## T9：覆盖注册冲突与原子性

**文件：** `tests/test_command_registry.py`
**依赖：** T6

**步骤：**

1. 分别测试名称/名称、名称/别名和别名/别名冲突。
2. 测试 `/Help` 与 `/help` 的大小写冲突。
3. 测试同一 `CommandSpec` 内重复别名。
4. 测试批量注册后项冲突时，前项没有写入正式注册表。
5. 断言异常消息含冲突标识和双方规范名。

**验证：** 运行 `python -m unittest tests.test_command_registry`，期望冲突用例全部通过。

## T10：覆盖解析、补全、隐藏项与帮助

**文件：** `tests/test_command_registry.py`
**依赖：** T8、T9

**步骤：**

1. 测试规范名和别名的大小写不敏感解析。
2. 测试候选稳定顺序和别名标记。
3. 测试隐藏命令可直接解析。
4. 测试隐藏命令及别名不出现在 `visible_commands`、`complete` 和帮助中。
5. 测试帮助文本含描述、用法与参数提示。

**验证：** 运行 `python -m unittest tests.test_command_registry`，期望全部通过。

## T11：实现普通消息与未知命令分流

**文件：** `rhinecode/commands/dispatcher.py`
**依赖：** T3、T7

**步骤：**

1. 定义持有 `CommandRegistry` 的 `CommandDispatcher`。
2. 空输入返回 `DispatchKind.EMPTY` 且不调用控制器。
3. 普通消息依次调用一次 `show_user_input` 和一次 `send_user_message`。
4. 未知斜杠输入回显一次原文，显示“未知命令 + `/help` 引导”。
5. 未知命令返回 `DispatchKind.UNKNOWN`，绝不调用 `send_user_message`。

**验证：** 运行 `python -m py_compile rhinecode/commands/dispatcher.py`，期望退出码 0。

## T12：实现已知命令执行与错误边界

**文件：** `rhinecode/commands/dispatcher.py`
**依赖：** T11

**步骤：**

1. 命中规范名或别名后构造完整 `CommandInvocation`。
2. 命令执行前只回显一次用户原始输入。
3. `requires_argument=True` 且参数为空时，本地显示用法并返回 `ERROR`。
4. 调用处理函数，成功返回 `COMMAND` 与规范命令名。
5. 捕获命令处理异常，记录调试上下文并显示简洁本地错误。
6. 异常路径不降级成普通消息，也不重复回显。

**验证：** 运行 `python -m py_compile rhinecode/commands/dispatcher.py`，期望退出码 0。

## T13：覆盖普通消息、空输入与未知命令

**文件：** `tests/test_command_dispatcher.py`
**依赖：** T11

**步骤：**

1. 建立记录方法调用的 Fake Controller。
2. 验证空输入没有任何控制器调用。
3. 验证普通消息按“回显 → 发送”顺序各调用一次。
4. 验证未知斜杠输入显示 `/help` 引导。
5. 断言未知命令没有调用 `send_user_message`。

**验证：** 运行 `python -m unittest tests.test_command_dispatcher`，期望本组用例全部通过。

## T14：覆盖别名、参数校验与异常

**文件：** `tests/test_command_dispatcher.py`
**依赖：** T12、T13

**步骤：**

1. 测试规范名、别名和大写别名命中同一个 `CommandSpec`。
2. 断言 `typed_name`、`matched_name`、`spec.name` 和参数值正确。
3. 测试必需参数缺失时显示用法且不调用处理函数。
4. 测试处理函数异常只显示一次本地错误。
5. 测试处理函数异常与未知命令都不会进入 Agent。

**验证：** 运行 `python -m unittest tests.test_command_dispatcher`，期望全部通过。

## 3. 内置命令

## T15：实现帮助与只读报告处理函数

**文件：** `rhinecode/commands/builtins.py`
**依赖：** T2、T8

**步骤：**

1. 实现捕获同一注册表实例的 `/help` 处理函数。
2. 实现 `/mcp`、`/context`、`/memory` 报告处理函数。
3. 报告函数调用 `query_report` 后用 `show_message` 展示。
4. 处理函数不导入 Textual、ConversationManager 或具体 Manager 类型。

**验证：** 运行 `python -m py_compile rhinecode/commands/builtins.py`，期望退出码 0。

## T16：实现模式处理函数

**文件：** `rhinecode/commands/builtins.py`
**依赖：** T15

**步骤：**

1. 实现 `/think`、`/plan`、`/perm` 处理函数。
2. 分别调用 `switch_mode(THINKING|PLAN|PERMISSION)`。
3. 把返回文本交给 `show_message`。
4. 三个处理函数都调用 `refresh_status`，由控制器决定实际显示字段。
5. 不在处理函数中复制 Provider 支持判断。

**验证：** 运行 `python -m py_compile rhinecode/commands/builtins.py`，期望退出码 0。

## T17：实现压缩、恢复、清空与退出处理函数

**文件：** `rhinecode/commands/builtins.py`
**依赖：** T16

**步骤：**

1. `/compact` 调用 `compact_context()`，不创建用户消息。
2. `/resume` 把空参数转换为 `None`，非空参数原样传给 `resume_session(key)`。
3. `/clear` 调用 `clear_conversation()`，显示兼容确认文本并刷新状态。
4. `/exit` 调用 `exit_application()`，不抛 `SystemExit` 穿过命令层。
5. 所有无参命令忽略 `CommandInvocation.arguments` 中的额外内容。

**验证：** 运行 `python -m py_compile rhinecode/commands/builtins.py`，期望退出码 0。

## T18：实现静态提示词命令

**文件：** `rhinecode/commands/builtins.py`
**依赖：** T17

**步骤：**

1. 将现有 `INIT_PROMPT` 明确迁入 `rhinecode/commands/builtins.py` 作为静态内置常量，不能从 `conversation.py` 反向导入，也不能在运行时动态生成。
2. `/init` 先检查 `controller.tools_enabled`。
3. 不支持时显示现有兼容提示且不发送消息。
4. 支持时调用 `send_user_message(INIT_PROMPT, display_content=invocation.raw_text)`。
5. 不在处理函数里再次调用 `show_user_input`。

**验证：** 运行 `python -m py_compile rhinecode/commands/builtins.py`，期望退出码 0。

## T19：建立完整内置注册表

**文件：** `rhinecode/commands/builtins.py`
**依赖：** T15、T16、T17、T18

**步骤：**

1. 实现无导入副作用的 `build_builtin_registry()`。
2. 登记 `/help`、当前 11 条命令及 Plan 确认的全部别名。
3. 为每条命令填写描述、用法、`CommandType`、参数提示和处理函数。
4. 仅 `/resume` 填 `[编号或ID]` 参数提示；C10 全部内置命令 `requires_argument=False`。
5. 通过 `register_many` 一次性完成原子注册。

**验证：** 运行最小导入脚本构建注册表，期望 `visible_commands()` 有 12 条规范命令且 `resolve("/QUIT").name == "/exit"`。

## T20：覆盖内置命令元数据

**文件：** `tests/test_command_builtins.py`
**依赖：** T19

**步骤：**

1. 断言 12 条规范名完整且没有多余命令。
2. 断言 `/quit`、`/continue`、`/permissions`、`/allowed-tools`、`/ctx`、`/h`、`/reset`、`/new` 映射正确。
3. 断言三类命令分类与批准的表格一致。
4. 断言每条命令的描述和用法非空。
5. 断言 `/resume` 参数提示和所有 `requires_argument=False`。

**验证：** 运行 `python -m unittest tests.test_command_builtins`，期望元数据用例全部通过。

## T21：覆盖内置命令控制器行为

**文件：** `tests/test_command_builtins.py`
**依赖：** T19、T20

**步骤：**

1. 用 Fake Controller 执行帮助、三种报告和三种模式命令。
2. 验证模式命令显示结果并刷新状态。
3. 验证 `/clear extra` 仍清空，`/compact extra` 仍压缩。
4. 验证 `/resume` 与 `/continue ABC` 分别传入 `None` 与 `ABC`。
5. 验证 `/exit` 只调用退出接口。
6. 验证帮助内容来自注册表且包含别名。

**验证：** 运行 `python -m unittest tests.test_command_builtins`，期望本地与 UI 命令行为全部通过。

## T22：覆盖 `/init` 双内容行为

**文件：** `tests/test_command_builtins.py`
**依赖：** T18、T21

**步骤：**

1. 工具关闭时执行 `/init`，断言只显示不支持提示。
2. 工具开启时执行 `/init`，断言发送完整静态提示词。
3. 断言 `display_content` 保留用户实际输入，例如 `/INIT`。
4. 断言命令处理函数没有重复回显原始命令。

**验证：** 运行 `python -m unittest tests.test_command_builtins`，期望全部通过。

## T23：导出稳定命令 API

**文件：** `rhinecode/commands/__init__.py`
**依赖：** T3、T8、T12、T19

**步骤：**

1. 导出控制器协议、核心枚举和数据类。
2. 导出 `CommandRegistry`、`CommandRegistrationError`、`CommandDispatcher`、`parse_input` 与 `build_builtin_registry`。
3. 不创建模块级注册表实例。
4. 不在导入时调用 `register` 或 `build_builtin_registry`。

**验证：** 运行 `python -c "import rhinecode.commands as c; print(c.CommandDispatcher.__name__)"`，期望输出 `CommandDispatcher` 且无其它副作用输出。

## 4. 提示词双内容与会话兼容

## T24：扩展 Message 显示字段

**文件：** `rhinecode/provider/base.py`
**依赖：** 无

**步骤：**

1. 在 `Message` 数据类末尾增加 `display_content: str | None = None`。
2. 保持现有字段顺序，避免破坏已有位置参数调用。
3. 不修改 `BaseProvider` 或 `StreamChunk`。
4. 不修改三个具体 Provider。

**验证：** 运行 `python -m py_compile rhinecode/provider/base.py`，再构造不带与带 `display_content` 的 `Message`，期望二者都成功。

## T25：持久化可选显示内容

**文件：** `rhinecode/memory/session.py`
**依赖：** T24

**步骤：**

1. `_serialize` 仅在 `display_content is not None` 时写入 JSONL 字段。
2. `_deserialize_line` 读取字符串类型的 `display_content`。
3. 缺失、`null` 或非法类型时回退 `None`。
4. 保持未知字段忽略与坏行容错逻辑不变。

**验证：** 运行 `python -m py_compile rhinecode/memory/session.py`，期望退出码 0。

## T26：会话标题优先使用显示内容

**文件：** `rhinecode/memory/session.py`
**依赖：** T25

**步骤：**

1. `_scan_one` 读取首条用户行时优先采用非空 `display_content`。
2. 显示内容缺失或为空时回退 `content`。
3. 保留换行替换、标题长度截断和坏行跳过逻辑。
4. 不改变消息计数与最后时间计算。

**验证：** 运行 `python -m py_compile rhinecode/memory/session.py`，期望退出码 0。

## T27：覆盖 JSONL 新旧格式与标题

**文件：** `tests/test_memory_session.py`
**依赖：** T25、T26

**步骤：**

1. 添加带 `display_content="/init"` 的追加与载入往返测试。
2. 断言 JSONL 实际同时保存完整 `content` 与 `display_content`。
3. 添加旧 JSONL 无显示字段的兼容载入测试。
4. 添加非法显示字段回退 `None` 的测试。
5. 添加会话标题优先显示 `/init`、普通消息仍用 `content` 的测试。

**验证：** 运行 `python -m unittest tests.test_memory_session`，期望全部通过。

## T28：历史回放优先显示命令原文

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T24

**步骤：**

1. 修改 `build_replay_items` 的用户消息分支。
2. 非空 `display_content` 优先产出 `("user", display_content)`。
3. 显示字段缺失或为空时回退原 `content`。
4. 保持 assistant、tool 配对和截断行为不变。

**验证：** 运行 `python -m py_compile rhinecode/tui/widgets.py`，期望退出码 0。

## T29：覆盖双内容历史回放

**文件：** `tests/test_resume_replay.py`
**依赖：** T28

**步骤：**

1. 添加 `content=完整提示词、display_content="/init"` 的用户消息。
2. 断言回放项只展示 `/init`。
3. 添加旧消息与空显示字段回退完整内容的测试。
4. 保留并运行工具调用回放、空 assistant 和未知 role 回归。

**验证：** 运行 `python -m unittest tests.test_resume_replay.ReplayBuilderTests`，期望全部通过。

## 5. ConversationManager 领域入口

## T30：新增普通用户消息入口

**文件：** `rhinecode/conversation.py`
**依赖：** T24

**步骤：**

1. 新增 `submit_user_message(content, display_content=None)`。
2. 构造 `Message(role="user", content=content, display_content=display_content)`。
3. 按现有顺序追加 `history`、调用 `memory_manager.record_message`、返回 `_run()`。
4. 此方法不解析斜杠、不做界面回显。
5. 暂时保留 `handle_input`，直到 App 与旧测试全部迁移。

**验证：** 运行 `python -m py_compile rhinecode/conversation.py`，期望退出码 0。

## T31：拆出模式切换方法

**文件：** `rhinecode/conversation.py`
**依赖：** T30

**步骤：**

1. 新增 `cycle_thinking()`，迁移现有 Provider 支持判断、循环和文本。
2. 新增 `toggle_plan()`，迁移工具能力判断、布尔切换和文本。
3. 新增 `cycle_permission()`，迁移权限模式循环和文本。
4. 每个方法只维护领域状态，不直接刷新 TUI。
5. 临时让旧 `handle_input` 分支调用这些方法，避免迁移期间行为分叉。

**验证：** 运行 `python -m unittest tests.test_review_fixes tests.test_perm_engine`，期望全部通过。

## T32：拆出只读报告方法

**文件：** `rhinecode/conversation.py`
**依赖：** T31

**步骤：**

1. 新增 `mcp_report()` 并迁移未配置与状态报告行为。
2. 新增 `context_report()` 并迁移 Provider/ContextManager 支持判断。
3. 新增 `memory_report()` 并委托现有 MemoryManager。
4. 暴露只读 `tools_enabled` property。
5. 临时让旧 `handle_input` 对应分支调用新方法。

**验证：** 运行 `python -m py_compile rhinecode/conversation.py`，期望退出码 0；报告行为在 T21 与最终目标测试中覆盖。

## T33：公开压缩与恢复领域方法

**文件：** `rhinecode/conversation.py`
**依赖：** T32

**步骤：**

1. 新增 `manual_compact()`，保留支持判断并在可用时返回现有 `_manual_compact()` 事件流。
2. 新增 `resume(key=None)`，无参构造 `SessionListRequest`，有参返回 `_resume_stream(key)`。
3. 保留无会话、只有当前会话、全部锁定时的原提示文本。
4. 让旧 `handle_input` 的 `/compact` 与 `/resume` 分支临时委托新方法。
5. 让 `clear()` 返回兼容确认文本，副作用保持不变。

**验证：** 运行 `python -m py_compile rhinecode/conversation.py`，期望退出码 0。

## T34：迁移普通消息并覆盖 Provider 显示元数据边界

**文件：** `tests/test_review_fixes.py`
**依赖：** T24、T30、T31、T32

**步骤：**

1. 把只用于普通自然语言输入的 `manager.handle_input("...")` 改为 `manager.submit_user_message("...")`。
2. 保留 AgentEvent、权限决策、计划审批和停止原因断言。
3. 新增一次 `display_content` 透传断言，确认历史中的模型内容与显示内容分离。
4. 为 DeepSeek、OpenAI、Anthropic 分别使用 SDK 客户端替身捕获请求消息；构造带 `display_content` 的 `Message` 后，断言模型负载只包含各协议支持字段，不包含 `display_content`。
5. 不修改三个具体 Provider 的生产实现，也不把命令分发测试继续放在此旧回归文件中。

**验证：** 运行 `python -m unittest tests.test_review_fixes`，期望全部通过。

## T35：迁移会话恢复领域测试

**文件：** `tests/test_resume_replay.py`
**依赖：** T33、T29

**步骤：**

1. 将 `handle_input("/resume")` 改为 `resume(None)`。
2. 将 `handle_input("/resume ID")` 改为 `resume("ID")`。
3. 保持无存档、不可选、成功 HISTORY、失败不清屏和压缩通知断言。
4. 增加别名不在 Manager 测试的说明；`/continue` 由命令层测试覆盖。

**验证：** 运行 `python -m unittest tests.test_resume_replay`，期望全部通过。

## 6. TUI 组件

## T36：实现命令字段高亮器

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T7

**步骤：**

1. 新增持有 `CommandRegistry` 的 `CommandHighlighter`。
2. 只检查输入开头到第一个空白之前的字段。
3. 仅当完整字段可由注册表解析时，为该字段施加与普通文本不同的命令样式。
4. 不改变参数、前后空白或用户输入大小写。
5. 正文内斜杠与未完成前缀不着色。

**验证：** 运行 `python -m py_compile rhinecode/tui/widgets.py`，期望退出码 0。

## T37：让命令面板读取注册表候选

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T8

**步骤：**

1. 删除 `CommandPanel.COMMANDS` 静态列表。
2. 构造函数接收同一个 `CommandRegistry`。
3. `show_for` 使用 `registry.complete(prefix)` 重建 Option。
4. Option id 使用候选 `value`，正文显示描述；别名额外标注其规范命令。
5. 参数区输入和零候选时隐藏面板。
6. 每次重建保持首个可选项高亮，供 Enter 执行当前候选。

**验证：** 运行 `python -m py_compile rhinecode/tui/widgets.py`，期望退出码 0。

## T38：给 InputBar 注入高亮器

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T36

**步骤：**

1. 扩展 `InputBar` 构造函数接收 `CommandRegistry`。
2. 通过 Textual `Input` 的公开 `highlighter` 参数安装 `CommandHighlighter`。
3. 保持现有 `InputSubmitted` 消息和空输入过滤。
4. 不覆写 Textual 的私有渲染方法。

**验证：** 运行 `python -m py_compile rhinecode/tui/widgets.py`，期望退出码 0；构造与高亮行为在 T41 覆盖。

## T39：实现命令 Tab 请求

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T38

**步骤：**

1. 定义携带命令前缀的 `CommandCompletionRequested` 消息。
2. InputBar 仅在值以 `/` 开头、光标仍处于命令字段时拦截 Tab。
3. 命中条件时停止按键传播并 post 补全请求。
4. 已进入参数区或普通文本时不拦截 Tab，保留 Textual 正常行为。
5. 提供替换命令字段并把光标移到末尾的窄方法，供 App 单候选补全调用。

**验证：** 运行 `python -m py_compile rhinecode/tui/widgets.py`，期望退出码 0。

## T40：更新状态栏模式标记

**文件：** `rhinecode/tui/widgets.py`
**依赖：** 无

**步骤：**

1. 删除“计划模式：开/关”文本。
2. 默认状态渲染字面 `[DEFAULT]`，使用弱化主题色。
3. Plan Mode 渲染字面 `[PLAN]`，使用加粗的醒目蓝色或青色主题色。
4. 正确转义字面左方括号，避免被 Textual markup 解释为标签。
5. 保留 Provider、模型、思考、权限、MCP 和上下文段的现有顺序与样式。

**验证：** 运行 `python -m py_compile rhinecode/tui/widgets.py`，期望退出码 0；可见文本与样式差异在 T41 覆盖。

## T41：覆盖高亮、候选和状态纯逻辑

**文件：** `tests/test_command_tui.py`
**依赖：** T36、T37、T38、T40

**步骤：**

1. 测试完整规范名、完整别名和大写命令字段着色。
2. 测试部分前缀、正文内斜杠和参数不着色。
3. 测试命令面板读取规范名与别名，并过滤隐藏项。
4. 测试别名候选显示规范命令说明。
5. 测试 `[DEFAULT]` 与 `[PLAN]` 可见且样式不同。
6. 测试状态栏其它现有字段仍保留。

**验证：** 运行 `python -m unittest tests.test_command_tui`，期望本组纯逻辑用例全部通过。

## 7. RhineApp 控制器与交互接线

## T42：向 App 注入命令注册表

**文件：** `rhinecode/tui/app.py`
**依赖：** T23、T37、T38

**步骤：**

1. 扩展 `RhineApp.__init__` 接收 `CommandRegistry`。
2. 保存注册表并构造单个 `CommandDispatcher`。
3. `compose` 用同一注册表构造 `CommandPanel` 与 `InputBar`。
4. 更新构造函数文档，不在 App 内重新创建注册表。

**验证：** 运行 `python -m py_compile rhinecode/tui/app.py`，期望退出码 0。

## T43：实现显示、模式与报告控制器方法

**文件：** `rhinecode/tui/app.py`
**依赖：** T31、T32、T42

**步骤：**

1. 实现 `tools_enabled` property，委托 Manager。
2. 实现 `show_user_input` 与 `show_message`，分别调用 HistoryView 的用户和系统追加方法。
3. 实现 `switch_mode`，按 `ModeTarget` 调用三个领域方法并返回文本。
4. 实现 `query_report`，按 `ReportTarget` 调用三个报告方法。
5. 实现 `refresh_status`，复用现有 `_refresh_status`。
6. 未知枚举值采用明确异常，不静默选择默认分支。

**验证：** 运行 `python -m py_compile rhinecode/tui/app.py`，期望退出码 0。

## T44：统一消费 Manager 返回值

**文件：** `rhinecode/tui/app.py`
**依赖：** T33、T43

**步骤：**

1. 抽取 `_consume_manager_result(result)`。
2. `str` 交给 `show_message`。
3. `SessionListRequest` 交给现有会话选择面板。
4. 事件迭代器设置 streaming 状态，并在下一帧启动现有 Worker。
5. 保持同一时间只允许一个流式 Worker。
6. 让 `send_user_message` 调 `manager.submit_user_message` 后进入该统一入口。

**验证：** 运行 `python -m py_compile rhinecode/tui/app.py`，期望退出码 0；三类返回值路径在 T49–T50 的 Fake Manager 集成测试中覆盖。

## T45：实现清空、压缩、恢复与退出控制器方法

**文件：** `rhinecode/tui/app.py`
**依赖：** T33、T44

**步骤：**

1. `clear_conversation` 调用 Manager `clear()` 并清空 HistoryView。
2. `compact_context` 调用 Manager `manual_compact()` 并统一消费结果。
3. `resume_session(key)` 调用 Manager `resume(key)` 并统一消费结果。
4. `exit_application` 调用 Textual App 的 `exit()`。
5. 这些方法不解析或拼接斜杠命令字符串。

**验证：** 运行 `python -m py_compile rhinecode/tui/app.py`，期望退出码 0；清屏、事件消费、恢复参数和退出行为在 T48–T50 覆盖。

## T46：提交入口改走 CommandDispatcher

**文件：** `rhinecode/tui/app.py`
**依赖：** T12、T44、T45

**步骤：**

1. 保留交互待决、流式运行和会话面板展示时的提交守卫。
2. 若命令面板有高亮项，先用候选 value 替换待提交文本。
3. 隐藏命令面板后只调用 `dispatcher.dispatch(text, self)`。
4. 删除提交入口提前 `append_user` 的逻辑，由分发器统一回显。
5. 删除 `SystemExit` 捕获、字符串命令判断和状态刷新白名单。
6. 不在 App 中再区分 `/clear`、`/plan` 等具体命令名。

**验证：** 运行 `python -m py_compile rhinecode/tui/app.py`，并期望提交方法正文不再引用 `handle_input`。

## T47：接入动态候选与 Tab 补全

**文件：** `rhinecode/tui/app.py`
**依赖：** T37、T39、T46

**步骤：**

1. 输入变化时只把开头命令字段交给 `CommandPanel.show_for`。
2. 参数区、普通文本、流式运行和交互面板状态继续隐藏或忽略命令菜单。
3. 处理 `InputBar.CommandCompletionRequested`。
4. 单候选时替换命令字段；若候选规范命令有参数提示，在末尾保留一个空格。
5. 多候选时显示候选菜单并保持 InputBar 焦点，方向键继续移动面板高亮。
6. Enter 在菜单可见且有高亮项时提交该候选，即使原输入只是部分命令。

**验证：** 运行 `python -m py_compile rhinecode/tui/app.py`，期望退出码 0。

## T48：会话面板直接复用恢复控制器

**文件：** `rhinecode/tui/app.py`
**依赖：** T45

**步骤：**

1. SessionPanel 选中后调用 `resume_session(session_id)`。
2. 删除拼接 `"/resume {session_id}"` 后重新进入命令解析的路径。
3. 保持选中后先关闭面板、恢复 InputBar 可用状态。
4. 保持恢复可能触发压缩时走后台 Worker。
5. 不在聊天区回显用户没有手输的隐藏恢复命令。

**验证：** 运行 `python -m py_compile rhinecode/tui/app.py`，期望退出码 0；完整 session id 与无伪造命令行为在 T51 覆盖。

## T49：覆盖单候选、多候选与参数区 Tab

**文件：** `tests/test_command_tui.py`
**依赖：** T39、T42、T47

**步骤：**

1. 用 Textual `run_test()` / Pilot 输入 `/compa` 并按 Tab。
2. 断言输入框变为 `/compact`。
3. 输入 `/cont` 并按 Tab，断言菜单同时出现 `/context` 与 `/continue`。
4. 输入 `/resume 12` 并按 Tab，断言参数文本未被改写。
5. 注册隐藏测试命令，断言它不出现在候选菜单。

**验证：** 运行 `python -m unittest tests.test_command_tui`，期望补全交互用例全部通过。

## T50：覆盖 Enter 执行、未知命令与模式状态

**文件：** `tests/test_command_tui.py`
**依赖：** T46、T47、T49

**步骤：**

1. 输入部分命令，显示多候选菜单后用方向键选择并按 Enter。
2. 断言执行当前高亮候选，而不是把部分前缀发给 Agent。
3. 输入未知斜杠命令，断言显示 `/help` 引导且 Fake Manager 没收到普通消息。
4. 执行 `/plan`，断言 `[DEFAULT]` 与 `[PLAN]` 互换且颜色或样式不同。
5. 执行 `/init`，断言界面只回显原命令一次，Manager 收到完整提示词和显示内容。

**验证：** 运行 `python -m unittest tests.test_command_tui`，期望全部通过。

## T51：更新旧 TUI 结构回归测试

**文件：** `tests/test_tui_keybindings.py`
**依赖：** T46、T47、T48

**步骤：**

1. 删除对 `CommandPanel.COMMANDS` 的旧断言。
2. 更新提交入口结构断言为 `CommandDispatcher` 单入口。
3. 断言旧命令字符串状态刷新白名单已删除。
4. 断言运行中 Esc 取消、确认面板和会话面板优先级仍存在。
5. 断言 SessionPanel 选中路径调用 `resume_session`，不拼接斜杠文本。

**验证：** 运行 `python -m unittest tests.test_tui_keybindings`，期望全部通过。

## T52：删除旧 handle_input 命令路由

**文件：** `rhinecode/conversation.py`、`tests/test_review_fixes.py`、`tests/test_resume_replay.py`
**依赖：** T34、T35、T46、T48、T51

**步骤：**

1. 删除 `ConversationManager.handle_input`。
2. 删除已迁入 `builtins.py` 的命令字符串分支和未使用常量/import。
3. 确认普通消息只从 `submit_user_message` 进入。
4. 确认压缩、恢复、模式和报告只通过领域方法暴露。
5. 清理测试文档字符串与注释中的旧入口描述。

**验证：** 运行 `rg -n "handle_input|text\.startswith\(\"/\"\)" rhinecode tests`，期望无运行时代码或测试调用匹配；再运行 `python -m unittest tests.test_review_fixes tests.test_resume_replay`。

## 8. 启动接线

## T53：在昂贵资源前构建命令注册表

**文件：** `rhinecode/__main__.py`
**依赖：** T23、T42

**步骤：**

1. 在配置加载和占位 API Key 校验后调用 `build_builtin_registry()`。
2. 此调用必须位于 `create_provider`、`ToolRegistry.default`、`MCPManager` 和 `connect_all` 之前。
3. 捕获 `CommandRegistrationError`，向 stderr 输出可定位错误并以退出码 1 结束。
4. 将命令注册表注入 `RhineApp`。
5. 把工具注册表局部变量改为 `tool_registry` 等明确名称，避免与命令注册表混淆。
6. 保持现有 finally 中 Memory 锁和 MCP 资源回收逻辑不变。

**验证：** 运行 `python -m py_compile rhinecode/__main__.py`，期望退出码 0。

## T54：覆盖启动成功与冲突失败

**文件：** `tests/test_command_startup.py`
**依赖：** T53

**步骤：**

1. 测试正常构建时同一命令注册表实例注入 App。
2. 将 `build_builtin_registry` 替换为抛 `CommandRegistrationError` 的假实现。
3. 断言入口以退出码 1 结束并输出冲突标识。
4. 断言冲突时 `create_provider` 未调用。
5. 断言冲突时 ToolRegistry、MCPManager、MCP 子进程和会话锁均未创建。

**验证：** 运行 `python -m unittest tests.test_command_startup`，期望全部通过。

## 9. 文档与最终回归

## T55：更新用户命令说明

**文件：** `README.md`
**依赖：** T52、T54

**步骤：**

1. 更新运行时命令表，加入 `/help`、全部别名、大小写不敏感和未知命令行为。
2. 说明单匹配 Tab、多匹配菜单、Enter 执行高亮项和隐藏命令规则。
3. 说明命令字段高亮、`[DEFAULT]` / `[PLAN]` 差异。

**验证：** 人工核对 README 命令表与 `build_builtin_registry()` 的规范名、别名和类型一致。

## T56：更新开发架构说明

**文件：** `CLAUDE.md`
**依赖：** T55

**步骤：**

1. 增加 `rhinecode/commands/` 的模块职责与依赖方向。
2. 说明 ConversationManager 不再解析斜杠，RhineApp 实现控制接口。
3. 把旧的双维护规则改为“登记一个 `CommandSpec` + 处理函数 + 测试”。

**验证：** 运行 `rg -n "CommandPanel\.COMMANDS|handle_input.*斜杠|新增斜杠命令.*成对" CLAUDE.md`，期望旧架构规则无匹配。

## T57：更新仓库协作说明

**文件：** `AGENTS.md`
**依赖：** T56

**步骤：**

1. 编辑前查看当前 `AGENTS.md` diff，识别并保留用户已有无关改动。
2. 更新当前能力、架构和运行时命令段中的 C10 内容。
3. 把新增斜杠命令的成对维护点改为单一 `CommandSpec` 注册规则。
4. 保留字面左方括号的 Textual markup 转义提醒。

**验证：** 运行 `git diff -- AGENTS.md`，期望只有 C10 相关段落新增或调整，用户原有无关改动完整保留。

## T58：运行命令层目标测试

**文件：** 所有 C10 新增与修改测试文件
**依赖：** T27、T29、T34、T35、T41、T50、T51、T52、T54

**步骤：**

1. 运行 parser、registry、dispatcher、builtins、startup、TUI 六个新测试模块。
2. 运行 memory session、resume replay、review fixes、TUI keybindings 四个修改测试模块。
3. 若失败，只修复本任务范围内的 C10 回归并重跑对应模块。
4. 记录测试数与最终通过结果，不能用“预计通过”替代证据。

**验证：**

```powershell
python -m unittest tests.test_command_parser tests.test_command_registry tests.test_command_dispatcher tests.test_command_builtins tests.test_command_startup tests.test_command_tui tests.test_memory_session tests.test_resume_replay tests.test_review_fixes tests.test_tui_keybindings
```

期望退出码 0。

## T59：运行编译与完整回归

**文件：** `rhinecode/`、`tests/`
**依赖：** T55、T56、T57、T58

**步骤：**

1. 编译全部源码与测试。
2. 运行完整 unittest discovery。
3. 检查没有新增导入循环、Provider 请求字段泄漏或旧命令入口引用。
4. 失败时定位并修复后，从完整命令重新执行。

**验证：**

```powershell
python -m compileall rhinecode tests
python -m unittest discover -s tests
```

两条命令都应退出码 0。

## T60：核对变更边界与工作区

**文件：** 全部本次变更
**依赖：** T59

**步骤：**

1. 查看 `git status --short` 和 `git diff --stat`。
2. 确认三个具体 Provider、Agent Loop 与 Permission/Context/MCP/Memory 核心编排没有越界修改。
3. 确认 `AGENTS.md` 的用户原有改动未被覆盖。
4. 运行 `git diff --check`。
5. 对照 `docs/c10/checklist.md` 进入逐项验收，不在本任务中自行宣布验收通过。

**验证：** `git diff --check` 无输出且退出码 0；变更文件均能映射到本文件清单。

## 10. 执行顺序

```text
命令模型与纯逻辑
T1 → T2 → T3 → T4 → T5
          └→ T6 → T7 → T8 → T9 → T10
T3 + T7 → T11 → T12 → T13 → T14
T2 + T8 → T15 → T16 → T17 → T18 → T19 → T20 → T21 → T22
T3 + T8 + T12 + T19 → T23

双内容与领域层（可在命令纯逻辑测试稳定后并行）
T24 → T25 → T26 → T27
T24 → T28 → T29
T24 → T30 → T31 → T32 → T33
T24 + T30 + T31 + T32 → T34
T29 + T33 → T35

TUI
T7 → T36 → T38 → T39
T8 → T37
T40
T36 + T37 + T38 + T40 → T41
T23 + T37 + T38 → T42 → T43 → T44 → T45 → T46 → T47
T45 → T48
T39 + T42 + T47 → T49 → T50
T46 + T47 + T48 → T51

收口、启动与回归
T34 + T35 + T46 + T48 + T51 → T52
T23 + T42 → T53 → T54
T52 + T54 → T55 → T56 → T57
T27 + T29 + T34 + T35 + T41 + T50 + T51 + T52 + T54 → T58
T55 + T56 + T57 + T58 → T59 → T60
```

允许并行的分支：

- T3–T5（解析器）与 T6–T10（注册表）在 T1/T2 完成后可分别推进。
- T24–T29（双内容持久化）与 T30–T35（领域入口）可在依赖满足后交错推进。
- T36–T41（Widget）与 T53–T54（启动测试准备）只在共同接口稳定后并行。

## 11. Plan 组件覆盖

| Plan 组件 | 对应任务 |
|---|---|
| models / Protocol | T1–T2 |
| parser | T3–T5 |
| registry | T6–T10 |
| dispatcher | T11–T14 |
| builtins / aliases / help | T15–T22 |
| package exports | T23 |
| Message 双内容 | T24 |
| Session JSONL 与标题 | T25–T27 |
| 历史回放 | T28–T29 |
| ConversationManager 领域方法 | T30–T35、T52 |
| Provider 显示元数据隔离 | T34 |
| 输入高亮 | T36、T38、T41 |
| 动态补全菜单与 Tab | T37、T39、T47、T49 |
| 模式状态标签 | T40–T41、T50 |
| App 控制器与 Worker | T42–T46 |
| Enter 与会话面板 | T47–T50 |
| 启动 fail-fast | T53–T54 |
| 文档与全量回归 | T55–T60 |

所有 Plan 模块均至少有一个实现任务和一个明确验证落点；依赖链无循环。

## 12. 完成定义

单个任务只有同时满足以下条件才可标记完成：

1. 步骤中的代码或测试改动全部落地。
2. 该任务“验证”命令已实际运行。
3. 结果与期望一致；失败不能带到下一个依赖任务。
4. 没有修改任务文件清单之外的模块，或已先向用户说明必要原因。
5. 不以测试替代后续 `checklist.md` 的用户可见行为验收。

本任务文档获批后才进入 `checklist.md`；`checklist.md` 获批前仍禁止执行 T1–T60 的实现步骤。
