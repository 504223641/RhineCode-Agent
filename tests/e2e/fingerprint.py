"""
产品代码的版本标识（spec F9）。

## 它解决什么问题

宿主是**常驻**进程：起一次，然后驱动几十次交互。这意味着它跑的是**启动那一刻**的
代码。你改完 `rhinecode/` 里的某个文件，宿主对此一无所知——它还在跑旧代码。

于是最坏的失败形态出现了：你改了代码、跑了驱动、看到「通过」，就以为改动生效了；
其实那次通过验的是改动之前的行为。**这个误判是本模块存在的全部理由。**

指纹随 `status` 一起返回。做法很简单：改完代码后看一眼指纹变没变，变了说明该重启宿主。
（本设施刻意**不做热重载**——热重载会带来「一半新代码一半旧代码」这种更难判断的状态。）

## 算法与已知代价

遍历 `rhinecode/**/*.py`（跳过 `__pycache__`），按相对路径排序，
把 `(相对 POSIX 路径, 字节数, mtime_ns)` 逐个喂进 sha256，取 hexdigest 前 12 位。

**用 mtime 而不是文件内容**：快得多，而「改了就变」是本条唯一需要保证的性质。

**已知误报（不回避）**：`pip install -e .`、`git checkout` 切分支、某些编辑器的保存
都会改 mtime 而内容并未变，于是会**误报「代码变了」**。

之所以仍然选它：**误报只让人多重启一次宿主，漏报会让人以为改动生效了其实没有**。
后者正是本模块要防的那件事，两种错误的代价完全不对称。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Union


def compute(package_root: Union[str, Path]) -> str:
    """
    算出一个包目录的代码版本标识。

    :param package_root: 产品包根目录（通常是 `rhinecode/`）
    :returns: sha256 十六进制摘要的前 12 位；目录不存在时返回 `"unknown"`
        （宿主不该因为算不出指纹而起不来——它是排障辅助，不是功能依赖）

    副作用：无（只 stat 文件，不读内容、不写任何东西）。
    """
    root = Path(package_root)
    if not root.is_dir():
        return "unknown"

    digest = hashlib.sha256()
    # 排序保证同一份代码在任何机器、任何遍历顺序下都得到同一个值
    for path in sorted(root.rglob("*.py"), key=lambda p: p.relative_to(root).as_posix()):
        if "__pycache__" in path.parts:
            continue
        try:
            st = path.stat()
        except OSError:
            # 遍历途中文件被删（比如正在切分支）——跳过即可，指纹本就只需「变了能看出来」
            continue
        rel = path.relative_to(root).as_posix()
        digest.update(f"{rel}:{st.st_size}:{st.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()[:12]
