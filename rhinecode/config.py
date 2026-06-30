"""
配置加载模块。

负责从 YAML 文件中读取 LLM 供应商信息，并校验四个必填字段。
配置文件示例见项目根目录的 config.example.yaml。

注意：api_key 属于敏感信息，config.yaml 已加入 .gitignore，禁止提交到版本库。
"""

from dataclasses import dataclass
from typing import Any
import yaml


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
