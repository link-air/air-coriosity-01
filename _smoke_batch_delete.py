"""冒烟验证：批量删除记忆（多个 id / 内容子串匹配，归档到 L5）。"""
import sys
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from core.memory import Memory

cfg = Config()
cfg.data_root = Path(__file__).parent / "data_test_batchdel"
cfg.data_root.mkdir(parents=True, exist_ok=True)

ok = True
def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False

m = Memory(cfg)

# 准备数据：L2 写 5 条（3 条含"外部熵增"）
m.write("L2", "外部熵增导致数据不可靠", category="insight")
m.write("L2", "外部熵增常态化确认", category="reflection")
m.write("L2", "外部熵增再次验证", category="insight")
m.write("L2", "峡谷双重信仰并存", category="insight")
m.write("L2", "内部逻辑推演优先", category="reflection")
check("准备 5 条 L2", len(m.l2) == 5)

# 1. 子串匹配删除
res = m.delete_many("L2", ["外部熵增"])
check("子串匹配删 3 条", len(res["deleted"]) == 3 and len(m.l2) == 2)
check("归档 L5 3 条", sum(1 for it in m.l5 if it.get("type") == "archived") == 3)

# 2. 批量 id 删除
ids = [it["id"] for it in m.l2]
res = m.delete_many("L2", ids)
check("批量 id 删 2 条", len(res["deleted"]) == 2 and len(m.l2) == 0)

# 3. 找不到返回
res = m.delete_many("L2", ["不存在的关键词", "fak3id"])
check("找不到返回 not_found", len(res["deleted"]) == 0 and len(res["not_found"]) == 2)

# 4. 混合 id + 子串
m.write("L2", "测试条目A", category="insight")
m.write("L2", "测试条目B", category="insight")
res = m.delete_many("L2", [m.l2[0]["id"], "测试条目B"])
check("混合删 2 条", len(res["deleted"]) == 2 and len(m.l2) == 0)

# 5. 非法层
res = m.delete_many("L9", ["随便"])
check("非法层返回 not_found", len(res["deleted"]) == 0 and len(res["not_found"]) == 1)

shutil.rmtree(cfg.data_root, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
