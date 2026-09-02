"""
首次启动配置向导的纯逻辑包（first-run-setup 扩展）。

## 这个包是什么

把「首次启动该不该问、问什么、怎么验、怎么写盘」这套逻辑收在一处，
**完全不依赖界面框架**——每一件事都能在无终端、无网络的进程里单独测。
界面（`tui/setup_screen.py`）只负责画和收键。这是本项目 `todo/` /
`classifier/` 一贯的分法。

## 依赖清单

只依赖**标准库**、`rhinecode.config`、以及 `openai` SDK（DeepSeek 走的就是
OpenAI 兼容协议）。

⚠ **绝不 import `bootstrap` / `conversation` / `tui` / `provider`。**
本包跑在装配**之前**——`build_app` 要拿 `cfg.api_key` 去造 Provider，而本包
存在的理由恰恰是「那个 key 还没有」。反向依赖会成环。护栏见
`tests/test_setup_entry.py` 里那条 import 扫描。

⚠ **`openai` 的 import 刻意留在 `probe.py` 的函数体内。** 那一句实测约 600ms
（见 `provider/factory.py` 里那段同样的说明），而只是 `import rhinecode.setup`
的进程——比如只想判一下该不该弹向导——完全不必付这笔钱。

## 四件事

| 模块 | 回答什么 |
| --- | --- |
| `trigger` | 这次启动该不该弹向导 |
| `catalog` | 拉不到清单时有哪些模型可选、各自窗口多大（**唯一硬编码处**） |
| `probe` | 服务端现在有哪些模型、这套配置真的能用吗 |
| `writer` | 怎么把结果写进 YAML 而不毁掉注释和用户自己写的东西 |
"""

from rhinecode.setup.catalog import (
    FALLBACK_OPTIONS,
    describe,
    options_from_ids,
    window_for,
)
from rhinecode.setup.models import (
    ModelListResult,
    ModelOption,
    ProbeFailure,
    ProbeResult,
    SetupAction,
    SetupDraft,
    SetupMode,
    SetupOutcome,
    TriggerReason,
)
from rhinecode.setup.probe import list_models, verify
from rhinecode.setup.trigger import classify
from rhinecode.setup.writer import apply, read_current, set_scalar

__all__ = [
    # 数据结构
    "ModelListResult",
    "ModelOption",
    "ProbeFailure",
    "ProbeResult",
    "SetupAction",
    "SetupDraft",
    "SetupMode",
    "SetupOutcome",
    "TriggerReason",
    # 触发
    "classify",
    # 模型目录
    "FALLBACK_OPTIONS",
    "describe",
    "options_from_ids",
    "window_for",
    # 网络
    "list_models",
    "verify",
    # 写盘
    "apply",
    "read_current",
    "set_scalar",
]
