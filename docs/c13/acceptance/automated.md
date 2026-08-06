# C13 验收报告（自动化部分）

跑于 2026-08-07，分支 `c13`。对应 [`../checklist.md`](../checklist.md) 里标 🤖 的条目。

**总计：50 条自动化判据全部通过。** 全量测试 1745 项通过、skipped 仍为 4
（改动前 1478 项 / skipped 4，新增 267 项）。

---

## 一、角色定义与加载

| 判据 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| AC1a 最小定义可用 | `test_subagent_parser.MinimalDefinitionTest` 4 项通过：只写 `description` 的定义解析成功，`name` 回落文件名，`max_turns` 取 15，`warnings` 为空元组 | 唯一必填项确实只有 `description` |
| AC1b 两种写法等价 | 同一份定义只改 `disallowed-tools` / `disallowed_tools`，两个 `AgentSpec` **相等**；`max-turns` / `permission-mode` 同样 | 键名归一生效，从别处复制来的定义不会因写法失败 |
| AC1c 未支持字段 | `UnsupportedFieldsTest` 遍历 `UNSUPPORTED_FIELDS` 全部 8 项，每项都产出**含该字段名**的警告；带这些字段的角色 `description` 仍正确解析 | 只警告不阻断，且警告具名——用户知道具体哪一项没生效 |
| AC2 层级覆盖 | 项目级与用户级同名 `rev`，`tools` 故意写不同。生效的 `source` 是 `PROJECT` **且 `tools == ("read_file",)`**（项目级那份的值） | 不只是 source 对，读的确实是项目级那份配置 |
| AC3a 单文件失败不阻断 | 坏 YAML + 好文件同目录：`specs` 含 `good`，`errors` 恰 1 条且 `path.name == "bad.md"`；三个目录都不存在时 `errors == ()` | fail-safe 成立 |
| AC3b 同层重名可见 | `aaa.md` 与 `zzz.md` 同名 `dup`：生效的是「第一份」，`zzz.md` 进 `shadowed`，错误消息里**同时出现两个文件名** | 用户能定位到该改哪一个 |
| AC5 开箱可用 | `test_subagent_builtin.UsableOutOfTheBoxTest`：`explorer` 在 `ToolRegistry.default()` 下 `is_empty` 为假、`unresolved == ()`、`allowed == set(spec.tools)` | 缺省配置下 explorer 能启动 |
| 内置样板自检 | `explorer` 的 `warnings == ()`、工具全只读、`permission_mode` 为 STRICT、`description` 长度 111 且「需要」出现在前 40 字符内 | 它作为唯一范例自身合规 |

## 二、委派入口与系统提示

| 判据 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| AC6a 工具列表稳定 | 加载 0 个与 5 个角色两种情况下 `registry.names()` 集合**完全相等** | 角色数量不影响模型看到的工具数 |
| AC7a 系统提示口径 | **走真实装配链路**（`test_subagent_e2e`）：假 Provider 收到的子 Agent 侧 `system` 参数 **逐字等于** `"你是查找员。最后一段必须是自包含的结论。"` | 确实只给角色正文，不含主对话八模块 |
| AC7b 不可信内容段 | 工具集含 `web_fetch` 时该段出现在请求体里；不含时不出现；**声明了但被黑名单减掉时也不出现**（判据取最终工具集而非声明） | web_fetch 扩展那个坑没有重演 |
| 清单进系统提示 | 端到端：`provider.systems[0]` 含 `"finder"` 与 `"而不是自己动手做"` | 清单真的到了模型手里，且是指令式口径 |
| 同口径护栏 | `SameVoiceTest` 4 项：清单表头与工具描述**都**含「而不是自己动手做」「不必明确说」「倾向委派」「上下文」 | 两处文本不会一强一弱 |

## 三、运行时隔离与权限

| 判据 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| AC10 结论提取 | 子历史最后一条 assistant 是「我打算这样做：先读文件……」且 `stop_reason` 为 CANCELLED 时，回流文本**不含**「我打算这样做」、含「取消」 | C11 那个「前言被当成结论」的坑没有重演 |
| AC11a 全局禁止 | 遍历 `GLOBAL_DENIED_TOOLS` 逐个断言不在 `allowed` 里；**白名单里显式写 `run_agent` 也翻不过去**，它反而进了 `unresolved` | 安全边界确实排在用户配置之前 |
| AC11b 白名单外拒绝 | 白名单只有 `read_file` 的角色调 `write_file`：工具的 `executed` 计数为 **0** | 不只是没发给它，调了也真的没执行 |
| AC12 空工具集 | `tools` 全写错时：`ok=False`、文本含那些错误名、**Provider 调用计数为 0、任务表为空、线程数没涨** | 确实不起线程不发 API |
| AC13a 非交互拒绝 | 判 ASK 的调用被拒，`ask` 回调**调用次数为 0**；回灌文本含「非交互」、**不含**「这是用户的决定」 | 走的是新分支，且面板不会从后台线程弹出 |
| AC13b allow 规则放行 | 加一条 `allow: Write(*)` 后同一任务的 `executed` 变为 1 | 用户配了规则就能写 |
| AC13c 不禁下一轮工具 | 非交互被拒后第 2 轮 `tools` 非空；**反证**：`interactive=True` 同场景下第 2 轮 `tools` 为 `None` | 新旧两条路径确实分开了 |
| AC14a/b 权限只能收紧 | 角色声明 PERMISSIVE + 主对话 DEFAULT → 派生实例 `mode` 为 DEFAULT；**跑完后主引擎 `mode` 仍是 DEFAULT** | 声明放行档不提权，且后台线程没改主对话档位 |
| AC14c 报告双值 | 报告文本同时含「放行」「默认」「受主对话档位限制」；两者相同时那句解释**不出现** | 用户看得出差异从何而来 |
| AC15 不继承预授权 | 主引擎 `grant_turn_rules` 一条后启动子 Agent：派生实例 `turn_rules == []`，主引擎那条**仍在** | 回合级预授权不跟着委派跑出去 |
| AC15b 继承会话规则 | 派生后主对话再 append 一条 session rule，子 Agent 的 `session_rules` **是同一个对象**且能看到 | 用户明确授予的放行对子 Agent 生效 |
| AC9b 独立预算 | 角色 `max_turns: 2` 时 Provider 调用次数 ≤ 4（不是主对话的 25） | 预算各算各的 |

## 四、后台任务

| 判据 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| AC17a 显式后台 | **端到端**：子 Agent 睡 0.15 秒，主对话 `submit_user_message` 耗时 < 0.12 秒返回，工具结果含「后台」 | 确实没阻塞 |
| AC17b 超时转后台 | 超时打补丁成 0.05 秒 + 慢子 Agent：返回 `backgrounded=True`，任务 `status` 仍为 RUNNING、`cancel_event` **未置位**；等待后变 COMPLETED | 转后台不是取消，任务照跑 |
| AC17c 手动切（可自动化部分） | 另起线程进入前台等待后调 `request_background()`：等待方在 5 秒内返回且 `backgrounded=True` | 切换机制成立（真实按键手感留给人眼） |
| AC18 并发上限 | 塞 3 个 RUNNING 后第 4 次委派 `ok=False`，文本含那 3 个的 `task_id`，Provider 调用计数 0 | 明确失败且信息足够 |
| AC19a/b 通知与交付 | 两条消费线各自幂等；先 `drain_notifications` 再 `take_deliverables`，**两者都拿得到** | 合成一条会导致的那个故障不存在 |
| **AC19c 再下一轮仍在** | **端到端**：连发三轮，第二轮与**第三轮**的请求体里都含结论原文 | 结论真的进了历史，不是一次性提醒 |
| 交付恰好一次 | 第三轮请求体里 `<subagent-result` 出现次数为 **1** | 不重复交付 |
| AC20b 清空取消 | `/clear` 返回文案含「2」，两个任务的 `cancel_event` 都置位，`history == []` | 先取消后清空的顺序成立 |
| 文案零回归 | 无任务时 `clear()` **逐字等于**「对话历史已清空」 | 既有逐字断言不受影响 |
| AC20c 取消入口 | 单个 / 全部 / 未知标识 / 已结束任务四种输入各有对应文案 | 取消可用 |
| AC25d 并发安全 | 20 线程并发 create+bump+finish：0 异常、20 条终态记录、`turns` 全为 5、`usage_tokens` 全为 50、标识无重复；10 线程并发消费 30 条结论，**恰好各取一次** | 任务表线程安全 |
| AC25c 异常兜底 | Provider 抛异常时任务变 FAILED、`done_event` 已置位、异常不逃逸 | 不会永远停在「运行中」 |

## 五、Hook、trace 与零回归

| 判据 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| **AC22 Hook 生效** | 装一条拦截型 `pre_tool_use`，子 Agent 调 `run_command "git push origin main"`：`hooks.seen` 含 `"run_command"`，工具 `executed` 为 **0** | 主 Agent 无法靠委派绕过拦截规则 |
| AC22b 不吞注入队列 | 跑完子 Agent 后假 Hook 管理器的 `injections` **仍非空** | 主对话的注入型 Hook 不会凭空消失 |
| AC23 trace 可复现 | **端到端**：产出 `subagent_start` / `subagent_end`；存在 `subagent:` 作用域；`api_request` 在 `subagent:*` 与 `main` 下**各有至少一条**；start 事件含 kind/agent/task_id/task/tool_count，end 含 status/turns/stop_reason | 一次委派可从记录完整复现，且不与主对话混算 |
| AC24a/b/c 零回归 | 服务为 `None` 时七个接入点全部安全降级；`RunOptions()` 的 `interactive` 缺省为 True；既有 perm(194) / skill(268) / trace(126) / command(85) / tui(35) 全绿 | 不用委派的用户行为不变 |
| 架构不变量 | `rhinecode/tools/__init__.py` 仍只有 docstring；`grep -rn "from rhinecode.\(conversation\|tui\|commands\)" rhinecode/subagents/` **零输出** | 第四组包级互依未成环，`subagents` 依赖方向干净 |

## 六、端到端场景

| 场景 | 机器判到了什么 | 据此做的判断 |
| --- | --- | --- |
| 场景 1（主线·前台） | 真实 `build_app` 装配 → 模型调 `run_agent` → 子 Agent 用角色正文作 stable、工具集为白名单两项 → 结论回灌进第 2 轮请求体 → `/agents` 报告含角色、档位与任务标识 | 前台委派全链路成立 |
| 场景 2（后台闭环） | 显式后台 → 主对话 <0.12s 返回 → 子 Agent 完成 → 通知线取到 1 条 → 第二、第三轮请求体都含结论且只出现一次 | 后台闭环成立，含最易错的 AC19c |
| 场景 3（权限边界） | 缺省档下写入被自动拒绝、回灌引导性文案、主引擎档位未变；配 allow 规则后同一任务成功写入 | 权限边界与逃生通道都成立 |
| 场景 5（失败路径） | 角色不存在时失败文本列出全部可用角色名；`tools` 全写错时列出那些错误名 | 模型有足够信息自我纠正 |
| 场景 6（并发上限） | 第 4 次委派明确失败并列出在跑的 3 个 | 上限行为可预期 |

**场景 4（分支式）** 的自动化部分已覆盖（父历史可见、快照是副本、强制后台），
但**未在真实装配链路下跑过**——它需要主对话先积累一段上下文，
留给真实模型验收时一并观察。

---

## 未通过

无。

## 已知未覆盖

| 项 | 原因 |
| --- | --- |
| AC4 启动提示的**醒目程度** | 机器只能验文本存在，程度是人眼判据 |
| AC6b / 场景 7 模型**主动**委派 | 只有真实模型能答 |
| AC17c `Ctrl+B` 真实按键 | 需真实终端 |
| AC21a 聊天区行数 | 需 TUI 驱动设施跑完整交互，本轮用「子 Agent 事件不转发给调用方」的结构判据替代 |
| AC25e 长时间运行手感 | 需人眼 |
| 场景 8 结论质量 | 需人眼 |
