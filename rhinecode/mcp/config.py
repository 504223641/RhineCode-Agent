"""
MCP 配置层（c7，spec F1–F4）：两层 mcp.yaml 的定位、加载、${VAR} 展开、容错。

两层配置（后者覆盖前者，按 Server 名字合并）：
- 用户级 ~/.rhinecode/mcp.yaml          —— 跨项目的全局默认
- 项目级 <项目根>/.rhinecode/mcp.yaml   —— 随仓库走、可提交

文件格式（顶层键 mcpServers 是「名字 → 条目」的映射）：
    mcpServers:
      everything:                # stdio 型：有 command
        command: npx
        args: ["-y", "@modelcontextprotocol/server-everything"]
        env:
          TOKEN: ${MY_TOKEN}     # 值支持 ${VAR} 环境变量展开
      remote-api:                # http 型：有 url
        url: https://example.com/mcp
        headers:
          Authorization: Bearer ${API_KEY}

容错原则（fail-safe，对齐 permission/config.py）：文件缺失视为该层无 Server；YAML 解析失败
或单个条目结构非法时，跳过问题项并收集一条可读错误，**绝不**因配置坏掉而崩溃或阻断启动。
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from rhinecode.tools.path_guard import workspace_root

# 配置目录与文件名常量（与权限系统同目录 .rhinecode，但文件名不同）。
_CONFIG_DIR_NAME = ".rhinecode"
_CONFIG_FILE = "mcp.yaml"

# ${VAR} 占位符正则：捕获花括号内的变量名（非贪婪到第一个 }）。
_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class MCPServerConfig:
    """
    一个 MCP Server 的规范化配置。

    :ivar name: Server 名字（mcpServers 的 key），用作工具命名前缀与状态展示
    :ivar kind: "stdio" 或 "http"，由字段自动判定（有 command→stdio，否则有 url→http）
    :ivar command: stdio 型的可执行文件（http 型为 None）
    :ivar args: stdio 型的命令行参数列表
    :ivar env: stdio 型的环境变量（值已完成 ${VAR} 展开）
    :ivar url: http 型的端点地址（stdio 型为 None）
    :ivar headers: http 型的请求头（值已完成 ${VAR} 展开）
    """
    name: str
    kind: str
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)


def user_config_path() -> Path:
    """用户级配置路径：~/.rhinecode/mcp.yaml（跨项目全局默认）。"""
    return Path.home() / _CONFIG_DIR_NAME / _CONFIG_FILE


def project_config_path() -> Path:
    """项目级配置路径：<项目根>/.rhinecode/mcp.yaml（随仓库走）。"""
    return workspace_root() / _CONFIG_DIR_NAME / _CONFIG_FILE


def _expand_env(value: str) -> str:
    """
    展开字符串里的 ${VAR} 占位符（spec F3）。

    引用的环境变量不存在时展开为空字符串（不崩溃、不保留原样占位符），
    让「缺失变量」表现为「空值」，把是否致命交给后续连接阶段判断。

    :param value: 可能含 ${VAR} 的原始字符串
    :returns: 展开后的字符串

    副作用：读取进程环境变量。
    """
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _expand_str_map(raw: object) -> dict[str, str]:
    """
    把 YAML 里的一个 map 规范化为 dict[str,str]，并对每个字符串值做 ${VAR} 展开。

    非 map（如缺省/写错类型）→ 返回空字典（容错）。键与值统一转字符串。

    :param raw: YAML 解析出的对象（期望是 dict）
    :returns: 展开后的字符串字典
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        out[str(k)] = _expand_env(str(v))
    return out


def _parse_server(name: str, raw: object) -> tuple[Optional[MCPServerConfig], Optional[str]]:
    """
    把一个 Server 条目解析成 MCPServerConfig。

    类型判定：含 command → stdio；否则含 url → http；两者皆无 → 视为非法，返回错误串。

    :param name: Server 名字
    :param raw: 该条目的 YAML 对象（期望是 dict）
    :returns: (配置或 None, 错误串或 None)；非法时第一项为 None、第二项为可读错误

    副作用：通过 _expand_env 读取环境变量。
    """
    if not isinstance(raw, dict):
        return None, f"MCP Server「{name}」配置应为映射（含 command 或 url）"

    command = raw.get("command")
    url = raw.get("url")

    if command:
        # stdio 型：args 缺省空列表；env 做 ${VAR} 展开。
        args_raw = raw.get("args") or []
        args = [str(a) for a in args_raw] if isinstance(args_raw, list) else []
        env = _expand_str_map(raw.get("env"))
        return MCPServerConfig(name=name, kind="stdio", command=str(command), args=args, env=env), None

    if url:
        # http 型：headers 做 ${VAR} 展开。
        headers = _expand_str_map(raw.get("headers"))
        return MCPServerConfig(name=name, kind="http", url=str(url), headers=headers), None

    return None, f"MCP Server「{name}」缺少 command（stdio）或 url（http），已跳过"


def _load_layer(path: Path) -> tuple[dict, Optional[str]]:
    """
    加载单层配置文件，返回该层的原始 mcpServers map 与可选错误。

    容错（fail-safe）：
    - 文件不存在 → ({}, None)：该层无 Server，正常情况。
    - 读取/解析异常、顶层非映射、mcpServers 非映射 → ({}, 可读错误)：该层降级为空。

    :param path: 配置文件路径
    :returns: (原始 name→条目 的 map, 错误串或 None)
    """
    if not path.exists():
        return {}, None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 任何读/解析异常都按降级处理，不放权、不崩溃
        return {}, f"MCP 配置解析失败（{path}）：{exc}"

    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, f"MCP 配置顶层应为映射（含 mcpServers）：{path}"

    servers = data.get("mcpServers")
    if servers is None:
        return {}, None
    if not isinstance(servers, dict):
        return {}, f"MCP 配置 mcpServers 应为映射（名字→条目）：{path}"
    return servers, None


def load_all() -> tuple[list[MCPServerConfig], list[str]]:
    """
    加载两层 mcp.yaml 并合并，产出规范化的 Server 配置列表与错误列表。

    合并语义（spec F1）：先用户级、后项目级，按 Server 名字合并——同名以**项目级覆盖用户级**
    （后者盖前者）。合并后逐条 _parse_server；解析错误与两层加载错误一并收集返回，
    由上层（MCPManager/启动流程）展示但不阻断启动（fail-safe）。

    :returns: (MCPServerConfig 列表, 可读错误列表)；无错误时列表为空

    副作用：读取两层配置文件（若存在）、读取环境变量。
    """
    errors: list[str] = []

    user_map, user_err = _load_layer(user_config_path())
    if user_err:
        errors.append(user_err)
    project_map, project_err = _load_layer(project_config_path())
    if project_err:
        errors.append(project_err)

    # 按名字合并：项目级覆盖用户级同名条目。
    merged: dict = dict(user_map)
    merged.update(project_map)

    configs: list[MCPServerConfig] = []
    for name, raw in merged.items():
        cfg, err = _parse_server(str(name), raw)
        if err:
            errors.append(err)
        if cfg is not None:
            configs.append(cfg)

    return configs, errors
