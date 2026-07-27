"""
宿主的**发布文件**：写入、扫描、解析，以及两级陈旧判定。

## 要解决的问题

外部驱动者每次操作都是一个独立进程，它启动时**只知道自己想连一个宿主，不知道宿主
监听在哪个端口**。而端口是系统随机分配的（`bind(("127.0.0.1", 0))`），宿主的工作区
也是它自己新建的随机临时目录。

于是需要一个**与工作区无关的固定可发现位置**放一张「名片」：

    <系统临时目录>/rhinecode-e2e/host-<pid>.json

把名片放进工作区是个死循环——要先找到工作区才能找到端口，可工作区路径本身就写在
名片里。文件名带 pid 是为了支持多个宿主并存（客户端用 `--pid` 指定）。

## 两级陈旧判定

宿主被强杀、崩溃或断电时，名片会留在原地不会自动消失。客户端拿着一张过期名片去连，
如果不做判定就会卡在连接超时上，最后报一个和真实原因毫不相干的错。两级判定是：

1. **连不上就是陈旧**（`ConnectionRefusedError` / 其它 `OSError`）：立即报错并给出
   清理指引，**不等待超时**（spec AC10）。端口只在宿主活着时被监听，连不上必然是没了。
2. **连上了还要校验 pid**：宿主退出后端口被别的进程复用是有可能的，这时能连上、
   但对面根本不是宿主。用响应里的 `pid` 与名片比对即可识破。

**刻意不做进程存活检测**（`os.kill(pid, 0)` / `tasklist` 之类）：跨平台写法各不相同、
Windows 上还要处理权限，而端口连通性已经完整覆盖了「宿主是否还在服务」这个问题——
一个活着但没在监听的进程，对驱动器来说和死了没有区别。
"""

from __future__ import annotations

import json
import socket
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


PUBLISH_DIR_NAME = "rhinecode-e2e"


@dataclass(frozen=True)
class HostInfo:
    """
    一份宿主名片。字段全部是「客户端在连上之前就需要知道」或「事后排障要用」的信息。

    :param pid: 宿主进程号，兼作文件名与 pid 校验的依据
    :param port: 控制通道监听的回环端口（系统分配）
    :param workspace: 宿主自建的临时工作区（项目根）
    :param user_dir: 宿主自建的临时用户级目录（隔离真实 ~/.rhinecode）
    :param trace_path: 行为记录文件路径，断言层据此读取事实
    :param fingerprint: 产品代码版本标识，用于判断「宿主是不是跑在旧代码上」
    :param mode: scripted / live
    :param started_at: 启动时刻（`time.time()`），排障时看宿主活了多久
    """

    pid: int
    port: int
    workspace: str
    user_dir: str
    trace_path: str
    fingerprint: str
    mode: str
    started_at: float


class StaleHostError(RuntimeError):
    """名片指向的宿主已不在（或端口被别的进程占用）。异常消息即最终用户可见文案。"""


def publish_dir() -> Path:
    """
    发布目录的位置。

    **刻意不在这里建目录**：建目录是副作用，放在 `publish` 里，
    使本函数成为一个可以被测试自由打桩（`mock.patch`）的纯查询。
    """
    return Path(tempfile.gettempdir()) / PUBLISH_DIR_NAME


def host_file(pid: int) -> Path:
    """某个 pid 对应的名片路径。"""
    return publish_dir() / f"host-{pid}.json"


def publish(info: HostInfo) -> Path:
    """
    写下一张名片。

    :returns: 写入的文件路径
    :raises OSError: 发布目录建不了、或文件写不了。**刻意不吞**——
        没有名片谁都找不到这个宿主，它继续跑下去毫无意义，
        应当让宿主在启动早期就以非零退出码死掉，而不是变成一个没人能连的僵尸。

    副作用：创建发布目录（若不存在）并写入 `host-<pid>.json`。
    """
    directory = publish_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"host-{info.pid}.json"
    path.write_text(json.dumps(asdict(info), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def unpublish(pid: int) -> bool:
    """
    删掉某个 pid 的名片。

    :returns: 真正删掉了返回 True；文件本就不存在返回 False（**幂等**）。

    幂等是必需的：宿主的清理路径与测试的 `addCleanup` 都会调它，
    正常退出时会被调用两次。
    """
    path = host_file(pid)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def list_hosts() -> list[HostInfo]:
    """
    扫出当前发布目录里的所有名片，按 pid 排序。

    **坏文件跳过、不报错**：名片可能是半截的（宿主写到一半被杀），
    也可能是别的东西留下的同名文件。`hosts` 子命令正是人在排障时用的，
    让它因为一个残留的破文件而崩掉，等于在最需要它的时候把它拿走。
    """
    directory = publish_dir()
    if not directory.is_dir():
        return []
    infos: list[HostInfo] = []
    for path in sorted(directory.glob("host-*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            infos.append(
                HostInfo(
                    pid=int(raw["pid"]),
                    port=int(raw["port"]),
                    workspace=str(raw["workspace"]),
                    user_dir=str(raw["user_dir"]),
                    trace_path=str(raw["trace_path"]),
                    fingerprint=str(raw["fingerprint"]),
                    mode=str(raw["mode"]),
                    started_at=float(raw["started_at"]),
                )
            )
        except (OSError, ValueError, KeyError, TypeError):
            # 半截 JSON、字段缺失、类型不对——一律跳过
            continue
    return sorted(infos, key=lambda i: i.pid)


def resolve_host(pid: Optional[int] = None) -> HostInfo:
    """
    定位要连的宿主。

    :param pid: 指定则精确取该 pid 的名片；不指定则要求当前**恰好只有一个**宿主
    :returns: 命中的名片
    :raises StaleHostError: 找不到、或有多个而没有指定 pid

    异常消息就是**最终用户看到的全部文案**，所以要写成「能照着做」的形式：
    说清现状（几个宿主）、以及下一步该敲什么命令。
    """
    infos = list_hosts()
    if pid is not None:
        for info in infos:
            if info.pid == pid:
                return info
        raise StaleHostError(
            f"找不到 pid={pid} 的宿主名片（发布目录 {publish_dir()}）。\n"
            f"用 `python -m tests.e2e.client hosts` 看看当前有哪些宿主。"
        )
    if not infos:
        raise StaleHostError(
            f"没有正在运行的宿主（发布目录 {publish_dir()} 下没有名片）。\n"
            f"先启动一个：`python -m tests.e2e.host --mode scripted --script <MOD:ATTR>`"
        )
    if len(infos) > 1:
        listing = "\n".join(f"  pid={i.pid} port={i.port} mode={i.mode} ws={i.workspace}" for i in infos)
        raise StaleHostError(
            f"有 {len(infos)} 个宿主正在运行，请用 --pid 指定其中一个：\n{listing}"
        )
    return infos[0]


def connect(info: HostInfo, timeout: float = 10.0) -> socket.socket:
    """
    连上名片指向的宿主。

    :param timeout: 连接与后续读写的超时（秒）
    :returns: 已连接的 socket，调用方负责关闭
    :raises StaleHostError: 连不上——**第一级陈旧判定**。这里**立即失败、不重试、
        不等待**（spec AC10 / N7）：端口只在宿主活着时被监听，连不上就是没了，
        等待只会把一个 0.001 秒能给出的准确结论拖成一个十几秒后的模糊超时。

    副作用：建立一条 TCP 连接。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", info.port))
    except OSError as e:
        # ConnectionRefusedError 是最常见的形态（端口没人监听），
        # 但 Windows 上也可能是别的 OSError 子类，一律按陈旧处理。
        sock.close()
        raise StaleHostError(
            f"宿主已不在：连不上 127.0.0.1:{info.port}（{type(e).__name__}: {e}）。\n"
            f"名片文件：{host_file(info.pid)}（pid={info.pid}）\n"
            f"用 `python -m tests.e2e.client hosts` 查看当前宿主，"
            f"确认该宿主确已退出后手工删除上面那个名片文件。"
        ) from e
    return sock


def verify_pid(info: HostInfo, status_data: dict) -> None:
    """
    **第二级陈旧判定**：连上了，但对面是不是我们要找的那个宿主？

    :param status_data: 一条 `status` 响应的 `data` 负载
    :raises StaleHostError: 响应里的 pid 与名片不一致

    宿主退出后端口被别的进程复用时，连接会成功但对面根本不是它。
    这一级把那个漏洞补上。
    """
    actual = status_data.get("pid")
    if actual != info.pid:
        raise StaleHostError(
            f"端口疑似被其它进程复用：名片写的是 pid={info.pid}，"
            f"但 127.0.0.1:{info.port} 上答话的是 pid={actual}。\n"
            f"名片文件：{host_file(info.pid)}（疑似陈旧，可手工删除）"
        )
