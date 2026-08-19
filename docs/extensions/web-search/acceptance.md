# 网络搜索工具（web_search）验收记录

> 日期：2026-08-19 · 分支 `web-search` · 对照 [`checklist.md`](checklist.md)
>
> 每条分**「机器判到了什么」**与**「据此做的判断」**两栏——沿用
> `docs/c11/acceptance/` 与 `docs/extensions/web-fetch/acceptance.md` 的既有形态。
>
> ⚠ **本轮是离线验收。** 真实模型的 7 个端到端场景**未跑**，原因与状态见第六节，
> **不要把本文当成「已真机验收」**。

## 总览

| | 数 |
| --- | --- |
| 任务 | 35（T1–T35） |
| 新增测试 | **154**（7 个文件） |
| 全量套件 | **3297 项全绿**，skipped 4，234.5 秒 |
| 变异实测 | **2 次**（配额并发、分类器参数名），两次都确认护栏有牙 |
| 开发中发现的真实缺口 | **3 个**（1 个自己撞出、1 个被既有护栏抓出、1 个全量跑撞出） |
| 「测试写错而非实现错」 | **6 次** |

新增测试的分布：

| 文件 | 项 | 管什么 |
| --- | --- | --- |
| `test_web_search.py` | 26 | 纯逻辑：条数夹取、端点校验、响应解析三态 |
| `test_web_search_render.py` | 19 | 渲染：标记内外之分、四类文案 |
| `test_web_search_manager.py` | 19 | 配额（含并发）、失败回退、注入替身 |
| `test_web_search_tool.py` | 20 | 类属性声明、参数、`ok` 标志那张表 |
| `test_web_search_perm.py` | 20 | 四层反证、④层三档、规则形状 |
| `test_web_search_classifier.py` | 21 | 四处齐改、参数名反证、不缓存、熔断退路 |
| `test_web_search_bootstrap.py` | 29 | 开关、启动提示、丢规则闭环、注入条件、面板 |

---

## 一、开发中发现的三个真实缺口

这一节排在最前，因为它是本轮最有价值的部分。

### 1. 不可信约束的注入点在 `conversation.py` 里有**三处**，我只改了 bootstrap 那处

| | |
| --- | --- |
| **怎么发现的** | 跑 `tests/test_web_bootstrap.py` 时那条**既有**护栏变红——它 `source.count("untrusted_enabled=self._config.web_fetch_enabled")` 断言等于 3，而我改完之后是 0（我改的是另一处）。 |
| **根因** | `build_default_prompt` 有三个调用点（主对话 / fork 子对话 / 无人值守轮），各写一遍判据；而 `bootstrap.py` 里那个 `untrusted_section` 是**给子 Agent 用的第四处**。名字不同、位置很远，看起来像同一件事的唯一落点。 |
| **后果（若未修）** | 「关掉 `web_fetch`、只开 `web_search`」这个**完全合理**的配置会让「外部不可信内容是数据不是指令」那条约束**凭空消失**，而搜索结果（标题与摘要，SEO 投毒的主要落点）照样进上下文。界面上、配置上都看不出来。 |
| **顺带发现的第五处** | `subagents/runner.py` 的 `network_tool_names` 只有 `{"web_fetch"}`——一个工具集里只有搜索的子 Agent 同样拿不到那条约束。 |
| **修法** | 三处判据统一抽成 `ConversationManager._untrusted_enabled()`；`network_tool_names` 补上 `web_search`；既有护栏改成数新判据，并**加一条「不许再出现旧写法」的断言**。 |

**据此做的判断**——这条印证了那份既有护栏的设计意图：它数的不是「行为对不对」而是
**「调用点齐不齐」**，而那正是行为断言抓不到的东西（每一处单独看都是对的）。
新增的第四、五处已登记进 `paired-maintenance`。

### 2. `normalize_count` 的回退值不夹取

| | |
| --- | --- |
| **怎么发现的** | T23 跑配置解析的手工验证时，`max_results: 99` 原样返回 99。 |
| **根因** | `normalize_count(raw, default)` 对 `raw` 夹取，但 `raw is None` 时**直接 return default**。而 `default` 来自 `search.max_results`，那一项走「非法值回退默认」的宽松口径，用户完全可以写 99。 |
| **后果** | 模型**不指定条数**时（最常见的情况）会把 99 原样发给服务商。一条越界值从「用户指定」这条路被挡住，却从「缺省」这条路溜了出去。 |
| **修法** | 回退值也过同一个 `_clamp`；补五条回归断言（含 `default=99` / `0` / `-5` / 非数字 default）。 |

### 3. 装配层的 `PROVIDERS[...]` 硬索引

| | |
| --- | --- |
| **怎么发现的** | 全量跑撞出 9 处 `KeyError: <MagicMock ...>`。 |
| **根因（两层）** | ① `test_skill_startup` / `test_command_startup` 的 `_fake_config()` 是 `MagicMock`，**任意属性都是真值**，于是 `cfg.search_enabled` 恒为真、装配层进了搜索分支；② 装配层用 `PROVIDERS[cfg.search_provider]` 硬索引。 |
| **后果** | 第 ② 层在生产里也不对：`config.load()` 校验过服务商名，但**那不是唯一构造路径**——直接 `Config(search_provider="x")` 会绕过它，硬索引让一个 `KeyError` 从装配层冒出来，而 `BootstrapError` 才是那里的「致命配置错误」通道。 |
| **修法** | 装配层改 `PROVIDERS.get(...)` + `BootstrapError`（补护栏）；两处 fixture 补上三个 `search` 字段。 |

**据此做的判断**——这是 C16 那段注释预言过的同一类问题（「`cfg` 在若干启动测试里是
`MagicMock`」）。**`MagicMock` 的真值性是这个仓库反复踩的坑**：任何「读一个新配置开关
然后据此做事」的改动，都要想一遍那批 fixture。

---

## 二、两次变异实测

护栏写完就断言「它有效」是不算数的。两条最关键的护栏各做了一次变异。

### 配额并发（`test_web_search_manager.py::QuotaTests::test_concurrent_take_respects_limit`）

**变异**：把 `_take_quota` 从「锁内读改一步完成」换成「先查、让出、再改」
（即那个直觉写法「成功后再加一」的等价形态）。

**机器判到了什么**：`AssertionError: 20 != 10` —— 20 个线程全部拿到名额。

**据此做的判断**——护栏有牙，且钉的正是那个形态。⚠ 用例注释里写死了
「**必须用真线程**」：串行循环在同一个错误实现下会全绿通过。

### 分类器参数名（`test_web_search_classifier.py::ArgumentNameTests`）

**变异**：把 `_review_action` 里搜索类取的 `args.get("query")` 改成 `args.get("url")`
（即照抄 `SCOPE_URL` 那一支的写法）。

**机器判到了什么**：两条断言同时红，其中一条的失败信息是 `AssertionError: '' == ''`。

**据此做的判断**——那正是 `send_message` 那次踩过的**无声形态**：分类器拿到一个空的
待判内容，然后因为「看不出有什么问题」而放行，**没有任何东西报错**。
⚠ 断言刻意**不写死 `"query"` 字面量**去比自己，而是拿工具真实声明的 `parameters`
比对——写死的话改了工具参数名它照样绿。

---

## 三、逐条验收（离线部分）

### 实现完整性

| 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- |
| AC1 工具可调用、条数夹取 | `test_web_search_tool` / `test_web_search_manager` 的 `count=0 → 1`、`count=99 → 10`、`count=None → 配置缺省` | 通过 |
| AC2 只有两个参数、多余参数无效果 | 参数表恰为 `{query, count}`；传 `headers` / `lang` / `body` 时两次调用的 `manager.calls` 与 `output` **逐字相同** | 通过 |
| AC3 非只读、进第④层 | `read_only is False`；权限用例断言结论 `layer is Layer.MODE` | 通过。**这条是硬约束**：只读工具在③层就短路放行、根本不进④，而分类器的触发条件正是「结论来自④」 |
| AC5 描述含两层外泄提醒 | 描述里同时有 `third-party` / `verbatim` / `credentials` / `internal project` | 通过。另有一条**反证**断言它**不含** `cannot be used to submit data`——`web_fetch` 那句「只取不发」在这里是错的 |
| AC5a 外泄面被明确承认 | spec 安全边界第 1 条含「挡得住 / 挡不住」表；`CLAUDE.md` 安全边界新增三条 | 通过 |

### 权限管线

| 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- |
| AC11 前四层都不生效 | 四条并列断言：`rm -rf /` / `../../etc/passwd` / `http://127.0.0.1:8080` / `.rhinecode/permissions.yaml` 作查询词时，结论均 `layer is Layer.MODE`，且 `isNot` 黑名单 / 沙箱 / 网络层 / 保护路径 | 通过。第三条最要紧：复用 `kind="url"` 会让 `check_hard` 拿查询词当地址解析、判「地址畸形」，表现是**每次搜索都被硬拒**，而那看起来像「网络边界在正常工作」 |
| AC12 三档 + 对照组 | 严格→DENY、默认→ASK、**放行→ASK**；同放行档下 `write_file` 仍 ALLOW | 通过 |
| AC12 文案不提白名单 | 放行档的 reason **不含**「白名单」、**含**「第三方」 | 通过。反证：沿用网络类那句会把用户引去写一条不存在的规则 |
| AC10 规则形状 | `deny: WebSearch` / `allow: WebSearch` 命中；**`allow: WebSearch(*)` 与 `deny: WebSearch(*)` 都不命中** | 通过 |
| AC10 带括号无警告 | `parse_rule_string("WebSearch(*)", ...)` 返回 Rule 且 `warnings == []` | 通过。**这是已知边界的正面钉住**——谁要补警告，这条会红 |
| AC10a 配置模板 | `_CONFIG_TEMPLATE` 含 `WebSearch` / 「不会有任何警告」/ `search.enabled: false`；模板仍是全注释（`yaml.safe_load` 得 `None`） | 通过 |
| AC15 面板三选项 | 真挂 Textual 面板，`_ids == ["yes","yes_session","no"]`；url 类对照组 `== [...,"yes_permanent","no"]` | 通过 |
| AC15 本会话放行有效 | `to_allow_rule` 返回空模式；塞进 `RuleSet` 后命中**另一条完全不同的**查询 | 通过 |
| AC6 完整展示 | 300 字查询词完整出现在面板补充行、无省略号 | 通过 |
| AC6 markup 稳健 | 查询词含 `list[int]` / `[/dim]` / `[bold]` 时面板正常挂出 | 通过。⚠ 用错 `escape` 会在**布局阶段**抛 `MarkupError`，没有任何 try/except 兜得住 |
| AC7 无过滤改写 | 含 `RhineCode PermissionEngine` 的查询词**原样**出现在替身收到的 `params["q"]` | 通过 |

### 分类器

| 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- |
| AC8 四处齐改 | 四条**分开**的断言：常量 / 工具声明 / 提示词专属措辞 / 界面中文名 | 通过。分开写的理由：合成一条只会告诉你「有地方不对」而不告诉你是哪里 |
| AC8 待判内容是完整查询词 | `_review_action` 得到 `specifier == "内部系统 排查"`、`recipient == ""`、`host == ""` | 通过（并经变异实测） |
| AC9 不缓存 | `cache_key(搜索类) == ""`；url 类对照组 `== "a.test:443"` | 通过。**这是「确认 `cache.py` 不用改」的反证** |
| AC14 熔断退回弹面板 | 结构断言：`_apply_classifier` 的熔断分支只特判 `SCOPE_MESSAGE`，源码中**不出现** `SCOPE_SEARCH` | 通过。**这是「确认熔断分支不用改」的反证** |
| — 规则匹配层不用改 | `_rule_matches` 对 `WebSearch` + 空模式命中、+ 任意非空模式不命中；`Web*` 通配照常 | 通过。三条「不用改」的反证齐了 |

### 宽泛规则丢弃

| 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- |
| AC17 丢弃并告知 | 分类器开时 `("allow","WebSearch","")` 不在 `file_ruleset`；启动提示含规则原文与「搜索」 | 通过 |
| AC17 闭环上半 | 分类器关 → 该规则**恢复生效** | 通过 |
| AC4 闭环下半 | 搜索关 → 该规则**不被丢弃** | 通过。**关掉的能力不该影响用户的规则文件** |
| AC16 deny 永不被丢 | 四种开关组合下 `("deny","WebSearch","")` 均在位 | 通过 |
| — 带括号不被丢 | `("allow","WebSearch","*")` 保留 | 通过。丢一条本来就无效的规则只会让人以为它原本生效过 |
| — 命令类对照组 | `Bash(python *)` 仍被丢 | 通过 |

### 配额与降级

| 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- |
| AC18 上限文案 | 上限 1 时第二次 `failure == quota`，文案含「不要重试」「用已经拿到的信息继续」 | 通过 |
| AC18 0 = 不限 | 连搜 5 次全成功，`quota_state() == (5, 0)` | 通过 |
| AC19 四种不计数 | 无密钥 / 已耗尽两种：`client_factory` **零次调用**且计数不变；服务失败：计数**回退**到调用前 | 通过 |
| AC19 0 条计数 | `parse` 返回 `[]` 时 `ok=True` 且计数 **+1** | 通过。与「解析不出来」（`None`，失败且退配额）恰好相反 |
| AC20 并发守上限 | 20 线程 / 上限 10 → 成功恰好 10、`quota_state()[0] == 10`、工厂被调 10 次 | 通过（并经变异实测） |
| AC21 `/clear` 复位 | 用掉 2 次后 `manager.clear()` → `(0, 5)`；搜索关闭时 `web_search_manager is None`，`clear()` 不炸 | 通过 |
| AC22 四类文案 | `no_key` 与 `quota` 含「不要重试」/「重试不会成功」；`service` 含「重试一次」且**不含**「不要重试」；三条两两不等 | 通过 |
| AC22 ok 标志 | 有结果→True、**0 条→True**、三类 failure→False | 通过。照抄 `web_fetch` 那个「界面把失败显示成绿色成功」的教训 |
| AC23 端点 | `file://` → `BootstrapError`（含「协议」）；`http://` → 启动警告且照常启动；`https://` → 无警告 | 通过 |
| — 未知服务商 | `Config(search_provider="google")` → `BootstrapError`，信息里同时含 `google` 与 `brave` | 通过 |

### 结果与不可信

| 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- |
| AC24 不发模型请求 | `WebSearchManager` **完全不持有 provider**（结构性成立，非断言得来） | 通过。搜索结果不经二次抽取（spec F18） |
| AC25 标记内外 | 用**字符串下标**比较：元信息位置 < 开标记位置；结果正文在开闭标记之间 | 通过。只断言「都出现了」在元信息被塞进标记里时照样通过 |
| AC25 0 条不包标记 | 输出**不含** `</untrusted-content>`、含「没搜到」 | 通过 |
| AC26 注入条件四组合 | `(开,开)` 有 / `(开,关)` 有 / **`(关,开)` 有** / `(关,关)` 无；并另有装配层接线断言 | 通过。**这是本扩展最容易漏改的一处**，见第一节缺口 1 |
| AC29 四项元信息 | 输出可读出查询词 / `brave` / `2 条` / `3/50`；`limit=0` 显示「3/不限」 | 通过 |

### 集成

| 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- |
| AC31 密钥掩码 | 快照里 `search_api_key` 为固定掩码、`search_provider` 原样、**`search_endpoint` 不在快照里**、真实密钥字符串搜不到 | 通过。端点刻意不记录——自定义端点可能内嵌令牌 |
| AC32 Skill 预授权 | `_TOOL_ALIASES` 认 `websearch` / `web_search` → `WebSearch`；`test_skill*` 268 项无回归 | 通过 |
| AC33 缺密钥提示 | 启动提示含 `search.api_key` 与 `search.enabled` | 通过 |
| AC34 替身可注入 | `build_app(search_client_factory=...)` 与 `tests/e2e/host.py --web-stub` 两个调用方都能透传；`test_e2e_host` 28 项无回归 | 通过 |
| AC4 总开关 | 关闭时工具不在注册中心、无搜索相关启动提示；`exclude_tools` 也摘得掉 | 通过 |

### 编译与测试

| 判据 | 机器判到了什么 | 判断 |
| --- | --- | --- |
| `compileall` | 无错误 | 通过 |
| 全量 | **3297 项，OK，skipped=4**，234.5 秒 | 通过 |
| 无回归 | `test_perm_*` 297 / `test_classifier_*` 135 / `test_web_*` 319 / `test_subagent_*` 352 / `test_skill*` 268 / `test_e2e_host` 28 / `test_tui*` 272 全绿 | 通过 |
| 三个一组的数字 | 按 `CLAUDE.md` 记载的量法实测：3297 / 96% / 679 重模块用例 / 2618 纯逻辑用例（9.6 秒） | 已同步。⚠ **上一组本身就对不上**：737 + 2493 = 3230 ≠ 3143；本次自洽（679 + 2618 = 3297） |

---

## 四、开发中的六次「测试写错，不是实现错」

留在这里是因为它们都指向同一类问题：**看起来更自然的写法，往往什么也钉不住，
或者干脆测的不是自己以为的东西。**

1. **权限引擎读的是 `request.mode`，不是 `engine.mode`**（两条用例）。档位只传给
   `_engine(...)` 而没传给 `_request(...)` 时，测的一直是**默认档**——「严格档应当拒绝」
   那条拿到 ASK，看起来像实现漏了那一支。已在 `_engine` 的 docstring 里写死这条。
2. **`parse_layer` 这个函数不存在**——真名是 `parse_rule_string`，签名也不同。
3. **`fixed_modules` 的参数是 `untrusted_enabled` 不是 `untrusted_section`**；
   后者是 `SubAgentRuntime` 那一侧的字段。两个名字都存在，且都与不可信内容有关。
4. **`startup_notice` 是一整段字符串，不是 `startup_notices` 列表。**
5. **面板的补充展示行是 `id=None` 的选项，不是 `Static` 组件**——
   `panel.query("Static")` 返回**空列表**，断言「渲染里含某段文字」于是恒假。
6. **用 `inspect.getsource` 断言面板选项**是第一版写法，被换掉了：`show_tool` 这个
   方法名根本不存在（真名 `show_for`），而且源码断言本来就弱。改成照
   `test_protected_wiring.py::PanelTest` 的形态**真挂一个 Textual app**。

**唯一一次「真的是实现错」是第一节那三个缺口**——其中两个（不可信注入点、
`PROVIDERS` 硬索引）**离线单元测试单独跑发现不了**，一个靠既有护栏、
一个靠全量跑才现形。这个比例本身值得记住（`web_fetch` 那轮是「五次测试写错、
一次实现错」）。

---

## 五、遗留与已知边界

以下都是**设计时就承认、未在本轮解决**的，不是漏做：

1. **`deny: WebSearch(*)` 静默无效** —— 带括号的写法一律不命中**且不产生任何警告**。
   plan 评审明确裁定不做加载期容错（`web_fetch` F13 那套）。缓解手段只有
   `permissions.yaml` 模板里那段说明。⚠ AC10 **正面断言「不产生任何警告」**，
   使日后「顺手补一个」会当场变红、被迫重新评审。
2. **三道对冲全都依赖判断，没有一道是硬边界** —— 工具描述的约束（模型可能不听）、
   分类器（会误判，官方公布拦截率 89%）、面板完整展示（用户可能不看）。
   唯二的硬边界是 `deny: WebSearch` 与 `search.enabled: false`。
3. **`run_command` 跑起来的 `curl` 与 MCP 工具不受任何约束** —— 与已知项 #4
   （OS 级沙箱）同源。`web_fetch` 的验收记录第七节有一次**未经诱导、模型自主选择**
   的同型绕过实测样本，那条对搜索同样成立。
4. **规划阶段搜不了网** —— `read_only = False` 的连带后果，与 `web_fetch` F3 同一取舍。
   改成只读会让分类器**无声失效**，这是不能接受的那一侧。
5. **本机跑不通真实搜索** —— 这台机器的 DNS 把公网域名重写成 `10.x`
   （企业代理 / TUN，见 `web_fetch` 验收记录遗留项 #3）。搜索端点虽**不过**②′的
   地址范围校验（spec F11），但 TCP 连接本身仍连不到。
6. **服务商响应的字段名未经真实核对** —— 因上一条，`_brave_parse` 的字段名
   （`web.results[].title/url/description`）是按文档记忆写的。**这正是三态设计的理由**：
   字段名若有偏差，走的是 `None` → `FAILURE_SERVICE` → 「可以重试一次 / 连续失败请检查配置」
   这条**可读的失败路径**，而不是一个 `KeyError`，也不会伪装成「这个词搜不到」。
7. **配额不持久化** —— 只在本次运行内计数，重启即清零（spec 明确不做）。

---

## 六、⚠ 未完成：真实模型端到端

**checklist 第十节的 7 个场景本轮一个都没跑。** 这不是遗漏，是当前环境办不到，
但**必须写清楚**，否则本文会被误读成「已真机验收」。

| 场景 | 需要什么 | 当前状态 |
| --- | --- | --- |
| 1 正常链路（搜→抓→答） | 有效模型凭据 + 离线替身 | **未跑** |
| 2 分类器拦下（含密钥样式查询） | 同上 | **未跑** |
| 3 配额耗尽后模型是否停手 | 同上 | **未跑**。⚠ **这条只有真实模型跑得出来**——它验的不是「配额生效了」（离线已验），而是「模型拿到那条文案后**停下来了**」 |
| 4 注入抵抗（标题与摘要藏指令） | 同上；替身已备好 `POISONED_SEARCH_RESULTS` | **未跑** |
| 5 衔接点（白名单外的结果） | 同上；替身已备好 `OFF_WHITELIST_SEARCH_RESULTS` | **未跑** |
| 6 缺密钥（调用次数恰为 1） | 同上 | **未跑** |
| 7 关掉能力 | 无需凭据 | **离线已验**（AC4） |

**离线设施已经就绪**：`tests/e2e/webstub.py` 的三组预置结果、`--web-stub` 的透传、
以及 `is_classifier_request` 那条必读说明都在位。接手的人只需要有效凭据 +
`python -m tests.e2e.host --mode live --web-stub`。

⚠ **写剧本前必读** `tests/e2e/scripted.py::is_classifier_request` 上方的说明：
分类器与主对话**共用同一个 Provider 工厂**（刻意的——绕过注入的工厂会让测试
静默连上真实网络），因此剧本模型必须自己分辨两种请求，否则分类器会吃掉主对话的
轮次。**C15 的并行组队剧本真的因此整个错位过**，表现是「一条队友消息都没发出去」，
看起来像协作功能坏了。

---

## 七、与原 todo 的四处不同

`docs/todo/1-web-search.md` 已随本条完成删除，四处差异记在
`docs/todo/README.md` 的「已完成」一节，此处只列标题：

1. **进分类器，且开的是第四类**——原 todo 第 ⑤ 节的预言对了。
2. **确认面板要去掉「永久放行」**——原 todo 没提，是 F10/F16 推导出来的必然结果。
3. **带括号的 `deny` 静默无效**，评审时明知而接受。
4. **实测撞出三个真缺口**（第一节）。
