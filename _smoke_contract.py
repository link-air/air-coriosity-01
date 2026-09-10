"""
冒烟：工具接口的契约（2026-09-06 加）。

跑：.venv\\Scripts\\python.exe _smoke_contract.py
用独立 data_test_contract 目录，不碰真实 data/。

这两个 bug 是同一类毛病：接口两端对不上，而且都只在**部分路径**上暴露——
静态检查抓不到，常规冒烟也走不到那条路上：

  1. write_memory 参数错位：_h_write_memory 按位置传 6 个实参，而形参是
     (layer, text, title, parent, pyramid, link, source)，第 5、6 位串了：
     link 列表落进 pyramid、source 落进 link。
     写 L1/L2 → 三维大类被静默吞掉（pyramid 拿到个列表，取值时空 → 恒归默认
     self_cognition）；写 L3 → 交叉标签变成 source 字符串、source 本身丢失。
     现在改成关键字传参，形参顺序再怎么调都不会错位。
  2. trace_memory 查 L3 必崩：memory._confirmed_by 返回键 content，而
     tools.trace_memory 读 c['text']。查 L1 走 _examples_of（键名本来就是 text）
     不受影响，所以这个坑一直没暴露。现在统一成 text。
"""
import io
import os
import shutil
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent
TEST = ROOT / "data_test_contract"
if TEST.exists():
    shutil.rmtree(TEST)
TEST.mkdir(parents=True)
os.environ["AIR2_DATA_ROOT"] = str(TEST)
sys.path.insert(0, str(ROOT))

from config import Config
from core.agent import Agent

FAILS = []


def check(name, ok, detail=""):
    print(("  [OK] " if ok else "  [FAIL] ") + name + ((" — " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


cfg = Config()
agent = Agent(cfg, None)   # 不连 LLM：只测参数传递和字段读取
mem = agent.memory

print("\n1. write_memory 的参数传递（按位置传曾把第 5、6 位串了）")
# 先建一条 L1，既当 L3 的归属，也当交叉标签的目标（link 只能连已存在的 L1 主标签）
mem.write("L1", "条件→契约测试；结论→用来挂 L3 和当交叉标签",
          category="value", tag="契约测试（价值观）")

# 写 L1：三维大类不能被吞（旧代码恒归默认 self_cognition）
ok, res = agent._execute({"action": "write_memory", "done": False, "layer": "L1",
                          "text": "条件→另一条框架；结论→三维大类不该被吞",
                          "title": "新框架", "pyramid": "世界认知"})
check("写 L1 成功", ok is True, res[:80])
if ok:
    check("L1 的三维大类没被吞（不是默认 self_cognition）",
          mem.l1[-1].get("pyramid") == "world_cognition", str(mem.l1[-1].get("pyramid")))

# 写 L3：交叉标签和 source 都不能丢
ok, res = agent._execute({"action": "write_memory", "done": False, "layer": "L3",
                          "text": "一条带交叉标签和信源的例证", "title": "契约例证",
                          "parent": "契约测试", "link": ["契约测试"],
                          "source": "某篇文章"})
check("写 L3 成功", ok is True, res[:80])
if ok:
    check("L3 的交叉标签没丢", "契约测试" in mem.links_of(mem.l3[-1]),
          str(mem.links_of(mem.l3[-1])))
    check("L3 的 source 没丢", mem.l3[-1].get("source") == "某篇文章",
          str(mem.l3[-1].get("source")))

print("\n2. trace_memory 查 L3 不崩（confirms 的键名曾对不上）")
if mem.l3:
    ok, res = agent._execute({"action": "trace_memory", "id": mem.l3[-1]["id"]})
    check("查 L3 不抛 KeyError", ok is True, res[:80])
    if ok:
        check("L3 能列出它印证的 L1", "它印证的 L1" in res, res[:120])
# 查 L1 走 examples 那条路，键名本来就是对的——回归保护，别改坏了
if mem.l1:
    ok, res = agent._execute({"action": "trace_memory", "id": mem.l1[0]["id"]})
    check("查 L1 正常", ok is True, res[:80])

print("\n3. search_memory 能按 ID 直达")
# 以前的 search 只比正文，而 id 不在正文里——她拿 suggest_cleanup 给的 id 来搜，
# 必然「没搜到」，据此判定数据坏了。现在命中 id 就直接返回那条。
mem.write("L3", "一条用来验证 ID 直达的例证", tag="ID直达（契约测试）")
tid = mem.l3[-1]["id"]
hits = mem.search(tid)
check("按 id 能搜到", len(hits) == 1 and hits[0]["id"] == tid, str(hits)[:80])
check("id 不在正文里也搜得到（这正是原 bug 的现场）", tid not in hits[0]["text"], tid)

print("\n4. revise 能补主标签 / 归属（修那批「有正文没名字」的条目）")
mem.write("L3", "一条有正文但没名字的历史条目")
orphan = mem.l3[-1]
mem.set_title(orphan, "")
mem.set_parent(orphan, "")
oid, otext = orphan["id"], orphan["text"]
check("复现脏数据：没名字也没归属",
      not mem.title_of(orphan) and not mem.parent_of(orphan))

r = mem.revise(oid, title="补上的名字")
check("只补名字 → 成功", r.get("ok") is True, str(r.get("err", ""))[:60])
now = next(x for x in mem.l3 if x["id"] == oid)
check("名字补上了", mem.title_of(now) == "补上的名字", mem.title_of(now))
check("正文没被动过", now["text"] == otext, now["text"][:40])

l1_ctg = next(x for x in mem.l1 if x.get("title") == "契约测试")
ev_before = l1_ctg.get("evidence_count", 0)
r = mem.revise(oid, parent="契约测试")
check("补归属 → 成功", r.get("ok") is True, str(r.get("err", ""))[:60])
now = next(x for x in mem.l3 if x["id"] == oid)
check("归属补上了", mem.parent_of(now) == "契约测试", mem.parent_of(now))
check("补归属给新归属的 L1 印证 +1",
      l1_ctg.get("evidence_count", 0) == ev_before + 1,
      f"{ev_before} → {l1_ctg.get('evidence_count', 0)}")

r = mem.revise(oid)
check("什么都不填 → 明确报错（不静默放过）",
      r.get("ok") is False and "至少填一个" in r.get("err", ""), str(r)[:80])

print("\n5. 只改标签时不起草正文（段B 不该替她重写）")
# agent 的 llm 是 None：真走了起草就会 AttributeError。能返回空串说明跳过了段B。
err = agent._compose_write({"action": "revise_memory", "done": False,
                            "id": oid, "title": "再改一次名"}, [])
check("只填 title → 不进起草", err == "", err)

shutil.rmtree(TEST, ignore_errors=True)
print()
if FAILS:
    print(f"[冒烟失败] {len(FAILS)} 项：{FAILS}")
    sys.exit(1)
print("[冒烟通过] 工具接口契约全部通过")
