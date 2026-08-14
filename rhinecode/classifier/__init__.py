"""
分类器审查包（c16）：在权限管线第④层给三类动作接一次独立的模型判定。

## 一句话

`auto` 档下，跑命令、访问网络、给队友发消息这三类动作在执行前先问一个**独立的
分类器模型**「这该不该做」。代码守边界，模型判语义。

## 它在整条管线里的位置

    ⓪Hook → ①黑名单 → ②沙箱 → ②′网络 → ③规则 → 只读短路 → ④权限档兜底
                                                                    │
                                          结论来自④ 且 工具声明了类别 ？
                                                                    ↓
                                                            **本包**
                                                                    ↓
                                                    ②″保护路径（出口收紧器）

由此得到两条性质，两条都有护栏钉着：

- **用户写的 `deny` 规则压得过分类器**（③排在④之前，分类器说放行也没用）
- **用户写的 `allow` 规则短路分类器**，那次运行里分类器零次调用

## ⚠ 三条不变量

1. **本包是叶子**：只依赖 `provider.base`、`trace` 与标准库。
   **不 import `permission` / `agent` / `tools` / `tui`**——
   `import permission.models` 会连带执行 `permission/__init__.py`，
   把引擎与 `rhinecode.tools.path_guard` 一起拉起来。
   `broad.py` 的入参收成两个字符串而不是 `Rule`，正是为此。
2. **权限判定引擎一个字不改**。它的既有性质是「同样的输入必然得到同样的
   结果，不联网、不看时间、无副作用」，那是它能被彻底测透的原因；
   分类器要发网络请求，因此挂在**调用方**（`agent/loop.py` 的决策预扫）而不是
   塞进引擎。
3. **分类器只在④层那一格生效**，其余全部请求逐字不变。

## 模块地图

| 模块 | 管什么 |
| --- | --- |
| `models` | 值对象、三类 scope 常量、`ClassifierProtocol` |
| `prompt` | 分类器能看到什么、按什么顺序排、怎么无害化 |
| `parse` | 两阶段输出解析（两个阶段的失败语义**刻意不同**） |
| `breaker` | 两种熔断（拦得太多 / 连不上），加锁 |
| `cache` | 网络判定缓存（放行与拒绝的有效期不同） |
| `broad` | 宽泛放行规则识别（它们会静默关掉整层） |
| `render` | 全部文案（**给模型的与给用户的是两样东西**） |
| `service` | 门面：两阶段调用，会话共享、线程安全 |
| `session` | 每次运行一个：绑转录来源与缓存，不加锁 |

四份设计文档在 `docs/c16/`。
"""

from rhinecode.classifier.models import (
    SCOPE_COMMAND,
    SCOPE_MESSAGE,
    SCOPE_URL,
    BreakerReason,
    BreakerState,
    ClassifierConfig,
    ClassifierProtocol,
    RecordedCall,
    ReviewAction,
    Transcript,
    Verdict,
    VerdictKind,
)
from rhinecode.classifier.broad import is_broad_command_allow, why_broad
from rhinecode.classifier.service import ClassifierService
from rhinecode.classifier.session import ReviewSession

__all__ = [
    "SCOPE_COMMAND",
    "SCOPE_MESSAGE",
    "SCOPE_URL",
    "BreakerReason",
    "BreakerState",
    "ClassifierConfig",
    "ClassifierProtocol",
    "ClassifierService",
    "RecordedCall",
    "ReviewAction",
    "ReviewSession",
    "Transcript",
    "Verdict",
    "VerdictKind",
    "is_broad_command_allow",
    "why_broad",
]
