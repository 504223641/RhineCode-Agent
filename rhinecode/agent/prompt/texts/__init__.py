"""
提示词文案子包（c5 重构）。

集中存放所有 prompt 中文文案，按「不同 prompt 拆到不同 .py」组织，与逻辑代码分离：
- 七个固定模块各一个文件（identity / system_constraints / task_mode / action_execution /
  tool_usage / tone / text_output）；
- plan       ：Plan Mode 完整/精简文案；
- environment：环境信息渲染模板。

逻辑层（modules.py / reminders.py / environment.py）只需从本子包一行导入对应常量，
无需关心文案落在哪个文件。改 prompt 措辞 = 改对应文件里的字符串常量，不触碰任何逻辑。
"""

from rhinecode.agent.prompt.texts.identity import IDENTITY
from rhinecode.agent.prompt.texts.system_constraints import SYSTEM_CONSTRAINTS
from rhinecode.agent.prompt.texts.task_mode import TASK_MODE
from rhinecode.agent.prompt.texts.action_execution import ACTION_EXECUTION
from rhinecode.agent.prompt.texts.tool_usage import TOOL_USAGE
from rhinecode.agent.prompt.texts.tone import TONE
from rhinecode.agent.prompt.texts.text_output import TEXT_OUTPUT
from rhinecode.agent.prompt.texts.plan import PLAN_FULL, PLAN_BRIEF
from rhinecode.agent.prompt.texts.environment import ENVIRONMENT_TEMPLATE

__all__ = [
    "IDENTITY",
    "SYSTEM_CONSTRAINTS",
    "TASK_MODE",
    "ACTION_EXECUTION",
    "TOOL_USAGE",
    "TONE",
    "TEXT_OUTPUT",
    "PLAN_FULL",
    "PLAN_BRIEF",
    "ENVIRONMENT_TEMPLATE",
]
