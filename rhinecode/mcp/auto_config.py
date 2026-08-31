"""自动解析并写入 MCP Server 配置的底层能力。

本模块刻意不依赖 TUI 或 Agent Loop：内置工具、未来 CLI 命令和单元测试都可以复用
同一套“解析候选配置 + 安全写入 YAML”的逻辑。这里不负责权限确认，调用方需要在真正
写入和启动外部 MCP 前接入现有 HITL 流程。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from rhinecode.mcp import config as mcp_config
from rhinecode.mcp.config import MCPServerConfig

NPM_SEARCH_URL = "https://registry.npmjs.org/-/v1/search"
NPM_PACKAGE_URL = "https://registry.npmjs.org/{package}"
_HTTP_SCHEMES = {"http", "https"}
_SERVER_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_NPM_PACKAGE_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")


JsonFetcher = Callable[[str, float], dict[str, Any]]


@dataclass
class ResolvedMCPCandidate:
    """解析器产出的一个候选 MCP 配置。

    字段同时服务给模型和 UI：`config` 是可直接写入 `mcp.yaml` 的 server entry，
    `source`/`confidence`/`warnings` 用来帮助 Agent 判断是否需要继续向用户确认。
    """

    server_name: str
    config: dict[str, Any]
    source: str
    confidence: float
    description: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "config": self.config,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "description": self.description,
            "warnings": self.warnings,
        }


@dataclass
class ResolveResult:
    """MCP 解析结果，最终会作为结构化 JSON 返回给工具调用方。

    `status` 只使用三类稳定值：`resolved`、`ambiguous`、`error`。这样 Agent 可以用
    简单分支决定下一步：直接添加、让用户选择，或解释失败原因。
    """

    status: str
    message: str
    candidates: list[ResolvedMCPCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "candidates": [c.to_dict() for c in self.candidates],
        }


@dataclass
class ConfigWriteResult:
    """写入 MCP 配置后的结果。

    `changed=False` 表示同名同配置的 no-op；调用方仍然可以据此决定是否尝试重载，
    以支持“配置已存在但当前会话尚未连接”的场景。
    """

    scope: str
    path: Path
    server_name: str
    config: dict[str, Any]
    action: str
    changed: bool


def _fetch_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """从 NPM registry 拉取 JSON，并把网络/解析错误转换成可读异常。

    这里限制单次读取大小，避免异常 registry 响应把进程内存拖垮。异常消息保留来源，
    但不在本层吞掉；上层 resolver 会把它们转换成 `ResolveResult(error)`。
    """

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "RhineCode/0.1 MCP resolver",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(2_000_000)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"NPM registry request failed: {exc}") from exc
    try:
        loaded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"NPM registry returned invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("NPM registry returned a non-object JSON payload")
    return loaded


def default_npx_command() -> str:
    """返回适合当前平台的 npx 命令名。

    Windows 的 `npx` 实际通常由 `npx.cmd` 提供；直接写 `npx` 交给 `Popen` 时可能触发
    `[WinError 2]`。生成配置时先落到 `npx.cmd`，可以减少用户手动修正成本。
    """

    return "npx.cmd" if os.name == "nt" else "npx"


def sanitize_server_name(value: str) -> str:
    """把用户输入、URL 或包名归一化为可作为 `mcpServers` key 的名字。

    Server 名会进入工具名、状态展示和 YAML key，因此只保留稳定的 ASCII 字符，避免
    空白、斜杠或特殊符号在后续注册工具时产生不可预测的名字。
    """

    name = _SERVER_NAME_CHARS.sub("_", value.strip()).strip("_.-").lower()
    if not name:
        raise ValueError("MCP server name is empty after sanitization")
    return name[:64]


def _looks_like_url(query: str) -> bool:
    parsed = urllib.parse.urlparse(query)
    return parsed.scheme in _HTTP_SCHEMES and bool(parsed.netloc)


def _server_name_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "remote-mcp"
    label = host.split(".")[0] or host
    return sanitize_server_name(label)


def _looks_like_exact_package(query: str) -> bool:
    lowered = query.strip().lower()
    if not _NPM_PACKAGE_RE.match(lowered):
        return False
    if lowered.startswith("@") or "/" in lowered:
        return True
    return lowered.endswith("-mcp") or lowered.startswith("mcp-") or lowered.startswith("server-")


def _package_basename(package_name: str) -> str:
    return package_name.split("/")[-1]


def _server_name_from_package(package_name: str) -> str:
    base = _package_basename(package_name).lower()
    for prefix in ("mcp-server-", "server-", "mcp-"):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    for suffix in ("-mcp-server", "-server", "-mcp"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return sanitize_server_name(base)


def _metadata_warnings(package: dict[str, Any]) -> list[str]:
    """根据包元数据给出保守风险提示。

    第一版不尝试自动推断真实密钥，也不会写入明文 secret；只要描述里出现凭据相关词汇，
    就提醒 Agent/用户后续应使用环境变量占位。
    """

    text = " ".join(
        str(package.get(k) or "")
        for k in ("name", "description", "keywords")
    ).lower()
    warnings: list[str] = []
    if any(word in text for word in ("api key", "token", "secret", "auth")):
        warnings.append("This package may require credentials; use environment variables, not plaintext secrets.")
    return warnings


def _candidate_from_package(package: dict[str, Any], confidence: float) -> Optional[ResolvedMCPCandidate]:
    """把 NPM 包元数据转换为 stdio MCP 候选配置。

    NPM 分发的 MCP 第一版统一通过 `npx -y <package>` 启动；真正执行前仍会经过
    `mcp_add_server` 的写入和权限确认流程。
    """

    name = package.get("name")
    if not name:
        return None
    package_name = str(name)
    return ResolvedMCPCandidate(
        server_name=_server_name_from_package(package_name),
        config={"command": default_npx_command(), "args": ["-y", package_name]},
        source=f"npm:{package_name}",
        confidence=max(0.0, min(0.99, confidence)),
        description=str(package.get("description") or ""),
        warnings=_metadata_warnings(package),
    )


def _score_package(query: str, package: dict[str, Any], search_score: float = 0.0) -> float:
    """给 NPM 搜索结果打一个面向 MCP 场景的置信度分数。

    NPM registry 的搜索分数只代表通用相关性；这里叠加包名匹配、描述里是否明确提到
    MCP/Model Context Protocol 等信号，避免把普通 helper 包误当成可启动的 MCP Server。
    """

    q = query.lower().strip()
    q_tokens = [t for t in re.split(r"[^a-z0-9]+", q) if t]
    name = str(package.get("name") or "").lower()
    base = _package_basename(name)
    desc = str(package.get("description") or "").lower()
    combined = f"{name} {desc}"

    score = min(max(search_score, 0.0), 1.0) * 0.25
    if q == name or q == base:
        score += 0.45
    elif q and (q in name or q in base):
        score += 0.30
    if q_tokens and all(token in combined for token in q_tokens):
        score += 0.15
    if "mcp" in name:
        score += 0.15
    if "model context protocol" in desc or "mcp" in desc:
        score += 0.15
    if name.startswith("@modelcontextprotocol/"):
        score += 0.05
    return min(score, 0.99)


def _search_npm(query: str, fetch_json: JsonFetcher, timeout: float) -> list[ResolvedMCPCandidate]:
    """通过 NPM 搜索自然语言名称，并返回按置信度排序的候选项。"""

    params = urllib.parse.urlencode({"text": f"{query} mcp", "size": "8"})
    payload = fetch_json(f"{NPM_SEARCH_URL}?{params}", timeout)
    objects = payload.get("objects")
    if not isinstance(objects, list):
        return []

    candidates: list[ResolvedMCPCandidate] = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        package = item.get("package")
        if not isinstance(package, dict):
            continue
        score_data = item.get("score")
        final_score = 0.0
        if isinstance(score_data, dict):
            try:
                final_score = float(score_data.get("final") or 0.0)
            except (TypeError, ValueError):
                final_score = 0.0
        confidence = _score_package(query, package, final_score)
        candidate = _candidate_from_package(package, confidence)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates[:5]


def _fetch_exact_package(package_name: str, fetch_json: JsonFetcher, timeout: float) -> ResolvedMCPCandidate:
    """读取精确包名的 packument，并转换为高置信候选。

    对 `@scope/name`、`foo-mcp` 这类明显包名，直接查包详情比搜索更稳定，也能避免同名
    搜索结果把用户已经明确给出的包名挤到后面。
    """

    encoded = urllib.parse.quote(package_name, safe="@")
    payload = fetch_json(NPM_PACKAGE_URL.format(package=encoded), timeout)
    package = {
        "name": payload.get("name") or package_name,
        "description": payload.get("description") or "",
        "keywords": payload.get("keywords") or [],
    }
    candidate = _candidate_from_package(package, 0.95)
    if candidate is None:
        raise RuntimeError(f"NPM package metadata for {package_name} is missing a name")
    return candidate


def resolve_mcp_query(
    query: str,
    *,
    fetch_json: Optional[JsonFetcher] = None,
    timeout: float = 10.0,
) -> ResolveResult:
    """把用户给出的 MCP 名称、NPM 包名或 URL 解析成候选配置。

    处理顺序按“确定性优先”排列：URL 直接生成 HTTP 配置；精确 NPM 包名直接查包；
    普通自然语言再走 NPM 搜索和置信度排序。网络失败、无结果和候选接近都会以结构化
    状态返回，避免 Agent 在低置信场景里静默写配置。
    """

    fetcher = fetch_json or _fetch_json
    q = str(query or "").strip()
    if not q:
        return ResolveResult(status="error", message="Missing MCP query")
    if q.startswith("npm:"):
        q = q[4:].strip()

    # URL 是用户已经明确给出的远端 MCP 端点，不需要访问 NPM，也不猜 headers。
    if _looks_like_url(q):
        candidate = ResolvedMCPCandidate(
            server_name=_server_name_from_url(q),
            config={"url": q},
            source=q,
            confidence=1.0,
            description="HTTP MCP endpoint",
        )
        return ResolveResult("resolved", "Resolved HTTP MCP endpoint", [candidate])

    try:
        # 明确包名走 packument，可以验证包存在，并拿到描述/关键词用于风险提示。
        if _looks_like_exact_package(q):
            candidate = _fetch_exact_package(q.lower(), fetcher, timeout)
            return ResolveResult("resolved", f"Resolved exact NPM package {q}", [candidate])

        # 自然语言名称只能作为搜索线索；后续置信度和歧义判断决定是否需要用户选择。
        candidates = _search_npm(q, fetcher, timeout)
    except Exception as exc:  # noqa: BLE001 - return readable resolver errors to the model
        return ResolveResult(status="error", message=str(exc))

    if not candidates:
        return ResolveResult(status="error", message=f"No likely NPM MCP package found for {q!r}")

    top = candidates[0]
    # 低置信或前两名过近都交还给 Agent 询问用户，避免自动安装相似但错误的 MCP 包。
    if top.confidence < 0.55:
        return ResolveResult(
            status="ambiguous",
            message="No high-confidence MCP candidate found; ask the user to choose or provide a package/URL.",
            candidates=candidates,
        )
    if len(candidates) > 1 and candidates[1].confidence >= top.confidence - 0.08:
        return ResolveResult(
            status="ambiguous",
            message="Multiple MCP candidates are close; ask the user to choose one.",
            candidates=candidates,
        )
    return ResolveResult(status="resolved", message="Resolved NPM MCP package", candidates=[top])


def normalize_server_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化一个原始 `mcpServers` entry。

    写入层只支持两种互斥形态：stdio 的 `command/args/env`，或 HTTP 的 `url/headers`。
    归一化会把可序列化值转成字符串，保证后续比较、写 YAML 和运行时重载看到同一形状。
    """

    if not isinstance(entry, dict):
        raise ValueError("MCP server config must be an object")

    has_command = bool(entry.get("command"))
    has_url = bool(entry.get("url"))
    if has_command == has_url:
        raise ValueError("MCP server config must contain exactly one of command or url")

    if has_command:
        # stdio 配置只接受参数列表和环境变量对象，防止用户输入被错误地拼成 shell 字符串。
        args_raw = entry.get("args") or []
        env_raw = entry.get("env") or {}
        if not isinstance(args_raw, list):
            raise ValueError("stdio MCP args must be a list")
        if not isinstance(env_raw, dict):
            raise ValueError("stdio MCP env must be an object")
        normalized: dict[str, Any] = {
            "command": str(entry["command"]),
            "args": [str(a) for a in args_raw],
        }
        if env_raw:
            normalized["env"] = {str(k): str(v) for k, v in env_raw.items()}
        return normalized

    # HTTP 配置暂不自动补鉴权头；若需要 secret，调用方应写 `${VAR}` 形式的占位。
    headers_raw = entry.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise ValueError("HTTP MCP headers must be an object")
    normalized = {"url": str(entry["url"])}
    if headers_raw:
        normalized["headers"] = {str(k): str(v) for k, v in headers_raw.items()}
    return normalized


def _same_config(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """在归一化后比较两份配置，避免 YAML 表示差异导致误判冲突。"""

    try:
        return normalize_server_entry(left) == normalize_server_entry(right)
    except ValueError:
        return False


def _config_path_for_scope(scope: str) -> tuple[str, Path]:
    """把工具输入的 scope 映射到唯一允许写入的配置路径。

    `auto` 第一版默认项目级，避免用户一句“添加 MCP”就污染全局环境。路径由配置模块统一
    生成，写入服务不会接受任意路径参数。
    """

    normalized = (scope or "auto").strip().lower()
    if normalized == "auto":
        normalized = "project"
    if normalized == "project":
        return "project", mcp_config.project_config_path()
    if normalized == "user":
        return "user", mcp_config.user_config_path()
    raise ValueError("scope must be one of auto, project, or user")


def _load_yaml_for_update(path: Path) -> dict[str, Any]:
    """读取待更新 YAML，并在结构异常时拒绝覆盖。

    这是写入服务最重要的保护之一：损坏 YAML、顶层不是对象、或 `mcpServers` 不是对象时，
    直接报错并保留原文件，避免“自动添加”把用户手写配置清空。
    """

    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    # ⚠ 这里没有吞异常——下一行 `raise ... from exc` 把它重新抛出去了，
    # 所以**不需要**标 BLE001（那条规则只管「捕获了却不再抛」）。
    # 原注释「never overwrite broken user config」说的是语义，仍然成立：
    # 解析失败时不写盘，交由上层处理。
    except Exception as exc:
        raise ValueError(f"MCP config parse failed, not writing {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"MCP config top level must be an object, not writing {path}")
    servers = loaded.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ValueError(f"MCP config mcpServers must be an object, not writing {path}")
    return loaded


def write_server_config(
    server_name: str,
    entry: dict[str, Any],
    *,
    scope: str = "auto",
    replace: bool = False,
) -> ConfigWriteResult:
    """把单个 MCP Server 写入用户级或项目级 `mcp.yaml`。

    副作用：可能创建 `.rhinecode/` 或 `~/.rhinecode/` 目录，并写入 YAML 文件。函数只处理
    文件安全边界和同名冲突；权限确认、用户说明和运行时重载由工具层负责。
    """

    safe_name = sanitize_server_name(server_name)
    normalized_entry = normalize_server_entry(entry)
    resolved_scope, path = _config_path_for_scope(scope)
    data = _load_yaml_for_update(path)
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers

    existing = servers.get(safe_name)
    # 同名同配置视为幂等成功，方便用户重复执行“添加 context7”而不产生无意义覆盖。
    if isinstance(existing, dict) and _same_config(existing, normalized_entry):
        return ConfigWriteResult(resolved_scope, path, safe_name, normalized_entry, "unchanged", False)
    if existing is not None and not replace:
        raise ValueError(
            f"MCP server {safe_name!r} already exists in {path}; pass replace=true to overwrite it"
        )

    servers[safe_name] = normalized_entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    action = "replaced" if existing is not None else "created"
    return ConfigWriteResult(resolved_scope, path, safe_name, normalized_entry, action, True)


def server_config_from_entry(server_name: str, entry: dict[str, Any]) -> MCPServerConfig:
    """把可写入 YAML 的 entry 转成运行时 `MCPServerConfig`。

    `mcp_add_server` 写入成功后会立即调用它进行单 server 重载，因此这里复用同一套
    归一化逻辑，确保“落盘配置”和“当前会话启动配置”完全一致。
    """

    safe_name = sanitize_server_name(server_name)
    normalized = normalize_server_entry(entry)
    if "command" in normalized:
        return MCPServerConfig(
            name=safe_name,
            kind="stdio",
            command=str(normalized["command"]),
            args=[str(a) for a in normalized.get("args", [])],
            env={str(k): str(v) for k, v in normalized.get("env", {}).items()},
        )
    return MCPServerConfig(
        name=safe_name,
        kind="http",
        url=str(normalized["url"]),
        headers={str(k): str(v) for k, v in normalized.get("headers", {}).items()},
    )
