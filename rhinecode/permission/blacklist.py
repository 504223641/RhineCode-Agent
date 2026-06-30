"""
①危险命令黑名单（spec F2）—— 决策管线的第一道、也是最硬的一道防线。

职责：对 run_command 的命令字符串，在执行前用一组**固定的、代码内置的**正则匹配
已知高危操作；命中即拒，且这层**不可被任何配置、权限模式或人工确认放开**
（circuit breaker）。这是「即使用户手滑点了确认/开了放行档，rm -rf / 也照样拦」的依据。

实现要点：
- 匹配前先用 matching.split_commands 把复合命令拆段，逐段检查——任一段命中即整条拒绝，
  防止 `git status && rm -rf /` 这类把危险命令藏在分隔符后的绕过（spec AC2）。
- 正则用 re.IGNORECASE，并同时覆盖 Unix（rm/dd/mkfs/git）与 Windows/PowerShell
  （format/rd/del/Remove-Item/vssadmin）的高危形式，因为本项目运行在 Windows（spec N8）。
- 黑名单宁可错拒不可错放（fail-safe）：模式偏保守，命中即判危险。

新增危险模式：在 DANGEROUS_PATTERNS 追加 (编译正则, 中文原因) 即可，无需改动其它层。
"""

import re
from typing import Optional

from rhinecode.permission.matching import split_commands

# 危险命令模式表：每项为 (已编译正则, 命中时的中文原因)。
# 正则用 search（子串命中即算），统一 IGNORECASE。
DANGEROUS_PATTERNS: list[tuple["re.Pattern[str]", str]] = [
    # —— 递归强删（rm 同时带 recursive 与 force 标志，最典型的不可挽回操作）——
    (
        re.compile(
            r"\brm\b(?=.*(?:\s-\S*r|--recursive))(?=.*(?:\s-\S*f|--force))",
            re.IGNORECASE,
        ),
        "递归强删：rm 同时带 -r/-R 与 -f 标志，会不可恢复地删除整棵目录",
    ),
    # —— 磁盘/分区破坏 ——
    (re.compile(r"\bmkfs(\.\w+)?\b", re.IGNORECASE), "磁盘破坏：mkfs 会格式化文件系统"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/", re.IGNORECASE), "磁盘破坏：dd 直接写入块设备"),
    (re.compile(r"\bfdisk\b", re.IGNORECASE), "磁盘破坏：fdisk 修改分区表"),
    (re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE), "磁盘破坏：向块设备重定向写入"),
    # —— git 破坏性操作（丢弃未提交改动 / 改写历史）——
    (
        re.compile(r"\bgit\b[^\n]*\breset\b[^\n]*--hard", re.IGNORECASE),
        "git 破坏性：reset --hard 会丢弃未提交改动",
    ),
    (
        re.compile(r"\bgit\b[^\n]*\bpush\b[^\n]*(--force\b|--force-with-lease\b|\s-f\b)", re.IGNORECASE),
        "git 破坏性：push --force 会改写远端历史",
    ),
    (
        re.compile(r"\bgit\b[^\n]*\bclean\b[^\n]*-\S*f", re.IGNORECASE),
        "git 破坏性：clean -f 会删除未跟踪文件",
    ),
    # —— fork 炸弹（耗尽进程资源使系统瘫痪）——
    (
        re.compile(r"\(\s*\)\s*\{\s*[^}]*\|[^}]*&\s*\}", re.IGNORECASE),
        "fork 炸弹：自我复制函数会耗尽系统进程",
    ),
    # —— 向关键系统路径写入/删除 ——
    (re.compile(r">\s*/(etc|boot|sys|proc)/", re.IGNORECASE), "系统破坏：向关键系统目录重定向写入"),
    (re.compile(r"\brm\b[^\n]*\s/(\s|$)", re.IGNORECASE), "系统破坏：rm 直接以根目录 / 为目标"),
    # —— Windows / PowerShell 高危形式（本项目运行在 Windows，spec N8）——
    (re.compile(r"\bformat\b\s+[a-zA-Z]:", re.IGNORECASE), "磁盘破坏：format 格式化整个盘符"),
    (re.compile(r"\b(rd|rmdir)\b[^\n]*/s", re.IGNORECASE), "递归强删：rd/rmdir /s 递归删除目录"),
    (re.compile(r"\bdel\b[^\n]*/s", re.IGNORECASE), "递归强删：del /s 递归删除文件"),
    (
        re.compile(r"\bRemove-Item\b(?=.*-Recurse)(?=.*-Force)", re.IGNORECASE),
        "递归强删：Remove-Item -Recurse -Force 会不可恢复地删除整棵目录",
    ),
    (
        re.compile(r"\bvssadmin\b[^\n]*\bdelete\b[^\n]*shadows", re.IGNORECASE),
        "系统破坏：vssadmin delete shadows 删除卷影副本（勒索软件常用手法）",
    ),
]


def check_command(command: str) -> Optional[str]:
    """
    检查一条命令是否命中危险命令黑名单。

    执行流程：
    1. 候选串 = 整条原始命令 + split_commands 拆出的各子命令。
       - 整条命令：保证像 fork 炸弹 `:(){ :|:& };:` 这种「分隔符本身就是其语法一部分」
         的危险结构不会被拆碎而漏检（这类结构拆段后反而匹配不到）。
       - 各子命令：保证 `safe && rm -rf` 这类把危险命令藏在分隔符后的也被独立检查。
       由于这里用 search（子串匹配），整条命令本就是各段的超集，二者并集最稳妥（fail-safe）。
    2. 对每个候选串依次用 DANGEROUS_PATTERNS 匹配；任一命中即返回中文原因（短路）。
    3. 全部安全 → 返回 None。

    :param command: run_command 的原始命令字符串
    :returns: 命中时返回中文危险原因；未命中返回 None

    副作用：无（纯匹配）。
    """
    candidates = [command, *split_commands(command)]
    for candidate in candidates:
        for pattern, reason in DANGEROUS_PATTERNS:
            if pattern.search(candidate):
                return reason
    return None
