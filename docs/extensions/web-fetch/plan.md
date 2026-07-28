# 网络访问工具（web_fetch）Plan

> 状态：待批准（2026-07-29，初稿）
>
> 上游：[`spec.md`](spec.md)（已批准）。本文档与语言相关，按项目技术栈（Python 3.11+ / httpx / Textual）编写。

## 架构概览

改动落在**三处已有的层**加**一个新的叶子包**：

| 位置 | 性质 | 做什么 |
| --- | --- | --- |
| `rhinecode/permission/` | 既有层，加一个模块 + 改四个 | ②′网络边界层的**判定逻辑**：硬校验、域名规则求值、白名单语义、模式例外 |
| `rhinecode/web/` | **新增叶子包** | 真实的抓取、连接期地址守卫、HTML 转换、抽取编排、结果渲染 |
| `rhinecode/tools/web_fetch.py` | 既有层，加一个文件 | `Tool` 实现，把参数交给 `web` 包，把结果包成 `ToolResult` |
| `rhinecode/agent/prompt/` | 既有层，加一段文案 | 「外部不可信内容」的系统约束（F21） |

一句话概括分工：**`permission` 决定「能不能去」，`web` 负责「去了之后怎么办」。**
两者唯一的耦合点是一个纯函数——地址范围判断（`is_forbidden_address`），
判定期与连接期共用它，保证两处口径不会漂移。

### 依赖方向

```
tools/web_fetch.py  ──→  web/  ──→  permission/network.py  ──→  tools/path_guard.py
                          │
                          └──→  provider/base.py
```

`tools → web → permission → tools` 在**包级别**看是一个环。它不成环，靠的仍是
`rhinecode/tools/__init__.py` **保持为空**——导入 `tools.path_guard` 不会连带执行
`tools/web_fetch.py`。这与既有的 `tools ↔ skills`、`tools ↔ mcp` 是**同一个机制、同一条戒律**，
本次不新增例外，但让这条戒律又多了一个依赖它的调用方。

`web` 是叶子包：不被 `permission` / `agent` / `commands` / `context` / `memory` / `skills` 反向依赖。

## ⚠ 一处需要你确认的实现位置调整

**spec F5 把「域名解析结果的地址校验」列在②′网络边界层的硬校验里；plan 把它挪到连接期执行。**

语义完全不变（照样是代码强制、照样不可被任何配置或模式放开），变的是**在哪一步做**：

| | spec 的字面位置 | plan 的位置 |
| --- | --- | --- |
| IP 字面量校验（`http://127.0.0.1/`） | 判定期 | 判定期（不变） |
| 协议、内嵌凭据校验 | 判定期 | 判定期（不变） |
| **域名解析结果校验** | 判定期 | **连接期**（`web/fetcher.py`，紧邻建立连接） |

三条理由：

1. **判定层必须保持零 I/O**（spec N4「新增的决策层是纯逻辑……不发起任何网络请求」、
   N5「单测不真连网」）。DNS 解析是 I/O。放进 `engine.decide()` 会让**每一次权限判定**
   都可能阻塞在网络上，也会让既有的一大批纯逻辑权限测试变成潜在的联网测试。
2. **连接期做反而更严。** 判定只发生一次，而 F8 允许同主机重定向自动跟随——
   每一跳都是一次新的连接，都要重新校验。校验放在连接期，**每一跳自动都过**；
   放在判定期则只覆盖第一跳。
3. **判定期做并不能消除 TOCTOU。** spec 的「安全边界」一节已经承认 DNS rebinding 的时间窗
   无法在本次封死。既然两处都关不掉那个窗口，就应该选**离 connect 更近**的那处，窗口更小。

对应地，本 plan 把这条明确定义为：`web/fetcher.py` 在每次发起连接前，
对该跳的主机名做解析并逐个校验解析出的地址；任一地址落入禁止范围即整次抓取失败，
返回的错误文案与判定层拒绝时同源（复用同一份原因字符串常量）。

**如果你要求严格按 spec 字面来（判定期解析），告诉我，我改 plan——代价是 N4/N5 要相应放宽。**

## 核心数据结构

### 权限层

```python
# permission/models.py —— 两处扩充

class Layer(str, Enum):
    BLACKLIST = "blacklist"
    SANDBOX = "sandbox"
    NETWORK = "network"      # 新增：②′网络边界层
    RULE = "rule"
    MODE = "mode"

# PermissionRequest.kind 新增取值 "url"：
#   specifier = 完整 URL 原文（不是主机名——主机名由 network 模块自己解析出来，
#   这样确认面板和 trace 记录里留下的是模型实际请求的那个地址，而不是被加工过的片段）
```

```python
# permission/network.py —— 新增模块（纯逻辑，零 I/O）

# 禁止的地址范围。用标准库 ipaddress 判断，覆盖 IPv4 与 IPv6。
FORBIDDEN_REASONS: dict[str, str]      # 类别 → 中文原因，判定期与连接期共用

def split_url(url: str) -> tuple[str, str]:
    """把 URL 拆成 (scheme, host)。解析失败抛 ValueError，由调用方转拒绝。"""

def check_hard(url: str) -> Optional[str]:
    """
    结构性硬校验。命中返回中文拒绝原因，通过返回 None。
    依次检查：URL 可解析 → scheme ∈ {http, https} → 不含内嵌凭据 →
    host 非保留名字（localhost 等）→ host 若是 IP 字面量则不在禁止范围内。
    **不做 DNS 解析**（见上文位置调整）。
    """

def is_forbidden_address(addr: str) -> Optional[str]:
    """
    单个 IP 地址的范围判断，命中返回中文原因。
    禁止范围：环回、私有地址段、链路本地（含 169.254.169.254 云元数据）、
    未指定地址、保留段、IPv6 的对应形态与 IPv4-mapped 形式。
    连接期守卫直接调用本函数，与 check_hard 共用同一份判断，口径不会漂移。
    """

def decide(request: PermissionRequest, merged: RuleSet) -> Optional[DecisionResult]:
    """
    ②′层的完整判定。返回 None 表示本层不下结论（交由④模式兜底）。
    步骤：check_hard → merged.evaluate（deny 优先，复用③层求值）→
    「存在 allow 域名规则但未命中」则 DENY。
    """
```

```python
# permission/rules.py —— RuleSet 增一个查询方法

def has_allow_for(self, rule_name: str) -> bool:
    """是否存在针对该工具的 allow 规则（白名单是否已被建立）。"""
```

```python
# permission/matching.py —— 新增第三种匹配算法

def match_domain(pattern: str, host: str) -> bool:
    """
    域名模式匹配（spec F11）。pattern 是去掉 "domain:" 前缀后的部分。
    归一化：两侧转小写、去末尾 "."。
    通配：前导 "*." → 任意深度子域（不含裸域）；单独 "*" → 全匹配；
    其余位置的 "*" → 只匹配 [^.]* （不跨点）。
    """
```

### web 包

```python
# web/models.py —— 值对象，全部 frozen dataclass

@dataclass(frozen=True)
class FetchOutcome:
    """一次 HTTP 抓取的结果（未经抽取）。"""
    ok: bool
    source_url: str          # 模型请求的原始地址
    final_url: str           # 跟随同主机重定向后的最终地址
    content_type: str
    text: str                # 已做本地转换的正文（失败时为空）
    truncated: bool          # 是否因体量上限被截断
    redirect_to: Optional[str]   # 跨主机重定向时的目标（F8），非空表示未抓取
    error: str               # ok=False 时的中文原因

@dataclass(frozen=True)
class ExtractOutcome:
    """抽取阶段的结果。"""
    ok: bool                 # False 表示走降级路径
    text: str                # 抽取答案，或降级时的原文节选
    degraded_reason: str     # ok=False 时说明为什么降级
```

## 模块设计

### `permission/network.py` — ②′网络边界层

**职责：** 对 `kind == "url"` 的请求下结论。纯逻辑，零 I/O，可脱离网络与 TUI 单测。

**对外接口：** `check_hard` / `is_forbidden_address` / `decide`（签名见上）。

**依赖：** `permission.models`、`permission.rules`、标准库 `urllib.parse` 与 `ipaddress`。

**为什么放在 `permission/` 而不是新开一个包：** 它是决策管线的一层，和 `blacklist.py`（①层）
性质完全一致——纯匹配逻辑、不可被配置放开、被 `engine.decide` 顺序调用。放进 `permission/`
使「五层防御」这件事在一个目录里读得完；新开包会让读者以为它是独立子系统。

### `permission/engine.py` — 管线插入点

`decide()` 的改动有三处，改完后的顺序：

```python
def decide(self, request):
    # 规则合并提到最前面：②′层要用它做域名求值，③层用同一个对象，避免合并两次
    merged = RuleSet(self.turn_rules + self.session_rules + self.file_ruleset.rules)

    # ① 黑名单（kind == "command"）        —— 不变
    # ② 沙箱（read_path / write_path / glob）—— 不变

    # ②′ 网络边界（kind == "url"）—— 新增
    if request.kind == "url":
        verdict = network.decide(request, merged)
        if verdict is not None:
            return verdict
        # 不下结论 → 直接进④。**刻意跳过③**：②′ 内部已经用同一个 merged
        # 求过一次值，再求一次必然还是 None，重复求值只会让读代码的人怀疑自己看漏了。
    else:
        # ③ 规则 —— 不变
        hit = merged.evaluate(request)
        if hit is not None:
            return hit

    # 只读简化分支 —— 不变（web_fetch 是非只读，走不到）

    # ④ 模式兜底 —— 新增一处按 kind 的例外
    if request.mode is PermissionMode.STRICT:
        return DENY
    if request.mode is PermissionMode.PERMISSIVE:
        if request.kind == "url":
            return ASK   # ← spec F7 的例外
        return ALLOW
    return ASK
```

**⚠ 不变量（要写进代码注释与 CLAUDE.md）：** ②′**必须排在①②之后、③之前**。
排在①②之前无意义（那两层对 url 本就不生效），排在③**之后**则是致命的——
②′的白名单语义（「有 allow 但未命中即拒」）一旦晚于③执行，`allow: WebFetch(domain:*)`
之外的任何一条 allow 规则都会先在③层放行掉本该被白名单拦住的域名。

### `permission/adapter.py` / `rules.py` / `matching.py` / `config.py`

- **adapter**：`_TOOL_MAP` 加一行 `"web_fetch": lambda a: ("WebFetch", str(a.get("url") or ""), "url")`。
- **rules**：`_rule_matches` 加 `url` 分支——工具名精确匹配 `WebFetch`；`pattern == ""` 视为整工具命中（F12）；
  `pattern` 以 `domain:` 开头则取后半段交给 `match_domain`；其余写法**不命中**（加载期已丢弃并警告）。
- **matching**：新增 `match_domain`。
- **config**：`parse_rule_string` 对 `WebFetch(...)` 的括号内容做前缀校验，无法识别的整条丢弃并
  产出一条可读警告（F13），并入既有的 `load_all` 错误列表向上返回——**复用既有的错误展示通道，不新增渠道**。

### `web/fetcher.py` — 真实抓取 + 连接期守卫

**职责：** 执行 HTTP 请求，逐跳控制重定向，做连接期地址校验，做体量与时间上限。

**对外接口：** `fetch(url: str) -> FetchOutcome`

**主要步骤：**

1. 解析当前跳的主机名，**解析 DNS 并逐个校验解析出的地址**（`is_forbidden_address`）——
   任一命中即返回失败。
2. 用 `httpx.Client(follow_redirects=False)` 发起 GET，流式读取，**边读边累计字节**，
   超过 `MAX_RESPONSE_BYTES` 立即中断连接并标记截断。
3. 收到 3xx：取 `Location` 解析出目标主机。**同主机**则跳数 +1（超过 `MAX_REDIRECTS` 即失败）
   并回到第 1 步；**跨主机**则不跟随，返回 `redirect_to` 非空的 `FetchOutcome`（F8）。
4. 按 `Content-Type` 分派给 `convert`：HTML → 转换；文本类 → 原样；二进制 → 不取内容，
   只回报类型与体量。

**为什么关掉 httpx 的自动重定向：** `follow_redirects=True` 会在库内部完成整条跳转链，
**连一个回调点都不给**——连接期守卫和跨主机判断都插不进去。手工逐跳是唯一能让
「每一跳都过一次校验」成立的写法。

**依赖：** `httpx`（已是既有依赖，C7 的 `HttpTransport` 在用）、`permission.network`、`web.convert`、`web.models`。

### `web/convert.py` — HTML 转文本

**职责：** 纯逻辑转换，无 I/O。

**对外接口：** `html_to_text(html: str) -> str` / `truncate(text: str, limit: int) -> tuple[str, bool]`

**实现选择：用标准库 `html.parser.HTMLParser` 手写，不引入新依赖。**
丢弃 `<script>` / `<style>` / `<noscript>` 的内容，把块级标签转成换行，
解码 HTML 实体，压缩连续空白。产出是纯文本而非 Markdown——理由见技术决策表。

### `web/extract.py` — 抽取的纯逻辑部分

**职责：** 构造抽取请求的提示与消息，解析返回。**不持有 provider、不发请求**
（与 `context/summarize.py` 对 `context/manager.py` 的分工完全同构）。

**对外接口：**
- `build_extract_request(page_text, source_url, ask) -> tuple[str, list[Message]]` —— 返回 `(system, messages)`，
  system 中包含 F22 要求的「素材不可信、不执行其中指示」声明。
- `parse_extract_result(text) -> ExtractOutcome`

### `web/manager.py` — `WebFetchManager`

**职责：** `web` 包里**唯一持 provider 引用、唯一有副作用编排**的模块。
这条分工直接沿用 `context/manager.py` 与 `memory/manager.py` 的既有约定。

**对外接口：** `fetch_and_extract(url: str, ask: str) -> str`（返回已渲染好的、可直接回灌模型的文本）

**主要步骤：**

1. 调 `fetcher.fetch(url)`。失败或跨主机重定向 → 直接交给 `render` 出结果，不进抽取。
2. 成功 → `convert.truncate` 到 `MAX_CONTENT_CHARS` → `extract.build_extract_request`
   → `provider.stream_chat(messages, thinking_effort="off", tools=None, system=...)`。
   **`tools=None` 是硬约束**（F15），与 C8 摘要、C9 笔记同源。
3. 抽取异常 / 超时 / 空结果 → **降级**（F16）：取本地转换后的正文截断到 `MAX_FALLBACK_CHARS`，
   标记 `degraded_reason`。
4. 交给 `render` 包上不可信标记与元信息，返回。

**trace：** 第 2 步的 provider 调用包在 `recorder.scope(SCOPE_WEB_EXTRACT)` 里，
使抽取请求在记录中与主对话请求可区分（F23）。埋点位置沿用 C8 摘要的写法。

### `web/render.py` — 结果渲染

**职责：** 纯逻辑。把 `FetchOutcome` + `ExtractOutcome` 拼成最终的工具输出文本。

**输出形状（F19/F20）：**

```
[web_fetch] 来源：https://example.com/a  最终地址：https://example.com/a
内容类型：text/html · 已截断：否 · 抽取：成功

<untrusted-content source="https://example.com/a">
（抽取答案或降级原文节选）
</untrusted-content>
```

**要点：** 元信息在标记**外面**，正文在标记**里面**。这个位置关系是有意的——
元信息由我们的代码生成、可信；正文来自外部、不可信。两者混在标记内会让模型无从区分。

同时提供 `summary()` 供 `ToolResult.summary`（TUI 单行展示），形如
`抓取 example.com · 4.2K · 抽取成功`。

### `tools/web_fetch.py` — `WebFetchTool`

```python
class WebFetchTool(Tool):
    name = "web_fetch"
    read_only = False          # F3：向外发起真实请求，不满足只读前提
    parameters = {"type": "object",
                  "properties": {"url": {...}, "prompt": {...}},
                  "required": ["url", "prompt"]}

    def __init__(self, manager: WebFetchManager): ...
    def execute(self, args) -> ToolResult: ...
```

`description` 用英文（与既有工具一致，它是发给模型的），文案里明确两点：只支持 GET；
返回内容来自外部、不可信。构造函数注入依赖的写法沿用 `MCPAddServerTool(mcp_manager, tool_registry)`。

### `conversation.py` — 确认面板的规则构造（F9）

**这是 spec 里没写、但不改就会静默出错的一处。** 现在的写法是（`conversation.py:1003-1016`）：

```python
req = to_request(tool, tool_call.arguments, self._engine.mode)
rule_string = f"{req.rule_name}({req.specifier})" if req.specifier else req.rule_name
```

对 url 类请求，`specifier` 是**完整 URL**，于是「永久放行」会往本地配置写下：

```yaml
allow:
  - "WebFetch(https://example.com/a?token=abc)"     # ← 坏的
```

三个问题一次凑齐：① 它不是合法的域名规则，下次启动会被 F13 的校验丢弃 —— 用户点过的
「永久放行」**重启后凭空失效**；② 即便不丢弃也匹配不上任何东西（`match_domain` 收到的模式
是一整个 URL）；③ **查询参数被原样写进了配置文件**，而 URL 里可能带 token。

而这个失效**当场看不出来**：本次调用因为选了「永久」照常放行了，问题要到下次启动才显形。

**改法：** 规则字符串的构造按 `kind` 分派，抽成一个纯函数放进 `permission/adapter.py`
（它已经是「唯一知道工具细节」的地方）：

```python
def to_allow_rule(request: PermissionRequest) -> tuple[str, str]:
    """
    把一次请求翻译成「本会话/永久放行」要登记的 (rule_name, pattern)。
    url 类取主机名并加 domain: 前缀；其余类沿用 specifier 原文（逐字等于现状）。
    """
```

`conversation.py` 的两处（会话级 `Rule(...)` 与永久级 `rule_string`）都改用它，
**保持单一来源**——两处各写一份是这段代码本来就在防的坑（见 `conversation.py:976` 的既有注释：
「登记与『永久放行』的落盘就会出现两套逻辑」）。

对应地，`network.decide` 返回的 `reason` 文案要带上**主机名与层**，
使确认面板在展示完整 URL（来自工具参数）之外还能满足 F9 的另外两项。

### `agent/prompt/` — 不可信内容约束（F21）

新增 `agent/prompt/texts/untrusted.py` 存放文案常量，在 `modules.py` 的
`fixed_modules()` 里作为**第八个固定模块**加入，`cacheable=True`，
`priority=25`（排在「系统约束」20 与「任务模式」30 之间——它是对
`<system-reminder>` 那条语义说明的自然延伸，两者相邻便于模型建立对照）。

**能力关闭时（F4）不注入该模块**，因此 `fixed_modules()` 需要接受一个开关参数。
这是本次对 C5 结构的唯一改动。

## 模块交互

一次成功抓取的完整调用链：

```
模型发起 tool_call(web_fetch, {url, prompt})
    │
    ▼
agent/loop.py  ──→ permission/adapter.to_request()        kind="url", specifier=完整URL
    │                    │
    │                    ▼
    │              permission/engine.decide()
    │                    ├ ① 黑名单：kind 不是 command，跳过
    │                    ├ ② 沙箱：kind 不是路径类，跳过
    │                    ├ ②′ network.decide(request, merged)
    │                    │      ├ check_hard()          协议/凭据/IP字面量
    │                    │      ├ merged.evaluate()     deny 优先 → allow
    │                    │      └ has_allow_for()       白名单已建立但未命中 → DENY
    │                    └ ④ 模式兜底（放行档对 url 降级为 ASK）
    │                    │
    │                    ▼  ASK → TUI 确认面板 →「永久」→ config.append_local_allow("WebFetch(domain:<host>)")
    ▼
tools/web_fetch.execute()
    │
    ▼
web/manager.fetch_and_extract()
    ├──→ web/fetcher.fetch()
    │       ├ 解析 DNS → is_forbidden_address() 逐个校验   ← 连接期守卫（每跳都做）
    │       ├ httpx GET（follow_redirects=False，流式，字节上限）
    │       ├ 3xx 同主机 → 跳数+1 回到上一步 / 跨主机 → 返回 redirect_to
    │       └ web/convert.html_to_text()
    ├──→ web/extract.build_extract_request()
    ├──→ provider.stream_chat(tools=None)  ← 包在 scope(SCOPE_WEB_EXTRACT) 里
    │       └ 失败 → 降级为原文截断
    └──→ web/render.render()   ← 包上 <untrusted-content> 与元信息
    │
    ▼
ToolResult(ok, output, summary)  ──→ 回灌模型
```

### 装配顺序（`bootstrap.py`）

`WebFetchManager` 需要 provider，`WebFetchTool` 需要 manager，因此**不能**放进
`ToolRegistry.default()`（那里造不出 provider），与 `MCPAddServerTool` 同理。

插入位置：**②Provider 创建之后、`MCPAddServerTool` 注册之后、Skill 第一阶段之前**。
两头的约束：

- **必须在 Provider 之后**——manager 要持它，且要拿到已经被 `TracingProvider` 包过的那一个
  （否则抽取请求不进行为记录）。
- **必须在 `exclude_tools` 摘除与 `session_start` 快照之前**——摘除要能摘掉它；
  快照里的 `tool_names` 要与实际工具集一致（既有窄窗口注释里的理由，一字不改地适用）。

关闭时（`cfg.web_fetch_enabled is False`）：不创建 manager、不注册工具、
`fixed_modules()` 不注入不可信模块、`permission/config.py` 跳过 domain 语法校验与警告。

## 文件组织

```
rhinecode/
├── permission/
│   ├── network.py            ← 新增：②′层判定（硬校验 + 域名求值 + 白名单语义）
│   ├── engine.py             ← 改：decide() 插入②′；④层 url 例外；merged 提前构造
│   ├── models.py             ← 改：Layer 新增 NETWORK；kind 文档新增 "url"
│   ├── adapter.py            ← 改：_TOOL_MAP 登记 web_fetch
│   ├── rules.py              ← 改：_rule_matches 新增 url 分支；RuleSet.has_allow_for
│   ├── matching.py           ← 改：新增 match_domain
│   └── config.py             ← 改：domain: 前缀校验 + 警告
├── web/                      ← 新增叶子包
│   ├── __init__.py           ← 只 re-export WebFetchManager 与常量
│   ├── models.py             ← FetchOutcome / ExtractOutcome
│   ├── fetcher.py            ← httpx 抓取 + 连接期地址守卫 + 逐跳重定向 + 上限
│   ├── convert.py            ← HTML → 纯文本、截断
│   ├── extract.py            ← 抽取提示构造与结果解析（纯逻辑）
│   ├── manager.py            ← WebFetchManager：唯一持 provider、唯一编排副作用
│   └── render.py             ← 不可信标记 + 元信息渲染 + TUI 摘要
├── tools/
│   └── web_fetch.py          ← 新增：WebFetchTool
├── conversation.py           ← 改：确认面板的 allow 规则构造改用 adapter.to_allow_rule（两处）
├── agent/prompt/
│   ├── texts/untrusted.py    ← 新增：F21 文案常量
│   └── modules.py            ← 改：第八个固定模块 + 开关参数
├── trace/models.py           ← 改：新增 SCOPE_WEB_EXTRACT
├── trace/reader.py           ← 改：_LAYER_NAMES 加一行 network（漏了只会显示英文原名，不报错）
├── config.py                 ← 改：Config 新增 web_fetch_enabled / web_extract_model
└── bootstrap.py              ← 改：装配 WebFetchManager + WebFetchTool

tests/
├── test_perm_network_layer.py    ← ②′层判定（硬校验 / 白名单语义 / 模式例外）
├── test_perm_match_domain.py     ← 域名通配语义（含 example.* 不跨点的反证）
├── test_web_fetcher.py           ← 抓取：重定向逐跳、上限、连接期守卫（httpx 用替身）
├── test_web_convert.py           ← HTML 转换与截断
├── test_web_manager.py           ← 编排：tools=None、降级路径、trace scope
├── test_web_render.py            ← 不可信标记包裹（含降级路径也要包）
├── test_web_fetch_tool.py        ← 工具层：参数校验、异常兜底、summary
└── test_perm_allow_rule.py       ← to_allow_rule：url 类取主机名加 domain: 前缀、
                                     其余类逐字等于现状（防「永久放行重启后失效」回归）
```

## 关键常量

放在各自模块顶部作为模块常量（不做配置项，理由见技术决策），全部带中文注释说明取值依据：

| 常量 | 值 | 依据 |
| --- | --- | --- |
| `MAX_RESPONSE_BYTES` | 5 MiB | 边读边计，超限中断连接。足够覆盖正常文档页，又不至于让一次误抓吃满内存 |
| `MAX_CONTENT_CHARS` | 100 000 | 喂给**抽取模型**的正文上限。它不进主上下文，故可远大于下一行 |
| `MAX_FALLBACK_CHARS` | 8 000 | 降级时回灌**主上下文**的原文节选上限。按 `estimate.py` 的 `CHARS_PER_TOKEN = 3.0` 折算约 2 700 token，**稳定低于 `offload.py` 的 `SINGLE_RESULT_TOKENS = 4000`**，使降级结果不会每次都触发 C8 存盘（spec F17 的相容要求） |
| `FETCH_TIMEOUT` | 30 s | 单跳的连接 + 读取总时长 |
| `EXTRACT_TIMEOUT` | 60 s | 抽取请求上限，超时即降级 |
| `MAX_REDIRECTS` | 5 | 同主机跳数上限 |

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| ②′层放哪 | `permission/network.py`，与 `blacklist.py` 同目录 | 它是决策管线的一层，不是独立子系统。放一起让「五层防御」在一个目录里读得完 |
| DNS 校验放哪 | **连接期**（`web/fetcher.py`），不放判定期 | 判定层须零 I/O（N4/N5）；且连接期每跳都过，覆盖面反而更大。详见上文「需要你确认的实现位置调整」 |
| 判定期与连接期怎么保证口径一致 | 共用同一个纯函数 `is_forbidden_address` 与同一份原因常量 | 两处各写一套是典型的「改一处漏一处」坑，且漏改**不报错**——只是某个地址悄悄能访问了 |
| url 类是否再走③层 | **不走**，②′内部已用同一个 `merged` 求过值 | 重复求值结果必然相同，留着只会让读者怀疑自己看漏了分支 |
| 域名匹配语法 | 逐条对齐 Claude Code | 用户可直接迁移既有 `settings.json` 里的规则；且其「非前导 `*` 不跨点」是有安全理由的设计，值得抄 |
| 重定向 | `follow_redirects=False`，手工逐跳 | 自动跟随不给任何回调点，连接期守卫与跨主机判断都插不进去 |
| HTML 转换 | 标准库 `HTMLParser` 手写，输出纯文本 | 不引入新依赖（`beautifulsoup4` / `markdownify` 都是新增依赖，而项目当前依赖极克制）。输出纯文本而非 Markdown：正文接下来只喂给抽取模型，Markdown 的结构信息对它价值有限，而手写 HTML→Markdown 的正确性成本远高于 HTML→文本 |
| 抽取的 provider 从哪来 | 构造期注入 `WebFetchManager`，与 C8/C9 的 manager 同构 | 项目已有明确约定：一层里只有 manager 持 provider、只有 manager 有副作用编排，其余模块保持纯逻辑可单测 |
| 抽取用哪个模型 | 缺省复用全局 provider；`web_extract_model` 非空时按 C11 Skill 换模型的既有旁路另造一个 provider | 复用既有机制，不新造第二套「怎么换模型」 |
| 阈值是否可配置 | 否，只暴露 `web_fetch_enabled` 与 `web_extract_model` | 与 C8 的既有取舍一致（那边的阈值也是模块常量）。多一个配置项就多一条要校验、要容错、要写进模板的路径，收益不明显 |
| 是否新增 trace 事件类型 | **否** | 拒绝走既有的 `permission_decision`（`layer` 字段自然带出 `network`），抽取走既有的 `api_request` + 新 scope。新增事件类型要同步改 `reader.py` 的 `SUMMARIZERS`，属于「漏改不报错」的成对维护点，能不加就不加 |
| 不可信约束进哪条通道 | 稳定可缓存通道（`cacheable=True`），第八个固定模块 | 它逐轮逐字节一致，进动态通道会白白浪费缓存 |
| 元信息与正文的位置关系 | 元信息在 `<untrusted-content>` **外**，正文在**内** | 元信息可信、正文不可信，混在一起模型无从区分 |

## 对 CLAUDE.md 的登记项

实现完成后要往 `CLAUDE.md` 补的内容（在 task.md 里作为独立任务）：

**架构分层速查表**——`Permission` 那一行的 ⚠ 列补一句：②′网络边界层**必须排在③规则之前**，
晚于③会让白名单语义整个失效（`allow` 规则会先在③层放行掉本该被白名单拦住的域名）。

**成对维护点**——新增三条：

- 新增禁止的地址范围 → `permission/network.py` 的判断函数（判定期与连接期共用，改一处即可，但**别在 `web/fetcher.py` 里另写一份**）
- 新增 `Layer` 枚举值 → `permission/models.py`（枚举）+ `trace/reader.py` 的 `_LAYER_NAMES`（**漏了不报错**，只会在阅读器里显示英文原名）
- `tools/web_fetch.py` 是 `tools ↔ web` 包级互依的第三个依赖方 → `rhinecode/tools/__init__.py` 必须继续保持为空
- 新增一种 `kind` → `permission/adapter.py` 的 `_TOOL_MAP`（映射）+ **同文件的 `to_allow_rule`**（「本会话/永久放行」要登记成什么规则）。**漏改后者不报错**：本次调用照常放行，要到下次启动才发现那条永久规则是废的

**安全边界**——把 spec「安全边界」一节的三条压缩成条目并入。
