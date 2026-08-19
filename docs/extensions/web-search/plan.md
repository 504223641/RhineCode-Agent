# 网络搜索工具（web_search）Plan

> 状态：待批准（2026-08-19，第 1 轮）
>
> 输入：已批准的 [`spec.md`](spec.md)。本文只回答「怎么做」，不重复「做什么」。
>
> 语言：Python 3.11+，与既有代码同栈（`httpx` 已是 `web_fetch` 的依赖，不新增第三方库）。

## 架构概览

本扩展分三块，边界与 `web_fetch` 那一轮完全对齐：

```
                        ┌─────────────────────────────────────────┐
   模型发起调用          │  tools/web_search.py  WebSearchTool     │
        │               │  只做参数取值与异常兜底，不含任何业务逻辑 │
        ▼               └──────────────────┬──────────────────────┘
 ┌──────────────┐                          │
 │ 权限管线      │                          ▼
 │ ①②②′②″③④   │      ┌───────────────────────────────────────────┐
 │ + C16 分类器  │      │ web/search_manager.py  WebSearchManager   │
 └──────────────┘      │ **本扩展唯一有副作用的模块**               │
        （执行前）      │ · 配额计数（加锁，预留+失败回退）          │
                       │ · 发一次 HTTP                             │
                       └────┬──────────────────────────┬───────────┘
                            │                          │
                            ▼                          ▼
              ┌──────────────────────────┐  ┌──────────────────────────┐
              │ web/search.py            │  │ web/search_render.py     │
              │ 纯逻辑、零 IO            │  │ 纯逻辑、零 IO            │
              │ · 服务商适配（Brave）    │  │ · 不可信标记 + 元信息     │
              │ · 参数规范化 / 端点校验  │  │ · 四类文案                │
              │ · 响应解析 → 值对象      │  │ · TUI 单行摘要            │
              └──────────────────────────┘  └──────────────────────────┘
```

这个分层沿用 `context/manager.py`、`memory/manager.py`、`web/manager.py` 的既有约定：
**一层里只有 manager 有副作用，其余模块保持纯逻辑**，因而可以脱离网络单测（spec N5）。

三个模块之外，本扩展还要在**九个既有位置**各插一个分支。它们全都是 spec 里那张
「改造点」表的落点，逐一列在下面的「既有代码改动」一节。

## 核心数据结构

### `SearchResult`（新增，放 `web/models.py`）

一条搜索结果。`frozen=True`，与既有的 `FetchOutcome` / `ExtractOutcome` 同风格
——构造后单向传递、不被改写，也让它能安全进断言与日志。

| 字段 | 类型 | 含义 |
|---|---|---|
| `title` | `str` | 标题。**外部不可信内容**，SEO 投毒的主要落点 |
| `url` | `str` | 结果地址。**不做任何可访问性判断**（spec F20） |
| `snippet` | `str` | 摘要。**外部不可信内容** |

### `SearchOutcome`（新增，放 `web/models.py`）

一次搜索的完整结果，是 manager → tool → render 之间唯一的传递单位。

| 字段 | 类型 | 含义 |
|---|---|---|
| `ok` | `bool` | **「有没有真的问出去并拿到答复」**，不是「有没有抛异常」。0 条结果也是 `True`（spec F21 那张表） |
| `query` | `str` | 原始查询词**原文**，不截断、不改写 |
| `provider` | `str` | 服务商名（`"brave"`） |
| `results` | `tuple[SearchResult, ...]` | 结果列表；失败时为空 |
| `used` | `int` | 本次会话已用次数（含本次） |
| `limit` | `int` | 上限；`0` = 不限制 |
| `failure` | `str` | 失败类别，四选一常量之一；成功时为空串 |
| `error` | `str` | `failure` 非空时的补充说明（异常原文、HTTP 状态码等） |

**`failure` 用常量而不是布尔组合**，四个取值定义在 `web/search.py`：

```
FAILURE_NO_KEY   = "no_key"     # 未配置密钥
FAILURE_QUOTA    = "quota"      # 配额已用完
FAILURE_SERVICE  = "service"    # 服务不可用 / 超时 / 响应解析不出来
（空串）                          # 成功（含 0 条结果）
```

⚠ **它必须是一个显式字段，不能靠 `error` 文本判断。** spec F21 要求四类各有文案、
且「是否计入配额」「`ok` 取什么值」都按类别分岔——用字符串匹配去分类是典型的
「改一个字就静默失效」。

### `SearchProvider`（新增，放 `web/search.py`）

**服务商适配的那道边界**（spec「不做的事」：留边界、只实现一家）。

```
SearchProvider（frozen dataclass）
├── name          : str                     服务商名，进结果元信息
├── endpoint      : str                     默认端点
├── auth_header   : str                     密钥放哪个请求头
├── build_params  : (query, count) -> dict  构造查询参数
└── parse         : (payload) -> list[SearchResult]   解析响应
```

模块里只有一个实例 `BRAVE`，与一张 `PROVIDERS = {"brave": BRAVE}` 表。

**为什么是「两个函数装进一个 frozen dataclass」而不是抽象基类**：一个基类只有
一个子类时，它表达不出任何东西，只是把两个函数挪到了别处并多一层继承。
dataclass 里放两个 callable 是同样的接缝、少一层间接，而且**加第二家的成本是
写两个函数 + 加一行表项**，不是重构。

### `WebSearchManager`（新增，`web/search_manager.py`）

| 成员 | 用途 |
|---|---|
| `_provider: SearchProvider` | 服务商适配 |
| `_api_key: str` | 密钥；空串 = 未配置 |
| `_endpoint: str` | 实际端点（配置覆盖过的） |
| `_max_results: int` | 缺省条数 |
| `_limit: int` | 会话配额上限；`0` = 不限 |
| `_timeout: float` | 单次请求超时 |
| `_client_factory: Optional[Callable]` | HTTP 客户端工厂；**注入替身用**（spec N5） |
| `_used: int` | 已用次数 |
| `_lock: threading.Lock` | 保护 `_used` |

对外三个方法：

```
search(query: str, count: Optional[int]) -> SearchOutcome   # 唯一的业务入口
reset_quota() -> None                                       # /clear 调用
quota_state() -> tuple[int, int]                            # (已用, 上限)，供展示与测试
```

## 模块设计

### `web/search.py` —— 纯逻辑、零 IO

**职责**：服务商适配 + 三个纯函数。**只依赖标准库**（`urllib.parse`），
不 import `permission`、不 import `httpx`（spec N4）。

| 函数 | 签名 | 说明 |
|---|---|---|
| `normalize_count` | `(raw, default) -> int` | 夹取到 1–10；`None` / 非数字 / 越界一律**夹取或取缺省，不报错**（spec F1） |
| `check_endpoint` | `(url) -> Optional[str]` | 端点校验：非 `http`/`https` 返回中文原因，合法返回 `None` |
| `_brave_params` | `(query, count) -> dict` | `{"q": query, "count": count}` |
| `_brave_parse` | `(payload) -> list[SearchResult]` | 从 `payload["web"]["results"]` 逐条取 `title` / `url` / `description` |

⚠ **`_brave_parse` 必须是防御式的**：任一层键缺失、类型不对、单条结果缺字段——
一律**跳过那一条**而不是抛异常。整个响应取不出任何一条时返回空列表，由 manager
判成 `FAILURE_SERVICE`（spec F21 第三行「返回无法解析」）。理由是我们**连不上真实
服务去核对字段名**（本机 DNS 把公网域名重写成内网地址，见 web_fetch 验收记录遗留项 #3），
因此实现期必须假设自己对字段名的记忆可能有偏差，让「解析不出来」走一条可读的失败路径，
而不是让一个 `KeyError` 冒泡上去。

⚠ **`check_endpoint` 刻意不复用 `permission/network.py` 的 `split_url`。** 那会让
`web/search.py` 依赖 `permission`，而 `import permission.network` 会连带执行
`permission/__init__.py`、把整个引擎与 `rhinecode.tools` 拉起来——spec N4 的叶子性当场失效。
这里要判的只有「协议是不是 http/https」，`urllib.parse.urlsplit` 三行就够，
**不是同一个判断，不构成重复实现**（②′层判的是「地址范围 + 凭据 + 协议」，
而端点由用户写死、按 spec F11 明确不过②′）。

### `web/search_render.py` —— 纯逻辑、零 IO

**职责**：把 `SearchOutcome` 渲染成两样东西——回灌模型的完整文本、TUI 单行摘要。

沿用 `web/render.py` 的两条约定：

1. **元信息在标记外、结果在标记内**（spec F19）。模型看到「服务商：brave · 5 条」时，
   必须能确定那是系统说的，而不是某条搜索结果里写的。
2. 成功与失败走两条渲染路径，**失败文案按 `failure` 分四支**（spec F21）。

成功时的形状：

```
[web_search] 查询：<原文> · 服务商：brave · 结果：5 条 · 本次会话已用 3/50
<untrusted-content source="brave-search:<原始查询词>">
1. <标题>
   <地址>
   <摘要>
2. …
</untrusted-content>
```

四条失败文案的**要点**（完整措辞在 task.md 里定死，此处只写它们必须回答什么）：

| `failure` | 必须写明 |
|---|---|
| `no_key` | 这不是临时故障、**不要重试**、请让用户在配置文件的哪一项里填密钥 |
| `quota` | 已用/上限、这不会恢复、**不要重试**、请用已有信息继续 |
| `service` | 具体原因、**可以换个说法重试一次**、连续失败请让用户检查网络与配置 |
| 空串 + 0 条 | 走成功路径，但提示「这个词没搜到东西，可换个说法」 |

⚠ **`no_key` 与 `quota` 两条必须明确写「不要重试」，`service` 那条必须明确写
「可以重试一次」。** 三条文案如果长得差不多，模型会一律重试——而前两类重试
一万次也不会成功，只会把剩下的迭代轮次烧光（这正是 spec F13 不返回错误的同一条理由）。

`summary()` 返回 TUI 单行：`搜索「<查询词前若干字>」· 5 条 · 3/50`，失败时 `搜索失败：<类别>`。

### `web/search_manager.py` —— 唯一有副作用的模块

**职责**：配额 + 一次 HTTP + 组装 `SearchOutcome`。

`search()` 的执行顺序**是有讲究的**：

```
1. 密钥为空                    → FAILURE_NO_KEY   （不发请求、不计数）
2. _take_quota() 失败          → FAILURE_QUOTA    （不发请求、不计数）
3. 发一次 HTTP
     ├─ 异常 / 非 2xx / 解析不出结果 → _release_quota() → FAILURE_SERVICE
     └─ 成功                        → 组装 SearchOutcome（ok=True）
```

**配额用「预留 + 失败回退」而不是「成功后再加一」**，理由有两条，缺一个都不成立：

- **成功后再加一**在并发下守不住上限：主对话与三个子 Agent 同时发起搜索时，
  四个线程都读到 `used=49`、都判定「没超」，于是发出四次请求。
  预留是在锁内**读改一步完成**的，上限因此是硬的。
- **只预留不回退**会违反 spec F13 那张表：服务不可用时那一次不该计数。

```
_take_quota():           # 加锁
    if _limit > 0 and _used >= _limit: return False
    _used += 1; return True

_release_quota():        # 加锁
    if _used > 0: _used -= 1
```

⚠ **加锁临界区只做纯内存读写**——HTTP 调用、渲染、埋点一律在锁外。
这是本项目第 N 次面对同一条戒律（`hooks` / `skills` / `team` / `classifier` /
`subagents` 各有一条同样的不变量），违反的后果是一个卡住的 HTTP 请求锁死整个 manager，
而调用栈上没有任何线索。

⚠ **本类刻意不持有任何回调、不做任何跨线程调度**，从结构上杜绝违反上一条
（照抄 `subagents/tasks.py::TaskManager` 的先例）。

HTTP 调用本身：`client_factory` 为 `None` 时用 `httpx.Client(timeout=...)`，
否则用注入的替身（spec N5 / F28）。**只发 GET，只带一个鉴权头与一个 `Accept` 头**
（spec F2）。

### `tools/web_search.py` —— 工具层

照 `tools/web_fetch.py` 写，**不含任何业务逻辑**：取参数、调 manager、把
`SearchOutcome` 交给 render、构造 `ToolResult`。

四个类属性是本扩展的安全声明，各自有 spec 依据：

| 属性 | 取值 | 依据 |
|---|---|---|
| `read_only` | `False` | spec F3——只读会让分类器**无声失效** |
| `primary_arg` | `"query"` | 界面上要显示的是查询词（spec F7） |
| `classifier_scope` | `SCOPE_SEARCH` | spec F9 |
| `system_serial` | 不设（默认 `False`） | 它有真实副作用，该弹面板时就该弹 |

`ToolResult.ok` **必须取 `SearchOutcome.ok`**，不能是「本函数有没有抛异常」——
这条是照抄 `web_fetch` 验收期修掉的那个真实缺陷（界面把一次失败显示成绿色成功）。

⚠ `except Exception` 而**不是** `except BaseException`：用户按 Esc / Ctrl-C
要能中断一次卡住的搜索。

## 既有代码改动

九处，每一处都对应 spec「改造点」表里的一条。**共同点是漏改不报错**，
因此每一处都要在 checklist 里有一条护栏。

### ① 权限：请求种类新增「搜索类」

`permission/adapter.py`

```
_TOOL_MAP["web_search"] = lambda a: ("WebSearch", str(a.get("query") or ""), "search")
to_allow_rule():  if request.kind == "search": return request.rule_name, ""
```

**`to_allow_rule` 返回空模式（整工具形式）是必须的**：搜索类落在规则匹配的
「其它类」分支上，而那个分支只认空模式（`rule.pattern == ""`）。返回
`("WebSearch", <查询词>)` 会写出一条**永远不会命中任何东西的废规则**——
与 `web_fetch` 那次 `WebFetch(https://…?token=abc)` 是同一个形态，
而那正是 `to_allow_rule` 这个函数被造出来的原因。

⚠ 这两处是 `adapter.py` 模块 docstring 已登记的成对维护点（「新增一种 kind 时两处都要加」）。

### ② 权限：规则匹配层 —— **一行都不改**

`permission/rules.py` 的最后一行落到「其它类」分支：
`rule.pattern == "" and fnmatch(request.rule_name, rule.tool)`。

这正好是 spec F10 要的语义：`allow: WebSearch` 命中，`allow: WebSearch(任何东西)` 不命中。
**这一条要在 checklist 里有反证**——「不用改」这件事本身需要被钉住，
否则下一个人会以为是漏了。

### ③ 权限：第④层为搜索类加一格

`permission/engine.py`，放行档分支：

```
if request.kind == "url":     → ASK（既有，文案不动）
if request.kind == "search":  → ASK（新增，文案单独一句）
```

**两支刻意分开写而不是合并成 `kind in ("url", "search")`**：理由文案要不一样。
网络类那句说的是「未建立域名白名单时仍交由用户确认」，而搜索类**根本没有白名单这回事**
（spec F10），沿用那句话会把用户引去写一条不存在的规则。

⚠ **①②②′②″ 四层不动一个字**。它们各自按 `request.kind` 早退
（①只对 `"command"`、②只对路径三种、②′只对 `"url"`、②″只对 `"write_path"`），
搜索类天然不进。checklist 要有一条**四层并列的反证**——
构造 `rm -rf /` 与 `../../etc/passwd` 两条查询词，验证它们照常走到第④层而不是被前面拦下。

### ④ 权限：带括号写法的加载期容错（**spec 增补，见文末「需要审批的两处增补」**）

### ⑤ 分类器：新增第四种审查范围

`CLAUDE.md` 明确登记的成对维护点，**四处齐改，漏后三处不报错**：

| 文件 | 改什么 |
|---|---|
| `classifier/models.py` | `SCOPE_SEARCH = "search"`，并更新「三类」措辞 |
| `agent/loop.py::_review_action` | 加一支：`specifier = args.get("query")` |
| `classifier/prompt.py::render_pending` | 加一支措辞（见下） |
| `classifier/render.py::_SCOPE_LABELS` | 加 `SCOPE_SEARCH: "网络搜索"` |

`render_pending` 的搜索类措辞：

```
助手准备用下面这段文字去第三方搜索服务商检索
（注意：这段文字会**原样发给那家服务商**，等同于把它公开出去）
```

**括号里那半句不是修辞。** 分类器要判的核心问题是「这段文字发出去要不要紧」，
而不是「这个搜索请求合不合理」。不点破的话它会去评价「搜这个有没有用」——
那是另一个问题，而且不是它该管的。

`classifier/cache.py` **不改**：`cache_key` 已经是「非网络类一律返回空串（不缓存）」，
搜索类天然不缓存，正是 spec F9a 要的。checklist 要有反证钉住这条「不用改」。

熔断后的退路 `agent/loop.py` **也不改**：既有代码是「消息类返回原结论，其余一律降为 ASK」，
搜索类落「其余」，退回逐次弹面板——正是 spec F12 要的。同样要有反证。

### ⑥ 分类器：宽泛放行规则丢弃新增一类

`classifier/broad.py` 当前只认命令类（`tool != "Bash"` 直接返回 `False`）。

新增：

```
is_broad_search_allow(tool, pattern) -> bool     # tool == "WebSearch" 且 pattern 为空
is_broad_allow(tool, pattern) -> bool            # 伞：命令类 or 搜索类
why_broad(tool, pattern) -> str                  # 加一支搜索类理由
```

**为什么加一个伞函数而不是让装配层写 `A(...) or B(...)`**：装配层那个循环是
本扩展与 C16 共用的唯一落点，写成两个调用的话，将来加第三类时**漏加一个 `or`
不会报错**，只表现为某一类规则悄悄不再被丢弃。伞函数把「有哪些类」收在一处。

⚠ **装配层的调用必须由 `cfg.search_enabled` 把门**：spec F4 要求关闭该能力时
`allow: WebSearch` **不被丢弃**（关掉的能力不该影响用户的规则文件）。

### ⑦ 界面：确认面板

`tui/widgets.py`，两处：

1. **补充展示行**：既有 `_url_detail_lines` 只对 `kind == "url"` 生效。
   新增 `_search_detail_lines`，对 `kind == "search"` 展示**完整查询词（不截断）**
   与判定来源层。
   ⚠ **必须用本模块的 `escape`，绝不用 rich 那版**——查询词是用户/模型的自由文本，
   出现落单的 `[` 完全正常，而那会在布局阶段抛 `MarkupError`，**没有任何 try/except
   兜得住，整个 app 退出**。这条与 `_url_detail_lines` 的注释同源。
2. **选项少一个**：`protected = layer_value == "protected"` 这个判据要扩展成
   「保护路径 **或** 搜索类」→ 不渲染「永久放行」（spec F14）。

   ⚠ **但两者的「本会话放行」不一样，不能顺手合并成一个布尔**：
   保护路径那一支的「本会话放行」走的是引擎里的**内存豁免集合**，
   而搜索类的「本会话放行」走**正常的会话级规则**（`allow: WebSearch`，
   F16 只丢文件规则集，会话级不受影响）。
   因此实现上要两个判据：`no_permanent = protected or is_search`（控制选项条数）、
   `protected`（控制说明文字与 `conversation.py` 那侧的机制）。
   合并成一个会让搜索类的「本会话放行」跑去登记一个保护路径豁免——**不报错，
   但那次放行不生效**。

`tools/display.py`：`TOOL_LABELS["web_search"] = "WebSearch"`。

⚠ **一处 spec 措辞的落地澄清**：spec F7 说「确认面板与工具活动行两处完整展示」。
工具活动行的**折叠态**必然截断（它只有一行高），完整查询词由**展开态**
（`Ctrl+O`，走 `resolve_full_title`，既有实现已不截断）与**确认面板的补充行**
承担。这不改变 F7 的意图——**做决定的地方（面板）必须看得到全文**，
而折叠态只是给人扫读的。checklist 按这个口径写。

### ⑧ 配置、装配与记录

`config.py` 新增一段（整段可缺省）：

```yaml
search:
  enabled: true          # 安全开关口径：非法值抛错
  provider: brave        # 未知服务商抛错（写错等于把数据发给了别处/发不出去）
  api_key:               # 缺省空串
  endpoint:              # 缺省空 = 用服务商官方地址
  max_results: 5         # 调优项口径：非法值回退默认
  session_quota: 50      # 调优项口径；0 或负数 = 不限制
  timeout: 10            # 调优项口径
```

**两种解析口径是刻意的，别顺手统一**（照抄 `classifier` 段的既有注释）：
`enabled` / `provider` 决定「要不要把数据发出去、发给谁」，写错是明确的配置错误，
静默回退会让用户以为关掉了而其实没关；其余三项写错最坏是数值不对，不该阻断启动。

`bootstrap.py`：

- 在 `web_fetch` 那一步（④'）旁边建 `WebSearchManager` 并注册工具，
  **位置约束与 `web_fetch` 完全相同**：必须在 `exclude_tools` 摘除与
  `session_start` 快照之前。（它**不需要** provider，因此没有「必须在第②步之后」那条约束。）
- 新增注入参数 `search_client_factory`，与既有的 `web_client_factory` / `web_resolver` 并列。
- **端点协议校验放在这里**，不放 `config.py`：`check_endpoint` 返回非空即抛
  `BootstrapError`（`file://` 端点会让搜索工具变成文件读取工具，与②′的协议限制同性质）。
  非 https 的合法端点则走 `add_startup_notice` 出一条警告（spec F17）。
- 未配置密钥时 `add_startup_notice` 一条（spec F27）。
- 「外部不可信内容」系统提示模块的注入条件：
  `cfg.web_fetch_enabled` → `cfg.web_fetch_enabled or cfg.search_enabled`（spec F19）。
- 宽泛规则丢弃的循环改调 `is_broad_allow`，并由 `cfg.search_enabled` 把门。

`conversation.py`：属性注入 `manager.web_search_manager`（照抄 `manager.classifier`
与 `load_skill` 的先例，解耦构造顺序），`clear()` 里调 `reset_quota()`（spec F13/AC21）。

⚠ **`clear()` 是成对维护点**：它已经要复位 c8 压缩锚点与熔断、卸载 Skill、清空 c15 花名册。
再加一件，而**漏掉不报错**——只表现为「清空对话之后配额没回来」。

`trace/models.py::redact_config`：白名单新增 **三项**——
`search_enabled`、`search_provider` 原样记录，`search_api_key` 记固定掩码。

⚠ **`search_endpoint` 刻意不记录**：用户自定义端点可能把令牌写在查询串里
（部分搜索服务就是这么设计的），而 `redact_config` 是白名单式取值——
不加进去就是默认安全，加进去就要再判一次内容。不加。

`skills/validation.py` 与 `skills/audit.py`：词汇表加 `WebSearch`
（认 `websearch` / `web_search` 两种写法，与既有 `webfetch` / `web_fetch` 同形），
两处「可用类别」文案各加一个名字。

### ⑨ 端到端驱动设施

`tests/e2e/webstub.py` 扩展：新增 `make_search_client_factory(sites=None)`，
预置三组结果——正常结果、**标题与摘要里藏伪装指令的注入样例**（spec AC27）、
一条指向未在白名单内的域名的结果（spec AC28 的衔接点）。

`tests/e2e/host.py`：`--web-stub` **一并覆盖搜索**（一个开关管两样，
少一个会漏改的地方），透传 `search_client_factory`。

⚠ `web_search` **刻意不进 `EXCLUDED_TOOLS`**，理由与 `web_fetch` 相同——
它是要被端到端验的对象；隔离由注入替身保证。

⚠ **剧本 Provider 会被分类器吃掉轮次**：分类器与主对话共用 `provider_factory`
（刻意的，绕过注入的工厂会让测试静默连上真实网络），因此每写一个 web_search 的
端到端剧本，都要先读 `tests/e2e/scripted.py::is_classifier_request` 上方的说明。
C15 的并行组队剧本真的因此整个错位过。

## 模块交互

一次成功搜索的完整调用链（**从模型产出工具调用开始**）：

```
Agent Loop 预扫
  ├─ adapter.to_request(tool, args, mode, cwd)
  │     → PermissionRequest(rule_name="WebSearch", specifier=<查询词>, kind="search")
  ├─ engine.decide(request)
  │     ① 命令黑名单   —— kind != "command"，早退
  │     ② 路径沙箱     —— kind 不在路径三种，早退
  │     ②′ 网络边界    —— kind != "url"，早退
  │     ③ 规则         —— 落「其它类」分支：只有 deny/allow WebSearch 命中
  │     ④ 模式兜底     —— 放行档 → ASK（新增的那一格）
  │     ②″ 保护路径    —— kind != "write_path"，原样返回
  ├─ _review_action(tool, tc, cwd) → ReviewAction(scope="search", specifier=<查询词>)
  └─ _apply_classifier(...)
        ├─ ALLOW  → 把 ASK 覆写成 ALLOW（面板一次都不弹）
        ├─ BLOCK  → DENY + 固定文案（理由只进界面与 trace）
        └─ FAILED → 未熔断即 DENY；已熔断则退回 ASK（弹面板）
                    ↓
WebSearchTool.execute(args)
  └─ WebSearchManager.search(query, count)
        ├─ 密钥空 → FAILURE_NO_KEY
        ├─ _take_quota() 失败 → FAILURE_QUOTA
        ├─ httpx GET <endpoint>?q=…&count=… ，头里带密钥
        │     异常/非 2xx/解析空 → _release_quota() → FAILURE_SERVICE
        └─ search.BRAVE.parse(payload) → SearchOutcome(ok=True, …)
             ↓
search_render.render(outcome)   → ToolResult.output（元信息 + <untrusted-content>）
search_render.summary(outcome)  → ToolResult.summary（TUI 单行）
```

**与 `web_fetch` 的衔接**（spec F20）：模型从结果里挑一个地址去调 `web_fetch`，
那一次是**全新的一轮工具调用**，从上图最顶端重新走一遍——`kind` 变成 `"url"`，
②′网络边界层这次**会**生效，域名策略照常求值。
搜索这一侧**不传递任何授权信息**，因此这条衔接是结构性成立的，不靠约定。

## 文件组织

```
rhinecode/
├── web/
│   ├── models.py           — 修改：加 SearchResult / SearchOutcome
│   ├── search.py           — 新建：SearchProvider、BRAVE、四个纯函数、失败类别常量
│   ├── search_render.py    — 新建：render / summary / 四类文案
│   └── search_manager.py   — 新建：WebSearchManager（唯一副作用）
├── tools/
│   ├── web_search.py       — 新建：WebSearchTool
│   └── display.py          — 修改：TOOL_LABELS 加一项
├── permission/
│   ├── adapter.py          — 修改：_TOOL_MAP + to_allow_rule 各加一支
│   ├── engine.py           — 修改：④层放行档加 search 一格
│   └── config.py           — 修改：WEB_SEARCH_RULE_NAME + 带括号写法容错
├── classifier/
│   ├── models.py           — 修改：SCOPE_SEARCH
│   ├── prompt.py           — 修改：render_pending 加一支
│   ├── render.py           — 修改：_SCOPE_LABELS 加一项
│   └── broad.py            — 修改：is_broad_search_allow / is_broad_allow / why_broad
├── agent/loop.py           — 修改：_review_action 加一支
├── tui/widgets.py          — 修改：_search_detail_lines + 选项少一个
├── conversation.py         — 修改：属性注入 + clear() 复位配额
├── config.py               — 修改：search 段（模板 + Config 字段 + 解析）
├── trace/models.py         — 修改：redact_config 加三项
├── skills/validation.py    — 修改：词汇表 + 文案
├── skills/audit.py         — 修改：文案
└── bootstrap.py            — 修改：注册、注入口、端点校验、两条启动提示、
                               不可信模块注入条件、宽规则丢弃扩展
tests/
├── test_web_search.py            — 新建：纯逻辑（夹取 / 端点 / 防御式解析）
├── test_web_search_render.py     — 新建：渲染与四类文案、标记内外之分
├── test_web_search_manager.py    — 新建：配额（含并发与失败回退）、注入替身
├── test_web_search_tool.py       — 新建：参数校验、ok 标志那张表
├── test_web_search_perm.py       — 新建：五层反证、④层三档、规则形状
├── test_web_search_classifier.py — 新建：四处齐改、参数名反证、不缓存、熔断退路
├── test_web_search_bootstrap.py  — 新建：开关、启动提示、宽规则丢弃闭环、系统提示注入条件
└── e2e/
    ├── webstub.py          — 修改：加搜索替身与三组预置结果
    └── host.py             — 修改：--web-stub 一并覆盖搜索
docs/extensions/web-search/
├── spec.md / plan.md / task.md / checklist.md
└── acceptance.md           — 开发后补
```

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 搜索放哪个包 | **新开 `web/search*.py` 三个模块**，不塞进 `fetcher.py` | 它是 API 调用不是 HTTP 抓取：没有重定向、没有 HTML 转换、没有逐跳硬校验。塞进去会让 `fetcher` 变成「什么都干」的模块 |
| 服务商接缝 | frozen dataclass 装两个 callable + 一张表 | 只有一个实现时抽象基类表达不出任何东西。加第二家 = 写两个函数 + 加一行 |
| 端点协议校验放哪 | **装配层**，不放 `config.py` | 保持 `config.py` 只做类型解析；且 `BootstrapError` 是既有的「致命错误」通道 |
| 端点校验实现 | `urllib.parse` 自己判，**不复用 `permission.network`** | 复用会让 `web/search.py` 依赖 `permission`，叶子性当场失效（spec N4） |
| 配额算法 | **预留 + 失败回退** | 「成功后加一」在并发下守不住上限；「只预留不回退」违反 spec F13 的计数表 |
| 配额作用域 | 一个 manager 实例，主对话与子 Agent 共享 | 额度是钱，不是每条对话各一份（spec F13） |
| 失败分类 | 显式 `failure` 常量字段 | 用 `error` 文本匹配来分类是「改一个字就静默失效」 |
| 结果要不要二次抽取 | **不做** | 结果本来就短。多一次模型往返 = 多一条失败路径 + 多一次计费 + 多一处可被注入 |
| 第④层两支 | `url` 与 `search` **分开写**，不合并条件 | 文案要不一样：搜索类根本没有域名白名单这回事，沿用网络类文案会把用户引去写一条不存在的规则 |
| 宽规则丢弃 | 加**伞函数** `is_broad_allow` | 让装配层只有一个调用点；写成 `A or B` 时将来漏加一个 `or` 不报错 |
| 面板选项判据 | `no_permanent` 与 `protected` **两个布尔** | 两者的「本会话放行」机制不同（内存豁免 vs 会话规则），合并会让搜索类的放行静默不生效 |
| trace 记录端点 | **不记录** | 自定义端点可能内嵌令牌；白名单式取值下「不加」就是默认安全 |
| trace 新增作用域 | **不新增** | 搜索是 HTTP 调用不是模型调用，既有的工具执行记录已覆盖 spec F24 |
| 端到端替身开关 | 复用 `--web-stub`，不新增开关 | 一个开关管两样，少一个会漏改的地方 |

## spec 覆盖自检

| spec 需求 | 归属 |
|---|---|
| F1 / F2 | `tools/web_search.py` 参数表 + `search.normalize_count` |
| F3 | `WebSearchTool.read_only = False` |
| F4 | `bootstrap` 由 `cfg.search_enabled` 把门（注册 / 丢规则 / 启动提示三处） |
| F5 | `WebSearchTool.description` |
| F6 | spec 与 `CLAUDE.md` 的安全边界条目（文档产物） |
| F7 | `_search_detail_lines`（面板）+ 既有 `resolve_full_title`（展开态） |
| F8 | 反向需求：`search.py` / `search_manager.py` 中不存在任何查询词改写 |
| F9 / F9a | `SCOPE_SEARCH` 四处齐改；`cache_key` 天然不缓存 |
| F10 | `_TOOL_MAP` 的 `kind="search"` + `rules.py` 既有「其它类」分支 |
| F11 | 既有四层的 `kind` 早退（不改代码，靠反证钉住） |
| F12 | `engine.py` 第④层新增一支 |
| F13 | `WebSearchManager` 的配额三方法 + `conversation.clear()` |
| F14 | `widgets.py` 的 `no_permanent` 判据 |
| F15 | 既有 deny 优先 + `is_broad_search_allow` 只认 allow |
| F16 | `broad.py` + `bootstrap` 丢弃循环 |
| F17 | `search.check_endpoint` + `bootstrap` 两条出口（抛错 / 警告） |
| F18 | `search_render.render` 的成功路径 |
| F19 | `search_render` 的标记内外之分 + `bootstrap` 注入条件 |
| F20 | 反向需求：搜索侧不传递任何授权信息（结构性成立，靠护栏钉住） |
| F21 | `SearchOutcome.failure` + `search_render` 四支文案 + `ToolResult.ok` |
| F22 | `WebSearchManager._timeout` |
| F23 | `SearchOutcome` 的 `query` / `provider` / `results` / `used` / `limit` |
| F24 | 既有 `tool_execute` 记录（不新增） |
| F25 | `redact_config` 白名单三项 |
| F26 | `skills/validation.py` 词汇表 |
| F27 | `bootstrap` 的 `add_startup_notice` |
| F28 | `search_client_factory` 注入口 + `host.py` 透传 |

## 需要审批的两处增补

开发前要定，因为它们各自会让 spec 多一条需求。

### 增补一：`WebSearch(...)` 带括号写法的加载期容错（建议编号 **F10a**）

**问题**：spec F10 说只支持整工具形式，`allow: WebSearch(anything)` 不命中——
这是对的。但反过来，**`deny: WebSearch(anything)` 也不命中**，
于是一个想拦住搜索的用户写了一条**什么都不拦的规则**，而没有任何提示。

这正是 `web_fetch` 的 F13 处理过的形态。建议照抄它那套「按效果分两支」：

| 写坏的是 | 怎么处理 | 为什么 |
|---|---|---|
| `allow: WebSearch(...)` | **整条丢弃** + 警告 | 丢弃一条放行 = 少放行一些，偏严 |
| `deny: WebSearch(...)` | **降级为整工具拒绝** + 警告 | 丢弃一条拒绝 = 少拦一些，偏松，不可接受 |

代价：`permission/config.py` 的加载期校验多一个分支。收益：把一条
「用户以为拦住了、其实没拦」的静默失效变成一条可读警告。

**建议采纳。**

### 增补二：`web_search` 与 `web_fetch` 的总开关是否合并

现状是两个独立开关（`web_fetch_enabled` 与 `search.enabled`）。可以合并成一个
「网络能力总开关」，但**建议不合并**：用户完全可能只想要其中一个
（「让它能查文档但别把我的问题发给第三方」是一个非常合理的配置），
而合并之后表达不了。

代价是 spec F19 那条注入条件必须写成「两者任一启用」，
而**那正是最容易漏改的一处**——checklist 里 AC26 专门钉它。

**建议不合并**（即维持 spec 现状，此条只是把取舍写下来备查）。
