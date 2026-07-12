# C9 记忆系统（项目指令 · 会话存档 · 自动笔记）Checklist

> 每一项通过运行代码或观察行为来验证，聚焦系统行为。标注【单测】的项由 `python -m unittest` 对应用例覆盖；标注【手测】的项在 tmux / 真实终端里对真实 LLM 验证。

## 实现完整性

### 项目指令文件（RHINE.md）

- [ ] 三层拼接顺序与来源标注正确：用户级 → 项目 `.rhinecode/` → 项目根，各带来源路径；缺任意层其余正常（AC1）【单测 test_memory_instructions】
- [ ] @include 原地展开；第 5 层嵌套不展开；A↔B 循环正常结束；越界引用不展开；围栏代码块内 `@` 保持原文（AC2）【单测 test_memory_instructions】
- [ ] include 指向不存在文件 / 某层不可读：整体不抛异常、其余内容照常、errors 有可读提示（AC3）【单测 test_memory_instructions】
- [ ] 在 RHINE.md 写「回复开头带固定标记」，启动后**首条**消息回复即遵循（AC4）【手测】
- [ ] 无 RHINE.md 项目执行 `/init`：Agent 探索后弹 write_file 权限确认、生成合理的项目根 RHINE.md；已有 RHINE.md 时不覆盖、输出改进建议（AC5）【手测】

### 会话存档与恢复

- [ ] 聊 N 轮后 sessions 目录出现 `YYYYMMDD-HHMMSS-xxxx.jsonl`，行数与消息数一致、每行可独立解析、含 ts（AC6）【单测 test_memory_session + 手测确认目录】
- [ ] 强杀进程（任务管理器 / kill）后重启，已写入消息一条不丢（AC6）【手测】
- [ ] sessions 目录无任何 meta 文件；手工删档后 `/resume` 列表立即反映（AC7）【手测】
- [ ] `/clear` 后消息写入新档、旧档不变（AC8）【单测 on_clear + 手测】
- [ ] `/resume` 列表含编号/ID/标题/消息数/时间；`/resume <编号>` 载入后追问旧对话内容模型能答上（AC9）【手测】
- [ ] `rhine --continue` 启动即恢复最近会话（AC10）【手测】
- [ ] 坏行跳过；结尾不成对 tool_calls 组被丢弃且后续对话无 API 报错；超窗历史载入先触发 C8 压缩；最后消息改到 25 小时前则下一条请求带时间跨度提醒（AC11）【单测 test_memory_session / test_memory_manager；压缩与提醒手测佐证】
- [ ] 恢复后新消息追加进同一文件；再次载入全部历史完整（AC12）【单测 attach 后 append + 手测】
- [ ] 31 天前的档启动时被删、30 天内保留（AC13）【单测 cleanup_expired】

### 自动笔记与索引

- [ ] 对话给出明确偏好，自然停止后用户级 memory 出现 category 正确的 frontmatter 笔记；项目知识落项目级；期间输入不被阻塞（AC14）【手测】
- [ ] 笔记新增后索引同步多一行；>200 行索引注入时只取前 200 行（AC15）【单测 test_memory_notes / test_memory_manager】
- [ ] 笔记 LLM 调用失败：对话不受影响、`/memory` 显示最近一次更新失败（AC16）【单测假 provider 抛异常 + 手测断网佐证】
- [ ] 索引内容出现在「长期记忆」槽位（stable 通道，debug 或单测断言）；模型可 `read_file` 用户级笔记全文；其它工作区外路径仍被拦（AC17）【单测 test_memory_sandbox + 手测】
- [ ] `/memory` 输出含：RHINE.md 各层状态、include 展开情况、两级索引位置与超限标记、四类笔记数、最近更新结果、当前会话 ID 与消息数、锁状态（AC18）【手测对照】
- [ ] 笔记实际变更时界面出现低打扰提示；无变更轮次不打扰（AC19）【手测】
- [ ] anthropic/openai 配置：RHINE.md 注入与 `/resume`、`--continue` 可用；不产生笔记；`/init` 返回不支持（AC20）【手测（可用假 key 验证命令分支）】

### 并发防护

- [ ] memory 目录放新鲜锁：本轮笔记跳过、不等待；删锁后下轮恢复；正常写完锁被释放（AC21）【单测 test_memory_manager】
- [ ] 双实例：B `/resume` A 的会话被拒并提示占用；B `--continue` 顺延到下一个未锁定会话；锁改过期后 B 接管且旧锁清除（AC22）【单测 attach/startup + 手测双终端】
- [ ] 锁不可创建（只读目录）时笔记跳过不崩溃；过期残留锁不会永久堵死写入（AC23）【单测 test_memory_lockfile / test_memory_manager】

## 集成

- [ ] `build_default_prompt` 缺省参数时输出与 C8 完全一致（既有测试全绿），传入两参数时 stable 末尾出现两段内容且顺序 110 < 130【单测】
- [ ] `Agent.run` 的 recorder 缺省 None 时行为与 C8 一致（既有测试全绿）；传入时 assistant/tool 消息逐条回调【单测】
- [ ] 权限沙箱回归：`tests/test_perm_*` 全绿——只读白名单未放松写路径、未影响 glob/grep 的 Read deny 过滤【单测】
- [ ] `/resume` 载入成功后 `context_manager.reset()` 被调用（锚点复位）再做压缩评估【单测或代码走查 + debug 日志】
- [ ] 退出（正常 `/exit`）后 sessions 目录无残留会话锁【手测】

## 编译与测试

- [ ] `python -m compileall rhinecode tests` 无错误
- [ ] `python -m unittest discover -s tests` 全绿（含 C6/C7/C8 既有用例，无回归）

## 端到端场景

- [ ] **场景 1·冷启动记忆注入**：项目根写 RHINE.md（含 @include 一个子文件与一条可观测指令）→ 启动 → 首条消息回复遵循指令；`/memory` 显示三层中该层已加载、include 已展开。
- [ ] **场景 2·中断恢复**：正常对话（含工具调用）→ 强杀进程 → `rhine --continue` → 模型记得此前内容 → 继续对话后 `/resume` 再次载入验证新旧消息都在。
- [ ] **场景 3·越用越懂你**：告诉 Agent 一条长期偏好 → 自然停止后看到「已更新记忆」提示 → `/exit` 重启 → 新会话首条消息不重复交代，模型行为已体现该偏好（索引注入生效）。
- [ ] **场景 4·双实例并发**：两个终端同一项目各启动一个 RhineCode → B `/resume` A 的会话被拒 → 两边各自对话，笔记与索引文件无交错损坏（用编辑器检查 frontmatter 与索引行完整）。
- [ ] **场景 5·隔天回来**：把当前会话存档最后一条消息的 ts 改成 25 小时前 → `rhine --continue` → 首条请求的 debug 日志（或模型回复）体现时间跨度提醒。
- [ ] **场景 6·/init 引导**：在一个无 RHINE.md 的小项目运行 `/init` → 确认面板批准 → 生成的 RHINE.md 内容与项目实际情况相符（技术栈/命令正确）。
