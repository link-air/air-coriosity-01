"""
冒烟：core/lock.py 的单实例锁 + 原子写（双进程那次事故的根因就在这一块）。

跑：.venv\\Scripts\\python.exe _smoke_lock.py
用独立 data_test_lock 目录，不碰真实 data/。

覆盖：
  1. 同进程内重复拿锁
  2. held() 存活探测 + pid() 记录
  3. 跨进程互斥（子进程持锁，父进程拿不到）
  4. 进程被强杀后锁自动释放（旧方案靠 PID 猜，这里靠 OS）
  5. 并发原子写：读到的永远是完整 JSON，且不留临时文件
  6. 同时拉起 4 个进程，只有 1 个能拿到锁

注：旧 bug（O_EXCL 创建与写 PID 之间的空文件窗口）是**低概率竞态**，
不是每次都触发，所以这里不复现它——新方案用内核锁，加锁本身是原子的，
只要"持锁时别人拿不到"成立，任何时序下都成立。
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from core.lock import InstanceLock, atomic_write_json, read_text_retry

FAILS = []


def check(name, ok, detail=""):
    print(("  [OK] " if ok else "  [FAIL] ") + name + ((" — " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def _script(body: str) -> str:
    """子进程脚本：固定插入 sys.path，print 全部 flush=True（管道是块缓冲，不 flush 父进程读不到）。"""
    return ("import sys\n"
            f"sys.path.insert(0, r'{ROOT}')\n"
            "from core.lock import InstanceLock, atomic_write_json\n"
            + body)


def main():
    d = ROOT / "data_test_lock"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    pid_file = d / "brain.pid"

    print("\n1. 同一进程内重复拿锁")
    a = InstanceLock(pid_file)
    check("第一次 acquire 成功", a.acquire() is True)
    check("第二个实例 acquire 失败", InstanceLock(pid_file).acquire() is False)

    print("\n2. held() 探测 + pid() 记录")
    check("持锁时 held() == True", InstanceLock(pid_file).held() is True)
    check("pid() 是本进程", a.pid() == __import__("os").getpid(), f"记录到 {a.pid()}")
    a.release()
    check("释放后 held() == False", InstanceLock(pid_file).held() is False)

    print("\n3. 跨进程互斥（子进程持锁 3 秒）")
    hold = d / "hold.py"
    hold.write_text(_script(
        f"lk = InstanceLock(r'{pid_file}')\n"
        "print('CHILD_GOT' if lk.acquire() else 'CHILD_DENIED', flush=True)\n"
        "import time; time.sleep(3)\n"), encoding="utf-8")
    p = subprocess.Popen([sys.executable, str(hold)], stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline().strip()
    check("子进程拿到锁", line == "CHILD_GOT", f"输出 {line!r}")
    check("父进程 acquire 被拒", InstanceLock(pid_file).acquire() is False)
    check("父进程 held() 探测到子进程", InstanceLock(pid_file).held() is True)
    p.wait()   # 子进程没调 release，纯靠退出自动释放
    check("子进程退出后锁自动释放", InstanceLock(pid_file).held() is False)

    print("\n4. 被强杀后锁自动释放（旧方案的残留 pid 文件就卡在这一步）")
    p2 = subprocess.Popen([sys.executable, str(hold)], stdout=subprocess.PIPE, text=True)
    p2.stdout.readline()
    p2.kill()          # Windows 上是 TerminateProcess，等价于强杀
    p2.wait()
    time.sleep(0.3)
    check("强杀后 held() == False", InstanceLock(pid_file).held() is False)

    print("\n5. 并发原子写（3 个进程各写 40 次，父进程边读边验）")
    target = d / "mem.json"
    atomic_write_json(target, {"n": 0})
    writer = d / "writer.py"
    writer.write_text(_script(
        f"for i in range(40):\n"
        f"    atomic_write_json(r'{target}', {{'n': i, 'pad': 'x' * 2000}})\n"), encoding="utf-8")
    ps = [subprocess.Popen([sys.executable, str(writer)],
                           stderr=subprocess.PIPE, text=True) for _ in range(3)]
    bad = 0
    for _ in range(150):
        try:
            json.loads(read_text_retry(target))
        except Exception:
            bad += 1
        time.sleep(0.01)
    # 必须检查退出码：写进程撞上 WinError 5 是会整个崩掉的，
    # 光看"文件始终是合法 JSON"发现不了（它只是没写成而已）
    codes = []
    for x in ps:
        _, err = x.communicate()
        codes.append(x.returncode)
        if x.returncode != 0 and err:
            print("      写进程报错：", err.strip().splitlines()[-1])
    check("3 个写进程全部正常退出", all(c == 0 for c in codes), f"退出码 {codes}")
    check("并发写期间读到坏 JSON 次数 == 0", bad == 0, f"坏 {bad} 次")
    check("写完后仍是合法 JSON", isinstance(json.loads(read_text_retry(target)), dict))

    print("\n6. 同时拉起 4 个进程，只有 1 个能拿到锁")
    racer = d / "racer.py"
    racer.write_text(_script(
        f"lk = InstanceLock(r'{pid_file}')\n"
        "print('GOT' if lk.acquire() else 'NO', flush=True)\n"
        "import time; time.sleep(2)\n"), encoding="utf-8")
    rs = [subprocess.Popen([sys.executable, str(racer)], stdout=subprocess.PIPE, text=True)
          for _ in range(4)]
    outs = [r.stdout.readline().strip() for r in rs]
    for r in rs:
        r.wait()
    check("恰好 1 个进程拿到锁", outs.count("GOT") == 1, f"结果 {outs}")

    leftovers = list(d.glob("**/*.tmp"))
    check("没有残留临时文件", not leftovers, str(leftovers))

    shutil.rmtree(d, ignore_errors=True)
    print()
    if FAILS:
        print(f"[冒烟失败] {len(FAILS)} 项：{FAILS}")
        sys.exit(1)
    print("[冒烟通过] 单实例锁 + 原子写全部通过")


if __name__ == "__main__":
    main()
