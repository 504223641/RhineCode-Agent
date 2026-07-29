"""
②′网络边界层（web_fetch 扩展 spec F5/F5a/F6）—— 决策管线里专管 URL 类请求的一层。

## 它在管线的哪个位置

    ① 黑名单（仅命令类）
    ② 沙箱（仅路径/glob 类）
    ②′ 网络边界（仅 URL 类）  ← 本模块
    ③ 可配置规则
    ④ 权限模式兜底

**⚠ 必须排在③之前，理由是「硬校验必须先于任何 allow 规则」。** 若②′晚于③，
一条 `allow: WebFetch(domain:*)` 会在③层先行放行，`file://` 与 `127.0.0.1`
就整个跳过了硬校验。护栏见 `tests/test_perm_network_layer.py` 里那条
「全域名 allow + 禁止地址仍被拒且命中层为 NETWORK」的用例。

（注意：白名单语义本身**不**依赖这个顺序——能在③命中 allow 的域名本来就在白名单内，
两种顺序结论相同。所以顺序护栏必须用「全域名 allow + 禁止地址」构造，
用「白名单未命中」构造是发现不了顺序错误的。）

## 本层的两类职责

1. **结构性硬校验**（`check_hard`）——协议、内嵌凭据、IP 字面量范围。
   命中即拒，**不可被任何配置、任何权限规则、任何权限模式放开**（与①黑名单同一性质）。
2. **域名策略求值**（`decide`）——deny 优先、allow 命中放行、
   「白名单已建立但未命中」即拒。

## 为什么判定期不做 DNS 解析

spec N4 要求判定层是**纯逻辑**：给定「地址 + 模式 + 规则集」即可判定，不发起任何网络请求。
DNS 解析是 I/O——放进这里会让**每一次权限判定**都可能阻塞在网络上，
也会让一大批纯逻辑权限测试变成潜在的联网测试（spec N5 既是可测性要求也是安全要求：
测试套件不应因为跑测试而向外发出请求）。

因此域名解析结果的校验放在**连接期**（`rhinecode/web/fetcher.py`，每跳建立连接前），
它复用本模块的 `is_forbidden_address` 与 `check_hard`。

**两个时机必须共用同一份实现**（spec F5a 硬要求）——各写一套是典型的
「改一处漏一处」，且漏改不报错，只是某个地址悄悄能访问了。

## 判定期只挡得住 IP 字面量

`http://2130706433/`（十进制形式的 127.0.0.1）与 `http://0177.0.0.1/`（八进制）
在判定期**不会**被识别为 IP —— `ipaddress.ip_address` 对它们直接抛 ValueError，
于是它们被当成主机名放过，要到连接期才被拦下。这是「两个时机缺一不可」的最好例证。
"""

import ipaddress
from typing import Optional
from urllib.parse import urlsplit

from rhinecode.permission.models import Decision, DecisionResult, Layer, PermissionRequest
from rhinecode.permission.rules import RuleSet

# 协议白名单：只允许这两种。写成常量而非字面量，是为了让「新增协议」这件事
# 必须显式改这一行，而不是散落在条件判断里。
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# 各协议的默认端口。用于 split_url 归一化——使 https://a.com 与 https://a.com:443
# 在「同主机」判据（spec F8）下判为相等。
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

# 被视为「指向本机」的保留名字。这些名字不经 IP 字面量分支，必须单独拦。
_RESERVED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }
)

# ---------------------------------------------------------------------------
# 拒绝原因常量：判定期与连接期**共用同一份**，保证两处文案与口径不漂移。
# ---------------------------------------------------------------------------
REASON_MALFORMED = "地址无法解析为合法的 URL"
REASON_SCHEME = "只允许 http / https 协议，不允许 {scheme}（file:// 会绕过路径沙箱）"
REASON_CREDENTIALS = "地址中不允许内嵌用户名/密码"
REASON_RESERVED_NAME = "不允许访问指向本机的保留名字：{host}"
REASON_ADDRESS = "不允许访问非公网地址 {addr}（{why}）"

# is_forbidden_address 内部使用的分类原因，会被填进 REASON_ADDRESS 的 {why}。
_WHY_UNPARSEABLE = "无法解析为合法 IP"
_WHY_NOT_GLOBAL = "不是全局可路由地址"
_WHY_MULTICAST = "组播地址"
_WHY_RESERVED = "保留地址段"
_WHY_ENCAPSULATED = "封装形式还原后指向 {inner}（{inner_why}）"


def split_url(url: str) -> tuple[str, str, int]:
    """
    把一个 URL 拆成 (scheme, host, port) 三元组。

    :param url: 完整 URL 原文
    :returns: (小写协议, 归一化主机名, 端口)；端口未显式给出时按协议取默认值
    :raises ValueError: URL 无法解析、协议缺失、主机名为空，或端口非法

    这个三元组同时服务两处：
    - 判定期：`check_hard` 取 scheme 与 host；
    - 连接期：`web/fetcher.py` 的「同主机」判据（spec F8）要求
      **(scheme, host, port) 三者全部相等**才算同主机，端口在此按协议归一化，
      使 `https://a.com` 与 `https://a.com:443` 判为相等。

    副作用：无（纯字符串运算，不做任何解析查询）。
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:  # urlsplit 对个别畸形输入（如非法 IPv6 括号）会抛
        raise ValueError(f"URL 解析失败：{exc}") from exc

    scheme = (parts.scheme or "").lower()
    if not scheme:
        raise ValueError("URL 缺少协议")

    # parts.hostname 已经做了「去括号、转小写」，但不去末尾点，故仍走一次归一化。
    raw_host = parts.hostname or ""
    if not raw_host:
        raise ValueError("URL 缺少主机名")
    host = normalize_host(raw_host)
    if not host:
        raise ValueError("URL 主机名为空")

    try:
        port = parts.port
    except ValueError as exc:  # 端口不是数字或超出范围时 urlsplit 在取值时才抛
        raise ValueError(f"URL 端口非法：{exc}") from exc
    if port is None:
        port = _DEFAULT_PORTS.get(scheme, 0)

    return scheme, host, port


def normalize_host(host: str) -> str:
    """
    把主机名归一化：去两侧空白、转小写、去掉末尾的 `.`。

    与 `matching._normalize_domain` 同口径——两处必须一致，否则
    「规则里写的域名」与「请求里带的主机名」会在不同的归一化结果上比对。

    :param host: 原始主机名
    :returns: 归一化后的主机名
    """
    return host.strip().lower().rstrip(".")


def _unwrap_encapsulated(ip: "ipaddress.IPv6Address") -> Optional["ipaddress.IPv4Address"]:
    """
    把三种「IPv6 里包着 IPv4」的封装形式还原成内层 IPv4 地址。

    :param ip: 已解析的 IPv6 地址
    :returns: 还原出的 IPv4 地址；不是封装形式时返回 None

    **为什么必须显式还原**：`is_global` 对封装形式不可靠。实测（Python 3.11）——
    `2002:7f00:1::`（6to4 封装的 127.0.0.1）的 `is_global` 是 **True**，
    直接放过就等于允许访问本机。三种形式：

    - IPv4-mapped `::ffff:a.b.c.d`  —— 标准库有现成的 `.ipv4_mapped`
    - 6to4        `2002::/16`       —— 第 2–5 字节即内层 IPv4
    - NAT64       `64:ff9b::/96`    —— 末 4 字节即内层 IPv4
    """
    mapped = ip.ipv4_mapped
    if mapped is not None:
        return mapped

    packed = ip.packed  # 16 字节
    # 6to4：2002:WWXX:YYZZ::/16，其中 WW.XX.YY.ZZ 是内层 IPv4（第 2–5 字节）。
    if packed[0] == 0x20 and packed[1] == 0x02:
        return ipaddress.IPv4Address(packed[2:6])
    # NAT64 well-known prefix：64:ff9b::/96，末 4 字节是内层 IPv4。
    if packed[:4] == b"\x00\x64\xff\x9b" and packed[4:12] == b"\x00" * 8:
        return ipaddress.IPv4Address(packed[12:16])
    return None


def _classify_address(ip) -> Optional[str]:
    """
    对一个已解析的 IP 地址做范围分类，返回拒绝原因或 None（允许）。

    :param ip: `ipaddress.IPv4Address` 或 `IPv6Address`
    :returns: 中文原因；地址可用时返回 None

    **判据是白名单式的**：`not ip.is_global` 即拒（默认拒绝、只放行确认全局可路由的），
    再补判 `is_multicast` 与 `is_reserved`（`is_global` 不覆盖它们）。

    为什么不用黑名单式枚举——审查阶段实测，按
    「环回 ∪ 私有 ∪ 链路本地 ∪ 保留 ∪ 未指定」这套黑名单：

        100.64.0.1     (CGNAT)        漏
        224.0.0.1      (组播)          漏
        2002:7f00:1::  (6to4 封环回)   漏

    三个全部放过。逐条列举必漏，因为特殊用途地址段一直在增补。
    """
    if not ip.is_global:
        return _WHY_NOT_GLOBAL
    # 以下两类的 is_global 实测为 True，必须显式补判。
    if ip.is_multicast:
        return _WHY_MULTICAST
    if ip.is_reserved:
        return _WHY_RESERVED
    return None


def is_forbidden_address(addr: str) -> Optional[str]:
    """
    判断单个 IP 地址是否落在禁止范围内。

    :param addr: IP 地址字面量（IPv4 或 IPv6，不含方括号）
    :returns: 命中返回完整的中文拒绝原因；地址可用时返回 None

    **本函数被两个时机共用**（spec F5a）：判定期的 `check_hard`（校验 URL 里写死的
    IP 字面量）与连接期的 `web/fetcher.py`（校验域名解析出来的每一个地址）。
    **不要在 `web/` 里另写一份**——那是典型的「改一处漏一处」，且漏改不报错。

    处理顺序：
    1. 解析失败 → 拒绝（fail-safe 偏严，spec N1：不确定就偏向更严）。
    2. IPv6 且是封装形式 → 还原出内层 IPv4 并**对内层复判**，命中即拒。
    3. 对地址本身走 `_classify_address`。

    副作用：无（纯运算，不做任何解析查询）。
    """
    text = (addr or "").strip()
    # 允许调用方传入带方括号的 IPv6 字面量（如 "[::1]"），统一剥掉。
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]

    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return REASON_ADDRESS.format(addr=addr, why=_WHY_UNPARSEABLE)

    # 封装形式：先还原内层再判。内层被拒时，原因里同时点出内外两层，便于排查。
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _unwrap_encapsulated(ip)
        if inner is not None:
            inner_why = _classify_address(inner)
            if inner_why is not None:
                why = _WHY_ENCAPSULATED.format(inner=inner, inner_why=inner_why)
                return REASON_ADDRESS.format(addr=addr, why=why)

    why = _classify_address(ip)
    if why is not None:
        return REASON_ADDRESS.format(addr=addr, why=why)
    return None


def check_hard(url: str) -> Optional[str]:
    """
    结构性硬校验：对一个 URL 做「不可被任何配置放开」的三项检查。

    :param url: 完整 URL 原文
    :returns: 命中返回中文拒绝原因；通过返回 None

    检查顺序（任一命中即返回）：
    1. **可解析** —— 解析失败即拒（fail-safe）。
    2. **协议限制** —— 只允许 http / https。`file://` 会让网络工具变成文件读取工具、
       直接绕过②路径沙箱；其余协议（ftp、gopher 等）无正当用途。
    3. **凭据限制** —— 地址中内嵌用户名/密码即拒。无正当用途，且会把凭据写进
       对话历史与行为记录。
    4. **保留名字** —— `localhost` 及其变体。
    5. **IP 字面量范围** —— host 若能解析成 IP，过 `is_forbidden_address`。

    **本函数被两个时机共用**（spec F5a）。连接期必须对**每一跳**重跑一次完整校验：
    只在判定期对原始地址查一次的话，`https://a.com/x` → `Location: http://user:pass@a.com/`
    这样的同主机跳转会让协议限制与凭据限制对新地址完全失效。

    **不做 DNS 解析**（判定层零 I/O，见模块 docstring）。

    副作用：无。
    """
    raw = (url or "").strip()
    if not raw:
        return REASON_MALFORMED

    # 凭据检查要在 split_url 之前单独取——split_url 只返回 (scheme, host, port)，
    # 拿不到 username。这里再解析一次，成本可以忽略。
    try:
        parts = urlsplit(raw)
        scheme, host, _port = split_url(raw)
    except ValueError:
        return REASON_MALFORMED

    if scheme not in ALLOWED_SCHEMES:
        return REASON_SCHEME.format(scheme=scheme)

    # username 非空即拒。注意 `http://:pw@h/` 这种只有密码的形式，
    # urlsplit 给出的 username 是空串而 password 非空，故两个都要看。
    try:
        if parts.username or parts.password:
            return REASON_CREDENTIALS
    except ValueError:
        return REASON_MALFORMED

    if host in _RESERVED_HOSTNAMES:
        return REASON_RESERVED_NAME.format(host=host)

    # host 是 IP 字面量时才做范围判断；是域名则留给连接期（本层不做 DNS 解析）。
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return is_forbidden_address(host)


def decide(
    request: PermissionRequest,
    merged: RuleSet,
    policy_ruleset: RuleSet,
) -> Optional[DecisionResult]:
    """
    ②′层的完整判定：给定一次 URL 类请求，返回本层的结论或「不下结论」。

    :param request: 规范化后的权限请求（`kind == "url"`，specifier 是完整 URL，
                    host 已由 adapter 填好）
    :param merged: **全部层级**的规则（turn + session + file），用于 deny/allow 命中判断
    :param policy_ruleset: **仅 user + project 两层**的规则，用于判断「白名单是否已建立」
    :returns: 本层的 DecisionResult；本层不下结论时返回 None（交由④模式兜底）

    ## 两个 RuleSet 参数的区别（spec F6a，别搞混）

    `merged` 决定「这次请求有没有被某条规则命中」——任何层级的规则都算数。

    `policy_ruleset` 只决定「用户有没有声明过白名单」，而这**只认手写进用户级 /
    项目级 YAML 的 allow 规则**。本地级（「永久放行」自动写入的授权记录）、
    会话级、本次执行级（Skill 预授权）**只放行、不建立白名单**。

    不做这个区分会出两个问题：
    - 用户在确认面板点一次「永久放行」，就等于建立了只含一个域名的白名单，
      此后其它所有域名从「弹确认」变成「硬拒且永不再问」，界面上没有恢复手段；
    - Skill 的 `allowed-tools` 走本次执行级规则，若它能建立白名单，
      一个声明了 `WebFetch(domain:x)` 的 Skill 会在执行期间**收紧**其它域名的访问——
      直接违反「`allowed-tools` 只放宽、从不收紧」这条既有安全承诺。

    ## 判定顺序

    1. 硬校验命中 → DENY（不可被任何规则或模式翻案）
    2. 规则命中（deny 优先，由 RuleSet.evaluate 保证）→ 原样返回
    3. 未命中，但**白名单已建立** → DENY
    4. 否则 → None

    副作用：无（纯判定）。
    """
    # ① 硬校验：优先于一切规则。这个顺序是本层安全性的全部依据。
    hard = check_hard(request.specifier)
    if hard is not None:
        return DecisionResult(
            Decision.DENY,
            Layer.NETWORK,
            f"网络边界拒绝：{hard}",
            kind=request.kind,
            host=request.host,
        )

    # ② 规则求值：复用③层的同一套 deny 优先逻辑，规则来源涵盖全部层级。
    hit = merged.evaluate(request)
    if hit is not None:
        # 原样返回（含它自己的 layer 与 reason），只补上 kind/host 两个展示字段。
        return DecisionResult(
            hit.decision,
            hit.layer,
            hit.reason,
            kind=request.kind,
            host=request.host,
        )

    # ③ 白名单语义：用户一旦在 user/project 层写下任何一条 allow 域名规则，
    # 就等于声明「只许访问这些」，此后未命中即拒——**且④模式兜底翻不过来**。
    # 这是「限制不可被权限模式放开」这条判据的实现根基。
    if policy_ruleset.has_allow_for(request.rule_name):
        return DecisionResult(
            Decision.DENY,
            Layer.NETWORK,
            f"网络边界拒绝：域名 {request.host} 不在允许范围内"
            "（用户级/项目级配置已声明放行域名白名单，未列出的域名一律拒绝）",
            kind=request.kind,
            host=request.host,
        )

    # ④ 本层不下结论，交由权限模式兜底（放行档对 URL 类降级为 ASK，见 engine）。
    return None
