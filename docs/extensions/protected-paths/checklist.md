# 保护路径层（②″）Checklist

> 每一项通过运行代码或观察行为来验证。括号里是验证方式与对应的 AC。
>
> ⚠ **「机器判到了什么」与「据此做的判断」要分开记**——这是 C11 起沿用的验收
> 记法。一条判据通过不等于那个行为对，只等于「实现与判据一致」。

## 一、判定范围

- [ ] **`.rhinecode/` 下的配置类写入全部命中**（AC4）
      验证：`python -m unittest tests.test_perm_protected.ScopeTest`；
      逐条覆盖 `permissions.yaml`、`permissions.local.yaml`、`hooks.yaml`、
      `mcp.yaml`、`agents/` 下任意文件、`skills/` 下任意文件、`memory/` 下任意文件、
      `worktrees/` 下任意文件、`.git/config`、`.git/hooks/pre-commit`

- [ ] **三个运行期产物目录不命中**（AC4）
      验证：同上；`sessions/`、`context/`、`traces/` 下的写入断言为「不命中」

- [ ] **普通业务路径不命中**（AC1）
      验证：同上；`rhinecode/agent/loop.py`、`README.md` 之类断言为「不命中」

- [ ] **排除优先于保护**（AC4，反证）
      验证：`tests.test_perm_protected.ExcludeBeatsProtectTest`；
      把 `inspect` 里「先查排除」与「先查保护」两步对调后该用例必须变红

- [ ] **三种路径写法与符号链接都命中**（AC5）
      验证：`tests.test_perm_protected.PathFormTest`；
      相对写法 / `./` 前缀 / 绝对路径三条恒跑，
      「工作区内指向 `.rhinecode/hooks.yaml` 的符号链接」一条在建不了软链的平台上 skip

## 二、工作目录基准（坑 1）

- [ ] **同一个绝对目标在两个 cwd 下结论相反**（AC2，**坑 1 的护栏**）
      验证：`tests.test_perm_protected.CwdTest`；
      `<主项目根>/.rhinecode/worktrees/w/a.py` 以主项目根为 cwd 时**命中**、
      以该工作区为 cwd 时**不命中**
      ⚠ 不能写成「同一个**相对**路径 `.rhinecode/hooks.yaml` 在两个 cwd 下结论相反」
      ——那句话是错的（以工作区为 cwd 时它照样命中，F14 的期望行为），
      按它建的护栏在判定基准退回主项目根时照样通过

- [ ] **工作目录缺失/非法时按命中处理且不抛异常**（AC3）
      验证：`tests.test_perm_protected.FailSafeTest`；`cwd=None`、`cwd=""`、
      含 `..` 的路径三种都得到「命中」，且 `engine.decide` 不向上抛任何异常

## 三、升级语义（本扩展的核心）

- [ ] **顺序护栏：宽 allow 盖不过本层**（AC6）
      验证：`tests.test_perm_protected.PipelineOrderGuardTest`；
      规则集含 `allow: Write(.rhinecode/**)` 时写 `hooks.yaml` 仍得 `ASK`
      且 `layer is Layer.PROTECTED`
      ⚠ 这条**必须用宽 allow 构造**——用「③层没命中」的形态在实现写错时照样通过

- [ ] **反证一：③层 deny 不被降级**（AC7）
      验证：`tests.test_perm_protected.NoDowngradeTest`；
      `deny: Write(.rhinecode/hooks.yaml)` 下结论仍是 `DENY`、层仍是 `RULE`

- [ ] **反证二：④层严格档 DENY 不被降级**（AC8）
      验证：同上；严格档 + 无规则命中时结论仍是 `DENY`、层仍是 `MODE`

- [ ] **反证三：②层沙箱 DENY 不被降级**（AC8）
      验证：同上；路径越界时层仍是 `SANDBOX`

- [ ] **放行档下命中即问**（AC9）
      验证：`tests.test_perm_protected.UpgradeTest`；放行档写 `hooks.yaml` 得 `ASK @ PROTECTED`

- [ ] **默认档下「层被换掉」而不是原样返回**（AC9）
      验证：同上；默认档写 `hooks.yaml` 得 `ASK @ PROTECTED`（**不是 `ASK @ MODE`**）
      ⚠ 这条是 plan 阶段那个修正的护栏——原样返回会让面板照样显示「永久放行」

- [ ] **Skill 预授权盖不过本层**（AC9）
      验证：同上；`turn_rules` 塞一条 allow 后仍得 `ASK @ PROTECTED`

- [ ] **其余 kind 逐字不变**（AC1）
      验证：`tests.test_perm_protected.OtherKindsUnchangedTest`；
      命令 / 读取 / glob / URL / 未映射五类各造一个请求，
      断言 `decide` 与 `_decide_core` 的结果**逐字相等**

- [ ] **既有权限测试全绿**（AC1，结构性证据）
      验证：`python -m unittest tests.test_perm_engine tests.test_perm_rules
      tests.test_perm_network_layer tests.test_perm_turn_grant tests.test_perm_matching
      tests.test_perm_system_serial tests.test_perm_deny_stops_retry`

## 四、本会话豁免

- [ ] **豁免后同一路径不再升级，且标记可见**（AC12）
      验证：`tests.test_perm_protected.ExemptTest`；
      结论回落为既有管线结论且 `protected_exempt is True`

- [ ] **豁免不扩散到同目录其它文件**（AC12，反证）
      验证：同上；同目录下另一个文件仍得 `ASK @ PROTECTED`

- [ ] **豁免不解除 deny**（AC13）
      验证：同上；豁免 + `deny` 规则同时成立时结论仍是 `DENY`

- [ ] **对非保护路径登记豁免是空操作**（AC12）
      验证：同上；`grant_protected_exemption` 返回 `False` 且集合不变

- [ ] **豁免不落盘、不产生③层规则**（AC12）
      验证：`tests.test_protected_wiring.AskClosureTest`；
      模拟用户选「本会话放行」后断言豁免集合 +1、`session_rules` 长度不变、
      **`persist_local_rule` 零次调用**（计数，不是看返回值）

- [ ] **派生引擎共享豁免集合**（AC14）
      验证：`tests.test_perm_protected.DeriveTest`；
      主引擎登记后派生实例不再升级，反向也成立（同一个对象）

## 五、界面与可观测

- [ ] **保护路径面板是三项，不含「永久放行」**（AC11）
      验证：`tests.test_protected_wiring.PanelTest`；
      选项 id 集合为 `{yes, yes_session, no}`

- [ ] **非保护路径面板仍是四项**（AC11，反证）
      验证：同上；喂 `layer=MODE` 的决策时仍含 `yes_permanent`

- [ ] **「本会话放行」的说明写明了「不写入配置」**（AC11）
      验证：同上；断言该选项的说明文本里含这层意思
      （用户对这个选项的既有心智是「登记一条会话规则」，这里换了机制）

- [ ] **新层在三份展示表里都有名字**（AC10）
      验证：`python -m unittest tests.test_trace_reader tests.test_web_bootstrap
      tests.test_protected_wiring.LayerTableTest`
      （前两个是既有的遍历 `Layer` 的一致性断言，漏改当场红）

- [ ] **判定原因说明了这个文件为什么特殊**（AC10）
      验证：`python -c "from pathlib import Path; from rhinecode.permission import
      protected as p; print(p.inspect('.rhinecode/hooks.yaml', Path('.').resolve()).reason)"`；
      输出里能读到「Hook 动作会直接执行，不经权限管线」这层意思，
      而不只是「需要确认」

- [ ] **埋点带上豁免标记，摘要行看得见**（AC16）
      验证：`tests.test_protected_wiring.TraceFieldTest`；
      `permission_decision` 负载含 `protected_exempt`；
      阅读器对 `protected_exempt=True` 的记录渲染出「保护路径已豁免」，
      对 `layer=protected` 的记录渲染出「②″保护路径」

- [ ] **符号护栏未被触发**（AC10）
      验证：`python -m unittest tests.test_tui_symbols`
      （`②″` 不在 `_SUSPECT` 扫描区间内，本项应当无需改白名单即通过；
      若变红说明扫描区间被人扩过，要先登记再继续）

## 六、编译与全量测试

- [ ] **编译无错**
      验证：`python -m compileall rhinecode tests`

- [ ] **全量测试通过**
      验证：`python -m unittest discover -s tests`；
      失败 0，skipped 仍是 4（真实模型 e2e + 慢速专项）
      ⚠ 任何既有用例变红都要先解释清楚再改——那是 N4 被破坏的信号

## 七、文档

- [ ] **CLAUDE.md 三处都改了**
      验证：`grep -n "②″" CLAUDE.md`；架构表 Permission 行的 ⚠ 列、
      成对维护点两条、安全边界新增一节各能定位到

- [ ] **`path_guard` 与 `protected.py` 互相指认**
      验证：两份文件里各能搜到指向对方的「刻意不合一」说明

- [ ] **扩展已登记**
      验证：`docs/extensions/README.md` 与 `CLAUDE.md` 顶部「已实现的扩展」各有一条

- [ ] **todo 序号连续、交叉引用无悬空**
      验证：`ls docs/todo/` 序号 1–8 连续；
      `grep -rn "docs/todo/[0-9]-" docs/` 无指向已删/已改名文件的引用

- [ ] **`4-classifier.md` 的管线示意图已修正**
      验证：重排后那份文档里不再把②″画成「②沙箱→②″→②′→③规则」里的一站

## 八、端到端场景（真机，`rhine` 实跑）

> ⚠ 第 2 步与第 3 步是**两个方向的反证**，缺任一条就分不清
> 「本层生效」与「碰巧没人写配置」。四步都要跑。

- [ ] **场景 1：放行档下改配置会被拦住**（AC17）
      操作：`/perm` 切到放行档 → 让模型「往 `.rhinecode/hooks.yaml` 加一条规则」
      预期：**弹出确认面板**（不是直接写入），面板上写明这是保护路径判定、
      且只有三个选项

- [ ] **场景 2：宽 allow 规则也盖不过它**（AC17，顺序反证）
      操作：往 `permissions.yaml` 写一条 `allow: Write(.rhinecode/**)` → 重启 → 重跑场景 1
      预期：**仍然弹面板**
      （这一步是整个顺序论证在真机上的落点；若不弹，说明收紧器被写成了短路站）

- [ ] **场景 3：隔离子 Agent 照常能写**（AC17，误伤反证）
      操作：委派一个 `isolation: worktree` 的子 Agent 让它在工作区里写文件
      预期：**照常写成功**，没有任何面板、没有「路径越界」类失败
      （若失败，说明判定基准退回了主项目根——坑 1 复现）

- [ ] **场景 4：日常改代码不受影响**（AC17）
      操作：让模型正常改一次业务代码（放行档下）
      预期：**一次面板都不多**，与本扩展之前逐字一致

- [ ] **场景 5：本会话放行真的生效且没写盘**（AC12 的真机落点）
      操作：场景 1 的面板上选「本会话放行」→ 让模型再改一次同一个文件
      预期：第二次**不弹面板**；检查 `.rhinecode/permissions.local.yaml`
      **没有新增任何条目**

- [ ] **场景 6：非隔离子 Agent 改配置被自动拒绝**（AC15）
      操作：委派一个非隔离的子 Agent 让它写 `.rhinecode/hooks.yaml`
      预期：该次写入**被拒绝**（子 Agent 非交互、判 ASK 即自动拒绝），
      结论里如实说明没做成
      ⚠ 这是**期望行为不是缺陷**——记录时写清楚，免得后来的人当误伤修掉
