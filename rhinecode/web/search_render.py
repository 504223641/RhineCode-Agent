"""
搜索结果渲染（web_search 扩展 spec F19/F21/F23）：纯逻辑，零 IO。

把一个 `SearchOutcome` 拼成两样东西——回灌模型的完整文本，与给人看的单行摘要。

## 元信息在标记外、结果在标记内

    [web_search] 查询：… · 服务商：brave · 结果：5 条 · 本次会话已用 3/50
    <untrusted-content source="brave-search:…">
    1. 标题
       地址
       摘要
    </untrusted-content>

这个位置关系是有意的，与 `web/render.py` 同一条理由：**元信息由我们的代码生成、
可信；结果来自外部、不可信。** 模型看到「结果：5 条」时，必须能确定那是系统说的，
而不是某条搜索结果的标题里写的。

## 注入面在标题与摘要里

网页正文的注入是「藏在文章里的一段指令」；搜索结果的注入面是**标题与摘要**——
攻击者做一次 SEO 投毒，就能让一个精心构造的标题排到前面。而标题 / 摘要比正文
**更容易被模型直接采信**：它看起来像「系统给我的检索结果」，不像「某个网页里
的一段话」。因此标注不因为「结果只有几行」而放松。

## 四类文案为什么必须写得不一样

`no_key` 与 `quota` 必须明确写**不要重试**，`service` 必须明确写**可以重试一次**。
三条长得差不多时，模型会一律重试——而前两类重试一万次也不会成功，
只会把剩下的迭代轮次全部烧光。这与 spec F13「达到上限返回说明而不是错误」
是同一条理由的两个落点。
"""

from rhinecode.web.models import SearchOutcome
from rhinecode.web.search import FAILURE_NO_KEY, FAILURE_QUOTA, FAILURE_SERVICE

# 不可信内容标记。**与 `web/render.py` 是同一对标签**——两处形态一致，
# 模型对「什么叫不可信内容」的认知才不会分叉；系统提示里那条固定约束
# 也是按这一对标签写的。
UNTRUSTED_OPEN = '<untrusted-content source="{source}">'
UNTRUSTED_CLOSE = "</untrusted-content>"

_PREFIX = "[web_search]"

# 单行摘要里查询词的展示上限。**只影响给人看的那一行**——
# 回灌模型的正文与确认面板都是完整原文（spec F7）。
_SUMMARY_QUERY_MAX = 24

# 失败类别 → 给人看的短名（进单行摘要）。
_FAILURE_LABELS = {
    FAILURE_NO_KEY: "未配置密钥",
    FAILURE_QUOTA: "配额已用完",
    FAILURE_SERVICE: "服务不可用",
}


def _quota_text(outcome: SearchOutcome) -> str:
    """
    渲染配额那一格。

    :param outcome: 搜索结果
    :returns: 形如 `3/50` 或 `3/不限`

    副作用：无（纯函数）。
    """
    if outcome.limit <= 0:
        return f"{outcome.used}/不限"
    return f"{outcome.used}/{outcome.limit}"


def _meta_line(outcome: SearchOutcome) -> str:
    """
    组装元信息行。**全部字段来自我们自己的代码，可信。**

    :param outcome: 搜索结果
    :returns: 单行文本

    副作用：无（纯函数）。
    """
    bits = [f"{_PREFIX} 查询：{outcome.query}"]
    if outcome.provider:
        bits.append(f"服务商：{outcome.provider}")
    bits.append(f"结果：{len(outcome.results)} 条")
    bits.append(f"本次会话已用 {_quota_text(outcome)}")
    return " · ".join(bits)


def _wrap(outcome: SearchOutcome, body: str) -> str:
    """
    把结果正文包进不可信标记。

    :param outcome: 搜索结果（提供来源标注）
    :param body: 已编号的结果列表文本
    :returns: 带开闭标记的文本

    来源写成 `<服务商>-search:<原始查询词>`，让模型一眼看出这段内容
    「是谁给的、是回答哪个问题的」——它是判断内容可信度的依据之一。

    副作用：无（纯函数）。
    """
    source = f"{outcome.provider or 'unknown'}-search:{outcome.query}"
    return "\n".join([UNTRUSTED_OPEN.format(source=source), body, UNTRUSTED_CLOSE])


def _results_body(outcome: SearchOutcome) -> str:
    """
    把结果列表渲染成编号文本。

    :param outcome: 搜索结果
    :returns: 多行文本，每条三行（标题 / 地址 / 摘要）

    地址单独占一行且不加任何标注——spec F20 明确不做可访问性过滤或标注：
    真正的访问控制发生在模型拿它去调 `web_fetch` 的那一刻。

    副作用：无（纯函数）。
    """
    lines: list[str] = []
    for index, item in enumerate(outcome.results, start=1):
        lines.append(f"{index}. {item.title or '(无标题)'}")
        lines.append(f"   {item.url}")
        if item.snippet:
            lines.append(f"   {item.snippet}")
    return "\n".join(lines)


def render(outcome: SearchOutcome) -> str:
    """
    把搜索结果渲染成回灌模型的完整文本。

    :param outcome: 搜索结果
    :returns: 可直接作为 `ToolResult.output` 的文本

    五条路径：

    1. **未配置密钥** —— 明说这不是临时故障、不要重试、请让用户去配置。
    2. **配额已用完** —— 明说不会恢复、不要重试、请用已有信息继续。
    3. **服务不可用** —— 给出原因、允许换个说法重试**一次**。
    4. **成功但 0 条** —— 走成功路径，但**不包不可信标记**（没有外部内容要包）。
    5. **成功且有结果** —— 元信息 + 不可信标记包裹的编号列表。

    副作用：无（纯函数）。
    """
    # ① 未配置密钥：这是配置问题，模型自己解决不了。
    #
    # ⚠ 「不是临时故障」这半句不可省。不写的话模型会把它当成网络抖动，
    # 一轮一轮重试下去——而它一万次也不会成功。
    if outcome.failure == FAILURE_NO_KEY:
        return "\n".join(
            [
                f"{_PREFIX} 搜索不可用：未配置搜索服务密钥。",
                "说明：这**不是临时故障**，重试不会成功。",
                "请告诉用户在 config.yaml 的 search.api_key 里填入密钥后重启，"
                "然后用你已经掌握的信息继续当前任务。",
            ]
        )

    # ② 配额已用完：同样不会恢复，但下一步动作不同——它该继续干活，不是等。
    if outcome.failure == FAILURE_QUOTA:
        return "\n".join(
            [
                f"{_PREFIX} 本次会话的搜索次数已用完（{_quota_text(outcome)}）。",
                "说明：这**不是错误，也不会恢复**——请不要重试搜索。",
                "请用已经拿到的信息继续回答；确实需要更多搜索时，"
                "告诉用户可以在 config.yaml 的 search.session_quota 里调高上限，"
                "或用 /clear 开始新一轮对话。",
            ]
        )

    # ③ 服务不可用：这一类**是**可能恢复的，所以文案与上面两条刻意相反。
    if outcome.failure == FAILURE_SERVICE:
        return "\n".join(
            [
                f"{_PREFIX} 搜索失败：{outcome.query}",
                f"原因：{outcome.error or '搜索服务未返回可识别的结果'}",
                "说明：这可能是一次临时故障，**可以换个说法重试一次**。"
                "若连续失败，请告诉用户检查网络连通性与 config.yaml 的 search 配置，"
                "不要反复重试。",
            ]
        )

    # ④ 成功但 0 条：没有外部内容，因此**不包不可信标记**——
    # 包一个空壳只会让模型以为里面有东西被吞了。
    if not outcome.results:
        return "\n".join(
            [
                _meta_line(outcome),
                "这个查询词没有搜到任何结果。可以换个说法、换个关键词，"
                "或改用更常见的表述再试一次。",
            ]
        )

    # ⑤ 正常：元信息（可信）+ 不可信标记包裹的结果（不可信）。
    return "\n".join([_meta_line(outcome), _wrap(outcome, _results_body(outcome))])


def summary(outcome: SearchOutcome) -> str:
    """
    渲染 TUI 单行摘要（`ToolResult.summary`）。

    :param outcome: 搜索结果
    :returns: 形如 `搜索「httpx 超时」· 5 条 · 3/50`

    与 `render` 的分工同 `ToolResult` 的既有约定：output 给模型看完整内容，
    summary 给人看量级与状态。

    ⚠ 这里的查询词**会被截短**，而那不违反 spec F7——F7 要求的是
    「做决定的地方看得到全文」，那是确认面板（`tui/widgets.py` 的补充展示行）
    与 `Ctrl+O` 展开态。单行摘要只有一行高，天然放不下。

    副作用：无（纯函数）。
    """
    query = outcome.query.replace("\n", " ").strip()
    if len(query) > _SUMMARY_QUERY_MAX:
        query = query[:_SUMMARY_QUERY_MAX] + "…"

    if outcome.failure:
        label = _FAILURE_LABELS.get(outcome.failure, "失败")
        return f"搜索「{query}」失败：{label}"
    return f"搜索「{query}」· {len(outcome.results)} 条 · {_quota_text(outcome)}"
