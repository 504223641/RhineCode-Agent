# RhineCode

RhineCode 是一个用 Python + Textual 实现的终端 AI 编程助手，交互体验参考 Claude Code。

当前版本以 DeepSeek Provider 为主实现了 C4 Agent Loop：模型可以在一次用户请求中循环读取项目、搜索代码、执行工具、回灌结果并继续下一轮，直到自然完成或命中停止条件。Anthropic / OpenAI Provider 目前保持纯对话能力。

## 语言
中文回答

## 技术栈

- Python 3.11+
- [Textual](https://textual.textualize.io/) — TUI 框架（流式渲染基于 Worker + `call_from_thread`）
- `anthropic` / `openai` SDK，`pyyaml` 配置
- 依赖与入口定义在 `pyproject.toml`，控制台脚本 `rhinecode`

## 当前能力

- **ReAct Agent Loop**：自动执行“调用模型 → 执行工具 → 回灌结果 → 再调用模型”的多轮循环。
- **流式输出**：正文与思考内容逐块渲染，后台 Worker 不阻塞 TUI 主线程。
- **DeepSeek 工具系统**：支持读文件、glob 找文件、grep 搜内容、写文件、精确编辑文件、运行命令。
- **Plan Mode**：`/plan` 开启后先只允许只读调研和需求澄清，完整计划进入聊天记录，经用户批准后才进入执行阶段。
- **逐项执行确认**：写文件、改文件、运行命令等副作用工具会弹出内联确认面板；计划获批不等于免确认，除非用户选择“执行且不再询问”。
- **明确停止原因**：支持自然完成、迭代上限、用户取消、计划拒绝、连续未知工具、流错误等停止路径。
- **路径安全边界**：文件类工具只能访问项目工作目录内路径，拒绝 `..`、越界绝对路径和指向项目外的符号链接。

工具调用与 Plan Mode 目前仅在 `protocol: deepseek` 且启用默认工具注册中心时可用。

## 架构

当前核心分层如下，上层尽量不感知下层具体实现，通过抽象接口和事件流解耦：

- **TUI 层**（`rhinecode/tui/`）— `app.py` 是 Textual App 主类，用 Worker 消费 AgentEvent 并逐块渲染；`widgets.py` 提供 HistoryView / InputBar / StatusBar / 工具行 / diff / 确认和澄清面板。
- **协调层**（`rhinecode/conversation.py`）— `ConversationManager` 是 TUI 与 Agent / Provider 之间的中转点，维护对话历史、解析斜杠命令、管理思考模式和 Plan Mode、封装确认/澄清/计划审批回调。
- **Agent 层**（`rhinecode/agent/`）— `loop.py` 实现 ReAct 循环；`events.py` 定义 AgentEvent、停止原因和确认决策；`collector.py` 收集流式正文、思考与工具调用；`plan_tools.py` 和 `prompt.py` 支撑 Plan Mode。
- **Provider 层**（`rhinecode/provider/`）— `base.py` 定义 `BaseProvider`/`Message`/`StreamChunk` 抽象；`anthropic.py`、`openai.py`、`deepseek.py` 为具体实现；`factory.py` 的 `create_provider` 按 `protocol` 分发。
- **Tools 层**（`rhinecode/tools/`）— `base.py` 定义 Tool / ToolResult 抽象；`registry.py` 注册默认工具；`path_guard.py` 负责路径边界；`read_file.py`、`write_file.py`、`edit_file.py`、`run_command.py`、`glob_files.py`、`grep_content.py` 是当前 6 个核心工具。

新增 Provider：在 `provider/` 下继承 `BaseProvider` 实现 `stream_chat`，再到 `factory.py` 添加 `elif` 分支，配置中 `protocol` 改为新值即可。

如果新 Provider 要支持工具调用，需要参考 `deepseek.py`：

- 把 `tools` schema 传给模型 API。
- 从流式响应中拼接工具调用参数。
- 产出 `StreamChunk(type="tool_call")`。
- 能序列化历史中的 `assistant(tool_calls)` 与 `role="tool"` 消息。

新增工具：在 `tools/` 下继承 `Tool`，声明 `name`、`description`、`parameters`、`read_only`，实现 `execute`，再到 `ToolRegistry.default()` 注册。文件类工具必须复用 `path_guard.py` 的路径边界校验。

## 常用命令

```bash
pip install -e .                          # 安装（开发模式）
cp config.example.yaml config.yaml        # 创建配置后填入真实 api_key
python -m rhinecode --config config.yaml  # 启动
rhinecode --config config.yaml            # 安装后也可以用控制台脚本启动
```

运行时斜杠命令：

- `/think`：在 off / high / max 间循环切换思考模式（Anthropic / DeepSeek 生效）。
- `/plan`：切换 Plan Mode，先规划、澄清和审批，再执行（DeepSeek 工具模式生效）。
- `/clear`：清空当前对话历史。
- `/exit`：退出程序。

运行中按 `Esc` 会请求取消当前 Agent Loop；如果正在等待确认或澄清，则由当前面板处理取消。

## 配置

`config.yaml`（git 忽略，从 `config.example.yaml` 复制）字段：`protocol`（anthropic/openai/deepseek）、`model`、`base_url`、`api_key`。

## Spec 驱动开发

开发新功能/章节前使用 `/spec` 技能，协作澄清需求后依次生成 `docs/<章节>/` 下的 `spec.md → plan.md → task.md → checklist.md`，再据此开发与验收。当前主线章节为 `docs/c4/`。

C4 文档描述当前 Agent Loop 与 Plan Mode 的实际行为：

- `docs/c4/spec.md`
- `docs/c4/plan.md`
- `docs/c4/task.md`
- `docs/c4/checklist.md`

## 测试

开发完成后优先运行：

```bash
python -m compileall rhinecode tests
python -m unittest discover -s tests
```

当前测试覆盖路径越界防护、确认回调、会话级免确认、Plan Mode 完整计划展示、拒绝计划停止、计划获批后仍逐项确认等关键行为。

涉及 TUI 行为时，再用 tmux 或真实终端做端到端测试：

1. 在 tmux 中启动 RhineCode
2. 输入一段真实的对话请求
3. 观察 RhineCode 是否正确调用工具、生成回复
4. 对照对应章节的 `checklist.md` 逐项验收

## 安全边界

- 文件、glob、grep 工具以启动 RhineCode 时的当前工作目录作为项目根。
- 工具路径不能包含 `..`。
- 绝对路径必须解析后仍位于项目根内。
- 指向项目外的符号链接会被拒绝或跳过。
- 命令工具显式以项目根作为 `cwd`，但不做命令沙箱；危险命令仍需要用户判断确认。
- `config.yaml` 可能包含真实 API Key，请勿提交到版本库。

## 代码注释规范

为了降低项目理解成本，所有新增或修改的代码都必须包含充分、清晰、准确的中文注释。注释目标是：让第一次接触本项目的开发者，仅通过阅读代码和注释，就能理解代码的设计意图、执行流程、关键边界条件，并能够复现或安全修改相关逻辑。

### 基本要求

1. **注释语言**
   - 注释主体必须使用中文。
   - 技术关键词、框架名、协议名、变量名、函数名、类型名、设计模式名等可以保留英文，例如：`React`、`hook`、`middleware`、`cache`、`token`、`retry`、`Promise`、`DTO`、`Repository`。
   - 不要为了中文化而强行翻译通用技术词汇，避免造成理解歧义。

2. **注释粒度**
   - 关键模块、核心函数、复杂逻辑、边界处理、异常处理、数据转换、状态变更、异步流程、缓存策略、权限判断等必须写注释。
   - 简单自解释代码不需要机械式注释，例如 `count += 1` 不需要写“计数加一”。
   - 注释应解释“为什么这么做”和“这段代码承担什么职责”，而不是重复代码表面含义。

3. **可复现性要求**
   - 对于核心业务流程，注释需要说明输入来源、处理步骤、输出结果、关键依赖和副作用。
   - 第一次看项目的人应能根据注释理解该逻辑如何运行，并能在相同输入条件下复现代码行为。
   - 如果代码依赖特定配置、环境变量、外部服务、数据库结构或第三方 API，必须在注释中说明。

4. **函数/方法注释**
   - 复杂函数必须说明：
     - 函数用途
     - 参数含义
     - 返回值含义
     - 主要执行步骤
     - 可能抛出的异常或失败情况
     - 是否存在副作用，例如写数据库、发请求、修改全局状态、写缓存等

   示例：

   ```ts
   /**
    * 根据用户 ID 获取用户的完整资料。
    *
    * 执行流程：
    * 1. 先从 cache 中读取用户资料，减少数据库查询压力。
    * 2. 如果 cache 未命中，则查询 database。
    * 3. 查询成功后会将结果写回 cache，供后续请求复用。
    *
    * @param userId 用户唯一标识，必须是已登录用户的 ID。
    * @returns 用户完整资料；如果用户不存在，则返回 null。
    *
    * 副作用：
    * - cache 未命中时会访问 database。
    * - 查询成功后会写入 cache。
    */
   async function getUserProfile(userId: string): Promise<UserProfile | null> {
     // 优先读取 cache，避免高频请求直接打到 database
     const cachedProfile = await cache.get(userId)

     if (cachedProfile) {
       return cachedProfile
     }

     // cache 未命中时再查询 database，保证数据仍然可以被正确获取
     const profile = await userRepository.findById(userId)

     if (!profile) {
       return null
     }

     // 将查询结果写回 cache，提高后续相同用户请求的响应速度
     await cache.set(userId, profile)

     return profile
   }
   ```

## 文档搜索
在参考任何文档之前请确保文档是否是最新版本

## 学习与解释要求

我是第一次独立完成这类项目，可能对项目中的部分技术概念、架构设计、工具链、代码写法或最佳实践不熟悉。

在协助我开发时，请遵守以下要求：

1. **不要默认我已经理解相关技术背景**

   * 如果涉及新的技术概念、框架、库、设计模式或工程实践，请先用清楚、通俗的中文解释它是什么、为什么要用、解决了什么问题。

2. **解释代码修改的原因**

   * 不只是直接给出代码，还需要说明为什么要这样改。
   * 如果有多种实现方式，请简单说明当前方案的优点，以及为什么更适合这个项目。

3. **使用适合初学者理解的说明方式**

   * 解释时尽量避免只堆砌专业术语。
   * 必要时可以使用类比、步骤拆解或简单示例帮助理解。
   * 专业关键词可以保留英文，但需要配合中文解释。

4. **指出我需要重点理解的知识点**

   * 如果某段代码或某个设计背后涉及重要知识点，请明确指出。
   * 例如：异步处理、事件循环、状态管理、配置加载、异常处理、UI 渲染、Provider 抽象等。

5. **避免只给结论**

   * 对于关键修改，请说明：

     * 问题是什么
     * 为什么会出现这个问题
     * 应该如何解决
     * 修改后会带来什么效果

6. **保持教学式协作**

   * 这个项目不仅是为了完成代码，也是为了让我理解项目是如何搭建和演进的。
   * 因此，请在保证代码质量的同时，帮助我逐步建立对项目结构、技术选型和实现细节的理解。
