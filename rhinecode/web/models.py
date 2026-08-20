"""
`web` 包的值对象：抓取结果、抽取结果，以及搜索结果（web_search 扩展）。

四个都是 `frozen=True` 的 dataclass——它们在 fetcher / search_manager →
manager → render 之间单向传递，构造后不该被改写；不可变也让它们能安全地
进日志、进断言。

## 两组值对象刻意放在同一个文件里

抓取侧（`FetchOutcome` / `ExtractOutcome`）与搜索侧（`SearchResult` /
`SearchOutcome`）在**行为**上毫无关系——一个走 HTTP 抓取 + 逐跳硬校验，
一个走 API 调用（spec F11 明确不过②′层）。但它们同属「`web` 包的纯数据」，
拆成两个文件只会让调用方多记一个导入路径，而这里没有任何逻辑可以分开。

真正需要分开的是**有行为的那部分**，那已经分了：`fetcher.py` 与
`search_manager.py` 是两个模块，`render.py` 与 `search_render.py` 也是。
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FetchOutcome:
    """
    一次 HTTP 抓取的结果（尚未经过抽取）。

    :param ok: 抓取是否成功。False 时 `error` 非空、`text` 为空
    :param source_url: 模型请求的**原始**地址（不随重定向变化，用于结果展示与追溯）
    :param final_url: 跟随同主机重定向后的**最终**地址；未发生跳转时等于 source_url
    :param content_type: 响应声明的内容类型（原样保留 `Content-Type` 头）
    :param charset: **实际用于解码的**字符集。它与 content_type 里声明的可能不同
                    （声明缺失或不被识别时会回退），单独记一份是为了排查乱码时
                    能一眼看出「到底用哪个编码解的」
    :param text: 已解码 + 已本地转换的正文（HTML 已剥标签）。二进制类型时为空
    :param bytes_truncated: **响应体字节**超限被截断。语义是「内容真的缺了一段」
    :param redirect_to: 跨主机重定向的目标地址；**非空表示本次没有抓取**，
                        由模型自行决定是否对新地址再发起一次调用（spec F8）
    :param binary: 内容类型不是文本类，**正文未被读取**（spec F14）。
                   此时 text 为空，由 render 说明「只回报类型与体量」
    :param content_length: 服务器声明的响应体字节数；未声明时为 -1。
                           只在 binary=True 时有展示价值——让用户/模型知道
                           「跳过的是多大一个东西」
    :param error: ok=False 时的中文原因

    ## 两个截断标志为什么要分开

    本结构只有 `bytes_truncated`（响应体字节超限），另一个 `chars_truncated`
    在 `ExtractOutcome` 里（进上下文的那份被切）。两者语义不同：

    - `bytes_truncated=True` → 我们**没读完**这个页面，内容真的缺了一段；
    - `chars_truncated=True` → 页面读完了，只是喂给模型/回灌上下文的那份被裁短。

    混成一个字段会让结果里的「是否截断」含混，模型也无从判断要不要换个提问重试。
    """

    ok: bool
    source_url: str
    final_url: str
    content_type: str = ""
    charset: str = ""
    text: str = ""
    bytes_truncated: bool = False
    redirect_to: Optional[str] = None
    binary: bool = False
    content_length: int = -1
    error: str = ""


@dataclass(frozen=True)
class ExtractOutcome:
    """
    抽取阶段的结果。

    :param ok: 抽取是否成功。**False 表示走了降级路径**（不是「出错了没结果」）——
               此时 `text` 仍然有内容，只是原文节选而非针对提问的回答
    :param text: 抽取答案，或降级时的原文节选
    :param chars_truncated: 文本因字符上限被截断（抽取答案超长，或降级节选超长）
    :param degraded_reason: ok=False 时说明为什么降级，会如实展示给模型——
                            它需要知道自己拿到的不是回答，才可能决定换个问法重试
    """

    ok: bool
    text: str = ""
    chars_truncated: bool = False
    degraded_reason: str = ""


@dataclass(frozen=True)
class SearchResult:
    """
    一条搜索结果（web_search 扩展 spec F18）。

    :param title: 标题。**外部不可信内容**——SEO 投毒的主要落点，
                  攻击者可以让一个精心构造的标题排到前面
    :param url: 结果地址。**本工具不对它做任何可访问性判断**（spec F20）：
                不过滤、不标注「能不能抓」。真正的访问控制发生在模型拿它去调
                `web_fetch` 的那一刻，那时会完整走一遍域名策略与②′硬校验
    :param snippet: 摘要。**外部不可信内容**，同 title

    ## 为什么标题和摘要比网页正文更危险

    网页正文的注入是「藏在文章里的一段指令」，而搜索结果的标题 / 摘要
    **看起来像「系统给我的检索结果」**，不像「某个网页里的一段话」——
    模型更容易直接采信。渲染时因此一律包进不可信标记（spec F19），
    这一点不因为「结果只有几行」而放松。
    """

    title: str = ""
    url: str = ""
    snippet: str = ""


@dataclass(frozen=True)
class SearchOutcome:
    """
    一次搜索的完整结果（web_search 扩展 spec F21/F23）。

    它是 `WebSearchManager` → `WebSearchTool` → `search_render` 之间
    **唯一的传递单位**：拿到它就能渲染出给模型的正文、给人看的单行摘要，
    以及决定 `ToolResult.ok`。

    :param ok: **「有没有真的问出去并拿到答复」**，不是「本函数有没有抛异常」
    :param query: 原始查询词**原文**——不截断、不改写（spec F8 明确不做任何过滤）
    :param provider: 服务商名（如 "brave"），进结果元信息
    :param results: 结果列表；任何失败情形下为空
    :param used: 本次会话已用搜索次数（含本次）
    :param limit: 会话配额上限；**0 表示不限制**
    :param failure: 失败类别，取 `rhinecode.web.search` 的三个常量之一；
                    **成功时为空串**
    :param error: `failure` 非空时的补充说明（异常原文、HTTP 状态码等）

    ## `ok` 怎么取值（spec F21 那张表）

    | 情形 | ok | 理由 |
    | --- | --- | --- |
    | 搜到结果 | `True` | 正常 |
    | **搜到 0 条** | **`True`** | 如实回报了「这个词搜不到东西」，那是有效信息 |
    | 未配置密钥 | `False` | 这次什么都没拿到 |
    | 配额已用完 | `False` | 同上 |
    | 服务不可用 / 超时 / 解析不出来 | `False` | 同上 |

    ⚠ 这张表照抄 `web_fetch` 验收期修掉的那个真实缺陷：`ok` 若不反映
    「有没有拿到内容」，**TUI 会把一次失败显示成绿色成功**、只是正文里写着失败
    ——界面在撒谎，模型也拿不到「这次失败了」的信号。

    ## `failure` 为什么必须是显式字段

    **不能靠 `error` 的文本去判断类别。** spec F21 要求四类各有文案，
    且「是否计入配额」「ok 取什么值」都按类别分岔——用字符串匹配去分类
    是典型的「改一个字就静默失效」：文案润色一次，分类就悄悄错了，
    而没有任何东西会报错。
    """

    ok: bool
    query: str = ""
    provider: str = ""
    results: tuple = ()
    used: int = 0
    limit: int = 0
    failure: str = ""
    error: str = ""
