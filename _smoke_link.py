"""冒烟验证：cross_link 连交叉边 = 结构新奇，记熵增（2026-09-02 从 organize 分出）。

原来 cross_link 归 organize，_settle 对 organize 统一记 record_produce(True)，
结果是「熵增记 0、熵减 +1」——和工具说明承诺的「连到另一座金字塔的熵增信号更强」
正好相反，结构新奇这条增长路径在 cross_link 上是断的。

关键回归点：连边只判刚连的那一条（_edge_novelty），不能用 _struct_novelty——
后者遍历整条记忆的全部交叉标签，会把该记忆已有的旧边重算一遍：
先连 A 再连 B，连 B 时 A 又被记一次，同一条边重复进账。
"""
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from core.memory import Memory
from core.drive import Drive
from core.tools import Toolbox
from core.agent import Agent

ok = True


def check(name, cond, detail=""):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name + ((" — " + detail) if detail else ""))
    if not cond:
        ok = False


def near(a, b, eps=0.001):
    return a is not None and abs(a - b) < eps


root = Path(__file__).parent / "data_test_link"
root.mkdir(exist_ok=True)
mf = root / "memory.json"
if mf.exists():
    mf.unlink()

cfg = Config()
cfg.data_root = root
memory = Memory(cfg)
drive = Drive(cfg, memory)
agent = SimpleNamespace(cfg=cfg, memory=memory, drive=drive)
agent.tools = Toolbox(agent)

# 两根价值观柱子 + 一根自我认知柱子，两条 L3 都挂在「边界感」下
memory.write("L1", "框架：边界感", "", "value", "边界感（价值观）", None)
memory.write("L1", "框架：分寸", "", "value", "分寸（价值观）", None)
memory.write("L1", "框架：好奇心", "", "self_cognition", "好奇心（自我认知）", None)
memory.write("L3", "例证一：具体内容一", "", "", "例一（边界感）", None)
memory.write("L3", "例证二：具体内容二", "", "", "例二（边界感）", None)

l3a, l3b = memory.l3[0], memory.l3[1]
l1a = memory.l1[0]
id_a, id_b, id_l1 = l3a["id"], l3b["id"], l1a["id"]
print(f"  id: L1={id_l1}  L3a={id_a}  L3b={id_b}")
print(f"  parent: L3a={memory.parent_of(l3a)}  L1a pyramid={l1a.get('pyramid')}")

# 1) 跨金字塔新边（边界感 → 好奇心）→ 0.7 × 0.8 = 0.56
print(" ", memory.link_memory(id_a, "好奇心"))
Agent._book_link(agent, {"id": id_a, "link": "好奇心"})
check("跨金字塔新边进熵增账", len(drive._explore) == 1)
check("跨金字塔新边 = 0.56", near(drive._explore[-1], 0.56))
check("不进熵减账", len(drive._produce) == 0)

# 2) 同金字塔新边（边界感 → 分寸，同属价值观）→ 0.5 × 0.8 = 0.40
print(" ", memory.link_memory(id_b, "分寸"))
Agent._book_link(agent, {"id": id_b, "link": "分寸"})
check("同金字塔新边进熵增账", len(drive._explore) == 2)
check("同金字塔新边 = 0.40", near(drive._explore[-1], 0.40))

# 3) 已连过的边不再计（边界感↔好奇心 已由 L3a 建过）
memory.link_memory(id_b, "好奇心")
Agent._book_link(agent, {"id": id_b, "link": "好奇心"})
check("重复边不进账", len(drive._explore) == 2)

# 4) 删边是收缩，不进账
memory.link_memory(id_b, "好奇心", remove=True)
Agent._book_link(agent, {"id": id_b, "link": "好奇心", "remove": "true"})
check("删边不进熵增账", len(drive._explore) == 2)

# 5) L1 加连边直接被拒（2026-09-05 命名治理：L1 的连边是没有消费方的假能力，砍掉）
res_l1 = ""
try:
    memory.link_memory(id_l1, "好奇心")
except ValueError as e:
    res_l1 = str(e)
check("L1 加连边被拒", "不支持连边" in res_l1, res_l1[:50])
Agent._book_link(agent, {"id": id_l1, "link": "好奇心"})
check("L1 连边不进账", len(drive._explore) == 2)

# 6) kind 已从 organize 分出来
check("link_memory 的 kind = link", agent.tools.kind("link_memory") == "link")
check("merge_memory 仍是 organize", agent.tools.kind("merge_memory") == "organize")
check("delete_memory 仍是 organize", agent.tools.kind("delete_memory") == "organize")

# 7) 失败路径必须判为失败（2026-09-03 修）
#    原来 memory 层失败是 return 文本，_execute 当成功 → 白记一笔结构新奇 + 连败检测不触发
n_e = len(drive._explore)
for title, act in (
        ("分支不存在", {"action": "link_memory", "id": id_a, "link": "不存在的分支"}),
        ("连自己的归属", {"action": "link_memory", "id": id_a, "link": "边界感"}),
        ("重复连已有的边", {"action": "link_memory", "id": id_a, "link": "好奇心"}),
        ("删一个不存在的交叉", {"action": "link_memory", "id": id_a, "link": "分寸",
                          "remove": "true"}),
        ("记忆不存在", {"action": "link_memory", "id": "V09-999", "link": "好奇心"}),
):
    okk, res = Agent._execute(agent, act)
    check(f"cross_link 失败（{title}）→ ok=False", okk is False, res[:50])
check("失败路径全程没进账", len(drive._explore) == n_e, str(drive._explore[n_e:]))

shutil.rmtree(root, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
