"""
冒烟：文字冒险的终局呈现 + 存档清理（2026-09-11，修 air 报告的两处「假断线」）。

背景（outbox #75/#77）：走到结局只回故事文本、没有任何「结束了」的标记，她以为
故事断了；结束后再 choose 报「没有进行中的冒险」，她以为进度丢了；start_adventure
会照续已通关的死档；save() 每步落一个新文件只增不删（adventures 攒了 23 个）。

跑：.venv\\Scripts\\python.exe _smoke_adventure.py
用独立 data_test_adventure 目录，不碰真实 data/。

覆盖：
  1. start_adventure 无存档 → 开新局
  2. 推进未到结局 → 正常返回可选项
  3. 走到结局 → 明确标注「冒险到此结束」+ 结局文案，内存局被清、本局存档被清
  4. 结束后再 choose：内存已清 → 报「没有进行中的冒险」（准确）；
     若内存里还挂着已终局的局 → 报「已经结束了」（不再误导）
  5. 最新存档已通关（跨重启场景）→ 开新局，不续死档，死档顺手清掉
  6. 在途存档跨重启 → 续上
  7. 清理只动本局：别的剧本的存档不受影响
"""
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent
TEST = ROOT / "data_test_adventure"
if TEST.exists():
    shutil.rmtree(TEST)
TEST.mkdir(parents=True)
os.environ["AIR2_DATA_ROOT"] = str(TEST)
sys.path.insert(0, str(ROOT))

from config import Config
from core.agent import Agent
from core import text_adventure as T

FAILS = []


def check(name, ok, detail=""):
    print(("  [OK] " if ok else "  [FAIL] ") + name + ((" — " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


cfg = Config()
cfg.data_root = TEST
agent = Agent(cfg, None)
tb = agent.tools


def save_files():
    d = TEST / "adventures"
    return sorted(d.glob("*.json")) if d.exists() else []


def ended_saves(scenario_id):
    """落盘且状态为已结束的存档文件名（用来验证死档被清掉）。"""
    out = []
    for f in save_files():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("scenario_id") == scenario_id and (d.get("state") or {}).get("ended"):
            out.append(f.name)
    return out


print("\n1. start_adventure 无存档 → 开新局")
out = tb.start_adventure("misty_forest")
check("翻开新剧本", "翻开剧本《迷雾森林》" in out, out[:60])
check("给出了可选项", "可选项" in out)

print("\n2. 推进未到结局 → 正常返回可选项")
out = tb.adventure_choose("走中间（直接）")
check("推进成功", "可选项" in out and "空地中央" in out, out[:80])
check("存了档", len(save_files()) == 1, str([f.name for f in save_files()]))

# 造一个同剧本「分叉在途」的存档（走左边的路），验证清理不误删别的在途局
eng2 = T.AdventureEngine(T.get_scenario("misty_forest"))
eng2.choose("走左边（谨慎）")
eng2.save()
before = len(save_files())

print("\n3. 走到结局 → 明确标注结束 + 清内存 + 清本局存档")
tb.adventure_choose("看水中的自己")
tb.adventure_choose("记住这感觉")
out = tb.adventure_choose("朝光走去")   # → lighthouse（结局）
check("返回里明确标注「冒险到此结束」", "冒险到此结束" in out, out[-120:])
check("带结局文案", "灯塔" in out, out[-80:])
check("带本局路径回顾（她能回看自己怎么选的）",
      "这一局你走的是" in out and "走中间（直接）" in out, out[-160:])
check("提示怎么再玩", "start_adventure" in out)
check("内存里的局已清", getattr(agent, "_adventure", None) is None)
left = save_files()
check("本局的存档被清理", len(left) == before - 1,
      f"{before} → {len(left)}: {[f.name for f in left]}")

print("\n4. 结束后再 choose 的两种情形")
try:
    tb.adventure_choose("1")
    check("应抛 ToolError", False)
except Exception as e:
    check("内存已清时报「没有进行中的冒险」（现在是准确描述）",
          "没有进行中的冒险" in str(e), str(e)[:80])
dead = T.AdventureEngine(T.get_scenario("misty_forest"))
for step in ("走中间（直接）", "看水中的自己", "记住这感觉", "朝光走去"):
    dead.choose(step)
agent._adventure = dead   # 模拟旧路径：内存里还挂着已终局的局
try:
    tb.adventure_choose("1")
    check("应抛 ToolError", False)
except Exception as e:
    check("挂着已终局的局时报「已经结束了」", "已经结束了" in str(e), str(e)[:80])
check("报错后内存局被清", getattr(agent, "_adventure", None) is None)

print("\n5. 最新存档已通关（跨重启场景）→ 开新局，不续死档")
eng3 = T.AdventureEngine(T.get_scenario("misty_forest"))
for step in ("走中间（直接）", "看水中的自己", "记住这感觉", "朝光走去"):
    eng3.choose(step)
eng3.save()
check("死档已落盘", any("misty_forest" in f.name for f in save_files()))
out = tb.start_adventure("misty_forest")
check("开新局而非续死档", "翻开剧本" in out and "冒险已到尽头" not in out, out[:60])
check("死档被顺手清掉", not ended_saves("misty_forest"), str(ended_saves("misty_forest")))

print("\n6. 在途存档跨重启 → 续上")
tb.adventure_choose("走左边（谨慎）")
agent._adventure = None   # 模拟重启：内存局丢掉
out = tb.start_adventure("misty_forest")
check("续上存档", "续上存档" in out, out[:60])

print("\n7. 清理只动本局：别的剧本的存档不受影响")
eng4 = T.AdventureEngine(T.get_scenario("lighthouse_night"))
eng4.choose("添油，守着灯")
eng4.save()
out = tb.adventure_choose("慢慢靠近狐狸")
out = tb.adventure_choose("问灯在哪里")
out = tb.adventure_choose("原地等雾散")   # → lost（结局）
check("这一局也标注结束", "冒险到此结束" in out)
after = save_files()
check("lighthouse_night 的存档还在", any("lighthouse_night" in f.name for f in after),
      str([f.name for f in after]))
check("目录不再堆积（只剩别的剧本的在途档）", len(after) == 1,
      f"剩 {len(after)} 个：{[f.name for f in after]}")

print("\n8. 全部剧本结构校验（含新写的 3 个）")
bad = []
for sid, sc in T.SCENARIOS.items():
    if sc.entry not in sc.nodes:
        bad.append(f"{sid}: entry {sc.entry} 不存在")
    for n, node in sc.nodes.items():
        for ch in node.get("choices", []):
            if ch.get("to") not in sc.nodes:
                bad.append(f"{sid}: {n} → {ch.get('to')} 不存在")
        if not node.get("end") and not node.get("choices"):
            bad.append(f"{sid}: {n} 是死路（非结局又无选项）")
        if node.get("end") and n not in sc.endings:
            bad.append(f"{sid}: 结局 {n} 缺结局文案")
    # 宽松可达性（忽略 req）：所有节点都应能从 entry 走到，别留孤儿
    seen, stack = {sc.entry}, [sc.entry]
    while stack:
        n = stack.pop()
        for ch in sc.nodes[n].get("choices", []):
            if ch.get("to") not in seen:
                seen.add(ch["to"])
                stack.append(ch["to"])
    unreach = [n for n in sc.nodes if n not in seen]
    if unreach:
        bad.append(f"{sid}: 不可达节点 {unreach}")
    # 变量卫生：init_vars 里每个变量都得有读方（req 条件或结局模板），只在 effects 里写、
    # 没人读 = 假机制（改了也没人用，纯冗余）。宁可当场报出来，也别让它烂在剧本里。
    for v in sc.init_vars:
        pat = re.compile(r"\{" + re.escape(v) + r"[}:!]")
        read_by_req = any(v in (ch.get("req") or {})
                          for node in sc.nodes.values() for ch in node.get("choices", []))
        read_by_end = any(pat.search(tpl) for tpl in sc.endings.values())
        if not (read_by_req or read_by_end):
            bad.append(f"{sid}: 变量 {v} 只写不读（假机制）")
check(f"{len(T.SCENARIOS)} 个剧本结构完好（选项可达/无死路/结局有文案）", not bad,
      "；".join(bad)[:200])
check("新写的 3 个剧本都在库内",
      all(k in T.SCENARIOS for k in ("midnight_store", "pirate_cat", "galaxy_delivery")))

print("\n9. 每个剧本随机通走 200 次：都要能走到结局")
stuck = []
for sid, sc in T.SCENARIOS.items():
    for _ in range(200):
        eng = T.AdventureEngine(sc)
        steps = 0
        while not eng.state.ended and steps < 60:
            ch = eng.auto_choose()
            if ch is None:
                break
            eng.choose(ch["label"])
            steps += 1
        if not eng.state.ended:
            stuck.append(f"{sid}({steps}步)")
            break
check("所有剧本都能走到结局（无死循环）", not stuck, "；".join(stuck))

print("\n10. 新剧本可被 list_adventures / start_adventure 打开")
out = tb.list_adventures()
check("list_adventures 列出新剧本",
      all(t in out for t in ("深夜便利店", "想当海盗的橘猫", "银河快递")), out[:140])
out = tb.start_adventure("midnight_store")
check("能翻开《深夜便利店》", "翻开剧本《深夜便利店》" in out, out[:60])
check("新剧本一局给出了可选项", "可选项" in out)

print("\n11. read_decisions 附判据（帮她识别流水账）")
agent.memory.write_decision("在整理结构和摄入新内容之间选了先摄入", "read_url", "取舍")
out = tb.read_decisions()
check("返回里带取舍判据", "真正的取舍" in out, out[-90:])
check("正常列出条目", "在整理结构和摄入新内容之间选了先摄入" in out)

shutil.rmtree(TEST, ignore_errors=True)
print()
if FAILS:
    print(f"[冒烟失败] {len(FAILS)} 项：{FAILS}")
    sys.exit(1)
print("[冒烟通过] 文字冒险终局呈现 + 存档清理 全部通过")
