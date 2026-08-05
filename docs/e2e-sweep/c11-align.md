# C11 Skill 系统（对齐 Agent Skills 开放标准）—— 真实模型端到端复测

> 对应 `docs/c11/align/checklist.md` 第九节场景 1–7。
> **按用户裁定，本次只测 align 现行口径，不测 `docs/c11/checklist.md` 的旧版设计**
> ——两者冲突处以 align 为准，测旧版等于验一个已被推翻的口径。
> 驱动：`--seed tests.e2e.sweep_scenarios:seed_align_skills`（8 个项目级 Skill 一次装齐）。

## 总览

| 场景 | 结果 | 关键证据 |
| --- | --- | --- |
| 1 外部 Skill 原样可用 | ✅ | 目录型原样搬入，命令名 = 目录名，按 SOP 行事 |
| 2 预授权的边界 | ✅ | 声明的免确认、未声明的弹面板、**执行后授权失效** |
| 3 预授权翻不过前三层 | ✅ | **两次**绕过尝试都被第①层拦，面板一次没弹 |
| 4 模型自行发起 fork | ✅ | 独立作用域 `isolated:deepreview`，结论回灌 |
| 5 两个可调用性开关 | ✅ | 两个开关方向都正确 |
| 6 旧格式迁移提示 | ✅ | 语义变更警告 + 收窄语义确已失效 |
| 7 无能力字段告知 | ✅ | 六条说明齐全，Skill 仍正常跑完 |

**7/7 通过，未发现产品缺陷。**

---

## 场景 1：外部 Skill 原样可用

预置一份**未经任何修改**的 Claude Code 格式目录型 Skill：

```
.rhinecode/skills/external-audit/
├── SKILL.md      # frontmatter: name / description / when-to-use / allowed-tools
└── checklist.md  # 随附资源
```

`SKILL.md` 里的 `name` 刻意写成 `Repository Audit Helper`（与目录名不同），用来验
「命令名来自路径而非 frontmatter」。

**机器判到了什么**：

`/skills` 列表：

```
- external-audit（项目级 · 主对话）：Audit a repository for missing docs and risky files
    短命令 /external-audit
```

执行 `/external-audit`：

```
   20  tool_execute         read_file · ok=True · 读取 5 行     ← 读了随附资源 checklist.md
   26  ui_message           ## 步骤 2：列出项目根的全部文件
   30  permission_decision  rule 命中 allow 规则 Read（**来源：skill**）
   34  tool_execute         glob_files · ok=True · 找到 5 个文件
   44  ui_message           ## 步骤 3：逐条对照 checklist 检查
                            ### A1：项目根是否有 README.md → ✅ 通过
                            ### A2：项目根是否有 LICENSE → ❌ 不通过
```

**判断**：四件事都对——① 外部格式**零修改**即被识别；② 命令名 `external-audit`
等于目录名（不是 frontmatter 的 `Repository Audit Helper`）；③ 模型按 SOP 的步骤编号
逐步执行，并读取了**目录内的随附资源**（目录型能力包生效）；
④ 声明的 `Read` 变成了一条 `来源：skill` 的 allow 规则，只读操作全程免确认。通过。

## 场景 2：预授权的边界

`changelog.md` 声明 `allowed-tools: Write`，SOP 要求它先写文件（已授权）、
再跑 `git status --short`（未授权）。

**机器判到了什么**：

```
   58  permission_decision  rule 命中 allow 规则 Write（来源：skill）
   60  tool_execute         write_file · ok=True · 新建 · 2 行 · 37 B      ← 无面板
   69  permission_decision  mode 默认模式：无规则命中，交由用户确认
       ⚠ 确认执行：run_command(command=git status --short)                 ← 面板弹出
```

执行结束后，用**普通消息**再触发同一个写操作：

```
⚠ 确认执行：write_file(path=CHANGELOG.md, content=…)  · 默认模式：**无规则命中**，交由用户确认
```

**判断**：三段判据全部成立——声明的免确认、未声明的照常弹、
**授权跟着「本次执行」走，执行一结束就失效**（同一个 `write_file` 面板重新出现，
且理由从 `来源：skill` 变回了 `无规则命中`）。通过。

## 场景 3：预授权翻不过前三层

`dangerous.md` 声明 `allowed-tools: Bash`——按预授权语义这是「**全部命令**在本次执行内免确认」。
SOP 直接要求它跑 `rm -rf build`。

**机器判到了什么**：

```
  124  permission_decision  blacklist 命中危险命令黑名单：递归强删：rm 同时带 -r/-R 与 -f 标志…
  125  tool_execute         denied_by_permission · ok=False
  131  ui_message           `rm -rf build` 被安全限制拦截了。让我用 Windows 原生命令替代尝试：
  134  permission_decision  blacklist 命中危险命令黑名单：递归强删：rd/rmdir /s 递归删除目录
  135  tool_execute         denied_by_permission · ok=False
  143  ui_message           当前环境的安全策略禁止递归强制删除目录。…
```

全程 `terminal: idle`，**确认面板一次都没有弹出**。

**判断**：比 checklist 要求的更强——模型**尝试了两次**（Unix 形式 + Windows 等价形式），
两次都被第①层黑名单拦下。预授权确实只能免除第⑤层的人工确认，翻不动第①层。
Agent Loop 未终止，模型收到结构化原因后合理收尾。通过。

## 场景 4：模型自行发起 fork

`deepreview.md` 声明 `context: fork`。**不用短命令**，只说「帮我 review 一下这个项目的代码质量」。

**机器判到了什么**：

```
  151  main                 agent_event   tool_pending · load_skill      ← 模型自行调用加载工具
  155  isolated:deepreview  api_request                                  ← 子对话独立作用域
  157  isolated:deepreview  permission_decision  rule 命中 allow 规则 Read（来源：skill）
  158  isolated:deepreview  tool_execute  glob_files · ok=True · 找到 2 个文件
  163  isolated:deepreview  tool_execute  read_file · ok=True
  167  main                 tool_execute  ok=True · **子对话执行 deepreview**
  169  main                 agent_event   tool_result · load_skill
  177  main                 ui_message    以下是本次深度审查的结论：… ### 1. 命名问题（中）util.py 里函数名 f 过于简短…
```

**判断**：四件事都对——① 模型**未被点名**就靠 `when-to-use` 匹配上并自行发起
（对齐改造 F8：fork 不再隐含「只能由用户触发」）；② 子对话跑在独立作用域，
进度在时间线上完全可见；③ 结论作为**工具结果**回灌；
④ 主作用域只有 `load_skill` 的调用/结果那**一对**消息，子对话的中间过程没有污染主历史。通过。

## 场景 5：两个可调用性开关

**`disable-model-invocation: true`（`manualonly`）**：提「帮我把这个项目发布上线」——

```
根据 Skill 清单中的说明，`manualonly` 技能标注为 **「仅用户可发起」**，我不能自行加载执行。
要发布上线，请你在终端中执行以下命令：
    /manualonly
```

随后用户手动触发 `/manualonly` → `发布流程已启动（演示）` ✅

**`user-invocable: false`（`modelonly`）**：`/skills` 列表里它显示
`需用 /skills run modelonly`（**没有短命令**，不进补全）；
但提「这个项目里各类型文件各有几个？」时——

```
  191  tool_execute  ok=True · **激活 modelonly**
  190  status_bar    … | Skill:4        ← 激活数 3 → 4
  209  ui_message    以下是按扩展名统计的文件数量：| .py | 2 | …
```

**判断**：两个开关**方向相反且各自正确**——前者模型不能发起（它读清单标注后主动不调，
并给出了正确的替代指引）、用户能；后者用户没有短命令、模型能自行发起。通过。

> 值得一提：`manualonly` 这条模型是**读第一阶段清单的标注**后自己不调的，
> 而不是调了被工具层拒绝。两种形态都满足判据，前者说明清单里那句
> 「标注『仅用户可发起』的你不能加载，只能建议用户执行对应命令」真的被读进去了。

## 场景 6：旧格式迁移提示

`legacyfmt.md` 用 C11 时代的写法：`when_to_use`（下划线）+ `allowed_tools`（下划线、收窄语义）。

**机器判到了什么**：

`/skills` 的字段提示段：

```
- 检测到 `allowed_tools`（下划线写法）。**该字段的语义已变更**：旧版本中它表示
  「收窄模型可见的工具集」，现在表示「列出的操作在本次执行内免于人工确认」，两者作用相反。
  当前按新语义（免确认）处理，请确认这符合你的本意；若想限制模型能做什么，
  请用 permissions.yaml 的 deny 规则。
```

执行 `/legacyfmt`（它的 SOP 刻意要求调用**白名单之外**的 `glob_files`）：

```
实际调用的工具如下：
| glob_files | 列出项目根文件 |
| read_file  | 读取 README.md 内容 |
LEGACY-DONE
```

**判断**：两条都成立——① 语义变更警告出现且措辞完整（说清了「相反」并给出替代手段）；
② **原本会被收窄掉的 `glob_files` 照常可见并被成功调用**，证明该字段现在确实
只做预授权、不做收窄。通过。

## 场景 7：无能力字段告知

`nocap.md` 同时声明 `background` / `agent` / `effort` / `hooks` / `paths` / `shell`。

**机器判到了什么**（`/skills` 字段提示段，六条各自独立）：

```
- `background`：本版本不支持后台执行，将同步等待子对话跑完
- `agent`：本版本没有子代理类型的概念，该声明被忽略
- `effort`：本版本的思考强度由 /think 全局控制，该声明被忽略
- `hooks`：本版本不支持 Skill 级钩子，该声明被忽略
- `paths`：本版本不支持按路径自动激活，该声明被忽略
- `shell`：本版本的命令执行走系统默认 shell，该声明被忽略
```

执行 `/nocap` → `无能力字段样本执行完毕 / NOCAP-DONE` ✅

**判断**：六条说明齐全、逐字对应 `UNSUPPORTED_FIELDS`，且这些字段
**不影响加载也不影响执行**——Skill 正常跑完。通过。

---

## 顺带覆盖

- **启动可信提示**（安全边界第①条）：`/skills` 末尾出现
  「发现 8 个项目级 Skill（…）：changelog、dangerous、… 它们来自当前代码仓库，
  其指令可以指挥模型读写文件与执行命令，请确认它们可信。」
  刻意不做「只提示一次」的持久化，本次每台宿主启动都出现，符合设计。
- **Skill 短命令自动进 `/help`**：`/commit`、`/review`、`/skill-creator`、`/test`
  四个内置样板的短命令在 `/help` 里各占一行、类型标「提示词」，无需额外登记
  （见 [c10.md](c10.md)）。
- **状态栏 Skill 段**：激活数随 `load_skill` 实时变化（`Skill:3` → `Skill:4`），
  不必等本轮流式结束。
- **作者期扩展的体检段**在同一份报告里产出（见 [ext-skill-authoring.md](ext-skill-authoring.md)）。
