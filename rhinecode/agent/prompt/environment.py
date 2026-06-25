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
import subprocess
from dataclasses import dataclass

from rhinecode.config import Config
from rhinecode.agent.prompt.texts import ENVIRONMENT_TEMPLATE

# 无法获取分支时（非 git 仓库、未安装 git、detached HEAD、超时等）统一用这个占位词，
# 让模型明确知道「当前没有分支信息」，而不是看到空字符串误以为字段缺失。
_NO_BRANCH = "无"


@dataclass
class EnvironmentInfo:
    """
    一次运行的环境快照。

    :param working_dir: 项目根（启动 RhineCode 时的工作目录）绝对路径；模型据此理解文件路径基准
    :param platform: 操作系统/平台描述（如 Windows-11-...）；影响模型生成命令时的语法假设
    :param date: 当前日期（ISO 格式 yyyy-mm-dd）；给模型对「最近 / 今年」等相对时间一个参照
    :param git_branch: 当前 git 分支名；非 git 仓库或获取失败时为「无」（见 _detect_git_branch）
    :param model: 当前使用的模型名
    :param protocol: 当前 Provider 协议（anthropic / openai / deepseek）
    """

    working_dir: str
    platform: str
    date: str
    git_branch: str
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
            git_branch=self.git_branch,
            model=self.model,
            protocol=self.protocol,
        )


def _detect_git_branch(project_root: str) -> str:
    """
    探测 project_root 所在 git 仓库的当前分支名。

    执行步骤：
    1. 用 `git -C <project_root> branch --show-current` 查询当前分支
       （-C 让 git 切到目标目录执行，不改变本进程 cwd）。
    2. returncode 非 0（非 git 仓库等）→ 视为无分支。
    3. 输出为空（detached HEAD，即不在任何分支上）→ 也视为无分支。
    4. 任何异常（git 未安装抛 FileNotFoundError、目录不存在、超时等）→ 视为无分支。

    设计取舍：失败一律「降级」返回占位词而非抛错——环境信息只是给模型的背景参考，
    采集失败不应中断整个 Agent 运行。git 调用是只读的，不改动仓库状态。

    :param project_root: 项目根绝对路径，作为 git 的工作目录
    :returns: 当前分支名；无法获取时返回 _NO_BRANCH（"无"）

    副作用：以子进程方式运行一次只读的 git 命令（设 2 秒超时，避免卡住主流程）。
    """
    try:
        result = subprocess.run(
            ["git", "-C", project_root, "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return _NO_BRANCH
        branch = result.stdout.strip()
        return branch if branch else _NO_BRANCH
    except Exception:
        # 包含 git 未安装、目录非法、超时等所有情况，统一降级为「无」。
        return _NO_BRANCH


def collect_environment(config: Config, project_root: str) -> EnvironmentInfo:
    """
    采集当前运行环境信息。

    执行步骤：
    1. 工作目录取传入的 project_root（由上层确定为启动时的项目根，与工具路径边界一致）。
    2. 平台用标准库 platform.platform()。
    3. 日期用 datetime.date.today() 的 ISO 字符串（每次运行实时取，跨午夜会变）。
    4. git 分支用 _detect_git_branch 探测（非 git 仓库/获取失败时为「无」）。
    5. 模型名与 protocol 取自配置。

    :param config: 运行配置，提供 model 与 protocol
    :param project_root: 项目根绝对路径
    :returns: 填充好的 EnvironmentInfo

    副作用：会以子进程方式运行一次只读的 git 命令探测分支（见 _detect_git_branch）；
            不发网络请求、不修改任何文件或仓库状态。
    """
    return EnvironmentInfo(
        working_dir=project_root,
        platform=_platform.platform(),
        date=datetime.date.today().isoformat(),
        git_branch=_detect_git_branch(project_root),
        model=config.model,
        protocol=config.protocol,
    )
