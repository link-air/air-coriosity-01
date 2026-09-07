"""冒烟验证：价值观配额（L1 按大类封顶 / L2 非价值≤19，倒逼 value 存在）。"""
import sys
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from core.memory import Memory

cfg = Config()
cfg.data_root = Path(__file__).parent / "data_test_quota"
cfg.data_root.mkdir(parents=True, exist_ok=True)

ok = True
def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False

m = Memory(cfg)

# ---- L2 分类：value 合法 ----
check("L2 value 是合法分类", Memory.normalize_category("L2", "价值观") == "value")

# ---- L2 非价值配额：最多 19 条，value 不受此限 ----
for i in range(19):
    m.write("L2", f"洞察{i}", category="insight")
check("L2 写满 19 条非价值", sum(1 for it in m.l2 if it.get("pyramid") != "value") == 19)
check("L2 第 20 条非价值被拒（配额满，总数 19<20）", m.write("L2", "第20条洞察", category="insight") is False)
check("L2 此时写 value 成功", m.write("L2", "价值洞察：我更看重诚实", category="value") is True)
check("L2 value 分类落地", m.l2[-1]["pyramid"] == "value")
check("L2 总数 20 满", len(m.l2) == 20)

# ---- L1 三维下限（2026-08-31 起：下限不是上限，某一维可以超过下限）----
for i in range(5):
    m.write("L1", f"自我认知{i}", category="self_cognition")
check("L1 self_cognition 写满 5 条（下限）", sum(1 for it in m.l1 if it.get("pyramid") == "self_cognition") == 5)
check("L1 第 6 条 self_cognition 放行（下限不是上限）", m.write("L1", "第6条自我认知", category="self_cognition") is True)
check("L1 此时写 value 成功", m.write("L1", "我信诚实比自洽更重要", category="value") is True)

# ---- deny_reason：某一维没到下限前，写别的维不该被拦 ----
check("deny_reason self_cognition 可写（空串，没到硬顶）", m.deny_reason("L1", "self_cognition") == "")
check("deny_reason value 类可写（空串）", m.deny_reason("L1", "value") == "")

shutil.rmtree(cfg.data_root, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
