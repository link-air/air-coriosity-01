"""
冒烟：L1 三维下限（2026-08-31 起）+ revise_memory 改写。

跑：.venv\\Scripts\\python.exe _smoke_revise.py
用独立 data_test_revise 目录，不碰真实 data/。

覆盖：
  1. L1 三维下限：某一维能超过下限（下限不是上限）
  2. 预留：别的维占不掉这一维的下限坑位 → 写到硬顶被拒 + deny_reason 说「位置不够了」
  3. revise 换内容，但 id / evidence_count / created / 反向引用全保留
  4. revise 计数 + 旧版本归档进 L5 留痕
  5. revise 不存在的 id → 报错，不误伤
  6. 形态 / 字数校验：L1 缺「条件→结论→」或超字数 → 拒（不静默截断）；L2/L3 只查字数
"""
import os
import shutil
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent
TEST = ROOT / "data_test_revise"
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

print("\n1. L1 三维下限：某一维能超过下限（下限不是上限）")
# 旧代码在 value 写到 10 条就拒（当上限）；现在 10 是下限，可以继续写
for i in range(11):
    mem.write("L1", f"value 第 {i+1} 条内容，验证下限不是上限", category="value")
n = sum(1 for it in mem.l1 if it.get("pyramid") == "value")
check("value 能写到 11 条（超过下限 10）", n == 11, f"实际 {n}")

print("\n2. 预留：别的维占不掉这一维的下限坑位")
# 单维硬顶 30 = 40 − 10（保 world_cognition 至少 10 条）。value 堆到 31 会挤掉 world 的下限
for i in range(11, 30):
    mem.write("L1", f"value 第 {i+1} 条", category="value")
n = sum(1 for it in mem.l1 if it.get("pyramid") == "value")
check("value 能写到 30 条", n == 30, f"实际 {n}")
check("value 第 31 条被拒（要保 world 至少 10）",
      mem.write("L1", "value 第 31 条（应被拒）", category="value") is False)
reason = mem.deny_reason("L1", "value")
check("deny_reason 说「位置不够了」而非「满了」", "位置不够" in reason, reason[:60])
check("deny_reason 指明要保 world_cognition 至少 10",
      "世界认知" in reason and "10" in reason, reason[:60])

print("\n3. revise 只换内容，其余全保留")
it = mem.l1[0]
mid = it.get("id")
it["evidence_count"] = 7          # 模拟已攒下的印证历史
created = it.get("created")
mem.save()
# 改写内容要守 L1 形态（2026-09-05 起改写也过 check_shape），所以这里按「条件→结论」写
NEW1 = "条件→原框架没写前提时；结论→改写时把前提条件补进来"
res = mem.revise(mid, NEW1)
check("revise 成功", res.get("ok") is True, str(res.get("err", "")))
new = [x for x in mem.l1 if x.get("id") == mid][0]
check("内容换了", new["text"] == NEW1, new["text"][:40])
check("id 没变", new.get("id") == mid)
check("evidence_count 保留（7）", new.get("evidence_count") == 7, str(new.get("evidence_count")))
check("created 保留", new.get("created") == created)
check("revised 计到 1", new.get("revised") == 1, str(new.get("revised")))

print("\n4. 再改一次：计数累加，旧版留痕")
NEW2 = "条件→第二次改写时；结论→再压一遍，把前提说清楚"
mem.revise(mid, NEW2)
new = [x for x in mem.l1 if x.get("id") == mid][0]
check("revised 计到 2", new.get("revised") == 2, str(new.get("revised")))
hits = [p for p in (TEST / "archive").rglob("*.md") if p.exists()]
blob = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in hits)
check("归档里有改写前的旧版", "改写前" in blob and "第1次" in blob,
      f"归档文件 {len(hits)} 个")

print("\n5. 改写不存在的 id 要报错，不误伤")
r = mem.revise("deadbeef", "随便改")
check("不存在的 id 返回 not ok", r.get("ok") is False, str(r))
check("返回里带原因", "没找到" in (r.get("err") or ""), r.get("err", ""))
r2 = mem.revise(mid, "   ")
check("空内容被拒", r2.get("ok") is False)

print("\n6. 形态 / 字数校验（2026-09-05 加）：写歪的 L1 进不来")
r = mem.revise(mid, "这次忘了写条件结论")
check("缺「条件→/结论→」的 L1 被拒", r.get("ok") is False, (r.get("err") or "")[:60])
check("报错指明缺了哪个标记", "条件→" in (r.get("err") or ""), (r.get("err") or "")[:60])
# 旧行为是静默截断（content[:LIMITS]），她以为写全了，落库的是半截句子
cap1 = mem.LIMITS["L1"]
long1 = "条件→" + "很长的条件" * 25 + "；结论→" + "很长的结论" * 25
r = mem.revise(mid, long1)
check(f"超 {cap1} 字的 L1 被拒（不静默截断）", r.get("ok") is False, f"{len(long1)} 字")
check("报错给出上限与超出量", "上限" in (r.get("err") or ""), (r.get("err") or "")[:60])
# L2/L3 是草稿与例证，不拿框架句的形态要求它们
check("L2 不要求框架句形态", mem.check_shape("L2", "一条草稿，没有条件结论标记") == "")
check("L3 不要求框架句形态", mem.check_shape("L3", "一条例证，没有条件结论标记") == "")
check("L1 合规内容放行", mem.check_shape("L1", "条件→当 X 时；结论→Y") == "")
# 拒了要真拒：原条目不能被改坏
cur = [x for x in mem.l1 if x.get("id") == mid][0]
check("被拒的改写没落库（原内容还在）", cur["text"] == NEW2, cur["text"][:30])

shutil.rmtree(TEST, ignore_errors=True)
print()
if FAILS:
    print(f"[冒烟失败] {len(FAILS)} 项：{FAILS}")
    sys.exit(1)
print("[冒烟通过] L1 三维下限 + revise 全部通过")
