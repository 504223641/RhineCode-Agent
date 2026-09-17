"""
Context 层（c8）：在每次 API 请求前把对话历史压到 token 预算内。

对外只暴露 ContextManager（编排入口）与两个数据类型；内部模块（estimate/
summarize）是实现细节，上层不直接依赖。导出在 T8 完成后即为最终形态。
"""

from rhinecode.context.manager import ContextManager
from rhinecode.context.models import CompactionNotice, ContextStats

__all__ = ["ContextManager", "CompactionNotice", "ContextStats"]
