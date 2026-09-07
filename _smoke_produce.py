"""
冒烟：产出记账（压缩事件 vs 堆料）+ 印证 + 结算反馈（领悟值 diff）。

跑：<项目根>/.venv/Scripts/python.exe _smoke_produce.py
用独立 data_test_produce 目录，不碰真实 data/。

新记账（《熵减重构设计思路.md》6.1 + 2026-09-01 修）：压缩动作 = 写 L1 / 写 L2 /
写 L3 且 tag 方向对得上某条 L1（印证）/ 改写 → 记 1；纯堆料 / 方向对不上 L1 → 记 0。
关键要验的：堆料记 0（不是不进分母）、方向对不上按堆料记 0（账本和真实印证一致）、
印证给 L1 加 evidence_count。

2026-09-03 反馈层改领悟值 diff（创建者定）：_settle 不再生成「印证/堆料/入账」等叙事文案，
只对比动作前后 learning_snapshot，哪个方向覆盖/精炼变了报数字。这里验证 diff 文案本身。
"""
import io
import os
import shutil
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent
TEST = ROOT / "data_test_produce"
if TEST.exists():
    shutil.rmtree(TEST)
TEST.mkdir(parents=True)
os.environ["AIR2_DATA_ROOT"] = str(TEST)
sys.path.insert(0, str(ROOT))

from config import Config
from core.agent import Agent

FAILS = []


def check(name, ok, detail=""):
    print(("  [OK] " if ok else "  [FAIL] ") + name + (" — " + detail if detail else ""))
    if not ok:
        FAILS.append(name)


cfg = Config()
agent = Agent(cfg, None)   # 不连 LLM：只测判定与记账
mem = agent.memory
drive = agent.drive


def rec(layer, direction=None, action="write_memory"):
    """跑一次产出记账（只喂账，文案已退役——反馈层改由 _settle 的 diff 生成）。

    direction 按层语义（2026-09-05 命名治理）：L3=parent（归属的 L1 名），L1/L2=pyramid（三维大类）。
    """
    a = {"action": action, "layer": layer}
    if direction:
        if layer == "L3":
            a["parent"] = direction
        else:
            a["pyramid"] = direction
    agent._book_produce(a)


# 先建一条 L1「边界感」，后面的 L3 印证方向才能对得上它
mem.write("L1", "边界感：尊重他人的独立，不越界不侵犯", category="value", tag="边界感（价值观）")

print("\n1. 写 L1 → 记 1（升维压缩）")
rec("L1", "价值观")
check("L1 记 1", drive._produce[-1] == 1.0, str(drive._produce[-1]))

print("\n2. 写 L3 且归属对得上 L1 → 记 1（印证）")
rec("L3", "边界感")
check("方向对上记 1", drive._produce[-1] == 1.0, str(drive._produce[-1]))

print("\n3. 写 L3 带归属但对不上任何 L1 → 记 0（堆料，账本不虚涨）")
rec("L3", "世界认知")
check("方向对不上记 0", drive._produce[-1] == 0.0, str(drive._produce[-1]))

print("\n4. 纯写 L3、不带归属 → 记 0（堆料）")
rec("L3")
check("不带方向记 0", drive._produce[-1] == 0.0, str(drive._produce[-1]))

print("\n5. 改写 → 记 1（修正框架）")
rec("L3", action="revise_memory")
check("改写记 1", drive._produce[-1] == 1.0, str(drive._produce[-1]))

print("\n6. 领悟值 = 窗口内压缩占比")
vals = list(drive._produce)
print(f"      produce 序列: {vals}")
# 前 5 次：1,1,0,0,1 → 占比 0.6
check("领悟值 = 0.6", abs(drive.insight() - 0.6) < 1e-9, str(drive.insight()))

print("\n7. tag 解析")
check("parse_tag 带方向", mem.parse_tag("尊重他人（边界感）") == ("尊重他人", "边界感"),
      str(mem.parse_tag("尊重他人（边界感）")))
check("parse_tag 无括号", mem.parse_tag("市场") == ("市场", ""), str(mem.parse_tag("市场")))

print("\n8. 印证：tag 方向匹配 L1 → evidence_count +1")
l1_before = [x for x in mem.l1 if mem.title_of(x) == "边界感"][0]
n_before = l1_before.get("evidence_count", 0)
ok = mem.bump_evidence_by_tag("边界感")
check("印证成功", ok is True)
check("evidence_count +1", l1_before.get("evidence_count") == n_before + 1,
      str(l1_before.get("evidence_count")))
check("方向匹配不到返回 False", mem.bump_evidence_by_tag("不存在的方向") is False)

print("\n9. 领悟值账本（drive.feel）：只报覆盖条数")
feel = drive.feel()
print(f"      熵增趋势={feel['熵增趋势']} 熵减趋势={feel['熵减趋势']}")
print(f"      领悟值={feel['领悟值']}")
check("领悟值账本含边界感方向", "边界感" in feel["领悟值"], feel["领悟值"][:80])
check("领悟值账本不报高度", "高度" not in feel["领悟值"])

print("\n10. 改写走 _settle（H1 修复：revise 分支不再是死代码）")
out = agent._settle({"action": "revise_memory", "id": "x"}, "已改写 x（…）", True)
check("_settle 分派 revise 记 1", drive._produce[-1] == 1.0, str(drive._produce[-1]))

print("\n11. promote 走 _settle：补记结构新奇 0.72（和 write_memory 写 L1 同口径）")
# 草稿在 L2（独立草稿本，不接入金字塔），升上去才是第一次在金字塔上建这个节点
mem.write("L2", "草稿：待验证的洞察", category="value", tag="待验证（价值观）")
draft_id = mem.l2[0]["id"]
n_explore = len(drive._explore)
out = agent._settle({"action": "promote", "id": draft_id},
                    f"已把草稿 [{draft_id}] 升为 L1 [V03]", True)
check("promote 记结构新奇 0.72",
      len(drive._explore) == n_explore + 1
      and abs(drive._explore[-1] - 0.72) < 1e-9,
      str(drive._explore[-1:]))
check("promote 同时记熵减 1（收进框架 = 压缩）",
      drive._produce[-1] == 1.0, str(drive._produce[-1]))

print("\n12. undo_memory 走 _settle：撤销是回滚，不记熵减")
n_produce = len(drive._produce)
out = agent._settle({"action": "undo_memory"}, "已撤销：删除 L3：1 个目标（…）", True)
check("撤销不进熵减账", len(drive._produce) == n_produce,
      str(drive._produce[-1:]))

print("\n13. promote 真跑一次（第 11 项是喂模拟文本，没走 memory.promote）")
# 草稿得长成框架句的样子才升得上去：promote 现在守 L1 的形态/字数/标签/重名
#（2026-09-06 补的入口校验，见 memory.promote）。以前除了配额什么都不查，
# L2 的 300 字草稿原样升上来，落成一堆既没框架形态又超长的 L1。
mem.write("L2", "条件→草稿想升成框架时；结论→得先长成框架句的样子",
          category="value", tag="待验证二（价值观）")
d2 = mem.l2[-1]["id"]
n_e2 = len(drive._explore)
ok, res = agent._execute({"action": "promote", "id": d2})
check("promote 成功 → ok=True", ok is True, res[:60])
check("草稿真的从 L2 移走了", all(x["id"] != d2 for x in mem.l2), d2)
out = agent._settle({"action": "promote", "id": d2}, res, ok)
check("成功才记结构新奇 0.72",
      len(drive._explore) == n_e2 + 1
      and abs(drive._explore[-1] - 0.72) < 1e-9,
      str(drive._explore[-1:]))

print("\n13b. promote 在入口拦住不合格的草稿（2026-09-06 补的校验）")
# 形态不合格：升上去就是一条没有框架的「框架」
mem.write("L2", "草稿三：一句话的感想，既没条件也没结论",
          category="value", tag="待验证三（价值观）")
d3 = mem.l2[-1]["id"]
ok3, res3 = agent._execute({"action": "promote", "id": d3})
check("形态不合格 → 被拒", ok3 is False, res3[:60])
check("被拒的草稿还留在 L2（没被搬走）", any(x["id"] == d3 for x in mem.l2), d3)
# 超长：L2 上限 300 字、L1 只有 200，原样升上去就是半截框架。
# 不静默截断——截断会留半截句子，她以后读不懂也改不动。
mem.write("L2", "条件→" + "很长的铺垫" * 40 + "；结论→超了",
          category="value", tag="待验证四（价值观）")
d4 = mem.l2[-1]["id"]
ok4, res4 = agent._execute({"action": "promote", "id": d4})
check("超 200 字 → 被拒（不静默截断）", ok4 is False, res4[:60])
# 重名：历史实证过——「能动性」「区分信念」「身份确认」各两条同名 L1，
# 全是草稿 promote 时没查重留下的，同名分支会把覆盖/精炼读数劈成两行。
mem.write("L2", "条件→跟已有 L1 撞名时；结论→会被同名拦下",
          category="value", tag="待验证二（价值观）")
d5 = mem.l2[-1]["id"]
ok5, res5 = agent._execute({"action": "promote", "id": d5})
check("主标签与已有 L1 重名 → 被拒", ok5 is False, res5[:60])

print("\n14. 工具软失败 → 判为失败，不记账（2026-09-03 修：返回值改抛 ValueError）")
n_e, n_p = len(drive._explore), len(drive._produce)
ok, res = agent._execute({"action": "promote", "id": "D99"})
check("promote 找不到草稿 → ok=False", ok is False, res[:60])
out = agent._settle({"action": "promote", "id": "D99"}, res, ok)
check("失败不记结构新奇", len(drive._explore) == n_e, str(drive._explore[n_e:]))
check("失败不记熵减", len(drive._produce) == n_p, str(drive._produce[n_p:]))
check("失败回显引导她排查", "没成" in out, out[-60:])

print("\n15. 同上：retarget / reclassify 失败也不记账")
n_p = len(drive._produce)
for title, act in (
        ("retarget 目标不存在", {"action": "retarget", "old": "边界感", "new": "不存在的方向"}),
        ("reclassify 大类非法", {"action": "reclassify", "id": "V01", "category": "瞎填的"}),
):
    ok, res = agent._execute(act)
    check(f"{title} → ok=False", ok is False, res[:50])
check("失败不记熵减", len(drive._produce) == n_p, str(drive._produce[n_p:]))

print("\n16. 结算反馈 = 领悟值 diff（动作前后各一帧，覆盖/精炼变了才报）")
# 真写两条 L3 印证「边界感」→ 覆盖 0→2，diff 应该点出方向 + 数字变化
before = drive.learning_snapshot()
mem.write("L3", "例证：给彼此留空间", source="", category="", tag="例1（边界感）", cross=None)
mem.write("L3", "例证：不窥探隐私", source="", category="", tag="例2（边界感）", cross=None)
after = drive.learning_snapshot()
diff = Agent._learning_diff(before, after)
print(f"      diff: {diff!r}")
check("diff 报「边界感」覆盖 0→2", "边界感" in diff and "0→2" in diff, diff)
check("只报变化的方向（没别的方向可写）", diff.count("→") == 1, diff)

print("\n17. diff 用 id 对齐：方向改名不算删了又建")
before = drive.learning_snapshot()
old = next(r for r in before if r["head"] == "边界感")
renamed = dict(old, head="分寸感")
diff = Agent._learning_diff([r if r["id"] != old["id"] else old for r in before],
                            [r if r["id"] != old["id"] else renamed for r in before])
check("改名 → 报「改名」", "改名" in diff and "移除" not in diff and "建立" not in diff, diff)

print("\n18. diff 空帧 → 空串（查询/读世界这类不动认知的动作没有结算尾巴）")
check("空对空返回空", Agent._learning_diff([], []) == "")
check("全没变化返回空", Agent._learning_diff(before, before) == "")

shutil.rmtree(TEST, ignore_errors=True)
print()
if FAILS:
    print(f"[冒烟失败] {len(FAILS)} 项：{FAILS}")
    sys.exit(1)
print("[冒烟通过] 压缩事件记账 + 印证 + 领悟值 diff 全部通过")
