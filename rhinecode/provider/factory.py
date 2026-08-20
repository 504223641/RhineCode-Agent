"""
Provider 工厂模块。

根据配置文件中的 protocol 字段，返回对应的 Provider 实例。

## 本项目只保留 DeepSeek 一个 Provider

早期版本同时实现了 anthropic / openai / deepseek 三个 Provider，但后两者
**一直停留在纯对话能力**——工具调用、Plan Mode、权限系统、Skill、子 Agent
等等全部只在 `protocol: deepseek` 下可用。三份实现里有两份没人用，却要跟着
每次协议改动一起维护，还把 anthropic SDK 拖进了每次启动的 import 链
（实测 492ms，见下文「为什么仍然延迟 import」）。

2026-08-20 起两者已删除。`protocol` 字段本身**刻意保留**：

- 它不是「支持几个 Provider」的开关，而是「当前用的什么协议」的标识，
  环境信息模块、状态栏、trace 的配置快照都在读它；
- 老配置里写着 `protocol: anthropic` 的用户升级上来时，应当拿到一句
  **说得清楚该怎么办**的错误，而不是一个 KeyError 或者静默按 deepseek 跑。

## 为什么 deepseek 这一支仍然延迟 import

`import` 在 Python 里是**执行整个模块**，不是「登记一个名字」。`deepseek.py`
顶层 `import openai`（DeepSeek 走的是 OpenAI 兼容协议，真的要用那个 SDK），
而那一句实测 601ms。

把它留在 `create_provider` 内部，意味着**只 import 本模块、不创建 Provider**
的进程完全不必付这笔钱。这不是假想场景：e2e 宿主的多数用例跑的是剧本
Provider，根本不走 `create_provider`——实测 `tests.test_e2e_host` 那 28 条
端到端用例因此从 44.7s 降到 28.7s。

    import rhinecode.provider.factory   902ms -> 68ms
    import rhinecode.bootstrap         1187ms -> 464ms

⚠ **别顺手把它挪回模块顶层**。看起来只是「早导晚导」的区别，实际是每一个
不创建 Provider 的进程都白等半秒。
"""

from rhinecode.config import Config
from rhinecode.provider.base import BaseProvider

# 已删除的 protocol → 给用户的迁移说明。
# 单独抽出来是为了让错误信息**说得出该怎么办**：只说「无效供应商」的话，
# 一个从旧版本升上来的用户只知道坏了，不知道是自己配错了还是程序坏了。
_REMOVED_PROTOCOLS = {
    "anthropic": "Anthropic Claude",
    "openai": "OpenAI",
}


def create_provider(config: Config) -> BaseProvider:
    """
    根据 config.protocol 创建并返回 Provider 实例。

    :param config: 包含 protocol、model、base_url、api_key 的配置对象
    :returns: 实现了 BaseProvider 接口的 Provider 实例
    :raises ValueError: protocol 不是 deepseek 时抛出。已删除的两个协议
        （anthropic / openai）有各自的迁移提示，其余值走通用分支
    :raises ImportError: openai SDK 未安装时抛出（延迟到这里才暴露，见模块 docstring）

    副作用：首次调用时会 import openai SDK（约 600ms）；之后 Python 的
    模块缓存会让重复调用不再付这个代价。
    """
    if config.protocol == "deepseek":
        # 延迟 import：只 import 本模块而不建 Provider 的进程不必付 SDK 的加载成本
        # （见模块 docstring「为什么 deepseek 这一支仍然延迟 import」）
        from rhinecode.provider.deepseek import DeepSeekProvider
        return DeepSeekProvider(config)

    if config.protocol in _REMOVED_PROTOCOLS:
        name = _REMOVED_PROTOCOLS[config.protocol]
        raise ValueError(
            f"{name}（protocol: {config.protocol}）的支持已于 2026-08-20 移除，"
            f"本项目现在只支持 DeepSeek。\n"
            f"请把配置文件里的这几行改成：\n"
            f"  protocol: deepseek\n"
            f"  model: deepseek-chat\n"
            f"  base_url: https://api.deepseek.com\n"
            f"  api_key: <你的 DeepSeek API Key>"
        )

    raise ValueError(f"无效供应商 {config.protocol}，目前仅支持 deepseek")
