"""
日志设施（C5）：`--log-file` 的最小实现。

## 这个模块解决什么问题

在它之前，产品代码里 `import logging` 只有一处（`commands/dispatcher.py`），
而全仓**没有 `basicConfig`、没有任何 handler**——那条 `_logger.debug` 写了也
落不到任何地方。所以这不是「日志少」，是「日志系统不存在」：用户报「它卡住了」
时，唯一的排查手段是让他开 `--trace`，而 trace 产物含完整对话原文与工具输出
（模型读过配置文件时还可能含**明文 API Key**），代价大得不成比例。

日志与 trace 是**两件不同的东西**，本模块刻意保持它们分开：

| | trace（`--trace`） | 日志（`--log-file`） |
| --- | --- | --- |
| 记什么 | 完整请求/响应/工具输出**原文** | 「发生了什么」的一行摘要 |
| 敏感度 | 与会话存档同级（可能含明文 key） | 低（见下面的纪律） |
| 用途 | 验收行为、复盘一次运行 | 排查「它卡在哪一步了」 |
| 缺省 | 关闭 | 关闭 |

## ⚠ handler 只能写文件，绝不能写 stderr

Textual 全程持有终端（备用屏幕缓冲 + raw mode）。往 stderr 挂一个
`StreamHandler` 会让日志行直接画在界面上、把布局搅烂，而且**看起来像界面出了
bug**。`logging.basicConfig()` 的缺省行为正是挂 stderr，所以本模块**不用它**，
而是自己装一个 `FileHandler`。

## 三条纪律

1. **不记密钥、不记内容。** 日志行只写「哪一步、什么结果、耗时多少」，
   绝不写工具输出原文、文件内容、消息正文或 `api_key`。要看内容请用 trace。
2. **失败一律静默降级。** 路径不可写时打印一行提示后照常启动——观测设施绝不
   能反过来阻断被观测的系统（与 `trace/recorder.py` 的 `create_recorder` 同一
   条纪律）。
3. **进程级单例。** logging 本来就是进程全局的，因此本模块用模块级状态而不是
   给 `build_app` 加参数——后者会新增一处成对维护点（`bootstrap.py` ↔
   `tests/e2e/host.py`），而那处维护点买不到任何东西。

用法：

    path = configure(default_log_path(root))   # 或 configure(Path("x.log"))
    if path is None:
        ...  # 没开日志，或开失败了（已经提示过用户）
"""

import datetime
import logging
from pathlib import Path
from typing import Optional

# 本次运行实际写入的日志文件；未开启（或开启失败）时为 None。
# 模块级状态是刻意的：logging 本身就是进程全局的，`--log-file` 也是进程级开关。
_log_path: Optional[Path] = None

# 日志行格式。带毫秒是为了排查「卡在哪一步」——秒级精度看不出一次调用花了多久。
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def default_log_path(project_root: Path) -> Path:
    """
    计算 `--log-file` 不带参数时的缺省路径。

    :param project_root: 项目根目录（启动时的当前工作目录）
    :returns: `<项目根>/.rhinecode/logs/<时间戳>.log`

    副作用：**不创建任何目录**。建目录是 `configure` 的事（它才知道要不要真写），
    这里保持纯计算，便于测试直接断言路径形态而不留下垃圾目录。

    时间戳含毫秒的理由与 `trace.default_trace_path` 完全相同：秒级时间戳在同一秒
    内启动两次会算出同一个路径，两次运行的日志互相穿插，读的人分不开。
    """
    now = datetime.datetime.now()
    stamp = f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}"
    return project_root / ".rhinecode" / "logs" / f"{stamp}.log"


def configure(path: Optional[Path], level: int = logging.INFO) -> Optional[Path]:
    """
    装上一个只写文件的日志 handler。

    :param path: 日志文件路径；None 表示不开启日志（直接返回 None，什么都不做）
    :param level: 根 logger 的级别，缺省 INFO
    :returns: 实际写入的路径；未开启或开启失败时返回 None

    执行步骤：
    1. path 为 None → 直接返回（`--log-file` 没写，这是缺省情形）
    2. 建父目录 → 造 `FileHandler` → 挂到根 logger 上
    3. 任一步抛 OSError → 打印一行提示、返回 None，**不抛出**

    副作用：创建日志目录与文件、修改根 logger 的 handlers 与 level、
    写入模块级 `_log_path`。

    ⚠ **只挂 FileHandler，不碰 stderr**：Textual 持有终端，往 stderr 写日志会把
    界面搅烂。也因此不能用 `logging.basicConfig()`——它的缺省行为就是挂 stderr。
    """
    global _log_path

    if path is None:
        return None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as e:
        # fail-safe：日志开不起来不是启动失败。提示一行让用户知道「你要的日志没写成」
        # ——静默失败会让他一边以为在记录、一边拿不到任何东西。
        print(f"无法写入日志文件 {path}：{e}（本次运行不记录日志）")
        return None

    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    _log_path = path
    logging.getLogger(__name__).info("日志开始记录：%s", path)
    return path


def log_path() -> Optional[Path]:
    """
    本次运行的日志文件路径；未开启日志时为 None。

    崩溃处理器（C6 缺口二）用它决定「完整堆栈见 xxx」那句话里写哪个路径，
    所以它必须是**读得到的进程级状态**而不是某个对象的字段——崩溃可能发生在
    任何一层，那里未必拿得到 app 或 config。
    """
    return _log_path


def reset_for_test() -> None:
    """
    摘掉本模块装上的 handler 并清空状态，**仅供测试使用**。

    不提供这个的话，一条用例装的 FileHandler 会留在根 logger 上，
    后面每条用例的日志都往那个（可能已被删掉的临时）文件里写。
    """
    global _log_path

    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            root.removeHandler(handler)
            handler.close()
    _log_path = None


__all__ = ["configure", "default_log_path", "log_path", "reset_for_test"]
