# 网络访问工具（web_fetch）Plan

> 状态：待批准（2026-07-29，**第 2 轮修订**：已按独立审查的阻塞级与重要问题修订）
>
> 上游：[`spec.md`](spec.md)（已批准）。本文档与语言相关，按项目技术栈（Python 3.11+ / httpx / Textual）编写。

## 架构概览

改动落在**四处已有的层**加**一个新的叶子包**：

| 位置 | 性质 | 做什么 |
| --- | --- | --- |
| `rhinecode/permission/` | 既有层，加一个模块 + 改五个 | ②′网络边界层的**判定逻辑**：硬校验、域名规则求值、白名单语义、模式例外 |
| `rhinecode/web/` | **新增叶子包** | 真实抓取、逐跳硬校验、HTML 解码与转换、抽取编排、结果渲染 |
| `rhinecode/tools/web_fetch.py` | 既有层，加一个文件 | `Tool` 实现 |
| `rhinecode/agent/prompt/` | 既有层，加一段文案 | 「外部不可信内容」的系统约束（F21） |
| `rhinecode/conversation.py` | 既有层，改两处 | 放行规则构造（F9）+ 加载警告并入启动提示（F25） |

一句话概括分工：**`permission` 决定「能不能去」，`web` 负责「去了之后怎么办」。**
两者唯一的耦合点是一组纯函数——硬校验（`check_hard` / `is_forbidden_address`），
判定期与连接期共用它，保证两处口径不会漂移（spec F5a 的硬要求）。

### 依赖方向

```
tools/web_fetch.py  ──→  web/  ──→  permission/network.py  ──→  permission/matching.py
                          │
                          └──→  provider/base.py
```

`tools → web → permission → tools.path_guard` 在**包级别**看是一个环。它不成环，靠的仍是
`rhinecode/tools/__init__.py` **不 re-export 任何子模块**（该文件有包级 docstring，
但没有一行 `from . import ...`）——导入 `tools.path_guard` 不会连带执行 `tools/web_fetch.py`。
这与既有的 `tools ↔ skills`、`tools ↔ mcp` 是同一个机制、同一条戒律。

`web` 是叶子包：不被 `permission` / `agent` / `commands` / `context` / `memory` / `skills` 反向依赖。
**`web/__init__.py` 只 re-export 值对象与常量，不 re-export `WebFetchManager`**——
后者会连带拉起 provider SDK，让一句 `import rhinecode.web` 产生意外的重量级导入。

## 三处对 spec 的实现层细化

### 一、硬校验的两个执行时机（spec F5a）

spec 已定「判定期 + 连接期」两个时机、共用同一份实现。plan 明确各自的落点：

| 时机 | 位置 | 做什么 | 为什么在这 |
| --- | --- | --- | --- |
| 判定期 | `permission/network.py`，被 `engine.decide` 调用 | `check_hard(url)`：协议、内嵌凭据、IP 字面量范围 | 判定层必须零 I/O（N4）。这三项都是纯字符串与 `ipaddress` 运算 |
| 连接期 | `web/fetcher.py`，每跳建立连接前 | `check_hard(该跳 url)` **再跑一次** + 对域名解析结果逐个跑 `is_forbidden_address` | 域名解析是 I/O，不能进判定层；且同主机重定向会产生新地址，只在判定期查一次会让协议与凭据限制对新地址失效（spec F5a） |

**关于 DNS rebinding，plan 不声称能缩小时间窗。** 连接期解析并校验之后，httpx 拿到的仍是
**主机名**，它会独立地再解析一次——两次解析之间 DNS 记录可以改变。因此这个设计与
「判定期解析」在 rebinding 面前是**等价的**，都挡不住。选连接期的理由只有两条，
都与 rebinding 无关：**判定层零 I/O**（N4/N5），以及**每一跳都要过**（F5a/F8）。
spec 的「安全边界」已把 rebinding 列为已知边界，plan 不再声称额外收益。

**连带后果**：域名解析结果被拒时，权限判定已经是 ALLOW，工具执行**失败**而不是权限**拒绝**。
这一类不产生 `permission_decision` 记录，且回灌模型的文案必须自成一类（spec F24 第三类）。

### 二、「同主机」的判据（spec F8）

`(scheme, host, port)` 三元组全部相等才算同主机。端口按协议取默认值归一化
（`http`→80、`https`→443），使 `https://a.com` 与 `https://a.com:443` 判为同主机。

域名规则本身只匹配主机名（F10），此处更严是刻意的——重定向的自动跟随发生在权限判定**之后**，
没有第二次人工复核的机会。

### 三、权限规则加载警告的展示路径（spec F25）

**不新建机制。** 项目已有一条现成通道：`ConversationManager.startup_notice`
（`conversation.py:300`）由 `tui/app.py:215-216` 在界面挂载时写进聊天区。
本扩展把权限规则的加载警告并入这个字段即可。

之所以不做成 `/perm` 报告：那要同步 `commands/models.py` 的 `ReportTarget` +
`tui/app.py` 的 `query_report` 分支 + `conversation.py` 的领域方法**三处**
（CLAUDE.md 的成对维护点里有这条），而 `startup_notice` 是既有字段、零新增维护点，
且**不需要用户主动敲命令就能看见**——更符合 AC33 的要求。

## 核心数据结构

### 权限层

```python
# permission/models.py —— 三处扩充

class Layer(str, Enum):
    BLACKLIST = "blacklist"
    SANDBOX = "sandbox"
    NETWORK = "network"      # 新增：②′网络边界层
    RULE = "rule"
    MODE = "mode"

@dataclass
class PermissionRequest:
    ...
    kind: str        # 新增取值 "url"；specifier = 完整 URL 原文
    host: str = ""   # 新增字段：url 类请求的主机名（已归一化：小写、去末尾点）
```

**为什么加 `host` 字段而不是让规则层自己从 URL 里解析：** 三处都要主机名——
规则匹配、确认面板展示（F9）、行为记录（F23）。若让规则层去调 `network.py` 的解析函数，
而 `network.py` 又要 import `rules.py` 拿 `RuleSet`，就成了**真的循环导入**。
由 `adapter` 在规范化时一次填好，三处直接取用，环不存在，也不必解析三遍。

```python
# permission/network.py —— 新增模块（判定期部分零 I/O）

# 拒绝原因常量表（中文）。判定期与连接期共用同一份，保证文案与口径不漂移。
REASON_SCHEME: str
REASON_CREDENTIALS: str
REASON_ADDRESS: str        # 带 {addr} 与 {why} 占位

def split_url(url: str) -> tuple[str, str, int]:
    """拆成 (scheme, host, port)。port 按协议取默认值。解析失败抛 ValueError。"""

def normalize_host(host: str) -> str:
    """小写 + 去末尾点。adapter 填 PermissionRequest.host 时调用。"""

def is_forbidden_address(addr: str) -> Optional[str]:
    """
    单个 IP 地址的范围判断，命中返回中文原因。

    **判据是白名单式的**：`not ip.is_global` 即拒（默认拒绝、只放行确认全局可路由的）。
    逐条列黑名单必漏——审查阶段实测：按「环回∪私有∪链路本地∪保留∪未指定」这套黑名单，
    100.64.0.1（CGNAT）、224.0.0.1（组播）、2002:7f00:1::（6to4 封环回）全部漏网。

    **封装形式先还原再复判**（`is_global` 对它们也不可靠——实测 2002:7f00:1::
    与 224.0.0.1 的 is_global 都是 True）：
      - IPv4-mapped IPv6 (::ffff:a.b.c.d) → 取内层 IPv4 复判
      - 6to4 (2002::/16)                  → 取第 2–5 字节还原 IPv4 复判
      - NAT64 (64:ff9b::/96)              → 取末 4 字节还原 IPv4 复判
      - 组播 / 保留：显式补判（is_global 不覆盖）

    非法地址串返回「无法解析」原因（fail-safe 偏严）。
    """

def check_hard(url: str) -> Optional[str]:
    """
    结构性硬校验，命中返回中文拒绝原因。
    顺序：可解析 → scheme ∈ {http,https} → 无内嵌凭据 → host 非保留名字（localhost 等）
    → host 若是 IP 字面量则过 is_forbidden_address。
    **不做 DNS 解析**（判定层零 I/O）。判定期与连接期都调它。
    """

def decide(request: PermissionRequest, merged: RuleSet,
           policy_ruleset: RuleSet) -> Optional[DecisionResult]:
    """
    ②′层的完整判定。返回 None 表示本层不下结论。

    :param merged: turn + session + file 全量规则，用于 deny/allow 命中判断
    :param policy_ruleset: **仅** user + project 两层的规则，用于「白名单是否已建立」
                           （spec F6a：只有手写进这两层的 allow 才算策略声明）
    """
```

```python
# permission/rules.py

def has_allow_for(self, rule_name: str) -> bool:
    """是否存在针对该工具的 allow 规则。用于判断白名单是否已建立。"""
```

```python
# permission/matching.py

def match_domain(pattern: str, host: str) -> bool:
    """域名模式匹配（spec F11）。pattern 是去掉 "domain:" 前缀后的部分。"""
```

**`PermissionEngine` 需多持一个字段**：`policy_ruleset`（仅 user + project 两层）。
`config.load_all` 相应返回分层结果，而不是只返回一个合并后的 `RuleSet`。

### web 包

```python
# web/models.py —— 值对象，全部 frozen dataclass

@dataclass(frozen=True)
class FetchOutcome:
    ok: bool
    source_url: str              # 模型请求的原始地址
    final_url: str               # 跟随同主机重定向后的最终地址
    content_type: str
    charset: str                 # 实际用于解码的字符集（F14a，便于排查乱码）
    text: str                    # 已解码 + 已本地转换的正文
    bytes_truncated: bool        # 因响应体字节超限而截断（内容真缺了一段）
    redirect_to: Optional[str]   # 跨主机重定向目标；非空表示未抓取
    error: str

@dataclass(frozen=True)
class ExtractOutcome:
    ok: bool                     # False 表示走降级路径
    text: str
    chars_truncated: bool        # 抽取答案或降级节选因字符上限被截断
    degraded_reason: str
```

**两个截断标志分开**（spec F19）：`bytes_truncated` 是「内容真缺了一段」，
`chars_truncated` 是「进上下文的那份被切」，语义不同，混成一个会让 F19 的报告含混。

## 模块设计

### `permission/network.py` — ②′网络边界层

**职责：** 对 `kind == "url"` 的请求下结论；并向 `web/fetcher.py` 提供连接期复用的硬校验函数。

**依赖：** `permission.models`、`permission.rules`、`permission.matching`、
标准库 `urllib.parse` 与 `ipaddress`。**不依赖 `web`**。

**为什么放在 `permission/` 而不是新开一个包：** 它是决策管线的一层，和 `blacklist.py`（①层）
性质完全一致。放进 `permission/` 使「五层防御」在一个目录里读得完。

### `permission/engine.py` — 管线插入点

```python
def decide(self, request):
    # 规则合并提到最前面：②′与③层用同一个对象，避免合并两次
    merged = RuleSet(self.turn_rules + self.session_rules + self.file_ruleset.rules)

    # ① 黑名单（kind == "command"）        —— 不变
    # ② 沙箱（read_path / write_path / glob）—— 不变

    # ②′ 网络边界（kind == "url"）—— 新增
    if request.kind == "url":
        verdict = network.decide(request, merged, self.policy_ruleset)
        if verdict is not None:
            return verdict
        # 不下结论 → 直接进④。**刻意跳过③**：②′ 内部已用同一个 merged 求过一次值。
    else:
        # ③ 规则 —— 不变
        hit = merged.evaluate(request)
        if hit is not None:
            return hit

    # 只读简化分支 —— 不变（web_fetch 非只读，走不到）

    # ④ 模式兜底 —— 新增一处按 kind 的例外
    if request.mode is PermissionMode.STRICT:
        return DENY
    if request.mode is PermissionMode.PERMISSIVE:
        if request.kind == "url":
            return ASK   # ← spec F7 的例外
        return ALLOW
    return ASK
```

**⚠ 不变量（要写进代码注释与 CLAUDE.md）：②′必须排在③之前。**

**理由是「硬校验必须先于任何 allow 规则」**——若②′晚于③，一条
`allow: WebFetch(domain:*)` 会在③层先行放行，`file://` 与 `127.0.0.1` 就**整个跳过了硬校验**。

（注：白名单语义本身**不**依赖这个顺序——能在③命中 allow 的域名本来就在白名单内，
两种顺序结论相同。第 1 轮 plan 把理由写成「allow 规则会先在③层放行掉本该被白名单拦住的域名」
是错的，独立审查指出后已改正。这条记在这里，是因为对应的护栏测试形态也随之变了：
护栏必须用「全域名 allow + 禁止地址」构造，用「白名单未命中」构造发现不了顺序错误。）

### `permission/adapter.py`

- `_TOOL_MAP` 加一行：
  `"web_fetch": lambda a: ("WebFetch", str(a.get("url") or ""), "url")`。
- `to_request` 对 url 类额外填 `host`（调 `network.split_url` + `normalize_host`，失败留空串）。
- 新增 `to_allow_rule(request) -> tuple[str, str]`：返回「本会话/永久放行」要登记的
  `(rule_name, pattern)`。url 类返回 `(rule_name, f"domain:{request.host}")`
  ——**只取主机名，不带路径、查询参数与端口**（spec F9）；其余 kind 逐字等于现状。

### `permission/rules.py` / `matching.py` / `config.py`

- **rules**：`_rule_matches` 加 `url` 分支——工具名精确匹配；`pattern == ""` 视为整工具命中；
  `pattern` 以 `domain:` 开头则取后半段与 `request.host` 交给 `match_domain`；其余写法不命中。
  新增 `RuleSet.has_allow_for`。
- **matching**：新增 `match_domain`。
- **config**：三件事——
  1. `parse_rule_string` 对 `WebFetch(...)` 的括号内容做 `domain:` 前缀校验。
     **写坏的 allow 丢弃、写坏的 deny 降级为 `Rule(effect="deny", tool="WebFetch", pattern="")`**
     （spec F13，两支都偏严）。
  2. `_load_layer` 的返回从「单个错误」改为「错误列表」（当前是 `tuple[list[Rule], Optional[str]]`
     且出错即整层降级 return；要收集多条警告必须改返回类型与控制流）。
  3. `load_all` 除返回合并 `RuleSet` 外，**另返回一个仅含 user + project 两层的 `RuleSet`**
     供白名单判据使用。

  **⚠ `parse_rule_string` 有三个调用方**，改签名要一并检查：`config._load_layer`、
  `engine.persist_local_rule`（`engine.py:222`）、`skills/validation.grants_for`
  （`validation.py:125`）。

### `skills/validation.py`

`_TOOL_ALIASES` 加一行 `"web_fetch": "WebFetch"`（并加标准词汇 `"webfetch": "WebFetch"`），
否则 Skill 声明 `allowed-tools: WebFetch(...)` 会走进「本系统没有对应的工具类别」警告被忽略
（spec F26）。这条是 CLAUDE.md 成对维护点明文要求的。

按 spec F6a，该预授权进 `turn_rules`，**不进 `policy_ruleset`**，因此只放行、不建立白名单——
「`allowed-tools` 只放宽从不收紧」这条既有承诺自动成立。

### `conversation.py` — 两处改动

**① 放行规则构造（F9）。** 现在的写法（`conversation.py:1003-1016`）：

```python
req = to_request(tool, tool_call.arguments, self._engine.mode)
rule_string = f"{req.rule_name}({req.specifier})" if req.specifier else req.rule_name
```

对 url 类，`specifier` 是完整 URL，于是「永久放行」会写下
`WebFetch(https://example.com/a?token=abc)`：① 不是合法域名规则，下次启动被 F13 处理掉，
用户点过的「永久放行」**重启后凭空失效**；② 匹配不上任何东西；③ **查询参数里的 token
被原样写进配置文件**。而这个失效**当场看不出来**——本次调用照常放行了。

改法：两处（会话级 `Rule(...)`、永久级 `rule_string`）以及**永久写入失败时的回退分支**
（`conversation.py:1014`，容易漏）统一改用 `adapter.to_allow_rule(req)`。
**共三处，不是两处。**

**② 加载警告并入启动提示（F25）。** `startup_notice`（`conversation.py:300`）
当前只承载记忆系统的提示，改为「记忆提示 + 权限规则加载警告」拼接。
`tui/app.py:215-216` 那侧不动。

### `web/fetcher.py` — 抓取 + 逐跳硬校验

**对外接口：** `fetch(url, *, client_factory=None, resolver=None) -> FetchOutcome`

**两个可注入参数是 spec N5 的硬要求**，不是可选的灵活性：`client_factory` 换掉 httpx、
`resolver` 换掉域名解析，使全部测试（含端到端场景）离线跑。它们要一路透传到
`WebFetchManager` → `WebFetchTool` → `bootstrap.build_app`，形态与既有的 `provider_factory`
完全一致。

**逐跳循环：**
1. 对当前跳地址跑**完整** `check_hard`（协议、凭据、IP 字面量）——不只第一跳。
2. `resolver` 解析主机名，对每个解析结果跑 `is_forbidden_address`，任一命中即整次失败。
3. `client.stream("GET", url, follow_redirects=False, timeout=FETCH_TIMEOUT)`，
   **先看响应头**：`Content-Type` 表明是二进制则不读正文，只回报类型与体量
   （避免先下载 5 MiB 再丢弃）；否则边读边累计字节，超 `MAX_RESPONSE_BYTES` 中断并置
   `bytes_truncated`。
4. 3xx：解析 `Location`（相对地址基于当前地址补全），按 `(scheme, host, port)` 三元组
   判同主机。同主机 → 跳数 +1，超 `MAX_REDIRECTS` 即失败，否则回到第 1 步；
   跨主机 → 返回 `redirect_to` 非空的 `FetchOutcome`。
5. 解码（`web/decode.py`）+ 转换（`web/convert.py`）。
6. 所有异常转成 `ok=False` 的 `FetchOutcome`，**不外抛**。

**为什么关掉 httpx 的自动重定向：** `follow_redirects=True` 在库内部走完整条跳转链，
**不给任何回调点**——逐跳硬校验与跨主机判断都插不进去。

### `web/decode.py` — 字节到文本（F14a）

**对外接口：** `decode(raw: bytes, content_type_header: str) -> tuple[str, str]`
返回 `(文本, 实际使用的字符集)`。

推断链：HTTP 响应头的 `charset` → 文档内声明（HTML 的 `<meta charset>` /
`<meta http-equiv>`，从前 2 KB 字节里正则找）→ 兜底 `utf-8` 且 `errors="replace"`。
声明的字符集不被 Python 认识时回退兜底而非抛异常。

单独成模块而不是塞进 `convert.py`：它处理的是**字节**，`convert` 处理的是**文本**，
职责与可测输入类型都不同。

### `web/convert.py` — HTML 转文本

`html_to_text(html) -> str` 用标准库 `html.parser.HTMLParser` 手写：
跳过 `<script>` / `<style>` / `<noscript>` 内容；块级标签产出换行；压缩连续空白。
`truncate(text, limit) -> tuple[str, bool]`。

不引入 `beautifulsoup4` / `markdownify`：项目当前依赖极克制。输出纯文本而非 Markdown——
正文接下来只喂给抽取模型，手写 HTML→Markdown 的正确性成本远高于收益。

### `web/extract.py` — 抽取的纯逻辑部分

**不持有 provider、不发请求**（与 `context/summarize.py` 对 `context/manager.py` 的分工同构）。

- `content_budget(context_window: int) -> int` —— **喂给抽取模型的正文上限，按窗口比例算再夹上限**
  （spec F17）。口径对齐 `context/summarize.py` 的 `retain_budget`。
  **不得写成固定常量**：CLAUDE.md 已知项 #8 记录过同型缺陷（`RETAIN_TOKENS` 固定值
  在小窗口下失效导致机制空转）。
- `build_extract_request(page_text, source_url, ask) -> tuple[str, list[Message]]` ——
  system 含 F22 要求的不可信声明。
- `parse_extract_result(text) -> ExtractOutcome` —— 空白判为降级；
  **超 `MAX_ANSWER_CHARS` 时截断并置 `chars_truncated`**（spec F17 第三行：
  被注入的页面可诱导抽取模型输出上万字，绕过其余全部体量约束）。

### `web/render.py` — 结果渲染

`render(fetch, extract) -> str`，形状：

```
[web_fetch] 来源：https://example.com/a  最终地址：https://example.com/a
内容类型：text/html; charset=utf-8 · 响应截断：否 · 正文截断：否 · 抽取：成功

<untrusted-content source="https://example.com/a">
（抽取答案或降级原文节选）
</untrusted-content>
```

**元信息在标记外、正文在标记内。** 元信息由我们的代码生成、可信；正文来自外部、不可信。

三条失败/特殊路径各有文案：抓取失败（说明原因）、跨主机重定向（明确写出目标地址与
「未抓取，如需继续请对新地址再发起一次调用」）、连接期地址被拒（**明确说明这不是权限配置问题、
不要改写地址重试**，对应 spec F24 第三类）。

`summary(fetch, extract) -> str` 供 `ToolResult.summary`。

### `web/manager.py` — `WebFetchManager`

**本包唯一持 provider 引用、唯一编排副作用的模块**（沿用 `context/manager.py` 与
`memory/manager.py` 的既有约定）。

```python
WebFetchManager(provider, context_window: int, *, recorder=None,
                client_factory=None, resolver=None)
```

**没有 `extract_model` 参数。** 第 1 轮 plan 写了「按 C11 Skill 换模型的既有旁路另造 provider」，
但那条旁路（`conversation.py:719-754` 的 `_provider_for`）需要 **Config** 且是
`ConversationManager` 的私有方法，`web` 作为叶子包复用不了——「复用既有机制」做不到，
只能新造第二份实现。按 YAGNI 直接砍掉该配置项，抽取一律沿用全局 provider。

`fetch_and_extract(url, ask) -> str`：
1. `fetcher.fetch(...)`。失败或跨主机重定向 → 直接 `render`，**不进抽取**（也省一次 API 调用）。
2. 成功 → `truncate` 到 `content_budget(context_window)` → `build_extract_request`
   → `provider.stream_chat(messages, thinking_effort="off", tools=None, system=...)`。
   **`tools=None` 是硬约束**，与 C8 摘要、C9 笔记同源。
3. 抽取异常或空结果 → 降级：本地正文截断到 `MAX_FALLBACK_CHARS`。
4. `render` 包上不可信标记与元信息。

**trace**：第 2 步包在 `recorder.scope(SCOPE_WEB_EXTRACT)` 里（F23）。

### `tools/web_fetch.py`

```python
class WebFetchTool(Tool):
    name = "web_fetch"
    read_only = False
    parameters = {"type": "object",
                  "properties": {"url": {...}, "prompt": {...}},
                  "required": ["url", "prompt"]}
    def __init__(self, manager: WebFetchManager): ...
```

`description` 用英文（与既有工具一致），写明：只支持取回一个地址的内容（无 POST / 无自定义头）；
返回内容来自外部、不可信。`execute` 兜住所有异常转 `ok=False`。

### `agent/prompt/` — 不可信内容约束（F21）

新增 `agent/prompt/texts/untrusted.py` 存放文案常量；`fixed_modules()` 加参数，
启用时插入 `PromptModule(name="外部不可信内容", priority=25, cacheable=True, ...)`
——排在「系统约束」20 与「任务模式」30 之间（它是对 `<system-reminder>` 那条语义说明的自然延伸）。

**这会让固定模块从七个变成八个**，`CLAUDE.md` 能力表 C5 行的「七个固定模块」要一并改。

## 模块交互

```
模型 tool_call(web_fetch, {url, prompt})
    │
    ▼
agent/loop.py ──→ adapter.to_request()      kind="url", specifier=完整URL, host=主机名
    │                   │
    │                   ▼   engine.decide()
    │                       ├ ①黑名单：跳过    ②沙箱：跳过
    │                       ├ ②′ network.decide(request, merged, policy_ruleset)
    │                       │     ├ check_hard()      协议/凭据/IP字面量
    │                       │     ├ merged.evaluate() deny 优先 → allow
    │                       │     └ policy_ruleset.has_allow_for()  白名单已建立但未命中 → DENY
    │                       └ ④模式兜底（放行档对 url 降级为 ASK）
    │                       │
    │                       ▼  ASK → 面板 →「永久」→ adapter.to_allow_rule() → WebFetch(domain:<host>)
    ▼
tools/web_fetch.execute() ──→ web/manager.fetch_and_extract()
    ├──→ web/fetcher.fetch()
    │       每跳： check_hard(该跳) → resolver → is_forbidden_address 逐个
    │              → httpx GET(follow_redirects=False, 先看头再读体)
    │              → 3xx: (scheme,host,port) 同主机则续跳 / 跨主机则返回不跟随
    │       → web/decode.decode()  → web/convert.html_to_text()
    ├──→ web/extract.content_budget(context_window) → build_extract_request()
    ├──→ provider.stream_chat(tools=None)   ← scope(SCOPE_WEB_EXTRACT)
    │       └ 失败/空 → 降级为原文截断
    └──→ web/render.render()   ← <untrusted-content> + 元信息
    ▼
ToolResult ──→ 回灌模型
```

### 装配顺序（`bootstrap.py`）

`WebFetchManager` 需要 provider 与 `cfg.context_window`，`WebFetchTool` 需要 manager，
因此不能放进 `ToolRegistry.default()`（同 `MCPAddServerTool`）。

插入位置：**②Provider 创建之后、`MCPAddServerTool` 注册之后、Skill 第一阶段之前**。

- **必须在 Provider 之后**——要拿到已被 `TracingProvider` 包过的那一个，否则抽取请求不进记录。
- **必须在 `exclude_tools` 摘除与 `session_start` 快照之前**——摘除要能摘到它；
  快照里的 `tool_names` 要与实际工具集一致（既有窄窗口注释的理由一字不改地适用）。

`build_app` 新增两个可选参数 `web_client_factory` / `web_resolver`，透传给 manager。
**`tests/e2e/host.py` 是 `build_app` 的第二个真实调用方**，新增参数要同步它
（CLAUDE.md 成对维护点有这条），否则驱动设施与真实启动行为分叉。

关闭时（`cfg.web_fetch_enabled is False`）：不造 manager、不注册工具，
并把开关透传给 `fixed_modules()` 与 `permission.config.load_all`（后者跳过 domain 语法校验与警告）。

## 文件组织

```
rhinecode/
├── permission/
│   ├── network.py            ← 新增：②′层判定 + 连接期复用的硬校验
│   ├── engine.py             ← 改：插入②′；④层 url 例外；merged 提前；持 policy_ruleset
│   ├── models.py             ← 改：Layer 新增 NETWORK；PermissionRequest 新增 host
│   ├── adapter.py            ← 改：_TOOL_MAP 登记；填 host；新增 to_allow_rule
│   ├── rules.py              ← 改：url 分支；has_allow_for
│   ├── matching.py           ← 改：新增 match_domain
│   └── config.py             ← 改：domain 前缀校验（deny 降级）；错误改列表；返回分层 RuleSet
├── web/                      ← 新增叶子包
│   ├── __init__.py           ← 只 re-export 值对象与常量（不含 WebFetchManager）
│   ├── models.py             ← FetchOutcome / ExtractOutcome
│   ├── decode.py             ← 字节→文本，字符集推断链
│   ├── convert.py            ← HTML→纯文本、截断
│   ├── fetcher.py            ← 抓取 + 逐跳硬校验 + 重定向 + 上限
│   ├── extract.py            ← 预算计算、抽取提示、结果解析
│   ├── render.py             ← 不可信标记 + 元信息 + 摘要
│   └── manager.py            ← WebFetchManager
├── tools/web_fetch.py        ← 新增
├── skills/validation.py      ← 改：_TOOL_ALIASES 加 WebFetch
├── conversation.py           ← 改：三处 allow 规则构造 + startup_notice 并入警告
├── agent/prompt/texts/untrusted.py  ← 新增
├── agent/prompt/modules.py   ← 改：第八个固定模块 + 开关参数
├── trace/models.py           ← 改：新增 SCOPE_WEB_EXTRACT
├── trace/reader.py           ← 改：_LAYER_NAMES 加 network
├── config.py                 ← 改：Config 新增 web_fetch_enabled
└── bootstrap.py              ← 改：装配 + 两个注入参数 + 关闭开关

tests/e2e/host.py             ← 改：build_app 新增参数要同步（第二个真实调用方）
```

## 关键常量

| 常量 | 值 | 位置 | 依据 |
| --- | --- | --- | --- |
| `MAX_RESPONSE_BYTES` | 5 MiB | `fetcher.py` | 边读边计，超限中断 |
| `content_budget(window)` | `window // 4` 夹在 `[4_000, 100_000]` 字符 | `extract.py` | **随窗口缩放**（F17）。64K 窗口取 16 384 字符；8192 窗口取 4 000（下限）——不会像固定 100 000 那样必然超窗 |
| `MAX_ANSWER_CHARS` | 8 000 | `extract.py` | 抽取答案上限。防注入诱导超长输出 |
| `MAX_FALLBACK_CHARS` | 8 000 | `extract.py` | 降级节选上限。按 `estimate.py` 的 `CHARS_PER_TOKEN = 3.0` 折算约 2 700 token，**低于 `offload.py` 的 `SINGLE_RESULT_TOKENS = 4000`**，使降级结果不必每次触发 C8 存盘 |
| `FETCH_TIMEOUT` | 30 s | `fetcher.py` | **每一跳**的连接 + 读取总时长 |
| `MAX_REDIRECTS` | 5 | `fetcher.py` | 同主机跳数上限 |

## 技术决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| ②′层放哪 | `permission/network.py`，与 `blacklist.py` 同目录 | 它是决策管线的一层，不是独立子系统 |
| ⚠ ②′与③的顺序 | ②′在前 | **硬校验必须先于任何 allow 规则**。晚于③会让 `allow: WebFetch(domain:*)` 把 `file://` 与 `127.0.0.1` 整个放过 |
| 主机名从哪来 | `PermissionRequest` 新增 `host` 字段，由 adapter 填 | 三处都要它；让 `rules` 去调 `network` 会造成真的循环导入（`network` 已依赖 `rules`） |
| 白名单的来源层 | 只认 user + project | spec F6a：手写的是策略，面板点出来的与 Skill 预授权是授权。不区分会造成「点一次永久放行就把自己锁死」，并让 `allowed-tools` 反向收紧 |
| 地址判据 | `not is_global` + 显式还原封装形式 + 补判组播/保留 | 黑名单式枚举实测漏 CGNAT / 组播 / 6to4 三类 |
| DNS 校验位置 | 连接期 | 判定层零 I/O（N4/N5）+ 每跳都过（F5a）。**不声称缩小 rebinding 窗口**——httpx 会独立再解析一次 |
| 同主机判据 | `(scheme, host, port)` 三元组 | 跟随发生在判定之后、无二次复核，判据须严于域名规则 |
| 重定向 | 手工逐跳 | 自动跟随不给回调点 |
| 二进制处理 | 先看响应头再决定读不读体 | 否则先下 5 MiB 再丢弃 |
| 抽取模型 | 一律沿用全局 provider，砍掉配置项 | 换模型的既有旁路需要 Config 且是协调层私有方法，叶子包复用不了；YAGNI |
| 抽取超时 | 不做独立超时 | `BaseProvider.stream_chat` 无 timeout 参数，加参数要改三个 Provider，超出范围。与主对话/C8/C9 同口径，已写进 spec 安全边界 |
| 加载警告展示 | 并入既有 `startup_notice` | 零新增维护点，且不需用户敲命令就能看见。做成 `/perm` 报告要同步三处 |
| HTML 转换 | 标准库 `HTMLParser` 手写，输出纯文本 | 不引入新依赖；正文只喂抽取模型，Markdown 结构价值有限 |
| 注入口 | `client_factory` / `resolver` 一路透传到 `build_app` | spec N5 的硬要求：端到端场景也要离线跑。形态同既有 `provider_factory` |
| 是否新增 trace 事件类型 | 否 | 拒绝走既有 `permission_decision`，抽取走既有 `api_request` + 新 scope |
| 不可信约束进哪条通道 | 稳定可缓存通道 | 逐轮逐字节一致 |

## 对 CLAUDE.md 的登记项

实现完成后要补（task.md 里有独立任务）：

**架构分层速查表** —— `Permission` 行 ⚠ 列补：②′网络边界层**必须排在③规则之前**，
晚于③会让 `allow: WebFetch(domain:*)` 把硬校验整个跳过。

**能力表 C5 行** —— 「七个固定模块」改为八个。

**已知后续工程项 #5** —— 「网络请求限制」子项已兑现，划掉。

**`/skills reload` 那段** —— 现文写着「外部 Skill 里出现 `WebFetch` 这类名字是正常现象」
（言下之意是认不出），WebFetch 变成真工具后这句话就错了，要改。

**成对维护点** —— 新增四条：

- 新增禁止的地址范围 → `permission/network.py` 一处即可（判定期与连接期共用），
  **别在 `web/fetcher.py` 里另写一份**
- 新增 `Layer` 枚举值 → `permission/models.py` + `trace/reader.py` 的 `_LAYER_NAMES`
  （**漏了不报错**，只会显示英文原名）
- 新增一种 `kind` → `permission/adapter.py` 的 `_TOOL_MAP` + **同文件的 `to_allow_rule`**
  （**漏改后者不报错**：本次调用照常放行，下次启动才发现那条永久规则是废的）
- `tools/web_fetch.py` 是 `tools ↔ web` 包级互依的第三个依赖方 →
  `rhinecode/tools/__init__.py` 必须继续不 re-export 任何子模块

**安全边界** —— 把 spec「安全边界」一节压缩成条目并入（含「缺省配置下白名单不存在」
与「MCP 是第二条不受约束的外泄腿」两点）。
