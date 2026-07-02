# C8 上下文管理（两层压缩）Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为，与具体实现解耦。

## 实现完整性

- [ ] `Config.context_window` 存在，配置缺失/非法时回退默认 65536（验证：`load` 一份不含该字段的配置，读到 65536；写 `context_window: "abc"` 也回退默认，不抛异常）。对应 AC1。
- [ ] 估算函数在无锚点时对全部消息求和、有锚点时 = 锚点 + 增量（验证：`tests/test_context_estimate.py` 通过；构造 anchor_len 越界仍不报错）。对应 AC2。
- [ ] `Offloader.run` 把超单结果阈值的 tool 消息替换为「预览 + 文件路径」占位，且磁盘生成对应完整内容文件（验证：`tests/test_context_offload.py` 通过，占位含路径、文件内容 == 原始 output）。对应 AC4。
- [ ] 多个不超单阈值但合计超聚合阈值的 tool 结果按体积从大到小依次存盘直到达标，较小的保持原文（验证：offload 测试的聚合用例）。对应 AC5。
- [ ] 第一层压缩后所有 `role="user"` 消息逐字不变（验证：offload 测试断言 user 消息 content 未变）。对应 AC6。
- [ ] 对已存盘历史再次 `run` 不重复处理、磁盘不产生重复文件（验证：连续两次 run，第二次返回空通知、`count` 不增）。对应 AC7。
- [ ] `compute_retain_index` 保留尾部约 10K token 或 ≥5 条，边界回退到 `role="user"`、不切碎消息、不拆散 `assistant(tool_calls)`↔`tool`（验证：`tests/test_context_summarize.py` 通过）。对应 AC9。
- [ ] `parse_summary` 丢弃 `<<<正式摘要>>>` 之前的草稿、仅取正文（验证：summarize 测试）。对应 AC11。
- [ ] `reconstruct` 产出 `[user(摘要), assistant(边界提示), *retained]`，边界消息含「重新读取文件、勿照摘要脑补」语义（验证：summarize 测试断言结构与边界文本）。对应 AC10/AC12。
- [ ] `ContextManager` 连续 3 次摘要失败后置熔断并返回 circuit_break 通知，之后不再自动摘要（验证：`tests/test_context_manager.py` 用连返 error 的假 provider）。对应 AC12。
- [ ] `manual_compact` 在估算未超「窗口 − 3K」时返回 noop「上下文尚宽裕」，超过时执行摘要（验证：manager 测试两种阈值分支）。对应 AC8。
- [ ] `usage_report` 返回含估算 token / 上限 / 余量 / 已存盘数 / 熔断态的文本（验证：manager 测试断言字段出现）。对应 AC13。

## 集成

- [ ] `Agent.run` 新增 `context_manager` 参数默认 `None`，为 `None` 时行为与改动前完全一致（验证：既有 `tests/test_*loop*` / 全量测试仍全绿）。对应 AC15/N1。
- [ ] `context_manager` 非 `None` 时，每轮请求前调用 `before_request`（先 offload 后 summary），offload 使第二层看到的估算值下降（验证：manager 测试构造大结果，断言 `before_request` 内 offload 先降低估算再判断摘要阈值）。对应 AC3。
- [ ] loop 拿到 usage 后调用 `record_usage(usage, sent_len)`，`sent_len` 取 append reminder 前的 `len(history)`（验证：manager/loop 集成断言锚点被更新为该长度）。对应 AC2。
- [ ] `before_request` 产出的每条 `CompactionNotice` 被 loop 转成 `AgentEvent(NOTICE)` 并由 TUI 追加系统行（验证：`_do_stream` 存在 NOTICE 分支；端到端观察到压缩提示行）。对应 AC14/F17。
- [ ] `ConversationManager` 仅在 DeepSeek 工具模式构造 `ContextManager`；`/context`、`/compact` 在非工具模式返回「不支持」（验证：用 anthropic 配置构造，命令返回不支持文案）。对应 F 范围约束。
- [ ] `/clear` 同时清空 history 并 `ctx.reset()`（锚点/熔断/存盘集合归零）（验证：manager 有 reset；conversation.clear 调用它）。

## 编译与测试

- [ ] `python -m compileall rhinecode tests` 无错误。
- [ ] `python -m unittest discover -s tests` 全部通过（既有 + 新增 4 个测试文件）。
- [ ] 新增代码遵循中文注释规范，关键流程（估算锚点、两层顺序、保留边界、熔断）有设计意图注释（验证：人工过目 `context/` 各文件）。对应 N5。

## 端到端场景（tmux 或真实终端，DeepSeek 工具模式）

- [ ] **场景 1（第一层存盘）**：让 Agent 读取一个大文件（或跑输出很长的命令）使单个工具结果超阈值 → 观察到系统行提示「已存盘 …」，历史里该结果变为预览 + 路径，`.rhinecode/context/` 下出现对应文件，后续对话正常继续。对应 F4/F17。
- [ ] **场景 2（/context 只读）**：输入 `/context` → 展示估算 token、距上限余量、已存盘数；`/context` 出现在输入 `/` 的补全面板中；命令不改变任何状态。对应 AC13/F16。
- [ ] **场景 3（/compact 手动摘要）**：在积累了较长历史后输入 `/compact` → 走后台不卡界面，观察到摘要提示行；此后历史被替换为「摘要 + 边界提示 + 近期原文」，模型能据摘要继续任务且在需要细节时重新读取文件。对应 AC14/F12/F14。
- [ ] **场景 4（自动兜底不溢出）**：持续多轮工具操作把估算推过「窗口 − 13K」→ 自动触发摘要、对话不因溢出报错而中断，Agent 循环继续推进。对应 F8。
- [ ] **场景 5（熔断不死循环）**：构造摘要连续失败（如临时把窗口配得极小 + 断网/坏 key 使摘要请求失败）→ 3 次后熔断、给出可读提示，普通对话仍可进行、程序不崩溃、不陷入反复摘要。对应 F15/N2。
