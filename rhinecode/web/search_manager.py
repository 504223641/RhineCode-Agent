"""
`WebSearchManager`（web_search 扩展 spec F13/F17/F21）：配额 + 一次 HTTP 调用的编排。

**本模块是 `web` 包搜索侧唯一持有 HTTP 客户端、唯一有副作用的地方。**
这条分工沿用 `context/manager.py`、`memory/manager.py`、`web/manager.py` 的既有约定：
一层里只有 manager 有副作用，其余模块（`search` / `search_render`）保持纯逻辑、
可脱离网络单测。

## 一次调用的流程

    search(query, count)
      ├─ 密钥为空                → FAILURE_NO_KEY   （不发请求、不计数）
      ├─ _take_quota() 失败      → FAILURE_QUOTA    （不发请求、不计数）
      └─ 发一次 HTTP GET
           ├─ 异常 / 非 2xx / parse 返回 None → _release_quota() → FAILURE_SERVICE
           └─ parse 返回列表（含空列表）      → SearchOutcome(ok=True)

## ⚠ 它不做抓取该做的那些事，这是刻意的

没有重定向跟随、没有 HTML 转换、没有逐跳硬校验——因为**这不是 HTTP 抓取，
是 API 调用**：端点由用户在配置里写死，不是模型指定的地址（spec F11）。
把它塞进 `fetcher.py` 会让那个模块变成「什么都干」的模块。

## ⚠ 它不持有 provider（模型）

搜索结果**不经过二次抽取**（spec F18）：结果本来就短，多一次模型往返只是
多一条失败路径、多一次计费、多一处可被注入的地方。因此本类与
`WebFetchManager` 不同，**完全不碰 LLM**。
"""

import threading
from typing import Callable, Optional

import httpx

from rhinecode.web.models import SearchOutcome
from rhinecode.web.search import (
    FAILURE_NO_KEY,
    FAILURE_QUOTA,
    FAILURE_SERVICE,
    SearchProvider,
    normalize_count,
)

# 单次搜索的默认超时（秒）。装配层会用配置里的 `search.timeout` 覆盖它。
DEFAULT_TIMEOUT: float = 10.0


class WebSearchManager:
    """
    搜索的编排者：管配额、发请求、组装结果。

    :ivar _provider: 服务商适配（决定端点默认值、鉴权头、参数与解析）
    :ivar _api_key: 搜索服务密钥；**空串 = 未配置**
    :ivar _endpoint: 实际端点（配置覆盖过的；为空时取 `provider.endpoint`）
    :ivar _max_results: 缺省结果条数，调用方不指定时用它
    :ivar _limit: 会话配额上限；**0 或负数 = 不限制**
    :ivar _timeout: 单次请求超时（秒）
    :ivar _client_factory: HTTP 客户端工厂；注入替身用（spec N5）
    :ivar _used: 本次会话已用次数
    :ivar _lock: 保护 `_used` 的锁

    ## 配额为什么是「预留 + 失败回退」

    直觉写法是「搜成功了再 +1」，但那在**并发**下守不住上限：主对话与三个
    子 Agent 同时搜索时，四个线程都读到 `used == 49`、都判定「没超」，
    于是发出四次请求。预留是在锁内**读改一步完成**的，上限因此是硬的。

    反过来「只预留不回退」会违反 spec F13 的计数表——服务不可用时那一次
    不该计数（配额是一道**花钱**的护栏，没花钱就不该扣）。

    两者合起来就是下面 `_take_quota` / `_release_quota` 这一对。

    ## ⚠ 加锁不变量：临界区只做纯内存读写

    HTTP 调用、渲染、埋点一律在锁外。这是本项目第五次面对同一条戒律
    （`hooks` / `skills` / `team` / `classifier` / `subagents` 各有一条同型的
    不变量），违反的后果是**一个卡住的 HTTP 请求锁死整个 manager**，
    而调用栈上没有任何线索。

    ⚠ **本类刻意不持有任何回调、不做任何跨线程调度**，从结构上杜绝违反上一条
    （照抄 `subagents/tasks.py::TaskManager` 的先例）。它唯一的可变状态是一个整数。
    """

    def __init__(
        self,
        provider: SearchProvider,
        api_key: str,
        endpoint: str = "",
        max_results: int = 5,
        session_quota: int = 50,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        client_factory: Optional[Callable] = None,
    ) -> None:
        self._provider = provider
        self._api_key = str(api_key or "").strip()
        self._endpoint = str(endpoint or "").strip() or provider.endpoint
        self._max_results = int(max_results or 5)
        self._limit = int(session_quota or 0)
        self._timeout = float(timeout or DEFAULT_TIMEOUT)
        self._client_factory = client_factory
        self._used = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 配额（spec F13）
    # ------------------------------------------------------------------ #
    def _take_quota(self) -> bool:
        """
        预留一个搜索名额。

        :returns: 预留成功返回 True；已达上限返回 False

        副作用：`_used` 加一（成功时）。**临界区只做纯内存读写。**
        """
        with self._lock:
            if self._limit > 0 and self._used >= self._limit:
                return False
            self._used += 1
            return True

    def _release_quota(self) -> None:
        """
        退回一个已预留但未真正用掉的名额。

        请求发出后失败（异常 / 非 2xx / 响应结构不认识）时调用——
        spec F13 明确规定这三种情形**不计入配额**。

        副作用：`_used` 减一。**临界区只做纯内存读写。**
        """
        with self._lock:
            if self._used > 0:
                self._used -= 1

    def reset_quota(self) -> None:
        """
        把已用次数清零。

        唯一调用方是协调层的 `clear()`（`/clear` 命令）——那是「新一次会话」
        的既有语义所在，spec F13 要求配额跟着复位。

        副作用：`_used` 归零。
        """
        with self._lock:
            self._used = 0

    def quota_state(self) -> "tuple[int, int]":
        """
        读当前配额状态。

        :returns: `(已用次数, 上限)`；上限为 0 表示不限制

        副作用：无。
        """
        with self._lock:
            return self._used, self._limit

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    def search(self, query: str, count: Optional[int] = None) -> SearchOutcome:
        """
        执行一次搜索。

        :param query: 查询词（已通过权限判定与分类器审查）
        :param count: 结果条数；None 表示用配置里的缺省值
        :returns: SearchOutcome。**对普通异常永不抛出**——一切失败都转成
                  带 `failure` 类别的结果

        ⚠ **边界**：`BaseException`（`KeyboardInterrupt` / `SystemExit`）**刻意不吞**。
        下面用的是 `except Exception` 而不是 `except BaseException`——用户按
        Ctrl-C / Esc 要能中断一次卡住的搜索，把中断信号吞掉才是 bug。

        副作用：向搜索服务商发起一次 HTTP 请求（除非注入了替身）；改动配额计数。
        """
        text = str(query or "").strip()

        # ① 未配置密钥：不发请求，因此**不计数**（spec F13）。
        if not self._api_key:
            return self._outcome(text, FAILURE_NO_KEY, "未配置搜索服务密钥")

        # ② 配额：预留失败即已用完。同样没发请求。
        if not self._take_quota():
            return self._outcome(text, FAILURE_QUOTA, "本次会话的搜索配额已用完")

        # ③ 真正发请求。**从这里开始，任何失败都要 _release_quota()。**
        size = normalize_count(count, self._max_results)
        try:
            payload = self._request(text, size)
        except Exception as exc:  # noqa: BLE001 —— 一切失败转成可读结果
            self._release_quota()
            return self._outcome(text, FAILURE_SERVICE, f"{type(exc).__name__}: {exc}")

        results = self._provider.parse(payload)
        if results is None:
            # ⚠ `None` 与 `[]` 在这里分道扬镳，这是本扩展最容易写错的一处。
            #
            # `None` = 响应结构不认识（多半是字段名对不上）→ **失败、退配额**；
            # `[]`   = 服务商确实没搜到              → **成功、计配额**。
            #
            # 合并两者的净效果是：一次解析故障伪装成「这个词搜不到」，
            # 模型于是去换关键词反复重试，而根因在别处，查半天查不到。
            self._release_quota()
            return self._outcome(
                text, FAILURE_SERVICE, "搜索服务返回了无法识别的响应结构"
            )

        used, limit = self.quota_state()
        return SearchOutcome(
            ok=True,
            query=text,
            provider=self._provider.name,
            results=tuple(results),
            used=used,
            limit=limit,
        )

    def _request(self, query: str, count: int):
        """
        发一次 GET 并返回解码后的 JSON 负载。

        :param query: 查询词原文
        :param count: 已规范化的条数
        :returns: 解码后的负载（通常是 dict）
        :raises Exception: 网络异常、超时、非 2xx、JSON 解码失败

        ⚠ **只发 GET，只带两个请求头**（spec F2）：一个鉴权头、一个 `Accept`。
        没有请求体、没有 cookie、没有自定义头——「除查询词之外还能往外发什么」
        被压到零。（但请注意：**查询词本身就是发出去的数据**，
        spec F2 明确禁止把这条写成「只取不发」。）

        副作用：一次真实的 HTTP 请求（除非注入了替身）。
        """
        # 工厂**不带参数**调用、超时逐次传给 `get`——与 `fetcher.fetch` 同一形态。
        # 这样替身只需实现一个 `get`，不必模仿 httpx 的构造签名。
        factory = self._client_factory or httpx.Client
        client = factory()

        try:
            response = client.get(
                self._endpoint,
                params=self._provider.build_params(query, count),
                headers={
                    self._provider.auth_header: self._api_key,
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
            status = int(getattr(response, "status_code", 0))
            if not 200 <= status < 300:
                raise RuntimeError(f"搜索服务返回 HTTP {status}")
            return response.json()
        finally:
            # 替身不一定有 close，因此用 getattr 而不是直接调。
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 —— 关闭失败不该盖住真正的错误
                    pass

    def _outcome(self, query: str, failure: str, error: str) -> SearchOutcome:
        """
        组装一个失败结果，配额状态取当前值。

        :param query: 查询词原文
        :param failure: 失败类别常量
        :param error: 补充说明
        :returns: `ok=False` 的 SearchOutcome

        副作用：无（只读一次配额状态）。
        """
        used, limit = self.quota_state()
        return SearchOutcome(
            ok=False,
            query=query,
            provider=self._provider.name,
            used=used,
            limit=limit,
            failure=failure,
            error=error,
        )
