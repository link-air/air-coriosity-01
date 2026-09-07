"""冒烟验证：决策日志容量（≤DECISIONS_MAX 条，多了最旧的滚进 L5 冷存不真删）。"""
import sys
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from core.memory import Memory

cfg = Config()
cfg.data_root = Path(__file__).parent / "data_test_decisions"
# 每次重建：本冒烟会写 n+5 条决策、把最旧的滚进 L5。目录留着上次的数据，
# 第二次跑就带着上次的 L5 累积，条数断言必然失败（2026-09-05 补）。
if cfg.data_root.exists():
    shutil.rmtree(cfg.data_root)
cfg.data_root.mkdir(parents=True, exist_ok=True)

ok = True
def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False

m = Memory(cfg)
n = Memory.DECISIONS_MAX

# ---- 写入超限：最旧的滚进 L5 ----
for i in range(n + 5):
    m.write_decision(f"决策{i}", "think", "测试")
check("决策日志不超上限", len(m.decisions) == n)
check("最旧的 5 条滚进 L5", sum(1 for it in m.l5 if it.get("type") == "decision") == 5)
check("L5 决策条目不丢内容", m.l5[-1]["text"].startswith("决策"))
check("保留的是最新决策", m.decisions[-1]["text"] == f"决策{n + 4}")

# ---- 空内容不写 ----
check("空决策不写", m.write_decision("   ") is False)

# ---- 存量超限：重新加载时裁剪一次 ----
m.decisions = [{"id": f"x{i}", "tag": "t", "text": f"c{i}", "created": "2026-01-01 00:00"}
               for i in range(n + 8)]
m.save()
m2 = Memory(cfg)
check("加载时超限裁剪", len(m2.decisions) == n)
check("加载裁剪进 L5", sum(1 for it in m2.l5 if it.get("type") == "decision") >= 8)

shutil.rmtree(cfg.data_root, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
