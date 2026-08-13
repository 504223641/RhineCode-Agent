# 少一档「写放行、执行仍确认」——用户只能靠切放行档来止痛

> 状态：待开工 · **建议走 `/spec`**（动的是权限档位语义，四份文档进
> `docs/extensions/accept-edits/`）· 预计 1 天
>
> 建议分支：`perm-accept-edits`（从 `main` 起）
>
> ⚠ **依赖第 2 条（保护路径层）**，理由见下面「为什么必须排在 2 之后」。

## 症状

现在 `/perm` 只有三档，而这三档把**「改文件」与「跑命令」绑在一起调**：

| 档位 | 改文件 | 跑命令 |
| --- | --- | --- |
| 严格 | 拒绝 | 拒绝 |
| 默认 | **弹面板** | **弹面板** |
| 放行 | 放行 | **放行** |

于是一个受不了「改五个文件弹五次面板」的用户，唯一的出路是切到放行档——
**而那同时把 `run_command` 也放开了**。

这两类动作的危险度差一个数量级：

- 写文件搞错了 → `git checkout` 就回来了
- 跑命令搞错了 → 装了个东西、污染了环境、往数据库写了脏数据，**没有 undo**

②路径沙箱兜得住「写」（再放行也出不了工作目录），但兜不住「执行」——
`run_command` 起的子进程用绝对路径访问工作目录外的文件是**已登记的边界**
（`CLAUDE.md` 已知后续工程项 #4，要 OS 级沙箱才解决）。

**所以现在的档位设计有一处真实的错配：用户为了省一次确认，付出的是那道
兜不住的边界。** 这不是假设——Claude Code 把 `acceptEdits` 单独列一档、
并且是三档循环里的第二档，就是因为这是最高频的需求。

## 建议做法

新增一档 `PermissionMode.ACCEPT_EDITS`，严格程度**介于 DEFAULT 与 PERMISSIVE
之间**。改动集中在 `engine.decide` 的第④层：现在那里是

```
STRICT      → DENY
PERMISSIVE  → ALLOW（url 类例外，仍 ASK）
其余         → ASK
```

新档要**多看一眼 `request.kind`**：

| kind | ACCEPT_EDITS 下 |
| --- | --- |
| `write_path` | **ALLOW** |
| `command` | ASK |
| `url` | ASK（沿用放行档那条例外的同一个理由——未建立域名白名单时没有等价兜底） |
| `other`（MCP / 协作工具等） | ASK（保守，与默认档一致） |

①②③一个字不动，所以「隔离/预授权/Hook 只能收紧」那些论证全部原样成立。

### ⚠ 不要抄 Claude Code 那条「顺带放行 mkdir / touch / rm / mv / cp / sed」

官方 `acceptEdits` 确实放行这七个 shell 命令，但那是**它的妥协**：Claude Code
里没有覆盖这些语义的结构化工具，模型只能靠 shell 建目录、移文件。

**RhineCode 有 `write_file` / `edit_file`，不需要这条。** 抄过来的净效果是在
`run_command` 上开一个口子——而 `rm` 就在那个清单里。

## 为什么必须排在第 2 条（保护路径层）之后

**这一条单独上线会把第 2 条那个缺口放大。**

第 2 条讲的是：模型能写 `.rhinecode/permissions.yaml` / `hooks.yaml` /
`agents/` 给自己提权，现在唯一的实际拦截是默认档那次确认面板。

而 `acceptEdits` 恰恰是**「写文件不再弹面板」**。两条的先后顺序决定结果：

- **2 先做**：②″保护路径排在③之前、更排在④之前，所以哪怕在 `acceptEdits`
  档下，写 `.rhinecode/hooks.yaml` 照样弹面板。**两条互相加强。**
- **3 先做**：用户切到 `acceptEdits` 想少弹几次面板，顺带把「模型改写自己的
  权限配置」也变成静默的了。**净效果是安全性下降。**

⚠ 两条改的是**同一个文件**（`permission/engine.py`，一个在②与②′之间加层、
一个改④层分支）。要么串行（2 → 3），要么并成一轮做完——**不要并行**。

## ⚠ 新增一个 `PermissionMode` = 改六处

这是本条最容易出错的部分。逐个列出来，**做完请补进 `CLAUDE.md` 的
「成对维护点」**（现在那里没有这一条，因为枚举从 C6 起就没动过）：

| 位置 | 漏改的后果 |
| --- | --- |
| `permission/models.py` 的 `PermissionMode` | —— |
| `permission/engine.py` 的 `_MODE_ORDER` | ⚠ **见下，这条最危险** |
| `permission/engine.py` `decide()` 第④层分支 | 新档落进 `else` → 行为等同默认档，**不报错**，用户以为切了个寂寞 |
| `conversation.py` 的 `_PERM_CYCLE` + `_PERM_LABELS` | 循环跳过它 / 状态栏显示成英文原值 |
| `subagents/report.py` 的 `_MODE_LABELS` | `/agents` 里那档显示成 `accept_edits` 而不是中文 |
| `/perm` 的命令描述（`commands/builtins.py`） | 帮助里少一档 |

### `_MODE_ORDER` 那条单独说

它现在是硬编码的 `{STRICT: 0, DEFAULT: 1, PERMISSIVE: 2}`，而
`narrower_mode` 用它实现**「子 Agent 只能收紧不能放宽」这条安全承诺的全部**。

插一档进中间意味着**要给 `PERMISSIVE` 重新编号**（2 → 3）。两种错法：

- **忘了加新档** → `KeyError`，**当场炸**，安全（这是好消息）
- **加成 `{... ACCEPT_EDITS: 2, PERMISSIVE: 2}`**（忘了给 PERMISSIVE 挪号）
  → `narrower_mode` 里那个 `<=` 遇到平局返回 `a`，于是
  `narrower_mode(PERMISSIVE, ACCEPT_EDITS)` 返回 **PERMISSIVE**——
  **一个声明 `accept_edits` 的角色在放行档主对话下拿到了放行档。**
  静默提权，测试不红，界面看不出来。

⚠ 护栏建议直接钉死「顺序值两两不同」+「`narrower_mode` 对全部
`len(PermissionMode)²` 组合的结果表」，而不是只测新档那几条。

## 一个要在 `/spec` 里定的问题：循环还是点名

`/perm` 现在是 `默认 → 严格 → 放行 → 默认` 三档循环（注意**它本来就不是按
松紧排序的**，第一下是往严格走）。加到四档之后，按一次要走三下才回到原处。

三条路：

- **A**：维持循环，顺序改成按松紧排 `严格 → 默认 → 接受编辑 → 放行`
  （可预测「按一次更松」，但改变了既有手感）
- **B**：维持现有循环顺序，新档插在放行之前
- **C**：`/perm` 支持带参直接点名（`/perm accept-edits`），循环保留作快捷方式

倾向 **C + A**：四档之后循环本身就不好用了，点名是更直接的入口；而循环顺序
按松紧排是唯一说得清的口径。⚠ 带参会动 `CommandSpec` 的参数提示与补全，
记得那是 C10 的单一注册来源，改一处即可。

## 验证

单测之外必须真机跑：

1. 切到新档，让模型连改三个文件 —— **一次面板都不弹**
2. 同一档下让模型跑 `python -m unittest` —— **弹面板**（这是本条的全部意义）
3. 同一档下让模型写 `.rhinecode/hooks.yaml` —— **弹面板**（第 2 条的落点，
   两条一起做时这是最关键的一条判据）
4. 委派一个声明 `permission_mode: accept_edits` 的角色，主对话在**默认档** ——
   `/agents` 里显示生效值是「默认」而不是「接受编辑」（收紧承诺的反证）

## 一键开工 Prompt

```
先 git branch --show-current，在 main 上就 git checkout -b perm-accept-edits。

⚠ 先确认 docs/todo/2-perm-protected-paths.md 已经做完（或决定两条一起做）。
理由：这一条让"写文件不弹面板"，而第 2 条那个缺口的唯一实际拦截就是那次面板。
顺序反了会让安全性净下降。两条改的是同一个文件 permission/engine.py，不要并行。

读 docs/todo/3-perm-accept-edits.md，然后走 /spec 做「acceptEdits 档位」，
四份文档进 docs/extensions/accept-edits/。

问题：/perm 三档把"改文件"与"跑命令"绑在一起调，用户想少弹几次面板只能切
放行档，而那同时放开了 run_command——②沙箱兜得住写、兜不住执行
（已知项 #4 的边界）。

做法：新增 PermissionMode.ACCEPT_EDITS，严格程度在 DEFAULT 与 PERMISSIVE 之间，
只改 engine.decide 第④层：write_path 判 ALLOW，command / url / other 仍判 ASK。
①②③一字不动。

⚠ 不要抄 Claude Code 那条"顺带放行 mkdir/touch/rm/mv/cp/sed"——那是它没有
结构化写工具的妥协，我们有 write_file/edit_file，抄过来只在 run_command 上
开口子而且 rm 就在清单里。

⚠ 新增枚举要改六处，文档里有表。最危险的是 _MODE_ORDER：插中间要给
PERMISSIVE 重新编号，忘了挪号会让两档序号相同，而 narrower_mode 的 <= 遇平局
返回 a —— 声明 accept_edits 的角色在放行档下拿到放行档，静默提权。
护栏要钉"顺序值两两不同"+ narrower_mode 的全组合结果表。

循环顺序 vs 带参点名在文档里有三条路，/spec 阶段跟用户定。

验证四步都要跑，第 2 步（跑命令仍弹面板）与第 4 步（声明档位不产生提权）
是两个方向的反证，不可省。
做完这条后把 docs/todo/3-perm-accept-edits.md 删掉，
并重排 docs/todo/ 下其余文档的序号。
```
