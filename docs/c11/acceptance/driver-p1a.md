# TUI 驱动器 P1a 验收报告

> 对照 `docs/c11/testing/p1-driver/checklist.md`。**记录的是实际结果与证据，不是预期结果**。
> 手测项按项目既定规矩交给用户亲自跑，本报告只给分步操作与预期（见末节）。

## 环境前置

| 项 | 实际 |
| --- | --- |
| git | **有**，`git version 2.50.1.windows.1` |
| 真实模式开关 | 跑全量时**未开**（`RHINE_E2E_LIVE` 未设）→ `test_e2e_live` 的 3 条报告为 **skipped**，即 AC41。场景 2 是经用户授权后**单独手工驱动**的（走 `--mode live --config <含有效 key 的配置>`），与全量测试的默认跳过互不影响 |
| P1a 起点提交 SHA | `62e6e19`（「产品侧只动三处」的 diff 基线） |
| 基线测试数 | **714**（`Ran 714 tests ... OK`） |

## 分段自动化

| 段 | 文件 | 条数 | 结果 |
| --- | --- | --- | --- |
| 一 纯逻辑 | `test_e2e_protocol` / `test_e2e_discovery` / `test_e2e_sandbox_seed` | 16 / 11 / 24 | 全绿（沙箱含 6 条 `force_rmtree` 闸门护栏） |
| 二 产品接缝 | 无新增测试文件（+1 条结构护栏进 `test_tui_keybindings`） | — | 全量此刻 **715**（714 + 1 护栏） |
| 三 假模型 | `test_e2e_scripted` | 17 | 全绿 |
| 四 断言层 | `test_e2e_assertions` | 24 | 全绿 |
| 五 驱动内核 | `test_e2e_control` | 20 | 全绿（连跑 3 次稳定；含 2 条 `ui_message` 补齐的回归护栏） |
| 六 宿主与客户端 | `test_e2e_host` | 26（含 1 条慢速专项 skip） | 全绿、零残留（连跑 2 次） |
| 七 收尾 | `test_e2e_live` | 3（**默认全 skip**） | 符合 AC41 |

**新增测试合计 141 条**（16+11+24+17+24+20+26+3 = 141，其中默认跳过 4 条）。

**轮次预算实测**（N5）：`--max-turns 1` 起宿主，一轮对话后
`"turns": 1, "turn_budget": 1`；再次 `send` 得
`[turn_budget] 已用 1 轮，触及预算 1。重启宿主或用 --max-turns 调大。`

## 架构与集成

| 检查 | 命令 | 实际输出 | 判定 |
| --- | --- | --- | --- |
| 客户端不依赖产品包 | AST 扫 `client.py` 的 import | `[]` | ✅ |
| 两个包初始化文件为空 | AST body 长度 | `[0, 0]` | ✅ |
| 驱动设施不随产品分发 | `find_packages(include=['rhinecode*'])` | `False` | ✅ |
| 控制台入口未新增 | `[project.scripts]` | 只有 `rhine` 一项 | ✅ |
| 产品不反向依赖 tests | `grep "import tests" rhinecode/` | 无匹配 | ✅ |
| `tools/__init__.py` 无导入 | AST body | 仅 1 个 docstring 节点、零 import | ✅ |
| **产品侧改动范围** | `git diff --stat 62e6e19 HEAD -- rhinecode/` | 仍是 `bootstrap.py` / `conversation.py` / `tui/app.py` **三个文件**；其中 `tui/app.py` 除三处接缝外另含一处埋点补齐（见下节），故按「改动点」计为四处 | ✅ |

【人读】四条不变量：**在**（`tests/e2e/control.py` 模块 docstring 顶部完整写下四条及各自违反后果，并在
`_lock` 定义处、`snapshot`/`wait`、`send` 四段式、`answer` 复核四处放了呼应注释）。

【人读】关键注释：**6/6 到位**——`run_on_main` 的三条 `call_from_thread` 实测限制、
`send` 的三行不可省、`shutdown_on_main` 的交织循环与 Python 3.11 `shutdown_default_executor`
无超时、`extract_panel` 的 `display` 同名不同义、`settlement_for` 的三套标识、
`bootstrap` 的 `exclude_tools` 窄窗口（与 C11 Skill 校验并列）。

## 工程基线

| 项 | 实际 |
| --- | --- |
| 测试总数 | 714 → **856**（既有 714 条**一条不少**，断言语义未弱化） |
| `compileall rhinecode tests` | **通过**，无错误 |
| 新测试文件数 | **8**（`tests/test_e2e_*.py`） |
| 新增第三方依赖 | **0**（只用标准库 + 项目已有的 `pyyaml`） |
| 残留检查 | 宿主进程 **0** / 临时工作区 **0** / 陈旧发布文件 **0** |

## 开发过程中实测发现并修掉的缺陷

全部属于「不报错但行为不对」——正是本设施存在的理由。

| # | 缺陷 | 后果 | 处置 |
| --- | --- | --- | --- |
| 1 | venv shim 使 `Popen.pid ≠ 宿主 pid` | ① 名片永远匹配不上，每条用例超时；② `proc.kill()` 杀错进程 → **宿主全部泄漏**，二十几个常驻进程把机器拖到全部级联失败，而报错全指向「启动慢」这个假象 | 改按「名片是新出现的」判定 + 杀整棵进程树 |
| 2 | 客户端 `recv` 漏累积 buffer | 读到 EOF 误报「宿主在处理本指令期间退出了」——**而宿主好好地活着** | 补 `buffer += chunk` |
| 3 | 客户端 stdout 是 GBK | 打印含 `⚠` 的面板原文直接崩：**排障工具因打印排障信息而失败** | `force_utf8_output()` |
| 4 | handler 用 `with conn` | 内部异常时连接先关，客户端只能读到 EOF 并报出错误的原因 | 改为「组装好响应再关」+ 异常留痕 |
| 5 | 清理链三处 | ① `tearDown`/`addCleanup` 次序使 stderr 日志删不掉（一轮攒 44 个）；② 两个目录串在同一 `try` 里，工作区失败则用户目录被跳过；③ `rmtree` 撞上 git 的**只读** `.git/objects` 会「删一半」留下空壳且**一个错都不报** | 统一到 `sandbox.force_rmtree`（只读位处理 + 有界重试） |
| 6 | `quit_host` 只等 shim 退出 | 真宿主还在跑自己的清理就被强杀，留下残骸 | 改为等名片消失（那是清理的最后一步） |
| 7 | 客户端只处理 EOF、不处理连接重置 | 宿主被硬杀时 Windows 抛 `ConnectionResetError [WinError 10054]`，那句「宿主在处理本指令期间退出了」**永远不会出现**，用户看到一坨 traceback——**正是它本来要防的东西**（手测场景 4b 抓到） | 两条断开路径共用同一份文案 |
| 8 | `force_rmtree` 没有闸门 | 空路径 → `Path("")` → `"."` → **误删整个代码仓库**（靠远端恢复，零丢失） | 补两道闸门（拒空串/相对路径 + `assert_disposable`），六条护栏钉死 |

## 相对 plan 的偏离（已写进 `spec.md` 末节）

1. **`ConversationManager` 的工厂改延迟绑定**——plan 的写法会让既有
   `test_skill_isolated.py` 的模块属性猴补静默失效，那才是真回归。
2. **AC10「耗时 < 1 秒」按字面执行做不到**——Windows 上 OS 自己就要约 2.03 秒才返回
   `ConnectionRefusedError`（裸 socket 连测三次 2.032 / 2.031 / 2.016 秒，与本设施无关）。
   判据改为「不等满调用方给的超时」。
3. **`test_zzz_no_global_leftovers` 只保证类内次序**——unittest 按字典序排类，
   `CleanupTest` 实际排在最前。真正的兜底仍是每条用例自己的 `addCleanup`。

## 已修：一处 P0 遗留的观测缺口（产品侧因此共动四处）

`ui_message` 的 AI 正文由 `tui/app.py` 的 `reset_text_widgets()` 在「下一轮开始 /
工具开始 / 历史回放」三个时机收尾产出——**总是靠下一个动作给上一段正文收尾**。
于是一轮运行里的**最后**一段正文永远等不到那个动作，一条 `ui_message` 都不产出
（实测：跑完两轮对话，`ui_message` 里只有两条 `user_echo`，两段 AI 正文一条都没有）。

- **为什么值得修**：断言词汇③「界面消息含某文本」在「最后一句话」上完全不可用，
  而**这个缺口在界面上看不出来**——界面显示得好好的，只是没被记下来。
  正是本设施要抓的那类问题。
- **修法**：在 `_do_stream` 的 `finally` 里补一次 `reset_text_widgets()`。
  位置有两条约束：夹在 `bind_scope(SCOPE_MAIN)` **之后**（该段正文呈现在主界面上、
  该记成 `main`，而独立模式子对话结束时线程作用域可能还是 `isolated:<name>`）、
  两个 `call_from_thread` **之前**（它们在退出竞态下会抛 `RuntimeError`，
  放后面等于「出错时不记录」）。
- **验证**：同一场景重跑，两段 AI 正文均产出 `assistant` 的 `ui_message`。
- **护栏**：`tests/test_e2e_control.py::FinalTextRecordedTest` 两条
  （最后一段必须被记录 / 工具执行时不得重复记录），**经反证有效**——
  临时注释掉修复后两条全红（`AssertionError: 1 != 2`）。

> 因此产品侧最终改动为**四处**（`bootstrap.py` / `conversation.py` / `tui/app.py`
> 的三处接缝 + `tui/app.py` 的这处埋点补齐），与 spec 原定的三处有一处偏离，
> 已在 `spec.md` 末节登记。

## P1b 交接

- spec 末节「推后到 P1b 的内容」**完整在册**，且已补写「可直接复用的七个接缝」与
  「P1a 相对 plan 的四处偏离」两小节。
- 应答者接缝**可换实现**（F6）：`Responder` 协议 + `ExternalResponder` 是它的第一个实现，
  P1b 只需加一个 `source = "policy"` 的实现，`DriverCore` 一行不动。

---

## 场景验收（**6/6 全部通过**，均由 Claude 实跑）

> 起初把这 6 条全列为「用户亲自跑」，那是**误用了规矩**——「交互式验证由用户亲自跑」
> 针对的是**必须人眼看渲染**的 TUI 验收，而 P1a 这套设施存在的全部意义恰恰是把
> 这类交互场景变成可无人驱动的。真正需要用户拍板的只有场景 2（花钱、用真实凭据），
> 用户授权后已跑。

### ✅ 场景 1：完整闭环（已跑通）

九步全中：`status`(idle) → `send` → `wait`(pending) → `status` 读到含 `[dim]` 标记的
面板原文与四个选项 → `answer once`(source=driver) → `wait`(idle) →
`observe` 得 `write_file · executed · ok=True` → **再 send 一次**（验「常驻」）→
`quit`，零残留。

### ✅ 场景 2：真实模型（已跑通，用户授权后执行）

`--mode live` 起宿主，向 DeepSeek（`deepseek-v4-flash`）发一条真实请求
「在当前目录新建一个 hello.txt，内容写一行 hello，然后告诉我做完了」，
面板由驱动器决策，**三条判据全中**：

| 判据 | 实际 |
| --- | --- |
| 真实模型请求与响应 | **3 次往返，0 流错误**：turn1 1311ms、turn2 1108ms（首字 750ms · 20 块）、notes 891ms |
| 至少一条工具执行成功 | `write_file · executed · ok=True`；**文件真的建出来了**（`hello.txt` 内容 `'hello'`） |
| 面板决策来源标为 driver | `('confirm', 'allow', 'driver')` |

模型的真实输出：**「已新建 `hello.txt` 并写入内容 `hello`，共 1 行。」**

关键时间线（24 条事件、坏行 0）：

```
 7  api_request          turn 1 · deepseek-v4-flash · 消息 2 条 · 工具 7 个
 9  api_response         turn 1 · 1311ms · 工具调用 1 个
10  permission_decision  write_file → ask（④模式）· 默认模式：无规则命中，交由用户确认
11  interaction          confirm → allow · write_file {'path': 'hello.txt', …}
13  tool_execute         write_file · executed · ok=True · 新建 · 1 行 · 5 B
17  api_request          turn 2 · 消息 4 条
19  api_response         turn 2 · 1108ms · 首字 750ms · 20 块
20  agent_event          finished · stop_reason=completed
22  notes/api_request    turn 1 · 消息 1 条 · 工具 0 个      ← 自动记忆，作用域独立
24  notes/api_response   turn 1 · 891ms
```

三点值得单独记下：

1. **权限管线走的是真路子**——`permission_decision write_file → ask（④模式）`：
   第④层兜底判「问用户」→ 弹面板 → 驱动器答 `once` → 才执行。**没有被绕过**。
   这是「驱动器不扩大权限面」在真实模型下的正面证据。
2. **`notes` 作用域出现了**（seq 22–24）：真实模式**保留自动记忆**，它与主对话
   共用同一个 Provider 但作用域独立标注——正是 P0 当初坚持要区分四种作用域的理由，
   在这里得到实证（若不区分，记忆那两次调用会污染主对话的轮次计数）。
3. **`rhine-notes` 线程已收敛**（AC24 的 live 一半）：退出后该线程不在
   `threading.enumerate()` 里，说明 `shutdown_on_main` 的 join 生效且没有超时。

流式确实是流式：turn2 是 20 个块、首字 750ms。

⚠️ **真实模式产物已按纪律删除**（含真实模型往返与工具输出原文）。

### ✅ 场景 3：代码版本标识（已跑通）

重启前 `b5b82f3ec9d2` → `touch rhinecode/conversation.py` → 重启后 `f4daacec05c8`，
**两者不同**；且仓库 `git status` 全程干净（宿主只动它自己的临时工作区）。

### ✅ 场景 4：三种失败的提示原文（已跑通，**并因此修掉一个真缺陷**）

- **强杀后 `status`**（退出码 1）：
  ```
  宿主已不在：连不上 127.0.0.1:53738（ConnectionRefusedError: [WinError 10061] …）。
  名片文件：…
hinecode-e2e\host-6016.json（pid=6016）
  用 `python -m tests.e2e.client hosts` 查看当前宿主，确认该宿主确已退出后手工删除上面那个名片文件。
  ```
- **`wait` 在途时宿主硬死**（退出码 1）：
  ```
  宿主在处理本指令期间退出了（pid=11768，指令 'wait'）。
  常见原因：空闲超时到了、别的客户端发了 quit、或宿主崩溃。
  用 `python -m tests.e2e.client hosts` 确认它是否还在。
  ```
  ⚠️ **这条一开始是坏的**：Windows 上宿主被硬杀时对端 `recv` 抛
  `ConnectionResetError [WinError 10054]` 而**不是**干净 EOF，而客户端只处理了 EOF，
  于是这句精心写的文案永远不会出现、用户看到一坨 traceback——**正是它本来要防的东西**。
  已修（两条断开路径共用同一份文案）。
- **两个宿主并存时 `status`**（退出码 1）：正确报「有 2 个宿主，请用 --pid 指定」
  并列出各自 pid / port / workspace；`--pid` 指定后恢复正常。

### ✅ 场景 5：挂着面板时退出（已跑通）

`wait` 得 pending → 直接 `quit` → **172 ms** 干净退出（退出码 0）；
记录 21 条、坏行 0、末条 `session_end reason=quit`；
交互事件**恰好一条**：`kind=confirm result=deny source=driver_forced`。
（对照：不做强制结算时实测「永远退不出去，40 秒后被外部杀掉」。）

### ✅ 场景 6：观察面四项（已跑通）

① 时间线可读——20 条事件按时序一行一条，`seq / ts / scope / type / 摘要` 齐备；
② `ui_message` 有富文本 AI 正文——**两段都在**（含 seq=19 的最后一段，
   那正是本轮补掉的埋点缺口，在真实端到端跑里得到印证）；
③ `tool_execute` 有完整参数与输出——`arguments={'path': 'seed.txt'}`、
   `output='文件: seed.txt · 1 行 · 19 B
1│ 一行中文内容'`；
④ 作用域标注正确——全部 `main`。坏行 0，零残留。

---

## ⚠️ 一次真实事故：误删整个仓库（已恢复，零丢失）

跑场景 5 时，从 `status` 的 JSON 里抽 `workspace` 路径的 `sed` 因反斜杠失败、
变量成了空串，传进 `force_rmtree` 后 `Path("")` 解析成 `"."`——**当前工作目录，
即仓库根**，`rmtree` 把整个代码仓库删光。

- **恢复**：全部 6 个 P1a 提交此前已推到远端（`c11-trace` 至 `17e84b7`），
  重新 clone 回原路径，`git fsck` 无异常、240 个文件齐全、全量测试通过。**零丢失。**
- **根因不是「调用方不小心」，而是闸门有旁路**：本模块 docstring 把
  `assert_disposable` 称为「`rmtree` 之前唯一的闸门」，而我为测试兜底清理新写的
  `force_rmtree` 却刻意跳过了它，理由是「路径都来自可信来源」——那个假设被证伪了。
- **已修**：`force_rmtree` 现在两道闸门（拒绝空串与相对路径 + 完整
  `assert_disposable`），护栏 `ForceRmtreeGuardTest` 六条钉死。
- **顺带的教训**：这套设施本身也**必须**遵守它为被测系统立下的规矩。

---
