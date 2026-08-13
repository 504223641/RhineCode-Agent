"""
运行模式预设（auto-plan 扩展）。

## 这个模块解决什么问题

本扩展之前，用户面对的是**两个开关六种组合**：

- `/perm` 三档（严格 / 默认 / 放行）—— 权限管线**第④层**的兜底档，
  决定「③规则未命中的灰色地带怎么算」；
- `/plan` 布尔量 —— **阶段状态机**，决定「先规划还是直接干」。

两者正交，笛卡尔积是六种组合，而其中四种从来没人用过
（「严格档 + 计划模式」是什么意思？）。用户要在脑子里同时维护两个变量。

本模块把它们收成**两个预设**：`auto` 与 `plan`，`Shift+Tab` 两态循环。

## 预设不是第三条状态轴

⚠ **这是本模块最重要的一条，破坏它不会报错，只会让界面开始撒谎。**

预设是**两条既有轴上的一个命名坐标**，不是新状态。类比：相机的「夜景模式」
不是第三个旋钮，而是「光圈 + 快门」两个旋钮的一组固定值。

因此本模块**无状态、纯映射**：系统里可变的运行期状态仍然只有

- `PermissionEngine.mode`（轴一：权限档）
- `ConversationManager.plan_mode`（轴二：规划阶段）

两个。「当前是哪个预设」永远由这两者**推导**而来
（`ConversationManager.preset` 是唯一的推导点，spec N5）。

单独存一份「当前预设」的后果很实际：它与两条轴迟早对不上，
表现为**状态栏说 `auto`、实际却在规划阶段**——而那种不一致最难查，
因为两边各自看都是对的。

## ⚠ 一处会让人误以为是冗余的地方

当前两个预设**共用同一个权限档**（都是 `PERMISSIVE`，见 spec F2），
于是 `preset_of` 实际上只看 `plan_mode`、根本没用到档位那条轴：

    preset = PLAN if plan_mode else AUTO

看起来「那还叫什么两条轴」。**结构仍然是两条轴，只是当前这版预设在档位那条轴
上取了同一个点。** 权威定义在 `PRESET_AXES` 里，`preset_of` 是它的逆向查表。

所以 **`PRESET_AXES` 里的档位字段不是冗余，别删**。它有两个作用：

1. 它是 spec **AC6a**（来回切换后两条轴逐字复原）唯一的可断言对象；
2. 将来若真出现档位不同的第三个预设，改那张表即可，推导逻辑跟着走。

删掉它的话，新预设会**静默地不生效**——切过去了但档位没变，界面上看不出来。

## 为什么 `plan` 不动权限档（spec F2 与 todo 原文的刻意分歧）

todo 原文写的是 `plan = 只读 + 规划阶段开`，落地时改成「不动权限档」。两条理由：

1. **权限档里没有「只读」这个值**，最接近的 `STRICT` 在规划阶段是**空转**：
   规划阶段暴露的工具全是只读的，而只读请求在 `engine._decide_core` 里
   **走只读短路、压根不进第④层**（那一行在 ④ 的判断之前 return）。
   `STRICT` 与 `PERMISSIVE` 在规划阶段的每一次判定结果**逐字相同**。

2. **（硬的）获批发生在回合中途。** `agent/loop.py` 里 `execution_phase` 是每轮的
   局部变量，`present_plan` 获批后**同一回合内**立刻放开全部工具。若 `plan` 预设
   动了档位，就得从 Agent 循环里回头改那个**单实例共享**的引擎的 `mode`
   ——而那正是 `CLAUDE.md` 明令禁止的形态（「权限必须 `derive()` 派生，
   绝不改主引擎的 `mode`」）。不改档位则这个问题整个不存在。

规划阶段「一个文件都改不了」由**工具过滤 + 预扫拦截**两道保证
（后者即已知工程项 #2 修掉的那条），**都不看权限档**。

## 依赖

只依赖 `permission.models.PermissionMode`。是**叶子模块**，谁都能引它——
放进 `permission/` 包里的话，那个包会开始知道「规划阶段」这个与权限无关的概念。
"""

from enum import Enum

from rhinecode.permission.models import PermissionMode


class Preset(str, Enum):
    """
    用户可见的两个运行模式。

    取值即显示名（状态栏与命令回显直接用 `.value`），故刻意用小写英文——
    与 `permissions.yaml` 里的档位字面量风格一致，也便于用户口头指代。
    """

    AUTO = "auto"
    PLAN = "plan"


# 预设 → (权限档, 规划阶段是否开启) 的**权威定义**。
#
# ⚠ **这张表是「预设 = 两条轴的组合」这个结构的唯一落点，别当冗余删掉。**
#    当前两个预设的档位恰好相同（都是 PERMISSIVE），于是档位那一列看起来没用——
#    但它是 spec AC6a 唯一的可断言对象，也是将来加预设时的落点。
#    删掉之后新预设会「切过去了但档位没变」，而界面上完全看不出来。
PRESET_AXES: dict[Preset, tuple[PermissionMode, bool]] = {
    # auto：放行档兜底 + 规划阶段关 —— 放手干活
    Preset.AUTO: (PermissionMode.PERMISSIVE, False),
    # plan：档位**不变** + 规划阶段开 —— 先看方案。
    # 「只读」由规划阶段的工具过滤保证，与权限档无关（理由见模块 docstring）。
    Preset.PLAN: (PermissionMode.PERMISSIVE, True),
}

# `Shift+Tab` 与 `/mode` 的两态循环顺序。
PRESET_CYCLE: dict[Preset, Preset] = {
    Preset.AUTO: Preset.PLAN,
    Preset.PLAN: Preset.AUTO,
}

# 启动缺省预设（spec F3）。
#
# 它与 `PermissionEngine.load` 的默认参数（仍是 DEFAULT 档）**刻意不一致**：
# 那个默认值服务于大量直接构造引擎的测试，改它等于静默翻掉一批基线的前提。
# 主对话的启动档在 `conversation.py` 的构造点上**显式传入**。
DEFAULT_PRESET: Preset = Preset.AUTO

# 权限档的显示名（spec F16）。
#
# ⚠ **`PERMISSIVE` 显示成 "auto" 是刻意的**：`auto` 预设内部就是这个档位，
#    两处叫同一个名字，用户才不会以为它们是两回事。
#
# ⚠ **三档必须都留着。** `/perm` 命令虽已删除，但 `STRICT` / `DEFAULT` 仍可经
#    两条路径抵达：`permissions.yaml`，以及角色定义的 `permission_mode` 字段
#    （内置的 `explorer` / `planner` 就声明了 `strict`）。手法对齐 Claude Code
#    的 `dontAsk`——那个档位存在、可被显式指定，只是永不进用户的切换循环。
MODE_LABELS: dict[PermissionMode, str] = {
    PermissionMode.STRICT: "严格（strict）",
    PermissionMode.DEFAULT: "默认（default）",
    PermissionMode.PERMISSIVE: "auto",
}


def preset_of(plan_mode: bool) -> Preset:
    """
    从运行期状态推导当前预设。

    :param plan_mode: 规划阶段是否开启（`ConversationManager.plan_mode`）
    :returns: 对应的预设

    ⚠ **只看 `plan_mode`，不看权限档**——这是 `PRESET_AXES` 当前取值的**结果**
    而不是设计上的简化：两个预设的档位相同，档位就无法区分它们。
    加入档位不同的第三个预设时，本函数要改成对 `PRESET_AXES` 的完整反查。

    副作用：无（纯函数）。
    """
    return Preset.PLAN if plan_mode else Preset.AUTO


def axes_of(preset: Preset) -> tuple[PermissionMode, bool]:
    """
    取一个预设对应的两条轴的取值。

    :param preset: 预设
    :returns: `(权限档, 规划阶段是否开启)`
    :raises KeyError: 传入未登记的预设（新增 `Preset` 成员却漏改 `PRESET_AXES`
            时当场暴露，好过静默按某个默认值处理）

    副作用：无（纯函数）。
    """
    return PRESET_AXES[preset]


def next_preset(preset: Preset) -> Preset:
    """
    两态循环里的下一个预设。

    :param preset: 当前预设
    :returns: 下一个预设
    :raises KeyError: 传入未登记的预设（理由同 `axes_of`）

    副作用：无（纯函数）。
    """
    return PRESET_CYCLE[preset]
