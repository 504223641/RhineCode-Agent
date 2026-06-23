# RhineCode

我正在构建一个终端 AI 编程助手（类似 Claude Code），项目名叫 RhineCode，使用 Python 实现。

终端启动后进入 Textual TUI 多面板界面，支持与 Anthropic Claude、OpenAI、DeepSeek 进行多轮流式对话。当前处于 MVP 阶段（纯对话，工具调用尚未实现）。

## 语言
中文回答

## 技术栈

- Python 3.11+
- [Textual](https://textual.textualize.io/) — TUI 框架（流式渲染基于 Worker + `call_from_thread`）
- `anthropic` / `openai` SDK，`pyyaml` 配置
- 依赖与入口定义在 `pyproject.toml`，控制台脚本 `rhinecode`

## 架构

分三层，上层不感知下层具体实现，通过抽象接口解耦：

- **TUI 层**（`rhinecode/tui/`）— `app.py` 是 Textual App 主类，用 Worker 消费流式 chunk 并逐块渲染；`widgets.py` 提供 HistoryView / InputBar / StatusBar。
- **协调层**（`rhinecode/conversation.py`）— `ConversationManager` 是 TUI 与 Provider 之间的唯一中转点，维护对话历史、解析斜杠命令、管理思考模式三档强度（off/high/max）。
- **Provider 层**（`rhinecode/provider/`）— `base.py` 定义 `BaseProvider`/`Message`/`StreamChunk` 抽象；`anthropic.py`、`openai.py`、`deepseek.py` 为具体实现；`factory.py` 的 `create_provider` 按 `protocol` 分发。

新增 Provider：在 `provider/` 下继承 `BaseProvider` 实现 `stream_chat`，再到 `factory.py` 添加 `elif` 分支，配置中 `protocol` 改为新值即可。

## 常用命令

```bash
pip install -e .                          # 安装（开发模式）
cp config.example.yaml config.yaml        # 创建配置后填入真实 api_key
python -m rhinecode --config config.yaml  # 启动
```

运行时斜杠命令：`/think`（切换思考模式，仅 anthropic/deepseek 生效）、`/clear`（清空历史）、`/exit`（退出）。

## 配置

`config.yaml`（git 忽略，从 `config.example.yaml` 复制）字段：`protocol`（anthropic/openai/deepseek）、`model`、`base_url`、`api_key`。

## Spec 驱动开发

开发新功能/章节前使用 `/spec` 技能，协作澄清需求后依次生成 `docs/<章节>/` 下的 `spec.md → plan.md → task.md → checklist.md`，再据此开发与验收。当前章节为 `docs/c2/`。

## 测试

开发完功能后，用 tmux 做端到端测试：

1. 在 tmux 中启动 RhineCode
2. 输入一段真实的对话请求
3. 观察 RhineCode 是否正确调用工具、生成回复
4. 对照对应章节的 `checklist.md` 逐项验收

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
