# 阶段 0 · 基线数据

> 2026-08-22 实测。本文只记录**机器测出来的数字**，不含判断。
> 判断在 `docs/review/README.md`。

## 怎么复现

```bash
python -m tests.run_parallel                                   # 测试基线
python -m coverage run --source=rhinecode -m unittest discover -s tests
python -m coverage report --skip-covered --sort=cover
ruff check rhinecode tests --statistics                        # ruff 0.8.4
```

环境：Python 3.11.9 / Windows 11 / textual 8.2.8 / rich 15.0.0 / coverage 7.14.0。

---

## 1. 测试基线

| 项 | 值 |
| --- | --- |
| 用例数 | **3,323**（与 `CLAUDE.md` 一致） |
| 结果 | **3,323 / 3,323 全绿** |
| 墙钟（8 分片并行） | **33.7 秒** |
| 分片完整性自检 | 通过（`run_parallel` 比对期望条数） |

方案要求连跑 3 次（项目有 flaky 史）。**实跑 3 次：并行 33.7s / 串行（覆盖率那次）/ 并行 33.6s，三次全绿、条数一致。**

## 2. 覆盖率

| 项 | 值 |
| --- | --- |
| 总语句数 | 13,578 |
| 未覆盖 | 1,257 |
| **总覆盖率** | **91%** |
| 0% 的文件 | **0 个** |
| < 50% 的文件 | **2 个** |

最低的 10 个：

| 文件 | 语句 | 未覆盖 | 覆盖率 |
| --- | --- | --- | --- |
| `agent/cache_log.py` | 10 | 7 | **30%** |
| `tools/mcp_config.py` | 48 | 28 | **42%** |
| `mcp/transport.py` | 259 | 105 | 59% |
| `__main__.py` | 57 | 22 | 61% |
| `provider/factory.py` | 11 | 4 | 64% |
| `tools/edit_file.py` | 75 | 25 | 67% |
| `tui/clipboard.py` | 63 | 21 | 67% |
| `worktree/render.py` | 48 | 14 | 71% |
| `worktree/provision.py` | 68 | 19 | 72% |
| `hooks/report.py` | 84 | 22 | 74% |

### ⚠ 这个 91% 要怎么读（两条都必须一起看）

**它是保守估计。** 620 条测试真起子进程（e2e 宿主 / git / run_command），子进程里执行的产品代码 coverage 默认抓不到。`__main__.py` 61%、`mcp/transport.py` 59% 这类偏低的，正是子进程相关的那批。真实覆盖率**高于** 91%。

**但它在一个地方是误导性的。** `agent/prompt/texts/` 下 10 个提示词正文文件是模块级字符串常量——**import 就算 100% 覆盖**，而内容对不对完全没被验证。这个项目的行为很大程度上由提示词决定，覆盖率对这一块**没有任何指示意义**。

## 3. 静态检查（ruff 0.8.4）

### 默认规则集（E4/E7/E9 + F）

| 规则 | 数量 | 性质 |
| --- | --- | --- |
| F401 unused-import | 45 | 清理项 |
| E741 ambiguous-variable-name | 16 | 风格（`l`/`I`/`O` 做变量名） |
| F541 f-string-missing-placeholders | 6 | 清理项 |
| F841 unused-variable | 5 | 清理项 |
| E402 module-import-not-at-top | 3 | 多为刻意的延迟 import |
| **F821 undefined-name** | **1** | **真问题，见下** |

### 高信号规则组

| 规则组 | 数量 | 结论 |
| --- | --- | --- |
| **B** (bugbear) | **2** | 1 个真问题 + 1 个误报 |
| ASYNC | 0 | — |
| RET | 0 | — |
| C4 | 1 | 清理项 |
| PERF | 24 | 19 个 `manual-list-comprehension`，非热点路径 |
| RUF012 mutable-class-default | 35 | **全部误报**（见下） |
| RUF100 unused-noqa | 77 | 写了但实际不需要的 `noqa` |
| S110 try-except-pass | 50 | 与已分类的 fail-safe 吻合 |
| **RUF001/002/003** | **29,867** | **中文标点，必须关掉** |

### 逐条：ruff 找到的 2 个真问题

**① `rhinecode/conversation.py:1727` — F821 悬空类型注解**

```python
def _build_ask(self) -> "AskFn":
```

`AskFn` 定义在 `rhinecode/agent/loop.py:211`，`conversation.py` **从未导入它**。字符串注解运行时不求值，所以不崩；但 `typing.get_type_hints()` 会 `NameError`，任何类型检查器直接报错。同文件已有语义相近的别名 `ConfirmCallback`（`:112`）。

**② `rhinecode/config.py:347` — B904 异常链丢失**

```python
except FileNotFoundError:
    raise FileNotFoundError(f"{path} 配置文件不存在")      # ← 漏了 from
except yaml.YAMLError as e:
    raise ValueError(f"配置文件 YAML 解析失败 {e}") from e  # ← 下一行就写了
```

同一个 try 块两个分支风格不一致，是笔误。后果是原始异常的 traceback 丢失。

### 逐条：确认为误报的（别去"修"）

**B008 `agent/loop.py:1016`** — `options: "RunOptions" = RunOptions()`。看起来是可变默认参数陷阱，实际 `RunOptions` 是 `@dataclass(frozen=True)` 且字段用 `frozenset()` 而非 `set()`，docstring 里写明"打包成一个 frozen 数据类"。**作者是懂这个陷阱的。**

**RUF012（35 个全部）** — 抽查三处：`tools/read_file.py:25` 是 `parameters = {...}`（工具的 JSON schema 常量）、`conversation.py:191` 是 `_EFFORT_CYCLE` 查表常量、`widgets.py:3398` 是 Textual 框架约定的 `BINDINGS`。全部是"该标 `ClassVar` 但没标"的风格问题。

### ⚠ 上 ruff 前必须先做的事

`RUF001/002/003`（ambiguous-unicode）对中文标点全量触发，一口气 **29,867 条**——不在配置里禁用这三条，ruff 对本项目完全不可用。这不是项目的问题，是规则与中文项目不兼容。

## 3b. 类型检查（mypy 2.3.1，`--ignore-missing-imports`）

**109 个错误 / 19 个文件（检查了 171 个源文件）。** 对一个从未跑过类型检查的
51k 行项目，这个数字非常低——项目的类型注解覆盖率约 87%，且大体是准的。

| 错误码 | 数量 | 性质 |
| --- | --- | --- |
| arg-type | 41 | 多为 `Optional` 传给非 Optional 形参 |
| assignment | 20 | 协议类型与具体实现的赋值 |
| **union-attr** | **16** | **Optional 未判空就取属性——最可能是真 AttributeError** |
| attr-defined | 15 | 动态属性 |
| **call-arg** | **5** | **`Tool.execute` 的签名契约问题，见下** |
| override | 3 | 子类签名不兼容 |
| 其它 | 9 | — |

**mypy 独立确认了 ruff 的 F821**：`conversation.py:1727: Name "AskFn" is not defined [name-defined]`。两个工具用不同机制指向同一处，可信度高。

### `call-arg` 那 5 条揭示了一个真实的契约缺口

`Tool.execute` 的基类签名（`tools/base.py:229`）是：

```python
def execute(self, args: dict) -> ToolResult:
```

**既没有 `cwd` 也没有 `plan_stage`**。而 20 个工具实现分裂成三种签名：

| 签名 | 数量 | 例子 |
| --- | --- | --- |
| `execute(args)` | 8 | `web_fetch` / `web_search` / `load_skill` |
| `execute(args, cwd=None)` | 6 | `read_file` / `write_file` / `run_command` |
| `execute(args, plan_stage=False)` | 6 | `run_agent` / `send_message` / `todo_write` |

调用方 `agent/loop.py:2107` 与 `:2265-2269` 用两个布尔标志 `tool.workspace_aware`
与 `tool.plan_safe` 决定传哪些参数。**这是有意的设计**，注释写明"声明了 `plan_safe`
的工具**必须**接受这个关键字参数（契约写在 `Tool.plan_safe` 的说明里）"，且有护栏
`tests/test_loop_cwd_dispatch.py`。

**缺口在于护栏钉的不是这条契约。** 那 7 条用例用的是替身工具，验的是**分发逻辑**
（loop 有没有按标志正确传参），**不是**"所有真实工具的签名与自己的标志一致"。
全仓只有 `tests/test_todo_tool.py:99` 一处对单个工具做过 `inspect.signature` 检查。

失败形态正是本项目定义的那种「漏改一律不报错」：新工具声明 `workspace_aware = True`
却忘了给 `execute` 加 `cwd=None` → 编译过、全部测试绿 → 只在该工具真被调用时
`TypeError`。而**两条路径的表现还不一样**：串行路径（`:2272`）有 `except Exception`
兜成"工具执行异常"，并发路径（`:2107`）刻意不加 try/except、靠 `future.result()` 兜。


## 4. 依赖闭包

| 项 | 结果 |
| --- | --- |
| 声明的依赖 | textual / openai / pyyaml / httpx |
| 实际直接 import 的第三方 | textual / openai / yaml / httpx / **rich** |
| `rich` 现状 | 是 textual 8.2.8 的直接依赖（`Requires: ... rich ...`），**当前不会崩** |
| 本机 textual 版本 | **8.2.8** |
| 声明的 textual 下界 | **`>=0.80.0`** |
| 代码实际用到的 textual API | `textual.content`（`Content`, `Span`）、`textual.style`（`Style`）——**3.0+ 才有** |

**下界与实际需求差 7 个大版本。** `textual.content` 在 0.80 里不存在，装到旧版启动即 `ModuleNotFoundError`。

未验证（留给阶段 1）：干净 venv 里 `pip install .`（非 `-e`）的实际结果、Linux 平台。

## 5. 依赖方向（AST 全量扫描）

对 `rhinecode/` 下 171 个文件做 AST 解析，统计跨子包 import，再跑 Tarjan 强连通分量。

**结果：10 个包处于同一个强连通分量**（互相可达）：

```
agent ↔ context ↔ hooks ↔ mcp ↔ permission ↔ skills ↔ subagents ↔ tools ↔ web ↔ worktree
```

枢纽是 `tools`：它 import `mcp`/`skills`/`subagents`/`team`/`todo`/`web`，同时被其中大多数反向 import。

### 叶子包声明的逐条核验

| 包 | CLAUDE.md 说 | 实测 import | 结论 |
| --- | --- | --- | --- |
| `classifier` | 只依赖 `provider.base` 与 `trace`，**绝不 import permission/agent/tools** | `provider`, `trace` | ✅ **不变量成立** |
| `todo` | 只依赖标准库与 `trace`，绝不 import `team` | `trace` | ✅ **成立** |
| `team` | 叶子包 | `provider`, `trace` | ✅ 成立 |
| `trace` | 叶子包只依赖标准库 | `provider`（`tracing_provider.py:26`） | ⚠ 有一处 |
| `skills` | 叶子包 | `permission`, `trace` | ❌ 它在环里 |
| `web` | （README 标为叶子包） | `permission`, `provider`, `tools`, `trace` | ❌ 不是叶子 |

### 这不是隐患，但护栏有缺口

`rhinecode/tools/__init__.py` 有 **2,799 字节的 docstring**，把这个互依结构、为什么不成环、以及违反后的具体报错形态逐条写明，并标注"**警告：不要在此处 re-export 任何子模块**"。**这是被刻意管理的已知结构。**

缺口在护栏：`CLAUDE.md` 架构表把它列为「⚠ 致命不变量」，而同类约束在别的包上**都有护栏测试**——

- `permission/__init__.py` 的：`tests/test_classifier_broad.py:219`
- `trace/__init__.py` 的：`tests/test_trace_reader.py:404`
- **`tools/__init__.py` 的：一条都没有**

即：图方便在那里加一行 re-export，测试全绿，直到某个 import 顺序下崩掉，且报错位置离原因很远（docstring 自己写了这一点）。

## 6. 未完成项

| 项 | 状态 |
| --- | --- |
| mypy | ✅ 已装并跑完（2.3.1），见 3b |
| pyright | 未跑（mypy 已足够） |
| 全量测试第 3 次 | ✅ 已补，三次全绿 |
| 干净 venv 安装验证 | 留给阶段 1 |
| Linux 平台验证 | 留给阶段 1 |
