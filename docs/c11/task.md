# C11 Skill 系统 Tasks

> 状态：待批准（2026-07-26，第 1 轮修订：已按独立审查的 2 条阻塞级与 7 条重要问题修订）
> 依据：已批准的 `docs/c11/spec.md` 与 `docs/c11/plan.md`
> 共 69 个任务。每个任务自包含，可不按顺序阅读；「依赖」列出**全部**必须先完成的任务号（不只主要前置）。

## 通用完成条件

每个任务除自身「验证」外，还须满足（对应 spec N7 与 plan 第 8 节）：

- 新增/修改的代码带中文注释，解释**为什么这么做**与**边界条件**，不是复述代码；
- 新增模块的 docstring 说明「职责 + 依赖方向 + 对应的 spec 条款」，与 `permission/` `context/` `memory/` `commands/` 既有风格一致；
- 涉及副作用（写盘、起线程、跨线程调度、持锁）的函数，在 docstring 中显式写出。

## 既有代码的精确影响面（plan 第 6 节已核实，直接引用）

| 被改对象 | 全部调用点 |
|---|---|
| `Agent.run` 的 `dynamic` 参数 | `rhinecode/conversation.py:531`（源码）、`tests/test_perm_loop.py:59`（第 5 个位置参数传 `""`） |
| `build_default_prompt` | `rhinecode/conversation.py:477`、`tests/test_c5_prompt.py:38,58`（均用默认参数，新增关键字参数不影响） |
| `ContextManager.before_request` | `rhinecode/agent/loop.py:199`、`rhinecode/conversation.py:418` |
| `CommandRegistry._specs` 的写入 | `rhinecode/commands/registry.py:95`（`append`）、`:113`（`extend`） |
| **内置命令集合**（新增 `/skills` 会打破） | `tests/test_command_builtins.py`：`EXPECTED_TABLE`（第 22–35 行，写死 12 项）、`test_exactly_twelve_canonical_commands`（第 44 行起，断言 `len == 12`）、`test_command_types_match_approved_table`（第 70 行起，会 KeyError）、`test_argument_hints_and_requires_argument`（第 81 行起，断言「仅 `/resume` 有 argument_hint」） |

> 最后一行是**确定性红灯**，不是「可能受影响」：T41 加一条 `/skills` 会同时打破三处断言。已核对**不受影响**的相关文件：`tests/test_command_tui.py`（补全断言用 `/compa`、`/resu`、`/c`、`/cont`、`/h` 前缀，均不匹配 `/skills`）、`tests/test_command_registry.py`（用自建注册表）、`tests/test_c5_prompt.py`（不断言槽位数量）、`compose_status_text` 的 4 处测试调用（新参数有默认值）。

---

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `rhinecode/tools/policy.py` | `ToolPolicy` 数据类（agent 与 skills 的共同下层） |
| 修改 | `rhinecode/tools/__init__.py` | 加注释固化「不 re-export 子模块」这一无环前提 |
| 修改 | `rhinecode/tools/registry.py` | 新增 `names()` 名字访问器 |
| 新建 | `rhinecode/tools/load_skill.py` | `load_skill` 系统级工具 |
| 新建 | `rhinecode/skills/__init__.py` | 对外导出 `SkillManager` 与关键类型 |
| 新建 | `rhinecode/skills/models.py` | 枚举、数据类、常量、`builtin_skills_dir()` |
| 新建 | `rhinecode/skills/parser.py` | `parse_skill` 纯函数 |
| 新建 | `rhinecode/skills/discovery.py` | `discover` 三层扫描 |
| 新建 | `rhinecode/skills/render.py` | 全部文本产出（纯函数） |
| 新建 | `rhinecode/skills/validation.py` | 白名单两段校验与降级（纯函数） |
| 新建 | `rhinecode/skills/manager.py` | `SkillManager` 编排（持锁） |
| 新建 | `rhinecode/skills/builtin/commit.md` | 样板：共享模式 |
| 新建 | `rhinecode/skills/builtin/review.md` | 样板：独立模式 |
| 新建 | `rhinecode/skills/builtin/test.md` | 样板：共享模式 |
| 修改 | `rhinecode/agent/loop.py` | `RunOptions`、`dynamic` callable、`_schema_for` 过滤 |
| 修改 | `rhinecode/agent/prompt/modules.py` | 新增 priority 140 稳定槽位 |
| 修改 | `rhinecode/agent/prompt/builder.py` | `build_default_prompt` 两个新参数 |
| 修改 | `rhinecode/context/summarize.py` | 抽出 `snap_back_to_user` |
| 修改 | `rhinecode/context/manager.py` | `before_request` 增 `allow_summary` |
| 修改 | `rhinecode/commands/registry.py` | `_builtin_specs`/`_skill_specs` 拆分、`replace_skill_commands`、`has_skill_command` |
| 修改 | `rhinecode/commands/models.py` | `ReportTarget` 两个新值 + 协议三个新方法 |
| 新建 | `rhinecode/commands/skill_commands.py` | `SkillCommandInfo` → `CommandSpec` |
| 修改 | `rhinecode/commands/builtins.py` | `/skills` 命令 |
| 修改 | `rhinecode/conversation.py` | SkillManager 接入、六个领域方法、`_take_tail`、`_run_isolated_skill` |
| 修改 | `rhinecode/tui/app.py` | 控制器方法、报告分支、状态栏、激活通知、提交守卫提示 |
| 修改 | `rhinecode/tui/widgets.py` | `compose_status_text` 增 skill 段 |
| 修改 | `rhinecode/__main__.py` | 新增 A / 新增 B 两段启动接线 |
| 修改 | `pyproject.toml` | package-data 打包 `skills/builtin/*.md` |
| 修改 | `.gitignore` | 说明中登记项目级 `skills/` 可提交 |
| 新建 | `tests/test_skill_parser.py` | T8 |
| 新建 | `tests/test_skill_discovery.py` | T11 |
| 新建 | `tests/test_skill_render.py` | T16 |
| 新建 | `tests/test_skill_validation.py` | T19 |
| 新建 | `tests/test_skill_manager.py` | T27 / T29 |
| 新建 | `tests/test_skill_sandbox.py` | T43b（AC36/AC37，本章唯一的安全边界验收） |
| 新建 | `tests/test_skill_loop_policy.py` | T36 |
| 新建 | `tests/test_skill_commands.py` | T42 |
| 新建 | `tests/test_skill_isolated.py` | T51 |
| 新建 | `tests/test_skill_tui.py` | T56（较 plan 第 6 节增列，承载 AC34/AC35 的 Pilot 验收） |
| 新建 | `tests/test_skill_startup.py` | T59 |
| 修改 | `tests/test_perm_loop.py` | 适配 `dynamic` callable |
| 修改 | `tests/test_command_builtins.py` | `EXPECTED_TABLE` 加入 `/skills`、放宽 `argument_hint` 断言（见上表末行） |
| 修改 | `CLAUDE.md` / `AGENTS.md` / `README.md` | 同步 C11 能力 |

---

# 阶段一：基础类型与共同下层（T1–T5）

## T1: ToolPolicy 数据类

**文件：** `rhinecode/tools/policy.py`（新建）
**依赖：** 无
**步骤：**
1. 新建模块，docstring 说明：本模块定义每轮工具集的收窄策略；它被 `agent/loop.py` 消费、被 `skills/manager.py` 生产，故下沉到两者的共同下层 `tools`；本模块零依赖，是打破 `agent ↔ skills` 潜在环的关键。
2. 定义 `@dataclass(frozen=True) class ToolPolicy`，三个字段：`allowed: Optional[frozenset[str]]`、`exempt: frozenset[str]`、`excluded: frozenset[str]`。
3. 每个字段写注释说明对应的 spec 条款：`allowed` ← F14（None 表示不收窄）、`exempt` ← F8/F15、`excluded` ← F23。
4. **不定义 `UNRESTRICTED` 之类的模块级常量**：`SkillManager` 的四个「不收窄」分支返回的都是 `ToolPolicy(None, {LOAD_SKILL_TOOL}, ∅)`（`exempt` 非空），用不上一个 `exempt` 为空的常量；留一个无人使用的死常量只会误导。

**验证：** `python -c "from rhinecode.tools.policy import ToolPolicy; print(ToolPolicy(None, frozenset(), frozenset()))"` 打印出实例且不报错。

## T2: 固化 tools 包的无环前提

**文件：** `rhinecode/tools/__init__.py`
**依赖：** T1
**步骤：**
1. 在现有 docstring 末尾追加一段注释，写明：本包 `__init__.py` **刻意不导入任何子模块**；`tools.policy` 被 `skills` 依赖、`tools.load_skill` 依赖 `skills`，这组包级互相依赖靠空 `__init__` 才不成环（与既有 `tools ↔ mcp` 同一模式：`tools/mcp_config.py → mcp.auto_config`、`mcp/tool_adapter.py → tools.base`）。
2. 明确警告：若在此处 re-export 任何子模块，`skills.models → tools.policy` 会在 `tools` 包半初始化状态下触发 `load_skill → skills` 回环并 `ImportError`。

**验证：** 文件仍只含 docstring、无 import 语句；`python -c "import rhinecode.tools"` 正常。

## T3: ToolRegistry 名字访问器

**文件：** `rhinecode/tools/registry.py`
**依赖：** 无
**步骤：**
1. 新增方法 `def names(self) -> frozenset[str]: return frozenset(self._tools)`。
2. docstring 说明用途：供 Skill 白名单的启动校验与**每轮**运行期自愈取交集（spec F14/F16）；注明现有 API 没有 `__iter__`，调用方不得写 `for t in registry`。

**验证：** `python -c "from rhinecode.tools.registry import ToolRegistry; r=ToolRegistry.default(); print(sorted(r.names()))"` 输出 7 个内置工具名。

## T4: skills 包骨架与常量

**文件：** `rhinecode/skills/__init__.py`、`rhinecode/skills/models.py`（新建）
**依赖：** T1
**步骤：**
1. 建 `rhinecode/skills/` 目录与 `builtin/` 子目录。
2. `models.py` docstring：说明 skills 包的六模块单向依赖链与本模块处于最底层；只依赖标准库与 `tools/policy.py`。
3. 定义三个枚举：`SkillMode`（SHARED/ISOLATED）、`SkillSource`（PROJECT/USER/BUILTIN，**成员定义顺序即优先级顺序**，注释写明 `discovery` 依赖此顺序）、`DegradeKind`（TRUNCATED/DROPPED，注释写明两者严重程度不同、必须能被用户区分）。
4. 定义 `ActivationStatus` 枚举（ACTIVATED/NOT_FOUND/ISOLATED）。
5. 定义全部常量（plan 3.1）：`NAME_PATTERN`、`RESERVED_SUBCOMMANDS`、`ENTRY_FILENAME`、`LOAD_SKILL_TOOL`、`PLACEHOLDER`、`INDEX_MAX_LINES/BYTES`、`BODY_MAX_LINES/BYTES`、`TOTAL_MAX_LINES/BYTES`、`RESOURCE_LIST_MAX`、`MCP_PREFIX`、`SKILL_MAX_ITERATIONS`。每个常量注明对应的 spec 条款。
6. 定义 `builtin_skills_dir() -> Path`，返回 `Path(__file__).resolve().parent / "builtin"`；docstring 说明为何不用 `importlib.resources`，以及它依赖 pyproject 的 package-data。
7. `__init__.py` 暂时为空 docstring（导出在 T27 补齐）。

**验证：** `python -c "from rhinecode.skills.models import SkillMode, MCP_PREFIX, builtin_skills_dir; print(MCP_PREFIX, builtin_skills_dir().name)"` 输出 `mcp__ builtin`。

## T5: skills 数据类

**文件：** `rhinecode/skills/models.py`
**依赖：** T4
**步骤：**
1. 定义 `SkillSpec`（11 字段，plan 2.3）。给 `allowed_tools` 写注释：`None` = 未声明（不收窄），空 tuple = 声明了但被剔空（F17 降级输入），**两者语义不同不可合并**。
2. 定义 `SkillLoadError`（path/source/reason）。
3. 定义 `SkillCatalog`（skills/errors/warnings，全 tuple）。注释说明：不可变快照使热更新的整体替换天然原子。
4. 定义 `ActiveSkill`（**只有** name/arguments）。注释说明为何 `degrade` 不作为字段（frozen 写不回、整列表 replace 会与加锁范围纠缠，改由 manager 持纯派生字典）。
5. 定义 `SkillCommandInfo`（name/description/mode）。注释说明它存在的意义是切断 `skills → commands` 依赖。
6. 定义 `ActivationResult`（status/name/available_names/entry_hint/degrade）与 `ReloadOutcome`（added/removed/auto_deactivated/dropped_fatal/warnings/errors）。各字段注明消费者。
7. 全部 `@dataclass(frozen=True)`。

**验证：** `python -c "from rhinecode.skills.models import *; s=SkillSpec(name='a',description='b',body='c',mode=SkillMode.SHARED,allowed_tools=None,history_messages=0,model=None,source=SkillSource.USER,entry_path=__import__('pathlib').Path('x'),resource_dir=None,resource_files=());print(s.name)"` 正常构造。

---

# 阶段二：parser（T6–T8）

## T6: frontmatter 切分与 YAML 解析

**文件：** `rhinecode/skills/parser.py`（新建）
**依赖：** T5
**步骤：**
1. 模块 docstring：职责（单份文本 → SkillSpec）、纯函数不碰文件系统、对应 spec F1/F4/F5/F22。
2. 定义签名 `parse_skill(text, path, source, resource_dir, resource_files) -> tuple[Optional[SkillSpec], Optional[str], list[str]]`，docstring 写明「spec 与失败原因恰有一个非 None」。
3. 实现 frontmatter 切分：首行须为 `---`（允许前置空白行），向下找第二个 `---` 行；缺失任一 → 返回失败原因「缺少 YAML frontmatter」。
4. `yaml.safe_load` 解析；`yaml.YAMLError` → 失败「frontmatter 解析失败：<异常首行>」；结果不是 dict → 失败「frontmatter 顶层必须是键值映射」。
5. 正文 = 第二个 `---` 之后的全部内容；`strip()` 为空 → 失败「SOP 正文为空」。

**验证：** 临时脚本喂三份文本（正常 / 无 frontmatter / 坏 YAML），分别得到「解析继续」「缺少 frontmatter」「解析失败」。

## T7: frontmatter 字段校验

**文件：** `rhinecode/skills/parser.py`
**依赖：** T6
**步骤：**
1. `name`：缺失或非 str → 失败；不匹配 `NAME_PATTERN` → 失败「名字不合法（须小写字母开头、仅含小写字母数字连字符、不超过 32 字符）」；在 `RESERVED_SUBCOMMANDS` 中 → 失败「名字使用了保留子命令词 `<词>`」。
2. `description`：缺失或 `strip()` 为空 → 失败。
3. `allowed_tools`：缺省 `None`；非 list 或任一元素非 str → 失败「allowed_tools 必须是字符串列表」；否则**去重保序**转 tuple。
4. `mode`：缺省 `"shared"`；不是 `shared`/`isolated` → 失败。
5. `history_messages`：缺省 0；非 int（注意排除 bool）或 < 0 → 失败。
6. `model`：缺省 None；非 str → 失败。**若 mode 为 SHARED 且 model 非空 → 不失败**，向 warnings 追加「Skill `<name>` 是共享模式，声明的 model 将被忽略（F22）」。
7. 未知键：忽略，不失败不警告（向前兼容，与 c9 笔记 frontmatter 口径一致）。
8. 构造并返回 `SkillSpec`。

**验证：** 临时脚本喂「六键写全」「只写两个必填」「allowed_tools 写成字符串」「name 含大写」「name 为 run」「共享模式带 model」六份文本，结果分别符合上述规则。

## T8: parser 测试

**文件：** `tests/test_skill_parser.py`（新建）
**依赖：** T7
**步骤：** 覆盖 AC1、AC4 与 F5 的解析类失败：
1. 六键写全 → 各字段值与声明一致。
2. 只写 `name` + `description` → 其余取缺省（`allowed_tools is None`、`mode is SHARED`、`history_messages == 0`、`model is None`）。
3. 含未知键 → 加载成功且未知键被忽略。
4. `allowed_tools` 写成字符串 → 失败。
5. `name` 含大写 / 含空格 / 含 `/` / 33 字符 → 各自失败。
6. `name` 取 `reload`/`off`/`prompt`/`run` → 各自失败且原因含「保留子命令词」。
7. 共享模式声明 `model` → 加载成功且 warnings 非空。
8. 无 frontmatter / 坏 YAML / frontmatter 非映射 / 正文为空 → 各自失败且原因可读。
9. `allowed_tools` 含重复项 → 去重且保序。

**验证：** `python -m unittest tests.test_skill_parser -v` 全绿。

---

# 阶段三：discovery（T9–T11）

## T9: 单层扫描

**文件：** `rhinecode/skills/discovery.py`（新建）
**依赖：** T7
**步骤：**
1. 模块 docstring：职责、只读文件系统、fail-safe（N2）、对应 F2/F3/F5/F29。
2. 私有函数 `_scan_layer(directory, source) -> tuple[list[SkillSpec], list[SkillLoadError], list[str]]`。
3. 目录不存在 → 返回三个空列表（**不算错误**）。
4. `sorted(directory.iterdir())` 遍历（注释写明 F29 的层内去重依赖此字典序）：
   - 后缀 `.md` 的文件 → 单文件型，`resource_dir=None`、`resource_files=()`；
   - 目录且含 `SKILL.md` → 目录型，`resource_dir=该目录`；`resource_files` = 对该目录 `rglob("*")` 取**文件**（排除 `SKILL.md` 自身）的相对路径，按字典序、截断到 `RESOURCE_LIST_MAX`；
   - 目录但缺 `SKILL.md` → 记错误「目录型 Skill 缺少 SKILL.md」；
   - 其它文件类型 → 静默跳过。
5. 读文件用 `read_text(encoding="utf-8")`，捕获 `OSError` 与 `UnicodeDecodeError` → 记错误「读取失败：<原因>」，继续下一个。
6. 调 `parse_skill`，失败原因包装成 `SkillLoadError`，warnings 直接透传。

**验证：** 临时脚本造一个含 1 个 `.md`、1 个合法目录型、1 个缺 `SKILL.md` 的目录 → 得到 2 个 spec、1 个 error。

## T10: 层内去重与跨层覆盖

**文件：** `rhinecode/skills/discovery.py`
**依赖：** T9
**步骤：**
1. 在 `_scan_layer` 内维护 `seen: dict[str, Path]`：解析成功后若 `name` 已在 `seen` → 记错误「与同层 `<先到文件名>` 重名」并丢弃后者（F29）。
2. 公开函数 `discover(project_root, user_dir, builtin_dir) -> SkillCatalog`。
3. 按 `SkillSource` 定义顺序确定三个目录：PROJECT=`project_root/.rhinecode/skills`、USER=`user_dir/skills`、BUILTIN=`builtin_dir`。
4. 逐层调 `_scan_layer`，用全局 `chosen: dict[str, SkillSpec]` 做 `setdefault`——先到（高优先层）保留；低优先层同名直接丢弃且**不记错误**（注释写明：覆盖是正常行为，不是失败，F3）。
5. 汇总：`skills` 按 name 排序转 tuple，errors 与 warnings 按发现顺序转 tuple，构造 `SkillCatalog`。

**验证：** 临时脚本造三层同名 Skill → `discover` 返回的那份 `source is SkillSource.PROJECT`；同层造两个同名 → 字典序在前者生效、另一个进 errors。

## T11: discovery 测试

**文件：** `tests/test_skill_discovery.py`（新建）
**依赖：** T10
**步骤：** 用 `tempfile.TemporaryDirectory` 造目录树，覆盖 AC2、AC3、AC5、AC31：
1. 单文件型与目录型都被发现；目录型正确定位 `SKILL.md` 并记录 `resource_dir`。
2. `name` 与文件名不一致时以 `name` 为准。
3. 目录型的 `resource_files` 含随附文件、不含 `SKILL.md`、按字典序、超过 `RESOURCE_LIST_MAX` 时被截断。
4. 三层同名 → 项目级生效，且 `body` 与 `allowed_tools` **整份**来自项目级（造两层字段互补的样本反证不做字段合并）。
5. 低优先层被覆盖时**不产生** error。
6. 四种坏样本（坏 YAML / 缺必填 / 名字非法 / 目录缺入口）同时存在 → 其余 Skill 全部可用，errors 含四条各自原因。
7. 层内同名 → 字典序在前生效，另一条 error 原因含「与同层」。
8. 目录不存在 → 不报错、不产生 error。

**验证：** `python -m unittest tests.test_skill_discovery -v` 全绿。

---

# 阶段四：render（T12–T16）

## T12: 第一阶段清单渲染

**文件：** `rhinecode/skills/render.py`（新建）
**依赖：** T5
**步骤：**
1. 模块 docstring：所有「给模型看的文本」集中于此，纯函数无状态，便于测试与调整措辞；对应 F6/F9/F10/F12/F13/F24。
2. 私有 `_truncate(text, max_lines, max_bytes) -> tuple[str, bool]`：按行截断与按 UTF-8 字节截断双重生效，**字节截断不得产生非法 UTF-8**（沿用 c9 笔记索引的既有做法）；返回是否发生截断。
3. `render_index(skills) -> str`：头部一句说明「以下 Skill 可用。共享模式可用 `load_skill` 工具加载；独立模式请建议用户执行其命令。」；每行 `- <name>（共享/独立）：<description>`；套 `INDEX_MAX_LINES/BYTES`，截断时末尾追加「（另有 N 个未列出）」。
4. 空列表 → 返回空串（使槽位整体跳过，保证 N3）。

**验证：** 临时脚本传 3 个 spec → 输出含 3 行且无正文；传 300 个 → 被截断且含「另有」。

## T13: 参数替换与资源清单

**文件：** `rhinecode/skills/render.py`
**依赖：** T12
**步骤：**
1. `substitute(body, arguments) -> str`：含 `$ARGUMENTS` → `body.replace(PLACEHOLDER, arguments)`（**替换全部出现处**）；不含且 `arguments.strip()` 非空 → 末尾追加 `\n\n## 用户补充参数\n\n<arguments>`；不含且参数为空 → 原样返回。注释写明「参数原样保留、不做 shell 分词、不做任何模板求值」（F12）。
2. `render_resources(spec) -> str`：`resource_dir` 为 None → 空串；否则输出资源目录**绝对路径** + 相对路径清单 + 一句「工作区外的资源目录不支持 glob/grep 枚举，请按上述清单直接读取」（F13）。

**验证：** 临时脚本验证含/不含占位符两种正文、参数含空格与引号时原样保留、目录型 spec 输出含绝对路径与清单。

## T14: 激活正文渲染与两种降级

**文件：** `rhinecode/skills/render.py`
**依赖：** T13
**步骤：**
1. `render_active_body(spec, arguments) -> tuple[str, Optional[DegradeKind]]`：拼「边界标识（含 Skill 名，使模型能分辨哪段属于哪个，F10）+ `substitute` 后的正文 + `render_resources`」；对结果套 `BODY_MAX_LINES/BYTES`，超出则切断并在末尾标注「（正文已截断）」，返回 `DegradeKind.TRUNCATED`。
2. `render_active_section(items) -> tuple[str, dict[str, DegradeKind]]`：按顺序对每项调 `render_active_body`；累加时若加入当前段会超 `TOTAL_MAX_LINES/BYTES`，则**当前段整段丢弃**、记 `DROPPED`，且**其后所有段一并 DROPPED**（不再尝试塞小的，保持顺序语义）。
3. 注释写明决策 10 的理由：切半会让模型执行残缺流程，整段丢弃至少是可判定状态。
4. 空 items → 返回 `("", {})`。

**验证：** 临时脚本造 1 个超长正文 → `TRUNCATED`；造 3 个各 10KB 的 → 第 3 个（及之后）为 `DROPPED`；空列表 → 空串。

## T15: 自包含语义文本

**文件：** `rhinecode/skills/render.py`
**依赖：** T12
**步骤：**
1. `render_invocation_text(spec, arguments) -> str`，**必含三项**（F24/S-5）：Skill 名、其一句话说明、原样参数。格式如：
   ```
   执行 Skill /<name>（<description>）
   参数：<arguments 或「无」>
   ```
2. docstring 写明三个消费者：主历史的 user 消息 content、独立模式子对话的首条 user 消息（F20 同源复用）、以及二者必须逐字一致的理由。

**验证：** 临时脚本验证有参/无参两种输出都含名字与说明。

## T16: render 测试

**文件：** `tests/test_skill_render.py`（新建）
**依赖：** T15
**步骤：** 覆盖 AC6、AC9（渲染侧）、AC12、AC13：
1. 清单含名字/说明/模式、不含任何正文；超上限被截断并标注省略条数；空列表返回空串。
2. `substitute`：含占位符时被替换、参数含空格与引号原样保留、多处占位符全部替换；不含占位符且参数非空 → 出现在末尾补充段；参数为空 → 正文不变。
3. `render_resources`：目录型含绝对路径与清单；单文件型返回空串。
4. `render_active_section`：两个 Skill 的边界标识可区分；单体超限 → TRUNCATED；总量超限 → 当前及其后 DROPPED；返回的 dict 精确区分两种降级。
5. 字节截断不产生非法 UTF-8（造含多字节中文的正文，`.encode()` 往返成功）。
6. `render_invocation_text` 含名字、说明、参数三项。

**验证：** `python -m unittest tests.test_skill_render -v` 全绿。

---

# 阶段五：validation（T17–T19）

## T17: 内置工具名校验与豁免提示

**文件：** `rhinecode/skills/validation.py`（新建）
**依赖：** T5
**步骤：**
1. 模块 docstring：职责（F16 两段校验 + F17 降级）、纯函数、输入是名字集合不接触注册中心对象。
2. 定义 `@dataclass(frozen=True) class FatalToolName`（skill_name/path/tool_name）。
3. `check_builtin_tool_names(skills, known) -> list[FatalToolName]`：遍历每个 spec 的 `allowed_tools`，**不以 `MCP_PREFIX` 开头**且不在 `known` 中的名字全部收集为致命项。docstring 写明 `known` 的组成是「工具注册中心当前全部名字 ∪ `{ask_user, present_plan}`」，且 `load_skill` 已在注册中心中、无需特判。
4. `collect_exempt_notices(skills, exempt) -> list[str]`：对声明了 `exempt` 内名字（`load_skill`/`ask_user`/`present_plan`）的 Skill 产出提示「Skill `<name>` 声明的 `<tool>` 不受白名单影响，声明无效果，可删除」。
5. 提供 `format_fatal_message(fatals) -> str`，输出 plan 3.5 规定的模板，**含「若确认该工具名无误，可能是 RhineCode 版本与该 Skill 不匹配」一句**。

**验证：** 临时脚本传一个白名单含 `read_fil`（笔误）的 spec → 得到 1 个致命项；含 `mcp__x__y` → 不算致命；含 `ask_user` → 产出豁免提示而非致命项。

## T18: MCP 名字剪枝与空集降级

**文件：** `rhinecode/skills/validation.py`
**依赖：** T17
**步骤：**
1. `prune_mcp_tool_names(skills, registered) -> tuple[list[SkillSpec], list[str]]`。
2. 对每个 spec：`allowed_tools` 为 None → 原样保留；否则剔除「以 `MCP_PREFIX` 开头且不在 `registered` 中」的名字，产出警告「Skill `<name>` 的白名单项 `<tool>` 对应的 MCP Server 未连接，已剔除」。
3. 剔除后为空 tuple → 把 `allowed_tools` 置回 `None`（F17 降级），追加警告「Skill `<name>` 的白名单已全部失效，本次不收窄工具集」。
4. 用 `dataclasses.replace` 产出新 spec（保持 frozen 语义）。

**验证：** 临时脚本传白名单 `["read_file", "mcp__a__b"]` 且 `registered` 不含后者 → 新 spec 的 `allowed_tools == ("read_file",)` 且有 1 条警告；传白名单只含 `mcp__a__b` → `allowed_tools is None` 且有 2 条警告。

## T19: validation 测试

**文件：** `tests/test_skill_validation.py`（新建）
**依赖：** T18
**步骤：** 覆盖 AC16（纯逻辑部分）、AC17：
1. 不存在的内置工具名 → 致命项含 skill 名、路径、工具名。
2. `mcp__` 前缀的不存在名字 → **不**产生致命项。
3. `ask_user` / `present_plan` / `load_skill` → 产出豁免提示、不产生致命项。
4. `mcp_add_server`（**单**下划线内置工具）在 `known` 中 → 既不致命也不豁免，正常通过（对应 AC16 的第三分支）。
5. `format_fatal_message` 输出含路径、工具名与版本错配提示。
6. 剪枝：部分剔除 / 全部剔除（降级为 None）两种情形与警告条数。
7. `allowed_tools is None` 的 spec 经剪枝后仍为 None、不产生警告。

**验证：** `python -m unittest tests.test_skill_validation -v` 全绿。

---

# 阶段六：SkillManager（T20–T27）

## T20: SkillManager 骨架、状态与 empty()

**文件：** `rhinecode/skills/manager.py`（新建）
**依赖：** T10, T14, T18
**步骤：**
1. 模块 docstring：唯一持有可变状态与副作用编排；**并把 plan 3.6 的「加锁不变量」总纲原文写进 docstring**——「临界区只包含纯内存状态读写；一切解析、渲染、回调、IO 都在锁外」，并附四步死锁链路说明（`call_from_thread` 阻塞 → 主线程 `status_segment` 申请同锁）。
2. `__init__(project_root, user_dir, builtin_dir, has_short_command, notify_activation=None)`。
3. 五项状态：`_catalog`、`_active`、`_degrades`、`_runtime_warnings`、`_lock`。锁的注释精确写成「保护 `_active` / `_degrades` / `_runtime_warnings`；`_catalog` 是不可变快照，读引用无需持锁、替换在锁内」。
4. `notify_activation` 作为可写属性（由 TUI 在 `on_mount` 注入，与 c9 `memory_manager.notify` 同形态）。
5. `@classmethod empty(cls)`：构造一个空 `SkillCatalog`、三个目录传不存在的哑路径、`has_short_command` 取 `lambda _: False`，**不调 `discover`**。docstring 写明它是 Null Object，供协调层缺省注入，避免在 `ConversationManager` 构造时扫盘（否则既有测试会隐式读用户主目录）。
6. `get(name)`、`command_infos()` 两个简单查询。

**验证：** `python -c "from rhinecode.skills.manager import SkillManager; m=SkillManager.empty(); print(m.get('x'), m.command_infos())"` 输出 `None ()`，且过程中无任何文件系统访问。

## T21: startup 与 bind_tools

**文件：** `rhinecode/skills/manager.py`
**依赖：** T20
**步骤：**
1. `startup(known) -> list[FatalToolName]`：调 `discover` 得新 catalog（锁内替换 `_catalog`，扫盘在锁外）；把 catalog 的 warnings 并入 `_runtime_warnings`；调 `check_builtin_tool_names` 与 `collect_exempt_notices`，后者并入警告；返回致命项列表（**不自行退出**，由 `__main__` 决定）。
2. `bind_tools(registered) -> None`：调 `prune_mcp_tool_names`，用新 spec 列表重建 `_catalog`（`dataclasses.replace`），警告并入 `_runtime_warnings`。
3. `project_skill_notice() -> Optional[str]`：catalog 中 `source is PROJECT` 的 Skill 非空时，返回「发现 N 个项目级 Skill（来自 `<目录>`）：a、b、c」；否则 None。docstring 写明这是 N8 的**零状态**方案——每次启动都提示，不做「只提示一次」的持久化，理由是仓库新增 Skill 时状态不失效会导致静默吞掉。

**验证：** 临时脚本造含项目级 Skill 的目录树 → `startup` 后 `project_skill_notice()` 非空且含名字与目录。

## T22: activate / deactivate / clear_active

**文件：** `rhinecode/skills/manager.py`
**依赖：** T21
**步骤：**
1. `activate(name, arguments) -> ActivationResult`，严格按 plan 3.6 的**四段式**实现，代码里用注释标出每段边界：
   - ① **锁外**：读 `_catalog` 解析 spec；None → 返回 `NOT_FOUND`（附 `available_names`）。
   - ② **锁外**：`mode is ISOLATED` → 调 `has_short_command(name)` 得 `entry_hint`（True → `/<name>`；False → `/skills run <name>`），返回 `ISOLATED`。注释写明该分支不动可变状态、且含跨层回调，必须在锁外。
   - ③ **锁内**：已在 `_active` → 用 `dataclasses.replace` 原地更新 arguments、**位置不动**（F30）；否则 append。
   - ④ **锁外**：调 `notify_activation`（外包 try/except，回调失败绝不影响激活结果）；返回 `ACTIVATED`（`degrade` 取 `_degrades.get(name)`）。
2. `deactivate(name)`：name 为 None → 清空 `_active` 与 `_degrades`，返回「已卸载全部 N 个 Skill」；否则移除指定项，不存在时返回「Skill `<name>` 当前未激活」。
3. `clear_active()`：清空 `_active` 与 `_degrades`（供 `/clear`，F11）。

**验证：** 临时脚本：激活同一 Skill 两次 → `_active` 只有一条且 arguments 为第二次的值；激活独立模式 Skill → 得到 `ISOLATED` 且 `entry_hint` 正确；`notify_activation` 抛异常 → `activate` 仍返回 `ACTIVATED`。

## T23: index_text 与 active_text

**文件：** `rhinecode/skills/manager.py`
**依赖：** T22
**步骤：**
1. `index_text()`：取 `_catalog.skills` 调 `render_index`。
2. `active_text()` 按 plan 3.6 的**分段加锁**实现：
   - 持锁取 `_active` 的浅拷贝与 `_catalog` 引用 → **出锁**；
   - 出锁后逐条 `get(name)` 取当前 spec（热更新后自动是新正文，F27），组装 items 调 `render_active_section`（渲染在锁外，纯函数可能处理数万字符）；
   - 再持锁把降级字典写回 `_degrades`，**只保留仍在 `_active` 中的名字**（S-1：两段临界区之间若 `deactivate`/`reload` 摘掉了某项，无条件赋值会留下幽灵标记）。
3. 注释写明：本方法每轮被调用，除刷新 `_degrades` 外无副作用。

**验证：** 临时脚本激活两个 Skill → `active_text()` 含两段且边界可辨；`deactivate` 一个后再调 → 只剩一段且 `_degrades` 无幽灵键。

## T24: tool_policy 与 isolated_policy

**文件：** `rhinecode/skills/manager.py`
**依赖：** T22
**步骤：**
1. `tool_policy(registered) -> ToolPolicy`，按 plan 3.6 的四分支：`_active` 空 → 不收窄；任一激活 Skill 的 `allowed_tools is None` → 不收窄（注释引用 N10 的塌缩说明）；否则 `allowed = 各白名单并集 ∩ registered`（**运行期自愈**，注释写明它服务于 C7 的运行时 MCP 重载）；`allowed` 为空 → 不收窄（F17）。三个分支返回的 `exempt` 均为 `{LOAD_SKILL_TOOL}`、`excluded` 均为空。
2. `isolated_policy(spec, registered) -> ToolPolicy`：同上但只看这一个 spec，且 `exempt=frozenset()`、`excluded={LOAD_SKILL_TOOL}`（F21/F23）。

**验证：** 临时脚本：无激活 → `allowed is None` 且 `exempt` 含 `load_skill`；激活两个各带白名单 → `allowed` 为并集；再激活一个无白名单 → `allowed is None`；`isolated_policy` 的 `excluded` 含 `load_skill` 且 `exempt` 为空。

## T25: reload

**文件：** `rhinecode/skills/manager.py`
**依赖：** T23, T24
**步骤：**
1. `reload(known, registered) -> ReloadOutcome`。
2. 重新 `discover` → 跑 `check_builtin_tool_names`：**致命项在这里不退出进程**，而是把对应 Skill 从新 catalog 中丢弃、记入 `dropped_fatal`，并产出警告——警告文案**必须包含**「该 Skill 已被本次热更新丢弃；**下次启动时此错误会导致启动失败**，请尽快修正」（决策 20 / S-c）。注释写明该差异的依据：F16/N2 的定语锚定「启动」，F26 只要求重跑校验，F31 明确热更新不影响运行中的循环。
3. 跑 `prune_mcp_tool_names`；替换 `_catalog`；重置 `_runtime_warnings`。
4. 对比新旧 name 集合得 `added` / `removed`；对「已激活但在新 catalog 中消失」的 Skill 自动从 `_active` 移除并记入 `auto_deactivated`（F27）。
5. 仍存在的激活项保持激活、位置与 arguments 不变（正文更新由 `active_text()` 现取自动完成，F27）。
6. 返回 `ReloadOutcome`。

**验证：** 临时脚本：新增文件后 reload → `added` 含它；删除一个已激活的后 reload → `removed` 与 `auto_deactivated` 都含它且 `_active` 已移除；改正文后 reload → `active_text()` 输出新正文。

## T26: 报告与状态段

**文件：** `rhinecode/skills/manager.py`
**依赖：** T25
**步骤：**
1. `report() -> str`（`/skills`）：逐条列出名字、来源层级、说明、执行模式、是否已激活、短命令是否可用（调 `has_short_command`，**在锁外**）、正文是否被截断（读 `_degrades`，按 `DegradeKind` 分别措辞为「正文被截断（超单体上限）」/「未注入（超总量上限）」）；随后列出加载失败的 Skill 及原因、以及全部 `_runtime_warnings`。
2. `prompt_report(registered) -> str`（`/skills prompt`）：输出三段——第一阶段清单（`index_text()`）、已激活正文（`active_text()`，可截断展示）、当前生效的可见工具集（由 `tool_policy(registered)` 推导，`allowed is None` 时显示「未收窄（全部工具可见）」）。
3. `status_segment() -> Optional[str]`（F32）：`_active` 为空 → None；否则 `f"Skill:{len(self._active)}"`。注释写明**不含方括号**是为绕开 Textual markup 转义坑。
4. 三个方法均遵守「持锁取快照 → 出锁渲染」。

**验证：** 临时脚本调三个方法，输出分别含预期字段；无激活时 `status_segment()` 返回 None。

## T27: SkillManager 测试与包导出

**文件：** `tests/test_skill_manager.py`（新建）、`rhinecode/skills/__init__.py`
**依赖：** T26
**步骤：**
1. `__init__.py` 导出 `SkillManager`、`SkillMode`、`SkillSource`、`SkillSpec`、`SkillCommandInfo`、`ActivationStatus`、`builtin_skills_dir`。
2. 测试覆盖 AC11、AC14、AC17、AC27、AC32、AC38 的逻辑层，以及 A1 的加锁不变量：
   - `empty()` 不触发任何文件系统访问（用 monkeypatch 断言 `discover` 未被调用）。
   - 幂等激活（AC32）：连续两次 → 一条记录、位置为首次、参数为第二次。
   - 独立模式激活 → `ISOLATED` 且 `entry_hint` 在「短命令可用/不可用」两种桩下分别为 `/x` 与 `/skills run x`。
   - `deactivate(name)` / `deactivate(None)` / `clear_active()` 行为。
   - `tool_policy` 四分支（AC14/AC17）。
   - `isolated_policy` 的 `excluded`/`exempt`。
   - reload 的 added/removed/auto_deactivated/dropped_fatal，以及「下次启动会失败」文案存在（AC27）。
   - `project_skill_notice()` 非空且**连续调用两次都非空**（AC38 的零状态断言）。
   - **加锁不变量回归**（A1 死锁的唯一自动化护栏，配方必须完整，否则这条护栏本身不可靠）：注入一个 `notify_activation` 桩，桩内**另起一个 daemon 线程**去调 `manager.status_segment()`（一个会读 `_active` 的公开方法），并 `join(timeout=2)` 等它；记录「是否在超时内返回」与「桩被调用次数」。断言两件事：
     1. 桩**确实被调用过**（次数 ≥ 1）——否则走了 `NOT_FOUND`/`ISOLATED` 早返回时断言一次都不执行，测试会**空过**；
     2. 那个子线程**在超时内完成**——证明回调发生时锁已释放。
     **为什么必须跨线程、不能在桩自己线程内直接调**：若同线程调用，一旦将来把 `Lock` 换成 `RLock`，同线程重入会被放行、测试照绿，而生产环境的死锁是**跨线程**的（`activate` 在 Worker 线程持锁，`call_from_thread` 让主线程去读同一份状态），`RLock` 对跨线程毫无帮助——死锁依旧。跨线程版本对任何锁实现都成立，且不必去断言 `_lock` 的具体类型（那是实现细节，换成队列或别的原语时会失效）。
     **子线程必须 `daemon=True` 且用 `join(timeout=...)`**：真出死锁时不能把整个测试进程一起挂死。
   - 同样方式验证 `has_short_command` 也在锁外被调用（桩内跨线程读一次状态并带超时）。
   - `notify_activation` 抛异常 → `activate` 仍返回 `ACTIVATED`。
   - **AC28 开箱可见**：不传项目级/用户级目录（或传空目录）、只用真实 `builtin_skills_dir()` 调 `discover` → 返回三个内置样板且 `source is SkillSource.BUILTIN`，且它们出现在 `index_text()` 与 `report()` 中。

**验证：** `python -m unittest tests.test_skill_manager -v` 全绿。

---

# 阶段七：load_skill 工具（T28–T29）

## T28: LoadSkillTool

**文件：** `rhinecode/tools/load_skill.py`（新建）
**依赖：** T22
**步骤：**
1. 模块 docstring：说明这是 F7 的第二阶段加载入口、F8 的系统级工具。
2. `class LoadSkillTool(Tool)`：`name = "load_skill"`、`read_only = True`、`parameters` 含 `name`（必填）与 `arguments`（可选）。
3. **`read_only = True` 的注释必须写全**：它是该工具在默认权限模式下免确认的唯一前提（`permission/engine.py:106-108` 的只读简化分支——只读工具在规则未命中时直接 ALLOW、不进模式层）；代价是它会进只读并发桶，因此 `SkillManager` 内部必须加锁。**改动此标志前须同时评估这两点。**
4. `description` 面向模型编写：说明何时该调用、共享模式与独立模式的区别、以及独立模式需建议用户执行命令。
5. `__init__(manager)` 注入 `SkillManager`。
6. `execute(args)`：调 `manager.activate(name, arguments)`，按 `ActivationStatus` 三态返回：
   - ACTIVATED → `ok=True`，output「已激活 Skill `<name>`，其指令已注入上下文。」；若 `degrade` 非空则附加降级说明；
   - NOT_FOUND → `ok=False`，output 含可用名字列表；
   - ISOLATED → `ok=False`，output 说明该 Skill 为独立模式、需由用户触发，并给出 `entry_hint`（**不得指向不存在的命令**）。
7. 自行兜底全部异常转 `ToolResult(ok=False)`（Tool 契约）。
8. 在类 docstring 中写明 plan 决策 13：**有意不在 `permission/adapter.py` 的 `_TOOL_MAP` 中登记**——落 `other` 分支仍走规则层与模式层、不漏检，而它没有可映射的 Bash/Read/Edit/Write 语义，强行映射反而制造误导。写明这一条是为了避免 review 反复追问。

**验证：** 临时脚本用 Fake SkillManager 驱动三态，各自输出符合预期；`read_only is True`。

## T29: load_skill 权限与工具测试

**文件：** `tests/test_skill_manager.py`（追加）
**依赖：** T28, **T27**（本任务是向 T27 创建的文件追加用例）
**步骤：**
1. 断言 `LoadSkillTool().read_only is True`。
2. 用真实 `PermissionEngine`（DEFAULT 模式）跑 `to_request` + `decide`，断言结果是 `ALLOW` 而非 `ASK`——**这是 AC8 的前提，也是 `read_only` 不能被误改的回归护栏**。
3. 三态输出断言（AC7）：ACTIVATED 结果是简短确认**而非完整正文**；NOT_FOUND 含可用名字；ISOLATED 含 `entry_hint` 且短命令不可用时为 `/skills run <name>`。

**验证：** `python -m unittest tests.test_skill_manager -v` 全绿。

---

# 阶段八：agent 层改造（T30–T36）

## T30: RunOptions 与 dynamic callable

**文件：** `rhinecode/agent/loop.py`
**依赖：** T1
**步骤：**
1. 定义 `@dataclass(frozen=True) class RunOptions`（四字段，plan 2.7）。`tool_policy` 类型是 `Optional[Callable[[], ToolPolicy]]`——**注释必须写明为何是 callable**：与 `dynamic` 同理，模型可能在第 N 轮激活 Skill，第 N+1 轮工具集就该收窄；取值型会让 F14 的运行期自愈与 AC14 失效。
2. `run()` 的 `dynamic: str` 改为 `dynamic: Callable[[], str]`，docstring 说明「每轮求值一次」及其理由（改造点 1）。
3. 循环体内 `build_system_reminder(dynamic(), toggle)`。
4. `run()` 新增末位参数 `options: RunOptions = RunOptions()`，docstring 写明**默认值即 C10 行为，不传等于零回归**（N3 的实现保障）。

**验证：** `python -m compileall rhinecode/agent/loop.py` 通过。

## T31: 工具集过滤

**文件：** `rhinecode/agent/loop.py`
**依赖：** T30
**步骤：**
1. `_schema_for(plan_mode, execution_phase, policy)` 改为 plan 4.2(b) 的**显式分支**写法（先算 `planning`，再取 base，过滤，最后按 `planning` 决定是否拼 `plan_schemas()`）。
2. 新增静态方法 `_visible(name, policy) -> bool`：`excluded` → False；`exempt` → True；`allowed is None` → True；否则 `name in allowed`。
3. 注释写明：`plan_schemas()` 在过滤**之后**拼接，因此 `ask_user`/`present_plan` 天然不受白名单影响（F15）。
4. 主循环调用处改为 `self._schema_for(plan_mode, execution_phase, options.tool_policy() if options.tool_policy else None)`——**每轮求值**。

**验证：** `python -m compileall` 通过；临时脚本用假 registry 验证过滤结果。

## T32: RunOptions 三处接线

**文件：** `rhinecode/agent/loop.py`
**依赖：** T31, **T34**（第 2 步要用 `before_request` 的新签名，先做 T34 否则 TypeError）
**步骤：**
1. 迭代上限：`for iteration in range(1, options.max_iterations + 1)`，超限文案里的数字也改用 `options.max_iterations`（N6）。
2. C8 第一层：`context_manager.before_request(history, allow_summary=options.allow_summary)`。
3. 锚点：`if options.record_usage and context_manager is not None and collector.usage is not None: record_usage(...)`。

**验证：** `python -m compileall` 通过。

## T33: 系统提示两个槽位

**文件：** `rhinecode/agent/prompt/modules.py`、`rhinecode/agent/prompt/builder.py`
**依赖：** 无
**步骤：**
1. `optional_slots()` 新增 `PromptModule(name="可用 Skill 清单", priority=140, cacheable=True, content="")`。注释写明 **priority 140 而非 115 的理由**：稳定段是前缀缓存的作用对象，排最后使热更新只失效清单自己那段、不波及 130 的记忆索引（F6）。
2. `build_default_prompt` 增两个关键字参数 `skill_index=""`、`active_skills=""`；前者 `cacheable=True`/priority 140，后者 `cacheable=False`/priority 120。
3. 更新 `optional_slots()` 的跳过列表（现已含「自定义指令」「长期记忆」，追加「可用 Skill 清单」「已激活 Skill」），避免重复添加空槽。
4. 更新 `build_default_prompt` 的 docstring，说明四个槽位各自的通道与理由。

**验证：** `python -m unittest tests.test_c5_prompt -v` 全绿（既有测试用默认参数，应不受影响）；临时脚本传非空 `skill_index` → 出现在 `stable`，传非空 `active_skills` → 出现在 `dynamic`。

## T34: ContextManager 的 allow_summary

**文件：** `rhinecode/context/manager.py`
**依赖：** 无
**步骤：**
1. `before_request(self, history, allow_summary: bool = True)`。
2. 条件改为 `if allow_summary and not self._circuit_broken and self._estimate(history) > ...`——**`allow_summary` 必须在 `and` 链最前面短路**。
3. 注释写明理由：`_estimate` 依赖的锚点对应**主历史**，子对话传入的是另一条短历史，用主历史锚点估算它会得到无意义的值；最前短路使 `_estimate` 根本不会被调用。
4. 追加两项已评估结论的注释：`_offloaded` 幂等集合共享无害（键是全局唯一的 `tool_call_id`）；`_consecutive_failures` 熔断计数因子对话不摘要而不会被污染。

**验证：** `python -m unittest tests.test_context_manager -v` 全绿。

## T35: 抽出 snap_back_to_user

**文件：** `rhinecode/context/summarize.py`
**依赖：** 无
**步骤：**
1. 新增 `def snap_back_to_user(history, idx) -> Optional[int]`：从 idx 向前找最近的 `role == "user"` 下标；**找不到返回 None**；**空序列或 idx 越界一律返回 None**（S-b）。
2. 改写 `compute_retain_index` 的第 2 步调用它，`None` → 返回 0（**语义与改写前完全一致**）。
3. docstring 写明双语义设计：返回 `None` 而非某个哨兵下标，是因为两个调用方对「找不到边界」的正确反应相反——C8 是「全部保留」（0），取尾场景是「不带入任何历史」；若沿用返回 0，后者会退化成「带入整个主历史」的反向 bug。

**验证：** `python -m unittest tests.test_context_summarize -v` 全绿——**这是「C8 语义零变化」的关键回归，不得跳过**。

## T36: loop 策略测试

**文件：** `tests/test_skill_loop_policy.py`（新建）、`tests/test_perm_loop.py`（修改）
**依赖：** T32
**步骤：**
1. `tests/test_perm_loop.py:59` 的第 5 个位置参数 `""` 改为 `lambda: ""`。
2. 新建测试覆盖 AC8、AC14、AC15、AC23、AC29（工具集部分）、AC33：
   - 不传 `options` → 工具集与 C10 一致（除 `load_skill` 已注册进 registry 外无差异）。
   - `allowed` 收窄 → 只剩白名单内工具 + `load_skill`。
   - `excluded={load_skill}` → 工具集不含它（AC23）。
   - Plan Mode + 白名单 → 「白名单 ∩ 只读」+ `ask_user`/`present_plan`（AC15）。
   - `tool_policy` 是 callable 且**每轮被调用**（用计数桩断言调用次数等于轮数）。
   - `dynamic` 每轮被调用（同样用计数桩）——**这是改造点 1 的核心回归**。
   - `max_iterations=2` → 循环最多两轮并给出正确超限文案（AC33 的循环层）。
   - `record_usage=False` → `context_manager.record_usage` 未被调用。
3. **AC9 跨轮端到端（改造点 1 的要害护栏，必做）**：只验「`dynamic` 被调了 N 次」证明不了「第 N+1 轮真的看到了正文」。用假 Provider 编排两轮——第 1 轮返回一个 `load_skill` 工具调用，第 2 轮返回纯文本；`dynamic` 闭包接真实 `SkillManager`。断言：**第 1 轮实际发出的 messages 里那条 `<system-reminder>` 不含该 Skill 的 SOP 正文，第 2 轮的含**。这条同时锁住 AC9 与整个改造点 1 的存在理由（spec 审查阶段的阻塞项 B1：「模型激活了 Skill，本次循环剩余轮次却完全看不到指令」）。
4. **AC29 动态段零回归**：未激活任何 Skill 时，`dynamic_provider()` 的输出与 C10 口径（仅环境信息 + 可能的一次性提醒）**逐字节相等**。这是 N3 零回归的唯一硬护栏。

**验证：** `python -m unittest tests.test_skill_loop_policy tests.test_perm_loop -v` 全绿。

---

# 阶段九：commands 层改造（T37–T42）

## T37: 注册表内部结构拆分

**文件：** `rhinecode/commands/registry.py`
**依赖：** 无
**步骤：**
1. `__init__` 中 `self._specs` 拆为 `self._builtin_specs: list[CommandSpec]` 与 `self._skill_specs: list[CommandSpec]`。
2. 新增只读属性 `_specs`，返回 `self._builtin_specs + self._skill_specs`。注释写明拼接顺序（builtin 在前）即 `/help` 与补全候选的稳定顺序，符合 C10 N3。
3. **必改**：`register()` 第 95 行的 `self._specs.append(spec)` 与 `register_many()` 第 113 行的 `self._specs.extend(pending)` 的写入目标改为 `self._builtin_specs`。注释写明：属性每次返回新列表，对它 `append` 会写进临时对象后被丢弃——**静默不生效**，是最难排查的失败形态。
4. 确认只读方 `visible_commands()` / `complete()` / `render_help()` 无需改动。

**验证：** `python -m unittest tests.test_command_registry tests.test_command_builtins -v` 全绿（既有 C10 测试是这一步的回归护栏）。

## T38: replace_skill_commands 与 has_skill_command

**文件：** `rhinecode/commands/registry.py`
**依赖：** T37
**步骤：**
1. `has_skill_command(name) -> bool`：`any(s.name == f"/{name}" for s in self._skill_specs)`。docstring 写明**为何不能用 `resolve`**：Skill 名与内置命令重名时（F25 场景，短命令未注册）`resolve` 会命中内置命令返回 True，导致 F7 的文案指向一个存在但错误的命令。
2. `replace_skill_commands(specs) -> list[CommandSpec]`，按 plan 4.4 三步：
   - 从零重建 `staged`：先 stage 全部 `_builtin_specs`；
   - 逐个 skill spec：**先 stage 进独立临时字典 `probe = dict(staged)`**，成功则 `staged = probe` 并记入 `accepted`；抛 `CommandRegistrationError` 则记入 `skipped` 并丢弃 probe，继续下一个；
   - 全部处理完，一次性提交 `self._index = staged`、`self._skill_specs = accepted`。
3. 注释写明**为何必须用独立临时字典**：现有 `_stage` 边遍历边写入、遇冲突才抛、不回滚；直接复用会残留指向不在 `_skill_specs` 中的 spec 的索引项，`resolve` 会解析出一条不存在的命令。当前 skill spec `aliases=()` 只是让问题暂时不显现，这是隐含不变量。
4. 注释写明**为何不复用 `register_many`**：后者语义是整批失败抛异常，这里要求冲突的跳过、其余照常（F25）。
5. 提交前不触碰任何实例字段 → 中途失败自动保持原状（改造点 2 的原子性）。

**验证：** 临时脚本：传入含一个与 `/clear` 重名的 spec → 该条进 `skipped`、其余注册成功、`resolve("/clear")` 仍是内置命令；第二次调用传入不同集合 → 旧 skill 命令被完全替换。

## T39: 协议扩展

**文件：** `rhinecode/commands/models.py`
**依赖：** 无
**步骤：**
1. `ReportTarget` 新增 `SKILLS = "skills"`、`SKILLS_PROMPT = "skills_prompt"`。
2. `CommandController` 协议新增三个方法：`run_skill(name, arguments, display) -> None`、`reload_skills() -> str`、`deactivate_skill(name: Optional[str]) -> str`。
3. **`display` 参数的 docstring 必须写明不可省的理由**：spec F24/AC24 要求界面与会话回放显示用户原始输入；若没有它，`Message.display_content` 无从设置，`/resume` 回放会显示机器文本而非用户敲的 `/commit 修复登录超时`。
4. 更新 `ReportTarget` 的类 docstring 与 CLAUDE.md 成对维护点相关的注释。

**验证：** `python -m compileall rhinecode/commands` 通过。

## T40: Skill 短命令工厂

**文件：** `rhinecode/commands/skill_commands.py`（新建）
**依赖：** T39, T5
**步骤：**
1. 模块 docstring：职责（`SkillCommandInfo` → `CommandSpec`）、依赖方向（`commands → skills.models` 单向，skills 不认识 commands）。
2. `build_skill_command_specs(infos) -> list[CommandSpec]`：每条产出 `CommandSpec(name=f"/{info.name}", aliases=(), description=info.description, usage=f"/{info.name} [参数]", command_type=CommandType.PROMPT, handler=<闭包>, argument_hint="[参数]")`。
3. 闭包 handler：`controller.run_skill(info.name, invocation.arguments, invocation.raw_text.strip())`。
   **必须用工厂函数捕获 `info`**：`def _make_handler(info): def h(inv, ctrl): ...; return h`，然后 `handler=_make_handler(info)`。
   **这个陷阱是真实的**：`for info in infos:` 里直接定义 handler，所有闭包捕获的是**同一个 cell**，循环结束后全部指向最后一条 `info`；而 `CommandSpec(name=f"/{info.name}", ...)` 是即时求值的——失败形态是最恶心的那种：补全菜单和 `/help` 显示 `/commit`，执行起来却跑 `review`，不崩不报错，只静默跑错 Skill。
   不用默认参数写法（`def h(inv, ctrl, info=info)`）：它虽与 `CommandHandler = Callable[[CommandInvocation, CommandController], None]` 兼容（调用方只传两个位置参数），但把第三个参数暴露在签名上，读代码的人会困惑它是不是协议的一部分。
4. 注释写明：`command_type` 复用 `PROMPT` 而非新增枚举值，因语义与 `/init` 同类，可避免触发「新增枚举值 → 三处同步」的成对维护点。
5. 注释写明 `aliases=()` 是**显式不变量**，若将来放开需同步复查 `replace_skill_commands` 的 probe 逻辑（T38）。

**验证：** 临时脚本传 3 条 info → 得到 3 条 spec，逐个执行 handler 时 Fake Controller 收到正确的 name/arguments/display（**特别验证第 1 条不会调成第 3 条**）。

## T41: /skills 命令

**文件：** `rhinecode/commands/builtins.py`
**依赖：** T39
**步骤：**
1. 新增处理函数 `_handle_skills(invocation, controller)`。
2. 按首个空白切分 `invocation.arguments` 得子命令词与其余部分（沿用 C10 的 `split(maxsplit=1)` 口径）。
3. 五个分支（plan 4.6 表格）：空 → `query_report(SKILLS)`；`prompt` → `query_report(SKILLS_PROMPT)`；`reload` → `reload_skills()` + 显示 + `refresh_status()`；`off [name]` → `deactivate_skill(name or None)` + 显示 + `refresh_status()`；`run <name> [args]` → 再切一刀取 name、其后**原样**作为参数，缺 name 时显示用法。其它 → 「未知子命令」+ 用法。
4. `off` 分支切分口径：`off` 之后 `strip()` 非空即视为 name，内部不再切分（Skill 名按 F4 不含空白，多词输入必然走「未找到」分支）。
5. 在 `build_builtin_registry()` 中登记 `CommandSpec(name="/skills", aliases=(), description="管理 Skill：列表 / 热更新 / 卸载 / 执行 / 查看注入", usage="/skills [reload|off [name]|run <name> [参数]|prompt]", command_type=CommandType.LOCAL, handler=_handle_skills, argument_hint="[子命令]")`。
6. **同步修改 `tests/test_command_builtins.py`（必做，否则确定性红灯）**：
   - `EXPECTED_TABLE` 增一项 `"/skills": (set(), CommandType.LOCAL)`；
   - `test_exactly_twelve_canonical_commands` 的 `len(names) == 12` 改为 `13`，并把方法名与 docstring 同步改为「十三条」；
   - `test_argument_hints_and_requires_argument` 的「仅 `/resume` 有参数提示」改为「`/resume` 与 `/skills` 各有其参数提示，其余为 None」，分别断言 `"[编号或ID]"` 与 `"[子命令]"`。
   - **不得用「删掉 `len` 断言」的方式绕过**——那条断言是 C10 批准表的护栏，删掉等于永久失效。

**验证：** 用 Fake Controller 驱动五种形态，各自调到正确的控制器方法与参数；且 `python -m unittest tests.test_command_builtins -v` 全绿。

## T42: commands 层测试

**文件：** `tests/test_skill_commands.py`（新建）
**依赖：** T41, T40, T38
**步骤：** 覆盖 AC24（命令层）、AC25、AC26、AC31（注册层）：
1. `has_skill_command`：Skill 短命令已注册 → True；因与内置重名未注册 → **False**（即使 `resolve` 能命中内置命令）。
2. `replace_skill_commands`：冲突条目进 `skipped`、内置命令行为不变；二次调用完全替换旧集合；传入会触发异常的 spec 时注册表保持原状（原子性）。
3. `build_skill_command_specs`：闭包绑定正确（三条各自调对）、`command_type is PROMPT`、`aliases == ()`。
4. `/skills` 五种形态分派正确；`run` 缺 name → 显示用法；未知子命令 → 提示 + 用法；`run x a  b` 的参数原样为 `"a  b"`（不做 shell 分词）。
5. Tab 补全候选中出现 Skill 短命令（走 `registry.complete`）。

**验证：** `python -m unittest tests.test_skill_commands -v` 全绿。

---

# 阶段十：conversation 层（T43–T51）

## T43: SkillManager 接入与 clear

**文件：** `rhinecode/conversation.py`
**依赖：** T20
**步骤：**
1. `__init__` 增参数 `skill_manager: Optional[SkillManager] = None`，赋值 `self.skill_manager = skill_manager or SkillManager.empty()`。注释写明**协调层绝不自行扫盘**的三条理由（拿不到 `has_short_command`、会给既有测试引入读用户主目录的隐式 IO 违背 N1、与 `mcp_manager` 形态不符），以及 `empty()` 作为 Null Object 使各使用点无需散落判空。
2. `register_read_root(user_dir / "skills")` 与 `register_read_root(builtin_skills_dir())`（N5）。注释写明只对读类判定生效、写/搜索面不动。
3. `clear()` 追加 `self.skill_manager.clear_active()`（F11），并更新其 docstring。
4. **`_resume_stream` 也必须清空激活态（spec N4/AC36 要求，plan 遗漏了这条接线）**：在 `memory_manager.resume_into` 成功、产出 `HISTORY` 事件**之前**调 `self.skill_manager.clear_active()`。
   注释写明理由：激活态是**进程内存**状态，`/resume` 换的是历史而不是进程——不主动清空的话，用户在会话 A 激活的 Skill 会跟着进入会话 B 的上下文，其 SOP 正文继续每轮注入、白名单继续收窄工具集，而会话 B 的历史里根本没有激活它的痕迹。spec N4 明确要求「恢复历史会话后激活列表为空、槽位为空」。
   载入**失败**时不清空（此时仍停留在原会话，激活态应保持）。

**验证：** 既有 conversation 相关测试全绿（**验证没有引入扫盘副作用**：构造 `ConversationManager` 不应访问 `~/.rhinecode/skills/`）；另单测：激活一个 Skill → 调 `resume(key)` 成功载入 → `skill_manager` 的激活列表为空；载入失败（目标会话被锁）→ 激活列表**保持不变**。

## T43b: Skill 沙箱与恢复语义测试

**文件：** `tests/test_skill_sandbox.py`（新建）
**依赖：** T43, T22
**步骤：** 覆盖 **AC37（N5/N9，本章唯一的安全边界验收）** 与 **AC36（N4）**。参照 C9 的同构先例 `tests/test_memory_sandbox.py` 的写法：
1. 用 `register_read_root` 注册一个临时目录充当用户级 Skill 目录，测试结束 `clear_read_roots()` 复位（避免用例间污染）。
2. **只读面已扩大**：`read_file` 能读到该目录内的文件（绝对路径）。
3. **写面未动**：`write_file` 对同一路径被拒（`PathGuardError` 或权限引擎 DENY）。
4. **搜索面未动**：绝对路径的 `glob_files` 被拒；含 `..` 的路径仍被拒。
5. **原工作区语义不变**：工作区内文件的读写行为与注册前完全一致。
6. **权限引擎口径一致**：对同一路径构造 `read_path` 与 `write_path` 两类 `PermissionRequest`，前者 ALLOW、后者 DENY（验证 c9 的「只对 read 类判定生效」在本章沿用无误）。
7. **N9 无豁免**：构造一个 SOP 正文要求执行危险命令的 Skill，激活后走 `to_request` + `engine.decide` → 仍被①黑名单拦截（证明 Skill 不能放宽任何一层）。
8. **AC36 恢复语义（走真实 `resume` 路径，不得用 `clear_active()` 冒充）**：激活若干 Skill → 调 `conversation.resume(key)` 并消费其事件流至载入成功 → 断言激活列表为空、`prompt_report()` 的激活正文段为空。
   **必须打真实路径**：`clear_active()` 只被 `clear()`（`/clear`）与 T43 步骤 4 新增的 `_resume_stream` 接线调用；若测试直接调 `clear_active()`，验的只是「这个方法本身能清空状态」这一孤立行为，而 AC36 要的是「`/resume` 之后确实清空了」——两者之间隔着一条接线，正是 T43 步骤 4 补上的那条。
9. **失败路径**：目标会话被其它实例锁定导致载入失败 → 激活列表**保持不变**（此时仍停留在原会话）。

**验证：** `python -m unittest tests.test_skill_sandbox -v` 全绿。

## T44: ask 闭包抽取

**文件：** `rhinecode/conversation.py`
**依赖：** 无
**步骤：**
1. 把 `_run()` 内就地构造的 `ask` 闭包提为方法 `_build_ask(self) -> AskFn`，逻辑逐字不变。
2. `_run()` 改为 `ask = self._build_ask()`。
3. docstring 写明：抽取是为了让独立模式子对话（T49）复用同一实现，避免会话规则登记与永久落盘出现两份逻辑。

**验证：** `python -m unittest tests.test_perm_loop tests.test_review_fixes -v` 全绿（四态确认相关的既有测试是回归护栏）。

## T45: 动态段闭包与 skill_index

**文件：** `rhinecode/conversation.py`
**依赖：** T33, T43, T30, **T23**（`index_text`）, **T24**（`tool_policy`）, **T3**（`ToolRegistry.names`）
**步骤：**
1. `_run()` 中 `build_default_prompt(...)` 增传 `skill_index=self.skill_manager.index_text()`、`active_skills=""`。
2. 取 `base_dynamic = assembled.dynamic`；`pending = self.memory_manager.consume_pending_notice()`（**保持在闭包外取一次**）。
3. 定义闭包 `dynamic_provider()`，按 环境信息 → 已激活 Skill → 一次性提醒 的顺序拼接非空段（F9 要求 Skill 排在环境信息之后）。
4. **注释必须写明 `pending` 位置是刻意的**：`conversation.py:484-487` 现状就是取一次拼进字符串、`loop.py:213` 每轮重新包一次，所以「同一次运行每轮都带这条提醒」本来就是 c9 既有行为；「取走即清」指的是「本次运行消费掉、不带到下一条用户消息」。若改成闭包内消费，第 2 轮起会变空、反而破坏现状。
5. 传给 `self._agent.run(...)` 的 `dynamic` 改为 `dynamic_provider`；新增 `options=RunOptions(tool_policy=lambda: self.skill_manager.tool_policy(self._registry.names()))`——**名字在 lambda 体内现取**（R-3），注释写明捕获旧快照会让 F14 自愈失效的具体场景。
6. `_registry` 为 None（无工具模式）时 `tool_policy` 传 None。

**验证：** 临时脚本或单测：激活一个 Skill 后调 `dynamic_provider()` → 含其正文；`_registry` 增删工具后再调 → `tool_policy()` 的 allowed 随之变化。

## T46: 只读领域方法

**文件：** `rhinecode/conversation.py`
**依赖：** T26, T43
**步骤：**
1. `skills_report() -> str` → `self.skill_manager.report()`。
2. `skills_prompt_report() -> str` → `self.skill_manager.prompt_report(self._registry.names() if self._registry else frozenset())`。
3. `skill_status_segment() -> Optional[str]` → `self.skill_manager.status_segment()`。
4. `reload_skills() -> str`：调 `self.skill_manager.reload(known, registered)`（`known` 同 `__main__` 的口径：`registry.names() | {"ask_user","present_plan"}`），把 `ReloadOutcome` 渲染成可读报告。
5. `deactivate_skill(name) -> str` → `self.skill_manager.deactivate(name)`。

**验证：** 临时脚本调五个方法均返回字符串且不抛异常（含 `empty()` 场景）。

## T47: _take_tail

**文件：** `rhinecode/conversation.py`
**依赖：** T35
**步骤：**
1. `_take_tail(history, n) -> list[Message]`：`n <= 0` 或历史为空 → 返回 `[]`；否则 `idx = max(0, len(history) - n)`，调 `snap_back_to_user(history, idx)`。
2. 返回 `None` → **返回 `[]`**（不带入任何历史）。注释写明：这是与 C8 相反的 `None` 语义，若沿用 C8 的「返回 0」会退化成「带入整个主历史」——用户写 `history_messages: 3` 却灌进几百条，既违背独立模式目的，又因子对话关闭了第二层摘要而当场撑爆窗口。
3. 否则返回 `history[边界:]`。注释写明：**实际带入条数可能因回退而多于 n**，这是 F20 明确允许的。

**验证：** 单测：n=0 → 空；历史尾部是 `assistant(tool_calls)+tool` 配对时不被拆散；无任何 user 消息的历史 → 返回空列表（**不是全量**）。

## T48: run_skill 共享模式路径

**文件：** `rhinecode/conversation.py`
**依赖：** T45, T15, **T22**（`activate`）
**步骤：**
1. `run_skill(self, name, arguments, display) -> "Iterator[AgentEvent] | str"`。
2. 调 `self.skill_manager.activate(name, arguments)`：
   - `NOT_FOUND` → 返回提示文本（含可用名字）；
   - `ISOLATED` → 走 T49 的 `self._wrap_events(self._run_isolated_skill(spec, arguments, display))`；
   - `ACTIVATED` → 取 spec，返回 `self.submit_user_message(render_invocation_text(spec, arguments), display_content=display)`。
3. **降级说明的落点在此定死**（不转发给后续任务）：`ACTIVATED` 且 `degrade` 非空时，返回的事件流**前置一条 `AgentEvent(NOTICE, message=降级说明)`**，与 T49c 步骤 10 的处理形态一致。文案按 `DegradeKind` 分别为「Skill `<name>` 正文被截断（超单体上限），可能只执行了前半部分流程」/「Skill `<name>` 未注入（超总量上限），本次不会生效，请先卸载部分 Skill」。这承载 spec F9「截断必须对用户可见」中的「激活时给出警告」那一半。

**验证：** 单测：共享模式 → 主历史新增一条 user 消息，其 `content` 含名字/说明/参数三项、`display_content` 为原始输入；构造一个超单体上限的 Skill → 事件流首个事件是 NOTICE 且文案含「被截断」。

## T49a: 独立模式——子历史与子系统提示构造

**文件：** `rhinecode/conversation.py`
**依赖：** T47, T15, T14, T3
**步骤：** 对应 plan 4.9 的步骤 1–3、5。
1. 定义生成器 `_run_isolated_skill(self, spec, arguments, display)`（本任务只填前半段）。
2. `invocation = render_invocation_text(spec, arguments)`。
3. 主历史追加 `Message(role="user", content=invocation, display_content=display)` + `self.memory_manager.record_message(...)`。
4. 子历史 = `_take_tail(self.history[:-1], spec.history_messages)` + `[Message("user", invocation)]`。注释写明追加那条不可省：缺省不带历史时子对话会完全没有用户轮，模型会收到一个只有系统提示、没有任务陈述的请求。
5. 子系统提示：`stable` 复用主对话同一份；子动态段闭包返回 `环境信息 + render_active_body(spec, arguments)[0]`（只此一个 Skill，**不带**主对话其它已激活正文）。
6. **`self.plan_mode` 为真时**在子动态段追加衔接语「当前处于计划模式：请先依据上述 Skill 指令拟出执行计划并提交审批，获批后再按该指令执行。」注释写明理由：继承 Plan Mode 后子循环每轮会把 `plan_toggle_instruction` 与 `dynamic()` 合并进同一条 `<system-reminder>`（`loop.py:210-213`），「SOP 让你按步骤做」与「Plan 让你先别动手」会互相拉扯，需显式串成一条流程。

**验证：** 单测（可先用桩挡住后半段）：`history_messages=0` 时子历史恰为一条 user 消息且内容与主历史那条同源；`plan_mode=True` 时子动态段含衔接语、为假时不含。

## T49b: 独立模式——子 Agent 驱动与事件转发

**文件：** `rhinecode/conversation.py`
**依赖：** T49a, T44, T50, T24, T30
**步骤：** 对应 plan 4.9 的步骤 4、6、7。
1. 选 Provider：`spec.model` 为空 → `self._provider`；否则 `self._provider_for(spec.model)`。
2. `sub_agent = Agent(sub_provider, self._registry)`。
3. 按 plan 4.9 的**完整参数表**调 `sub_agent.run(...)`，逐个参数对照，其中三个必须加注释：
   - `plan_mode` **继承** `self.plan_mode`；
   - `cancel_event` **重建**（`self._cancel_event = threading.Event()`）——注释写明不重建会让上次运行残留的置位使子对话**开局即被取消**（`request_cancel()` 置的就是这个字段，而它原本只在 `_run()` 里重建）；
   - `ask=self._build_ask()`（复用抽取后的实现，避免两份）；`recorder=None`（子对话不写会话存档）。
   - `options=RunOptions(max_iterations=SKILL_MAX_ITERATIONS, record_usage=False, allow_summary=False, tool_policy=lambda: self.skill_manager.isolated_policy(spec, self._registry.names()))`——注释写明名字在 lambda 体内现取。
4. 逐个 `yield` 子循环事件，但**拦下 FINISHED**：记录其 `stop_reason` 供后半段使用，不向外产出（外层要自己收尾）。

**验证：** 单测：子对话跑完后 `recorder` 未被调用；主历史的估算锚点在子对话前后不变；先 `request_cancel()` 再跑 → 子对话**不**开局即取消。

## T49c: 独立模式——结论提取、原因映射与回流

**文件：** `rhinecode/conversation.py`
**依赖：** T49b
**步骤：** 对应 plan 4.9 的步骤 8–11 与那条 docstring 义务。
1. 从子历史反向找最后一条 `role=="assistant"` 且 `content.strip()` 非空的消息取正文。
2. **找不到，或 `stop_reason != COMPLETED`** → 按下表取文案。注释写明后一个条件不可省：计划被拒时子历史的最后一条 assistant 可能带非空前言正文，只按「找最后一条非空 assistant」会把前言误当结论回流。

   | `StopReason` | 文案 |
   |---|---|
   | `USER_CANCELLED` | 已取消，本次 Skill 未产出结果 |
   | `MAX_ITERATIONS` | 达到子任务迭代上限，本次 Skill 未产出结果 |
   | `STREAM_ERROR` | 模型请求出错（含上下文超限），本次 Skill 未产出结果 |
   | `UNKNOWN_TOOL` | 连续调用未知工具已停止，本次 Skill 未产出结果 |
   | `PLAN_REJECTED` | 计划未获批准，本次 Skill 未执行 |

3. 主历史追加 `Message(role="assistant", content=结论)` + `record_message`。至此主历史新增**恰好两条配对消息**。
4. 若走了「未产出」分支 → `yield AgentEvent(NOTICE, message=文案)`。注释写明：T49b 步骤 4 拦下了全部 FINISHED，而 `_do_stream` 对 `COMPLETED` 的 `_finish_line` 返回空串、**不渲染任何东西**（`tui/app.py:604-624`）；不补这条，用户按 Esc 取消后界面会完全没有反应。
5. `yield AgentEvent(FINISHED, stop_reason=COMPLETED)`。
6. 在 `_run_isolated_skill` 的 docstring 中写明**子对话触达上下文上限的兜底链路**（兑现 spec F21 的说明义务）：超窗 → API 报错 → `chunk.type=="error"` → 循环产出 ERROR 事件（`_do_stream` 渲染红色错误行）→ `FINISHED(STREAM_ERROR)` → 本任务步骤 2 判为未产出 → 步骤 4 的 NOTICE + 步骤 3 的历史记录。用户可见反馈两处，历史中留可追溯记录。
7. 确认 `run_skill` 返回的是 `self._wrap_events(self._run_isolated_skill(...))`——步骤 5 的自然完成需触发 C9 笔记钩子（F21）。

**验证：** 见 T51。

## T50: 临时 Provider 缓存

**文件：** `rhinecode/conversation.py`
**依赖：** 无
**步骤：**
1. `self._provider_cache: dict[str, BaseProvider] = {}`。
2. `_provider_for(model) -> BaseProvider`：命中缓存直接返回；否则 `create_provider(dataclasses.replace(self._config, model=model))` 存入缓存。
3. docstring 写明：这样**不需要改动 `BaseProvider.stream_chat` 接口**，三个 Provider 一行不改；缓存**有界**（= Skill 声明的不同模型数）、**不关闭**，与主 Provider 今天的处理同口径（`BaseProvider` 无 `close()`，`__main__` 的 finally 只回收 MCP 与会话锁）。

**验证：** 临时脚本用同一 model 调两次 → 返回同一对象；不同 model → 不同对象。

## T51: 独立模式测试

**文件：** `tests/test_skill_isolated.py`（新建）
**依赖：** T49c
**步骤：** 用假 Provider 与假回调，覆盖 AC18–AC23、AC33。按 T49a/b/c 三段分组组织用例，便于定位失败：
1. 正常结束 → 主历史新增**恰好两条配对消息**（user 自包含文本 + assistant 结论），且子对话的工具调用与结果**均不在**主历史中；两条都调了 `record_message`。
2. 用户取消 / 迭代上限 / 流错误 / 计划被拒 四种 → 助手位各得到对应原因文案，**配对结构仍成立**，且各产出一条 NOTICE 事件。
3. `history_messages=0` → 子历史恰为一条 user 消息且内容与主历史那条**同源**。
4. `history_messages=N` 且主历史尾部是 `assistant(tool_calls)+tool` 配对 → 子历史不含被拆散的半对，且末尾仍追加了那条 user 消息。
5. 子对话系统提示含本 Skill 正文、**不含**主对话其它已激活 Skill 的正文。
6. `plan_mode=True` 时子动态段含衔接语。
7. 子对话未调 `recorder`（不写会话存档）。
8. `record_usage=False`：主历史的估算锚点在子对话前后不变。
9. `cancel_event` 被重建：先 `request_cancel()` 再跑独立 Skill → 子对话**不**开局即取消。
10. `spec.model` 非空 → 用了不同的 Provider 实例。
11. 事件流经 `_wrap_events` → 结束时触发了 C9 笔记钩子（用假 memory_manager 断言）。
12. **AC21 的 C8 第一层**（决策 14 的唯一收益点，不验就不知道有没有接上）：让子对话产生一个超过第一层阈值的工具结果 → 断言它在子历史中被替换为「预览 + 路径」占位，且 `.rhinecode/context/` 下生成了对应文件；同时断言**第二层摘要未被调用**（假 provider 上无摘要请求）。

**验证：** `python -m unittest tests.test_skill_isolated -v` 全绿。

---

# 阶段十一：TUI 层（T52–T56）

## T52: 控制器方法与报告分支

**文件：** `rhinecode/tui/app.py`
**依赖：** T46, T48, T39
**步骤：**
1. 实现 `run_skill(name, arguments, display)`：`self._consume_manager_result(self._manager.run_skill(name, arguments, display))`。
2. 实现 `reload_skills()` 与 `deactivate_skill(name)`，直接返回 Manager 的字符串结果。
3. `query_report` 增两个分支：`SKILLS` → `self._manager.skills_report()`；`SKILLS_PROMPT` → `self._manager.skills_prompt_report()`。保持未知枚举值明确抛错的既有写法。

**验证：** `python -m compileall rhinecode/tui` 通过；`python -m unittest tests.test_tui_keybindings -v` 全绿。

## T53: 激活通知的跨线程刷新

**文件：** `rhinecode/tui/app.py`
**依赖：** T52
**步骤：**
1. 新增绑定方法 `_notify_skill_activation(self)`：`try: self.call_from_thread(self._refresh_status) except Exception: pass`。注释照抄 c9 `_notify_memory` 的口径（应用正在退出等边缘情况：刷新丢弃即可）。
2. `on_mount` 中注入 `self._manager.skill_manager.notify_activation = self._notify_skill_activation`。
3. 注释写明**为何不能用裸 lambda**：`activate()` 跑在 `LoadSkillTool.execute()` 里、又在只读并发桶的 `ThreadPoolExecutor` 里，`future.result()` 外层的 `except Exception` 会把回调异常转成「工具执行异常」——退出竞态下一次本已成功的激活会被报告成工具失败回灌给模型。
4. 注释补充事实：`_do_stream` 的 finally 已无条件 `call_from_thread(self._refresh_status)`（`app.py:597-601`），本回调的价值只在「激活当下立刻刷新」。

**验证：** `python -m compileall` 通过；T56 的 Pilot 测试覆盖。

## T54: 状态栏 Skill 段

**文件：** `rhinecode/tui/widgets.py`、`rhinecode/tui/app.py`
**依赖：** T26, **T46**（第 4 步调 `skill_status_segment`）
**步骤：**
1. `compose_status_text` 增形参 `skill_status: Optional[str] = None`，无内容时不渲染该段（与 MCP 段「None 即隐藏」同构）。
2. 段文本直接用传入值（形如 `Skill:2`）。注释写明**不用方括号**是为绕开 Textual markup 需转义 `[` 的既有坑。
3. `StatusBar.update_status` 增同名参数并透传。
4. `app._refresh_status` 增传 `skill_status=self._manager.skill_status_segment()`。

**验证：** 单测直接调 `compose_status_text`：无 skill → 输出不含 `Skill:`；有 → 含且其它字段保留。

## T55: 提交守卫三分支提示

**文件：** `rhinecode/tui/app.py`
**依赖：** T52
**步骤：**
1. `on_input_bar_input_submitted` 的守卫按分支给不同处理（plan 4.10 表格）：
   - `_pending_interaction` → 显示「正在等待你的确认，请先在面板上做出选择」。注释写明**该分支可达且最常见**：只有澄清面板禁用 InputBar（`app.py:715`，其 docstring 明写 confirm/approve 不做此限制），确认面板与计划审批面板期间用户点回输入框敲回车即命中此分支。
   - `_stream_active` → 显示「正在运行中，可按 Esc 取消后再执行命令」。
   - `_session_panel_active` → **保留裸 return**，注释写明不可达的原因（`_show_session_panel` 已 `disabled = True`，`app.py:474`）。
2. 新增 `self._busy_hint_shown: bool`，复位规则写死三条：进入流式时复位一次；每次**新**面板弹出时复位一次；面板关闭时**不**复位。
3. 提示只在 `_busy_hint_shown` 为 False 时显示，显示后置 True。

**验证：** T56 的 Pilot 测试覆盖。

## T56: TUI 测试

**文件：** `tests/test_skill_tui.py`（新建）
**依赖：** T55, T54, T53
**步骤：** 用 Fake Manager（不触真实 Provider）与 Textual Pilot，覆盖 AC24（补全）、AC34、AC35：
1. `compose_status_text` 的 Skill 段有无两种情形。
2. Pilot：Skill 短命令出现在 Tab 补全候选中；执行它调到 `run_skill` 且三个参数正确。
3. Pilot：流式运行中提交 → 出现可见提示（**不是静默无反应**）；同一次流式内连按两次只提示一次。
4. Pilot：确认面板弹出期间提交 → 出现「等待确认」提示（**验证 `_pending_interaction` 分支可达**）。
5. Pilot：模型路径激活（直接调 `_notify_skill_activation`）→ 状态栏出现 `Skill:1`；卸载后消失。

**验证：** `python -m unittest tests.test_skill_tui -v` 全绿。

---

# 阶段十二：启动接线（T57–T59）

## T57: 启动新增 A

**文件：** `rhinecode/__main__.py`
**依赖：** T21, T28, T38, **T17**（`format_fatal_message`）, **T4**（`builtin_skills_dir`）, **T3**（`names()`）
**步骤：**
1. 在 `tool_registry.register(MCPAddServerTool(...))`（第 137 行）**之后**、`mcp_manager.connect_all(...)`（第 138 行）**之前**插入。
2. 构造 `skill_manager = SkillManager(workspace_root(), Path.home()/".rhinecode", builtin_skills_dir(), has_short_command=command_registry.has_skill_command)`。
3. `tool_registry.register(LoadSkillTool(skill_manager))`。
4. `known = tool_registry.names() | {"ask_user", "present_plan"}`；`fatal = skill_manager.startup(known)`。
5. `fatal` 非空 → `print(format_fatal_message(fatal), file=sys.stderr)` 并 `sys.exit(1)`。
6. **注释必须写明这个窄窗口的理由**：`mcp_add_server` 是内置工具但名字是**单**下划线 `mcp_`（不匹配 `mcp__` 判别式），会落进第一段严格校验，而它比其它内置工具**晚注册**（依赖 `MCPManager` 实例）；放在「核心工具注册完成」这个看似自然的位置会误杀一个合法白名单条目并硬终止启动。同时此刻 `connect_all` 未执行，**没有任何 stdio 子进程与网络连接**，退出干净、不留孤儿进程。

**验证：** 临时造一个白名单含笔误内置工具名的项目级 Skill，运行 `python -m rhinecode --config <测试配置>` → 退出码 1、stderr 含 Skill 路径与工具名与版本错配提示、且无子进程残留。

## T58: 启动新增 B

**文件：** `rhinecode/__main__.py`
**依赖：** T57
**步骤：**
1. 在 `connect_all` **之后**插入。
2. `skill_manager.bind_tools(registered=tool_registry.names())`（F16 第二段）。
3. `skipped = command_registry.replace_skill_commands(build_skill_command_specs(skill_manager.command_infos()))`。
4. 打印：`skipped` 的短命令冲突警告（F25）、`skill_manager` 的全部 `_runtime_warnings`、以及 `project_skill_notice()`（N8）。
5. `ConversationManager(...)` 增传 `skill_manager=skill_manager`。

**验证：** 造一个与 `/clear` 重名的 Skill + 一个白名单含未连接 MCP 工具的 Skill → 启动成功、两条警告都打印、`/clear` 行为不变。

## T59: 启动接线测试

**文件：** `tests/test_skill_startup.py`（新建）
**依赖：** T58
**步骤：** 参照 C10 `test_command_startup` 的写法，覆盖 AC16、AC25、AC38：
1. 白名单含不存在的内置工具名 → `main()` 以退出码 1 结束，且 **`connect_all` / `ConversationManager` / `RhineApp` 均未被创建或调用**（用 monkeypatch 断言）。对齐 C10 `test_command_startup` 的既有口径（它断言的是「退出码 1 且 Provider/工具/MCP/Manager/App 均未创建」）。「`connect_all` 未被调用」是「无子进程残留」的可测形式——stdio 子进程只可能由 `connect_all → _connect_one → StdioTransport.start()` 创建，比查进程表稳定得多。
2. 白名单含 `ask_user` / `load_skill` → 正常启动，仅有「声明无效果」提示。
3. 白名单含 `mcp_add_server` → 正常启动，**不被误判为笔误**（AC16 第三分支，也是 T57 窄窗口的回归护栏）。
4. 白名单含未连接的 `mcp__` 工具 → 正常启动、有警告、该 Skill 的 `allowed_tools` 不含该项。
5. 与内置命令重名的 Skill → 短命令未注册、有警告、`resolve("/clear")` 仍是内置命令、该 Skill 仍在 `command_infos()` 中。
6. 含项目级 Skill → 启动输出含「发现项目级 Skill」，且**连续两次启动都输出**（AC38 零状态）。

**验证：** `python -m unittest tests.test_skill_startup -v` 全绿。

---

# 阶段十三：样板、打包与收尾（T60–T66）

## T60: commit 样板 Skill

**文件：** `rhinecode/skills/builtin/commit.md`（新建）
**依赖：** T4
**步骤：**
1. frontmatter：`name: commit`、`description: 按项目约定生成提交信息并提交`、`mode: shared`、`allowed_tools: [run_command, read_file, grep_content]`。
2. 正文 SOP：查看 `git status` 与 `git diff` → 参照最近提交的风格 → 生成信息 → 提交。含 `$ARGUMENTS` 占位符承接用户补充说明。
3. 正文兼作「怎么写一个 Skill」的可读示例，步骤清晰、不超过 40 行。

**验证：** `parse_skill` 解析成功、`mode is SHARED`、白名单三个名字都在内置工具集中（**否则启动会 fail-fast**）。

## T61: review 样板 Skill

**文件：** `rhinecode/skills/builtin/review.md`（新建）
**依赖：** T4
**步骤：**
1. frontmatter：`name: review`、`description: 审查当前改动并只回流结论`、`mode: isolated`、`history_messages: 0`、`allowed_tools: [read_file, grep_content, glob_files, run_command]`。
2. 正文 SOP：用 `git diff` 取改动 → 逐文件审查 → **最后必须输出一段完整结论**（因为独立模式取最后一条 assistant 正文回流，这条要求写明白）。
3. 注释性说明它覆盖独立模式这条执行路径（F28）。

**验证：** `parse_skill` 解析成功、`mode is ISOLATED`、白名单四个名字都在内置工具集中。

## T62: test 样板 Skill

**文件：** `rhinecode/skills/builtin/test.md`（新建）
**依赖：** T4
**步骤：**
1. frontmatter：`name: test`、`description: 跑项目测试并解读失败`、`mode: shared`、`allowed_tools: [run_command, read_file, grep_content, edit_file]`。
2. 正文 SOP：识别测试命令 → 运行 → 失败时定位到具体用例与源码位置 → 给出修复建议。含 `$ARGUMENTS` 用于指定测试范围。

**验证：** `parse_skill` 解析成功；三个样板的 `name` 均不与内置命令重名（`/commit` `/review` `/test` 都不在 C10 的 12 命令 + 8 别名、也不与 T41 新增的 `/skills` 冲突，**须实际核对一遍**）。「三个样板开箱可见」的端到端断言在 T27（`discover` 扫到 BUILTIN 层）。

## T63: 打包与 gitignore

**文件：** `pyproject.toml`、`.gitignore`
**依赖：** T62
**步骤：**
1. `pyproject.toml` 增 `[tool.setuptools.package-data]` 段，把 `rhinecode.skills` 的 `builtin/*.md` 纳入分发。注释说明：现有配置只有 `packages.find`，`.md` 不会进 wheel，`pip install -e .` 能跑但真安装后样板会消失。
2. `.gitignore` 的既有说明段（现已列出项目级 RHINE.md / permissions.yaml / mcp.yaml 属可提交内容）追加一行：项目级 `.rhinecode/skills/` 随仓库提交、团队共享。

**验证：** `python -m build`（或 `pip wheel . -w /tmp/w`）后解包检查 wheel 内含三个 `.md`；`git check-ignore -v .rhinecode/skills/x.md` 无输出（未被忽略）。

## T64: 全量测试与编译

**文件：** —
**依赖：** T63 及全部前置任务
**步骤：**
1. `python -m compileall rhinecode tests`。
2. `python -m unittest discover -s tests`。
3. 有失败则修复后重跑，**不得以「应该没问题」跳过**。
4. **文件数硬断言**（漏做任务的直接探测器）：`ls tests/test_skill_*.py | wc -l` 应为 **11**，即 parser / discovery / render / validation / manager / sandbox / loop_policy / commands / isolated / tui / startup 十一个文件全部存在。少一个即说明有测试任务被跳过。（`tests/test_command_builtins.py` 是**修改**而非新建，不参与本计数。）
5. **数量下界**：C10 结束时基线为 **358** 条（已实测 `Ran 358 tests OK`）。本章各测试任务枚举的用例数累加约 **80 条**，总数应落在 **430–440** 量级。断言「总数 ≥ 430」**且既有 358 条一条不少**（不得为了让新测试通过而删改既有用例）。

**验证：** 两条命令均无错误输出；测试全绿；`ls tests/test_skill_*.py | wc -l` 输出 **11**；总用例数 ≥ 430 且既有 358 条一条不少。

## T65: 端到端手动验收准备

**文件：** —
**依赖：** T64
**步骤：**
1. 在 tmux 或真实终端启动 `rhine`，确认启动提示含「发现项目级 Skill」（若项目内有）。
2. 逐项跑 `docs/c11/checklist.md` 中标注为手动的场景（该文件是 spec→plan→task→checklist 四份文档中的第四份，在本阶段之前产出）。
3. 记录实际输出作为验收证据。

**验证：** checklist 中的手动项全部有实际观察记录。

## T66: 文档同步

**文件：** `CLAUDE.md`、`AGENTS.md`、`README.md`
**依赖：** T65
**步骤：**
1. `CLAUDE.md` 首段改为以 C11 为当前阶段，概述 Skill 系统。
2. 「当前能力」新增 Skill 系统条目；「架构」新增 Skills 层描述与依赖方向（含 `tools/policy.py` 的无环前提）。
3. **「成对维护点」备忘新增四条**：新增 Skill 内置样板 → `skills/builtin/` + `pyproject` package-data；新增 `ReportTarget` 值 → 三处同步（已有，确认覆盖新增的两个值）；`tools/__init__.py` 不得 re-export 子模块；`SkillManager` 持锁期间禁止回调与跨线程调度。
4. 「运行时斜杠命令」新增 `/skills` 五种形态与 Skill 短命令说明。
5. 「安全边界」新增 N8/N9/N10 三条（Skill 信任模型、无权限豁免、白名单不是安全边界）。
6. 「已知后续工程项」新增 C11 明确不做的项（市场分发/版本管理、嵌套激活、参数 schema、模板引擎、并行执行、跨会话保持激活态、文件监听热更新）。
7. 「测试」新增 C11 测试文件与覆盖点。
8. `AGENTS.md` 与 `README.md` 同步对应内容。

**验证：** 可 grep 的清单，逐条确认——`CLAUDE.md` 含 `/skills`、`rhinecode/skills/`、`load_skill`、`ToolPolicy`；「成对维护点」段含新增四条的关键词（`skills/builtin` + package-data、`ReportTarget` 已覆盖新增两值、`tools/__init__.py` 不得 re-export、`SkillManager` 持锁禁回调）；「安全边界」段含 Skill 信任模型 / 无权限豁免 / 白名单不是安全边界三条；`AGENTS.md` 与 `CLAUDE.md` 的 C11 段落一致；`ls docs/c11/` 输出四个 `.md` 文件。

---

## 执行顺序

```
基础层（可并行起步）
T1 → T2
T1 → T4 → T5 ─┬─ T6 → T7 → T8                  （parser）
              ├─ T12 → T13 → T14 → T15 → T16   （render）
              ├─ T17 → T18 → T19               （validation）
              └─ T60/T61/T62（样板，仅依赖 T4）
T3（独立，但 T45/T49b/T57 都要它）
T7 → T9 → T10 → T11                             （discovery）

manager（汇聚点）
T10 + T14 + T18 → T20 → T21 → T22 → T23 → T24 → T25 → T26 → T27
T22 → T28                                       （load_skill 工具，不依赖测试文件）
T27 + T28 → T29                                 （T29 向 T27 建的文件追加用例）

agent 层（与 skills 包并行）
T1 → T30 → T31 → T32 → T36
T34 → T32          ← 正式边，不是备注
T33（独立）  T35（独立）

commands 层（与上并行）
T37 → T38 ─┐
T39 → T40 ─┼→ T42
T39 → T41 ─┘      T41 同时改 tests/test_command_builtins.py

conversation 层（汇聚）
T20 → T43 ─┬→ T46
           └→ T43b（沙箱与恢复语义测试，需 T22）
T44（独立，尽早做）
T33 + T43 + T30 + T23 + T24 + T3 → T45
T35 → T47
T45 + T15 + T22 → T48
T47 + T15 + T14 + T3 → T49a → T49b → T49c → T51
                        ↑ T49b 另需 T44 + T50 + T24 + T30
T50（独立）

TUI 层
T46 + T48 + T39 → T52 → T53
T26 + T46 → T54
T52 → T55 → T56

启动接线
T21 + T28 + T38 + T17 + T4 + T3 → T57 → T58 → T59

收尾（严格串行）
T62 → T63 → T64 → T65 → T66
```

**关键路径**：`T1 → T4 → T5 → T7 → T9 → T10 → T20 → T21 → T22 → T23 → T24 → T25 → T26 → T43 → T45 → T48 → T49a → T49b → T49c → T51 → T57 → T58 → T64 → T66`。

**建议的提交切分**（每组一次 commit）：基础类型（T1–T5）、parser+discovery（T6–T11）、render（T12–T16）、validation（T17–T19）、manager（T20–T27）、load_skill（T28–T29）、agent 改造（T30–T36）、commands 改造（T37–T42）、conversation（T43–T51，含 T43b 与 T49a/b/c）、TUI（T52–T56）、启动接线（T57–T59）、样板与打包（T60–T63）、收尾（T64–T66）。
