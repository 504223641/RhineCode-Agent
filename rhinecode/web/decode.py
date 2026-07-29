"""
字节 → 文本的解码（web_fetch 扩展 spec F14a）。

单独成模块而不是塞进 `convert.py`：本模块处理的是**字节**，`convert` 处理的是**文本**，
职责与可测输入类型都不同。

## 为什么需要一条明确的推断链

「查中文文档」是本扩展的主场景之一。一个声明 GBK 的页面若按 UTF-8 解，
会整页变成乱码**而不报任何错**——模型拿到一堆问号，用户以为是抓取失败，
排查方向完全错。

推断顺序（前一步拿不到才走下一步）：

1. HTTP 响应头 `Content-Type: text/html; charset=gbk`
2. 文档内声明（HTML 的 `<meta charset>` / `<meta http-equiv="Content-Type">`），
   只在**前 2 KB 字节**里找——规范要求它出现在文档开头，扫全文既慢又可能撞上正文里
   偶然出现的相似片段
3. 兜底 UTF-8

任一步拿到的字符集若 Python 不认识（打错、或是个冷门别名），**回退到下一步而不是抛异常**：
解码失败不该让整次抓取失败，拿到「大部分能读的文本」远好过什么都没有。

一律 `errors="replace"`：个别坏字节替换成 `�` 而不是中断，同样是这个理由。
"""

import codecs
import re
from typing import Optional

# 文档内字符集声明的扫描窗口。规范要求 <meta charset> 出现在文档开头。
_SNIFF_BYTES = 2048

# 兜底字符集。放在常量里而不是散在代码里，便于一眼看出「猜不出时用什么」。
FALLBACK_CHARSET = "utf-8"

# HTTP 头里的 charset 参数：`text/html; charset=utf-8` / `charset="gbk"`。
_HEADER_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([\w.:+-]+)", re.IGNORECASE)

# HTML5 写法：<meta charset="gbk">
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*['"]?([\w.:+-]+)""", re.IGNORECASE)

# HTML4 写法：<meta http-equiv="Content-Type" content="text/html; charset=gbk">
_META_HTTP_EQUIV_RE = re.compile(
    rb"""<meta[^>]+http-equiv\s*=\s*['"]?content-type['"]?[^>]+content\s*=\s*['"][^'"]*charset\s*=\s*([\w.:+-]+)""",
    re.IGNORECASE,
)


def _normalize_charset(name: Optional[str]) -> Optional[str]:
    """
    把字符集名字归一化，并验证 Python 是否认识它。

    :param name: 原始字符集名（可能来自 HTTP 头或文档声明，大小写与别名不定）
    :returns: Python 认识的规范名；不认识或为空时返回 None

    用 `codecs.lookup` 而不是自己维护别名表：标准库那份别名表覆盖面远超手写，
    且「Python 认不认识」正是我们真正关心的问题（认识才解得了）。
    """
    if not name:
        return None
    try:
        return codecs.lookup(name.strip()).name
    except (LookupError, ValueError):
        return None


def charset_from_header(content_type: str) -> Optional[str]:
    """
    从 `Content-Type` 响应头里取字符集。

    :param content_type: 响应头原文，如 `text/html; charset=gbk`
    :returns: 规范化后的字符集名；未声明或不被识别时返回 None
    """
    if not content_type:
        return None
    m = _HEADER_CHARSET_RE.search(content_type)
    return _normalize_charset(m.group(1) if m else None)


def charset_from_document(raw: bytes) -> Optional[str]:
    """
    从文档开头的 `<meta>` 声明里取字符集。

    :param raw: 响应体原始字节
    :returns: 规范化后的字符集名；未声明或不被识别时返回 None

    只扫前 `_SNIFF_BYTES` 字节。两种写法都认（HTML5 的 `charset=` 与
    HTML4 的 `http-equiv="Content-Type"`），HTML5 那种优先——它更常见也更短。
    """
    head = raw[:_SNIFF_BYTES]
    for pattern in (_META_CHARSET_RE, _META_HTTP_EQUIV_RE):
        m = pattern.search(head)
        if m:
            found = _normalize_charset(m.group(1).decode("ascii", errors="ignore"))
            if found:
                return found
    return None


def decode(raw: bytes, content_type: str = "") -> tuple[str, str]:
    """
    把响应体字节解码成文本。

    :param raw: 响应体原始字节
    :param content_type: `Content-Type` 响应头原文（可为空）
    :returns: `(文本, 实际使用的字符集)`

    推断链见模块 docstring。任一步的声明若 Python 不认识就跳到下一步；
    最终一律以 `errors="replace"` 解码，**不抛异常**——
    个别坏字节不该让整次抓取失败。

    副作用：无。
    """
    if not raw:
        return "", FALLBACK_CHARSET

    charset = charset_from_header(content_type) or charset_from_document(raw) or FALLBACK_CHARSET
    try:
        return raw.decode(charset, errors="replace"), charset
    except (LookupError, ValueError):
        # 兜底的兜底：charset 已经过 codecs.lookup 校验，理论上到不了这里；
        # 但解码器本身也可能在个别输入上抛，仍然选择「给出文本」而不是失败。
        return raw.decode(FALLBACK_CHARSET, errors="replace"), FALLBACK_CHARSET
