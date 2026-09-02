"""
模型知识的**唯一硬编码处**（first-run-setup 扩展 T3）。

⚠⚠ **这个文件会过期，而且一定会过期。** 它记的是「2026-09-02 那天，DeepSeek
官方文档上有哪些模型、各自窗口多大」。权威来源永远是**服务端**——向导第三屏
的正常路径是调 `probe.list_models` 现拉一份清单，只有拉不到（网络不通、地址填错、
端点变了）时才退回这里。

## 为什么要专门写这段声明

本项目刚在这件事上栽过一次，值得记全：

`config.py` 的模板里曾经写着 `model: deepseek-chat`。DeepSeek 于 2026-04-24
公告该别名 **2026-07-24 停用**，而模板一直没改——于是从那天起，任何一个新用户
装好、填上真 key、照模板跑，**第一句话就报模型不存在**。

它没被发现整整一个多月。原因不是没人用，而是**项目自己的真机验收从年中起
用的都是 `deepseek-v4-flash`**（见 `docs/e2e-sweep/`、各扩展的 acceptance 记录）
——实际在用的和写下来的分了家，两边各自都「对」，只是不是同一件事。

结论有两条，都落在本文件上：
1. **首选服务端**，把硬编码降级成兜底；
2. 兜底这份**自带过期声明与核实日期**，让下一个读到它的人知道该去哪儿核对。

## 核实记录

- **核实日期**：2026-09-02
- **出处**：DeepSeek 官方 API 文档（api-docs.deepseek.com）的模型与定价页
- **当时的事实**：在售 `deepseek-v4-flash` / `deepseek-v4-pro` /
  `deepseek-v4-flash-vision-exp`，三者上下文窗口均 1M、最大输出 384K；
  `deepseek-chat` / `deepseek-reasoner` 已停用且不再列出。

⚠ **成对维护点**：本文件的兜底清单 ↔ `config.DEFAULT_MODEL` ↔
`config.example.yaml`。三处都在声明「我们认为当前该用哪个模型」，
已经分家过一次了。护栏见 `tests/test_setup_catalog.py::PairedWithConfigTest`。
"""

from rhinecode.config import DEFAULT_CONTEXT_WINDOW, DEFAULT_MODEL
from rhinecode.setup.models import ModelOption

# 已知模型的上下文窗口（token）。
#
# ⚠ `deepseek-v4-flash-vision-exp` 在这张表里、但**不在下面的兜底清单里**，
# 这不矛盾：表回答「如果用了它，窗口该填多少」，清单回答「拉不到时推荐用什么」。
# 服务端返回它时我们照常列出来，那时就需要这张表给出正确的窗口值。
_WINDOWS: dict[str, int] = {
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4-flash-vision-exp": 1_000_000,
}

# 已知模型的一句话说明与是否推荐。
#
# 说明文字是给**第一次配置的人**看的，判据只有一条：他现在要做的决定是
# 「选哪个」，因此写的必须是**差别**（快/便宜 vs 更能想/更贵），
# 而不是模型的自我介绍。
_BLURBS: dict[str, tuple[str, bool]] = {
    "deepseek-v4-flash": ("日常编码，速度快、成本低", True),
    "deepseek-v4-pro": ("推理能力更强，速度与成本更高", False),
    "deepseek-v4-flash-vision-exp": ("实验性多模态；本项目只发送文本", False),
}

# 拉不到清单时的兜底候选。
#
# ⚠ **`deepseek-v4-flash-vision-exp` 刻意不进这份清单。** 它是实验性多模态
# 模型，而 RhineCode 只发送文本——把它摆在一个正在做首次配置的人面前，
# 只会制造一个他没有依据去做的选择。服务端返回它时照常显示（我们**不过滤**
# 服务端结果，过滤等于又一次把「我们认为有哪些模型」写死），但不主动推荐。
FALLBACK_OPTIONS: tuple[ModelOption, ...] = (
    ModelOption(
        model_id=DEFAULT_MODEL,
        blurb=_BLURBS[DEFAULT_MODEL][0],
        recommended=True,
    ),
    ModelOption(
        model_id="deepseek-v4-pro",
        blurb=_BLURBS["deepseek-v4-pro"][0],
        recommended=False,
    ),
)


def window_for(model_id: str) -> int:
    """
    按模型标识给出该写进配置的上下文窗口上限。

    :param model_id: 模型标识，可能是服务端返回的、我们没见过的名字
    :returns: 已知模型返回表里的值；**认不出时返回 `DEFAULT_CONTEXT_WINDOW`**

    ⚠ 认不出时**刻意不猜**。返回缺省值等价于「用户没写这一项」，
    是本项目在这个场景下唯一诚实的行为——猜一个更大的值会让 c8 一直不压缩、
    直到服务端报超长；猜一个更小的值则白丢上下文。

    无副作用。
    """
    return _WINDOWS.get(model_id, DEFAULT_CONTEXT_WINDOW)


def describe(model_id: str) -> tuple[str, bool]:
    """
    按模型标识给出一句话说明与「是否推荐」。

    :param model_id: 模型标识
    :returns: (说明文字, 是否推荐)。**认不出时返回 ("", False)**——
        服务端返回了我们不认识的新模型时照常列出来，只是没有说明文字，
        也不会被标成推荐。

    无副作用。
    """
    return _BLURBS.get(model_id, ("", False))


def options_from_ids(model_ids: "list[str] | tuple[str, ...]") -> tuple[ModelOption, ...]:
    """
    把服务端返回的一串模型标识包装成候选项，顺带补上说明与推荐标记。

    :param model_ids: 服务端 `/models` 返回的 id 序列，**原样不过滤**
    :returns: 候选项元组，顺序与入参一致

    ⚠ **顺序刻意保持服务端给的那个**，不按推荐与否重排：服务端的顺序本身
    可能有含义（新模型在前），而我们没有依据去否定它。推荐项靠界面上的标记
    体现，不靠排在第一个。

    无副作用。
    """
    result = []
    for model_id in model_ids:
        blurb, recommended = describe(model_id)
        result.append(ModelOption(model_id=model_id, blurb=blurb, recommended=recommended))
    return tuple(result)
