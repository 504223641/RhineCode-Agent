"""
配置写盘：**在现有文本上定点替换，保住注释**（first-run-setup 扩展 T5/T6，spec F11）。

## 为什么不用 `yaml.dump`

一行 `yaml.safe_dump(data)` 能把配置写出来，而且绝对正确——代价是把
`_CONFIG_TEMPLATE` 里那六十多行注释**全部抹掉**。

那些注释是本项目重要的文档载体：`context_window` 为什么是 1000000、
`stream_idle_timeout` 为什么不是总时长上限、分类器会丢弃哪些过宽规则……
**多数用户只会读这份模板，不会去翻 `docs/internals/config.md`。**
写一次配置就把它们清空，等于每个走过向导的人从此少了一份说明书。

所以这里做的是**文本级的定点替换**：只动目标那一行，其余一个字节不碰。

## `set_scalar` 的三种情形

| 文本里的状态 | 做法 |
| --- | --- |
| 有该 key 的**生效行** | 替换它的值，**行尾注释保留** |
| 只有**被注释掉的模板行**（`# context_window: 1000000`） | 在其后插入生效行，注释行原样留着 |
| 两者都没有 | 追加到文件末尾，前面带一行来源说明 |

第二种情形里「注释行原样留着」是刻意的：那一行不是废行，它带着整段说明
（往上还有好几行 `#` 开头的依据）。删掉它等于把说明和值一起搬走了。

## ⚠ 绝不覆盖一份读不出来的文件

Windows 中文环境下手写的 `config.yaml` 可能是 **GBK 编码**的。那种文件
`config.load()` 读不出来（`UnicodeDecodeError`，而它是 `ValueError` 的子类，
于是 `trigger.classify` 判 `INVALID`、向导被拉起来）——但**它的内容对用户
是有价值的**。

因此 `apply` 遇到「文件存在、却读不出来」时**先改名备份再写新的**，
并把备份路径一并交回去让界面显示出来。只判 INVALID 而写盘直接覆盖，
等于把用户的配置悄悄删了。这是 T4 实测撞出来的边界，两处注释成对存在。

⚠ **「GBK 配置」其实有两种形态，本机制只覆盖得了一种，别以为都堵上了。**
一份 GBK 文件是不是合法 UTF-8，**取决于里面具体是哪些汉字**：
`中` 的 GBK 编码 `D6 D0` 解不出来（`D0` 不是合法的后续字节），
而 `一` 的 `D2 BB` **恰好是一个合法的 UTF-8 双字节序列**——整份文件读得出来，
只是内容是乱码，一个错都不报。

- 解不出来的 → 本机制生效：判 INVALID、进向导、备份后重写。
- **解得出来但是乱码的 → 完全无从察觉**：配置带着一堆乱码值正常加载。

后者不在本扩展范围内（也没有可靠的检测手段），记在这里免得下一个人以为
已经覆盖到了。护栏见 `tests/test_setup_writer.py` 那条同名说明。
"""

import re
from pathlib import Path
from typing import Optional, Union

import yaml

from rhinecode.config import _CONFIG_TEMPLATE, DEFAULT_CONTEXT_WINDOW, DEFAULT_MODEL
from rhinecode.setup.models import SetupDraft

# 向导写盘时**不加引号也不折行**的官方默认地址。
# 单独取名是为了让 `read_current` 在读不出 base_url 时有个诚实的回退值。
_DEFAULT_BASE_URL = "https://api.deepseek.com"

# 顶层生效行：行首**没有任何缩进**，`键: 值`。
#
# ⚠ 「行首无缩进」这个约束是本正则的全部要害。少了它，`worktree:` 段里的
# `  cleanup_days: 7` 会被当成顶层键——于是想改顶层某项时改到了嵌套项上，
# 而 YAML 照样解析得通，只是配置的含义整个变了。
_ACTIVE_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*:(?P<rest>.*)$")

# 被注释掉的顶层行：`#` 顶格，后面**最多一个空格**，然后是 `键:`。
#
# ⚠ 「最多一个空格」同样是要害。写成 `\s*` 的话，模板里那些缩进的注释
# （`#   cleanup_days: 7`、`#   enabled: true`）全都会被认成顶层键，
# 于是 `set_scalar(text, "enabled", ...)` 会在 `worktree` 段的注释中间
# 插一行顶层 `enabled:`。
_COMMENTED_LINE = re.compile(r"^#[ ]?(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*:")

# 不加引号也安全的标量。刻意保守：只放行明确无歧义的字符集。
#
# `:` 与 `/` 必须放行——`https://api.deepseek.com` 是最常见的值，而它在 YAML 里
# 是合法的平铺标量（`:` 只有在**后面跟空格**时才有分隔含义）。
_SAFE_PLAIN = re.compile(r"^[A-Za-z0-9_./:@+~-]+$")

# YAML 里有特殊含义、不能裸写的词。裸写 `no` 会被解析成布尔假，
# 而用户想要的是字符串 "no"。
_YAML_KEYWORDS = frozenset(
    {"true", "false", "null", "yes", "no", "on", "off", "~", "y", "n"}
)


def _looks_numeric(text: str) -> bool:
    """
    判断一段文本会不会被 YAML 当成数字。

    :param text: 待判定文本
    :returns: 会被当成数字则真

    用途：模型名叫 `123` 时必须加引号，否则读回来是 int 而不是 str。
    这种模型名当然不常见，但**判断成本是零，而漏判的表现是类型悄悄变了**。
    """
    try:
        float(text)
        return True
    except ValueError:
        return False


def _dump_scalar(value: Union[str, int]) -> str:
    """
    把一个标量序列化成能安全写进 YAML 的形式。

    :param value: 字符串或整数
    :returns: 可直接拼在 `键: ` 后面的文本

    规则：
    - 整数直接写。
    - 字符串在**明确安全**时裸写（可读性好，模板本来就是裸写的），
      否则用**单引号**包起来并把内部单引号翻倍（YAML 单引号串里不解析转义，
      比双引号安全——双引号会把 `\\n` 当换行，而 api_key 里出现反斜杠虽罕见却致命）。

    无副作用。
    """
    if isinstance(value, bool):
        # bool 是 int 的子类，必须排在前面判，否则 True 会被写成 "1"
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)

    text = str(value)
    safe = (
        bool(text)
        and _SAFE_PLAIN.match(text) is not None
        and text.lower() not in _YAML_KEYWORDS
        and not _looks_numeric(text)
        # 以 `-` / `?` / `:` 开头在 YAML 里有块结构含义，一律加引号
        and not text.startswith(("-", "?", ":"))
    )
    if safe:
        return text
    return "'" + text.replace("'", "''") + "'"


def _split_inline_comment(rest: str) -> tuple[str, str]:
    """
    把 `键:` 后面那一截拆成「值」和「行尾注释」。

    :param rest: `键:` 之后的全部文本（含前导空格）
    :returns: (值部分, 注释部分)；没有注释时注释部分是空串

    ⚠ **必须认引号**：`api_key: 'abc#def'` 里那个 `#` 是值的一部分，不是注释。
    朴素地按第一个 `#` 切会把密钥截断，而截断后的配置**照样能加载**
    （只是 key 短了一截），表现为「填了正确的 key 却报 401」。

    YAML 的规则是「`#` 前面必须有空白才算注释起始」，这里照此实现。

    无副作用。
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(rest):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            # `#` 要算注释起始，前一个字符必须是空白（或它就在最前面）
            if i == 0 or rest[i - 1] in " \t":
                # ⚠ 值那半必须 `strip()` 而不是 `rstrip()`：`rest` 是从
                # `键:` 之后原样切下来的，**带着前导空格**。只去尾部的话
                # 值会变成 " abc"，拼回去就成了 `key:  abc`（两个空格）——
                # YAML 照样解析得通，于是这个错误完全静默，只是文件越写越歪。
                return rest[:i].strip(), rest[i:].rstrip()
    return rest.strip(), ""


def set_scalar(text: str, key: str, value: Union[str, int]) -> str:
    """
    在一份 YAML 文本里把某个**顶层**键设成指定值，尽量不动其余内容。

    :param text: 原始 YAML 文本
    :param key: 顶层键名
    :param value: 标量值（字符串或整数）
    :returns: 新的 YAML 文本

    三种情形见模块 docstring。返回的文本**保持原有换行风格**
    （按 `\\n` 切分再拼回，不引入 `\\r\\n` 也不去掉已有的 `\\r`）。

    无副作用——纯字符串进、纯字符串出，因此可以脱离文件系统单独测试。
    """
    dumped = _dump_scalar(value)
    lines = text.split("\n")

    # 情形 1：已有生效行 → 就地替换值，行尾注释保留
    for i, line in enumerate(lines):
        match = _ACTIVE_LINE.match(line)
        if match and match.group("key") == key:
            _, comment = _split_inline_comment(match.group("rest"))
            lines[i] = f"{key}: {dumped}" + (f"  {comment}" if comment else "")
            return "\n".join(lines)

    # 情形 2：只有被注释掉的模板行 → 在它后面插入生效行，注释行原样留着
    for i, line in enumerate(lines):
        match = _COMMENTED_LINE.match(line)
        if match and match.group("key") == key:
            lines.insert(i + 1, f"{key}: {dumped}")
            return "\n".join(lines)

    # 情形 3：两者都没有 → 追加到末尾，带一行来源说明
    #
    # 说明那一行不是装饰：一个用户回头看到文件末尾凭空多出几行配置时，
    # 「这是谁写的」是他的第一个问题。
    tail = list(lines)
    while tail and not tail[-1].strip():
        tail.pop()
    tail.append("")
    tail.append("# 以下由 RhineCode 配置向导写入（/setup 可随时重来）")
    tail.append(f"{key}: {dumped}")
    tail.append("")
    return "\n".join(tail)


def read_current(path: Path) -> Optional[SetupDraft]:
    """
    读出当前配置里可供向导预填的几项。

    :param path: 配置文件路径
    :returns: 预填草稿；文件不存在、读不出来或结构不对时返回 None
        （调用方据此走「什么都不预填」的路径，而不是拿一堆空字符串去填框）

    ⚠ **`api_key` 一律回空串，绝不把真实密钥读进草稿。** 两个理由，各自独立成立：

    1. 空串在本扩展里的语义就是「不改」（spec F16）——`/setup` 重跑时
       用户不必为了换个模型把密钥重新粘一遍。
    2. 读进来就意味着它会被摆到界面上、进到某个 widget 的属性里。
       没有必要，就不要碰它。

    副作用：读一次文件。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # 读不出来（不存在、没权限、不是 UTF-8）一律当「没有可预填的东西」。
        # ⚠ 这里**吞掉异常是对的**：预填是锦上添花，读不到就不填，
        # 绝不能因为预填失败把整个向导拦住。真正要紧的是 `apply` 那一侧
        # 不许覆盖这种文件——见下面那个函数。
        return None

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None

    model = data.get("model") or DEFAULT_MODEL
    base_url = data.get("base_url") or _DEFAULT_BASE_URL
    window = data.get("context_window")
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        window = DEFAULT_CONTEXT_WINDOW

    return SetupDraft(
        api_key="",
        base_url=str(base_url),
        model=str(model),
        context_window=window,
    )


def _backup_unreadable(path: Path) -> Path:
    """
    把一份读不出来的配置改名备份，返回备份路径。

    :param path: 原配置文件
    :returns: 备份后的路径（形如 `config.yaml.bak`，重名时递增编号）

    ⚠ **重名时必须递增，不能覆盖已有备份。** 用户反复走向导时，
    第二次备份把第一次的覆盖掉，等于备份这件事没做。

    副作用：重命名文件。
    """
    index = 0
    while True:
        suffix = ".bak" if index == 0 else f".bak{index}"
        candidate = path.with_name(path.name + suffix)
        if not candidate.exists():
            path.rename(candidate)
            return candidate
        index += 1


def apply(path: Path, draft: SetupDraft) -> tuple[Path, ...]:
    """
    把草稿写进配置文件，保住注释与用户自己写的其它内容。

    执行步骤：
    1. 取**基底文本**：
       - 文件存在且读得出来 → 用它自己的内容（这样用户手写的 `worktree` /
         `classifier` / `search` 三段原样保留）；
       - 文件不存在 → 用 `_CONFIG_TEMPLATE`；
       - **文件存在但读不出来** → 先改名备份，再用模板起手（见下方 ⚠）。
    2. 依次 `set_scalar` 写入 `protocol` / `model` / `base_url` /
       `context_window`，**`api_key` 仅在非空时写**。
    3. 建父目录、写盘。

    :param path: 目标配置文件路径
    :param draft: 用户在向导里填好的草稿
    :returns: 本次实际动过的文件路径元组；备份发生时备份路径也在里面
        （界面据此告诉用户「你原来那份已经存到哪儿了」）
    :raises OSError: 建目录或写盘失败时抛出，由调用方决定如何提示

    ⚠ **绝不覆盖一份读不出来的文件。** Windows 中文环境下手写的
    `config.yaml` 可能是 GBK 编码的，那种文件 `config.load()` 读不出来
    （`UnicodeDecodeError` 是 `ValueError` 的子类，于是 `trigger.classify`
    判 `INVALID`、向导被拉起来）——但它的内容对用户是有价值的。
    只判 INVALID 而写盘直接覆盖，等于把用户的配置悄悄删了。
    这是 T4 实测撞出来的边界，`setup/trigger.py` 里有一段成对的说明。

    副作用：写文件；可能创建目录；可能重命名原文件为备份。
    """
    written: list[Path] = []

    if path.exists():
        try:
            base = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            written.append(_backup_unreadable(path))
            base = _CONFIG_TEMPLATE
    else:
        base = _CONFIG_TEMPLATE

    text = base
    text = set_scalar(text, "protocol", "deepseek")
    text = set_scalar(text, "model", draft.model)
    text = set_scalar(text, "base_url", draft.base_url)
    text = set_scalar(text, "context_window", draft.context_window)

    # ⚠ **空串表示「不改」，因此整个跳过这一键**（spec F16）。
    # 写成 `api_key: ''` 会**静默清空用户的密钥**，而他要到下次启动
    # 才会发现——那时看到的是「api_key 为空」，完全联想不到是上次
    # 在 /setup 里换了个模型导致的。
    if draft.api_key:
        text = set_scalar(text, "api_key", draft.api_key)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written.append(path)
    return tuple(written)
