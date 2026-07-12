"""
锁文件原语（c9 F22/F23/F24）：跨进程互斥的最小机制，不含任何业务语义。

解决的问题：同一台机器上开两个终端各跑一个 RhineCode，两个进程可能同时写
同一份记忆（笔记 / 索引）或同一个会话存档。锁文件用「一个特殊文件存不存在」
当信号量：创建成功 = 拿到锁；创建失败（已存在）= 别人正拿着，调用方退让。

三个关键设计（与 spec F24 对应）：
1. **原子创建**——不能写成「先检查存在、再创建」两步（两个进程可能同时检查到
   「不存在」然后都创建成功，锁形同虚设）。os.open 带 O_CREAT|O_EXCL 是操作系统
   保证的「不存在则创建、存在则报错」一步操作，Windows / Linux 语义一致。
2. **非阻塞退让**——拿不到锁一律立即返回 False，绝不循环等待。业务方自己决定
   退让策略（笔记跳过本轮 / 会话拒绝载入），这里只提供机制（机制与策略分离）。
3. **过期自愈**——进程崩溃可能留下残留锁（stale lock）把后续写入永久堵死。
   锁文件的 mtime 距今超过调用方给的阈值即视为残留，可被清除接管；锁内容写入
   持有者 PID 与时间戳仅用于人工排查，不参与判定（跨平台探测 PID 存活远比
   mtime 判定复杂，见 plan 技术决策）。

所有函数都遵循 fail-safe：任何意外异常都转成「拿不到锁 / 静默」，绝不向上抛。
"""

import os
import time
from datetime import datetime
from pathlib import Path


def try_acquire(lock_path: Path, stale_after_seconds: float) -> bool:
    """
    尝试原子地获取锁（非阻塞）。

    执行流程：
    1. 用 os.open(O_CREAT | O_EXCL) 原子创建锁文件；成功则写入 PID + 时间戳，返回 True。
    2. 若文件已存在（FileExistsError）：检查其 mtime 是否超过 stale_after_seconds——
       超过视为崩溃残留，删除后**重试一次**原子创建（只重试一次：如果两个进程同时
       发现残留锁、同时删除，重试时仍由 O_EXCL 保证只有一个成功；不循环重试是为了
       避免两实例在「互相删对方刚建的锁」上打转）。
    3. 其余任何异常（目录不存在、无权限、mtime 读取失败等）→ 返回 False。
       宁可少写一次，不冒写坏记忆的风险（spec F24③）。

    :param lock_path: 锁文件路径（父目录需已存在；不存在按拿不到锁处理）
    :param stale_after_seconds: 过期阈值（秒）；锁的 mtime 距今超过该值视为残留
    :returns: True = 拿到锁（调用方负责在 finally 里 release）；False = 退让

    副作用：成功时创建锁文件并写入内容；发现残留锁时可能删除旧锁文件。
    """
    for attempt in range(2):
        try:
            # O_CREAT|O_EXCL：不存在则创建、存在则抛 FileExistsError，检查与创建同一原子动作。
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                payload = f"pid={os.getpid()}\ntime={datetime.now().isoformat()}\n"
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            # 已有人持锁：只有当它是过期残留时才清除并再试一次；新鲜锁直接退让。
            if attempt == 0 and _is_stale(lock_path, stale_after_seconds):
                try:
                    os.unlink(str(lock_path))
                except OSError:
                    # 删不掉（对方刚好也在删 / 权限问题）→ 退让，下轮再说。
                    return False
                continue
            return False
        except OSError:
            # 目录不存在、无权限等一律按「拿不到锁」的保守路径处理。
            return False
    return False


def release(lock_path: Path) -> None:
    """
    释放锁：删除锁文件。文件不存在或删除失败都静默——释放失败最坏是留下一个
    会被过期判定自愈的残留锁，不值得为此打断业务流程。
    """
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def touch(lock_path: Path) -> None:
    """
    刷新锁文件的 mtime（心跳）。

    会话锁靠它保鲜：进程活着就定期 touch，锁的 mtime 始终新鲜；进程崩了心跳停止，
    超过过期阈值后锁可被其它实例接管。文件不存在或刷新失败静默。
    """
    try:
        os.utime(str(lock_path), None)
    except OSError:
        pass


def is_fresh(lock_path: Path, stale_after_seconds: float) -> bool:
    """
    判断锁是否存在且仍然新鲜（mtime 距今小于阈值）。

    供「/resume 列表标注 locked」「过期清理跳过被保护会话」等只读场景使用。
    锁不存在、无法读取 mtime 等任何异常都返回 False（视为无有效锁）。
    """
    try:
        mtime = os.path.getmtime(str(lock_path))
    except OSError:
        return False
    return (time.time() - mtime) < stale_after_seconds


def _is_stale(lock_path: Path, stale_after_seconds: float) -> bool:
    """
    判断锁是否为过期残留：文件存在且 mtime 距今超过阈值。

    与 is_fresh 不是简单取反——锁文件恰好不存在（对方刚释放）时，is_fresh 为 False
    但也不是「残留」，此处返回 False 让上层直接走重试的原子创建即可。
    """
    try:
        mtime = os.path.getmtime(str(lock_path))
    except OSError:
        return False
    return (time.time() - mtime) >= stale_after_seconds
