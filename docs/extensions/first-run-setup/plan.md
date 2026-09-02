# 首次启动配置向导 Plan

> 依据已批准的 `spec.md`。语言：Python 3.11+，界面 Textual 8.x。

## 一、架构概览

四个新增部件，加五处接线：

```
                    ┌───────────────────────────────────────┐
   启动路径          │  rhinecode/setup/  （纯逻辑，零界面）  │
   __main__ ────────▶│  trigger  catalog  probe  writer      │
                    └───────────────┬───────────────────────┘
                                    │ 被两个界面入口共用
                    ┌───────────────┴───────────────────────┐
                    │  rhinecode/tui/setup_screen.py         │
                    │  SetupScreen（ModalScreen，四屏）      │
                    └───────────────┬───────────────────────┘
                        ┌───────────┴────────────┐
                        │                        │
              tui/setup_host.py            tui/app.py
              （启动期最小宿主 App）        （/setup 推同一个 Screen）
```

**分层理由**：`setup/` 是纯逻辑包，「有哪几步 / 算不算填好了 / 怎么写盘 / 怎么验」
全部可以在无终端无网络的进程里测；界面只负责画和收键（spec N2，与
`todo/` / `classifier/` 一贯的分法相同）。

**依赖清单**（按模块级 import 量，口径同 `CLAUDE.md` 架构表）：
`setup/` 只依赖标准库、`rhinecode.config`、`openai` SDK。
**绝不 import `bootstrap` / `conversation` / `tui` / `provider`**——
它跑在装配之前，反向依赖会成环（spec N3）。

## 二、为什么用 ModalScreen（而不是现有的 OverlayPanel）

本项目已有三个交互面板（`ConfirmPanel` / `ClarifyPanel` / `SessionPanel`），
它们都是 `NumberedPanel(OverlayPanel, OptionList)`——**贴在输入框上方的内联
覆盖层**，靠数字键选项工作，自由文本要绕到主输入框去打（`ClarifyPanel` 的
「其它…」就是这么做的）。

本向导**不适合套那个模子**，三条理由：

1. 它有**两个必填文本框**（key 与地址），绕到主输入框去打意味着一次一个、
   来回切换，而这两项在同一屏上要能互相对照。
2. 它有**四个步骤加一个等待态**，内联覆盖层没有「屏」的概念。
3. 启动期那次**根本没有主输入框**——那时 `RhineApp` 还不存在。

`ModalScreen` 是 Textual 为「占据整屏、有自己的按键绑定、结束时回一个值」
提供的原语，两个入口能原样复用同一个类。

⚠ **这是本项目第一个 `ModalScreen`，是刻意的偏离，不是没看见既有模式。**
它不改变既有三个面板的任何行为。

## 三、核心数据结构

### `SetupDraft`（`setup/models.py`）

向导的草稿，四屏共同填写的那份东西。不可变，每屏产出一个新的。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `api_key` | `str` | 空串表示「不改」（`/setup` 重跑时的语义，spec F16） |
| `base_url` | `str` | 预填官方默认 |
| `model` | `str` | 第三屏选定或手输 |
| `context_window` | `int` | 由 `catalog.window_for(model)` 推出 |

### `ModelOption`（`setup/models.py`）

第三屏的一个候选项。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `model_id` | `str` | 模型标识 |
| `blurb` | `str` | 一句话说明（便宜/更能想）；服务端不给，由 `catalog` 提供，认不出就空串 |
| `recommended` | `bool` | 是否标为推荐 |

### `ModelListResult`（`setup/models.py`）

第三屏拉清单的结果。**它必须能表达「这是兜底」**（spec F9）。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `options` | `tuple[ModelOption, ...]` | 候选项 |
| `from_fallback` | `bool` | 真 = 服务端没拉到，用的是内置清单 |
| `error` | `str 或 None` | 拉取失败的可读原因（仅 `from_fallback` 为真时有值） |

### `ProbeResult`（`setup/models.py`）

第四屏终验的结果。**分类必须细**（spec F10：不许一句「请求失败」打发）。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | `bool` | 通过与否 |
| `kind` | `ProbeFailure 或 None` | 失败分类：`AUTH` / `NETWORK` / `MODEL` / `OTHER` |
| `detail` | `str` | 给用户看的可读一句话 |
| `elapsed_ms` | `int` | 耗时，成功时显示 |

### `SetupOutcome`（`setup/models.py`）

向导结束时交给调用方的东西。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `action` | `SetupAction` | `SAVED` / `ABANDONED` |
| `written` | `tuple[Path, ...]` | 实际写了哪些文件（`ABANDONED` 时为空） |

## 四、模块设计

### `setup/trigger.py` —— F1 的判定

**职责**：回答「这次启动该不该弹向导」，纯函数、不做界面决策。

**对外接口**

- `classify(path) -> TriggerReason | None`
  返回 `MISSING` / `PLACEHOLDER` / `INVALID` 之一，或 `None`（配置可用）。
  内部复用 `config.load` 的异常语义，**不重新实现一遍校验**——重新实现意味着
  「向导认为配置可用」与「`load()` 认为配置可用」会分家，而那是静默的。

⚠ **刻意不落任何「已完成首次配置」的标记位**（spec F1）：判据是配置当前
可不可用，不是历史。有标记位的话，用户手工删掉 key 之后就再也引导不出来。

### `setup/catalog.py` —— 唯一允许硬编码模型知识的地方

**职责**：兜底清单、模型 → 上下文窗口、推荐项与一句话说明。

**对外接口**

- `FALLBACK_OPTIONS: tuple[ModelOption, ...]`
- `window_for(model_id) -> int`
- `describe(model_id) -> tuple[str, bool]`（说明文字，是否推荐）

**内容**：`deepseek-v4-flash`（推荐）/ `deepseek-v4-pro`，窗口均 1_000_000。

⚠ **`deepseek-v4-flash-vision-exp` 刻意不进兜底清单**：它是实验性多模态模型，
而本项目只发送文本。它若出现在服务端返回的清单里会照常显示（我们**不过滤**
服务端结果——过滤等于又一次把「我们认为有哪些模型」写死），但不主动推荐。

⚠ **本文件是「会过期」的那一份，必须自带声明**：文件头注明「这是拉不到清单
时的兜底，可能已过期；权威来源是服务端」，并写上核实日期与出处。
背景里那次分家（模板写 `deepseek-chat`、实测用 `deepseek-v4-flash`，一个多月
没人发现）就是这份声明存在的理由。

### `setup/probe.py` —— 两次网络请求

**职责**：拉模型清单、发最小请求终验。

**对外接口**

- `list_models(api_key, base_url, timeout) -> ModelListResult`
- `verify(api_key, base_url, model, timeout) -> ProbeResult`

**实现要点**

- 两个函数各自构造一个 `openai.OpenAI` 客户端（DeepSeek 走 OpenAI 兼容协议）。
- `list_models` 调 `client.models.list()`；失败**不抛异常**，返回
  `from_fallback=True` 的结果——拉不到清单是给用户看的一种流程分支，
  不是内部错误。
- `verify` 调 `client.chat.completions.create(..., max_tokens=1, stream=False)`，
  内容是一句最短的问候。**非流式**：这里要的是「通不通」，不是逐块渲染。
- **失败分类**按 SDK 异常类型 + HTTP 状态码归到四类，四类各有一套措辞。
- 两者都**必须传显式 timeout**（spec N4）。

⚠ **这是一处成对维护点**：本模块构造客户端的方式与
`provider/deepseek.py` 的构造方式**刻意不同**（那边流式、长空闲超时；这边
非流式、短超时），但 `base_url` 的语义与鉴权头的形式两边必须一致。
**理由是绕不开的**：`create_provider` 需要一个已经构造好的 `Config`，
而向导跑的时候那份配置恰恰还不存在——先有鸡还是先有蛋。

### `setup/writer.py` —— 定点替换写盘

**职责**：把草稿写进 YAML，**保住注释**（spec F11），并读回当前值供预填。

**对外接口**

- `read_current(path) -> SetupDraft | None`（`/setup` 预填用；读不出来返回 `None`）
- `apply(path, draft) -> tuple[Path, ...]`（写盘，返回实际写了哪些文件）
- `set_scalar(text, key, value) -> str`（纯字符串函数，可单独测）

**`set_scalar` 的三种情形**（这是本模块的全部难点）

1. 文本里有该 key 的**生效行**（顶层、未注释）→ 替换它的值，**行尾注释保留**。
2. 没有生效行，但有**被注释掉的模板行**（如 `# context_window: 65536`）→
   在其后插入生效行，**注释行原样留着**（它是说明文字，不是废行）。
3. 两者都没有 → 追加到文件末尾，前面带一行来源说明。

⚠ **`/setup` 重跑时的基底是「文件现有内容」，不是模板**——否则用户手写的
其它段落（`worktree` / `classifier` / `search` 三段）会被整段抹掉。
只有文件不存在时才以 `_CONFIG_TEMPLATE` 起手。

⚠ **`api_key` 为空串时整个跳过该键**（spec F16 的「留空表示不改」）。
这条要有单独护栏：写错了会**静默清空用户的密钥**，而要到下次启动才发现。

### `tui/setup_screen.py` —— 四屏界面

**职责**：画四屏、收键、调 `setup/` 的纯逻辑、结束时 `dismiss(SetupOutcome)`。

**对外接口**

- `SetupScreen(ModalScreen[SetupOutcome])`
  - `__init__(config_path, prefill: SetupDraft | None, mode: SetupMode)`
  - `mode`：`FIRST_RUN` / `RERUN`，只影响文案与预填，**不影响流程**

**四屏与状态迁移**

```
  ┌────────┐  开始   ┌────────┐ 下一步 ┌────────┐ 下一步 ┌────────┐
  │ 1 说明 │───────▶│ 2 凭据 │──────▶│ 3 模型 │──────▶│ 4 终验 │
  └────┬───┘         └───┬────┘        └───┬────┘        └───┬────┘
       │ 我自己改        │ Esc            │ Esc            │ 重填 ─┐
       ▼                 ▼                ▼                │       │
   ABANDONED ◀───────────┴────────────────┘                │◀──────┘
                                                            │ 成功 / 仍然保存
                                                            ▼
                                                          SAVED
```

- 第 3 屏进入时**异步**调 `probe.list_models`，期间显示等待态；
  `from_fallback` 为真时在清单上方挂一条明确的兜底提示 +「手动输入模型名」入口。
- 第 4 屏进入时**异步**调 `probe.verify`，成功即调 `writer.apply` 并展示文件清单。
- 两次等待期间 `Esc` 都要能取消（spec N4）。

⚠ **所有从外部来的文本（服务端返回的模型名、SDK 的错误消息、Windows 路径）
渲染前必须过 `tui/widgets.py` 的 `escape`**，绝不用 rich 那版。落单的 `[`
会在布局阶段主线程抛 `MarkupError`，**没有任何 try/except 兜得住，整个 app 退出**
（`CLAUDE.md` 架构表 TUI 层第一条致命不变量）。错误消息里带方括号是常见形态，
Windows 路径更是天天见。

⚠ **新增组件的字段名先在 `Static` 实例上 `hasattr` 查一遍**——撞上 Textual
`MessagePump` 的内部字段（`_render` / `_closed` / `_running` …）一律不报错，
只表现为「界面上东西凭空少了」（同表第三条）。

### `tui/setup_host.py` —— 启动期的最小宿主

**职责**：启动期没有 `RhineApp`，需要一个只做一件事的 App 把 `SetupScreen`
推上去、拿到结果、退出。

**对外接口**

- `run_setup(config_path, prefill, mode) -> SetupOutcome`
  同步函数，内部 `App.run()`，返回向导结果。

⚠ **同进程内会连续跑两个 Textual App**（先宿主、后 `RhineApp`）。
Textual 支持这个用法（每次 `run()` 起自己的事件循环），但**这是本项目
从未做过的事**，因此 task 的第一个任务就是把它验通——别等四屏都画完了
才发现两个 App 跑不到一块。

### 密钥不外泄的三个落点（spec F14）

它不是某一个模块的职责，而是三处各管一段，**缺一处就漏**：

1. **`probe.py` 的错误措辞里不得回显 key。** SDK 抛出的异常有时会把请求头
   或 URL 带进 `str(e)`；四类失败的 `detail` 一律**自己组织措辞**，
   不直接把 `str(e)` 交出去。
2. **`writer.py` 之外没有第二处写 key 的地方。** 向导不打印、不记日志。
3. **行为记录天然够不着启动期那一次**——`recorder` 在 `build_app` 之前尚未
   创建。但 `/setup` 那次跑在记录器已存在之后，因此 **`SetupScreen` 一个
   埋点都不加**：这里没有任何值得观测的东西，而它经手的恰恰是最敏感的字段。

## 五、模块交互

### 启动路径

```
__main__.main()
  ├─ parse_args / logsetup                       （不变）
  ├─ explicit = args.config is not None
  ├─ if not explicit:
  │    scaffold 四份模板                          （不变）
  │    reason = trigger.classify(config_path)
  │    if reason and sys.stdin.isatty():
  │        outcome = setup_host.run_setup(...)   ← 新增
  │        if outcome.action is ABANDONED:
  │            打印模板路径 → sys.exit(0)        （= 今天的行为）
  │        # SAVED → 落盘已完成，继续往下
  │    elif reason:
  │        走今天的打印 + 退出分支                （逐字不变，spec F2）
  ├─ cfg = load(config_path)
  ├─ 占位符拦截                                   （保留：非 TTY 路径仍需要）
  └─ build_app(...) → app.run()
```

⚠ **向导写完之后仍然照常走 `load()`**——由它把我们写下去的东西再校验一遍。
向导自己不做「我写的一定对」的假设。

### `/setup` 路径

```
InputBar → dispatcher → CommandSpec("/setup", CommandType.UI)
   → controller.open_setup()
   → RhineApp.open_setup():
        draft = writer.read_current(path)
        push_screen(SetupScreen(..., mode=RERUN), callback)
   → callback(outcome):
        SAVED     → show_event("已保存到 <path>，新配置下次启动生效")
        ABANDONED → 什么都不做（spec F18）
```

`/setup` 在 Agent 正在跑的时候**也允许打开**：它不碰任何运行期状态
（F17 已明确不做热切换），后台 Worker 照常跑，面板关掉就看得到结果。

## 六、文件组织

```
rhinecode/
├── setup/                      ← 新建包
│   ├── __init__.py             门面：导出 models 的类型与四个入口函数
│   ├── models.py               SetupDraft / ModelOption / ModelListResult
│   │                           / ProbeResult / SetupOutcome 与三个枚举
│   ├── trigger.py              classify —— F1 判定
│   ├── catalog.py              兜底清单 / window_for / describe（唯一硬编码处）
│   ├── probe.py                list_models / verify —— 两次网络请求
│   └── writer.py               read_current / apply / set_scalar
├── tui/
│   ├── setup_screen.py         ← 新建：SetupScreen（ModalScreen）
│   └── setup_host.py           ← 新建：run_setup（启动期最小宿主）
├── __main__.py                 ← 改：接入触发与向导
├── config.py                   ← 改：F19 模板模型名 / F20 缺省窗口
├── commands/
│   ├── models.py               ← 改：CommandController 加 open_setup
│   └── builtins.py             ← 改：注册 /setup
└── tui/app.py                  ← 改：实现 open_setup

config.example.yaml             ← 改：F19 / F20
docs/internals/config.md        ← 改：F21
README.md                       ← 改：F21（「30 秒跑起来」那段整体重写）
CLAUDE.md                       ← 改：扩展清单加一行
docs/extensions/README.md       ← 改：索引加一行

tests/
├── test_setup_trigger.py       F1 三种触发 + 一种不触发
├── test_setup_writer.py        set_scalar 三情形 / 保注释 / 空 key 不清空
├── test_setup_probe.py         四类失败分类（假客户端，不联网）
├── test_setup_catalog.py       兜底清单形状 / window_for
├── test_setup_screen.py        四屏流转 / Esc / 兜底提示（run_test）
├── test_setup_entry.py         __main__ 的分支：TTY / 非 TTY / --config
└── test_setup_command.py       /setup 注册与控制器接线
```

## 七、技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 向导界面用什么 | `ModalScreen` | 有两个文本框、四个步骤、启动期没有主输入框；现有内联覆盖层三条都不满足 |
| 启动期怎么跑 | 独立最小宿主 App，跑完再 `build_app` | 装配顺序「一处不动」是硬不变量；让 `build_app` 容忍空 key 要动 Provider 初始化 |
| 模型清单来源 | 服务端优先，内置兜底 | 硬编码已经错过一次，且一个多月没人发现（见 spec 背景） |
| 兜底清单放哪 | 单独一个 `catalog.py`，文件头写明「会过期」与核实日期 | 把「会过期的知识」收在一处，比散在模板、文档、代码里三份好 |
| 终验请求形态 | 非流式、`max_tokens=1` | 要的是「通不通」，不是渲染；非流式的失败分类更干净 |
| `probe` 是否复用 Provider | 不复用，自己造客户端 | `create_provider` 需要已构造的 `Config`，而向导跑时它还不存在（登记为成对维护点） |
| 写盘方式 | 在**现有文件文本**上定点替换；文件不存在才以模板起手 | 保住注释（F11），也保住用户手写的其它段落 |
| 空 `api_key` 的语义 | 跳过该键，不写 | `/setup` 的「留空表示不改」（F16）；写错会静默清空密钥 |
| `/setup` 是否热切换 | 不切，提示重启 | 对齐 `/hooks` `/agents` 既有约定；热切换要在 Agent 循环运行期换共享 Provider |
| `/setup` 忙时能否开 | 能 | 它不碰运行期状态 |
| 非交互判据 | `sys.stdin.isatty()` | 一条覆盖 CI、管道、重定向三种；显式 `--config` 另判 |

## 八、本次新增/涉及的成对维护点

四条，实现时一并登记进 `paired-maintenance` Skill：

1. **`setup/probe.py` 的客户端构造 ↔ `provider/deepseek.py` 的客户端构造**——
   两边刻意不同（流式/非流式、长/短超时），但 `base_url` 与鉴权语义必须一致。
2. **`/setup` 的三处接线**——`commands/builtins.py` 注册表 ↔
   `commands/models.py` 的 `CommandController` 协议 ↔ `tui/app.py` 的实现。
   漏一处的表现是「命令能补全但按了没反应」，不报错。
3. **`config.py` 字段 ↔ `_CONFIG_TEMPLATE` ↔ `docs/internals/config.md` ↔
   `setup/writer.py` 写入的键集合**——已有的三处（护栏
   `tests/test_config_timeout.py::TemplateAndDocsTest`）本次变成**四处**。
4. **`setup/catalog.py` 的兜底清单 ↔ `config.py` 模板里的默认模型名**——
   两处都在声明「我们认为当前该用哪个模型」，这两处已经分家过一次了。
