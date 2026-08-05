"""
瘦客户端：找到宿主 → 发一条指令 → 打印响应 → 退出。

    python -m tests.e2e.client status [--pid N] [--json]
    python -m tests.e2e.client send "/skills"
    python -m tests.e2e.client wait --timeout 180
    python -m tests.e2e.client answer once [--via keys]
    python -m tests.e2e.client observe --since 42 --types tool_execute
    python -m tests.e2e.client cancel
    python -m tests.e2e.client quit
    python -m tests.e2e.client hosts          # 列出名片（排障用，不需要宿主活着）

## 两条设计纪律

**① 无状态、无重试**（spec N7）。一条指令一次连接，失败就报错退出。
重试会把「宿主已经没了」这种确定结论拖成一串看不懂的超时；而无状态让它可以在
任意时刻、任意目录被调用——外部驱动者的每次操作本来就是独立进程。

**② 刻意不 import 任何 `rhinecode` 模块。**
宿主挂掉的时候，客户端仍然要能起来把原因报出来。少一层导入就少一处失败面
——如果客户端也依赖产品代码，那么「产品代码本身有语法错误」这种情形下，
连报错都报不出来。

## 四种失败必须翻译成人话

| 现象 | 翻译 |
| --- | --- |
| 连不上 | 「宿主已不在（名片 X，pid N）」+ 清理指引 |
| 响应 pid 与名片不符 | 「端口已被其它进程占用，名片疑似陈旧」 |
| **读到 EOF（空响应）** | 「宿主在处理本指令期间退出了」 |
| **连接被对端 RST**（宿主被强杀） | 同上，另注明「连接被强制关闭」 |

第三条最容易被写成 `JSONDecodeError`：`wait` 期间宿主自行退出（空闲超时、
或别人发了 `quit`）正是这个形态，而那个异常名字对使用者毫无信息量。

**第四条是全阶段复测（`docs/e2e-sweep/testing-p1a-driver.md` 缺陷 D1）补上的，
它和第三条是同一件事的两种 TCP 形态**，漏掉一种就等于这条翻译只做了一半：

| 宿主怎么没的 | TCP 层 | 不处理时的表现 |
| --- | --- | --- |
| `quit` / 空闲超时 / 正常崩溃 | FIN → `recv` 返回 `b""` | 走下面那条可读提示 |
| **强杀**（`Stop-Process -Force` / `SIGKILL`） | **RST → `recv` 直接抛异常** | 裸 `ConnectionResetError` traceback |

也就是说：**最需要可读信息的那一刻（宿主意外死了），给出的却是最没有信息量的输出。**
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Optional

from tests.e2e import discovery, protocol
from tests.e2e.discovery import HostInfo, StaleHostError


def force_utf8_output() -> None:
    """
    把本进程的 stdout / stderr 切成 UTF-8。

    **为什么必需**（实测踩过）：Windows 控制台默认代码页是 GBK，而面板原文里有
    `⚠`、emoji 与中文。直接 `print` 会抛 `UnicodeEncodeError`，
    **客户端整个崩掉、连响应都看不到**——一个排障工具因为打印排障信息而失败，
    是最讽刺也最耽误事的失败形态。

    `errors="replace"` 是刻意的：终端显示不出来的字符退化成 `?` 即可，
    绝不能因此中断输出。通道本身一直是 UTF-8，这里只是显示层的兜底。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass  # 被重定向到不支持 reconfigure 的对象上，忽略即可


def _host_gone(info: HostInfo, request: dict, detail: str = "") -> RuntimeError:
    """
    造一条「宿主没了」的可读提示。

    :param info: 名片，用于把 pid 报给使用者
    :param request: 本次指令，用于说明是在做什么的时候断的
    :param detail: 形态补充说明（EOF 与 RST 两种路径各自不同），空串表示不补充
    :returns: 待抛出的 RuntimeError（**只造不抛**，由调用方 raise，保留 `from e`）

    EOF 与 RST 是同一件事的两种 TCP 形态，所以文案收在这里**只写一份**——
    分开写的话，将来改措辞必然只改到一处，而另一处要等下一次宿主意外死掉才被发现。
    """
    detail_line = f"{detail}\n" if detail else ""
    return RuntimeError(
        f"宿主在处理本指令期间退出了（pid={info.pid}，指令 {request.get('cmd')!r}）。\n"
        f"{detail_line}"
        f"常见原因：空闲超时到了、别的客户端发了 quit、或宿主崩溃。\n"
        f"用 `python -m tests.e2e.client hosts` 确认它是否还在。"
    )


def send_command(info: HostInfo, request: dict, *, timeout: float) -> dict:
    """
    发一条指令并读回一条响应。

    :raises StaleHostError: 连不上（第一级陈旧判定）
    :raises RuntimeError: 宿主在处理本指令期间没了——EOF（正常退出）
        与 RST（被强杀）两种形态翻译成同一条可读提示
    """
    sock = discovery.connect(info, timeout=timeout)
    try:
        sock.sendall(protocol.encode(request))
        buffer = b""
        while b"\n" not in buffer:
            try:
                chunk = sock.recv(65536)
            except (ConnectionResetError, ConnectionAbortedError) as exc:
                # 宿主被强杀 → 对端发 RST → recv 直接抛，走不到下面的 EOF 分支。
                #
                # **这里无条件抛、不像 EOF 那样「能解多少算多少」**，理由是结构性的：
                # while 的条件是「buffer 里还没有 \n」，所以能执行到这次 recv，
                # 就说明**完整的一行响应必然还没到齐**。此时保留半截 buffer 交给
                # decode，只会把一个明确的「宿主没了」换成一个 JSONDecodeError
                # ——正是本模块 docstring 点名要消灭的那种没信息量的报错。
                raise _host_gone(
                    info,
                    request,
                    f"连接被对端强制关闭（{type(exc).__name__}），常见于宿主被强杀；"
                    f"这种情况通常还会残留临时工作区，需一并手工清理。",
                ) from exc
            if chunk:
                # ⚠️ 这一行漏掉过一次：不累积的话 buffer 永远为空，
                # 下一次 recv 读到 EOF 就会误报「宿主退出了」——
                # 而宿主其实一直好好地在跑（实测踩过，靠裸 socket 探针才定位到）。
                buffer += chunk
                continue
            if not buffer:
                raise _host_gone(info, request)
            break  # 收到过数据但连接先断了：能解多少算多少，交给下面的 decode 判定
        line, _, _ = buffer.partition(b"\n")
        return protocol.decode(line)
    finally:
        sock.close()


def print_hosts() -> int:
    """列出当前的名片（排障用，**不需要宿主活着**）。"""
    hosts = discovery.list_hosts()
    if not hosts:
        print(f"没有正在运行的宿主（发布目录 {discovery.publish_dir()}）。", file=sys.stderr)
        return 1
    for h in hosts:
        print(f"pid={h.pid} port={h.port} mode={h.mode} fingerprint={h.fingerprint}")
        print(f"    workspace = {h.workspace}")
        print(f"    trace     = {h.trace_path}")
    return 0


def build_request(args: argparse.Namespace) -> dict:
    """把命令行参数翻译成一条线上指令。"""
    cmd = args.cmd
    if cmd == "send":
        return {"cmd": "send", "text": args.text}
    if cmd == "wait":
        return {"cmd": "wait", "timeout": args.timeout}
    if cmd == "answer":
        return {"cmd": "answer", "choice": args.choice, "via": args.via}
    if cmd == "observe":
        types = [t.strip() for t in args.types.split(",")] if args.types else None
        return {"cmd": "observe", "since": args.since, "types": types}
    return {"cmd": cmd}


def render(response: dict, as_json: bool) -> int:
    """
    打印响应，返回进程退出码（0 成功 / 2 指令被拒）。

    退出码用 2 而不是 1 来表示「指令被宿主拒绝」，与 1（连不上 / 找不到宿主）区分开
    ——脚本里据此分辨「环境有问题」和「这一步被拒了」。
    """
    if as_json:
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0 if response.get("ok") else 2

    if response.get("ok"):
        data = response.get("data")
        if data is None:
            print("ok")
        elif isinstance(data, (dict, list)):
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(data)
        return 0

    error = response.get("error") or {}
    print(f"[{error.get('code')}] {error.get('message')}", file=sys.stderr)
    if error.get("data"):
        print(json.dumps(error["data"], ensure_ascii=False, indent=2), file=sys.stderr)
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="python -m tests.e2e.client",
        description="RhineCode 端到端驱动的瘦客户端（无状态、无重试）",
    )
    # 三个全局标志放进 parent parser 再挂到每个子命令上，
    # 这样 `client --json status` 与 `client status --json` **两种写法都能用**。
    # argparse 默认只认前一种，而后一种才是大多数人下意识会敲的
    # ——为这点小事让人多试一次是没必要的摩擦。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pid", type=int, default=None, help="有多个宿主时指定其中一个")
    common.add_argument("--json", action="store_true", help="原样打印整份响应")
    common.add_argument("--connect-timeout", type=float, default=300.0)
    for action in common._actions:
        parser._add_action(action)

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", parents=[common])
    sub.add_parser("cancel", parents=[common])
    sub.add_parser("quit", parents=[common])
    sub.add_parser("hosts", parents=[common])

    p_send = sub.add_parser("send", parents=[common])
    p_send.add_argument("text")

    p_wait = sub.add_parser("wait", parents=[common])
    p_wait.add_argument("--timeout", type=float, default=180.0)

    p_answer = sub.add_parser("answer", parents=[common])
    p_answer.add_argument("choice")
    p_answer.add_argument("--via", choices=("channel", "keys"), default="channel")

    p_observe = sub.add_parser("observe", parents=[common])
    p_observe.add_argument("--since", type=int, default=0)
    p_observe.add_argument("--types", default=None, help="逗号分隔的事件类型")

    args = parser.parse_args(argv)

    if args.cmd == "hosts":
        return print_hosts()

    try:
        info = discovery.resolve_host(args.pid)
    except StaleHostError as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        response = send_command(info, build_request(args), timeout=args.connect_timeout)
    except StaleHostError as e:
        print(str(e), file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    # 第二级陈旧判定：连上了，但对面是不是我们要找的那个宿主？
    if args.cmd == "status" and response.get("ok"):
        try:
            discovery.verify_pid(info, response.get("data") or {})
        except StaleHostError as e:
            print(str(e), file=sys.stderr)
            return 1

    return render(response, args.json)


if __name__ == "__main__":
    sys.exit(main())
