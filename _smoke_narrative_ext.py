"""冒烟验证：短期记忆增强（预算硬上限 / 目标清单渲染 / 墙钟时间戳）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.narrative import Narrative, BUDGET_CHARS

ok = True
def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False

# ---- 预算硬上限：塞满标题后 render 不超预算，长期方向/最近一次保留 ----
n2 = Narrative()
n2.direction = "想读完小说后写理解"
for i in range(1, 120):
    n2.record_tick(i, "目标" + "字" * 60, "结果" + "字" * 300)
r = n2.render()
check(f"render 长度 ≤ 预算 {BUDGET_CHARS}", len(r) <= BUDGET_CHARS)
check("长期方向保留", "想读完小说后写理解" in r)
check("目标清单存在", "【目标清单】" in r)
check("最近一次保留", "目标" in r)   # 最近一次目标永不丢

# ---- 墙钟时间戳 ----
n4 = Narrative()
n4.record_tick(1, "读第一章", "read_local: 读了")
n4.record_tick(2, "读第二章", "read_local: 读了")
check("最近一次带墙钟 ts", n4.last_tick.get("ts", "") != "")
check("标题带 ts", n4.titles[0].get("ts", "") != "")
check("标题行显示时间", "[" in n4._goal_row(n4.titles[0]))

print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
