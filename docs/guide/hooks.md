# Hook 系统

> RhineCode 用户手册 · [返回手册目录](README.md) · [返回项目 README](../../README.md)

## Hook 系统

把「触发条件明确、动作固定、每次都一样」的重复劳动交给机器：AI 改完 `.py` 就自动跑格式化、
拦住某种参数组合的命令、每个回合开始时给模型注入当前分支、干完活响一声。

### 写一条规则

在 `~/.rhinecode/hooks.yaml`（或项目级 `<项目根>/.rhinecode/hooks.yaml`）里写：

```yaml
hooks:
  # ① 改完 Python 文件自动格式化
  - name: 格式化改动的 Python 文件
    event: post_tool_use
    if:
      all:
        - tool: edit_file
        - file_path: "**/*.py"
    action:
      type: command
      command: "python -m black ."
      timeout: 30

  # ② 拦住直接 push 到 main（退出码 2 = 拦截）
  - name: 禁止直接 push 到 main
    event: pre_tool_use
    if:
      all:
        - tool: run_command
        - command: "git push * main*"
    action:
      type: command
      command: "python .rhinecode/block_push.py"
```

三要素：**event**（何时，必填）+ **if**（条件，可省，省略即无条件）+ **action**（做什么，必填）。

### 十二个事件

| 层 | 事件 |
|------|------|
| 会话级 | `session_start` / `session_end` |
| 回合级 | `turn_start` / `turn_end`（主对话与子对话都触发，用 `scope` 区分） |
| 消息级 | `user_message`（含斜杠命令） / `assistant_message` |
| 工具级 | `pre_tool_use`（**唯一可拦截**） / `post_tool_use` / `post_tool_use_failure` |
| 系统级 | `pre_compact` / `post_compact`（只对第二层 LLM 摘要） / `notification` |

工具级事件的负载会把模型生成的工具参数**逐字展开成顶层字段**，所以能直接写
`command:` / `file_path:`，不必写 `tool_input.command`。

**后置事件只在「真的执行了」之后触发**——未知工具、参数非法、被规划阶段挡下、
权限拒绝、用户拒绝、被 Hook 自己拦下，这些都不产生 `post_tool_use*`。

### 条件

`if` 下写 `all`（全部满足）或 `any`（任一满足），**二选一，不混用不嵌套**。
每项是「字段: 模式」，四种形态：

| 形态 | 写法 | 说明 |
|------|------|------|
| 精确 | `tool: run_command` | 完全相等 |
| 通配 | `command: "git *"` | 命令类带**词边界**（`git *` 不命中 `github-cli`），路径类是 gitignore 风格 |
| 正则 | `command: "/^git (push\|reset)/"` | 一对 `/` 包裹，**非锚定** |
| 反向 | `tool: "!run_command"` | 值前缀 `!`，可与上面三种叠加 |

**命令类字段会整条 + 逐段双重检查**：`command: "git push *"` 同样能命中
`git add x && git commit -m y && git push origin main`。这一条不能省——
真实模型在一次普通的「改完提交推上去」请求里，自己就会写出那种复合命令。

字段不存在时该条件**判为不成立，反向匹配也不成立**（「字段不存在」不等于「不匹配」）。

### 四种动作

| 类型 | 说明 |
|------|------|
| `command` | 跑一条 shell 命令。**事件负载以 JSON 从标准输入喂给它**，配置里不做任何字符串插值。退出码 0 通过 / 2 拦截 / 其它算失败；stdout 可以输出 `{"decision": "deny"\|"ask", "reason": "..."}` |
| `prompt` | 往 `<system-reminder>` 注入一段文本，模型**下一次请求**可见，**只出现一次** |
| `http` | 发一个 HTTP 请求。响应**不参与任何决策** |
| `agent` | 启动子 Agent —— **本版本仅占位，不会真的运行** |

执行控制：`once: true`（本次进程内只跑一次，不持久化）、`async: true`（后台不等结果，
`pre_tool_use` 上**禁止**）、`timeout`（command 缺省 60 秒、http 缺省 10 秒）。

### ⚠️ 两条性质，写规则前必须知道

**一、Hook 只能收紧，不能放宽。** `pre_tool_use` 的结论只有三种——拦截、升级为人工确认、
不表态，**没有放行**。「升级」也只把权限管线的 ALLOW 变成 ASK，绝不把 DENY 降级。
照 Claude Code 文档写出的 `{"decision":"allow"}` 会被明确忽略。
要**放行**什么，用 `permissions.yaml` 的 allow 规则。

**二、拦截类 Hook 自己跑失败 = 拦截（fail-closed）。** 脚本崩了、超时了、退出码不对，
一律按拦下处理。这样一条上周就写坏的安全 Hook 会**立刻可见**，而不是静默失效
让你以为防线还在。其余事件的 Hook 失败只记录一行，不影响任何流程。

### 存放位置

| 位置 | 用途 |
|------|------|
| `~/.rhinecode/hooks.yaml` | 跨项目的个人自动化 |
| `<项目根>/.rhinecode/hooks.yaml` | 随仓库走、可提交、团队共享 |

**没有本地级。** 两层规则**全部生效**、不按层级覆盖，执行顺序是「用户级 → 项目级 → 层内声明序」。

⚠️ **项目级 `hooks.yaml` 里的命令会直接在你机器上执行**，不经模型、不经确认面板。
启动时会用醒目样式**逐条列出**它们的事件与完整命令串——请当作代码来评审。

### 排查

`/hooks` 列出全部规则（来源层、事件、条件、动作、**本次运行的触发次数与最近结论**）+
加载警告 + 配置位置。

「触发：0 次」是排查的主力信息：工具级事件的字段集是开放的（参数名取决于是哪个工具），
所以字段笔误在加载期发现不了，唯一的表现就是这条规则永远不命中。

开 `rhine --trace` 还能看到 `hook_dispatch`（**零命中也记**）与 `hook_execute` 两类事件。

