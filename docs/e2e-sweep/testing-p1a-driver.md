# 测试设施：端到端驱动设施（P1a）—— 复测

> 对应 `docs/c11/testing/p1-driver/checklist.md` 第八节场景 1–6（原标注【手测】）。
> **本次复测的全部 100 余条判据都是用这套设施跑出来的**，场景 1/2 因此有极强的实证。

## 总览

| 场景 | 结果 | 说明 |
| --- | --- | --- |
| 1 闭环基本可用 | ✅ | 全程未看终端界面，靠 `status`/`observe` 完成全部判定 |
| 2 真实模型驱动 | ✅ | 本次整场复测；面板决策标为 `driver` |
| 3 改代码 → 重跑的闭环 | ✅ | 指纹重启后变化、不重启不变 |
| 4 宿主异常与多宿主 | ⚠️ **3/4** | ② **强杀期间 `wait` 抛裸异常** —— 见缺陷 D1 |
| 5 面板挂着时退出 | ⚠️ 部分 | 强制结算记录正确；退出耗时 **31 秒**而非「数秒」 |
| 6 对真实使用零影响 | ⊘ 需人眼 | 要在真实终端跑 `rhine` 观察 |

**3 条通过、2 条部分、1 条需人眼。发现设施缺陷 1 条（D1）、判据措辞偏差 1 条。**

---

## 场景 1 / 2：闭环基本可用 + 真实模型驱动

这两条不需要单独构造——**本次复测的全过程就是它们**：

- 十四个章节 / 扩展 / 设施的判据，全部通过 `send` → `wait` → `status` → `answer` →
  `observe` 这条链完成，**一次都没有看终端界面、一次都没有人点面板**；
- `observe` 的输出确实足以看清「模型这轮收到了哪些工具（`api_request.tools`）、
  调了什么（`tool_execute`）、权限走了哪层（`permission_decision` 的层标记）、
  界面显示了什么（`ui_message` / `status_bar`）」——这正是判据的原话；
- 面板决策的来源在记录里标为外部驱动者：

```json
{"type": "interaction", "kind": "confirm", "result": "deny", "source": "driver"}
```

**判断**：两条都通过，且是在**远超原验收规模**的负载下通过的
（本次跑了十余台宿主、数百轮真实模型往返）。

> 沿途也暴露了这套设施在真实使用中的几处摩擦，都记在下面。

## 场景 3：改代码 → 重跑的闭环

用「改 mtime 但不改内容」触发——这正好也验证了 `fingerprint.py` docstring 里
明确承认的那条已知误报（「`pip install -e .`、`git checkout` 都会改 mtime 而内容并未变，
于是会误报『代码变了』」）。

| 步骤 | 指纹 |
| --- | --- |
| 起始 | `6c0b4ffe970f` |
| 改 `rhinecode/trace/models.py` 的 mtime（内容未动）、**不重启** | `6c0b4ffe970f`（不变） |
| 重启宿主 | `5986469fff45`（**已变**） |
| 再改 `rhinecode/trace/reader.py` 的 mtime、**不重启** | `5986469fff45`（不变） |

**判断**：两条都成立——重启后指纹确实不同；**不重启则不变**（启动时算一次并缓存，
这是设计不是缺陷）。通过。

> 顺带：多宿主并存时两台的指纹不同（`5986469fff45` vs `13582619c23a`），
> 一眼就能看出哪台跑的是旧代码。这就是这个字段存在的全部理由。

## 场景 4：宿主异常、陈旧发布信息与多宿主

### ③ 多宿主 —— ✅

```
$ python -m tests.e2e.client status
有 2 个宿主正在运行，请用 --pid 指定其中一个：
  pid=4280  port=64799 mode=live ws=…\rhine_e2e_ws_al2ocgtu
  pid=25732 port=64762 mode=live ws=…\rhine_e2e_ws_rlouttmb

$ python -m tests.e2e.client status --pid 4280      → pid4280 state= idle
$ python -m tests.e2e.client hosts                  → 两者都列出，含端口/模式/指纹/工作区/记录路径
```

**判断**：歧义提示明确且给出了区分所需的全部信息，`--pid` 能分别驱动。通过。

### ① 强杀后再发指令 —— ✅

```
宿主已不在：连不上 127.0.0.1:64799（ConnectionRefusedError: [WinError 10061] …）。
名片文件：…\rhinecode-e2e\host-4280.json（pid=4280）
用 `python -m tests.e2e.client hosts` 查看当前宿主，确认该宿主确已退出后手工删除上面那个名片文件。
```

**判断**：**立即**返回（不是超时等待）、说清了原因、指出了名片路径与清理办法。通过。
按提示删掉名片后能正常起新宿主。

**顺带确认**：强杀确实残留临时工作区（本次累计残留 9 个），需一并手工删——与判据的说明一致。

### ② `wait` 挂起期间强杀 —— ❌ **缺陷 D1** · ✅ **已于 2026-08-05 修复**

> 修法即下方「建议修法」，另把文案收进 `_host_gone` 只写一份（EOF 与 RST 对使用者
> 是同一件事，分开写将来改措辞必然只改到一处）。护栏 `tests/test_e2e_client.py`。
> 以下保留复测当时的原始记录。

**期望**（判据原文）：得到「宿主在处理本指令期间退出了」的**可读提示而非异常堆栈**。

**实际**：

```
Traceback (most recent call last):
  File "…\tests\e2e\client.py", line 227, in <module>
    sys.exit(main())
  File "…\tests\e2e\client.py", line 207, in main
    response = send_command(info, build_request(args), timeout=args.connect_timeout)
  File "…\tests\e2e\client.py", line 81, in send_command
    chunk = sock.recv(65536)
ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。
```

**稳定复现**（跑了两次，形态一致）。

**成因**（`client.py:76-98`）：那个 `while` 循环只处理「`recv` 返回空 bytes（EOF）」的情形——

```python
chunk = sock.recv(65536)
if chunk:
    buffer += chunk
    continue
if not buffer:
    raise RuntimeError("宿主在处理本指令期间退出了（…）")
```

也就是说：

| 宿主怎么没的 | TCP 层 | 客户端表现 |
| --- | --- | --- |
| `quit` / 空闲超时 / 正常崩溃 | FIN → `recv` 返回 `b""` | ✅ 走到那条可读提示 |
| **强杀（`Stop-Process -Force` / `SIGKILL`）** | **RST → `recv` 抛 `ConnectionResetError`** | ❌ 裸 traceback |

模块 docstring 里那张「三种失败必须翻译成人话」的表**只覆盖了 EOF 形态**，
漏了 RST 形态。而 checklist 的场景 4② 恰恰是用强杀构造的——判据点名要的就是这一条。

**严重度：低—中（测试设施的排障体验）。** 不影响产品，但它违背了 `client.py`
自己写在 docstring 里的设计目标（「三种失败必须翻译成人话」），
且在最需要可读信息的时刻（宿主意外死了）给出的是最没有信息量的输出。

**建议修法（一行级，不在本分支修）**：把 `sock.recv` 那句包起来——

```python
try:
    chunk = sock.recv(65536)
except (ConnectionResetError, ConnectionAbortedError) as e:
    raise RuntimeError(
        f"宿主在处理本指令期间退出了（pid={info.pid}，指令 {request.get('cmd')!r}）。\n"
        f"连接被对端强制关闭（{type(e).__name__}），常见于宿主被强杀。\n"
        f"用 `python -m tests.e2e.client hosts` 确认它是否还在，并清理残留的临时工作区。"
    ) from e
```

顺带把 docstring 那张表从三行改成四行。

## 场景 5：面板挂着时退出

驱动到弹出确认面板，**不应答直接 `quit`**。

### 强制结算记录 —— ✅

```json
{"seq": 13, "type": "interaction", "kind": "confirm",
 "display": "run_command {'command': 'dir /b'}｜默认模式：无规则命中，交由用户确认",
 "source": "**driver_forced**", "result": "deny"}
```

**判断**：来源标为 `driver_forced`（区别于正常应答的 `driver`），结果为 `deny`。
正是判据要的那条。通过。

> reader 的时间线摘要里**不显示** `source` 字段（只显示 `confirm → deny · run_command …`），
> 要 `--seq` 展开才看得到。这是摘要的取舍，不是缺陷，但排障时值得知道。

### 退出耗时 —— ⚠️ 判据措辞与实现不符

判据写「宿主在**数秒内**干净退出（不是挂死）」。实测：

```
04:15:56  quit 发出（面板被强制结算）
04:15:57  Agent Loop finished · stop_reason=completed
04:15:59  notes 作用域的笔记请求完成
04:16:27  session_end  quit · 轮次合计 3 · 用时 56.984s
```

**从 `quit` 到进程真正消失约 31 秒。**

**这不是挂死**——它确实退出了、`session_end` 正常、退出码正常、工作区按
`--keep-workspace` 保留。原因是 `control.py:832` 那段刻意的等待：

```python
if thread.name == "rhine-notes":
    thread.join(timeout=30.0)
```

即等笔记线程写完再退，避免笔记文件写坏。**设计是对的**，只是「数秒内」这个措辞
与 30 秒的 join 上限对不上。

**建议**：把判据改成「**不挂死**：`quit` 后进程在 40 秒内退出并写出 `session_end`；
若当轮触发了笔记更新，会等笔记线程最多 30 秒（见 `control.py` 的 `shutdown_on_main`）」。

## 场景 6：对真实使用零影响 —— 需人眼

判据要求不经驱动设施、直接 `rhine` 启动一次，观察四项二元指标与「与改动前肉眼无差别」。
需要真实终端与人眼，本次未跑，记为未覆盖。

**间接证据**：本次全部十余台宿主都走 `bootstrap.build_app`（与真实启动同一条装配路径，
宿主是它的第二个真实调用方），四项指标在驱动侧都观察正常
（流式分块 `text_chunks=118`、工具行状态转换、确认面板四选项、状态栏随轮次刷新）。
但这不能替代「真实终端里肉眼看」。

---

## 顺带记录：本次复测暴露的两处使用摩擦（非缺陷）

### 1. 轮次预算耗尽时的提示很好，但默认值偏小

第一台宿主跑到一半撞上：

```
[turn_budget] 已用 42 轮，触及预算 40。重启宿主或用 --max-turns 调大。
```

提示本身完全够用（说清了现状与两条出路）。但 `DEFAULT_MAX_TURNS = 40` 对
「一台宿主跑完一个章节」这种用法偏小——本次后续宿主一律用 `--max-turns 200`。
**不建议改默认值**（保守默认是对的，它防的是失控的自动化），只是记下来。

### 2. 还原工作区会让 trace 的 `seq` 重叠

本次为了验「重启后 X 仍生效」，写了 `seed_restore` 把上一台宿主的目录树还原进新宿主
（见 [c9.md](c9.md)）。副作用是：**旧的 `host.jsonl` 也被还原了**，新宿主的记录器
从 `seq=1` 重新开始追加，于是同一个文件里出现两段 `seq` 重叠的记录。

我因此误读过一次（把上一批的旧报告当成新结果）。**排查办法是按 `ts` 而不是 `seq` 过滤。**

这不是缺陷（`seed_restore` 是我这次新加的东西，设施本身没这个问题），
但如果 P1b 要把「还原工作区」做成正式能力，**应当在还原时跳过 `.rhinecode/traces/`**。
