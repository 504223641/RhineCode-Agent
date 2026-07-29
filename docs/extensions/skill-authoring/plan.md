# Skill 作者期 Plan

> 对应 [`spec.md`](spec.md)。语言：Python 3.11+。

## 架构概览

三块改动，彼此独立：

| 块 | 做什么 | 触及的层 |
| --- | --- | --- |
| **A. 体检** | 新增一个纯函数模块，把「已加载的 Skill 定义」算成「一组建议」 | `skills/`（新增一个模块 + 两处数据流补齐 + 报告集成） |
| **B. `skill-creator`** | 新增一个目录型内置样板（入口 + 随附字段参考） | `skills/builtin/` + `pyproject.toml` |
| **C. 清理** | 删掉状态报告的死参数与它的过期文档 | `skills/manager.py` + `conversation.py` |

**没有新增层，也没有新增交互态。** 建议只是报告里多一段文本；修复走既有的
`edit_file` 确认面板；创建走既有的 `write_file` 确认面板。

### 体检模块坐在哪一层

skills 包是严格单向的依赖链，新模块必须放进这条链里，不能横插：

```
models.py      数据结构与常量
   ↑
parser.py      单份文本 → SkillSpec
   ↑
discovery.py   三层扫描、命令名推导、跨层覆盖
   ↑
render.py      清单 / 激活正文 / 参数替换
   ↑
validation.py  预授权声明 → 权限规则
   ↑
audit.py       ← 新增：SkillSpec + 覆盖事实 → 建议列表
   ↑
manager.py     唯一持有可变状态与副作用编排
```

放在 `validation` 之上是**被依赖关系决定的**，不是随意选的——体检的两项检查
必须调用下层：

- 「预授权全军覆没」要知道声明**翻译成了几条规则** → 依赖 `validation.grants_for`
- 「逼近注入上限」要量**渲染后的注入段** → 依赖 `render.render_active_body`

放在 `manager` 之下则是 N1（纯函数零 IO）决定的：manager 是唯一持有可变状态和
做 IO 的地方，体检一旦进去就不再是纯函数。

> **为什么不叫 `lint_*`**：`tests/test_skill_validation.py::ObsoleteApiRemovedTest`
> 钉着 `validation` 模块里不得再出现 `lint_skill` / `lint_skills`——那是上一轮
> 作者期为「白名单收窄」语义写的，已随对齐改造删除。沿用旧名字会让人以为
> 是同一套东西回来了。用 `audit` 明确区分。

## 核心数据结构

全部放 `skills/models.py`——该模块是本包唯一的数据结构与常量集中地，
新增结构跟着走，不另立门户。

### `AdviceKind`（枚举）

七项检查各一个成员：

| 成员 | 对应 spec |
| --- | --- |
| `DESCRIPTION_TOO_LONG` | F2-1 |
| `MISSING_DESCRIPTION` | F2-2 |
| `NO_PLACEHOLDER` | F2-3 |
| `BROAD_GRANT` | F2-4 |
| `GRANTS_ALL_DROPPED` | F2-5 |
| `NEAR_INJECTION_LIMIT` | F2-6 |
| `OVERRIDES_BUILTIN` | F2-7 |

**为什么要枚举而不只是一段文本**：测试要能断言「命中的是哪一条」。
只有文本的话，测试只能写 `assertIn("过长", report)`——措辞一改测试就碎，
而措辞恰恰是这次要反复打磨的东西（F1 要求建议可操作）。有了枚举，
措辞怎么改都不影响判定类的断言。

### `SkillAdvice`（frozen dataclass）

```python
kind: AdviceKind    # 判定类别，供测试与去重
skill: str          # 命令名
finding: str        # 发现了什么
suggestion: str     # 建议怎么改
```

**三段式而不是一个 `text` 字段**：F1 要求每条建议必须同时说清
「哪个 Skill / 发现了什么 / 建议怎么改」。拆成字段让结构本身强制这三样都填，
写建议时漏掉「怎么改」在类型层面就过不去；合成一个字符串则全靠自觉。

渲染成一行：`- <skill>：<finding> → 建议：<suggestion>`

### 新增常量

| 常量 | 值 | 说明 |
| --- | --- | --- |
| `DESCRIPTION_MAX_CHARS` | 100 | F2-1 的阈值 |
| `NEAR_LIMIT_RATIO` | 0.8 | F2-6 的比例 |
| `READ_ONLY_GRANT_TOOLS` | `{"Read"}` | F2-4 判定「有无副作用」的**唯一白名单**，见下方决策 4 |

### 两处数据流补齐（spec F2a）

#### `SkillSpec` 新增 `description_explicit: bool = True`

记录「说明字段是不是作者自己写的」。由 `parser.parse_skill` 填写：
frontmatter 里读到非空 `description` 时为真，走正文第一段回填时为假。

**默认值取 `True` 而不是 `False`**：这个默认只对手工构造的定义生效
（解析器永远显式赋值）。取 `False` 会让任何一个手工构造、带触发说明却没设该字段的
定义**误报**一条「你忘了写说明」；取 `True` 最坏是漏报。建议系统里误报比漏报贵——
一条错的建议会让用户去改一个本来没问题的地方。

代价是「解析器忘了赋值」会变成静默漏报，用一条测试钉住解析器两条路径各自的取值。

#### `SkillCatalog` 新增 `shadowed: tuple[tuple[str, SkillSource], ...] = ()`

记录「哪个命令名覆盖掉了哪一层的同名定义」，每个元素是
`(命令名, 被覆盖那份所在的层)`。

**必须在 `discover` 的 `setdefault` 落空处当场记下**——那是这个信息唯一还存在的
时刻，之后被丢弃那份的对象就没有任何引用了（spec F2a 第二处说的「信息已被丢弃、
无从推断」就是指这里）。

体检只关心内置层，判定即 `(name, SkillSource.BUILTIN) in shadowed`。
记全部层而不只记内置，是因为多记两个元组不花什么代价，
而将来若要放宽到「项目级盖用户级也提示」，数据已经在了。

## 模块设计

### `skills/audit.py`（新增）

**职责**：把「已加载的 Skill 定义 + 覆盖事实」算成「一组建议」。

**对外接口**：一个函数，接收 Skill 定义序列与覆盖事实，返回建议元组。
建议按「Skill 名 → 检查项定义顺序」排序，使输出稳定可断言。

**依赖**：`models`、`render`、`validation`。不依赖 `manager`、不依赖任何 IO。

**七项检查的实现要点**：

| 项 | 怎么判 | 注意 |
| --- | --- | --- |
| 1 说明过长 | `len(description) > DESCRIPTION_MAX_CHARS` | 按**字符数**不按字节数——中文说明按字节算会莫名其妙地比英文早三倍命中 |
| 2 有触发说明无说明 | `when_to_use` 非空 且 `description_explicit` 为假 | 判的是「作者写没写」，**不是**「是不是空的」（说明字段永不为空） |
| 3 无占位符 | 占位符常量不在正文中 | 建议措辞必须含「不会丢、只是位置在末尾」 |
| 4 预授权过宽 | 对 `grants_for` 产出的每条规则：`tool not in READ_ONLY_GRANT_TOOLS and not pattern` | 判的是**翻译后的规则**不是原始声明串——原始串的写法太多（列表/分隔串/大小写/别名），判规则只有一种形态 |
| 5 全军覆没 | `granted_tools` 非空 且 `grants_for` 返回的规则列表为空 | |
| 6 逼近上限 | 见下方决策 5 | |
| 7 覆盖内置 | `(name, BUILTIN) in shadowed` | |

### `skills/manager.py`（修改）

`report()` 两处改动：

1. **删除 `registered` 参数**（F14）。它从不被使用，其文档字符串还在描述一套
   已随对齐改造删除的检查。
2. **新增建议段**，排在既有的「加载失败 / 警告 / 字段提示」之后、
   项目级信任提示之前；无建议时整段不出现（F5）。存在建议时段尾附一句
   指向 `skill-creator` 的入口提示（F13）。

**体检在锁外算**（N3）。现有 `report()` 已经是「持锁取快照 → 出锁渲染」的结构，
体检放在出锁之后即可，不需要改动加锁范围。`_catalog` 是不可变快照，
读它的引用无需持锁。

### `skills/discovery.py`（修改）

`discover` 里 `setdefault` 落空时记一条 `(命令名, 该层)` 进覆盖清单，
填入产出的快照。**不改变任何加载行为**——覆盖依然静默、依然不记错误（N6）。

### `skills/parser.py`（修改）

产出定义时填 `description_explicit`。**只多一个赋值**，解析规则一字不动。

### `skills/builtin/skill-creator/`（新增，目录型）

```
skill-creator/
├── SKILL.md      入口：三种用途的操作流程
└── reference.md  随附资源：完整字段参考
```

**为什么必须是目录型**：F11 要求携带一份完整字段参考，而正文有注入上限
（第 6 项检查自己就查这个）。把参考塞进正文会让这个样板自己逼近上限，
且每次激活都付出这份参考的上下文代价——而它只在模型真要写 frontmatter 时才用得上。
做成随附资源，模型按需 `read_file` 读取，正文里只留一句「先去读它」。

**读得到吗**：内置目录在工作区之外，路径沙箱本来会拒绝。但启动接线里已有
`register_read_root(builtin_skills_dir())`，把内置目录注册进了 path_guard 的
**只读白名单**——这条通路是 C11 为「目录型 Skill 的随附资源」建的，正好复用。

**frontmatter 要点**：

- 不写 `context` → 留在主对话（F8）
- 不写 `disable-model-invocation` → 模型可自行发起（与其它三个样板一致）
- `allowed-tools` **只预授权只读调研**（读文件、检索）。
  **写入与编辑刻意不预授权**——那正是需要用户看一眼的那一步（F10、安全边界），
  与既有 `commit` 样板「不预授权 `git commit`」是同一个取舍
- 正文长度必须控制在第 6 项检查的阈值以下，否则这个样板自己会触发建议

**正文结构**：开场分流三种用途 → 第 0 步先读随附参考 → 三条分支各自的步骤 →
最后一段硬规则（默认项目级、内置层永不写入、写盘前先把内容给用户看）。

### `pyproject.toml`（修改）

现有的分发资源声明是**单层**通配，覆盖不到目录型样板的两个文件（spec N5 已载明
这条现在不成立）。改为同时覆盖单层与两层。

**为什么不用 `**` 递归通配**：它对 setuptools 的版本有要求，而写两条显式模式
在所有版本上都成立，且内置样板不会嵌套超过两层（一个 Skill 一个目录，就到底了）。

### `conversation.py`（修改）

跟随 `report()` 签名变更，去掉传参。**只有这一处调用方**（测试里的调用本来就是无参的）。

## 模块交互

### 体检的数据流（每次查看状态时走一遍）

```
用户敲 /skills
      │
      ▼
命令层 → 协调层 → manager.report()
                      │
      ┌───────────────┴───────────────┐
      │ 持锁：取激活态 / 降级标记 / 警告快照 │
      └───────────────┬───────────────┘
                      │ ← 出锁
                      ▼
              audit_skills(catalog.skills, catalog.shadowed)
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
  grants_for()  render_active_body()  纯字段判断
   （规则）        （注入段长度）      （长度/占位符/覆盖）
        └─────────────┼─────────────┘
                      ▼
              tuple[SkillAdvice, ...]
                      │
                      ▼
              拼进报告的「建议」段
```

### 覆盖事实的产生（每次扫描时走一遍）

```
discover()
   │ 逐层扫描 PROJECT → USER → BUILTIN
   │
   ├─ setdefault 成功 → 该份胜出
   └─ setdefault 落空 → 记一条 (命令名, 本层) 进覆盖清单
                              │
                              ▼
                     SkillCatalog.shadowed
```

### 修复路径（用户认可建议后）

```
/skills 看到建议
      │
      ▼
/skill-creator 按建议修复 xxx
      │
      ▼
主对话 Agent Loop
      │
      ├─ read_file 读 reference.md（预授权，免确认）
      ├─ read_file 读目标 Skill 文件（预授权，免确认）
      ├─ 向用户报告将怎么改，逐条确认采纳与否
      └─ edit_file 落盘 → **弹确认面板，用户看到改前改后** → 确认后写入
```

**这条链上没有任何新代码**——全部由既有的 Agent Loop、权限管线与确认面板承担。
`skill-creator` 只是一份文本，它做的全部事情是「让模型知道该怎么走这条链」。

## 文件组织

```
rhinecode/skills/
├── models.py                    改：AdviceKind / SkillAdvice / 三个常量
│                                    + SkillSpec.description_explicit
│                                    + SkillCatalog.shadowed
├── parser.py                    改：填 description_explicit
├── discovery.py                 改：记录 shadowed
├── audit.py                     新增：audit_skills（纯函数）
├── manager.py                   改：report() 删参数 + 加建议段
└── builtin/
    ├── commit.md                （不动）
    ├── review.md                （不动）
    ├── test.md                  （不动）
    └── skill-creator/           新增
        ├── SKILL.md
        └── reference.md

rhinecode/conversation.py        改：report() 调用去掉传参
pyproject.toml                   改：package-data 覆盖目录型样板

tests/
├── test_skill_audit.py          新增：七项检查 + 边界 + 纯函数性
├── test_skill_parser.py         改：description_explicit 两条路径
├── test_skill_discovery.py      改：shadowed 的产生与内容
└── test_skill_manager.py        改：建议段的位置与「无建议不出现」

docs/
├── extensions/README.md         改：当前扩展表加一行
└── todo/                        改：删 1-skill-authoring.md，其余重排序号

CLAUDE.md                        改：扩展说明 + 成对维护点（见下）
```

### CLAUDE.md 要同步的成对维护点

本次**新增两条**，都属于「漏改不报错」那一类：

1. **新增一项体检检查** → `models.py` 的 `AdviceKind`（枚举）+ `audit.py`（判定与措辞）。
   漏了枚举则无法在测试里精确断言，只能退回脆弱的字符串匹配。
2. **新增一个只读工具类别** → `validation.py` 的 `_TOOL_ALIASES` + `models.py` 的
   `READ_ONLY_GRANT_TOOLS`。**漏改的后果是「多报一条预授权过宽」**——见决策 4，
   这是刻意选的偏严方向，但仍要登记，否则下一个人会以为是 bug。

## 技术决策

| # | 决策点 | 选择 | 理由 |
| --- | --- | --- | --- |
| 1 | 体检模块位置 | `skills/audit.py`，坐在 `validation` 与 `manager` 之间 | 被依赖关系决定：要调 `grants_for` 与 `render_active_body`，又必须在持有状态的 `manager` 之下才能保持纯函数 |
| 2 | 建议的表示 | 三段式 frozen dataclass + 判定枚举 | 枚举让测试断言「命中哪一条」而不依赖措辞；三段式让「必须给出改法」在类型层面被强制 |
| 3 | 建议何时算 | 每次渲染报告时现算，不缓存 | 缓存就要考虑何时失效，多一处「reload 之后忘了更新」的机会。体检是纯函数、成本极低（几十个 Skill 的字符串判断） |
| 4 | 「有无副作用」怎么判 | 维护**只读**白名单（现仅 `Read`），不在白名单里的一律视为有副作用 | 反过来维护「有副作用清单」的话，将来新增一个有副作用的工具**忘了登记就会漏报**，而漏报是静默的。现在这个方向下，忘了登记只会**多报一条建议**——用户看得见、能反馈，且不造成任何实际损害。**让遗漏偏向可见的一侧** |
| 5 | 注入上限量什么 | 量 `render_active_body` 的**返回文本**，且渲染时传空参数 | 截断作用在渲染后的整段（含边界标识、说明行、资源清单），量正文原文会漏报。传空参数会让含占位符的 Skill 量出的长度略小于实际——阈值本身留了 20% 余量，可接受，且这是**偏松**的方向（宁可少报一条建议，不要为一个正常大小的 Skill 报警） |
| 6 | 已经超限的怎么算 | 渲染返回了截断标记时**直接判命中** | 「已经被截断」比「逼近上限」更该报，两者共用一条建议、措辞里区分 |
| 7 | `description_explicit` 默认值 | `True` | 只影响手工构造的定义；取 `False` 会误报「你忘了写说明」，取 `True` 最坏是漏报。建议系统里误报比漏报贵 |
| 8 | `shadowed` 记几层 | 记全部层，体检只用内置那部分 | 多记两个元组不花什么代价；将来若放宽到「项目级盖用户级也提示」，数据已经在了 |
| 9 | `skill-creator` 单文件还是目录型 | 目录型 | 字段参考塞进正文会让样板自己逼近注入上限，且每次激活都付出它的上下文代价；做成随附资源则按需读取 |
| 10 | `skill-creator` 执行模式 | 留主对话（不写 `context`） | 三种用途都要与用户往复确认；子对话一次性跑完只回流结论，用户看不到中间过程也无从插话（spec F8） |
| 11 | 写盘怎么走 | 模型调既有写入/编辑工具，走完整权限管线 | 复用既有确认面板，**零新增交互态**。自建 diff 面板要动四处（事件枚举 / 面板选项 / id 映射 / 回调），且用户要多学一种确认体验 |
| 12 | 预授权给到哪 | 只给只读调研；写入与编辑**不给** | 那正是需要用户看一眼的那一步。与既有 `commit` 样板「不预授权 `git commit`」同一取舍 |
| 13 | 分发资源通配写法 | 显式写两条模式，不用 `**` | `**` 对 setuptools 版本有要求；内置样板不会嵌套超过两层 |
| 14 | 内置样板零建议怎么保证 | 实现时逐一跑一遍并记录，**不建 自动化断言** | 硬判据会让日后内置样板的正常调整被无关测试拦住，而人被拦住时倾向改判据而非改样板（spec F6） |

## 对既有行为的影响

**除报告多一段外，本次不改变任何既有行为**——这是 N6 的实现约束，
也是 review 时该盯的地方：

| 既有行为 | 会不会变 |
| --- | --- |
| Skill 的加载、覆盖、失败容错 | 不变（只多记一份覆盖事实，判定逻辑一字不动） |
| 第一阶段清单与激活正文的内容 | 不变 |
| 预授权的翻译结果与生效范围 | 不变（体检只是**读**翻译结果，不改它） |
| 权限管线的任何一层 | 不变 |
| 状态报告除建议段外的部分 | 不变 |
| `report()` 的调用方 | 签名少一个参数，唯一的生产调用方跟着改 |
