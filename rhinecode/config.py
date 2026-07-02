"""
配置加载模块。

负责从 YAML 文件中读取 LLM 供应商信息，并校验四个必填字段。
配置文件示例见项目根目录的 config.example.yaml。

注意：api_key 属于敏感信息，config.yaml 已加入 .gitignore，禁止提交到版本库。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml


# 用户级配置目录/文件名：与权限、MCP 配置同放 ~/.rhinecode 下，仅文件名不同。
_CONFIG_DIR_NAME = ".rhinecode"
_CONFIG_FILE = "config.yaml"

# 模板里的占位 api_key。它是「非空字符串」，能通过 load() 的非空校验，
# 因此需要单独识别，用来区分「用户已填真实 key」和「刚生成模板还没填」。
PLACEHOLDER_API_KEY = "YOUR_API_KEY"

# 首次运行自动生成的配置模板。内容与 config.example.yaml 对齐（默认 deepseek），
# api_key 用占位符，引导用户填入真实值后再运行。
_CONFIG_TEMPLATE = """\
# RhineCode 全局配置。填入真实 api_key 后即可在任意目录运行 `rhine`。
# 也可用 `rhine --config <路径>` 指定其它配置文件覆盖本文件。

# 使用 DeepSeek（默认，支持工具调用 / Plan Mode / 权限系统）
protocol: deepseek
model: deepseek-chat
base_url: https://api.deepseek.com
api_key: YOUR_API_KEY

# 使用 Anthropic Claude（纯对话）
# protocol: anthropic
# model: claude-sonnet-4-6
# base_url: https://api.anthropic.com
# api_key: sk-ant-...

# 使用 OpenAI（纯对话）
# protocol: openai
# model: gpt-4o
# base_url: https://api.openai.com/v1
# api_key: sk-...
"""


def user_config_path() -> Path:
    """
    返回用户级全局配置文件路径 ~/.rhinecode/config.yaml。

    这是「命令未显式传 --config 时」的缺省配置位置：把 api_key 等全局设置放在
    用户主目录下的固定位置，使 `rhine` 在任意工作目录都能读到同一份配置
    （工作目录本身仍作为 AI 操作的项目根，二者互不影响）。

    :returns: ~/.rhinecode/config.yaml 的 Path（不保证文件已存在）
    """
    return Path.home() / _CONFIG_DIR_NAME / _CONFIG_FILE


def scaffold_user_config(path: Path) -> bool:
    """
    在指定路径生成配置模板，供首次运行引导使用。

    执行步骤：
    1. 若目标文件已存在，直接返回 False，绝不覆盖用户已有配置（幂等、防误伤）。
    2. 创建父目录（parents=True, exist_ok=True）。
    3. 写入 _CONFIG_TEMPLATE 模板（含占位 api_key）。

    :param path: 目标配置文件路径（通常是 user_config_path()）
    :returns: 实际写入了模板返回 True；文件已存在未改动返回 False
    :raises OSError: 目录创建或文件写入失败时抛出（由调用方决定如何提示）

    副作用：可能创建目录并写入文件。
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    return True


@dataclass
class Config:
    """
    LLM 供应商配置。

    字段说明：
    - protocol：后端协议类型，决定使用哪个 Provider 实现（anthropic / openai / deepseek）
    - model：模型名称，直接传给 API（例如 claude-sonnet-4-6、gpt-4o、deepseek-chat）
    - base_url：API 请求基础地址，支持自定义代理或私有部署
    - api_key：身份认证密钥，仅在运行时内存中使用，不打印到界面或日志
    - debug_log：是否把每次请求的缓存命中/未命中 token 追加到 <项目根>/.rhinecode_debug.log，
                 用于验证缓存策略是否生效（c5 F10）。可选字段，缺省为 True；每次请求仅写一行，
                 IO 异常会静默降级，不影响对话。不想生成该文件时在配置里设为 false。
    """
    protocol: str
    model: str
    base_url: str
    api_key: str
    # 调试日志开关：默认开启便于随时验证缓存；非必填字段，老配置不写也能正常加载。
    debug_log: bool = True


def _parse_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError(f"配置字段 {field_name} 必须是布尔值 true/false")


def load(path: str) -> Config:
    """
    从指定路径加载并校验 YAML 配置文件。

    执行步骤：
    1. 打开并解析 YAML 文件
    2. 逐一检查四个必填字段是否存在且非空
    3. 构造并返回 Config 对象

    :param path: 配置文件的文件系统路径
    :returns: 填充完毕的 Config 对象
    :raises FileNotFoundError: 文件路径不存在时抛出，错误信息包含路径
    :raises ValueError: 任意必填字段缺失或为空时抛出，错误信息包含字段名

    副作用：无（纯读取，不修改任何状态）
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"{path} 配置文件不存在")
    except yaml.YAMLError as e:
        raise ValueError(f"配置文件 YAML 解析失败 {e}") from e

    if not isinstance(data, dict):
        raise ValueError("顶层必须是 YAML 对象，且包含 protocol/model/base_url/api_key")

    # 逐字段校验，确保错误信息精确到具体缺失的字段，方便用户定位问题
    for field in ("protocol", "model", "base_url", "api_key"):
        if not data.get(field):
            raise ValueError(f"配置文件缺少必填字段 {field}")

    # debug_log 为可选项：缺省为 True，字符串写法需显式表达 true/false，避免 "false" 被当成 True。
    debug_log = _parse_bool(data.get("debug_log", True), "debug_log")

    return Config(
        protocol=data["protocol"],
        model=data["model"],
        base_url=data["base_url"],
        api_key=data["api_key"],
        debug_log=debug_log,
    )
