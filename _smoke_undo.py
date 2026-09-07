"""
冒烟：撤销体系（2026-09-06 重写）+ ID 不复用。

跑：.venv\\Scripts\\python.exe _smoke_undo.py
用独立 data_test_undo 目录，不碰真实 data/。

覆盖 2026-09-06 那次审查改掉的几条：
  1. R4：改写可撤销——revise 以前不压快照，撤销弹出来的是更早某次删除/合并的快照，
         而 undo 是整层覆盖，会把改写连同之后所有写入一起抹掉。
  2. R3：撤销不碰 L5（冷存只增不真删）。以前撤销会顺手删掉本次归档进 L5 的条目，
         做法是 del self.l5[l5_len:] 按长度一刀切——快照之后由改写留痕 /
         meta_log·decisions 超限滚动追加进来的**无关**条目会被连带删掉。
  3. R20：合并 L1 的撤销/回滚要连 L3 一起——删 L1 会把它名下的 L3 置「未分类」，
          只还原 L1 的话那些 L3 就永久失去归属了。
  4. R5：ID 序号水位线只涨不回退。以前只扫现有 ID，删掉最大号后再写会复用同一个号，
         而旧号可能还挂在 id_remap 上，那时候「改/删旧号」会命中无关的记忆。
"""
import io
import os
import shutil
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent
TEST = ROOT / "data_test_undo"
if TEST.exists():
    shutil.rmtree(TEST)
TEST.mkdir(parents=True)
os.environ["AIR2_DATA_ROOT"] = str(TEST)
sys.path.insert(0, str(ROOT))

from config import Config
from core.memory import Memory

FAILS = []


def check(name, ok, detail=""):
    print(("  [OK] " if ok else "  [FAIL] ") + name + ((" — " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


cfg = Config()
mem = Memory(cfg)


def by_id(layer, mid):
    lst = {"L1": mem.l1, "L2": mem.l2, "L3": mem.l3}[layer]
    return next((x for x in lst if x.get("id") == mid), None)


print("\n1. R4：改写可撤销（revise 现在压撤销快照）")
mem.write("L1", "条件→改写前；结论→这是原内容", category="value", tag="改写撤销（价值观）")
lid = mem.l1[-1]["id"]
before_text = mem.l1[-1]["text"]
r = mem.revise(lid, "条件→改写后；结论→这是改过的内容")
check("改写成功", r.get("ok") is True, str(r)[:60])
check("内容确实变了", (by_id("L1", lid) or {}).get("text") != before_text)
check("改写把旧版本留痕进了 L5",
      any(x.get("reason") == "revise" and x.get("revised_of") == lid for x in mem.l5))
u = mem.undo()
check("撤销的是这次改写（不是更早的删除/合并）", "改写" in u, u)
check("内容回到改写前", (by_id("L1", lid) or {}).get("text") == before_text,
      (by_id("L1", lid) or {}).get("text", "")[:40])
check("撤销不动冷存：改写前的旧版本仍留在 L5（只增不真删）",
      any(x.get("reason") == "revise" and x.get("revised_of") == lid for x in mem.l5))

print("\n2. R3：撤销不碰 L5（冷存只增不真删）")
mem.write("L3", "一条待删的例证", tag="待删例证（改写撤销）")
tid = mem.l3[-1]["id"]
n_l5 = len(mem.l5)
mem.delete_many("L3", [tid], reason="test")
check("删掉后归档进了 L5", len(mem.l5) == n_l5 + 1, f"{n_l5} → {len(mem.l5)}")
# 快照之后往 L5 追加一条完全无关的（模拟 meta_log 超限滚动、或别处的改写留痕）。
# 旧行为撤销时按 len(L5) 一刀切，这条会被连带删掉；现在撤销压根不碰 L5。
mem.cold_append("这是快照之后追加的无关冷存条目", typ="short_term")
n_l5_2 = len(mem.l5)
mem.undo()
check("撤销把删掉的 L3 捞回来了", any(x["id"] == tid for x in mem.l3))
check("L5 一条都没少（冷存只增不真删）", len(mem.l5) == n_l5_2, f"{n_l5_2} → {len(mem.l5)}")
check("无关的冷存条目还在（没被按长度一刀切误删）",
      any("无关冷存条目" in (x.get("text") or "") for x in mem.l5))

print("\n3. R20：合并 L1 后撤销 → 名下的 L3 归属一起恢复")
mem.write("L1", "条件→合并撤销测试；结论→这是框架", category="value", tag="合并撤销（价值观）")
l1_id = mem.l1[-1]["id"]
head = "合并撤销"
mem.write("L3", "例证甲，印证这条框架", tag="例证甲（合并撤销）")
mem.write("L3", "例证乙，印证这条框架", tag="例证乙（合并撤销）")
l3_ids = [x["id"] for x in mem.l3 if mem.parent_of(x) == head]
check("名下挂了两条 L3", len(l3_ids) == 2, str(l3_ids))
parents_before = {x["id"]: mem.parent_of(x) for x in mem.l3 if x["id"] in l3_ids}
res = mem.merge("L1", [l1_id], "条件→合并后；结论→新框架",
                category="value", tag="合并撤销（价值观）")
check("合并成功", res.get("written") is True, str(res)[:80])
after = {x["id"]: mem.parent_of(x) for x in mem.l3 if x["id"] in l3_ids}
check("合并确实清掉了名下 L3 的归属（联动生效，测试前提成立）",
      all(v != head for v in after.values()), str(after))
u = mem.undo()
check("撤销的是这次合并", "合并" in u, u)
restored = {x["id"]: mem.parent_of(x) for x in mem.l3 if x["id"] in l3_ids}
check("撤销后 L3 归属恢复（L3 进了撤销快照）", restored == parents_before, str(restored))

print("\n4. R5：ID 水位线只涨不回退，删掉最大号也不复用")
mem.write("L2", "草稿甲，用来占一个号", category="value", tag="号测试甲（价值观）")
id_a = mem.l2[-1]["id"]
mem.delete_many("L2", [id_a], reason="test")
mem.write("L2", "草稿乙，删了甲之后再写一条", category="value", tag="号测试乙（价值观）")
id_b = mem.l2[-1]["id"]
check("删掉最大号后再写，拿到的是新号（没复用）", id_b != id_a, f"{id_a} → {id_b}")
# 水位线得能跨重启：重新加载一次再写，号不能退回去
mem.save()
mem2 = Memory(cfg)
mem2.write("L2", "草稿丙，重启之后再写一条", category="value", tag="号测试丙（价值观）")
id_c = mem2.l2[-1]["id"]
check("重启后水位线还在，号不回退", id_c not in (id_a, id_b), f"{id_b} → {id_c}")

print()
if FAILS:
    print(f"[冒烟失败] {len(FAILS)} 项：{FAILS}")
    sys.exit(1)
print("[冒烟通过] 撤销体系 + ID 水位线全部通过")
