"""
环境信息采集（c5 F2）。

环境信息属于「动态内容」：工作目录、平台、日期、模型等会随运行环境变化，
因此它不进入可缓存的稳定前缀，而是走消息通道（被包进 <system-reminder> 注入，见 reminders.py）。

为什么把它单独成一个模块：
- 采集逻辑（读 cwd / 平台 / 日期 / 配置）与「如何渲染成文本」「往哪注入」解耦；
- 将来要加字段（如 git 分支）只改这里，不影响拼装器与循环。
"""

import datetime
import platform as _platform
from dataclasses import dataclass

from rhinecode.config import Config
from rhinecode.agent.prompt.texts import ENVIRONMENT_TEMPLATE


@dataclass
class EnvironmentInfo:
    """
    一次运行的环境快照。

    :param working_dir: 项目根（启动 RhineCode 时的工作目录）绝对路径；模型据此理解文件路径基准
    :param platform: 操作系统/平台描述（如 Windows-11-...）；影响模型生成命令时的语法假设
    :param date: 当前日期（ISO 格式 yyyy-mm-dd）；给模型对「最近 / 今年」等相对时间一个参照
    :param model: 当前使用的模型名
    :param protocol: 当前 Provider 协议（anthropic / openai / deepseek）
    """

    working_dir: str
    platform: str
    date: str
    model: str
    protocol: str

    def render(self) -> str:
        """
        渲染成多行文本，供注入到 <system-reminder> 中。

        :returns: 形如「工作目录: ... / 系统: ... / 日期: ... / 模型: ...（protocol）」的多行字符串

        副作用：无（纯字符串拼接）。
        """
        # 文案模板在 texts/environment.py，这里只负责把字段值填进占位符。
        return ENVIRONMENT_TEMPLATE.format(
            working_dir=self.working_dir,
            platform=self.platform,
            date=self.date,
            model=self.model,
            protocol=self.protocol,
        )


def collect_environment(config: Config, project_root: str) -> EnvironmentInfo:
    """
    采集当前运行环境信息。

    执行步骤：
    1. 工作目录取传入的 project_root（由上层确定为启动时的项目根，与工具路径边界一致）。
    2. 平台用标准库 platform.platform()。
    3. 日期用 datetime.date.today() 的 ISO 字符串（每次运行实时取，跨午夜会变）。
    4. 模型名与 protocol 取自配置。

    :param config: 运行配置，提供 model 与 protocol
    :param project_root: 项目根绝对路径
    :returns: 填充好的 EnvironmentInfo

    副作用：无（只读取系统信息，不跑 git、不发请求）。
    """
    return EnvironmentInfo(
        working_dir=project_root,
        platform=_platform.platform(),
        date=datetime.date.today().isoformat(),
        model=config.model,
        protocol=config.protocol,
    )
