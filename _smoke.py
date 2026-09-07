"""冒烟验证：不连 LLM，只测 drive / memory 核心逻辑。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from core.drive import Drive
from core.memory import Memory

cfg = Config()
# 独立测试目录：绝不碰真实 data/（曾因用默认 data/ 误删过 air 的真实记忆）
cfg.data_root = Path(__file__).parent / "data_test_smoke"
cfg.data_root.mkdir(parents=True, exist_ok=True)
ok = True


def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False


# ---- Drive（谱版）----
d = Drive(cfg)
check("无样本新奇值 None", d.novelty() is None)
check("无产出领悟值 0", d.insight() == 0.0)
for v in [0.9, 0.8, 0.1, 0.15, 0.2]:
    d.record_explore(v)
check("新奇值 = 探索均值", abs(d.novelty() - 0.43) < 1e-9)
for v in [True, True, False, True]:
    d.record_produce(v)
check("领悟值 = 加深率", abs(d.insight() - 0.75) < 1e-9)

feel = d.feel()
check("感受翻译含百分比", "%" in feel["熵增趋势"])
check("记忆少熵增趋势不带健康区（认知未成形不贴区间）", "健康区" not in feel["熵增趋势"])
check("无记忆时领悟值降级文案", "还没有方向" in feel["领悟值"])

# ---- Memory ----
m = Memory(cfg)
check("记忆初始为空", len(m.l1) + len(m.l2) + len(m.l3) == 0)

check("写 L3 成功", m.write("L3", "今天读了一篇关于熵的文章") is True)
check("写 L1 成功", m.write("L1", "我相信诚实比自洽更重要") is True)

hits = m.search("熵")
check("搜索能命中", len(hits) > 0)

# 删除 = 归档到 L5（不真删），记忆总量减少、L5 增加
before = len(m.l1) + len(m.l2) + len(m.l3)
m.delete_many("L3", [m.l3[0]["id"]])
check("删除后记忆减少", len(m.l1) + len(m.l2) + len(m.l3) == before - 1)
check("删除后 L5 归档", len(m.l5) == 1)

# 上限测试：L2 非价值最多 19 条（配额倒逼价值观），value 可补到总数上限 20
for i in range(Memory.NON_VALUE_CAP["L2"]):
    m.write("L2", f"洞察 {i}")
check("L2 非价值写满到配额", len(m.l2) == Memory.NON_VALUE_CAP["L2"])
check("L2 非价值超额被拒（配额满）", m.write("L2", "第 20 条非价值应该失败") is False)
check("L2 写 value 补到 20", m.write("L2", "价值洞察", category="value") is True)
check("L2 总数满被拒", m.write("L2", "第 21 条应该失败") is False)

# 清理测试数据
import shutil
shutil.rmtree(cfg.data_root, ignore_errors=True)

print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
