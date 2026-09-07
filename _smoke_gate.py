"""冒烟验证：查重闸（简化版门1）/ L1 相关常驻 / 分层字数上限 / 宪章完整注入。"""
import sys
import shutil
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from core.memory import Memory
from core.tools import Toolbox, ToolError
from core.agent import Agent

cfg = Config()
cfg.data_root = Path(__file__).parent / "data_test_gate"
if cfg.data_root.exists():
    shutil.rmtree(cfg.data_root)   # 清掉上一轮残留（脚本中途崩会留下脏 memory.json 污染重跑）
cfg.data_root.mkdir(parents=True, exist_ok=True)

ok = True
def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False

m = Memory(cfg)   # 无 embedding 服务 → 降级字符重叠

# ---- 分层字数上限 ----
m.write("L3", "长" * 1000)
check("L3 截断到 300", len(m.l3[-1]["text"]) <= 300)
m.write("L1", "长" * 1000)
check("L1 截断到 200", len(m.l1[-1]["text"]) <= 200)

m.write("L3", "今天读了一篇关于熵的文章")

# ---- write_memory 查重闸（2026-09-02 加）：相似 ≥90% 拒绝写入 ----
class FakeAgent:
    cfg = cfg
    memory = m
    drive = MagicMock()   # 熵增账桩（write_memory 会喂 v_new）
t = Toolbox(FakeAgent())
before = len(m.l3)
try:
    t.write_memory("L3", "今天读了一篇关于熵的文章")
    check("重复内容（100% 相似）被拒", False)
except ToolError as e:
    check("重复内容（100% 相似）被拒", "重复" in str(e))
check("被拒后 L3 没多写", len(m.l3) == before)
r3 = t.write_memory("L3", "一条全新的经历")
check("新内容写入附新奇值", r3.startswith("已写入") and "新奇" in r3)

# ---- 元认知：reflect 写自省 + 返回附同类历史 ----
r = t.reflect("根因：把框架当无条件规则，前提没校验", "框架失效")
check("reflect 写自省返回 id", r.startswith("已记下自省"))
try:
    t.reflect("")
    check("reflect 空内容抛 ToolError", False)
except ToolError:
    check("reflect 空内容抛 ToolError", True)
t.reflect("卡在 list_memory 反复调用", "卡点")
r3 = t.reflect("又一条框架失效", "框架失效")
check("reflect 返回分两段：还没想通的（带 id）", "还没想通的" in r3)
check("reflect 按类型过滤（只附同类型）", "把框架当无条件规则" in r3 and "卡在 list_memory" not in r3)

# ---- relevant_l1：无向量退化最近 top_k（mock embed_one 返回 None）----
from unittest.mock import MagicMock
m.emb.embed_one = MagicMock(return_value=None)
for i in range(15):
    m.write("L1", f"信念 {i}", category="world_cognition" if i < 10 else "value")
rel = m.relevant_l1("随便什么", top_k=12)
check("无向量返回最近 12 条", len(rel) == 12 and rel[-1]["text"] == "信念 14")

# ---- relevant_l1：有向量按相关性排序（mock embed_one）----
m.l1[-1]["emb"] = [1.0, 0.0]    # 信念 14 → 相关
m.l1[0]["emb"] = [0.0, 1.0]     # 信念 0 → 不相关
m.emb.embed_one = MagicMock(side_effect=lambda q: [0.99, 0.01])
rel2 = m.relevant_l1("上下文", top_k=12)
check("有向量按相关排序（最相关的排最前）", rel2[0]["text"] == "信念 14")

# ---- 宪章完整注入 system ----
a = Agent(cfg, None)
p = a._build_prompt()
check("宪章完整注入（含三部分）",
      "一、基石" in p[0]["content"] and "二、认知" in p[0]["content"] and "三、生长" in p[0]["content"])
check("宪章不含旧一句话版", "善意、诚实、边界感。" not in p[0]["content"])

# ---- 新奇值标注 ----
check("空库/无服务不标", t._novelty_tag(None) == "" and t._novelty_tag((1.0, None)) == "（新奇 100%）")

# ---- 作品集：write_code 落盘 + list/read；画图缺依赖降级 ----
class FL2:
    ok = True
    def chat(self, messages, think=None, max_tokens=None):
        return "print('hello air')"
class FA2:
    cfg = cfg
    memory = m
fa2 = FA2()
fa2.llm = FL2()
cfg.portfolio_dir = Path(cfg.data_root) / "作品集"   # 显式指到测试目录（Config.__init__ 里绑的是默认 data_root）
cfg.creations_dir = Path(cfg.data_root) / "creations"
t3 = Toolbox(fa2)
r = t3.write_code("打印 hello")
pf = Path(cfg.portfolio_dir) / "写代码"
check("write_code 落盘作品集", pf.exists() and len(list(pf.glob("*.md"))) >= 1)
check("list_portfolio 列出作品", "写代码" in t3.list_portfolio())
fname = list(pf.glob("*.md"))[0].stem
check("read_portfolio 读到内容", "print" in t3.read_portfolio(fname))
try:
    t3.read_portfolio("不存在的东西")
    check("read_portfolio 找不到抛 ToolError", False)
except ToolError:
    check("read_portfolio 找不到抛 ToolError", True)
# paint：mock 掉画图（不真出图——有 torch/diffusers 时会真画，慢且污染）
import core.image_gen as IG
IG.generate = MagicMock(return_value=None)   # 模拟缺依赖/权重 → 降级提示
try:
    t3.paint("一朵云")
    check("paint 缺依赖抛 ToolError", False)
except ToolError:
    check("paint 缺依赖抛 ToolError", True)
IG.generate = MagicMock(return_value="air_x.png")
check("paint 成功路径", "画好了" in t3.paint("一朵云"))
try:
    t3.paint("")
    check("paint 空描述抛 ToolError", False)
except ToolError:
    check("paint 空描述抛 ToolError", True)
ok2, _ = a._execute({"action": "list_portfolio"})
check("_execute 分发 list_portfolio（ok=True）", ok2 is True and "写代码" in _)
ok3, _ = a._execute({"action": "paint", "text": ""})
check("_execute paint 空描述 ok=False", ok3 is False)

shutil.rmtree(cfg.data_root, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
