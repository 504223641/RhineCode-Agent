"""
缓存命中调试日志（c5 F10 / N6）。

把每次模型请求的缓存命中/未命中 token 写入一个调试文件，用于验证「稳定系统提示走可缓存通道」
的策略是否真的生效：对同一稳定前缀连续请求，第二次的 hit token 应明显高于第一次。

设计取舍：
- 不污染 TUI（项目此前已移除界面上的 Token 显示），只追加到独立文件，验收时 tail 查看。
- 安全降级（N6）：日志只是「锦上添花」的观测手段，任何 IO 异常都不应影响主流程，
  因此整体包 try/except 并静默吞掉——宁可少一条日志，也不能让写日志拖垮一次正常对话。
"""

import datetime
from typing import Any


def log_cache_usage(usage: Any, model: str, path: str) -> None:
    """
    向调试日志文件追加一行缓存用量记录。

    记录格式：`<ISO 时间戳> | model=<模型> | prompt=<N> hit=<N> miss=<N>`

    :param usage: 一轮请求的用量对象（agent.events.Usage），需含 prompt_tokens 与
                  prompt_cache_hit_tokens / prompt_cache_miss_tokens 字段
    :param model: 当前模型名，写入日志便于区分
    :param path: 调试日志文件路径（由上层给出，通常为 <项目根>/.rhinecode_debug.log）

    副作用：以追加模式写入 path 指向的文件；IO/属性异常一律静默降级，不抛给调用方。
    """
    try:
        timestamp = datetime.datetime.now().isoformat(timespec="seconds")
        line = (
            f"{timestamp} | model={model} | "
            f"prompt={getattr(usage, 'prompt_tokens', 0)} "
            f"hit={getattr(usage, 'prompt_cache_hit_tokens', 0)} "
            f"miss={getattr(usage, 'prompt_cache_miss_tokens', 0)}\n"
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # 写日志失败不影响主流程：宁可丢一条观测记录，也不让它干扰正常对话（N6）。
        pass
