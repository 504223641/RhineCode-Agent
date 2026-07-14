"""
输入解析器（c10 T3）：无副作用的纯函数，只做输入分类与「命令字段/参数」一次切分。

职责边界（plan 12.2）：
- 不查询注册表——命令是否存在由 registry 决定；
- 不决定未知命令行为——那是 dispatcher 的事；
- 不做 shell 分词、引号解析或转义（spec F11/N10）：参数是「命令字段之后
  去除两端空白的原文」，内部空白与大小写原样保留。
"""

from rhinecode.commands.models import InputKind, ParsedInput


def parse_input(text: str) -> ParsedInput:
    """
    把用户输入分类为 空输入 / 普通消息 / 斜杠命令，并对斜杠输入做一次切分。

    规则（spec F4–F7，plan 4.3）：
    1. 去除两端空白后为空 → EMPTY（不回显、不报错、不调 AI）；
    2. 去除两端空白后首字符不是 "/" → MESSAGE（正文里的 /plan 不触发命令）；
    3. 斜杠输入按**第一个空白字符**切分：之前是命令字段（保留原始大小写，
       查注册表时才 casefold），之后去除两端空白作为参数（内部内容不改）。

    :param text: 用户原始输入（含前后空白）
    :returns: ParsedInput；raw_text 始终保留传入原文
    """
    stripped = text.strip()
    if not stripped:
        return ParsedInput(kind=InputKind.EMPTY, raw_text=text)
    if not stripped.startswith("/"):
        return ParsedInput(kind=InputKind.MESSAGE, raw_text=text)
    # str.split(None, 1)：按任意连续空白（空格/Tab/换行）切一次——
    # 命令字段与参数之间的连续分隔空白不属于参数（spec F5）。
    parts = stripped.split(None, 1)
    command_token = parts[0]
    arguments = parts[1].strip() if len(parts) > 1 else ""
    return ParsedInput(
        kind=InputKind.SLASH,
        raw_text=text,
        command_token=command_token,
        arguments=arguments,
    )
