"""
向导的两次网络请求（first-run-setup 扩展 T7，spec F8/F9/F10/F14）。

第三屏拉一份**当前可用的模型清单**，第四屏用选定的模型发一次**最小请求**
确认真的能连上。两者都不抛异常——它们服务的是一个界面流程，
「连不上」在这里是一种**要展示给用户的结果**，不是内部错误。

## ⚠ 为什么自己造 SDK 客户端，而不复用 `provider.create_provider`

**先有鸡还是先有蛋。** `create_provider(cfg)` 需要一个已经构造好的 `Config`，
而配置向导跑起来的时候，那份配置恰恰**还不存在**——它正是向导要产出的东西。

顺带两者要的也不是一回事：

| | `provider/deepseek.py` | 本模块 |
| --- | --- | --- |
| 形态 | 流式 | **非流式**（要的是「通不通」，不是逐块渲染） |
| 超时 | 块间空闲 90s / 连接 10s | **短**（用户正盯着一个转圈等结果） |
| 失败 | 交给 Agent 循环处理 | **翻译成给人看的一句话** |

⚠ **这是一处成对维护点**：两边构造客户端的方式刻意不同，但 `base_url`
的语义与鉴权头的形式**必须一致**——那边能连上而这边连不上（或反过来）
会让向导的结论骗人：校验通过了，进主界面第一句话却报错。

## ⚠ 密钥不外泄（spec F14 落点之一）

SDK 异常的字符串**可能带上请求 URL、响应体、甚至请求头**。因此：

1. 四类失败**各自组织措辞**，不把 `str(exception)` 原样转发；
2. 确实要附上服务端说法时，先过 `_redact` —— 它按**我们手上那个 key 的字面量**
   和一个通用的 key 形态正则各洗一遍；
3. 附加的说明**截断到一行**，免得一个 HTML 错误页把界面撑爆。
"""

import re
import time
from typing import Any, Callable, Optional

from rhinecode.setup import catalog
from rhinecode.setup.models import (
    ModelListResult,
    ProbeFailure,
    ProbeResult,
)

# 附在措辞后面的服务端说法最多留这么长。
# 一个 HTML 错误页有几十 KB，原样塞进 Textual 的 Static 里既没用又难看。
_DETAIL_LIMIT = 200

# 通用的「看起来像密钥」形态。刻意宽松：宁可多打码，也不要漏。
_KEY_LIKE = re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}", re.IGNORECASE)

# 终验用的最小请求。内容无所谓，要的只是一次真实往返。
_PROBE_MESSAGES = [{"role": "user", "content": "hi"}]


def _redact(text: str, api_key: str) -> str:
    """
    把一段文本里可能出现的密钥打码。

    :param text: 原始文本（多半来自 SDK 异常）
    :param api_key: 本次用的密钥，按字面量整串替换
    :returns: 打码后的文本

    ⚠ **两道都要**：按字面量替换治的是「异常里原样带出了我们发出去的 key」，
    正则治的是「异常里带出了**别的** key 形态的东西」（比如服务端把请求体
    回显了一部分）。只做前者的话，一个我们没预料到的来源就漏出去了。

    无副作用。
    """
    cleaned = text
    if api_key:
        cleaned = cleaned.replace(api_key, "***")
    return _KEY_LIKE.sub("sk-***", cleaned)


def _one_line(text: str, api_key: str) -> str:
    """
    把服务端说法压成安全、简短的一行。

    :param text: 原始文本
    :param api_key: 用于打码的密钥
    :returns: 打码、去换行、截断后的一行文本

    无副作用。
    """
    cleaned = _redact(text, api_key).replace("\r", " ").replace("\n", " ").strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if len(cleaned) > _DETAIL_LIMIT:
        cleaned = cleaned[:_DETAIL_LIMIT] + "…"
    return cleaned


def _status_code(exc: BaseException) -> Optional[int]:
    """
    尽量取出 HTTP 状态码。

    :param exc: SDK 异常
    :returns: 状态码；取不到返回 None

    SDK 的 `APIStatusError` 带 `status_code`，但这个属性不在类上、只在实例上，
    所以用 `getattr` 而不是 `isinstance` 之后直接取。

    无副作用。
    """
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _classify(exc: BaseException, api_key: str) -> tuple[ProbeFailure, str]:
    """
    把一个异常翻译成「失败分类 + 给用户看的一句话」。

    :param exc: SDK 抛出的异常
    :param api_key: 用于打码
    :returns: (分类, 措辞)

    ⚠ **分四类是因为用户接下来该做的事完全不同**：凭据无效要回去重填 key，
    连不上要检查网络或改地址，模型不可用要换个模型。合并成一句
    「请求失败」的代价很具体：填错 key 的人会去检查网络，网络不通的人会去
    重填 key，两边都在错的方向上耗时间。

    ⚠ **措辞由本函数组织，`str(exc)` 只作为「服务端说：…」附在后面，
    且必过 `_redact`**（spec F14）。

    无副作用。
    """
    # 延迟 import：本模块被 `setup/__init__.py` 的门面拉起来时，
    # 不该顺带把 openai SDK 的 import 成本（实测 ~600ms）也付掉。
    import openai

    # `said` **只用于内部分流，绝不显示给用户**（见下面 400 那一支）。
    said = _one_line(str(exc), api_key)

    # ⚠ **给用户的每一句话都是本模块写死的固定文案，不含任何服务端字符串。**
    # 这是真机反馈定下来的（「文案太复杂了」）：英文报错对多数用户没有意义，
    # 而它挤在中文句子里只会让人更慌。
    #
    # 顺带它让 spec F14「密钥不外泄」从「靠打码」升级成**结构性成立**——
    # 服务端说法根本不进 `detail`，也就没有泄漏的载体。护栏见
    # `tests/test_setup_probe.py::DetailIsAlwaysOurOwnWordsTest`。

    # 非 ASCII 的密钥要单独说，这是真机撞出来的。
    #
    # HTTP 头只能是 ASCII，而 `Authorization: Bearer <key>` 里一旦有中文或
    # 全角符号，httpx 在**发出去之前**就抛 `UnicodeEncodeError`。原始消息是
    # 「'ascii' codec can't encode character ... in position 7」——
    # `"Bearer "` 正好 7 个字符，所以位置 7 就是密钥的第一个字。
    #
    # 用户看到那句话完全不知道该干什么，而真实成因往往很具体：**从控制台
    # 网页上复制时，复制到的是打码后的那串圆点**。这一支排在最前面，
    # 因为它既不是网络问题也不是凭据无效，归到 OTHER 给出的建议全是错的。
    if isinstance(exc, UnicodeEncodeError):
        return (ProbeFailure.AUTH, "密钥里有非 ASCII 字符")

    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return (ProbeFailure.NETWORK, "连不上服务器")

    if isinstance(exc, openai.AuthenticationError):
        return (ProbeFailure.AUTH, "密钥无效或已失效")

    if isinstance(exc, openai.PermissionDeniedError):
        return (ProbeFailure.AUTH, "密钥没有访问权限")

    if isinstance(exc, openai.NotFoundError):
        return (ProbeFailure.MODEL, "服务端上没有这个模型")

    if isinstance(exc, openai.RateLimitError):
        return (ProbeFailure.OTHER, "被限流了，稍后再试")

    code = _status_code(exc)

    if code == 402:
        return (ProbeFailure.OTHER, "账户余额不足")

    if code == 400:
        # ⚠ 400 是个筐，模型名不对最常落在这里（DeepSeek 对未知模型返回的
        # 就是 400 而不是 404）。靠**服务端说法里有没有提到 model** 来分流——
        # 那句话只参与判断，不进 `detail`。猜错的代价只是措辞不够贴切，
        # 而不分流的代价是「模型填错了」永远被显示成「未知错误」。
        if "model" in said.lower():
            return (ProbeFailure.MODEL, "服务端不接受这个模型名")
        return (ProbeFailure.OTHER, "服务端拒绝了这次请求")

    if code is not None and code >= 500:
        return (ProbeFailure.OTHER, f"服务端出错了（HTTP {code}）")

    return (ProbeFailure.OTHER, "没能完成这次请求")


def _default_client_factory(api_key: str, base_url: str, timeout: float) -> Any:
    """
    构造一个指向 DeepSeek 的 OpenAI 兼容客户端。

    :param api_key: 密钥
    :param base_url: 接口地址
    :param timeout: 单次请求超时（秒）
    :returns: `openai.OpenAI` 实例

    ⚠ **成对维护点**：与 `provider/deepseek.py` 的客户端构造刻意不同
    （那边流式、长空闲超时；这边非流式、短超时），但 `base_url` 的语义
    与鉴权头的形式两边必须一致。不能复用 `create_provider` 的理由见模块 docstring。

    副作用：无（构造客户端不发请求）。
    """
    import openai

    return openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)


def list_models(
    api_key: str,
    base_url: str,
    timeout: float = 10.0,
    *,
    _client_factory: Optional[Callable[[str, str, float], Any]] = None,
) -> ModelListResult:
    """
    向服务端要一份当前可用的模型清单（spec F8/F9）。

    :param api_key: 密钥
    :param base_url: 接口地址
    :param timeout: 超时（秒）
    :param _client_factory: 仅供测试注入假客户端
    :returns: `ModelListResult`。拿到了就是服务端那份；**任何失败都退回
        内置兜底清单**并把可读原因放进 `error`，`from_fallback` 置真

    ⚠ **本函数不抛异常。** 拉不到清单是给用户看的一种流程分支
    （界面上要挂一条「这是兜底、可能已过期」的提示），不是内部错误。
    让它抛的话，第三屏就得在界面代码里再写一遍这套翻译。

    ⚠ **服务端返回的结果一律不过滤**——过滤等于又一次把「我们认为有哪些模型」
    写死，而那正是本扩展要治的病（见 `catalog.py` 开头那段）。

    副作用：一次 HTTP 请求。
    """
    factory = _client_factory or _default_client_factory
    try:
        client = factory(api_key, base_url, timeout)
        response = client.models.list()
        model_ids = [m.id for m in response.data]
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：这里必须吞
        _, detail = _classify(exc, api_key)
        return ModelListResult(
            options=catalog.FALLBACK_OPTIONS, from_fallback=True, error=detail
        )

    if not model_ids:
        # 连上了但一个模型都没有：形态上是成功、实质上没法用，同样退兜底。
        return ModelListResult(
            options=catalog.FALLBACK_OPTIONS,
            from_fallback=True,
            error="服务端返回的模型清单是空的。",
        )

    return ModelListResult(
        options=catalog.options_from_ids(model_ids), from_fallback=False, error=None
    )


def verify(
    api_key: str,
    base_url: str,
    model: str,
    timeout: float = 20.0,
    *,
    _client_factory: Optional[Callable[[str, str, float], Any]] = None,
) -> ProbeResult:
    """
    用选定的模型发一次最小请求，确认真的能用（spec F10）。

    :param api_key: 密钥
    :param base_url: 接口地址
    :param model: 选定的模型
    :param timeout: 超时（秒）
    :param _client_factory: 仅供测试注入假客户端
    :returns: `ProbeResult`，失败时带分类与可读措辞

    为什么不用 `/models` 那次请求代替本次：那个端点只验「密钥能不能通过鉴权」，
    验不到**这个模型此刻能不能真的跑起来**（余额、权限、模型是否下线）。
    向导的承诺是「进去就能用」，那就得真发一次。

    ⚠ `max_tokens=1` + 非流式：花费压到最低，同时拿到一次完整往返。

    ⚠ **本函数不抛异常**，理由同 `list_models`。

    副作用：一次 HTTP 请求，**会消耗极少量 token**（用户已在 spec 阶段知情）。
    """
    factory = _client_factory or _default_client_factory
    started = time.monotonic()
    try:
        client = factory(api_key, base_url, timeout)
        client.chat.completions.create(
            model=model,
            messages=_PROBE_MESSAGES,
            max_tokens=1,
            stream=False,
        )
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：这里必须吞
        kind, detail = _classify(exc, api_key)
        return ProbeResult(
            ok=False,
            kind=kind,
            detail=detail,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    return ProbeResult(
        ok=True,
        kind=None,
        detail="连上了。",
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
