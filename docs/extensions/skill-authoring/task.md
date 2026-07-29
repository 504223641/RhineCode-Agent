# Skill 作者期 Tasks

> 对应 [`spec.md`](spec.md) 与 [`plan.md`](plan.md)。共 22 个任务。
>
> **每个任务完成后立即提交一个 commit**（本项目约定：一次改动一个 commit，
> 提交信息说清「问题是什么、为什么这么改」）。

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 改 | `rhinecode/skills/models.py` | 建议的枚举与数据类、三个新常量、两处数据流字段 |
| 改 | `rhinecode/skills/parser.py` | 填 `description_explicit` |
| 改 | `rhinecode/skills/discovery.py` | 记录 `shadowed` |
| **新建** | `rhinecode/skills/audit.py` | 七项检查的纯函数实现 |
| 改 | `rhinecode/skills/manager.py` | `report()` 删死参数 + 加建议段 |
| **新建** | `rhinecode/skills/builtin/skill-creator/SKILL.md` | 内置样板入口 |
| **新建** | `rhinecode/skills/builtin/skill-creator/reference.md` | 随附字段参考 |
| 改 | `rhinecode/conversation.py` | 跟随 `report()` 签名变更 |
| 改 | `pyproject.toml` | 分发资源覆盖目录型样板 |
| **新建** | `tests/test_skill_audit.py` | 七项检查 + 边界 + 纯函数性 |
| 改 | `tests/test_skill_parser.py` | `description_explicit` 两条路径 |
| 改 | `tests/test_skill_discovery.py` | `shadowed` 的产生与内容 |
| 改 | `tests/test_skill_manager.py` | 建议段的位置与「无建议不出现」 |
| 改 | `CLAUDE.md` | 扩展说明 + 两条新成对维护点 |
| 改 | `docs/internals/architecture.md` | skills 层的模块分解加 `audit.py` |
| 改 | `docs/internals/capabilities.md` | Skill 能力段补体检与 `skill-creator` |
| 改 | `docs/internals/testing.md` | 新增测试文件的覆盖清单 |
| 改 | `docs/extensions/README.md` | 当前扩展表加一行 |
| 删 | `docs/todo/1-skill-authoring.md` | 本扩展做完即删 |
| 改 | `docs/todo/2-web-search.md`、`3-p1b-unattended.md` | 重排序号为 1、2 |

---

## 阶段一：数据流补齐（T1–T5）

体检要用的两处事实现在没被保留，先把它们接出来。**这一阶段结束时体检还不存在，
但所有输入已经就位。**

### T1: 新增建议的数据结构与常量

**文件：** `rhinecode/skills/models.py`
**依赖：** 无

**步骤：**
1. 新增枚举 `AdviceKind`，七个成员对应 spec F2 的七项检查：
   `DESCRIPTION_TOO_LONG` / `MISSING_DESCRIPTION` / `NO_PLACEHOLDER` /
   `BROAD_GRANT` / `GRANTS_ALL_DROPPED` / `NEAR_INJECTION_LIMIT` /
   `OVERRIDES_BUILTIN`。docstring 写明「为什么要枚举而不只是文本」——
   测试要能断言命中哪一条而不依赖措辞。
2. 新增 frozen dataclass `SkillAdvice`，字段 `kind` / `skill` / `finding` / `suggestion`。
   docstring 写明三段式是为了在类型层面强制「必须给出改法」。
3. 新增三个常量并各写一句理由注释：
   - `DESCRIPTION_MAX_CHARS = 100`（按字符数不按字节数，否则中文说明会比英文早三倍命中）
   - `NEAR_LIMIT_RATIO = 0.8`
   - `READ_ONLY_GRANT_TOOLS = frozenset({"Read"})`（**只读白名单**，
     不在其中的一律视为有副作用；注释必须写清这个方向是刻意选的——
     忘了登记只会多报一条建议，而反过来会静默漏报）

**验证：** `python -c "from rhinecode.skills.models import AdviceKind, SkillAdvice, READ_ONLY_GRANT_TOOLS; print(len(AdviceKind), READ_ONLY_GRANT_TOOLS)"` 输出 `7 frozenset({'Read'})`

---

### T2: `SkillSpec` 记录说明字段是否显式声明

**文件：** `rhinecode/skills/models.py`
**依赖：** 无（可与 T1 并行）

**步骤：**
1. 给 `SkillSpec` 追加字段 `description_explicit: bool = True`，**放在
   `notices` 之后**（frozen dataclass 的带默认值字段必须排在末尾）。
2. docstring 说明：这不是 frontmatter 字段而是**解析期的派生事实**，
   记录「说明字段是作者自己写的，还是由正文第一段回填的」。
3. 注释写明默认取 `True` 的理由（只影响手工构造的定义；取 `False` 会误报
   「你忘了写说明」，取 `True` 最坏是漏报；建议系统里误报比漏报贵）。

**验证：** `python -m unittest tests.test_skill_render tests.test_perm_allow_rule tests.test_perm_turn_grant` 全绿
（这三个文件手工构造 `SkillSpec`，用它们确认加字段没打断既有构造点）

---

### T3: 解析层填写 `description_explicit`

**文件：** `rhinecode/skills/parser.py`、`tests/test_skill_parser.py`
**依赖：** T2

**步骤：**
1. 在读取说明字段的那段分支里记下本次走的是哪条路：
   frontmatter 提供了非空值 → 真；走正文第一段回填或最终兜底 → 假。
2. 构造 `SkillSpec` 时传入该值。
3. 在 `tests/test_skill_parser.py` 加两条用例，**两条路径各一条**：
   - 显式写了说明字段 → 该标记为真
   - 未写说明字段（由正文第一段回填）→ 该标记为假
4. 再加一条**关键的反向用例**：作者显式写下的说明**恰好与正文第一段完全相同**时，
   标记仍为真（spec AC2a——这正是「不许用重新提取正文再比对」那种启发式的原因）。

**验证：** `python -m unittest tests.test_skill_parser` 全绿，且新增三条用例在其中

---

### T4: `SkillCatalog` 记录跨层覆盖事实

**文件：** `rhinecode/skills/models.py`
**依赖：** 无

**步骤：**
1. 给 `SkillCatalog` 追加字段
   `shadowed: tuple[tuple[str, SkillSource], ...] = ()`，放在末尾。
2. docstring 说明每个元素是 `(命令名, 被覆盖那份所在的层)`，
   并写明**为什么必须在扫描时当场记**——之后被丢弃那份就没有任何引用了，
   这个信息无从推断。
3. 说明「记全部层但体检只用内置那部分」的理由（多记两个元组不花代价，
   将来放宽范围时数据已经在了）。

**验证：** `python -c "from rhinecode.skills.models import SkillCatalog; print(SkillCatalog(skills=(), errors=(), warnings=()).shadowed)"` 输出 `()`

---

### T5: 发现层产生覆盖事实

**文件：** `rhinecode/skills/discovery.py`、`tests/test_skill_discovery.py`
**依赖：** T4

**步骤：**
1. 在 `discover` 的逐层循环里，把 `setdefault` 改成「先判断该名字是否已被占用」：
   已占用 → 记一条 `(命令名, 当前层)` 进覆盖清单；未占用 → 正常放入。
   **加载行为一字不变**——依然静默、依然不记错误（spec N6）。
2. 把覆盖清单填进产出的快照。
3. 在 `tests/test_skill_discovery.py` 加三条用例：
   - 项目级与内置层同名 → 覆盖清单含 `(名字, BUILTIN)`，且生效那份来自项目级
   - 无同名 → 覆盖清单为空
   - 三层都有同名 → 覆盖清单含 `USER` 与 `BUILTIN` 两条

**验证：** `python -m unittest tests.test_skill_discovery` 全绿

---

## 阶段二：体检模块（T6–T11）

### T6: 体检模块骨架与排序契约

**文件：** `rhinecode/skills/audit.py`（新建）、`tests/test_skill_audit.py`（新建）
**依赖：** T1、T2、T4

**步骤：**
1. 新建 `audit.py`，模块 docstring 说明三件事：
   - 职责（已加载的定义 + 覆盖事实 → 建议列表）
   - **为什么坐在 `validation` 与 `manager` 之间**（要调下层的 `grants_for` 与
     `render_active_body`；又必须在持有状态的 `manager` 之下才能保持纯函数）
   - **为什么不叫 `lint_*`**（那两个名字被 `ObsoleteApiRemovedTest` 钉着不许存在，
     它们属于已删除的「白名单收窄」语义，沿用会让人误以为那套东西回来了）
2. 定义唯一的对外函数，接收 Skill 定义序列与覆盖事实，返回 `SkillAdvice` 元组。
   本任务里先返回空元组。
3. 明确排序契约并写进 docstring：**按「Skill 名 → 检查项定义顺序」排序**，
   使输出稳定、可逐条断言。
4. 新建 `tests/test_skill_audit.py`，写一个构造 `SkillSpec` 的测试辅助
   （**全部用字符串字面量，不创建任何文件**——spec N1/AC16），
   并加一条用例：一份各方面都合规的定义产出零建议。

**验证：** `python -m unittest tests.test_skill_audit` 全绿

---

### T7: 检查 1 与检查 2（说明字段族）

**文件：** `rhinecode/skills/audit.py`、`tests/test_skill_audit.py`
**依赖：** T6

**步骤：**
1. **检查 1（说明过长）**：说明字段字符数超过阈值时产出建议。
   建议正文必须包含：具体字符数、建议压到多少、以及
   **「若超出部分在描述『什么时候该用』，请挪进触发说明字段；
   但这不会省下清单预算——两个字段在清单里拼成同一行、共享同一份预算，
   挪动只改善状态列表与命令菜单的可读性」**（spec F2-1 明确要求这句）。
2. **检查 2（有触发说明却没说明字段）**：触发说明非空且 `description_explicit`
   为假时产出建议。**判的是「作者写没写」不是「是不是空的」**——
   在代码注释里写明说明字段永不为空，按空判永远命不中。
3. 测试用例：
   - 超长 → 命中检查 1，且建议文本含「不会省下清单预算」相关措辞
   - 恰好等于阈值 → **不命中**（边界）
   - 有触发说明 + 未显式声明说明 → 命中检查 2
   - 有触发说明 + 显式声明说明 → 不命中
   - **无 frontmatter 的定义**（未显式声明说明、也没有触发说明）→
     **不产出任何建议**（spec AC2b——这条钉住「刻意不查说明是自动提取的」）

**验证：** `python -m unittest tests.test_skill_audit` 全绿

---

### T8: 检查 3（正文不含参数占位符）

**文件：** `rhinecode/skills/audit.py`、`tests/test_skill_audit.py`
**依赖：** T6

**步骤：**
1. 正文中不含占位符常量时产出建议。
2. 建议正文**必须写明「参数不会丢，会被追加到正文末尾的补充段」**——
   spec F2-3 要求，否则会把「位置不对」误导成「功能失效」。
   同时给出改法：想让参数出现在流程中间的某一步，就在那里写占位符。
3. 测试用例：含占位符 → 不命中；不含 → 命中且建议含「不会丢」相关措辞。

**验证：** `python -m unittest tests.test_skill_audit` 全绿

---

### T9: 检查 4 与检查 5（预授权族）

**文件：** `rhinecode/skills/audit.py`、`tests/test_skill_audit.py`
**依赖：** T6

**步骤：**
1. 对每个定义调一次 `validation.grants_for`，两项检查**共用这一次调用结果**。
2. **检查 4（过宽）**：产出的规则中，工具名不在只读白名单内**且**没有参数模式时，
   产出建议。建议里给出带模式的写法范例。
   代码注释写明**判的是翻译后的规则而不是原始声明串**——
   原始串的写法太多（列表 / 分隔串 / 大小写 / 别名），规则只有一种形态。
3. **检查 5（全军覆没）**：原始声明非空**且**产出的规则列表为空时，产出建议，
   并给出一条正确写法范例（含 `Bash(...)` 与 `WebFetch(domain:...)` 两种形态）。
4. 测试用例：
   - 声明裸的命令类 → 命中检查 4
   - 同类别带了参数模式 → 不命中
   - **声明裸的只读类 → 不命中**（spec AC4 明确要求）
   - 声明全是本系统认不出的名字 → 命中检查 5
   - **部分认得、部分认不出 → 不命中检查 5**（spec AC5 明确要求）

**验证：** `python -m unittest tests.test_skill_audit` 全绿

---

### T10: 检查 6（逼近注入上限）

**文件：** `rhinecode/skills/audit.py`、`tests/test_skill_audit.py`
**依赖：** T6

**步骤：**
1. 调 `render.render_active_body` 渲染一次，**参数传空串**，量它的**返回文本**。
2. 渲染返回了截断标记 → 直接判命中（「已经超了」比「逼近」更该报）。
3. 未截断 → 行数或字节数任一达到上限的既定比例时命中。
4. 代码注释写明两件事：
   - **量的是渲染后的整段**（含边界标识、说明行、随附资源清单），
     不是正文原文——截断作用在前者，按后者判会漏报
   - 传空参数会让含占位符的定义量出的长度略小于实际，这是**刻意偏松**的方向
     （阈值本身留了 20% 余量，宁可少报一条也不为正常大小的 Skill 报警）
5. 建议正文按「已超限」与「逼近」两种情形分别措辞，改法指向
   「拆分，或把细节挪进随附资源」。
6. 测试用例：短正文 → 不命中；逼近阈值 → 命中；远超上限 → 命中且措辞为「已超限」；
   **正文本身未达阈值、但加上随附资源清单后达到阈值的目录型定义 → 命中**
   （spec AC6 明确要求这条）。

**验证：** `python -m unittest tests.test_skill_audit` 全绿

---

### T11: 检查 7（覆盖了内置样板）

**文件：** `rhinecode/skills/audit.py`、`tests/test_skill_audit.py`
**依赖：** T6

**步骤：**
1. 覆盖事实中存在 `(该命令名, 内置层)` 时产出建议。
2. 措辞**必须中性**并明确写出「若这是有意定制，忽略本条」——
   spec F2-7 载明这条命中的多数情况是正常用法。改法给「若非有意，请改名」。
3. 测试用例：
   - 覆盖了内置 → 命中
   - 没覆盖任何东西 → 不命中
   - **项目级覆盖用户级**（两边都不是内置）→ **不命中**（spec AC6a 明确要求）
   - 建议文本含「有意定制」相关措辞

**验证：** `python -m unittest tests.test_skill_audit` 全绿

---

## 阶段三：报告集成与清理（T12–T14）

### T12: 删除状态报告的死参数

**文件：** `rhinecode/skills/manager.py`、`rhinecode/conversation.py`
**依赖：** 无（可提前做）

**步骤：**
1. 删掉 `report()` 的 `registered` 参数，以及 docstring 里描述
   「白名单是否等于全集」那套已废止检查的整段文字。
2. 改 `conversation.py` 里唯一的生产调用方，去掉传参
   （**注意 `prompt_report` 仍然需要这个参数，不要一起删**——
   它真的用它来展示当前可见工具集）。
3. 确认测试里的调用本来就是无参形式，无需改动。

**验证：** `python -m unittest discover -s tests` 全绿；
`grep -n "def report" rhinecode/skills/manager.py` 显示签名只剩 `self`

---

### T13: 报告新增建议段

**文件：** `rhinecode/skills/manager.py`
**依赖：** T11、T12

**步骤：**
1. 在 `report()` 的**出锁之后**调体检（spec N3：持锁期间不做任何非纯内存读写；
   体检虽是纯函数，但把它放进临界区会让临界区无谓变长，且违反既有纪律）。
   加一句注释说明这一点。
2. 建议段排在「加载失败 / 警告 / 字段提示」三段**之后**、
   项目级信任提示**之前**。
3. 无建议时**整段不出现**（spec F5）——不打印「无建议」之类的空段落。
4. 存在建议时，段尾附一句指向 `skill-creator` 的入口提示（spec F13），
   让用户知道这些建议可以被代为执行。

**验证：** 手工构造一个带问题的 Skill 目录跑一次报告，观察建议段出现在正确位置
（下一个任务用测试固化）

---

### T14: 报告集成的测试

**文件：** `tests/test_skill_manager.py`
**依赖：** T13

**步骤：**
1. 加一条用例：全部合规的 Skill → 报告中**不出现**建议段的任何标记（F5）。
2. 加一条用例：存在有问题的 Skill → 建议段出现，且其位置在「字段提示」段之后
   （用两段标题在报告文本中的下标先后来断言，不逐字比对整段文本）。
3. 加一条用例：存在建议时，报告含指向 `skill-creator` 的入口提示（F13）。
4. 加一条用例：改动 Skill 定义并热更新后再取报告，建议随之变化（spec AC8/F4，
   钉住「每次现算、不缓存」）。

**验证：** `python -m unittest tests.test_skill_manager` 全绿

---

## 阶段四：`skill-creator`（T15–T18）

### T15: 随附字段参考

**文件：** `rhinecode/skills/builtin/skill-creator/reference.md`（新建）
**依赖：** 无

**步骤：**
1. 写一份完整的字段参考，至少包含：
   - **全部 frontmatter 字段**的语义、取值与缺省（八个字段，见 spec F1 的表）
   - **`allowed-tools` 是预授权不是限制**——单列一段并加重强调，
     这是最容易被外部作者误解、且误解后果最严重的一条
   - **本系统明确不支持的标准字段清单**及各自的实际行为
   - **命令名来自文件系统路径**，`name` 只是显示标签
   - 一份可照抄的最小范例（单文件型）与一份目录型范例
   - 常见错误速查（域名规则漏 `domain:` 前缀、下划线写法的语义变更、
     目录型缺入口文件名）
2. 参考里**不要写死具体行数/字节数阈值**——那些会变，写「有上限、体检会提醒」即可。

**验证：** `python -c "import pathlib; p=pathlib.Path('rhinecode/skills/builtin/skill-creator/reference.md'); print(p.exists(), len(p.read_text(encoding='utf-8')))"` 输出存在且非空

---

### T16: 内置样板入口

**文件：** `rhinecode/skills/builtin/skill-creator/SKILL.md`（新建）
**依赖：** T15

**步骤：**
1. frontmatter：
   - `name: skill-creator`、`description`（**必须 100 字符以内**，否则自己触发检查 1）
   - `when_to_use`：覆盖三种触发说法（「做成 Skill」「帮我适配」「按建议修复」）
   - `allowed-tools`：**只给只读调研**（读文件、检索）。
     **写入与编辑刻意不给**——那正是要用户看一眼的那一步（spec F10 与安全边界）。
     在 frontmatter 上方用注释写明这个取舍，与既有 `commit` 样板同风格
   - **不写 `context`**（留主对话，spec F8）、**不写 `disable-model-invocation`**
2. 正文结构：
   - 开场：三种用途的分流
   - 第 0 步：先读随附的字段参考（说明资源清单会给出绝对路径）
   - 分支 A 创作：问清流程 → **与用户确认命令名** → 写文件 → 提示 `/skills reload`
   - 分支 B 适配：读进来 → 对照参考逐项检查 → 报告差异 → 按需改
   - 分支 C 修复：拿状态报告里的建议 → **逐条问用户采纳与否** → 改
   - 收尾硬规则：
     **默认项目级、不追问层级**（用户明说要全局才写用户级）；
     **内置层永不写入**（随程序分发，用户写进去下次升级就没了）；
     落盘前把完整内容给用户看
3. 正文里含 `$ARGUMENTS`（否则自己触发检查 3）。
4. **控制长度**：正文加上随附资源清单后必须低于检查 6 的阈值，
   否则这个样板自己会触发建议。

**验证：** 启动后 `/skills` 能看到 `skill-creator`，来源标为内置、执行模式为主对话

---

### T17: 分发资源覆盖目录型样板

**文件：** `pyproject.toml`
**依赖：** T16

**步骤：**
1. 把内置样板的资源声明从单层通配改为**同时覆盖单层与两层**
   （显式写两条模式，不用 `**` 递归通配——它对 setuptools 版本有要求，
   而内置样板不会嵌套超过两层）。
2. 在该行上方加一句注释说明为什么需要两条。

**验证：** 构建一个分发包并检视其内容，确认两个新文件都在里面：
`python -m pip wheel --no-deps -w <scratchpad> .`，
再用 `python -c "import zipfile,glob; z=zipfile.ZipFile(glob.glob('<scratchpad>/*.whl')[0]); print([n for n in z.namelist() if 'skill-creator' in n])"`
应列出 `SKILL.md` 与 `reference.md` 两项。
**若本机缺构建工具**：改为直接读取配置确认两条模式都在，并在验收记录里注明
本条未经实际构建验证（spec AC15 要求的是「检视包内文件清单」，不能用
「在本机装一遍看看」这种不可复现的方式替代）。

---

### T18: 内置样板逐一体检并记录

**文件：** 无（只跑不改）
**依赖：** T11、T16

**步骤：**
1. 写一段一次性脚本（放 scratchpad，不进仓库），扫描内置目录、
   对四个内置样板各跑一次体检。
2. 记录每个样板的建议数与内容。
3. 目标是**四个全部零建议**；若有非零项，判断是「样板该改」还是
   「这条检查该收窄」，做出修改并说明理由。
4. **不建立自动化断言**（spec F6/AC9：硬判据会让日后内置样板的正常调整
   被无关测试拦住，而人被拦住时倾向改判据而非改样板）。

**验证：** 脚本输出四个样板各自的建议数；把结果贴进验收记录

---

## 阶段五：文档与收尾（T19–T22）

### T19: 更新项目总纲

**文件：** `CLAUDE.md`
**依赖：** T18

**步骤：**
1. 「已实现的扩展」段补一句 Skill 作者期（体检 + `skill-creator`），
   指向 `docs/extensions/skill-authoring/`。
2. `/skills` 命令说明补上「报告含建议段」。
3. 「成对维护点」**新增两条**：
   - 新增一项体检检查 → 判定枚举 + 体检实现（漏了枚举只能退回脆弱的字符串断言）
   - 新增一个**只读**工具类别 → 预授权别名表 + 只读白名单（
     **漏改的后果是多报一条「预授权过宽」**，这是刻意选的偏严方向，
     但仍要登记，否则下一个人会以为是 bug）
4. 「安全边界」的 Skill 三条之后补一条：体检只观测、不改变任何判定，
   也不读任何文件内容。

**验证：** `grep -n "skill-authoring\|体检" CLAUDE.md` 能找到新增内容

---

### T20: 更新内部分册与扩展索引

**文件：** `docs/internals/architecture.md`、`docs/internals/capabilities.md`、
`docs/internals/testing.md`、`docs/extensions/README.md`
**依赖：** T19

**步骤：**
1. `architecture.md`：skills 层的模块分解加入体检模块，
   说明它在依赖链上的位置及理由。
2. `capabilities.md`：Skill 能力段补「体检的七项检查与各自阈值」
   和「`skill-creator` 的三种用途」——这份文档回答「具体怎么表现」，
   阈值应当写在这里。
3. `testing.md`：新增测试文件的覆盖清单，
   并记一句「内置样板零建议**刻意不建自动化断言**」及理由。
4. `docs/extensions/README.md`：「当前扩展」表加一行，
   状态、日期与一句话说明，格式对齐既有的 web-fetch 那行。

**验证：** 四份文档各自 `grep` 到新增内容；人工通读一遍确认与实现一致

---

### T21: 清理待办目录

**文件：** `docs/todo/`
**依赖：** T20

**步骤：**
1. 删除 `1-skill-authoring.md`（本扩展已完成）。
2. 把 `2-web-search.md` 重命名为 `1-web-search.md`、
   `3-p1b-unattended.md` 重命名为 `2-p1b-unattended.md`。
3. 改两份文档内部的序号标题。
4. 更新 `docs/todo/README.md` 里的清单与序号引用。
5. 全仓库检索这三份文档的**旧路径引用**并一并更新
   （`CLAUDE.md` 与其它文档可能引用了带序号的文件名）。

**验证：** `ls docs/todo/` 只剩 `README.md`、`1-web-search.md`、`2-p1b-unattended.md`；
`grep -rn "1-skill-authoring\|2-web-search\|3-p1b" . --include=*.md` 无残留引用

---

### T22: 全量验证

**文件：** 无
**依赖：** T1–T21 全部

**步骤：**
1. `python -m compileall rhinecode tests`
2. `python -m unittest discover -s tests`
3. 对照 `checklist.md` 逐项走一遍。

**验证：** 编译无错；测试全绿且总数比改动前有增加、skipped 仍为 4

---

## 执行顺序

```
T1 ─┬─ T6 ─┬─ T7 ──┐
T2 ─┤      ├─ T8 ──┤
T4 ─┘      ├─ T9 ──┼─ T13 ─ T14 ──┐
           ├─ T10 ─┤              │
T2 → T3    └─ T11 ─┘              │
T4 → T5 ──────────────────────────┤
T12 ──────────────────────────────┘   （可提前独立完成）
                                  │
T15 → T16 → T17                   │
        └──→ T18 ─────────────────┤
                                  ▼
                     T19 → T20 → T21 → T22
```

**关键路径**：`T1/T2/T4 → T6 → T7–T11 → T13 → T14 → T19 → T20 → T21 → T22`

**可并行的三条支线**：
- `T12`（删死参数）与其它完全无关，随时可做
- `T3`（解析层）与 `T5`（发现层）只依赖各自的数据结构任务
- `T15/T16/T17`（内置样板）不依赖体检实现，但 `T18`（核对零建议）依赖 `T11`
