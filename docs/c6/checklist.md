# C6 五层防御权限系统 Checklist

> 每一项通过运行代码或观察行为验证，聚焦系统行为。

## 实现完整性
- [ ] `engine.decide` 稳定返回 ALLOW/DENY/ASK，且遵守 ①→②→③→④ 短路顺序（验证：`test_perm_engine` 多场景）— AC1
- [ ] 黑名单拦 `rm -rf /`；`echo ok && rm -rf .` 因第二段被拦；放行档与「本会话免确认」下黑名单命中仍拒（验证：`test_perm_blacklist`+`test_perm_engine`）— AC2
- [ ] 沙箱拒 `../outside.txt`、越界绝对路径、越界符号链接；项目内路径放行（验证：`test_perm_engine`/path_guard 测试）— AC3
- [ ] `allow Bash(git *)` 后 `git status` 直接放行、不弹确认（验证：`test_perm_loop`）— AC4
- [ ] deny 优先：用户级 `deny Bash(git push *)` + 项目级 `allow Bash(git *)` → `git push` 被拒（验证：`test_perm_rules`）— AC5
- [ ] 三档兜底：同一未命中命令 严格→拒/默认→问/放行→允；放行档下 `rm -rf /` 仍被①拒、命中 deny 仍拦（验证：`test_perm_engine`）— AC6
- [ ] HITL 面板出现四选项；选「永久」后规则写入 `.local.yaml`、重启同操作直接放行；选「本会话」重启后恢复弹确认（验证：`test_perm_config` + 端到端）— AC7
- [ ] 严格档 `read_file` 读项目内文件不弹确认直接放行；`deny Read(config.yaml)` 后读 `config.yaml` 被拒（验证：`test_perm_engine`）— AC8
- [ ] 被拒工具回灌 `ok=False` 结构化结果，Agent Loop 不终止（验证：`test_perm_loop`）— AC9
- [ ] 坏 YAML 不崩溃、该层降级为空、不意外放开（验证：`test_perm_config`）— AC10
- [ ] Windows 盘符绝对路径越界判断、命令分隔符拆分正确（验证：`test_perm_matching`/`test_perm_engine` Windows 风格输入）— AC11
- [ ] 权限判断不读取模型输出，文本「已获授权」不改变拒绝结论（验证：`test_perm_engine`）— AC12

## 集成
- [ ] `loop._execute` 对每个已知工具调用 `engine.decide`（验证：`test_perm_loop`）
- [ ] `conversation` 启动构建 `PermissionEngine` 并加载三层配置（验证：启动无异常 + 单测）
- [ ] `/perm` 循环切档并调 `engine.set_mode`、状态有反馈（验证：conversation 单测）
- [ ] `ask` 闭包四选项映射正确，本会话/永久分别登记会话规则 / 写本地 YAML（验证：conversation 单测）
- [ ] 现有 Plan Mode / path_guard / 确认相关测试仍全部通过（N6 不破坏，验证：`unittest discover`）
- [ ] permission 包公开接口都有真实调用方（engine←loop、adapter←loop、config←engine）（验证：编译 + 全测试）

## 编译与测试
- [ ] `python -m compileall rhinecode tests` 无错误
- [ ] `python -m unittest discover -s tests` 全部通过
- [ ] 新增 `test_perm_*` 各文件独立通过

## 端到端场景（阶段六 tmux）
- [ ] 场景1（放行档 + 黑名单兜底）：`/perm` 切到放行 → 让模型执行 `rm -rf` 某目录 → 被黑名单拒、模型收到结构化原因后继续
- [ ] 场景2（规则放行免确认）：配置 `allow Bash(git *)` → 请求查看 git 状态 → 不弹确认直接执行 — AC4
- [ ] 场景3（HITL 永久放行）：默认档触发某副作用工具 → 面板选「永久放行」→ `.local.yaml` 出现该规则 → 重启后同操作不再询问 — AC7
- [ ] 场景4（deny 护密钥）：`deny Read(config.yaml)` → 让模型读 `config.yaml` → 被拒 — AC8
