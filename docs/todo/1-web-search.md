# 网络搜索工具（web_search）

> 状态：待开工 · **建议走完整 `/spec`** · 预计 1–2 天
>
> 建议分支：`web-search`（从 `main` 起）
>
> ⚠️ **比 web_fetch 简单，但简单的地方和难的地方跟它完全不是同一处。**
> 权限管线已经建好了，不用重想；**难点全在「往外发的是什么」**。
>
> ⚠️ **本文写于 2026-08-10，而 C16 分类器审查是 08-14 落地的。**
> 当时的「权限管线」只有五层，现在第④层上多了一个会真的读你这次调用参数的
> 分类器模型——它对本条的影响不止是「多过一层」，见新增的第 ⑤ 节。
> 其余各节的判断至 2026-08-19 复核仍然成立。

## 背景

`web_fetch` 已于 2026-07-29 落地（`docs/extensions/web-fetch/`）。它解决了
「**上网取**」——你给一个地址，模型把那页拿回来。

但没有搜索，模型不能「**上网找**」：

```
你：帮我查一下 httpx 的超时怎么配
模型：（没有 web_search）只能凭训练时的记忆猜一个地址，猜错就抓不到；
     或者反过来问你要链接
```

这两个工具在实际使用里是**配合**的，典型链路：

```
web_search("httpx timeout configuration")
  → 1. https://www.python-httpx.org/advanced/timeouts/
    2. https://stackoverflow.com/questions/...
web_fetch("https://www.python-httpx.org/advanced/timeouts/", "超时怎么配？")
  → 那一页的正文要点
```

搜索只给**路标**，fetch 才**取货**——搜索结果里那几行标题和摘要通常不足以回答问题。

## ✅ 已经不用再想的部分

这是它比 `web_fetch` 轻的地方，**照抄即可**：

- **权限管线**：②′网络边界层已经存在（`permission/network.py`）。规则语法、
  白名单来源层、放行档例外、确认面板的 URL 展示——全部现成。
- **不可信标注**：`<untrusted-content>` 标记与系统提示里那条固定约束已经在了
  （`agent/prompt/texts/untrusted.py`），搜索结果直接复用同一套。
- **包结构**：`rhinecode/web/` 的分层（纯逻辑模块 + 唯一持 provider 的 manager）
  可以直接沿用，`tools/web_search.py` 照 `tools/web_fetch.py` 写。
- **测试与验收形态**：离线替身 + 真实模型多轮，`tests/e2e/webstub.py` 可扩展。

## ⚠️ 真正要想清楚的五件事

### ① **搜索词是一条新的外泄面** —— 这是最该先想的

`web_fetch` 往外发的只有**地址**，而且受域名白名单约束。
`web_search` 往外发的是**用户的问题本身**，发给一个第三方搜索服务商。

一个模型完全可能这样搜：

```
web_search("RhineCode PermissionEngine decide 方法 AttributeError policy_ruleset")
web_search("公司内部 xxx 系统 数据库连接超时 排查")
```

**项目名、内部系统名、代码里的标识符就这样进了搜索服务商的日志。**
`web_fetch` 的「只取不发」在这里不成立——搜索的本质就是把查询词发出去。

要决定的：
- 系统提示里要不要加一条「搜索词里不要放项目专有名词/内部标识符」？
- 那条约束是**引导**（模型可能不听）还是要在代码里做点什么？
- `docs/extensions/web-fetch/spec.md` 的「安全边界」第 1 条要不要补一段？

### ② 搜索服务商选型 —— 影响密钥、配额、可用性

| 候选 | 要点 |
|---|---|
| Google Custom Search JSON API | 官方稳定，每天 100 次免费，超出按次计费；要建 CSE 实例 |
| Bing Web Search API | 已并入 Azure，要 Azure 订阅 |
| Brave Search API | 有免费档，API 干净 |
| SerpAPI 等聚合服务 | 省事但更贵，多一层第三方 |
| DuckDuckGo 非官方接口 | **别用**——非官方、会被封、无 SLA |

选型影响后面三件事，先定它。

### ③ 密钥与配额

- 又一个 `config.yaml` 字段、又一处敏感信息（`redact_config` 要跟着加）
- **搜索通常按次计费**。一个跑飞的 Agent Loop 能在几分钟里烧掉一个月额度——
  Claude Code 对此的做法值得抄：**一个会话最多 N 次搜索，达到上限后返回一条
  「继续用已有信息」的提示而不是错误**，因为返回错误会诱导模型重试。
- 配额耗尽 / 服务不可用时怎么降级？（`web_fetch` 的抽取失败降级是个好先例）

### ④ 搜索结果的「不可信」形态与网页正文不同

网页正文的注入是「藏在文章里的一段指令」。搜索结果的注入面是**标题与摘要**——

- 攻击者可以做 SEO 投毒，让一个精心构造的标题排到前面
- 标题/摘要比正文**更容易被模型直接采信**：它看起来像「系统给我的检索结果」而不是「某个网页的内容」
- 结果里的 URL 会被模型拿去 `web_fetch`——**那时会重新过一遍完整域名策略**，
  这是好事，spec 里要把这个衔接写明

### ⑤ **它要不要进 C16 分类器** —— 2026-08-14 之后新增的一层

C16 给三类动作在执行前接了一个**独立的分类器模型**（命令 / 网络 / 队友消息），
接在权限管线**第④层**。`web_fetch` 已经在里面（`classifier_scope = "url"`），
所以 web_search 落地时**必须当场决定它算不算一类**，不能默默跳过。

要定的三件：

- **进不进，以及算哪一类。** 空着 `classifier_scope` 就是「不审」——这是个
  合法选项（`todo_write` 就刻意空着并写明了理由），但**必须写进 spec 并给出理由**，
  否则下一个人会以为是漏声明。
- **如果进，`url` 那一类装不下它。** `agent/loop.py` 的 `_review_action` 对
  `SCOPE_URL` 取的是 `args["url"]`，而搜索的参数是**查询词**不是地址；
  照抄那一支会让分类器拿到一个**空的待判内容**，然后因为「看不出有什么问题」
  而放行——`send_message` 的 `body` / `message` 那次踩的就是这个坑，
  它**完全无声**。要么开第四类（`SCOPE_SEARCH`），要么在 `url` 支里分叉，
  两条路都要同步 `classifier/prompt.py` 的待判动作段落与 `render.py` 的
  `_SCOPE_LABELS`（措辞是给用户看的）。
- **网络那一类是分类器唯一可以「放宽」的地方。** 它的④层基线是 ASK
  （放行档对网络不生效），分类器判放行会把它**覆写成 ALLOW**。
  web_search 若归进网络类，就继承了这条——**那意味着缺省配置下它可能一次面板
  都不弹**，而本文第 ① 节说的外泄面（用户的问题原文发给第三方）恰恰是最该让人
  看一眼的东西。这两件事要放在一起评审，别分开决定。

⚠ **这是一处 `CLAUDE.md` 明确登记的成对维护点**（「新增一类要经分类器审查的
动作」）：工具上声明 `classifier_scope` + `agent/loop.py` 的**两条**判定分支
（普通分支与 `system_serial` 分支）+ `classifier/prompt.py` 的待判动作段落。
**漏后两处不报错**，只表现为「声明了但从不被审查」，配置和界面上都看不出异常。

⚠ 还有一条与本条独立的：C16 的 F20 在启用时会**丢弃过宽的命令放行规则**，
但**域名规则刻意不丢**（它同时承担「建立白名单」的语义）。web_search 的规则形状
一旦定下来（见「已知的坑」里那条），要顺带回答它属于哪一边。

## 判据

- 模型能用一句话搜到相关链接，并接着 `web_fetch` 其中一条
- **搜索词的外泄面被明确承认**，且 spec 的「安全边界」有对应条目
- 搜索结果被当作不可信输入标注（与 `web_fetch` 同一套标记）
- **会话级配额生效**，达到上限时返回「继续用已有信息」而不是错误
- 密钥缺失 / 服务不可用时有可读降级，不让整个工具报废
- **搜索结果里的链接被 fetch 时仍然过完整域名策略**（衔接点，要有护栏）
- **「进不进分类器」有一个写进 spec 的明确答案**（进的话三处齐改、有护栏；
  不进的话理由写在工具的 `classifier_scope` 旁边）
- 关掉它之后，系统行为与现在**逐字一致**

## 要读的文件

- `docs/extensions/web-fetch/` 全部五份——尤其 `acceptance.md` 的
  「五次测试写错、一次实现错」那一节，那些坑同类会再踩
- `rhinecode/web/` 整个包——结构可直接复用，但**别把搜索塞进 `fetcher.py`**
  （见「已知的坑」）
- `rhinecode/permission/network.py` —— 看②′层要不要为搜索扩展（多半不用）
- `rhinecode/tools/web_fetch.py` —— 工具层的形状照它写
- `rhinecode/trace/models.py` 的 `redact_config` —— 新密钥要进掩码白名单
  （⚠ 它在 `trace` 包里不在 `config.py`，本文原先写错过一次）
- `rhinecode/classifier/` 整个包（尤其 `models.py` 的三个 scope 常量、
  `prompt.py` 的模块 docstring）+ `rhinecode/agent/loop.py` 的 `_review_action`
  —— 第 ⑤ 节要用
- `docs/c16/spec.md` —— 分类器的边界、两阶段判定、fail-closed 与熔断口径
- `CLAUDE.md` 的「安全边界」与「成对维护点」（**分类器那三条都要读**：
  新增一类审查动作 / `_review_action` 的参数名 / 剧本 Provider 要认出分类器请求）

## 已知的坑

- **别把 search 塞进 `web/fetcher.py`** —— 它不是 HTTP 抓取而是 API 调用，
  没有重定向、没有 HTML 转换、没有逐跳硬校验。硬塞进去会让 `fetcher` 变成
  「什么都干」的模块。新开 `web/search.py` + `web/search_manager.py`。
- **规则名的形状要先定**：Claude Code 的 `WebSearch` 规则**不带 specifier**
  （只能整工具 allow/deny）。要不要支持 `WebSearch(domain:...)` 来限制结果域名？
  想清楚再动——加了就是新的匹配语义，`rules.py` 又多一个分支。
- **搜索 API 的 HTTP 请求本身要不要过②′层的硬校验？** 它打的是固定的 API 端点、
  由配置指定，与「模型自己指定地址」性质不同。多半不该过（否则用户配一个内网
  搜索代理就用不了），但**这个决定要显式写进 spec**，别默默跳过。
- `redact_config` 是**白名单式逐字段取值**，新增含密字段默认不记录——
  但要确认新字段名不会意外落进已记录的字段里。
- 别忘了 `tests/e2e/host.py`——它是 `build_app` 的第二个真实调用方，
  新增注入参数要同步（`web_fetch` 那次的成对维护点里有这条）。
- **端到端剧本会被分类器吃掉轮次**（C16 之后新增的坑）：分类器与主对话共用
  `provider_factory`（刻意的——绕过注入的工厂会让测试静默连上真实网络），
  于是剧本模型必须靠 `tests/e2e/scripted.py` 的 `is_classifier_request`
  自己分辨两种请求。**C15 的并行组队剧本真的因此整个错位过**，表现是
  「一条队友消息都没发出去」，看起来像协作功能坏了。写 web_search 的剧本前
  先读那个函数上方的说明。

---

## 一键开工 Prompt

```
先检查当前分支：`git branch --show-current`。
- 如果在 `main` 上：**先起一条新分支**再动手，不要直接在 main 上改。
  `git checkout -b web-search`
- 如果已经在别的分支上：确认那是本任务的分支再继续；不是的话先问我。

给 RhineCode 加网络搜索工具 web_search，走完整 /spec 流程
（spec → plan → task → checklist），四份文档写完之后统一找一个子 agent 审查，
过了再开工。

背景与边界见 docs/todo/1-web-search.md。核心要点：

这次**比 web_fetch 简单**——②′网络边界层、不可信标注、包结构、测试形态
全部现成，照抄即可。但难点跟它完全不是同一处：

**最该先想的是「往外发的是什么」。** web_fetch 只发地址且受域名白名单约束；
web_search 发的是**用户的问题本身**，发给第三方搜索服务商——项目名、内部系统名、
代码里的标识符会直接进那家的日志。「只取不发」在这里不成立。

其余三件 web_fetch 完全不涉及的事：搜索服务商选型（影响密钥/配额/可用性）、
会话级配额（跑飞的 Agent Loop 能几分钟烧掉一个月额度；Claude Code 的做法是
达到上限返回「继续用已有信息」而不是错误，因为返回错误会诱导重试）、
以及搜索结果的注入面在**标题与摘要**里（SEO 投毒，且比正文更容易被模型直接采信）。

判据里必须有这三条：
- **搜索词的外泄面被明确承认**，spec 的「安全边界」有对应条目
- **搜索结果里的链接被 web_fetch 时仍然过完整域名策略**（衔接点，要有护栏）
- **「它进不进 C16 分类器」有一个写进 spec 的明确答案**

⚠️ 最后那条是本文档 2026-08-19 复核时补的：本 todo 写于 2026-08-10，
而 C16 分类器审查是 08-14 落地的，文中原先一个字都没提。要点——
`web_fetch` 已在分类器里（scope 是 `url`），web_search 必须当场决定算不算一类；
若归进网络类就继承了「分类器可以把④层的 ASK 覆写成 ALLOW」这条（网络是它
唯一能放宽的地方），也就是缺省下可能一次面板都不弹，这跟上面那个外泄面
要放在一起评审。若要进，`url` 那一支装不下它——`_review_action` 取的是
`args["url"]` 而搜索的参数是查询词，照抄会让分类器拿到空内容然后放行，
**完全无声**（send_message 的参数名那次踩过同一个坑）。
细节见 docs/todo/1-web-search.md 第 ⑤ 节。

先读 docs/extensions/web-fetch/ 五份文档（尤其 acceptance.md 里
「五次测试写错、一次实现错」那节，那些坑同类会再踩），
再读 rhinecode/web/ 整个包与 rhinecode/tools/web_fetch.py。

⚠️ 别把搜索塞进 web/fetcher.py——它不是 HTTP 抓取而是 API 调用，
没有重定向、没有 HTML 转换、没有逐跳硬校验。新开 web/search.py。

验收沿用 web_fetch 那次的形态：离线替身 + **真实模型每个端到端场景至少 3 轮**。

做完这条后把 docs/todo/1-web-search.md 删掉，并重排 docs/todo/ 下其余文档的序号。
```
