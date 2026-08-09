"""
常驻宿主进程：把 RhineCode 完整装配起来、让界面一直活着，并开一个「接线员」等指令。

    python -m tests.e2e.host --mode scripted --script tests.e2e.scripts.demo:SCRIPT
    python -m tests.e2e.host --mode live --idle-timeout 600

## 为什么需要一个常驻进程

界面测试框架给的驱动上下文（`app.run_test()`）是**一次函数调用里的异步上下文**
——进去、跑完、出来就没了。而外部驱动者（AI 协作方）的每次操作都是**独立进程**，
不常驻就等于每次从零开一个新 Rhine，历史全丢，根本谈不上「交互闭环」。

## 启动顺序（不可调，理由见各步注释）

    1. 建发布目录（建不了直接非零退出——没有名片谁都找不到它）
    2. 建临时工作区与临时用户目录 → assert_disposable → chdir 进工作区
    3. 跑 --seed 预置（在校验之后、装配之前）
    4. bind + listen + publish + 起 accept 线程     ← **先于装配**
         此刻 DriverCore 尚不存在，一切指令回 {"code": "starting"}
    5. asyncio.run(_serve())
         └─ build_app(...)
              失败 → 进 fatal 态，继续服务一个有界宽限窗口让客户端读到，再退出码 1
         └─ async with app.run_test() as pilot:
                DriverCore 就绪 → state 从 starting 转 idle
                await stop_event.wait()
                await core.shutdown_on_main(reason)      ← 在主线程
    6. cleanup(reason) → chdir 回去 → 按需 rmtree → unpublish

**socket 为什么必须先于装配**：spec AC5 点名要验「**装配期**致命错误经通道回报」。
先装配再监听的话，装配一失败就永远等不到监听，客户端只会收到「连接被拒」，
根本读不到那段成文的错误文案。socket 与装配零依赖，顺序可以自由安排。

## ⚠️ 一个实测事实：Textual 会接管 stdout

`app.run_test()` 期间 `print` 出来的东西看不见。所以本模块的诊断输出一律走
**stderr**，且关键信息（端口、工作区、致命错误）都同时写进名片文件或经控制通道回报。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib
import os
import socket
import sys
import threading
import traceback
import time
from pathlib import Path
from typing import Any, Optional

from rhinecode.bootstrap import BootstrapError, build_app
from rhinecode.config import Config, load as load_config
from rhinecode.trace.recorder import create_recorder
from tests.e2e import discovery, fingerprint, protocol, sandbox
from tests.e2e.control import DriverCore, ExternalResponder, SessionState
from tests.e2e.discovery import HostInfo
from tests.e2e.scripted import ScopedScriptedProvider, ScriptedProvider
from tests.e2e.webstub import stub_client_factory, stub_resolver


# 装配时一并摘掉的两个工具（spec F8 第二条 / F19）：
# - `mcp_add_server`：会**写真实用户主目录**且不吃 `user_dir`，隔离在它这里破功；
# - `mcp_resolve_server`：`read_only=True` 却会访问外部包索引，而**只读且被放行的
#   工具根本不弹面板**——应答者拦不住它，确定性形态下它是唯一的真实外网出口。
EXCLUDED_TOOLS = frozenset({"mcp_add_server", "mcp_resolve_server"})

# ⚠ **`web_fetch` 刻意不进 EXCLUDED_TOOLS。**
#
# 它与上面两个不同：不写真实用户主目录、不访问外部包索引。
# 而真正的兜底是 spec F7——URL 类请求在**任何**权限模式下最多到「交人工确认」，
# 驱动者必须显式应答才会真的发出去。
#
# （不要把理由写成「注入替身后完全受控」：`--web-stub` 是可选的，
#  不加时宿主拿的是真 httpx + 真域名解析，那个理由站不住。）

# 装配失败后继续服务的宽限窗口（秒），让客户端有机会读到那段成文文案
FATAL_GRACE_SECONDS = 60.0

# 默认空闲超时：半小时没人发指令就自己退出，避免忘了关的宿主一直占着资源
DEFAULT_IDLE_TIMEOUT = 1800.0

DEFAULT_MAX_TURNS = 40


class HostState:
    """
    宿主的共享状态。accept 线程、handler 线程、watchdog 线程与主线程都要读它，
    故所有可变字段的写入都集中在少数几处，且都是单个赋值（原子）。
    """

    def __init__(self) -> None:
        self.core: Optional[DriverCore] = None
        self.state: str = SessionState.STARTING.value
        self.fatal_message: str = ""
        self.info: Optional[HostInfo] = None
        self.mode: str = "scripted"
        self.turn_budget: int = DEFAULT_MAX_TURNS
        self.build_result: Any = None
        self.stop_event: Optional[asyncio.Event] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.stop_reason: str = "quit"
        self.last_command_at: float = time.monotonic()


def log(message: str) -> None:
    """诊断输出走 stderr —— run_test 期间 stdout 被 Textual 接管，print 是看不见的。"""
    print(f"[host] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 控制服务器
# ---------------------------------------------------------------------------
class ControlServer:
    """
    回环 TCP 上的「接线员」：一条指令一次连接，**每连接一个 handler 线程**。

    为什么每连接一个线程而不是串行处理：`wait` 会阻塞很久（默认 180 秒），
    串行的话这段时间里 `status` 完全答不了，直接违反 spec N4「任何查询一秒内返回」。
    """

    def __init__(self, host_state: HostState):
        self.host_state = host_state
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 端口交给系统分配：写死端口会在连跑时撞车，而名片机制让端口号不必是已知的
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self._closed = threading.Event()

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def start(self) -> None:
        threading.Thread(target=self._accept_loop, name="e2e-accept", daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                # 监听 socket 被关掉（正常退出路径）
                return
            threading.Thread(
                target=self._handle, args=(conn,), name="e2e-handler", daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        """
        处理一条指令：读一行 → 解码 → 派发 → 编码 → 写回 → 关闭。

        **客户端中途断开一律吞掉**（`ConnectionResetError` / `BrokenPipeError`）：
        `wait` 期间用户按 Ctrl-C 就是这个形态。它只该结束这一个 handler，
        绝不能影响 accept 线程——否则一次误操作会让宿主再也接不了新连接。
        """
        # ⚠️ **不用 `with conn`**：连接必须在「组装好错误响应之后」才关，
        # 否则 handler 内部异常时 socket 已经关了，客户端只能读到 EOF，
        # 于是收到一句「宿主在处理本指令期间退出了」——**那句话是错的**
        # （宿主还活着），排查会被彻底带偏。
        response: Optional[dict] = None
        try:
            conn.settimeout(None)
            buffer = b""
            while b"\n" not in buffer:
                chunk = conn.recv(65536)
                if not chunk:
                    return  # 客户端没发完就断了
                buffer += chunk
            line, _, _ = buffer.partition(b"\n")
            try:
                request = protocol.decode(line)
            except Exception as e:  # noqa: BLE001
                response = protocol.err("bad_request", f"指令不是合法 JSON：{e}")
            else:
                response = dispatch(self.host_state, request)
        except (ConnectionResetError, BrokenPipeError, OSError):
            # 客户端中途断开（`wait` 期间按 Ctrl-C 就是这个形态）：
            # 只结束这一个 handler，绝不影响 accept 线程
            conn.close()
            return
        except Exception as e:  # noqa: BLE001 —— handler 绝不能把整个宿主带崩
            log(f"handler 异常：{e}\n{traceback.format_exc()}")
            response = protocol.err("internal", f"宿主内部异常：{type(e).__name__}: {e}")

        try:
            if response is not None:
                conn.sendall(protocol.encode(response))
        except OSError:
            pass  # 写回时客户端已经走了，无所谓
        finally:
            conn.close()


def dispatch(host_state: HostState, request: dict) -> dict:
    """
    把一条指令翻译成 `DriverCore` 的一次调用。

    :returns: 待编码的响应字典

    三个前置态各有专门的错误码，让客户端能分辨「还没准备好」「装配挂了」「正在退出」
    ——这三者的下一步动作完全不同（等一下 / 看文案 / 别再发了）。
    """
    host_state.last_command_at = time.monotonic()
    cmd = str(request.get("cmd", ""))

    # `quit` 在任何状态下都必须可用——包括 fatal 与 starting，
    # 否则一个装配失败的宿主只能靠强杀收场。
    if cmd == "quit":
        host_state.stop_reason = str(request.get("reason") or "quit")
        loop, stop_event = host_state.loop, host_state.stop_event
        if loop is not None and stop_event is not None:
            # ⚠️ `asyncio.Event` **不是线程安全的**，必须经 call_soon_threadsafe。
            # 裸 `set()` 实测也能退出，但那是靠 Textual 的定时器恰好把循环叫醒，
            # 属侥幸，不可依赖。
            loop.call_soon_threadsafe(stop_event.set)
        host_state.state = SessionState.SHUTTING_DOWN.value
        return protocol.ok({"stopping": True})

    if cmd == "hosts":
        return protocol.ok({"hosts": [h.__dict__ for h in discovery.list_hosts()]})

    if host_state.state == SessionState.FATAL.value:
        return protocol.err("fatal", host_state.fatal_message, {"mode": host_state.mode})

    core = host_state.core
    if core is None:
        return protocol.err("starting", "宿主仍在装配，稍后重试（socket 先于装配就绪）")

    if host_state.state == SessionState.SHUTTING_DOWN.value:
        return protocol.err("shutting_down", "宿主正在退出")

    if cmd == "status":
        return protocol.ok(build_status(host_state))

    if cmd == "send":
        text = request.get("text")
        if not isinstance(text, str) or not text:
            return protocol.err("bad_request", "send 需要非空的 text")
        return core.send(text)

    if cmd == "wait":
        try:
            timeout = float(request.get("timeout", 180.0))
        except (TypeError, ValueError):
            return protocol.err("bad_request", "wait 的 timeout 必须是数字")
        until = request.get("until", "terminal")
        bad = protocol.validate_until(until)
        if bad:
            return protocol.err("bad_request", bad)
        return core.wait(timeout, until)

    if cmd == "answer":
        choice = request.get("choice")
        via = request.get("via", "channel")
        if not isinstance(choice, str):
            return protocol.err("bad_request", "answer 需要字符串 choice")
        return core.answer(choice, str(via))

    if cmd == "cancel":
        return core.cancel()

    if cmd == "observe":
        try:
            since = int(request.get("since", 0))
        except (TypeError, ValueError):
            return protocol.err("bad_request", "observe 的 since 必须是整数")
        types = request.get("types")
        if types is not None and not isinstance(types, list):
            return protocol.err("bad_request", "observe 的 types 必须是字符串数组")
        return protocol.ok(core.observe(since, types))

    return protocol.err("bad_request", f"未知指令 {cmd!r}")


def build_status(host_state: HostState) -> dict:
    """
    组装完整的 `status` 负载：`DriverCore` 的界面部分 + 宿主级字段。

    ⚠️ **字段名是 AC9 与 AC11 的读取契约**，改名前先看那两条。
    """
    core = host_state.core
    ui = core.snapshot() if core is not None else {}
    info = host_state.info
    turns = host_state.build_result.recorder.turn_total() if host_state.build_result else 0
    return {
        "pid": os.getpid(),  # 客户端据此识别「端口被别的进程复用」
        "state": ui.get("state", host_state.state),
        "panel": ui.get("panel"),
        "trace_seq": ui.get("trace_seq", 0),
        "focused": ui.get("focused"),
        "panel_visible": ui.get("panel_visible"),
        # 后台活动量与「系统真的停下来了」——C13/C15 的场景靠它们判定，
        # 因为三态只描述界面（见 control._is_quiescent）
        "background": ui.get("background", {}),
        "quiescent": ui.get("quiescent", False),
        "turns": turns,
        "turn_budget": host_state.turn_budget,
        "fingerprint": info.fingerprint if info else "",
        "workspace": info.workspace if info else "",
        "user_dir": info.user_dir if info else "",
        "trace_path": info.trace_path if info else "",
        "mode": host_state.mode,
        "tool_names": (
            sorted(host_state.build_result.tool_registry.names())
            if host_state.build_result
            else []
        ),
    }


# ---------------------------------------------------------------------------
# 脚本与预置的加载
# ---------------------------------------------------------------------------
def load_attr(spec: str) -> Any:
    """
    按 `模块路径:属性名` 载入一个对象（脚本或预置函数）。

    :raises SystemExit: 格式不对或载入失败——**明确报错**，
        因为一个载不进来的脚本会让整场驱动跑在空剧本上，而那看起来像「模型什么都没说」。
    """
    if ":" not in spec:
        raise SystemExit(f"格式应为 MOD:ATTR，收到 {spec!r}")
    module_name, attr = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise SystemExit(f"载入不了模块 {module_name}：{e}") from e
    if not hasattr(module, attr):
        raise SystemExit(f"模块 {module_name} 里没有 {attr}")
    return getattr(module, attr)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tests.e2e.host",
        description="RhineCode 端到端驱动宿主（测试设施，不是产品功能）",
    )
    parser.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    parser.add_argument("--script", default=None, help="剧本位置，形如 MOD:ATTR（scripted 模式）")
    parser.add_argument("--seed", default=None, help="预置函数，形如 MOD:FUNC，签名 (workspace, user_dir)")
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT)
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument(
        "--web-stub",
        action="store_true",
        help="把 web_fetch 的 HTTP 客户端与域名解析换成离线替身（端到端场景用）",
    )
    parser.add_argument("--config", default=None,
                        help="配置文件路径。live 模式必须含有效凭据；scripted 模式也认它"
                             "（用于调 context_window 等构造场景），但 api_key 会被换成假值")
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="退出时保留临时工作区（事后要读产物时用；路径在名片与 status 里）",
    )
    # ⚠️ **刻意没有 --workspace / --user-dir**：外部传入的路径永远过不了
    # `sandbox.assert_disposable`（没有本设施的标记文件），
    # 除非把校验放宽——那样这道 rmtree 之前唯一的闸门就形同虚设了。
    # 要事后看产物请用 --keep-workspace。
    return parser.parse_args(argv)


def make_config(args: argparse.Namespace) -> Config:
    """
    造一份配置。

    - scripted：默认用一份写死的假配置（假 key 即可——`create_provider` 只构造客户端
      对象、不联网，且真正被调用的是假模型）。**给了 `--config` 时以该文件为准**，
      但 `api_key` 会被强制换成假值。
    - live：读真实配置；**凭据缺失或仍是占位符时明确报错退出，不静默降级**（spec F23）
      ——静默降级会让一次「真实模式」验收其实跑的是假模型，那比失败更糟。

    ## 为什么 scripted 也要认 `--config`（实测补的）

    早先这个函数在 scripted 分支里**完全无视 `--config`**，直接返回写死的
    `context_window=65536`。后果是任何想调配置来构造场景的驱动都会**静默失效**——
    用 P1a 验 P0 的「压缩动作可解释」场景时就撞上了：那条场景要求用
    `context_window: 8192` 的窄窗口**可控地**触发两层压缩（P0 checklist 明确
    「不要靠聊很久等自动触发」），而传进来的 8192 被丢掉、仍按 64K 判定，
    于是第二层摘要永远不触发，看起来像是 C8 的 bug，其实是这里吞了参数。

    **静默丢弃参数是最坏的一类缺陷**：调用方以为生效了，现象却出在别处。

    `api_key` 强制换成假值是纵深防御：scripted 模式下 `provider_factory` 恒返回
    假模型、真 key 本来就用不上，但也没必要让它进到进程内存与 `session_start` 快照里。
    """
    if args.mode == "live":
        path = Path(args.config) if args.config else Path.home() / ".rhinecode" / "config.yaml"
        if not path.is_file():
            raise SystemExit(f"真实模式需要配置文件，但找不到 {path}")
        cfg = load_config(str(path))
        if not cfg.api_key or cfg.api_key in ("YOUR_API_KEY", "fake", ""):
            raise SystemExit(
                f"真实模式需要有效的 api_key，但 {path} 里仍是占位符。"
                "（本设施不会静默降级成假模型——那会让一次真实模式验收名不副实。）"
            )
        return cfg

    # scripted：给了 --config 就以它为准（但 api_key 换成假值），否则用写死的默认
    if args.config:
        path = Path(args.config)
        if not path.is_file():
            raise SystemExit(f"--config 指向的文件不存在：{path}")
        return dataclasses.replace(load_config(str(path)), api_key="fake-key-for-e2e")
    return Config(
        protocol="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="fake-key-for-e2e",
        debug_log=False,
        context_window=65536,
    )


async def serve(args: argparse.Namespace, host_state: HostState, workspace: Path, user_dir: Path) -> int:
    """
    装配 + 常驻 + 退出编排。

    :returns: 进程退出码（0 正常，1 装配失败）
    """
    loop = asyncio.get_running_loop()
    host_state.loop = loop
    host_state.stop_event = asyncio.Event()

    trace_path = workspace / ".rhinecode" / "traces" / "host.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    recorder = create_recorder(trace_path)

    # ⚠️ **必须立刻断言记录器真的开着**。`create_recorder` 在路径不可写时会
    # **静默降级为 NullRecorder**（那是 P0 的有意设计：观测设施绝不能反过来阻断
    # 被观测的系统）。但宿主要求一律开启记录（spec F9）——真降级了，
    # 你会看到所有断言以「记录里没有这条事件」的形式失败，而根因其实是磁盘或权限。
    if not recorder.enabled:
        log(f"致命：记录器降级为 Null（路径不可写？）：{trace_path}")
        return 1

    provider_factory = None
    if args.mode == "scripted":
        script = load_attr(args.script) if args.script else []
        # 剧本有两种形态，按类型自动分派：
        #
        # - **list**（`[[chunk, ...], ...]`）→ `ScriptedProvider`，按全局调用序取轮次。
        #   单条对话的场景用它，C2–C12 全部如此。
        # - **dict**（`{作用域: [[chunk, ...], ...]}`）→ `ScopedScriptedProvider`，
        #   **按线程本地的 trace 作用域**分派。多 Agent 并发（C13/C15）**必须**用它——
        #   队员与主对话谁先调模型取决于线程调度，全局序号在那里是不确定的。
        #
        # 自动分派而不是加一个 `--script-kind` 参数：形态从剧本本身就能看出来，
        # 多一个参数只会多一处「写错了但不报错」的地方（写 list 剧本却传
        # `--script-kind scoped` 会静默走兜底，现象是「模型什么都不说」）。
        if isinstance(script, dict):
            provider = ScopedScriptedProvider(script)
        else:
            provider = ScriptedProvider(script)
        provider_factory = lambda cfg: provider  # noqa: E731

    try:
        # 网络替身：`--web-stub` 时把 HTTP 客户端与域名解析都换成离线替身，
        # 使端到端场景不发出任何真实请求（spec N5）。缺省 None = 用真实实现。
        web_client_factory = stub_client_factory() if args.web_stub else None
        web_resolver = stub_resolver if args.web_stub else None
        result = build_app(
            make_config(args),
            user_dir=user_dir,
            recorder=recorder,
            provider_factory=provider_factory,
            exclude_tools=EXCLUDED_TOOLS,
            web_client_factory=web_client_factory,
            web_resolver=web_resolver,
        )
    except BootstrapError as e:
        # 装配期致命错误：进 fatal 态并**继续服务一个有界的宽限窗口**，
        # 让客户端有机会读到那段成文文案（`e.args[0]` 已是完整 stderr 文案，
        # **不要再拼前缀**）。直接退出的话客户端只会收到「连接被拒」，
        # 根本区分不了「装配失败」与「宿主还没起来」。
        host_state.fatal_message = e.args[0]
        host_state.state = SessionState.FATAL.value
        log(f"装配失败，进入 fatal 态（宽限 {FATAL_GRACE_SECONDS} 秒）：{e.args[0]}")
        deadline = time.monotonic() + FATAL_GRACE_SECONDS
        while time.monotonic() < deadline and not host_state.stop_event.is_set():
            await asyncio.sleep(0.1)
        recorder.close()
        return 1

    host_state.build_result = result
    if args.mode == "scripted":
        # F16 裁决：确定性形态关掉自动笔记——它本身就是不确定性来源
        # （实测：关掉时一轮对话模型被调 1 次，不关是 2 次）。
        # 该属性是普通实例属性、门控点每次调用现读，赋值即生效。
        result.manager.memory_manager.notes_enabled = False

    exit_code = 0
    app = result.app
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            core = DriverCore(
                app,
                pilot,
                loop,
                result,
                ExternalResponder(),
                turn_budget=args.max_turns,
                trace_path=trace_path,
            )
            host_state.core = core
            host_state.state = SessionState.IDLE.value
            log(f"就绪：pid={os.getpid()} mode={args.mode} workspace={workspace}")

            await host_state.stop_event.wait()
            host_state.state = SessionState.SHUTTING_DOWN.value
            # ⚠️ 只能在主线程调用，且内部不得使用 run_on_main（见其 docstring）
            await core.shutdown_on_main(host_state.stop_reason, live_mode=args.mode == "live")
    finally:
        # 清理三步的第①步：关句柄、释放会话锁、复位进程级白名单
        result.cleanup(host_state.stop_reason)
    return exit_code


def main(argv: Optional[list[str]] = None) -> int:
    """
    宿主入口。返回进程退出码。

    副作用（很多，按发生顺序）：建发布目录与名片文件、建两个临时目录、
    切换进程工作目录、跑预置、监听回环端口、装配整个应用（可能拉起 MCP 子进程）、
    退出时删除临时目录与名片。
    """
    args = parse_args(argv)
    host_state = HostState()
    host_state.mode = args.mode
    host_state.turn_budget = args.max_turns

    # 步骤①：发布目录（建不了直接非零退出——没有名片谁都找不到这个宿主）
    try:
        discovery.publish_dir().mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"致命：建不了发布目录 {discovery.publish_dir()}：{e}")
        return 1

    # 步骤②：临时工作区与用户目录 → 可丢弃校验 → chdir
    previous_cwd = Path.cwd()
    workspace = sandbox.create_workspace()
    user_dir = sandbox.create_user_dir()
    sandbox.assert_disposable(workspace)
    sandbox.assert_disposable(user_dir)
    os.chdir(workspace)

    # 步骤③：预置（在校验之后、装配之前——启动时扫到的内容必须已经落好盘）
    #
    # 必须包 try：预置函数是会抛的（`seed_git_repo` 缺 git 时抛、
    # `seed_foreign_skill` 找不到源文件时抛，两者都是**刻意**明确报错而非静默跳过）。
    # 不包的话异常直接掀掉进程，第②步刚建的两个临时目录就永远留在系统临时目录里，
    # 而**这时候还没发布名片**——`client hosts` 看不见它们，排障时只会发现
    # 「临时目录莫名其妙攒了一堆」。实测就是这么攒出 4 个的。
    if args.seed:
        try:
            load_attr(args.seed)(workspace, user_dir)
        except Exception as e:  # noqa: BLE001 —— 预置什么都可能抛，一律清理后退出
            log(f"致命：预置 {args.seed} 失败：{e}")
            for path in (workspace, user_dir):
                try:
                    sandbox.cleanup_workspace(path, previous_cwd=previous_cwd)
                except OSError as ce:
                    log(f"（清理 {path} 失败：{ce}）")
            return 1

    # 步骤④：**socket 先于装配**（见模块 docstring）
    server = ControlServer(host_state)
    info = HostInfo(
        pid=os.getpid(),
        port=server.port,
        workspace=str(workspace),
        user_dir=str(user_dir),
        trace_path=str(workspace / ".rhinecode" / "traces" / "host.jsonl"),
        fingerprint=fingerprint.compute(Path(__file__).resolve().parents[2] / "rhinecode"),
        mode=args.mode,
        started_at=time.time(),
    )
    host_state.info = info
    discovery.publish(info)
    server.start()
    log(f"监听 127.0.0.1:{server.port}（装配尚未开始，指令会回 starting）")

    # watchdog：空闲超时自己退出，避免忘了关的宿主一直占着资源
    def watchdog() -> None:
        while True:
            time.sleep(1.0)
            if host_state.state == SessionState.SHUTTING_DOWN.value:
                return
            if time.monotonic() - host_state.last_command_at > args.idle_timeout:
                log(f"空闲超过 {args.idle_timeout} 秒，自行退出")
                host_state.stop_reason = "idle_timeout"
                loop, stop_event = host_state.loop, host_state.stop_event
                if loop is not None and stop_event is not None:
                    loop.call_soon_threadsafe(stop_event.set)
                return

    threading.Thread(target=watchdog, name="e2e-watchdog", daemon=True).start()

    # 步骤⑤：装配 + 常驻
    try:
        exit_code = asyncio.run(serve(args, host_state, workspace, user_dir))
    finally:
        # 步骤⑥：清理。顺序不可调（见 sandbox 模块 docstring）
        server.close()
        if args.keep_workspace:
            os.chdir(previous_cwd)
            log(f"保留工作区：{workspace}（用户目录 {user_dir}）")
        else:
            # ⚠️ **两个目录必须各自独立地清理，不能串在同一个 try 里**：
            # 串起来的话，工作区一旦删不掉（Windows 下句柄未释放是常见情形），
            # 用户目录就根本轮不到清理，于是临时目录里只见 `_ws_` 残留、不见
            # `_user_`——实测就是这个形态，攒了十几个才被发现。
            for path in (workspace, user_dir):
                try:
                    sandbox.cleanup_workspace(path, previous_cwd=previous_cwd)
                except OSError as e:
                    # 删不掉要说出来（N7），但不改变退出码——那会掩盖真正的运行结果
                    log(f"清理临时目录失败（需手工删除）：{path}：{e}")
        discovery.unpublish(info.pid)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
