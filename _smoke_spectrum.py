"""冒烟测试：熵谱（spectrum，按标签统计）+ 谱驱动（drive）。

不连 LLM，用假向量直接构造记忆矩阵，验证 分支聚合 / 有效秩 / 投影残差 / 新方向判定 / 降级。
用独立 data_test_spectrum 目录，不碰真实 data/。
跑：python _smoke_spectrum.py
"""
import os
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).parent
TEST_ROOT = BASE / "data_test_spectrum"
if TEST_ROOT.exists():
    shutil.rmtree(TEST_ROOT)   # 清掉上一轮残留（脚本中途崩会留下脏账本污染重跑）
os.environ["AIR2_DATA_ROOT"] = str(TEST_ROOT)
sys.path.insert(0, str(BASE))

from config import Config
from core.spectrum import Spectrum
from core.drive import Drive

ok = True


def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False


class FakeMemory:
    """只提供 Spectrum 需要的 l1（每条带 emb + title），供按分支聚合。"""

    def __init__(self, items):
        self.l1 = [{"text": c, "title": t, "emb": v} for c, t, v in items]
        self.l2 = []
        self.l3 = []

    @staticmethod
    def title_of(it):
        return ((it or {}).get("title") or "").strip()

    @staticmethod
    def parent_of(it):
        return ((it or {}).get("parent") or "").strip()

    @classmethod
    def tag_cross(cls, it):
        return []


cfg = Config()

# ---- 1. 谱：两个分支（x/y 平面，每分支 2 条）→ 有效秩精确 = 2 ----
# 分支均值 = 分支内向量平均，所以每分支内部要同向（不能 ±x 抵消成零向量）
items = [
    ("自我 A", "自我", [1.0, 0.0, 0.0]),
    ("世界 B", "世界", [0.0, 1.0, 0.0]),
    ("自我 C", "自我", [0.9, 0.1, 0.0]),
    ("世界 D", "世界", [0.1, 0.9, 0.0]),
]
sp = Spectrum(cfg)
cache = sp.compute(FakeMemory(items))
check("谱能算出来", cache is not None)
check("有效秩约 2（两个分支）", cache is not None and 1.8 < cache["erank"] < 2.2)
check("分支数 = 2", cache is not None and cache["k"] == 2)
check("分支名可读", cache is not None and "自我" in cache["reps"] and "世界" in cache["reps"])

# ---- 2. 投影残差：旧方向低、新方向高 ----
r_old = sp.project_residual([0.8, 0.2, 0.0])
r_new = sp.project_residual([0.0, 0.0, 1.0])
check("旧方向残差低", r_old is not None and r_old < 0.5)
check("新方向残差高", r_new is not None and r_new > 0.5)

# ---- 3. classify：新方向判定 ----
idx_old, res_old = sp.classify([0.8, 0.2, 0.0])
idx_new, res_new = sp.classify([0.0, 0.0, 1.0])
check("旧点归已有方向", idx_old is not None)
check("新点判为新方向（idx=None）", idx_new is None and res_new is not None)

# ---- 4. 降级：记忆 < 3 条 ----
sp2 = Spectrum(cfg)
check("记忆太少降级", sp2.compute(FakeMemory([("a", "x", [1.0, 0.0])])) is None)
check("降级后残差 None", sp2.project_residual([1.0, 0.0]) is None)
check("降级后 classify 返回 (None, None)", sp2.classify([1.0, 0.0]) == (None, None))

# ---- 5. Drive 喂信号 + 趋势 + feel ----
d = Drive(cfg)
for v in [0.9, 0.8, 0.1, 0.15, 0.2]:
    d.record_explore(v)
check("新奇值 = 探索均值", abs(d.novelty() - 0.43) < 1e-9)
trend = d._trend(d._explore)
check("探索趋势向下（前高后低）", trend is not None and trend < 0)
for v in [True, True, False, True]:
    d.record_produce(v)
check("领悟值 = 压缩占比 0.75", abs(d.insight() - 0.75) < 1e-9)

feel = d.feel()
check("feel 三个键齐全", all(k in feel for k in ("熵增趋势", "熵减趋势", "领悟值")))
check("熵增趋势不贴健康区（读数只报事实）", "健康区" not in feel["熵增趋势"])
check("无记忆时领悟值降级文案", "还没有方向" in feel["领悟值"])

# ---- 6. 领悟值方向账本：覆盖（直接例证数 + 交叉连入）+ 精炼，按覆盖降序 ----
class TagMemory:
    """只提供带 tag/cat_tag/cross 的 l1 + 带方向的 l3（覆盖从 l3 实时算分支密度）。"""

    def __init__(self, l1_items, l3_items):
        self.l1 = [{"text": c, "title": t, "pyramid": ct, "link": cr,
                    "refined": rf}
                   for c, t, ct, cr, rf in l1_items]
        self.l2 = []
        self.l3 = [{"title": t, "parent": ct, "link": cr}
                   for t, ct, cr in l3_items]

    @staticmethod
    def title_of(it):
        return ((it or {}).get("title") or "").strip()

    @staticmethod
    def parent_of(it):
        return ((it or {}).get("parent") or "").strip()

    @staticmethod
    def links_of(it):
        return list((it or {}).get("link") or [])


tag_mem = TagMemory(
    [("边界感框架", "边界感", "value", [], 1),
     ("诚实框架", "诚实", "value", [], 0)],
    [("尊重他人", "边界感", []), ("不追问沉默", "边界感", []), ("诚实承认未知", "诚实", [])],
)
d_tag = Drive(cfg, tag_mem)
prog = d_tag._learning_progress()
check("领悟值账本含方向", "边界感" in prog and "诚实" in prog)
check("覆盖 = 分支密度（例证数+交叉连入），精炼并列不相加",
      "边界感：覆盖 2 · 精炼 1" in prog and "诚实：覆盖 1 · 精炼 0" in prog)
check("领悟值账本按覆盖降序", prog.index("边界感") < prog.index("诚实"))

# ---- 7. summary / locate（变化反馈 + 回溯用）----
s = sp.summary()
check("summary 返回标量", s is not None and "erank" in s and "k" in s)
loc_old = sp.locate([1.0, 0.0, 0.0])   # x 轴方向内
check("locate 归到方向", loc_old is not None and loc_old["branch_idx"] is not None)
loc_new = sp.locate([0.0, 0.0, 1.0])   # z 轴，游离
check("locate 判游离", loc_new is not None and loc_new["branch_idx"] is None)
check("降级 locate 返回 None", sp2.locate([1.0, 0.0]) is None)

shutil.rmtree(TEST_ROOT, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
