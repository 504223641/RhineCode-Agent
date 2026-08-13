# 保护路径层（②″）Tasks

> 依据已批准的 `spec.md` + `plan.md`。共 14 个任务。

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 新建 | `rhinecode/permission/protected.py` | 两张常量表 + `_WHY` + `ProtectedHit` + `inspect` |
| 修改 | `rhinecode/permission/models.py` | `Layer.PROTECTED`、`DecisionResult.protected_exempt` |
| 修改 | `rhinecode/permission/engine.py` | `_decide_core` / `_apply_protected` / 豁免集合 / `derive` 共享 / 两个新方法 |
| 修改 | `rhinecode/trace/reader.py` | `_LAYER_NAMES` 加一行 + `_s_permission_decision` 加标记 |
| 修改 | `rhinecode/agent/loop.py` | 普通工具那处埋点加 `protected_exempt` |
| 修改 | `rhinecode/tui/widgets.py` | `_LAYER_LABELS` 加一行 + `show_for` 保护路径三选项 |
| 修改 | `rhinecode/conversation.py` | `_build_ask` 两个分支前置豁免登记 |
| 新建 | `tests/test_perm_protected.py` | 判定层：范围 / cwd / 顺序护栏 / 两条反证 / 豁免语义 |
| 新建 | `tests/test_protected_wiring.py` | 接线：面板三选项 / ask 闭包不落盘 / 埋点字段 / 阅读器摘要 |
| 修改 | `rhinecode/tools/path_guard.py` | 只加一句注释，指认「排除表与 `protected.py` 那张刻意不合一」 |
| 修改 | `CLAUDE.md` | 架构表 ⚠ 列 + 成对维护点两条 + 安全边界一节 |
| 修改 | `docs/extensions/README.md` | 登记本扩展 |
| 删除 | `docs/todo/2-perm-protected-paths.md` | 本扩展落地即删 |
| 修改 | `docs/todo/*` | 序号重排 3→2…9→8，修交叉引用，**并修 `4-classifier.md` 的管线示意图** |

---

## T1: 数据层——新增枚举值与字段

**文件：** `rhinecode/permission/models.py`
**依赖：** 无

**步骤：**
1. `Layer` 枚举加 `PROTECTED = "protected"`，位置排在 `SANDBOX` 与 `NETWORK` 之间
   （**按逻辑先后排，不按代码执行顺序**——它是收紧器，但语义上属于「边界」那一族）。
2. 该枚举成员写注释：说明它是**收紧器**而非管线中的一站，
   并点明「只把非 DENY 升级为 ASK、绝不降级 DENY」。
3. 枚举的类 docstring 里那条「新增枚举值要同步 `trace/reader.py` 的 `_LAYER_NAMES`」
   补上第三份表 `tui/widgets.py` 的 `ConfirmPanel._LAYER_LABELS`（现在只写了两份）。
4. `DecisionResult` 加字段 `protected_exempt: bool = False`，写清语义
   （**命中了保护路径但因会话级豁免未被升级**）与「为什么只加这一个布尔」。

**验证：** `python -c "from rhinecode.permission.models import Layer, DecisionResult; print(Layer.PROTECTED.value, DecisionResult('allow','rule','r').protected_exempt)"` 输出 `protected False`

---

## T2: 保护路径判定模块

**文件：** `rhinecode/permission/protected.py`（新建）
**依赖：** 无（不依赖 T1）

**步骤：**
1. 模块 docstring：写清本层在管线里的**真实形态**（出口处的收紧器）、
   为什么不是「②之后③之前的一站」（会吞掉③的 deny 与④严格档的 DENY，
   两处都是放宽），以及判定基准为什么必须是 `request.cwd`。
2. 定义 `PROTECTED_RELATIVE`（`(".rhinecode",)`、`(".git",)`）与
   `EXCLUDED_RELATIVE`（`sessions` / `context` / `traces`）。
   `EXCLUDED_RELATIVE` 上方写注释指认「与 `path_guard._RUNTIME_ARTIFACT_RELATIVE`
   取值相同但**刻意不合一**」，并说明语义差别。
3. 定义 `_WHY` 表（plan.md 里那八条，由细到粗），注释说明按最长前缀匹配、
   以及 `permissions.yaml` 为什么由 `.rhinecode` 那条兜底而不单列。
4. 定义 `@dataclass(frozen=True) ProtectedHit(path: Path, reason: str)`。
5. 实现 `protected_roots_of(root)` / `excluded_roots_of(root)`（各返回 `tuple[Path, ...]`）。
6. 实现 `inspect(specifier, cwd) -> Optional[ProtectedHit]`：
   - `path_guard.resolve_in_workspace(specifier, cwd)` 解析，**任何异常按命中处理**
     （reason 写「路径无法解析，按保护路径处理」），并注释说明这是 N3 的偏严方向；
   - 命中任一 `excluded_roots_of` → 返回 `None`（**排除优先**，注释说明顺序反了排除永不生效）；
   - 命中任一 `protected_roots_of`（用 `path_guard.is_inside`，含相等）→ 查 `_WHY` 组装 reason 返回；
   - 否则 `None`。
   - reason 的格式：`保护路径：写入 <相对路径> 会改变 RhineCode 以后的行为（<why>），需要你过目`

**验证：**
```bash
python -c "
from pathlib import Path
from rhinecode.permission import protected as p
r = Path('.').resolve()
print(bool(p.inspect('.rhinecode/hooks.yaml', r)))     # True
print(bool(p.inspect('.rhinecode/traces/a.jsonl', r))) # False
print(bool(p.inspect('rhinecode/agent/loop.py', r)))   # False
print(p.inspect('.rhinecode/hooks.yaml', r).reason)
"
```

---

## T3: 判定层测试（第一批：范围与解析）

**文件：** `tests/test_perm_protected.py`（新建）
**依赖：** T2

**步骤：**
1. `ScopeTest`（AC4）：遍历 `permissions.yaml` / `permissions.local.yaml` /
   `hooks.yaml` / `mcp.yaml` / `agents/x.md` / `skills/x/SKILL.md` / `memory/x.md` /
   `worktrees/w/a.py` / `.git/config` / `.git/hooks/pre-commit` 逐个断言命中；
   遍历 `sessions/x.jsonl` / `context/x.txt` / `traces/x.jsonl` 逐个断言**不**命中；
   再断言几个普通业务路径不命中。
2. `PathFormTest`（AC5）：同一个目标用相对写法、`./` 前缀写法、**绝对路径写法**
   三种都命中。符号链接那条单列一个用例（真建一个临时目录 + `os.symlink`，
   Windows 上建不了软链时 `skipTest`，理由写进注释）。
3. `CwdTest`（AC2）：同一个相对路径 `.rhinecode/hooks.yaml`，
   以主项目根为 cwd 时命中、以「主项目根/.rhinecode/worktrees/w」为 cwd 时**不**命中。
   **这条是坑 1 的护栏**，注释里写明漏掉的表现是「隔离委派静默全失败」。
4. `FailSafeTest`（AC3）：`cwd=None`、`cwd=""`、含 `..` 的路径，
   三种都断言命中（偏严）且不抛异常。
5. `ExcludeBeatsProtectTest`：断言排除项在保护项内部仍然不命中——
   **反证**：把 `inspect` 的两步顺序对调会让这条红。

**验证：** `python -m unittest tests.test_perm_protected -v` 全绿

---

## T4: 引擎接入——收紧器与豁免集合

**文件：** `rhinecode/permission/engine.py`
**依赖：** T1, T2

**步骤：**
1. 模块 docstring 的「短路顺序」段落末尾补一段：说明②″是**出口处的收紧器**，
   不在短路序列里，以及它「只把非 DENY 升级为 ASK」的完整判定表。
2. `__init__` 加 `self.protected_exemptions: set[Path] = set()`，
   docstring 的 `:ivar:` 列表补一条。
3. 把现有 `decide` 的整个 body 原封不动改名为 `_decide_core`（**一行不改**，
   `_verdict` 闭包留在里面），新 `decide` 只有一句：
   `return self._apply_protected(request, self._decide_core(request))`。
4. 实现 `_apply_protected(request, result)`，按 plan.md 的五行判定表。
   ⚠ 构造新 `DecisionResult` 时**显式填 `kind` / `host`**，并在注释里指认
   CLAUDE.md 那条不变量（这是本方法作为**第二个出口**必须付的代价）。
   ⚠ 「结论非 DENY 一律换层」那一支写注释说明为什么不能只处理 ALLOW
   （默认档那次 `ASK @ MODE` 会让面板照样给出「永久放行」，骗人的按钮换个入口）。
5. 实现 `grant_protected_exemption(request) -> bool`（内部调 `protected.inspect`，
   未命中不登记并返回 False）与 `is_protected_exempt(path) -> bool`。
6. `derive()` 里显式赋值 `derived.protected_exemptions = self.protected_exemptions`，
   并在那张「共享与独立的分界」表里加一行，理由与 `session_rules` 同口径。
   ⚠ 注释点明与 `load_errors` 同一个坑：构造函数会新建一个 `set()`，不显式赋值就静默不共享。

**验证：**
```bash
python -m compileall rhinecode/permission
python -m unittest tests.test_perm_engine tests.test_perm_rules tests.test_perm_network_layer tests.test_perm_turn_grant -v
```
既有权限测试全绿（证明 N4「其余种类逐字不变」）

---

## T5: 判定层测试（第二批：升级语义与豁免）

**文件：** `tests/test_perm_protected.py`
**依赖：** T4

**步骤：**
1. `PipelineOrderGuardTest`（AC6，**顺序护栏**）：规则集里放一条
   `allow: Write(.rhinecode/**)`，断言写 `hooks.yaml` 得到 `ASK` 且 `layer is Layer.PROTECTED`。
   注释写明**为什么必须用「宽 allow」构造**（用「③层没命中」的形态在实现写错时照样通过，
   这是②′网络边界层付过一次学费的教训）。
2. `NoDowngradeTest`（AC7/AC8，**两个方向的反证**）：
   - `deny: Write(.rhinecode/hooks.yaml)` → 结论仍是 `DENY`、层仍是 `RULE`；
   - 严格档 + 无规则 → 结论仍是 `DENY`、层仍是 `MODE`；
   - 顺带一条：路径越界（②层 DENY）时层仍是 `SANDBOX`。
3. `UpgradeTest`（AC9）：放行档 → `ASK @ PROTECTED`；默认档 → `ASK @ PROTECTED`
   （**层被换掉**，这条是 plan 里那个修正的护栏）；
   `turn_rules` 里塞一条 allow（模拟 Skill 预授权）→ 仍 `ASK @ PROTECTED`。
4. `ExemptTest`（AC12/AC13）：
   - 登记豁免后同一路径不再升级，且 `protected_exempt is True`；
   - **同目录下另一个文件仍被升级**（最小授权的反证）；
   - 豁免 + `deny` 规则同时成立时结论仍是 `DENY`；
   - `grant_protected_exemption` 对非保护路径返回 `False` 且不登记。
5. `DeriveTest`（AC14）：主引擎登记豁免后，`derive()` 出来的实例对同一路径同样不升级；
   反向再断言 `derive()` 之后**在派生实例上登记**也会影响主引擎（同一个对象）。
6. `OtherKindsUnchangedTest`（AC1/N4）：命令类、读取类、glob 类、URL 类、
   未映射类各造一个请求，断言加了本层之后结论与 `_decide_core` 的结论**逐字相等**。

**验证：** `python -m unittest tests.test_perm_protected -v` 全绿

---

## T6: 行为记录阅读器

**文件：** `rhinecode/trace/reader.py`
**依赖：** T1

**步骤：**
1. `_LAYER_NAMES` 加 `"protected": "②″保护路径"`。
2. `_s_permission_decision` 的标记位加一支：`r.get("protected_exempt")` 为真时
   在摘要行标出「保护路径已豁免」。注释写明进摘要行的判据
   （用户问「它怎么没弹面板就改了 hooks.yaml」时第一件要确认的就是它）。

**验证：** `python -m unittest tests.test_trace_reader -v` 全绿
（该文件里有一条遍历 `Layer` 的一致性断言，漏改这里会当场红）

---

## T7: 埋点字段

**文件：** `rhinecode/agent/loop.py`
**依赖：** T1

**步骤：**
1. 在**普通工具**那处 `permission_decision` 埋点（`decision = self._apply_hook_ask(...)`
   之后那一处）加 `protected_exempt=decision.protected_exempt,`。
2. 注释说明**为什么系统级工具那处不加**（那七个工具落 `other` 分支、
   `kind != "write_path"`，本层恒不生效，加进去是恒假字段、会稀释 `mode_downgraded`）。

**验证：** `python -m unittest tests.test_trace_system_serial tests.test_trace_full_output -v` 全绿

---

## T8: 确认面板

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T1

**步骤：**
1. `ConfirmPanel._LAYER_LABELS` 加 `"protected": "②″保护路径"`。
2. `show_for` 里取 `layer_value`（沿用 `_url_detail_lines` 已有的
   `getattr(layer, "value", layer)` 写法），保护路径时走三选项分支：
   `本次放行 / 本会话放行（本会话内对该文件不再询问，不写入配置）/ 拒绝`。
   ⚠ 「不写入配置」不可省——用户对这个选项的既有心智是「登记一条会话规则」，
   这里换了机制。
3. 类 docstring 的「四个可选项的 option.id 约定」补一段：说明保护路径场景下
   `yes_permanent` **不出现**及其理由（写出来的③层规则永远不会被求值）。

**验证：** `python -m unittest tests.test_tui_panels tests.test_tui_symbols -v` 全绿

---

## T9: 协调层——本会话放行改走豁免

**文件：** `rhinecode/conversation.py`
**依赖：** T4

**步骤：**
1. `_build_ask` 的闭包里，在既有 `grant_tool, grant_pattern = to_allow_rule(req)`
   **之前**插入保护路径分支：`decision.layer is Layer.PROTECTED` 且
   `choice in (ALLOW_SESSION, ALLOW_PERMANENT)` 时调
   `self._engine.grant_protected_exemption(req)` 并 `return True`。
2. 注释说明两件事：① 保护路径的「本会话放行」**不产生任何③层规则、不写任何文件**；
   ② `ALLOW_PERMANENT` 也走这一支是**防御性**的（面板压根不给这个选项，
   但若将来出现第二条结算路径，落盘会写出一条永远不被求值的规则，
   正是本扩展要消灭的那个按钮）。
3. 补 `Layer` 的 import。

**验证：** `python -m unittest tests.test_review_fixes tests.test_perm_allow_rule -v` 全绿

---

## T10: 接线测试

**文件：** `tests/test_protected_wiring.py`（新建）
**依赖：** T6, T7, T8, T9

**步骤：**
1. `PanelTest`（AC11）：直接构造 `ConfirmPanel`，喂一个 `layer=PROTECTED` 的
   `DecisionResult`，断言选项 id 集合是 `{yes, yes_session, no}`；
   **反证**：喂一个 `layer=MODE` 的，断言仍是四项含 `yes_permanent`。
2. `AskClosureTest`（AC12）：构造引擎与 `ConversationManager`（或直接取
   `_build_ask` 的闭包），模拟用户选 `ALLOW_SESSION`：
   断言豁免集合多了一项、`session_rules` **长度不变**、
   且 `persist_local_rule` **零次调用**（用 mock 计数，不是断言返回值）。
3. `TraceFieldTest`（AC16）：跑一次带 trace 的判定，断言 `permission_decision`
   负载里有 `protected_exempt`；再让阅读器渲染一条 `protected_exempt=True` 的记录，
   断言摘要行里出现「保护路径已豁免」，以及一条 `layer=protected` 的记录
   摘要行里出现「②″保护路径」。
4. `LayerTableTest`（AC10）：遍历 `Layer` 断言三份表都有对应项
   （既有护栏已覆盖两份，这里补面板那份的正向断言）。

**验证：** `python -m unittest tests.test_protected_wiring -v` 全绿

---

## T11: 全量回归

**文件：** 无
**依赖：** T1–T10

**步骤：**
1. `python -m compileall rhinecode tests`
2. `python -m unittest discover -s tests`
3. 逐条核对新增/失败项；**任何既有用例变红都要先解释清楚再改**
   （N4 要求其余种类逐字不变，一条既有用例变红就是 N4 被破坏的信号）。

**验证：** 全量测试通过，失败数为 0，skipped 仍是 4

---

## T12: `path_guard` 的互相指认注释

**文件：** `rhinecode/tools/path_guard.py`
**依赖：** T2

**步骤：**
1. `_RUNTIME_ARTIFACT_RELATIVE` 上方的注释补一句：指认
   `permission/protected.py` 的 `EXCLUDED_RELATIVE` 取值相同但**刻意不合一**，
   写明语义差别（搜索跳过 vs 写入免审）与合一的后果
   （新增一个「不该进搜索结果但改了会变天」的目录时，合一的表会静默把它从保护里摘掉）。

**验证：** `python -m unittest tests.test_search_artifact_exclusion -v` 全绿（纯注释改动，行为不变）

---

## T13: 文档

**文件：** `CLAUDE.md`、`docs/extensions/README.md`
**依赖：** T11

**步骤：**
1. `CLAUDE.md` 架构表 **Permission 行的 ⚠ 列**加一条：②″保护路径是**收紧器不是短路站**，
   短路会吞掉③的 deny 与④严格档的 DENY；且「非 DENY 一律换层」不能改成「只处理 ALLOW」。
2. `CLAUDE.md` **成对维护点**加两条：
   - 新增保护路径 / 排除项 → `permission/protected.py` 的两张表 + `_WHY`
     （漏 `_WHY` 不报错，只是面板上那行退回泛泛的「配置目录」，用户看不出这个文件为什么特殊）；
   - 保护路径的「本会话放行」→ `tui/widgets.py` 的三选项分支 + `conversation.py`
     的豁免分支**必须成对**（只改前者：面板不给「永久放行」但「本会话放行」仍写③层规则，
     用户点了之后下次还弹；只改后者：面板仍显示一个点了没用的「永久放行」）。
3. `CLAUDE.md` **安全边界**新增一节「保护路径（②″）」，五条：
   ① 它绕不过——不是管线里的一站而是出口收紧器，任何 allow 规则、任何权限档都消解不掉它；
   ② 它只收紧不放宽（DENY 一律保留），论证形态与 C12 Hook 的 ASK 同构；
   ③ 判定基准是 `request.cwd`，因此隔离子 Agent 天然不受影响，
      **非隔离子 Agent 命中即自动拒绝——这是期望行为不是误伤**；
   ④ 豁免只在内存、只对单个文件、关程序即失效，**刻意不做落盘的永久豁免**
      （落盘的豁免本身就是一份能改变以后会发生什么的配置）；
   ⑤ **`run_command` 与 MCP 工具不受本层约束**（登记为已知边界，与已知项 #4 同源）。
4. `CLAUDE.md` 已知后续工程项：在权限系统那条（#5）下补登本层未覆盖的两个缺口。
5. `docs/extensions/README.md` 登记本扩展（一行索引 + 一句话说明它做了什么）。
6. `CLAUDE.md` 顶部「已实现的扩展」列表加一条。

**验证：** 通读改动段落，确认没有与既有条目矛盾的表述；
`grep -n "②″" CLAUDE.md` 能看到新增各处

---

## T14: todo 清理与重排

**文件：** `docs/todo/`
**依赖：** T13

**步骤：**
1. 删 `docs/todo/2-perm-protected-paths.md`。
2. 重命名：`3→2`、`4→3`、`5→4`、`6→5`、`7→6`、`8→7`、`9→8`（用 `git mv`）。
3. **修 `4-classifier.md`（重排后为 `3-classifier.md`）的管线示意图**：
   它把②″画成 `②沙箱 → ②″保护路径 → ②′网络 → ③规则` 里的一站，
   与实际实现（出口收紧器）不符。改成正确的形态并加一句说明
   ——按错图去实现第 4 条不会立刻出错（分类器只碰 `command`/`url`、
   ②″只碰 `write_path`，两者不相交），但下一个人会从错误的图推理。
4. 修 `3-perm-auto-plan.md`（重排后为 `2-*`）里对「第 2 条」的引用：
   把「待开工」改为已完成并指向 `docs/extensions/protected-paths/`；
   它那张 auto 行为表里「写（保护路径）→ 问」的结论**不变**（放行档下④给 ALLOW、
   被②″升级为 ASK，正是该表描述的行为）。
5. 修 `docs/todo/README.md` 的序号与索引。
6. 全目录搜一遍旧序号的交叉引用（`grep -rn "第 [2-9] 条\|docs/todo/[2-9]-" docs/`）逐个修。

**验证：**
```bash
ls docs/todo/
grep -rn "perm-protected-paths" docs/ | grep -v "docs/extensions/protected-paths"
```
前者序号连续无缺口，后者只剩指向新位置的引用

---

## 执行顺序

```
T1 ─┬─ T4 ─┬─ T5
    │      └─ T9 ─┐
    ├─ T6 ────────┤
    ├─ T7 ────────┼─ T10 ─ T11 ─ T12 ─ T13 ─ T14
    └─ T8 ────────┘
T2 ─┬─ T3
    ├─ T4（同上）
    └─ T12
```

T2 与 T1 无依赖，可先做。T6 / T7 / T8 三者互不依赖，可并行。
**T11（全量回归）必须在所有代码任务之后、文档任务之前**——文档要写的是
实际跑过的行为，不是设想的行为。
