# 模型能改自己的权限配置——缺一层「保护路径」

> 状态：待开工 · **建议走 `/spec`**（动的是安全边界，四份文档进
> `docs/extensions/protected-paths/`）· 预计 1 天
>
> 建议分支：`perm-protected-paths`（从 `main` 起）

## 症状

`.rhinecode/` 下有一批文件，**它们的内容决定「以后会发生什么」**：

| 文件 | 改了之后 |
| --- | --- |
| `permissions.yaml` / `permissions.local.yaml` | 模型给自己加 `allow` 规则 |
| `hooks.yaml` | **Hook 动作直接执行、不过五层管线**（`CLAUDE.md` 安全边界 c12 第 ② 条原话：本章最大的攻击面） |
| `mcp.yaml` | 启动一个外部程序，并把它的工具注册进工具中心 |
| `agents/*.md` | 造一个新角色，或给既有角色加工具白名单 |
| `skills/*.md` | 一段会被自动加载、指挥后续所有行为的文本 |

**现在这五处的写入没有任何特殊待遇。** 走一遍管线看：

- ①黑名单——只管命令类，`write_file` 不进这层
- ②沙箱——`.rhinecode/` **就在项目根内**，`is_within_workspace` 判它合法。
  （`path_guard._RUNTIME_ARTIFACT_RELATIVE` 那三条只排除 `sessions` /
  `context` / `traces`，而且只对**搜索类**工具生效，写入完全不受影响）
- ③规则——用户没写 deny 就不表态
- ④模式——放行档直接 ALLOW；默认档弹一次面板，而用户点了「永久放行」
  之后连面板都没了

所以**放行档下，或者存在一条宽松 allow 规则时，模型可以直接改写自己的权限配置**。

⚠ 一条重要的限定，别把危害说过头：这五份配置**都是装配期加载**的
（`/hooks` 与 `/agents` 都刻意不做 reload），所以不是即时提权，
是**持久化提权、下次启动生效**。但用户当然会重启——而且重启之后
`/hooks` 的项目级启动提示会照常列出那条新规则，用户看到的是
「我的项目里怎么多了条 hook」，不会想到是上一段对话里模型自己写的。

## 为什么值得单独做

**它绕过的不是某一层，是整套论证的前提。**

C11 / C12 / C13 / C14 / C15 的安全边界全都建立在同一句话上：**配置由人写下，
模型只在配置划定的范围内行动**。所以那些论证才成立——「Skill 拿不到权限捷径」
「`allowed-tools` 翻不过前两层」「子 Agent 的能力只会比主对话小」。

一旦模型能改配置，这些话的主语就变了。而这条路径**在现有的任何一层里都不违规**：
每一步都是合法的 `write_file`，落在工作区内，没碰黑名单。

Claude Code 对同一个问题的做法叫 **protected paths**：`.claude`、`.git`、
`.bashrc`、`.pre-commit-config.yaml`、`.npmrc` 这类文件的写入**永不自动放行**，
而且——这是关键——**这道检查跑在 allow 规则之前**，settings 里写
`Edit(.claude/**)` 也盖不过它（官方文档
[Choose a permission mode](https://code.claude.com/docs/en/permission-modes)
的 Protected paths 一节写得很清楚）。

**这跟本项目「②′网络边界层必须排在③之前」是同一条理由的第二次。**
排在③后面的话，一条 `allow: Write(.rhinecode/**)` 就把它整个跳过去了。

## 建议做法

在管线里加一层 **②″保护路径**，位置：**②沙箱之后、②′网络之前**
（②′只管 url 类，两者不会互相干扰，排在它前面还是后面都行，
但**必须在③之前**）。

判定很简单：`request.kind == "write_path"` 且路径落在保护清单内 → 返回 **ASK**。

⚠ **是 ASK 不是 DENY。** 「让模型帮我加一条 deny 规则」是完全正当的需求，
DENY 会把它变成做不到；这层要的只是**人眼必须过一遍**。

### 保护清单（初稿，`/spec` 阶段再定）

**保护**：

- `.rhinecode/permissions.yaml`、`.rhinecode/permissions.local.yaml`
- `.rhinecode/hooks.yaml`
- `.rhinecode/mcp.yaml`
- `.rhinecode/agents/`（整个目录）
- `.rhinecode/skills/`（整个目录）
- `.git/`（尤其 `.git/hooks/`——往那儿写一个 `pre-commit` 等于拿到
  「下次提交时执行任意代码」；`.git/config` 能改 push 目标）

**刻意不保护**（写清楚理由，免得后来的人当成漏改顺手补上）：

- `.rhinecode/worktrees/` —— C14 的隔离子 Agent **就在这里面干活**，
  保护它等于把隔离功能整个关掉
- `.rhinecode/sessions/`、`context/`、`traces/` —— 运行期产物，
  改了不影响「以后会发生什么」（而且这三个已经在
  `_RUNTIME_ARTIFACT_RELATIVE` 里，搜索时就跳过了）
- `.rhinecode/memory/` —— 记忆落盘是**内部可信写盘**、压根不走工具管线
  （`CLAUDE.md` 安全边界 c9），这层管不到它也不需要管

## ⚠ 四个已经能预见的坑

### 1. 判定必须基于 `request.cwd`，不是主项目根

这是最容易写错的一条，而且**写错了不报错**。

隔离子 Agent 的 `cwd` 是 `.rhinecode/worktrees/<名字>`——**从主项目根看，
它整个人都在保护目录里**。若按主项目根做前缀判断，隔离子 Agent 的
**每一次写入**都会命中保护层，而它是非交互的（判 ASK 自动拒绝），
结果是隔离委派全部静默失败、界面上只看到「子 Agent 什么都没做出来」。

正确口径与②层完全一致：**以 `request.cwd` 为根去拼保护路径**。
隔离工作区里没有 `.rhinecode/`（被忽略规则排除、checkout 不出来），
所以隔离子 Agent 天然一条都命中不了——这正是想要的。

⚠ 顺带：`cwd` 缺失时**拒绝，不要回退到主项目根**（spec N2 的既有决定）。

### 2. 「永久放行」会变成一个骗人的按钮

确认面板四选项里的「本会话放行 / 永久放行」走的是
`permission/adapter.py` 的 `to_allow_rule`，写出一条规则进③层——
**而②″排在③之前，那条规则永远不会被求值**。

用户会看到：点了「永久放行」，下次还是弹。**这比不做还糟**，
因为它让一个明确的用户决定看起来失效了。

三条路，`/spec` 阶段挑一条：

- **A**：保护路径的面板**不提供**「永久放行」这个选项（最诚实，但要改面板）
- **B**：提供，但它写的是一份**独立的保护路径豁免清单**，②″自己认（
  对齐 Claude Code 那句 "Yes, and allow Claude to edit its own settings
  for this session"）
- **C**：只提供「本会话放行」，用②″层自己的会话态记，不落盘

倾向 **C**——落盘的豁免本身就是一份「能改变以后会发生什么」的配置，
绕了一圈又回到原问题。

### 3. 新增 `Layer` 枚举值 = 改三份表

`CLAUDE.md` 成对维护点已登记：`permission/models.py`（枚举）+
`trace/reader.py` 的 `_LAYER_NAMES` + `tui/widgets.py` 的
`ConfirmPanel._LAYER_LABELS`。**三份表刻意不合一**（合并会让只依赖标准库的
`trace` 叶子包反向依赖 `permission`）。

好消息：这条有护栏钉着（`test_trace_reader.py` 与 `test_web_bootstrap.py`
里两条遍历 `Layer` 的断言），**漏改当场红**，不用担心静默。

### 4. 顺序护栏要用「宽 allow + 保护路径」构造

抄②′那条的教训（`test_perm_network_layer.py::PipelineOrderGuardTests`
的注释里写得很清楚）：用「③层没命中」的形态构造护栏，**在错序下照样通过**，
发现不了顺序错误。

这一层的护栏必须是：**写一条 `allow: Write(.rhinecode/**)`，
然后断言写 `hooks.yaml` 仍然判 ASK**。②″排到③后面的话这条当场红。

## 验证

单测之外，**必须真机跑一次**（这是 C11 起每一轮都在重复的教训）：

1. `/perm` 切到放行档，让模型「往 `.rhinecode/hooks.yaml` 加一条规则」
   —— 确认弹面板，而不是直接写
2. 写一条 `allow: Write(.rhinecode/**)` 进 `permissions.yaml`，重跑第 1 步
   —— 确认**仍然**弹面板（这条是顺序论证的落点）
3. **委派一个 `isolation: worktree` 的子 Agent 让它写文件** —— 确认它照常
   能写，没被保护层误伤（坑 1 的反证，不跑这条就验不到）
4. 让模型正常改一次业务代码 —— 确认没有多出任何一次面板

## 一键开工 Prompt

```
先 git branch --show-current，在 main 上就 git checkout -b perm-protected-paths。

读 docs/todo/2-perm-protected-paths.md，然后走 /spec 做「保护路径层」，
四份文档进 docs/extensions/protected-paths/。

问题：.rhinecode/ 下的 permissions.yaml / hooks.yaml / mcp.yaml / agents/ /
skills/ 以及 .git/ 的写入没有任何特殊待遇——②沙箱判它们在工作区内、
③规则不表态、④放行档直接 ALLOW。于是模型能改写自己的权限配置
（装配期加载，属"持久化提权、下次启动生效"）。这绕过的不是某一层，
是 C11–C15 全部安全论证共同的前提「配置由人写下」。

做法：管线里加一层②″保护路径，排在②沙箱之后、③规则之前
（必须在③之前，理由与②′网络边界层同源——排在后面的话一条
allow: Write(.rhinecode/**) 就整个跳过去了）。判 ASK 不判 DENY。

四个坑在文档里，逐条读，尤其第 1 条（判定必须基于 request.cwd 而不是
主项目根，否则 C14 的隔离子 Agent 会被自己的工作区路径全部误伤，
而且它非交互、判 ASK 即自动拒绝，表现为静默失败）与第 2 条
（「永久放行」写进③层的规则永远不会被求值，会变成一个骗人的按钮）。

验证四步都要跑，第 2 步与第 3 步是两个方向的反证，不可省。
做完这条后把 docs/todo/2-perm-protected-paths.md 删掉，
并重排 docs/todo/ 下其余文档的序号。
```
