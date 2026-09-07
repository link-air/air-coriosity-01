"""冒烟验证：短期记忆目标清单化（2026-08-31）：
长期方向 / 目标清单（最近 10 个 tick 的目标 + 完成状态）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.narrative import Narrative, TITLES_KEEP, BUDGET_CHARS

ok = True
def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False

# 完全空白：初始提示
n = Narrative()
check("初始 render 空提示", "还没有" in n.render())

# record_tick：记最近一次（含完成状态）
n.record_tick(1, "读完第4章并处理", "read_local: 读完了；write_memory: 记了一条", done=True)
check("record_tick 记 last_tick", n.last_tick is not None and n.last_tick["t"] == 1)
check("goal 记录", n.last_tick["goal"] == "读完第4章并处理")
check("done 记录", n.last_tick["done"] is True)
check("summary 记录", "read_local" in n.last_tick["summary"])
check("last 兼容属性", n.last == n.last_tick)

# 第二次：旧的滚进标题层（目标 + 完成状态一并带过去）
n.record_tick(2, "思考第4章隐喻", "think: 琢磨了")   # 默认 done=False
check("旧的滚进标题层", len(n.titles) == 1 and n.titles[0]["t"] == 1)
check("标题带 goal", n.titles[0]["goal"] == "读完第4章并处理")
check("标题带 done", n.titles[0]["done"] is True)
check("标题带 brief", n.titles[0]["brief"] != "")

# 标题上限
n3 = Narrative()
for i in range(1, 60):
    n3.record_tick(i, f"目标{i}", f"做了{i}")
check("标题不超过上限", len(n3.titles) <= TITLES_KEEP)

# 长期方向 + 目标清单 渲染
n4 = Narrative()
n4.direction = "把星空卷轴机制研究透"
n4.record_tick(1, "读第33章", "read_local: 读了")
n4.record_tick(2, "写理解", "write_memory: 记了一条", done=True)
r = n4.render()
check("长期方向渲染", "【长期方向】把星空卷轴机制研究透" in r)
check("目标清单渲染", "【目标清单】" in r and "写理解" in r)
check("完成状态标记", "✓" in r and "✗" in r)
check("最近一次在最前", r.find("【目标清单】") < r.find("写理解"))

# render 预算
n5 = Narrative()
n5.direction = "长期" * 50
for i in range(1, 120):
    n5.record_tick(i, "目标" * 60, "做了" * 300)
check(f"render 长度 ≤ 预算 {BUDGET_CHARS}", len(n5.render()) <= BUDGET_CHARS)

# 持久化 round-trip
d = n4.to_dict()
n6 = Narrative()
n6.from_dict(d)
check("持久化恢复", n6.direction == n4.direction
      and n6.last_tick == n4.last_tick and n6.titles == n4.titles)

# 滚动进冷存（on_discard）
dropped = []
n7 = Narrative(on_discard=dropped.extend)
for i in range(1, 4):
    n7.record_tick(i, f"目标{i}", f"做了{i}")
check("旧 tick 记录滚冷存", len(dropped) == 2 and dropped[0].get("goal") == "目标1")

print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
