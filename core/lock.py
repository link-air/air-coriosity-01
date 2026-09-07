"""
多进程安全工具：单实例锁 + 原子写。

air 是双进程（仪表盘 + 大脑），这两件事都出过事，所以单独收在一个地方：

1. 单实例锁（InstanceLock）
   旧方案是 PID 文件 + 活进程探针：os.open(O_CREAT|O_EXCL) 拿到句柄后要再 os.write(pid)，
   这中间有个窗口——并发启动时第二个进程撞进 FileExistsError，读到一个**空文件**，
   int("") 抛异常被 except 吞掉，于是判定"旧进程死了"→ 接管 → 双开。
   实测抓到过 2 个 dashboard + 2 个 main.py 同时跑（同一秒启动），
   两个大脑并发写 memory.json，前端状态来回跳、点了没反应。
   现在换成内核字节锁：加锁是原子的，没有创建→写入的窗口；
   而且进程被强杀时 OS 自动释放锁，不用靠 PID 去猜"它死没死"，
   也就没有 PID 复用误判、以及残留锁文件把下次启动卡死的问题。

2. 原子写（atomic_write）
   旧方案的临时文件是固定名 xxx.json.tmp。两个大脑同时写同一个目标就会共用同一个
   临时文件，Windows 允许并发写同一文件句柄，内容交错 → JSON 损坏 →
   memory.py 读到坏文件就"用空白开始" → 她的记忆清零。
   现在临时文件带进程号 + 随机后缀，各写各的；rename 是原子的，
   读者只会看到旧版或新版全文，不会看到写了一半的文件。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜常量 —— 锁字节偏移（刻意锁在 1GB 处，避开 PID 内容区）
#   段 2｜class InstanceLock —— 跨进程单实例锁（内核字节锁；
#       加锁原子、进程被强杀时 OS 自动释放）
#   段 3｜原子写 —— atomic_write / atomic_write_json + 读侧退避重试
#       （随机后缀临时文件 + rename；Windows 撞上正在 replace 要重试）
# =====================================================================
import os
import secrets
import time

# 锁字节的偏移：刻意选在远超文件实际大小的位置（1GB 处）。
# Windows 的字节锁是**强制锁**——被锁范围内的字节，别的进程连读都会被拒。
# 所以锁不能盖住真正要读的 PID 内容，锁在 1GB 处、内容写在开头，两者互不影响
# （Windows 允许锁定超过文件当前大小的范围）。
_LOCK_OFFSET = 1 << 30
_LOCK_SIZE = 1

_WIN = os.name == "nt"


class InstanceLock:
    """跨进程单实例锁。锁句柄必须持有到进程结束（进程退出时 OS 也会自动释放）。

    用法：
        lock = InstanceLock(data_root / "brain.pid")
        if not lock.acquire():
            print("已有实例在跑"); raise SystemExit(0)
        # ... 干活 ...
        lock.release()   # 主动退出时释放；被强杀时靠 OS 释放
    """

    def __init__(self, path):
        self.path = str(path)
        self._fd = None

    def acquire(self) -> bool:
        """拿锁。成功 = 本进程成为唯一实例（返回 True）；已被别人持有 = 返回 False。"""
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        try:
            self._lock(fd)
        except OSError:
            os.close(fd)   # 别人持锁：原样返回 False，绝不删除/接管别人的锁文件
            return False
        self._fd = fd
        self._write_pid()
        return True

    def held(self) -> bool:
        """探测锁是否被某个进程持有（= 那个实例还活着）。不改变本进程的持有状态。

        仪表盘靠它判断大脑在不在跑。比"读 PID 再探针"准：进程死了锁立刻没，
        不存在 PID 复用导致的"明明死了却判成活着"。
        """
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        try:
            self._lock(fd)
        except OSError:
            return True     # 拿不到 = 有人持着
        finally:
            os.close(fd)    # 句柄一关，锁自动放（不干扰真正的持有者）
        return False

    def pid(self) -> int:
        """锁文件里记的 PID（只作显示/排查用；判定存活请用 held()）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return int(f.read().strip() or 0)
        except Exception:
            return 0

    def release(self):
        if self._fd is None:
            return
        try:
            self._unlock(self._fd)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None

    # ---- 内部 ----

    def _lock(self, fd):
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        if _WIN:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_SIZE)   # 非阻塞：拿不到立刻抛 OSError
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self, fd):
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        if _WIN:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_SIZE)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)

    def _write_pid(self):
        try:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, str(os.getpid()).encode())
        except OSError:
            pass


def atomic_write(path, text: str):
    """原子替换文件内容：随机后缀临时文件 → fsync → os.replace。

    临时文件名带进程号 + 随机串，两个进程同时写同一个目标也各写各的，
    不会像固定名 tmp 那样被并发写交错。replace 是原子的：
    读者只会看到旧版或新版全文，不会看到写了一半的文件。
    fsync 是刻意的：她的记忆不可重建，宁可慢几十毫秒，
    也不要"rename 成功了但内容还在系统缓存里"，断电后读到空文件当成记忆清零。
    """
    p = str(path)
    tmp = f"{p}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace_retry(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace_retry(tmp, target, retries: int = 7):
    """os.replace 带退避重试（Windows 必需）。

    Windows 上打开文件默认不带 FILE_SHARE_DELETE，所以只要有人正打开着目标文件
    （读或写），replace 就会直接拒绝访问（WinError 5）。实测 3 个进程并发写、
    1 个进程轮询读，稳定复现。这是瞬时冲突，退避重试即可——
    读的一侧始终拿到完整内容，不会看到半个文件。
    """
    delay = 0.005
    for i in range(retries):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if i == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.15)


def read_text_retry(path, retries: int = 6):
    """读文件，撞上别人正在 replace 时退避重试（Windows 必需）。

    和 _replace_retry 是同一个坑的另一面：读的瞬间别人在 replace，open 会拿到
    WinError 5。她的记忆文件读失败会被当成"文件坏了 → 用空白开始"，
    那等于记忆清零，代价太大，必须重试。
    """
    delay = 0.01
    for i in range(retries):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except PermissionError:
            if i == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)


def atomic_write_json(path, data):
    """atomic_write 的 JSON 版（项目里的落盘全是 JSON，统一走这一个出口）。"""
    import json
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
