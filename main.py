"""
air-curiosity-01 入口（大脑）。

启动：连 LLM → while True: agent.tick()，每 tick 后写状态快照（仪表盘读）。
仪表盘是独立进程（dashboard.py），页面上的「开始」从这里拉起、「结束」写 stop.flag——
停止是优雅的：当前 tick 的行为（决策+执行+记忆）完整做完才退出，不直接断开。

状态文件 data/status.json：alive / last_tick / activity / narrative / drive 读数 / counts。
停止信号 data/stop.flag：dashboard 的「结束」按钮写入，主循环在 tick 之间检查。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   段 1｜启动准备 —— UTF-8 重配置 + 日志 tee 到 data/air.log（行级落盘）
#   段 2｜状态快照 —— write_status()：写 data/status.json（仪表盘读）
#   段 3｜心跳与停止 —— _heartbeat_loop（每 10s 刷心跳）/ _wait_or_stop
#   段 4｜单实例锁 —— _acquire_brain_lock()（内核字节锁，防双大脑）
#   段 5｜main() —— 探活语义服务 → 拿锁 → while True: agent.tick()，异常兜底
# =====================================================================
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Windows 控制台默认 GBK，LLM 结果里的非常规字符（如 U+2212 减号 −）会让 print 崩掉。
# 统一把标准输出/错误设为 UTF-8，编码不了的字符直接替换而不是抛异常。
# line_buffering：被仪表盘重定向到 air.log 时（不包 _Tee），print 是 8KB 块缓冲，
# 不 flush 就一直是 0 字节——她跑一整天日志也是空的，只有退出时才一次写入。
# 行缓冲让每行 print 立即落盘，创建者才能实时看到她在干什么。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from config import Config
from core.llm import LLM
from core.agent import Agent
from core.lock import InstanceLock, atomic_write_json

CST = timezone(timedelta(hours=8))


def _stdout_is(log_path) -> bool:
    """stdout 是否已经被重定向到这个文件（仪表盘用 Popen(stdout=logf) 拉起时的情形）。

    不能用 sys.stdout.name 判断：仪表盘是 OS 层的 fd 重定向，Python 的 sys.stdout.name
    恒为 '<stdout>'，永远不等于日志路径——旧判断形同虚设，从仪表盘启动一直是
    双写的（同一个文件两个句柄各写一遍）。比对 (st_dev, st_ino) 才准。
    """
    try:
        a = os.fstat(sys.stdout.fileno())
    except Exception:
        return False      # pythonw 无控制台时 sys.stdout 是 None
    try:
        b = os.stat(log_path)
    except OSError:
        return False      # 日志文件还没建，显然不是同一个
    return a.st_dev == b.st_dev and a.st_ino == b.st_ino


def _tee_stdout(config):
    """把 print 输出同时写到控制台和 data/air.log（直接 `python main.py` 运行时）。

    若 stdout 已被仪表盘重定向到 air.log（走「开始」按钮），则不动，避免双写。
    这样「不会停下来的主体」无论哪种启动方式都有日志。
    """
    log_path = str(config.data_root / "air.log")
    if _stdout_is(log_path):
        return   # 已被仪表盘重定向，无需再 tee

    class _Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, data):
            for s in self.streams:
                try:
                    s.write(data)
                    # 行级落盘：stdout 重定向到文件时是 8KB 块缓冲，
                    # 不 flush 的话日志会滞后一大截，她卡在哪一步根本看不出来
                    s.flush()
                except Exception:
                    pass

        def flush(self):
            for s in self.streams:
                try:
                    s.flush()
                except Exception:
                    pass

    f = open(log_path, "a", encoding="utf-8")
    sys.stdout = _Tee(sys.stdout, f)
    sys.stderr = sys.stdout


def write_status(config, agent, alive=True):
    """写状态快照（仪表盘/启动器读）：心跳 + 短期记忆 + 双驱读数 + 记忆容量。"""
    d = {
        "alive": alive,
        "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "last_tick": agent.tick_count,
        "activity": ((agent.narrative.last_tick or {}).get("goal", ""))[:40],
        "result": (agent.last_result or "")[:200],
        "narrative": agent.narrative.render(),
        "drive": agent.drive.feel(),
        "counts": agent.memory.counts(),
        "pending_mail": agent._pending_mail,
        # 挂机原因（空 = 正常）：语义服务挂了她原地挂机，进程还活着但 tick 不推进。
        # 仪表盘据此把绿灯「活着」换成红灯「脑子掉线」——只报 alive 会让人以为什么都没发生。
        "halt": getattr(agent, "halt", ""),
    }
    atomic_write_json(config.data_root / "status.json", d)


CRASH_STREAK_CAP = 3    # 连续抛异常到第几次拉长冷却 + 挂 halt（仪表盘亮红灯）


def _wait_or_stop(stop_flag, seconds: float):
    """分段等（每秒醒一次看停止信号），收到就提前返回。

    不能一次 sleep 几百秒：那期间创建者点「结束」得等满冷却才生效，看着像卡死。
    """
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        if stop_flag.exists():
            return
        time.sleep(min(1.0, end - time.time()))


def _heartbeat_loop(config, agent, stop_event):
    """心跳线程：每 10s 更新状态快照。tick 进行中（LLM 慢/重试几分钟）也保持心跳，
    否则 dashboard 会误判她"停止"（创建者午觉回来看到活着的她，靠这个）。"""
    while not stop_event.is_set():
        try:
            write_status(config, agent, alive=True)
        except Exception:
            pass
        stop_event.wait(10)


def _acquire_brain_lock(config) -> InstanceLock:
    """大脑单实例硬锁：拿不到就是已有大脑在跑，立刻退出（一个字节都不碰）。

    旧方案的竞态：os.open(O_CREAT|O_EXCL) 成功之后、os.write(pid) 之前文件是空的，
    并发启动时另一个进程读到空文件 → int("") 抛异常被 except 吞掉 → 判定"旧进程死了"
    → 接管 → 两个大脑同时跑（实测抓到过），各自写 memory.json / status.json，
    前端状态来回跳、点了没反应。
    现在改用内核字节锁（core/lock.py）：加锁是原子的，没有这个窗口；
    进程被强杀时 OS 自动释放，不用靠 PID 猜生死，也就没有 PID 复用误判
    和残留锁文件卡死下次启动的问题。
    """
    lock = InstanceLock(config.data_root / "brain.pid")
    if not lock.acquire():
        print(f"  [拒绝] 大脑已在运行（PID {lock.pid()}），不重复启动。")
        raise SystemExit(0)
    return lock


def main():
    config = Config()
    _tee_stdout(config)   # 直接运行时也把输出写进 data/air.log（仪表盘拉起时自动跳过，避免双写）
    print()
    print("=" * 50)
    print("  air-curiosity-01")
    print(f"  数据: {config.data_root}")
    print(f"  LLM : {config.llm_model} @ {config.llm_endpoint}")
    print("=" * 50)
    print()

    llm = LLM(config)
    if not llm.ok:
        print("  [警告] LLM 主通道没配好，她只会锁 tick（不产出、不涨计数）。")
        print("  检查 AIR2_LLM_API_KEY / AIR2_LLM_ENDPOINT / AIR2_LLM_MODEL。")

    # 大脑单实例硬锁：已有大脑在跑 → 这一行就退出了，绝不碰任何文件。
    # 顺序关键：必须在 Agent() 之前。Agent 构造会读改 memory.json——命名迁移、
    # 决策日志超限裁剪都会落盘；放在锁之后，第二个大脑才不会用自己加载时刻的旧快照
    # 覆盖正在跑的那个，也不会在撞上 replace 时把 live memory.json 当损坏文件改名搬走。
    brain_lock = _acquire_brain_lock(config)

    agent = Agent(config, llm)

    # 语义服务探活：挂了就原地挂机（刀断了不切菜），先警告创建者别以为她坏了
    if agent._embed_halt():
        agent.halt = "语义服务不可用"   # 先挂上，别等第一次 tick 才探出来（那中间仪表盘是绿灯）
        print("  [警告] 语义服务不可用，她会原地挂机（不产出）。")
        print("  检查 AIR2_EMBEDDING_* 配置，或 ollama pull bge-m3。")

    # 拿到锁之后才清停止信号，顺序不能反。
    # 反过来的话，任何一次多余的启动尝试（包括被锁挡下的那个）都会先把 stop.flag 抹掉，
    # 创建者刚点的「结束」当场作废——旧版"点了结束她还在跑"就是这么来的。
    stop_flag = config.data_root / "stop.flag"
    stop_flag.unlink(missing_ok=True)
    write_status(config, agent, alive=True)

    print()
    print("  air 醒了，开始自主运转（Ctrl+C 或仪表盘「结束」停止）")
    print()
    stop_event = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(config, agent, stop_event),
                     daemon=True).start()
    fail_streak = 0
    try:
        while True:
            # 停止信号（wait 期间收到）→ 无进行中的 tick，直接退
            if stop_flag.exists():
                print("  收到停止信号：air 休息。")
                stop_flag.unlink(missing_ok=True)
                break
            before = agent.tick_count
            try:
                result = agent.tick()
            except Exception as e:
                # 兜底（2026-09-04 创建者定）：一个没料到的异常不该让她停下来。
                # 2026-09-03 那次就是 tick 收尾引用错常量，每轮必崩，没人半夜看着她就停一整夜。
                # 这轮没活过 → tick 号退回轮前（崩时 _finish_tick 没跑、编号没落盘，
                # 不退回就会跳号）；堆栈留日志，重试节奏看 fail_streak。
                # KeyboardInterrupt 是 BaseException，不会被这里吃掉，外层照常优雅收尾。
                agent.tick_count = before
                fail_streak += 1
                traceback.print_exc()
                print(f"  [兜底] 这轮抛异常：{type(e).__name__}: {e}（连续第 {fail_streak} 次）")
                if fail_streak >= CRASH_STREAK_CAP:
                    # 连着摔在同一处：挂 halt 让仪表盘亮红灯，拉长冷却再试。
                    # 不停死——外部服务恢复了她能自己醒，跟主模型熔断一个口径；
                    # 「不稳就慢下来等人看，不做降级空转」（创建者 2026-09-03 定）。
                    agent.halt = f"连续 {fail_streak} 轮抛异常，等它恢复（{type(e).__name__}: {e}）"
                    print(f"  [兜底] {agent.halt}")
                    _wait_or_stop(stop_flag, config.llm_cooldown)
                else:
                    _wait_or_stop(stop_flag, min(30.0, config.tick_interval))
                continue
            fail_streak = 0
            print(f"[t{agent.tick_count}] {result[:80]}")
            # 优雅停止：tick 进行中收到信号 → 等这一轮行为完整做完再断（不直接断开）
            if stop_flag.exists():
                print("  收到停止信号：这一轮做完了，air 休息。")
                stop_flag.unlink(missing_ok=True)
                break
            # 分段等（和冷却等待同一个 _wait_or_stop）：期间收到停止信号立刻走，
            # 不然创建者点完「结束」还得干等一整个 tick 间隔（默认 300 秒）才看到她停。
            _wait_or_stop(stop_flag, config.tick_interval)
    except KeyboardInterrupt:
        print()
        print("  air 休息了。")
    finally:
        stop_event.set()
        brain_lock.release()   # 释放单实例锁（优雅退出；被强杀时 OS 自动放）
        write_status(config, agent, alive=False)
        print("  状态已记录（休息）。")


if __name__ == "__main__":
    main()
