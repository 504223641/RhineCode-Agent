"""
`web` 包的值对象：抓取结果与抽取结果。

两个都是 `frozen=True` 的 dataclass——它们在 fetcher → manager → render 之间
单向传递，构造后不该被改写；不可变也让它们能安全地进日志、进断言。
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
