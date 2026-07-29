"""
HTML → 纯文本转换与截断（web_fetch 扩展 spec F14）。

## 为什么手写而不是引入依赖

`beautifulsoup4` / `markdownify` 都是新增依赖，而本项目的依赖极克制
（`pyproject.toml` 里只有 anthropic / openai / pyyaml / textual / httpx）。
标准库的 `html.parser.HTMLParser` 足够做「剥标签取正文」这件事。

## 为什么输出纯文本而不是 Markdown

转换结果接下来只喂给**抽取模型**（它按提问从素材里摘事实），
Markdown 的结构信息对这件事价值有限；而手写 HTML→Markdown 的正确性成本
（嵌套列表、表格、代码块、行内格式的边界情况）远高于 HTML→文本。

本模块是纯逻辑，无 I/O，可脱离网络单测。
"""

import re
from html.parser import HTMLParser

# 内容整段丢弃的标签：它们的正文对「读页面内容」毫无价值，却常常占大头。
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})

# 产出换行的块级标签。不求完备——目的只是让正文不粘成一坨，
# 便于抽取模型分辨段落边界。
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "hr", "li", "tr", "td", "th",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "nav", "aside",
        "blockquote", "pre", "table", "ul", "ol", "dl", "dt", "dd",
        "form", "figure", "figcaption",
    }
)

# 收尾清理：把连续空白压成一个空格、连续空行压成一个空行。
_MULTI_SPACE_RE = re.compile(r"[ \t　]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    """
    把 HTML 事件流累积成纯文本。

    `convert_charrefs=True`（基类默认）会自动把 `&amp;` / `&#65;` 这类实体
    解成字符并作为普通数据交给 `handle_data`，因此本类不必自己解实体。
    """

    def __init__(self) -> None:
        # convert_charrefs 显式写出来，是因为这个行为本类要依赖它。
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        # 用计数而不是布尔标志。
        #
        # 对 `script` / `style` 两个标签其实无所谓——`HTMLParser` 对它们用 **CDATA 模式**，
        # 内层的同名开始标签压根不触发 `handle_starttag`，第一个结束标签就收尾了
        # （浏览器也是这么解析的，所以那之后的文本本来就是正文）。
        #
        # 真正需要计数的是 `svg` / `template`：它们走**普通解析**，可以合法嵌套。
        # 用布尔标志的话，`<svg><svg></svg>这里</svg>` 里的「这里」会被内层结束标签
        # 提前解除跳过、漏进正文。
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        # 自闭合形式（<br/>）不会触发 handle_endtag，单独处理。
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    """
    把 HTML 转成可读的纯文本。

    :param html: HTML 原文（已解码的字符串）
    :returns: 剥去标签与脚本/样式后的正文；输入为空或全是标签时返回空串

    处理：丢弃 script/style/noscript/template/svg 的内容；块级标签产出换行；
    HTML 实体由基类解码；收尾压缩连续空白与空行。

    **不抛异常**：畸形 HTML 由 `HTMLParser` 自身容错（它对未闭合标签等情况宽松），
    真出了意外也吞掉并返回已累积的部分——抓取的价值在于「拿到能读的内容」，
    不该因为一个坏标签整次失败。

    副作用：无。
    """
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 —— 畸形页面不该让整次抓取失败
        pass
    text = parser.text()

    # 逐行清理：行内连续空白压成一个空格，行首尾空白去掉。
    lines = [_MULTI_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    # 连续空行压成一个空行（保留段落感，但不留大片空白）。
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """
    按字符上限截断文本。

    :param text: 待截断文本
    :param limit: 字符上限；<=0 时视为不截断
    :returns: `(结果文本, 是否发生了截断)`

    返回布尔标志而不是让调用方自己比长度：截断这件事要如实告诉模型
    （spec F19），标志与文本一起返回可以保证两者不会对不上。

    副作用：无。
    """
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True
