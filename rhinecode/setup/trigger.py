"""
向导的触发判定（first-run-setup 扩展 T4，spec F1）。

回答一个问题：**这次启动，该不该把人引到配置向导上？**

## 判据是「配置当前可不可用」，不是「跑过没跑过」

三种情形都属于「这份配置没法用」，因此都该引导：

| 情形 | 返回 | 用户视角 |
| --- | --- | --- |
| 文件不存在 | `MISSING` | 全新用户，刚装完 |
| `api_key` 还是占位符 | `PLACEHOLDER` | 跑过一次、看到提示，但没去填 |
| 缺必填字段 / YAML 解析不了 | `INVALID` | 手改配置改坏了 |

⚠ **刻意不落任何「已完成首次配置」的标记位。** 这一条是抄 Gemini CLI 的：
它看的是 `settings.json` 里 `selectedAuthType` 这个字段在不在，而不是记一个
「已完成 onboarding」的布尔。差别在**用户手工删掉 key 之后**——判据是状态时
向导会再来一次，判据是历史时它再也不出现，而那时用户手上是一个起不来的程序
和一句「请填入 api_key」。

## ⚠ 校验一律复用 `config.load`，绝不自己再写一遍

自己写一遍的代价很具体：**「向导认为配置可用」与「`load()` 认为配置可用」
会静默分家**。分家的两个方向都很难查——

- 向导更宽松 → 不弹向导，然后 `load()` 在下一行抛错退出，用户看到的是
  「程序启动就报错，而且从来没问过我什么」；
- 向导更严格 → 明明能用却每次都弹向导。

本模块因此只做一件事：**调用 `load()`，把它的异常翻译成一个枚举值。**
"""

from pathlib import Path
from typing import Optional

from rhinecode.config import PLACEHOLDER_API_KEY, load
from rhinecode.setup.models import TriggerReason


def classify(path: Path) -> Optional[TriggerReason]:
    """
    判断指定配置文件当前是否可用，不可用时给出原因。

    执行步骤：
    1. 文件不存在 → `MISSING`（**先判这条**：`load()` 对缺文件也会抛，
       但那样就分不清「还没配」和「配坏了」，而这两种情形给用户的第一屏
       文案完全不同）。
    2. 调 `config.load()`；抛 `FileNotFoundError` / `ValueError` → `INVALID`。
    3. 加载成功但 `api_key` 是模板占位符 → `PLACEHOLDER`。
    4. 以上都不是 → 返回 `None`，表示配置可用、**不该打扰用户**。

    :param path: 用户级配置文件路径（通常是 `config.user_config_path()`）
    :returns: 三个 `TriggerReason` 之一，或 `None`（配置可用）

    副作用：读一次文件。不写盘、不联网、不改变任何全局状态。
    """
    if not path.exists():
        return TriggerReason.MISSING

    try:
        cfg = load(str(path))
    except (FileNotFoundError, ValueError):
        # ⚠ 只接这两类，与 `__main__` 里那段现有的异常处理**同口径**。
        # 接得更宽（裸 `except Exception`）会把 PermissionError、磁盘错误这类
        # 「真的出问题了」的情形也吞成「配置无效」，于是用户拿到一个配置向导，
        # 填完还是起不来，而真正的原因一个字都没显示出来。
        #
        # ⚠ **`UnicodeDecodeError` 是个意外的成员，实测才发现：它是
        # `ValueError` 的子类**（`UnicodeDecodeError` → `UnicodeError` →
        # `ValueError`），所以「文件不是合法 UTF-8」这种情形**已经落在这一支里**，
        # 判成 `INVALID`。
        #
        # 这个结果本身是对的——一份读都读不出来的配置确实没法用，该引导。
        # 但它带来一个**必须在写盘那侧兑现的义务**：Windows 中文环境下一份
        # GBK 编码的 `config.yaml` 正是这个形态，而它的内容对用户是有价值的。
        # 因此 `writer.apply` **绝不覆盖一份读不出来的文件**，而是先改名备份
        # 再写新的（见 `setup/writer.py` 的同名说明）。两处缺一不可：
        # 只有这里判 INVALID 而写盘直接覆盖，等于把用户的配置悄悄删了。
        return TriggerReason.INVALID

    if cfg.api_key == PLACEHOLDER_API_KEY:
        return TriggerReason.PLACEHOLDER

    return None
