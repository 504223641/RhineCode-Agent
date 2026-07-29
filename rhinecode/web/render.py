"""
工具结果渲染（web_fetch 扩展 spec F19/F20/F24）。

本模块是纯逻辑，把 `FetchOutcome` + `ExtractOutcome` 拼成一段可直接回灌模型的文本。

## 元信息在标记外、正文在标记内

    [web_fetch] 来源：… 最终地址：… 内容类型：… 抽取：成功
    <untrusted-content source="…">
    （抽取答案或降级原文节选）
    </untrusted-content>

这个位置关系是有意的：**元信息由我们的代码生成、可信；正文来自外部、不可信。**
把两者混在标记里会让模型无从区分——它看到「抽取：成功」这句话时，
必须能确定那是系统说的，而不是页面里写的。

## 「抽取不是消毒」

无论抽取成功还是降级，正文一律包在不可信标记里。抽取模型读的是同一张
可能被注入的页面，它完全可能把伪装成指令的文本当作正文如实转述出来。
省掉标注等于假设「过了一道模型就干净了」，那是错的。
"""

from rhinecode.tools.base import human_size
from rhinecode.web.models import ExtractOutcome, FetchOutcome

# 不可信内容标记。与 extract.py 用的是同一对标签（那边包的是给抽取模型看的素材，
# 这边包的是给主模型看的结果），两处形态一致，模型的认知才不会分叉。
UNTRUSTED_OPEN = '<untrusted-content source="{source}">'
UNTRUSTED_CLOSE = "</untrusted-content>"

_PREFIX = "[web_fetch]"


def _meta_line(fetch: FetchOutcome, extract: ExtractOutcome | None) -> str:
    """组装元信息行。全部字段来自我们自己的代码，可信。"""
    bits = [f"{_PREFIX} 来源：{fetch.source_url}"]
    if fetch.final_url and fetch.final_url != fetch.source_url:
        bits.append(f"最终地址：{fetch.final_url}")
    if fetch.content_type:
        bits.append(f"内容类型：{fetch.content_type}")
    if fetch.charset:
        bits.append(f"字符集：{fetch.charset}")
    # 两类截断分开展示（spec F19）：前者是「内容真缺了一段」，
    # 后者是「页面读完了、进上下文的那份被裁短」，语义不同。
    bits.append(f"响应截断：{'是' if fetch.bytes_truncated else '否'}")
    if extract is not None:
        bits.append(f"正文截断：{'是' if extract.chars_truncated else '否'}")
        bits.append(f"抽取：{'成功' if extract.ok else '降级'}")
    return " · ".join(bits)


def _wrap(source_url: str, body: str) -> str:
    """把正文包进不可信标记。"""
    return "\n".join(
        [UNTRUSTED_OPEN.format(source=source_url), body, UNTRUSTED_CLOSE]
    )


def render(fetch: FetchOutcome, extract: ExtractOutcome | None = None) -> str:
    """
    把抓取与抽取结果渲染成回灌模型的文本。

    :param fetch: 抓取结果
    :param extract: 抽取结果；抓取失败/跨主机重定向/二进制内容时为 None
    :returns: 可直接作为 ToolResult.output 的文本

    四条路径各有文案：

    1. **抓取失败** —— 说明原因。连接期地址限制的失败已由 fetcher 打上前缀，
       文案里会额外点明「不是权限配置问题」（spec F24 第三类）。
    2. **跨主机重定向** —— 明确写出目标地址与「未抓取」，并说明再发一次调用
       会重新走完整权限判定。
    3. **二进制内容** —— 只报类型与体量。
    4. **正常** —— 元信息 + 不可信标记包裹的正文。

    副作用：无。
    """
    # ① 抓取失败
    if not fetch.ok:
        lines = [f"{_PREFIX} 抓取失败：{fetch.source_url}", f"原因：{fetch.error}"]
        # 连接期地址限制**不是权限拒绝**。不点明的话，模型会去建议用户改
        # permissions.yaml，而那改不动任何东西。
        if "连接期地址限制" in fetch.error:
            lines.append(
                "说明：这不是权限配置问题，而是该地址（或它解析到的地址）"
                "本身不被允许访问。请不要改写地址重试，换一个可公开访问的来源。"
            )
        return "\n".join(lines)

    # ② 跨主机重定向：未抓取
    if fetch.redirect_to:
        return "\n".join(
            [
                f"{_PREFIX} 未抓取：{fetch.source_url} 重定向到了**另一个主机**。",
                f"重定向目标：{fetch.redirect_to}",
                "说明：跨主机跳转不会被自动跟随（否则一个被放行的域名只要回一个 302，"
                "就能把抓取导向任意地址）。如需继续，请对上面这个新地址**再发起一次调用**——"
                "它会重新走一遍完整的权限判定。",
            ]
        )

    # ③ 二进制内容：只报类型与体量
    if fetch.binary:
        size = human_size(fetch.content_length) if fetch.content_length >= 0 else "未知大小"
        return "\n".join(
            [
                _meta_line(fetch, None),
                f"该地址返回的是二进制内容（{fetch.content_type or '类型未声明'}，{size}），"
                f"未读取正文。本工具只处理文本类内容。",
            ]
        )

    # ④ 正常：元信息 + 不可信标记包裹的正文
    outcome = extract if extract is not None else ExtractOutcome(ok=True, text=fetch.text)
    parts = [_meta_line(fetch, outcome)]
    if not outcome.ok:
        parts.append(
            f"注意：抽取未能完成（{outcome.degraded_reason}），"
            f"以下是**页面原文节选**而不是针对你提问的回答。"
            f"如需针对性的答案，可换个提问再调用一次。"
        )
    parts.append(_wrap(fetch.source_url, outcome.text))
    return "\n".join(parts)


def summary(fetch: FetchOutcome, extract: ExtractOutcome | None = None) -> str:
    """
    渲染 TUI 单行摘要（`ToolResult.summary`）。

    :param fetch: 抓取结果
    :param extract: 抽取结果（可为 None）
    :returns: 形如「抓取 example.com · 4.2K · 抽取成功」的单行文本

    与 render 的分工同 `ToolResult` 的既有约定：output 给模型看完整内容，
    summary 给人看量级与状态。

    副作用：无。
    """
    host = ""
    try:
        from rhinecode.permission.network import split_url

        _scheme, host, _port = split_url(fetch.source_url)
    except Exception:  # noqa: BLE001 —— 摘要不该因为地址畸形而失败
        host = fetch.source_url[:40]

    if not fetch.ok:
        return f"抓取 {host} 失败"
    if fetch.redirect_to:
        return f"抓取 {host} · 跨主机重定向，未抓取"
    if fetch.binary:
        size = human_size(fetch.content_length) if fetch.content_length >= 0 else "未知大小"
        return f"抓取 {host} · 二进制 {size}，未读取"

    body = extract.text if extract is not None else fetch.text
    state = "抽取成功" if (extract is None or extract.ok) else "已降级"
    return f"抓取 {host} · {human_size(len(body.encode('utf-8')))} · {state}"
