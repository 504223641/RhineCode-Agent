"""
网络搜索的纯逻辑层（web_search 扩展 spec F1/F17/F18）：服务商适配 + 三个纯函数。

## 本模块零 IO，且必须保持零 IO

它不发请求、不读文件、不碰环境变量。给定「查询词 / 条数 / 一份响应负载」
就能算出结果，因此全部单测可以脱离网络跑（spec N5）——那既是可测性要求，
也是安全要求：**测试套件不该因为跑测试就把查询词发出去**。

## ⚠ 两条「刻意不 import」

**不 import `rhinecode.permission`。** `import permission.network` 会连带执行
`permission/__init__.py`，把整个权限引擎与 `rhinecode.tools` 一起拉起来，
spec N4 的叶子性当场失效。下面 `check_endpoint` 要判的只有「协议是不是
http/https」，标准库的 `urlsplit` 三行就够——**这不是重复实现**：②′网络边界层
判的是「地址范围 + 内嵌凭据 + 协议」，而搜索端点由用户在配置里写死、
按 spec F11 明确不过②′（否则用户配一个内网搜索代理就用不了）。

**不 import `httpx`。** 真正发请求是 `search_manager.py` 的事。本模块只负责
「请求该带什么参数」与「响应该怎么读」，两头都是纯数据。

## 服务商适配的那道边界

`SearchProvider` 是 spec「不做的事」里那句「留边界、只实现一家」的落点：
本模块只有一个实例 `BRAVE`，与一张 `PROVIDERS` 表。加第二家的成本是
**写两个函数 + 加一行表项**，不是重构。
"""

from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from rhinecode.web.models import SearchResult

# ── 失败类别（spec F21）────────────────────────────────────────────────────
#
# ⚠ **必须是显式常量，不能靠错误文本判断类别。** 四类的「是否计入配额」与
# 「ok 取什么值」完全不同，用字符串匹配去分类的话，文案润色一次分类就悄悄错了，
# 而没有任何东西会报错。理由详见 `models.SearchOutcome` 的 docstring。
#
# 空串 = 成功（**含搜到 0 条那一种**）。
FAILURE_NO_KEY = "no_key"      # 未配置搜索服务密钥
FAILURE_QUOTA = "quota"        # 本次会话的搜索配额已用完
FAILURE_SERVICE = "service"    # 服务不可用 / 超时 / 响应结构不认识

# ── 结果条数边界（spec F1）────────────────────────────────────────────────
MIN_COUNT = 1
MAX_COUNT = 10
DEFAULT_COUNT = 5


def normalize_count(raw: Any, default: int = DEFAULT_COUNT) -> int:
    """
    把调用方给的「结果条数」规范化成一个合法值。

    :param raw: 原始值。可能是 int、数字字符串、None，或任何非法值
    :param default: 无法解析时的回退值（装配层会传配置里的 `search.max_results`）
    :returns: `[MIN_COUNT, MAX_COUNT]` 区间内的整数

    **任何情况都不抛异常**（spec F1「越界只夹取、不报错」）：模型把条数写成 0
    或 99 不是一次需要打断它的错误，夹取到边界就是它想要的效果。

    ⚠ **`bool` 单独处理，不走夹取。** Python 里 `bool` 是 `int` 的子类，
    `True` 会被夹成 1——模型于是拿到一条结果，还以为自己要到的就是一条。
    传布尔进来是明显的类型错误，退回缺省比夹成 1 更诚实。

    ⚠ **回退值也要夹取，这不是多余的。** `default` 来自配置里的
    `search.max_results`，而那一项走的是「非法值回退默认」的宽松口径，
    用户完全可以写 `max_results: 99`。不夹的话，模型**不指定条数**时
    （最常见的情况）反而会把 99 原样发给服务商——一条越界值从「用户指定」
    这条路被挡住，却从「缺省」这条路溜了出去。实现期实测撞到过。

    副作用：无（纯函数）。
    """

    def _clamp(value: int) -> int:
        if value < MIN_COUNT:
            return MIN_COUNT
        return MAX_COUNT if value > MAX_COUNT else value

    try:
        fallback = _clamp(int(default))
    except (TypeError, ValueError):
        fallback = DEFAULT_COUNT

    if isinstance(raw, bool):
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return fallback
    return _clamp(value)


def check_endpoint(url: str) -> Optional[str]:
    """
    校验搜索端点是否可用。

    :param url: 端点地址（配置里的 `search.endpoint`，或服务商的官方地址）
    :returns: 不合法时返回一句中文原因；合法返回 `None`

    只判两件事：**协议必须是 http 或 https**、**主机名必须非空**。

    协议限制的理由与②′网络边界层同源——一个 `file://` 端点会让搜索工具
    变成文件读取工具，绕过第②层路径沙箱。**但这里不判地址范围**
    （不禁环回、不禁私有段）：端点是用户在配置里写死的，配一个内网搜索代理
    是合理需求（spec F11）。

    非 https 的合法端点由**装配层**出一条启动警告（spec F17），不在这里拦——
    查询词是明文外发的数据，走明文通道值得说一句，但那是用户的选择。

    副作用：无（纯函数，不做任何域名解析）。
    """
    text = str(url or "").strip()
    if not text:
        return "搜索端点为空"
    try:
        parts = urlsplit(text)
    except ValueError as exc:  # 极畸形的地址，urlsplit 才会抛
        return f"搜索端点无法解析：{exc}"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return (
            f"搜索端点的协议不被允许：{scheme or '（缺失）'}。"
            "只接受 http 或 https——其它协议（如 file://）会让搜索工具"
            "变成文件读取工具，绕过路径沙箱。"
        )
    if not parts.hostname:
        return f"搜索端点缺少主机名：{text}"
    return None


# ── 服务商适配 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SearchProvider:
    """
    一家搜索服务商的适配描述。

    :ivar name: 服务商名，进结果元信息给模型与用户看
    :ivar endpoint: 官方端点，配置里没写 `search.endpoint` 时用它
    :ivar auth_header: 密钥放哪个请求头
    :ivar build_params: `(查询词, 条数) -> 查询参数字典`
    :ivar parse: `(响应负载) -> Optional[list[SearchResult]]`，**三态**，见下

    ## 为什么是「dataclass 装两个 callable」而不是抽象基类

    一个基类只有一个子类时，它表达不出任何东西——只是把两个函数挪到了别处，
    再多一层继承。dataclass 里放两个 callable 是**同样的接缝、少一层间接**，
    而且加第二家的成本是「写两个函数 + 在 `PROVIDERS` 加一行」，不是重构。

    spec 的「不做的事」里明确写了：**只实现一家**。没有第二个真实实现来检验的
    抽象，很容易设计成只适配自己的想象。
    """

    name: str
    endpoint: str
    auth_header: str
    build_params: Callable[[str, int], dict]
    parse: Callable[[Any], Optional[list]]


def _brave_params(query: str, count: int) -> dict:
    """
    构造 Brave Search API 的查询参数。

    :param query: 查询词原文（**不改写、不脱敏**，spec F8）
    :param count: 已规范化的条数
    :returns: 查询参数字典

    副作用：无（纯函数）。
    """
    return {"q": query, "count": count}


def _brave_parse(payload: Any) -> Optional[list]:
    """
    解析 Brave Search API 的响应负载。

    :param payload: 已解码的 JSON 负载（通常是 dict）
    :returns: **三态** —— 见下表

    | 情形 | 返回 | 调用方据此做什么 |
    | --- | --- | --- |
    | 结构不认识（非 dict、缺 `web`、`results` 不是列表…） | **`None`** | 判 `FAILURE_SERVICE`，**不计配额** |
    | 结构认得、`results` 是空列表 | **`[]`** | **成功 0 条**，`ok=True`，**计配额** |
    | 正常 | 结果列表 | 成功 |

    ⚠ **三态是必须的，不能只返回列表。** 「解析不出来」与「真的没搜到」
    在 HTTP 层看不出任何区别（都是拿不到结果），而 spec F21 对两者的判定
    **完全相反**。用空列表同时表示这两件事的净效果是：一次解析故障会伪装成
    「这个词搜不到」——模型于是去换关键词反复重试，而根因在字段名对不上，
    查半天查不到。

    ⚠ **单条结果的畸形只跳过那一条，绝不抛异常。** 理由是我们**连不上真实服务
    去核对字段名**——本机 DNS 把公网域名重写成内网地址（见 web_fetch 验收记录
    的遗留项 #3），因此实现期必须假设自己对字段名的记忆可能有偏差，
    让「读不出来」走一条可读的失败路径，而不是让一个 `KeyError` 冒泡上去
    变成「你的工具坏了」。

    副作用：无（纯函数）。
    """
    if not isinstance(payload, dict):
        return None
    web = payload.get("web")
    if not isinstance(web, dict):
        return None
    raw_results = web.get("results")
    if not isinstance(raw_results, list):
        return None

    items: list = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            # 单条不是对象：跳过它，其余照常。
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            # 没有地址的一条结果对模型毫无用处（它连「去哪取正文」都答不了）。
            continue
        items.append(
            SearchResult(
                title=str(entry.get("title") or "").strip(),
                url=url,
                # Brave 把摘要放在 description 字段。取不到时留空——
                # 一条只有标题和地址的结果仍然是有用的路标。
                snippet=str(entry.get("description") or "").strip(),
            )
        )
    return items


# Brave Search API。选它的理由（plan 评审 2026-08-19）：注册后拿一个密钥就能用，
# 配置里只多一个字段，出问题时排查面最窄。
BRAVE = SearchProvider(
    name="brave",
    endpoint="https://api.search.brave.com/res/v1/web/search",
    auth_header="X-Subscription-Token",
    build_params=_brave_params,
    parse=_brave_parse,
)

# 服务商名 → 适配描述。
#
# ⚠ **与 `rhinecode/config.py` 的 `_KNOWN_PROVIDERS` 是成对维护点**：
# 那边要校验用户写的 `search.provider` 是否合法，但 `config.py` 刻意不 import
# 本模块（那是层级倒挂——配置层不该依赖能力层）。加第二家时两处齐改，
# 漏改的表现是「配置里写了新服务商，启动时说不认识」。
PROVIDERS: dict = {BRAVE.name: BRAVE}

DEFAULT_PROVIDER = BRAVE.name
