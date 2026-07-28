# C11 对齐 Agent Skills 开放标准 Plan

> 依据已批准的 `docs/c11/align/spec.md`（F1–F15 / N1–N5 / AC1–AC18）。

## 架构概览

改动集中在四层，其余各层不受影响：

| 层 | 改什么 | 不改什么 |
|---|---|---|
| **skills/** | 字段表重定义、命令名改由发现层计算、白名单校验模块换职责 | 三层扫描与整份覆盖、渲染的截断策略、加锁不变量 |
| **permission/** | 新增「本次执行内有效」的第三级规则 | 五层管线顺序、deny 优先、三层配置加载 |
| **agent/** | `ToolPolicy` 三元组塌缩为单一排除集；`load_skill` 改走串行特殊路径 | 循环骨架、权限接入点、trace 埋点 |
| **conversation/** | 每次执行外层成对授予/撤销；提供子对话回调给循环 | 子对话本身的实现、恢复语义、笔记钩子 |

`commands/` 只跟着 `SkillCommandInfo` 的字段变化走，`tui/` 不动。

## 一处 spec 内部矛盾及处置

**F13 写的是「移除『模型调用了本轮未提供的工具』这一兜底分支」，但它不能整条移除。**

理由：C11 的子对话通过「把加载工具从子对话工具集中排除」来防止无限嵌套
（Skill 里再激活 Skill）。排除之后模型仍可能凭训练先验硬造出一次调用——
若没有兜底判定，那次调用会**照常执行**，嵌套防线失效。

**处置**：把 F13 精确化为——移除**白名单相关**的越界判定（白名单没了，它确实无意义），
**保留**子对话防嵌套所需的排除判定。对应地，`ToolPolicy` 的三元组
（allowed / exempt / excluded）塌缩为单一的排除集合。

这不扩大 spec 范围，只是把「移除」的边界划准。已在下方技术决策表中登记。

## 核心数据结构

### SkillSpec（重定义字段）

| 字段 | 类型 | 来源 | 说明 |
|---|---|---|---|
| `command_name` | 字符串 | **发现层计算** | 目录名或去扩展名的文件名，命令与覆盖判定都用它 |
| `display_name` | 字符串 | frontmatter `name`，缺省取 `command_name` | 仅用于列表与清单展示 |
| `description` | 字符串 | frontmatter，缺省取正文第一段 | |
| `when_to_use` | 可空字符串 | frontmatter | 拼在 description 之后进清单 |
| `granted_tools` | 字符串元组 | frontmatter `allowed-tools` / `allowed_tools` | 预授权规则串，未声明为空元组 |
| `forked` | 布尔 | frontmatter `context == "fork"` | |
| `model_invocable` | 布尔 | `not disable-model-invocation` | 缺省真 |
| `user_invocable` | 布尔 | frontmatter，缺省真 | |
| `model` | 可空字符串 | frontmatter | 仅 `forked` 时生效 |
| `body` / `source` / `entry_path` / `resource_dir` / `resource_files` | 同 C11 | | |
| `notices` | 字符串元组 | 解析期产出 | 旧字段警告与无能力字段警告，随 spec 一路带到状态报告 |

**删除**：`allowed_tools`（收窄语义）、`mode`、`history_messages`、`name`（改为 `display_name`）。

### ToolGrant（新，permission 层）

一次预授权 = 一批 `Rule`，`effect="allow"`、`source="skill"`。
`PermissionEngine` 新增一个与 `session_rules` 同型的 `turn_rules` 列表，
在第③层合并时排在最前（`turn_rules + session_rules + file_rules`）。

选择「复用既有 Rule 与第③层」而不是新造一层的理由：预授权在语义上**恰好等价于**
用户在确认面板上选「本会话放行」，只是有效期更短。复用同一套求值逻辑，
deny 优先、黑名单与沙箱先于它生效这两条（N2）自动成立，不需要额外保证。

### RunOptions（简化）

`tool_policy` 回调返回的三元组塌缩为 `excluded: frozenset[str]`——
本轮不提供给模型、且调用了也要拒绝的工具名。唯一使用者是子对话的防嵌套。

## 模块设计

### skills/models.py
**职责**：字段表、枚举、常量。
**变化**：按上表重定义 `SkillSpec`；删 `SkillMode` 枚举；新增「无对应能力字段」的名单常量。

### skills/parser.py
**职责**：单份文本 → SkillSpec 片段或失败原因。**不碰文件系统**（既有分层，保持）。
**变化**：
- 接受连字符与下划线两种键名，归一化
- `description` 缺省从正文第一段提取
- 布尔字段接受标准规定的多种写法
- **产出 `notices`**：遇到旧 `allowed_tools`（F6）或无能力字段（F10）时各追加一条
- 由于命令名不再来自 frontmatter，`name` 的合法性校验整段删除

**为什么警告在 parser 产出**：警告必须能定位到具体文件的具体字段，而 parser 是
唯一同时掌握「原始键名」与「所属文件」的地方。放到更上层就只能给出笼统措辞。

### skills/discovery.py
**职责**：三层扫描、计算命令名、跨层整份覆盖。
**变化**：新增「从路径推导命令名」并写入 SkillSpec；覆盖与层内去重的键从 `name` 换成
`command_name`；**层内重名整段删除**（同目录下命令名天然唯一）。

### skills/render.py
**职责**：全部「给模型看的文本」。
**变化**：清单条目改为「命令名 + description + when_to_use」；其余（正文截断、
资源清单、参数替换、自包含调用文本）不变。

### skills/validation.py
**职责变更**：从「白名单两段校验」改为「**预授权规则串的解析与校验**」。
- 把 `allowed-tools` 的每一项解析成 `Rule`
- 标准里把只读检索拆成多个工具名的写法，映射到本系统的只读类别
- 无法识别的项：跳过并产出警告，**不 fail-fast**——外部 Skill 可能声明本系统没有的工具，
  那不该让程序起不来（这与 C11 对内置工具名笔误 fail-fast 的取舍相反，理由是来源不同）

C11 的 `lint_skill`（作者期体检）随白名单语义变更而失去大半依据，一并删除。

### skills/manager.py
**职责**：唯一持可变状态与副作用编排。**加锁不变量原样保留**。
**变化**：激活态不再携带白名单；新增「取当前激活 Skill 的预授权规则集合」；
状态报告增加 `notices` 段。

### permission/engine.py
**变化**：新增 `turn_rules` 及其授予/撤销两个操作，第③层合并时置于最前。
撤销必须是幂等的整体清空，而非按条移除——按条移除会在异常路径上留下残余。

### agent/loop.py
**变化**：
- `_schema_for` 的 `policy` 参数换成 `excluded` 集合
- 越界判定只保留「是否在排除集合中」
- **`load_skill` 改由串行特殊路径处理**（见下）

### tools/load_skill.py
**变化**：非 fork 分支行为不变；fork 分支通过注入的回调运行子对话，
把结论作为工具结果返回；被 `disable-model-invocation` 挡下时返回结构化错误。

### conversation.py
**变化**：
- 每次 `agent.run` 外层 `try/finally` 成对授予与撤销预授权（N3）
- 向循环提供「运行一个 fork Skill 并返回结论」的回调
- `run_skill` 的分支判据从 `mode` 换成 `forked`；`user_invocable` 为假时不注册短命令

## 模块交互

### 路径一：用户经斜杠命令触发

```
用户输入 /commit 修复超时
  → 命令层解析 → 协调层 run_skill
  → 授予预授权（engine.turn_rules ← spec.granted_tools）
  → forked ? 子对话执行 : 主对话提交自包含消息
  → agent.run(...)              ← 期间所有工具调用在第③层命中预授权即免确认
  → finally: 撤销预授权
```

### 路径二：模型自行发起

```
模型调用 load_skill(name)
  → 循环识别为特殊工具，走串行路径（不进并发桶、不进权限管线）
  → model_invocable ? 继续 : 返回结构化错误 + 用户可用入口
  → 授予预授权
  → forked ? 经回调跑子对话、把结论作为工具结果回灌
           : 激活正文进动态槽位、返回一句简短确认
  → 预授权由外层那次 agent.run 的 finally 统一撤销
```

**两条路径共用同一次撤销**：无论谁授予的，都在本次执行的 finally 里清空。
这正是 F12「有效期是触发它的那一次执行」的实现。

### 防嵌套

子对话的 `RunOptions.excluded` 含加载工具名 → 它不出现在子对话的工具集里；
模型若凭先验硬造一次调用，被越界判定拒绝并回灌原因，子对话继续。

## 文件组织

```
rhinecode/
├── skills/
│   ├── models.py        — 字段表重定义，删 SkillMode
│   ├── parser.py        — 键名归一、缺省提取、notices 产出
│   ├── discovery.py     — 命令名推导、覆盖键更换、删层内重名
│   ├── render.py        — 清单条目含 when_to_use
│   ├── validation.py    — 职责换成预授权规则解析（删 lint 与白名单校验）
│   └── manager.py       — 激活态去白名单、新增取预授权规则、报告加 notices
├── permission/
│   └── engine.py        — turn_rules 与成对的授予/撤销
├── agent/
│   └── loop.py          — excluded 取代 ToolPolicy；load_skill 走串行特殊路径
├── tools/
│   ├── policy.py        — 删除（三元组塌缩后无存在必要）
│   └── load_skill.py    — fork 分支经回调跑子对话
├── conversation.py      — 成对授予撤销、fork 回调、分支判据更换
└── skills/builtin/*.md  — 三个样板按新格式与新语义重写
```

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 预授权存哪 | `PermissionEngine` 新增 `turn_rules`，与会话规则同型、优先级更高 | 语义上等价于「本会话放行」但更短命；复用第③层求值，N2 自动成立 |
| 何时撤销 | 协调层每次执行外层 `try/finally` 整体清空 | 唯一覆盖取消/出错/迭代上限三种终止的位置；整体清空比按条移除更抗异常 |
| 模型发起 fork 怎么跑 | 循环把加载工具当**串行特殊工具**，经注入回调运行子对话 | 与既有的澄清/审批特殊工具同型；**避免在只读并发桶里跑一整条子对话**（那会让子对话的确认面板从线程池里弹出） |
| 防嵌套 | `RunOptions` 保留极简 `excluded` 集合 | 三元组里只有排除项还有用户；塌缩成集合是净简化 |
| 命令名在哪算 | 发现层，不在解析层 | 解析层不碰文件系统是既有分层，破坏它会让解析层的单测被迫造真实目录 |
| 旧字段警告在哪产出 | 解析层，随 spec 带到报告 | 只有解析层同时知道原始键名与所属文件 |
| 无法识别的预授权项 | 跳过 + 警告，不 fail-fast | 与 C11 对内置工具名笔误 fail-fast 的取舍相反：那时白名单是自家格式、写错就是笔误；现在声明可能来自外部工具，不该让程序起不来 |
| 是否保留作者期体检 | 删除 | 它的三条检查里两条（白名单等于全集、description 双份注入）随语义变更而失效；剩一条不值得单独维护 |
