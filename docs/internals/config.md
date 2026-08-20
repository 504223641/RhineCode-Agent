# 配置详解

> 本文件是 `CLAUDE.md` 的分册，**按需读取**，不随会话自动注入。
> 主文件只保留索引与「必须不请自来」的内容，细节在这里。

> 五份配置（`config` / `permissions` / `mcp` / `hooks` / Skill 定义）的字段、层级、
> 定位规则与首次运行的模板生成流程。面向用户的简版在仓库根 `README.md`。

`config.yaml`（git 忽略，从 `config.example.yaml` 复制）字段：`protocol`（**只支持 `deepseek`**——anthropic / openai 两个 Provider 已于 2026-08-20 删除，配成这两个值会在装配期报错并给出迁移说明；字段本身刻意保留，环境信息 / 状态栏 / trace 配置快照都在读它）、`model`、`base_url`、`api_key`。可选字段 `debug_log` 控制是否写入 `.rhinecode_debug.log` 缓存命中调试日志，默认开启。可选字段 `context_window`（c8）声明上下文窗口上限（token），作为「历史是否逼近溢出、何时压缩」的判断基准；缺省 / 非法 / 非正值都由 `_parse_int` fail-safe 回退默认 65536（不抛异常），故首次生成的模板**不含**此项——没写即用默认值，想调大/调小手动加一行即可。注意它只影响 RhineCode 的压缩时机，不改变模型真实上限，应贴近所用模型的实际上下文长度。 可选字段 `web_fetch_enabled`（web_fetch 扩展）是网络访问工具的总开关，缺省 `true`；设为 `false` 后工具不注册、系统提示不含「外部不可信内容」那条约束、权限规则里的 `WebFetch(domain:...)` 不做语法校验也不产生警告——**行为与该扩展之前逐字一致**。注意它走 `_parse_bool`：**非法值抛 ValueError**（与 `debug_log` 同口径），而不是像 `context_window` 那样回退默认——一个开关被写成 `maybe` 是明确的配置错误，静默回退会让用户以为自己关掉了网络访问而实际上没关。

可选段 `worktree`（c14）配置子 Agent 隔离工作区的两件事：`cleanup_days`（启动时清理多少天没动过的工作区，缺省 7；**0 或负数 = 不清理**——这是它与 `context_window` 的关键差别，后者遇到 0 会回退默认，而这里 0 是合法语义，靠 `_parse_int` 的 `allow_zero=True` 表达）、`copy` 与 `link`（环境初始化清单，两段都缺省为空）。整段可缺省；**非法结构一律回退默认、不抛错**——与 `web_fetch_enabled` 刻意不同口径，理由是这三项都不是安全开关，写错了最坏是「没清理」或「没复制文件」，而 `web_fetch_enabled` 写错会让用户以为关掉了网络访问却没关。

⚠ **清理与「有没有变更」的关系要说清楚**：`cleanup_days` 只决定「要不要考察某个工作区」，**不决定能不能删**。有未提交改动的工作区**永远不会**被清理（不论多老）；有提交、无未提交改动的只删目录、**保留分支**（成果仍可 `git checkout` 取回）。

配置定位（`rhinecode/config.py` + `__main__.py`）：命令**不带 `--config` 时缺省读用户级全局配置 `~/.rhinecode/config.yaml`**，使 `rhine` 在任意工作目录都能读到同一份配置（工作目录本身仍作为 AI 操作的项目根，二者互不影响）。该缺省文件不存在时首次运行会自动写入模板（`scaffold_user_config`，占位 `api_key: YOUR_API_KEY`）并提示后退出；模板占位符会被 `__main__` 单独拦下引导（占位符是非空串、能过 `load()` 校验，不拦会带假 key 启动）。显式 `--config <路径>` 优先且指向不存在的文件时按错误处理（不自动造文件）。

首次运行的模板生成是**四类统一**的（都在缺省流程、仅动用户级 `~/.rhinecode/`）：`__main__` 依次调 `config.scaffold_user_config` / `permission.config.scaffold_user_config` / `mcp.config.scaffold_user_config` / `hooks.config.scaffold_user_config`。语义分两类——`config.yaml` 必需，本次才生成时引导填 key 后退出；`permissions.yaml` / `mcp.yaml` / `hooks.yaml` 可选、模板**全注释**（`yaml.safe_load` 得 `None`、`_load_layer` 返回空集，与无文件等价），静默生成、**不因它们退出**，老用户下次运行会顺带补上。新增 config 模块要接入首次生成，需在其 `config.py` 加 `_CONFIG_TEMPLATE` + `scaffold_user_config` 并在 `__main__` 那段追加一次调用（**成对维护点**）。

权限规则配置（c6，可选，从 `permissions.example.yaml` 复制）：三层 YAML，`allow` / `deny` 列表，每条写成 `Tool(模式)`（如 `Bash(git *)`、`Read(config.yaml)`）。位置与优先语义——

- 用户级 `~/.rhinecode/permissions.yaml`（跨项目默认；首次运行自动生成全注释模板）
- 项目级 `<项目根>/.rhinecode/permissions.yaml`（随仓库走、可提交）
- 本地级 `<项目根>/.rhinecode/permissions.local.yaml`（git 忽略；「永久放行」自动写这里）

三层合并后按 **deny 永远优先**求值（不按层级覆盖）；命令用前缀+glob（`npm:*` 带词边界），文件用 gitignore 风格，**网络域名用 `WebFetch(domain:模式)`**（前导 `*.` 匹配任意深度子域但不含裸域；非前导位置的 `*` 不跨点，防 `example.*` 连带放行攻击者可注册的 `example.evil.com`）。

⚠ **域名规则有一处与其它规则不同的语义**：在**用户级或项目级**写下任何一条 `allow: WebFetch(domain:...)`，就等于声明「只许访问这些」——此后未列出的域名一律被**直接拒绝**，`/perm` 切到放行档也翻不过来。而**本地级**（「永久放行」自动写入的那份）**只放行、不建立白名单**。这是本项目对「层级不决定优先级」的唯一一处例外，理由：不区分的话，用户在确认面板点一次「永久放行」就会把自己锁死（其它域名从「弹确认」变成「硬拒且永不再问」，界面上无从恢复），且 Skill 的 `allowed-tools` 会反向收紧其它域名。写坏的域名规则按效果分两支处理、两支都偏严：allow 整条丢弃、deny **降级为整工具拒绝**。危险命令黑名单与路径沙箱是更靠前、不可被规则放开的硬防线。用户级模板首次运行自动生成（全注释=空、fail-safe 行为不变），不必再手动复制 `permissions.example.yaml`。

Hook 规则配置（c12，可选）：两层 YAML，顶层键 `hooks`（列表），每条写 `event` / `if` / `action` 三要素。位置——

- 用户级 `~/.rhinecode/hooks.yaml`（跨项目的个人自动化；首次运行自动生成全注释模板）
- 项目级 `<项目根>/.rhinecode/hooks.yaml`（**随仓库走、可提交**）

**没有本地级**——C6 的本地级是「永久放行」自动写入的授权记录，Hook 没有这种自动写入场景，规则永远由人手写。两层规则**全部生效**、不按层级覆盖，执行顺序固定为「用户级 → 项目级 → 层内声明序」。

容错 fail-safe：文件缺失视为空规则集；YAML 解析失败、顶层非映射、`hooks` 字段非列表三种情形**整层降级为空**并收一条警告；单条规则的七项校验任一不通过则**丢弃该条、其余照常加载**，启动绝不中断。全部警告经启动提示展示在首屏，也可用 `/hooks` 复查。

⚠ **项目级 `hooks.yaml` 的风险与项目级 Skill 不是一个量级**：Hook 的动作**直接执行**，不经模型、不经确认面板。因此每次启动都会**逐条列出**项目级规则的「事件 → 动作原文」（命令串与 URL 完整不截断，且刻意不做「只提示一次」的持久化）。评审它应与评审代码同等对待。模板本身全注释（解析后为空、与无文件等价），并在注释里写明「Hook 只能收紧不能放宽」与「拦截类 fail-closed」两条性质——多数用户只会读这份模板。

Skill 定义（c11，可选）：无需任何 YAML 配置，直接放 Markdown 文件即可——
- 项目级 `<项目根>/.rhinecode/skills/`（随仓库走、可提交、团队共享，优先级最高）
- 用户级 `~/.rhinecode/skills/`（跨项目默认）
- 内置 `rhinecode/skills/builtin/`（随包分发的 commit / review / test 三个样板）

同名按上述优先级**整份覆盖**（不做字段合并）。单文件型直接放 `<name>.md`；目录型放一个含 `SKILL.md` 入口的目录，目录内其余文件作为随附资源（模板/示例/脚本/参考文档），其绝对路径与清单会一并注入，模型按清单用绝对路径 `read_file` 读取（这两个目录在工作区外，不支持 glob/grep 枚举）。

文件级 `Read(...)` deny 不只约束 `read_file`：`ConversationManager` 会把权限过滤器注入 `glob_files` / `grep_content`，所以 `deny: Read(config.yaml)` 会同时阻止直接读取、grep 泄露内容、glob 输出路径。永久放行写入 `<项目根>/.rhinecode/permissions.local.yaml` 前必须先成功解析已有文件；如果 YAML 损坏或顶层不是映射，`append_local_allow` 会抛错且不覆盖原文件，上层保留本会话规则作为可用性 fallback。

MCP Server 配置（c7，可选，从 `mcp.example.yaml` 复制或由 `mcp_add_server` 自动写入）：两层 YAML，顶层键 `mcpServers`（`name → 条目`），stdio 型填 `command/args/env`、http 型填 `url/headers`，`env`/`headers` 值支持 `${VAR}` 展开。位置——用户级 `~/.rhinecode/mcp.yaml`（首次运行自动生成全注释模板）、项目级 `<项目根>/.rhinecode/mcp.yaml`，按名字合并、项目级覆盖用户级；自动添加时 `scope=auto` 默认项目级，只有用户明确说“全局/所有项目/以后都用”才写用户级。容错 fail-safe：启动加载时缺失/坏配置跳过并记错误，不阻断启动；自动写入时若目标 YAML 损坏、顶层结构异常或同名配置冲突，不会静默覆盖原文件。用户级模板首次运行自动生成（全注释=空、行为不变），不必再手动复制 `mcp.example.yaml`。
