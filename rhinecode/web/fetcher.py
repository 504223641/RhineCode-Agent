"""
HTTP 抓取（web_fetch 扩展 spec F5a/F8/F14/F17/F18）。

本模块是 `web` 包里唯一真正发出网络请求的地方，也是**连接期硬校验**的执行点。

## 逐跳循环

每一跳都做四件事，顺序不可换：

1. 对**该跳的地址**跑完整 `check_hard`（协议、内嵌凭据、IP 字面量范围）
2. 解析主机名，对**每一个**解析结果跑 `is_forbidden_address`
3. 发起 GET，先看响应头再决定读不读响应体
4. 3xx 时按 `(scheme, host, port)` 三元组判同主机：同主机续跳，跨主机不跟随

## 为什么第 1 步要「每跳都跑」而不是只在判定期跑一次

判定期（`engine.decide` → `network.check_hard`）只看到模型请求的**原始地址**。
同主机重定向会产生新地址，例如：

    https://a.com/x  →  Location: http://user:pass@a.com/

主机名没变（判为同主机、会自动跟随），但**协议降级了、还多了内嵌凭据**——
这两项限制若只在判定期查一次，对新地址完全失效。

## 为什么关掉 httpx 的自动重定向

`follow_redirects=True` 会在库内部走完整条跳转链，**不给任何回调点**——
上面那两步校验与跨主机判断都插不进去。手工逐跳是唯一能让「每一跳都过一次校验」
成立的写法。

## 关于 DNS rebinding

第 2 步解析并校验之后，交给 httpx 的仍是**主机名**，它会独立地再解析一次——
两次解析之间 DNS 记录可以改变。本模块**不声称**能缩小这个时间窗，
它与「在判定期解析」在 rebinding 面前是等价的。选连接期只有两条理由：
判定层必须零 I/O（spec N4/N5），以及每一跳都要过（spec F5a/F8）。
rebinding 已在 spec 的「安全边界」里列为已知边界。

## 两个可注入参数

`client_factory` 与 `resolver` 是 **spec N5 的硬要求**，不是可选的灵活性：
全部单测与端到端场景都要能离线跑（这既是可测性要求，也是安全要求——
测试套件不该因为跑测试而向外发出请求）。
"""

import socket
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from rhinecode.permission.network import check_hard, is_forbidden_address, split_url
from rhinecode.web.convert import html_to_text
from rhinecode.web.decode import decode
from rhinecode.web.models import FetchOutcome

# 响应体字节上限。边读边计，超限即中断连接并标记截断。
# 5 MiB 足够覆盖正常文档页，又不至于让一次误抓吃满内存。
MAX_RESPONSE_BYTES: int = 5 * 1024 * 1024

# **每一跳**的连接 + 读取总时长上限（秒）。
FETCH_TIMEOUT: float = 30.0

# 同主机重定向的跳数上限。
#
# ⚠ 一次抓取最多发出 MAX_REDIRECTS + 1 次请求（**加一是因为首次请求本身也算一跳**，
# 本常量管的是它之后的重定向次数）。因此单次调用能阻塞 Agent Loop 的时长上界是
# FETCH_TIMEOUT × (MAX_REDIRECTS + 1) = 30 × 6 = **180 秒**。
#
# 该上界**不覆盖域名解析**：标准库的解析函数没有超时参数，FETCH_TIMEOUT 管不到它，
# 它还是阻塞的底层调用、Esc 的协作式取消也打断不了。这是本扩展新引入的一段
# 不受控 I/O，已在 spec 的「安全边界」里列为已知边界。
MAX_REDIRECTS: int = 5

# 视为「文本类、值得取正文」的内容类型前缀。其余一律当二进制处理：
# 只回报类型与体量，不读响应体（避免先下载 5 MiB 再丢弃）。
_TEXT_CONTENT_TYPES = ("text/",)
_TEXT_CONTENT_SUBTYPES = (
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/ld+json",
)

# 本模块产出的失败原因前缀。与权限拒绝**刻意用不同的措辞**——
# 连接期失败不是权限判定的结果，模型需要能区分这两类（spec F24 第三类）。
CONNECT_PHASE_PREFIX = "连接期地址限制"

# 可注入替身的类型别名，写出来是为了让签名可读。
ClientFactory = Callable[[], httpx.Client]
Resolver = Callable[[str], list[str]]


def _default_resolver(host: str) -> list[str]:
    """
    缺省的主机名解析：返回该主机名解析出的全部 IP 字面量。

    :param host: 主机名
    :returns: 地址字符串列表（IPv4 与 IPv6 混合）
    :raises OSError: 解析失败（由调用方转成抓取失败）

    用 `getaddrinfo` 而不是 `gethostbyname`：后者只认 IPv4，
    一个只有 AAAA 记录的内网主机会「解析失败」而不是「被拦下」——
    结论恰好相反。
    """
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    # sockaddr 的第一项是地址；IPv6 的 sockaddr 是四元组，取法相同。
    return [info[4][0] for info in infos]


def _is_textual(content_type: str) -> bool:
    """判断内容类型是否值得取正文。"""
    main = (content_type or "").split(";", 1)[0].strip().lower()
    if not main:
        # 没声明类型时按文本处理——多数简陋服务器不发 Content-Type，
        # 一律当二进制会让它们全部抓不到内容。
        return True
    if main.startswith(_TEXT_CONTENT_TYPES):
        return True
    return main in _TEXT_CONTENT_SUBTYPES


def _same_host(a: str, b: str) -> bool:
    """
    判断两个地址是否属于「同主机」——`(scheme, host, port)` 三者全部相等。

    :param a: 当前地址
    :param b: 重定向目标地址
    :returns: 三元组全等返回 True

    **比域名规则更严是刻意的**（spec F8）：域名规则只匹配主机名，
    但重定向的自动跟随发生在权限判定**之后**，没有第二次人工复核的机会。
    照域名规则的宽松口径判的话，`https://a.com` → `http://a.com:8080`
    会在无人复核的情况下被跟随。

    端口按协议归一化（由 split_url 完成），使 `https://a.com` 与
    `https://a.com:443` 判为同主机。
    """
    try:
        return split_url(a) == split_url(b)
    except ValueError:
        # 任一侧解析不出来就按「不同主机」处理（偏严，交回模型决定）。
        return False


def _failure(source_url: str, final_url: str, reason: str) -> FetchOutcome:
    """构造一个失败结果。集中一处，保证字段不会漏填。"""
    return FetchOutcome(ok=False, source_url=source_url, final_url=final_url, error=reason)


def _guard_hop(url: str, resolver: Resolver) -> Optional[str]:
    """
    连接期守卫：对**一跳**做完整硬校验 + 域名解析结果校验。

    :param url: 该跳的地址
    :param resolver: 主机名解析函数（可注入替身）
    :returns: 命中返回中文拒绝原因；通过返回 None

    两步：
    1. `check_hard(url)` —— 与判定期**共用同一份实现**（spec F5a 硬要求）。
       每跳都跑，使协议与内嵌凭据的限制对重定向产生的新地址同样有效。
    2. 解析主机名，对每个解析结果跑 `is_forbidden_address`。
       解析失败也算拒绝（fail-safe 偏严，spec N1）。

    第 2 步捎带挡住了判定期挡不住的**混淆写法**：`http://2130706433/`
    （十进制的 127.0.0.1）在判定期不被识别为 IP 字面量、被当成主机名放过，
    到这里才见分晓。成因依平台而异——类 Unix 的解析器认这种宽松写法、
    会解出 127.0.0.1 后命中地址范围限制；Windows 的解析器不认、直接解析失败后
    命中「解析失败即拒」。**两条路都通向拒绝**，所以断言这一类时只断言
    「失败」，不要断言具体成因。

    副作用：调用 resolver（缺省实现会发起真实的域名解析）。
    """
    hard = check_hard(url)
    if hard is not None:
        return hard

    try:
        _scheme, host, _port = split_url(url)
    except ValueError as exc:
        return f"地址无法解析：{exc}"

    try:
        addresses = resolver(host)
    except Exception as exc:  # noqa: BLE001 —— 解析失败一律按拒绝处理（fail-safe）
        return f"主机名 {host} 解析失败：{exc}"

    if not addresses:
        return f"主机名 {host} 未解析出任何地址"

    for addr in addresses:
        why = is_forbidden_address(addr)
        if why is not None:
            return f"主机名 {host} 解析到了 {why}"
    return None


def fetch(
    url: str,
    *,
    client_factory: Optional[ClientFactory] = None,
    resolver: Optional[Resolver] = None,
) -> FetchOutcome:
    """
    抓取一个地址的内容。

    :param url: 目标地址（已通过权限判定）
    :param client_factory: 造 HTTP 客户端的工厂，缺省 `httpx.Client`。
                           **可注入是 spec N5 的硬要求**——全部单测与端到端场景
                           都要能离线跑
    :param resolver: 主机名解析函数，缺省 `socket.getaddrinfo` 包装。同上
    :returns: FetchOutcome。**永不抛异常**——一切失败都转成 ok=False 的结果

    执行流程见模块 docstring。

    副作用：发起真实的 HTTP 请求与域名解析（除非注入了替身）。
    """
    factory = client_factory or httpx.Client
    resolve = resolver or _default_resolver

    source_url = url
    current = url

    try:
        client = factory()
    except Exception as exc:  # noqa: BLE001
        return _failure(source_url, current, f"HTTP 客户端创建失败：{exc}")

    try:
        for _hop in range(MAX_REDIRECTS + 1):
            # ① + ② 连接期守卫：每一跳都跑，不只第一跳。
            blocked = _guard_hop(current, resolve)
            if blocked is not None:
                return _failure(source_url, current, f"{CONNECT_PHASE_PREFIX}：{blocked}")

            try:
                with client.stream(
                    "GET",
                    current,
                    follow_redirects=False,  # ⚠ 必须关掉，理由见模块 docstring
                    timeout=FETCH_TIMEOUT,
                ) as response:
                    status = response.status_code
                    content_type = response.headers.get("content-type", "")

                    # ④ 重定向：先判同主机再决定跟不跟。
                    if 300 <= status < 400:
                        location = response.headers.get("location", "")
                        if not location:
                            return _failure(
                                source_url, current, f"服务器返回 {status} 但没有给出跳转目标"
                            )
                        target = urljoin(current, location)
                        if not _same_host(current, target):
                            # 跨主机：**不跟随**。返回目标地址，由模型自行决定是否
                            # 对新地址再发起一次调用——那时会重新走一遍完整权限判定。
                            return FetchOutcome(
                                ok=True,
                                source_url=source_url,
                                final_url=current,
                                content_type=content_type,
                                redirect_to=target,
                            )
                        current = target
                        continue

                    # ③ 先看响应头：二进制类型**不读响应体**，只回报类型与体量。
                    # 先看头再决定读不读，是为了避免「先下载 5 MiB 再丢弃」。
                    if not _is_textual(content_type):
                        raw_len = response.headers.get("content-length", "")
                        try:
                            declared = int(raw_len)
                        except (TypeError, ValueError):
                            declared = -1
                        return FetchOutcome(
                            ok=True,
                            source_url=source_url,
                            final_url=current,
                            content_type=content_type,
                            text="",
                            binary=True,
                            content_length=declared,
                        )

                    # 文本类：边读边累计字节，超限即中断。
                    chunks: list[bytes] = []
                    total = 0
                    truncated = False
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= MAX_RESPONSE_BYTES:
                            truncated = True
                            break
                    raw = b"".join(chunks)[:MAX_RESPONSE_BYTES]

                    text, charset = decode(raw, content_type)
                    main_type = content_type.split(";", 1)[0].strip().lower()
                    if main_type in ("text/html", "application/xhtml+xml"):
                        text = html_to_text(text)

                    return FetchOutcome(
                        ok=True,
                        source_url=source_url,
                        final_url=current,
                        content_type=content_type,
                        charset=charset,
                        text=text,
                        bytes_truncated=truncated,
                    )
            except httpx.TimeoutException:
                return _failure(
                    source_url, current, f"抓取超时（单跳上限 {FETCH_TIMEOUT:.0f} 秒）"
                )
            except httpx.HTTPError as exc:
                return _failure(source_url, current, f"抓取失败：{exc}")

        return _failure(
            source_url, current, f"重定向次数超过上限（{MAX_REDIRECTS} 次）"
        )
    except Exception as exc:  # noqa: BLE001 —— 契约：本函数永不抛异常
        return _failure(source_url, current, f"抓取失败：{exc}")
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 —— 关闭阶段吞异常
            pass
