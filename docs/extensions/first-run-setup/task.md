# 首次启动配置向导 Tasks

> 依据已批准的 `spec.md` + `plan.md`。分支 `first-run-setup`。
> **每完成一个（或一组逻辑相关的）任务就提交一次**。

## 文件清单

| 操作 | 文件 | 职责 |
| --- | --- | --- |
| 新建 | `rhinecode/setup/__init__.py` | 门面：导出类型与入口函数 |
| 新建 | `rhinecode/setup/models.py` | 五个数据类 + 三个枚举 |
| 新建 | `rhinecode/setup/catalog.py` | 兜底清单 / `window_for` / `describe` |
| 新建 | `rhinecode/setup/trigger.py` | `classify` —— F1 判定 |
| 新建 | `rhinecode/setup/writer.py` | `set_scalar` / `read_current` / `apply` |
| 新建 | `rhinecode/setup/probe.py` | `list_models` / `verify` |
| 新建 | `rhinecode/tui/setup_screen.py` | `SetupScreen`（ModalScreen，四屏） |
| 新建 | `rhinecode/tui/setup_host.py` | `run_setup`（启动期最小宿主） |
| 修改 | `rhinecode/config.py` | F19 模板模型名 / F20 缺省窗口 |
| 修改 | `config.example.yaml` | F19 / F20 |
| 修改 | `rhinecode/__main__.py` | 接入触发与向导 |
| 修改 | `rhinecode/commands/models.py` | `CommandController` 加 `open_setup` |
| 修改 | `rhinecode/commands/builtins.py` | 注册 `/setup` |
| 修改 | `rhinecode/tui/app.py` | 实现 `open_setup` |
| 修改 | `docs/internals/config.md` | F21 |
| 修改 | `README.md` | F21（「30 秒跑起来」整段重写） |
| 修改 | `CLAUDE.md` | 扩展清单加一行 |
| 修改 | `docs/extensions/README.md` | 索引加一行 |
| 修改 | `.claude/skills/paired-maintenance/SKILL.md` | 登记 4 条成对维护点 |
| 新建 | `tests/test_setup_host.py` | 连跑两个 App / `run_setup` 返回值 |
| 新建 | `tests/test_setup_catalog.py` | 兜底清单形状 / `window_for` |
| 新建 | `tests/test_setup_trigger.py` | 三种触发 + 一种不触发 |
| 新建 | `tests/test_setup_writer.py` | `set_scalar` 三情形 / 保注释 / 空 key |
| 新建 | `tests/test_setup_probe.py` | 四类失败分类（假客户端，不联网） |
| 新建 | `tests/test_setup_screen.py` | 四屏流转 / Esc / 兜底提示 |
| 新建 | `tests/test_setup_entry.py` | `__main__` 三分支：TTY / 非 TTY / `--config` |
| 新建 | `tests/test_setup_command.py` | `/setup` 三处接线 |

---

## 阶段 A：先验最大的未知

### T1: 验通「同进程连跑两个 Textual App」

**文件：** `rhinecode/tui/setup_host.py`（最小版）、`tests/test_setup_host.py`
**依赖：** 无
**为什么排第一：** 这是 plan 里唯一「本项目从没做过」的事。跑不通的话整个
方案要换（退回到让 `build_app` 容忍空 key），越早知道越好。

**步骤：**
1. 写一个最小 `ModalScreen` 占位（`_ProbeScreen`），`on_mount` 里立刻
   `self.dismiss("ok")`。
2. 写 `run_setup_probe()`：起一个最小 `App`，`on_mount` 里
   `push_screen(_ProbeScreen(), callback)`，回调里 `self.exit(result)`；
   返回 `App.run()` 的返回值。
3. 写 `tests/test_setup_host.py::TwoAppsInOneProcessTest`：**同一个测试进程里
   连续调两次**，断言两次都返回 `"ok"`。
4. 再加一条：调一次 `run_setup_probe()`，然后用 `run_test()` 跑一个普通
   `App`，断言两者都正常——形态更贴近真实（宿主 + `RhineApp`）。

**验证：** `python -m unittest tests.test_setup_host` 全绿。
⚠ 若这里跑不通，**停下来报告**，不要绕过——方案要改。

---

## 阶段 B：纯逻辑（不依赖界面、不联网）

### T2: 数据结构

**文件：** `rhinecode/setup/models.py`、`rhinecode/setup/__init__.py`（空壳）
**依赖：** 无

**步骤：**
1. 建包目录 `rhinecode/setup/`，`__init__.py` 先留空（门面在 T8 补）。
2. 按 plan 三节写五个 `@dataclass(frozen=True)`：`SetupDraft` / `ModelOption` /
   `ModelListResult` / `ProbeResult` / `SetupOutcome`。
3. 写三个枚举：`SetupAction`（`SAVED` / `ABANDONED`）、
   `ProbeFailure`（`AUTH` / `NETWORK` / `MODEL` / `OTHER`）、
   `TriggerReason`（`MISSING` / `PLACEHOLDER` / `INVALID`）、
   `SetupMode`（`FIRST_RUN` / `RERUN`）。
4. 每个类写完整中文 docstring，说明字段含义与「空串表示不改」这类特殊语义。

**验证：** `python -c "from rhinecode.setup import models; print(models.SetupAction.SAVED)"` 打印出枚举值。

### T3: 兜底模型清单

**文件：** `rhinecode/setup/catalog.py`、`tests/test_setup_catalog.py`
**依赖：** T2

**步骤：**
1. 文件头写**过期声明**：这是拉不到清单时的兜底、可能已过期、权威来源是
   服务端、核实日期 2026-09-02、出处是 DeepSeek 官方文档。
2. `FALLBACK_OPTIONS`：`deepseek-v4-flash`（推荐，「日常写代码，快、便宜」）、
   `deepseek-v4-pro`（「更能想，慢一些」）。
   ⚠ **`deepseek-v4-flash-vision-exp` 不进这份清单**，注释写明理由
   （实验性多模态，本项目只发文本）。
3. `_WINDOWS` 映射表：三个 V4 模型均 `1_000_000`。
4. `window_for(model_id) -> int`：命中返回表里的值，认不出返回
   `config.Config.context_window` 的缺省值（**不要再写一个字面量**）。
5. `describe(model_id) -> tuple[str, bool]`：认不出返回 `("", False)`。
6. 测试：兜底清单非空且含推荐项恰好一个；`window_for` 命中与不命中两条；
   `describe` 认不出时不抛。
   ⚠ 再加一条护栏：**断言兜底清单里的模型名与 `config._CONFIG_TEMPLATE` 里
   写的默认模型一致**（成对维护点 4）。

**验证：** `python -m unittest tests.test_setup_catalog` 全绿。

### T4: 触发判定

**文件：** `rhinecode/setup/trigger.py`、`tests/test_setup_trigger.py`
**依赖：** T2

**步骤：**
1. `classify(path) -> TriggerReason | None`：
   - 文件不存在 → `MISSING`
   - 能加载但 `api_key == PLACEHOLDER_API_KEY` → `PLACEHOLDER`
   - `config.load` 抛 `FileNotFoundError` / `ValueError` → `INVALID`
   - 其余 → `None`
2. ⚠ **复用 `config.load` 的异常语义，不重新实现校验**——注释写明理由
   （重新实现会让「向导认为可用」与「`load()` 认为可用」静默分家）。
3. ⚠ 注释写明**刻意不落任何「已完成首次配置」标记位**及其理由。
4. 测试四条：缺文件 / 占位符 / 坏 YAML / 完好配置，各断言返回值。
   用 `tempfile.TemporaryDirectory()` 造临时配置。

**验证：** `python -m unittest tests.test_setup_trigger` 全绿。

### T5: `set_scalar` —— 定点替换的纯字符串函数

**文件：** `rhinecode/setup/writer.py`、`tests/test_setup_writer.py`
**依赖：** T2

**步骤：**
1. `set_scalar(text, key, value) -> str`，三种情形按 plan：
   - 有顶层生效行 → 替换值，**保留行尾注释**
   - 只有被注释掉的模板行 → 在其后插入生效行，**注释行原样留着**
   - 都没有 → 追加到末尾，前面带一行来源说明
2. 值的序列化：字符串加不加引号按 YAML 安全性决定（含 `#`、`:`、前后空格
   时必须加引号）；整数直接写。
3. 测试至少七条：
   - 三种情形各一条
   - **保注释**：拿真实的 `_CONFIG_TEMPLATE` 跑一遍，断言注释行数不减
   - 行尾注释保留
   - 值里含 `#` 时被正确引用
   - **同名前缀不误伤**：文件里同时有 `model:` 与 `model_alias:` 时只改前者

**验证：** `python -m unittest tests.test_setup_writer` 全绿。

### T6: `read_current` 与 `apply`

**文件：** `rhinecode/setup/writer.py`（续）、`tests/test_setup_writer.py`（续）
**依赖：** T5, T3

**步骤：**
1. `read_current(path) -> SetupDraft | None`：读现有 YAML 取
   `base_url` / `model` / `context_window`；**`api_key` 一律回空串**
   （F16「留空表示不改」，也避免把密钥读进界面）。文件不存在或解析失败返回 `None`。
2. `apply(path, draft) -> tuple[Path, ...]`：
   - 基底：文件存在读其内容，不存在用 `config._CONFIG_TEMPLATE`
   - 依次 `set_scalar` 写入 `protocol` / `model` / `base_url` /
     `context_window`；**`draft.api_key` 为空串时跳过 `api_key` 这一键**
   - 写盘，返回写了哪些文件
3. 测试至少四条：
   - 文件不存在时以模板起手，写完能被 `config.load` 正常加载
   - 文件存在时**用户手写的 `worktree:` 段原样还在**
   - **空 `api_key` 不清空原值**（这条单独命名，写错会静默清空密钥）
   - `read_current` 返回的 `api_key` 恒为空串

**验证：** `python -m unittest tests.test_setup_writer` 全绿。

### T7: 两次网络请求

**文件：** `rhinecode/setup/probe.py`、`tests/test_setup_probe.py`
**依赖：** T2, T3

**步骤：**
1. `_build_client(api_key, base_url, timeout)`：构造 `openai.OpenAI`。
   ⚠ 注释写明这是**成对维护点 1**，与 `provider/deepseek.py` 的构造刻意不同
   （非流式、短超时）但 `base_url` 语义必须一致，并写明为什么不能复用
   `create_provider`（它需要已构造的 `Config`，而向导跑时它还不存在）。
2. `list_models(api_key, base_url, timeout, *, _client_factory=None)`：
   调 `client.models.list()`，把每个 `id` 经 `catalog.describe` 补说明；
   **任何异常都不外抛**，返回 `from_fallback=True` + `error=<可读原因>` 的结果。
   `_client_factory` 仅供测试注入。
3. `verify(api_key, base_url, model, timeout, *, _client_factory=None)`：
   调 `chat.completions.create(model=..., messages=[一句最短问候],
   max_tokens=1, stream=False)`，计时，返回 `ProbeResult`。
4. `_classify(exc) -> tuple[ProbeFailure, str]`：按 SDK 异常类型与状态码归四类，
   **四类各自组织措辞**。
   ⚠ **绝不把 `str(exc)` 原样交出去**（成对维护点之外的 F14 落点：SDK 异常
   有时会把请求头或 URL 带进消息）。
5. 测试至少六条（全部用 `_client_factory` 注入假客户端，**不联网**）：
   - `list_models` 正常返回，选项含服务端给的 id
   - `list_models` 抛异常 → `from_fallback=True` 且 `error` 非空
   - `verify` 成功 → `ok=True` 且 `elapsed_ms >= 0`
   - 401 → `AUTH`；连接错误 → `NETWORK`；模型不存在 → `MODEL`
   - **反证**：构造一个消息里含 `sk-secret-key` 的异常，断言返回的
     `detail` **不含**该串（F14）

**验证：** `python -m unittest tests.test_setup_probe` 全绿。

### T8: 门面

**文件：** `rhinecode/setup/__init__.py`
**依赖：** T2–T7

**步骤：**
1. 从各模块 re-export：五个数据类、四个枚举、
   `classify` / `list_models` / `verify` / `read_current` / `apply`。
2. 模块 docstring 说明本包的依赖清单与「绝不 import bootstrap/tui/provider」。

**验证：** `python -c "import rhinecode.setup as s; print(s.classify, s.apply)"` 不报错。

---

## 阶段 C：配置默认值修正（与阶段 B 无依赖，可并行）

### T9: 改模板与示例配置

**文件：** `rhinecode/config.py`、`config.example.yaml`
**依赖：** 无

**步骤：**
1. `_CONFIG_TEMPLATE` 里 `model: deepseek-chat` → `model: deepseek-v4-flash`。
2. 在该行上方补注释：当前官方在售模型有哪两个、各自定位、
   **老别名 `deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 停用**。
3. `Config.context_window` 缺省 `65536` → `1_000_000`；`load()` 里
   `data.get("context_window", 65536)` 的两处字面量一并改（**搜一遍，别漏**）。
4. 模板里 `# context_window: 65536` 那段注释改写：说明依据（V4 全系 1M）、
   **换模型时要跟着改**、调小的后果（压缩过早触发）。
5. `config.example.yaml` 同步以上两处。

**验证：**
```
python -c "from rhinecode.config import Config; print(Config.context_window)"   # 1000000
grep -rn "deepseek-chat" rhinecode/ config.example.yaml                          # 只剩「已停用」说明文字
python -m unittest tests.test_config_timeout                                      # 既有护栏仍绿
```

### T10: 同步配置文档

**文件：** `docs/internals/config.md`、`README.md`
**依赖：** T9

**步骤：**
1. `docs/internals/config.md` 里 `context_window` 那段的「缺省 65536」改掉，
   补一句依据。
2. `README.md`「30 秒跑起来」整段重写成向导版（先按最终形态写，
   阶段 D 完成后再核对一遍措辞）。
3. 其中的 YAML 示例块 `model:` 一并改。

**验证：** `grep -rn "65536" docs/ README.md` 只剩说明历史的地方；
`python -m unittest tests.test_docs_facts tests.test_docs_links` 全绿。

---

## 阶段 D：界面

### T11: `SetupScreen` 骨架 + 第 1、2 屏

**文件：** `rhinecode/tui/setup_screen.py`
**依赖：** T1, T2

**步骤：**
1. `class SetupScreen(ModalScreen[SetupOutcome])`，
   `__init__(config_path, prefill, mode)`。
2. ⚠ **动手前先在 `Static` 实例上 `hasattr` 查一遍**打算用的字段名
   （`_step` / `_draft` / `_result` …），撞上 Textual `MessagePump` 内部字段
   一律不报错、只表现为「界面上东西凭空少了」。把查过的结论写进注释。
3. 一个 `_step` 状态 + `_render_step()` 分发；`BINDINGS` 里 `escape` → 放弃。
4. 第 1 屏：说明 + 完整配置文件路径（**过 `escape`**）+ 两个出口。
5. 第 2 屏：两个 `Input`——key（明文、`placeholder` 提示可粘贴）与 base_url
   （预填 `prefill.base_url` 或官方默认）。key 为空时「下一步」不可用。
6. `RERUN` 模式下 key 的 `placeholder` 改成「留空表示不改」。
7. 放弃时 `dismiss(SetupOutcome(ABANDONED, ()))`。
8. ⚠ **本文件一个 trace 埋点都不加**，注释写明理由（plan「密钥不外泄」落点 3）：
   这里没有值得观测的东西，而它经手的恰恰是最敏感的字段。

**验证：** 临时在 `tests/test_setup_screen.py` 写一条 `run_test()` 用例：
推入 Screen，断言第 1 屏文本含配置路径；按 `escape` 后拿到 `ABANDONED`。

### T12: 第 3 屏（选模型）

**文件：** `rhinecode/tui/setup_screen.py`（续）
**依赖：** T11, T7

**步骤：**
1. 进入第 3 屏时用 `run_worker` 异步调 `probe.list_models`，期间显示等待态。
2. 结果回来后渲染选项列表：推荐项标出来，`blurb` 跟在后面。
   ⚠ **模型名与 blurb 渲染前过 `escape`**（服务端来的文本）。
3. `from_fallback=True` 时，清单上方挂一条**明确的兜底提示**（含
   `error` 原因）+「手动输入模型名」入口。
4. 选中后 `catalog.window_for` 推出 `context_window` 存进草稿。
5. 等待期间 `escape` 要能取消（spec N4）。

**验证：** 在 `tests/test_setup_screen.py` 加两条：注入一个必定成功的假
`list_models` 断言选项渲染出来；注入一个必定失败的，断言界面上出现兜底提示
且提示里含「兜底」字样。

### T13: 第 4 屏（终验与写盘）

**文件：** `rhinecode/tui/setup_screen.py`（续）
**依赖：** T12, T6

**步骤：**
1. 进入时异步调 `probe.verify`，显示等待态。
2. 成功 → 调 `writer.apply`，展示实际写入的文件清单 + 一句「三份可选模板
   全是注释、暂不改变行为」；出口「开始用」→ `dismiss(SAVED, written)`。
3. 失败 → 按 `ProbeFailure` 四类给不同措辞（**过 `escape`**），三个出口：
   「重填 key」（回第 2 屏）/「改接口地址」（回第 2 屏并聚焦地址）/
   「仍然保存并继续」（照样 `writer.apply` 后 `dismiss(SAVED, ...)`）。
4. `RERUN` 模式下成功文案追加「新配置下次启动生效」（F17）。

**验证：** `tests/test_setup_screen.py` 加三条：成功路径写盘且返回 `SAVED`；
失败路径三个出口都在；选「仍然保存并继续」后文件确实被写。

### T14: `setup_host.run_setup` 补全

**文件：** `rhinecode/tui/setup_host.py`
**依赖：** T1, T13

**步骤：**
1. 把 T1 的占位 Screen 换成真的 `SetupScreen`。
2. `run_setup(config_path, prefill, mode) -> SetupOutcome`：起 App、推 Screen、
   回调里 `exit(outcome)`、返回结果。
3. 异常兜底：App 跑挂时返回 `ABANDONED`（**不让向导的故障拖垮启动**），
   并在 stderr 打印一行可读提示。

**验证：** `python -m unittest tests.test_setup_host tests.test_setup_screen` 全绿。

---

## 阶段 E：接线

### T15: `__main__` 接入

**文件：** `rhinecode/__main__.py`
**依赖：** T4, T14

**步骤：**
1. 在「首次运行引导」那段之后插入触发判定：
   `reason = trigger.classify(config_path)`。
2. `if reason and sys.stdin.isatty():` → 调 `run_setup`；
   `ABANDONED` 则打印模板路径并 `sys.exit(0)`（**与今天的文案一致**）。
3. `elif reason:` → **原样保留今天的分支**（`config_created` 打印 + `exit(0)`，
   以及后面的占位符 `exit(1)`）。
4. ⚠ 注释写明 F2：非 TTY 与显式 `--config` 一律不弹向导，理由是 CI 与脚本包装。
5. 向导写完后**照常走 `load()`**，不跳过校验。

**验证：** `tests/test_setup_entry.py`（下一任务）。手动：
`echo "" | python -m rhinecode` 应保持老行为。

### T16: 入口分支测试

**文件：** `tests/test_setup_entry.py`
**依赖：** T15

**步骤：**
1. 用 `unittest.mock.patch` 替掉 `run_setup` 与 `build_app`，
   `patch("sys.stdin.isatty", return_value=True/False)`。
2. 四条用例：
   - TTY + 缺配置 → `run_setup` 被调用一次
   - **非 TTY + 缺配置 → `run_setup` 一次都没被调用**，且退出码与老行为一致
   - 显式 `--config` + 缺配置 → `run_setup` 未被调用
   - 配置完好 → `run_setup` 未被调用，直接 `build_app`
3. ⚠ 断言的是**调用次数**而不是「最终跑起来没有」——只断言后者的话，
   「没弹向导」与「弹了但立刻返回」看不出区别（照 C16 那条护栏的写法）。

**验证：** `python -m unittest tests.test_setup_entry` 全绿。

### T17: `/setup` 三处接线

**文件：** `rhinecode/commands/models.py`、`rhinecode/commands/builtins.py`、
`rhinecode/tui/app.py`
**依赖：** T13

**步骤：**
1. `CommandController` 协议加 `def open_setup(self) -> None: ...`。
2. `builtins.py` 注册 `CommandSpec(name="/setup", aliases=(), type=UI, ...)`，
   `description` 一句话；⚠ **写入必须直接操作两个源列表之一**，
   不能对 `_specs` 属性 append（那不报错也不生效）。
3. `RhineApp.open_setup()`：`writer.read_current` 取预填 →
   `push_screen(SetupScreen(..., mode=RERUN), self._on_setup_done)`。
4. `_on_setup_done(outcome)`：`SAVED` 则 `show_event("已保存到 <path>，
   新配置下次启动生效")`；`ABANDONED` 什么都不做。
5. ⚠ 三处都加注释指向**成对维护点 2**。

**验证：** `tests/test_setup_command.py`（下一任务）。

### T18: `/setup` 接线测试

**文件：** `tests/test_setup_command.py`
**依赖：** T17

**步骤：**
1. `/setup` 在注册表里且进入 `/help` 与补全候选。
2. 分发 `/setup` 时 `controller.open_setup` 被调用一次（用假 controller）。
3. ⚠ **反证**：断言 `CommandController` 协议里有 `open_setup`，
   且 `RhineApp` 确实实现了它——这条钉的正是「命令能补全但按了没反应」
   那种漏改形态。

**验证：** `python -m unittest tests.test_setup_command` 全绿。

---

## 阶段 F：收尾

### T19: 文档索引与能力表

**文件：** `CLAUDE.md`、`docs/extensions/README.md`
**依赖：** T18

**步骤：**
1. `docs/extensions/README.md` 索引表加一行（状态、一句话说明）。
2. `CLAUDE.md`「已实现的扩展」列表加一段，写明：四屏向导、`/setup` 可重跑、
   非交互逐字不变、模型清单动态拉取带兜底、以及**顺带修掉的两个默认值**。
3. 核对 T10 写的 `README.md` 措辞与最终实现一致。

**验证：** `python -m unittest tests.test_docs_links tests.test_docs_facts` 全绿。

### T20: 登记成对维护点

**文件：** `.claude/skills/paired-maintenance/SKILL.md`
**依赖：** T19

**步骤：** 按 plan 第八节登记 4 条，每条写清「漏改的表现是什么」。

**验证：** `grep -c "first-run-setup\|/setup\|setup/probe" .claude/skills/paired-maintenance/SKILL.md` 不为 0。

### T21: 全量回归

**依赖：** T20

**步骤：**
1. `python -m compileall rhinecode tests`
2. `python -m tests.run_parallel`
3. 有红的先修再往下。

**验证：** `python -m unittest discover -s tests` 全绿，skipped 4。

---

## 执行顺序

```
T1（先验风险）
 │
 ├─ T2 → T3 → T4
 │    └→ T5 → T6
 │    └→ T7 → T8
 │
 ├─ T9 → T10                （可与上面并行）
 │
 └─ T11 → T12 → T13 → T14   （依赖 T1、T7、T6）
                      │
                      ├─ T15 → T16
                      └─ T17 → T18
                                │
                                └─ T19 → T20 → T21
```
