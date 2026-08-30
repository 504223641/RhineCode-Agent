# 测试

> RhineCode 用户手册 · [返回手册目录](README.md) · [返回项目 README](../../README.md)

## 测试

```bash
python -m compileall rhinecode tests
python -m unittest discover -s tests      # 3,349 项，skipped 4（2026-08-30 实测）
python -m tests.run_parallel              # 同一批用例分 8 片并行跑，约 37 秒
```

默认跳过 4 项：真实模型端到端（需 `RHINE_E2E_LIVE=1` 与有效凭据）与「连续起停」
慢速专项（需 `RHINE_E2E_SLOW=1`）。**本机需装 git**——有预置依赖真实提交历史，
缺 git 时会明确报错而不是静默跳过（静默跳过会让那些场景假绿）。

覆盖面按层大致是：权限系统（命令/路径匹配、危险命令黑名单、deny 优先、四层管线、
预授权翻不过前两层）、MCP（配置合并、JSON-RPC、stdio 端到端起真实子进程、单 Server
隔离与重载）、上下文（估算、两层压缩、熔断与复位）、记忆（锁原语、RHINE.md 展开、
存档容错载入、`/resume` 回放）、Skill（解析、三层扫描、预授权翻译、加锁不变量的
**跨线程死锁护栏**、跨轮「第 N 轮激活第 N+1 轮生效」）、命令层（注册冲突 fail-fast、
分发、Tab 补全与高亮）、Hook（四种匹配形态、七项加载校验、四种动作、加锁不变量、
**Hook 翻不过①黑名单与②沙箱的四条安全反证**、十二个分发点、缺省零行为）、
子 Agent（角色解析与三层扫描、工具集三层过滤、**权限只能收紧的派生**、任务表的加锁不变量、闸门交付、**Hook 对子 Agent 全量生效**）、工作区隔离（路径遍历构造逐条独立断言、真实临时仓库上的 git 封装、三层过滤与三种清理结局、**路径判定按 root 的反证**、cwd 四个分发点）、行为记录（并发序号无重号无跳号、开关双跑逐字节零回归）、
子 Agent 协作（**20 线程并发认领恰好一个成功**、依赖强制与环检测、
「`Event.set()` 确实在锁外」的四条探针、**`TeamGate.has_awaited` 恒假**的专门用例、
待命唤醒续跑、无人轮三条拒绝文案两两不同、自动唤起的空闲判定与连锁上限）、
端到端驱动设施（完整交互闭环全程不重启、危险命令即使驱动者放行也在第①层被拦）。

各层的详细覆盖清单见 `CLAUDE.md` 的「测试」一节。真实 LLM 下的输出质量与
TUI 视觉效果留作手测，验收记录在 `docs/c11/acceptance/`、`docs/c12/acceptance.md`、
`docs/c13/acceptance/`、`docs/c14/acceptance/`（两份：逻辑层 + 真实模型）
与 `docs/c15/acceptance/live-model.md`。


