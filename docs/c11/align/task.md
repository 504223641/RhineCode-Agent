# C11 对齐 Agent Skills 开放标准 Tasks

> 依据已批准的 `spec.md` 与 `plan.md`。共 31 个任务，分六段。

## 文件清单

### 产品代码

| 操作 | 文件 | 职责 |
|---|---|---|
| 修改 | `rhinecode/skills/models.py` | 字段表重定义、删 `SkillMode`、新增无能力字段名单 |
| 修改 | `rhinecode/skills/parser.py` | 键名归一、缺省提取、notices 产出、删名字校验 |
| 修改 | `rhinecode/skills/discovery.py` | 命令名推导、覆盖键更换、删层内重名 |
| 修改 | `rhinecode/skills/render.py` | 清单条目含 `when_to_use` |
| 修改 | `rhinecode/skills/validation.py` | 职责换成预授权规则解析 |
| 修改 | `rhinecode/skills/manager.py` | 激活态去白名单、取预授权规则、报告加 notices |
| 修改 | `rhinecode/skills/__init__.py` | 导出跟随 |
| 修改 | `rhinecode/permission/engine.py` | `turn_rules` 与成对授予/撤销 |
| 修改 | `rhinecode/agent/loop.py` | `excluded` 取代 `ToolPolicy`、加载工具走串行特殊路径 |
| **删除** | `rhinecode/tools/policy.py` | 三元组塌缩后无存在必要 |
| 修改 | `rhinecode/tools/load_skill.py` | fork 分支经回调跑子对话 |
| 修改 | `rhinecode/conversation.py` | 成对授予撤销、fork 回调、分支判据更换 |
| 修改 | `rhinecode/commands/skill_commands.py` | 字段跟随、`user_invocable` 过滤 |
| 修改 | `rhinecode/bootstrap.py` | 清理 `ToolPolicy` 引用与白名单 fail-fast 段 |
| 重写 | `rhinecode/skills/builtin/{commit,review,test}.md` | 新格式与新语义 |

### 测试

| 操作 | 文件 | 说明 |
|---|---|---|
| **删除** | `tests/test_skill_lint.py` | 作者期体检随白名单语义变更失效 |
| **删除** | `tests/test_skill_loop_policy.py` | 白名单收窄用例整体失效，新用例见 T24 |
| 重写 | `tests/test_skill_parser.py` | 23 条 |
| 重写 | `tests/test_skill_discovery.py` | 15 条 |
| 修改 | `tests/test_skill_render.py` | 19 条 |
| 重写 | `tests/test_skill_validation.py` | 14 条 |
| 修改 | `tests/test_skill_manager.py` | 44 条 |
| 修改 | `tests/test_skill_isolated.py` | 22 条 |
| 修改 | `tests/test_skill_commands.py` | 27 条 |
| 修改 | `tests/test_skill_startup.py` | 11 条 |
| 修改 | `tests/test_skill_tui.py` | 16 条 |
| 修改 | `tests/test_skill_sandbox.py` | 12 条 |
| 新建 | `tests/test_perm_turn_grant.py` | 预授权的授予/撤销/边界 |
| 新建 | `tests/test_skill_invocation.py` | 模型发起 fork、两个可调用性开关 |
| 修改 | `tests/test_bootstrap.py`、`tests/test_trace_hooks.py` | 引用清理 |
| 修改 | `tests/e2e/{c11_scenarios,p0_scenarios}.py` | 预置改用新字段 |

---

## 第一段：skills 层数据与解析（T1–T9）

### T1: 重定义 SkillSpec 字段
**文件：** `rhinecode/skills/models.py`　**依赖：** 无
**步骤：**
1. 把 `name` 改名为 `display_name`，新增 `command_name`
2. 新增 `when_to_use`（可空）、`model_invocable`（布尔，缺省真）、`user_invocable`（布尔，缺省真）、`notices`（字符串元组，缺省空）
3. 把 `allowed_tools` 改名为 `granted_tools`（元组，缺省空元组而非 None——预授权没有「未声明=不收窄」这种三态，空即不授权）
4. 用 `forked`（布尔）取代 `mode`
5. 删 `history_messages`

**验证：** `python -c "from rhinecode.skills.models import SkillSpec; print(SkillSpec.__dataclass_fields__.keys())"` 输出含上述全部新字段、不含旧字段

### T2: 删 SkillMode 枚举，新增无能力字段名单
**文件：** `rhinecode/skills/models.py`　**依赖：** T1
**步骤：**
1. 删除 `SkillMode` 枚举及其全部引用点的导入
2. 新增常量：无对应能力的标准字段名单（`background` / `agent` / `effort` / `hooks` / `paths` / `shell`），每项附一句「本版本的实际行为」文案
3. 新增常量：连字符↔下划线的键名归一映射

**验证：** `python -m compileall rhinecode/skills` 通过；`grep -rn "SkillMode" rhinecode/` 无输出

### T3: 解析器键名归一
**文件：** `rhinecode/skills/parser.py`　**依赖：** T2
**步骤：**
1. 解析出 frontmatter 映射后，先做一次键名归一（连字符转下划线）
2. 归一时若同一逻辑键的两种写法同时出现，以连字符版（标准写法）为准并产出一条 notice

**验证：** 单测：`allowed-tools` 与 `allowed_tools` 解析结果一致

### T4: 解析器缺省值与布尔写法
**文件：** `rhinecode/skills/parser.py`　**依赖：** T3
**步骤：**
1. `description` 缺失时从正文提取第一个非空段落（去掉 Markdown 标题行）
2. 布尔字段接受标准规定的多种写法（真/假的多种字面量，大小写不敏感）
3. `context` 取值为 `fork` 时置 `forked` 为真；其它取值产出 notice 并按非 fork 处理

**验证：** 单测：无 frontmatter 的纯正文文件解析成功且 description 非空

### T5: 解析器产出 notices
**文件：** `rhinecode/skills/parser.py`　**依赖：** T4
**步骤：**
1. 遇到下划线写法的旧字段 `allowed_tools` → 追加一条**明确说明语义已变更**的 notice（spec F6，措辞不可淡化）
2. 遇到 T2 名单里的无能力字段 → 各追加一条含「本版本实际行为」的 notice
3. notices 随 SkillSpec 返回

**验证：** 单测：含 `allowed_tools` 的样本产出恰好一条含「语义」二字的 notice

### T6: 删解析器的名字合法性校验
**文件：** `rhinecode/skills/parser.py`　**依赖：** T5
**步骤：**
1. 删除 `name` 的字符集/长度/保留词校验与对应失败原因
2. `name` 缺失不再是失败原因（改为 `display_name` 留空，由发现层回填）

**验证：** 单测：`name: My Skill`（含大写与空格）不再导致加载失败

### T7: 发现层推导命令名
**文件：** `rhinecode/skills/discovery.py`　**依赖：** T6
**步骤：**
1. 目录型取目录名、单文件型取去扩展名的文件名，写入 `command_name`
2. `display_name` 为空时回填为 `command_name`
3. 命令名做最低限度校验：非空、不含路径分隔符、不是保留子命令词；不通过按加载失败记录

**验证：** 单测：`foo/SKILL.md` 中 `name: bar` → `command_name == "foo"`、`display_name == "bar"`

### T8: 发现层覆盖键更换与删层内重名
**文件：** `rhinecode/skills/discovery.py`　**依赖：** T7
**步骤：**
1. 跨层整份覆盖的键从 `name` 换成 `command_name`
2. 删除层内重名的检测与对应失败原因（同目录下命令名天然唯一）

**验证：** 单测：三层同 `command_name` 时项目级生效；旧的层内重名用例整体删除后其余全绿

### T9: 清单条目含 when_to_use
**文件：** `rhinecode/skills/render.py`　**依赖：** T8
**步骤：**
1. 清单条目改为「命令名 + description + when_to_use」，后两者拼接
2. 拼接后一并计入既有的清单字符预算与截断逻辑

**验证：** 单测：声明了 `when_to_use` 的 Skill，其清单条目同时含两段文本

---

## 第二段：权限层预授权（T10–T12）

### T10: 引擎新增 turn_rules
**文件：** `rhinecode/permission/engine.py`　**依赖：** 无（可与第一段并行）
**步骤：**
1. 新增 `turn_rules` 列表，初始为空
2. 第③层合并顺序改为 `turn_rules + session_rules + file_rules`
3. 新增授予（追加一批规则）与撤销（**整体清空**，幂等）两个操作

**验证：** 单测：授予一条 allow 规则后同一请求由 ASK 变 ALLOW；撤销后恢复 ASK

### T11: 预授权不越过前三层
**文件：** `rhinecode/permission/engine.py`　**依赖：** T10
**步骤：**
1. 确认合并点位于黑名单与沙箱**之后**（既有顺序即满足，本任务只加护栏测试）
2. 若不满足则调整合并点位置

**验证：** 单测：授予「放行全部命令」后，危险命令仍被第①层拒绝；越界路径仍被第②层拒绝

### T12: 预授权规则解析
**文件：** `rhinecode/skills/validation.py`　**依赖：** T1
**步骤：**
1. 把 `granted_tools` 的每一项解析成权限规则（工具类别名 + 可选括号模式）
2. 标准中拆分的只读检索工具名映射到本系统的只读类别
3. 无法识别的项：跳过并产出警告，**不 fail-fast**
4. 删除白名单两段校验与作者期体检的全部函数

**验证：** 单测：`Bash(git *)` 解析成一条规则；无法识别项产出警告且其余项照常解析

---

## 第三段：循环与工具（T13–T17）

### T13: RunOptions 塌缩为 excluded
**文件：** `rhinecode/agent/loop.py`　**依赖：** 无
**步骤：**
1. `tool_policy` 回调换成 `excluded: frozenset[str]`（静态值，不需要逐轮求值）
2. 工具 schema 过滤改为按 `excluded` 排除
3. 越界判定改为「是否在 `excluded` 中」

**验证：** 不传 `excluded` 时行为与改造前一致；传入后该工具不在 schema 中且调用被拒

### T14: 删除 tools/policy.py
**文件：** `rhinecode/tools/policy.py`（删除）　**依赖：** T13
**步骤：**
1. 删除该文件
2. 清理各处导入

**验证：** `grep -rn "ToolPolicy" rhinecode/` 无输出；`python -m compileall rhinecode` 通过

### T15: 加载工具改走串行特殊路径
**文件：** `rhinecode/agent/loop.py`　**依赖：** T14
**步骤：**
1. 在工具分桶处识别加载工具名，与既有的澄清/审批特殊工具同路处理——**串行、不进并发桶**
2. 该路径不进权限管线（与既有只读默认放行的实际效果一致）
3. 新增 `run_fork` 回调参数，透传给该路径

**验证：** 单测：模型同轮发起加载工具与另一只读工具时，加载工具在串行段执行

### T16: 加载工具的三种分支
**文件：** `rhinecode/tools/load_skill.py`　**依赖：** T15
**步骤：**
1. `model_invocable` 为假 → 返回结构化错误 + 用户可用入口
2. `forked` 为真 → 经 `run_fork` 回调跑子对话，结论作为工具结果
3. 其余 → 激活正文进动态槽位，返回简短确认（现有行为）

**验证：** 单测：三个分支各产出预期的工具结果形态

### T17: 管理器的激活态与预授权集合
**文件：** `rhinecode/skills/manager.py`　**依赖：** T12
**步骤：**
1. 激活态不再携带白名单，删 `tool_policy()`
2. 新增「取当前激活 Skill 的预授权规则集合」
3. 状态报告新增 notices 段
4. **加锁不变量原样保留**——新方法同样遵守「临界区只做纯内存读写」

**验证：** 单测：既有的跨线程死锁护栏仍通过；报告含 notices 段

---

## 第四段：协调层与命令层（T18–T22）

### T18: 成对授予与撤销
**文件：** `rhinecode/conversation.py`　**依赖：** T10, T17
**步骤：**
1. 每次 `agent.run` 外层包 `try/finally`
2. 进入前授予当前激活 Skill 的预授权规则
3. `finally` 中整体撤销

**验证：** 单测：正常结束、取消、抛异常三种路径下 `turn_rules` 均为空

### T19: fork 回调与分支判据
**文件：** `rhinecode/conversation.py`　**依赖：** T16, T18
**步骤：**
1. 提供「运行一个 fork Skill 并返回结论」的回调，传给 `agent.run`
2. `run_skill` 的分支判据从 `mode` 换成 `forked`
3. 子对话的 `excluded` 含加载工具名（防嵌套）

**验证：** 单测：模型发起 fork Skill 后主历史不变、结论作为工具结果回灌；子对话中加载工具不可见

### T20: 删 history_messages 相关实现
**文件：** `rhinecode/conversation.py`　**依赖：** T19
**步骤：**
1. 删除「取尾部历史并回退到 user 边界」的实现与调用
2. 子对话固定只带自包含调用消息

**验证：** `grep -rn "history_messages" rhinecode/` 无输出；子对话单测断言历史恰好一条

### T21: 命令层字段跟随
**文件：** `rhinecode/commands/skill_commands.py`　**依赖：** T17
**步骤：**
1. `SkillCommandInfo` 的名字字段改用 `command_name`，描述用 `display_name` + `description`
2. `user_invocable` 为假的 Skill 不生成短命令

**验证：** 单测：`user-invocable: false` 的 Skill 不出现在补全候选中，但仍在 `/skills` 列表里

### T22: 装配层清理
**文件：** `rhinecode/bootstrap.py`　**依赖：** T14, T21
**步骤：**
1. 删除白名单 fail-fast 整段（内置工具名笔误校验）
2. 清理 `ToolPolicy` 相关接线
3. **保留**装配顺序注释中仍然成立的部分，删掉已失效的那半段

**验证：** 子进程实跑启动成功；`tests/test_bootstrap.py` 中相关用例更新后全绿

---

## 第五段：样板与端到端预置（T23–T25）

### T23: 重写三个内置样板
**文件：** `rhinecode/skills/builtin/{commit,review,test}.md`　**依赖：** T9
**步骤：**
1. `allowed-tools` 按预授权语义重写（「这些操作免确认」）
2. `review` 的 `mode: isolated` 换成 `context: fork`
3. 各加一条 `when_to_use`
4. 三者均不声明 `disable-model-invocation`（采用标准缺省，模型可自行发起）

**验证：** 全新环境启动后 `/skills` 列出三条、来源为内置、各含 `when_to_use` 内容

### T24: 新建两个测试文件
**文件：** `tests/test_perm_turn_grant.py`、`tests/test_skill_invocation.py`　**依赖：** T11, T16
**步骤：**
1. 预授权：授予/撤销/幂等/三种终止路径/不越过前三层
2. 可调用性：模型发起 fork、`disable-model-invocation` 挡下、`user-invocable` 不进菜单、防嵌套

**验证：** 两个文件各自单跑全绿

### T25: 更新端到端预置
**文件：** `tests/e2e/{c11_scenarios,p0_scenarios}.py`　**依赖：** T23
**步骤：**
1. 预置里的 `mode: isolated` 换成 `context: fork`
2. `allowed_tools` 换成 `allowed-tools` 并按预授权语义调整取值
3. 删除「白名单收窄可观测」相关的预置与剧本（场景 4 作废）
4. 模块 docstring 里关于白名单的告诫改写

**验证：** `python -m compileall tests/e2e` 通过；宿主用例全绿

---

## 第六段：既有测试改造与收尾（T26–T31）

### T26: 删除两个失效测试文件
**文件：** `tests/test_skill_lint.py`、`tests/test_skill_loop_policy.py`（删除）　**依赖：** T12, T13
**验证：** 全量测试无导入错误

### T27: 重写解析与发现测试
**文件：** `tests/test_skill_parser.py`、`tests/test_skill_discovery.py`　**依赖：** T8
**步骤：** 按新字段表与命令名规则重写；补 AC1–AC5 对应用例
**验证：** 两个文件单跑全绿

### T28: 改造渲染、校验、管理器测试
**文件：** `tests/test_skill_render.py`、`tests/test_skill_validation.py`、`tests/test_skill_manager.py`　**依赖：** T17
**步骤：** 清单条目断言加 `when_to_use`；校验测试换成预授权规则解析；管理器测试去白名单、加 notices
**验证：** 三个文件单跑全绿，**跨线程死锁护栏必须仍在**

### T29: 改造子对话、命令、启动、界面、沙箱测试
**文件：** `tests/test_skill_{isolated,commands,startup,tui,sandbox}.py`　**依赖：** T22
**步骤：** 按新字段与新分支判据调整；删除已移除能力的用例（N5：不保留「已废弃但仍断言」）
**验证：** 五个文件单跑全绿

### T30: 文档同步
**文件：** `CLAUDE.md`、`AGENTS.md`、`README.md`、`docs/c11/checklist.md`　**依赖：** T29
**步骤：**
1. Skill 系统段落按新格式与新语义改写
2. 「成对维护点」中失效的条目删除，新增预授权的成对维护点
3. C11 checklist 中因本次改造失效的条目标注为已作废并指向本目录

**验证：** `grep -rn "allowed_tools\|mode: isolated" CLAUDE.md` 无残留

### T31: 全量验证
**依赖：** T30
**步骤：**
1. `python -m compileall rhinecode tests`
2. `python -m unittest discover -s tests`
3. 起一次真实宿主冒烟

**验证：** 编译通过、全量全绿、宿主正常启动并能执行一个内置样板

---

## 执行顺序

```
第一段  T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9
第二段  T10 → T11        （可与第一段并行）
        T12              （依赖 T1）
第三段  T13 → T14 → T15 → T16
        T17              （依赖 T12）
第四段  T18 → T19 → T20 → T21 → T22
第五段  T23 → T24 → T25
第六段  T26 → T27 → T28 → T29 → T30 → T31
```

提交节奏：每段结束提交一次，第六段的 T27–T29 各自单独提交。
