# RhineCode

我正在构建一个终端 AI 编程助手（类似 Claude Code），项目名叫 RhineCode，使用 Python 实现。

## 语言
中文回答

## 测试

开发完功能后，用 tmux 做端到端测试：

1. 在 tmux 中启动 RhineCode
2. 输入一段真实的对话请求
3. 观察 RhineCode 是否正确调用工具、生成回复
4. 对照 checklist.md 逐项验收

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