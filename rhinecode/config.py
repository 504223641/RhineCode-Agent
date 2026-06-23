"""
配置加载模块。

负责从 YAML 文件中读取 LLM 供应商信息，并校验四个必填字段。
配置文件示例见项目根目录的 config.example.yaml。

注意：api_key 属于敏感信息，config.yaml 已加入 .gitignore，禁止提交到版本库。
"""

from dataclasses import dataclass
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
    """
    protocol: str
    model: str
    base_url: str
    api_key: str


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

    # 逐字段校验，确保错误信息精确到具体缺失的字段，方便用户定位问题
    for field in ("protocol", "model", "base_url", "api_key"):
        if not data.get(field):
            raise ValueError(f"配置文件缺少必填字段 {field}")

    return Config(
        protocol=data["protocol"],
        model=data["model"],
        base_url=data["base_url"],
        api_key=data["api_key"],
    )
