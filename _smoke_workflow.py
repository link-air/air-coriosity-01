"""冒烟验证：工作流卫生机制（DSH 借鉴）——重复调用提醒 / 工具结果 head/tail 修剪 / 结构化概括。"""
import sys
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from core.agent import Agent
from core.narrative import Narrative

ok = True
def check(name, cond, detail=""):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        ok = False

# ---- 长期方向（direction，set_goal 覆盖式维护，跨 tick 保留）----
n6 = Narrative()
n6.direction = "把星空卷轴机制研究透"   # set_goal 写入 = 设字段
check("长期方向写入", n6.direction == "把星空卷轴机制研究透")
check("长期方向渲染", "【长期方向】把星空卷轴机制研究透" in n6.render())
n6.record_tick(2, "读第33章", "read_local: 读了")   # 普通 tick 不碰长期方向 → 保留
check("长期方向跨 tick 保留", n6.direction == "把星空卷轴机制研究透")
n6.direction = "换研究方向"   # set_goal 再写 = 覆盖
check("长期方向可更新", n6.direction == "换研究方向")
d = n6.to_dict()
n7 = Narrative()
n7.from_dict(d)
check("长期方向持久化恢复", n7.direction == "换研究方向")

# ---- merge_memory（合并记忆）----
from core.memory import Memory
from core.embedding import EmbeddingService
mc = Config()
mc.data_root = Path(__file__).parent / "data_test_merge"
mc.data_root.mkdir(parents=True, exist_ok=True)
mm = Memory(mc)
mm.emb = EmbeddingService("http://127.0.0.1:1", "x", "m")   # 连不上 = 降级，不影响合并主逻辑
mm.write("L3", "星空卷轴会随观测变化", category="knowledge")
mm.write("L3", "古画的星空区域每次看都不同", category="knowledge")
mm.write("L3", "卷轴与画是同一个谜团的两面", category="insight")
ids3 = [it["id"] for it in mm.l3]
r = mm.merge("L3", ids3[:2], "星空卷轴与古画的星空区域都会随观测变化，是同一谜团的两面")
check("merge 删 2 条", len(r["deleted"]) == 2)
check("merge 写成 1 条", r["written"] is True)
check("合并后 L3 剩 2 条", len(mm.l3) == 2)
check("空目标不写", mm.merge("L3", [], "x")["written"] is False)
check("不存在 id 不写", mm.merge("L3", ["nonexistent"], "x")["written"] is False)
check("非法层拒绝", mm.merge("L9", ["x"], "y")["deleted"] == [])
shutil.rmtree(mc.data_root, ignore_errors=True)

# ---- 整理记忆 = 领悟值（熵减）----
from core.drive import Drive
d8 = Drive(mc)
d8.record_produce(deepen=False)   # 开新方向（熵增）
check("领悟值基线 0", d8.insight() == 0.0)
d8.record_produce(deepen=True)    # 整理 = 领悟
check("整理涨领悟值 0.5", abs(d8.insight() - 0.5) < 1e-6)

# ---- 重复调用提醒 ----
cfg = Config()
cfg.data_root = Path(__file__).parent / "data_test_workflow"
cfg.data_root.mkdir(parents=True, exist_ok=True)
a = Agent(cfg, None)

# set_goal 工具：覆盖式写/清目标（action 正文字段 2026-09-05 起叫 text，见 render_schema）
a.tools._tools["set_goal"][1]({"text": "研究星空卷轴"})
check("set_goal 写目标", a.narrative.direction == "研究星空卷轴")
a.tools._tools["set_goal"][1]({"text": ""})
check("set_goal 空内容清目标", a.narrative.direction == "")

a._track_repeat({"action": "list_memory", "text": "L2"})
a._track_repeat({"action": "list_memory", "text": "L2"})
check("连续 2 次不提醒", a.repeat_note() == "")
a._track_repeat({"action": "list_memory", "text": "L2"})
check("连续 3 次提醒", "原地打转" in a.repeat_note())
check("同阈值不重复提醒", a.repeat_note() == "")

a._track_repeat({"action": "write_memory", "text": "一条新内容"})
check("换动作重置为 1", a._repeat_count == 1)
a._track_repeat({"action": "nothing"})
check("nothing 不计数", a._repeat_count == 1)

a._track_repeat({"action": "delete_memory", "layer": "L2", "text": "f63a3748"})
a._track_repeat({"action": "delete_memory", "layer": "L2", "text": "f63a3748"})
a._track_repeat({"action": "delete_memory", "layer": "L2", "text": "f63a3748"})
a.repeat_note()   # 消费第 3 次提醒
a._track_repeat({"action": "delete_memory", "layer": "L2", "text": "f63a3748"})
a._track_repeat({"action": "delete_memory", "layer": "L2", "text": "f63a3748"})
check("连续 5 次再次提醒", "原地打转" in a.repeat_note())

# 参数不同不算重复
a._track_repeat({"action": "delete_memory", "layer": "L2", "text": "另一个id"})
check("参数不同重置", a._repeat_count == 1)

# 用户来信重置
for _ in range(3):
    a._track_repeat({"action": "list_memory", "text": "L2"})
check("重置前计数到 3", a._repeat_count == 3)
a._handle_incoming(["随便一句话"])
check("用户来信重置计数", a._repeat_count == 0)

# ---- 思考链存量封顶（2026-09-05 第二层：老 reasoning 压摘要，留最近 keep 条完整）----
msgs2 = []
for i in range(5):
    msgs2.append({"role": "user", "content": f"x{i}"})
    msgs2.append({"role": "assistant", "content": '{"action":"nothing","done":false}',
                  "reasoning_content": "FULL" * 100})   # 每条思考链 400 字
Agent._trim_reasoning(msgs2, keep=3)
rcs = [m["reasoning_content"] for m in msgs2 if m.get("reasoning_content")]
check("5 条思考链字段都保留（压摘要不删字段）", len(rcs) == 5, f"实际 {len(rcs)}")
full = [r for r in rcs if len(r) > 300]      # 最新的 3 条（a2/a3/a4）完整
check("最近 3 条思考链保留完整", len(full) == 3, f"实际 {len(full)}")
short = [r for r in rcs if len(r) < 150]     # 最早的 2 条（a0/a1）压成摘要
check("更早的思考链压成一行摘要", len(short) == 2, f"实际 {len(short)}")
check("content/role 不受影响",
      all(m.get("role") in ("user", "assistant") and m.get("content") for m in msgs2))
m_nore = {"role": "user", "content": "没有思考链的消息"}
Agent._trim_reasoning([m_nore])
check("无 reasoning 的消息不动", "reasoning_content" not in m_nore)

shutil.rmtree(cfg.data_root, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
