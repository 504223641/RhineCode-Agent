"""
Provider 层的错误分类（C7）。

## 这个模块解决什么问题

在它之前，`DeepSeekProvider.stream_chat` 的收尾是一句
`except Exception as e: yield StreamChunk(type="error", content=str(e))`。
于是 401（key 填错）、429（限流）、断网、模型名写错，用户看到的是**同一坨
SDK 原文**。R3 实跑四类，一个刚装好、key 填错一位的新用户第一次敲回车看到的是：

    错误：Error code: 401 - {'error': {'message': 'Authentication Fails, Your api
    key: ****0000 is invalid', 'type': 'authentication_error', 'param': None,
    'code': 'invalid_request_error'}}
    警告：因流错误已停止

**两行都不告诉他去改哪个文件的哪一行。**

## 两条贯穿全表的原则

1. **每条都要给出「下一步做什么」**，哪怕只是「可以直接重试」。此前那九类全都
   收敛成同一句「因流错误已停止」，而那句话不含任何行动信息。
2. **别把服务端 message 丢掉。** 分类文案是**前缀**不是**替换**——服务端偶尔会
   说出你没预料到的原因（配额、地区限制、内容审核），盖掉它会让排查变成猜谜。
   做成「一句中文说明 + 原文附在后面」。

## ⚠ 分类的轴是异常类型，不是 `.code`

R3 实测：DeepSeek 在一次 401 上返回的 `.code` 是 `'invalid_request_error'`、
`.type` 才是 `'authentication_error'`——**`.code` 与 HTTP 语义对不上**，拿它做
分派会把认证失败判成参数错误。可用的是 `type(e)`（SDK 已按 status 分好类）与
`e.status_code`。

## ⚠ 一处更正：「无重试退避」是错的

原报告写「且无重试退避（依赖 SDK 默认）」。R3 实测 SDK **已经在重试**：
`max_retries=2`、退避 `0.5 × 2ⁿ`（上限 8 秒）带抖动、且服务端给了 `Retry-After`
就听它的。拿一个只回 429 的本机服务器实测，一次 429 真发了 **3 次** HTTP 请求。
所以 C7 的缺口不是「不重试」，是**重试不可见 / 不可配 / 文案不分类**。
本模块只治第三件（那是 90% 的体验收益），前两件见模块末尾那段说明。

## 为什么这张表在 `provider/` 里

它是 **SDK 的知识**：哪个异常类对应哪个 HTTP 状态、哪几类值得重试，全都只在这
一层成立。漏到 `agent/` 或 `tui/` 会让上层去认 `openai.*` 的异常类型，而上层
连 `import openai` 都不该有。
"""

from urllib.parse import urlparse

import openai


def _host_of(base_url: str) -> str:
    """
    从 base_url 取主机名，用于「连不上 xxx」那句话。

    :param base_url: 配置里的 API 地址
    :returns: 主机名；解析不出来时返回原串（宁可多说一点也别说不出话）

    副作用：无。
    """
    try:
        return urlparse(base_url).hostname or base_url
    except ValueError:
        return base_url


def _server_message(exc: Exception) -> str:
    """
    取服务端说的那句话，取不到时退回 `str(exc)`。

    :param exc: SDK 抛出的异常
    :returns: 一段可读文本（可能为空串）

    ⚠ **必须容错到底**：`body` 可能是 None、可能是字符串、可能是没有 `message`
    键的字典——这里是错误处理路径，它自己再抛一次的话，用户看到的就从「一条
    看不懂的报错」变成「程序崩了」。
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
    return str(exc)


def classify(
    exc: Exception,
    *,
    model: str,
    base_url: str,
    config_path: str = "",
) -> str:
    """
    把一个 SDK 异常翻成「一句中文说明 + 服务端原文」。

    :param exc: `stream_chat` 捕获到的异常
    :param model: 本次请求用的模型名（403/404 两类要把它说出来）
    :param base_url: 配置里的 API 地址（连接类要把主机名说出来）
    :param config_path: **实际生效的**配置文件路径。401 那条必须打印它——
                        `--config` 与用户级两条来源，用户常改错文件。
                        取不到时（未经 `load()` 造出来的 Config）省略该句。
    :returns: 给用户看的完整错误文本

    :raises: 不抛。本函数在错误处理路径上，抛出去等于把一次可解释的失败
             变成一次崩溃。

    副作用：无（纯函数）。
    """
    detail = _server_message(exc)
    where = f"（{config_path}）" if config_path else ""

    # ⚠ 分支顺序按「从具体到一般」。`APITimeoutError` 与 `APIConnectionError`
    # 是父子关系（前者继承后者），**超时必须排在连接错误之前**——反过来的话
    # 一次超时会被报成「连不上，请检查网络」，而网络明明是通的。
    if isinstance(exc, openai.AuthenticationError):
        head = f"API Key 无效或已过期。请检查配置文件{where}里的 api_key。"
    elif isinstance(exc, openai.PermissionDeniedError):
        head = (
            f"这个 Key 没有调用 `{model}` 的权限，或账户余额不足。"
            f"请检查账户余额，或换一个有权限的 Key 与模型。"
        )
    elif isinstance(exc, openai.NotFoundError):
        head = (
            f"模型 `{model}` 不存在。也请检查 base_url 是否多写或少写了 `/v1`"
            f"（当前是 {base_url}）。"
        )
    elif isinstance(exc, openai.RateLimitError):
        # SDK 已经替我们重试过 3 次了（见模块 docstring 那段更正），
        # 所以走到这里意味着**重试也没救回来**，文案要说的是「等一会儿」。
        head = "已达到调用频率上限（已自动重试 3 次仍失败）。稍后再试。"
    elif isinstance(exc, openai.BadRequestError):
        # ⚠ **这里刻意不做「上下文超长」的二次分流。**
        #
        # R3 的错误分类表给了一条「命中长度类关键词 → 提示 /compact」的分支，
        # 但同时注明它**没有实测过**：触发它要真发一次超长请求（需有效凭据且
        # 花钱），而 DeepSeek 返回的 `body["message"]` 长什么样没人见过。
        # **不要凭 OpenAI 的文案去猜 DeepSeek 的措辞**——猜错的后果是一条
        # 「试试 /compact」的建议出现在一个与长度毫无关系的参数错误上，
        # 用户照做之后问题还在，而他已经不信这条提示了。
        #
        # 所以在有人拿真实超长请求量过之前，它落在这一支：原样给出服务端的
        # message，并明说这多半是个 bug。量过之后再加分支，届时把这段注释
        # 一起删掉。
        head = "请求被服务端拒绝。这多半是一个 bug，请带上下面这条信息报 issue。"
    elif isinstance(exc, openai.InternalServerError):
        head = "模型服务暂时不可用（已自动重试 3 次仍失败）。稍后再试。"
    elif isinstance(exc, openai.APITimeoutError):
        head = (
            "等待模型响应超时。网络不稳或响应过慢，可以直接重试；"
            "若经常发生，可在配置里调大 stream_idle_timeout。"
        )
    elif isinstance(exc, openai.APIConnectionError):
        head = (
            f"连不上 {_host_of(base_url)}。请检查网络、代理，"
            f"以及 base_url 是否写对。"
        )
    else:
        head = (
            f"与模型服务通信时发生未预期的错误（{type(exc).__name__}）。"
            f"请带上下面这条信息报 issue。"
        )

    # 说明是**前缀**不是替换：服务端原文一律附在后面。
    return f"{head}\n{detail}" if detail else head


def log_level_for(exc: Exception) -> int:
    """
    这一类错误该记到什么级别。

    :param exc: SDK 异常
    :returns: `logging` 的级别常量

    分级依据是**作者要不要看到它**，不是「有多严重」：
    - 400 与 5xx → WARNING（前者是唯一需要作者看到服务端原文的一类，
      后者说明对端出了问题）；
    - 其余全部 → INFO（401/404/429/超时/断网都是**用户侧**的情形，
      日志里记一行「发生过」就够，记成 WARNING 只会让真正的问题淹在里面）。

    副作用：无。
    """
    import logging

    if isinstance(exc, (openai.BadRequestError, openai.InternalServerError)):
        return logging.WARNING
    if isinstance(exc, openai.APIError):
        return logging.INFO
    # 非 SDK 异常 = 解析异常或 SDK 内部错，作者必须看到完整堆栈
    return logging.ERROR


def is_sdk_error(exc: Exception) -> bool:
    """
    是不是一个 SDK 已经分好类的错误。

    :param exc: 异常
    :returns: True 表示走上面那张表的九类之一；False 表示未预期

    用途：调用方据此决定要不要 `exc_info=True`（记完整堆栈）。
    未预期的那一类才需要堆栈——已分类的九类里，堆栈只是噪音。

    副作用：无。
    """
    return isinstance(exc, openai.APIError)


__all__ = ["classify", "log_level_for", "is_sdk_error"]


# ── 明确不做的两件事（R3 建议第一版就不做，理由记在这里免得下次再纠结）──
#
# ① **把重试从 SDK 接管过来。** 改成 `openai.OpenAI(max_retries=0)` + 自己在
#    `stream_chat` 外层重试，好处是能在重试之间发一条 NOTICE 事件，用户看得见
#    「正在重试（第 n/3 次）」——现在这 3 次尝试 + 退避在长响应时可能耗掉十几秒
#    而界面上什么都不显示。代价是要自己实现退避与 `Retry-After` 解析。
#    R3 的原话：不建议第一版就做，先把文案分类做掉，那是 90% 的体验收益。
# ② **让重试次数可配。** 同上，它依赖 ① 先落地。
