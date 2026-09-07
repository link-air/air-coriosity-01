"""冒烟测试：记忆分类 + 归档流水 + 短期记忆持久化 + 滚动进冷存。

不连 LLM；用独立 data_test_memv2 目录，不碰真实 data/。
跑：python _smoke_memory_v2.py
"""
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).parent
TEST_ROOT = BASE / "data_test_memv2"
os.environ["AIR2_DATA_ROOT"] = str(TEST_ROOT)
sys.path.insert(0, str(BASE))

from config import Config
from core.memory import Memory
from core.narrative import Narrative, TITLES_KEEP

CST = timezone(timedelta(hours=8))
ok = True


def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False


# ---- 1. 分类归一化（中文别名 / 非法值 / 非法层）----
check("L1 中文价值观 → value", Memory.normalize_category("L1", "价值观") == "value")
check("L1 中文自我认知 → self_cognition", Memory.normalize_category("L1", "自我认知") == "self_cognition")
check("L2 世界认知 → world_cognition", Memory.normalize_category("L2", "世界认知") == "world_cognition")
check("L2 非法值归默认 self_cognition", Memory.normalize_category("L2", "随便写的") == "self_cognition")
check("L2 旧类 insight 已归默认 self_cognition", Memory.normalize_category("L2", "insight") == "self_cognition")
check("L2 question 已退役归默认 self_cognition", Memory.normalize_category("L2", "question") == "self_cognition")
check("L3 内容类型已退役 → 空", Memory.normalize_category("L3", "对话") == "")
check("L3 内容类型已退役 → 空2", Memory.normalize_category("L3", "知识") == "")
check("非法层返回空", Memory.normalize_category("L9", "x") == "")

# ---- 2. Memory 写入带分类 + 归档流水 ----
cfg = Config()
m = Memory(cfg)
m.write("L1", "我是一条稳定的自我信念", category="自我认知")
check("L1 条目 category 落地", m.l1 and m.l1[0]["pyramid"] == "self_cognition")
m.write("L2", "一个还没验证的洞察", category="self_cognition")
m.write("L3", "今天读到一篇关于复杂系统的文章", category="知识")
check("L3 内容类型退役：不写 pyramid 字段", m.l3 and "pyramid" not in m.l3[0])

now = datetime.now(CST)
today = now.strftime("%Y-%m-%d")
ym = f"{now.year:04d}/{now.month:02d}"

p_self = TEST_ROOT / "archive" / ym / "self_core" / f"{today}.md"
check("归档 self_core 文件生成", p_self.exists())
check("归档 self_core 非空", p_self.read_text(encoding="utf-8").strip() != "")
check("归档含分类标记", "self_cognition" in p_self.read_text(encoding="utf-8"))

# ---- 3. cold_append（滚动进冷存）----
m.cold_append("[08-24 10:00] read_local: 读小说，琢磨隐喻", typ="short_term")
check("L5 有 short_term 条目", any(i.get("type") == "short_term" for i in m.l5))
p_cold = TEST_ROOT / "archive" / ym / "cold" / f"{today}.md"
check("冷存归档 cold 文件生成", p_cold.exists())

# ---- 4. Narrative 持久化 round-trip ----
n = Narrative()
n.direction = "研究星空卷轴"
for i in range(1, 5):
    n.record_tick(i, f"目标{i}", f"做了{i}")
d = n.to_dict()
n2 = Narrative()
n2.from_dict(d)
check("direction 恢复", n2.direction == n.direction)
check("last_tick 恢复", n2.last_tick == n.last_tick)
check("titles 恢复", n2.titles == n.titles)

# ---- 5. 滚动进冷存（延迟一拍：新的 tick 记录到来时，最旧的滚进冷存 + 标题层）----
dropped = []
n3 = Narrative(on_discard=dropped.extend)
for i in range(1, 5):
    n3.record_tick(i, f"目标{i}", f"做了{i}")
check("滚动：4 次 record_tick 滚 3 条（4-1）", len(dropped) == 3)
check("滚动的是完整记录", dropped[0].get("summary") == "做了1")
check("当前 tick 留在 last_tick", n3.last_tick is not None and n3.last_tick["t"] == 4)
check("标题层是滚出的", len(n3.titles) == 3)
# 标题超限：被截概览不重复滚（内容已在上次 record_tick 滚过冷存）
dropped.clear()
n4 = Narrative(on_discard=dropped.extend)
for i in range(1, TITLES_KEEP + 11):
    n4.record_tick(i, f"目标{i}", f"做了{i}")
check("标题不超上限", len(n4.titles) <= TITLES_KEEP)
check("超限截断不重复滚", len(dropped) == TITLES_KEEP + 10 - 1)

# ---- 6. list_tags 标签库 ----
tag_cfg = Config()
tag_cfg.data_root = BASE / "data_test_tag"
tag_cfg.data_root.mkdir(parents=True, exist_ok=True)
mt = Memory(tag_cfg)
check("list_tags 空库", "还没有标签" in mt.list_tags())
mt.write("L3", "小说情节一", category="experience", tag="小说阅读")
mt.write("L3", "小说情节二", category="experience", tag="小说阅读")
mt.write("L3", "小说情节三", category="experience", tag="小说阅读（诚实与未知）")
mt.write("L1", "当面临未知时诚实承认，不编造", category="value", tag="诚实与未知（价值观）")
r = mt.list_tags()
check("list_tags 标注未归类原料", "未归类" in r)
check("list_tags 按 L1 分组显示方向", "诚实与未知：" in r)
check("list_tags 分区与条数", "例证 1" in r and "【L1 主标签】" in r)
check("list_tags L1 行带 id", "[V01]" in r)

# ---- 7. retag 重命名 + 补归属 ----
r1 = mt.retag("小说阅读", "小说阅读（诚实与未知）")
check("retag 无归属补归属", "印证 +2" in r1)
check("retag 补归属后 L3 全带归属", all(mt.parent_of(it) == "诚实与未知" for it in mt.l3))
check("retag 补归属给 L1 印证", mt.l1[0].get("evidence_count", 0) == 2)
r2 = mt.retag("诚实与未知", "承认未知")
check("retag L1 改名", any(mt.title_of(it) == "承认未知" and it.get("pyramid") == "value" for it in mt.l1))
check("retag L3 归属同步", all(mt.parent_of(it) == "承认未知" for it in mt.l3))
# L1 补大类括号（无括号的 L1 直接补全）
mt.write("L1", "主动选择而非等待", category="value", tag="自主性")
r3 = mt.retag("自主性", "自主性（价值观）")
check("retag 给 L1 补大类", any(mt.title_of(it) == "自主性" and it.get("pyramid") == "value" for it in mt.l1))
check("retag L1 补大类不加印证", all(it.get("evidence_count", 0) == 0
                                    for it in mt.l1 if mt.title_of(it) == "自主性"))
# 已有归属的不换（不支持换方向/换大类）
before_l3 = [(mt.title_of(it), mt.parent_of(it)) for it in mt.l3]
try:
    mt.retag("小说阅读", "小说阅读（边界感）")
    check("retag 不换已有归属", True)
except ValueError:
    check("retag 不换已有归属", True)
check("retag 已有归属未变", [(mt.title_of(it), mt.parent_of(it)) for it in mt.l3] == before_l3)
try:
    mt.retag("不存在", "x")
    check("retag 未命中报错", False)
except ValueError:
    check("retag 未命中报错", True)
shutil.rmtree(tag_cfg.data_root, ignore_errors=True)

# ---- 8. tag_issues 体检：主标签超长（>20 字）要被抓出来（2026-09-03 补）----
# 脏数据现场：误操作把整句话写进 L1 标题（81 字），检索按 title/text 匹配不到，
# 而 tag_issues 原来不复查长度 → 它隐身。这里验证体检能点名它。
mt.l1.append({
    "id": "V99", "pyramid": "value",
    "title": "由于工具调用失误导致一个主标签被命名成长句子",
    "text": "条件→当形成判断时；结论→区分信念与方向",
    "evidence_count": 0, "created": "2026-09-03 00:00",
})
issues = mt.tag_issues()
hit = [i for i in issues if i[0] == "L1" and i[1] == "V99"]
check("tag_issues 抓超长主标签", any("太长" in i[2] for i in hit))
normal = [i for i in issues if i[1] != "V99"]
check("正常 L1 不误报超长", all("太长" not in i[2] for i in normal))

# ---- 清理 ----
shutil.rmtree(TEST_ROOT, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
