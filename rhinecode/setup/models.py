"""
首次启动配置向导的数据结构（first-run-setup 扩展 T2）。

本模块是 `setup` 包依赖方向的最底层：**只定义数据类与枚举**，不做 IO、
不联网、不 import 任何界面模块。`trigger` / `catalog` / `probe` / `writer`
四个模块都向上依赖它。

## 为什么全部用 frozen dataclass

向导是一个「四屏各填一段、最后一次性写盘」的流程。用可变对象的话，
「第三屏改了草稿、第四屏又改回去」这类问题只能靠通读全流程才看得出来；
每屏产出一个**新的**草稿，则每一步的输入输出都是显式的。

代价是每次改一个字段都要 `dataclasses.replace`，那是刻意付的。
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class TriggerReason(Enum):
    """
    向导被触发的原因（spec F1）。

    三种情形都属于「当前这份配置没法用」，因此都该把人引到向导上：

    - MISSING：用户级配置文件根本不存在（全新用户）
    - PLACEHOLDER：文件在，但 `api_key` 还是模板里的占位符
      （用户跑过一次、看到提示、但没去填）
    - INVALID：文件在，但缺必填字段或 YAML 解析不了

    ⚠ **判据刻意是「配置当前可不可用」，不是「跑过没跑过」**——不落任何
    「已完成首次配置」的标记位。有标记位的话，用户手工删掉 key 之后就再也
    引导不出来了（这是 Gemini CLI 的同款判据：看的是 settings.json 里那个
    字段在不在，而不是记一个「已完成 onboarding」的布尔）。
    """

    MISSING = "missing"
    PLACEHOLDER = "placeholder"
    INVALID = "invalid"


class SetupMode(Enum):
    """
    向导的两种进入方式。

    - FIRST_RUN：启动期，配置不可用，由最小宿主 App 推起来
    - RERUN：运行期，用户敲了 `/setup`，由 RhineApp 推起来

    ⚠ **它只影响文案与预填，不影响流程**——四屏的顺序、能不能放弃、
    校验失败有几个出口，两种模式下逐字相同。做成「重跑时少几屏」会让
    两条路分家，而分家的那一半迟早只有一条被维护到。
    """

    FIRST_RUN = "first_run"
    RERUN = "rerun"


class SetupAction(Enum):
    """
    向导结束时发生了什么。

    - SAVED：用户确认并已落盘（含「校验失败但仍然保存」那条路）
    - ABANDONED：用户放弃，**没有写任何配置**（spec F3/F18）
    """

    SAVED = "saved"
    ABANDONED = "abandoned"


class ProbeFailure(Enum):
    """
    终验失败的分类（spec F10：不许一句「请求失败」打发）。

    分四类是因为**用户接下来该做的事完全不同**：

    - AUTH：凭据无效 → 回去重填 key
    - NETWORK：连不上 → 检查网络或改接口地址
    - MODEL：模型不可用 → 回去换一个模型
    - OTHER：其它（限流、服务端 5xx、解析不了的响应）→ 多半稍后再试

    ⚠ 合并成一类的代价很具体：一个填错 key 的人会去检查网络，
    一个网络不通的人会去重填 key，两边都在错误的方向上耗时间。
    """

    AUTH = "auth"
    NETWORK = "network"
    MODEL = "model"
    OTHER = "other"


@dataclass(frozen=True)
class SetupDraft:
    """
    向导的草稿——四屏共同填写的那份东西，最后由 writer 落盘。

    字段：

    - api_key：API 密钥。**空串表示「不改」**（spec F16），这是 `/setup`
      重跑时的语义：用户不必为了换个模型而把密钥重新粘一遍。
      ⚠ 写盘时必须据此**整个跳过 `api_key` 这一键**，而不是写一个空值——
      写空值会静默清空用户的密钥，且要到下次启动才发现。
    - base_url：接口地址。走代理或私有部署时才需要改。
    - model：模型标识，第三屏选定或手动输入。
    - context_window：上下文窗口上限（token），由 `catalog.window_for(model)`
      按所选模型推出。显式写进配置而不是依赖缺省值，是为了让用户**看得见**
      这个数字，将来换模型时知道有这么一项要跟着改。
    """

    api_key: str
    base_url: str
    model: str
    context_window: int


@dataclass(frozen=True)
class ModelOption:
    """
    第三屏的一个候选模型。

    字段：

    - model_id：模型标识，直接写进配置的那个字符串
    - blurb：一句话说明（「日常写代码，快、便宜」）。**服务端不提供这个**，
      由 `catalog.describe` 按已知模型补；认不出的模型这里是空串——
      服务端返回了我们不认识的新模型时照常列出来，只是没有说明文字。
    - recommended：是否标为推荐项
    """

    model_id: str
    blurb: str
    recommended: bool


@dataclass(frozen=True)
class ModelListResult:
    """
    第三屏拉取模型清单的结果。

    ⚠ **它必须能表达「这是兜底」**（spec F9）：静默退回内置清单等于把
    「清单会过期」这个问题原样搬回来了，还多骗用户一次。

    字段：

    - options：候选项，来自服务端或内置兜底
    - from_fallback：真 = 服务端没拉到，用的是内置清单
    - error：拉取失败的可读原因；只在 `from_fallback` 为真时有值
    """

    options: tuple[ModelOption, ...]
    from_fallback: bool
    error: Optional[str] = None


@dataclass(frozen=True)
class ProbeResult:
    """
    第四屏终验的结果。

    字段：

    - ok：通过与否
    - kind：失败分类；`ok` 为真时是 None
    - detail：给**用户**看的一句话。
      ⚠ **绝不是 `str(exception)` 的原样转发**——SDK 异常有时会把请求头或
      完整 URL 带进消息里，而请求头里有 `Authorization: Bearer <key>`
      （spec F14 的落点之一）。四类失败各自组织措辞。
    - elapsed_ms：耗时，成功时显示给用户看（「连上了，0.8 秒」比单纯一个
      对勾更有说服力）
    """

    ok: bool
    kind: Optional[ProbeFailure]
    detail: str
    elapsed_ms: int


@dataclass(frozen=True)
class SetupOutcome:
    """
    向导结束时交给调用方的东西。

    字段：

    - action：SAVED / ABANDONED
    - written：实际写了哪些文件。ABANDONED 时必为空元组。
      第四屏成功页要把它列出来给用户看（spec F10），调用方也据此判断
      「到底有没有落盘」——比让调用方自己去 stat 文件可靠。
    """

    action: SetupAction
    written: tuple[Path, ...] = ()
