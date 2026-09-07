import sys
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from core.memory import Memory

cfg = Config()
cfg.data_root = Path(__file__).parent / "data_test"
# 每次重建：不清理的话第二次跑会带着上次的 meta_log / L5 累积，
# 「自省字段完整」这类按下标本取第一条的断言必然失败（2026-09-05 补）。
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

# 元认知记录 meta_log
check("写自省返回 id", bool(m.write_meta_log("认知失调", "我想通了：诚实比自洽更重要")))
check("自省计入", len(m.meta_log) == 1)
check("自省字段完整", m.meta_log[0]["type"] == "认知失调" and m.meta_log[0]["status"] == "open")
check("非法 status 归 open", bool(m.write_meta_log("卡点", "又卡住了", "未知"))
      and m.meta_log[-1]["status"] == "open")

# 印证（tag 方向）：写 L1（主标签（三维分类））+ L3（主标签（归属的 L1 主标签）），tag 方向匹配 → evidence +1
m.write("L1", "我相信诚实比自洽更重要", tag="诚实（价值观）")
m.write("L3", "我相信诚实比自洽更重要", tag="某例证（诚实）")
l1 = m.l1[0]
check("写 L2/L3 不自动印证", l1["evidence_count"] == 0)
check("tag 方向命中 +1", m.bump_evidence_by_tag("诚实") is True and l1["evidence_count"] == 1)
check("方向匹配不到返回 False", m.bump_evidence_by_tag("不存在的方向") is False)

# 证据双向映射（tag 方向）：trace(L1) 反向列例证
m.write("L3", "今天遇到一件事印证了这条", tag="今天遇到（诚实）")
t1 = m.trace(l1["id"])
check("trace(L1) 反向列例证", bool(t1.get("examples"))
      and t1["examples"][-1]["text"].startswith("今天遇到"))

# delete_many 降级 L5（不真删）
l3 = m.l3[-1]
before_total = len(m.l1) + len(m.l2) + len(m.l3)
check("删除成功", m.delete_many("L3", [l3["id"]], reason="重复")["deleted"] == [l3["id"]])
check("L3 少一条", len(m.l1) + len(m.l2) + len(m.l3) == before_total - 1)
check("L5 多一条（归档不丢）", len(m.l5) == 1 and m.l5[0]["from_layer"] == "L3")

# trace
l1id = m.l1[0]["id"]
t = m.trace(l1id)
check("trace 命中", t["layer"] == "L1" and t["id"] == l1id)
check("trace 找不到返回空", m.trace("不存在") == {})

# counts 含新层（L4 攻略已删）
c = m.counts()
check("counts 含 L5 且无 L4", "L5" in c and "L4" not in c)

shutil.rmtree(cfg.data_root, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
