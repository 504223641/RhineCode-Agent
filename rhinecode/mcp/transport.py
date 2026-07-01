"""
传输层（c7，spec F5/F6/N5）：屏蔽 stdio 与 Streamable HTTP 的差异，
对上层提供统一的「阻塞式请求 + 按 id 配对」能力。

为什么需要「按 id 配对」：
- stdio 是**单管道复用**——所有请求的响应都从同一个 stdout 流回来，可能与服务端主动发的
  通知交织、甚至乱序。因此不能「写完就顺序读下一行当响应」，必须靠 JSON-RPC 的 id 把
  「回来的响应」与「发出的请求」对上。
- 实现方式：后台 reader 线程持续读流，每读到一条响应就按 id 找到在等待的请求、写入结果并
  唤醒它（threading.Event）。调用线程发完请求后阻塞在自己的 Event 上，直到被唤醒或超时。
- HTTP 则天然 request-scoped（一次 POST 对应一次响应流），无需后台线程，直接阻塞读回包即可。

同步设计的原因：现有 Tool.execute 与 Agent Loop 全是同步（Textual Worker 线程模型），
引入 asyncio 会与线程模型冲突，故这里用线程 + 阻塞实现，不碰事件循环（见 plan 技术决策）。
"""

import json
import os
import subprocess
import threading
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Optional

import httpx

from rhinecode.mcp.jsonrpc import (
    JsonRpcError,
    TransportError,
    build_notification,
    build_request,
    is_response,
    next_id,
)


class Transport(ABC):
    """
    传输抽象：一个连接的「发请求 / 发通知 / 关闭」三件事。

    上层（MCPClient）只依赖本抽象，不感知底层是子进程还是 HTTP（spec N5）。
    """

    @abstractmethod
    def start(self) -> None:
        """建立连接（stdio 拉起子进程并启动 reader；http 建 client）。失败抛 TransportError。"""
        ...

    @abstractmethod
    def request(self, method: str, params: Optional[dict], timeout: float) -> dict:
        """
        阻塞发送一个 JSON-RPC 请求并返回其 result。

        :param method: 方法名
        :param params: 参数（可 None）
        :param timeout: 等待响应的秒数上限（spec N3）
        :returns: 响应里的 result 字典
        :raises JsonRpcError: 对端返回了 error 对象
        :raises TransportError: 超时、连接断开、非 2xx 等传输故障
        """
        ...

    @abstractmethod
    def notify(self, method: str, params: Optional[dict]) -> None:
        """发送一条通知（无 id、不等回包）。"""
        ...

    @abstractmethod
    def close(self) -> None:
        """释放资源（关管道/终止子进程/关闭 http），best-effort，不应抛出。"""
        ...


def _extract_result(msg: dict) -> dict:
    """
    从一条 JSON-RPC 响应里取出 result；若含 error 则抛 JsonRpcError。

    :param msg: 已解析的响应字典
    :returns: result（无 result 时返回空字典）
    :raises JsonRpcError: 响应含 error
    """
    if "error" in msg and msg["error"] is not None:
        err = msg["error"]
        if isinstance(err, dict):
            raise JsonRpcError(err.get("code", -1), str(err.get("message", "")), err.get("data"))
        raise JsonRpcError(-1, str(err))
    result = msg.get("result")
    return result if isinstance(result, dict) else {}


class _Pending:
    """一个在途请求的等待槽：调用线程等在 event 上，reader 线程写好 box 后 set。"""

    __slots__ = ("event", "box")

    def __init__(self) -> None:
        self.event = threading.Event()
        # box 用列表当可变容器：[0] 放响应字典，[1] 放传输错误（二者之一）
        self.box: list[Any] = [None, None]


class StdioTransport(Transport):
    """
    stdio 传输：把 MCP Server 作为子进程拉起，通过其 stdin/stdout 交换换行分隔的 JSON。

    收发模型：
    - 写：在调用线程直接写 stdin（受 _write_lock 串行化）。
    - 读：后台守护 reader 线程循环 readline，按 id 唤醒对应的在途请求。
    """

    def __init__(self, command: str, args: list[str], env: dict[str, str]):
        """
        :param command: 可执行文件
        :param args: 命令行参数
        :param env: 额外环境变量（会叠加在继承的 os.environ 之上）
        """
        self._command = command
        self._args = args
        self._env = env
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._stderr_lock = threading.Lock()
        self._write_lock = threading.Lock()
        # id → _Pending；reader 与调用线程共享，用 _pending_lock 保护结构性读写
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._alive = False

    def start(self) -> None:
        """
        拉起子进程并启动 reader 线程。

        环境：继承当前进程 os.environ 再叠加配置 env（多数 Server 依赖 PATH 等基础环境，
        配置 env 优先级更高）。stderr 单独捕获，避免污染 stdout 的 JSON 流。
        """
        merged_env = {**os.environ, **self._env}
        try:
            self._proc = subprocess.Popen(
                [self._command, *self._args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
                text=True,
                encoding="utf-8",  # MCP stdio 规范要求 UTF-8
                errors="replace",  # 个别坏字节替换而非抛异常，避免 reader 线程被一行坏数据搞死
                bufsize=1,  # 行缓冲，配合按行分帧
            )
        except (OSError, ValueError) as exc:
            raise TransportError(f"启动子进程失败（{self._command}）：{exc}") from exc

        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, name=f"mcp-stdio-{self._command}", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr,
            name=f"mcp-stderr-{self._command}",
            daemon=True,
        )
        self._stderr_reader.start()

    def _drain_stderr(self) -> None:
        """持续排空 stderr，避免 MCP Server 因日志管道写满而阻塞。"""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            for line in self._proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                with self._stderr_lock:
                    self._stderr_tail.append(line)
        except Exception:
            return

    def recent_stderr(self) -> list[str]:
        """返回最近若干行 stderr，供诊断连接/超时问题。"""
        with self._stderr_lock:
            return list(self._stderr_tail)

    def _stderr_suffix(self) -> str:
        tail = self.recent_stderr()
        if not tail:
            return ""
        return "；stderr: " + " | ".join(tail[-3:])

    def _read_loop(self) -> None:
        """
        后台 reader：逐行读 stdout，把响应按 id 派发给等待中的请求。

        - EOF（子进程退出）→ 标记死亡并唤醒所有在途请求（置 TransportError），然后退出。
        - 非响应（服务端通知/请求）→ 本章忽略。
        - 解析失败的行 → 跳过（不因一行坏数据崩掉整条连接）。
        """
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            for line in stdout:  # 迭代到 EOF 自然结束
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if is_response(msg):
                    self._dispatch(msg)
                # 其它类型（通知/服务端请求）本章不处理
        finally:
            self._fail_all("MCP 子进程输出流已结束（连接断开）" + self._stderr_suffix())

    def _dispatch(self, msg: dict) -> None:
        """把一条响应交给对应 id 的在途请求并唤醒它。"""
        msg_id = msg.get("id")
        with self._pending_lock:
            pending = self._pending.pop(msg_id, None)
        if pending is not None:
            pending.box[0] = msg
            pending.event.set()

    def _fail_all(self, reason: str) -> None:
        """连接断开时，唤醒所有在途请求并置传输错误，避免调用线程永久阻塞。"""
        self._alive = False
        with self._pending_lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for pending in pendings:
            pending.box[1] = TransportError(reason)
            pending.event.set()

    def request(self, method: str, params: Optional[dict], timeout: float) -> dict:
        if not self._alive or self._proc is None or self._proc.stdin is None:
            raise TransportError("MCP 连接未建立或已断开")
        req_id = next_id()
        pending = _Pending()
        with self._pending_lock:
            self._pending[req_id] = pending

        payload = json.dumps(build_request(req_id, method, params), ensure_ascii=False) + "\n"
        try:
            with self._write_lock:
                self._proc.stdin.write(payload)
                self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TransportError(f"写入 MCP 子进程失败：{exc}{self._stderr_suffix()}") from exc

        # 阻塞等待 reader 唤醒或超时。
        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TransportError(f"MCP 请求超时（{method}，{timeout}s）{self._stderr_suffix()}")

        # 传输错误（连接断开）优先。
        if pending.box[1] is not None:
            raise pending.box[1]
        return _extract_result(pending.box[0])

    def notify(self, method: str, params: Optional[dict]) -> None:
        if not self._alive or self._proc is None or self._proc.stdin is None:
            raise TransportError("MCP 连接未建立或已断开")
        payload = json.dumps(build_notification(method, params), ensure_ascii=False) + "\n"
        try:
            with self._write_lock:
                self._proc.stdin.write(payload)
                self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise TransportError(f"写入 MCP 子进程失败：{exc}{self._stderr_suffix()}") from exc

    def close(self) -> None:
        """关闭 stdin → 温和终止 → 限时等待 → 必要时强杀。reader 是守护线程，随进程退出。"""
        self._alive = False
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001 —— 关闭阶段一律吞异常，保证清理不中断
            pass
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass
        # 关闭 stdout/stderr 管道，避免 ResourceWarning（reader 迭代结束后句柄仍打开）。
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass


class HttpTransport(Transport):
    """
    Streamable HTTP 传输：单端点 POST 发 JSON-RPC，响应可能是 application/json 或 SSE 流。

    会话管理：initialize 的响应头若带 Mcp-Session-Id，则记录下来，后续请求回带该头；
    并在 initialize 后回带 MCP-Protocol-Version（对齐 MCP 规范 2025-11-25）。
    """

    _PROTOCOL_VERSION = "2025-11-25"

    def __init__(self, url: str, headers: dict[str, str]):
        """
        :param url: MCP 端点地址
        :param headers: 配置声明的额外请求头（已做 ${VAR} 展开）
        """
        self._url = url
        self._headers = headers
        self._client: Optional[httpx.Client] = None
        self._session_id: Optional[str] = None
        self._initialized = False

    def start(self) -> None:
        self._client = httpx.Client()

    def _base_headers(self) -> dict[str, str]:
        """组装每次请求的公共头：配置头 + JSON/SSE 双 Accept + 会话/协议版本头。"""
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._initialized:
            headers["MCP-Protocol-Version"] = self._PROTOCOL_VERSION
        return headers

    def request(self, method: str, params: Optional[dict], timeout: float) -> dict:
        if self._client is None:
            raise TransportError("MCP HTTP 连接未建立")
        req_id = next_id()
        body = build_request(req_id, method, params)
        headers = self._base_headers()

        try:
            # 用 stream 以便统一处理 application/json 与 text/event-stream 两种响应。
            with self._client.stream("POST", self._url, json=body, headers=headers, timeout=timeout) as resp:
                # initialize 响应可能下发会话 id，记录以便后续请求回带。
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self._session_id = sid

                if resp.status_code >= 400:
                    text = resp.read().decode("utf-8", errors="replace")[:500]
                    raise TransportError(f"MCP HTTP {resp.status_code}：{text}")

                content_type = resp.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    msg = self._read_sse_response(resp, req_id)
                else:
                    raw = resp.read().decode("utf-8", errors="replace")
                    msg = json.loads(raw) if raw.strip() else {}
        except httpx.HTTPError as exc:
            raise TransportError(f"MCP HTTP 请求失败（{method}）：{exc}") from exc

        if method == "initialize":
            self._initialized = True
        return _extract_result(msg)

    def _read_sse_response(self, resp: "httpx.Response", req_id: int) -> dict:
        """
        从 SSE 流里读出与 req_id 匹配的 JSON-RPC 响应。

        SSE 格式为若干 `data: <json>` 行；服务端可能在响应前后交织通知，这里只取
        第一个「是响应且 id 匹配」的消息，忽略其余。

        :param resp: 打开的流式响应
        :param req_id: 期望匹配的请求 id
        :returns: 匹配到的响应字典
        :raises TransportError: 流结束仍未拿到匹配响应
        """
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data:
                continue
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            if is_response(msg) and msg.get("id") == req_id:
                return msg
        raise TransportError("MCP SSE 流结束但未收到匹配的响应")

    def notify(self, method: str, params: Optional[dict]) -> None:
        if self._client is None:
            raise TransportError("MCP HTTP 连接未建立")
        body = build_notification(method, params)
        try:
            resp = self._client.post(self._url, json=body, headers=self._base_headers(), timeout=30)
            # 通知按规范返回 202/200，不解析回包；非 2xx 记为传输错误。
            if resp.status_code >= 400:
                raise TransportError(f"MCP HTTP 通知失败 {resp.status_code}")
        except httpx.HTTPError as exc:
            raise TransportError(f"MCP HTTP 通知失败（{method}）：{exc}") from exc

    def close(self) -> None:
        """best-effort：有会话 id 则发 DELETE 终止会话，然后关闭 httpx.Client。"""
        client = self._client
        if client is None:
            return
        try:
            if self._session_id:
                client.delete(self._url, headers={"Mcp-Session-Id": self._session_id}, timeout=5)
        except Exception:  # noqa: BLE001 —— 关闭阶段吞异常
            pass
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
