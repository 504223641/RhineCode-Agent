"""
Memory 层（c9）：记忆系统的独立包。

对标 permission / mcp / context 的既有模式——纯逻辑模块在下、唯一的编排者
MemoryManager 在上、通过少量单点接入挂进现有流程：

- lockfile.py      锁原语：原子创建 / 释放 / 心跳 / 过期判定（跨进程互斥的最小机制）
- instructions.py  RHINE.md 三层加载 + @include 展开（纯函数）
- session.py       SessionStore：JSONL 会话存档的建档 / 追加 / 扫描 / 载入 / 清理 / 会话锁
- memories.py      记忆 frontmatter 与索引的解析 / 渲染 / 截断（纯逻辑，零 IO 决策）
- memory_updater.py 记忆 LLM 的 Prompt 与响应解析（只产出结构化动作，不写盘）
- manager.py       MemoryManager：唯一持 provider 引用与副作用编排
"""

from rhinecode.memory.manager import MemoryManager

__all__ = ["MemoryManager"]
