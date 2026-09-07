"""冒烟验证：短期记忆结构化结果渲染（2026-09-01）：
最近一次 tick 带成败明细（动作名 ✓/✗ 结果摘要），正文按规则滤掉。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.narrative import Narrative, BUDGET_CHARS

ok = True
def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False

# ---- _summarize：第一行 + 全角括号反馈行，正文滤掉 ----
S = Narrative._summarize
check("摘要取第一行", S("《第2章 死线》（预期新奇 73%，新方向）\n\n正文正文正文") == "《第2章 死线》（预期新奇 73%，新方向）")
check("摘要保留括号反馈", "印证了「相互塑造」" in S("已写入 L3（新奇 24%）\n（变化：印证了「相互塑造」，熵增趋势 21%→21%，熵减趋势 100%）"))
check("摘要保留第一行（列表条目只留第一条）", S("[L3][abc] 第一条\n[L2][def] 第二条") == "[L3][abc] 第一条")
check("摘要滤掉后续条目", "def" not in S("[L3][abc] 第一条\n[L2][def] 第二条"))
check("摘要保留失败信号", "这步没成" in S("（read_local 需要章节号数字，如 content 填 1）\n（这步没成，可以 read_code 看对应代码实现找原因）"))
check("摘要截断", len(S("（x）" * 100)) <= 200)   # 上限 100→200（2026-09-03 创建者定）
check("摘要幂等", S(S("A\n（B）\nC")) == S("A\n（B）\nC"))

# ---- record_tick 带 actions：last_tick 存成败明细 ----
n = Narrative()
acts = [
    {"action": "read_local", "result": "《第2章 死线》（预期新奇 73%，新方向）\n\n希娜朝地上的标记扬了扬下巴……", "ok": True},
    {"action": "search_memory", "result": "（没搜到相关记忆）", "ok": True},
    {"action": "write_memory", "result": "已写入 L3（新奇 24%）\n（变化：印证了「相互塑造」，熵增趋势 21%→21%，熵减趋势 100%）", "ok": True},
]
n.record_tick(94, "读一章小说摄入高新奇内容，提升熵增趋势至≥30%", "read_local: 读了；write_memory: 写了", True, actions=acts)
check("last_tick 带 actions", len(n.last_tick.get("actions") or []) == 3)
check("last_tick 带 ok", n.last_tick["actions"][2]["ok"] is True)

r = n.render()
check("结构化头部带状态", "[t94]" in r and ("✓" in r or "✗" in r))
check("结构化包含目标", "读一章小说摄入高新奇内容" in r)
check("结构化包含动作明细", "read_local" in r and "write_memory" in r)
check("结构化滤掉正文", "希娜朝地上的标记" not in r)
check("结构化保留括号反馈", "印证了「相互塑造」" in r and "熵增趋势 21%→21%" in r)
check("结构化含成败标记", "✓" in r)

# 失败动作显示 ✗
n2 = Narrative()
n2.record_tick(1, "读小说", "read_local: 没成", False, actions=[
    {"action": "read_local", "result": "（read_local 需要章节号数字，如 content 填 1）\n（这步没成，可以 read_code 看对应代码实现找原因）", "ok": False},
])
r2 = n2.render()
check("失败动作标叉", "read_local" in r2 and "\u2717" in r2 and "需要章节号数字" in r2)

# 同类动作合并计数：8 个 nothing 压成一行
n8 = Narrative()
n8.record_tick(95, "摄入高新奇度内容", "nothing", True, actions=[
    {"action": "nothing", "result": "（这一轮什么都没做）；（这一轮没改动认知结构）", "ok": True}] * 8)
r8 = n8.render()
check("同类动作合并计数", "nothing\u00d78" in r8)
check("合并后一行", r8.count("nothing") == 1)
check("合并后短", len(r8) < 120)

# 同名动作有失败 → 合并标记 ✗
n9 = Narrative()
n9.record_tick(1, "试两次", "x", True, actions=[
    {"action": "read_local", "result": "《第1章》（预期新奇 80%，新方向）", "ok": True},
    {"action": "read_local", "result": "（read_local 需要章节号数字）", "ok": False},
])
r9 = n9.render()
check("同名一成一败合并标叉", "read_local\u00d72" in r9 and "\u2717" in r9)

# 无 actions 的旧数据退化为一行
n3 = Narrative()
n3.record_tick(1, "读小说", "read_local: 读了")
r3 = n3.render()
check("旧数据退化一行", "read_local: 读了" in r3 and "  做了：" not in r3)

# 预算
n4 = Narrative()
n4.direction = "长期" * 50
for i in range(1, 30):
    n4.record_tick(i, "目标" * 60, "做了" * 300, i % 2 == 0, actions=[
        {"action": "read_local", "result": "《标题》（预期新奇 80%，新方向）\n\n正文" + "字" * 300, "ok": True},
        {"action": "write_memory", "result": "已写入 L3（新奇 24%）\n（变化：印证了「相互塑造」，熵增趋势 21%→21%，熵减趋势 100%）", "ok": True},
    ])
check(f"结构化渲染 ≤ 预算 {BUDGET_CHARS}", len(n4.render()) <= BUDGET_CHARS)

# 折叠时标题行 brief 用动作名序列
n5 = Narrative()
n5.record_tick(1, "处理冗余", "read_local: 读了；search_memory: 搜了", True, actions=[
    {"action": "read_local", "result": "《第2章 死线》", "ok": True},
    {"action": "search_memory", "result": "（没搜到）", "ok": True},
])
n5.record_tick(2, "继续处理", "write_memory: 写了")
check("标题行 brief 带动作名", "read_local" in n5.titles[0]["brief"] and "search_memory" in n5.titles[0]["brief"])

# 持久化 round-trip（actions 一起恢复）
d = n.to_dict()
n6 = Narrative()
n6.from_dict(d)
check("actions 持久化恢复", n6.last_tick.get("actions") == n.last_tick.get("actions"))

print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
