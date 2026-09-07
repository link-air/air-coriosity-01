"""冒烟验证：行为新奇账（2026-09-01 创建者定）——
玩类行为（画画/写代码/下棋/冒险）久违地做 = 熵增，间隔驱动。"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.drive import Drive
from config import Config

ok = True
def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False

def near(a, b, eps=0.001):
    return abs(a - b) < eps

d = Drive(Config())
T0 = time.time()

# 首次行为：全新行为 v=0.7（鼓励探索）
v = d.record_behavior("paint", now=T0)
check("首次行为 v=0.7", near(v, 0.7))
check("记录上次时间", d.behaviors.get("paint") == T0)

# 刚玩过：v 低（独立实例，上次=T0，1 小时后约 0.325）
da = Drive(Config())
da.record_behavior("paint", now=T0)
v = da.record_behavior("paint", now=T0 + 3600)
check("1 小时后 v≈0.325", near(v, 0.325, 0.01))

# 间隔驱动（按小时，2026-09-01 创建者定）：越久没做越高，一天一次拿满
db = Drive(Config())
db.record_behavior("paint", now=T0)
v = db.record_behavior("paint", now=T0 + 12 * 3600)   # 距上次 12 小时
check("隔 12 小时 v=0.6", near(v, 0.6))
dc = Drive(Config())
dc.record_behavior("paint", now=T0)
v = dc.record_behavior("paint", now=T0 + 24 * 3600)   # 距上次 24 小时（一天一次）封顶
check("隔 24 小时 v=0.9", near(v, 0.9))
dd = Drive(Config())
dd.record_behavior("paint", now=T0)
v = dd.record_behavior("paint", now=T0 + 72 * 3600)   # 距上次 72 小时，仍封顶
check("封顶 0.9", near(v, 0.9))

# 不同行为独立记录
v = d.record_behavior("write_code", now=T0)
check("不同行为独立首次 0.7", near(v, 0.7) and d.behaviors.get("write_code") == T0)

# 行为账进熵增趋势（合并内容新奇 + 行为新奇）
d2 = Drive(Config())
d2.record_explore(0.5)                       # 写了一条 v_new=0.5 的记忆
d2.record_explore(0.5)
d2.record_behavior("paint", now=T0)          # 行为 0.7
m = d2.novelty()
check("熵增趋势合并行为账", near(m, (0.5 + 0.5 + 0.7) / 3))
check("行为账无样本时纯内容", Drive(Config()).novelty() is None)

# 窗口上限：行为序列不超 spectrum_window
d3 = Drive(Config())
for i in range(20):
    d3.record_behavior("chess", now=T0 + i * 86400)
check("行为序列不超窗口", len(d3._behave) <= 10)

# 持久化 round-trip（behave + behaviors 一起）
d4 = Drive(Config())
d4.record_behavior("paint", now=T0)
d4.record_explore(0.4)
dd = d4.to_dict()
d5 = Drive(Config())
d5.from_dict(dd)
check("行为序列持久化", near(d5._behave[0], 0.7))
check("行为时间持久化", d5.behaviors.get("paint") == T0)
check("内容序列仍恢复", near(d5._explore[0], 0.4))

# 工具注册表 kind：玩类工具标 play，读自己的不算
from core.tools import Toolbox
class _A:  # 最小 agent 桩（Toolbox 构造只用到 cfg）
    def __init__(self):
        self.cfg = Config()
        self.data_root = self.cfg.data_root
tb = Toolbox(_A())
check("paint 是 play", tb.kind("paint") == "play")
check("play_chess 是 play", tb.kind("play_chess") == "play")
check("write_code 是 play", tb.kind("write_code") == "play")
check("start_adventure 是 play", tb.kind("start_adventure") == "play")
check("adventure_choose 是 play", tb.kind("adventure_choose") == "play")
check("list_portfolio 不是 play", tb.kind("list_portfolio") != "play")

print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
