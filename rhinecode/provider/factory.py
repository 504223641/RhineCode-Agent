"""
Provider 工厂模块。

根据配置文件中的 protocol 字段，返回对应的 Provider 实例。
新增 Provider 时只需在此处添加一个 elif 分支，无需改动其他模块。

当前支持的 protocol：
- anthropic：Anthropic Claude，支持 Extended Thinking
- openai：OpenAI，流式对话，thinking 参数无效
- deepseek：DeepSeek，OpenAI 兼容协议，thinking 参数无效

## ⚠ 三个 Provider 一律在分支内部 import，别挪回模块顶层

Python 的 `import` 是**执行整个模块**，不是「登记一个名字」。因此顶层写
`from rhinecode.provider.anthropic import AnthropicProvider`，会让 `anthropic`
这个 SDK 的全部类型定义**真的被执行一遍**——哪怕本次运行根本不会创建它。

实测（本机 Python 3.11，各取 5 次最小值）：

    裸解释器                                39 ms
    import openai（deepseek 必需）         601 ms
    import anthropic（多数运行用不上）      577 ms
    两个都 import（本次改动前的写法）       877 ms
    只 import deepseek 需要的              612 ms
                                          ────────
                                       省  290 ms

省下的是**增量**而不是 anthropic 的全部 577 ms——两个 SDK 共享 httpx / pydantic，
先导的那个已经把公共部分付掉了。`openai` 则省不掉：`deepseek.py` 走的就是
OpenAI 兼容协议，主力 Provider 真的要用它。

这 290 ms 出现在**每一次进程启动**上，收益有两处：
1. 用户每次敲 `rhine` 少等 290 ms；
2. 测试套件里约 44 处真起子进程的地方（e2e 宿主 28 次、装配层子进程用例等）
   各省一份，合计约 12 秒。

代价只有一处：配了 `protocol: anthropic` 却没装对应 SDK 时，`ImportError` 从
「启动时」推迟到「创建 Provider 时」。这反而更合理——用不到的 Provider
缺依赖不该拦住启动。

⚠ 本项目的方针是「只针对 DeepSeek 开发」（Anthropic / OpenAI Provider 保持
纯对话能力），所以 anthropic 那条分支在实际使用中几乎不会被走到，
这正是延迟导入在这里收益最大的原因。
"""

from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider


def create_provider(config: Config) -> BaseProvider:
    """
    根据 config.protocol 创建并返回对应的 Provider 实例。

    :param config: 包含 protocol、model、base_url、api_key 的配置对象
    :returns: 实现了 BaseProvider 接口的 Provider 实例
    :raises ValueError: protocol 不在支持列表中时抛出，错误信息包含实际值
    :raises ImportError: 对应 Provider 的第三方 SDK 未安装时抛出
        （本次改动把它从「import 本模块时」推迟到了这里，见模块 docstring）

    副作用：首次为某个 protocol 调用时会 import 对应的 SDK（几百毫秒）；
    之后 Python 的模块缓存会让重复调用不再付这个代价。
    """
    if config.protocol == "anthropic":
        # 延迟 import：只有真的选了 anthropic 才付它 SDK 的加载成本（见模块 docstring）
        from rhinecode.provider.anthropic import AnthropicProvider
        return AnthropicProvider(config)
    elif config.protocol == "openai":
        from rhinecode.provider.openai import OpenAIProvider
        return OpenAIProvider(config)
    elif config.protocol == "deepseek":
        # DeepSeek 与 OpenAI API 完全兼容，通过 base_url 区分请求目标
        from rhinecode.provider.deepseek import DeepSeekProvider
        return DeepSeekProvider(config)
    else:
        raise ValueError(f"无效供应商 {config.protocol}，目前仅支持 anthropic / openai / deepseek")
