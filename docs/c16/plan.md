# C16：命令与网络的分类器审查 Plan

> 输入：已批准的 [`spec.md`](spec.md)（32 条 F / 6 条 N / 50 条 AC）
> 语言：Python 3.11+

## 架构概览

一个新的**叶子包** `rhinecode/classifier/`，加上 `agent/loop.py` 决策预扫里的**两个调用点**。

```
                        ┌─────────────────────────────────────┐
                        │  agent/loop.py  决策预扫（既有）       │
   工具调用 ──────────▶ │  to_request → engine.decide         │
                        │        ↓                             │
                        │  结论来自 Layer.MODE 且工具声明了      │
                        │  classifier_scope ？                 │
                        └────────┬────────────────────────────┘
                                 │ 是
                                 ▼
                     ┌───────────────────────────┐
                     │  ReviewSession（每次运行一个）│  ← 缓存、转录来源
                     └────────────┬──────────────┘
                                  ▼
                     ┌───────────────────────────┐
                     │  ClassifierService（会话共享）│  ← provider、配置、熔断
                     │   ① 快速过滤（一个词）        │
                     │   ② 仅被标记时：带理由的判定   │
                     └────────────┬──────────────┘
                                  ▼
                            Verdict（放行/拦截/失败）
                                  │
                        ┌─────────┴──────────┐
                        ▼                    ▼
                 覆写 DecisionResult      NOTICE 事件 + trace
```

**`permission/` 包一个字不改**（spec N2）。分类器不进引擎，因为引擎的既有性质是
「同样的输入必然得到同样的结果，不联网、不看时间、无副作用」，那是它能被彻底测透的原因。

### 为什么调用点在决策预扫而不是别处

预扫是**全系统唯一**一个「已经拿到权限结论、但还没执行工具」的位置。放在这里，
spec F2 那三个触发条件全都能直接读到：结论的层标（`DecisionResult.layer`）、
工具的类别（`Tool.classifier_scope`）、以及分类器自身的状态。

`_execute` 里有**两条**判定分支，两条都要接（成对维护点）：

| 分支 | 覆盖的工具 | 结论变量 |
| --- | --- | --- |
| `if tool.system_serial:` | `send_message`（消息类） | `system_decision` |
| 普通分支 | `run_command`、`web_fetch` | `decision` |

⚠ **消息类必须走第一条分支，且不能为它另开路径。** 发消息的工具对第④层免疫
（判 ASK/DENY 都按放行处理），但它**仍然照常跑完整条管线**、结论的层标就是 `MODE`。
另开一条「消息类专用」的判定路径会绕开 `engine.decide`，那正是
`perm-system-serial-bypass` 修掉的那个缺陷的形态——`deny: send_message` 会再次失效。

## 核心数据结构

### `ReviewAction`（待判动作）

```python
@dataclass(frozen=True)
class ReviewAction:
    scope: str          # "command" / "url" / "message"，取自 Tool.classifier_scope
    tool_name: str      # 真实工具名，仅用于文案
    specifier: str      # 命令类=完整命令串；网络类=完整地址；消息类=完整正文
    recipient: str = "" # 仅消息类：收件人名字
    host: str = ""      # 仅网络类：主机名（已归一化）
    port: int = 0       # 仅网络类：端口（缺省按协议推定，用于缓存键）
    cwd: str = ""       # 本次调用的工作目录
```

`frozen=True` 与 `PermissionRequest` 同理由：它是值对象，构造后只被纯函数读取。

### `Verdict`（判定结果）

```python
class VerdictKind(str, Enum):
    ALLOW = "allow"       # 分类器放行
    BLOCK = "block"       # 分类器拦截
    FAILED = "failed"     # 调用失败 / 超时 / 解析不出来

@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    reason: str            # 给**用户**看的完整理由；FAILED 时是错误描述
    staged: bool = False   # 是否跑到了第二阶段
    cached: bool = False   # 是否命中缓存
    elapsed_ms: int = 0
```

⚠ **`reason` 永远不进模型历史。** 回灌给模型的是 `render.DENIED_BY_CLASSIFIER`
这条固定文案（spec F13）。两者在 `render.py` 里相邻定义并互相指认，
免得后来的人「顺手把理由也带给模型，反正它更有用」。

### `ClassifierConfig`

```python
@dataclass(frozen=True)
class ClassifierConfig:
    enabled: bool = True
    model: str = ""        # 空串 = 跟主对话同一个模型
    timeout: float = 10.0  # 秒
```

### `BreakerState`（熔断的对外快照）

```python
class BreakerReason(str, Enum):
    NONE = "none"
    BLOCKS = "blocks"      # 拦截过多（F15）
    FAILURES = "failures"  # 连续调用失败（F16）

@dataclass(frozen=True)
class BreakerState:
    tripped: bool
    reason: BreakerReason
    detail: str            # 最后一次失败的错误 / 触发时的计数
```

### 协议：`ClassifierProtocol`

```python
@runtime_checkable
class ClassifierProtocol(Protocol):
    def review(self, action: ReviewAction, transcript: Transcript) -> Verdict: ...
    def is_tripped(self) -> bool: ...
    def note_manual_approval(self) -> None: ...   # 用户在面板上批准一次 → 恢复（F15）
```

⚠ **协议放在 `classifier/` 而不是 `agent/`，与 `agent/gate.py` 刻意不同。**
`gate.py` 把协议放在消费方是因为 `subagents` 反过来 import `agent.loop`、硬接会撞循环导入；
本包**不依赖 `agent`**（它只调 provider），因此没有那个约束。
照抄 gate.py 的形状会让下一个人以为这里也有循环导入风险，而那是假的。

### `Transcript`（喂给分类器的转录）

```python
@dataclass(frozen=True)
class Transcript:
    user_messages: tuple[str, ...]        # 全部用户消息，**不截断**（F6）
    tool_calls: tuple[RecordedCall, ...]  # 模型发起过的工具调用，**含参数、不截断**

@dataclass(frozen=True)
class RecordedCall:
    name: str
    arguments: str   # JSON 序列化后的原文
```

## 模块设计

### `classifier/models.py`

**职责：** 上述全部值对象与枚举，零行为逻辑。
**依赖：** 标准库。

### `classifier/prompt.py`

**职责：** 纯函数，把 `Transcript + ReviewAction` 渲染成两个阶段各自的请求。

```python
STAGE1_SYSTEM: str          # 快速过滤的系统提示，要求只输出一个词
STAGE2_SYSTEM: str          # 带理由的系统提示

def build_transcript(
    principal_history: list[Message],   # 取用户消息的来源
    own_history: list[Message],         # 取工具调用的来源
) -> Transcript: ...

def render_stage1(t: Transcript, action: ReviewAction) -> list[Message]: ...
def render_stage2(t: Transcript, action: ReviewAction) -> list[Message]: ...

def neutralize(text: str) -> str:       # 标记块片段无害化（F7）
```

**依赖：** `provider.base.Message`、`models.py`。

三条要点：

- **排布顺序定死**（F6a）：系统提示 → 用户消息 → 历史工具调用 → **待判动作（最后）**。
  待判动作放最后才让前缀逐字稳定、可命中 Provider 的前缀缓存。写反了不报错，
  只是每次判定按满价重算，界面上完全看不出来。
- **`build_transcript` 只读 `role == "user"` 与 `role == "assistant" and tool_calls`**，
  显式跳过 `role == "tool"`（工具输出）与无 `tool_calls` 的 assistant 正文（模型的话）。
  两条排除的理由不同，注释里要分开写（F5）。
- **无害化对三样都生效**：命令串、地址、工具调用参数。

### `classifier/parse.py`

**职责：** 纯函数，把模型输出解析成结论。

```python
def parse_stage1(text: str) -> bool | None:
    """返回 True=可疑、False=放行、None=解析不出来。"""

def parse_stage2(text: str) -> tuple[bool, str] | None:
    """返回 (是否拦截, 理由)；解析不出来返回 None。"""
```

⚠ **第一阶段解析不出来时按「可疑」处理、进第二阶段，而不是直接判失败。**
第二阶段的提示词更明确、允许推理，是一次真正的补救；直接失败等于把一次输出格式
的抖动升级成一次拒绝。**这不是放宽**——放行的路径只有 `parse_stage1` 明确返回
`False` 这一条。第二阶段解析不出来才是失败（F12）。

### `classifier/breaker.py`

**职责：** 熔断状态机。`threading.Lock` 保护。

```python
class CircuitBreaker:
    def record_allow(self) -> None:      # 重置连续拦截计数（F15）
    def record_block(self) -> BreakerState | None:   # 返回非 None 表示本次触发了熔断
    def record_failure(self) -> BreakerState | None:
    def reset(self) -> None:             # 用户面板批准一次（F15）
    def state(self) -> BreakerState:
```

阈值是模块级常量、**不可配置**（spec「不做的事」，对齐官方）：

```python
CONSECUTIVE_BLOCKS = 3
TOTAL_BLOCKS = 20
CONSECUTIVE_FAILURES = 3
```

⚠ **临界区只做纯内存读写**（N6）：`record_*` 返回快照，界面通知与 trace 埋点由调用方
在锁外做。这是本项目第五次面对同一类死锁——`SkillManager` / `TaskManager` /
`HookManager` / `team` 的加锁不变量说的是同一件事。

⚠ **三类动作共用同一个计数器**（F16a）。不按 `scope` 分桶——分桶会让「分类器整体
不可用」被拆成三份、各自不到阈值，于是**永远不熔断**。

### `classifier/cache.py`

**职责：** 网络判定缓存（F18），只服务 `scope == "url"`。

```python
class VerdictCache:
    def get(self, key: str) -> Verdict | None: ...
    def put(self, key: str, verdict: Verdict) -> None: ...
    def begin_iteration(self) -> None:   # 清放行缓存（"有新内容进入对话"）
    # 拒绝缓存随对象生命周期结束而消失（一次运行 = 一个回合）

def cache_key(action: ReviewAction) -> str:   # f"{host}:{port}"
```

⚠ **缓存键是主机+端口，绝不含路径与查询参数。** 含了等于不缓存
（每个地址都不同），且会把令牌存进内存里的键上。
⚠ **命令类与消息类不进这里**（F19），`cache_key` 对它们返回空串、调用方据此跳过。

### `classifier/broad.py`

**职责：** 纯函数，判断一条命令放行规则是否「宽泛」（F20）。

```python
def is_broad_command_allow(tool: str, pattern: str) -> bool: ...
def why_broad(tool: str, pattern: str) -> str:   # 给用户看的原因（F21）

INTERPRETERS: frozenset[str]      # python / node / sh / bash / pwsh / uv / npx / deno …
PACKAGE_RUNNERS: frozenset[str]   # npm run / yarn / pnpm run / make / cargo run …
```

**依赖：标准库。刻意不 import `permission`**——签名收成两个字符串，
调用方（装配层）自己从 `Rule` 上取。这样本包保持只依赖 `provider`，
与 `trace` 叶子包避开 `permission` 是同一个考虑（`import permission.models`
会连带执行 `permission/__init__.py`，把整个包 + `rhinecode.tools` 拉起来）。

三类判据（对齐官方清单，**刻意不自行加料**）：

1. `pattern` 为空串，或只有一个 `*`
2. 首段命令名在 `INTERPRETERS` 内，且 `pattern` 以 `*` 结尾
3. 首两段匹配 `PACKAGE_RUNNERS`，且 `pattern` 以 `*` 结尾

⚠ **`Bash(git *)` 刻意不在清单内**，尽管它确实有洞（`git -c core.pager=<任意命令> log`）。
理由写在常量表上方：一张自己加料的启发式清单会给人虚假的安全感，
而官方那张清单至少有公开的判据。它登记在 spec「已知边界」里。

### `classifier/render.py`

**职责：** 纯函数，全部面向人或面向模型的文案。

```python
DENIED_BY_CLASSIFIER: str            # 回灌给**模型**的固定文案（F13）
def render_denied_notice(action, verdict) -> str      # 给**用户**的系统行（F27）
def render_breaker_notice(state: BreakerState) -> str # 熔断提示（F17）
def render_dropped_rules(dropped: list[tuple[str, str]]) -> str  # 启动告知（F21）
```

⚠ **`DENIED_BY_CLASSIFIER` 与 `render_denied_notice` 必须在同一个文件里相邻定义**，
并互相注释指认。它们是同一次判定的两个出口，一个给模型一个给用户，
而「顺手让模型也看到具体理由」看起来永远像是个改进——直到模型开始照着理由
换域名重试。

### `classifier/service.py`

**职责：** 门面。持有 provider、配置、熔断器；执行两阶段调用。

```python
class ClassifierService:
    def __init__(
        self,
        provider: BaseProvider,
        config: ClassifierConfig,
        emit: Optional[Callable[..., None]] = None,   # trace
    ): ...

    def review(self, action: ReviewAction, transcript: Transcript) -> Verdict: ...
    def is_tripped(self) -> bool: ...
    def note_manual_approval(self) -> None: ...
    def breaker_state(self) -> BreakerState: ...
```

`review` 的流程：

1. 熔断中 → 直接返回 `FAILED`（调用方据此走退路，见下方「熔断后的退路」）
2. 第一阶段：`stream_chat(render_stage1(...), tools=None)`，收全文
3. `parse_stage1` → `False` → `ALLOW`（`record_allow`）
4. 否则第二阶段 → `parse_stage2` → `BLOCK` / `FAILED`
5. 任何异常、超时 → `FAILED`（`record_failure`）
6. 埋 `classifier_verdict` 事件（**锁外**）

⚠ **`tools=None` 强制**（F11），与 c8 摘要、c9 记忆两处先例同口径。

### `classifier/session.py`

**职责：** 每次 `Agent.run` 一个实例，把「转录来源 + 缓存」绑在一起。

```python
class ReviewSession:
    def __init__(
        self,
        service: ClassifierProtocol,
        own_history: list[Message],
        principal_history: Optional[list[Message]] = None,
    ): ...
    def begin_iteration(self) -> None: ...
    def review(self, action: ReviewAction) -> Verdict: ...
```

- `principal_history` 为 None 时取 `own_history`（主对话）；
  子 Agent 由运行器传**主对话历史**（F9）。
- **本类不加锁**：一个 `ReviewSession` 只被一个 `Agent.run` 使用，
  而 `run` 在单线程内跑完。跨线程共享的只有 `ClassifierService`（它自己加锁）。

## 既有模块的改动

| 模块 | 改什么 | 为什么 |
| --- | --- | --- |
| `tools/base.py` | `Tool` 新增 `classifier_scope: str = ""` | 「哪些工具要审」的**唯一**声明处 |
| `tools/run_command.py` | `classifier_scope = "command"` | |
| `tools/web_fetch.py` | `classifier_scope = "url"` | |
| `tools/send_message.py` | `classifier_scope = "message"` | |
| `agent/loop.py` | 两个判定分支各接一处；`RunOptions` 加两个字段；每轮迭代调 `begin_iteration` | 唯一调用点 |
| `agent/events.py` | 无（复用 `NOTICE` + `level`） | 四级分级通道已存在 |
| `config.py` | `Config` 加三个 `classifier_*` 字段 + `request_timeout` + 模板注释 | F25 |
| `provider/deepseek.py` | 构造客户端时按 `request_timeout` 传 `timeout` | 超时必须落到 SDK 层 |
| `permission/engine.py` | **不改** | N2 |
| `bootstrap.py` | 建 `ClassifierService`；启用时过滤宽泛规则并收集告知 | F20/F21 |
| `conversation.py` | 把 service 塞进 `RunOptions`；面板批准时调 `note_manual_approval` | F15 |
| `subagents/runner.py` | 传 `principal_history` = 主对话历史 | F9 |
| `trace/models.py` | 新增 `CLASSIFIER_VERDICT` 与 `SCOPE_CLASSIFIER` | F28 |
| `trace/reader.py` | 新增摘要函数 | 成对维护点 |
| `tui/app.py` | 无（`NOTICE` 已有分级渲染） | |

### 熔断后的退路（F16a）

熔断时 `review` 返回 `FAILED`。`_execute` 按 `scope` 分流：

| scope | 熔断后 |
| --- | --- |
| `command` / `url` | 结论改成 `ASK @ Layer.MODE` → 弹面板 |
| `message` | 结论保持原样（放行）→ 一律投递 |

⚠ 未熔断时的 `FAILED` 一律是**拒绝**（F12）。
「熔断中」与「本次失败」都返回 `FAILED`，靠 `service.is_tripped()` 区分——
判据只有一个地方，不在 `Verdict` 上再加一个布尔（两个来源的真值早晚会不一致）。

### 网络类第④层例外的取代（F3）

`engine._decide_core` 里那段「放行档对 url 降级为 ASK」**保留不动**（N2：引擎不改）。
分类器启用时，`_execute` 拿到的 `ASK @ MODE` 会被分类器的 `ALLOW` 覆写成放行；
关闭时没有覆写，行为逐字回到本章之前。

⚠ **这使 url 类成为唯一一处「分类器可以放宽」的地方**，与命令/消息类相反。
`_execute` 里那段覆写逻辑必须显式注释说明这个不对称，否则下一个人会以为是 bug。

## 模块交互

一次 `run_command` 调用的完整链路：

```
模型产出 tool_call
  │
  ▼ agent/loop.py _execute 决策预扫
to_request → engine.decide → DecisionResult(ALLOW @ MODE)
  │
  ▼ tool.classifier_scope == "command" 且 layer is MODE 且 review 非空
ReviewSession.review(action)
  │  ├─ scope 不是 url → 跳过缓存
  │  ▼
  ClassifierService.review
      ├─ breaker.is_tripped() → FAILED（退路：ASK）
      ├─ prompt.render_stage1 → provider.stream_chat(tools=None)
      ├─ parse_stage1 == False → breaker.record_allow() → ALLOW
      ├─ 否则 render_stage2 → stream_chat → parse_stage2
      │     ├─ 拦 → breaker.record_block() → BLOCK
      │     └─ 解析不出来 → breaker.record_failure() → FAILED
      └─ emit(CLASSIFIER_VERDICT)              ← 锁外
  │
  ▼ 回到 _execute
BLOCK → DecisionResult(DENY @ MODE, render.DENIED_BY_CLASSIFIER)
      + yield NOTICE(render_denied_notice(...), level=warning)
FAILED（未熔断）→ 同上，reason 换成失败文案
ALLOW → 原结论不动
```

## 文件组织

```
rhinecode/classifier/          ← 新增，叶子包（只依赖 provider.base 与标准库）
├── __init__.py                — 导出 ClassifierService / ClassifierProtocol / 值对象
├── models.py                  — ReviewAction / Verdict / Transcript / 配置 / 熔断快照 / 协议
├── prompt.py                  — 两阶段系统提示、转录构造、无害化
├── parse.py                   — 两阶段输出解析
├── breaker.py                 — 熔断状态机（加锁）
├── cache.py                   — 网络判定缓存
├── broad.py                   — 宽泛放行规则的识别
├── render.py                  — 全部文案（给模型的固定文案 + 给用户的理由）
├── service.py                 — 门面，两阶段调用
└── session.py                 — 每次运行一个，绑定转录来源与缓存

tests/
├── test_classifier_prompt.py      — 转录构造、排除项、排布顺序、无害化
├── test_classifier_parse.py       — 两阶段解析与「解析不出来」的三种落点
├── test_classifier_breaker.py     — 两种熔断、重置、共用计数器的反证
├── test_classifier_cache.py       — 两种有效期、缓存键、命令类不缓存
├── test_classifier_broad.py       — 三类判据 + 窄规则反证 + git * 反证
├── test_classifier_service.py     — 两阶段调用次数、tools=None、失败三形态
├── test_classifier_loop.py        — 两个判定分支的接线、零次调用反证
├── test_classifier_message.py     — 消息类：不投递、不弹面板、deny 规则优先
└── test_classifier_config.py      — 三个配置项与缺省值
```

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 分类器放引擎内还是外 | **外**（`agent/loop.py` 调用） | 引擎「纯判定、无副作用」是它能被彻底测透的原因；塞进去要把大量测试改成带假网络的形态 |
| 「哪些工具要审」的声明处 | `Tool.classifier_scope` 属性 | 与 `read_only` / `plan_safe` / `system_serial` 同形；比在分类器里硬编码工具名可 grep、可扩展，且只产生一个成对维护点 |
| 消息类是否另开判定路径 | **否**，走既有 `system_serial` 分支 | 另开就绕过 `engine.decide`，`deny: send_message` 会再次失效——那是刚修过的缺陷 |
| 协议放哪 | `classifier/models.py` | 本包不依赖 `agent`，没有 `gate.py` 那个循环导入约束；照抄会误导 |
| `broad.py` 的入参 | 两个**字符串**而非 `Rule` | 保住「只依赖 provider」的叶子性质；`import permission.models` 会连带拉起整个包 |
| 转录来源怎么传进 `_execute` | `run()` 里建 `ReviewSession`，只往 `_execute` 传**一个**回调 | `_execute` 已有 14 个参数；history 在 `run()` 手上，闭包捕获最直接 |
| 缓存的作用域 | **每次运行一个**（不跨线程共享） | 「有新内容进入对话」是本次运行的概念；共享给并发的子 Agent 等于让 A 批准的主机对 B 生效 |
| 熔断的作用域 | **会话共享**（跨主对话与全部子 Agent） | F15 说的是「本段对话累计」；分开计数会让整体不可用永远不到阈值 |
| 第一阶段解析失败怎么办 | 进第二阶段，**不算失败** | 第二阶段是真正的补救；放行路径仍只有「明确返回不可疑」一条，方向仍是收紧 |
| 超时怎么落地 | `Config.request_timeout` → SDK 客户端的 `timeout` | 只在调用方计时是假超时（第一个数据块永不到达时仍会挂死），与 `run_shell_captured` 那次「shell+捕获下 timeout 是假的」同一个教训 |
| 分类器用哪个 provider | `create_provider(替换了 model 与 timeout 的 Config 副本)` | 复用既有实现；`model` 留空时与主对话同模型 |
| 宽泛规则在哪里丢 | 装配层过滤 `file_ruleset`，**不动会话级与本次执行级规则** | 确认面板生成的规则用的是**完整命令串原文**（`to_allow_rule` 的既有行为），天然是窄的 |
| 丢弃是否要能运行期恢复 | **否**，`enabled` 是启动期配置 | 本章不提供运行期开关，做「恢复」是给一个不存在的场景写代码 |
| 界面提示的通道 | 复用 `NOTICE` 事件 + `level` | 四级分级已存在；被拒与熔断都属「你没主动做什么但情况变了」→ 警告级 |

## spec 覆盖自检

| spec | 落在哪 |
| --- | --- |
| F1 / F2 | `Tool.classifier_scope` + `_execute` 两处触发判断 |
| F3 / F3a / F3b | `_execute` 的 url 覆写分支 / `system_serial` 分支 / 不接面板 |
| F4 / F5 / F6 / F6a | `prompt.build_transcript` + `render_stage*` |
| F7 | `prompt.neutralize` |
| F8 | `STAGE1_SYSTEM` / `STAGE2_SYSTEM` 正文 |
| F9 | `ReviewSession.principal_history` + `subagents/runner.py` |
| F10 | `service.review` 两阶段 |
| F11 | `service.review` 的 `tools=None` |
| F12 | `parse.py` + `service.review` 的异常收敛 |
| F13 | `render.DENIED_BY_CLASSIFIER` |
| F14 | `service.review` 的取消检查 + SDK 超时 |
| F15 / F16 / F16a / F17 | `breaker.py` + `_execute` 的退路分流 + `render_breaker_notice` |
| F18 / F19 | `cache.py` + `cache_key` 对非 url 返回空 |
| F20 / F21 / F22 | `broad.py` + `bootstrap.py` |
| F23 / F24 | `subagents/runner.py` 传 service 与 cwd |
| F25 / F26 | `config.py` |
| F27 / F28 | `render.py` + `trace/models.py` + `trace/reader.py` |
| N1 | `_execute` 的触发条件（`layer is MODE`） |
| N2 | `permission/` 零改动 |
| N3 | `prompt` / `parse` / `breaker` / `cache` / `broad` 全是纯逻辑 |
| N4 | `service.review` 的异常收敛 |
| N5 | 包依赖只有 `provider.base` |
| N6 | `breaker.py` 加锁，埋点在锁外 |
