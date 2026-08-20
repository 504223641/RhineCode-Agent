# 网络搜索工具（web_search）Tasks

> 状态：**已批准**（2026-08-19，第 1 轮）
>
> 输入：已批准的 [`spec.md`](spec.md) + [`plan.md`](plan.md)。
>
> 共 **35 个任务**。每个任务自包含（不要求按顺序读），依赖写在各自的「依赖」行里。

## 文件清单

| 操作 | 文件 | 职责 |
|---|---|---|
| 修改 | `rhinecode/web/models.py` | 加 `SearchResult` / `SearchOutcome` 两个值对象 |
| 新建 | `rhinecode/web/search.py` | 失败类别常量、`SearchProvider`、`BRAVE`、四个纯函数 |
| 新建 | `rhinecode/web/search_render.py` | 结果渲染、四类失败文案、TUI 单行摘要 |
| 新建 | `rhinecode/web/search_manager.py` | `WebSearchManager`：配额 + HTTP（唯一副作用） |
| 新建 | `rhinecode/tools/web_search.py` | `WebSearchTool` |
| 修改 | `rhinecode/tools/display.py` | `TOOL_LABELS` 加一项 |
| 修改 | `rhinecode/permission/adapter.py` | `_TOOL_MAP` + `to_allow_rule` 各加一支 |
| 修改 | `rhinecode/permission/engine.py` | ④层放行档加 search 一格 |
| 修改 | `rhinecode/permission/config.py` | **只动 `_CONFIG_TEMPLATE` 注释文字** |
| 修改 | `rhinecode/classifier/models.py` | `SCOPE_SEARCH` + 「三类」措辞更新 |
| 修改 | `rhinecode/classifier/prompt.py` | `render_pending` 加一支 + docstring 措辞 |
| 修改 | `rhinecode/classifier/render.py` | `_SCOPE_LABELS` 加一项 + 丢弃文案泛化 |
| 修改 | `rhinecode/classifier/broad.py` | `is_broad_search_allow` / `is_broad_allow` / `why_broad` |
| 修改 | `rhinecode/classifier/__init__.py` | 导出新增的常量与函数 |
| 修改 | `rhinecode/agent/loop.py` | `_review_action` 加一支 |
| 修改 | `rhinecode/tui/widgets.py` | `_search_detail_lines` + 选项少一个 |
| 修改 | `rhinecode/conversation.py` | 属性注入 + `clear()` 复位配额 |
| 修改 | `rhinecode/config.py` | `search` 段（模板 + 字段 + 解析） |
| 修改 | `rhinecode/trace/models.py` | `redact_config` 加三项 |
| 修改 | `rhinecode/skills/validation.py` | 词汇表 + 文案 |
| 修改 | `rhinecode/skills/audit.py` | 文案 |
| 修改 | `rhinecode/bootstrap.py` | 装配、注入口、端点校验、两条启动提示、注入条件、丢规则 |
| 新建 | `tests/test_web_search.py` | 纯逻辑 |
| 新建 | `tests/test_web_search_render.py` | 渲染与四类文案 |
| 新建 | `tests/test_web_search_manager.py` | 配额（含并发）、注入替身 |
| 新建 | `tests/test_web_search_tool.py` | 参数校验、ok 标志 |
| 新建 | `tests/test_web_search_perm.py` | 五层反证、④层三档、规则形状 |
| 新建 | `tests/test_web_search_classifier.py` | 四处齐改、参数名反证、不缓存、熔断退路 |
| 新建 | `tests/test_web_search_bootstrap.py` | 开关、启动提示、丢规则闭环、系统提示注入条件 |
| 修改 | `tests/e2e/webstub.py` | 搜索替身 + 三组预置结果 |
| 修改 | `tests/e2e/host.py` | `--web-stub` 一并覆盖搜索 |
| 修改 | `CLAUDE.md` | 扩展清单、安全边界、成对维护点触发块 |
| 修改 | `docs/internals/capabilities.md` | 网络搜索小节 |
| 修改 | `docs/extensions/README.md` | 扩展索引 |
| 修改 | `.claude/skills/paired-maintenance/SKILL.md`（或其所在路径） | 新增成对维护点条目 |
| 删除 | `docs/todo/1-web-search.md` | 做完即删，其余文档重排序号 |

---

## 第一阶段：纯逻辑层（T1–T6）

### T1: 加两个值对象

**文件：** `rhinecode/web/models.py`
**依赖：** 无

**步骤：**

1. 在文件末尾加 `SearchResult`（`@dataclass(frozen=True)`）：`title` / `url` / `snippet`，
   三个都是 `str`，都给缺省空串。
2. docstring 写明 **`title` 与 `snippet` 是外部不可信内容**、SEO 投毒的主要落点；
   `url` **不做任何可访问性判断**（spec F20）。
3. 加 `SearchOutcome`（`@dataclass(frozen=True)`）：`ok: bool`、`query: str`、
   `provider: str`、`results: tuple = ()`、`used: int = 0`、`limit: int = 0`、
   `failure: str = ""`、`error: str = ""`。
4. docstring 写明 **`ok` 的语义是「有没有真的问出去并拿到答复」**，不是「有没有抛异常」，
   并抄一份 spec F21 的那张 ok 取值表（含「0 条结果也是 True」）。
5. `failure` 的 docstring 指向 `web/search.py` 的四个常量，并写明
   **它必须是显式字段、不能靠 `error` 文本判断**。

**验证：** `python -c "from rhinecode.web.models import SearchResult, SearchOutcome; print(SearchOutcome(ok=True, query='x', provider='brave'))"` 打印出实例。

---

### T2: 搜索纯逻辑（一）——常量与两个规范化函数

**文件：** `rhinecode/web/search.py`（新建）
**依赖：** 无

**步骤：**

1. 写模块 docstring：本模块**纯逻辑、零 IO、只依赖标准库**；
   **刻意不 import `permission`**（会连带拉起整个引擎，叶子性失效，spec N4）；
   **刻意不 import `httpx`**（那是 manager 的事）。
2. 定义三个失败类别常量：`FAILURE_NO_KEY = "no_key"`、`FAILURE_QUOTA = "quota"`、
   `FAILURE_SERVICE = "service"`。注释写明「空串 = 成功（含 0 条结果）」。
3. 定义条数边界常量：`MIN_COUNT = 1`、`MAX_COUNT = 10`、`DEFAULT_COUNT = 5`。
4. 实现 `normalize_count(raw, default=DEFAULT_COUNT) -> int`：
   `None` / 空 / 非数字 → 取 `default`；数字 → 夹取到 `[MIN_COUNT, MAX_COUNT]`。
   **任何情况都不抛异常**（spec F1「越界只夹取、不报错」）。
   注意 `bool` 是 `int` 的子类——`True` 应按 `default` 处理还是按 1？
   **按 `default`**：模型传布尔进来是明显的类型错误，夹成 1 会让它拿到一条结果还以为正常。
5. 实现 `check_endpoint(url) -> Optional[str]`：用 `urllib.parse.urlsplit`，
   协议不在 `("http", "https")` 内返回一句中文原因，主机名为空也返回原因，
   合法返回 `None`。

**验证：**
```
python -c "from rhinecode.web.search import normalize_count as n; print(n(None), n(0), n(99), n('3'), n(True), n('x'))"
```
期望 `5 1 10 3 5 5`。
```
python -c "from rhinecode.web.search import check_endpoint as c; print(c('https://a/b'), c('file:///etc'), c('http://a'))"
```
期望 `None <一句中文> None`。

---

### T3: 搜索纯逻辑（二）——服务商适配

**文件：** `rhinecode/web/search.py`
**依赖：** T1, T2

**步骤：**

1. 定义 `SearchProvider`（`@dataclass(frozen=True)`）：
   `name: str`、`endpoint: str`、`auth_header: str`、
   `build_params: Callable[[str, int], dict]`、
   `parse: Callable[[Any], list]`。
2. docstring 写明 **为什么是 dataclass 装两个 callable 而不是抽象基类**
   （抄 plan 的那段：只有一个子类的基类表达不出任何东西；加第二家 = 写两个函数 + 加一行表项）。
3. 实现 `_brave_params(query, count) -> dict`：返回 `{"q": query, "count": count}`。
4. 实现 `_brave_parse(payload) -> Optional[list[SearchResult]]`：
   从 `payload["web"]["results"]` 逐条取 `title` / `url` / `description`。
   **必须防御式**，且**返回值分三态**：

   | 情形 | 返回 | 语义 |
   |---|---|---|
   | 结构不认识（不是 dict、缺 `web`、`results` 不是列表…） | **`None`** | 解析不出来 → manager 判 `FAILURE_SERVICE` |
   | 结构认得、`results` 是空列表 | **`[]`** | 服务商确实没搜到 → **成功 0 条** |
   | 正常 | 结果列表 | 单条不是 dict / 缺 `url` → **跳过那一条**，绝不抛异常 |

   ⚠ **三态是必须的，不能只返回列表。** 「解析不出来」与「真的 0 条」在 HTTP 层
   看不出任何区别，而 spec F21 对两者的判定完全相反：前者是失败、**不计配额**，
   后者是成功、**计配额**。用「空列表」同时表示两件事，等于让一次解析故障
   被当成「这个词搜不到」——模型会去换关键词重试，而根因在字段名对不上。
5. 注释写明防御式的理由：**本机连不上真实服务去核对字段名**
   （web_fetch 验收记录遗留项 #3：DNS 把公网域名重写成内网地址），
   因此必须假设字段名记忆有偏差，让「解析不出来」走可读失败路径而不是让 `KeyError` 冒泡。
6. 定义 `BRAVE = SearchProvider(name="brave", endpoint="https://api.search.brave.com/res/v1/web/search", auth_header="X-Subscription-Token", build_params=_brave_params, parse=_brave_parse)`。
7. 定义 `PROVIDERS = {"brave": BRAVE}` 与 `DEFAULT_PROVIDER = "brave"`。

**验证：**
```
python -c "
from rhinecode.web.search import BRAVE
print(BRAVE.build_params('x', 3))
print(BRAVE.parse({'web':{'results':[{'title':'T','url':'https://a','description':'D'},{'no_url':1}]}}))
print(BRAVE.parse('垃圾'), BRAVE.parse({}), BRAVE.parse({'web':{'results':'不是列表'}}))
print(BRAVE.parse({'web':{'results':[]}}))
"
```
期望：参数字典正确；第一条解析出来、第二条被跳过；中间三个都返回 **None** 且不抛；
最后一个返回 **`[]`**（结构认得、确实 0 条）。

---

### T4: 纯逻辑测试

**文件：** `tests/test_web_search.py`（新建）
**依赖：** T2, T3

**步骤：** 写四组用例：

1. `normalize_count`：`None` / `0` / `99` / `"3"` / `True` / `"x"` / `-1` / `3.7` 各一条断言。
2. `check_endpoint`：`https` / `http` 通过；`file://` / `ftp://` / 空主机 / 空串 各返回非空原因。
3. `_brave_parse` 正常路径：三条结果全部解析、字段对上。
4. `_brave_parse` 防御式：**六种畸形输入各一条**——非 dict、缺 `web`、`web` 不是 dict、
   `results` 不是列表、单条不是 dict、单条缺 `url`。全部**不抛异常**。
5. **三态反证**（最重要的一条）：前四种「结构不认识」返回 `None`；
   `{'web': {'results': []}}` 返回 `[]`；两者**用 `is None` 断言而不是布尔真值**
   ——`None` 与 `[]` 都是假值，写 `assertFalse` 的话这条护栏什么也钉不住。
   后两种（单条畸形）返回的是**列表**，且畸形那条被跳过。

**验证：** `python -m unittest tests.test_web_search -v` 全绿。

---

### T5: 结果渲染

**文件：** `rhinecode/web/search_render.py`（新建）
**依赖：** T1, T2

**步骤：**

1. 模块 docstring：抄 `web/render.py` 那两条约定——**元信息在标记外、结果在标记内**
   （模型看到「结果：5 条」时必须能确定那是系统说的，不是某条搜索结果里写的）；
   四类文案分开的理由。
2. 常量：`UNTRUSTED_OPEN = '<untrusted-content source="{source}">'`、
   `UNTRUSTED_CLOSE = "</untrusted-content>"`、`_PREFIX = "[web_search]"`。
   ⚠ 标记与 `web/render.py` **必须是同一对标签**——两处形态一致，模型的认知才不会分叉。
3. `_meta_line(outcome) -> str`：`[web_search] 查询：… · 服务商：… · 结果：N 条 · 本次会话已用 x/y`。
   `limit == 0` 时那一格写「不限」。
4. `render(outcome) -> str`，五条路径：
   - `failure == FAILURE_NO_KEY`：说明未配置密钥、**这不是临时故障、不要重试**、
     请让用户在 `config.yaml` 的 `search.api_key` 填入密钥。
   - `failure == FAILURE_QUOTA`：写出已用/上限、**这不会恢复、不要重试**、
     请用已经拿到的信息继续回答。
   - `failure == FAILURE_SERVICE`：写出具体原因、**可以换个说法重试一次**、
     连续失败请告诉用户检查网络与 `search` 配置。
   - 成功但 0 条：走元信息 + 一句「这个词没搜到东西，可换个说法或换个关键词」，
     **不包不可信标记**（没有外部内容要包）。
   - 成功且有结果：元信息 + `<untrusted-content source="brave-search:<原查询词>">` 包裹的编号列表。
5. `summary(outcome) -> str`：成功 `搜索「<前若干字>」· N 条 · x/y`；
   失败 `搜索失败：<类别中文名>`。
6. ⚠ 在 `no_key` / `quota` 两条文案上方写一段注释：**它们必须明确写「不要重试」，
   而 `service` 那条必须明确写「可以重试一次」**——三条长得差不多时模型会一律重试，
   而前两类重试一万次也不会成功，只会把剩下的迭代轮次烧光。

**验证：**
```
python -c "
from rhinecode.web.models import SearchOutcome, SearchResult
from rhinecode.web import search_render as r
from rhinecode.web.search import FAILURE_NO_KEY, FAILURE_QUOTA, FAILURE_SERVICE
ok = SearchOutcome(ok=True, query='q', provider='brave', results=(SearchResult('T','https://a','D'),), used=1, limit=50)
print(r.render(ok)); print('---'); print(r.summary(ok))
for f in (FAILURE_NO_KEY, FAILURE_QUOTA, FAILURE_SERVICE):
    print('===', f); print(r.render(SearchOutcome(ok=False, query='q', provider='brave', failure=f, error='e', used=50, limit=50)))
"
```
肉眼确认四段文案各不相同，且元信息在标记之外。

---

### T6: 渲染测试

**文件：** `tests/test_web_search_render.py`（新建）
**依赖：** T5

**步骤：**

1. 成功路径：断言结果正文在 `<untrusted-content>` 与 `</untrusted-content>` **之间**，
   而元信息行（含「服务商」「结果」）在**标记之前**——用字符串下标比较位置，
   不要只断言「都出现了」。
2. 四类失败：断言 `no_key` / `quota` 两条各含「不要重试」，`service` 那条含「重试一次」；
   断言三条两两不相等。
3. 0 条结果：断言走成功路径、**不含 `<untrusted-content>`**、含「没搜到」。
4. `limit == 0`：断言配额那一格显示「不限」。
5. `summary`：四类各一条断言。

**验证：** `python -m unittest tests.test_web_search_render -v` 全绿。

---

## 第二阶段：编排层（T7–T12）

### T7: 配额三方法

**文件：** `rhinecode/web/search_manager.py`（新建）
**依赖：** T1, T2

**步骤：**

1. 写模块 docstring：本模块是 `web` 包搜索侧**唯一持有 HTTP 客户端、唯一有副作用**的地方，
   沿用 `context/manager.py` / `memory/manager.py` / `web/manager.py` 的既有分工。
2. 建 `WebSearchManager.__init__`，参数：`provider`（`SearchProvider`）、`api_key`、
   `endpoint`（空串 = 用 `provider.endpoint`）、`max_results`、`session_quota`、`timeout`、
   关键字参数 `client_factory=None`。
3. `self._used = 0`、`self._lock = threading.Lock()`。
4. 实现 `_take_quota() -> bool`：加锁；`_limit > 0 and _used >= _limit` → `False`；
   否则 `_used += 1` 并返回 `True`。
5. 实现 `_release_quota() -> None`：加锁；`_used > 0` 时 `-= 1`。
6. 实现 `reset_quota() -> None`（加锁置 0）与 `quota_state() -> tuple[int, int]`（加锁读）。
7. ⚠ 在 `_take_quota` 上方写清楚**为什么是「预留 + 失败回退」**：
   「成功后再加一」在并发下守不住上限（四个线程都读到 49、都判没超、都发请求）；
   「只预留不回退」违反 spec F13 的计数表（服务不可用那次不该计数）。
8. ⚠ 写一条**加锁不变量**注释：临界区只做纯内存读写，HTTP / 渲染 / 埋点一律在锁外；
   本类**刻意不持有任何回调**，从结构上杜绝违反（照抄 `subagents/tasks.py::TaskManager` 先例）。

**验证：**
```
python -c "
from rhinecode.web.search_manager import WebSearchManager
from rhinecode.web.search import BRAVE
m = WebSearchManager(BRAVE, 'k', '', 5, 2, 10.0)
print(m._take_quota(), m._take_quota(), m._take_quota(), m.quota_state())
m._release_quota(); print(m.quota_state()); m.reset_quota(); print(m.quota_state())
"
```
期望 `True True False (2, 2)` → `(1, 2)` → `(0, 2)`。

---

### T8: 搜索编排与 HTTP

**文件：** `rhinecode/web/search_manager.py`
**依赖：** T7, T3

**步骤：**

1. 实现 `search(query, count=None) -> SearchOutcome`，严格按 plan 的顺序：
   ① 密钥空 → `FAILURE_NO_KEY`（不发请求、不计数）；
   ② `_take_quota()` 失败 → `FAILURE_QUOTA`；
   ③ 发一次 HTTP；异常 / 非 2xx / 解析出空列表 → `_release_quota()` → `FAILURE_SERVICE`；
   ④ 成功 → `SearchOutcome(ok=True, ...)`，`used` / `limit` 取 `quota_state()`。
2. HTTP 部分：`count = normalize_count(count, self._max_results)`；
   参数 `self._provider.build_params(query, count)`；
   请求头 `{self._provider.auth_header: self._api_key, "Accept": "application/json"}`；
   **只发 GET，只带这两个头**（spec F2）。
3. 客户端：`self._client_factory` 为 `None` 时用 `httpx.Client(timeout=self._timeout)`，
   否则调注入的工厂。两条路都要用 `with`（或等价的关闭）。
4. ⚠ `except Exception` 而**不是** `except BaseException`——用户按 Esc / Ctrl-C
   要能中断一次卡住的搜索。注释写明。
5. ⚠ **`parse` 的三态必须在这里被真的用上**（T3 已按三态实现）：
   `None` → `_release_quota()` → `FAILURE_SERVICE`；
   `[]` → **成功、`ok=True`、计数不回退**（服务商确实没搜到，那是有效信息）。
   注释写明：把两者合并会让一次解析故障被当成「这个词搜不到」——
   模型去换关键词重试，而根因在字段名对不上。

**验证：**
```
python -c "
from rhinecode.web.search_manager import WebSearchManager
from rhinecode.web.search import BRAVE
class FakeResp:
    status_code=200
    def json(self): return {'web':{'results':[{'title':'T','url':'https://a','description':'D'}]}}
class FakeClient:
    def __enter__(self): return self
    def __exit__(self,*a): return False
    def get(self,*a,**k): return FakeResp()
m = WebSearchManager(BRAVE,'k','',5,50,10.0, client_factory=lambda **kw: FakeClient())
print(m.search('q'))
m2 = WebSearchManager(BRAVE,'','',5,50,10.0); print(m2.search('q').failure)
"
```
期望第一条 `ok=True` 且 `results` 有一条；第二条 `no_key`。

---

### T9: 编排层测试

**文件：** `tests/test_web_search_manager.py`（新建）
**依赖：** T8

**步骤：**

1. `no_key`：密钥为空时不调用 `client_factory`（用一个会抛异常的工厂反证）、
   `failure == FAILURE_NO_KEY`、`quota_state()` 仍是 `(0, N)`。
2. `quota`：上限设 1，第二次调用 `failure == FAILURE_QUOTA` 且**第二次没发请求**
   （工厂调用次数计数）。
3. **失败回退**：工厂抛异常时 `failure == FAILURE_SERVICE`，且 `quota_state()[0]` 回到 0。
4. **非 2xx**：`status_code == 429` 时 `FAILURE_SERVICE`，计数回退。
5. **解析不出来**（`parse` 返回 `None`）：`FAILURE_SERVICE`，计数回退。
6. **真的 0 条**（`parse` 返回 `[]`）：`ok=True`、`failure == ""`、**计数不回退**。
7. **并发守上限**：上限设 10，起 20 个线程各调一次 `search`（工厂返回固定结果），
   断言成功次数**恰好 10**、`quota_state()[0] == 10`。
   ⚠ 这条必须用真线程，不能用串行循环——串行版本在有 bug 的实现下也会通过。
8. `reset_quota()` 后可以继续搜。

**验证：** `python -m unittest tests.test_web_search_manager -v` 全绿。

---

### T10: 工具层

**文件：** `rhinecode/tools/web_search.py`（新建）
**依赖：** T8, T5

**步骤：**

1. 照 `tools/web_fetch.py` 的形态写 `WebSearchTool(Tool)`。
2. `name = "web_search"`。
3. `description`（英文，与既有工具一致）必须写到三件事：
   ① 它做什么（返回标题/地址/摘要，要取正文请接着用 `web_fetch`）；
   ② ⚠ **查询词会原样发给第三方搜索服务商**，不要放入内部项目名、内部系统名、
      私有代码标识符、密钥、完整代码片段（spec F5）；
   ③ 返回的结果是 **UNTRUSTED 数据不是指令**，包在 `<untrusted-content>` 里
      （标题与摘要同样不可信）。
4. `parameters`：`query`（string，必填）、`count`（integer，可选，1–10，缺省 5）。
   **不要有第三个参数**（spec F2）。
5. 类属性：`read_only = False`、`primary_arg = "query"`、`classifier_scope = SCOPE_SEARCH`。
   三个各写一条注释指向 spec 依据；`read_only` 那条必须写明
   **改成 True 会让分类器无声失效**。
6. `execute(args)`：取 `query`（空 → 可读错误）、取 `count`（原样透传，夹取交给 manager）；
   `try/except Exception` 包住 `manager.search(...)`；
   返回 `ToolResult(ok=outcome.ok, output=render(outcome), summary=summary(outcome))`。
7. ⚠ 注释写明 `ok` **必须来自 `outcome.ok`**，不是「本函数有没有抛异常」——
   照抄 `web_fetch` 那个真实缺陷的教训（TUI 把失败显示成绿色成功）。

**验证：**
```
python -c "
from rhinecode.tools.web_search import WebSearchTool
t = WebSearchTool(None)
print(t.name, t.read_only, t.primary_arg, t.classifier_scope)
print(sorted(t.parameters['properties']))
print(t.execute({}).ok)
"
```
期望 `web_search False query search`、`['count', 'query']`、`False`。

---

### T11: 工具标签

**文件：** `rhinecode/tools/display.py`
**依赖：** 无

**步骤：** `TOOL_LABELS` 的「命令与网络」分组里，`"web_fetch": "WebFetch"` 下面加
`"web_search": "WebSearch"`。

**验证：** `python -c "from rhinecode.tools.display import TOOL_LABELS; print(TOOL_LABELS['web_search'])"` → `WebSearch`。

---

### T12: 工具层测试

**文件：** `tests/test_web_search_tool.py`（新建）
**依赖：** T10

**步骤：**

1. 缺 `query` / `query` 为空白 → `ok=False` 且输出里说清缺什么。
2. `count` 原样透传给 manager（用替身 manager 记录入参）。
3. **ok 标志那张表**：四种 `SearchOutcome` 各一条断言
   （成功有结果 → True；成功 0 条 → True；三类 failure → False）。
4. manager 抛普通异常 → `ok=False`、不向上抛。
5. **`KeyboardInterrupt` 照常传播**（反证，抄 web_fetch 那条教训）。
6. 类属性断言：`read_only is False`、`classifier_scope == SCOPE_SEARCH`、
   `primary_arg == "query"`、`parameters` 只有 `query` / `count` 两项。

**验证：** `python -m unittest tests.test_web_search_tool -v` 全绿。

---

## 第三阶段：权限管线（T13–T16）

### T13: 权限适配层

**文件：** `rhinecode/permission/adapter.py`
**依赖：** 无

**步骤：**

1. `_TOOL_MAP` 里 `web_fetch` 那行下面加：
   `"web_search": lambda a: ("WebSearch", str(a.get("query") or ""), "search"),`
   并注释：specifier 用**完整查询词原文**——确认面板与行为记录里要留下模型实际搜了什么。
2. `to_allow_rule` 加一支（放在 `url` 那支之后）：
   `if request.kind == "search": return request.rule_name, ""`。
3. ⚠ 注释写明**为什么返回空模式**：搜索类落规则匹配的「其它类」分支，
   该分支只认 `rule.pattern == ""`；返回 `("WebSearch", <查询词>)` 会写出一条
   **永远不会命中任何东西的废规则**——与 `WebFetch(https://…?token=abc)` 同形，
   而那正是本函数被造出来的原因。
4. 更新模块 docstring 顶部那条成对维护点提醒（现在有两种 kind 需要在两处同步）。

**验证：**
```
python -c "
from rhinecode.permission.adapter import _TOOL_MAP, to_allow_rule
from rhinecode.permission.models import PermissionRequest
print(_TOOL_MAP['web_search']({'query':'abc'}))
r = PermissionRequest(tool_name='web_search', rule_name='WebSearch', specifier='abc', kind='search', is_read_only=False, mode=None, cwd=None)
print(to_allow_rule(r))
"
```
期望 `('WebSearch', 'abc', 'search')` 与 `('WebSearch', '')`。
（`PermissionRequest` 的实际字段以源码为准，构造失败时按源码调整。）

---

### T14: 第④层加一格

**文件：** `rhinecode/permission/engine.py`
**依赖：** 无

**步骤：**

1. 在 `PermissionMode.PERMISSIVE` 分支里，`if request.kind == "url":` 那一支**之后**
   加一支 `if request.kind == "search":`，返回
   `ASK @ Layer.MODE`，理由文案写：
   「放行模式对网络搜索不生效：查询词会发给第三方，交由安全审查或用户确认」。
2. ⚠ 注释写明**为什么两支分开而不是 `kind in ("url", "search")`**：
   网络类那句说的是「未建立域名白名单时仍交由用户确认」，
   而搜索类**根本没有白名单这回事**（spec F10），沿用那句话会把用户
   引去写一条不存在的规则。
3. **①②②′②″ 四层一个字都不改**——在②′那段注释末尾加一句：
   搜索类（`kind == "search"`）不进本层，理由见 spec F11。

**验证：** `python -m unittest tests.test_perm_network_layer -v` 无回归。

---

### T15: 权限配置模板加说明

**文件：** `rhinecode/permission/config.py`
**依赖：** 无

**步骤：**

1. 在 `_CONFIG_TEMPLATE` 里 `WebFetch(domain:...)` 那段之后，加一段搜索规则说明：
   ```
   # 网络搜索（web_search）的规则**只支持不带括号的整工具形式**：
   #
   # deny:
   #   - "WebSearch"          # 彻底禁止模型上网搜索
   # allow:
   #   - "WebSearch"          # 放行搜索（⚠ 启用安全审查时这条会被丢弃，见启动提示）
   #
   # ⚠ 带括号的任何写法（WebSearch(*)、WebSearch(domain:x)…）**一律不生效，
   #   而且不会有任何警告**。想彻底关掉请用 config.yaml 的 search.enabled: false。
   ```
2. **校验函数与常量一行都不改**——不新增 `WEB_SEARCH_RULE_NAME`
   （既有的 `WEB_FETCH_RULE_NAME` 只服务于域名语法校验，没有校验就没有它的用武之地，
   加一个用不到的常量只会让人以为哪里有对应逻辑）。

**验证：** `python -c "from rhinecode.permission.config import _CONFIG_TEMPLATE as t; print('WebSearch' in t, '不会有任何警告' in t)"` → `True True`。

---

### T16: 权限测试

**文件：** `tests/test_web_search_perm.py`（新建）
**依赖：** T13, T14

**步骤：**

1. **四层反证**（spec AC11）：构造 `query = "rm -rf /"` 与 `query = "../../etc/passwd"`
   两次请求，断言结论**来自第④层**（`layer is Layer.MODE`）而不是黑名单 / 沙箱 / 网络层 / 保护路径。
2. **三档模式**（AC12）：严格→DENY、默认→ASK、放行→**ASK**；
   同一放行档下一次 `write_file` 调用仍是 ALLOW（对照组）。
3. **规则形状**（AC10）：`deny: WebSearch` 命中；`allow: WebSearch` 命中；
   `allow: WebSearch(*)` 与 `deny: WebSearch(*)` **都不命中**。
4. ⚠ 加一条注释说明第 3 条最后半句是**已知边界的正面钉住**：
   spec 明确不做加载期容错，谁要补警告就会让这条红，从而被迫走一遍评审。
5. **`to_allow_rule`**：搜索类返回空模式；把它塞进 `RuleSet` 后能命中同类请求
   （反证「本会话放行」真的有效）。
6. **规则匹配层未改的反证**：直接断言 `rules.py` 的「其它类」分支对
   `WebSearch` + 空模式命中、+ 非空模式不命中。

**验证：** `python -m unittest tests.test_web_search_perm -v` 全绿。

---

## 第四阶段：分类器（T17–T22）

### T17: 新增审查范围常量

**文件：** `rhinecode/classifier/models.py`
**依赖：** 无

**步骤：**

1. 三个 `SCOPE_*` 常量后加 `SCOPE_SEARCH = "search"`，注释写明它对应
   `web_search` 工具、待判内容是**完整查询词**。
2. 把模块里「三类被审查的动作」相关措辞改成「四类」（docstring 与常量上方注释各一处）。
3. `ReviewAction` 的 `specifier` 字段 docstring 加一行：搜索类是**完整查询词**。
4. 保留并强化那条成对维护点警告（新增取值时四处齐改）。

**验证：** `python -c "from rhinecode.classifier.models import SCOPE_SEARCH; print(SCOPE_SEARCH)"` → `search`。

---

### T18: 待判动作翻译加一支

**文件：** `rhinecode/agent/loop.py`
**依赖：** T17

**步骤：**

1. `import` 处加 `SCOPE_SEARCH`。
2. `_review_action` 里加一支（放在 `SCOPE_MESSAGE` 之后、`else` 之前）：
   ```
   elif scope == SCOPE_SEARCH:
       specifier = str(args.get("query") or "")
       recipient = ""
   ```
3. 更新该方法 docstring 里那条「参数名与工具的 `parameters` 是成对维护点」的清单
   （加上 `query`）。

**验证：** `python -m compileall rhinecode/agent/loop.py` 通过；
`grep -n "SCOPE_SEARCH" rhinecode/agent/loop.py` 有两处（import 与分支）。

---

### T19: 分类器提示词加一支

**文件：** `rhinecode/classifier/prompt.py`
**依赖：** T17

**步骤：**

1. `import` 处加 `SCOPE_SEARCH`。
2. `render_pending` 加一支：
   ```
   elif action.scope == SCOPE_SEARCH:
       head = ("助手准备用下面这段文字去第三方搜索服务商检索"
               "（注意：这段文字会**原样发给那家服务商**，等同于把它公开出去）")
       body = action.specifier
   ```
3. ⚠ 注释写明括号里那半句**不是修辞**：分类器要判的核心问题是
   「这段文字发出去要不要紧」，不是「搜这个有没有用」；不点破的话它会去评价后者。
4. 更新模块 docstring 里「三类」的措辞。

**验证：**
```
python -c "
from rhinecode.classifier.prompt import render_pending
from rhinecode.classifier.models import ReviewAction, SCOPE_SEARCH
print(render_pending(ReviewAction(scope=SCOPE_SEARCH, tool_name='web_search', specifier='内部系统 xxx 报错')))
"
```
期望文本里出现「原样发给那家服务商」与完整查询词。

---

### T20: 分类器界面文案

**文件：** `rhinecode/classifier/render.py`
**依赖：** T17

**步骤：**

1. `_SCOPE_LABELS` 加 `SCOPE_SEARCH: "网络搜索"`。
2. `render_dropped_rules` 的文案**泛化**：现在写的是「过宽的**命令**放行规则」与
   「这类规则会让分类器完全看不到对应的**命令**」，加入 `WebSearch` 之后两句都不再准确。
   改成不限定类别的说法（如「过宽的放行规则」「会让分类器完全看不到对应的动作」）。
3. ⚠ 注释写明这次泛化的原因，免得日后有人以为原文更精确而改回去。

**验证：**
```
python -c "
from rhinecode.classifier.render import _SCOPE_LABELS, render_dropped_rules
from rhinecode.classifier.models import SCOPE_SEARCH
print(_SCOPE_LABELS[SCOPE_SEARCH])
print(render_dropped_rules([('WebSearch','project','它放行全部搜索')]))
"
```
期望标签为「网络搜索」，且丢弃文案里不再出现把范围限定成「命令」的措辞。

---

### T21: 宽泛规则识别加一类

**文件：** `rhinecode/classifier/broad.py` + `rhinecode/classifier/__init__.py`
**依赖：** 无

**步骤：**

1. `broad.py` 加常量 `SEARCH_RULE_NAME = "WebSearch"`。
2. 实现 `is_broad_search_allow(tool, pattern) -> bool`：
   `tool == SEARCH_RULE_NAME` 且 `pattern` 去空白后为空串 → `True`，否则 `False`。
   ⚠ **带括号的写法一律 `False`**——它本来就不命中任何调用（spec F10），
   丢弃一条本来就无效的规则只会产生一条让人困惑的启动提示。
3. 实现伞函数 `is_broad_allow(tool, pattern) -> bool`：
   `is_broad_command_allow(...) or is_broad_search_allow(...)`。
4. `why_broad` 开头改成先判 `is_broad_allow`，并在命令类分支之前加搜索类分支：
   返回「它放行全部搜索，等于对搜索这一类关掉分类器」。
5. ⚠ 在伞函数上方注释写明**为什么要伞函数**：装配层只有一个调用点，
   写成 `A(...) or B(...)` 时将来加第三类**漏加一个 `or` 不会报错**，
   只表现为某一类规则悄悄不再被丢弃。
6. `classifier/__init__.py` 导出 `SCOPE_SEARCH`、`is_broad_search_allow`、`is_broad_allow`
   （`__all__` 一并加）。

**验证：**
```
python -c "
from rhinecode.classifier.broad import is_broad_allow, is_broad_search_allow, why_broad
print(is_broad_search_allow('WebSearch',''), is_broad_search_allow('WebSearch','*'), is_broad_search_allow('WebFetch',''))
print(is_broad_allow('Bash','python *'), is_broad_allow('WebSearch',''), is_broad_allow('Bash','git *'))
print(why_broad('WebSearch',''))
"
```
期望 `True False False` / `True True False` / 一句关于搜索的说明。

---

### T22: 分类器测试

**文件：** `tests/test_web_search_classifier.py`（新建）
**依赖：** T18–T21, T10

**步骤：**

1. **参数名反证**（AC8，最重要的一条）：拿 `WebSearchTool.parameters` 里真实声明的
   参数名，构造一次 `ToolCall`，断言 `_review_action` 得到的 `specifier`
   **等于那个查询词且非空**。照抄 `tests/test_classifier_message.py::ArgumentNameTest` 的形态。
2. **四处齐改**：分别断言 `SCOPE_SEARCH` 存在、`_review_action` 认得它、
   `render_pending` 对它有专属措辞、`_SCOPE_LABELS` 有对应中文名。
   ⚠ 四条分开写，**漏改哪一处就红哪一条**。
3. **不缓存**（AC9）：`cache_key(ReviewAction(scope=SCOPE_SEARCH, ...))` 返回空串。
   加注释说明这是「确认 `cache.py` 不用改」的反证。
4. **熔断退路**（AC14）：模拟熔断后一次搜索判定，断言结论是 `ASK`（退回弹面板）
   而不是原样放行。加注释说明这是「确认 `loop.py` 熔断分支不用改」的反证。
5. **④层覆写**（AC13）：分类器 ALLOW 时 `ASK @ MODE` 被改写成 `ALLOW`；
   BLOCK 时是 `DENY` 且回灌文案是固定文案（不含分类器写的理由）。
6. **宽规则丢弃**（AC17 的单元半）：`is_broad_allow("WebSearch", "")` 为真、
   `("WebSearch", "*")` 为假。

**验证：** `python -m unittest tests.test_web_search_classifier -v` 全绿。

---

## 第五阶段：配置、装配与界面（T23–T29）

### T23: 配置新增 search 段

**文件：** `rhinecode/config.py`
**依赖：** 无

**步骤：**

1. `_CONFIG_TEMPLATE` 里 `classifier` 段之后加 `search` 段（全注释），
   逐项写明含义，并**明确告诉用户查询词会发给服务商**（spec 安全边界第 4 条）。
2. `Config` 加七个字段：`search_enabled: bool = True`、`search_provider: str = "brave"`、
   `search_api_key: str = ""`、`search_endpoint: str = ""`、`search_max_results: int = 5`、
   `search_session_quota: int = 50`、`search_timeout: float = 10.0`。
3. `load()` 里解析 `search` 段（整段可缺省）：
   - `enabled` 走 `_parse_bool`（**非法值抛错**）；
   - `provider` 取小写去空白，**不在 `PROVIDERS` 里则抛 `ValueError`**；
   - `api_key` / `endpoint` 取字符串；
   - `max_results` 走 `_parse_int`（回退 5）；
   - `session_quota` 走 `_parse_int(allow_zero=True)`（回退 50，0 = 不限）；
   - `timeout` 走 `_parse_float`（回退 10.0）。
4. ⚠ 注释写明**两种口径是刻意的**（照抄 `classifier` 段那段注释的形态）：
   `enabled` / `provider` 决定「要不要把数据发出去、发给谁」，写错是明确的配置错误；
   其余三项是调优项，写错最坏是数值不对，不该阻断启动。
5. ⚠ `provider` 的合法值来自 `rhinecode.web.search.PROVIDERS`——
   若 `config.py` 不便 import `web`（避免层级倒挂），**在本模块内写一份
   `_KNOWN_PROVIDERS = ("brave",)` 并注释它与 `web/search.py` 的 `PROVIDERS`
   是成对维护点**（加第二家时两处齐改）。采用这一种，保持 `config.py` 不依赖 `web`。

**验证：**
```
python -c "
from rhinecode.config import Config, load
import tempfile, pathlib, textwrap
p = pathlib.Path(tempfile.mkdtemp())/'c.yaml'
p.write_text(textwrap.dedent('''
protocol: deepseek
model: m
base_url: u
api_key: k
search:
  api_key: sk
  session_quota: 0
'''), encoding='utf-8')
c = load(str(p)); print(c.search_enabled, c.search_provider, c.search_api_key, c.search_session_quota, c.search_max_results)
"
```
期望 `True brave sk 0 5`。再试 `provider: unknown` 应抛 `ValueError`。

---

### T24: 行为记录掩码

**文件：** `rhinecode/trace/models.py`
**依赖：** 无

**步骤：**

1. `redact_config` 的 `fields` 元组加 `"search_enabled"`、`"search_provider"`、
   `"search_api_key"`。
2. 掩码分支从 `if name == "api_key"` 改成 `if name in ("api_key", "search_api_key")`。
3. ⚠ 注释写明 **`search_endpoint` 刻意不记录**：用户自定义端点可能把令牌写在查询串里，
   而本函数是白名单式取值——不加进去就是默认安全。

**验证：**
```
python -c "
from rhinecode.trace.models import redact_config
class C: pass
c=C(); c.api_key='real'; c.search_api_key='sk-real'; c.search_provider='brave'; c.search_enabled=True; c.search_endpoint='https://x?token=t'
d=redact_config(c); print(d['search_api_key'], d['search_provider'], 'search_endpoint' in d, 'sk-real' not in str(d))
"
```
期望掩码值、`brave`、`False`、`True`。

---

### T25: Skill 词汇表

**文件：** `rhinecode/skills/validation.py` + `rhinecode/skills/audit.py`
**依赖：** 无

**步骤：**

1. `validation.py` 的别名表加 `"websearch": "WebSearch"` 与 `"web_search": "WebSearch"`。
2. 该文件里那句「可用的类别：Read / Write / Edit / Bash / WebFetch」加上 `WebSearch`。
3. `audit.py` 里同样的类别清单文案加上 `WebSearch`。
4. ⚠ `audit.py` 那条关于「域名规则必须带 `domain:` 前缀」的建议**不要套用到 WebSearch**
   ——搜索只有整工具形式。若那段文案按工具名分支，加一支说明；否则确认它只对
   `WebFetch` 触发。

**验证：**
```
python -c "
from rhinecode.skills.validation import _TOOL_ALIASES
print(_TOOL_ALIASES.get('websearch'), _TOOL_ALIASES.get('web_search'))
"
```
（别名表的真实变量名以源码为准。）再跑 `python -m unittest tests.test_skills_validation -v` 无回归。

---

### T26: 装配层

**文件：** `rhinecode/bootstrap.py`
**依赖：** T8, T10, T21, T23

**步骤：**

1. `build_app` 新增关键字参数 `search_client_factory=None`，docstring 补一条说明。
2. 在 ④' 那一步（`web_fetch` 注册）之后加 ④''：
   `if cfg.search_enabled:` → 用 `check_endpoint` 校验端点
   （`cfg.search_endpoint` 为空时校验 `PROVIDERS[cfg.search_provider].endpoint`）；
   非空原因 → 抛 `BootstrapError`；
   端点协议不是 `https` → `manager.add_startup_notice(...)` 一条警告；
   `cfg.search_api_key` 为空 → `add_startup_notice(...)` 一条提示（说清配置项与位置）。
   ⚠ 启动提示需要协调层已存在——若 `add_startup_notice` 在第 ⑤ 步之后才可用，
   把两条提示**暂存到局部列表**，在协调层建好之后统一 `add_startup_notice`。
   实现时先确认既有 `add_startup_notice` 的可用时点，按它安排。
3. 建 `WebSearchManager(...)` 并 `tool_registry.register(WebSearchTool(manager))`。
   ⚠ 位置必须在 `exclude_tools` 摘除与 `session_start` 快照之前（与 `web_fetch` 同）。
4. 「外部不可信内容」注入条件：
   `untrusted_section=UNTRUSTED_CONTENT if cfg.web_fetch_enabled else ""`
   改成 `... if (cfg.web_fetch_enabled or cfg.search_enabled) else ""`。
   ⚠ 加注释指向 spec F19 与 AC26——**这是本扩展最容易漏改的一处**。
5. 宽泛规则丢弃循环：`is_broad_command_allow(...)` 改成 `is_broad_allow(...)`，
   并在循环外用 `cfg.search_enabled` 决定是否把搜索类纳入
   （关闭该能力时 `allow: WebSearch` **不该被丢弃**，spec F4）。
   实现方式：判据写成
   `is_broad_command_allow(t, p) or (cfg.search_enabled and is_broad_search_allow(t, p))`，
   **或**给 `is_broad_allow` 加一个 `include_search: bool` 参数。**采用后者**，
   使装配层仍只有一个调用点（伞函数存在的理由）。
6. 协调层建好后：`manager.web_search_manager = search_manager`（属性注入，
   照抄 `manager.classifier` 的先例）。

**验证：** `python -m unittest tests.test_web_bootstrap tests.test_classifier_config -v` 无回归；
`python -c "import rhinecode.bootstrap"` 通过。

---

### T27: 协调层复位配额

**文件：** `rhinecode/conversation.py`
**依赖：** T26

**步骤：**

1. 加属性 `self.web_search_manager = None`（在 `__init__` 里，与 `classifier` 同处）。
2. `clear()` 里加：`if self.web_search_manager is not None: self.web_search_manager.reset_quota()`。
3. ⚠ 注释写明 `clear()` 是**成对维护点**：它已经要复位 c8 压缩锚点与熔断、
   卸载 Skill、清空 c15 花名册，再加一件而**漏掉不报错**——
   只表现为「清空对话之后配额没回来」。

**验证：** `python -m unittest tests.test_conversation -v`（或该文件对应的测试模块）无回归。

---

### T28: 确认面板

**文件：** `rhinecode/tui/widgets.py`
**依赖：** T13

**步骤：**

1. 加 `_search_detail_lines(self, tool_call, decision) -> list[str]`：
   展示**完整查询词（不截断）**与「判定来自」那一行。
   ⚠ **必须用本模块的 `escape`，绝不用 `rich.markup.escape`**——
   查询词是自由文本，落单的 `[` 完全正常，而那会在布局阶段抛 `MarkupError`，
   **没有任何 try/except 兜得住，整个 app 退出**。注释照抄 `_url_detail_lines` 那段。
2. 调用处：`if ... kind == "url":` 之后加 `elif ... kind == "search":` 调新函数。
3. 选项构造：新增局部变量
   ```
   protected = layer_value == "protected"
   is_search = (decision is not None and getattr(decision, "kind", "") == "search")
   no_permanent = protected or is_search
   ```
   「永久放行」那一项的条件从 `protected` 改成 `no_permanent`；
   「本会话放行」的**说明文字**仍只按 `protected` 分支（搜索类用默认那句）。
4. ⚠ 注释写明**为什么是两个布尔**：两者的「本会话放行」机制不同——
   保护路径走引擎的内存豁免集合，搜索类走正常的会话级规则。
   合并成一个会让搜索类的「本会话放行」跑去登记一个保护路径豁免，
   **不报错，但那次放行不生效**。

**验证：** `python -m unittest tests.test_tui_confirm -v`（或对应模块）无回归；
新增断言在 T29 里写。

---

### T29: 装配与界面测试

**文件：** `tests/test_web_search_bootstrap.py`（新建）
**依赖：** T26, T27, T28

**步骤：**

1. **总开关**（AC4）：`search_enabled=False` 时工具不在注册中心、
   `allow: WebSearch` **不被丢弃**、无搜索相关启动提示。
2. **系统提示注入条件**（AC26，**本扩展最容易漏改的一处**）：
   四种组合各一条断言——
   `(fetch=T, search=T)` 有约束、`(T, F)` 有、`(F, T)` **有**、`(F, F)` **无**。
3. **端点校验**（AC23）：`search_endpoint="file:///etc/passwd"` → `BootstrapError`；
   `http://…` → 有警告提示且启动成功；`https://…` → 无警告。
4. **缺密钥提示**（AC33）：`search_api_key=""` 且启用时，启动提示里出现配置项名。
5. **宽规则丢弃闭环**（AC17）：
   - 分类器开 + 搜索开 → `allow: WebSearch` 被丢弃，提示里有它；
   - 分类器**关** → 不被丢弃；
   - 搜索**关** → 不被丢弃；
   - `deny: WebSearch` 在任何组合下**都不被丢弃**（AC16）。
6. **面板选项**（AC15）：构造一次 `kind="search"` 的 ASK 决定，
   断言选项里**没有** `yes_permanent`、**有** `yes_session`；
   再构造 `kind="url"` 作对照组，断言它**有** `yes_permanent`。
7. **面板不截断**（AC6）：一条 300 字的查询词，断言补充展示行里出现完整原文。
8. **markup 反证**：查询词含 `[` 与 `[/dim]` 时，把生成的行喂给
   `Content.from_markup`（或既有护栏用的同一手法）**不抛异常**。
9. **`/clear` 复位配额**（AC21）。

**验证：** `python -m unittest tests.test_web_search_bootstrap -v` 全绿。

---

## 第六阶段：端到端设施（T30–T31）

### T30: 离线搜索替身

**文件：** `tests/e2e/webstub.py`
**依赖：** T3

**步骤：**

1. 加 `make_search_client_factory(results=None)`：返回一个与 `httpx.Client` 同形的替身
   （支持 `with`、`get(url, params=..., headers=...)`，返回带 `status_code` / `json()` 的对象）。
2. 预置三组结果：
   - `DEFAULT_SEARCH_RESULTS`：三条正常结果，地址指向 `a.test` / `b.test`
     （复用既有站点，使「搜到 → fetch 到」的链路能走通）；
   - `POISONED_SEARCH_RESULTS`：**标题与摘要里各藏一段伪装成系统指令的文本**
     （AC27 用）。内容本身完全无害，只是一段字符串；
   - 一条地址指向**未在白名单内**的域名（AC28 的衔接点用）。
3. ⚠ 模块 docstring 补一段：搜索替身**不经过连接期硬校验**
   （搜索端点不过②′，spec F11），因此这里**不受** `STUB_ADDRESS 必须全局可路由`
   那条约束——但别把这句理解成「搜索没有任何边界」。

**验证：**
```
python -c "
from tests.e2e.webstub import make_search_client_factory
f = make_search_client_factory()
with f() as c:
    r = c.get('https://api.search.brave.com/x', params={'q':'a'}, headers={})
    print(r.status_code, len(r.json()['web']['results']))
"
```

---

### T31: 宿主透传

**文件：** `tests/e2e/host.py`
**依赖：** T30, T26

**步骤：**

1. `--web-stub` 分支里**一并**构造搜索替身，透传 `search_client_factory` 给 `build_app`。
2. 更新 `--web-stub` 的 `help` 文本（现在管两样）。
3. ⚠ 确认 `web_search` **不进** `EXCLUDED_TOOLS`，并在那段注释里补一句理由
   （与 `web_fetch` 同：它是要被端到端验的对象，隔离由注入替身保证）。

**验证：** `python -m unittest tests.test_e2e_host -v` 无回归。

---

## 第七阶段：文档与收尾（T32–T35）

### T32: 项目文档

**文件：** `CLAUDE.md`、`docs/internals/capabilities.md`、`docs/extensions/README.md`
**依赖：** T1–T31

**步骤：**

1. `CLAUDE.md` 的「已实现的扩展」列表加 `web_search` 一条，
   点明三件事：查询词外泄面、分类器第四类、`deny: WebSearch` 必须不带括号。
2. `CLAUDE.md`「安全边界」一节加对应条目（spec 安全边界第 1 条与第 8 条的浓缩版）。
3. `CLAUDE.md` 的 C16 那一行「给**三类**动作」改成「四类」。
4. `docs/internals/capabilities.md` 加网络搜索小节：阈值（50 次、1–10 条、超时）、
   降级路径（四类失败）、哪些 Provider 生效。
5. `docs/extensions/README.md` 索引加一行。

**验证：** `grep -c "web_search" CLAUDE.md` ≥ 1；肉眼确认 C16 那行已改成「四类」。

---

### T33: 成对维护点

**文件：** `paired-maintenance` Skill 的定义文件
**依赖：** T32

**步骤：** 新增三条：

1. **搜索类的 kind**：`adapter._TOOL_MAP` ↔ `adapter.to_allow_rule` ↔ `engine` ④层三处。
2. **面板的两个布尔**：`widgets.py` 的 `no_permanent` ↔ `protected`——
   合并会让搜索类的「本会话放行」静默不生效。
3. **`config.py` 的 `_KNOWN_PROVIDERS`** ↔ `web/search.py` 的 `PROVIDERS`。

并在既有的「新增一类分类器审查动作」条目里，把 `web_search` 作为第二个实例补进去。

**验证：** `grep -c "web_search" <Skill 文件>` ≥ 1。

---

### T34: 清理 todo

**文件：** `docs/todo/`
**依赖：** T32

**步骤：**

1. 删除 `docs/todo/1-web-search.md`。
2. 其余文档按既有约定重排序号（`2-*` → `1-*`，依此类推），
   并同步 `docs/todo/README.md` 里的清单与编号引用。
3. 全仓搜一遍是否有别处按编号引用了这些 todo（`grep -rn "docs/todo/"`），有则同步。

**验证：** `ls docs/todo/` 序号连续无缺口；`grep -rn "1-web-search" .` 无残留。

---

### T35: 全量验证

**依赖：** T1–T34

**步骤：**

1. `python -m compileall rhinecode tests`
2. `python -m unittest discover -s tests`
3. 记录用例总数与耗时；若总数变化显著，按 `CLAUDE.md`「测试」一节的要求
   **同时**更新那三个一组的数字（总数 / 百分比 / 纯逻辑用例数），别只改一个。

**验证：** 全绿，skipped 仍为 4。

---

## 执行顺序

```
T1 ─┬─ T2 ─ T3 ─ T4
    │       └────────┬─ T5 ─ T6
    │                │
    └────────────────┴─ T7 ─ T8 ─ T9
                              └─── T10 ─ T12
                                    T11（可随时并行）

T13 ─┬─ T16          （权限，可与上面并行）
T14 ─┘
T15                  （可随时并行）

T17 ─┬─ T18 ─┬─ T22  （分类器，依赖 T10 拿工具的 parameters）
     ├─ T19 ─┤
     └─ T20 ─┤
T21 ─────────┘

T23 ─┬─ T26 ─ T27 ─┬─ T29
T24  │       T28 ──┘
T25 ─┘

T30 ─ T31            （依赖 T26 的注入口）

T32 ─ T33 ─ T34 ─ T35
```

**关键路径**：`T1 → T2 → T3 → T8 → T10 → T26 → T29 → T35`。
`T11` / `T15` / `T24` / `T25` 与主线无依赖，卡住时可以先做它们。
